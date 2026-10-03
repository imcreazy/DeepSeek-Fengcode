"""实测 exe：起服务 → 验接口 → 停掉。

用法：python scripts/check_exe.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "Fengcode" / "Fengcode.exe"
PORT = 7877
BASE = f"http://127.0.0.1:{PORT}"

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


def get(path: str, timeout: float = 10.0):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    print("=" * 62)
    print("exe 端到端检查")
    print("=" * 62)
    print()

    if not EXE.is_file():
        print(f"  找不到 exe：{EXE}")
        print("  请先运行：pyinstaller fengcode.spec --noconfirm")
        return 1

    size_mb = EXE.stat().st_size / 1048576
    check("exe 存在", True, f"{size_mb:.1f} MB")

    # 先用二进制本身做一次 CLI 调用（不依赖网络/服务）
    cp = subprocess.run([str(EXE), "version"], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    check("`version` 子命令可用", cp.returncode == 0 and "Fengcode" in (cp.stdout or ""),
          (cp.stdout or "").strip().split("\n")[0][:60])

    # 起服务
    print()
    print(f"启动服务（127.0.0.1:{PORT}）…")
    proc = subprocess.Popen(
        [str(EXE), "serve", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    ok_up = False
    for _ in range(60):
        if proc.poll() is not None:
            break
        try:
            st, _ = get("/health", timeout=2)
            if st == 200:
                ok_up = True
                break
        except Exception:
            time.sleep(0.5)
    check("服务启动并 /health 200", ok_up)

    if not ok_up:
        proc.kill()
        out = ""
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:
            pass
        print("  服务输出：", out[-800:])
        return 1

    print()
    print("接口检查：")
    try:
        st, body = get("/health")
        d = json.loads(body)
        check("/health 返回 JSON", isinstance(d, dict), str(d)[:70])
    except Exception as e:
        check("/health 返回 JSON", False, str(e))

    try:
        st, body = get("/")
        text = body.decode("utf-8", "replace")
        check("首页 200 且含 Fengcode", st == 200 and "Fengcode" in text,
              f"{len(body)} 字节")
    except Exception as e:
        check("首页可访问", False, str(e))

    for path, name in [
        ("/api/bootstrap", "bootstrap（界面初始化数据）"),
        ("/api/skills", "技能列表"),
        ("/api/tools", "工具列表"),
        ("/api/plugins", "插件列表"),
        ("/api/memories", "记忆列表"),
        ("/api/config", "配置读取"),
        ("/api/providers", "模型供应商"),
        ("/api/mcp", "MCP 状态"),
        ("/api/stats", "用量统计"),
        ("/api/sessions", "会话列表"),
        ("/api/tasks", "任务列表"),
        ("/api/workspace", "工作区信息"),
        ("/api/subagents", "子智能体"),
        ("/api/workflows", "工作流"),
        ("/api/jobs", "定时任务"),
        ("/api/status", "服务状态"),
    ]:
        try:
            st, body = get(path, timeout=20)
            ok = st == 200
            detail = ""
            if ok:
                try:
                    d = json.loads(body)
                    if isinstance(d, dict):
                        for key in ("skills", "tools", "plugins", "items", "models", "groups"):
                            if key in d and isinstance(d[key], list):
                                detail = f"{key}={len(d[key])}"
                                break
                    elif isinstance(d, list):
                        detail = f"{len(d)} 项"
                except Exception:
                    detail = body[:40].decode("utf-8", "replace")
            check(name, ok, detail)
        except urllib.error.HTTPError as e:
            check(name, False, f"HTTP {e.code}")
        except Exception as e:
            check(name, False, f"{type(e).__name__}: {e}")

    # 确认没有 5xx
    print()
    print("稳定性：")
    bad = []
    for path in ("/api/bootstrap", "/api/skills", "/api/tools", "/api/config",
                 "/api/plugins", "/api/memories", "/api/providers", "/api/tasks",
                 "/api/mcp", "/api/stats", "/api/sessions", "/api/workspace",
                 "/api/subagents", "/api/workflows", "/api/jobs", "/api/status"):
        try:
            st, _ = get(path, timeout=15)
            if st >= 500:
                bad.append((path, st))
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                bad.append((path, e.code))
        except Exception:
            pass
    check("无 5xx 响应", not bad, "" if not bad else str(bad))

    # 停服务
    print()
    proc.terminate()
    try:
        proc.wait(timeout=15)
        check("服务能正常停止", True, f"退出码 {proc.returncode}")
    except subprocess.TimeoutExpired:
        proc.kill()
        check("服务能正常停止", False, "超时，已强杀")

    print()
    print("=" * 62)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
