"""代码质量与设计辅助工具：格式化、静态检查、测试运行、依赖图。

这些工具让 Agent 能自己验证改动的正确性，而不是"写完就交"。
所有外部命令都先探测是否存在，缺失时给出安装建议。
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from ...utils import human_duration, iter_files, truncate_middle
from ..base import Tool, ToolContext, ToolResult


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        for ext in (".cmd", ".exe", ".bat"):
            found = shutil.which(name + ext)
            if found:
                return found
    return None


async def _run(argv: list[str], cwd: Path, *, timeout: float = 300.0) -> tuple[int, str]:
    from ...security.sandbox import build_env

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=build_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"命令不存在：{argv[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, f"命令超时（>{timeout}s）"
    return proc.returncode or 0, out.decode("utf-8", "replace")


class RunTestsTool(Tool):
    name = "run_tests"
    group = "开发"
    dangerous = True
    description = (
        "运行项目测试并返回结果。自动识别 pytest / unittest / npm test / cargo test / go test。"
        "改完代码后应该主动跑一次验证。"
    )
    parameters = {
        "path": {"type": "string", "description": "项目目录，默认当前工作区"},
        "target": {"type": "string", "description": "指定测试文件或用例，如 tests/test_x.py::test_y"},
        "framework": {"type": "string", "enum": ["auto", "pytest", "unittest", "npm", "cargo", "go"], "description": "测试框架"},
        "timeout": {"type": "number", "description": "超时秒数，默认 600"},
        "verbose": {"type": "boolean", "description": "是否显示详细输出"},
        "keyword": {"type": "string", "description": "按名称过滤用例（-k）"},
    }
    required = []

    async def run(self, ctx: ToolContext, path: str = ".", target: str = "", framework: str = "auto",
                  timeout: float = 600, verbose: bool = False, keyword: str = "",
                  **_: Any) -> ToolResult:
        from ...security.paths import resolve

        root = resolve(path or ".", workspace=ctx.workspace)
        if not root.is_dir():
            return ToolResult.fail(f"不是目录：{root}")
        fw = (framework or "auto").lower()
        if fw == "auto":
            fw = _detect_framework(root)

        if fw == "pytest":
            cmd = [sys.executable, "-m", "pytest", "-q"]
            if verbose:
                cmd = [sys.executable, "-m", "pytest", "-v"]
            if keyword:
                cmd += ["-k", keyword]
            cmd.append(target or "tests" if (root / "tests").is_dir() else ".")
            if not target and not (root / "tests").is_dir():
                cmd = [sys.executable, "-m", "pytest", "-q"] + (["-k", keyword] if keyword else [])
        elif fw == "unittest":
            cmd = [sys.executable, "-m", "unittest", "discover", "-v" if verbose else "-s", str(target or "tests")]
            if not verbose:
                cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(target or "tests")]
        elif fw == "npm":
            npm = _which("npm")
            if not npm:
                return ToolResult.fail("没有找到 npm")
            cmd = [npm, "test", "--silent"]
        elif fw == "cargo":
            cargo = _which("cargo")
            if not cargo:
                return ToolResult.fail("没有找到 cargo")
            cmd = [cargo, "test"]
        elif fw == "go":
            go = _which("go")
            if not go:
                return ToolResult.fail("没有找到 go")
            cmd = [go, "test", "./..."]
        else:
            return ToolResult.fail(
                "未识别出测试框架。可显式传 framework=pytest/unittest/npm/cargo/go。"
            )

        t0 = time.time()
        rc, out = await _run(cmd, root, timeout=float(timeout or 600))
        dt = time.time() - t0
        summary = _test_summary(out)
        head = f"命令：{' '.join(cmd)}\n退出码：{rc}　耗时：{human_duration(dt)}\n{summary}"
        return ToolResult(
            ok=rc == 0,
            content=f"{head}\n\n{truncate_middle(out, 20000)}",
            error=None if rc == 0 else "测试未全部通过",
            display=f"运行测试（{'通过' if rc == 0 else '失败'}，{human_duration(dt)}）",
            data={"returncode": rc, "duration": dt, "framework": fw, "output": out[-6000:]},
        )


def _detect_framework(root: Path) -> str:
    if (root / "package.json").is_file():
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            if "test" in (pkg.get("scripts") or {}):
                return "npm"
        except Exception:
            pass
    if (root / "Cargo.toml").is_file():
        return "cargo"
    if (root / "go.mod").is_file():
        return "go"
    if (root / "pytest.ini").is_file() or (root / "pyproject.toml").is_file() or (root / "tests").is_dir():
        return "pytest"
    for _ in iter_files(root, exts=["py"], max_files=200):
        return "pytest"
    return ""


def _test_summary(out: str) -> str:
    for pat in (
        r"=+ ([\d]+ passed[^=]*) =+",
        r"([\d]+ passed[^\n]*)",
        r"Tests:\s+([^\n]+)",
        r"test result: ([^\n]+)",
        r"(OK|FAILED)[^\n]{0,80}",
    ):
        m = re.search(pat, out)
        if m:
            return f"结果：{m.group(0).strip('= ')}"
    return ""


class LintTool(Tool):
    name = "lint"
    group = "开发"
    dangerous = True
    description = (
        "对代码做静态检查与格式化：支持 Python(ruff/flake8/pylint/black)、"
        "JS/TS(eslint/prettier)、Rust(cargo clippy/fmt)。自动探测已安装的工具。"
    )
    parameters = {
        "path": {"type": "string", "description": "要检查的文件或目录"},
        "tool": {"type": "string", "description": "指定工具名，默认自动选择"},
        "fix": {"type": "boolean", "description": "是否自动修复（格式化）"},
        "timeout": {"type": "number", "description": "超时秒数，默认 180"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = ".", tool: str = "", fix: bool = False,
                  timeout: float = 180, **_: Any) -> ToolResult:
        from ...security.paths import resolve

        target = resolve(path or ".", workspace=ctx.workspace)
        if not target.exists():
            return ToolResult.fail(f"路径不存在：{target}")
        ext = target.suffix.lower()
        chosen: list[str] = []
        want = (tool or "").lower()

        if want:
            chosen = [want]
        elif ext == ".py" or target.is_dir():
            for name in ("ruff", "flake8", "pylint", "black"):
                if _which(name):
                    chosen.append(name)
            if not chosen:
                chosen = ["_py_compile"]
        elif ext in (".js", ".ts", ".jsx", ".tsx", ".vue", ".mjs", ".cjs"):
            for name in ("eslint", "prettier"):
                if _which(name):
                    chosen.append(name)
        elif ext == ".rs":
            chosen = ["cargo"]

        if not chosen:
            return ToolResult.fail(
                "没有找到可用的检查工具。建议安装：\n"
                "  · Python：python -m pip install ruff\n"
                "  · JS/TS：npm i -g eslint prettier\n"
                "  · Rust：rustup component add clippy"
            )

        reports: list[str] = []
        worst = 0
        for name in chosen[:2]:
            if name == "_py_compile":
                rc, out = await _py_compile_check(target)
            elif name == "ruff":
                cmd = [_which("ruff") or "ruff", "check" if not fix else "check", str(target)]
                if fix:
                    cmd = [_which("ruff") or "ruff", "check", "--fix", str(target)]
                rc, out = await _run(cmd, ctx.workspace, timeout=timeout)
            elif name == "black":
                cmd = [_which("black") or "black", str(target)] + ([] if fix else ["--check", "--diff"])
                rc, out = await _run(cmd, ctx.workspace, timeout=timeout)
            elif name in ("flake8", "pylint"):
                rc, out = await _run([_which(name) or name, str(target)], ctx.workspace, timeout=timeout)
            elif name == "eslint":
                cmd = [_which("eslint") or "eslint", str(target)]
                if fix:
                    cmd.append("--fix")
                rc, out = await _run(cmd, ctx.workspace, timeout=timeout)
            elif name == "prettier":
                cmd = [_which("prettier") or "prettier", "--write" if fix else "--check", str(target)]
                rc, out = await _run(cmd, ctx.workspace, timeout=timeout)
            elif name == "cargo":
                rc1, out1 = await _run([_which("cargo") or "cargo", "fmt", "--", "--check"],
                                       ctx.workspace, timeout=timeout)
                rc2, out2 = await _run([_which("cargo") or "cargo", "clippy", "--quiet"],
                                       ctx.workspace, timeout=timeout)
                rc, out = max(rc1, rc2), (out1 + "\n" + out2).strip()
            else:
                continue
            worst = max(worst, rc)
            reports.append(f"### {name}（退出码 {rc}）\n{truncate_middle(out or '（无输出）', 8000)}")

        body = "\n\n".join(reports)
        return ToolResult(
            ok=worst == 0,
            content=f"检查对象：{target}\n工具：{', '.join(chosen)}\n\n{body}",
            error=None if worst == 0 else "检查发现问题",
            display=f"静态检查（{', '.join(chosen)}）",
            data={"returncode": worst, "tools": chosen},
        )


async def _py_compile_check(target: Path) -> tuple[int, str]:
    """没有 linter 时，至少做语法检查 + 简单 AST 体检。"""
    files = [target] if target.is_file() else list(iter_files(target, exts=["py"], max_files=400))
    errors: list[str] = []
    warnings: list[str] = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(f))
        except SyntaxError as e:
            errors.append(f"{f}:{e.lineno}: 语法错误 {e.msg}")
            continue
        except Exception as e:
            errors.append(f"{f}: 读取失败 {e}")
            continue
        # 简单体检
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                warnings.append(f"{f}:{node.lineno}: 裸 except（建议指明异常类型）")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("eval", "exec") and node.args:
                    warnings.append(f"{f}:{node.lineno}: 使用了 {node.func.id}（注意安全）")
            if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops
            ):
                for comp in (node.left, *node.comparators):
                    if isinstance(comp, ast.Constant) and comp.value is None:
                        warnings.append(f"{f}:{node.lineno}: 用 == None 比较（建议用 is None）")
                        break
    lines = [f"检查了 {len(files)} 个 Python 文件（内置 AST 检查）"]
    if errors:
        lines.append(f"\n发现 {len(errors)} 个错误：")
        lines.extend("  " + e for e in errors[:40])
    if warnings:
        lines.append(f"\n{len(warnings)} 条提示：")
        lines.extend("  " + w for w in warnings[:30])
    if not errors and not warnings:
        lines.append("没有发现问题。")
    return (1 if errors else 0), "\n".join(lines)


class CodeMapTool(Tool):
    name = "code_map"
    group = "开发"
    read_only = True
    description = (
        "生成代码结构地图：列出文件中的类、函数、方法及其行号与签名（Python 用 AST 精确解析，"
        "其它语言用正则近似）。理解陌生文件结构时比逐行读更高效。"
    )
    parameters = {
        "path": {"type": "string", "description": "文件或目录"},
        "max_files": {"type": "integer", "description": "最多解析文件数，默认 60"},
        "show_private": {"type": "boolean", "description": "是否显示下划线开头的私有成员"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", max_files: int = 60,
                  show_private: bool = False, **_: Any) -> ToolResult:
        from ...security.paths import resolve

        target = resolve(path, workspace=ctx.workspace)
        if not target.exists():
            return ToolResult.fail(f"路径不存在：{target}")
        files: list[Path]
        if target.is_file():
            files = [target]
        else:
            files = list(iter_files(
                target,
                exts=["py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "cs", "rb", "php", "kt"],
                max_files=int(max_files),
            ))
        if not files:
            return ToolResult.text("没有找到可分析的代码文件。")

        parts: list[str] = []
        total_syms = 0
        for f in files[: int(max_files)]:
            try:
                syms = await asyncio.to_thread(_extract_symbols, f, show_private)
            except Exception:
                syms = []
            if not syms:
                continue
            total_syms += len(syms)
            rel = f.relative_to(target) if target.is_dir() else f.name
            parts.append(f"## {rel}")
            for kind, name, line, sig in syms:
                marker = {"class": "类", "def": "函数", "method": "方法", "arrow": "函数"}.get(kind, kind)
                parts.append(f"  {line:>5}  [{marker}] {name}{sig}")
            parts.append("")
        if not parts:
            return ToolResult.text("未能提取到结构信息。")
        return ToolResult(
            content=f"结构地图（{len(parts) // 2} 个文件，{total_syms} 个符号）：\n\n" + "\n".join(parts),
            display=f"代码结构地图（{len(files)} 个文件）",
            data={"symbols": total_syms},
        )


def _extract_symbols(f: Path, show_private: bool) -> list[tuple[str, str, int, str]]:
    src = f.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[str, str, int, str]] = []
    if f.suffix.lower() == ".py":
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if not show_private and node.name.startswith("_"):
                    continue
                bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
                out.append(("class", node.name, node.lineno, f"({bases})" if bases else ""))
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not show_private and sub.name.startswith("_") and sub.name != "__init__":
                            continue
                        prefix = "async " if isinstance(sub, ast.AsyncFunctionDef) else ""
                        out.append(("method", f"  {prefix}{sub.name}", sub.lineno, _py_sig(sub)))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not show_private and node.name.startswith("_"):
                    continue
                prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                out.append(("def", f"{prefix}{node.name}", node.lineno, _py_sig(node)))
        return out
    # 其它语言：正则近似
    pats = [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("), "def"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), "arrow"),
        (re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"), "def"),
        (re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:[\w<>\[\],\s]+)\s+(\w+)\s*\("), "method"),
    ]
    for i, line in enumerate(src.split("\n"), 1):
        s = line.strip()
        if not s or s.startswith(("//", "#", "*", "/*")):
            continue
        for rx, kind in pats:
            m = rx.match(line)
            if m:
                name = m.group(1)
                if not show_private and name.startswith("_"):
                    break
                out.append((kind, name, i, ""))
                break
    return out


def _py_sig(node: Any) -> str:
    try:
        args = ast.unparse(node.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"({args}){ret}"
    except Exception:
        return "(…)"


class DependencyGraphTool(Tool):
    name = "dependency_graph"
    group = "开发"
    read_only = True
    description = "分析 Python 项目内部的模块依赖关系，检测循环依赖，输出导入图。"
    parameters = {
        "path": {"type": "string", "description": "项目根目录"},
        "max_files": {"type": "integer", "description": "最多分析文件数，默认 200"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", max_files: int = 200, **_: Any) -> ToolResult:
        from ...security.paths import resolve

        root = resolve(path, workspace=ctx.workspace)
        if not root.is_dir():
            return ToolResult.fail(f"不是目录：{root}")

        def _analyze() -> tuple[dict[str, set[str]], list[str]]:
            files = list(iter_files(root, exts=["py"], max_files=int(max_files)))
            # 模块名 → 文件
            modmap: dict[str, Path] = {}
            for f in files:
                rel = f.relative_to(root).with_suffix("")
                parts = [p for p in rel.parts if p != "__init__"]
                if rel.name == "__init__":
                    parts = list(rel.parent.parts)
                modmap[".".join(parts)] = f
            graph: dict[str, set[str]] = {m: set() for m in modmap}
            for mod, f in modmap.items():
                try:
                    tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    names: list[str] = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        if node.level == 0:
                            names = [node.module]
                        else:
                            base = mod.split(".")
                            prefix = base[: max(0, len(base) - node.level)]
                            names = [".".join(prefix + [node.module])]
                    for n in names:
                        for candidate in (n, n.rsplit(".", 1)[0]):
                            if candidate in graph and candidate != mod:
                                graph[mod].add(candidate)
                                break
            # 循环依赖检测
            cycles: list[str] = []
            seen: set[str] = set()

            def dfs(node: str, stack: list[str]) -> None:
                if node in stack:
                    cyc = " → ".join(stack[stack.index(node):] + [node])
                    if cyc not in cycles:
                        cycles.append(cyc)
                    return
                if node in seen:
                    return
                stack.append(node)
                for nxt in sorted(graph.get(node, ())):
                    dfs(nxt, stack)
                stack.pop()
                seen.add(node)

            for m in list(graph):
                dfs(m, [])
            return graph, cycles[:30]

        try:
            graph, cycles = await asyncio.to_thread(_analyze)
        except Exception as e:
            return ToolResult.fail(f"分析失败：{type(e).__name__}: {e}")
        if not graph:
            return ToolResult.text("没有找到 Python 模块。")
        lines = [f"模块依赖图（{len(graph)} 个模块）：", ""]
        edges = sum(len(v) for v in graph.values())
        for mod in sorted(graph):
            deps = sorted(graph[mod])
            if deps:
                lines.append(f"{mod}")
                for d in deps[:20]:
                    lines.append(f"  └→ {d}")
                if len(deps) > 20:
                    lines.append(f"  └→ …（还有 {len(deps) - 20} 个）")
        lines.insert(1, f"依赖边数：{edges}")
        if cycles:
            lines.append("")
            lines.append(f"⚠️ 检测到 {len(cycles)} 处循环依赖：")
            lines.extend("  " + c for c in cycles)
        return ToolResult(
            content=truncate_middle("\n".join(lines), 20000),
            display=f"依赖图（{len(graph)} 模块，{len(cycles)} 处循环）",
            data={"modules": len(graph), "edges": edges, "cycles": cycles},
        )


TOOLS = [RunTestsTool, LintTool, CodeMapTool, DependencyGraphTool]
