"""Shell 与代码执行工具、后台任务、进程管理。"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ...security.sandbox import ExecResult, LocalSandbox, default_shell, python_executable, _decode_output
from ...utils import human_duration, new_id, truncate_middle
from ..base import Tool, ToolContext, ToolResult


def _sandbox(ctx: ToolContext, *, timeout: float | None = None) -> LocalSandbox:
    cfg = ctx.config
    to = timeout
    if to is None:
        # ★ 默认超时取设置里的「命令最多跑多久」（sandbox.timeout_seconds）。
        #   早先读的是 tools.shell_timeout_seconds，而界面写的是 sandbox 那个 —— 改了没反应。
        to = float(
            getattr(getattr(cfg, "sandbox", None), "timeout_seconds", 0)
            or getattr(getattr(cfg, "tools", None), "shell_timeout_seconds", 120)
            or 120
        )
    max_out = int(getattr(getattr(cfg, "tools", None), "max_output_chars", 60_000) or 60_000)
    net = True
    bash_mode = "auto"
    try:
        net = bool(cfg.sandbox.network)
        bash_mode = str(getattr(cfg.sandbox, "bash", "") or "auto")
    except Exception:
        pass
    return LocalSandbox(
        cwd=ctx.workspace,
        timeout=to,
        max_output=max_out,
        network=net,
        bash_mode=bash_mode,
    )


class ShellTool(Tool):
    name = "shell"
    group = "执行"
    dangerous = True
    description = (
        "在工作区里执行一条 Shell 命令并返回输出。"
        "Windows 默认用 PowerShell，Linux/macOS 用 bash。"
        "命令会在受限子进程里运行（有超时、输出截断、进程树终止），危险命令需要用户批准。"
    )
    parameters = {
        "command": {"type": "string", "description": "要执行的命令"},
        "cwd": {"type": "string", "description": "工作目录（相对当前工作区），默认当前工作区"},
        "timeout": {"type": "number", "description": "超时秒数，默认取配置值"},
        "shell": {"type": "string", "description": "强制指定 shell 类型：powershell/cmd/bash/sh"},
    }
    required = ["command"]
    max_output_chars = 40_000

    async def run(self, ctx: ToolContext, command: str = "", cwd: str = "", timeout: float | None = None,
                  shell: str = "", **_: Any) -> ToolResult:
        if not command.strip():
            return ToolResult.fail("命令不能为空")
        # ★ 1-F：Windows 命令行上限 32767 字符，超了会报一句看不懂的系统错误。
        #   这里提前挡下，并明确告知「改用文件工具」——否则用户无从自救。
        if os.name == "nt" and len(command) > 32000:
            return ToolResult.fail(
                f"命令过长（{len(command)} 字符，Windows 上限约 32767）："
                "请改用 write_file / edit_file 直接写文件，或把内容先写进脚本再用 run_script 执行。"
            )
        from ...security.paths import resolve

        workdir = resolve(cwd or ".", workspace=ctx.workspace) if cwd else ctx.workspace
        if not workdir.exists():
            return ToolResult.fail(f"工作目录不存在：{workdir}")

        sb = _sandbox(ctx, timeout=timeout)
        shell_path = _pick_shell(shell)
        res = await sb.shell(command, cwd=workdir, shell_path=shell_path)
        return _exec_result(res, display=f"执行 {truncate_middle(command, 60)}")


class PythonTool(Tool):
    name = "python_exec"
    group = "执行"
    dangerous = True
    description = (
        "执行一段 Python 代码并返回输出（在临时文件里运行，避免转义问题）。"
        "适合做计算、数据处理、快速验证想法。可用 print 输出结果。"
        "注意：可 import 本机已安装的库（numpy/pandas 等）。"
    )
    parameters = {
        "code": {"type": "string", "description": "要执行的 Python 代码"},
        "cwd": {"type": "string", "description": "工作目录，默认当前工作区"},
        "timeout": {"type": "number", "description": "超时秒数，默认 120"},
    }
    required = ["code"]
    max_output_chars = 40_000

    async def run(self, ctx: ToolContext, code: str = "", cwd: str = "", timeout: float | None = None,
                  **_: Any) -> ToolResult:
        if not code.strip():
            return ToolResult.fail("代码不能为空")
        from ...security.paths import resolve

        workdir = resolve(cwd, workspace=ctx.workspace) if cwd else ctx.workspace
        sb = _sandbox(ctx, timeout=timeout or 120)
        res = await sb.python(code, cwd=workdir, timeout=timeout or 120)
        return _exec_result(res, display="执行 Python 代码")


class RunScriptTool(Tool):
    name = "run_script"
    group = "执行"
    dangerous = True
    description = "运行一个已存在的脚本文件（自动按扩展名选择解释器：.py/.sh/.ps1/.js/.bat）。"
    parameters = {
        "path": {"type": "string", "description": "脚本文件路径"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "传给脚本的参数"},
        "cwd": {"type": "string", "description": "工作目录"},
        "timeout": {"type": "number", "description": "超时秒数"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", args: list[str] | None = None,
                  cwd: str = "", timeout: float | None = None, **_: Any) -> ToolResult:
        p = ctx.guard.check_read(path)
        from ...security.paths import resolve

        workdir = resolve(cwd, workspace=ctx.workspace) if cwd else p.parent
        sb = _sandbox(ctx, timeout=timeout or 120)
        argv = _script_argv(p, args or [])
        if argv is None:
            return ToolResult.fail(f"不支持的脚本类型：{p.suffix}（支持 .py/.sh/.bash/.ps1/.js/.bat/.cmd）")
        res = await sb.run(argv, cwd=workdir, timeout=timeout or 120)
        return _exec_result(res, display=f"运行脚本 {p.name}")


def _script_argv(p: Path, args: list[str]) -> list[str] | None:
    ext = p.suffix.lower()
    if ext == ".py":
        from ...security.sandbox import python_argv

        return python_argv(str(p), args)
    if ext in (".sh", ".bash"):
        bash = shutil.which("bash") or "/bin/bash"
        return [bash, str(p), *args]
    if ext == ".ps1":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p), *args]
    if ext in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if not node:
            return None
        return [node, str(p), *args]
    if ext in (".bat", ".cmd"):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", str(p), *args]
    return None


class InstallPackagesTool(Tool):
    name = "install_packages"
    group = "执行"
    dangerous = True
    description = "安装依赖：pip/npm/cargo 包。会自动识别包管理器并执行安装。"
    parameters = {
        "packages": {"type": "array", "items": {"type": "string"}, "description": "包名列表"},
        "manager": {
            "type": "string",
            "enum": ["auto", "pip", "npm", "cargo", "apt", "winget"],
            "description": "包管理器，默认 auto 自动判断",
        },
        "upgrade": {"type": "boolean", "description": "是否升级到最新版"},
    }
    required = ["packages"]

    async def run(self, ctx: ToolContext, packages: list[str] | None = None, manager: str = "auto",
                  upgrade: bool = False, **_: Any) -> ToolResult:
        if not packages:
            return ToolResult.fail("packages 不能为空")
        mgr = manager or "auto"
        if mgr == "auto":
            mgr = _guess_manager(packages)
        cmd = _install_command(mgr, packages, upgrade)
        if not cmd:
            return ToolResult.fail(f"不支持的包管理器：{mgr}")
        sb = _sandbox(ctx, timeout=600)
        res = await sb.shell(cmd, cwd=ctx.workspace)
        return _exec_result(res, display=f"安装 {len(packages)} 个包（{mgr}）")


def _guess_manager(packages: list[str]) -> str:
    joined = " ".join(packages)
    if joined.startswith("@") or any("/" in p and not p.startswith(".") for p in packages):
        return "npm"
    if all("." not in p and p.islower() for p in packages) and any(
        p in ("requests", "numpy", "pandas", "flask", "django", "pillow") for p in packages
    ):
        return "pip"
    if shutil.which("pip") or shutil.which("python"):
        return "pip"
    return "npm"


def _install_command(mgr: str, packages: list[str], upgrade: bool) -> str | None:
    pkgs = " ".join(packages)
    if mgr == "pip":
        flag = " --upgrade" if upgrade else ""
        return f'"{python_executable()}" -m pip install{flag} {pkgs}'
    if mgr == "npm":
        flag = " update" if upgrade else " install"
        return f"npm{flag} {pkgs}"
    if mgr == "cargo":
        return f"cargo install {pkgs}"
    if mgr == "apt":
        return f"apt-get install -y {pkgs}"
    if mgr == "winget":
        return f"winget install {pkgs}"
    return None


# ---- 后台任务 ------------------------------------------------------------

class BackgroundJobManager:
    """后台任务管理：启动、查询、终止、取输出。"""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def start(self, command: str, *, cwd: Path, shell_path=None, name: str = "") -> dict[str, Any]:
        jid = new_id("job")
        exe, prefix = shell_path or default_shell()
        from ...security.sandbox import build_env

        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            exe, *prefix, command,
            cwd=str(cwd), env=build_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            **kwargs,
        )
        out_path = cwd / f".fengcode-job-{jid}.log"
        job = {
            "id": jid,
            "name": name or command[:60],
            "command": command,
            "cwd": str(cwd),
            "pid": proc.pid,
            "started": time.time(),
            "status": "running",
            "output_file": str(out_path),
            "lines": [],
            "returncode": None,
            # ★ 1-M：最后一次有新输出的时刻，用于判断「静默但仍在跑」而不是「卡死」
            "last_output_at": time.time(),
        }
        self.jobs[jid] = job
        self._procs[jid] = proc
        asyncio.create_task(self._pump(jid, proc))
        return job

    async def _pump(self, jid: str, proc: asyncio.subprocess.Process) -> None:
        job = self.jobs.get(jid)
        if not job:
            return
        chunks: list[str] = []
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = _decode_output(line)
                chunks.append(text)
                job["lines"].append(text.rstrip("\n"))
                job["last_output_at"] = time.time()
                if len(job["lines"]) > 4000:
                    del job["lines"][:2000]
                if len(chunks) > 800:
                    try:
                        with open(job["output_file"], "a", encoding="utf-8") as f:
                            f.writelines(chunks)
                        chunks = []
                    except OSError:
                        pass
        except Exception:
            pass
        rc = await proc.wait()
        try:
            with open(job["output_file"], "a", encoding="utf-8") as f:
                f.writelines(chunks)
        except OSError:
            pass
        job["returncode"] = rc
        job["status"] = "done" if rc == 0 else "failed"
        job["ended"] = time.time()

    async def kill(self, jid: str) -> bool:
        proc = self._procs.get(jid)
        if not proc or proc.returncode is not None:
            return False
        await LocalSandbox.kill(proc)
        job = self.jobs.get(jid)
        if job:
            job["status"] = "killed"
        return True

    def get(self, jid: str) -> dict[str, Any] | None:
        return self.jobs.get(jid)

    def list(self) -> list[dict[str, Any]]:
        return sorted(self.jobs.values(), key=lambda j: j["started"], reverse=True)

    def tail(self, jid: str, n: int = 200) -> list[str]:
        job = self.jobs.get(jid)
        if not job:
            return []
        return job["lines"][-n:]

    def cleanup(self, older_than: float = 3600) -> int:
        now = time.time()
        dead = [
            jid for jid, j in self.jobs.items()
            if j.get("status") not in ("running",) and now - j.get("ended", j["started"]) > older_than
        ]
        for jid in dead:
            self.jobs.pop(jid, None)
            self._procs.pop(jid, None)
        return len(dead)


_job_manager: BackgroundJobManager | None = None


def get_job_manager() -> BackgroundJobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = BackgroundJobManager()
    return _job_manager


class BackgroundJobTool(Tool):
    name = "background_job"
    group = "执行"
    dangerous = True
    description = (
        "管理后台长任务：start 启动、status 查询状态与输出、kill 终止、list 列出全部。"
        "适合启动服务器、长时构建、watch 进程等不会自己结束的命令。"
    )
    parameters = {
        "action": {"type": "string", "enum": ["start", "status", "kill", "list", "check"], "description": "操作类型"},
        "command": {"type": "string", "description": "start 时要执行的命令"},
        "job_id": {"type": "string", "description": "status/kill 时的任务 ID"},
        "cwd": {"type": "string", "description": "工作目录"},
        "name": {"type": "string", "description": "任务别名"},
        "tail": {"type": "integer", "description": "status 时返回末尾多少行，默认 80"},
        "wait_seconds": {"type": "number", "description": "check 时最多等待多少秒"},
        "pattern": {"type": "string", "description": "check 时等待输出中出现该文本"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", command: str = "", job_id: str = "",
                  cwd: str = "", name: str = "", tail: int = 80, wait_seconds: float = 0,
                  pattern: str = "", **_: Any) -> ToolResult:
        from ...security.paths import resolve

        mgr = get_job_manager()
        action = (action or "").lower()

        if action == "start":
            if not command:
                return ToolResult.fail("start 需要提供 command")
            workdir = resolve(cwd or ".", workspace=ctx.workspace)
            job = await mgr.start(command, cwd=workdir, name=name)
            return ToolResult(
                content=f"已在后台启动任务 {job['id']}（PID {job['pid']}）：\n{command}\n"
                        f"用 background_job(action='status', job_id='{job['id']}') 查看输出。",
                display=f"后台任务 {job['id']} 已启动",
                data=job,
            )

        if action == "list":
            jobs = mgr.list()
            if not jobs:
                return ToolResult.text("当前没有后台任务。")
            lines = [f"共 {len(jobs)} 个后台任务："]
            for j in jobs[:20]:
                dur = human_duration((j.get("ended") or time.time()) - j["started"])
                lines.append(f"  {j['id']}  [{j['status']}]  {dur}  {j['name'][:60]}")
            return ToolResult.text("\n".join(lines), data={"jobs": jobs[:20]})

        if not job_id:
            return ToolResult.fail(f"{action} 需要提供 job_id")
        job = mgr.get(job_id)
        if not job:
            return ToolResult.fail(f"未找到后台任务：{job_id}")

        if action == "kill":
            ok = await mgr.kill(job_id)
            return ToolResult.text(f"{'已终止' if ok else '任务已结束，无需终止'}：{job_id}")

        if action in ("status", "check"):
            if action == "check" and pattern and wait_seconds > 0:
                deadline = time.time() + float(wait_seconds)
                while time.time() < deadline:
                    if any(pattern in ln for ln in job["lines"]):
                        break
                    if job["status"] != "running":
                        break
                    await asyncio.sleep(0.5)
            lines = mgr.tail(job_id, int(tail or 80))
            dur = human_duration((job.get("ended") or time.time()) - job["started"])
            head = f"任务 {job_id}：[{job['status']}] 运行 {dur}，PID {job['pid']}"
            # ★ 1-M：静默 ≠ 卡死。没有新输出就说清楚「仍在运行、已静默多久」，
            #   而不是让调用方/容易以为它挂了。
            if job["status"] == "running":
                silent = time.time() - float(job.get("last_output_at") or job["started"])
                if silent > 60:
                    head += f"（仍在运行，已 {human_duration(silent)} 没有新输出）"
            hit = ""
            if pattern:
                hit = f"\n（{'已找到' if any(pattern in l for l in job['lines']) else '未找到'}文本 “{pattern}”）"
            body = "\n".join(lines) if lines else "（暂无输出）"
            return ToolResult(
                content=f"{head}{hit}\n\n最后 {len(lines)} 行输出：\n{body}",
                display=f"后台任务 {job_id}：{job['status']}",
                data={k: v for k, v in job.items() if k != "lines"},
            )
        return ToolResult.fail(f"不支持的操作：{action}")


class ProcessTool(Tool):
    name = "process"
    group = "执行"
    description = (
        "查看与管理系统进程：list 列出、find 按名查找、kill 终止、port 查端口占用。"
    )
    parameters = {
        "action": {"type": "string", "enum": ["list", "find", "kill", "port"], "description": "操作类型"},
        "name": {"type": "string", "description": "find 时的进程名关键字"},
        "pid": {"type": "integer", "description": "kill 时的进程号"},
        "port": {"type": "integer", "description": "port 时要查的端口号"},
        "top": {"type": "integer", "description": "list 时按资源排序返回前几个，默认 30"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", name: str = "", pid: int = 0,
                  port: int = 0, top: int = 30, **_: Any) -> ToolResult:
        action = (action or "").lower()
        try:
            import psutil  # type: ignore
        except ImportError:
            psutil = None

        if action == "kill":
            if not pid:
                return ToolResult.fail("kill 需要提供 pid")
            if pid in (0, 1, 4) and os.name != "nt":
                return ToolResult.fail("拒绝终止系统关键进程")
            try:
                if os.name == "nt":
                    r = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(pid),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    )
                    out, _ = await r.communicate()
                    ok = r.returncode == 0
                    return ToolResult(
                        content=f"{'已终止' if ok else '终止失败'}进程 {pid}\n{out.decode('utf-8', 'replace')[:500]}",
                        ok=ok,
                    )
                os.kill(int(pid), signal.SIGTERM)
                return ToolResult.text(f"已向进程 {pid} 发送 SIGTERM")
            except (OSError, ProcessLookupError) as e:
                return ToolResult.fail(f"终止失败：{e}")

        if psutil is None:
            return await self._fallback(ctx, action, name, port, top)

        if action == "port":
            if not port:
                return ToolResult.fail("port 需要提供端口号")
            hits = []
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == int(port):
                    try:
                        proc = psutil.Process(c.pid) if c.pid else None
                        pname = proc.name() if proc else "?"
                    except Exception:
                        pname = "?"
                    hits.append(f"PID {c.pid}  {pname}  {c.laddr.ip}:{c.laddr.port}  {c.status}")
            if not hits:
                return ToolResult.text(f"端口 {port} 当前没有被占用。")
            return ToolResult.text(f"占用端口 {port} 的进程：\n" + "\n".join(hits))

        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
            try:
                info = p.info
                if action == "find" and name and name.lower() not in (info.get("name") or "").lower():
                    continue
                mem = info.get("memory_info")
                procs.append(
                    (
                        info.get("cpu_percent") or 0.0,
                        info.get("pid"),
                        info.get("name") or "",
                        (mem.rss if mem else 0),
                        " ".join(info.get("cmdline") or [])[:140],
                    )
                )
            except Exception:
                continue
        procs.sort(key=lambda x: -x[3])
        if not procs:
            return ToolResult.text("没有找到匹配的进程。")
        lines = [f"共 {len(procs)} 个进程（按内存排序，显示前 {min(top, len(procs))} 个）："]
        for cpu, p_pid, pname, rss, cmd in procs[: int(top)]:
            lines.append(f"  {p_pid:>7}  {rss / 1048576:>7.1f}MB  {pname[:28]:<28}  {cmd[:80]}")
        return ToolResult.text("\n".join(lines), data={"count": len(procs)})

    async def _fallback(self, ctx: ToolContext, action: str, name: str, port: int, top: int) -> ToolResult:
        """没有 psutil 时的降级实现。"""
        if action == "port":
            cmd = f"netstat -ano | findstr :{port}" if os.name == "nt" else f"ss -lntp | grep :{port}"
        elif os.name == "nt":
            cmd = "tasklist"
        else:
            cmd = "ps aux"
        sb = _sandbox(ctx, timeout=30)
        res = await sb.shell(cmd, cwd=ctx.workspace)
        text = res.stdout or res.stderr
        if action == "find" and name:
            text = "\n".join(l for l in text.split("\n") if name.lower() in l.lower())
        lines = text.split("\n")
        return ToolResult.text(
            f"（未安装 psutil，使用系统命令近似结果；pip install psutil 可获得更完整信息）\n\n"
            + truncate_middle("\n".join(lines[: int(top) + 3]), 8000)
        )


# ---- 工具函数 ------------------------------------------------------------

def _pick_shell(shell: str):
    if not shell:
        return None
    s = shell.lower()
    if s == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        return (exe, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]) if exe else None
    if s == "cmd":
        return (os.environ.get("COMSPEC", "cmd.exe"), ["/d", "/s", "/c"])
    if s in ("bash", "sh"):
        exe = shutil.which(s)
        return (exe, ["-c"]) if exe else None
    return None


def _exec_result(res: ExecResult, *, display: str = "") -> ToolResult:
    ok = res.ok
    return ToolResult(
        ok=ok,
        content=res.summary(),
        error=None if ok else (res.error or f"命令以退出码 {res.returncode} 结束"),
        display=display,
        duration=res.duration,
        truncated=res.truncated,
        data=res.to_dict(),
    )


def restart_python() -> str:
    """返回当前 Python 解释器路径（供界面展示）。"""
    return python_executable()


TOOLS = [
    ShellTool,
    PythonTool,
    RunScriptTool,
    InstallPackagesTool,
    BackgroundJobTool,
    ProcessTool,
]
