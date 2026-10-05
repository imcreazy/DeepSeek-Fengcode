"""「始终允许」记忆粒度 + cd 前缀绕过（2026-10-05）。

现象：用户在截图那条「清理工作区临时文件」上点「始终允许」，
      界面提示看着只针对那一条命令，实际却把**所有以 cd 开头的命令**都放行了。

两个真问题，根因相同：判据看的是命令的**第一个词**，而模型爱写
`cd "<工作区>"` 换行再接真正的操作。

  ① 记忆粒度错：记忆键取到 `cd`，等于「以后所有 cd 开头的都放行」；
  ② 可绕过「改仓库状态每次都问」：`cd X && git restore .` 被判成「未知操作」
     直接放行；不带 cd 前缀的 `git restore .` 却会拦下来问 ——
     包一层 cd 就绕过了不可逆操作的确认。
"""

from __future__ import annotations

from fengcode.config.schema import PermissionsConfig
from fengcode.security.approval import ApprovalGate
from fengcode.security.paths import classify_command, effective_program


def test_effective_program_skips_navigation_prefix():
    """记忆键要看「真正要干的事」，不能被 cd 遮住。"""
    assert effective_program('cd "C:\\ws"\nRemove-Item -Recurse -Force a.py') == "remove-item"
    assert effective_program('cd "C:\\ws" && Remove-Item -Recurse -Force a.py') == "remove-item"
    assert effective_program("Remove-Item -Recurse -Force a.py") == "remove-item"
    # 同类操作（换目录、换文件名）仍算同一类 —— 不能把「始终允许」变得没用
    assert effective_program('cd "C:\\other"\nRemove-Item -Recurse -Force b.py') == "remove-item"


def test_remembered_scope_does_not_leak_to_other_navigation_commands():
    """回归现场：点了「始终允许」后，**另一个目录**下的其它操作不该被顺手放行。"""
    gate = ApprovalGate(PermissionsConfig(mode="ask"), workspace=r"C:\ws")
    cmd = 'cd "C:\\ws"\nRemove-Item -Recurse -Force _a.py'
    ok, _, req = gate.evaluate("shell", command=cmd, session_id="s1")
    assert not ok and req is not None
    gate.resolve_with_key(req.id, True, action="shell", target=cmd,
                          session_id="s1", remember="always")

    # ★ 记忆键里不能出现裸 `cd`（那正是「放行整类」的成因）
    keys = list(gate._always_allows)
    assert keys == ["s1|shell|remove-item"], f"记忆键粒度错了：{keys}"
    assert not any(k.endswith("|cd") for k in keys), "不得把 cd 记成一类操作"


def test_cd_prefix_cannot_bypass_repo_always_ask():
    """回归现场：`cd X && git restore .` 曾被判成「未知操作」直接放行。

    这类命令会丢弃未提交的改动、不可逆，约定是**每次都问**且不复用「始终允许」。
    """
    assert classify_command("git restore .") == "repo"
    assert classify_command("cd X && git restore .") == "repo", "cd 前缀不得遮住 git restore"
    assert classify_command("cd X\ngit restore .") == "repo"

    gate = ApprovalGate(PermissionsConfig(mode="ask"), workspace=r"C:\ws")
    # 先把不带前缀的那条记住
    ok, _, req = gate.evaluate("shell", command="git restore .", session_id="s2")
    assert not ok and req is not None
    gate.resolve_with_key(req.id, True, action="shell", target="git restore .",
                          session_id="s2", remember="always")
    # 不带前缀：仍要问（repo 类每次问）
    ok2, _, _ = gate.evaluate("shell", command="git restore .", session_id="s2")
    assert not ok2, "改仓库状态的命令不应复用「始终允许」"
    # ★ 带 cd 前缀：同样要问（这正是被绕过的入口）
    ok3, _, req3 = gate.evaluate("shell", command="cd X && git restore .", session_id="s2")
    assert not ok3, "包一层 cd 不得绕过「改仓库状态每次都问」"
    assert req3 is not None
