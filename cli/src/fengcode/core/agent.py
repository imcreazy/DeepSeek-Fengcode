"""编排内核：把一次用户输入变成"思考 → 调用工具 → 观察 → 再思考"的完整回合。

职责
----
- 组装消息（系统提示 + 历史 + 记忆 + 当前输入）
- 上下文预算管理（超限自动压缩）
- 路由（直接回答 / 用工具 / 拆子任务）
- ReAct 循环（模型调工具 → 执行 → 回灌结果 → 继续）
- 反思（一步做完自检是否需要纠偏）
- 事件广播（界面实时呈现）
- 自动记忆（从对话中抽取值得记住的事实）

不做：具体工具实现、模型协议细节、界面渲染。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config.manager import get_manager
from ..events import Ev, get_bus
from ..llm.types import LLMResponse, Message, StreamEvent, ToolCall, ToolSpec
from ..memory.summarizer import (
    TRIGGER_HARD,
    TRIGGER_SOFT,
    detect_trigger,
    drop_superseded_blocks,
    extract_facts,
    mask_observations,
    render_skeleton,
    should_compact,
    should_proceed,
    split_for_compaction,
    summarize,
    summarize_chunked,
    summary_is_smaller,
)
from ..security.approval import ApprovalGate
from ..security.paths import PathGuard
from ..security.sandbox import LocalSandbox
from ..storage.sessions import SessionStore
from ..storage.stats import AuditStore, StatsStore
from ..storage.tasks import KVStore, TaskStore
from ..tools.base import ToolContext, ToolResult, get_registry
from ..utils import estimate_tokens, new_id, truncate, truncate_middle
from .prompts import SUBMIT_CHECKLIST, build_system_prompt, normalize_mode

# 某些模型会在正文里"表演"工具调用（XML/JSON 风格），需要兜底解析
_FAKE_TOOL_PATTERNS = [
    re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S),
    re.compile(r"<\|tool_call\|>\s*(\{.*?\})", re.S),
    re.compile(r"```(?:json)?\s*(\{\s*\"(?:name|tool)\"\s*:.*?\})\s*```", re.S),
]


@dataclass
class TurnResult:
    """一个回合的结果。"""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    # 最后一次上游调用的用量。★ 一个回合里每轮工具循环都会把「完整上下文」重算一遍，
    # 累加 prompt_tokens 等于把同一份上下文重复计十几次 —— 实测界面 250K、
    # 上游后台只有 20K，就是这个原因。「上下文占用」必须以最后一次调用为准。
    last_usage: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    currency: str = "¥"
    duration: float = 0.0
    error: str | None = None
    stopped: bool = False
    compacted: bool = False
    # 是否因输出上限被截断过（已自动续写；界面可据此提示用户调大上限）
    truncated: bool = False
    # ★ 2-O：自动续写了多少次。让「续跑」这件事**可见**：
    #   用户能知道这条回复是分几段接起来的、以及是不是该调大输出上限了。
    continues: int = 0
    mode: str = ""
    # ★ 流式模式下的重复交付防护：这段 content 在流式阶段已经逐字发给前端并渲染完成，
    #   前端据此只做收尾（改 meta、清 streaming），不再整段重绘 ——
    #   否则同一段回答会在界面上出现两遍（实测「发你好输出两次、重启才变一次」）。
    content_streamed: bool = False
    # ★ 完成回执：回合结束时告诉用户「这轮到底改了什么、验证没验证、还差什么」。
    #   为什么要有：用户看不出 AI 是不是真做完了、改的是哪些文件（实测反馈
    #   「他说写好了，我去桌面找不到」）。三样东西都来自本回合的真实执行记录，
    #   不是模型自述 —— 所以它比正文里那句话可信。
    changed_files: list[str] = field(default_factory=list)   # 本回合被写/改的文件（真实路径）
    verify_commands: list[str] = field(default_factory=list)  # 本回合跑过的测试/校验类命令
    gaps: list[str] = field(default_factory=list)             # 已知没做完的项

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "reasoning": self.reasoning,
            "tool_calls": self.tool_calls,
            "steps": self.steps,
            "usage": self.usage,
            "last_usage": self.last_usage,
            "cost": round(self.cost, 6),
            "currency": self.currency,
            "duration": round(self.duration, 3),
            "error": self.error,
            "stopped": self.stopped,
            "compacted": self.compacted,
            "truncated": self.truncated,
            "continues": self.continues,
            "mode": self.mode,
            "content_streamed": self.content_streamed,
            # ★ 完成回执三件套（界面据此渲染回执卡）
            "changed_files": self.changed_files,
            "verify_commands": self.verify_commands,
            "gaps": self.gaps,
        }


class Agent:
    """一个会话的执行体。"""

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        session_id: str | None = None,
        config: Any = None,
    ) -> None:
        from .. import paths
        from ..config import get_config

        self.config = config or get_config()
        self.manager = get_manager()
        self.workspace = Path(workspace) if workspace else paths.workspace_dir()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.bus = get_bus()

        # 存储
        from ..storage.db import get_db

        self.db = get_db()
        self.sessions = SessionStore(self.db)
        self.stats = StatsStore(self.db)
        self.audit = AuditStore(self.db)
        self.tasks = TaskStore(self.db)
        self.kv = KVStore(self.db)

        # 记忆
        from ..memory.manager import get_memory

        self.memory = get_memory(self.config.memory)
        self.memory.bind_config_manager(self.manager)

        # 技能
        from ..skills.manager import get_skills

        self.skills = get_skills(self.config, self.db)

        # LLM
        from ..llm.router import get_llm

        self.llm = get_llm(self.manager)

        # 安全
        self.guard = PathGuard(
            workspace=self.workspace,
            write_paths=self.config.permissions.write_paths,
            read_paths=self.config.permissions.read_paths,
            deny_patterns=self.config.permissions.deny_patterns or None,
            mode=self.config.permissions.mode,
        )
        self.approval = ApprovalGate(
            self.config.permissions,
            workspace=str(self.workspace),
            audit=self.audit,
            on_request=self._on_approval,
        )
        self.sandbox = LocalSandbox(
            cwd=self.workspace,
            timeout=float(self.config.tools.shell_timeout_seconds),
            max_output=int(self.config.tools.max_output_chars),
            network=bool(self.config.sandbox.network),
        )

        # 工具
        self.registry = get_registry()
        if not self.registry.all():
            from ..tools import register_builtin

            register_builtin(self.registry)
        for name in self.config.tools.disabled or []:
            self.registry.disable(name)

        # 子智能体运行器（延迟创建，避免循环导入）
        self._subagent_runner: Any = None
        # 提问通道（由 Server 注入；CLI 可另设）。None = 无交互，ask_user 会直接返回说明。
        self._asker: Any = None

        # 会话
        self.session_id = session_id or new_id("s")
        if self.sessions.get(self.session_id) is None:
            self.sessions.create(
                title="新对话", workspace=str(self.workspace),
                model=self.manager.default_model_ref(), session_id=self.session_id,
            )

        self._cancel = asyncio.Event()
        self._step_count = 0
        self._checklist_done = False
        # ★ 上下文阈值提醒：75% / 92% 各提醒一次，不重复打扰。
        #   记在实例上、每回合开始时清空，保证「同一档位每回合最多提示一次」。
        self._budget_notified: set[str] = set()

    # ------------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------------
    @property
    def subagent_runner(self) -> Any:
        if self._subagent_runner is None:
            from ..agents.runner import SubAgentRunner

            self._subagent_runner = SubAgentRunner(agent=self)
        return self._subagent_runner

    def tool_context(self, session_id: str | None = None, **extra: Any) -> ToolContext:
        # ★ 注入提问通道：Server（Web/桌面端）会设置 self._asker，
        #   ask_user 工具据此把问题发给界面并**等待**用户选择。
        #   没有它时 ask_user 只能回一句「当前没有交互通道」——实测过这个空转。
        if self._asker is not None and "asker" not in extra:
            extra["asker"] = self._asker
        return ToolContext(
            workspace=self.workspace,
            config=self.config,
            sandbox=self.sandbox,
            guard=self.guard,
            approval=self.approval,
            bus=self.bus,
            session_id=session_id or self.session_id,
            memory=self.memory,
            skills=self.skills,
            llm=self.llm,
            db=self.db,
            agent=self,
            subagent_runner=self.subagent_runner,
            extra=extra,
            cancel=self._cancel,
        )

    def tool_specs(self, *, allow: list[str] | None = None,
                   deny: list[str] | None = None) -> list[ToolSpec]:
        specs = self.registry.specs(allow=allow, deny=deny)
        # MCP 工具
        try:
            from ..mcp.client import MCPManager  # noqa: F401

            mcp = getattr(self, "_mcp", None)
            if mcp is not None:
                specs.extend(mcp.specs())
        except Exception:
            pass
        # 插件工具
        try:
            plugins = getattr(self, "_plugins", None)
            if plugins is not None:
                specs.extend(plugins.tool_specs())
        except Exception:
            pass
        # ★ 必须按名字去重：插件工具既会经 registry 注册进来（PluginManager 建时传了
        #   registry），又会由上面的 plugins.tool_specs() 再追加一次 —— 不去重就会把
        #   同一个工具名发两遍，上游直接报 400 `Tool names must be unique`。
        #   保留首次出现的（registry 中的那份），顺序稳定。
        seen: set[str] = set()
        unique: list[ToolSpec] = []
        for s in specs:
            if s.name in seen:
                continue
            seen.add(s.name)
            unique.append(s)
        return unique

    async def build_messages(
        self,
        user_input: str | None,
        *,
        session_id: str | None = None,
        history: list[Message] | None = None,
        extra_context: str = "",
        subagent: str = "",
        system_override: str | None = None,
        skip_memory: bool = False,
        mode: str | None = None,
        attachments: list[Any] | None = None,
    ) -> list[Message]:
        """组装发送给模型的消息列表（含记忆召回与预算控制）。"""
        sid = session_id or self.session_id
        cfg = self.config

        if history is None:
            history = self.sessions.messages(
                sid, limit=int(cfg.memory.short_term_max_turns) * 4
            )

        # ---- 动态块 ----
        memory_block = ""
        if not skip_memory and cfg.memory.enabled and user_input:
            try:
                memory_block = await self.memory.context_block(
                    user_input, session_id=sid, top_k=cfg.memory.recall_top_k
                )
            except Exception:
                memory_block = ""
        skill_catalog = ""
        try:
            skill_catalog = self.skills.catalog_block(user_input or "")
        except Exception:
            skill_catalog = ""
        todo_block = ""
        try:
            items = self.tasks.list(session_id=sid)
            if items:
                todo_block = self.tasks.render(sid)
        except Exception:
            pass
        goal_block = ""
        try:
            g = self.tasks.get_goal_for(sid)
            if g:
                goal_block = (
                    f"目标：{g['objective']}\n阶段：{g['phase']}　已完成轮次：{g['rounds']}"
                    + (f"/{g['max_rounds']}" if g.get("max_rounds") else "")
                )
        except Exception:
            pass
        notes_block = ""
        try:
            notes_block = self.memory.working_block(sid)
        except Exception:
            pass

        session = self.sessions.get(sid) or {}
        provider, model = self.manager.parse_model_ref(
            session.get("model") or self.manager.default_model_ref()
        )
        # 工作模式：显式传入优先，否则取会话里存的值（'' = 不选，AI 自主判断）
        effective_mode = mode if mode is not None else session.get("mode")
        system_prompt = system_override or build_system_prompt(
            config=cfg,
            workspace=str(self.workspace),
            model=model,
            provider=provider,
            tools=[s.name for s in self.tool_specs()],
            skill_catalog=skill_catalog,
            memory_block=memory_block,
            notes_block=notes_block,
            todo_block=todo_block,
            goal_block=goal_block,
            session_title=str(session.get("title") or ""),
            subagent=subagent,
            mode=normalize_mode(effective_mode),
            extra=extra_context,
        )

        msgs: list[Message] = [Message.system(system_prompt)]
        msgs.extend(m for m in history if m.role != "system")
        # ★ 缓存优化：把「当前时间」这类每轮都变的信息放在**尾部**（而不是系统提示里）。
        #   前缀保持逐字节稳定 → provider 的 prompt cache 才能命中（输入便宜 ~90%）。
        #   做法：静态内容集中在前、动态内容一律后置，时间提醒作为尾部独立消息注入。
        try:
            import datetime as _dt

            _now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
            msgs.append(Message.system(f"<system-reminder>当前时间：{_now}</system-reminder>"))
        except Exception:
            pass
        if user_input:
            # ★ 去重：run() 已经把这条用户消息 append 进会话了（history 里就有），
            #   这里再 append 一次会把**同一条消息发给模型两遍** —— 实测 user 消息
            #   条数为 2，既白烧 token，也可能让模型以为用户说了两次。
            #   命中同一条时复用 history 里那条，并把本次附件（含图片 data）补挂上去：
            #   history 里的附件只带 path，网页版没有 path 就得靠 data。
            last_user = None
            for _m in reversed(msgs):
                if _m.role == "user":
                    last_user = _m
                    break
            if last_user is not None and last_user.content == user_input:
                if attachments:
                    last_user.attachments = list(attachments)
            else:
                msgs.append(Message.user(user_input, attachments=list(attachments or [])))
        return await self._fit_budget(msgs, session_id=sid)

    def _context_budget(self, session_id: str) -> tuple[int, bool]:
        """返回 (上下文窗口 token 数, 是否「未限制」)。

        ★ 「未限制」的判定必须基于「模型是否真声明过窗口」，不能看兜底值：
        `model_info()` 在没有 catalog/供应商声明时会给个出厂的 128000，
        拿它去做压缩阈值，等于凭空给用户加了一道 128K 的隐形上限 ——
        要求「不填即不限，让上游自己限制」。
        因此：用户没在设置里填、模型与供应商也都没声明 → 未限制，
        本地不做压缩、界面显示「未限制」，一切交给上游。
        """
        cfg = self.config
        provider, model = self.manager.parse_model_ref(
            (self.sessions.get(session_id) or {}).get("model")
            or self.manager.default_model_ref()
        )
        info = self.manager.model_info(model or "", self.manager.get_provider(provider))
        if cfg.llm.context_window_override:
            return int(cfg.llm.context_window_override), False
        # ★ 「未限制」只保留一种情形：模型能力里确实取不到窗口（win <= 0）。
        #   旧写法把 source == "fallback" 整体判为「未限制 / 交给上游限制」，
        #   而 fallback 恰恰是「用户没填、内置表也没有」的常态 ——
        #   于是界面长期显示「未限制」，看起来像没有限额（实测反馈）。
        #   现在默认值改为 1M 且来源记为 default，这里按实际数值判断即可。
        win = int(info.get("context_window") or 0)
        unlimited = win <= 0
        # ★ 双阈值：除了「窗口 × compact_ratio」，
        #   再叠加一个「经济上限」context_soft_limit_tokens（默认 160000），两者取先到者。
        #   做法是把等效窗口压到 soft / ratio —— 这样 should_compact 里的
        #   window × ratio 正好等于 soft。否则 1M 窗口的触发点是 838K，
        #   真实会话永远到不了，压缩一次都不会发生（实测就是这个问题）。
        ratio = float(cfg.llm.compact_ratio or 0.8)
        soft = int(getattr(cfg.llm, "context_soft_limit_tokens", 160_000) or 0)
        if soft > 0 and ratio > 0:
            eff = int(soft / ratio)
            win = min(win, eff) if win > 0 else eff
            unlimited = False
        return win, unlimited

    async def _fit_budget(self, msgs: list[Message], *, session_id: str) -> list[Message]:
        """上下文预算控制：按 HARD/SOFT 触发决定压缩策略。"""
        cfg = self.config
        window, unlimited = self._context_budget(session_id)
        if unlimited or window <= 0:
            # 未限制：不做任何本地压缩，交给上游
            return msgs
        trigger, tokens = detect_trigger(
            msgs, context_window=window, ratio=float(cfg.llm.compact_ratio)
        )
        if trigger is None:
            return msgs
        if trigger == TRIGGER_HARD:
            # 硬触发：必须先摘掉冗长的旧工具输出把体积压下来，
            # 否则连摘要请求本身都可能超窗失败
            self.bus.emit(
                Ev.STATUS,
                {"stage": "compact", "message": f"上下文逼近上限（约 {tokens} tokens），紧急整理中…"},
                session_id=session_id,
            )
            if cfg.llm.mask_observations:
                # ★ 按 token 的保留量（0 = 用内置默认）。字符与真实占用差得远：
                #   中文一字约 0.6 token、英文一字符约 0.25 token，同一个数字
                #   对中英文的实际效果完全不同 —— 按 token 才与上下文预算同一把尺子。
                msgs = mask_observations(
                    msgs,
                    keep_recent=int(cfg.llm.keep_recent_turns) * 2,
                    max_tokens=max(0, int(getattr(cfg.llm, "mask_keep_tokens", 0) or 0)),
                )
        return await self.compact(msgs, window=window, session_id=session_id, tokens=tokens)

    async def compact(
        self, msgs: list[Message], *, window: int, session_id: str, tokens: int = 0,
        force: bool = False,
    ) -> list[Message]:
        """压缩中段消息为摘要，保留系统提示、初始目标与最近几轮。

        压缩策略（分块摘要 + 观测遮罩）：
        - ``keep_first``：**永远原样保留**会话最开头的几条消息（系统提示 + 用户
          最初的诉求）。那是本次会话的"锚点"，压缩掉会导致模型忘记原始目标。
        - ``minimum_progress``：压缩收益不足 10% 时放弃，因为一次摘要调用
          的成本可能高于省下的 token，还会破坏 prompt 缓存。
        - 执行骨架：摘要里附带"调用过哪些工具、成败如何"，避免模型重走弯路。
        """
        cfg = self.config
        keep_recent = max(2, int(cfg.llm.keep_recent_turns))
        keep_first = max(1, int(cfg.llm.keep_first_messages))
        # ★ 结构化压缩：先丢掉「已被后续同款块取代」的旧状态块。
        #   为什么放在切分**之前**：这些旧副本（早前的 todo/计划/轨迹）本来就是冗余，
        #   先扔掉能让待压缩段变小、摘要请求更省；而且丢掉的是「确知被取代」的内容，
        #   不是自由正文，不存在误伤。
        msgs = drop_superseded_blocks(msgs)
        systems, middle, recent = split_for_compaction(
            msgs, keep_recent=keep_recent, keep_first=keep_first
        )
        if not middle:
            return msgs

        before_tokens = sum(
            estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "")
            for m in msgs
        )
        self.bus.emit(
            Ev.STATUS,
            {"stage": "compact", "message": f"上下文超过预算（约 {tokens or before_tokens} tokens），正在压缩历史…"},
            session_id=session_id,
        )
        # ★ 摘要调用加整体超时。
        #   若摘要失败/超时：`summarize` 内部会返回 fallback 摘要并带 ok=False，
        #   **绝不退回有损截断** —— 保底是「原样返回 msgs」，让这一轮照常跑，
        #   下轮再试压缩，而不是把历史切掉一半导致模型失忆。
        # ★ 摘要调用复用主对话的缓存前缀（省一次全价调用）。
        #   把「主对话开头的系统提示 + 工具定义」原样放在最前，压缩指令缀在最后，
        #   这次请求就成了主对话请求的一个真前缀，可直接命中上游前缀缓存。
        #   工具定义必须一起带：它在请求体前缀里，漏掉就与主对话的字节对不上。
        cache_prefix = list(systems)
        try:
            cache_tools = self.tool_specs()
        except Exception:
            cache_tools = []
        try:
            result = await asyncio.wait_for(
                # ★ 2-B：历史长时走「分块重放 + 树形归并」，避免一次摘要吃不下整段
                #   导致**整段压缩失败**（早前的行为：失败就全留着）。
                summarize_chunked(
                    middle, self.llm, model=None, max_words=800,
                    chunk_tokens=int(getattr(cfg.llm, "compact_chunk_tokens", 24000) or 24000),
                    prefix=cache_prefix, tools=cache_tools,
                ),
                timeout=300.0,
            )
        except asyncio.TimeoutError:
            self.bus.emit(
                Ev.LOG,
                {"level": "warn", "message": "摘要生成超时（>300 秒），本轮跳过压缩、保留完整上下文"},
                session_id=session_id,
            )
            return msgs
        except Exception as e:
            self.bus.emit(
                Ev.LOG,
                {"level": "warn", "message": f"摘要生成失败（{type(e).__name__}），本轮跳过压缩、保留完整上下文"},
                session_id=session_id,
            )
            return msgs
        summary_text = result.get("text") or ""
        # 附执行骨架（不占多少 token，但能避免重复无效尝试）
        skeleton = render_skeleton(middle) if cfg.llm.keep_skeleton else ""
        body = summary_text
        if skeleton:
            body += f"\n\n【执行轨迹】\n{skeleton}"
        summary_msg = Message(
            role="system",
            content=(
                "<compacted_history>\n"
                "以下是本次会话早前内容的压缩摘要（原始消息已折叠，"
                "需要细节时可用工具重新查看）：\n\n"
                f"{body}\n</compacted_history>\n"
                "（说明：以上压缩是系统**自动维护**上下文的结果，"
                "**当前任务尚未结束**，请照原目标继续推进，不要因此停下、"
                "也不要重新开始或交还任务。）"
            ),
            meta={"compacted": True, "original_messages": len(middle)},
        )
        candidate = [*systems, summary_msg, *recent]

        # ★ 摘要净收益校验：摘要本身必须比被它顶替掉的内容短。
        #   为什么单靠下面的 should_proceed 不够：那比的是「整段压缩前后的总账」，
        #   只保证总体变小。但摘要完全可能比它顶替的内容还长（写得啰嗦、或原内容
        #   本来就不长），此时这次摘要调用纯属白花钱，还把信息换成了更长的一段。
        #   强制压缩（用户主动点的）不受此限 —— 他要的是「看到效果」。
        summary_tok = estimate_tokens(summary_msg.content or "")
        shadowed_tok = sum(
            estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "")
            for m in middle
        )
        small_ok, small_why = summary_is_smaller(summary_tok, shadowed_tok)
        if not small_ok and not force:
            self.bus.emit(
                Ev.LOG,
                {"level": "info",
                 "message": f"{small_why}，本次不压缩（保留原文，下轮再试）"},
                session_id=session_id,
            )
            return msgs

        # 收益检查：缩减不足 minimum_progress 就放弃（除非强制）
        after_tokens = sum(
            estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "")
            for m in candidate
        )
        worth, progress = should_proceed(
            before_tokens, after_tokens, minimum_progress=float(cfg.llm.minimum_progress)
        )
        if not worth and not force:
            self.bus.emit(
                Ev.LOG,
                {"level": "info",
                 "message": f"压缩收益仅 {progress * 100:.1f}%（阈值 {cfg.llm.minimum_progress * 100:.0f}%），已跳过以免浪费调用"},
                session_id=session_id,
            )
            return msgs
        # 抽取事实进长期记忆
        try:
            joined = "\n".join(m.content or "" for m in middle if m.role == "user")
            for fact in extract_facts(joined, limit=3):
                await self.memory.remember(
                    fact, kind="fact", session_id=session_id, source="auto-compact",
                    importance=0.5,
                )
        except Exception:
            pass
        if not result.get("ok"):
            self.bus.emit(
                Ev.LOG,
                {"level": "warn", "message": f"摘要生成降级：{result.get('error')}"},
                session_id=session_id,
            )
        return [*systems, summary_msg, *recent]

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def run(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        tools: list[str] | None = None,
        stream: bool = True,
        max_steps: int | None = None,
        extra_context: str = "",
        mode: str | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        attachments: list[Any] | None = None,
    ) -> TurnResult:
        """执行一个完整回合。

        ``mode``：工作模式，``""``/``None`` 表示不指定（由模型自主判断），
        ``"plan"`` 强制计划模式，``"goal"`` 强制目标模式。
        """
        sid = session_id or self.session_id
        t0 = time.time()
        result = TurnResult()
        self._cancel.clear()
        self._step_count = 0
        self._checklist_done = False
        self._budget_notified = set()   # 新回合：阈值提醒重新计数（每档每回合最多一次）
        cfg = self.config
        limit = int(max_steps or cfg.agent.max_steps)

        # 工作模式：显式传入则写回会话，供后续轮次与前端读取
        if mode is not None:
            normalized = normalize_mode(mode)
            try:
                self.sessions.update(sid, mode=normalized)
            except Exception:
                pass
        else:
            normalized = normalize_mode((self.sessions.get(sid) or {}).get("mode"))
        result.mode = normalized

        self.bus.emit(Ev.TURN_START, {"input": truncate(user_input, 500)}, session_id=sid)

        # 记录用户消息（★ 附件一并挂上：图片/文件随消息进库与进模型）
        atts = list(attachments or [])
        user_msg = Message.user(user_input, attachments=atts)
        self.sessions.append(sid, user_msg)
        self.memory.working_append(sid, user_msg)
        self.sessions.auto_title(sid, user_input)

        # 组装消息（可能触发压缩）
        try:
            msgs = await self.build_messages(
                user_input, session_id=sid, extra_context=extra_context,
                mode=normalized, attachments=atts,
            )
        except Exception as e:
            result.error = f"组装上下文失败：{type(e).__name__}: {e}"
            self.bus.emit(Ev.ERROR, {"error": result.error}, session_id=sid)
            return result

        specs = self.tool_specs(allow=tools)
        ctx = self.tool_context(sid)
        step = 0
        final_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # ★ 流式期间已经逐字发给前端的正文（按步累积）。
        #   收尾时 result.content 会把它再返回一次，而前端在流式阶段已经渲染过 ——
        #   这正是「同一段回答在界面上出现两遍、还挂着『补充（连接中断后从会话记录恢复）』」
        #   的根因。这里记下来，收尾时若 content 与「已流式交付的文字」一致，就不再重复交付。
        streamed_text_len = 0
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                       "cached_tokens": 0, "reasoning_tokens": 0, "cache_miss_tokens": 0}
        # 最后一次调用的用量（上下文占用 / 命中率 / 输出速度都用它，不用累加值）
        last_usage: dict[str, int] = {}
        cost_sum = 0.0
        currency = "¥"
        # ★ 无进展防护按「实际进展」评估（实测痛点：认真调研的任务被固定轮数硬掐断）。
        #   旧写法是 `while step < limit` —— 跑到第 50 步无条件停，哪怕模型一直在
        #   读新文件、拿到新证据、稳步推进。
        #   新规则：**只要出现新证据就把计数归零**，只有「连续多轮没有任何新证据」
        #   才判定为绕圈，提前收尾并说明原因（既不放任死循环，也不误杀调研）。
        stall = 0                       # 连续无新证据的轮数
        stall_limit = 8                 # 连续这么多轮没有新证据 → 提前收尾
        seen_evidence: set[str] = set()  # 去重后的证据指纹（工具结果摘要）

        while step < limit:
            if self._cancel.is_set():
                result.stopped = True
                break
            step += 1
            self._step_count = step
            self.bus.emit(Ev.STEP, {"step": step, "max": limit}, session_id=sid)

            # ---- 调用模型 ----
            resp: LLMResponse | None = None
            step_text = ""       # 本轮产生的正文
            step_reason = ""     # 本轮产生的思考
            new_evidence = False  # 本步是否产生新证据（无进展防护用它判定是否绕圈）
            try:
                if stream:
                    resp, ev_text, ev_reason = await self._stream_once(
                        msgs, specs, sid, model=model, on_event=on_event
                    )
                    step_text, step_reason = ev_text, ev_reason
                else:
                    resp = await self._call_once(msgs, specs, model=model)
                    step_text, step_reason = resp.content or "", resp.reasoning or ""
            except Exception as e:
                result.error = f"模型调用失败：{type(e).__name__}: {e}"
                self.bus.emit(Ev.ERROR, {"error": result.error}, session_id=sid)
                break

            if resp is None:
                result.error = "模型没有返回结果"
                break
            if step_text:
                final_text_parts.append(step_text)
                # 流式模式下这段正文已逐字发给前端；记下长度用于收尾时判断是否重复交付。
                if stream:
                    streamed_text_len += len(step_text)
                # ★ 重复输出防护（实测：模型陷入「我现在写。好。写。输出。」无限重复）。
                #   为什么单独一条判据：现有的「无进展防护」只看工具轮次，
                #   而纯文本打转时模型每一轮都在「写新内容」，stall 永远归零 ——
                #   于是它能一直重复下去，直到步数或输出上限才停。
                #   判据：把最近若干段的正文拼起来，如果**尾部以固定周期反复重复**
                #   （即刚写出的这段，前面已经原样写过好几轮），就是打转；
                #   此时提前收尾并说明原因（不静默截断）。
                repeat_rounds = _count_runaway_repeat(final_text_parts)
                if repeat_rounds >= 5:
                    self.bus.emit(
                        Ev.LOG,
                        {"level": "warn",
                         "message": f"检测到正文在重复（同一段内容重复 {repeat_rounds} 轮），提前收尾"},
                        session_id=sid,
                    )
                    result.gaps.append(
                        f"模型出现重复输出（同一段内容重复 {repeat_rounds} 轮），已提前收尾"
                    )
                    break
            if step_reason:
                reasoning_parts.append(step_reason)
            # ★ 流式期间被用户中断：立刻收尾，不要再执行工具、不要再发起下一轮。
            if self._cancel.is_set():
                result.stopped = True
                break

            # 用量累计
            if resp.usage:
                u = resp.usage
                # ★★ 只有**真的拿到用量**才更新快照（覆盖式记录：界面要的是「此刻上下文有多大」）。
                #   为什么必须判定：调用失败（连接断开、上游没发 usage 事件）时 resp.usage 是
                #   一个**全 0 的空对象**，而 dataclass 没有 __bool__ → `if resp.usage:` 对它
                #   同样成立 —— 于是「读失败的全 0」覆盖掉了几十万的真实读数并落库，
                #   界面就一直显示 0，退出重进也还是 0（实测：库里 input_tokens=463905，
                #   而 meta.last_usage 全是 0）。
                #   这与前端 usage 分支的 hasFresh 判定是同一个道理，两端必须一致。
                if u.has_data():
                    last_usage = {
                        "prompt_tokens": u.prompt_tokens,
                        "completion_tokens": u.completion_tokens,
                        "total_tokens": u.total_tokens or (u.prompt_tokens + u.completion_tokens),
                        "cached_tokens": u.cached_tokens,
                        "reasoning_tokens": u.reasoning_tokens,
                        # ★ 未命中量：命中率的分母用它 + cached，口径才可核对
                        "cache_miss_tokens": u.cache_miss_tokens,
                    }
                total_usage["prompt_tokens"] += u.prompt_tokens
                total_usage["completion_tokens"] += u.completion_tokens
                total_usage["total_tokens"] += u.total_tokens or (u.prompt_tokens + u.completion_tokens)
                total_usage["cached_tokens"] += u.cached_tokens
                total_usage["reasoning_tokens"] += u.reasoning_tokens
                total_usage["cache_miss_tokens"] += u.cache_miss_tokens
            # 成本
            try:
                from ..llm.router import compute_cost

                provider_name, model_name = self.manager.parse_model_ref(
                    model or self.manager.default_model_ref()
                )
                prov = self.manager.get_provider(provider_name)
                info = self.manager.model_info(model_name or "", prov)
                amount, cur = compute_cost(resp.usage, info.get("price") or {})
                cost_sum += amount
                currency = cur or currency
            except Exception:
                pass
            try:
                self.stats.record(
                    provider=resp.provider or "", model=resp.model or "",
                    usage=resp.usage, cost=cost_sum, currency=currency,
                    duration=time.time() - t0, session_id=sid, kind="chat", error=resp.error,
                )
            except Exception:
                pass

            if resp.error:
                result.error = resp.error
                self.bus.emit(Ev.ERROR, {"error": resp.error}, session_id=sid)
                # 有部分文本也返回给用户
                if not final_text_parts:
                    break

            # ---- 兜底：模型把工具调用写在正文里 ----
            calls = resp.tool_calls
            if not calls and resp.content:
                calls, cleaned = _extract_fake_tool_calls(resp.content, specs)
                if calls:
                    resp.content = cleaned
                    if final_text_parts:
                        final_text_parts[-1] = cleaned
                    else:
                        final_text_parts.append(cleaned)

            # ---- 没有工具调用：检查是否需要先自检再收尾 ----
            if not calls:
                # ★ 输出被「输出上限」截断（finish_reason = length / max_tokens）：
                #   把已生成的内容写进历史，然后让模型**接着往下写**。
                #   否则用户看到的就是「思考到一半突然停」「回答说到一半就断」
                #   （实测根因：配置里残留了 max_tokens = 8192）。
                _fin = (resp.finish_reason or "").lower()
                if _fin in ("length", "max_tokens", "max_output_tokens") and step < limit:
                    if resp.content or resp.reasoning:
                        part = Message(role="assistant", content=resp.content or "",
                                       reasoning=resp.reasoning or "")
                        msgs.append(part)
                        self.sessions.append(sid, part)
                    msgs.append(Message.system(
                        "上一条回复因达到输出长度上限而被截断。请**从中断处接着写**，"
                        "不要重复已经写过的内容，也不要重新开头。"
                    ))
                    result.truncated = True
                    result.continues += 1   # ★ 2-O：续跑次数，界面据此显示「已自动续写 N 次」
                    self.bus.emit(
                        Ev.STATUS,
                        {"stage": "continue", "message": "输出达到上限，自动接着写完…"},
                        session_id=sid,
                    )
                    continue
                if (
                    cfg.agent.submit_checklist
                    and step < limit
                    and not self._checklist_done
                    and _needs_submit_check(msgs)
                ):
                    self._checklist_done = True
                    msgs.append(
                        Message.system(
                            SUBMIT_CHECKLIST
                            + "\n\n如果你确认全部无误，请直接给出最终答复；"
                            "如果发现需要修正的地方，现在就动手改。"
                        )
                    )
                    self.bus.emit(
                        Ev.STATUS, {"stage": "submit_check", "message": "执行提交前自检"}, session_id=sid
                    )
                    continue
                if resp.content and not final_text_parts:
                    final_text_parts.append(resp.content)
                if resp.reasoning and not reasoning_parts:
                    reasoning_parts.append(resp.reasoning)
                break

            # ---- 有工具调用：执行 ----
            assistant_msg = Message(
                role="assistant",
                content=resp.content or "",
                reasoning=resp.reasoning or "",
                tool_calls=calls,
            )
            msgs.append(assistant_msg)
            self.sessions.append(sid, assistant_msg)

            # ★ 关键：这一轮的正文只是「准备调用工具前的说明」（如"我来查一下…"），
            #   它已经作为 assistant 消息写进会话历史了。交付给用户的答复是**工具链
            #   之后**那一轮的正文；所以这里把已累积的正文/思考清空，只留最后一轮的。
            #   否则界面上同一个结论会被说两三遍（实测反馈：「重复三段」）。
            final_text_parts.clear()
            reasoning_parts.clear()

            # ★ 单轮并行只读工具调用：同一轮里的**只读**调用并发执行。
            #   为什么：模型常在一轮里发好几个互不依赖的读取（读 3 个文件、搜 2 个关键词），
            #   串行执行纯属白等 —— 实测墙钟有大幅改善空间。
            #   为什么只并行「只读」：写操作之间有顺序依赖（先建目录再写文件），
            #   并发会打乱因果关系；只读没有副作用，并发是安全的。
            #   结果顺序仍按 calls 原序回填，保证历史与模型看到的一致。
            readonly_flags = [self._is_readonly_call(c) for c in calls]
            parallel_ok = sum(readonly_flags) >= 2 and not any(
                self._cancel.is_set() for _ in [0]
            )
            results: list[ToolResult | None] = [None] * len(calls)
            if parallel_ok:
                # 只并发那些「只读」的调用；其余（写）保持原序串行执行
                idxs = [i for i, ro in enumerate(readonly_flags) if ro]
                gathered = await asyncio.gather(
                    *[self._execute_call(calls[i], ctx, sid) for i in idxs],
                    return_exceptions=True,
                )
                for i, res in zip(idxs, gathered):
                    if isinstance(res, BaseException):
                        results[i] = ToolResult.fail(f"{type(res).__name__}: {res}")
                    else:
                        results[i] = res

            for i, call in enumerate(calls):
                if self._cancel.is_set():
                    result.stopped = True
                    break
                tr = results[i] if results[i] is not None else await self._execute_call(call, ctx, sid)
                result.tool_calls.append(
                    {"name": call.name, "arguments": call.arguments, "ok": tr.ok,
                     "error": tr.error, "duration": round(tr.duration, 3),
                     # ★ 产出文件要带进回执卡：这是「本回合到底改了什么」的唯一真实来源
                     #   （工具自己回报的，不是模型自述）。
                     "files": list(tr.files or [])}
                )
                tool_msg = await self._tool_message(call, tr)
                msgs.append(tool_msg)
                self.sessions.append(sid, tool_msg)
                # ★ 判定「这一轮是否产生了新证据」：工具结果的内容指纹。
                #   取前 400 字符做指纹即可 —— 同一份文件重读两次内容相同，
                #   指纹一样就算「没有新证据」；读到新文件/新输出则算有进展。
                try:
                    fp = f"{call.name}|{(tr.content or '')[:400]}"
                    if fp not in seen_evidence:
                        seen_evidence.add(fp)
                        new_evidence = True
                except Exception:
                    pass

            # 每轮工具执行完就把最新用量广播出去：界面上的「上下文占用 / 输出速度」
            # 才能在回合进行中实时更新，而不是等整个回合结束才一次性跳变。
            # ★ 不再用 `if last_usage:` 包着：第一次调用还没成功就被用户停止时，
            #   last_usage 是空的 —— 若因此不发，界面会一直留着**上一轮**的旧读数
            #   （实测「发了新消息，右上角还是老数字 / 停在 0」）。
            #   发出去让前端按「有无数据」自行显示数字或占位符。
            self.bus.emit(
                Ev.USAGE,
                {"usage": total_usage, "last_usage": last_usage,
                 "cost": cost_sum, "currency": currency,
                 # ★ 缓存前缀归因：界面据此解释「这次为什么没命中」。
                 **({"diagnostics": resp.raw.get("cache_diagnostics")}
                    if resp is not None and getattr(resp, "raw", None)
                    and resp.raw.get("cache_diagnostics") else {})},
                session_id=sid,
            )

            if result.stopped:
                break

            # ★ 无进展防护：有进展就归零，连续绕圈才止损。
            #   进度信号 = 本步拿到了新证据（新工具结果）或写下了新内容。
            if new_evidence or (resp is not None and (resp.content or "").strip()):
                stall = 0
            else:
                stall += 1
            new_evidence = False
            if stall >= stall_limit:
                self.bus.emit(
                    Ev.LOG,
                    {"level": "warn",
                     "message": f"连续 {stall} 轮没有新进展（无新工具结果/无新结论），提前收尾"},
                    session_id=sid,
                )
                result.gaps.append(
                    f"连续 {stall} 轮没有新进展，已提前收尾（不是步数上限，而是判定为绕圈）"
                )
                break

            # ---- 上下文阈值提醒（75% / 92%，每档每回合只提示一次）----
            #   ★ 为什么要有：模型在长会话里会盲目重读文件，等到「临爆才压缩」，
            #     既费钱又容易触发上游截断。提前把「快满了」告诉它，它能自己收敛范围。
            try:
                win, unl = self._context_budget(sid)
                if not unl and win > 0:
                    used_now = int((last_usage or total_usage).get("prompt_tokens") or 0)
                    pct_now = used_now / win * 100 if win else 0.0
                    if pct_now >= 92 and "92" not in self._budget_notified:
                        self._budget_notified.add("92")
                        msgs.append(Message.system(
                            f"【上下文提示】当前已用 {pct_now:.0f}%（{used_now}/{win} tokens），"
                            "接近上限。请不要再大批量读取文件，先给出阶段性结论或收敛范围。"
                        ))
                        self.bus.emit(Ev.STATUS, {"stage": "ctx", "message": "上下文接近上限"},
                                      session_id=sid)
                    elif pct_now >= 75 and "75" not in self._budget_notified:
                        self._budget_notified.add("75")
                        msgs.append(Message.system(
                            f"【上下文提示】当前已用 {pct_now:.0f}%（{used_now}/{win} tokens）。"
                            "后续读取请注意控制量；如需大范围检索，先说明目的再动手。"
                        ))
            except Exception:
                pass

            # ---- 反思：是否需要纠偏 ----
            if cfg.agent.reflection and step >= 2 and step % 3 == 0:
                note = await self._reflect(msgs, sid)
                if note:
                    msgs.append(Message.system(note))

            # ---- 压缩检查 ----
            compacted = await self._maybe_compact(msgs, sid, result)
            if compacted:
                msgs = compacted
                result.compacted = True

        if step >= limit and not result.stopped:
            self.bus.emit(
                Ev.LOG,
                {"level": "warn", "message": f"已达最大步数 {limit}，可能还有未完成的工作"},
                session_id=sid,
            )

        # ---- 收尾 ----
        # ★ 交付文字只取「本回合流式累积的正文」。
        #   兜底分支（final_text_parts 为空时回捞历史）只在**本回合一个字都没产出**时使用，
        #   并且回捞到的文字若与已产出内容重复，绝不再拼一次 ——
        #   旧写法无条件 `content = self._last_assistant_text(msgs)`，会把历史里
        #   前几轮已经写过的 assistant 文字再拼进来，实测出现 content="收到收到收到"
        #   （模型分 2 步各说一次「收到」，界面渲染 2 段，后端却返回 3 段）。
        content = "".join(p for p in final_text_parts if p).strip()
        if not content:
            content = self._last_assistant_text(msgs) or ""
        reasoning = "".join(p for p in reasoning_parts if p).strip()
        # ★ 重复交付防护：流式模式下这段正文已逐字发给前端并渲染完成，
        #   若此处再作为 result.content 返回，前端会整段重绘 —— 表现为同一段回答
        #   在界面上出现两遍（实测「发你好输出两次、重启才变一次」，
        #   以及末轮调工具时出现的「补充（连接中断后从会话记录恢复）」重复块）。
        #   判据：流式确实发送过正文，且交付文字没有超出已发送的内容
        #   （用「已发送文本被 content 以相同前缀承载」判断，避免长度相等才成立的脆断言）。
        streamed_joined = "".join(p for p in final_text_parts if p).strip()
        already_streamed = bool(
            stream and streamed_joined and content and streamed_joined in content
        )
        result.content = content
        result.reasoning = reasoning
        result.steps = step
        result.usage = total_usage
        # ★ 上下文占用 / 命中率取「最后一次上游调用」的快照。
        #   不能回退到 total_usage：那是**本回合累加值**（一轮工具循环会把同一份
        #   上下文向上游重发十几次），拿它当占用会显示成远大于真实上下文的天文数字。
        #   上游一次都没返回用量时（例如刚发出去就被停止），这里是空 dict ——
        #   前端据此显示「— / 尚无调用记录」，而不是伪装成一个 0 或旧数字。
        result.last_usage = last_usage
        result.currency = currency
        result.duration = time.time() - t0
        # 告诉前端「这段正文你已经在流式阶段画过了」→ 前端据此只收尾、不重绘。
        result.content_streamed = already_streamed
        # ---- 完成回执（照 v1.21.5「完成回执：变更路径 / 验证命令 / 差距」）----
        # 三样东西全部来自本回合的**真实执行记录**，不是模型自述：
        #   · changed_files  —— 工具自己回报的产出文件（ToolResult.files）
        #   · verify_commands—— 本回合跑过的测试/校验类命令（按名字识别）
        #   · gaps           —— 明确的未完成信号（已达步数上限、工具有失败）
        try:
            seen_files: list[str] = []
            for tc in result.tool_calls:
                for f in (tc.get("files") or []):
                    if f and f not in seen_files:
                        seen_files.append(str(f))
            result.changed_files = seen_files
            verify_cmds: list[str] = []
            for tc in result.tool_calls:
                nm = str(tc.get("name") or "")
                args = tc.get("arguments") or {}
                if nm in _VERIFY_TOOLS:
                    cmd = ""
                    if isinstance(args, dict):
                        cmd = str(args.get("command") or args.get("cmd") or args.get("script") or "")
                    verify_cmds.append(cmd.strip() or nm)
            result.verify_commands = verify_cmds
            gaps: list[str] = []
            if step >= limit and not result.stopped:
                gaps.append(f"已达最大步数 {limit}，可能还有未完成的工作")
            failed = [tc.get("name") for tc in result.tool_calls if tc.get("ok") is False]
            if failed:
                uniq = []
                for f in failed:
                    if f and f not in uniq:
                        uniq.append(str(f))
                gaps.append("这些工具执行失败：" + "、".join(uniq))
            if result.stopped:
                gaps.append("回合被用户中断，未完成的部分没有继续")
            if result.error:
                gaps.append("回合出错：" + str(result.error))
            result.gaps = gaps
        except Exception:
            pass

        if content:
            # ★ 回执随消息落库（meta.receipt）：重开会话时前端按它把回执卡**回放**出来。
            #   只在前端即时插入的话，用户回头查「那轮到底改了哪些文件」就没了
            #   （流式卡片不落库 → 重开即消失，实测确认过）。
            receipt_meta = {}
            if result.changed_files or result.verify_commands or result.gaps or result.error:
                receipt_meta = {"receipt": {
                    "changed_files": result.changed_files,
                    "verify_commands": result.verify_commands,
                    "gaps": result.gaps,
                    "error": result.error,
                }}
            final_msg = Message(role="assistant", content=content, reasoning=reasoning,
                                meta=receipt_meta)
            self.sessions.append(sid, final_msg)
            self.memory.working_append(sid, final_msg)
        try:
            self.sessions.add_usage(sid, type("U", (), total_usage)(), cost_sum) if False else None
        except Exception:
            pass
        try:
            from ..llm.types import Usage

            self.sessions.add_usage(
                sid,
                Usage(
                    total_usage["prompt_tokens"], total_usage["completion_tokens"],
                    total_usage["total_tokens"], total_usage["cached_tokens"],
                    total_usage["reasoning_tokens"],
                ),
                cost_sum,
                # ★ 「上下文占用 / 命中率」用最后一次调用的用量，不能用上面的累计值：
                #   一轮工具循环会把同一份上下文向上游重发十几次，累计值等于重复
                #   计数同一份上下文。这个快照落进 sessions.meta，重开会话时据此恢复读数。
                last_usage=last_usage,
            )
        except Exception:
            pass

        # 自动记忆（仅在消息中明确出现「记住」类措辞时写入）
        await self._auto_remember(user_input, content, sid)
        # ★ 「重要任务要不要存进记忆」不再在这里弹窗：改由模型自己判断，
        #   并在最终答复的末尾用一句话问用户（见 prompts.SUBMIT_CHECKLIST 第 6 条）。
        #   为什么不在代码里问：用户明确要求「让 ai 自主判断然后在最终的输出里询问」，
        #   而不是弹一个需要点击的选项框。

        self.bus.emit(Ev.USAGE, {"usage": total_usage, "last_usage": last_usage,
                                 "cost": cost_sum, "currency": currency},
                      session_id=sid)
        self.bus.emit(Ev.TURN_END, {"result": result.to_dict()}, session_id=sid)
        return result

    # ------------------------------------------------------------------
    # 单次调用
    # ------------------------------------------------------------------
    async def _call_once(self, msgs: list[Message], specs: list[ToolSpec],
                         *, model: str | None) -> LLMResponse:
        ref = model or (self.sessions.get(self.session_id) or {}).get("model") or None
        return await self.llm.chat(msgs, model=ref, tools=specs or None)

    async def _stream_once(
        self, msgs: list[Message], specs: list[ToolSpec], sid: str, *,
        model: str | None, on_event: Callable[[StreamEvent], None] | None = None,
    ) -> tuple[LLMResponse, str, str]:
        """流式调用一次，边收边广播事件，返回完整响应。"""
        ref = model or (self.sessions.get(sid) or {}).get("model") or None
        text_parts: list[str] = []
        reason_parts: list[str] = []
        calls: list[ToolCall] = []
        usage = None
        finish = None
        error = None
        model_used = ""
        provider_used = ""
        # ★ 缓存前缀归因（由 usage 事件带回，见 router._diagnose）
        last_diag: dict[str, Any] | None = None

        try:
            async for ev in self.llm.chat_stream(msgs, model=ref, tools=specs or None):
                # ★ 用户点「停止」要立刻生效：_cancel 一置位就跳出流式接收。
                #   旧写法只在「步与步之间」检查，流式过程中完全不看 ——
                #   一次调用可能要跑几百秒（实测 651 秒），点停止像没反应。
                if self._cancel.is_set():
                    break
                if on_event is not None:
                    try:
                        on_event(ev)
                    except Exception:
                        pass
                if ev.type == "text" and ev.text:
                    text_parts.append(ev.text)
                    self.bus.emit(Ev.TEXT, {"text": ev.text}, session_id=sid)
                elif ev.type == "reasoning" and ev.text:
                    reason_parts.append(ev.text)
                    self.bus.emit(Ev.REASONING, {"text": ev.text}, session_id=sid)
                elif ev.type == "tool_call" and ev.tool_call is not None:
                    calls.append(ev.tool_call)
                elif ev.type == "tool_delta":
                    self.bus.emit(Ev.TOOL_DELTA, {"text": ev.text, "meta": ev.meta}, session_id=sid)
                elif ev.type == "usage" and ev.usage is not None:
                    usage = ev.usage
                    # ★ 缓存前缀归因随用量一起带到上层，供界面解释「为什么没命中」。
                    if getattr(ev, "diagnostics", None):
                        last_diag = ev.diagnostics
                elif ev.type == "done":
                    finish = ev.finish_reason
                    self.bus.emit(Ev.DONE, {"finish_reason": finish}, session_id=sid)
                elif ev.type == "error":
                    error = ev.error
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        try:
            provider_name, model_name = self.manager.parse_model_ref(
                ref or self.manager.default_model_ref()
            )
            provider_used = provider_name or ""
            model_used = model_name or ""
        except Exception:
            pass

        from ..llm.types import Usage

        resp = LLMResponse(
            content="".join(text_parts),
            reasoning="".join(reason_parts),
            tool_calls=calls,
            usage=usage or Usage(),
            finish_reason=finish,
            model=model_used,
            provider=provider_used,
            error=error,
        )
        # ★ 缓存前缀归因随响应带回调用方（存 raw 里，不改返回签名）。
        #   为什么放 raw 而不是新增字段：raw 本就是「供应商原始信息的落脚处」，
        #   诊断属于同类的旁路信息，放这里不会牵动 LLMResponse 的既有用法。
        if last_diag:
            try:
                resp.raw["cache_diagnostics"] = last_diag
            except Exception:
                pass
        return resp, resp.content, resp.reasoning

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------
    def _is_readonly_call(self, call: ToolCall) -> bool:
        """这次调用是否「无副作用」，可以与其他只读调用并发执行。

        判据（保守优先，判不准就当写操作）：
          · 内置工具看注册表里的 read_only 标记；
          · MCP / 插件工具的副作用无法确知 → 一律当作写操作，不并发。
        宁可少并发，也不要为了快把有依赖的写操作打乱顺序。
        """
        name = getattr(call, "name", "") or ""
        if not name:
            return False
        # MCP / 插件：不并发（副作用未知）
        try:
            mcp = getattr(self, "_mcp", None)
            if mcp is not None and (name.startswith("mcp__")
                                    or name in {t.full_name for t in mcp.all_tools()}):
                return False
            plugins = getattr(self, "_plugins", None)
            if plugins is not None and plugins.has_tool(name):
                return False
        except Exception:
            return False
        try:
            tool = self.registry.get(name)
        except Exception:
            return False
        return bool(getattr(tool, "read_only", False)) if tool is not None else False

    async def _execute_call(self, call: ToolCall, ctx: ToolContext, sid: str) -> ToolResult:
        """执行一次工具调用（含 MCP 与插件分发）。

        注意事件上报归属：**内置工具**由 ``registry.execute`` 统一发
        tool.start / tool.end；这里只负责 MCP 与插件两个分支自报，
        否则同一次调用会被上报两遍（界面里表现为每个工具打印两次）。
        """
        name = call.name
        args = call.arguments or {}

        # MCP 工具
        mcp = getattr(self, "_mcp", None)
        if mcp is not None and (name in {t.full_name for t in mcp.all_tools()} or name.startswith("mcp__")):
            self.bus.emit(Ev.TOOL_START, {"name": name, "arguments": args, "id": call.id},
                          session_id=sid)
            t0 = time.time()
            try:
                ok, text, data = await mcp.call_tool(name, args)
                tr = ToolResult(ok=ok, content=text, error=None if ok else text, data=data)
            except Exception as e:
                tr = ToolResult.fail(f"MCP 调用失败：{type(e).__name__}: {e}")
            tr.duration = time.time() - t0
            self.bus.emit(
                Ev.TOOL_END,
                {"name": name, "ok": tr.ok, "error": tr.error, "duration": tr.duration,
                 "display": tr.display, "preview": tr.content[:1500],
                 "files": list(tr.files or [])},
                session_id=sid,
            )
            return tr

        # 插件工具
        plugins = getattr(self, "_plugins", None)
        if plugins is not None and plugins.has_tool(name):
            self.bus.emit(Ev.TOOL_START, {"name": name, "arguments": args, "id": call.id},
                          session_id=sid)
            t0 = time.time()
            try:
                out = await plugins.call_tool(name, args, ctx)
                tr = out if isinstance(out, ToolResult) else ToolResult.text(str(out))
            except Exception as e:
                tr = ToolResult.fail(f"插件工具失败：{type(e).__name__}: {e}")
            tr.duration = time.time() - t0
            self.bus.emit(
                Ev.TOOL_END,
                {"name": name, "ok": tr.ok, "error": tr.error, "duration": tr.duration,
                 "display": tr.display, "preview": tr.content[:1500],
                 "files": list(tr.files or [])},
                session_id=sid,
            )
            return tr

        # 内置工具（事件由 registry 自己上报，此处不重复）
        return await self.registry.execute(name, args, ctx)

    async def _tool_message(self, call: ToolCall, tr: ToolResult) -> Message:
        """把工具结果转成回灌给模型的消息，附件（图片）另行附加。"""
        text = tr.to_model_text()
        images = (tr.data or {}).get("_attach") or []
        if images:
            from ..llm.types import Attachment

            atts = []
            for img in images[:4]:
                if isinstance(img, dict) and img.get("path"):
                    atts.append(
                        Attachment(
                            kind=img.get("kind", "image"), path=img.get("path"),
                            mime=img.get("mime", "image/png"), name=img.get("name", ""),
                        )
                    )
            if atts:
                # 图片作为独立 user 消息（部分供应商不接受 tool 消息带图）
                msg = Message.tool_result(call.id, call.name, text)
                msg.attachments = atts
                return msg
        return Message.tool_result(call.id, call.name, text)

    async def _maybe_compact(self, msgs: list[Message], sid: str,
                             result: TurnResult) -> list[Message] | None:
        """回合中途检查预算。"""
        cfg = self.config
        window, unlimited = self._context_budget(sid)
        if unlimited or window <= 0:
            return None
        need, tokens = should_compact(msgs, context_window=window, ratio=float(cfg.llm.compact_ratio))
        if not need:
            return None
        return await self.compact(msgs, window=window, session_id=sid, tokens=tokens)

    async def compact_session(self, *, force: bool = True) -> dict[str, Any]:
        """★ 2-A：手动压缩当前会话（供 `/compact` 命令与 POST /api/chat/compact 使用）。

        与自动压缩的区别：
          · **force=True**：不因「收益不足 minimum_progress」而跳过 —— 用户是主动点
            的，就要看到效果；
          · **写回会话**：压缩结果用 `set_messages` 落库，否则刷新页面又变回原样；
          · 返回回执（压缩前后消息数 / 估算 token），供界面显示。

        不做的事：**不改变自动阈值**（自动压缩仍按原逻辑走），也不影响正在跑的回合。
        """
        from ..utils import estimate_tokens as _est

        sid = self.session_id
        msgs = self.sessions.messages(sid)
        before_n = len(msgs)
        before_tok = sum(_est(m.content or "") + _est(m.reasoning or "") for m in msgs)
        window, unlimited = self._context_budget(sid)
        if unlimited or window <= 0:
            # 未限制上下文时仍允许手动压缩，给一个等效窗口用于收益计算
            window = max(1000, before_tok or 1000)
        out = await self.compact(msgs, window=window, session_id=sid,
                                 tokens=before_tok, force=bool(force))
        changed = len(out) != before_n
        if changed:
            try:
                self.sessions.set_messages(sid, out)
            except Exception as e:
                return {"ok": False, "error": f"写回会话失败：{type(e).__name__}: {e}"}
        after_tok = sum(_est(m.content or "") + _est(m.reasoning or "") for m in out)
        return {
            "ok": True,
            "changed": changed,
            "before_messages": before_n,
            "after_messages": len(out),
            "before_tokens": before_tok,
            "after_tokens": after_tok,
            "saved_tokens": max(0, before_tok - after_tok),
        }

    async def _reflect(self, msgs: list[Message], sid: str) -> str:
        """轻量反思：让模型自检是否需要调整方向。返回要追加的提示（可能为空）。"""
        recent = [m for m in msgs if m.role in ("assistant", "tool")][-6:]
        if not recent:
            return ""
        digest = []
        for m in recent:
            if m.role == "assistant" and m.tool_calls:
                digest.append("助手调用：" + ", ".join(c.name for c in m.tool_calls))
            elif m.role == "tool":
                digest.append(f"工具 {m.tool_name} 返回：{truncate(m.content, 300)}")
        if not digest:
            return ""
        try:
            resp = await self.llm.chat(
                [
                    Message.system(
                        "你是一个执行监督者。用一句话判断：根据上面的执行轨迹，"
                        "当前方向是否正确？如果发现明显问题（重复无效调用、方向跑偏、遗漏关键步骤），"
                        "给出一条具体纠正建议。如果一切正常，只回复「正常」。"
                    ),
                    Message.user("\n".join(digest)),
                ],
                max_tokens=180,
                temperature=0.0,
            )
        except Exception:
            return ""
        text = (resp.content or "").strip()
        if not text or "正常" == text.strip("。.") or text.startswith("正常"):
            return ""
        self.bus.emit(Ev.LOG, {"level": "info", "message": f"自我检查：{truncate(text, 200)}"},
                      session_id=sid)
        return f"<self_check>执行监督者提示：{text}</self_check>"

    # ------------------------------------------------------------------
    # 自动记忆
    # ------------------------------------------------------------------
    async def _auto_remember(self, user_input: str, answer: str, sid: str) -> None:
        """**仅在消息中明确出现「记住」类措辞时**保存记忆。

        设计取舍：此前每轮对话都从用户输入里正则抽取「值得记的句子」直接落库，
        结果是记忆条目迅速膨胀、大量是完成一次性任务留下的任务描述（例如
        「帮我做一个 XX 页面」），它们既不跨对话复用，还会挤占每次召回的条数。

        现在只在两种情况下写入：
          1. 用户明确表达了「记住」的意图（请记住 / 记一下 / 别忘 / 以后都…）；
          2. 用户在模型询问后回复了「要」（模型据此调用 memory 工具写入）。
        除此之外一律不写，也不再替用户做主。
        """
        if not self.config.memory.enabled:
            return
        if not _has_explicit_remember_intent(user_input):
            return
        try:
            facts = extract_facts(user_input, limit=2)
            for f in facts:
                await self.memory.remember(
                    f, kind="preference" if any(
                        k in f for k in ("偏好", "习惯", "以后", "请记住")
                    ) else "fact",
                    session_id=sid, source="user", importance=0.8,
                )
        except Exception:
            pass

    @staticmethod
    def _last_assistant_text(msgs: list[Message]) -> str:
        for m in reversed(msgs):
            if m.role == "assistant" and m.content:
                return m.content
        return ""

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """请求中断当前回合。"""
        self._cancel.set()
        self.approval.cancel_all("用户中断了回合")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _on_approval(self, req: Any) -> None:
        self.bus.emit(Ev.APPROVAL, req.to_dict(), session_id=self.session_id)


# --------------------------------------------------------------------------
# 兜底解析：模型把工具调用写在正文里
# --------------------------------------------------------------------------

# ★ 保存记忆的意图措辞。只有命中这些，才自动写入记忆；
#   其余情况由模型在收尾时用一句话征询（见 prompts.SUBMIT_CHECKLIST 第 6 条）。
#   为什么用「明确措辞」而不是让模型判断：模型倾向于把一切都当成值得记的，
#   实测记忆条目因此迅速膨胀，且大量是一次性任务描述。
_REMEMBER_INTENT_RE = re.compile(
    r"(请记住|帮我记住|记住这|记一下|记下来|别忘了|别忘记|以后都|以后请|"
    r"牢记|存进记忆|保存到记忆|记到记忆里)"
)


def _has_explicit_remember_intent(text: str) -> bool:
    """用户是否明确要求「记住这件事」。"""
    return bool(_REMEMBER_INTENT_RE.search(text or ""))


# 可能改变文件系统 / 需要收尾自检的工具
_MUTATING_TOOLS = {
    "write_file", "edit_file", "multi_edit", "apply_patch", "file_ops",
    "shell", "python_exec", "run_script", "install_packages",
    "extract_archive", "create_archive", "write_office", "make_chart",
    "sqlite", "ssh", "input_control", "clipboard", "create_archive",
}

# ★ 校验类工具：回执卡里「跑了什么验证」取自这里。
#   为什么按工具名而不是猜命令文本：命令是模型自由写的，靠正则认「这是不是测试」
#   会漏也会误判；而用哪个工具跑的是确定的（跑测试基本都走 shell / python_exec）。
_VERIFY_TOOLS = {"shell", "python_exec", "run_script", "pytest", "npm", "cargo"}

# ★ 计划/待办类工具：它们改写的是「本会话的任务清单」这份状态。
#   为什么要与 _MUTATING_TOOLS 并列为「实质进展」：实测「待办进度卡在第一步」——
#   模型只更新计划、没碰文件时，旧判据认为「什么都没发生」，
#   于是既不注入收尾自检、模型也不主动收尾，看着像死锁。
_PLAN_TOOLS = {"todo", "plan"}


def _count_runaway_repeat(parts: list[str], *, window: int = 40,
                          max_period: int = 80) -> int:
    """检测正文是否在「原地打转」，返回**尾部同周期重复了几轮**。

    ★ 为什么需要它（实测）：模型会陷入这种输出 ——
      「我现在写。好。写。输出。好。我现在写。好。写。输出。…」无限重复。
      已有的「无进展防护」只看工具轮次，而纯文本打转时模型每轮都在写新内容，
      stall 计数永远归零，于是能一直重复到步数/输出上限才停。

    ★ 判据为什么用「重复周期」而不是「片段出现频次」（实测对比过三版）：
      · 按片段频次统计：正常长回答与真打转都数出 2~3 次，阈值放不准；
      · 按「尾串在前出现过几次」：尾串跨过循环边界时只数出 1 次，漏判；
      · 按**最短重复周期**：真打转 = 周期 14 字重复 8 轮；正常回答 = 1 轮；
        正常的重复句式（第 N 步…」）= 3 轮。阈值取 5 轮，两侧都很宽裕。

    ★ window 取 40（实测定的）：窗口必须容纳得下 3~4 个周期才扫得出来。
      取 12 段时窗口只剩 33 字，周期 14 的循环在里面只够两轮，数不出 8 轮而漏判。
    """
    try:
        text = "".join(p for p in parts[-window:] if p)
    except Exception:
        return 0
    text = "".join(text.split())
    n = len(text)
    best_rounds = 0
    for p in range(4, min(max_period, n // 3) + 1):
        tail = text[-p:]
        rounds, i = 1, n - p
        while i - p >= 0 and text[i - p:i] == tail:
            rounds += 1
            i -= p
        if rounds > best_rounds:
            best_rounds = rounds
    return best_rounds


def _needs_submit_check(msgs: list[Message]) -> bool:
    """判断这一轮是否发生过实质改动（用于决定要不要注入提交前自检）。

    只在"确实动过东西"时才自检，纯问答不触发，避免无谓的额外往返。

    ★ 待办更新也算实质进展（实测「待办进度卡住不刷新」的成因之一）：
      `todo` / `plan` 会改写本会话的任务清单 —— 那是一份会被反复读写的状态，
      改了它就该走一次自检收尾，否则模型「只更新了计划、没动文件」时
      既不触发自检、也不主动收尾，面板停在第一步不动，看起来像死锁。
    """
    mutated = False
    for m in msgs:
        if m.role == "assistant":
            for tc in m.tool_calls or []:
                if tc.name in _MUTATING_TOOLS or tc.name in _PLAN_TOOLS:
                    mutated = True
                    break
        elif m.role == "tool" and (m.tool_name in _MUTATING_TOOLS or m.tool_name in _PLAN_TOOLS):
            mutated = True
        if mutated:
            break
    return mutated


def _extract_fake_tool_calls(content: str, specs: list[ToolSpec]) -> tuple[list[ToolCall], str]:
    """从正文里提取"假装"的工具调用，并返回清理后的正文。"""
    if not content or not specs:
        return [], content
    valid = {s.name for s in specs}
    found: list[ToolCall] = []
    cleaned = content
    for pat in _FAKE_TOOL_PATTERNS:
        for m in list(pat.finditer(content)):
            raw = m.group(1)
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            name = str(obj.get("name") or obj.get("tool") or "").strip()
            if name not in valid:
                continue
            args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
            if isinstance(args, str):
                args = ToolCall.parse_arguments(args)
            if not isinstance(args, dict):
                args = {}
            found.append(ToolCall(id=f"fake_{len(found)}", name=name, arguments=args,
                                  raw_arguments=json.dumps(args, ensure_ascii=False)))
            cleaned = cleaned.replace(m.group(0), "")
    if not found:
        return [], content
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return found, cleaned


__all__ = ["Agent", "TurnResult"]
