"""JS 语法检查：定位前端脚本并做 node --check。

支持两种形态（拆分前后都能用）：
1. 外链：static/app.js
2. 内联：index.html 里的 <script>...</script>
另外也扫一遍 index.html 自身的内联小脚本（若有）。
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC = ROOT / "cli" / "src" / "fengcode" / "server" / "static"
APP_JS = STATIC / "app.js"
HTML = STATIC / "index.html"

targets: list[tuple[str, str]] = []

# 1) 优先外链文件
if APP_JS.exists():
    targets.append(("app.js", APP_JS.read_text(encoding="utf-8")))

# 2) index.html 里的内联脚本（拆分后通常没有；保留兼容）
if HTML.exists():
    text = HTML.read_text(encoding="utf-8")
    for i, blk in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)):
        if blk.strip():
            targets.append((f"index.html#inline{i}", blk))

if not targets:
    print("没有找到任何 JS 脚本")
    sys.exit(1)

print(f"发现 {len(targets)} 个脚本：")
for name, js in targets:
    print(f"  {name}: {len(js):,} 字符, {js.count(chr(10)) + 1} 行")

failed = False
for name, js in targets:
    tmp = Path(tempfile.gettempdir()) / "fengcode_check.js"
    tmp.write_text(js, encoding="utf-8", newline="\n")
    cp = subprocess.run(
        ["node", "--check", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print()
    if cp.returncode == 0:
        print(f"✓ {name} 语法正确")
        continue

    failed = True
    print(f"✗ {name} 语法错误：")
    err = (cp.stderr or "") + (cp.stdout or "")
    print(err)

    m = re.search(r":(\d+)\b", err)
    if m:
        js_line = int(m.group(1))
        lines = js.split("\n")
        print(f"  第 {js_line} 行附近：")
        for i in range(max(0, js_line - 6), min(len(lines), js_line + 5)):
            mark = ">>" if i == js_line - 1 else "  "
            print(f"  {mark} {i + 1:>5}: {lines[i][:110]}")

sys.exit(1 if failed else 0)
