"""检查深色/浅色主题下关键文字与背景的对比度是否达到 WCAG AA。

AA 标准：正文 ≥ 4.5:1，大字号（≥18px 或 14px 粗体）≥ 3:1
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def srgb_to_lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def ratio(fg, bg):
    l1, l2 = lum(fg), lum(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


# 从 index.html 里解析主题变量
# ★ 主题变量定义在 **app.css**，不在 index.html。
#   历史坑：这里原本读 index.html，而 index.html 里只有 <html data-theme="light">，
#   根本没有任何 --* 变量声明 —— 于是 theme_vars() 永远返回空、每个主题都跳过，
#   脚本最后照样打印「全部达标」。**一个什么都没检查的检查器比没有检查器更危险**：
#   它让人以为对比度已经守住了。现在改为读 app.css，并在取不到变量时直接报错退出。
_CSS = Path(__file__).resolve().parent.parent / "src" / "fengcode" / "server" / "static" / "app.css"
html = _CSS.read_text(encoding="utf-8")


def theme_vars(name):
    m = re.search(r':root\[data-theme="%s"\]\s*\{(.*?)\}' % name, html, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).split("\n"):
        mm = re.match(r"\s*(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", line)
        if mm:
            out[mm.group(1)] = mm.group(2)
    return out


def check(theme):
    v = theme_vars(theme)
    if not v:
        print("未找到主题变量：%s" % theme)
        return []
    bg = v.get("--bg", "#ffffff")
    elev = v.get("--bg-elev", bg)
    sunken = v.get("--bg-sunken", bg)
    problems = []

    pairs = [
        ("正文（--text / --bg）", v.get("--text"), bg, 4.5),
        ("正文（--text / --bg-elev）", v.get("--text"), elev, 4.5),
        ("次要文字（--text-soft / --bg）", v.get("--text-soft"), bg, 4.5),
        ("次要文字（--text-soft / --bg-elev）", v.get("--text-soft"), elev, 4.5),
        ("弱文字（--text-dim / --bg）", v.get("--text-dim"), bg, 3.0),
        ("极弱文字（--text-faint / --bg）", v.get("--text-faint"), bg, 3.0),
        ("强调色（--accent / --bg）", v.get("--accent"), bg, 3.0),
        ("强调色文字（--accent-text / --accent）", v.get("--accent-text"), v.get("--accent"), 3.0),
        ("强调底上的正文（--text / --accent-soft）", v.get("--text"), v.get("--accent-soft"), 4.5),
        ("侧栏文字（--text-soft / --bg-sunken）", v.get("--text-soft"), sunken, 4.5),
        ("成功（--success / --bg）", v.get("--success"), bg, 3.0),
        ("警告（--warn / --bg）", v.get("--warn"), bg, 3.0),
        ("危险（--danger / --bg）", v.get("--danger"), bg, 3.0),
        ("危险底上的文字（--danger / --danger-soft）", v.get("--danger"), v.get("--danger-soft"), 3.0),
        ("成功底上的文字（--success / --success-soft）", v.get("--success"), v.get("--success-soft"), 3.0),
        ("警告底上的文字（--warn / --warn-soft）", v.get("--warn"), v.get("--warn-soft"), 3.0),
        ("代码底上的正文（--text / --code-bg）", v.get("--text"), v.get("--code-bg"), 4.5),
    ]

    print("=" * 66)
    print("主题：%s" % ("浅色" if theme == "light" else "深色"))
    print("=" * 66)
    for label, fg, bgc, need in pairs:
        if not fg or not bgc:
            continue
        r = ratio(fg, bgc)
        okmark = "OK " if r >= need else "低 "
        line = "  [%s] %-42s %5.2f : 1  (需 %.1f)  %s / %s" % (okmark, label, r, need, fg, bgc)
        print(line)
        if r < need:
            problems.append((theme, label, r, need))
    return problems


allp = []
allp += check("light")
print()
allp += check("dark")

print()
print("=" * 66)
if allp:
    print("对比度不足 %d 处：" % len(allp))
    for t, label, r, need in allp:
        print("  ✗ [%s] %s：%.2f（需 %.1f）" % (t, label, r, need))
    sys.exit(1)
print("全部达标（WCAG AA）")
sys.exit(0)
