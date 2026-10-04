"""验证「做完不回复收尾文字」的修复：被步数上限 / 绕圈判定打断时，必须补一次收尾发言。

用户实测：让 AI 做一个 HTML 动画，跑到一半被掐断，**连一句总结都没有**。
根因：模型一直在调工具，被 `while step < limit` 硬上限打断时，本回合
`final_text_parts` 是空的 —— 于是没有任何交付文字。

本文件用**假的 LLM** 真跑一遍 agent.run()：
  · 前 N 次返回工具调用（模拟长任务一直在干活）；
  · 之后返回一段收尾文本。
断言：被上限打断时必须补出收尾文字；正常完成时不得多补一次。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FENGCODE_HOME", tempfile.mkdtemp(prefix="fengcode-closing-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fengcode.config.schema import Config          # noqa: E402
from fengcode.core.agent import Agent              # noqa: E402
from fengcode.llm.types import (                   # noqa: E402
    LLMResponse,
    StreamEvent,
    ToolCall,
    Usage,
)

MARKER = "SUMMARY_MARKER"


class _FakeLLM:
    """前 tool_rounds 次返回工具调用，之后返回收尾文本。

    为什么用「不存在的工具」：它会执行失败，但主循环仍会把它当作「有工具调用」
    继续下一轮 —— 正好模拟「模型一直在干活、迟迟不产出正文」的情形。

    ★ 关键：当传入的工具表为**空**时（即 agent 在补收尾发言），必须直接返回文本。
      因为真实的补收尾就是不给工具、强制它说话；若这里仍返回工具调用，
      就模拟错了场景（会把「模型又要调工具」当成正常路径）。
    """

    def __init__(self, tool_rounds: int) -> None:
        self.n = 0
        self.tool_rounds = tool_rounds

    async def chat_stream(self, msgs, model=None, tools=None, **kw):  # noqa: ANN001
        self.n += 1
        no_tools = not tools
        if not no_tools and self.n <= self.tool_rounds:
            yield StreamEvent(
                type="tool_call",
                tool_call=ToolCall(id=f"c{self.n}", name="no_such_tool", arguments={}),
            )
            yield StreamEvent(type="done", finish_reason="tool_calls")
        else:
            yield StreamEvent(type="text", text=MARKER)
            yield StreamEvent(type="done", finish_reason="stop")
        yield StreamEvent(
            type="usage",
            usage=Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105),
        )

    async def chat(self, msgs, model=None, tools=None, **kw):  # noqa: ANN001
        self.n += 1
        return LLMResponse(content=MARKER)


def _agent(sid: str) -> Agent:
    return Agent(session_id=sid, config=Config())


def test_summary_added_when_cut_off_by_step_limit():
    """被步数上限打断时，必须补出收尾文字。"""
    ag = _agent("s-closing-cut")
    fake = _FakeLLM(tool_rounds=3)
    ag.llm = fake
    res = asyncio.run(
        ag.run("随便做点什么", session_id="s-closing-cut", stream=True, max_steps=3)
    )
    assert MARKER in (res.content or ""), (
        "被步数上限打断后应补一次收尾发言，实得 content=%r" % (res.content,)
    )


def test_no_extra_summary_on_normal_finish():
    """正常完成（一次就给出正文）时，不得多补一轮。"""
    ag = _agent("s-normal-finish")
    fake = _FakeLLM(tool_rounds=0)
    ag.llm = fake
    res = asyncio.run(
        ag.run("你好", session_id="s-normal-finish", stream=True, max_steps=3)
    )
    assert MARKER in (res.content or "")
    assert fake.n == 1, f"正常完成不该补收尾，实际调用了 {fake.n} 次"


def test_summary_added_when_stalled():
    """连续多轮无新证据（绕圈）而收尾时，也必须补出收尾文字。"""
    ag = _agent("s-closing-stall")
    # 工具轮数给足够多，让它先撞上「连续 8 轮无新证据」的绕圈判定
    fake = _FakeLLM(tool_rounds=20)
    ag.llm = fake
    res = asyncio.run(
        ag.run("做个东西", session_id="s-closing-stall", stream=True, max_steps=0)
    )
    assert MARKER in (res.content or ""), (
        "绕圈判定收尾后应补一次收尾发言，实得 content=%r" % (res.content,)
    )
