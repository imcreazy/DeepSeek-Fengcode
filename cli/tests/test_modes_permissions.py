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
