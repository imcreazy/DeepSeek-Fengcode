# -*- coding: utf-8 -*-
"""全面审查：找出前端调用但后端没有的接口（「写了但用不了」的典型）。

同时检查：前端引用的 DOM 元素是否存在、图标是否存在。
"""
import io
import re
import sys

STATIC = "src/fengcode/server/static"
APP = "src/fengcode/server/app.py"

# 前端已拆成 index.html（结构）+ app.js（逻辑）：引用检查要合并两者。
html = (io.open(STATIC + "/index.html", encoding="utf-8").read()
        + "\n" + io.open(STATIC + "/app.js", encoding="utf-8").read())
app = io.open(APP, encoding="utf-8").read()

bad = 0


def report(desc, items):
    global bad
    if items:
        bad += len(items)
        print(f"FAIL {desc}（{len(items)} 个）")
        for it in items:
            print("       " + it)
    else:
        print(f"OK   {desc}")


# ---- 1) 后端已注册的路由 ----
routes = set()
for m in re.finditer(r'Route\(\s*"([^"]+)"', app):
    routes.add(m.group(1))
# 归一化：/api/sessions/{sid} → /api/sessions/*
def norm(p):
    return re.sub(r"\{[^}]+\}", "*", p)
routes_norm = {norm(r) for r in routes}
print(f"后端路由 {len(routes)} 条")

# ---- 2) 前端调用的接口 ----
called = set()
for m in re.finditer(r'api\(\s*[`"\']([^`"\'?]+)', html):
    p = m.group(1).strip()
    if p.startswith("/api") or p.startswith("/health"):
        called.add(p)
for m in re.finditer(r'api\(\s*`([^`]+)`', html):
    p = m.group(1).split("?")[0].split("${")[0].strip()
    if p.startswith("/api"):
        called.add(p)
# 拼接形式：api("/api/sessions/" + id)  → 检查前缀能否匹配任何路由
for m in re.finditer(r'api\(\s*"([^"]+)"\s*\+', html):
    p = m.group(1).strip()
    if p.startswith("/api"):
        called.add(p)

# 去掉带 query 的写法（/api/memories?limit=300 → /api/memories）
cleaned = set()
for c in called:
    base = c.split("?")[0]
    # 模板串里未闭合的 ${...} 一律截断
    base = re.split(r"\$\{", base)[0]
    if base.endswith("/"):
        base = base.rstrip("/") + "*"
    if base:
        cleaned.add(base)
called = cleaned

missing = []
for c in sorted(called):
    key = norm(c) if c.endswith("*") else c
    ok = key in routes_norm or key in routes
    if not ok and c.endswith("*"):
        ok = any(r.startswith(c[:-1]) for r in routes_norm) or any(r.startswith(c[:-1]) for r in routes)
    if not ok:
        missing.append(c)
report("前端调用的接口都存在", missing)
print(f"      前端调用 {len(called)} 个接口")

# ---- 3) 图标引用检查 ----
icons_defined = set(re.findall(r"^\s*([a-zA-Z][a-zA-Z0-9_]*):\s*'<", html, re.M))
icons_used = set(re.findall(r'icon\(\s*"([a-zA-Z][a-zA-Z0-9_]*)"', html))
icons_used |= set(re.findall(r"icon\(\s*'([a-zA-Z][a-zA-Z0-9_]*)'", html))
icons_used |= {m for m in re.findall(r'icon:\s*"([a-zA-Z][a-zA-Z0-9_]*)"', html)}
missing_icons = sorted(i for i in icons_used if i not in icons_defined)
report("引用的图标都已定义", [f"缺图标：{i}" for i in missing_icons])
print(f"      定义 {len(icons_defined)} 个图标，引用 {len(icons_used)} 个")

# ---- 4) 设置导航里的 page 都有对应 PAGES.xxx ----
nav_pages = set(re.findall(r'page:\s*"([a-zA-Z]+)"', html))
pages_defined = set(re.findall(r"PAGES\.([a-zA-Z]+)\s*=", html))
missing_pages = sorted(p for p in nav_pages
                       if p not in pages_defined and p != "chat")  # chat 是主界面，非设置页
report("设置导航的 page 都有渲染函数", [f"缺 PAGES.{p}" for p in missing_pages])

# ---- 5) 设置导航里的 tab 都有对应 #set-xxx 面板 ----
nav_tabs = set(re.findall(r'tab:\s*"([a-zA-Z]+)"', html))
panels = set(re.findall(r'id="set-([a-zA-Z]+)"', html))
missing_tabs = sorted(t for t in nav_tabs if t not in panels)
report("设置导航的 tab 都有对应面板", [f"缺面板 #set-{t}" for t in missing_tabs])
print(f"      导航 tab: {sorted(nav_tabs)}")
print(f"      实际面板: {sorted(panels)}")

# ---- 6) page-xxx 容器存在（go() 依赖） ----
containers = set(re.findall(r'id="page-([a-zA-Z]+)"', html))
missing_containers = sorted(p for p in (pages_defined - {"settings"}) if p not in containers
                            and p not in ("chat",))
report("每页都有对应容器 div", [f"缺 #page-{p}" for p in missing_containers])

print()
print(f"FAILED: {bad}" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
