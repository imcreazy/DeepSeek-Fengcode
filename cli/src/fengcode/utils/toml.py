"""轻量 TOML 序列化（仅依赖标准库 ``tomllib`` 读取）。

Python 3.11+ 自带 ``tomllib`` 只读，写 TOML 需要第三方库。
为了不引入额外依赖并保证中文/特殊字符安全，这里自研一个够用的
序列化器，覆盖 Fengcode 配置出现的数据类型：

- 标量：str / int / float / bool / None
- 容器：list（同质或异质）、dict（嵌套）

输出风格：块状、可读、UTF-8 无 BOM。
"""

from __future__ import annotations

import io
import math
import re
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _fmt_str(v: str) -> str:
    out = ['"']
    for ch in v:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _fmt_key(k: str) -> str:
    if _BARE_KEY.match(k):
        return k
    return _fmt_str(k)


def _fmt_value(v: Any) -> str | None:
    """返回 TOML 值字面量；若该值本身是 dict/含 dict 的列表，返回 None 表示需另起表。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return _fmt_str(v)
    if isinstance(v, (list, tuple)):
        if any(isinstance(x, (dict, list, tuple)) for x in v):
            return None
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    return _fmt_str(str(v))


def _is_table(v: Any) -> bool:
    return isinstance(v, dict)


def _write_table(buf: list[str], table: dict, path: list[str]) -> None:
    """按 TOML 语义写出：先标量，再子表，再表数组。"""
    scalars: list[tuple[str, Any]] = []
    tables: list[tuple[str, dict]] = []
    arrays: list[tuple[str, list]] = []

    for k, v in table.items():
        if _is_table(v):
            if v and all(_is_table(x) for x in v.values()) and False:
                pass
            tables.append((k, v))
        elif isinstance(v, (list, tuple)) and v and all(_is_table(x) for x in v):
            arrays.append((k, list(v)))
        elif isinstance(v, (list, tuple)) and any(_is_table(x) for x in v):
            # 混合列表含表：TOML 不支持，退化为跳过（避免写出非法文件）
            scalars.append((k, [x for x in v if not _is_table(x)]))
        else:
            lit = _fmt_value(v)
            if lit is None:
                continue
            scalars.append((k, v))

    if path:
        header = ".".join(_fmt_key(p) for p in path)
        # 顶层表与子表之间留空行，便于阅读
        buf.append("")
        buf.append(f"[{header}]")

    for k, v in scalars:
        lit = _fmt_value(v)
        if lit is None:
            continue
        buf.append(f"{_fmt_key(k)} = {lit}")

    for k, v in tables:
        _write_table(buf, v, path + [k])

    for k, items in arrays:
        for item in items:
            header = ".".join(_fmt_key(p) for p in path + [k])
            buf.append("")
            buf.append(f"[[{header}]]")
            scalars2: list[tuple[str, Any]] = []
            tables2: list[tuple[str, dict]] = []
            arrays2: list[tuple[str, list]] = []
            for kk, vv in item.items():
                if _is_table(vv):
                    tables2.append((kk, vv))
                elif isinstance(vv, (list, tuple)) and vv and all(_is_table(x) for x in vv):
                    arrays2.append((kk, list(vv)))
                else:
                    lit = _fmt_value(vv)
                    if lit is not None:
                        scalars2.append((kk, vv))
            for kk, vv in scalars2:
                buf.append(f"{_fmt_key(kk)} = {_fmt_value(vv)}")
            for kk, vv in tables2:
                _write_table(buf, vv, path + [k, kk])
            for kk, vv in arrays2:
                for sub in vv:
                    header2 = ".".join(_fmt_key(p) for p in path + [k, kk])
                    buf.append("")
                    buf.append(f"[[{header2}]]")
                    for k3, v3 in sub.items():
                        lit = _fmt_value(v3)
                        if lit is not None:
                            buf.append(f"{_fmt_key(k3)} = {lit}")


def dumps(data: dict) -> str:
    """把 dict 序列化成 TOML 文本（UTF-8，无 BOM）。"""
    buf: list[str] = []
    _write_table(buf, data, [])
    text = "\n".join(buf).lstrip("\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def dumpf(path, data: dict) -> None:
    """写入文件：UTF-8、无 BOM、保留原编码风格。"""
    text = dumps(data)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def loadf(path) -> dict:
    """读取 TOML 文件；缺失或损坏时返回空 dict（不抛异常）。"""
    import os

    if not os.path.isfile(path):
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - 3.10 兼容
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            return {}
    try:
        with io.open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}
