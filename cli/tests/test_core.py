"""单元测试：配置、安全、工具、记忆、工作流、调度。

运行：python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 用临时数据目录，避免污染开发环境
_TMP = tempfile.mkdtemp(prefix="fengcode-test-")
os.environ["FENGCODE_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ==========================================================================
# 配置
# ==========================================================================

class TestConfig:
    def test_default_config_has_no_secrets(self):
        """默认配置不能预置任何密钥。"""
        from fengcode.config import default_config

        cfg = default_config()
        assert cfg.providers, "应有预置的供应商空壳"
        for p in cfg.providers:
            assert not p.api_key, f"{p.name} 不应预置密钥"

    def test_theme_defaults_to_light(self):
        from fengcode.config import default_config

        assert default_config().ui.theme == "light"

    def test_skills_do_not_import_others_by_default(self):
        """默认不继承其它程序的私人技能库（Fengcode 是独立应用）。"""
        from fengcode.config import default_config

        cfg = default_config()
        # 技能目录默认只指向本应用自己的数据目录，不残留任何外部程序的路径字段
        assert cfg.skills.paths == [] or all(
            "fengcode" in (p or "").lower() or "$" in (p or "") for p in cfg.skills.paths
        )
        # 配置里不应出现指向其它程序的导入开关
        for bad in ("import_other_agent", "foreign_skill_paths", "import_from_other_agent"):
            assert not hasattr(cfg.skills, bad), bad

    def test_model_ref_parsing(self):
        from fengcode.config import get_manager

        m = get_manager()
        m.add_provider("tp1", base_url="https://x/v1", models=["m1", "m2"])
        p, model = m.parse_model_ref("tp1/m2")
        assert (p, model) == ("tp1", "m2")

    def test_model_ref_with_slash_in_name(self):
        """模型名本身含斜杠时要正确拆分。"""
        from fengcode.config import get_manager

        m = get_manager()
        m.add_provider("tp2", models=["qwen/qwen3-max:free"])
        p, model = m.parse_model_ref("tp2/qwen/qwen3-max:free")
        assert p == "tp2"
        assert model == "qwen/qwen3-max:free"

    def test_thinking_enabled_string_coerced(self):
        """thinking='enabled' 这种字符串写法要能解析成布尔。"""
        from fengcode.config.schema import Provider

        assert Provider(name="x", thinking="enabled").thinking is True
        assert Provider(name="x", thinking="disabled").thinking is False


class TestCatalog:
    def test_lookup_known_model(self):
        from fengcode.config import catalog

        info = catalog.lookup("gpt-4o")
        assert info["context_window"] == 128_000
        assert info["vision"] is True

    def test_lookup_with_prefix(self):
        from fengcode.config import catalog

        assert catalog.lookup("openai/gpt-5")["vision"] is True

    def test_lookup_unknown_returns_empty(self):
        from fengcode.config import catalog

        assert catalog.lookup("完全不存在的模型xyz") == {}

    def test_presets_present(self):
        """只预置万象 API 一个可开箱即用的入口，其余由用户自己填。"""
        from fengcode.config import catalog

        pre = catalog.presets()
        # 万象 API 必须预置且带地址（填个 key 就能用）
        assert "wanxiang" in pre
        assert pre["wanxiang"]["base_url"].startswith("http")
        # 保留一个自定义入口（地址留空，让用户填）
        assert "custom" in pre
        # 预置的是「厂商官方」，都必须带 base_url（填个 key 就能用）
        for pid in ("deepseek", "zhipu", "mimo", "dashscope", "moonshot", "doubao", "minimax"):
            assert pid in pre, f"应预置官方厂商 {pid}"
            assert pre[pid]["base_url"].startswith("http"), f"{pid} 缺 base_url"
        # 用户指定顺序：万象 → DeepSeek → 智谱 → MiMo
        order = [k for k in pre if k != "custom"]
        assert order[:4] == ["wanxiang", "deepseek", "zhipu", "mimo"], f"预设顺序不对：{order[:4]}"
        # 不再预置「本地部署」或「第三方聚合」（删掉）
        for gone in ("ollama", "lmstudio", "vllm", "openrouter", "groq", "siliconflow", "nvidia"):
            assert gone not in pre, f"不该再预置 {gone}"

    # ---- 上下文窗口来源标注（v1.0.6 修：逐模型覆盖漏判成 fallback）----
    def test_context_window_source_model_override(self):
        """★ 用户在模型服务页给单个模型填了窗口 → 来源必须是 model，不能是 fallback。

        回归现场：填完窗口界面仍显示「未限制」，因为来源判定漏了
        「逐模型覆盖」这一支，把显式设置的值当成了出厂兜底值。
        """
        from fengcode.config.manager import ConfigManager
        from fengcode.config.schema import ModelOverride, Provider

        mgr = ConfigManager.__new__(ConfigManager)   # 只测纯函数，不走单例/磁盘
        prov = Provider(
            name="p", base_url="http://x", models=["不存在的模型abc"],
            model_overrides={"不存在的模型abc": ModelOverride(context_window=1_048_576)},
        )
        info = mgr.model_info("不存在的模型abc", prov)
        assert info["context_window"] == 1_048_576
        assert info["context_window_source"] == "model"

    def test_context_window_source_default_when_unknown(self):
        """没人声明过窗口 → 用默认 1M，来源标 default（不再是「未限制」）。

        ★ 契约变更：旧行为是「无人声明 → fallback → 界面显示未限制」，
        新行为是「无人声明 → 默认 1M 并照实显示」。理由：主流模型普遍 1M 起步，
        长期显示「未限制」会看起来像没有限额，且换模型时读数看着没变化。
        用户在模型服务页逐模型填了值仍以他填的为准（见 model_override 那条用例）。
        """
        from fengcode.config.manager import ConfigManager
        from fengcode.config.schema import Provider

        mgr = ConfigManager.__new__(ConfigManager)
        prov = Provider(name="p", base_url="http://x", models=["不存在的模型abc"])
        info = mgr.model_info("不存在的模型abc", prov)
        assert info["context_window_source"] == "default"
        assert info["context_window"] == 1_048_576

    def test_context_window_source_catalog(self):
        """内置能力表收录的模型 → 来源是 catalog，用表里的真实值。"""
        from fengcode.config.manager import ConfigManager
        from fengcode.config.schema import Provider

        mgr = ConfigManager.__new__(ConfigManager)
        info = mgr.model_info("gpt-4o", Provider(name="p", base_url="http://x"))
        assert info["context_window_source"] == "catalog"
        assert info["context_window"] == 128_000

    def test_stats_tool_stats_does_not_crash(self):
        """★ 回归：审计表工具统计曾拼出 `... AND WHERE ts>=?` 语法错被静默吞掉。

        现在统一由 _conds/_where 拼装，带时间条件也不该抛异常、应正常返回列表。
        """
        from fengcode.storage.stats import StatsStore

        st = StatsStore.__new__(StatsStore)
        # 直接验证条件拼装：时间条件不能被再次拼成 "WHERE"
        conds, params = StatsStore._conds(123.0)
        assert conds == ["ts>=?"] and params == [123.0]
        where, _ = StatsStore._where(123.0, "sid")
        assert where == "WHERE ts>=? AND session_id=?"
        assert "WHERE WHERE" not in where and "AND WHERE" not in where

    def test_default_config_only_wanxiang(self):
        """默认配置里只应该有一个供应商，且没有密钥。"""
        from fengcode.config import default_config

        cfg = default_config()
        assert len(cfg.providers) == 1
        p = cfg.providers[0]
        assert p.name == "wanxiang"
        assert p.models == []          # 不预置模型名，让用户点「获取模型」
        assert not p.api_key           # 不带任何密钥
        assert p.enabled is False      # 填了密钥再启用


# ==========================================================================
# TOML
# ==========================================================================

class TestToml:
    def test_roundtrip_with_chinese(self):
        from fengcode.utils import toml as T

        data = {"名字": "中文值", "n": {"x": 1, "list": [1, 2, 3]}}
        p = Path(_TMP) / "t.toml"
        T.dumpf(p, data)
        back = T.loadf(p)
        assert back["名字"] == "中文值"
        assert back["n"]["list"] == [1, 2, 3]

    def test_dumps_handles_tables_array(self):
        from fengcode.utils import toml as T

        text = T.dumps({"items": [{"a": 1}, {"a": 2}]})
        assert text.count("[[items]]") == 2


# ==========================================================================
# 安全
# ==========================================================================

class TestSecurity:
    def test_dangerous_commands_detected(self):
        from fengcode.security.paths import scan_dangerous

        for cmd in ("rm -rf /", "mkfs.ext4 /dev/sda", ":(){ :|:& };:",
                    "Format C:", "shutdown -h now"):
            assert scan_dangerous(cmd), f"未识别：{cmd}"

    def test_safe_commands_pass(self):
        from fengcode.security.paths import scan_dangerous

        for cmd in ("ls -la", "git status", "python main.py", "echo hello"):
            assert scan_dangerous(cmd) == [], f"误报：{cmd}"

    def test_path_guard_rejects_system_dirs(self):
        from fengcode.security.paths import PathGuard, PathNotAllowed

        g = PathGuard(workspace=_TMP, write_paths=[_TMP])
        with pytest.raises(PathNotAllowed):
            g.check_read("C:/Windows/System32/config/SAM")

    def test_path_guard_allows_workspace(self):
        from fengcode.security.paths import PathGuard, _norm_for_match

        g = PathGuard(workspace=_TMP, write_paths=[_TMP])
        p = g.check_write("sub/file.txt")
        # Windows 上 _TMP 可能是 8.3 短名、解析结果是长名，两边都归一化再比
        assert _norm_for_match(p).startswith(_norm_for_match(Path(_TMP)))

    def test_approval_gate_allows_normal(self):
        from fengcode.config.schema import PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(mode="ask"))
        ok, _, req = gate.evaluate("read_file", path="a.txt")
        assert ok and req is None

    def test_approval_gate_blocks_dangerous(self):
        from fengcode.config.schema import PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(mode="ask"))
        ok, _, req = gate.evaluate("shell", command="rm -rf /", dangerous=True)
        assert not ok
        assert req is not None and req.risk == "high"

    def test_readonly_mode_denies_write(self):
        from fengcode.config.schema import PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(mode="deny"))
        ok, _, _ = gate.evaluate("write_file", path="x.txt", dangerous=True)
        assert not ok

    # ---- 命令安全分类（参数感知）----
    def test_classify_command_three_tiers(self):
        """同一个程序、不同子命令要判成不同档位（参数感知的关键用例）。"""
        from fengcode.security.paths import classify_command

        # 只读
        for cmd in ("git status", "git log -5", "git stash list", "ls -la", "dir",
                    "npm list", "pip show requests", "cat a.txt"):
            assert classify_command(cmd) == "read", f"应判为只读：{cmd}"
        # 改仓库状态（会丢改动，必须每次问）
        for cmd in ("git restore .", "git reset --hard", "git clean -fd",
                    "git checkout main", "git stash drop", "git rebase main"):
            assert classify_command(cmd) == "repo", f"应判为改仓库状态：{cmd}"
        # 改文件
        for cmd in ("npm install x", "rm -f tmp.txt", "mkdir newdir", "mv a b"):
            assert classify_command(cmd) == "write", f"应判为改文件：{cmd}"

    def test_readonly_command_passes_gate(self):
        """只读命令不该被审批门拦住（实测抱怨「查状态也要批准」）。"""
        from fengcode.config.schema import PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(mode="ask"))
        ok, _, req = gate.evaluate("shell", command="git status")
        assert ok and req is None

    def test_repo_command_always_asks_even_when_remembered(self):
        """改仓库状态的命令即使被「始终允许」记住，也必须每次都问。

        回归现场：git restore 会丢弃未提交改动、不可逆；旧实现把它记进
        「本会话/始终允许」后，后续同类操作就再也不提示了。
        """
        from fengcode.config.schema import PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(mode="ask"))
        ok, _, req = gate.evaluate("shell", command="git restore .", session_id="s1")
        assert not ok and req is not None
        # 模拟用户点了「始终允许」
        gate.resolve_with_key(req.id, True, action="shell", target="git restore .",
                              session_id="s1", remember="always")
        ok2, _, req2 = gate.evaluate("shell", command="git restore .", session_id="s1")
        assert not ok2, "改仓库状态的命令不应复用「始终允许」"
        assert req2 is not None

    def test_write_command_can_be_remembered(self):
        """改文件的命令仍可被「始终允许」记住 —— 不能把所有操作都变成每次批准。"""
        from fengcode.config.schema import PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(mode="ask"))
        # 先让它必须问：用一个会命中黑名单的写命令
        ok, _, req = gate.evaluate("shell", command="rm -rf build", session_id="s2")
        assert not ok and req is not None
        gate.resolve_with_key(req.id, True, action="shell", target="rm -rf build",
                              session_id="s2", remember="always")
        ok2, _, _ = gate.evaluate("shell", command="rm -rf build", session_id="s2")
        assert ok2, "普通写操作应能被「始终允许」记住"

    # ---- 分层网络诊断 ----
    def test_diagnose_error_layers(self):
        """断线要能分出是哪一层断的（DNS / 拒连 / 超时 / 半途断）。

        回归现场：界面只显示「网络错误：ConnectError」，用户分不清是本机网络、
        上游网关还是模型服务的问题，只能干等超时。
        """
        import httpx

        from fengcode.llm.base import diagnose_error

        cases = [
            (httpx.ConnectError("getaddrinfo failed for api.example.com"), 0, "dns"),
            (httpx.ConnectError("[WinError 10061] Connection refused"), 0, "refused"),
            # ★ 这条是关键回归：ConnectTimeout 同时是 TimeoutException 与
            #   TransportError 的子类，早先被误判成 midway（连接被掐）。
            (httpx.ConnectTimeout("connect timed out"), 0, "connect"),
            (httpx.ReadTimeout("read timed out"), 0, "connect"),
            (httpx.RemoteProtocolError("peer closed connection"), 5, "midway"),
            (httpx.RemoteProtocolError("server disconnected"), 0, "midway"),
        ]
        for exc, got, want in cases:
            layer, hint = diagnose_error(exc, got_events=got)
            assert layer == want, f"{type(exc).__name__}(got={got}) 应为 {want}，实际 {layer}"
            assert hint and len(hint) > 6, "每层都要给人话提示"

    def test_diagnose_midway_mentions_partial_kept(self):
        """半途断开要明确告知「已收到的内容不会丢」——这是用户最担心的。"""
        import httpx

        from fengcode.llm.base import diagnose_error

        _layer, hint = diagnose_error(httpx.RemoteProtocolError("peer closed"), got_events=3)
        assert "不会丢" in hint or "重试" in hint


class TestSandbox:
    def test_python_exec(self):
        import asyncio

        from fengcode.security.sandbox import LocalSandbox

        async def go():
            sb = LocalSandbox(cwd=_TMP)
            r = await sb.python("print(2+3)")
            assert r.ok and "5" in r.stdout

        asyncio.run(go())

    def test_timeout_kills(self):
        import asyncio

        from fengcode.security.sandbox import LocalSandbox

        async def go():
            sb = LocalSandbox(cwd=_TMP)
            r = await sb.python("import time; time.sleep(30)", timeout=1)
            assert r.timed_out

        asyncio.run(go())

    def test_secrets_not_leaked_to_child(self):
        import asyncio

        from fengcode.security.sandbox import LocalSandbox

        os.environ["TEST_SECRET_KEY"] = "should-not-leak"

        async def go():
            sb = LocalSandbox(cwd=_TMP)
            r = await sb.python("import os;print(os.environ.get('TEST_SECRET_KEY','GONE'))")
            assert "GONE" in r.stdout

        asyncio.run(go())


# ==========================================================================
# 记忆
# ==========================================================================

class TestMemory:
    def test_hashing_embed_is_normalized(self):
        from fengcode.memory.embedding import hashing_embed

        v = hashing_embed("测试文本")
        assert len(v) == 512
        assert abs(sum(x * x for x in v) - 1.0) < 1e-6

    def test_similar_texts_more_similar(self):
        from fengcode.memory.embedding import cosine, hashing_embed

        a = hashing_embed("用户喜欢用 Python 写后端")
        b = hashing_embed("用户偏好 Python 后端开发")
        c = hashing_embed("今天天气不错适合出门散步")
        assert cosine(a, b) > cosine(a, c)

    def test_remember_and_recall(self):
        import asyncio

        from fengcode.memory.manager import MemoryManager

        async def go():
            m = MemoryManager()
            await m.remember("项目构建使用 Gradle 8.14.3", title="构建环境",
                             kind="fact", tags=["构建"])
            hits = await m.recall("构建工具是什么", top_k=3)
            assert hits
            assert any("Gradle" in h.content for h in hits)

        asyncio.run(go())

    def test_dedupe_by_content(self):
        import asyncio

        from fengcode.storage.db import Database
        from fengcode.memory.manager import MemoryManager

        async def go():
            # 用独立数据库，避免与其它测试互相干扰
            db = Database(Path(_TMP) / "dedupe-test.db")
            m = MemoryManager(db=db)
            await m.remember("同一条内容测试", title="A", kind="fact")
            await m.remember("同一条内容测试", title="A", kind="fact")
            assert m.stats()["total"] == 1
            # 不同 kind 应视为不同记忆
            await m.remember("同一条内容测试", title="A", kind="decision")
            assert m.stats()["total"] == 2
            db.close()

        asyncio.run(go())


# ==========================================================================
# 工作流
# ==========================================================================

class TestWorkflow:
    def test_validate_ok(self):
        from fengcode.workflow.engine import from_dict, validate

        steps = from_dict({"steps": [
            {"id": "a"},
            {"id": "b", "depends_on": ["a"]},
        ]})
        ok, err = validate(steps)
        assert ok, err

    def test_validate_detects_cycle(self):
        from fengcode.workflow.engine import Step, validate

        ok, err = validate([Step(id="a", depends_on=["b"]), Step(id="b", depends_on=["a"])])
        assert not ok
        assert "循环" in err

    def test_validate_missing_dep(self):
        from fengcode.workflow.engine import Step, validate

        ok, err = validate([Step(id="a", depends_on=["nonexistent"])])
        assert not ok

    def test_topo_layers(self):
        from fengcode.workflow.engine import Step, topo_layers

        steps = [Step(id="a"), Step(id="b", depends_on=["a"]), Step(id="c", depends_on=["a"])]
        layers = topo_layers(steps)
        assert len(layers) == 2
        assert len(layers[1]) == 2

    def test_template_render(self):
        from fengcode.workflow.engine import render_template

        assert render_template("前 {{x}} 后", {"x": "值"}) == "前 值 后"
        assert render_template("无变量", {}) == "无变量"


# ==========================================================================
# 调度
# ==========================================================================

class TestScheduler:
    def test_parse_valid_cron(self):
        from fengcode.scheduler.cron import parse_cron

        assert parse_cron("0 3 * * *") is not None
        assert parse_cron("*/15 * * * *") is not None
        assert parse_cron("0 */2 * * 1-5") is not None

    def test_parse_invalid_cron(self):
        from fengcode.scheduler.cron import parse_cron

        assert parse_cron("坏表达式") is None
        assert parse_cron("0 3 *") is None

    def test_next_run_time(self):
        import datetime
        import time

        from fengcode.scheduler.cron import next_run_time, parse_cron

        nxt = next_run_time(parse_cron("0 3 * * *"), time.time())
        assert nxt > time.time()
        dt = datetime.datetime.fromtimestamp(nxt)
        assert dt.hour == 3 and dt.minute == 0

    def test_describe(self):
        from fengcode.scheduler.cron import describe_schedule

        assert "每天" in describe_schedule({"cron": "0 3 * * *"})
        assert "每" in describe_schedule({"interval_seconds": 3600})


# ==========================================================================
# 工具
# ==========================================================================

class TestTools:
    def test_all_builtin_registered(self):
        from fengcode.tools import register_builtin
        from fengcode.tools.base import get_registry, reset_registry

        reset_registry()
        res = register_builtin()
        assert not res["errors"], res["errors"]
        assert len(res["registered"]) >= 45
        reset_registry()

    def test_specs_have_valid_schema(self):
        from fengcode.tools import register_builtin
        from fengcode.tools.base import get_registry, reset_registry

        reset_registry()
        register_builtin()
        for spec in get_registry().specs():
            assert spec.name
            assert spec.description
            assert spec.parameters.get("type") == "object"
        reset_registry()

    def test_write_and_read_file(self):
        import asyncio

        from fengcode.security.paths import PathGuard
        from fengcode.tools import register_builtin
        from fengcode.tools.base import ToolContext, get_registry, reset_registry

        async def go():
            reset_registry()
            reg = register_builtin() and get_registry()
            ws = Path(_TMP) / "ws1"
            ws.mkdir(parents=True, exist_ok=True)
            ctx = ToolContext(workspace=ws, guard=PathGuard(workspace=ws,
                                                            write_paths=[str(ws)], deny_patterns=[]))
            r = await reg.execute("write_file", {"path": "a.txt", "content": "内容"}, ctx,
                                  skip_approval=True)
            assert r.ok, r.error
            r2 = await reg.execute("read_file", {"path": "a.txt"}, ctx, skip_approval=True)
            assert "内容" in r2.content
            reset_registry()

        asyncio.run(go())

    def test_edit_file_rejects_ambiguous(self):
        """多处匹配时必须拒绝，避免改错。"""
        import asyncio

        from fengcode.security.paths import PathGuard
        from fengcode.tools import register_builtin
        from fengcode.tools.base import ToolContext, get_registry, reset_registry

        async def go():
            reset_registry()
            reg = register_builtin() and get_registry()
            ws = Path(_TMP) / "ws2"
            ws.mkdir(parents=True, exist_ok=True)
            ctx = ToolContext(workspace=ws, guard=PathGuard(workspace=ws,
                                                            write_paths=[str(ws)], deny_patterns=[]))
            await reg.execute("write_file", {"path": "d.txt", "content": "重复\n重复\n"}, ctx,
                              skip_approval=True)
            r = await reg.execute("edit_file",
                                  {"path": "d.txt", "old_string": "重复", "new_string": "改"},
                                  ctx, skip_approval=True)
            assert not r.ok
            assert "次" in (r.error or "")
            reset_registry()

        asyncio.run(go())

    def test_crlf_file_editable_with_lf_text(self):
        """CRLF 文件用 LF 文本也应能精确匹配（并对齐换行符）。"""
        import asyncio

        from fengcode.security.paths import PathGuard
        from fengcode.tools import register_builtin
        from fengcode.tools.base import ToolContext, get_registry, reset_registry

        async def go():
            reset_registry()
            reg = register_builtin() and get_registry()
            ws = Path(_TMP) / "ws3"
            ws.mkdir(parents=True, exist_ok=True)
            f = ws / "crlf.txt"
            f.write_bytes("第一行\r\n目标行\r\n末行\r\n".encode("utf-8"))
            ctx = ToolContext(workspace=ws, guard=PathGuard(workspace=ws,
                                                            write_paths=[str(ws)], deny_patterns=[]))
            r = await reg.execute("edit_file",
                                  {"path": "crlf.txt", "old_string": "目标行\n",
                                   "new_string": "已改\n"}, ctx, skip_approval=True)
            assert r.ok, r.error
            raw = f.read_bytes()
            assert "已改".encode("utf-8") in raw
            assert raw.count(b"\r\n") == 3, "换行符被改坏了"
            reset_registry()

        asyncio.run(go())


# ==========================================================================
# 技能
# ==========================================================================

class TestSkills:
    def test_builtin_skills_found(self):
        from fengcode.config import get_config
        from fengcode.skills import SkillManager

        sm = SkillManager(config=get_config())
        items = sm.list_all()
        assert len(items) >= 9, f"只发现 {len(items)} 个"
        names = [i["name"] for i in items]
        assert "代码改动规范" in names

    def test_no_foreign_skills_by_default(self):
        """默认不导入其它 Agent 的技能。"""
        from fengcode.config import get_config
        from fengcode.skills import SkillManager

        sm = SkillManager(config=get_config())
        for s in sm.list_all():
            assert s["source"] in ("builtin", "user", "plugin"), f"技能来源异常：{s['name']}"

    def test_frontmatter_parsing(self):
        from fengcode.skills import parse_frontmatter

        meta, body = parse_frontmatter("---\nname: 测试\ntags: [a, b]\n---\n正文")
        assert meta["name"] == "测试"
        assert meta["tags"] == ["a", "b"]
        assert body.strip() == "正文"

    def test_skill_load(self):
        from fengcode.config import get_config
        from fengcode.skills import SkillManager

        sm = SkillManager(config=get_config())
        d = sm.load("代码改动规范")
        assert d and "最小改动" in d["body"]


# ==========================================================================
# 事件与存储
# ==========================================================================

class TestEvents:
    def test_emit_and_subscribe(self):
        from fengcode.events import EventBus

        bus = EventBus()
        seen = []
        bus.on_event(lambda e: seen.append(e.type))
        q = bus.subscribe()
        bus.emit("x.y", {"a": 1}, session_id="s")
        assert seen == ["x.y"]
        assert q.qsize() == 1

    def test_history_filter(self):
        from fengcode.events import EventBus

        bus = EventBus()
        bus.emit("a", {}, session_id="s1")
        bus.emit("b", {}, session_id="s2")
        assert len(bus.history(session_id="s1")) == 1


class TestStorage:
    def test_session_lifecycle(self):
        from fengcode.llm.types import Message
        from fengcode.storage import SessionStore

        ss = SessionStore()
        s = ss.create(title="测试")
        ss.append(s["id"], Message.user("你好"))
        assert len(ss.messages(s["id"])) == 1
        md = ss.export_markdown(s["id"])
        assert "你好" in md
        assert ss.delete(s["id"])

    def test_task_store(self):
        from fengcode.storage.tasks import TaskStore

        ts = TaskStore()
        t = ts.add("任务一", session_id="sx")
        ts.set_status(t["id"], "completed")
        assert ts.summary("sx")["completed"] == 1

    def test_kv_store(self):
        from fengcode.storage.tasks import KVStore

        kv = KVStore()
        kv.set("k1", {"a": [1, 2]})
        assert kv.get("k1")["a"] == [1, 2]


# ==========================================================================
# LLM 类型
# ==========================================================================

class TestLLMTypes:
    def test_tool_call_arg_parsing_tolerant(self):
        from fengcode.llm.types import ToolCall

        assert ToolCall.parse_arguments('{"a": 1}')["a"] == 1
        assert ToolCall.parse_arguments('```json\n{"b": 2}\n```')["b"] == 2
        assert ToolCall.parse_arguments('{"c": [1,2,]}')["c"] == [1, 2]

    def test_message_roundtrip(self):
        from fengcode.llm.types import Message

        m = Message.user("内容")
        assert Message.from_dict(m.to_dict()).content == "内容"

    def test_turn_result_receipt_fields(self):
        """回执三件套要能序列化出来（界面按它渲染回执卡）。"""
        from fengcode.core.agent import TurnResult

        r = TurnResult()
        assert r.changed_files == [] and r.verify_commands == [] and r.gaps == []
        r.changed_files = ["C:/x/y.txt"]
        r.verify_commands = ["pytest tests/"]
        r.gaps = ["已达最大步数 50"]
        d = r.to_dict()
        assert d["changed_files"] == ["C:/x/y.txt"]
        assert d["verify_commands"] == ["pytest tests/"]
        assert d["gaps"] == ["已达最大步数 50"]

    def test_runaway_repeat_detection(self):
        """重复输出检测（1.2.6 实测复现：模型陷入「我现在写。好。写。输出。」无限重复）。

        ★ 判据选了「尾部同周期重复轮数」，因为它两侧都分得很干净：
          真打转 8 轮、正常回答 1 轮、正常重复句式 3 轮 —— 阈值取 5 很宽裕。
          （先试过「片段频次」与「尾串出现过几次」两版，都区分不开，已弃。）
        """
        from fengcode.core.agent import _count_runaway_repeat

        loop = ["我现在写。", "好。", "写。", "输出。", "好。"] * 8
        assert _count_runaway_repeat(loop) >= 5, "真打转必须被检出"
        normal = [
            "这个功能已经实现。",
            "我先读了配置文件，确认字段名是 api_key_env。",
            "然后改了校验逻辑，并补了回归用例。",
            "最后跑了全量测试，103 项通过。",
        ]
        assert _count_runaway_repeat(normal) < 5, "正常回答不得误判"
        # 正常回答里出现重复句式（每步都写「第 N 步…」）也不能误判
        steps = ["第一步读文件。", "第二步改代码。", "第三步跑测试并确认全部通过。",
                 "第四步汇报结果。"] * 3
        assert _count_runaway_repeat(steps) < 5, "重复句式不得误判"
        # 内容太少时不该判为打转（避免刚开头就误杀）
        assert _count_runaway_repeat(["好。"]) == 0

    def test_stream_options_not_sent_to_unknown_endpoint(self):
        """`stream_options` 不能无条件发给所有端点（1.2.5 实测复现）。

        ★ 复现现场：`build_payload(..., stream=True)` 此前一律带上
        `stream_options={"include_usage": True}`。这是 2024 年才有的字段，
        较老的网关或转发较真的中转会**拒掉带着它的整个请求** —— 用户侧的
        现象是「这个中转站别的工具都能用，在 Fengcode 里每条消息都被拒」，
        而报错完全指不到真正的原因。
        """
        from fengcode.config.schema import Provider
        from fengcode.llm.openai_client import OpenAIClient
        from fengcode.llm.types import Message

        def has_so(name, **kw):
            p = Provider(name=name, kind="openai", models=["m"], **kw)
            _u, pl, _h = OpenAIClient(p, "k").build_payload(
                [Message.user("hi")], model="m", stream=True)
            return "stream_options" in pl

        # 未登记的第三方端点：默认不发（少一个 token 统计，好过整场被拒）
        assert has_so("old-gw", base_url="https://old.example.com/v1") is False
        # 目录内官方端点：正常发
        assert has_so("deepseek", base_url="https://api.deepseek.com/v1") is True
        # 供应商可显式开关（覆盖默认判断）
        assert has_so("x1", base_url="https://x/v1",
                      extra={"stream_options": True}) is True
        assert has_so("deepseek2", base_url="https://api.deepseek.com/v1",
                      extra={"stream_options": False}) is False
        # 非流式请求本来就不该有它
        p = Provider(name="x2", kind="openai",
                     base_url="https://api.deepseek.com/v1", models=["m"])
        _u, pl, _h = OpenAIClient(p, "k").build_payload(
            [Message.user("hi")], model="m", stream=False)
        assert "stream_options" not in pl

    def test_validate_rules_rejects_unknown_tool(self):
        """权限规则里的工具名必须真实存在，否则规则静默失效（1.2.5 实测复现）。

        ★ 复现现场：`validate_rules(['tool:zzz_not_a_tool'])` 此前返回空列表 ——
        规则写法毫无问题、界面上也不会报错，但它**永远不会匹配到任何工具**。
        容易以为自己禁掉了某个工具，其实什么都没禁；这类"安全规则静默失效"
        比没有规则更危险，所以必须在校验期就拦住，并尽量给出近似建议。
        """
        from fengcode.security.approval import validate_rules
        from fengcode.tools import register_builtin
        from fengcode.tools.base import get_registry

        register_builtin()
        assert get_registry().names(), "需要真实工具注册表才能做存在性校验"

        # 编造的工具名 → 必须报出来
        bad = validate_rules(["tool:zzz_not_a_tool"])
        assert bad and "zzz_not_a_tool" in bad[0], f"未知工具名应被拦下：{bad}"
        # 拼错时给近似建议（read_fil → read_file）
        near = validate_rules(["tool:read_fil"])
        assert near and "read_file" in near[0], f"拼错工具名应给近似建议：{near}"
        # 真实工具名放行
        assert validate_rules(["tool:read_file"]) == []
        # 通配符不做存在性判断（本来就是要匹配一批）
        assert validate_rules(["tool:read_*"]) == []
        # 既有写法校验不能被破坏
        assert validate_rules(["rm"]), "裸命令名仍应提示"
        assert validate_rules(["cmd:"]), "前缀后为空仍应提示"

    def test_search_docs_tool(self):
        """应用内文档检索：能查到自带文档，且覆盖 docs/ 目录。

        ★ 回归现场（本版实测抓到）：`repo_root()` 在源码树下返回的是 **cli 目录**，
        而 `docs/` 在它的上一级 —— 只查 repo_root() 会漏掉 architecture.md 与
        getting-started.md，检索等于半个残废。
        """
        from fengcode.tools.builtin.assist import _doc_roots, _search_bundled_docs

        roots = [str(r) for r in _doc_roots()]
        assert any(r.endswith("docs") for r in roots), f"必须能定位到 docs 目录：{roots}"
        # 文档里必然出现的词，且命中应跨到 docs/ 之外
        hits = _search_bundled_docs("架构", limit=5)
        assert hits, "应能检索到文档内容"
        files = {h["file"] for h in hits}
        assert files & {"architecture.md", "getting-started.md", "AGENTS.md"}, \
            f"应命中自带文档：{files}"
        for h in hits:
            assert h["line"] >= 1 and h["text"], "命中片段要带行号与上下文"

    def test_provider_url_matrix(self):
        """供应商协议矩阵：完整 URL 一律原样使用，不重复拼后缀。

        ★ 回归现场（本版实测连抓两处）：接第三方网关时 base_url 常是
        `.../chat/completions?api-version=2024` 或 `.../responses?api-version=...`，
        旧写法用 endswith 判断，带 query 就判不出来 → 尾部又被拼一次 /v1/...，
        请求必然 404/400。
        """
        from fengcode.config.schema import Provider
        from fengcode.llm.openai_client import OpenAIClient, OpenAIResponsesClient

        def url(kind, base):
            p = Provider(name="p", kind=kind, base_url=base, models=["m"])
            c = OpenAIResponsesClient(p, "k") if kind == "openai-responses" else OpenAIClient(p, "k")
            return c._chat_url()

        # 完整端点（含 query）必须原样返回
        full_chat = "https://gw.example.com/openai/deployments/d/chat/completions?api-version=2024"
        assert url("openai", full_chat) == full_chat
        full_resp = "https://gw.example.com/openai/deployments/d/responses?api-version=2024"
        assert url("openai-responses", full_resp) == full_resp
        # 规整 base → 正常拼接
        assert url("openai", "https://api.example.com/v1").endswith("/v1/chat/completions")
        assert url("openai-responses", "https://api.example.com/v1").endswith("/v1/responses")

    def test_responses_payload_shape(self):
        """Responses 协议：system 走 instructions、消息走 input、上限走 max_output_tokens。

        直接把 chat 的 body 打过去必然 400 —— 这是接新协议最常见的报错。
        """
        from fengcode.config.schema import Provider
        from fengcode.llm.openai_client import OpenAIResponsesClient
        from fengcode.llm.types import Message

        p = Provider(name="r", kind="openai-responses",
                     base_url="https://api.example.com/v1", models=["gpt-5"])
        c = OpenAIResponsesClient(p, "k")
        _url, payload, _h = c.build_payload(
            [Message.system("SYS"), Message.user("hi")], model="gpt-5", max_tokens=4096,
        )
        assert payload["instructions"] == "SYS", "system 必须走 instructions"
        assert "messages" not in payload, "Responses 不用 messages 数组"
        assert payload["input"][0]["role"] == "user"
        assert payload["input"][0]["content"][0]["type"] == "input_text"
        assert payload["max_output_tokens"] == 4096, "上限字段名是 max_output_tokens"

    def test_peak_offpeak_pricing(self):
        """峰谷计价：配了谷价才生效，未配一律按常价。"""
        import datetime as _dt

        from fengcode.llm.router import compute_cost, price_phase
        from fengcode.llm.types import Usage

        u = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000,
                  total_tokens=2_000_000)
        base = {"input": 10.0, "output": 30.0, "currency": "CNY", "unit": 1_000_000}
        # 未配谷价 → 常价，且不给标签（不打扰没启用峰谷的用户）
        assert compute_cost(u, base, when=0)[0] == 40.0
        assert price_phase(base, when=0) == ""

        peak = dict(base, off_peak_input=5.0, off_peak_output=15.0,
                    peak_hours="08:30-00:30")
        noon = _dt.datetime.now().replace(hour=12, minute=0, second=0,
                                          microsecond=0).timestamp()
        night = _dt.datetime.now().replace(hour=3, minute=0, second=0,
                                           microsecond=0).timestamp()
        assert compute_cost(u, peak, when=noon)[0] == 40.0, "高峰应正常计价"
        assert price_phase(peak, when=noon) == "peak"
        assert compute_cost(u, peak, when=night)[0] == 20.0, "谷段应用低谷单价"
        assert price_phase(peak, when=night) == "off_peak"

    def test_image_gate_and_size_limit(self):
        """图片两道闸：能力门控不误拦 + 超尺寸本地拦截。

        ★ 回归现场（本版实测抓到）：`model_info()` 对没登记过的模型
        把 vision 兜底成 False，照搬它做门控会把用户接的第三方模型
        全判成「不支持图片」，本来能用的图片反而被本地拦死。
        所以门控只在**明确知道不支持**时才拒绝。
        """
        from fengcode.llm.types import Attachment
        from fengcode.server.app import _check_images, _vision_supported

        # ★ 门控只在「明确知道不支持」时才拒绝：这里用一个必然解析不出模型的
        #   引用，验证「取不到信息 → 不拦」（正是被误拦的那个场景）。
        assert _vision_supported("不存在的供应商/不存在的模型") is True
        # 小图放行
        small = Attachment(kind="image", name="ok.png", mime="image/png", data=b"x" * 100)
        assert _check_images([small], model_ref=None) == ""
        # 非图片放行
        assert _check_images([], model_ref=None) == ""
        # 超尺寸本地拦截，并给出可操作的说明
        big = Attachment(kind="image", name="huge.png", mime="image/png",
                         data=b"x" * (9 * 1024 * 1024))
        why = _check_images([big], model_ref=None)
        assert "huge.png" in why and "8 MB" in why, f"应说明尺寸与上限：{why}"

    def test_subtask_parses_depends_on(self):
        """依赖边要能从 dict 定义解析出来（fleet 的基础）。"""
        from fengcode.agents.runner import SubTask

        st = SubTask.from_any({
            "name": "实现", "task": "写代码", "depends_on": ["调研", "1"],
        })
        assert st.depends_on == ["调研", "1"], "depends_on 必须原样带上"
        assert SubTask.from_any("纯字符串").depends_on == []

    def test_fleet_dependency_edges_carry_upstream(self):
        """舰队：依赖边必须把上游结论带进下游任务的上下文。

        ★ 回归现场（本版实测抓到）：runner.py 少了 `import contextlib`，
        舰队里 `contextlib.suppress(...)` 抛 NameError、被外层 except 吞掉，
        于是**每个任务都被记成失败**、上游结论根本传不下去 ——
        而 pytest 仍然全绿（没有用例覆盖舰队）。这条用例专门堵这个洞。
        """
        import asyncio

        from fengcode.agents.runner import SubAgentRunner, SubResult

        class _AgentCfg:
            max_parallel_subagents = 4
            subagent_models = {}
            subagent_model = None
            max_depth = 3
            default_budget_tokens = 120000
            default_timeout = 900.0

        class _Cfg:
            agent = _AgentCfg()

        r = SubAgentRunner.__new__(SubAgentRunner)
        r._runs = {}
        r.config = _Cfg()
        r._depth = 0

        seen: dict[str, str] = {}

        async def fake_run_one(st, *, depth):  # noqa: ANN001
            seen[st.name] = st.context or ""
            await asyncio.sleep(0.02)
            return SubResult(name=st.name, agent_type=st.agent_type, task=st.task,
                             ok=True, content=f"【{st.name} 的结论】正文")

        r._run_one = fake_run_one  # type: ignore[assignment]

        tasks = [
            {"name": "调研", "task": "查资料", "agent_type": "research"},
            {"name": "实现", "task": "写代码", "depends_on": ["调研"]},
        ]
        res = asyncio.run(r.run_fleet(tasks, context="背景"))
        assert res["ok"], f"舰队应全部成功，实际：{res.get('content')}"
        assert res["deps"] == {"调研": [], "实现": ["调研"]}
        assert "【调研 的结论】" in seen.get("实现", ""), "下游必须携带上游结论"
        assert "上游" not in seen.get("调研", ""), "无上游的任务不该有上游块"

    def test_schedule_time_window(self):
        """时间窗口：只在指定时段内触发，跨零点自动识别。

        回归现场：光有「每 30 分钟」不够用，用户要的是「每 30 分钟，但只在
        9:00-18:00 跑」。窗口外应一律不触发。
        """
        import time as _t

        from fengcode.scheduler.cron import _in_window

        # 取一个「当前分钟」来构造窗口，避免测试与真实时间耦合
        lt = _t.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min

        def hhmm(m):
            return f"{m // 60:02d}:{m % 60:02d}"

        # 两端都空 → 不限制
        assert _in_window({}, _t.time())
        # 覆盖当前时刻的窗口 → True
        lo = max(0, now_min - 5)
        hi = min(23 * 60 + 59, now_min + 5)
        assert _in_window({"window_start": hhmm(lo), "window_end": hhmm(hi)}, _t.time())
        # 明确不含当前时刻的窗口 → False
        off = (now_min + 600) % (24 * 60)
        assert not _in_window({"window_start": hhmm(off), "window_end": hhmm((off + 1) % (24 * 60))},
                              _t.time())

    def test_schedule_window_accepts_payload_form(self):
        """窗口存进 payload._window 后，判定函数也要认（存与用必须是同一份）。"""
        import time as _t

        from fengcode.scheduler.cron import _in_window

        lt = _t.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min
        lo = max(0, now_min - 5)
        hi = min(23 * 60 + 59, now_min + 5)
        job = {"payload": {"_window": {"start": f"{lo // 60:02d}:{lo % 60:02d}",
                                       "end": f"{hi // 60:02d}:{hi % 60:02d}"}}}
        assert _in_window(job, _t.time())

    def test_next_run_describe(self):
        """下次运行描述：给出具体时刻与剩余时间，停用任务明确标注。"""
        import time as _t

        from fengcode.scheduler.cron import next_run_describe

        now = _t.time()
        txt = next_run_describe({"interval_seconds": 1800, "enabled": True, "last_run": now}, now=now)
        assert "还有" in txt and "分" in txt, f"应给出剩余时间：{txt}"
        assert next_run_describe({"interval_seconds": 60, "enabled": False}, now=now) == "已停用"
        # cron：应算出一个未来时刻
        txt2 = next_run_describe({"cron": "0 3 * * *", "enabled": True}, now=now)
        assert "还有" in txt2 or "即将" in txt2, f"cron 也要给出下次：{txt2}"

    def test_drop_superseded_blocks(self):
        """结构化压缩：同一状态块被后续副本取代时，只保留最后一次。

        回归现场：长会话里待办/计划会被反复完整重述，早期副本纯占位却和最新副本
        一样占 token。丢掉「确知被取代」的块不影响信息（最新那份还在）。
        """
        from fengcode.llm.types import Message
        from fengcode.memory.summarizer import drop_superseded_blocks

        msgs = [
            Message.user("任务开始"),
            Message.system("<todos>\n- 旧清单 A\n</todos>"),
            Message.user("继续"),
            Message.system("<todos>\n- 新清单 B\n</todos>"),
            Message.user("收尾"),
        ]
        out = drop_superseded_blocks(msgs)
        assert len(out) == 4, "旧的 todos 块应被丢掉"
        joined = "\n".join(m.content or "" for m in out)
        assert "旧清单 A" not in joined, "被取代的旧内容不该还在"
        assert "新清单 B" in joined, "最后一次的块必须保留"

    def test_drop_superseded_keeps_free_text(self):
        """自由正文不做「相似即丢弃」的猜测 —— 宁可少丢，不能误伤。"""
        from fengcode.llm.types import Message
        from fengcode.memory.summarizer import drop_superseded_blocks

        msgs = [
            Message.user("请帮我看看这个函数"),
            Message.assistant("这个函数做了三件事……"),
            Message.user("再帮我看看另一个"),
        ]
        out = drop_superseded_blocks(msgs)
        assert len(out) == len(msgs), "没有结构化状态块时不应改动任何消息"

    def test_cache_aware_dynamic_block_order(self):
        """缓存感知投影：高频变化的块（记忆/便签/待办）必须排在低频块之后。

        回归现场：provider 的 prompt cache 是前缀逐字节匹配，高频块排在前面时，
        它一变后面的低频块也跟着作废，命中长度被削掉一大截。
        """
        from fengcode.core.prompts import build_system_prompt

        text = build_system_prompt(
            workspace="/tmp/ws", model="m", provider="p", tools=["read_file"],
            session_title="某会话", goal_block="目标 X",
            skill_catalog="技能目录 Y", memory_block="记忆块 Z",
            notes_block="便签块 N", todo_block="- 待办 T",
        )
        # 低频（会话主题/目标）应出现在高频（记忆/便签/待办）之前
        i_title = text.find("某会话")
        i_mem = text.find("记忆块 Z")
        i_todo = text.find("待办 T")
        assert i_title != -1 and i_mem != -1 and i_todo != -1
        assert i_title < i_mem < i_todo, "动态块必须按变化频率从慢到快排列"

    def test_session_write_lease(self):
        """会话写入租约：同会话同时只允许一个写者，冲突时明确报出占用者。

        回归现场：agent() 的锁只保护「创建 Agent」，建好之后两个请求能同时进 run()，
        交错往同一会话追加消息 —— 历史顺序会乱、用量统计互相覆盖。
        """
        from fengcode.server.app import AppState

        st = AppState()
        ok, why = st.acquire_session("s1", "turn-a")
        assert ok and why == ""
        # 第二个写者被拒，并拿到占用者标识（供界面明确报错）
        ok2, why2 = st.acquire_session("s1", "turn-b")
        assert not ok2 and why2 == "turn-a"
        # 不同会话互不影响
        ok3, _ = st.acquire_session("s2", "turn-c")
        assert ok3
        # 非持有者释放无效（不能把别人的锁清掉）
        st.release_session("s1", "turn-b")
        assert st.session_busy("s1") == "turn-a"
        # 持有者释放后可再次获取
        st.release_session("s1", "turn-a")
        assert st.session_busy("s1") == ""
        assert st.acquire_session("s1", "turn-d")[0]

    def test_drop_agent_releases_lease(self):
        """关闭会话时要一并释放租约，否则该会话会被永久判为「有人在写」。"""
        from fengcode.server.app import AppState

        st = AppState()
        st.acquire_session("s9", "turn-x")
        assert st.session_busy("s9") == "turn-x"
        st.drop_agent("s9")
        assert st.session_busy("s9") == ""

    def test_turn_result_content_streamed_flag(self):
        """流式已交付标记要能传出（前端据此避免重复渲染）。"""
        from fengcode.core.agent import TurnResult

        r = TurnResult()
        assert r.to_dict()["content_streamed"] is False
        r.content_streamed = True
        assert r.to_dict()["content_streamed"] is True

    def test_cost_calculation(self):
        from fengcode.llm import compute_cost
        from fengcode.llm.types import Usage

        amount, cur = compute_cost(
            Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000),
            {"input": 3, "output": 15, "currency": "$"},
        )
        assert abs(amount - 18.0) < 0.001
        assert cur == "$"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
