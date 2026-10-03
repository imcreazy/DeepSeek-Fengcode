"""文件系统工具：读、写、编辑、追加、删除、移动、复制、列目录、搜索、树。

设计要点
--------
- 所有路径都过 ``PathGuard``（读写白名单 + 危险模式）
- 读取带行号输出，便于模型精确引用
- 写入采用原子替换，并自动生成备份（可关闭）
- 编辑走精确字符串替换（要求唯一匹配），避免模糊改坏文件
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ...security.paths import PathNotAllowed, resolve
from ...utils import (
    display_path,
    human_size,
    iter_files,
    long_path,
    snapshot_path,
    truncate_middle,
)
from ..base import Tool, ToolContext, ToolResult

# ---- ★ 2-F：记录「读取时观察到的版本」 -----------------------------------
# 为什么需要：并发场景下（用户自己在编辑器里改、或是另一个进程），
# edit_file 拿着**过期的旧文本**去替换，可能改错位置甚至把别人的改动顶掉。
# 做法：read_file 时记下 (mtime, size)；edit_file 前比对，不一致就要求重新读取。
# 只在「确实读过」的前提下才校验 —— 没读过就不拦（不做无谓的打扰）。
_READ_STAMPS: dict[str, tuple[float, int]] = {}


def _stamp_of(p: Path) -> tuple[float, int] | None:
    try:
        st = p.stat()
        return (float(st.st_mtime), int(st.st_size))
    except OSError:
        return None


def _remember_read(p: Path) -> None:
    s = _stamp_of(p)
    if s is not None:
        _READ_STAMPS[str(p).lower()] = s


def _check_stale(p: Path) -> str | None:
    """文件在「被读取之后」是否又被改动过？返回错误说明或 None。"""
    key = str(p).lower()
    was = _READ_STAMPS.get(key)
    if was is None:
        return None
    now = _stamp_of(p)
    if now is None:
        return None
    if now != was:
        return (
            f"{display_path(p)} 在读取之后已被改动（读取时 {was[1]} 字节，"
            f"现在 {now[1]} 字节）。为避免覆盖别人的修改，请先重新 read_file 确认当前内容。"
        )
    return None

# ---- 文本检测 ------------------------------------------------------------

_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svgz", ".tif", ".tiff",
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".tar", ".zst",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class", ".pyc", ".pyo",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".db", ".sqlite", ".sqlite3", ".mdb", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".lib", ".pdb", ".obj", ".swp", ".lock",
}

MAX_READ_BYTES = 8 * 1024 * 1024
# 单行长度上限：防止压缩过的 minified 文件把上下文撑爆
MAX_LINE_CHARS = 2000
# 单次读取返回的最大字符数（48K 上限）
MAX_READ_OUTPUT_CHARS = 48_000


def looks_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXT:
        return True
    try:
        with open(long_path(path), "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return False
    if b"\x00" in chunk:
        return True
    # 控制字符比例过高也判为二进制
    ctrl = sum(1 for b in chunk if b < 9 or (13 < b < 32))
    return bool(chunk) and ctrl / len(chunk) > 0.25


def read_text_smart(path: Path, encoding: str = "auto") -> tuple[str, str]:
    """读取文本，自动识别常见编码；返回 ``(文本, 实际编码)``。"""
    raw = Path(long_path(path)).read_bytes()
    if len(raw) > MAX_READ_BYTES:
        raw = raw[:MAX_READ_BYTES]
    if encoding and encoding != "auto":
        return raw.decode(encoding, "replace"), encoding
    # BOM 优先
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", "replace"), "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace"), "utf-16-be"
    for enc in ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def detect_eol(text: str) -> str:
    """检测文本主换行符：返回 ``"\\r\\n"`` 或 ``"\\n"``。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def normalize_eol(text: str, eol: str) -> str:
    """把文本里的换行统一成 ``eol``。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", eol)


def numbered_lines(text: str, start: int = 1, width: int = 5) -> str:
    lines = text.split("\n")
    return "\n".join(f"{i + start:>{width}}→ {l}" for i, l in enumerate(lines))


# ---- 读取 ----------------------------------------------------------------

class ReadFileTool(Tool):
    name = "read_file"
    group = "文件"
    read_only = True
    description = (
        "读取文本文件内容，带行号返回。可用 offset/limit 读取片段，适合大文件。"
        "支持自动识别编码（UTF-8/GBK 等）。图片与二进制文件会被拒绝，请改用其他工具。"
    )
    parameters = {
        "path": {"type": "string", "description": "文件路径（相对路径基于当前工作区）"},
        "offset": {"type": "integer", "description": "起始行号（从 1 开始），默认 1"},
        "limit": {"type": "integer", "description": "最多读取行数，默认 800"},
        "encoding": {"type": "string", "description": "强制编码，默认自动识别"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", offset: int = 1, limit: int = 800,
                  encoding: str = "auto", **_: Any) -> ToolResult:
        p = ctx.guard.check_read(path)
        if not p.is_file():
            return ToolResult.fail(f"不是文件或不存在：{p}")
        if looks_binary(p):
            size = p.stat().st_size if p.exists() else 0
            return ToolResult.fail(
                f"{display_path(p)} 是二进制文件（{human_size(size)}），无法按文本读取。"
                "若要分析内容，请用 parse_document / read_image / read_archive 等工具。"
            )
        try:
            text, enc = await asyncio.to_thread(read_text_smart, p, encoding)
        except OSError as e:
            return ToolResult.fail(f"读取失败：{e}")
        _remember_read(p)   # ★ 2-F：记下这次读到的版本，供 edit_file 比对
        lines = text.split("\n")
        total = len(lines)
        start = max(1, int(offset or 1))
        count = max(1, int(limit or 800))
        seg = lines[start - 1 : start - 1 + count]
        # 超长单行截断（防 minified 文件）
        clipped = 0
        shown_lines = []
        for ln in seg:
            if len(ln) > MAX_LINE_CHARS:
                shown_lines.append(
                    ln[:MAX_LINE_CHARS] + f"  …（该行共 {len(ln)} 字符，已截断）"
                )
                clipped += 1
            else:
                shown_lines.append(ln)
        body = numbered_lines("\n".join(shown_lines), start=start)
        truncated = start - 1 + count < total
        footer = ""
        if truncated:
            footer = f"\n\n…（共 {total} 行，已显示第 {start}–{start + len(seg) - 1} 行，可用 offset 继续）"
        if clipped:
            footer += f"\n（{clipped} 行因过长被截断显示）"
        head = f"文件：{display_path(p)}（{human_size(p.stat().st_size)}，编码 {enc}，共 {total} 行）\n\n"
        out = head + body + footer
        if len(out) > MAX_READ_OUTPUT_CHARS:
            out = truncate_head_tail(out, MAX_READ_OUTPUT_CHARS, reason="单次读取上限")
            truncated = True
        return ToolResult(
            content=out,
            display=f"读取 {display_path(p)}（{len(seg)} 行）",
            files=[str(p)],
            truncated=truncated,
            data={"total_lines": total, "encoding": enc, "path": str(p)},
        )


class ReadManyTool(Tool):
    name = "read_many"
    group = "文件"
    read_only = True
    description = "一次读取多个文件（每个带路径标题），适合同时看几个相关文件。"
    parameters = {
        "paths": {"type": "array", "items": {"type": "string"}, "description": "文件路径列表"},
        "limit_each": {"type": "integer", "description": "每个文件最多读取行数，默认 300"},
    }
    required = ["paths"]

    async def run(self, ctx: ToolContext, paths: list[str] | None = None, limit_each: int = 300,
                  **_: Any) -> ToolResult:
        if not paths:
            return ToolResult.fail("paths 不能为空")
        parts: list[str] = []
        files: list[str] = []
        for pth in paths[:20]:
            try:
                p = ctx.guard.check_read(pth)
                if looks_binary(p):
                    parts.append(f"### {display_path(p)}\n（二进制文件，已跳过）")
                    continue
                text, enc = await asyncio.to_thread(read_text_smart, p)
                lines = text.split("\n")
                seg = lines[: max(1, int(limit_each))]
                tail = f"\n…（共 {len(lines)} 行）" if len(lines) > len(seg) else ""
                parts.append(
                    f"### {display_path(p)}（{len(lines)} 行，编码 {enc}）\n```\n"
                    + numbered_lines("\n".join(seg))
                    + tail
                    + "\n```"
                )
                files.append(str(p))
            except (PathNotAllowed, OSError) as e:
                parts.append(f"### {pth}\n（读取失败：{e}）")
        return ToolResult(content="\n\n".join(parts), display=f"读取 {len(files)} 个文件", files=files)


# ---- 写入与编辑 ----------------------------------------------------------

class WriteFileTool(Tool):
    name = "write_file"
    group = "文件"
    dangerous = True
    description = (
        "写入（覆盖）一个文本文件，自动创建父目录。默认会先备份原文件。"
        "新建文件请直接用本工具；修改大文件请优先用 edit_file 精确替换。"
    )
    parameters = {
        "path": {"type": "string", "description": "目标文件路径"},
        "content": {"type": "string", "description": "要写入的完整内容"},
        "append": {"type": "boolean", "description": "是否追加而不是覆盖，默认 false"},
        "backup": {"type": "boolean", "description": "覆盖前是否备份原文件，默认 true"},
        "encoding": {"type": "string", "description": "写入编码，默认 utf-8"},
    }
    required = ["path", "content"]

    async def run(self, ctx: ToolContext, path: str = "", content: str = "", append: bool = False,
                  backup: bool = True, encoding: str = "utf-8", **_: Any) -> ToolResult:
        p = ctx.guard.check_write(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        enc = encoding if encoding and encoding != "auto" else "utf-8"
        existed = p.exists()
        old_size = p.stat().st_size if existed else 0
        try:
            if append:
                with io.open(long_path(p), "a", encoding=enc, newline="") as f:
                    f.write(content)
            else:
                if existed and backup:
                    bak = snapshot_path(p)
                    await asyncio.to_thread(shutil.copy2, long_path(p), long_path(bak))
                tmp = p.with_name(p.name + ".fctmp")
                with io.open(long_path(tmp), "w", encoding=enc, newline="") as f:
                    f.write(content)
                os.replace(long_path(tmp), long_path(p))
        except OSError as e:
            return ToolResult.fail(f"写入失败：{e}")
        size = p.stat().st_size
        lines = content.count("\n") + 1
        action = "追加" if append else ("覆盖" if existed else "创建")
        return ToolResult(
            content=f"已{action} {display_path(p)}（{human_size(size)}，{lines} 行）",
            display=f"{action} {display_path(p)} {human_size(size)}",
            files=[str(p)],
            data={"path": str(p), "bytes": size, "lines": lines, "existed": existed,
                  "old_size": old_size, "append": append},
        )


class EditFileTool(Tool):
    name = "edit_file"
    group = "文件"
    dangerous = True
    description = (
        "精确替换文件中的一段文本。old_string 必须在文件中唯一出现（否则报错并提示），"
        "这样能避免改错位置。替换全部出现请把 replace_all 设为 true。"
        "（换行符会自动适配文件本身，CRLF 文件也能用 LF 文本匹配）"
    )
    parameters = {
        "path": {"type": "string", "description": "要修改的文件路径"},
        "old_string": {"type": "string", "description": "要被替换的原文本（需唯一匹配）"},
        "new_string": {"type": "string", "description": "替换成的新文本"},
        "replace_all": {"type": "boolean", "description": "是否替换所有出现，默认 false"},
        "backup": {"type": "boolean", "description": "是否备份原文件，默认 true"},
    }
    required = ["path", "old_string", "new_string"]

    async def run(self, ctx: ToolContext, path: str = "", old_string: str = "",
                  new_string: str = "", replace_all: bool = False, backup: bool = True,
                  **_: Any) -> ToolResult:
        p = ctx.guard.check_write(path, new_file=False)
        if looks_binary(p):
            return ToolResult.fail(f"{display_path(p)} 是二进制文件，无法文本编辑")
        # ★ 2-F：写前核对「读取时观察到的版本」，避免拿过期文本改错位置
        stale = _check_stale(p)
        if stale:
            return ToolResult.fail(stale)
        try:
            text, enc = await asyncio.to_thread(read_text_smart, p)
        except OSError as e:
            return ToolResult.fail(f"读取失败：{e}")
        if old_string == "":
            return ToolResult.fail("old_string 不能为空（要清空文件请用 write_file）")
        # 把匹配文本与文件统一到同一换行符再比，避免 CRLF 文件匹配失败
        eol = detect_eol(text)
        old_c = normalize_eol(old_string, eol)
        new_c = normalize_eol(new_string, eol)
        count = text.count(old_c)
        if count == 0:
            hint = _closest_hint(text, old_string)
            return ToolResult.fail(f"在 {display_path(p)} 中未找到要替换的文本。{hint}")
        if count > 1 and not replace_all:
            return ToolResult.fail(
                f"要替换的文本在文件中出现了 {count} 次，无法确定改哪一处。"
                "请增加上下文使其唯一，或设置 replace_all=true 全部替换。"
            )
        # ⚠️ 不用 str.replace 的字面替换语义会踩 "$" 展开坑吗？Python 不会，
        # 但为与其它语言实现保持一致、并支持计数，这里显式实现分段拼接。
        new_text = _replace_literal(text, old_c, new_c, 1 if not replace_all else -1)
        try:
            if backup:
                bak = snapshot_path(p)
                await asyncio.to_thread(shutil.copy2, long_path(p), long_path(bak))
            tmp = p.with_name(p.name + ".fctmp")
            with io.open(long_path(tmp), "w", encoding=enc, newline="") as f:
                f.write(new_text)
            os.replace(long_path(tmp), long_path(p))
        except OSError as e:
            return ToolResult.fail(f"写入失败：{e}")
        n = count if replace_all else 1
        diff = make_diff(text, new_text, path=display_path(p))
        return ToolResult(
            content=f"已修改 {display_path(p)}：替换 {n} 处（编码 {enc}，换行 {eol!r}）\n\n{diff}",
            display=f"编辑 {display_path(p)}（{n} 处）",
            files=[str(p)],
            data={"replacements": n, "path": str(p), "diff": diff[:8000]},
        )


class MultiEditTool(Tool):
    name = "multi_edit"
    group = "文件"
    dangerous = True
    description = "在一次调用中对同一文件执行多处精确替换（按顺序应用，任一处失败则整体不写入）。"
    parameters = {
        "path": {"type": "string", "description": "要修改的文件路径"},
        "edits": {
            "type": "array",
            "description": "编辑列表，每项包含 old_string 与 new_string",
            "items": {
                "type": "object",
                "properties": {
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
            },
        },
        "backup": {"type": "boolean", "description": "是否备份原文件，默认 true"},
    }
    required = ["path", "edits"]

    async def run(self, ctx: ToolContext, path: str = "", edits: list[dict] | None = None,
                  backup: bool = True, **_: Any) -> ToolResult:
        if not edits:
            return ToolResult.fail("edits 不能为空")
        p = ctx.guard.check_write(path, new_file=False)
        # ★ 2-F：同 edit_file —— 写前核对读取时的版本
        stale = _check_stale(p)
        if stale:
            return ToolResult.fail(stale)
        text, enc = await asyncio.to_thread(read_text_smart, p)
        eol = detect_eol(text)
        applied = 0
        for i, ed in enumerate(edits, 1):
            if not isinstance(ed, dict):
                return ToolResult.fail(f"第 {i} 项编辑格式不正确")
            old = ed.get("old_string", "")
            new = ed.get("new_string", "")
            if not old:
                return ToolResult.fail(f"第 {i} 项 old_string 为空")
            old_c = normalize_eol(old, eol)
            new_c = normalize_eol(new, eol)
            c = text.count(old_c)
            if c == 0:
                return ToolResult.fail(
                    f"第 {i} 项要替换的文本未找到，已放弃全部修改。{_closest_hint(text, old)}"
                )
            if c > 1 and not ed.get("replace_all"):
                return ToolResult.fail(f"第 {i} 项文本出现 {c} 次，请增加上下文或设置 replace_all")
            text = _replace_literal(text, old_c, new_c, -1 if ed.get("replace_all") else 1)
            applied += c if ed.get("replace_all") else 1
        if backup:
            bak = snapshot_path(p)
            await asyncio.to_thread(shutil.copy2, long_path(p), long_path(bak))
        tmp = p.with_name(p.name + ".fctmp")
        with io.open(long_path(tmp), "w", encoding=enc, newline="") as f:
            f.write(text)
        os.replace(long_path(tmp), long_path(p))
        return ToolResult(
            content=f"已对 {display_path(p)} 应用 {len(edits)} 项编辑（共 {applied} 处替换）",
            display=f"编辑 {display_path(p)}（{applied} 处）",
            files=[str(p)],
        )


class ApplyPatchTool(Tool):
    name = "apply_patch"
    group = "文件"
    dangerous = True
    description = (
        "应用一段 unified diff 补丁（适用于对多处、跨文件的修改）。"
        "补丁格式为标准的 ---/+++/@@ 形式。"
    )
    parameters = {
        "patch": {"type": "string", "description": "unified diff 格式的补丁文本"},
        "base_dir": {"type": "string", "description": "补丁中相对路径的基准目录，默认当前工作区"},
        "dry_run": {"type": "boolean", "description": "仅检查能否应用，不真正写入"},
    }
    required = ["patch"]

    async def run(self, ctx: ToolContext, patch: str = "", base_dir: str = "",
                  dry_run: bool = False, **_: Any) -> ToolResult:
        base = Path(base_dir) if base_dir else ctx.workspace
        files = _parse_unified_diff(patch)
        if not files:
            return ToolResult.fail("未能从补丁中解析出任何文件块（请检查是否为标准 unified diff 格式）")
        results: list[str] = []
        changed: list[str] = []
        plan: list[tuple[Path, str, str]] = []
        for rel, hunks in files:
            target = (base / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
            try:
                p = ctx.guard.check_write(target)
            except PathNotAllowed as e:
                return ToolResult.fail(f"补丁目标不允许写入：{e}")
            if not p.exists():
                return ToolResult.fail(f"补丁目标文件不存在：{p}（新建文件请用 write_file）")
            text, enc = await asyncio.to_thread(read_text_smart, p)
            new_text, err = _apply_hunks(text, hunks)
            if err:
                return ToolResult.fail(f"{display_path(p)}：{err}")
            plan.append((p, new_text, enc))
            results.append(f"{display_path(p)}：{len(hunks)} 个 hunk")
        if dry_run:
            return ToolResult.text("补丁可应用（dry_run）：\n" + "\n".join(results), files=[str(x[0]) for x in plan])
        for p, new_text, enc in plan:
            bak = snapshot_path(p)
            await asyncio.to_thread(shutil.copy2, long_path(p), long_path(bak))
            tmp = p.with_name(p.name + ".fctmp")
            with io.open(long_path(tmp), "w", encoding=enc, newline="\n") as f:
                f.write(new_text)
            os.replace(long_path(tmp), long_path(p))
            changed.append(str(p))
        return ToolResult(
            content="补丁已应用：\n" + "\n".join(results),
            display=f"应用补丁（{len(changed)} 个文件）",
            files=changed,
        )


# ---- 目录与搜索 ----------------------------------------------------------

class ListDirTool(Tool):
    name = "list_dir"
    group = "文件"
    read_only = True
    description = "列出目录内容（区分目录/文件，显示大小与修改时间），支持通配符过滤。"
    parameters = {
        "path": {"type": "string", "description": "目录路径，默认当前工作区"},
        "pattern": {"type": "string", "description": "通配符过滤，如 *.py"},
        "recursive": {"type": "boolean", "description": "是否递归列出，默认 false"},
        "max_entries": {"type": "integer", "description": "最多列出条目数，默认 200"},
    }
    required = []

    async def run(self, ctx: ToolContext, path: str = ".", pattern: str = "",
                  recursive: bool = False, max_entries: int = 200, **_: Any) -> ToolResult:
        base = resolve(path or ".", workspace=ctx.workspace)
        if not base.exists():
            return ToolResult.fail(f"路径不存在：{base}")
        if base.is_file():
            return ToolResult.text(_fmt_entry(base))
        entries: list[str] = []
        if recursive:
            for f in iter_files(base, max_files=int(max_entries) * 4):
                if pattern and not f.match(pattern):
                    continue
                entries.append(_fmt_entry(f, base))
                if len(entries) >= int(max_entries):
                    break
        else:
            try:
                items = sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except OSError as e:
                return ToolResult.fail(f"无法列出目录：{e}")
            for it in items:
                if pattern and not it.match(pattern):
                    continue
                entries.append(_fmt_entry(it))
                if len(entries) >= int(max_entries):
                    break
        head = f"目录：{display_path(base)}（{len(entries)} 项）"
        return ToolResult(
            content=head + "\n" + "\n".join(entries),
            display=f"列目录 {display_path(base)}（{len(entries)} 项）",
            data={"count": len(entries)},
        )


def _fmt_entry(p: Path, root: Path | None = None) -> str:
    try:
        st = p.stat()
        size = human_size(st.st_size)
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
    except OSError:
        size, mtime = "?", "?"
    name = str(p.relative_to(root)) if root else p.name
    if p.is_dir():
        return f"[目录] {name}/"
    return f"[文件] {name}  {size}  {mtime}"


class GlobTool(Tool):
    name = "glob"
    group = "文件"
    read_only = True
    description = "按文件名模式查找文件（如 **/*.py、src/**/*.ts），返回匹配路径列表。"
    parameters = {
        "pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
        "path": {"type": "string", "description": "搜索根目录，默认当前工作区"},
        "max_results": {"type": "integer", "description": "最多返回数量，默认 200"},
    }
    required = ["pattern"]

    async def run(self, ctx: ToolContext, pattern: str = "", path: str = ".",
                  max_results: int = 200, **_: Any) -> ToolResult:
        root = resolve(path or ".", workspace=ctx.workspace)
        if not root.is_dir():
            return ToolResult.fail(f"不是目录：{root}")
        out: list[str] = []
        try:
            for p in root.rglob(pattern):
                if any(part in (".git", "node_modules", "__pycache__", ".venv") for part in p.parts):
                    continue
                out.append(str(p.relative_to(root)).replace("\\", "/") + ("/" if p.is_dir() else ""))
                if len(out) >= int(max_results):
                    break
        except OSError as e:
            return ToolResult.fail(f"查找失败：{e}")
        if not out:
            return ToolResult.text(f"在 {display_path(root)} 下没有匹配 “{pattern}” 的文件。", data={"count": 0})
        return ToolResult(
            content=f"在 {display_path(root)} 下匹配 “{pattern}” 共 {len(out)} 项：\n" + "\n".join(sorted(out)),
            display=f"找到 {len(out)} 个文件",
            data={"count": len(out), "matches": out},
        )


class GrepTool(Tool):
    name = "grep"
    group = "文件"
    read_only = True
    description = (
        "在文件内容中搜索文本或正则表达式，返回匹配行（带文件名与行号）。"
        "等价于 ripgrep，但没有外部依赖。"
    )
    parameters = {
        "query": {"type": "string", "description": "要搜索的文本或正则"},
        "path": {"type": "string", "description": "搜索根目录或文件，默认当前工作区"},
        "pattern": {"type": "string", "description": "只搜索匹配此 glob 的文件，如 *.py"},
        "regex": {"type": "boolean", "description": "是否按正则解释 query，默认 false"},
        "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 true"},
        "context": {"type": "integer", "description": "额外显示匹配行前后各几行，默认 0"},
        "max_results": {"type": "integer", "description": "最多返回匹配数，默认 120"},
    }
    required = ["query"]

    async def run(self, ctx: ToolContext, query: str = "", path: str = ".", pattern: str = "",
                  regex: bool = False, ignore_case: bool = True, context: int = 0,
                  max_results: int = 120, **_: Any) -> ToolResult:
        root = resolve(path or ".", workspace=ctx.workspace)
        if not query:
            return ToolResult.fail("query 不能为空")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            rx = re.compile(query if regex else re.escape(query), flags)
        except re.error as e:
            return ToolResult.fail(f"正则表达式无效：{e}")

        targets: list[Path] = []
        if root.is_file():
            targets = [root]
        else:
            targets = list(iter_files(root, exts=_glob_to_exts(pattern), max_files=6000))

        hits: list[str] = []
        matched_files: set[str] = set()
        ctx_lines = max(0, int(context or 0))
        for f in targets:
            if looks_binary(f):
                continue
            try:
                text, _ = await asyncio.to_thread(read_text_smart, f)
            except (OSError, UnicodeError):
                continue
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                rel = display_path(f)
                matched_files.add(str(f))
                if ctx_lines:
                    lo = max(0, i - ctx_lines)
                    hi = min(len(lines), i + ctx_lines + 1)
                    block = [f"{rel}:{j + 1}: {lines[j]}" for j in range(lo, hi)]
                    hits.append("\n".join(block))
                else:
                    hits.append(f"{rel}:{i + 1}: {line.strip()[:400]}")
                if len(hits) >= int(max_results):
                    break
            if len(hits) >= int(max_results):
                break
        if not hits:
            return ToolResult.text(
                f"未找到 “{query}”（搜索了 {len(targets)} 个文件）。",
                data={"count": 0, "files_scanned": len(targets)},
            )
        body = "\n\n".join(hits) if ctx_lines else "\n".join(hits)
        return ToolResult(
            content=f"匹配 {len(hits)} 处（{len(matched_files)} 个文件）：\n{body}",
            display=f"grep “{truncate_middle(query, 30)}” 命中 {len(hits)} 处",
            files=sorted(matched_files),
            data={"count": len(hits)},
        )


def _glob_to_exts(pattern: str) -> list[str] | None:
    if not pattern or "*" not in pattern:
        return None
    m = re.match(r"^\*?\.?([A-Za-z0-9_]+)$", pattern.replace("**/", "").replace("*.", "*."))
    exts = re.findall(r"\*\.([A-Za-z0-9_]+)", pattern)
    if exts:
        return exts
    return None


# ---- 其他文件操作 --------------------------------------------------------

class FileOpsTool(Tool):
    name = "file_ops"
    group = "文件"
    dangerous = True
    description = (
        "文件与目录操作合集：删除(delete)、移动(move)、复制(copy)、"
        "建目录(mkdir)、查看信息(info)、修改权限(chmod)。删除默认进回收站式的备份目录，"
        "只有 recursive=true 且确认后才真删。"
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["delete", "move", "copy", "mkdir", "info", "chmod", "touch"],
            "description": "要执行的操作",
        },
        "path": {"type": "string", "description": "源路径"},
        "target": {"type": "string", "description": "目标路径（move/copy 用）"},
        "recursive": {"type": "boolean", "description": "是否递归处理目录，默认 false"},
        "mode": {"type": "string", "description": "chmod 的权限，如 755"},
    }
    required = ["action", "path"]

    async def run(self, ctx: ToolContext, action: str = "", path: str = "", target: str = "",
                  recursive: bool = False, mode: str = "", **_: Any) -> ToolResult:
        from ... import paths as P

        action = (action or "").lower()
        if action in ("info",):
            p = resolve(path, workspace=ctx.workspace)
            if not p.exists():
                return ToolResult.fail(f"路径不存在：{p}")
            st = p.stat()
            info = [
                f"路径：{p}",
                f"类型：{'目录' if p.is_dir() else '文件' if p.is_file() else '其他'}",
                f"大小：{human_size(st.st_size)}（{st.st_size} 字节）",
                f"创建：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_ctime))}",
                f"修改：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}",
                f"权限：{oct(st.st_mode)[-3:]}",
            ]
            if p.is_dir():
                try:
                    info.append(f"条目数：{len(list(p.iterdir()))}")
                except OSError:
                    pass
            try:
                info.append(f"可写：{os.access(p, os.W_OK)}")
            except OSError:
                pass
            return ToolResult.text("\n".join(info), data={"path": str(p)})

        if action == "mkdir":
            p = ctx.guard.check_write(Path(path).resolve() if os.path.isabs(path) else (ctx.workspace / path))
            p.mkdir(parents=True, exist_ok=True)
            return ToolResult.text(f"已创建目录：{display_path(p)}", files=[str(p)])

        if action == "touch":
            p = ctx.guard.check_write(Path(path).resolve() if os.path.isabs(path) else (ctx.workspace / path))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
            return ToolResult.text(f"已创建/更新时间戳：{display_path(p)}", files=[str(p)])

        if action == "chmod":
            p = ctx.guard.check_write(path, new_file=False)
            if not mode:
                return ToolResult.fail("chmod 需要提供 mode，如 755")
            try:
                os.chmod(long_path(p), int(mode, 8))
            except (OSError, ValueError) as e:
                return ToolResult.fail(f"修改权限失败：{e}")
            return ToolResult.text(f"已设置 {display_path(p)} 权限为 {mode}")

        if action in ("move", "copy"):
            if not target:
                return ToolResult.fail(f"{action} 需要提供 target")
            src = ctx.guard.check_read(path)
            dst = resolve(target, workspace=ctx.workspace)
            if dst.is_dir():
                dst = dst / src.name
            ctx.guard.check_write(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                if action == "move":
                    if dst.exists():
                        bak = snapshot_path(dst)
                        await asyncio.to_thread(shutil.move, long_path(dst), long_path(bak))
                    await asyncio.to_thread(shutil.move, long_path(src), long_path(dst))
                else:
                    if src.is_dir() or (dst.exists() and dst.is_dir()):
                        await asyncio.to_thread(
                            shutil.copytree, long_path(src), long_path(dst), dirs_exist_ok=True
                        )
                    else:
                        await asyncio.to_thread(shutil.copy2, long_path(src), long_path(dst))
            except OSError as e:
                return ToolResult.fail(f"{action} 失败：{e}")
            verb = "移动" if action == "move" else "复制"
            return ToolResult.text(f"已{verb}：{display_path(src)} → {display_path(dst)}", files=[str(dst)])

        if action == "delete":
            p = ctx.guard.check_write(path, new_file=False)
            if p.is_dir() and not recursive:
                return ToolResult.fail(f"{display_path(p)} 是目录，删除需要 recursive=true")
            # 安全起见：删除先移到备份目录（可恢复）
            backup_root = P.cache_dir() / "deleted"
            backup_root.mkdir(parents=True, exist_ok=True)
            dest = backup_root / f"{p.name}-{int(time.time())}"
            try:
                # 符号链接 / Windows junction 只删链接本身，绝不能跟着进目标
                # （实测教训：Windows 上递归删除会穿透 junction 删掉目标内容）
                if _is_link_like(p):
                    await asyncio.to_thread(_unlink_link_only, p)
                    return ToolResult.text(
                        f"已删除链接 {display_path(p)}（只删了链接，其指向的目标未被触碰）"
                    )
                await asyncio.to_thread(shutil.move, long_path(p), long_path(dest))
            except OSError as e:
                return ToolResult.fail(f"删除失败：{e}")
            return ToolResult.text(
                f"已删除 {display_path(p)}（已移入备份目录 {display_path(dest)}，可恢复）",
                data={"backup": str(dest)},
            )
        return ToolResult.fail(f"不支持的操作：{action}（可选：delete/move/copy/mkdir/info/chmod/touch）")


class BackupListTool(Tool):
    name = "file_history"
    group = "文件"
    read_only = True
    description = "查看某个文件的历史备份（每次 write_file/edit_file 都会自动生成），可用于回滚。"
    parameters = {
        "path": {"type": "string", "description": "文件路径"},
        "restore": {"type": "string", "description": "要恢复的备份文件名（留空只列出）"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", restore: str = "", **_: Any) -> ToolResult:
        p = resolve(path, workspace=ctx.workspace)
        baks = sorted(p.parent.glob(p.name + ".bak-*")) + sorted(p.parent.glob(p.name + ".bak"))
        if not baks:
            return ToolResult.text(f"{display_path(p)} 没有历史备份。")
        if not restore:
            lines = [f"{display_path(p)} 共 {len(baks)} 个备份："]
            for b in baks:
                try:
                    st = b.stat()
                    lines.append(
                        f"  {b.name}  {human_size(st.st_size)}  "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}"
                    )
                except OSError:
                    lines.append(f"  {b.name}")
            lines.append('\n用 file_history(path=..., restore="备份文件名") 可恢复。')
            return ToolResult.text("\n".join(lines))
        target = next((b for b in baks if b.name == restore or b.name.endswith(restore)), None)
        if target is None:
            return ToolResult.fail(f"未找到备份：{restore}")
        if p.exists():
            snapshot = snapshot_path(p, suffix="prerestore")
            await asyncio.to_thread(shutil.copy2, long_path(p), long_path(snapshot))
        await asyncio.to_thread(shutil.copy2, long_path(target), long_path(p))
        return ToolResult.text(f"已从 {target.name} 恢复 {display_path(p)}", files=[str(p)])


# ---- diff 与截断辅助 -----------------------------------------------------

def _is_link_like(p: Path) -> bool:
    """判断是否为符号链接或 Windows junction（而非普通目录/文件）。"""
    try:
        if p.is_symlink():
            return True
    except OSError:
        return False
    import stat as _stat

    try:
        st = os.lstat(long_path(p))
        if _stat.S_ISLNK(st.st_mode):
            return True
        # Windows 的 junction 带有 reparse point 属性
        if os.name == "nt":
            FILE_ATTRIBUTE_REPARSE_POINT = 0x400
            if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                return True
    except OSError:
        return False
    return False


def _unlink_link_only(p: Path) -> None:
    """只删除链接本身，不跟随到目标（OSError 会向上抛）。"""
    target = long_path(p)
    if os.name == "nt":
        # Windows：先试 unlink（对文件链接有效），junction 需 rmdir
        try:
            os.unlink(target)
            return
        except OSError:
            pass
        os.rmdir(target)   # junction / 目录符号链接
        return
    os.unlink(target)


def _replace_literal(text: str, old: str, new: str, count: int) -> str:
    """字面替换（不做任何模式展开）；``count<0`` 表示替换全部。"""
    if not old:
        return text
    parts: list[str] = []
    start = 0
    done = 0
    while True:
        idx = text.find(old, start)
        if idx < 0:
            break
        if 0 <= count <= done:
            break
        parts.append(text[start:idx])
        parts.append(new)
        start = idx + len(old)
        done += 1
    parts.append(text[start:])
    return "".join(parts)


def make_diff(old: str, new: str, *, path: str = "", max_lines: int = 200,
              context: int = 2) -> str:
    """生成精简的 unified diff（只显示改动附近）。

    实现思路：先裁掉公共前后缀，只输出变更区域，
    避免大文件改动时把整个文件塞进上下文。
    """
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    # 公共前缀
    start = 0
    while (start < len(old_lines) and start < len(new_lines)
           and old_lines[start] == new_lines[start]):
        start += 1
    # 公共后缀
    oe, ne = len(old_lines), len(new_lines)
    while oe > start and ne > start and old_lines[oe - 1] == new_lines[ne - 1]:
        oe -= 1
        ne -= 1

    removed = old_lines[start:oe]
    added = new_lines[start:ne]
    if not removed and not added:
        return "（内容无变化）"

    # 预算分配：两边都不至于被完全丢弃
    removed_budget = len(removed)
    added_budget = len(added)
    if removed_budget + added_budget > max_lines:
        removed_budget = min(len(removed), max(max_lines // 2, max_lines - len(added)))
        added_budget = min(len(added), max_lines - removed_budget)

    out: list[str] = ["```diff"]
    # 上文（带真实行号，便于直接跳转定位）
    for i in range(max(0, start - context), start):
        out.append(f" {i + 1:>5} | {old_lines[i]}")
    for i in range(removed_budget):
        out.append(f"-{start + i + 1:>5} | {removed[i]}")
    for i in range(added_budget):
        out.append(f"+{start + i + 1:>5} | {added[i]}")
    # 下文
    for i in range(ne, min(len(old_lines), ne + context)):
        out.append(f" {i + 1:>5} | {old_lines[i]}")
    omitted_r = len(removed) - removed_budget
    omitted_a = len(added) - added_budget
    if omitted_r > 0 or omitted_a > 0:
        out.append(f"…（省略 {omitted_r} 行删除、{omitted_a} 行新增）")
    out.append("```")
    head = (
        f"{path}：第 {start + 1}–{max(start + 1, ne)} 行，-{len(removed)} / +{len(added)}\n"
        if path
        else ""
    )
    return head + "\n".join(out)


def truncate_head_tail(text: str, limit: int, *, reason: str = "") -> str:
    """头尾保留式截断。

    提示语**必须留在保留区（头/尾）里**，不能放进被省略的中间：后续环节
    可能再做一次更紧的中间截断，放中间的提示会被二次切掉，导致
    "如何取回完整内容"的指引丢失。

    返回总长度不会超过 limit（提示语长度已计入预算）。
    """
    if text is None:
        return ""
    if limit <= 0 or len(text) <= limit:
        return text or ""

    note = f"\n…（内容被截断：共 {len(text)} 字符"
    if reason:
        note += f"，{reason}"
    note += "。可用 offset/limit 或 grep 查看被省略的部分）\n"

    # 给提示语留出空间；若 limit 太小则退化为纯截断
    budget = limit - len(note)
    if budget < 20:
        return text[: max(0, limit)]
    head = budget // 2
    tail = max(1, budget - head)
    return text[:head] + note + text[-tail:]


def _closest_hint(text: str, needle: str) -> str:
    """在找不到时给一个"是不是想找这个"的提示。"""
    first = (needle or "").strip().split("\n")[0].strip()[:40]
    if not first:
        return ""
    idx = text.find(first)
    if idx < 0:
        return "（文件中没有相似片段，请先 read_file 确认当前内容）"
    line_no = text[:idx].count("\n") + 1
    around = text[max(0, idx - 60) : idx + 160].replace("\n", "\\n")
    return f"（第 {line_no} 行附近有相似内容：{around[:200]}）"


def _parse_unified_diff(patch: str) -> list[tuple[str, list[tuple[int, list[str], list[str]]]]]:
    """解析 unified diff，返回 ``[(文件路径, [(起始行, 旧行, 新行)])]``。"""
    files: list[tuple[str, list[tuple[int, list[str], list[str]]]]] = []
    cur_path: str | None = None
    hunks: list[tuple[int, list[str], list[str]]] = []
    lines = patch.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            if cur_path and hunks:
                files.append((cur_path, hunks))
            hunks = []
            cur_path = None
            i += 1
            continue
        if line.startswith("+++ "):
            raw = line[4:].strip().split("\t")[0]
            cur_path = raw[2:] if raw.startswith(("a/", "b/")) else raw
            i += 1
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if not m:
                i += 1
                continue
            start = int(m.group(1))
            old_lines: list[str] = []
            new_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@", "--- ", "+++ ")):
                ln = lines[i]
                if ln.startswith("-"):
                    old_lines.append(ln[1:])
                elif ln.startswith("+"):
                    new_lines.append(ln[1:])
                elif ln.startswith(" "):
                    old_lines.append(ln[1:])
                    new_lines.append(ln[1:])
                elif ln.startswith("\\"):
                    pass
                else:
                    break
                i += 1
            hunks.append((start, old_lines, new_lines))
            continue
        i += 1
    if cur_path and hunks:
        files.append((cur_path, hunks))
    return files


def _apply_hunks(text: str, hunks: list[tuple[int, list[str], list[str]]]) -> tuple[str, str | None]:
    """应用 hunks；失败返回 ``(原文, 错误信息)``。"""
    src = text.split("\n")
    offset = 0
    for start, old, new in hunks:
        idx = start - 1 + offset
        # 允许 ±5 行漂移（对方改了上下文）
        found = -1
        for shift in range(0, 6):
            for cand in (idx + shift, idx - shift):
                if cand < 0 or cand + len(old) > len(src):
                    continue
                if src[cand : cand + len(old)] == old:
                    found = cand
                    break
            if found >= 0:
                break
        if found < 0:
            head = "\n".join(old[:4])
            return text, f"无法定位补丁片段（原文件第 {start} 行附近）。期望内容：\n{head}"
        src[found : found + len(old)] = new
        offset += len(new) - len(old)
    return "\n".join(src), None


TOOLS = [
    ReadFileTool,
    ReadManyTool,
    WriteFileTool,
    EditFileTool,
    MultiEditTool,
    ApplyPatchTool,
    ListDirTool,
    GlobTool,
    GrepTool,
    FileOpsTool,
    BackupListTool,
]
