"""单元测试：工作模式（计划/目标）、权限细粒度规则、工作区（项目）。

运行：python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="fengcode-test-modes-")
os.environ["FENGCODE_HOME"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ==========================================================================
# 工作模式：计划 / 目标 / 不指定（由 AI 自主判断）
# ==========================================================================

class TestWorkMode:
    def test_normalize_mode_maps_legacy_chat_to_empty(self):
        """旧的 'chat'（被去掉的强制对话模式）必须视为「不选」。"""
        from fengcode.core.prompts import normalize_mode

        assert normalize_mode("chat") == ""
        assert normalize_mode("") == ""
        assert normalize_mode(None) == ""
        assert normalize_mode("不认识的值") == ""

    def test_normalize_mode_accepts_aliases(self):
        from fengcode.core.prompts import normalize_mode

        for v in ("plan", "PLAN", "计划", "计划模式"):
            assert normalize_mode(v) == "plan", v
        for v in ("goal", "Goal", "目标", "目标模式"):
            assert normalize_mode(v) == "goal", v

    @pytest.mark.parametrize("mode,expect", [
        ("", "自动判断"),
        ("plan", "计划模式"),
        ("goal", "目标模式"),
    ])
    def test_system_prompt_injects_work_mode(self, mode, expect):
        """三种状态都要真的注入对应提示段。"""
        from fengcode.core.prompts import build_system_prompt

        p = build_system_prompt(mode=mode)
        assert "<work_mode>" in p
        assert expect in p
        # 只能有一份，避免重复注入
        assert p.count("<work_mode>") == 1

    def test_auto_mode_tells_model_to_decide(self):
        """不选任何模式时，提示里要明确让模型自己判断。"""
        from fengcode.core.prompts import build_system_prompt

        p = build_system_prompt(mode="")
        assert "自行判断" in p

    def test_plan_mode_requires_confirmation(self):
        from fengcode.core.prompts import build_system_prompt

        p = build_system_prompt(mode="plan")
        assert "等用户确认" in p or "停下" in p

    def test_goal_mode_references_goal_tool(self):
        from fengcode.core.prompts import build_system_prompt

        p = build_system_prompt(mode="goal")
        assert "goal" in p


# ==========================================================================
# 权限细粒度规则
# ==========================================================================

class TestApprovalRules:
    def test_rule_matching_syntax(self):
        """规则语法：tool:/cmd:/path:/risk:。"""
        from fengcode.security.approval import _match_rule

        assert _match_rule("tool:shell", "shell", "")
        assert _match_rule("tool:read_*", "read_file", "")
        assert not _match_rule("tool:shell", "write_file", "")
        assert _match_rule("cmd:git *", "shell", "git status")
        assert _match_rule("path:**/.ssh/*", "write_file", "C:/x/.ssh/id_rsa")
        assert not _match_rule("path:**/.ssh/*", "write_file", "C:/x/a.txt")
        assert _match_rule("risk:删除", "shell", "删除所有文件")

    def test_cmd_rule_matches_by_prefix_without_wildcard(self):
        """没有通配符时 cmd: 应按前缀匹配，符合直觉。"""
        from fengcode.security.approval import _match_rule

        assert _match_rule("cmd:git push", "shell", "git push origin main")
        assert not _match_rule("cmd:git push", "shell", "git status")

    def test_path_rule_matches_by_substring_without_wildcard(self):
        from fengcode.security.approval import _match_rule

        assert _match_rule("path:.ssh", "write_file", "C:/Users/x/.ssh/id_rsa")

    def test_deny_rule_overrides_allow_mode(self):
        """deny 规则必须优先于 allow 模式（真的拦住）。"""
        from fengcode.config.schema import ApprovalRule, PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(
            mode="allow",
            rules=[ApprovalRule(pattern="tool:shell", action="deny")],
        ))
        allowed, reason, _ = gate.evaluate("shell", command="rm -rf /", dangerous=True)
        assert allowed is False
        assert "禁止" in reason or "拒绝" in reason

    def test_allow_rule_short_circuits_ask_mode(self):
        from fengcode.config.schema import ApprovalRule, PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(
            mode="ask",
            rules=[ApprovalRule(pattern="cmd:pytest *", action="allow")],
        ))
        allowed, _reason, _ = gate.evaluate("shell", command="pytest -q")
        assert allowed is True

    def test_ask_rule_creates_approval_request(self):
        from fengcode.config.schema import ApprovalRule, PermissionsConfig
        from fengcode.security.approval import ApprovalGate

        gate = ApprovalGate(PermissionsConfig(
            mode="ask",
            rules=[ApprovalRule(pattern="cmd:git push", action="ask")],
        ))
        allowed, _reason, req = gate.evaluate("shell", command="git push origin main")
        assert allowed is False
        assert req is not None


# ==========================================================================
# 工作区（项目）
# ==========================================================================

class TestWorkspaces:
    def test_default_workspace_always_exists(self):
        from fengcode.storage.workspaces import WorkspaceStore

        store = WorkspaceStore()
        ws = store.ensure_default()
        assert ws["id"]
        assert ws["is_default"] is True

    def test_create_project_with_auto_dir(self):
        from fengcode.storage.workspaces import WorkspaceStore

        store = WorkspaceStore()
        ws = store.create(name="测试项目", path=str(Path(_TMP) / "proj-a"))
        assert ws["name"] == "测试项目"
        assert ws["path"]
        assert store.get(ws["id"]) is not None

    def test_duplicate_names_get_suffix(self):
        """同名项目要自动加序号，避免混淆。"""
        from fengcode.storage.workspaces import WorkspaceStore

        store = WorkspaceStore()
        a = store.create(name="重名项目", path=str(Path(_TMP) / "dup-1"))
        b = store.create(name="重名项目", path=str(Path(_TMP) / "dup-2"))
        assert a["name"] != b["name"]

    def test_default_workspace_cannot_be_deleted(self):
        from fengcode.storage.workspaces import WorkspaceStore

        store = WorkspaceStore()
        ws = store.ensure_default()
        assert store.delete(ws["id"]) is False

    def test_project_can_be_renamed(self):
        from fengcode.storage.workspaces import WorkspaceStore

        store = WorkspaceStore()
        ws = store.create(name="待改名", path=str(Path(_TMP) / "ren"))
        assert store.rename(ws["id"], "改好了")
        assert store.get(ws["id"])["name"] == "改好了"


class TestSessionWorkspaceIsolation:
    """会话的工作区必须真正决定「文件写到哪」。

    回归现场：会话表里的 workspace 此前只用于侧栏分组，从没传给 Agent ——
    不管在哪个项目里新建会话，文件都写进同一个默认工作目录。缺了这一环，
    后面的工作区写租约也无从谈起（所有会话会被算成同一个工作区）。
    """

    def test_session_workspace_resolves_absolute_path(self):
        from fengcode.server.app import AppState
        from fengcode.storage.db import get_db
        from fengcode.storage.sessions import SessionStore

        d = str(Path(_TMP) / "proj-path")
        s = SessionStore(get_db()).create(title="路径会话", workspace=d)
        st = AppState()
        assert st._norm_ws(st.session_workspace(s["id"])) == st._norm_ws(d)

    def test_session_workspace_resolves_project_name(self):
        """老数据 / 手填的是项目名时，按名字回查目录。"""
        from fengcode.server.app import AppState
        from fengcode.storage.db import get_db
        from fengcode.storage.sessions import SessionStore
        from fengcode.storage.workspaces import WorkspaceStore

        p = str(Path(_TMP) / "proj-name")
        ws = WorkspaceStore().create(name="按名字的项目", path=p)
        s = SessionStore(get_db()).create(title="名字会话", workspace=ws["name"])
        st = AppState()
        assert st._norm_ws(st.session_workspace(s["id"])) == st._norm_ws(p)

    def test_session_without_workspace_falls_back(self):
        """没记工作区的会话不该被塞一个空目录。"""
        from fengcode.server.app import AppState
        from fengcode.storage.db import get_db
        from fengcode.storage.sessions import SessionStore

        s = SessionStore(get_db()).create(title="无工作区")
        st = AppState()
        assert st.session_workspace(s["id"]) is None

    def test_set_workspace_moves_guard_and_sandbox(self):
        """切工作目录要连路径守卫、审批门的 guard、沙箱 cwd 一起换。

        回归现场：只改 self.workspace 会变成「人在 A 项目干活、守卫仍按 B 项目
        判越界」，表现为在自己项目里写文件反而弹审批。
        """
        from fengcode.core.agent import Agent

        a = Agent(workspace=str(Path(_TMP) / "ws-a"), session_id="sw1")
        b = str(Path(_TMP) / "ws-b")
        a.set_workspace(b)
        assert Path(a.workspace) == Path(b)
        assert Path(a.guard.workspace) == Path(b)
        # 审批门自带一份 guard，漏掉它守卫就只在部分工具上生效
        assert Path(a.approval.guard.workspace) == Path(b)
        assert Path(a.sandbox.cwd) == Path(b)

    def test_set_workspace_persists_to_session(self):
        """换目录要写回会话记录，否则下次建 Agent 又按旧目录算。"""
        from fengcode.core.agent import Agent
        from fengcode.storage.db import get_db
        from fengcode.storage.sessions import SessionStore

        ss = SessionStore(get_db())
        s = ss.create(title="跟随会话", workspace=str(Path(_TMP) / "ws-old"))
        a = Agent(workspace=str(Path(_TMP) / "ws-old"), session_id=s["id"])
        b = str(Path(_TMP) / "ws-new")
        a.set_workspace(b)
        assert Path((ss.get(s["id"]) or {}).get("workspace")) == Path(b)


# ==========================================================================
# 会话与工作模式的持久化联动
# ==========================================================================

class TestSessionMode:
    def test_session_stores_mode(self):
        from fengcode.storage.db import get_db
        from fengcode.storage.sessions import SessionStore

        ss = SessionStore(get_db())
        s = ss.create(title="模式测试", mode="plan")
        assert (ss.get(s["id"]) or {}).get("mode") == "plan"

    def test_session_mode_can_be_cleared(self):
        """「不选模式」要能存成空字符串。"""
        from fengcode.storage.db import get_db
        from fengcode.storage.sessions import SessionStore

        ss = SessionStore(get_db())
        s = ss.create(title="清空模式", mode="goal")
        ss.update(s["id"], mode="")
        assert (ss.get(s["id"]) or {}).get("mode") == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
