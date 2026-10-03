# -*- coding: utf-8 -*-
"""针对本轮改动的浏览器验证：真实点击，确认用户提的问题都真修好了。

覆盖：
  1. 模式选择器：只有「计划/目标」，默认「自动」，可反选
  2. 设置中心：点某分类只显示该分类，不串页
  3. 新建空白项目：能建、能出现在左侧
  4. 记忆页四标签：背景/归档/指令文件/召回记录
  5. 权限细粒度规则：三列可加
  6. 关于页存在
"""
import asyncio
import sys

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:7861/"
SHOT = "D:/Fengcode/cli/_ui_shots/round"
bad = 0


def check(desc, cond, extra=""):
    global bad
    if not cond:
        bad += 1
    print(("  OK   " if cond else "  FAIL ") + desc + (("  -> " + str(extra)) if extra and not cond else ""))


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(str(e)))

        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)

        # 向导若在，先跳过
        if await page.locator("#frw").count():
            await page.click("#frw-skip")
            await page.wait_for_timeout(500)

        # ---------------- 1. 模式选择器 ----------------
        print("\n【1】模式选择器（去掉强制「对话模式」，可都不选）")
        await page.click("#mode-chip")
        await page.wait_for_timeout(400)
        pop = page.locator("#choice-pop")
        check("模式菜单弹出", await pop.count() == 1)
        text = await pop.inner_text() if await pop.count() else ""
        check("含「自动（不指定）」", "自动" in text, text)
        check("含「计划模式」", "计划模式" in text, text)
        check("含「目标模式」", "目标模式" in text, text)
        check("不再有「对话模式」这一项", "对话模式" not in text, text)
        await page.screenshot(path=f"{SHOT}/01-模式菜单.png")

        # 选计划模式
        await pop.locator("button", has_text="计划模式").first.click()
        await page.wait_for_timeout(400)
        label = await page.locator("#mode-chip-label").inner_text()
        check("选计划模式后按钮显示「计划模式」", label.strip() == "计划模式", label)

        # 再点同一个 → 应取消（回到自动）
        await page.click("#mode-chip")
        await page.wait_for_timeout(300)
        await page.locator("#choice-pop button", has_text="计划模式").first.click()
        await page.wait_for_timeout(400)
        label2 = await page.locator("#mode-chip-label").inner_text()
        check("再次点击同一模式 = 取消，回到「自动」", label2.strip() == "自动", label2)

        # ---------------- 2. 设置中心不串页 ----------------
        print("\n【2】设置中心：点分类只显示该分类")
        await page.evaluate("go('settings')")
        await page.wait_for_timeout(900)
        # 点「模型服务」
        await page.locator(".ss-item", has_text="模型服务").first.click()
        await page.wait_for_timeout(900)
        body = page.locator("#settings-body")
        btxt = await body.inner_text()
        check("显示模型服务面板（含供应商/密钥相关）",
              ("供应商" in btxt or "密钥" in btxt or "模型" in btxt), btxt[:200])
        # 不应同时出现其它分类的面板标题
        check("不再同时显示「外观」面板", "界面字号" not in btxt and "显示缩放" not in btxt, btxt[:300])
        check("不再同时显示「记忆」面板的正文字样", "长期记忆（跨对话" not in btxt, btxt[:300])
        tabs = await body.locator("#set-tabs").count()
        check("页内旧 tabs 已移除", tabs == 0, f"找到 {tabs} 个")
        await page.screenshot(path=f"{SHOT}/02-设置模型服务.png")

        # 点「权限」，应看到细粒度规则三列
        await page.locator(".ss-item", has_text="权限").first.click()
        await page.wait_for_timeout(900)
        ptxt = await body.inner_text()
        check("权限页含「细粒度规则」", "细粒度规则" in ptxt, ptxt[:200])
        check("权限页含三列（阻止/始终确认/允许）",
              "阻止" in ptxt and "始终确认" in ptxt and "允许" in ptxt, ptxt[:300])
        # 加一条规则
        await page.fill("#rule-deny-in", "tool:shell")
        await page.click('[data-rule-add="deny"]')
        await page.wait_for_timeout(400)
        items = await page.locator("#rule-deny .rule-item").count()
        check("能添加一条 deny 规则", items == 1, f"找到 {items} 条")
        # 再测删除（同样走事件委托，容易出同类问题）
        if items == 1:
            await page.locator("[data-rule-del]").first.click()
            await page.wait_for_timeout(500)
            left = await page.locator("#rule-deny .rule-item").count()
            check("能删除规则", left == 0, f"还剩 {left} 条")
            await page.fill("#rule-deny-in", "tool:shell")
            await page.click('[data-rule-add="deny"]')
            await page.wait_for_timeout(400)
        await page.screenshot(path=f"{SHOT}/03-权限细粒度规则.png")

        # 沙箱页
        await page.locator(".ss-item", has_text="沙箱").first.click()
        await page.wait_for_timeout(1500)
        stxt = await body.inner_text()
        check("沙箱页含 Shell 解释器选择", "Shell 解释器" in stxt, stxt[:200])
        probe = await page.locator("#sb-probe").inner_text()
        check("沙箱页渲染了环境探测结果",
              ("已检测到" in probe or "未检测到" in probe), probe[:200])
        check("沙箱页含文件工具写入范围", "文件工具写入范围" in stxt, stxt[:300])
        await page.screenshot(path=f"{SHOT}/04-沙箱探测.png")

        # 关于页
        await page.locator(".ss-item", has_text="关于").first.click()
        await page.wait_for_timeout(1200)
        atxt = await body.inner_text()
        check("关于页含版本信息", "当前版本" in atxt, atxt[:200])
        check("关于页含数据位置", "数据位置" in atxt, atxt[:400])
        await page.screenshot(path=f"{SHOT}/05-关于页.png")

        # ---------------- 3. 新建空白项目 ----------------
        print("\n【3】新建空白项目")
        await page.evaluate("go('chat')")
        await page.wait_for_timeout(700)
        btn = page.locator("#new-ws-btn")
        check("侧边栏有「新建项目」按钮", await btn.count() == 1, await btn.count())
        await btn.click()
        await page.wait_for_timeout(500)
        check("弹出新建项目对话框", await page.locator("#np-name").count() == 1)
        await page.fill("#np-name", "浏览器验证项目")
        await page.click("#np-ok")
        await page.wait_for_timeout(1500)
        navtxt = await page.locator("#nav").inner_text()
        check("新项目出现在左侧列表", "浏览器验证项目" in navtxt, navtxt[:300])
        await page.screenshot(path=f"{SHOT}/06-新建项目.png")

        # 项目菜单（改名/换目录/删除）
        more = page.locator(".ws-more").first
        check("项目有「更多」菜单入口", await page.locator(".ws-more").count() >= 1)
        if await page.locator(".ws-more").count():
            await more.click()
            await page.wait_for_timeout(400)
            mtext = await page.locator("#choice-pop").inner_text()
            check("项目菜单含重命名/更改目录", "重命名项目" in mtext and "更改工作目录" in mtext, mtext)
            await page.keyboard.press("Escape")
            await page.evaluate("closeChoiceMenu()")
            await page.wait_for_timeout(300)

        # ---------------- 4. 记忆页四标签 ----------------
        print("\n【4】记忆页四标签")
        await page.evaluate("go('memory')")
        await page.wait_for_timeout(1500)
        mt = await page.locator("#mem-tabs").inner_text()
        for t in ["背景记忆", "归档的记忆", "指令文件", "召回记录"]:
            check(f"记忆页含「{t}」标签", t in mt, mt)
        # 切到指令文件
        await page.locator("#mem-tabs .tab", has_text="指令文件").first.click()
        await page.wait_for_timeout(1500)
        instr = await page.locator("#mem-list").inner_text()
        check("指令文件标签能渲染内容", ("AGENTS.md" in instr or "指令文件" in instr or "约定" in instr), instr[:300])
        # 切到召回记录
        await page.locator("#mem-tabs .tab", has_text="召回记录").first.click()
        await page.wait_for_timeout(1000)
        rec = await page.locator("#mem-list").inner_text()
        check("召回记录标签能渲染内容", len(rec) > 0, rec[:200])
        await page.screenshot(path=f"{SHOT}/07-记忆四标签.png")

        # ---------------- 5. 无 JS 错误 ----------------
        print("\n【5】运行时健康")
        check("无 JS 控制台错误", len(errs) == 0, errs[:5])

        await browser.close()

    print()
    print(f"FAILED: {bad}" if bad else "ALL PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
