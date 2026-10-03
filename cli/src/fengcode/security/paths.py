"""路径安全：把用户/模型给的路径解析到受控范围内，防止越界与目录穿越。

规则
----
1. 相对路径基于 **当前工作区** 解析。
2. ``$WORKSPACE`` / ``$HOME`` / ``$TMP`` / ``$FENGCODE`` 变量在配置里可作占位。
3. 写入必须落在 ``permissions.write_paths`` 之内；读取受 ``read_paths`` 约束。
4. 拒绝通过 ``..``、符号链接、Windows 8.3 短名等方式逃逸。
5. 命中 ``deny_patterns`` 的一律拒绝（如系统目录、.ssh、注册表导出点）。
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from .. import paths


class PathNotAllowed(Exception):
    """路径超出允许范围。"""

    def __init__(self, message: str, *, path: str = "", kind: str = "read") -> None:
        super().__init__(message)
        self.path = path
        self.kind = kind


_WIN_DENY = [
    r"c:\windows\**",
    r"c:\program files\**",
    r"c:\program files (x86)\**",
    r"c:\programdata\microsoft\**",
    r"**\.ssh\**",
    r"**\.aws\**",
    r"**\.gnupg\**",
    r"**\appdata\roaming\microsoft\protect\**",
    r"**\system volume information\**",
    r"**\$recycle.bin\**",
    r"**\ntuser.dat*",
    r"**\boot\**",
]

_POSIX_DENY = [
    "/etc/**",
    "/boot/**",
    "/sys/**",
    "/proc/**",
    "/dev/**",
    "/root/.ssh/**",
    "**/.ssh/**",
    "**/.aws/**",
    "**/.gnupg/**",
]

_TMP_DENY_EXT = {".sys", ".drv", ".lnk"}


def expand_vars(text: str, *, workspace: str | os.PathLike | None = None) -> str:
    """展开 ``$WORKSPACE`` 等占位符与 ``~``。"""
    if not text:
        return text
    ws = str(workspace) if workspace is not None else None
    tables = {
        "$WORKSPACE": ws or str(paths.workspace_dir()),
        "$FENGCODE": str(paths.home()),
        "$HOME": str(Path.home()),
        "$TMP": tempfile.gettempdir(),
    }
    out = str(text)
    for k, v in tables.items():
        out = out.replace(k, v)
    return os.path.expanduser(os.path.expandvars(out))


def long_name(path: str) -> str:
    """把 Windows 8.3 短名转成长名；其它情况原样返回。

    ``Path.resolve()`` / ``realpath()`` 在 Windows 上会**反向**产生短名，
    而用户配置里写的是长名，两边不一致会导致白名单误判。
    统一在这里转成长名，保证全程可比。

    ⚠️ ``GetLongPathNameW`` 对**不存在**的路径会失败（返回 0），
    此时退化为「转换父目录 + 拼回文件名」，这样新建文件的路径也能归一化。
    """
    if os.name != "nt" or not path:
        return path
    if any(ch in path for ch in "*?"):
        return path
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(32768)
        n = k32.GetLongPathNameW(str(path), buf, 32768)
        if n and n < 32768:
            return buf.value
        # 路径不存在：转换父目录后拼回
        parent = os.path.dirname(str(path))
        if parent and parent != path:
            lp = long_name(parent)
            if lp != parent:
                return os.path.join(lp, os.path.basename(str(path)))
    except Exception:
        pass
    return path


def resolve(path: str | os.PathLike, *, workspace: str | os.PathLike | None = None,
            must_exist: bool = False) -> Path:
    """把路径规范化为绝对路径（统一成长名形式）。

    刻意不用 ``Path.resolve()``：Windows 上它会把长目录名展开成 8.3 短名
    （如 ``Administrator.DESKTOP-AI2CN4U`` → ``ADMINI~1.DES``），
    导致与用户配置的白名单路径比对失败。这里用 ``abspath + normpath + 长名化``。
    符号链接逃逸在 ``check_*`` 里单独校验。
    """
    p = expand_vars(str(path), workspace=workspace)
    pp = Path(p)
    if not pp.is_absolute():
        base = Path(workspace) if workspace else paths.workspace_dir()
        pp = base / pp
    pp = Path(long_name(os.path.abspath(os.path.normpath(str(pp)))))
    if must_exist:
        try:
            ok = pp.exists()
        except OSError:
            ok = False
        if not ok:
            raise FileNotFoundError(f"文件不存在或无法访问：{pp}")
    return pp


def real_path(p: Path) -> Path:
    """取真实路径（解析符号链接），仅用于越界二次校验。"""
    try:
        return Path(long_name(os.path.realpath(os.path.abspath(str(p)))))
    except OSError:
        return p


def _norm_for_match(p: Path) -> str:
    """归一化到可比较的形式：长名 + 统一分隔符 + 小写 + 去尾斜杠。"""
    s = long_name(os.path.abspath(str(p))).replace("\\", "/").lower()
    while s.endswith("/") and len(s) > 3:
        s = s[:-1]
    return s


def _match_any(target: str, patterns: Iterable[str]) -> str | None:
    for pat in patterns:
        if not pat:
            continue
        # 白名单里的路径也要长名化：用户可能粘贴了短名，或 %TEMP% 展开出短名
        pat_n = long_name(os.path.abspath(expand_vars(pat))).replace("\\", "/").lower().rstrip("/")
        if not pat_n:
            continue
        # 目录前缀匹配
        if target == pat_n or target.startswith(pat_n + "/"):
            return pat
        if fnmatch.fnmatch(target, pat_n):
            return pat
        if fnmatch.fnmatch(target, pat_n + "/**"):
            return pat
    return None


def is_within(path: Path, roots: Iterable[str]) -> bool:
    t = _norm_for_match(path)
    return _match_any(t, roots) is not None


def default_deny_patterns() -> list[str]:
    return list(_WIN_DENY) if os.name == "nt" else list(_POSIX_DENY)


class PathGuard:
    """读写路径校验器。"""

    def __init__(
        self,
        *,
        workspace: str | os.PathLike | None = None,
        write_paths: Iterable[str] | None = None,
        read_paths: Iterable[str] | None = None,
        deny_patterns: Iterable[str] | None = None,
        mode: str = "ask",
    ) -> None:
        self.workspace = Path(workspace) if workspace else paths.workspace_dir()
        self.write_paths = [str(p) for p in (write_paths if write_paths is not None else ["$WORKSPACE", "$TMP"])]
        self.read_paths = [str(p) for p in (read_paths or [])]
        self.deny_patterns = list(deny_patterns if deny_patterns is not None else default_deny_patterns())
        self.mode = mode

    # ---- 校验 ----------------------------------------------------------
    def check_read(self, path: str | os.PathLike) -> Path:
        # 顺序很重要：先解析 → 再查禁止模式 → 再查白名单 → 最后查存在性。
        # 这样访问 C:\Windows\... 这类受限路径会给出"被安全策略禁止"，
        # 而不是泄露文件系统层的 PermissionError。
        p = resolve(path, workspace=self.workspace)
        if self._denied(p):
            raise PathNotAllowed(f"该路径被安全策略禁止读取：{p}", path=str(p), kind="read")
        if self.read_paths and not is_within(p, self.read_paths) and not is_within(
            real_path(p), self.read_paths
        ):
            raise PathNotAllowed(
                f"路径不在允许读取的范围内：{p}\n允许范围：{self.read_paths}",
                path=str(p), kind="read",
            )
        self._check_symlink_escape(p)
        try:
            exists = p.exists()
        except OSError as e:
            raise PathNotAllowed(f"无法访问该路径：{p}（{e}）", path=str(p), kind="read") from e
        if not exists:
            raise FileNotFoundError(f"文件不存在：{p}")
        return p

    def check_write(self, path: str | os.PathLike, *, new_file: bool = True) -> Path:
        p = resolve(path, workspace=self.workspace)
        if self._denied(p):
            raise PathNotAllowed(f"该路径被安全策略禁止写入：{p}", path=str(p), kind="write")
        if not is_within(p, self.write_paths) and not is_within(real_path(p), self.write_paths):
            raise PathNotAllowed(
                f"路径不在允许写入的范围内：{p}\n允许范围：{self.write_paths}"
                "\n可在「设置 → 安全」里添加允许目录。",
                path=str(p), kind="write",
            )
        self._check_symlink_escape(p)
        if not new_file:
            try:
                exists = p.exists()
            except OSError as e:
                raise PathNotAllowed(f"无法访问该路径：{p}（{e}）", path=str(p), kind="write") from e
            if not exists:
                raise PathNotAllowed(f"要修改的文件不存在：{p}", path=str(p), kind="write")
        return p

    def _check_symlink_escape(self, p: Path) -> None:
        """若路径经符号链接指向了允许范围之外，则拒绝。

        判定只针对「逃逸」方向：``p`` 在允许范围内、而真实目标在范围外。
        两边都在范围内（或都在范围外，由白名单逻辑另行处理）时不算逃逸——
        这样能避免 Windows 长短名差异造成的误判。
        """
        try:
            if not p.parent.exists():
                return
            rp = real_path(p)
        except OSError:
            return
        allowed = list(self.write_paths) + list(self.read_paths)
        if is_within(p, allowed) and not is_within(rp, allowed):
            raise PathNotAllowed(
                f"路径经符号链接指向了受保护位置：{p} → {rp}",
                path=str(p), kind="write",
            )

    def _denied(self, p: Path) -> bool:
        t = _norm_for_match(p)
        return _match_any(t, self.deny_patterns) is not None

    def is_write_allowed(self, p: Path) -> bool:
        return is_within(p, self.write_paths) and not self._denied(p)

    def suggest_write_paths(self) -> list[str]:
        return ["$WORKSPACE", "$TMP", str(Path.home() / "Documents"), str(Path.home() / "Desktop")]


# ---- 危险命令识别 --------------------------------------------------------

DANGEROUS_SHELL_PATTERNS: list[tuple[str, str]] = [
    (r"rm\s+-rf\s+/(?:\s|$)", "删除根目录"),
    (r"rm\s+-rf\s+~(?:/\s*)?$", "删除用户主目录"),
    (r"(?:^|\W)mkfs(?:\.\w+)?\s", "格式化文件系统"),
    (r"dd\s+if=.*of=/dev/", "直接写裸设备"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork 炸弹"),
    (r"chmod\s+-R\s+777\s+/(?:\s|$)", "全线权限放开"),
    (r"(?:^|[|&;]\s*)(?:shutdown|reboot|halt|poweroff)\b", "关机/重启系统"),
    (r"format\s+[a-z]:", "格式化磁盘"),
    (r"del\s+/[sfq].*[a-z]:\\(?:\s|$)", "递归删除盘符根目录"),
    (r"(?:rd|rmdir)\s+/s\s+/q\s+[a-z]:\\(?:\s|$)", "递归删除盘符根目录"),
    (r"diskpart\b", "磁盘分区操作"),
    (r"bcdedit\b", "修改启动配置"),
    (r"reg\s+delete\s+HKLM", "删除注册表项"),
    (r"vssadmin\s+delete\s+shadows", "删除卷影副本"),
    (r"cipher\s+/w", "擦除磁盘剩余空间"),
    (r"wevtutil\s+cl", "清空事件日志"),
    (r"netsh\s+firewall\s+(?:set|delete|add)\b", "修改防火墙规则"),
    (r"icacls\s+.*\s+/grant\s+Everyone", "给所有人授权"),
    (r"takeown\s+/f\s+[a-z]:\\", "夺取系统文件所有权"),
    (r"(?:git|hg)\s+push\s+.*--force", "强制推送覆盖远端"),
    (r"curl\s+[^|]*\|\s*(?:ba)?sh", "管道执行远程脚本"),
    (r"wget\s+[^|]*\|\s*(?:ba)?sh", "管道执行远程脚本"),
    (r"iwr\s+[^|]*\|\s*iex", "管道执行远程脚本（PowerShell）"),
    (r"Invoke-Expression\s*\(", "执行动态脚本"),
    (r"(?:stop|kill)\s+-9\s+1\b", "杀死 init"),
    (r"sudo\s+rm\s+-rf\s+", "以 root 递归删除"),
    (r">\s*/dev/sd[a-z]", "覆写块设备"),
    (r"(?:chown|chmod)\s+-R\s+.*\s+/(?:\s|$)", "递归修改根目录归属"),
]

DANGEROUS_FILE_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)[\\/](?:\.env|\.env\.local|credentials|id_rsa|id_ed25519|\.npmrc|\.pypirc)$", "疑似凭据文件"),
    (r"(?i)(?:\.pem|\.pfx|\.p12|\.key)$", "疑似私钥文件"),
    (r"(?i)(?:\.bashrc|\.zshrc|\.profile|\.gitconfig)$", "用户 shell 配置"),
    (r"(?i)C:[\\/]Windows[\\/]System32[\\/]", "系统目录"),
    (r"(?i)[\\/]\.git[\\/]config$", "Git 配置"),
]


def scan_dangerous(command: str) -> list[str]:
    """扫描命令里的高危模式，返回命中原因列表。"""
    if not command:
        return []
    hits: list[str] = []
    for pat, reason in DANGEROUS_SHELL_PATTERNS:
        if re.search(pat, command, re.IGNORECASE | re.MULTILINE):
            hits.append(reason)
    # 删除类命令整体判断一次
    if re.search(r"\brm\b", command, re.IGNORECASE) and re.search(r"-r|-rf|--recursive", command):
        hits.append("递归删除")
    if re.search(r"(?i)\bdel\b|\bRemove-Item\b", command) and re.search(r"(?i)/s|-recurse", command):
        hits.append("递归删除")
    return list(dict.fromkeys(hits))


def scan_dangerous_path(path: str) -> list[str]:
    hits: list[str] = []
    for pat, reason in DANGEROUS_FILE_PATTERNS:
        if re.search(pat, path or ""):
            hits.append(reason)
    return list(dict.fromkeys(hits))


# ---- 命令效果分类（参数感知）--------------------------------------------
# ★ 为什么需要：原来只有「危险 / 不危险」二元判断 + 一张黑名单，两头都不准：
#   · 只读命令（git status / ls / dir）没有危险模式 → 放行，这是对的；
#   · 但「看着无害实则改东西」的命令（git restore / git stash drop / npm install）
#     也常常不命中黑名单 → 被静默放行，用户不知道它在改工作区；
#   · 反过来，`git restore` 这种**会丢改动**的命令一旦被「本会话始终允许」记住，
#     后面同类操作就再也不问了 —— 实测反馈过这类不安。
#   所以这里按「命令 + 参数」判成三档：
#     read  只读           → 直接放行
#     write 会改文件       → 正常走审批（可被「始终允许」记住）
#     repo  会改仓库状态   → **每次都问**，不复用「始终允许」（丢改动不可逆）
COMMAND_EFFECTS: dict[str, str] = {
    # 只读
    "status": "read", "log": "read", "diff": "read", "show": "read",
    "branch": "read", "remote": "read", "tag": "read", "blame": "read",
    "ls": "read", "dir": "read", "cat": "read", "type": "read",
    "pwd": "read", "whoami": "read", "echo": "read",
    # 改仓库状态（不可逆 / 丢弃改动）
    "restore": "repo", "reset": "repo", "checkout": "repo",
    "clean": "repo", "revert": "repo", "rebase": "repo",
    "merge": "repo", "cherry-pick": "repo", "stash": "repo",
    "switch": "repo", "push": "repo", "fetch": "repo", "pull": "repo",
    # 改文件 / 环境
    "add": "write", "commit": "write", "mv": "write",
    "init": "write", "clone": "write", "config": "write",
}

# git 子命令里「只在特定参数下才安全」的例外
_GIT_READ_OK = {
    "stash": {"list", "show"},      # git stash list / show 是只读
    "branch": {"", "-a", "-r", "-v", "--all", "--list", "-l"},
}


def classify_command(command: str) -> str:
    """按「命令 + 参数」判定效果：read（只读）/ write（改文件）/ repo（改仓库状态）/ ""（未知）。

    ★ 参数感知是关键：`git status` 与 `git restore .` 都是 git，
      只按程序名判断必然误判；必须看子命令（以及个别参数）。
    """
    c = (command or "").strip()
    if not c:
        return ""
    # 取第一段命令（跳过 env 前缀、sudo 等）
    try:
        import shlex

        parts = shlex.split(c, posix=False)
    except Exception:
        parts = c.split()
    if not parts:
        return ""
    prog = parts[0].strip('"').strip("'").lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]

    if prog in ("git", "hg"):
        # 找第一个非选项参数作为子命令
        sub = ""
        rest: list[str] = []
        for p in parts[1:]:
            if p.startswith("-") and not sub:
                continue
            if not sub:
                sub = p.lower()
            else:
                rest.append(p)
        if not sub:
            return "read"          # 裸 `git` 只打印帮助
        special = _GIT_READ_OK.get(sub)
        if special is not None:
            # 只在这几个参数下才算只读；其余（如 stash drop）按 repo 处理
            arg = (rest[0].lower() if rest else "")
            return "read" if arg in special else "repo"
        return COMMAND_EFFECTS.get(sub, "write")

    if prog in ("npm", "pnpm", "yarn", "pip", "cargo", "apt", "apt-get"):
        sub = next((p.lower() for p in parts[1:] if not p.startswith("-")), "")
        # 只是查询版本 / 列出已装包 → 只读
        if sub in ("ls", "list", "--version", "-v", "show", "view", "info", "outdated"):
            return "read"
        return "write"

    if prog in ("ls", "dir", "cat", "type", "pwd", "whoami", "echo", "head", "tail", "wc", "find", "grep"):
        return "read"

    if prog in ("rm", "del", "rmdir", "rd", "mv", "move", "cp", "copy",
                "mkdir", "md", "touch", "tee", "sed", "chmod", "chown"):
        return "write"

    return ""


__all__ = [
    "PathGuard",
    "PathNotAllowed",
    "resolve",
    "expand_vars",
    "is_within",
    "scan_dangerous",
    "scan_dangerous_path",
    "classify_command",
    "default_deny_patterns",
]
