"""系统提示词的逐字节稳定性测试。

为什么需要这条防线：
  系统提示词是**每一次请求**的缓存前缀。上游的 prompt cache 是「前缀逐字节匹配」
  —— 从头比，一旦某处不同，**之后全部失效**。所以这里任何一个字节的不确定性
  （环境探测抖动、字典/集合遍历顺序、混进时间戳或会话 id）都会让整台机器的
  缓存冷启动，命中率掉到 0，而且现象是「莫名其妙就贵了」，极难排查。

  代码里其实已经有缓存意识（build_system_prompt 的注释专门讲前缀稳定，
  时间提醒也被挪到了消息尾部），但**没有任何测试保证它真的稳定**。
  这条测试就是那道防线：同样输入构建两次，必须完全相等。

失败时的输出只给「第一处分歧附近的一小段」，而不是把两份提示词全甩出来 ——
否则真出问题时，几万字的 diff 反而看不出问题在哪。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="fengcode-prompt-")
os.environ.setdefault("FENGCODE_HOME", _TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _first_divergence(a: str, b: str) -> str:
    """只返回第一处不同附近的一小段，便于一眼定位漂移的段落。"""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    start = max(i - 40, 0)
    return (
        f"第 {i} 字节处开始不同："
        f"...{a[start:i + 40]!r}... vs ...{b[start:i + 40]!r}..."
    )


def _kwargs(**over):
    """一组贴近真实调用的参数（照 agent.py 里 build_system_prompt 的实参）。"""
    from fengcode.config import default_config
    from fengcode.core.prompts import normalize_mode

    base = dict(
        config=default_config(),
        workspace="D:/proj",
        model="m1",
        provider="p1",
        tools=["read_file", "write_file", "shell", "grep"],
        skill_catalog="## 技能\n- 代码评审\n- 深度研究",
        memory_block="<memory>\n- 用户偏好简洁回复\n</memory>",
        notes_block="<notes>\n- 正在做 A\n</notes>",
        todo_block="- [ ] 第一步\n- [x] 第二步",
        goal_block="把 X 做完",
        session_title="测试会话",
        subagent="",
        mode=normalize_mode("auto"),
        extra="",
    )
    base.update(over)
    return base


class TestPromptByteStability:
    """同样输入两次构建必须逐字节相同 —— 这是缓存前缀能命中的前提。"""

    def test_identical_inputs_produce_identical_prompt(self):
        from fengcode.core.prompts import build_system_prompt

        first = build_system_prompt(**_kwargs())
        second = build_system_prompt(**_kwargs())
        assert first == second, (
            "系统提示词在同样输入下不稳定，会让上游前缀缓存整段失效：\n"
            + _first_divergence(first, second)
        )

    def test_stable_across_repeated_builds(self):
        """连构建多次也不能漂（防「偶发」的字典顺序 / 缓存抖动）。"""
        from fengcode.core.prompts import build_system_prompt

        prompts = {build_system_prompt(**_kwargs()) for _ in range(5)}
        assert len(prompts) == 1, (
            f"同样输入构建 5 次得到 {len(prompts)} 种不同结果，存在不确定性来源"
        )

    def test_no_timestamp_or_session_id_in_prefix(self):
        """前缀里不得混进时间戳 / 会话 id —— 那会让每次请求都从头失效。"""
        import re

        from fengcode.core.prompts import build_system_prompt

        p = build_system_prompt(**_kwargs())
        # 形如 2026-10-04 / 12:34:56 的时间痕迹
        assert not re.search(r"\d{4}-\d{2}-\d{2}", p), "提示词里出现了日期，会破坏前缀稳定"
        assert not re.search(r"\d{2}:\d{2}:\d{2}", p), "提示词里出现了时刻，会破坏前缀稳定"

    def test_static_blocks_come_before_dynamic_ones(self):
        """静态块必须排在动态块之前（前缀匹配从前往后，静态在前才留得住缓存）。"""
        from fengcode.core.prompts import build_system_prompt

        p = build_system_prompt(**_kwargs(
            memory_block="<memory>动态记忆</memory>",
            todo_block="- [ ] 动态待办",
        ))
        # 输出风格（静态）应出现在记忆/待办（每轮可能变）之前
        assert p.index("## 输出风格") < p.index("<memory>"), (
            "静态的输出风格排在了动态记忆之后，动态块一变就会把后面的静态文本一起作废"
        )
        assert p.index("## 输出风格") < p.index("<todos>"), (
            "静态的输出风格排在了动态待办之后"
        )

    def test_dynamic_blocks_ordered_by_change_frequency(self):
        """动态块内部按「变化频率从慢到快」排：环境 → 主题/目标 → 技能 → 记忆/便签/待办。"""
        from fengcode.core.prompts import build_system_prompt

        p = build_system_prompt(**_kwargs())
        i_env = p.index("## 当前会话环境")
        i_title = p.index("<session>")
        i_skill = p.index("## 技能")
        i_mem = p.index("<memory>")
        i_todo = p.index("<todos>")
        assert i_env < i_title < i_skill < i_mem < i_todo, (
            "动态块的顺序应体现「几乎不变 → 偶尔变 → 每轮可能变」，"
            f"实际顺序偏移为 env={i_env} title={i_title} skill={i_skill} "
            f"mem={i_mem} todo={i_todo}"
        )

    def test_unrelated_parameter_does_not_disturb_prefix(self):
        """加一段尾部动态内容，前面的静态前缀字节位置不应改变（前缀仍可命中）。"""
        from fengcode.core.prompts import build_system_prompt

        plain = build_system_prompt(**_kwargs(memory_block=""))
        with_mem = build_system_prompt(**_kwargs(memory_block="<memory>新召回的一条</memory>"))
        # 静态头部必须完全相同 —— 这样命中的前缀长度不受动态内容影响
        head_len = plain.index("## 当前会话环境")
        assert plain[:head_len] == with_mem[:head_len], (
            "尾部动态内容改变了静态前缀的字节内容：\n"
            + _first_divergence(plain[:head_len], with_mem[:head_len])
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
