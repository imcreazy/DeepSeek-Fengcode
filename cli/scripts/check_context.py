"""验证上下文管理改进：锚点保留、收益不足跳过、观测遮罩。

覆盖：
1. keep_first —— 压缩后仍保留会话初始目标（锚点）
2. minimum_progress —— 收益不足时跳过压缩
3. 观测遮罩 —— 折叠旧工具输出但保留调用脉络，且不花模型调用
4. HARD/SOFT 触发区分
5. 执行骨架 —— 摘要里带上"做过什么"
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

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
    print("上下文管理改进验证")
    print("=" * 62)
    print()

    from fengcode.llm.types import Message
    from fengcode.memory.summarizer import (
        TRIGGER_HARD,
        TRIGGER_SOFT,
        detect_trigger,
        mask_observations,
        render_skeleton,
        should_proceed,
        split_for_compaction,
    )

    @check("1. keep_first 保留会话初始目标（锚点）")
    async def _keep_first():
        msgs = [
            Message.system("系统提示"),
            Message.user("帮我重构 D:\\proj 下的解析模块，requirements.md 里有完整需求"),
        ] + [Message.user(f"中间消息 {i} " + "内容" * 40) for i in range(30)] + [
            Message.assistant("最近的回复"),
        ]
        head, middle, recent = split_for_compaction(msgs, keep_recent=5, keep_first=2)
        # 锚点必须包含系统提示与最初的任务描述
        assert len(head) >= 2, f"锚点只有 {len(head)} 条"
        assert head[0].role == "system"
        assert any("重构" in (m.content or "") for m in head), \
            f"初始任务描述丢失：{[m.content[:30] for m in head]}"
        assert len(recent) == 5
        assert middle, "应有待压缩段"
        print(f"         锚点 {len(head)} 条 / 待压缩 {len(middle)} 条 / 最近 {len(recent)} 条")

    @check("2. minimum_progress：收益不足时跳过")
    async def _progress():
        worth, p = should_proceed(10000, 9500)      # 只省 5%
        assert not worth, f"5% 不该算值得（得到 {p:.1%}）"
        worth2, p2 = should_proceed(10000, 4000)    # 省 60%
        assert worth2, f"60% 应当值得（得到 {p2:.1%}）"
        # 自定义阈值
        worth3, _ = should_proceed(10000, 9500, minimum_progress=0.03)
        assert worth3, "阈值放宽后应通过"
        # 边界
        worth4, _ = should_proceed(0, 0)
        assert not worth4, "零输入不该判定为值得"
        print(f"         5% → 跳过；60% → 执行；阈值可调")

    @check("3. 观测遮罩：折叠旧输出但保留调用脉络")
    async def _mask():
        msgs = [Message.system("sys"), Message.user("任务")]
        # 造 10 条工具消息，内容很长
        for i in range(10):
            msgs.append(Message.assistant("", [
                __import__("fengcode.llm.types", fromlist=["ToolCall"]).ToolCall(
                    id=f"c{i}", name=f"tool{i}", arguments={})
            ]))
            msgs.append(Message.tool_result(f"c{i}", f"tool{i}", "输出内容" * 500))
        before = sum(len(m.content or "") for m in msgs)
        masked = mask_observations(msgs, keep_recent=3, max_chars=300)
        after = sum(len(m.content or "") for m in masked)
        assert after < before * 0.6, f"遮罩没生效：{before} → {after}"
        # 最近 3 条工具消息必须原样保留
        tool_msgs = [m for m in masked if m.role == "tool"]
        assert len(tool_msgs) == 10, "工具消息数量不该变"
        for m in tool_msgs[-3:]:
            assert m.meta.get("masked") is not True, "最近的消息不该被遮罩"
        # 被遮罩的要留下标记与原长度
        masked_ones = [m for m in tool_msgs if m.meta.get("masked")]
        assert masked_ones, "应有被遮罩的消息"
        assert all(m.meta.get("original_chars") for m in masked_ones)
        assert "已折叠" in masked_ones[0].content
        # 工具名要保留（这是"脉络"）
        assert all(m.tool_name for m in masked_ones)
        print(f"         字符 {before} → {after}（省 {(1 - after / before) * 100:.0f}%），最近 3 条未动")

    @check("4. HARD / SOFT 触发区分")
    async def _trigger():
        small = [Message.user("短消息")]
        t0, _ = detect_trigger(small, context_window=100000)
        assert t0 is None, f"短上下文不该触发（得到 {t0}）"

        soft_msgs = [Message.user("内容" * 5000) for _ in range(4)]   # 约 60k tokens 量级
        t1, tok1 = detect_trigger(soft_msgs, context_window=60000, ratio=0.5, hard_ratio=0.95)
        assert t1 == TRIGGER_SOFT, f"应触发 SOFT（得到 {t1}, tokens={tok1}）"

        hard_msgs = [Message.user("内容" * 20000) for _ in range(4)]
        t2, tok2 = detect_trigger(hard_msgs, context_window=60000, ratio=0.5, hard_ratio=0.95)
        assert t2 == TRIGGER_HARD, f"应触发 HARD（得到 {t2}, tokens={tok2}）"
        print(f"         短→None；中→SOFT；超限→HARD")

    @check("5. 执行骨架保留「做过什么」")
    async def _skeleton():
        from fengcode.llm.types import ToolCall

        msgs = [
            Message.assistant("", [ToolCall(id="1", name="read_file", arguments={})]),
            Message.tool_result("1", "read_file", "文件内容……" * 100),
            Message.assistant("", [ToolCall(id="2", name="shell", arguments={})]),
            Message.tool_result("2", "shell", "【错误】命令不存在：foobar"),
            Message.assistant("", [ToolCall(id="3", name="write_file", arguments={})]),
            Message.tool_result("3", "write_file", "已写入 100 字节"),
        ]
        sk = render_skeleton(msgs)
        assert "read_file" in sk and "shell" in sk and "write_file" in sk
        assert "成功" in sk and "失败" in sk, f"未区分成败：{sk}"
        # 不该把全文塞进去
        assert len(sk) < 1500, f"骨架过长：{len(sk)}"
        print(f"         骨架 {len(sk)} 字符，含成败标记")

    @check("6. 端到端：Agent 压缩后仍记得初始目标")
    async def _e2e():
        import tempfile
        os.environ["FENGCODE_HOME"] = tempfile.mkdtemp(prefix="oh-")
        from fengcode.config import get_config
        from fengcode.core.agent import Agent

        cfg = get_config()
        cfg.llm.keep_first_messages = 2
        cfg.llm.minimum_progress = 0.05
        cfg.llm.keep_recent_turns = 4
        ag = Agent(config=cfg)

        msgs = [
            Message.system("你是助手"),
            Message.user("总目标：把 D:\\proj 的解析模块从正则改成 AST，要求不破坏现有测试"),
        ] + [Message.user(f"步骤{i}：" + "细节" * 60) for i in range(24)] + [
            Message.assistant("最新进展")
        ]
        out = await ag.compact(msgs, window=8000, session_id=ag.session_id, force=True)
        assert len(out) < len(msgs), f"没有压缩：{len(out)} vs {len(msgs)}"
        joined = "\n".join(m.content or "" for m in out)
        assert "总目标" in joined, "初始目标丢失（keep_first 失效）"
        assert "改成 AST" in joined, "关键需求丢失"
        assert "<compacted_history>" in joined, "没有摘要块"
        print(f"         {len(msgs)} 条 → {len(out)} 条，初始目标保留")

    for fn in (_keep_first, _progress, _mask, _trigger, _skeleton, _e2e):
        await fn()

    print()
    print("=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n, e in FAIL:
        print(f"  ✗ {n}\n      {e}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
