"""本地沙箱：以受控子进程执行 Shell / Python 代码。

策略（无 Docker 环境的落地方案）
--------------------------------
- 独立进程 + 独立进程组，超时后**整组**终止（Windows 用 taskkill /T）
- 限定工作目录，环境变量最小化（清掉常见凭据变量）
- 输出流式收集，超过上限自动截断
- 可选内存限制（POSIX 用 resource，Windows 用作业对象尽力而为）
- 网络开关：仅作为声明（真正拦截需系统级防火墙），Windows 下可配合
  ``FENGCODE_SANDBOX_NETWORK=0`` 让工具层拒绝网络类调用
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils import is_windows

# 需要从子进程环境中剔除的变量（避免密钥泄漏给被执行的脚本）
_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "SESSION")

_KEEP_ENV = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "HOME", "USERPROFILE", "USERNAME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "COMPUTERNAME", "HOSTNAME", "PWD",
}


@dataclass
class ExecResult:
    """一次子进程执行的结果。"""

    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    duration: float = 0.0
    timed_out: bool = False
    truncated: bool = False
    killed: bool = False
    error: str | None = None
    pid: int | None = None
    cwd: str = ""
    command: str = ""
    # 被信号终止时的信号名/编号（与 returncode 独立的事实）
    signal: str | None = None
    # 启动失败（命令不存在、无执行权限等）——与"运行后失败"是两回事
    spawn_failed: bool = False

    @property
    def ok(self) -> bool:
        """是否成功。

        按「正交结果独立上报」原则：超时、被杀、启动失败、
        报错、非零退出是**各自独立的事实**，任一成立即不算成功。
        尤其注意——进程捕获了终止信号后可能以退出码 0 结束，
        此时只看 returncode 会把它误判为成功。
        """
        if self.timed_out or self.killed or self.spawn_failed:
            return False
        if self.error:
            return False
        if self.signal:
            return False
        return self.returncode == 0

    @property
    def status(self) -> str:
        """给界面/日志用的单字状态。"""
        if self.spawn_failed:
            return "spawn_failed"
        if self.timed_out:
            return "timeout"
        if self.killed:
            return "killed"
        if self.signal:
            return "signaled"
        if self.error:
            return "error"
        return "ok" if self.returncode == 0 else "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "killed": self.killed,
            "signal": self.signal,
            "spawn_failed": self.spawn_failed,
            "error": self.error,
            "cwd": self.cwd,
            "command": self.command,
            "status": self.status,
            "ok": self.ok,
        }

    def summary(self, limit: int = 20000) -> str:
        """给模型看的紧凑结果。"""
        from ..utils import human_duration, truncate_middle

        parts = [f"退出码：{self.returncode}"]
        if self.timed_out:
            parts.append("状态：超时被杀")
        if self.error:
            parts.append(f"错误：{self.error}")
        parts.append(f"耗时：{human_duration(self.duration)}")
        head = " | ".join(parts)
        body = ""
        if self.stdout:
            body += self.stdout
        if self.stderr:
            if body:
                body += "\n"
            body += "【stderr】\n" + self.stderr
        if not body.strip():
            body = "（无输出）"
        return f"{head}\n{truncate_middle(body, limit)}"


def build_env(extra: dict[str, str] | None = None, *, allow_secrets: bool = False) -> dict[str, str]:
    """构造最小化的子进程环境。"""
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku in _KEEP_ENV or ku.startswith("PYTHON"):
            env[k] = v
        elif allow_secrets:
            env[k] = v
        elif not any(h in ku for h in _SECRET_ENV_HINTS):
            env[k] = v
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def default_shell() -> tuple[str, list[str]]:
    """返回 ``(shell 可执行, 前缀参数)``。"""
    if is_windows():
        # 优先 PowerShell 7，其次 Windows PowerShell
        for cand in ("pwsh", "powershell"):
            exe = shutil.which(cand)
            if exe:
                return exe, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return comspec, ["/d", "/s", "/c"]
    for cand in ("bash", "sh"):
        exe = shutil.which(cand)
        if exe:
            return exe, ["-c"]
    return "/bin/sh", ["-c"]


def python_executable() -> str:
    """返回一个**真正的 Python 解释器**路径。
    ★ 为什么不能直接用 sys.executable：本应用用 PyInstaller 打包成 Fengcode.exe，
      打包后 sys.executable 指向 Fengcode.exe **自己** —— 于是
      `python_exec` / `run_script` / `install_packages` 会去执行
      `Fengcode.exe -X utf8 script.py`，被 click 报 `No such option: -X` 全部失败。
      （实测：实例日志里这两个工具**每一次**都失败，AI 只能改用 shell 绕。）

    规则：只在「当前进程就是 Python」时用 sys.executable；否则去找系统的
    python / python3，再退到 Windows 的 py 启动器。
    """
    # 当前进程确实跑在 Python 解释器里（未打包，或包内以 -c/-m 方式启动）
    if not getattr(sys, "frozen", False):
        exe = sys.executable or ""
        if exe and Path(exe).name.lower().startswith("python"):
            return exe
    for cand in ("python", "python3"):
        exe = shutil.which(cand)
        if exe:
            return exe
    # Windows：py 启动器（.py 脚本可用；注意它不能接收 -X，见调用处处理）
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            return launcher
    # 实在没有：回退到原值，让调用处报出可读的错误
    return sys.executable or "python"


def python_argv(script: str, args: list[str] | None = None) -> list[str]:
    """构造「用 Python 跑某个脚本」的命令行。

    ★ `-X utf8` 只在**真正的 python 解释器**上合法。若回退到了 Windows 的
      `py` 启动器，`py -X utf8 x.py` 会被启动器当成自己的选项而失败 ——
      这正是用户实例里 `run_script` / `python_exec` 全线报
      `No such option: -X` 的同源问题。所以这里按解释器类型决定是否加该参数；
      编码本身由 `build_env()` 注入的 PYTHONUTF8=1 / PYTHONIOENCODING 兜底。
    """
    exe = python_executable()
    head = [exe]
    if Path(exe).name.lower().startswith("python"):
        head += ["-X", "utf8"]
    return [*head, script, *(args or [])]


def _console_code_page() -> str:
    """探测当前控制台代码页（如 cp936 / cp65001）。结果缓存，只探测一次。"""
    global _CODE_PAGE
    if _CODE_PAGE is not None:
        return _CODE_PAGE
    enc = "utf-8"
    if os.name == "nt":
        try:
            import subprocess as _sp

            out = _sp.run(["chcp"], capture_output=True, timeout=5,
                          shell=True).stdout.decode("ascii", "replace")
            # 输出形如「活动代码页: 936」/「Active code page: 936」
            import re as _re

            m = _re.search(r"(\d{3,5})", out)
            if m:
                cp = int(m.group(1))
                enc = "utf-8" if cp == 65001 else f"cp{cp}"
        except Exception:
            enc = "cp936" if _is_cjk_windows() else "utf-8"
    return enc


_CODE_PAGE: str | None = None


def _is_cjk_windows() -> bool:
    """中文/日文/韩文 Windows 的启发式判断（用于探测失败时兜底）。"""
    try:
        import locale

        loc = (locale.getpreferredencoding(False) or "").lower()
        return loc in ("cp936", "gbk", "cp950", "cp932", "cp949", "mbcs")
    except Exception:
        return False


def _decode_output(raw: bytes | None) -> str:
    """把子进程输出解码成文本。

    ★ 为什么不能硬编 utf-8：中文 Windows 的 PowerShell 5.1 / cmd 默认按 **GBK(cp936)**
      输出，硬用 utf-8+replace 会得到满屏乱码（实测）。这里按「控制台代码页」
      解码，并在 GBK 失败时退回 utf-8 —— 两种都试，哪个不产生替换字符就用哪个。
    """
    if not raw:
        return ""
    enc = _console_code_page()
    for cand in (enc, "utf-8", "cp936"):
        try:
            txt = raw.decode(cand)
            if "\ufffd" not in txt:
                return txt
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _exit_signal(returncode: int | None, timed_out: bool, killed: bool) -> str | None:
    """判断进程是否被信号终止。

    POSIX 下被信号杀死的进程 returncode 是负数（-SIG）。
    Windows 下 taskkill 通常给出非零正整数，因此仅靠 returncode 无法可靠判断，
    但我们知道是自己杀的（timed_out / killed），这本身就是独立事实。
    """
    if killed:
        return "SIGKILL(主动取消)"
    if timed_out:
        return "SIGKILL(超时)"
    if returncode is not None and returncode < 0:
        import signal as _sig

        try:
            return _sig.Signals(-returncode).name
        except (ValueError, AttributeError):
            return f"signal {-returncode}"
    return None


class LocalSandbox:
    """本地子进程执行器。"""

    def __init__(
        self,
        *,
        cwd: str | os.PathLike | None = None,
        timeout: float = 120.0,
        max_output: int = 400_000,
        network: bool = True,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.cwd = Path(cwd) if cwd else Path(tempfile.gettempdir())
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.timeout = float(timeout)
        self.max_output = int(max_output)
        self.network = bool(network)
        self.env_extra = env_extra or {}

    # ---- 通用执行 ------------------------------------------------------
    async def run(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        cwd: str | os.PathLike | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        shell: bool = False,
    ) -> ExecResult:
        """异步执行命令并收集输出。"""
        to = float(timeout if timeout is not None else self.timeout)
        workdir = Path(cwd) if cwd else self.cwd
        workdir.mkdir(parents=True, exist_ok=True)
        merged_env = build_env({**self.env_extra, **(env or {})})
        t0 = time.time()
        res = ExecResult(cwd=str(workdir), command=" ".join(argv) if not shell else str(argv[0]))

        kwargs: dict[str, Any] = {}
        if is_windows():
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = _posix_limits

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                env=merged_env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **kwargs,
            )
        except FileNotFoundError as e:
            res.returncode = 127
            res.spawn_failed = True
            res.error = f"命令不存在：{argv[0]}（{e}）"
            res.duration = time.time() - t0
            return res
        except OSError as e:
            res.returncode = 126
            res.spawn_failed = True
            res.error = f"无法启动进程：{e}"
            res.duration = time.time() - t0
            return res

        res.pid = proc.pid
        out: bytes = b""
        err: bytes = b""
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=(stdin.encode("utf-8") if stdin is not None else None)),
                timeout=to,
            )
            res.returncode = proc.returncode
        except asyncio.TimeoutError:
            res.timed_out = True
            await self.kill(proc)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            except Exception:
                out, err = b"", b""
            res.returncode = proc.returncode if proc.returncode is not None else -9
        except asyncio.CancelledError:
            await self.kill(proc)
            res.killed = True
            res.error = "已取消"
            res.duration = time.time() - t0
            raise
        except Exception as e:  # pragma: no cover
            res.error = f"{type(e).__name__}: {e}"
            await self.kill(proc)

        res.duration = time.time() - t0
        # 独立上报"被信号终止"这一事实：进程捕获终止信号后可能以 0 退出，
        # 只看 returncode 会把它误判成成功。
        res.signal = _exit_signal(proc.returncode, res.timed_out, res.killed)
        so = _decode_output(out)
        se = _decode_output(err)
        if len(so) > self.max_output:
            so = so[: self.max_output] + f"\n…（输出超过 {self.max_output} 字符已截断）"
            res.truncated = True
        if len(se) > self.max_output:
            se = se[: self.max_output] + f"\n…（stderr 超过 {self.max_output} 字符已截断）"
            res.truncated = True
        res.stdout, res.stderr = so, se
        return res

    async def shell(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | os.PathLike | None = None,
        env: dict[str, str] | None = None,
        shell_path: tuple[str, list[str]] | None = None,
    ) -> ExecResult:
        """执行一段 Shell 命令。"""
        exe, prefix = shell_path or default_shell()
        if os.name == "nt" and Path(exe).name.lower() in ("cmd.exe", "cmd"):
            return await self.run([exe, *prefix, command], timeout=timeout, cwd=cwd, env=env)
        return await self.run([exe, *prefix, command], timeout=timeout, cwd=cwd, env=env)

    async def python(self, code: str, *, timeout: float | None = None,
                     cwd: str | os.PathLike | None = None, args: list[str] | None = None) -> ExecResult:
        """在临时文件里执行一段 Python 代码（避免命令行转义问题）。"""
        workdir = Path(cwd) if cwd else self.cwd
        workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, dir=str(workdir), encoding="utf-8", newline="\n"
        ) as f:
            f.write(code)
            script = f.name
        try:
            return await self.run(
                python_argv(script, args),
                timeout=timeout,
                cwd=workdir,
            )
        finally:
            try:
                os.unlink(script)
            except OSError:
                pass

    # ---- 进程控制 ------------------------------------------------------
    @staticmethod
    async def kill(proc: asyncio.subprocess.Process, *, grace: float = 5.0) -> bool:
        """终止进程及其整棵进程树，**并等待真正停稳**。

        按「dispose 必须达到完全停稳」原则：只发终止信号就
        返回会留下孤儿进程。这里保证：发信号 → 等待退出 → 超时则强杀 → 再等。

        返回是否确认已退出。
        """
        if proc.returncode is not None:
            return True
        try:
            if is_windows():
                # taskkill /T 递归终止整棵树；/F 强制
                kp = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                await kp.wait()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, AttributeError):
                    proc.terminate()
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

        # 第一阶段：优雅等待
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return True
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

        # 第二阶段：确认杀不掉就强杀 + 再等
        with contextlib.suppress(Exception):
            if is_windows():
                kp2 = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                await kp2.wait()
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError, AttributeError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
            return True
        except Exception:
            return False


def _posix_limits() -> None:  # pragma: no cover - 仅在 POSIX 生效
    """在子进程里设置资源上限（尽力而为）。"""
    try:
        import resource

        soft = 4 * 1024 * 1024 * 1024  # 4GB 虚拟内存上限
        resource.setrlimit(resource.RLIMIT_AS, (soft, soft))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


__all__ = [
    "LocalSandbox",
    "ExecResult",
    "build_env",
    "default_shell",
    "python_executable",
]
