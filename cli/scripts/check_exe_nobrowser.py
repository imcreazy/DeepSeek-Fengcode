"""验证 exe「双击」行为：起服务但**不**打开浏览器。

双击 = 不带任何参数运行 exe（默认端口 7845）。

用法：python scripts/check_exe_nobrowser.py
"""
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "Fengcode" / "Fengcode.exe"
PORT = 7845
BASE = f"http://127.0.0.1:{PORT}"
LOG = ROOT / "_exe_boot.log"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BROWSERS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "iexplore.exe",
    "brave.exe", "opera.exe", "vivaldi.exe", "360se.exe", "360chrome.exe",
    "qqbrowser.exe", "sogouexplorer.exe",
}


def browser_pids() -> set:
    """当前浏览器进程 PID 集合（tasklist 比 Get-Process 快很多）。"""
    try:
        cp = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        out = set()
        for line in (cp.stdout or "").split("\n"):
            line = line.strip()
            if not line.startswith('"'):
                continue
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() in BROWSERS:
                try:
                    out.add(int(parts[1]))
                except ValueError:
                    pass
        return out
    except Exception:
        return set()


def alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=30)


def main() -> int:
    print("=" * 62)
    print("exe「双击」行为检查（应起服务、不开浏览器）")
    print("=" * 62)
    print()

    if not EXE.is_file():
        print(f"  找不到 exe：{EXE}")
        return 1

    if alive(PORT):
        print(f"  ! 端口 {PORT} 已有服务，请先停掉已有实例")
        return 1

    before = browser_pids()
    print(f"  启动前浏览器进程数：{len(before)}")

    print(f"  以双击方式启动：{EXE.name}（无参数，端口 {PORT}）")
    logf = open(LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(EXE)],
        stdout=logf, stderr=subprocess.STDOUT,
        cwd=str(EXE.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    up = False
    for _ in range(60):
        if proc.poll() is not None:
            break
        if alive(PORT):
            up = True
            break
        time.sleep(0.5)

    if not up:
        kill_tree(proc.pid)
        logf.close()
        print("  ✗ 服务未起来")
        print("  日志：")
        print(LOG.read_text(encoding="utf-8", errors="replace")[-800:])
        return 1

    print("  ✓ 服务已起来（/health 200）")

    # 观察窗口：如果会开浏览器，这段时间内进程就会出现
    time.sleep(5)
    after = browser_pids()
    new_browsers = after - before

    logf.flush()
    text = LOG.read_text(encoding="utf-8", errors="replace")
    says_no_browser = "不会自动打开浏览器" in text

    print()
    print("  检查项：")
    print("    · 服务可用：             是")
    print(f"    · banner 声明不开浏览器：{'是' if says_no_browser else '否'}")
    print(f"    · 5 秒内新增浏览器：     {len(new_browsers)} 个")

    kill_tree(proc.pid)
    logf.close()
    for _ in range(20):
        if not alive(PORT):
            break
        time.sleep(0.3)
    print(f"    · 停止后端口已释放：     {'是' if not alive(PORT) else '否'}")

    print()
    print("=" * 62)
    if new_browsers:
        print(f"✗ 有 {len(new_browsers)} 个浏览器进程被拉起 —— 不符合预期")
        print("=" * 62)
        return 1
    if not says_no_browser:
        print("✗ banner 文案不对")
        print("=" * 62)
        return 1
    print("✓ 双击只起服务、不打开浏览器 —— 符合预期")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
