# -*- coding: utf-8 -*-
"""穷举设置中心所有分类，逐个点开，抓任何 textContent 类报错。"""
import asyncio
import sys

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:7861/"
ITEMS = ["通用", "外观", "模型服务", "模型偏好", "用量统计", "MCP 与工具", "远程 SSH",
         "技能", "子智能体", "插件", "记忆", "上下文压缩", "定时任务", "审计日志",
         "权限", "沙箱", "高级", "关于"]


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
        await page.wait_for_timeout(800)

        bad = 0
        for name in ITEMS:
            errs.clear()
            try:
                loc = page.locator(".ss-item", has_text=name).first
                if await loc.count() == 0:
                    print(f"  SKIP {name}（导航里没有）")
                    continue
                await loc.click()
                await page.wait_for_timeout(1100)
            except Exception as e:
                print(f"  FAIL {name} 点击异常：{str(e)[:80]}")
                bad += 1
                continue
            if errs:
                bad += 1
                print(f"  FAIL {name}")
                for e in errs:
                    print("        " + e[:200])
            else:
                print(f"  OK   {name}")

        print()
        print(f"FAILED: {bad}" if bad else "ALL PASS")
        await b.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
