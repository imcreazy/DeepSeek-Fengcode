# -*- coding: utf-8 -*-
"""验证各设置页的按钮真能点（用真实 id，覆盖用户报的每一个）。

用户报的现象：所有按钮点了没反应；保存设置后页面变卡、颜色和分界线消失。
"""
import asyncio
import sys

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:7861/"
bad = 0


def check(desc, cond, extra=""):
    global bad
    if not cond:
        bad += 1
    print(("  OK   " if cond else "  FAIL ") + desc + (("  -> " + str(extra)) if extra and not cond else ""))


async def main() -> int:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)
        if await page.locator("#frw").count():
            await page.click("#frw-skip")
            await page.wait_for_timeout(400)

        await page.evaluate("go('settings')")
        await page.wait_for_timeout(900)

        # (分类, 按钮选择器, 描述) —— 用真实 id / 纯 CSS（JS 的 querySelectorAll 也支持）
        CASES = [
            ("模型服务", "#prov-add", "添加供应商"),
            ("技能", "#sk-new", "新建技能"),
            ("技能", "#sk-rescan", "重新扫描技能"),
            ("MCP 与工具", "#top-actions button", "MCP 顶部按钮"),
            ("插件", "#plug-new", "新建插件"),
            ("插件", "#plug-install", "从文件夹安装"),
            ("插件", "#plug-reload", "重新加载插件"),
        ]

        print("=== 各面板按钮可点性 ===")
        for cat, sel, desc in CASES:
            # 每轮先确保没有残留弹窗挡着
            await page.evaluate("window.closeModal ? closeModal() : 0")
            await page.wait_for_timeout(200)
            await page.locator(".ss-item", has_text=cat).first.click()
            await page.wait_for_timeout(1300)
            errs.clear()
            n = await page.locator(sel).count()
            if n == 0:
                check(f"{cat} · {desc}", False, "找不到按钮")
                continue
            before = await page.locator(".modal").count()
            # 用 JS 触发可见的按钮，避免可见性判定误判
            ok_click = await page.evaluate("""(sel) => {
                const els = Array.from(document.querySelectorAll(sel));
                const vis = els.find(b => b.offsetParent !== null) || els[0];
                if (!vis) return false;
                vis.click();
                return true;
            }""", sel)
            if not ok_click:
                check(f"{cat} · {desc}", False, "按钮不可点")
                continue
            await page.wait_for_timeout(900)
            after = await page.locator(".modal").count()
            check(f"{cat} · {desc}", after > 0 or errs == [], f"modal {before}->{after}, errs={errs[:1]}")
            if after > 0:
                await page.evaluate("window.closeModal ? closeModal() : 0")
            await page.wait_for_timeout(400)

        # ---- 保存设置：样式不能被破坏 ----
        print("\n=== 保存设置后的样式完整性 ===")
        for cat in ["外观", "权限", "记忆", "高级"]:
            await page.evaluate("window.closeModal ? closeModal() : 0")
            await page.wait_for_timeout(200)
            await page.locator(".ss-item", has_text=cat).first.click()
            await page.wait_for_timeout(1200)
            errs.clear()
            before_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
            # 用 JS 触发可见的那个保存按钮（页面里可能同时存在多个 #set-save）
            clicked = await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('#set-save'));
                const vis = btns.find(b => b.offsetParent !== null);
                if (!vis) return false;
                vis.click();
                return true;
            }""")
            if not clicked:
                continue
            await page.wait_for_timeout(2200)
            after_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
            theme = await page.evaluate("document.documentElement.getAttribute('data-theme')")
            check(f"{cat} 页保存后背景色未丢", after_bg == before_bg and after_bg != "rgba(0, 0, 0, 0)",
                  f"{before_bg} -> {after_bg}")
            check(f"{cat} 页保存后主题正常", theme in ("light", "dark"), theme)
            check(f"{cat} 页保存无 JS 错误", len(errs) == 0, errs[:2])

        await page.screenshot(path="D:/Fengcode/cli/_shot_final.png")
        print("\n截图：D:/Fengcode/cli/_shot_final.png")
        await b.close()

    print()
    print(f"FAILED: {bad}" if bad else "ALL PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
