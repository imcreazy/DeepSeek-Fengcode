"""文档解析工具：PDF / Word / Excel / PPT / 纯文本 / 图片。

不引入重依赖：分别用 pypdf、pdfplumber、python-docx、openpyxl 读取，
缺失时给出明确的安装提示而不是崩掉。
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any

from ...utils import human_size, truncate_middle
from ..base import Tool, ToolContext, ToolResult

_TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
             ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".env", ".toml", ".py", ".js",
             ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php",
             ".sh", ".bat", ".ps1", ".sql", ".html", ".htm", ".css", ".scss", ".vue", ".jsx",
             ".tsx", ".lua", ".kt", ".swift", ".r", ".m", ".pl", ".gradle", ".properties"}


def _missing(pkg: str, what: str) -> ToolResult:
    return ToolResult.fail(
        f"解析{what}需要 {pkg} 库，当前未安装。可以执行：\n"
        f"  python -m pip install {pkg}\n"
        f"或者用 install_packages(packages=['{pkg}']) 让助手自己安装。"
    )


class ParseDocumentTool(Tool):
    name = "parse_document"
    group = "文档"
    read_only = True
    description = (
        "解析文档并返回文本内容。支持 PDF、Word(.docx)、Excel(.xlsx/.xls)、PPT(.pptx)、"
        "CSV/TSV、JSON、以及各种纯文本/源码文件。是读取文档的首选工具。"
    )
    parameters = {
        "path": {"type": "string", "description": "文档路径"},
        "max_chars": {"type": "integer", "description": "最多返回字符数，默认 30000"},
        "pages": {"type": "string", "description": "PDF 页码范围，如 1-5 或 3，默认全部"},
        "sheets": {"type": "array", "items": {"type": "string"}, "description": "Excel 工作表名（默认全部）"},
        "tables_only": {"type": "boolean", "description": "PDF/Excel 是否只提取表格"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", max_chars: int = 30000,
                  pages: str = "", sheets: list[str] | None = None, tables_only: bool = False,
                  **_: Any) -> ToolResult:
        p = ctx.guard.check_read(path)
        ext = p.suffix.lower()
        limit = int(max_chars or 30000)
        try:
            if ext == ".pdf":
                text, meta = await asyncio.to_thread(_read_pdf, p, pages, tables_only)
            elif ext in (".docx", ".doc"):
                if ext == ".doc":
                    return ToolResult.fail("不支持老式 .doc 格式，请先另存为 .docx 或转为 PDF")
                text, meta = await asyncio.to_thread(_read_docx, p)
            elif ext in (".xlsx", ".xlsm", ".xls"):
                text, meta = await asyncio.to_thread(_read_excel, p, sheets)
            elif ext == ".pptx":
                text, meta = await asyncio.to_thread(_read_pptx, p)
            elif ext in (".csv", ".tsv"):
                text, meta = await asyncio.to_thread(_read_csv, p, ext)
            elif ext in (".json", ".jsonl"):
                text, meta = await asyncio.to_thread(_read_json_file, p)
            elif ext in _TEXT_EXT or ext == "":
                raw = p.read_text(encoding="utf-8", errors="replace")
                text, meta = raw, {"类型": "纯文本"}
            else:
                return ToolResult.fail(
                    f"暂不支持解析 {ext} 格式。纯文本类可直接用 read_file；"
                    "图片可用 read_image；压缩包可用 read_archive。"
                )
        except Exception as e:
            return ToolResult.fail(f"解析 {p.name} 失败：{type(e).__name__}: {e}")

        truncated = len(text) > limit
        shown = truncate_middle(text, limit) if truncated else text
        head = f"文档：{p.name}（{human_size(p.stat().st_size)}）"
        if meta:
            head += "\n" + "\n".join(f"{k}：{v}" for k, v in meta.items())
        if truncated:
            head += f"\n（原文 {len(text)} 字符，已截断）"
        return ToolResult(
            content=f"{head}\n\n{shown}",
            display=f"解析 {p.name}（{len(text)} 字符）",
            files=[str(p)],
            truncated=truncated,
            data={"length": len(text), "meta": meta, "path": str(p)},
        )


def _read_pdf(p: Path, pages: str, tables_only: bool) -> tuple[str, dict[str, Any]]:
    try:
        import pypdf  # type: ignore
    except ImportError:
        raise ImportError("pypdf")
    reader = pypdf.PdfReader(str(p))
    total = len(reader.pages)
    idxs = _parse_pages(pages, total)
    parts: list[str] = []
    if not tables_only:
        for i in idxs:
            try:
                t = reader.pages[i].extract_text() or ""
            except Exception as e:
                t = f"（第 {i + 1} 页提取失败：{e}）"
            parts.append(f"===== 第 {i + 1} 页 =====\n{t.strip()}")
    else:
        parts.append("（仅提取表格）")
    # 表格用 pdfplumber
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(p)) as pdf:
            for i in idxs:
                if i >= len(pdf.pages):
                    continue
                for ti, tb in enumerate(pdf.pages[i].extract_tables() or [], 1):
                    rows = ["\t".join(str(c or "") for c in (row or [])) for row in tb]
                    parts.append(f"===== 第 {i + 1} 页 表格 {ti} =====\n" + "\n".join(rows))
    except ImportError:
        if tables_only:
            raise
    except Exception:
        pass
    meta = {"页数": total, "已提取页": f"{len(idxs)} 页"}
    info = reader.metadata or {}
    for k, label in (("/Title", "标题"), ("/Author", "作者"), ("/CreationDate", "创建时间")):
        if info.get(k):
            meta[label] = str(info[k])
    return "\n\n".join(parts), meta


def _parse_pages(spec: str, total: int) -> list[int]:
    if not spec:
        return list(range(total))
    out: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            out.extend(range(max(1, lo) - 1, min(total, hi)))
        elif part.isdigit():
            n = int(part)
            if 1 <= n <= total:
                out.append(n - 1)
    return sorted(set(out)) or list(range(total))


def _read_docx(p: Path) -> tuple[str, dict[str, Any]]:
    try:
        import docx  # type: ignore
    except ImportError:
        raise ImportError("python-docx")
    doc = docx.Document(str(p))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text.strip():
            style = para.style.name if para.style else ""
            prefix = ""
            if style.startswith("Heading"):
                lvl = re.sub(r"\D", "", style) or "1"
                prefix = "#" * min(6, int(lvl)) + " "
            parts.append(prefix + para.text.strip())
    for ti, table in enumerate(doc.tables, 1):
        parts.append(f"\n【表格 {ti}】")
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    meta = {"段落数": len(doc.paragraphs), "表格数": len(doc.tables)}
    return "\n".join(parts), meta


def _read_excel(p: Path, sheets: list[str] | None) -> tuple[str, dict[str, Any]]:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        raise ImportError("openpyxl")
    wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
    parts: list[str] = []
    names = sheets or list(wb.sheetnames)
    for name in names:
        if name not in wb.sheetnames:
            continue
        ws = wb[name]
        parts.append(f"===== 工作表：{name}（{ws.max_row} 行 × {ws.max_column} 列）=====")
        for ri, row in enumerate(ws.iter_rows(values_only=True), 1):
            if ri > 500:
                parts.append(f"…（超过 500 行已截断）")
                break
            vals = ["" if v is None else str(v) for v in row]
            if any(v.strip() for v in vals):
                parts.append(" | ".join(vals))
        parts.append("")
    meta = {"工作表": ", ".join(wb.sheetnames)}
    return "\n".join(parts), meta


def _read_pptx(p: Path) -> tuple[str, dict[str, Any]]:
    try:
        from pptx import Presentation  # type: ignore
    except ImportError:
        raise ImportError("python-pptx")
    prs = Presentation(str(p))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        texts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs)
                    if t.strip():
                        texts.append(t.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    texts.append(" | ".join(c.text.strip() for c in row.cells))
        parts.append(f"===== 第 {i} 页 =====\n" + "\n".join(texts))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            parts.append("【备注】" + slide.notes_slide.notes_text_frame.text.strip())
    meta = {"幻灯片数": len(prs.slides)}
    return "\n\n".join(parts), meta


def _read_csv(p: Path, ext: str) -> tuple[str, dict[str, Any]]:
    delim = "\t" if ext == ".tsv" else ","
    raw = p.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(raw), delimiter=delim))
    parts: list[str] = []
    if rows:
        parts.append("表头：" + " | ".join(rows[0]))
        parts.append("")
    for i, row in enumerate(rows[1:501], 1):
        parts.append(f"{i}. " + " | ".join(row))
    if len(rows) > 501:
        parts.append(f"…（共 {len(rows)} 行，已显示 500 行）")
    return "\n".join(parts), {"行数": len(rows), "列数": len(rows[0]) if rows else 0}


def _read_json_file(p: Path) -> tuple[str, dict[str, Any]]:
    if p.suffix.lower() == ".jsonl":
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        objs = []
        for ln in lines[:400]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                objs.append(json.loads(ln))
            except ValueError:
                objs.append(ln)
        return "\n".join(json.dumps(o, ensure_ascii=False) for o in objs), {"行数": len(lines)}
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except ValueError as e:
        return p.read_text(encoding="utf-8", errors="replace"), {"警告": f"JSON 解析失败：{e}"}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    meta = {"类型": type(data).__name__}
    if isinstance(data, (list, dict)):
        meta["元素数"] = len(data)
    return text, meta


class ReadImageTool(Tool):
    name = "read_image"
    group = "文档"
    read_only = True
    description = (
        "读取图片文件并附加到对话中（交给视觉模型分析）。"
        "会返回图片的基本信息，并把图片作为附件传给模型。"
    )
    parameters = {
        "path": {"type": "string", "description": "图片路径"},
        "detail": {"type": "string", "description": "分析侧重，如“描述内容”“提取文字”“看错在哪”"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", detail: str = "", **_: Any) -> ToolResult:
        from ...llm.types import Attachment

        p = ctx.guard.check_read(path)
        ext = p.suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return ToolResult.fail(f"不是支持的图片格式：{ext}")
        info = {"路径": str(p), "大小": human_size(p.stat().st_size)}
        try:
            from PIL import Image  # type: ignore

            with Image.open(p) as im:
                info["尺寸"] = f"{im.width}×{im.height}"
                info["模式"] = im.mode
        except ImportError:
            pass
        except Exception:
            pass
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}.get(ext.lstrip("."), "image/png")
        att = Attachment(kind="image", path=str(p), mime=mime, name=p.name)
        lines = [f"已读取图片 {p.name}"] + [f"{k}：{v}" for k, v in info.items()]
        if detail:
            lines.append(f"请重点分析：{detail}")
        return ToolResult(
            content="\n".join(lines),
            display=f"读取图片 {p.name}",
            files=[str(p)],
            data={**info, "_attach": [{"kind": "image", "path": str(p), "mime": mime, "name": p.name}]},
        )


class MakeChartTool(Tool):
    name = "make_chart"
    group = "文档"
    dangerous = True
    description = "用 matplotlib 生成图表图片并保存到工作区（折线/柱状/饼图/散点/直方图）。"
    parameters = {
        "kind": {"type": "string", "enum": ["line", "bar", "pie", "scatter", "hist"], "description": "图表类型"},
        "data": {"type": "object", "description": "数据：{x: [...], y: [...]} 或 {labels:[...], values:[...]}"},
        "title": {"type": "string", "description": "标题"},
        "xlabel": {"type": "string", "description": "X 轴标签"},
        "ylabel": {"type": "string", "description": "Y 轴标签"},
        "path": {"type": "string", "description": "保存路径，默认工作区下 chart.png"},
        "dpi": {"type": "integer", "description": "分辨率，默认 120"},
    }
    required = ["kind", "data"]

    async def run(self, ctx: ToolContext, kind: str = "line", data: dict | None = None, title: str = "",
                  xlabel: str = "", ylabel: str = "", path: str = "", dpi: int = 120,
                  **_: Any) -> ToolResult:
        try:
            import matplotlib  # type: ignore

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt  # type: ignore
        except ImportError:
            return ToolResult.fail(
                "生成图表需要 matplotlib：python -m pip install matplotlib"
            )
        data = data or {}
        target = resolve(path or "chart.png", workspace=ctx.workspace)
        ctx.guard.check_write(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 中文支持
        for fname in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC", "Arial Unicode MS"):
            try:
                matplotlib.rcParams["font.sans-serif"] = [fname]
                break
            except Exception:
                continue
        matplotlib.rcParams["axes.unicode_minus"] = False

        def _plot() -> None:
            fig, ax = plt.subplots(figsize=(9, 5), dpi=int(dpi))
            if kind in ("line", "bar", "scatter"):
                x = data.get("x") or data.get("labels") or list(range(len(data.get("y") or data.get("values") or [])))
                y = data.get("y") or data.get("values") or []
                if kind == "line":
                    ax.plot(x, y, marker="o", linewidth=2)
                elif kind == "bar":
                    ax.bar([str(v) for v in x], y, color="#4f46e5")
                else:
                    ax.scatter(x, y, color="#4f46e5")
            elif kind == "pie":
                labels = data.get("labels") or []
                values = data.get("values") or []
                ax.pie(values, labels=[str(v) for v in labels], autopct="%1.1f%%", startangle=90)
                ax.axis("equal")
            elif kind == "hist":
                values = data.get("values") or data.get("y") or []
                ax.hist(values, bins=int(data.get("bins", 20)), color="#4f46e5")
            if title:
                ax.set_title(title)
            if xlabel:
                ax.set_xlabel(xlabel)
            if ylabel:
                ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            fig.savefig(str(target))
            plt.close(fig)

        try:
            await asyncio.to_thread(_plot)
        except Exception as e:
            return ToolResult.fail(f"生成图表失败：{type(e).__name__}: {e}")
        return ToolResult(
            content=f"已生成图表：{target}（{human_size(target.stat().st_size)}）",
            display=f"生成图表 {target.name}",
            files=[str(target)],
            data={"_attach": [{"kind": "image", "path": str(target), "mime": "image/png", "name": target.name}]},
        )


class WriteOfficeTool(Tool):
    name = "write_office"
    group = "文档"
    dangerous = True
    description = "生成 Office 文档：Word(.docx)、Excel(.xlsx)、PPT(.pptx)、或 CSV。"
    parameters = {
        "path": {"type": "string", "description": "输出文件路径（扩展名决定格式）"},
        "content": {
            "type": "object",
            "description": "内容结构：{title, paragraphs:[...], tables:[[[...]]], sheets:{名:[[...]]}, slides:[{title, bullets:[...]}]}",
        },
    }
    required = ["path", "content"]

    async def run(self, ctx: ToolContext, path: str = "", content: dict | None = None, **_: Any) -> ToolResult:
        target = resolve(path, workspace=ctx.workspace)
        ctx.guard.check_write(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = content or {}
        ext = target.suffix.lower()
        try:
            if ext == ".docx":
                await asyncio.to_thread(_write_docx, target, content)
            elif ext in (".xlsx", ".xlsm"):
                await asyncio.to_thread(_write_xlsx, target, content)
            elif ext == ".pptx":
                await asyncio.to_thread(_write_pptx, target, content)
            elif ext in (".csv", ".tsv"):
                await asyncio.to_thread(_write_csv, target, content, ext)
            elif ext in (".md", ".txt"):
                target.write_text(str(content.get("text") or content.get("content") or ""), encoding="utf-8")
            else:
                return ToolResult.fail(f"不支持的输出格式：{ext}（支持 .docx/.xlsx/.pptx/.csv/.md/.txt）")
        except ImportError as e:
            return _missing(str(e), ext)
        except Exception as e:
            return ToolResult.fail(f"生成失败：{type(e).__name__}: {e}")
        return ToolResult(
            content=f"已生成文档：{target}（{human_size(target.stat().st_size)}）",
            display=f"生成 {target.name}",
            files=[str(target)],
        )


def _write_docx(target: Path, content: dict) -> None:
    import docx  # type: ignore

    doc = docx.Document()
    if content.get("title"):
        doc.add_heading(str(content["title"]), level=0)
    for para in content.get("paragraphs") or []:
        doc.add_paragraph(str(para))
    for tbl in content.get("tables") or []:
        if not tbl:
            continue
        rows = len(tbl)
        cols = max(len(r) for r in tbl)
        table = doc.add_table(rows=rows, cols=cols)
        table.style = "Table Grid"
        for i, row in enumerate(tbl):
            for j in range(cols):
                table.cell(i, j).text = str(row[j]) if j < len(row) else ""
    if content.get("text"):
        doc.add_paragraph(str(content["text"]))
    doc.save(str(target))


def _write_xlsx(target: Path, content: dict) -> None:
    import openpyxl  # type: ignore
    from openpyxl.styles import Font  # type: ignore

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sheets = content.get("sheets") or {}
    if not sheets and content.get("tables"):
        sheets = {f"Sheet{i}": t for i, t in enumerate(content["tables"], 1)}
    if not sheets:
        sheets = {"Sheet1": [["内容"], [str(content.get("text") or "")]]}
    for name, rows in sheets.items():
        ws = wb.create_sheet(str(name)[:31])
        for row in rows:
            ws.append([str(c) if c is not None else "" for c in row])
        if rows:
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for i, _ in enumerate(rows[0], 1):
                ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 18
    wb.save(str(target))


def _write_pptx(target: Path, content: dict) -> None:
    from pptx import Presentation  # type: ignore
    from pptx.util import Pt  # type: ignore

    prs = Presentation()
    slides = content.get("slides") or []
    if not slides:
        slides = [{"title": content.get("title") or "演示文稿", "bullets": content.get("paragraphs") or []}]
    for s in slides:
        layout = prs.slide_layouts[1] if len(prs.slide_layouts) > 1 else prs.slide_layouts[0]
        slide = prs.slides.add_slide(layout)
        if slide.shapes.title:
            slide.shapes.title.text = str(s.get("title") or "")
        body = None
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 1:
                body = ph.text_frame
                break
        if body is not None:
            for i, b in enumerate(s.get("bullets") or []):
                p = body.paragraphs[0] if i == 0 else body.add_paragraph()
                p.text = str(b)
                p.font.size = Pt(int(s.get("font_size", 18)))
    prs.save(str(target))


def _write_csv(target: Path, content: dict, ext: str) -> None:
    delim = "\t" if ext == ".tsv" else ","
    rows = content.get("rows") or content.get("tables", [[]])[0] if content.get("tables") else content.get("rows")
    rows = rows or []
    with open(target, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=delim)
        for row in rows:
            w.writerow([str(c) if c is not None else "" for c in row])


TOOLS = [ParseDocumentTool, ReadImageTool, MakeChartTool, WriteOfficeTool]
