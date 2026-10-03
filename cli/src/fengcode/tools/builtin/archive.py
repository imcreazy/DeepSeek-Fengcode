"""归档工具：压缩包读取、创建、解压。"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

from ...utils import human_size, long_path, truncate_middle
from ..base import Tool, ToolContext, ToolResult

_ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".tar.gz", ".tar.bz2", ".tar.xz",
                ".7z", ".rar", ".jar", ".whl", ".apk", ".egg"}


def _norm_ext(p: Path) -> str:
    name = p.name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz"):
        if name.endswith(ext):
            return ext
    return p.suffix.lower()


def _is_safe_member(name: str, dest: Path) -> bool:
    """防止 zip slip：解压路径必须落在目标目录内。"""
    try:
        target = (dest / name).resolve()
    except OSError:
        return False
    try:
        return target == dest.resolve() or str(target).startswith(str(dest.resolve()) + os.sep)
    except OSError:
        return str(target).startswith(str(dest))


class ReadArchiveTool(Tool):
    name = "read_archive"
    group = "文档"
    read_only = True
    description = (
        "查看压缩包内容或读取其中某个文件。action=list 列出条目，action=read 读取指定文件内容。"
        "支持 zip / tar / tar.gz / tar.bz2 / tar.xz / jar / whl。"
    )
    parameters = {
        "path": {"type": "string", "description": "压缩包路径"},
        "action": {"type": "string", "enum": ["list", "read"], "description": "操作，默认 list"},
        "member": {"type": "string", "description": "action=read 时要读取的条目名"},
        "max_entries": {"type": "integer", "description": "最多列出条目数，默认 300"},
        "max_chars": {"type": "integer", "description": "读取内容上限，默认 30000"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", action: str = "list", member: str = "",
                  max_entries: int = 300, max_chars: int = 30000, **_: Any) -> ToolResult:
        p = ctx.guard.check_read(path)
        action = (action or "list").lower()
        try:
            entries = await asyncio.to_thread(_list_archive, p)
        except zipfile.BadZipFile as e:
            return ToolResult.fail(f"不是有效的压缩包：{e}")
        except Exception as e:
            return ToolResult.fail(f"读取压缩包失败：{type(e).__name__}: {e}")

        if action == "list":
            total_size = sum(e["size"] for e in entries)
            lines = [
                f"压缩包：{p.name}（{human_size(p.stat().st_size)}，共 {len(entries)} 个条目，"
                f"解压后约 {human_size(total_size)}）",
                "",
            ]
            for e in entries[: int(max_entries)]:
                kind = "[目录]" if e["dir"] else "[文件]"
                lines.append(f"{kind} {e['name']}  {human_size(e['size'])}")
            if len(entries) > int(max_entries):
                lines.append(f"…（还有 {len(entries) - int(max_entries)} 个条目未显示）")
            return ToolResult(
                content="\n".join(lines),
                display=f"列出 {p.name}（{len(entries)} 项）",
                data={"entries": entries[:500], "count": len(entries)},
            )

        if not member:
            names = ", ".join(e["name"] for e in entries[:20])
            return ToolResult.fail(f"action=read 需要提供 member。可用条目示例：{names}")
        try:
            data = await asyncio.to_thread(_read_member, p, member)
        except KeyError:
            return ToolResult.fail(f"压缩包里没有条目：{member}")
        except Exception as e:
            return ToolResult.fail(f"读取条目失败：{e}")
        if isinstance(data, bytes):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = data.decode("gb18030")
                except UnicodeDecodeError:
                    return ToolResult.fail(
                        f"{member} 是二进制内容（{human_size(len(data))}），无法按文本读取。"
                        "可先用 action=extract 解压到工作区再用 read_file。"
                    )
        else:
            text = data
        limit = int(max_chars or 30000)
        truncated = len(text) > limit
        return ToolResult(
            content=f"条目 {member}（{len(text)} 字符）：\n\n" + (
                truncate_middle(text, limit) if truncated else text
            ),
            display=f"读取 {member}",
            truncated=truncated,
        )


class ExtractArchiveTool(Tool):
    name = "extract_archive"
    group = "文档"
    dangerous = True
    description = "把压缩包解压到指定目录（默认工作区下的同名目录）。会自动防御路径穿越。"
    parameters = {
        "path": {"type": "string", "description": "压缩包路径"},
        "target": {"type": "string", "description": "解压目标目录，默认工作区"},
        "members": {"type": "array", "items": {"type": "string"}, "description": "只解压这些条目"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", target: str = "",
                  members: list[str] | None = None, **_: Any) -> ToolResult:
        from ...security.paths import resolve

        p = ctx.guard.check_read(path)
        dest = resolve(target, workspace=ctx.workspace) if target else (ctx.workspace / p.stem)
        dest.mkdir(parents=True, exist_ok=True)
        ext = _norm_ext(p)

        def _do() -> int:
            n = 0
            if ext in (".zip", ".jar", ".whl", ".apk", ".egg"):
                with zipfile.ZipFile(long_path(p)) as z:
                    for info in z.infolist():
                        if members and info.filename not in members:
                            continue
                        if not _is_safe_member(info.filename, dest):
                            continue
                        z.extract(info, str(dest))
                        n += 1
            elif ext in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"):
                mode = "r:*"
                with tarfile.open(long_path(p), mode) as t:
                    for info in t.getmembers():
                        if members and info.name not in members:
                            continue
                        if not _is_safe_member(info.name, dest):
                            continue
                        try:
                            t.extract(info, str(dest), filter="data")
                        except TypeError:
                            t.extract(info, str(dest))
                        n += 1
            elif ext in (".gz", ".bz2", ".xz"):
                with open(long_path(p), "rb") as f:
                    raw = f.read()
                if ext == ".gz":
                    import gzip

                    out = gzip.decompress(raw)
                elif ext == ".bz2":
                    import bz2

                    out = bz2.decompress(raw)
                else:
                    import lzma

                    out = lzma.decompress(raw)
                name = p.stem
                (dest / name).write_bytes(out)
                n = 1
            else:
                raise ValueError(f"不支持的压缩格式：{ext}（.7z/.rar 请先安装 7-Zip 并解压）")
            return n

        try:
            n = await asyncio.to_thread(_do)
        except ValueError as e:
            return ToolResult.fail(str(e))
        except Exception as e:
            return ToolResult.fail(f"解压失败：{type(e).__name__}: {e}")
        return ToolResult(
            content=f"已解压 {n} 个条目到 {dest}",
            display=f"解压 {p.name} → {dest.name}/",
            files=[str(dest)],
            data={"count": n, "target": str(dest)},
        )


class CreateArchiveTool(Tool):
    name = "create_archive"
    group = "文档"
    dangerous = True
    description = "创建压缩包（zip 或 tar.gz），可指定要打包的文件与目录。"
    parameters = {
        "target": {"type": "string", "description": "输出压缩包路径（.zip/.tar.gz/.tgz）"},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "要打包的文件/目录"},
        "format": {"type": "string", "enum": ["zip", "tar.gz", "tar"], "description": "格式，默认按扩展名推断"},
    }
    required = ["target", "paths"]

    async def run(self, ctx: ToolContext, target: str = "", paths: list[str] | None = None,
                  format: str = "", **_: Any) -> ToolResult:
        from ...security.paths import resolve

        if not paths:
            return ToolResult.fail("paths 不能为空")
        out = resolve(target, workspace=ctx.workspace)
        ctx.guard.check_write(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        sources = []
        for s in paths:
            try:
                sources.append(ctx.guard.check_read(s))
            except Exception as e:
                return ToolResult.fail(f"源路径不可读：{s}（{e}）")
        fmt = (format or "").lower()
        if not fmt:
            low = out.name.lower()
            fmt = "tar.gz" if low.endswith((".tar.gz", ".tgz")) else ("tar" if low.endswith(".tar") else "zip")

        def _do() -> int:
            n = 0
            if fmt == "zip":
                with zipfile.ZipFile(long_path(out), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                    for s in sources:
                        if s.is_file():
                            z.write(long_path(s), s.name)
                            n += 1
                        else:
                            for dirpath, _dirnames, files in os.walk(s):
                                for fn in files:
                                    full = Path(dirpath) / fn
                                    z.write(long_path(full), str(full.relative_to(s.parent)))
                                    n += 1
            else:
                mode = "w:gz" if fmt == "tar.gz" else "w"
                with tarfile.open(long_path(out), mode) as t:
                    for s in sources:
                        t.add(long_path(s), arcname=s.name)
                        n += 1 if s.is_file() else 0
            return n

        try:
            n = await asyncio.to_thread(_do)
        except Exception as e:
            return ToolResult.fail(f"打包失败：{type(e).__name__}: {e}")
        return ToolResult(
            content=f"已创建 {out}（{human_size(out.stat().st_size)}，含 {n} 个文件）",
            display=f"打包 {out.name}（{human_size(out.stat().st_size)}）",
            files=[str(out)],
        )


def _list_archive(p: Path) -> list[dict[str, Any]]:
    ext = _norm_ext(p)
    out: list[dict[str, Any]] = []
    if ext in (".zip", ".jar", ".whl", ".apk", ".egg"):
        with zipfile.ZipFile(long_path(p)) as z:
            for info in z.infolist():
                out.append(
                    {"name": info.filename, "size": info.file_size, "dir": info.is_dir(),
                     "mtime": info.date_time}
                )
        return out
    if ext in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"):
        with tarfile.open(long_path(p), "r:*") as t:
            for info in t.getmembers():
                out.append(
                    {"name": info.name, "size": info.size, "dir": info.isdir(),
                     "mtime": info.mtime}
                )
        return out
    raise ValueError(
        f"暂不支持列出 {ext} 格式（.7z/.rar 需外部工具）。可先解压再查看。"
    )


def _read_member(p: Path, member: str) -> bytes | str:
    ext = _norm_ext(p)
    if ext in (".zip", ".jar", ".whl", ".apk", ".egg"):
        with zipfile.ZipFile(long_path(p)) as z:
            return z.read(member)
    if ext in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"):
        with tarfile.open(long_path(p), "r:*") as t:
            f = t.extractfile(member)
            if f is None:
                raise KeyError(member)
            return f.read()
    raise ValueError(f"不支持的格式：{ext}")


TOOLS = [ReadArchiveTool, ExtractArchiveTool, CreateArchiveTool]
