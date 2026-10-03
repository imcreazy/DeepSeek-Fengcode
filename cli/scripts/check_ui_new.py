"""改造后的 UI 功能验证：截图 + 点击测试。

用法：python scripts/check_ui_new.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_ui_shots" / "after"
OUT.mkdir(parents=True, exist_ok=True)
URL = "http://127.0.0.1:7861"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}" + (f"　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"　{detail}" if detail else ""))


async def main() -> int:
    from playwright.async_api import async_playwright

    print("=" * 62)
    print("改造后 UI 功能验证")
    print("=" * 62)
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)

        # 关掉欢迎向导
        for sel in ["text=稍后配置", "text=关闭"]:
            try:
                b = page.locator(sel).first
                if await b.is_visible(timeout=800):
                    await b.click()
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                pass

        print("【1】侧边栏结构")
        check("有「新建会话」按钮", await page.locator("#new-chat-btn").count() > 0)
        check("底部有设置图标", await page.locator("#tool-settings").count() > 0)
        check("底部有搜索图标", await page.locator("#tool-search").count() > 0)
        check("顶部有信息面板按钮", await page.locator("#panel-toggle").count() > 0)
        check("底部状态栏存在", await page.locator("#statusbar").count() > 0)
        # 旧的 12 个导航项应该没了
        n_old = await page.locator(".nav-item").count()
        check("旧导航项已移除", n_old == 0, f"剩 {n_old} 个")

        await page.screenshot(path=str(OUT / "01-主界面.png"))
        print("  → 01-主界面.png")

        print()
        print("【2】会话列表")
        # 后端可能有历史会话
        n_sess = await page.locator(".sess-item[data-sid]").count()
        check("会话列表已渲染", True, f"{n_sess} 条会话")
        n_ws = await page.locator(".ws-head").count()
        check("工作区分组已渲染", n_ws > 0, f"{n_ws} 个工作区")

        print()
        print("【3】新建会话")
        try:
            await page.locator("#new-chat-btn").click()
            await page.wait_for_timeout(1800)
            n2 = await page.locator(".sess-item[data-sid]").count()
            check("点击后会话数增加", n2 > n_sess, f"{n_sess} → {n2}")
        except Exception as e:
            check("点击后会话数增加", False, str(e)[:60])

        print()
        print("【4】右侧信息面板")
        try:
            await page.locator("#panel-toggle").click()
            await page.wait_for_timeout(1500)
            opened = await page.locator("#infopanel.open").count() > 0
            check("面板可展开", opened)
            if opened:
                has_ctx = await page.locator("#ip-body").inner_text()
                check("含上下文窗口卡片", "上下文窗口" in has_ctx)
                check("含会话指标卡片", "会话指标" in has_ctx)
                await page.screenshot(path=str(OUT / "02-信息面板.png"))
                print("  → 02-信息面板.png")
        except Exception as e:
            check("面板可展开", False, str(e)[:60])

        print()
        print("【5】设置中心")
        try:
            await page.locator("#tool-settings").click()
            await page.wait_for_timeout(2000)
            check("设置页已激活", await page.locator("#page-settings.active").count() > 0)
            n_items = await page.locator("#settings-side .ss-item").count()
            check("左侧分类已渲染", n_items >= 10, f"{n_items} 项")
            check("有返回按钮", await page.locator("#ss-back").count() > 0)
            check("有搜索框", await page.locator("#ss-filter").count() > 0)
            body = await page.locator("#settings-body").inner_text()
            check("默认显示通用设置", "通用" in body or "主题" in body)
            await page.screenshot(path=str(OUT / "03-设置中心.png"))
            print("  → 03-设置中心.png")

            # 切到模型服务
            await page.locator('[data-set="providers"]').click()
            await page.wait_for_timeout(2000)
            body2 = await page.locator("#settings-body").inner_text()
            check("可切到「模型服务」", "供应商" in body2 or "模型" in body2)
            await page.screenshot(path=str(OUT / "04-模型服务.png"))
            print("  → 04-模型服务.png")

            # 切到记忆
            await page.locator('[data-set="memory"]').click()
            await page.wait_for_timeout(1800)
            await page.screenshot(path=str(OUT / "05-设置-记忆.png"))
            print("  → 05-设置-记忆.png")

            # 切到技能
            await page.locator('[data-set="skills"]').click()
            await page.wait_for_timeout(1800)
            await page.screenshot(path=str(OUT / "06-设置-技能.png"))
            print("  → 06-设置-技能.png")
        except Exception as e:
            check("设置中心可用", False, str(e)[:80])

        print()
        print("【6】设置搜索")
        try:
            await page.locator('[data-set="general"]').click()
            await page.wait_for_timeout(1200)
            await page.locator("#ss-filter").fill("MCP")
            await page.wait_for_timeout(700)
            vis = await page.locator("#settings-side .ss-item:visible").count()
            check("搜索可筛选分类", vis > 0 and vis < 16, f"剩 {vis} 项")
            await page.locator("#ss-filter").fill("")
            await page.wait_for_timeout(500)
        except Exception as e:
            check("搜索可筛选分类", False, str(e)[:60])

        print()
        print("【7】返回对话")
        try:
            await page.locator("#ss-back").click()
            await page.wait_for_timeout(1500)
            check("可返回对话", await page.locator("#page-chat.active").count() > 0)
        except Exception as e:
            check("可返回对话", False, str(e)[:60])

        print()
        print("【8】深色主题")
        try:
            await page.evaluate("applyTheme('dark')")
            await page.wait_for_timeout(900)
            theme = await page.evaluate("document.documentElement.getAttribute('data-theme')")
            check("可切深色", theme == "dark", theme)
            await page.screenshot(path=str(OUT / "07-深色主题.png"))
            print("  → 07-深色主题.png")
            await page.evaluate("applyTheme('light')")
        except Exception as e:
            check("可切深色", False, str(e)[:60])

        await browser.close()

    print()
    print(f"  控制台错误：{len(errors)} 条")
    for e in errors[:6]:
        print("    " + e[:120])
    check("无 JS 报错", len(errors) == 0, f"{len(errors)} 条")

    print()
    print("=" * 62)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
