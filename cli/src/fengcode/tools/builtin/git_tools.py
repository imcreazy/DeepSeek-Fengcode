"""Git 工具：状态、日志、差异、暂存、提交、分支、拉取推送。

注意：本机 Windows 侧可能没有 git 可执行文件，工具会自动探测
（含 WSL 内的 git 与常见安装路径），探测不到时给出明确提示。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...utils import is_windows, truncate_middle
from ..base import Tool, ToolContext, ToolResult

_GIT_CANDIDATES = [
    "git",
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
]


def find_git() -> str | None:
    """跨平台查找 git 可执行文件。"""
    for c in _GIT_CANDIDATES:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    # WSL 里的 git
    if is_windows():
        wsl = shutil.which("wsl")
        if wsl:
            try:
                r = subprocess.run([wsl, "which", "git"], capture_output=True, timeout=15)
                if r.returncode == 0 and r.stdout.strip():
                    return "wsl-git"
            except Exception:
                pass
    return None


async def _run_git(args: list[str], cwd: Path, git: str, timeout: float = 60.0) -> tuple[int, str, str]:
    from ...security.sandbox import build_env

    if git == "wsl-git":
        # 路径转换：C:\a\b → /mnt/c/a/b
        cwd_s = str(cwd)
        if len(cwd_s) > 1 and cwd_s[1] == ":":
            drive = cwd_s[0].lower()
            cwd_wsl = "/mnt/" + drive + cwd_s[2:].replace("\\", "/")
        else:
            cwd_wsl = cwd_s.replace("\\", "/")
        cmd = ["wsl", "--", "git", "-C", cwd_wsl, *args]
    else:
        cmd = [git, "-C", str(cwd), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=build_env()
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", "git 命令超时"
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


class GitTool(Tool):
    name = "git"
    group = "开发"
    dangerous = True
    description = (
        "执行 Git 操作。action 可为：status（状态）、log（提交历史）、diff（差异）、"
        "add（暂存）、commit（提交）、branch（分支列表/新建）、checkout（切换/新建分支）、"
        "push（推送）、pull（拉取）、clone（克隆）、init（初始化）、remote（查看远端）、"
        "config（读写配置）、show（查看某次提交）、stash（暂存改动）、raw（执行任意 git 子命令）。"
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["status", "log", "diff", "add", "commit", "branch", "checkout", "push", "pull",
                     "clone", "init", "remote", "config", "show", "stash", "reset", "raw"],
            "description": "要执行的 git 操作",
        },
        "cwd": {"type": "string", "description": "仓库目录，默认当前工作区"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "附加参数（如文件名、分支名）"},
        "message": {"type": "string", "description": "commit 时的提交信息"},
        "files": {"type": "array", "items": {"type": "string"}, "description": "add 时的文件列表（默认全部）"},
        "limit": {"type": "integer", "description": "log 时返回多少条，默认 20"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", cwd: str = "", args: list[str] | None = None,
                  message: str = "", files: list[str] | None = None, limit: int = 20,
                  **_: Any) -> ToolResult:
        from ...security.paths import resolve

        git = find_git()
        if not git:
            return ToolResult.fail(
                "本机没有找到 git。\n"
                "· Windows：下载安装 https://git-scm.com/download/win\n"
                "· 或使用 WSL：wsl --install，再在 WSL 里操作\n"
                "· 也可以用 GitHub MCP / http_request 调用 GitHub API 完成仓库操作"
            )
        repo = resolve(cwd or ".", workspace=ctx.workspace)
        if not repo.exists():
            return ToolResult.fail(f"目录不存在：{repo}")
        a = list(args or [])
        act = (action or "").lower()

        if act == "clone":
            if not a:
                return ToolResult.fail("clone 需要提供仓库地址（放在 args 里）")
            url = a[0]
            dest = a[1] if len(a) > 1 else ""
            cmd = ["clone", url] + ([dest] if dest else [])
            rc, out, err = await _run_git(cmd, ctx.workspace, git, timeout=600)
            return _res(rc, out, err, display=f"git clone {url}")

        if act == "init":
            rc, out, err = await _run_git(["init"], repo, git)
            return _res(rc, out, err, display=f"git init {repo.name}")

        if act == "status":
            rc, out, err = await _run_git(["status", "--short", "--branch"], repo, git)
            if not out.strip():
                out = "（工作区干净）"
            return _res(rc, out, err, display="git status")

        if act == "log":
            cmd = ["log", f"-{int(limit or 20)}", "--pretty=format:%h %ad %an  %s", "--date=short",
                   "--stat", "--stat-width=100"]
            rc, out, err = await _run_git(cmd, repo, git)
            return _res(rc, out or "（暂无提交记录）", err, display="git log")

        if act == "diff":
            cmd = ["diff"] + (a or ["--stat"])
            if not a:
                cmd = ["diff", "--stat", "--patch", "HEAD"]
            rc, out, err = await _run_git(cmd, repo, git)
            text = out or "（没有差异）"
            if len(text) > 20000:
                text = truncate_middle(text, 20000)
            return _res(rc, text, err, display="git diff")

        if act == "add":
            cmd = ["add", "--"] + (files or a or ["."])
            rc, out, err = await _run_git(cmd, repo, git)
            rc2, out2, _ = await _run_git(["status", "--short"], repo, git)
            return _res(rc, (out + "\n" + out2).strip(), err, display="git add")

        if act == "commit":
            if not message:
                return ToolResult.fail("commit 需要提供 message")
            rc, out, err = await _run_git(["commit", "-m", message], repo, git, timeout=180)
            return _res(rc, out, err, display=f"git commit：{truncate_middle(message, 40)}")

        if act == "branch":
            if a:
                rc, out, err = await _run_git(["branch", *a], repo, git)
            else:
                rc, out, err = await _run_git(["branch", "-vv", "--all"], repo, git)
            return _res(rc, out, err, display="git branch")

        if act == "checkout":
            if not a:
                return ToolResult.fail("checkout 需要提供分支名或 -b 新分支")
            rc, out, err = await _run_git(["checkout", *a], repo, git)
            return _res(rc, out, err, display=f"git checkout {' '.join(a)}")

        if act == "push":
            rc, out, err = await _run_git(["push", *a], repo, git, timeout=600)
            return _res(rc, out or "（已推送）", err, display="git push")

        if act == "pull":
            rc, out, err = await _run_git(["pull", *a], repo, git, timeout=600)
            return _res(rc, out, err, display="git pull")

        if act == "remote":
            rc, out, err = await _run_git(["remote", "-v"], repo, git)
            if a:
                rc, out, err = await _run_git(["remote", *a], repo, git)
            return _res(rc, out, err, display="git remote")

        if act == "config":
            rc, out, err = await _run_git(["config", *a], repo, git)
            return _res(rc, out, err, display="git config")

        if act == "show":
            rc, out, err = await _run_git(["show", *(a or ["HEAD"])], repo, git)
            return _res(rc, truncate_middle(out, 20000), err, display="git show")

        if act == "stash":
            rc, out, err = await _run_git(["stash", *a], repo, git)
            return _res(rc, out, err, display="git stash")

        if act == "reset":
            if not a:
                return ToolResult.fail("reset 需要提供参数，如 --soft HEAD~1")
            rc, out, err = await _run_git(["reset", *a], repo, git)
            return _res(rc, out, err, display="git reset")

        if act == "raw":
            if not a:
                return ToolResult.fail("raw 需要把 git 子命令放进 args")
            rc, out, err = await _run_git(a, repo, git)
            return _res(rc, out, err, display=f"git {' '.join(a[:3])}")

        return ToolResult.fail(f"不支持的 git 操作：{action}")


def _res(rc: int, out: str, err: str, *, display: str = "") -> ToolResult:
    text = out or ""
    if err.strip():
        text = (text + "\n【stderr】\n" + err).strip()
    return ToolResult(
        ok=rc == 0,
        content=text or "（无输出）",
        error=None if rc == 0 else f"git 退出码 {rc}",
        display=display,
        data={"returncode": rc},
    )


class RepoOverviewTool(Tool):
    name = "repo_overview"
    group = "开发"
    read_only = True
    description = (
        "快速了解一个代码仓库：目录结构、语言构成、关键文件、依赖清单、最近的提交。"
        "接手陌生项目时先用这个工具。"
    )
    parameters = {
        "path": {"type": "string", "description": "仓库目录，默认当前工作区"},
        "depth": {"type": "integer", "description": "目录树深度，默认 2"},
        "max_entries": {"type": "integer", "description": "最多列出条目数，默认 120"},
    }
    required = []

    async def run(self, ctx: ToolContext, path: str = ".", depth: int = 2, max_entries: int = 120,
                  **_: Any) -> ToolResult:
        from ...security.paths import resolve

        root = resolve(path or ".", workspace=ctx.workspace)
        if not root.is_dir():
            return ToolResult.fail(f"不是目录：{root}")
        lines: list[str] = [f"# 仓库概览：{root.name}", f"路径：{root}", ""]

        # 目录树
        lines.append("## 目录结构")
        tree = _tree(root, max_depth=int(depth), max_entries=int(max_entries))
        lines.extend(tree or ["（空目录）"])

        # 语言构成
        exts: dict[str, int] = {}
        total_files = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           (".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")]
            for fn in filenames:
                total_files += 1
                e = Path(fn).suffix.lower() or "(无扩展名)"
                exts[e] = exts.get(e, 0) + 1
        top = sorted(exts.items(), key=lambda x: -x[1])[:12]
        lines.append("")
        lines.append(f"## 语言/文件构成（共 {total_files} 个文件）")
        for e, c in top:
            lines.append(f"  {e:<16} {c} 个")

        # 关键文件
        keys = ["README.md", "README.rst", "readme.md", "package.json", "pyproject.toml",
                "requirements.txt", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
                "Makefile", "Dockerfile", "docker-compose.yml", ".gitignore", "LICENSE"]
        found = [k for k in keys if (root / k).exists()]
        if found:
            lines.append("")
            lines.append("## 关键文件")
            for k in found:
                size = (root / k).stat().st_size
                lines.append(f"  {k}  ({size} 字节)")

        # 依赖
        dep_lines: list[str] = []
        for fname, kind in (("requirements.txt", "Python"), ("pyproject.toml", "Python"),
                            ("package.json", "Node"), ("Cargo.toml", "Rust"),
                            ("go.mod", "Go"), ("pom.xml", "Maven")):
            fp = root / fname
            if not fp.is_file():
                continue
            try:
                head = fp.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            dep_lines.append(f"### {fname}（{kind}）")
            dep_lines.append("```\n" + head.strip() + "\n```")
            break
        if dep_lines:
            lines.append("")
            lines.append("## 依赖清单")
            lines.extend(dep_lines)

        # git 信息
        git = find_git()
        if git and (root / ".git").exists():
            rc, out, _ = await _run_git(["log", "-5", "--pretty=format:%h %ad %s", "--date=short"], root, git)
            if rc == 0 and out.strip():
                lines.append("")
                lines.append("## 最近提交")
                lines.extend("  " + l for l in out.strip().split("\n"))

        text = "\n".join(lines)
        return ToolResult(
            content=truncate_middle(text, 24000),
            display=f"仓库概览 {root.name}（{total_files} 个文件）",
            data={"files": total_files, "extensions": dict(top)},
        )


def _tree(root: Path, *, max_depth: int, max_entries: int) -> list[str]:
    out: list[str] = []
    count = 0
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".vscode"}

    def walk(d: Path, prefix: str, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            items = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        items = [i for i in items if i.name not in skip and not i.name.startswith(".")]
        for i, item in enumerate(items):
            if count >= max_entries:
                out.append(prefix + "…（已达显示上限）")
                return
            last = i == len(items) - 1
            branch = "└── " if last else "├── "
            name = item.name + ("/" if item.is_dir() else "")
            out.append(prefix + branch + name)
            count += 1
            if item.is_dir():
                walk(item, prefix + ("    " if last else "│   "), depth + 1)

    walk(root, "", 1)
    return out


TOOLS = [GitTool, RepoOverviewTool]
