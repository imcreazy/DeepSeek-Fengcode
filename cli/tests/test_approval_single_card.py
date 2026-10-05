"""一次危险操作只该弹一张确认卡（2026-10-05 实测反馈）。

现象：执行一次递归删除时，右侧「需要你确认（中等风险）」一下子弹出**四个**
一模一样的卡片。

两个独立成因，都要锁死：
  ① 后端对同一次审批发两条 approval.request ——
     tools/base.py 自己 emit 一次，ApprovalGate.request() 又通过
     on_request（Agent._on_approval）emit 一次；
  ② 前端 SSE（/api/chat）与 WebSocket 是两条独立通道，都订阅同一个事件总线，
     同一条事件被两条各送一份 → 2 × 2 = 4 张卡。
所以既断言后端单发，也断言前端按 id 幂等。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fengcode.config.schema import PermissionsConfig
from fengcode.events import EventBus, Ev
from fengcode.security.approval import ApprovalGate
from fengcode.tools import register_builtin
from fengcode.tools.base import ToolContext, get_registry

APP_JS = Path(__file__).resolve().parents[1] / "src" / "fengcode" / "server" / "static" / "app.js"
BASE_PY = Path(__file__).resolve().parents[1] / "src" / "fengcode" / "tools" / "base.py"

# 与截图里那条命令同型（递归删除），确保确实会被判定成「需要确认」。
DANGEROUS_CMD = "Remove-Item -Recurse -Force _shot.py, _probe.py"


async def test_one_dangerous_call_emits_exactly_one_approval_request():
    """走真实 ToolRegistry.execute：一次审批只发一条 approval.request。"""
    bus = EventBus()
    queue = bus.subscribe()
    seen: list[str] = []

    cfg = PermissionsConfig(mode="ask")
    # 与 Agent.__init__ 完全相同：on_request 是通知界面的唯一通道。
    gate = ApprovalGate(
        cfg,
        workspace=str(Path(__file__).resolve().parent),
        on_request=lambda req: bus.emit(Ev.APPROVAL, req.to_dict(), session_id="s1"),
    )
    gate.set_responder(bus)          # 与 server/app.py 一致

    registry = get_registry()
    if not registry.all():
        register_builtin(registry)

    ctx = ToolContext(
        workspace=Path(__file__).resolve().parent,
        config=cfg,
        approval=gate,
        bus=bus,
        session_id="s1",
    )

    task = asyncio.create_task(registry.execute("shell", {"command": DANGEROUS_CMD}, ctx))
    await asyncio.sleep(0.2)
    while not queue.empty():
        ev = queue.get_nowait()
        seen.append(ev.type)
        if ev.type == Ev.APPROVAL:
            # ★ 一律拒绝：本用例只验证「发几条」，绝不能让测试真的去删文件。
            gate.resolve_with_key(
                ev.data["id"], False,
                action=ev.data["action"], target=ev.data["target"], session_id="s1",
            )
    result = await task
    while not queue.empty():
        seen.append(queue.get_nowait().type)

    assert seen.count(Ev.APPROVAL) == 1, (
        f"一次危险操作只应发一条 approval.request，实际发了 "
        f"{seen.count(Ev.APPROVAL)} 条（事件序列：{seen}）。"
        "重复发会与前端两条订阅通道相乘，弹出多张一模一样的确认卡。"
    )
    assert result.ok is False                    # 被拒绝，且没真的执行
    assert result.error and "未批准" in result.error


def test_tools_base_does_not_emit_approval_request_itself():
    """tools/base.py 不得自己 emit approval.request —— 那是审批门的职责。

    为什么用静态断言而不是只跑上面的用例：上面的用例只覆盖 shell 一条路径，
    而这里是「任何工具」都要遵守的约定。留一行多余 emit 就会复发。
    """
    src = BASE_PY.read_text(encoding="utf-8")
    # 只查「真的去 emit 这个事件」的写法（注释里提到事件名是允许的，那是解释为什么）。
    assert not re.search(r'emit\(\s*["\']approval\.request["\']', src), (
        "tools/base.py 不应直接 emit approval.request："
        "ApprovalGate.on_request 才是通知界面的唯一通道。"
    )


def test_frontend_show_approval_is_idempotent_by_request_id():
    """前端 showApproval 必须按请求 id 幂等，否则 SSE + WS 双通道会叠成多张卡。"""
    src = APP_JS.read_text(encoding="utf-8")
    i = src.index("function showApproval")
    fn = src[i:i + 1200]
    m = re.search(r'getElementById\(\s*"ap-"\s*\+\s*a\.id\s*\)', fn)
    assert m, "showApproval 必须先查「同 id 的卡是否已存在」"
    assert re.search(r'if\s*\([^)]*ap-[\s\S]{0,120}?return', fn), (
        "已存在同 id 的卡时必须直接 return，不再渲染第二张"
    )
    assert 'id = "ap-" + a.id' in fn, "卡片 id 需保持 ap-<请求id> 的约定，去重才有依据"
