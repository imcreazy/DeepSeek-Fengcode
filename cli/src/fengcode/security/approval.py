"""审批门：危险操作在真正执行前请求用户批准。

设计
----
- ``mode``：``allow`` 全放行 / ``ask`` 危险操作需批准 / ``deny`` 只读（拒绝一切写操作）
- 判定顺序：显式规则 → 内置危险模式 → 路径白名单 → 默认放行
- 请求走 ``asyncio.Future``；界面通过事件总线拿到请求并回填结果
- 超时未响应按配置处理（默认拒绝，安全优先）
- 所有决定都会写入审计日志
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config.schema import PermissionsConfig
from ..storage.stats import AuditStore
from ..utils import new_id
from .paths import PathGuard, classify_command, scan_dangerous, scan_dangerous_path


@dataclass
class ApprovalRequest:
    """一次待批准的请求。"""

    id: str
    action: str            # 工具名
    target: str            # 目标（命令 / 路径）
    reason: str            # 为什么需要批准
    risk: str = "medium"   # low / medium / high
    detail: str = ""
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "target": self.target,
            "reason": self.reason,
            "risk": self.risk,
            "detail": self.detail,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "preview": self.preview,
        }


@dataclass
class Decision:
    """审批结果。"""

    allowed: bool
    reason: str = ""
    remembered: bool = False       # 是否记住本次选择
    by: str = "policy"

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "remembered": self.remembered, "by": self.by}


class ApprovalGate:
    """审批门实现。"""

    def __init__(
        self,
        config: PermissionsConfig,
        *,
        workspace: str | None = None,
        audit: AuditStore | None = None,
        on_request: Callable[[ApprovalRequest], None] | None = None,
    ) -> None:
        self.config = config
        self.audit = audit
        self.on_request = on_request
        self.guard = PathGuard(
            workspace=workspace,
            write_paths=config.write_paths,
            read_paths=config.read_paths,
            deny_patterns=config.deny_patterns,
            mode=config.mode,
        )
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = threading.RLock()
        # 界面响应者标记；未注册时审批请求会立即拒绝而不是空等超时
        self._responder: Any = None
        # 会话级"本会话内总是允许"记忆：{"会话id:动作模式": True}
        self._session_allows: set[str] = set()
        self._always_allows: set[str] = set()

    # ---- 策略更新 ------------------------------------------------------
    def update_config(self, config: PermissionsConfig) -> None:
        self.config = config
        self.guard = PathGuard(
            workspace=self.guard.workspace,
            write_paths=config.write_paths,
            read_paths=config.read_paths,
            deny_patterns=config.deny_patterns,
            mode=config.mode,
        )

    @property
    def mode(self) -> str:
        return self.config.mode

    # ---- 判定 ----------------------------------------------------------
    def evaluate(
        self,
        action: str,
        *,
        command: str = "",
        path: str = "",
        dangerous: bool = False,
        session_id: str | None = None,
        extra: str = "",
    ) -> tuple[bool, str, ApprovalRequest | None]:
        """返回 ``(是否可直接执行, 原因, 需批准的请求)``。"""
        cfg = self.config
        target = command or path or extra

        # 0) deny 模式：一律拒绝写操作
        if cfg.mode == "deny" and (dangerous or command or path):
            return False, "当前为只读模式，已拒绝该操作", None

        # 1) 显式规则优先
        for rule in cfg.rules:
            if not rule.pattern:
                continue
            if _match_rule(rule.pattern, action, target):
                if rule.action == "allow":
                    return True, rule.reason or f"规则放行：{rule.pattern}", None
                if rule.action == "deny":
                    return False, rule.reason or f"规则禁止：{rule.pattern}", None
                # ask 落到审批流程
                req = ApprovalRequest(
                    id=new_id("ap"),
                    action=action,
                    target=target,
                    reason=rule.reason or f"规则要求确认：{rule.pattern}",
                    risk="medium",
                    session_id=session_id,
                )
                return False, req.reason, req

        # 2) 内置危险模式
        reasons: list[str] = []
        if command:
            reasons.extend(scan_dangerous(command))
        if path:
            reasons.extend(scan_dangerous_path(path))

        # 2.5) 命令效果分类（参数感知）——决定「要不要问」以及「能不能被记住」
        effect = classify_command(command) if command else ""
        force_ask = effect == "repo"
        if effect == "read" and not reasons:
            # 只读命令（git status / ls / dir…）且没有危险模式 → 直接放行。
            # 这正是用户要的：查状态不该被拦。
            return True, "", None

        memory_key = self._memory_key(session_id, action, target)
        # ★ 改仓库状态的命令（git restore / reset / clean / stash drop…）**每次都问**：
        #   它们会丢弃工作区改动，不可逆；一旦被「本会话/始终允许」记住，
        #   后面同类操作就再也不提示了（用户明确要求这类必须每次批准）。
        if not force_ask:
            if memory_key in self._always_allows:
                return True, "此前已选择始终允许", None
            if memory_key in self._session_allows:
                return True, "本会话已允许同类操作", None

        # allow 模式：除高危外放行（但「改仓库状态」仍要问）
        if cfg.mode == "allow" and not force_ask:
            high = [r for r in reasons if r in _HIGH_RISK_REASONS]
            if not high:
                return True, "", None
            reasons = high

        if not reasons and not force_ask:
            # 没有命中任何具体危险模式：放行。
            # dangerous 标记只用于失败时的提示语气，不单独触发审批——
            # 否则 shell / write_file 这类工具会把每条命令都变成审批，完全不可用。
            return True, "", None

        risk = "high" if any(r in _HIGH_RISK_REASONS for r in reasons) else "medium"
        if force_ask and not reasons:
            reason = "会改动仓库状态（可能丢弃未提交的改动）"
            risk = "high"
        else:
            reason = "、".join(dict.fromkeys(reasons))
        req = ApprovalRequest(
            id=new_id("ap"),
            action=action,
            target=target,
            reason=f"检测到风险：{reason}" if not force_ask else f"需要确认：{reason}",
            risk=risk,
            session_id=session_id,
            preview=target[:1000],
        )
        return False, req.reason, req

    # ---- 异步审批 ------------------------------------------------------
    async def request(self, req: ApprovalRequest) -> Decision:
        """发起审批请求并等待结果。

        若没有任何界面订阅者能回应（如后台任务、无人值守运行），
        立即拒绝而不是空等超时——等待没有意义，还会拖慢整条链路。
        """
        if self.on_request is None and not self._has_responder():
            dec = Decision(
                allowed=False,
                reason="当前没有交互通道可以批准该操作，已按安全策略拒绝",
                by="no-responder",
            )
            self._log(req, dec)
            return dec

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        with self._lock:
            self._pending[req.id] = fut
        if self.on_request:
            try:
                self.on_request(req)
            except Exception:
                pass
        try:
            timeout = max(5.0, float(self.config.approval_timeout_seconds or 300))
            decision = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            decision = Decision(allowed=False, reason="等待批准超时，已自动拒绝", by="timeout")
        except asyncio.CancelledError:
            decision = Decision(allowed=False, reason="已取消", by="cancel")
        finally:
            with self._lock:
                self._pending.pop(req.id, None)
        self._log(req, decision)
        return decision

    def set_responder(self, responder: Any) -> None:
        """注册界面响应者（有它才认为"有人能批准"）。

        responder 可以是任意真值；仅用于判断是否存在交互通道。
        """
        self._responder = responder

    def _has_responder(self) -> bool:
        return bool(getattr(self, "_responder", None))

    def resolve(self, request_id: str, allowed: bool, *, remember: str = "once", reason: str = "", by: str = "user") -> bool:
        """由界面回填审批结果。

        remember: once / session / always
        """
        with self._lock:
            fut = self._pending.get(request_id)
            if fut is None or fut.done():
                return False
            req_action = ""
            for key in list(self._session_allows | self._always_allows):
                pass
            dec = Decision(allowed=allowed, reason=reason or ("用户允许" if allowed else "用户拒绝"),
                           remembered=remember != "once", by=by)
            fut.set_result(dec)
        return True

    def resolve_with_key(self, request_id: str, allowed: bool, *, action: str, target: str,
                         session_id: str | None = None, remember: str = "once") -> bool:
        """回填结果，并按 remember 记录同类操作的记忆键。"""
        key = self._memory_key(session_id, action, target)
        if allowed and remember == "session":
            self._session_allows.add(key)
        elif allowed and remember == "always":
            self._always_allows.add(key)
        return self.resolve(request_id, allowed, remember=remember, by="user")

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"id": k} for k, v in self._pending.items() if not v.done()]

    def cancel_all(self, reason: str = "会话结束") -> None:
        with self._lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result(Decision(allowed=False, reason=reason, by="cancel"))
            self._pending.clear()

    def clear_memories(self) -> None:
        self._session_allows.clear()
        self._always_allows.clear()

    def _memory_key(self, session_id: str | None, action: str, target: str) -> str:
        head = (target or "").strip().split("\n")[0][:80]
        # 命令只保留第一个词，避免把参数差异当成不同操作
        if target and not target.startswith(("/", "\\", "C:", "D:")):
            head = head.split()[0] if head.split() else head
        return f"{session_id or '-'}|{action}|{head}"

    def _log(self, req: ApprovalRequest, dec: Decision) -> None:
        if self.audit is None or not self.config.audit_enabled:
            return
        try:
            self.audit.log(
                action=req.action,
                target=req.target,
                decision="allow" if dec.allowed else "deny",
                reason=f"{req.reason} → {dec.reason}（{dec.by}）",
                session_id=req.session_id,
                actor="approval",
                ok=dec.allowed,
            )
        except Exception:
            pass


_HIGH_RISK_REASONS = {
    "删除根目录",
    "删除用户主目录",
    "格式化文件系统",
    "直接写裸设备",
    "fork 炸弹",
    "格式化磁盘",
    "递归删除盘符根目录",
    "磁盘分区操作",
    "修改启动配置",
    "删除卷影副本",
    "擦除磁盘剩余空间",
    "清空事件日志",
    "杀死 init",
    "覆写块设备",
    "关机/重启系统",
}


def validate_rules(rules: list[str] | None) -> list[str]:
    """校验权限规则写法，返回「可疑规则」的中文说明（空列表 = 全部正常）。

    ★ 1-G：为什么必须校验 —— 规则写成裸命令名（如 ``deny: ["rm"]``）时，
      它在 `_match_rule` 里只会按「子串出现在目标或等于工具名」去比，
      对 `shell` 工具的 `rm -rf /` 这类命令**永远匹配不到**。
      安全规则静默失效比没有规则更危险：用户以为自己禁掉了，其实没有。

    判定为可疑的情形：
      · 裸词（无 ``:` 前缀、无通配符）且看起来像命令/路径 —— 极可能匹配不到；
      · 前缀不认识（如 ``cmdd:``）—— 会被当成裸词，同样匹配不到；
      · 前缀后为空（如 ``cmd:``）—— 永不匹配。
    """
    out: list[str] = []
    known = {"tool", "action", "cmd", "command", "path", "file", "risk"}
    # ★ 可用的工具名（用于校验 `tool:` 前缀后的名字是否真实存在）。
    #   为什么必须查：规则写法看着对、工具名却是编的时，它永远匹配不到 ——
    #   用户以为自己禁掉了某个工具，其实什么都没禁。安全规则**静默失效**
    #   比没有规则更危险（1.2.5 实测复现：`tool:zzz_not_a_tool` 此前静默通过）。
    #   取不到注册表时（如纯配置校验场景）不做这层判断，避免误报。
    valid_tools: set[str] = set()
    try:
        from ..tools.base import get_registry

        valid_tools = {t.name for t in get_registry().all()}
    except Exception:
        valid_tools = set()
    for raw in rules or []:
        r = str(raw or "").strip()
        if not r:
            continue
        if ":" in r:
            kind, _, rest = r.partition(":")
            k = kind.lower()
            if k not in known:
                out.append(
                    f"「{r}」：前缀 “{kind}:” 不是已知前缀，会被当成普通子串匹配，很可能永远匹配不到。"
                    f"可用前缀：tool: / cmd: / path: / risk:"
                )
            elif not rest.strip():
                out.append(f"「{r}」：前缀后为空，永远不会匹配到任何东西。")
            elif k == "tool" and valid_tools:
                # 通配符规则不做存在性判断（本来就是要匹配一批）
                name = rest.strip()
                if not re.search(r"[*?\[]", name) and name not in valid_tools:
                    near = _closest_tool(name, valid_tools)
                    tip = f"，是否想写 “tool:{near}”？" if near else ""
                    out.append(
                        f"「{r}」：没有名为 “{name}” 的工具，这条规则永远不会生效{tip}"
                    )
            continue
        if re.search(r"[*?\[]", r):
            continue
        # 裸词：不含空格时若像命令名/路径，提醒加前缀
        if re.fullmatch(r"[A-Za-z0-9_.\\/:\-]+", r):
            hint = "cmd:" if "." not in r.split("/")[-1] else "path:"
            out.append(
                f"「{r}」：没有前缀也没有通配符，只会按子串匹配，通常匹配不到命令/路径。"
                f"如果是要拦命令请写 “cmd:{r}”，拦路径请写 “path:*/{r}”。"
            )
    return out


def _closest_tool(name: str, valid: set[str]) -> str:
    """给写错的工具名挑一个最相近的（仅供提示，不参与判定）。"""
    try:
        import difflib

        m = difflib.get_close_matches(name, sorted(valid), n=1, cutoff=0.6)
        return m[0] if m else ""
    except Exception:
        return ""


def _match_rule(pattern: str, action: str, target: str) -> bool:
    """规则匹配：``tool:shell``、``cmd:git push``、``path:**/.ssh/*``、或裸通配。

    各前缀的语义：
    - ``tool:`` / ``action:`` —— 按工具名匹配（支持通配）。
    - ``cmd:`` / ``command:`` —— 匹配命令。**没写通配符时按前缀匹配**，
      所以 ``cmd:git push`` 能命中 ``git push origin main``（对齐常见 agent 的直觉）。
    - ``path:`` / ``file:`` —— 匹配路径。没写通配符时按**子串**匹配。
    - ``risk:`` —— 在目标文本里找关键词。
    - 无前缀：含通配符则整体 fnmatch，否则按子串/等值匹配。
    """
    pat = pattern.strip()
    if not pat:
        return False
    if ":" in pat:
        kind, _, rest = pat.partition(":")
        kind = kind.lower()
        rest = rest.strip()
        if kind in ("tool", "action"):
            return fnmatch.fnmatch(action.lower(), rest.lower())
        if kind in ("cmd", "command"):
            t, r = target.lower().strip(), rest.lower()
            if re.search(r"[*?\[]", r):
                return fnmatch.fnmatch(t, r)
            # 无通配：前缀匹配（写 `cmd:git push` 就能拦住 `git push origin main`）
            return t == r or t.startswith(r)
        if kind in ("path", "file"):
            t = target.replace("\\", "/").lower()
            r = rest.replace("\\", "/").lower()
            if re.search(r"[*?\[]", r):
                return fnmatch.fnmatch(t, r)
            return r in t
        if kind == "risk":
            return rest.lower() in target.lower()
    if re.search(r"[*?\[]", pat):
        return fnmatch.fnmatch(target.lower(), pat.lower()) or fnmatch.fnmatch(action.lower(), pat.lower())
    return pat.lower() in target.lower() or pat.lower() == action.lower()


def build_gate(config: PermissionsConfig, *, workspace: str | None = None,
               audit: AuditStore | None = None,
               on_request: Callable[[ApprovalRequest], None] | None = None) -> ApprovalGate:
    return ApprovalGate(config, workspace=workspace, audit=audit, on_request=on_request)


__all__ = ["ApprovalGate", "ApprovalRequest", "Decision", "build_gate", "validate_rules"]
