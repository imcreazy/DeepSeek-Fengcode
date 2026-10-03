"""自查：找出潜在的运行时问题。

检查项：
  1. 引用了不存在的 DOM 元素（$("#xxx") 但 HTML 里没有）
  2. 定义了但从未调用的函数
  3. 引用了未定义的全局变量
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HTML = ROOT / "src" / "fengcode" / "server" / "static" / "index.html"
text = HTML.read_text(encoding="utf-8")

# 拆出静态 HTML 与 script
parts = re.split(r"(<script>.*?</script>)", text, flags=re.S)
static_html = "".join(p for p in parts if not p.startswith("<script>"))
js = re.findall(r"<script>(.*?)</script>", text, re.S)[-1]

# --- 1) 检查 $() 引用的 id ---
print("=" * 62)
print("【1】引用了不存在的 DOM id")
print("=" * 62)
ids_in_html = set(re.findall(r'id="([^"]+)"', static_html))
ids_in_js_template = set(re.findall(r'id="([^"$]+)"', js))
all_ids = ids_in_html | ids_in_js_template

missing = []
for m in re.finditer(r"""\$\(\s*["']#([\w\-]+)["']""", js):
    name = m.group(1)
    if name not in all_ids:
        ln = js[:m.start()].count("\n") + 1
        missing.append((ln, name))

seen = set()
for ln, name in missing:
    if name in seen:
        continue
    seen.add(name)
    print(f"  ? #{name}   （第一次出现在 JS 第 {ln} 行）")
if not missing:
    print("  无")
else:
    print(f"\n  共 {len(seen)} 个 id 未在静态 HTML 中找到")
    print("  注意：动态生成的元素（innerHTML 里的）也会命中，需人工确认")

# --- 2) 未调用的函数 ---
print()
print("=" * 62)
print("【2】定义了但从未被调用/引用的函数")
print("=" * 62)
funcs = set(re.findall(r"^(?:async )?function (\w+)", js, re.M))
unused = []
for fn in sorted(funcs):
    # 除定义外还有出现
    uses = len(re.findall(r"\b" + re.escape(fn) + r"\b", js))
    if uses <= 1:
        unused.append(fn)
for fn in unused:
    print(f"  ? {fn}()")
if not unused:
    print("  无")

# --- 3) 常见风险写法 ---
print()
print("=" * 62)
print("【3】风险写法")
print("=" * 62)
risks = [
    (r"\$\([^)]+\)\.(onclick|onchange)\s*=", "对可能为 null 的元素直接赋事件（应加 if 判断）"),
    (r"JSON\.parse\([^)]*\)(?!\s*\))", "JSON.parse 未包 try（可能抛异常）"),
    (r"innerHTML\s*\+=", "innerHTML += 会重建 DOM，慎用"),
]
for pat, desc in risks:
    hits = []
    for m in re.finditer(pat, js):
        ln = js[:m.start()].count("\n") + 1
        line = js.split("\n")[ln - 1].strip()
        hits.append((ln, line[:80]))
    print(f"\n  {desc}")
    if hits:
        for ln, line in hits[:5]:
            print(f"    第 {ln} 行：{line}")
        if len(hits) > 5:
            print(f"    …还有 {len(hits) - 5} 处")
    else:
        print("    无")
