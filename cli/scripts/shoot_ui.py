"""用 Playwright 截图 Web UI，验证界面实际渲染效果。"""
import asyncio
import os
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "_shots"
OUT.mkdir(parents=True, exist_ok=True)
BASE = os.environ.get("FENGCODE_SHOT_URL", "http://127.0.0.1:7856")


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("未安装 playwright，跳过截图")
        return 0

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:
            print("浏览器启动失败：%s" % e)
            return 0

        page = await browser.new_page(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("打开首页…")
        await page.goto(BASE, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(2500)

        # 关掉首次运行引导弹窗（它会挡住导航点击）
        try:
            if await page.query_selector("#modal-bg.show"):
                print("  检测到首次运行向导，先截图再关闭")
                await page.screenshot(path=str(OUT / "00-welcome.png"))
                # 点「稍后配置」或「去配置」都能关
                for sel in ["#modal-foot button", "#wz-go"]:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        break
                await page.wait_for_timeout(1200)
        except Exception as e:
            print("  关闭向导失败：%s" % str(e)[:80])
            # 兜底：直接移除弹窗
            try:
                await page.evaluate("document.getElementById('modal-bg').classList.remove('show')")
                await page.wait_for_timeout(400)
            except Exception:
                pass

        # 1. 浅色首页
        await page.screenshot(path=str(OUT / "01-chat-light.png"), full_page=False)
        print("  已截 01-chat-light.png")

        # 2. 逐个面板
        panels = [
            ("sessions", "02-sessions"),
            ("memory", "03-memory"),
            ("skills", "04-skills"),
            ("tools", "05-tools"),
            ("mcp", "06-mcp"),
            ("plugins", "07-plugins"),
            ("workflows", "08-workflows"),
            ("jobs", "09-jobs"),
            ("stats", "10-stats"),
            ("settings", "11-settings"),
            ("audit", "12-audit"),
        ]
        for pid, fname in panels:
            btn = await page.query_selector('.nav-item[data-page="%s"]' % pid)
            if btn is None:
                print("  跳过 %s（未找到导航项）" % pid)
                continue
            await btn.click()
            await page.wait_for_timeout(1600)
            await page.screenshot(path=str(OUT / (fname + ".png")))
            print("  已截 %s.png" % fname)

        # 3. 深色主题
        await page.evaluate("localStorage.setItem('fengcode_theme','dark')")
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(2200)
        await page.screenshot(path=str(OUT / "13-dark.png"))
        print("  已截 13-dark.png")

        # 4. 回到浅色 + 会话页
        await page.evaluate("localStorage.setItem('fengcode_theme','light')")
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # 5. 输入一段文字（不发送）看输入区
        try:
            await page.fill("#input", "帮我看一下 D 盘有哪些工程，并统计代码行数")
            await page.wait_for_timeout(600)
            await page.screenshot(path=str(OUT / "14-input.png"))
            print("  已截 14-input.png")
        except Exception as e:
            print("  输入框截图失败：%s" % e)

        await browser.close()

        if errors:
            print()
            print("控制台错误（%d 条）：" % len(errors))
            for e in errors[:10]:
                print("  !", e[:200])
        else:
            print()
            print("控制台无错误")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
