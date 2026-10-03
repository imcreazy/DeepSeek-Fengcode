"""验证工具事件不重复上报。

曾经的问题：agent._execute_call 与 tools/base.py 的 registry.execute
各自都发一次 tool.start / tool.end，界面里每个工具打印两遍。

用法：python scripts/check_events.py
"""
import asyncio
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

HOME = Path(tempfile.mkdtemp(prefix="fengcode-events-"))
os.environ["FENGCODE_HOME"] = str(HOME)

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
    print("=" * 62)
    print("工具事件唯一性检查")
    print("=" * 62)
    print()

    from fengcode.config import get_config
    from fengcode.core.agent import Agent
    from fengcode.events import Ev, get_bus

    cfg = get_config()
    ws = HOME / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    ag = Agent(config=cfg, workspace=ws)

    # 直接构造一次内置工具调用，绕开模型
    from fengcode.llm.types import ToolCall
    from fengcode.tools.base import ToolContext, ToolRegistry

    starts: list[str] = []
    ends: list[str] = []
    order: list[tuple[str, str]] = []

    def on_event(ev):
        d = ev.data or {}
        if ev.type == Ev.TOOL_START:
            starts.append(d.get("name"))
            order.append(("start", d.get("name")))
        elif ev.type == Ev.TOOL_END:
            ends.append(d.get("name"))
            order.append(("end", d.get("name")))

    bus = get_bus()
    bus.on_event(on_event)

    print("【1】内置工具（write_file）经 agent._execute_call")
    ctx = ag.tool_context("test-session")
    await ag._execute_call(
        ToolCall(id="c1", name="write_file",
                 arguments={"path": "hello.txt", "content": "测试内容\n"}),
        ctx, "test-session",
    )
    n_start = starts.count("write_file")
    n_end = ends.count("write_file")
    check("tool.start 恰好 1 次", n_start == 1, f"实际 {n_start} 次")
    check("tool.end 恰好 1 次", n_end == 1, f"实际 {n_end} 次")

    print()
    print("【2】连续 3 次内置工具调用")
    starts.clear(); ends.clear()
    for i in range(3):
        await ag._execute_call(
            ToolCall(id=f"c{i+2}", name="read_file", arguments={"path": "hello.txt"}),
            ctx, "test-session",
        )
    check("3 次调用 → 3 次 tool.start", starts.count("read_file") == 3,
          f"实际 {starts.count('read_file')} 次")
    check("3 次调用 → 3 次 tool.end", ends.count("read_file") == 3,
          f"实际 {ends.count('read_file')} 次")

    print()
    print("【3】事件配对：每次 start 都有对应 end")
    starts.clear(); ends.clear(); order.clear()
    for i in range(2):
        await ag._execute_call(
            ToolCall(id=f"d{i}", name="list_dir", arguments={"path": "."}),
            ctx, "test-session",
        )
    seq = [k for k, _ in order]
    check("start/end 交替出现", seq == ["start", "end", "start", "end"], f"实际 {seq}")

    print()
    print("【4】事件带时长与展示信息")
    got: list[dict] = []
    bus.off_event(on_event)

    def on2(ev):
        if ev.type == Ev.TOOL_END:
            got.append(ev.data or {})

    bus.on_event(on2)
    await ag._execute_call(
        ToolCall(id="e1", name="write_file",
                 arguments={"path": "b.txt", "content": "x"}),
        ctx, "test-session",
    )
    bus.off_event(on2)
    d = got[0] if got else {}
    check("含 duration 字段", "duration" in d, f"值={d.get('duration')}")
    check("含 display 字段", bool(d.get("display")), str(d.get("display"))[:40])

    print()
    print("【5】失败的工具也不重复上报")
    starts.clear(); ends.clear()
    bus.on_event(on_event)
    await ag._execute_call(
        ToolCall(id="f1", name="read_file", arguments={"path": "不存在的文件.txt"}),
        ctx, "test-session",
    )
    bus.off_event(on_event)
    check("失败调用 tool.start 1 次", starts.count("read_file") == 1,
          f"{starts.count('read_file')} 次")
    check("失败调用 tool.end 1 次", ends.count("read_file") == 1,
          f"{ends.count('read_file')} 次")

    print()
    print("=" * 62)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
