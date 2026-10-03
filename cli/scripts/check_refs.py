"""检查是否引用了已删除的元素。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HTML = ROOT / "src" / "fengcode" / "server" / "static" / "index.html"
text = HTML.read_text(encoding="utf-8")
js_path = ROOT / "src" / "fengcode" / "server" / "static" / "app.js"
js = js_path.read_text(encoding="utf-8")
static_html = text

all_ids = set(re.findall(r'id="([^"]+)"', static_html))
all_ids |= set(re.findall(r'id="([^"$]+)"', js))

GONE = ["btn-attach", "btn-ref-file", "btn-ref-session",
        "mode-chip-caret", "perm-chip-caret", "ro-toggle", "side-collapse"]

print("=" * 60)
print("已删除元素的残留引用")
print("=" * 60)
bad = 0
for g in GONE:
    n = len(re.findall(r'#?' + re.escape(g) + r'\b', js))
    n_html = text.count('id="' + g + '"')
    status = "✓" if (n == 0 and n_html == 0) else "✗"
    if status == "✗":
        bad += 1
    print(f"  {status} {g:20} JS 引用 {n} 处 / HTML 定义 {n_html} 处")

print()
print("=" * 60)
print("JS 里引用但 HTML 没有的 id（排除动态生成的）")
print("=" * 60)
DYNAMIC_OK = {"choice-pop", "plus-pop", "settings-body", "settings-side",
              "ss-host", "ss-back", "ss-filter", "file-preview",
              "page-", "app-", "ap-", "set-", "rule-",   # 模板拼接前缀，非真引用
              # ★ 下面几个都是 JS 里**动态创建**的节点，HTML 里本就不该有：
              #   tooldelta = 工具参数流式占位（ph.id = "tooldelta"）
              #   ask- / askin- = 提问卡与其输入框（id 带随机后缀）
              #   mh- = 记忆条目就地展开的修订历史容器（id 带记忆 id）
              "tooldelta", "ask-", "askin-", "mh-"}
missing = []
for m in re.finditer(r"""\$\(\s*["']#([\w\-]+)["']""", js):
    name = m.group(1)
    if name not in all_ids and name not in DYNAMIC_OK:
        ln = js[:m.start()].count("\n") + 1
        missing.append((ln, name))
seen = set()
for ln, name in missing:
    if name in seen:
        continue
    seen.add(name)
    print(f"  ? #{name}（JS 第 {ln} 行）")
if not missing:
    print("  无")

print()
print(f"结论：{'有问题' if bad or missing else '干净'}")
