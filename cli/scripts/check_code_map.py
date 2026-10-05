"""校验 docs/CODE-MAP.md 里写的「文件::符号」是否真的还存在。

为什么需要：地图只记符号名（不记行号，因为行号会漂移），但符号本身也会被改名或删除。
  地图一旦指向不存在的函数，就成了瞎话，比没有地图更坏——
  读它的人（尤其是新窗口的 AI）会照着去找、找不到、然后怀疑自己。

所以把它挂进验证链：改完代码跑一次，有对不上的条目会指名道姓报出来。

用法：
    cd D:\\Fengcode\\cli
    python scripts/check_code_map.py

退出码：0 = 全部对得上；1 = 有条目对不上（明细打印出来）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent          # D:\Fengcode\cli
PROJ = ROOT.parent                                     # D:\Fengcode
MAP_FILE = PROJ / "docs" / "CODE-MAP.md"

# 静态目录前缀：地图里 `tools/builtin/files.py` 这种相对 fengcode 包的写法，
# 也可能写成 `cli/src/fengcode/tools/...`。两种都试。
SEARCH_ROOTS = [
    ROOT / "src" / "fengcode",                         # 包内相对路径
    ROOT / "src",                                      # src 下
    ROOT,                                              # cli 下（scripts/ 等）
    PROJ,                                              # 工程根（docs/、desktop/）
    ROOT / "src" / "fengcode" / "server" / "static",   # 前端三文件
]

# 只认这些扩展名——地图里写的都是源码文件
CODE_EXTS = {".py", ".js", ".css", ".html", ".md", ".mjs", ".cjs", ".toml", ".json"}

# 一条引用：`path::symbol`，path 里含扩展名，symbol 是标识符（可带点、**可带尾括号**）。
# ★ 为什么必须允许尾括号：地图里常写成 `app.js::md()` / `agent.py::run()` 这种
#   「函数调用式」写法。上一版正则没吃 `(`，结果 100+ 条引用只认出 23 条 —— 检查形同虚设。
REF_RE = re.compile(
    r"`([A-Za-z0-9_./\\-]+\.(?:py|js|css|html|mjs|cjs))"       # 文件（含扩展名）
    r"::"                                                      # 分隔符
    r"([A-Za-z_][A-Za-z0-9_.]*)"                               # 符号
    r"(?:\(\))?"                                               # 可选的尾括号
    r"`"
)


def _locate(rel: str) -> Path | None:
    """把地图里的相对路径定位到真实文件。"""
    rel = rel.replace("\\", "/")
    for base in SEARCH_ROOTS:
        p = base / rel
        if p.is_file():
            return p
        # 地图里可能只写 `files.py`，而它在 tools/builtin/ 下 —— 全仓搜一次同名文件
    name = Path(rel).name
    hits = [p for p in (ROOT / "src").rglob(name) if p.is_file()]
    return hits[0] if len(hits) == 1 else None


def _has_symbol(path: Path, symbol: str) -> bool:
    """文件里是否定义了该符号（或其最后一个点号段）。

    `Agent.run` → 看文件里有没有 `def run`（类内方法）；
    `build_system_prompt` → 看 `def build_system_prompt`；
    `Tool` / `ApprovalGate` → 看 `class Tool`；
    前端 `md` / `handleEvent` → 看 `function md` / `function handleEvent` / `case "md"` 等。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    leaf = symbol.split(".")[-1]
    esc = re.escape(leaf)
    patterns = [
        rf"^\s*(?:async\s+)?def\s+{esc}\b",              # Python 函数/方法
        rf"^\s*class\s+{esc}\b",                         # Python 类
        rf"^\s*(?:async\s+)?function\s+{esc}\b",         # JS 函数
        rf"^\s*(?:const|let|var)\s+{esc}\s*=",           # JS 变量/箭头函数
        rf"^\s*{esc}\s*[:=]\s*",                         # 简单赋值 / 配置键
        rf"\b{esc}\s*\([^)]*\)\s*\{{",                   # 对象方法简写
    ]
    for pat in patterns:
        if re.search(pat, text, re.M):
            return True
    # 前端 CSS 类选择器、以及文档里提到的 `文件::符号` 式引用
    if f".{leaf}" in text or f'"{leaf}"' in text or f"'{leaf}'" in text:
        return True
    return False


def main() -> int:
    if not MAP_FILE.is_file():
        print(f"✗ 找不到地图文件：{MAP_FILE}")
        return 1

    md = MAP_FILE.read_text(encoding="utf-8")
    refs = REF_RE.findall(md)
    if not refs:
        print("✗ 地图里没解析到任何 `文件::符号` 引用（格式可能变了）")
        return 1

    bad: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, sym in refs:
        key = (rel, sym)
        if key in seen:
            continue
        seen.add(key)

        path = _locate(rel)
        if path is None:
            bad.append((rel, sym, "找不到文件"))
            continue
        if not _has_symbol(path, sym):
            bad.append((rel, sym, f"{path.name} 里没有这个符号"))

    total = len(seen)
    print(f"代码地图对账：检查 {total} 条引用")
    if bad:
        print(f"\n✗ 有 {len(bad)} 条对不上：\n")
        for rel, sym, why in bad:
            print(f"  {rel}::{sym}    —— {why}")
        print(
            "\n修法：① 符号改名了 → 更新 CODE-MAP.md 里的名字；"
            "② 功能删了 → 从地图里删掉这一条。"
        )
        return 1

    print(f"✓ 全部对得上（{total} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
