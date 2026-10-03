"""验证桌面端能否按它的方式拉起后端。

桌面端（main.js）用的命令是：
    python -m fengcode.cli.main serve --host 127.0.0.1 --port 7845
cwd 为项目根下的「命令端」目录

这里完全照搬，确认后端能起来且 /health 正常。
用法：python scripts/check_desktop_backend.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 7845
BASE = f"http://127.0.0.1:{PORT}"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    print("=" * 62)
    print("桌面端后端拉起检查")
    print("=" * 62)
    print()
    print(f"cwd       = {ROOT}")
    print(f"命令       = python -m fengcode.cli.main serve --host 127.0.0.1 --port {PORT}")
    print()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "fengcode.cli.main", "serve",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    ok = False
    for _ in range(60):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            time.sleep(0.5)

    if not ok:
        print("  ✗ 后端未能在 30 秒内就绪")
        try:
            proc.kill()
        except Exception:
            pass
        out = proc.stdout.read() if proc.stdout else ""
        print("  输出（尾部）：")
        for line in (out or "").split("\n")[-20:]:
            print("    " + line)
        return 1

    print("  ✓ 后端已就绪")

    with urllib.request.urlopen(f"{BASE}/health", timeout=5) as r:
        health = json.loads(r.read())
    print(f"  ✓ /health → {health}")

    with urllib.request.urlopen(f"{BASE}/", timeout=15) as r:
        html = r.read().decode("utf-8", "replace")
    print(f"  ✓ 首页 {len(html)} 字节，含 Fengcode：{'Fengcode' in html}")
    # 桌面端加载的就是这个页面
    print(f"  ✓ 桌面端将加载：{BASE}")

    proc.terminate()
    try:
        proc.wait(timeout=15)
        print("  ✓ 后端已停止")
    except subprocess.TimeoutExpired:
        proc.kill()
        print("  ! 停止超时，已强杀")

    print()
    print("=" * 62)
    print("结论：桌面端可以正常拉起后端。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
