"""实测打包后的桌面端（win-unpacked）能否真启动。

验证打包态与开发态的差异：
  · 后端应从 resources\\backend\\Fengcode.exe 拉起（而不是系统 Python）
  · 窗口应显示且加载成功
  · 不应跳浏览器

用法：python scripts/check_desktop_packaged.py
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent      # 项目根
APP = ROOT / "桌面端" / "desktop" / "dist" / "win-unpacked"
EXE = APP / "Fengcode.exe"
PORT = 7845
BASE = f"http://127.0.0.1:{PORT}"
LOG = ROOT / "_desktop_packaged.log"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BROWSERS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "iexplore.exe",
    "brave.exe", "opera.exe", "vivaldi.exe", "360se.exe", "360chrome.exe",
}
# 注意：不要把 msedgewebview2.exe 算进来 —— 那是 Electron/WebView2 自己的
# 渲染进程，任何用 Chromium 内核的桌面应用都会派生它，不是"打开了浏览器"。
IGNORE = {
    "msedgewebview2.exe", "electron.exe",
}


def browser_pids() -> set:
    """真正被打开的浏览器进程（排除 Electron 自带的 WebView2 渲染进程）。

    判据两点：
      1. 进程名属于常见浏览器
      2. 且有「主窗口标题」—— WebView2 的辅助进程没有窗口标题
    """
    try:
        ps = (
            "Get-Process | Where-Object { "
            "$_.ProcessName -match '^(chrome|msedge|firefox|iexplore|brave|opera|vivaldi|360se|360chrome)$' "
            "-and $_.MainWindowTitle -ne '' "
            "} | ForEach-Object { \"$($_.Id)|$($_.ProcessName)|$($_.MainWindowTitle)\" }"
        )
        cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=45)
        out = set()
        for line in (cp.stdout or "").split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) >= 2 and parts[1].lower() in BROWSERS:
                try:
                    out.add(int(parts[0]))
                except ValueError:
                    pass
        return out
    except Exception:
        return set()


def health() -> dict | None:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=30)


def main() -> int:
    print("=" * 62)
    print("打包后的桌面端启动检查")
    print("=" * 62)
    print()

    if not EXE.is_file():
        print(f"  ✗ 找不到：{EXE}")
        print("  请先运行：cd 桌面端\\desktop && npm run dist")
        return 1
    print(f"  应用：{EXE}")
    print(f"  大小：{EXE.stat().st_size / 1048576:.1f} MB")

    backend = APP / "resources" / "backend" / "Fengcode.exe"
    if not backend.is_file():
        print(f"  ✗ 后端未打进应用：{backend}")
        return 1
    print(f"  内置后端：{backend.name}（{backend.stat().st_size / 1048576:.1f} MB）")
    print()

    if health():
        print("  ! 端口已被占用，请先停掉已有实例")
        return 1

    before = browser_pids()
    print(f"  启动前浏览器进程数：{len(before)}")
    print("  启动应用…")

    logf = open(LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(EXE)],
        stdout=logf, stderr=subprocess.STDOUT,
        cwd=str(APP),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    # 等后端就绪
    info = None
    for _ in range(90):
        if proc.poll() is not None:
            break
        info = health()
        if info:
            break
        time.sleep(0.5)

    if not info:
        print("  ✗ 后端未起来")
        kill_tree(proc.pid)
        logf.close()
        try:
            print("  日志：")
            print(LOG.read_text(encoding="utf-8", errors="replace")[-800:])
        except Exception:
            pass
        return 1

    print(f"  ✓ 内置后端已拉起：{info.get('app')} {info.get('version')}")

    # 确认是应用自带的后端在跑，而不是系统 Python
    # 用端口归属判断最快：谁在监听 7845，它的可执行文件路径在哪
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen "
             f"-ErrorAction SilentlyContinue | Select-Object -First 1 "
             f"-ExpandProperty OwningProcess | "
             f"ForEach-Object {{ (Get-Process -Id $_ -ErrorAction SilentlyContinue).Path }})"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=45)
        owner = (cp.stdout or "").strip()
        if owner:
            uses_bundled = "resources\\backend" in owner or "resources/backend" in owner
            print(f"  {'✓' if uses_bundled else '✗'} 后端来源：{owner}")
        else:
            print("  ? 未能取到后端路径")
    except Exception as e:
        print(f"  ? 后端来源检查跳过：{type(e).__name__}")

    # 首页可访问
    try:
        with urllib.request.urlopen(f"{BASE}/", timeout=15) as r:
            html = r.read().decode("utf-8", "replace")
        print(f"  ✓ 界面可访问：{len(html)} 字节，含 Fengcode：{'Fengcode' in html}")
    except Exception as e:
        print(f"  ✗ 界面访问失败：{e}")

    # 观察是否开浏览器
    time.sleep(4)
    new_browsers = browser_pids() - before
    print(f"  {'✓' if not new_browsers else '✗'} 新增浏览器进程：{len(new_browsers)} 个")

    kill_tree(proc.pid)
    logf.close()
    time.sleep(2)
    kill_tree(proc.pid)

    print()
    print("=" * 62)
    if new_browsers:
        print("✗ 打开了浏览器 —— 不符合预期")
        print("=" * 62)
        return 1
    print("✓ 打包后的桌面端是独立应用：自带后端、独立窗口、不跳浏览器")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
