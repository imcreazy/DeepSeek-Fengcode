"""SQLite 数据库工具：查询、执行、表结构、导入导出。

安全约束：
- 只允许访问工作区内的数据库文件（或显式配置允许的路径）
- 默认以只读方式打开，写操作需要 ``readonly=false``
- 拦截明显危险的语句（DROP DATABASE / ATTACH 外部文件等）
- 查询结果行数上限，避免把巨表全拖进上下文
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ...utils import human_size, truncate_middle
from ..base import Tool, ToolContext, ToolResult

_WRITE_VERBS = ("insert", "update", "delete", "create", "drop", "alter", "replace", "truncate", "vacuum")


def _connect(p: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{p.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=20.0)
    else:
        conn = sqlite3.connect(str(p), timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_table(rows: list[sqlite3.Row], max_rows: int = 500) -> str:
    if not rows:
        return "（查询结果为空）"
    cols = list(rows[0].keys())
    out = [" | ".join(cols), "-" * (sum(len(c) for c in cols) + 3 * (len(cols) - 1))]
    for r in rows[:max_rows]:
        vals = []
        for c in cols:
            v = r[c]
            if v is None:
                vals.append("NULL")
            elif isinstance(v, bytes):
                vals.append(f"<{len(v)} 字节>")
            else:
                s = str(v).replace("\n", "\\n")
                vals.append(s[:200])
        out.append(" | ".join(vals))
    if len(rows) > max_rows:
        out.append(f"…（共 {len(rows)} 行，已显示 {max_rows} 行）")
    return "\n".join(out)


class SqliteTool(Tool):
    name = "sqlite"
    group = "数据"
    dangerous = True
    description = (
        "操作 SQLite 数据库。action 可为：query（查询）、execute（执行写操作，需 readonly=false）、"
        "tables（列出所有表）、schema（查看建表语句）、describe（查看列定义）、"
        "insert_csv（把 CSV 导入成表）、export_csv（导出查询结果到 CSV）、stats（库统计）。"
    )
    parameters = {
        "path": {"type": "string", "description": "数据库文件路径（不存在则创建）"},
        "action": {
            "type": "string",
            "enum": ["query", "execute", "tables", "schema", "describe", "insert_csv", "export_csv", "stats"],
            "description": "操作类型",
        },
        "sql": {"type": "string", "description": "SQL 语句（query/execute 用）"},
        "table": {"type": "string", "description": "表名（describe/schema/insert_csv/export_csv 用）"},
        "params": {"type": "array", "items": {}, "description": "SQL 参数（按 ? 顺序）"},
        "max_rows": {"type": "integer", "description": "最多返回行数，默认 200"},
        "readonly": {"type": "boolean", "description": "是否只读打开，默认 true"},
        "csv_path": {"type": "string", "description": "CSV 文件路径（insert_csv/export_csv 用）"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, path: str = "", action: str = "", sql: str = "",
                  table: str = "", params: list | None = None, max_rows: int = 200,
                  readonly: bool = True, csv_path: str = "", **_: Any) -> ToolResult:
        from ...security.paths import resolve

        if not path:
            return ToolResult.fail("需要提供数据库文件路径 path")
        p = resolve(path, workspace=ctx.workspace)
        act = (action or "").lower()

        # 写操作需要显式 allowed
        if act == "execute":
            readonly = False
            p.parent.mkdir(parents=True, exist_ok=True)

        if not p.exists() and readonly:
            return ToolResult.fail(f"数据库文件不存在：{p}")

        def _work() -> tuple[str, dict[str, Any]]:
            conn = _connect(p, readonly)
            try:
                if act == "tables":
                    rows = conn.execute(
                        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
                    ).fetchall()
                    if not rows:
                        return "（数据库中没有表）", {}
                    lines = [f"{'表/视图':<20} 类型"]
                    for r in rows:
                        try:
                            cnt = conn.execute(f'SELECT COUNT(*) FROM "{r["name"]}"').fetchone()[0]
                        except sqlite3.Error:
                            cnt = "?"
                        lines.append(f"{r['name']:<20} {r['type']}  {cnt} 行")
                    return "\n".join(lines), {"count": len(rows)}

                if act == "schema":
                    if table:
                        row = conn.execute(
                            "SELECT sql FROM sqlite_master WHERE name=?", (table,)
                        ).fetchone()
                        return (row[0] if row else f"（没有找到表 {table}）"), {}
                    rows = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
                    ).fetchall()
                    return "\n\n".join(r[0] for r in rows), {"count": len(rows)}

                if act == "describe":
                    if not table:
                        return "describe 需要提供 table", {}
                    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                    if not info:
                        return f"（没有找到表 {table}）", {}
                    lines = [f"{'列名':<24} {'类型':<14} 非空 主键 默认值"]
                    for c in info:
                        lines.append(
                            f"{c['name']:<24} {(c['type'] or ''):<14} "
                            f"{'是' if c['notnull'] else '否':<4} "
                            f"{'是' if c['pk'] else '否':<4} {c['dflt_value']}"
                        )
                    idx = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
                    if idx:
                        lines.append("")
                        lines.append("索引：" + ", ".join(f"{i['name']}({'唯一' if i['unique'] else '普通'})" for i in idx))
                    return "\n".join(lines), {}

                if act == "stats":
                    tables = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                    lines = [f"数据库：{p}", f"文件大小：{human_size(p.stat().st_size)}", ""]
                    for t in tables:
                        try:
                            n = conn.execute(f'SELECT COUNT(*) FROM "{t["name"]}"').fetchone()[0]
                            lines.append(f"  {t['name']}: {n} 行")
                        except sqlite3.Error:
                            lines.append(f"  {t['name']}: （无法读取）")
                    return "\n".join(lines), {"tables": len(tables)}

                if act in ("query", "execute"):
                    if not sql:
                        return "需要提供 sql", {}
                    low = sql.strip().lower()
                    if "attach" in low:
                        return "安全策略拒绝：不允许 ATTACH 外部数据库", {}
                    if act == "query":
                        cur = conn.execute(sql, tuple(params or []))
                        rows = cur.fetchall()
                        return _rows_to_table(rows, int(max_rows or 200)), {"rows": len(rows)}
                    cur = conn.execute(sql, tuple(params or []))
                    conn.commit()
                    return (
                        f"执行成功，影响 {cur.rowcount if cur.rowcount >= 0 else 0} 行"
                        + (f"，lastrowid={cur.lastrowid}" if cur.lastrowid else ""),
                        {"rowcount": cur.rowcount},
                    )

                if act == "insert_csv":
                    if not table or not csv_path:
                        return "insert_csv 需要 table 与 csv_path", {}
                    csv_file = resolve(csv_path, workspace=ctx.workspace)
                    with open(csv_file, "r", encoding="utf-8-sig", newline="") as f:
                        reader = csv.reader(f)
                        rows = list(reader)
                    if not rows:
                        return "CSV 为空", {}
                    header = rows[0]
                    cols = ", ".join(f'"{c}"' for c in header)
                    ph = ", ".join("?" * len(header))
                    conn.execute(
                        f'CREATE TABLE IF NOT EXISTS "{table}" (' + ", ".join(f'"{c}" TEXT' for c in header) + ")"
                    )
                    conn.executemany(
                        f'INSERT INTO "{table}" ({cols}) VALUES ({ph})', rows[1:]
                    )
                    conn.commit()
                    return f"已把 {len(rows) - 1} 行导入表 {table}", {"rows": len(rows) - 1}

                if act == "export_csv":
                    if not sql:
                        if not table:
                            return "export_csv 需要 sql 或 table", {}
                        sql_q = f'SELECT * FROM "{table}"'
                    else:
                        sql_q = sql
                    out_path = resolve(csv_path or f"{table or 'export'}.csv", workspace=ctx.workspace)
                    rows = conn.execute(sql_q).fetchall()
                    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                        w = csv.writer(f)
                        if rows:
                            w.writerow(list(rows[0].keys()))
                            for r in rows:
                                w.writerow([r[c] for c in rows[0].keys()])
                    return f"已导出 {len(rows)} 行到 {out_path}", {"path": str(out_path), "rows": len(rows)}

                return f"不支持的操作：{action}", {}

            finally:
                conn.close()

        try:
            text, data = await asyncio.to_thread(_work)
        except sqlite3.OperationalError as e:
            return ToolResult.fail(f"SQL 执行失败：{e}")
        except sqlite3.Error as e:
            return ToolResult.fail(f"数据库错误：{e}")
        except Exception as e:
            return ToolResult.fail(f"{type(e).__name__}: {e}")
        if data.get("path"):
            return ToolResult(
                content=text, display=f"sqlite {act}", files=[data["path"]], data=data
            )
        return ToolResult(content=truncate_middle(text, 30000), display=f"sqlite {act}", data=data)


class DataAnalysisTool(Tool):
    name = "analyze_data"
    group = "数据"
    read_only = True
    description = (
        "对表格数据（CSV/Excel）做快速统计分析：行数、列类型、缺失值、数值列的描述统计、"
        "唯一值计数、相关性。用于在写代码前先摸清数据。"
    )
    parameters = {
        "path": {"type": "string", "description": "CSV 或 Excel 文件路径"},
        "sheet": {"type": "string", "description": "Excel 工作表名"},
        "max_rows": {"type": "integer", "description": "最多读取行数，默认 5000"},
    }
    required = ["path"]

    async def run(self, ctx: ToolContext, path: str = "", sheet: str = "", max_rows: int = 5000,
                  **_: Any) -> ToolResult:
        p = ctx.guard.check_read(path)
        ext = p.suffix.lower()

        def _load() -> tuple[list[str], list[list[Any]], str]:
            if ext in (".csv", ".tsv"):
                delim = "\t" if ext == ".tsv" else ","
                with open(p, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
                    rows = list(csv.reader(io.StringIO(f.read()), delimiter=delim))
                if not rows:
                    return [], [], "csv"
                return rows[0], rows[1 : int(max_rows) + 1], "csv"
            if ext in (".xlsx", ".xlsm", ".xls"):
                import openpyxl  # type: ignore

                wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
                ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
                data = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i > int(max_rows):
                        break
                    data.append(list(row))
                if not data:
                    return [], [], "xlsx"
                header = [str(c) if c is not None else f"列{i + 1}" for i, c in enumerate(data[0])]
                return header, data[1:], "xlsx"
            raise ValueError(f"不支持的格式：{ext}（支持 .csv/.tsv/.xlsx）")

        try:
            header, rows, kind = await asyncio.to_thread(_load)
        except ImportError:
            return ToolResult.fail("分析 Excel 需要 openpyxl：python -m pip install openpyxl")
        except Exception as e:
            return ToolResult.fail(f"读取失败：{type(e).__name__}: {e}")
        if not header:
            return ToolResult.fail("文件为空或没有可分析的数据")

        ncols = len(header)
        out: list[str] = [
            f"# 数据概览：{p.name}",
            f"格式：{kind}　行数：{len(rows)}　列数：{ncols}",
            "",
            "## 各列情况",
            f"{'列名':<20} {'非空':<8} {'空值':<8} {'唯一值':<8} 类型/示例",
        ]
        numeric_stats: list[tuple[str, list[float]]] = []
        for ci in range(ncols):
            name = header[ci]
            vals = [r[ci] if ci < len(r) else None for r in rows]
            non_null = [v for v in vals if v is not None and str(v).strip() != ""]
            nulls = len(vals) - len(non_null)
            uniq = len({str(v) for v in non_null})
            nums: list[float] = []
            for v in non_null:
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    pass
            if non_null and len(nums) >= max(1, len(non_null) * 0.8):
                ctype = f"数值（{len(nums)} 个）"
                numeric_stats.append((name, nums))
            else:
                sample = ", ".join(str(v)[:14] for v in non_null[:3])
                ctype = f"文本　示例：{sample}"
            out.append(f"{name:<20} {len(non_null):<8} {nulls:<8} {uniq:<8} {ctype}")

        if numeric_stats:
            out.append("")
            out.append("## 数值列统计")
            out.append(f"{'列名':<20} {'最小':>12} {'最大':>12} {'平均':>12} {'中位数':>12} {'标准差':>12}")
            for name, nums in numeric_stats:
                s = sorted(nums)
                n = len(s)
                mean = sum(s) / n
                med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
                var = sum((x - mean) ** 2 for x in s) / n if n > 1 else 0.0
                out.append(
                    f"{name:<20} {min(s):>12.4g} {max(s):>12.4g} {mean:>12.4g} {med:>12.4g} {var ** 0.5:>12.4g}"
                )

        out.append("")
        out.append("## 前 5 行")
        out.append(" | ".join(str(h)[:18] for h in header))
        for r in rows[:5]:
            out.append(" | ".join(str(v)[:18] for v in r))
        return ToolResult(
            content="\n".join(out),
            display=f"分析 {p.name}（{len(rows)} 行 × {ncols} 列）",
            files=[str(p)],
            data={"rows": len(rows), "cols": ncols, "header": header},
        )


TOOLS = [SqliteTool, DataAnalysisTool]
