"""防止「答非所问：新问题贴回上一轮的回答」与「报错后重发上一条」回归。

★★★ 实测事故（用户报，1.2.27 之后仍在）：
  · 界面上的输出出现在工具卡与思考块**之前**；
  · 报错之后，上一轮的回答又冒出来一遍，还挂着「补充（连接中断后从会话记录恢复）」；
  · 聊天记录里同一条用户消息出现两次。

★ 根因（两个独立来源，必须两边都堵）：
  ① **后端**：收尾兜底 `content = self._last_assistant_text(msgs)` 从**整个** msgs 里
     找「最后一条有内容的 assistant」—— 本轮没产出时捞到的必然是**上一轮**的，写库
     并作为本轮结果交付。库证据：某会话里 `你会什么` 之后写入的正是上一轮
     「你好！我是 Fengcode…」，且 `reasoning` 为空、meta 里带着 503 错误。
  ② **前端**：`recoverFinalText` 同样「从会话记录找最后一条有内容的 assistant」，
     出错时贴出来就成了「补充（连接中断后从会话记录恢复）」。
  ③ **前端**：断线自动重试对**确定性失败**（后端已明确答复的 4xx/5xx）也重发整轮 ——
     后端会再跑一遍、再写一条用户消息，这就是「报错就重发一次上一次的消息」。

本文件三道防线：
  ① 静态：后端兜底必须限定在**本轮切片**内，且出错时不兜底；
  ② 静态：前端 error 分支必须标记本回合出错，recoverFinalText 据此拒绝补发；
  ③ 静态：前端只有**连接层面**的失败才重试，确定性失败不重试。
"""
from __future__ import annotations

import io
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
AGENT = _SRC / "fengcode" / "core" / "agent.py"
APP = _SRC / "fengcode" / "server" / "static" / "app.js"


def test_backend_fallback_is_limited_to_current_turn():
    """① 收尾兜底只能回捞**本轮**写进 msgs 的文字，且出错时不兜底。"""
    src = io.open(AGENT, encoding="utf-8").read()

    # 本轮起点必须被记下来（否则无法切片）
    assert "turn_msg_start = len(msgs)" in src, \
        "未记录本轮消息起点 —— 兜底无从划分「本轮」与「历史」"

    # 兜底必须用切片，且带「出错不兜底」的条件
    assert "self._last_assistant_text(msgs[turn_msg_start:])" in src, \
        "兜底仍在整个 msgs 里捞 —— 会捞到上一轮的回答（答非所问）"
    assert "if not content and not result.error:" in src, \
        "出错时未禁止兜底 —— 会把上一轮回答伪装成本轮结果"

    # 反向验证：绝不残留「无条件从整个 msgs 捞」的写法
    assert "self._last_assistant_text(msgs) or" not in src.replace(" ", ""), \
        "仍存在从整个 msgs 无条件回捞历史文字的写法"


def test_frontend_error_blocks_backfill():
    """② error 分支要标记本回合出错，补发兜底据此拒绝。"""
    src = io.open(APP, encoding="utf-8").read()

    i = src.find('case "error": {')
    assert i > 0, "找不到 error 分支"
    seg = src[i:src.find('case "result": {', i)]
    assert "c._errored = true" in seg, \
        "error 分支未标记本回合出错 —— 补发兜底会贴出上一轮的回答"

    j = src.find("async function recoverFinalText(")
    assert j > 0, "找不到 recoverFinalText"
    body = src[j:j + 1400]
    assert "c._errored" in body, "recoverFinalText 未检查「本回合出过错」"
    # 判据必须在**真正发网络请求之前**，避免无谓请求与竞态
    assert body.find("c._errored") < body.find("await api("), \
        "「出错不补发」的判据必须排在取会话记录之前"


def test_frontend_retries_only_connection_failures():
    """③ 只有连接层面的失败才自动重试，确定性失败直接如实报错。"""
    src = io.open(APP, encoding="utf-8").read()

    # HTTP 层失败要被标记成确定性失败
    assert "err.deterministic = true" in src, \
        "HTTP 层失败未标记为确定性失败 —— 会被无谓重试并重复写入用户消息"
    # 重试判定里要看这个标记
    k = src.find("for (let attempt = 0; attempt < 2; attempt++)")
    assert k > 0, "找不到断线重试循环"
    seg = src[k:k + 900]
    assert "e.deterministic" in seg, \
        "重试循环未排除确定性失败 —— 报错会重发整轮、写重复消息"


def test_fallback_slice_never_reaches_history():
    """行为验证：`_last_assistant_text` 作用在**本轮切片**上时，取不到历史。

    这是上面那条静态断言的语义补强 —— 只断言「代码里有切片」还不够，
    要证明切片**确实**把上一轮的回答挡在外面。
    """
    import os
    import sys
    import tempfile

    p = _SRC
    os.environ.setdefault("FENGCODE_HOME", tempfile.mkdtemp(prefix="fengcode-attr-"))
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

    from fengcode.core.agent import Agent
    from fengcode.llm.types import Message

    # 模拟 msgs：历史里有一段上一轮的 assistant 回答，然后才是本轮的用户输入
    history = [
        Message.system("系统提示"),
        Message.user("上一轮的提问"),
        Message(role="assistant", content="上一轮的回答：你好！我是 Fengcode…"),
    ]
    msgs = list(history)
    turn_msg_start = len(msgs)          # 本轮起点（run() 里就是这么记的）
    msgs.append(Message.user("本轮提问"))

    # 本轮一个字都没产出 → 切片兜底必须**取不到任何东西**
    assert Agent._last_assistant_text(msgs[turn_msg_start:]) == "", \
        "本轮切片兜底捞到了内容 —— 说明它会取到上一轮的回答（答非所问）"
    # 反向验证：不加切片（旧写法）确实会捞到上一轮那段 —— 坐实这是一处真缺陷
    assert "上一轮的回答" in Agent._last_assistant_text(msgs), \
        "对照失效：旧写法本应捞到历史回答，测试前提不成立"

    # 本轮自己产出了文字 → 切片能捞到它（兜底路径本身仍然可用）
    msgs.append(Message(role="assistant", content="本轮的答复"))
    assert Agent._last_assistant_text(msgs[turn_msg_start:]) == "本轮的答复"
