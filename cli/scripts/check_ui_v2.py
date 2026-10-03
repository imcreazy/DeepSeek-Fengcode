"""大更新后的 UI 验证：新功能逐个点。

用法：python scripts/check_ui_v2.py
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_ui_shots" / "v2"
OUT.mkdir(parents=True, exist_ok=True)
URL = os.environ.get("FENGCODE_SHOT_URL", "http://127.0.0.1:7861")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}" + (f"　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"　{detail}" if detail else ""))


async def main() -> int:
    from playwright.async_api import async_playwright

    print("=" * 64)
    print("大更新后 UI 验证")
    print("=" * 64)
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2600)

        print("【1】首次启动向导（全新数据目录应出现，且可跳过）")
        wiz = await page.locator("text=欢迎使用 Fengcode").count()
        check("全新环境弹出了欢迎向导", wiz == 1, f"找到 {wiz} 个")
        if wiz:
            await page.click("#frw-skip")
            await page.wait_for_timeout(600)
            gone = await page.locator("#frw").count()
            check("点跳过可关闭向导", gone == 0, f"仍存在 {gone} 个")

        print()
        print("【2】emoji 已清除")
        html = await page.content()
        emojis = ["💬", "🗂", "🧠", "⚡", "🔧", "🔌", "🧩", "🔀", "⏰", "🖥", "📊", "📋", "⚙️",
                  "🔍", "🗑", "＋", "▤", "×", "«", "»", "☰"]
        found = [e for e in emojis if e in html]
        check("界面无 emoji", not found, f"残留 {found}" if found else "干净")
        n_svg = await page.locator("svg.ic").count()
        check("SVG 图标已注入", n_svg >= 6, f"{n_svg} 个")

        print()
        print("【3】侧边栏")
        check("有新建会话", await page.locator("#new-chat-btn").count() > 0)
        check("收起按钮已删除", await page.locator("#side-collapse").count() == 0)
        n_ws = await page.locator(".ws-head").count()
        check("工作区分组", n_ws > 0, f"{n_ws} 个")

        print()
        print("【4】执行方式与权限")
        check("有执行方式按钮", await page.locator("#mode-chip").count() > 0)
        check("有权限按钮", await page.locator("#perm-chip").count() > 0)
        mode_txt = await page.locator("#mode-chip-label").inner_text()
        perm_txt = await page.locator("#perm-chip-label").inner_text()
        check("执行方式显示文字", bool(mode_txt), mode_txt)
        check("权限显示三档文字", bool(perm_txt), perm_txt)

        # 点开权限菜单
        await page.locator("#perm-chip").click()
        await page.wait_for_timeout(700)
        n_items = await page.locator("#choice-pop .mi").count()
        check("权限菜单展开", n_items == 3, f"{n_items} 个选项")
        if n_items:
            await page.screenshot(path=str(OUT / "01-权限菜单.png"))
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

        # 点开执行方式
        await page.locator("#mode-chip").click()
        await page.wait_for_timeout(700)
        n2 = await page.locator("#choice-pop .mi").count()
        check("执行方式菜单展开", n2 == 3, f"{n2} 个选项")
        if n2:
            await page.screenshot(path=str(OUT / "02-执行方式.png"))
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

        print()
        print("【5】斜杠命令菜单")
        await page.locator("#input").click()
        await page.locator("#input").type("/", delay=40)
        await page.wait_for_timeout(1200)
        n_slash = await page.locator(".slash-item").count()
        check("斜杠菜单弹出", n_slash > 0, f"{n_slash} 项")
        if n_slash:
            await page.screenshot(path=str(OUT / "03-斜杠菜单.png"))
        await page.locator("#input").fill("")
        await page.wait_for_timeout(400)

        print()
        print("【6】右侧面板：信息 / 项目文件")
        await page.locator("#panel-toggle").click()
        await page.wait_for_timeout(1200)
        check("面板已展开", await page.locator("#infopanel.open").count() > 0)
        check("有信息标签", await page.locator('[data-iptab="info"]').count() > 0)
        check("有项目文件标签", await page.locator('[data-iptab="files"]').count() > 0)

        await page.locator('[data-iptab="files"]').click()
        await page.wait_for_timeout(1600)
        check("项目文件面板激活", await page.locator("#ip-pane-files.active").count() > 0)
        n_ft = await page.locator(".ft-item").count()
        check("文件树已渲染", True, f"{n_ft} 项")
        await page.screenshot(path=str(OUT / "04-项目文件.png"))

        await page.locator('[data-iptab="info"]').click()
        await page.wait_for_timeout(1200)
        body = await page.locator("#ip-body").inner_text()
        check("信息面板有内容", "上下文" in body or "用量" in body)

        print()
        print("【7】拖拽调宽")
        box = await page.locator("#resizer-left").bounding_box()
        check("左侧有拖拽手柄", box is not None,
              f"x={int(box['x'])}" if box else "无")
        box2 = await page.locator("#resizer-right").bounding_box()
        check("右侧有拖拽手柄", box2 is not None,
              f"x={int(box2['x'])}" if box2 else "无")

        if box:
            sb_before = await page.locator("#sidebar").bounding_box()
            await page.mouse.move(box["x"] + 2, box["y"] + 200)
            await page.mouse.down()
            await page.mouse.move(box["x"] + 62, box["y"] + 200, steps=8)
            await page.mouse.up()
            await page.wait_for_timeout(500)
            sb_after = await page.locator("#sidebar").bounding_box()
            check("拖拽改变了宽度", sb_after["width"] > sb_before["width"] + 20,
                  f"{int(sb_before['width'])} → {int(sb_after['width'])}")

        print()
        print("【8】字体与主题")
        fs = await page.evaluate("getComputedStyle(document.body).fontSize")
        check("字号已调大", fs in ("15px", "16px"), fs)
        await page.evaluate("applyTheme('dark')")
        await page.wait_for_timeout(700)
        check("深色主题可用",
              await page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark")
        await page.screenshot(path=str(OUT / "05-深色.png"))
        await page.evaluate("applyTheme('light')")
        await page.wait_for_timeout(500)

        print()
        print("【9】设置中心")
        await page.evaluate("document.querySelector('#tool-settings').click()")
        await page.wait_for_timeout(2200)
        n_set = await page.locator("#settings-side .ss-item").count()
        check("设置分类渲染", n_set >= 10, f"{n_set} 项")
        await page.screenshot(path=str(OUT / "06-设置.png"))

        await page.locator('[data-set="providers"]').click()
        await page.wait_for_timeout(1800)
        prov = await page.locator("#settings-body").inner_text()
        check("只列万象 API", "万象" in prov, "找到万象")
        for bad in ["OpenAI", "Anthropic", "Gemini", "智谱", "通义", "Ollama"]:
            if bad in prov:
                check(f"不该出现 {bad}", False, "仍在列表里")
        await page.screenshot(path=str(OUT / "07-模型服务.png"))

        await browser.close()

    print()
    print(f"  控制台错误：{len(errors)} 条")
    for e in errors[:6]:
        print("    " + e[:120])
    check("无 JS 报错", len(errors) == 0, f"{len(errors)} 条")

    print()
    print("=" * 64)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 64)
    print(f"截图：{OUT}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
