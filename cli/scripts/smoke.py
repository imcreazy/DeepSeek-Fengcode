"""冒烟测试：验证内核各层能否真正跑起来。

运行：python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 用临时目录做数据根，避免污染真实环境
os.environ["FENGCODE_HOME"] = tempfile.mkdtemp(prefix="fengcode-smoke-")

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def check(name: str):
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                r = fn(*a, **kw)
                if asyncio.iscoroutine(r):
                    asyncio.run(r)
                PASS.append(name)
                print(f"  [OK]   {name}")
                return True
            except Exception as e:
                FAIL.append((name, f"{type(e).__name__}: {e}"))
                print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
                if os.environ.get("SMOKE_TRACE"):
                    traceback.print_exc()
                return False

        return wrapper

    return deco


def main() -> int:
    import sys as _sys

    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("Fengcode 冒烟测试")
    print("=" * 62)

    print("\n[1] 基础层")
    check("导入 fengcode 包")(lambda: __import__("fengcode"))()

    @check("路径系统")
    def _paths():
        from fengcode import paths

        paths.ensure_all()
        assert paths.home().is_dir(), "数据目录未创建"
        assert paths.config_file().parent.is_dir()
        return True

    _paths()

    @check("TOML 读写往返")
    def _toml():
        from fengcode.utils import toml as T

        data = {"a": 1, "中文键": "中文值", "nested": {"x": True, "arr": [1, 2]},
                "tables": [{"name": "n1", "v": 2}, {"name": "n2", "v": 3}]}
        text = T.dumps(data)
        assert "中文值" in text
        p = Path(os.environ["FENGCODE_HOME"]) / "t.toml"
        T.dumpf(p, data)
        back = T.loadf(p)
        assert back["中文键"] == "中文值", back
        assert back["nested"]["x"] is True
        assert len(back["tables"]) == 2
        return True

    _toml()

    @check("工具函数")
    def _utils():
        from fengcode.utils import estimate_tokens, human_size, scrub_secrets, sanitize_filename

        assert estimate_tokens("你好世界") > 0
        assert human_size(1536).startswith("1.5")
        assert "sk-" not in scrub_secrets("key=sk-abcdef1234567890abcdef")
        assert "/" not in sanitize_filename("a/b/c.txt")
        return True

    _utils()

    print("\n[2] 配置系统")
    cfg = None

    @check("默认配置生成（不含密钥）")
    def _cfg():
        nonlocal cfg
        from fengcode.config import default_config

        cfg = default_config()
        assert cfg.providers, "没有预置 provider"
        for p in cfg.providers:
            assert not p.api_key, f"{p.name} 预置了密钥（不应该）"
        assert cfg.ui.theme == "light", "默认主题应为浅色"
        return True

    _cfg()

    @check("配置管理器读写")
    def _mgr():
        from fengcode.config import get_manager

        m = get_manager()
        m.save(backup=False)
        assert Path(m.path).is_file()
        # 默认配置只预置万象 API 且不带密钥，所以一开始没有默认模型；
        # 配好一个能用的供应商后应该能解析出来。
        m.add_provider("test-prov", base_url="https://example.com/v1",
                       models=["m1", "m2"], api_key="x",
                       default="m1", enabled=True)
        m.config.llm.default_provider = "test-prov"
        m.config.llm.default_model = "m1"
        ref = m.default_model_ref()
        assert ref, "配好供应商后应该能解析默认模型引用"
        assert ref == "test-prov/m1", ref
        m2 = m
        assert m2.get_provider("test-prov") is not None
        pname, mname = m2.parse_model_ref("test-prov/m2")
        assert pname == "test-prov" and mname == "m2"
        # 含斜杠的模型名
        m2.add_provider("qwen-prov", models=["qwen/qwen3.8-max:free"])
        pn, mn = m2.parse_model_ref("qwen-prov/qwen/qwen3.8-max:free")
        assert pn == "qwen-prov" and mn == "qwen/qwen3.8-max:free", (pn, mn)
        return True

    _mgr()

    @check("模型能力推断")
    def _catalog():
        from fengcode.config import catalog

        info = catalog.lookup("gpt-4o")
        assert info.get("context_window") == 128_000
        assert catalog.lookup("openai/gpt-5").get("vision") is True
        # 预置里现在只有万象 API 与自定义入口
        pre = catalog.presets()
        assert "wanxiang" in pre and pre["wanxiang"]["base_url"].startswith("http")
        return True

    _catalog()

    print("\n[3] LLM 适配层")

    @check("消息与工具类型")
    def _types():
        from fengcode.llm.types import Message, ToolCall, ToolSpec

        tc = ToolCall.parse_arguments('```json\n{"a": 1, "b": [1,2,]}\n```')
        assert tc.get("a") == 1, tc
        assert ToolCall.parse_arguments('{"x": "y"}')["x"] == "y"
        spec = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}})
        assert spec.to_openai()["function"]["name"] == "t"
        assert spec.to_anthropic()["input_schema"]["type"] == "object"
        assert spec.to_gemini()["parameters"]["type"] == "OBJECT"
        m = Message.user("hi").to_dict()
        assert Message.from_dict(m).content == "hi"
        return True

    _types()

    @check("三家客户端可构造且 payload 正确")
    def _clients():
        from fengcode.config.schema import Provider
        from fengcode.llm import AnthropicClient, GeminiClient, OpenAIClient
        from fengcode.llm.types import Message, ToolSpec

        tools = [ToolSpec(name="echo", description="e", parameters={"type": "object", "properties": {}})]
        msgs = [Message.system("sys"), Message.user("hi")]

        p = Provider(name="o", kind="openai", base_url="https://api.example.com/v1", models=["m"])
        url, payload, hdr = OpenAIClient(p, "k").build_payload(msgs, model="m", tools=tools)
        assert url.endswith("/chat/completions"), url
        assert payload["tools"][0]["function"]["name"] == "echo"
        assert hdr["Authorization"] == "Bearer k"

        p2 = Provider(name="a", kind="anthropic", base_url="https://api.anthropic.com", models=["m"])
        url2, payload2, hdr2 = AnthropicClient(p2, "k").build_payload(msgs, model="m", tools=tools)
        assert url2.endswith("/v1/messages")
        assert payload2["system"], "system 应提到顶层"
        assert payload2["tools"][0]["name"] == "echo"
        assert hdr2["x-api-key"] == "k"

        p3 = Provider(name="g", kind="gemini", base_url="https://generativelanguage.googleapis.com/v1beta", models=["m"])
        url3, payload3, _ = GeminiClient(p3, "k").build_payload(msgs, model="m", tools=tools)
        assert "generateContent" in url3
        assert payload3["systemInstruction"]["parts"][0]["text"] == "sys"
        return True

    _clients()

    @check("成本计算")
    def _cost():
        from fengcode.llm import compute_cost
        from fengcode.llm.types import Usage

        u = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
        amount, cur = compute_cost(u, {"input": 3, "output": 15, "currency": "$"})
        assert abs(amount - 18.0) < 0.001, amount
        assert cur == "$"
        return True

    _cost()

    print("\n[4] 存储层")

    @check("数据库建表与基本读写")
    def _db():
        from fengcode.storage import get_db

        db = get_db()
        st = db.stats()
        assert "sessions" in st and "memories" in st
        assert st["fts"] in (True, False)
        return True

    _db()

    @check("会话与消息")
    def _sess():
        from fengcode.llm.types import Message
        from fengcode.storage import SessionStore

        ss = SessionStore()
        s = ss.create(title="测试会话")
        ss.append(s["id"], Message.user("你好，这是第一条消息"))
        ss.append(s["id"], Message.assistant("收到，这是回复"))
        msgs = ss.messages(s["id"])
        assert len(msgs) == 2, len(msgs)
        assert msgs[0].content.startswith("你好")
        # auto_title 只在标题还是默认值时生效
        assert ss.get(s["id"])["title"] == "测试会话"
        s2 = ss.create(title="新对话")
        ss.append(s2["id"], Message.user("帮我写一个排序算法并测试"))
        ss.auto_title(s2["id"], "帮我写一个排序算法并测试")
        assert ss.get(s2["id"])["title"] == "帮我写一个排序算法并测试"
        md = ss.export_markdown(s2["id"])
        assert "排序算法" in md
        return True

    _sess()

    @check("用量与审计")
    def _stats():
        from fengcode.llm.types import Usage
        from fengcode.storage import AuditStore, StatsStore

        st = StatsStore()
        st.record(provider="p", model="m", usage=Usage(100, 50, 150), cost=0.01, currency="¥")
        s = st.summary()
        assert s["calls"] >= 1
        assert s["total_tokens"] >= 150
        a = AuditStore()
        a.log(action="shell", target="ls", decision="allow")
        assert a.count() >= 1
        return True

    _stats()

    @check("任务/目标/键值/工作流")
    def _tasks():
        from fengcode.storage.tasks import KVStore, TaskStore, WorkflowStore

        ts = TaskStore()
        t1 = ts.add("第一步：摸清现状", session_id="sess-x")
        t2 = ts.add("第二步：实现", session_id="sess-x")
        ts.set_status(t1["id"], "completed")
        assert len(ts.list(session_id="sess-x")) == 2
        assert ts.summary("sess-x")["completed"] == 1
        assert "第一步" in ts.render("sess-x")
        g = ts.set_goal("做出一个能跑的 demo", session_id="sess-x", max_rounds=5)
        ts.bump_round(g["id"])
        assert ts.get_goal(g["id"])["rounds"] == 1
        kv = KVStore()
        kv.set("theme", {"mode": "light"})
        assert kv.get("theme")["mode"] == "light"
        wf = WorkflowStore()
        w = wf.save("流程A", {"steps": [{"id": "a"}]})
        assert wf.get(w["id"])["dag"]["steps"][0]["id"] == "a"
        return True

    _tasks()

    print("\n[5] 记忆系统")

    @check("本地哈希向量")
    def _embed():
        from fengcode.memory.embedding import cosine, hashing_embed

        a = hashing_embed("用户喜欢用 Python 写后端")
        b = hashing_embed("用户偏好 Python 后端开发")
        c = hashing_embed("今天天气不错适合出门散步")
        assert len(a) == 512
        assert abs(sum(x * x for x in a) - 1.0) < 1e-6, "未归一化"
        assert cosine(a, b) > cosine(a, c), (cosine(a, b), cosine(a, c))
        return True

    _embed()

    async def _mem_async():
        from fengcode.memory.manager import MemoryManager

        m = MemoryManager()
        await m.remember(
            "用户偏好中文界面，代码里用中文注释",
            title="语言偏好", kind="preference", tags=["偏好", "语言"],
            importance=0.9, pinned=True,
        )
        await m.remember(
            "项目构建用 Gradle 8.14.3，JDK 21",
            title="构建环境", kind="fact", tags=["构建"],
        )
        hits = await m.recall("构建环境是什么", top_k=3)
        assert hits, "召回为空"
        assert any("Gradle" in h.content for h in hits), [h.title for h in hits]
        # 去重
        await m.remember("用户偏好中文界面，代码里用中文注释", title="语言偏好", kind="preference")
        assert m.stats()["total"] == 2, m.stats()
        # 画像
        await m.profile_set("沟通风格", "喜欢直接给结论")
        prof = m.profile_get()
        assert "沟通风格" in prof
        stats = m.stats()
        assert stats["total"] >= 3
        return True

    check("记忆写入/召回/去重/画像")(lambda: asyncio.run(_mem_async()))()

    @check("摘要与压缩判定")
    def _summ():
        from fengcode.llm.types import Message
        from fengcode.memory.summarizer import (
            extract_facts,
            fallback_summary,
            should_compact,
            split_for_compaction,
        )

        # 构造足够长的中文消息，使 token 数超过小窗口阈值
        long_text = "这是一段用于测试上下文压缩的中文文本。" * 20
        msgs = [Message.user(f"{long_text} 第{i}条") for i in range(20)]
        need, tokens = should_compact(msgs, context_window=2000, reserve_output=0)
        assert need is True, f"token={tokens}"
        assert tokens > 0
        sys_msgs, mid, recent = split_for_compaction(msgs, keep_recent=5, keep_first=1)
        # 开头段（锚点）+ 待压缩段 + 最近保留段 = 全部消息
        assert len(recent) == 5, len(recent)
        assert len(sys_msgs) >= 1, "应保留开头锚点"
        assert len(sys_msgs) + len(mid) + len(recent) == len(msgs), \
            (len(sys_msgs), len(mid), len(recent))
        assert "自动摘要" in fallback_summary(msgs)
        facts = extract_facts("请记住：我的项目路径是 D:\\Fengcode。以后都用中文回答。")
        assert facts, "未抽取到事实"
        return True

    _summ()

    print("\n[6] 安全层")

    @check("路径守卫")
    def _guard():
        from fengcode.security.paths import PathGuard, PathNotAllowed, scan_dangerous

        ws = Path(os.environ["FENGCODE_HOME"]) / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        g = PathGuard(workspace=ws, write_paths=[str(ws)], deny_patterns=[])
        p = g.check_write("a/b.txt")
        # 写入路径必须落在白名单目录内（两边的长名/短名差异不应影响判定）
        from fengcode.security.paths import _norm_for_match

        assert _norm_for_match(p).startswith(_norm_for_match(ws)), (p, ws)
        # 越界路径应被拒绝
        try:
            g.check_write(str(Path(os.environ["FENGCODE_HOME"]) / "outside.txt"))
            raise AssertionError("应拒绝白名单之外的路径")
        except PathNotAllowed:
            pass
        try:
            g.check_read("C:/Windows/System32/config/SAM")
            raise AssertionError("应该拒绝系统路径")
        except PathNotAllowed:
            pass
        hits = scan_dangerous("rm -rf / --no-preserve-root")
        assert hits, "未识别危险命令"
        assert scan_dangerous("ls -la") == []
        return True

    _guard()

    @check("审批门判定")
    def _gate():
        import asyncio as aio

        from fengcode.config.schema import PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(mode="ask"))
        ok, reason, req = gate.evaluate("read_file", path="workspace/x.txt")
        assert ok and req is None
        ok2, reason2, req2 = gate.evaluate("shell", command="rm -rf /", dangerous=True)
        assert not ok2 and req2 is not None and req2.risk == "high"
        ok3, _, req3 = gate.evaluate("shell", command="ls -la", dangerous=True)
        assert ok3, f"普通命令不该被拦：{req3}"

        # 异步审批流程
        async def flow():
            g2 = ApprovalGate(PermissionsConfig(mode="ask"))
            # 没有响应者时应立即拒绝，而不是空等超时
            _, _, r0 = g2.evaluate("shell", command="rm -rf /", dangerous=True)
            d0 = await g2.request(r0)
            assert not d0.allowed and d0.by == "no-responder", d0

            # 注册响应者后走正常批准流程
            g2.set_responder(object())
            _, _, r = g2.evaluate("shell", command="rm -rf /", dangerous=True)
            task = aio.create_task(g2.request(r))
            await aio.sleep(0.05)
            g2.resolve_with_key(r.id, True, action="shell", target="rm -rf /", remember="session")
            dec = await task
            assert dec.allowed, dec
            ok4, reason4, _ = g2.evaluate("shell", command="rm -rf /", dangerous=True)
            assert ok4, "会话级允许未生效"

        aio.run(flow())
        return True

    _gate()

    @check("沙箱执行")
    def _sandbox():
        from fengcode.security.sandbox import LocalSandbox

        async def go():
            sb = LocalSandbox(cwd=Path(os.environ["FENGCODE_HOME"]) / "ws")
            r = await sb.python("print(1+1)")
            assert r.ok, r.summary()
            assert "2" in r.stdout
            r2 = await sb.python("import sys; print('中文输出正常'); sys.exit(3)")
            assert "中文输出正常" in r2.stdout
            assert r2.returncode == 3
            r3 = await sb.python("import time; time.sleep(30)", timeout=1)
            assert r3.timed_out, "超时未生效"
            # 环境变量隔离
            os.environ["FAKE_API_KEY"] = "secret123"
            r4 = await sb.python("import os; print(os.environ.get('FAKE_API_KEY', 'GONE'))")
            assert "GONE" in r4.stdout, "密钥变量泄漏到子进程"

        asyncio.run(go())
        return True

    _sandbox()

    print("\n[7] 工具系统")

    @check("内置工具全部注册")
    def _reg():
        from fengcode.tools import register_builtin
        from fengcode.tools.base import reset_registry

        reset_registry()
        res = register_builtin()
        assert not res["errors"], res["errors"]
        assert len(res["registered"]) >= 35, f"只注册了 {len(res['registered'])} 个工具"
        return True

    _reg()

    @check("工具 schema 生成完整")
    def _specs():
        from fengcode.tools.base import get_registry

        reg = get_registry()
        specs = reg.specs()
        names = {s.name for s in specs}
        for required in ("read_file", "write_file", "edit_file", "grep", "glob", "shell",
                         "python_exec", "web_search", "web_fetch", "memory", "todo", "subagent",
                         "skill", "ssh", "screenshot"):
            assert required in names, f"缺少工具 {required}"
        for s in specs:
            assert s.description, f"{s.name} 没有描述"
            assert s.parameters.get("type") == "object", f"{s.name} schema 不合法"
            assert len(s.description) < 4096, f"{s.name} 描述过长"
        return True

    _specs()

    @check("工具真实执行（文件读写/grep）")
    def _exec():
        import threading

        from fengcode.security.paths import PathGuard
        from fengcode.security.sandbox import LocalSandbox
        from fengcode.tools.base import ToolContext, get_registry

        box = {}

        async def go():
            ws = Path(os.environ["FENGCODE_HOME"]) / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            from fengcode.config import get_config

            cfg = get_config()
            ctx = ToolContext(
                workspace=ws,
                config=cfg,
                guard=PathGuard(workspace=ws, write_paths=[str(ws)], deny_patterns=[]),
                sandbox=LocalSandbox(cwd=ws),
            )
            reg = get_registry()
            r = await reg.execute(
                "write_file",
                {"path": "hello.txt", "content": "第一行\n第二行 关键词\n"},
                ctx, skip_approval=True,
            )
            assert r.ok, r.error
            assert (ws / "hello.txt").is_file()

            r2 = await reg.execute("read_file", {"path": "hello.txt"}, ctx, skip_approval=True)
            assert r2.ok and "关键词" in r2.content

            r3 = await reg.execute(
                "edit_file",
                {"path": "hello.txt", "old_string": "第二行", "new_string": "第二行（已改）"},
                ctx, skip_approval=True,
            )
            assert r3.ok, r3.error
            assert "（已改）" in (ws / "hello.txt").read_text(encoding="utf-8")

            r4 = await reg.execute("grep", {"query": "关键词", "path": "."}, ctx, skip_approval=True)
            assert r4.ok and "hello.txt" in r4.content

            r5 = await reg.execute("glob", {"pattern": "*.txt"}, ctx, skip_approval=True)
            assert r5.ok and "hello.txt" in r5.content

            r6 = await reg.execute("list_dir", {"path": "."}, ctx, skip_approval=True)
            assert r6.ok, r6.error

            r7 = await reg.execute("shell", {"command": "echo 你好"}, ctx, skip_approval=True)
            assert r7.ok, r7.error

            r8 = await reg.execute(
                "python_exec", {"code": "print(sum(range(10)))"}, ctx, skip_approval=True
            )
            assert r8.ok and "45" in r8.content

            r9 = await reg.execute(
                "todo", {"action": "add", "title": "测试任务"}, ctx, skip_approval=True
            )
            assert r9.ok, r9.error

            r10 = await reg.execute(
                "memory",
                {"action": "remember", "content": "冒烟测试写入的记忆", "title": "冒烟测试"},
                ctx, skip_approval=True,
            )
            assert r10.ok, r10.error

            r11 = await reg.execute("system_info", {"detail": "basic"}, ctx, skip_approval=True)
            assert r11.ok and "Python" in r11.content

            r12 = await reg.execute("不存在的工具", {}, ctx, skip_approval=True)
            assert not r12.ok, "未知工具应报错"

            # edit_file 唯一性保护
            await reg.execute("write_file", {"path": "dup.txt", "content": "重复\n重复\n"}, ctx,
                              skip_approval=True)
            r13 = await reg.execute(
                "edit_file", {"path": "dup.txt", "old_string": "重复", "new_string": "改"}, ctx,
                skip_approval=True,
            )
            assert not r13.ok and "次" in (r13.error or ""), "未阻止歧义替换"

            # 路径越界保护
            r14 = await reg.execute("write_file", {"path": "../../escape.txt", "content": "x"},
                                    ctx, skip_approval=True)
            assert not r14.ok, "越界写入未被拦截"

            box["ok"] = True

        def runner():
            try:
                asyncio.run(go())
            except BaseException as e:
                box["err"] = f"{type(e).__name__}: {e}"
                box["trace"] = traceback.format_exc()

        t = threading.Thread(target=runner)
        t.start()
        t.join(timeout=240)
        if t.is_alive():
            raise TimeoutError("工具执行测试超时")
        if box.get("err"):
            raise AssertionError(f"线程内失败：{box['err']}\n{box.get('trace', '')}")
        assert box.get("ok"), "工具执行测试未完成"
        return True

    _exec()

    print("\n[8] 技能与插件")

    @check("技能发现与加载")
    def _skills():
        from fengcode.config import get_config
        from fengcode.skills import SkillManager

        sm = SkillManager(config=get_config())
        items = sm.list_all()
        assert len(items) >= 9, f"只发现 {len(items)} 个技能"
        names = [i["name"] for i in items]
        assert "代码改动规范" in names, names
        # 默认不导入其它 Agent 的私人技能：技能来源只可能是本应用自己的三类
        assert all(i.get("source") in ("builtin", "user", "plugin") for i in items), \
            "出现了非本应用来源的技能"
        loaded = sm.load("代码改动规范")
        assert loaded and "最小改动" in loaded["body"]
        hits = sm.search("写代码 修改 bug")
        assert hits, "搜索无结果"
        block = sm.catalog_block("改代码")
        assert "<skills>" in block
        return True

    _skills()

    @check("SKILL.md frontmatter 解析")
    def _fm():
        from fengcode.skills import parse_frontmatter

        meta, body = parse_frontmatter("---\nname: 测试\ntags: [a, b]\nver: 1\n---\n正文内容")
        assert meta["name"] == "测试"
        assert meta["tags"] == ["a", "b"]
        assert body.strip() == "正文内容"
        meta2, body2 = parse_frontmatter("没有 frontmatter 的纯正文")
        assert meta2 == {} and body2 == "没有 frontmatter 的纯正文"
        return True

    _fm()

    print("\n[9] 调度与工作流")

    @check("cron 解析与匹配")
    def _cron():
        import time as _t

        from fengcode.scheduler.cron import describe_schedule, next_run_time, parse_cron

        c = parse_cron("0 3 * * *")
        assert c is not None
        c2 = parse_cron("*/15 * * * *")
        assert c2 is not None
        assert parse_cron("bad expr") is None
        nxt = next_run_time(c, _t.time())
        assert nxt > _t.time()
        import datetime

        dt = datetime.datetime.fromtimestamp(nxt)
        assert dt.hour == 3 and dt.minute == 0, dt
        assert "每天" in describe_schedule({"cron": "0 3 * * *"})
        assert "每" in describe_schedule({"interval_seconds": 3600})
        return True

    _cron()

    @check("工作流 DAG 校验与拓扑分层")
    def _wf():
        from fengcode.workflow.engine import (
            Step,
            from_dict,
            render_template,
            sample_dag,
            topo_layers,
            validate,
        )

        steps = from_dict(sample_dag())
        ok, err = validate(steps)
        assert ok, err
        layers = topo_layers(steps)
        assert len(layers) >= 3, len(layers)
        # 环检测
        bad = [Step(id="a", depends_on=["b"]), Step(id="b", depends_on=["a"])]
        ok2, err2 = validate(bad)
        assert not ok2 and "循环" in err2
        assert render_template("引用 {{x}} 结束", {"x": "值"}) == "引用 值 结束"
        return True

    _wf()

    print("\n[10] 事件总线")

    @check("事件发布与订阅")
    def _bus():
        from fengcode.events import EventBus

        bus = EventBus()
        seen = []
        bus.on_event(lambda e: seen.append(e.type))
        q = bus.subscribe()
        bus.emit("test.one", {"a": 1}, session_id="s1")
        bus.emit("test.two", {"b": 2}, session_id="s2")
        assert len(seen) == 2
        assert q.qsize() == 2
        hist = bus.history(session_id="s1")
        assert len(hist) == 1 and hist[0]["type"] == "test.one"
        return True

    _bus()

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败明细：")
        for name, err in FAIL:
            print(f"  ✗ {name}\n      {err}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
