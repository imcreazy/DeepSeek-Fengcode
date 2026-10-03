"""压缩切点的配对完整性 —— 把实测结论固定成回归防线。

结论来源（2026-10-04 实测）：
  穷举了 8 种真实消息结构（一问一答、一问多答即一次开多个调用、
  keep_recent 逐档 1~5、尾部有未回填的调用），现有切分逻辑**都不会**切断
  「工具调用」与「工具结果」的配对，因此无需额外改造。

  之所以能覆盖「一次开多个调用」，是因为切分逻辑会一路回退到非工具消息为止：
      while cut > 0 and rest[cut].role == "tool":
          cut -= 1
  一次开 3 个调用、结果连续返回时，切点会一路退到那条 assistant 消息之前。

  该结论做过反向验证：把上面两行保护拿掉后，同样的用例会报出 12 处配对断裂
  （「middle 段结尾还有调用没回填」「recent 段以工具结果开头」）——
  说明这条防线确实拦得住，不是恒绿摆设。

本文件把上述场景固化为测试：**今后若有人改坏切分逻辑，这里会立刻失败**。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_P = Path(__file__).resolve().parent.parent / "src"
os.environ.setdefault("FENGCODE_HOME", tempfile.mkdtemp(prefix="fengcode-pairing-"))
if str(_P) not in sys.path:
    sys.path.insert(0, str(_P))


def _asst(*names: str):
    from fengcode.llm.types import Message, ToolCall
    return Message(
        role="assistant", content="",
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)],
    )


def _tool(call_id: str, name: str = "x"):
    from fengcode.llm.types import Message
    return Message(role="tool", content="结果", tool_call_id=call_id, tool_name=name)


def _split(msgs, keep_recent):
    from fengcode.memory.summarizer import split_for_compaction
    return split_for_compaction(msgs, keep_recent=keep_recent, keep_first=1)


def _open_calls(msgs) -> int:
    """本段结尾还有多少个调用没回填。"""
    open_n = 0
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            open_n += len(m.tool_calls)
        elif m.role == "tool":
            open_n -= 1
    return open_n


def _assert_balanced(middle, recent, where: str) -> None:
    """检查切分是否**制造了**新的断裂。

    ★ 判据要分清「输入本来就未配平」与「切分切出来的」：
      真实会话里，最后一条 assistant 开了 N 个调用、只回了 M < N 个，是**正常中间态**
      （工具还在跑）。那不是切分的错，不能算失败。
      切分只该保证：不去切断**原本已经配平**的配对。
    所以这里检查两件事：
      ① middle 段结尾不能有未回填的调用（那说明把一个配平的对切开了）；
      ② recent 段不能以孤儿工具结果开头（那说明它的调用被切到了 middle）。
    """
    assert _open_calls(middle) == 0, (
        f"{where}：middle 段结尾有未回填的调用（把配平的一对切开了）"
    )
    if recent:
        assert recent[0].role != "tool", (
            f"{where}：recent 段以工具结果开头，其调用被切走在 middle（孤儿结果）"
        )


def test_simple_alternating_pairs():
    """一问一答交替：切点落在自然边界。"""
    from fengcode.llm.types import Message
    msgs = [Message.system("sys"), Message.user("目标")]
    for i in range(4):
        msgs.append(_asst("read_file"))
        msgs.append(_tool(f"c{i}", "read_file"))
    _, mid, rec = _split(msgs, 4)
    _assert_balanced(mid, rec, "一问一答")


def test_single_call_with_multiple_tools():
    """一问多答：一次开 3 个调用、结果连续返回 —— 最容易切出孤儿的形态。"""
    from fengcode.llm.types import Message
    msgs = [Message.system("sys"), Message.user("目标")]
    for r in range(3):
        msgs.append(_asst("a", "b", "c"))
        msgs.append(_tool(f"c{r * 3 + 0}", "a"))
        msgs.append(_tool(f"c{r * 3 + 1}", "b"))
        msgs.append(_tool(f"c{r * 3 + 2}", "c"))
    _, mid, rec = _split(msgs, 5)
    _assert_balanced(mid, rec, "一问多答")


def test_every_keep_recent_value():
    """keep_recent 逐档试探：任何一档都不能切出孤儿。"""
    from fengcode.llm.types import Message
    msgs = [Message.system("sys"), Message.user("目标")]
    for i in range(5):
        msgs.append(_asst("shell"))
        msgs.append(_tool(f"c{i}", "shell"))
    for kr in range(1, 6):
        _, mid, rec = _split(msgs, kr)
        _assert_balanced(mid, rec, f"keep_recent={kr}")


def test_tail_with_unanswered_calls():
    """尾部还有未回填的调用时，也不能切出孤儿。"""
    from fengcode.llm.types import Message
    msgs = [Message.system("sys"), Message.user("目标")]
    msgs.append(_asst("a"))
    msgs.append(_tool("c0", "a"))
    msgs.append(_asst("b", "c", "d"))
    msgs.append(_tool("c1", "b"))          # 只回了 1 个
    _, mid, rec = _split(msgs, 2)
    _assert_balanced(mid, rec, "尾部未回填")


def test_guard_code_is_present():
    """切分逻辑里的保护不能被人拿掉（拿掉后上面的用例会全线断裂）。"""
    import inspect
    from fengcode.memory import summarizer as sm
    src = inspect.getsource(sm.split_for_compaction)
    assert 'rest[cut].role == "tool"' in src, (
        "split_for_compaction 里的配对保护缺失：切点会停在工具消息上，切出孤儿调用"
    )
