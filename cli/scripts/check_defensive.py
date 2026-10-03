"""验证防御性实现：结果独立上报、进程停稳、符号链接不穿透。

覆盖：
1. 正交结果独立上报 —— 超时/被杀/启动失败不会被误判为成功
2. dispose 达到停稳 —— kill 后确认进程真的退出，不留孤儿
3. 符号链接删除不穿透 —— 只删链接，不碰目标
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
HOME = Path(tempfile.mkdtemp(prefix="defensive-"))
os.environ["FENGCODE_HOME"] = str(HOME)

PASS, FAIL = [], []


def check(name):
    def deco(fn):
        async def wrapper(*a, **kw):
            try:
                await fn(*a, **kw)
                PASS.append(name)
                print(f"  [OK]   {name}")
            except Exception as e:
                import traceback
                FAIL.append((name, f"{type(e).__name__}: {e}"))
                print(f"  [FAIL] {name} -> {type(e).__name__}: {e}")
                if os.environ.get("TRACE"):
                    traceback.print_exc()
        return wrapper
    return deco


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("防御性模式验证")
    print("=" * 62)
    print()

    from fengcode.security.sandbox import ExecResult, LocalSandbox

    @check("1. 正交结果独立上报：超时不因退出码 0 而误判成功")
    async def _orthogonal():
        # 构造一个"捕获终止信号后正常退出"的场景：Python 里捕获 SIGTERM 后 exit(0)
        code = (
            "import signal, sys, time\n"
            "def h(sig, frm):\n"
            "    print('收到信号，正常退出'); sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, h)\n"
            "time.sleep(30)\n"
        )
        sb = LocalSandbox(cwd=HOME, timeout=1.5)
        r = await sb.python(code, timeout=1.5)
        # 关键：即使进程最终可能以 0 退出，超时这一事实必须独立成立
        assert r.timed_out, "未标记超时"
        assert not r.ok, f"超时却判定为成功（status={r.status}）"
        assert r.status == "timeout", r.status
        d = r.to_dict()
        for k in ("timed_out", "killed", "signal", "spawn_failed", "status", "ok"):
            assert k in d, f"缺少独立字段 {k}"
        print(f"         status={r.status} ok={r.ok} signal={r.signal}")

    @check("2. 启动失败与运行失败区分开")
    async def _spawn():
        sb = LocalSandbox(cwd=HOME, timeout=10)
        r = await sb.run(["definitely-not-a-real-command-xyz123"])
        assert not r.ok
        assert r.spawn_failed is True, "未标记 spawn_failed"
        assert r.status == "spawn_failed", r.status
        assert r.returncode == 127
        # 对照：能启动但非零退出
        r2 = await sb.python("import sys; sys.exit(3)")
        assert not r2.ok
        assert r2.spawn_failed is False, "不该标记启动失败"
        assert r2.status == "failed", r2.status
        assert r2.returncode == 3
        print(f"         启动失败 status={r.status}；运行失败 status={r2.status}")

    @check("3. 正常成功判定")
    async def _success():
        sb = LocalSandbox(cwd=HOME, timeout=20)
        r = await sb.python("print('ok')")
        assert r.ok, f"正常执行应为成功：{r.to_dict()}"
        assert r.status == "ok"
        assert r.signal is None
        assert r.spawn_failed is False
        print(f"         status={r.status} returncode={r.returncode}")

    @check("4. dispose 达到停稳：kill 后进程确实退出")
    async def _quiescence():
        sb = LocalSandbox(cwd=HOME, timeout=60)
        # 起一个长睡进程，然后手动杀
        import asyncio as aio
        import subprocess as sp
        import sys as _sys

        proc = await aio.create_subprocess_exec(
            _sys.executable, "-c", "import time; time.sleep(120)",
            stdout=sp.DEVNULL, stderr=sp.DEVNULL,
        )
        pid = proc.pid
        assert proc.returncode is None, "进程应在运行"
        stopped = await LocalSandbox.kill(proc)
        assert stopped is True, "kill 应返回已停稳"
        assert proc.returncode is not None, "returncode 未落定，说明没等停稳"
        # 再用系统手段确认进程真的没了
        gone = await aio.to_thread(_pid_gone, pid)
        assert gone, f"进程 {pid} 仍存活（留下孤儿）"
        print(f"         PID {pid} 已确认退出（returncode={proc.returncode}）")

    @check("5. 符号链接删除不穿透目标")
    async def _symlink():
        ws = HOME / "linktest"
        ws.mkdir(parents=True, exist_ok=True)
        target_dir = HOME / "valuable_data"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "important.txt").write_text("重要数据", encoding="utf-8")

        link = ws / "shortcut"
        try:
            if os.name == "nt":
                # Windows 建 junction（无需管理员）
                import subprocess as sp
                r = sp.run(["cmd", "/c", "mklink", "/J", str(link), str(target_dir)],
                           capture_output=True, text=True)
                if r.returncode != 0:
                    print(f"         （跳过：无法创建 junction：{r.stderr.strip()[:60]}）")
                    return
            else:
                link.symlink_to(target_dir, target_is_directory=True)
        except Exception as e:
            print(f"         （跳过：{type(e).__name__}: {e}）")
            return

        from fengcode.security.paths import PathGuard
        from fengcode.tools import register_builtin
        from fengcode.tools.base import ToolContext, get_registry, reset_registry
        from fengcode.tools.builtin.files import _is_link_like

        assert _is_link_like(link), "未识别为链接"

        reset_registry()
        register_builtin()
        ctx = ToolContext(
            workspace=ws,
            guard=PathGuard(workspace=ws, write_paths=[str(HOME)], deny_patterns=[]),
        )
        res = await get_registry().execute(
            "file_ops", {"action": "delete", "path": str(link), "recursive": True},
            ctx, skip_approval=True,
        )
        assert res.ok, res.error
        assert not link.exists(), "链接本身应被删除"
        # 关键：目标目录与其中的文件必须完好
        assert target_dir.is_dir(), "目标目录被删了（穿透）"
        assert (target_dir / "important.txt").is_file(), "目标内容被删了（穿透）"
        assert (target_dir / "important.txt").read_text(encoding="utf-8") == "重要数据"
        reset_registry()
        print(f"         链接已删，目标目录与内容完好")

    for fn in (_orthogonal, _spawn, _success, _quiescence, _symlink):
        await fn()

    print()
    print("=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n, e in FAIL:
        print(f"  ✗ {n}\n      {e}")
    print("=" * 62)
    return 1 if FAIL else 0


def _pid_gone(pid: int) -> bool:
    """确认进程已不存在。"""
    try:
        import psutil  # type: ignore

        return not psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        import subprocess as sp

        r = sp.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) not in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return False
    except (OSError, ProcessLookupError):
        return True


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
