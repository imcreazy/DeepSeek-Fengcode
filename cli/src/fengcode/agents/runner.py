"""子智能体运行器：派生独立上下文的 Agent 并行/串行/辩论式工作。

设计要点
--------
- **独立上下文**：子代理只拿到一段自包含的任务描述，看不到主对话，避免上下文污染。
- **独立预算**：每个子代理有 token 上限、超时、以及可裁剪的工具集。
- **可嵌套**：子代理若被授予 `subagent` 工具，可再派生子代理（受 max_depth 限制）。
- **结构化返回**：只把结论回给父级，过程流水账留在自己的轨迹里。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..events import Ev, get_bus
from ..llm.types import Message
from ..utils import estimate_tokens, truncate

# 各类型的默认工具白名单（None 表示不限制）
TYPE_TOOLS: dict[str, list[str] | None] = {
    "general": None,
    "explore": ["read_file", "read_many", "list_dir", "glob", "grep", "code_map",
                "repo_overview", "parse_document", "shell", "web_fetch", "web_search",
                "todo", "memory", "skill"],
    "research": ["web_search", "web_fetch", "http_request", "parse_document",
                 "read_file", "memory", "skill", "python_exec", "todo"],
    "review": ["read_file", "read_many", "grep", "glob", "code_map", "git", "shell",
               "python_exec", "run_tests", "lint", "repo_overview", "memory"],
    "security-review": ["read_file", "read_many", "grep", "glob", "code_map",
                        "shell", "python_exec", "http_request", "dependency_graph", "memory"],
    "test": ["read_file", "read_many", "glob", "grep", "run_tests", "lint",
             "shell", "python_exec", "write_file", "edit_file", "code_map", "todo"],
    "plan": ["read_file", "read_many", "list_dir", "glob", "grep", "code_map",
             "repo_overview", "todo", "memory", "skill"],
}

TYPE_LABELS: dict[str, str] = {
    "general": "通用",
    "explore": "代码探索",
    "research": "研究调研",
    "review": "代码评审",
    "security-review": "安全审计",
    "test": "测试验证",
    "plan": "任务规划",
}


@dataclass
class SubTask:
    """一个子任务。"""

    task: str
    agent_type: str = "general"
    name: str = ""
    model: str | None = None
    tools: list[str] | None = None
    budget_tokens: int = 0
    timeout: float = 0.0
    context: str = ""
    # ★ 依赖边：本任务需要先完成哪些上游任务。
    #   写名字或列表位置序号都行；舰队据此只等自己那几个上游、其余并行，
    #   并把上游的结论**带进**本任务的上下文（避免重复调研）。
    depends_on: list[str] = field(default_factory=list)

    @staticmethod
    def from_any(x: Any) -> "SubTask":
        if isinstance(x, SubTask):
            return x
        if isinstance(x, str):
            return SubTask(task=x)
        if isinstance(x, dict):
            return SubTask(
                task=str(x.get("task") or x.get("prompt") or ""),
                agent_type=str(x.get("agent_type") or x.get("type") or "general"),
                name=str(x.get("name") or ""),
                model=x.get("model"),
                tools=list(x["tools"]) if x.get("tools") else None,
                budget_tokens=int(x.get("budget_tokens") or 0),
                timeout=float(x.get("timeout") or 0),
                context=str(x.get("context") or ""),
                depends_on=[str(d) for d in (x.get("depends_on") or [])],
            )
        return SubTask(task=str(x))


@dataclass
class SubResult:
    """子任务结果。"""

    name: str = ""
    agent_type: str = "general"
    task: str = ""
    content: str = ""
    ok: bool = True
    error: str | None = None
    steps: int = 0
    tokens: int = 0
    duration: float = 0.0
    model: str = ""
    tool_calls: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name or self.agent_type,
            "agent_type": self.agent_type,
            "task": self.task,
            "content": self.content,
            "ok": self.ok,
            "error": self.error,
            "steps": self.steps,
            "tokens": self.tokens,
            "duration": round(self.duration, 3),
            "model": self.model,
        }


class SubAgentRunner:
    """负责派生与协调子智能体。"""

    def __init__(self, *, agent: Any) -> None:
        self.agent = agent           # 宿主（主）Agent
        self.bus = get_bus()
        self.config = agent.config
        self._depth = 0
        # ★ 2-K：运行态表 —— 谁在跑、谁在排队、谁结束了。
        #   为什么需要：并行派发时用户只能看到「子智能体开始/结束」两条流水，
        #   中间谁在等、并发上限卡住了几个完全看不见。
        #   这里只记轻量元数据（名字/类型/状态/起止时间），不碰子代理的上下文。
        self._runs: dict[str, dict[str, Any]] = {}

    # ---- 运行态（★ 2-K）------------------------------------------------
    def _run_key(self, name: str, atype: str) -> str:
        return f"{name or atype}#{len(self._runs) + 1}"

    def mark_queued(self, name: str, atype: str) -> str:
        key = self._run_key(name, atype)
        self._runs[key] = {
            "id": key, "name": name or atype, "type": atype,
            "status": "queued", "queued_at": time.time(),
            "started_at": 0.0, "ended_at": 0.0, "ok": None,
            # ★ WaitCause：排队时**为什么在等**。
            #   用户看见的痛点是「有几个子代理卡在排队，但不知道被什么挡住」——
            #   只显示「排队中」等于没说。这里记下具体约束与说明。
            "wait_cause": "", "wait_detail": "", "depends_on": [],
        }
        return key

    def set_wait_cause(self, key: str, cause: str, detail: str = "",
                       depends_on: list[str] | None = None) -> None:
        """给排队中的任务标注「被什么挡住」。

        cause：``concurrency``（并发上限）/ ``dependency``（等上游）/ ``manual``（人工暂停）
        """
        r = self._runs.get(key)
        if r is None:
            return
        r["wait_cause"] = cause
        r["wait_detail"] = detail
        if depends_on is not None:
            r["depends_on"] = list(depends_on)

    def mark_running(self, key: str) -> None:
        r = self._runs.get(key)
        if r:
            r["status"] = "running"
            r["started_at"] = time.time()

    def mark_done(self, key: str, ok: bool) -> None:
        r = self._runs.get(key)
        if r:
            r["status"] = "done" if ok else "failed"
            r["ended_at"] = time.time()
            r["ok"] = bool(ok)

    def live(self, *, limit: int = 40) -> list[dict[str, Any]]:
        """当前运行态快照（供界面画「谁在等 / 谁在跑」）。"""
        items = sorted(self._runs.values(), key=lambda r: r.get("queued_at") or 0)
        out = []
        for r in items[-limit:]:
            d = dict(r)
            end = r.get("ended_at") or time.time()
            start = r.get("started_at") or r.get("queued_at") or end
            d["elapsed"] = round(max(0.0, end - start), 2)
            d["waited"] = round(max(0.0, (r.get("started_at") or end) - (r.get("queued_at") or end)), 2)
            # 给界面一句可直接显示的中文说明（WaitCause → 人话）
            d["wait_text"] = self._wait_text(r)
            out.append(d)
        return out

    @staticmethod
    def _wait_text(r: dict[str, Any]) -> str:
        """把 WaitCause 翻成给用户看的一句话（空串 = 没在等）。"""
        if r.get("status") != "queued":
            return ""
        cause = r.get("wait_cause") or ""
        detail = r.get("wait_detail") or ""
        if cause == "concurrency":
            return detail or "等待并发名额"
        if cause == "dependency":
            deps = r.get("depends_on") or []
            base = "等待上游任务：" + "、".join(str(x) for x in deps) if deps else "等待上游任务"
            return f"{base}（{detail}）" if detail else base
        if cause == "manual":
            return detail or "已暂停（人工）"
        return detail or "排队中"

    def graph(self, *, limit: int = 60) -> dict[str, Any]:
        """运行图：给界面画「谁在等谁、被哪条约束挡住」。

        返回 ``{nodes, edges, counts}``：
          · nodes：每个子任务的 id / 名称 / 类型 / 状态 / 等待原因
          · edges：依赖边（from = 上游 id，to = 下游 id）
          · counts：按状态计数，便于面板顶部一行说明
        这是**视图数据**，不改动调度本身；没有依赖关系时 edges 为空，
        界面退化成单纯的列表展示。
        """
        runs = sorted(self._runs.values(), key=lambda r: r.get("queued_at") or 0)[-limit:]
        nodes = []
        edges = []
        counts: dict[str, int] = {}
        by_name: dict[str, str] = {}
        for r in runs:
            st = str(r.get("status") or "")
            counts[st] = counts.get(st, 0) + 1
            by_name[str(r.get("name") or r.get("type") or "")] = str(r.get("id") or "")
            nodes.append({
                "id": r.get("id") or "",
                "name": r.get("name") or r.get("type") or "",
                "type": r.get("type") or "",
                "status": st,
                "wait_text": self._wait_text(r),
                "wait_cause": r.get("wait_cause") or "",
                "elapsed": round(max(0.0, (r.get("ended_at") or time.time())
                                     - (r.get("started_at") or r.get("queued_at") or 0)), 2),
                "waited": round(max(0.0, (r.get("started_at") or time.time())
                                    - (r.get("queued_at") or 0)), 2),
            })
        for r in runs:
            for dep in (r.get("depends_on") or []):
                src = by_name.get(str(dep))
                if src:
                    edges.append({"from": src, "to": r.get("id") or ""})
        return {"nodes": nodes, "edges": edges, "counts": counts}

    def clear_runs(self) -> None:
        self._runs.clear()

    # ---- 元信息 --------------------------------------------------------
    def list_types(self) -> list[dict[str, Any]]:
        out = []
        for name, tools in TYPE_TOOLS.items():
            out.append(
                {
                    "name": name,
                    "label": TYPE_LABELS.get(name, name),
                    "description": self._describe(name),
                    "tools": tools,
                    "model": (self.config.agent.subagent_models or {}).get(name)
                    or self.config.agent.subagent_model
                    or None,
                }
            )
        return out

    @staticmethod
    def _describe(name: str) -> str:
        return {
            "general": "通用任务：按给定描述独立完成一件具体工作",
            "explore": "代码探索：摸清陌生代码库的结构、入口与关键实现",
            "research": "研究调研：联网检索并交叉验证，产出带来源的结论",
            "review": "代码评审：审查改动，找出正确性/安全/性能问题",
            "security-review": "安全审计：从攻击者视角找漏洞与风险",
            "test": "测试验证：跑测试、补边界用例、如实报告结果",
            "plan": "任务规划：把目标拆成可执行、可验证的步骤",
        }.get(name, "")

    # ---- 配置解析 ------------------------------------------------------
    def _resolve(self, st: SubTask) -> dict[str, Any]:
        cfg = self.config.agent
        atype = st.agent_type if st.agent_type in TYPE_TOOLS else "general"
        model = st.model or (cfg.subagent_models or {}).get(atype) or cfg.subagent_model
        tools = st.tools if st.tools is not None else TYPE_TOOLS.get(atype)
        budget = st.budget_tokens or int(cfg.subagents.default_budget_tokens)
        timeout = st.timeout or float(cfg.subagents.default_timeout)
        return {
            "agent_type": atype,
            "model": model,
            "tools": tools,
            "budget_tokens": budget,
            "timeout": timeout,
        }

    # ---- 执行 ----------------------------------------------------------
    async def _run_one(self, st: SubTask, *, depth: int) -> SubResult:
        cfg = self.config.agent
        res = SubResult(name=st.name, agent_type=st.agent_type, task=st.task)
        if not cfg.subagents.enabled:
            res.ok, res.error = False, "子智能体已在配置中关闭"
            return res
        if depth > int(cfg.subagents.max_depth):
            res.ok, res.error = False, f"已超过最大嵌套深度（{cfg.subagents.max_depth}）"
            return res
        if not (st.task or "").strip():
            res.ok, res.error = False, "任务描述为空"
            return res

        opts = self._resolve(st)
        res.agent_type = opts["agent_type"]
        res.model = opts["model"] or ""
        t0 = time.time()
        label = st.name or f"{TYPE_LABELS.get(opts['agent_type'], opts['agent_type'])}子代理"
        self.bus.emit(
            Ev.SUBAGENT_START,
            {"name": label, "type": opts["agent_type"], "task": truncate(st.task, 300),
             "model": res.model, "depth": depth},
            session_id=self.agent.session_id,
        )

        # 组装消息：只给任务 + 角色提示，不给主对话历史
        prompt = st.task
        if st.context:
            prompt = f"{st.task}\n\n【补充背景】\n{st.context}"
        try:
            msgs = await self.agent.build_messages(
                prompt,
                session_id=self.agent.session_id,
                history=[],                       # 关键：清空历史，实现上下文隔离
                subagent=opts["agent_type"],
                extra_context=(
                    f"<subagent_meta>你是一个独立子智能体（类型：{opts['agent_type']}）。"
                    f"当前嵌套深度 {depth}。你的输出会直接返回给主 Agent，请只输出结论。</subagent_meta>"
                ),
                skip_memory=False,
            )
        except Exception as e:
            res.ok, res.error = False, f"组装上下文失败：{e}"
            self._emit_end(res, label, t0)
            return res

        # 工具集裁剪：若为嵌套调用，额外移除 subagent 工具（除非允许更深）
        allow = opts["tools"]
        if allow is not None:
            if depth >= int(cfg.subagents.max_depth):
                allow = [t for t in allow if t != "subagent"]

        specs = self.agent.tool_specs(allow=allow)
        ctx = self.agent.tool_context(self.agent.session_id, subagent_depth=depth)
        cancelled = asyncio.Event()

        try:
            content, steps, tokens, calls = await asyncio.wait_for(
                self._loop(msgs, specs, ctx, opts, depth, label),
                timeout=float(opts["timeout"]),
            )
            res.content, res.steps, res.tokens, res.tool_calls = content, steps, tokens, calls
            res.ok = True
        except asyncio.TimeoutError:
            res.ok = False
            res.error = f"子智能体超时（{opts['timeout']:.0f}s）"
        except Exception as e:
            res.ok = False
            res.error = f"{type(e).__name__}: {e}"

        res.duration = time.time() - t0
        self._emit_end(res, label, t0)
        return res

    def _emit_end(self, res: SubResult, label: str, t0: float) -> None:
        self.bus.emit(
            Ev.SUBAGENT_END,
            {
                "name": label,
                "type": res.agent_type,
                "ok": res.ok,
                "error": res.error,
                "duration": round(time.time() - t0, 2),
                "steps": res.steps,
                "preview": truncate(res.content, 400),
            },
            session_id=self.agent.session_id,
        )

    async def _loop(
        self, msgs: list[Message], specs: list[Any], ctx: Any, opts: dict[str, Any],
        depth: int, label: str,
    ) -> tuple[str, int, int, list[dict]]:
        """子代理的 ReAct 循环（与主循环同构，但更简单、有预算限制）。"""
        cfg = self.config.agent
        budget = int(opts["budget_tokens"])
        used = 0
        last_text = ""
        calls_log: list[dict] = []
        max_steps = 24

        for step in range(1, max_steps + 1):
            if self._depth != depth:
                self._depth = depth
            try:
                resp = await self.agent.llm.chat(msgs, model=opts["model"], tools=specs or None)
            except Exception as e:
                if last_text:
                    return last_text, step, used, calls_log
                raise RuntimeError(f"子代理模型调用失败：{e}") from e

            used += (resp.usage.total_tokens or 0) or estimate_tokens(resp.content)
            if resp.content:
                last_text = resp.content
            if resp.error and not resp.tool_calls:
                if last_text:
                    return last_text, step, used, calls_log
                raise RuntimeError(f"子代理模型错误：{resp.error}")

            if not resp.tool_calls:
                return resp.content or last_text, step, used, calls_log

            msgs.append(Message.assistant(resp.content or "", resp.tool_calls))
            for call in resp.tool_calls:
                # 子代理内的工具调用不走审批（父级已授权），但审计照记
                tr = await self.agent.registry.execute(call.name, call.arguments, ctx)
                calls_log.append({"name": call.name, "ok": tr.ok})
                msgs.append(Message.tool_result(call.id, call.name, tr.to_model_text()))

            if budget and used > budget:
                msgs.append(
                    Message.system(
                        f"<budget_warning>已使用约 {used} tokens，接近预算 {budget}。"
                        "请立即基于现有信息给出结论，不要再调用工具。</budget_warning>"
                    )
                )
        msgs.append(Message.system("已达步数上限，请直接给出当前结论。"))
        try:
            resp = await self.agent.llm.chat(msgs, model=opts["model"])
            return resp.content or last_text, max_steps, used, calls_log
        except Exception:
            return last_text, max_steps, used, calls_log

    # ---- 对外接口 ------------------------------------------------------
    async def run(self, task: str, *, agent_type: str = "general", context: str = "",
                  model: str | None = None, tools: list[str] | None = None,
                  budget_tokens: int = 0, timeout: float = 0.0, name: str = "") -> dict[str, Any]:
        """跑一个子智能体。"""
        st = SubTask(task=task, agent_type=agent_type, name=name, model=model,
                     tools=tools, budget_tokens=budget_tokens, timeout=timeout, context=context)
        res = await self._run_one(st, depth=self._depth + 1)
        return {
            "ok": res.ok,
            "content": res.content or (f"（子智能体失败：{res.error}）" if res.error else "（无输出）"),
            "error": res.error,
            "type": res.agent_type,
            "steps": res.steps,
            "tokens": res.tokens,
            "duration": res.duration,
            "tool_calls": res.tool_calls,
        }

    async def run_parallel(self, tasks: list[Any], *, context: str = "",
                           concurrency: int = 0, **kw: Any) -> dict[str, Any]:
        """并行跑多个子任务。"""
        subs = [SubTask.from_any(t) for t in tasks]
        if not subs:
            return {"ok": False, "content": "没有可执行的任务", "results": []}
        limit = int(concurrency or self.config.agent.max_parallel_subagents or 4)
        sem = asyncio.Semaphore(max(1, limit))
        for s in subs:
            if context and not s.context:
                s.context = context

        async def one(s: SubTask) -> SubResult:
            # ★ 2-K：先登记「排队」，拿到并发许可后再标记「运行中」——
            #   这样「被并发上限挡住、正在等」这件事才看得见。
            key = self.mark_queued(s.name, s.agent_type)
            # ★ WaitCause：把「为什么在等」写清楚。
            #   用户看到「排队中」却不知道被什么挡住，等于没说；这里明确标出
            #   「并发上限 N，当前在跑 M 个，等它们腾出位置」。
            running_now = sum(1 for r in self._runs.values() if r.get("status") == "running")
            if running_now >= max(1, limit):
                self.set_wait_cause(
                    key, "concurrency",
                    f"并发上限 {max(1, limit)}，当前 {running_now} 个在跑，等它们腾出位置",
                )
            async with sem:
                self.set_wait_cause(key, "", "")
                self.mark_running(key)
                try:
                    return await self._run_one(s, depth=self._depth + 1)
                finally:
                    self.mark_done(key, True)   # 详细 ok 由下面的结果回填修正

        results = await asyncio.gather(*(one(s) for s in subs), return_exceptions=True)
        out: list[SubResult] = []
        for s, r in zip(subs, results):
            if isinstance(r, Exception):
                out.append(SubResult(name=s.name, agent_type=s.agent_type, task=s.task,
                                     ok=False, error=f"{type(r).__name__}: {r}"))
            else:
                out.append(r)  # type: ignore[arg-type]
        # 回填真实成败（one() 的 finally 只能先按 True 收尾）
        try:
            for r in out:
                for run in self._runs.values():
                    if run["name"] == (r.name or r.agent_type) and run["status"] in ("done", "failed"):
                        run["status"] = "done" if r.ok else "failed"
                        run["ok"] = bool(r.ok)
                        break
        except Exception:
            pass

        lines = [f"并行执行 {len(out)} 个子任务的结果：", ""]
        for i, r in enumerate(out, 1):
            title = r.name or f"{TYPE_LABELS.get(r.agent_type, r.agent_type)}#{i}"
            flag = "✓" if r.ok else "✗"
            lines.append(f"## {flag} {title}（{r.duration:.1f}s）")
            if r.task:
                lines.append(f"任务：{truncate(r.task, 160)}")
            lines.append("")
            if r.ok:
                lines.append(r.content or "（无输出）")
            else:
                lines.append(f"失败：{r.error}")
            lines.append("")
        return {
            "ok": all(r.ok for r in out),
            "content": "\n".join(lines),
            "results": [r.to_dict() for r in out],
        }

    async def run_chain(self, tasks: list[Any], *, context: str = "", **kw: Any) -> dict[str, Any]:
        """顺序链：上一步的输出作为下一步的背景。"""
        subs = [SubTask.from_any(t) for t in tasks]
        if not subs:
            return {"ok": False, "content": "没有可执行的任务", "results": []}
        out: list[SubResult] = []
        carried = context
        for i, s in enumerate(subs, 1):
            if carried and not s.context:
                s.context = carried
            if not s.name:
                s.name = f"第 {i} 步：{TYPE_LABELS.get(s.agent_type, s.agent_type)}"
            r = await self._run_one(s, depth=self._depth + 1)
            out.append(r)
            if not r.ok:
                break
            carried = f"上一步（{r.name}）的结论：\n{truncate(r.content, 3000)}"

        lines = ["顺序链执行结果：", ""]
        for r in out:
            flag = "✓" if r.ok else "✗"
            lines.append(f"## {flag} {r.name or r.agent_type}（{r.duration:.1f}s）")
            lines.append(r.content if r.ok else f"失败：{r.error}")
            lines.append("")
        return {
            "ok": all(r.ok for r in out) and len(out) == len(subs),
            "content": "\n".join(lines),
            "results": [r.to_dict() for r in out],
        }

    async def run_fleet(self, tasks: list[Any], *, context: str = "",
                        concurrency: int = 0, fail_fast: bool = False) -> dict[str, Any]:
        """舰队：按依赖图跑多个子任务，**依赖边携带上游答案**。

        与 `run_parallel` / `run_chain` 的区别：
          · parallel 互不依赖，全平行；
          · chain 是严格一线串行，上一步结论传给下一步；
          · **fleet** 允许任意 DAG：每个任务可声明 `depends_on: [id...]`，
            只等自己那几个上游，其余能并行就并行。

        ★ 关键收益（"先调研再实现"不必付两次钱）：下游任务**开局就拿到上游的结论**
          作为上下文，不需要自己再查一遍。这正是用户要的「依赖边携带上游答案」。
        """
        subs = [SubTask.from_any(t) for t in tasks]
        if not subs:
            return {"ok": False, "content": "没有可执行的任务", "results": []}

        # 给每个任务一个稳定 id（用列表位置，便于 depends_on 引用）
        ids: list[str] = []
        for i, s in enumerate(subs, 1):
            sid = s.name or f"任务{i}"
            ids.append(sid)
            if not s.name:
                s.name = sid
        id_set = set(ids)

        # 解析依赖：支持位置序号（"1"）与名字（"调研"）两种写法
        deps: list[list[str]] = []
        for i, s in enumerate(subs):
            raw = [str(x) for x in (getattr(s, "depends_on", None) or [])]
            resolved: list[str] = []
            for d in raw:
                if d in id_set:
                    resolved.append(d)
                elif d.isdigit() and 1 <= int(d) <= len(subs):
                    resolved.append(ids[int(d) - 1])
            deps.append(resolved)

        limit = int(concurrency or self.config.agent.max_parallel_subagents or 4)
        sem = asyncio.Semaphore(max(1, limit))
        results: dict[str, SubResult] = {}
        skipped: set[str] = set()
        done_evt: dict[str, asyncio.Event] = {i: asyncio.Event() for i in ids}

        async def one(idx: int) -> None:
            s = subs[idx]
            sid = ids[idx]
            mine = deps[idx]
            key = self.mark_queued(s.name, s.agent_type)
            # 先等依赖完成
            if mine:
                self.set_wait_cause(key, "dependency",
                                    "等待上游 " + "、".join(mine), mine)
                for d in mine:
                    ev = done_evt.get(d)
                    if ev is not None:
                        await ev.wait()
                # 上游失败/被跳过 → 本任务也跳过（不假装成功）
                if fail_fast and any((results.get(d) is not None and not results[d].ok)
                                     or d in skipped for d in mine):
                    skipped.add(sid)
                    done_evt[sid].set()
                    self.mark_done(key, False)
                    results[sid] = SubResult(name=s.name, agent_type=s.agent_type,
                                             task=s.task, ok=False,
                                             error="上游任务未成功，已跳过")
                    return
                # ★ 携带上游答案：把每个上游的结论并进本任务的 context
                parts: list[str] = []
                for d in mine:
                    up = results.get(d)
                    if up is not None and up.content:
                        parts.append(f"【上游「{d}」的结论】\n{truncate(up.content, 2500)}")
                if parts:
                    carried = "\n\n".join(parts)
                    s.context = (s.context + "\n\n" + carried).strip() if s.context else carried
            running_now = sum(1 for r in self._runs.values() if r.get("status") == "running")
            if running_now >= max(1, limit):
                self.set_wait_cause(key, "concurrency",
                                    f"并发上限 {max(1, limit)}，当前 {running_now} 个在跑")
            async with sem:
                self.set_wait_cause(key, "", "")
                self.mark_running(key)
                try:
                    with contextlib.suppress(Exception):
                        if context and not s.context:
                            s.context = context
                    r = await self._run_one(s, depth=self._depth + 1)
                    results[sid] = r
                    self.mark_done(key, r.ok)
                except Exception as e:
                    results[sid] = SubResult(name=s.name, agent_type=s.agent_type,
                                             task=s.task, ok=False,
                                             error=f"{type(e).__name__}: {e}")
                    self.mark_done(key, False)
                finally:
                    done_evt[sid].set()

        await asyncio.gather(*(one(i) for i in range(len(subs))), return_exceptions=True)

        lines = [f"舰队执行结果（共 {len(subs)} 个任务）：", ""]
        for i, sid in enumerate(ids):
            r = results.get(sid)
            if r is None:
                lines.append(f"## ○ {sid}（未执行）")
                lines.append("")
                continue
            flag = "✓" if r.ok else "✗"
            by = f"　依赖：{'、'.join(deps[i])}" if deps[i] else ""
            lines.append(f"## {flag} {sid}（{r.duration:.1f}s）{by}")
            lines.append(r.content if r.ok else f"失败：{r.error}")
            lines.append("")
        return {
            "ok": all(r.ok for r in results.values()) and len(results) == len(subs),
            "content": "\n".join(lines),
            "results": [results[i].to_dict() for i in ids if i in results],
            "skipped": sorted(skipped),
            "deps": {ids[i]: deps[i] for i in range(len(subs))},
        }

    async def debate(self, topic: str, *, models: list[str] | None = None,
                     rounds: int = 1, context: str = "") -> dict[str, Any]:
        """多智能体辩论：多个模型各自给方案，再由一个裁判汇总择优。"""
        if not topic.strip():
            return {"ok": False, "content": "辩题不能为空", "results": []}
        # 确定参与模型
        cands = [m for m in (models or []) if m]
        if not cands:
            cands = _default_debate_models(self.agent, 3)
        if not cands:
            # 单个模型也要能跑（自我辩论：正反两方）
            cands = [None] * 2  # type: ignore[list-item]

        roles = ["甲方", "乙方", "丙方", "丁方"]
        subs: list[SubTask] = []
        for i, m in enumerate(cands[:4]):
            role = roles[i] if i < len(roles) else f"第{i + 1}方"
            subs.append(
                SubTask(
                    task=(
                        f"针对以下议题给出你的分析与方案，立场鲜明、论据具体：\n\n{topic}\n\n"
                        "要求：先给结论，再给 3-5 条支撑理由，最后指出你认为该方案的最大风险。"
                        "字数控制在 600 字内。"
                    ),
                    agent_type="general",
                    name=f"{role}（{m or '默认模型'}）",
                    model=m,
                    context=context,
                )
            )

        positions = await self.run_parallel(subs, concurrency=len(subs))
        texts = []
        for i, r in enumerate(positions.get("results") or []):
            role = roles[i] if i < len(roles) else f"第{i + 1}方"
            texts.append(f"### {role}\n{r.get('content') or '（无输出）'}")

        # 裁判
        judge_prompt = (
            f"议题：{topic}\n\n以下是各方观点：\n\n"
            + "\n\n".join(texts)
            + "\n\n请你作为裁判：\n"
            "1. 指出各方最有价值的论点与最关键的分歧\n"
            "2. 综合出一个更好的方案（吸收各方优点）\n"
            "3. 明确说明未采纳哪些观点及原因\n"
            "4. 给出可执行的下一步建议\n"
            "用中文，结构清晰，不要客套。"
        )
        try:
            resp = await self.agent.llm.chat(
                [
                    Message.system("你是严谨的技术裁判，善于综合多方观点并指出分歧本质。"),
                    Message.user(judge_prompt),
                ],
                max_tokens=3000,
                temperature=0.3,
            )
            verdict = (resp.content or "").strip()
        except Exception as e:
            verdict = f"（裁判环节失败：{e}）"

        content = (
            f"# 多智能体辩论：{truncate(topic, 100)}\n\n"
            f"## 各方观点\n\n" + "\n\n".join(texts) + f"\n\n## 裁判结论\n\n{verdict}"
        )
        return {
            "ok": positions.get("ok", False),
            "content": content,
            "results": positions.get("results") or [],
            "verdict": verdict,
        }


def _default_debate_models(agent: Any, n: int = 3) -> list[str]:
    """挑几个不同的可用模型参与辩论（优先跨供应商）。"""
    try:
        from ..llm.router import model_choices

        choices = [c for c in model_choices() if c.get("has_key") and c.get("enabled")]
        if len(choices) >= n:
            # 尽量来自不同供应商
            seen: set[str] = set()
            picked: list[str] = []
            for c in choices:
                if c["provider"] in seen:
                    continue
                seen.add(c["provider"])
                picked.append(c["ref"])
                if len(picked) >= n:
                    break
            return picked or [c["ref"] for c in choices[:n]]
        return [c["ref"] for c in choices[:n]]
    except Exception:
        return []


__all__ = ["SubAgentRunner", "SubTask", "SubResult", "TYPE_TOOLS", "TYPE_LABELS"]
