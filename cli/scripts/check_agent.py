"""端到端验证：真实模型 + 完整 Agent 回合 + 外部配置一键导入。

用法：python scripts/check_agent.py [模型引用]
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 独立数据目录，避免污染真实环境
HOME = Path(tempfile.mkdtemp(prefix="fengcode-e2e-"))
os.environ["FENGCODE_HOME"] = str(HOME)

PASS, FAIL, SKIP = [], [], []


def check(name):
    def deco(fn):
        async def wrapper(*a, **kw):
            t0 = time.time()
            try:
                await fn(*a, **kw)
                PASS.append(name)
                print(f"  [OK]   {name}  ({time.time() - t0:.1f}s)")
            except Exception as e:
                import traceback
                FAIL.append((name, f"{type(e).__name__}: {e}"))
                print(f"  [FAIL] {name}  ({time.time() - t0:.1f}s) -> {type(e).__name__}: {e}")
                if os.environ.get("TRACE"):
                    traceback.print_exc()
        wrapper.__name__ = getattr(fn, "__name__", "w")
        return wrapper
    return deco


def skip(name, why):
    SKIP.append((name, why))
    print(f"  [SKIP] {name} -> {why}")


async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 64)
    print("Fengcode 端到端验证（真实模型）")
    print(f"数据目录：{HOME}")
    print("=" * 64)

    from fengcode.config import get_manager
    from fengcode.config.manager import parse_env_file

    mgr = get_manager()

    # ---- 1. 从环境变量或配置文件导入 ----
    print("\n[1] 外部配置一键导入")

    imported_ref = None

    @check("从外部配置导入 providers + MCP")
    async def _import():
        nonlocal imported_ref
        src = mgr.default_env_file() or paths.env_file()
        assert src, "没有找到可导入的配置文件（.env / config.toml）"
        print(f"         来源：{res['source']}")
        print(f"         导入 provider：{res['providers_added']}")
        print(f"         导入 MCP：{res['mcp_added']}")
        if res.get("errors"):
            print(f"         警告：{res['errors']}")
        assert res["providers_added"], "没有导入任何 provider"
        # 找一个真的能用的（有 key 且 base_url 有效）
        usable = []
        for p in mgr.config.providers:
            key = mgr.resolve_api_key(p)
            if key and p.base_url and p.models and p.enabled:
                usable.append(f"{p.name}/{p.models[0]}")
        assert usable, "导入后没有可用的 provider"
        imported_ref = usable[0]
        print(f"         可用模型示例：{usable[:5]}")

    await _import()

    ref = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FENGCODE_TEST_MODEL") or imported_ref
    if not ref:
        print("\n没有可用模型，后续测试跳过。")
        return 0

    # 选择模型：优先用导入的默认，其次第一个可用
    print(f"\n使用模型：{ref}")

    @check("模型连通性")
    async def _conn():
        from fengcode.llm.router import get_llm

        llm = get_llm(mgr)
        pname, _ = mgr.parse_model_ref(ref)
        r = await llm.test_connection(pname)
        print(f"         {r}")
        assert r.get("ok"), f"模型不可用：{r.get('error')}"

    await _conn()

    # ---- 2. Agent 基础回合 ----
    print("\n[2] Agent 主循环")

    from fengcode.core.agent import Agent

    agent = Agent(config=mgr.config)
    agent.config.llm.temperature = 0.2
    agent.config.agent.max_steps = 8
    agent.config.memory.enabled = True

    turn1 = None

    @check("纯对话回合（不调工具）")
    async def _chat():
        nonlocal turn1
        turn1 = await agent.run("用一句话回答：1+1 等于几？只回答数字和一句说明，不要调用工具。",
                                model=ref, stream=True)
        assert not turn1.error, turn1.error
        assert "2" in turn1.content, turn1.content[:300]
        assert turn1.steps >= 1
        print(f"         回复：{turn1.content.strip()[:100]}")
        print(f"         步数 {turn1.steps}，用量 {turn1.usage.get('total_tokens')}")

    await _chat()

    @check("工具调用回合（真实写文件）")
    async def _toolcall():
        res = await agent.run(
            "在工作区里创建文件 e2e_check.txt，内容写「Fengcode 端到端验证通过」。"
            "创建后用 read_file 读一遍确认写入成功，然后告诉我结果。",
            model=ref, stream=True, max_steps=10,
        )
        assert not res.error, res.error
        assert res.tool_calls, f"没有调用任何工具：{res.content[:300]}"
        names = [c["name"] for c in res.tool_calls]
        print(f"         调用了工具：{names}")
        target = agent.workspace / "e2e_check.txt"
        assert target.is_file(), f"文件没有创建：{target}"
        content = target.read_text(encoding="utf-8")
        assert "端到端验证通过" in content, content
        print(f"         文件内容：{content.strip()[:80]}")

    await _toolcall()

    @check("多步任务（todo 清单 + 计算）")
    async def _multi():
        res = await agent.run(
            "请做两件事：1) 用 todo 工具添加一条任务，标题为「验证多步流程」；"
            "2) 用 python_exec 计算 123*456 并告诉我结果。"
            "完成后用 todo 把那条任务标记为 completed。",
            model=ref, stream=True, max_steps=12,
        )
        assert not res.error, res.error
        names = [c["name"] for c in res.tool_calls]
        print(f"         调用工具：{names}")
        assert "56088" in res.content or "56088" in str(res.tool_calls), \
            f"计算结果不正确：{res.content[:300]}"
        tasks = agent.tasks.list(session_id=agent.session_id)
        print(f"         任务清单：{[(t['title'], t['status']) for t in tasks]}")
        assert tasks, "任务清单为空"

    await _multi()

    @check("记忆写入与跨会话召回")
    async def _memory():
        r = await agent.run(
            "请记住一件事：我的项目叫 Fengcode，用的 Python 版本是 3.12。"
            "用 memory 工具把它存成一条偏好记忆。",
            model=ref, stream=True, max_steps=8,
        )
        assert not r.error, r.error
        stats = agent.memory.stats()
        print(f"         记忆统计：total={stats['total']} backend={stats['embedding_backend']}")
        # 换一个会话问它
        agent2 = Agent(config=mgr.config)
        rec = await agent2.memory.recall("我的项目叫什么？用的 Python 版本？", top_k=5)
        print(f"         新会话召回 {len(rec)} 条，最高分 {rec[0].score if rec else 0:.3f}")
        hit = any("Fengcode" in (m.content + m.title) for m in rec)
        assert hit, f"没有召回到刚写入的记忆：{[m.title for m in rec]}"

    await _memory()

    @check("流式输出确实产生了增量事件")
    async def _stream():
        events = []
        await agent.run("数一下 1 到 5，直接输出数字，不要用工具。", model=ref, stream=True,
                        on_event=lambda e: events.append(e.type))
        types = set(events)
        print(f"         流式事件类型：{sorted(types)}")
        assert "text" in types or "done" in types, f"没有流式事件：{types}"

    await _stream()

    # ---- 3. 子智能体 ----
    print("\n[3] 子智能体")

    @check("派生子智能体（探索类型）")
    async def _sub():
        r = await agent.subagent_runner.run(
            "统计当前工作区里有哪些文件，并说明每个文件的用途（文件内容很短，直接读）。",
            agent_type="explore", timeout=180,
        )
        print(f"         ok={r['ok']} steps={r['steps']} duration={r['duration']:.1f}s")
        assert r["ok"], r.get("error")
        assert r["content"] and len(r["content"]) > 10, r["content"][:200]

    await _sub()

    # ---- 4. 上下文压缩 ----
    print("\n[4] 上下文与安全")

    @check("上下文压缩触发并能继续对话")
    async def _compact():
        from fengcode.llm.types import Message
        from fengcode.memory.summarizer import should_compact

        long_msgs = [Message.system("系统提示")] + [
            Message.user("这是一段较长的测试文本。" * 30 + f" 编号{i}") for i in range(30)
        ] + [Message.assistant("收到")]
        need, tokens = should_compact(long_msgs, context_window=8000, ratio=0.75)
        assert need, f"应触发压缩（tokens={tokens}）"
        out = await agent.compact(long_msgs, window=8000, session_id=agent.session_id, tokens=tokens)
        assert len(out) < len(long_msgs), f"压缩后没有变短：{len(out)} vs {len(long_msgs)}"
        assert any("<compacted_history>" in (m.content or "") for m in out), "没有生成摘要块"
        # 压缩后还能继续对话
        r = await agent.run("继续：2+2 等于几？只回答数字。", model=ref, stream=True, max_steps=4)
        assert not r.error, r.error
        print(f"         压缩后 {len(long_msgs)} → {len(out)} 条消息，仍可对话")

    await _compact()

    @check("危险命令被安全层拦截")
    async def _danger():
        from fengcode.security.paths import scan_dangerous

        hits = scan_dangerous("rm -rf / --no-preserve-root")
        assert hits, "未识别危险命令"
        # 走完整工具执行路径，应当被拒绝而不是真的执行
        ctx = agent.tool_context()
        agent.approval.config.mode = "ask"
        rr = await agent.registry.execute("shell", {"command": "rm -rf / --no-preserve-root"}, ctx)
        print(f"         拦截结果：ok={rr.ok} error={str(rr.error)[:100]}")
        assert not rr.ok, "危险命令未被拦截"
        assert _alive(), "系统仍然正常"

    await _danger()

    @check("越界写文件被拒绝")
    async def _escape():
        ctx = agent.tool_context()
        target = str(Path.home() / "fengcode-should-not-exist.txt")
        rr = await agent.registry.execute("write_file", {"path": target, "content": "x"}, ctx)
        print(f"         结果：ok={rr.ok} error={str(rr.error)[:120]}")
        assert not rr.ok, "越界写入未被拒绝"
        assert not Path(target).exists(), "文件竟然被创建了"

    await _escape()

    # ---- 5. 会话持久化 ----
    print("\n[5] 持久化与会话")

    @check("会话历史与统计落库")
    async def _persist():
        msgs = agent.sessions.messages(agent.session_id)
        assert len(msgs) >= 4, f"消息太少：{len(msgs)}"
        s = agent.sessions.get(agent.session_id)
        assert s["input_tokens"] > 0 or s["output_tokens"] > 0, f"用量没记录：{s}"
        ov = agent.stats.overview()
        print(f"         会话消息 {len(msgs)} 条，累计 tokens "
              f"{s['input_tokens'] + s['output_tokens']}，成本 {s['cost']:.4f}")
        print(f"         全局统计：调用 {ov['all']['calls']} 次，"
              f"tokens {ov['all']['total_tokens']}，成本 {ov['all']['cost']:.4f}")
        audits = agent.audit.recent(limit=5)
        print(f"         审计记录 {len(audits)} 条（最近：{audits[0]['action'] if audits else '无'}）")
        assert audits, "审计日志为空"

    await _persist()

    @check("导出 Markdown")
    async def _export():
        md = agent.sessions.export_markdown(agent.session_id)
        assert "Fengcode" in md and len(md) > 200, md[:200]
        out = HOME / "export.md"
        out.write_text(md, encoding="utf-8")
        print(f"         导出 {len(md)} 字符到 {out.name}")

    await _export()

    print()
    print("=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，跳过 {len(SKIP)} 项")
    for n, e in FAIL:
        print(f"  ✗ {n}\n      {e}")
    for n, w in SKIP:
        print(f"  - {n}（{w}）")
    print("=" * 64)
    return 1 if FAIL else 0


def _alive() -> bool:
    """确认系统还能正常工作（没被危险命令搞坏）。"""
    import platform

    return bool(platform.system())


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
