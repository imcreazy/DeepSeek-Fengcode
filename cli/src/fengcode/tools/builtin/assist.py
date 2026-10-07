"""Agent 自我管理工具：记忆、任务清单、目标、技能、子智能体、计划、提问。

这些工具让 Agent 能"管理自己的认知状态"，而不是把一切塞进上下文。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ...utils import truncate
from ..base import Tool, ToolContext, ToolResult


# --------------------------------------------------------------------------
# 记忆
# --------------------------------------------------------------------------

class MemoryTool(Tool):
    name = "memory"
    # ★ 不碰工作区文件（只写数据库/状态或纯界面交互）→ 不算进工作区写租约判定
    touches_workspace = False
    group = "认知"
    description = (
        "管理长期记忆。action 可为："
        "recall（检索相关记忆）、remember（写入一条记忆）、update（修改）、"
        "forget（删除）、list（列出）、stats（统计）、"
        "profile_get（读用户画像）、profile_set（写用户画像）。"
        "当用户表达了偏好、重要事实、或需要跨会话记住的约定时，应主动 remember。"
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["recall", "remember", "update", "forget", "list", "stats", "profile_get", "profile_set"],
            "description": "操作类型",
        },
        "query": {"type": "string", "description": "recall 时的检索词"},
        "content": {"type": "string", "description": "remember 时的记忆正文"},
        "title": {"type": "string", "description": "记忆标题（简短）"},
        "kind": {
            "type": "string",
            "enum": ["fact", "preference", "episode", "profile", "task", "decision"],
            "description": "记忆种类，默认 fact",
        },
        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签"},
        "importance": {"type": "number", "description": "重要度 0-1，默认 0.6"},
        "pinned": {"type": "boolean", "description": "是否置顶（始终参与召回）"},
        "id": {"type": "string", "description": "update/forget 时的记忆 ID"},
        "key": {"type": "string", "description": "profile_get/profile_set 的键"},
        "value": {"type": "string", "description": "profile_set 的值"},
        "top_k": {"type": "integer", "description": "recall 返回条数，默认 6"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", query: str = "", content: str = "",
                  title: str = "", kind: str = "fact", tags: list[str] | None = None,
                  importance: float = 0.6, pinned: bool = False, id: str = "", key: str = "",
                  value: str = "", top_k: int = 6, **_: Any) -> ToolResult:
        mem = ctx.memory
        if mem is None:
            # 回退到全局记忆管理器和全局数据库，避免工具在部分上下文中不可用
            try:
                from ...memory.manager import get_memory

                mem = get_memory()
            except Exception:
                return ToolResult.fail("记忆系统不可用")
        act = (action or "").lower()

        if act == "recall":
            items = await mem.recall(query or content, top_k=int(top_k or 6), session_id=ctx.session_id)
            if not items:
                return ToolResult.text(f"没有找到与 “{query}” 相关的记忆。")
            lines = [f"检索到 {len(items)} 条记忆："]
            for it in items:
                lines.append(
                    f"· [{it.kind}] {it.title}（相关度 {it.score:.2f}，{it.reason}）\n"
                    f"  {truncate(it.content, 500)}\n  ID: {it.id}"
                )
            return ToolResult(
                content="\n".join(lines),
                display=f"回忆 {len(items)} 条记忆",
                data={"items": [i.to_dict() for i in items]},
            )

        if act == "remember":
            if not content:
                return ToolResult.fail("remember 需要 content")
            item = await mem.remember(
                content,
                title=title,
                kind=kind,
                tags=tags or [],
                session_id=ctx.session_id,
                source="agent",
                importance=float(importance or 0.6),
                pinned=bool(pinned),
            )
            return ToolResult(
                content=f"已记住：{item.title}（ID {item.id}，种类 {item.kind}）",
                display=f"记住「{truncate(item.title, 30)}」",
                data={"id": item.id},
            )

        if act == "update":
            if not id:
                return ToolResult.fail("update 需要 id")
            fields: dict[str, Any] = {}
            if content:
                fields["content"] = content
            if title:
                fields["title"] = title
            if tags:
                fields["tags"] = tags
            if importance:
                fields["importance"] = float(importance)
            if pinned:
                fields["pinned"] = True
            item = mem.update(id, **fields)
            if item is None:
                return ToolResult.fail(f"未找到记忆：{id}")
            return ToolResult.text(f"已更新记忆 {id}（第 {item.revision} 版）")

        if act == "forget":
            if not id:
                return ToolResult.fail("forget 需要 id")
            ok = mem.forget(id)
            return ToolResult(ok=ok, content="已删除" if ok else "未找到该记忆")

        if act == "list":
            items = mem.list(limit=50)
            if not items:
                return ToolResult.text("记忆库为空。")
            lines = [f"共 {len(items)} 条："]
            for it in items:
                flag = "📌" if it.pinned else "  "
                lines.append(f"{flag} [{it.kind}] {it.title}  ({it.id})  {truncate(it.content, 80)}")
            return ToolResult.text("\n".join(lines), data={"items": [i.to_dict() for i in items]})

        if act == "stats":
            s = mem.stats()
            lines = [
                f"记忆总数：{s['total']}（生效 {s['active']}，置顶 {s['pinned']}）",
                f"全文索引：{'启用' if s['fts'] else '未启用'}　向量后端：{s['embedding_backend']}",
                "按种类：" + ", ".join(f"{k}={v}" for k, v in s["by_kind"].items()),
                "按范围：" + ", ".join(f"{k}={v}" for k, v in s["by_scope"].items()),
            ]
            return ToolResult.text("\n".join(lines), data=s)

        if act == "profile_get":
            data = mem.profile_get(key or None)
            if not data:
                return ToolResult.text("用户画像还是空的。")
            if isinstance(data, str):
                return ToolResult.text(f"{key}：{data}")
            return ToolResult.text(
                "用户画像：\n" + "\n".join(f"· {k}：{truncate(str(v), 200)}" for k, v in data.items()),
                data=data,
            )

        if act == "profile_set":
            if not key or not value:
                return ToolResult.fail("profile_set 需要 key 与 value")
            item = await mem.profile_set(key, value, session_id=ctx.session_id)
            return ToolResult.text(f"已更新画像项「{key}」", data={"id": item.id})

        return ToolResult.fail(f"不支持的操作：{action}")


# --------------------------------------------------------------------------
# 任务清单
# --------------------------------------------------------------------------

class TodoTool(Tool):
    name = "todo"
    # ★ 不碰工作区文件（只写数据库/状态或纯界面交互）→ 不算进工作区写租约判定
    touches_workspace = False
    group = "认知"
    description = (
        "管理当前会话的任务清单（todo list），让多步任务有据可查。"
        "action 可为：add（新增）、update（改状态/内容）、list（查看）、clear（清理）、"
        "set（一次性替换整个清单）。做复杂任务时应先列出计划，再逐项推进。"
    )
    parameters = {
        "action": {"type": "string", "enum": ["add", "update", "list", "clear", "set"], "description": "操作"},
        "title": {"type": "string", "description": "任务标题"},
        "detail": {"type": "string", "description": "任务细节"},
        "id": {"type": "string", "description": "update 时的任务 ID"},
        "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "completed", "blocked", "cancelled"],
            "description": "任务状态",
        },
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "set 时的任务列表：[{title, detail, status}]",
        },
        "only_done": {"type": "boolean", "description": "clear 时只清理已完成项"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", title: str = "", detail: str = "",
                  id: str = "", status: str = "", items: list[dict] | None = None,
                  only_done: bool = False, **_: Any) -> ToolResult:
        store = _task_store(ctx)
        if store is None:
            return ToolResult.fail("任务存储不可用")
        act = (action or "").lower()

        if act == "add":
            if not title:
                return ToolResult.fail("add 需要 title")
            t = store.add(title, detail=detail, session_id=ctx.session_id)
            ctx.emit("task.update", {"action": "add", "task": t, "summary": store.summary(ctx.session_id)})
            return ToolResult(
                content=f"已添加任务 {t['id']}：{title}\n\n{store.render(ctx.session_id)}",
                display=f"加任务「{truncate(title, 24)}」",
                data=t,
            )

        if act == "update":
            if not id:
                return ToolResult.fail("update 需要 id")
            fields: dict[str, Any] = {}
            if status:
                fields["status"] = status
            if title:
                fields["title"] = title
            if detail:
                fields["detail"] = detail
            t = store.update(id, **fields)
            if t is None:
                return ToolResult.fail(f"未找到任务：{id}")
            ctx.emit("task.update", {"action": "update", "task": t, "summary": store.summary(ctx.session_id)})
            return ToolResult(
                content=f"任务已更新：{t['title']} → {t['status']}\n\n{store.render(ctx.session_id)}",
                display=f"任务 {t['status']}",
                data=t,
            )

        if act == "list":
            ctx.emit("task.update", {"action": "list", "summary": store.summary(ctx.session_id)})
            return ToolResult.text(store.render(ctx.session_id), data=store.summary(ctx.session_id))

        if act == "clear":
            n = store.clear(ctx.session_id, only_done=only_done)
            ctx.emit("task.update", {"action": "clear", "summary": store.summary(ctx.session_id)})
            return ToolResult.text(f"已清理 {n} 项任务。")

        if act == "set":
            store.clear(ctx.session_id)
            created = []
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                t = store.add(
                    str(it.get("title") or "未命名"),
                    detail=str(it.get("detail") or ""),
                    session_id=ctx.session_id,
                )
                st = str(it.get("status") or "pending")
                if st != "pending":
                    store.set_status(t["id"], st)
                created.append(t["id"])
            ctx.emit("task.update", {"action": "set", "summary": store.summary(ctx.session_id)})
            return ToolResult(
                content=f"已设置 {len(created)} 项任务：\n\n{store.render(ctx.session_id)}",
                display=f"设置 {len(created)} 项任务",
            )

        return ToolResult.fail(f"不支持的操作：{action}")


def _task_store(ctx: ToolContext):
    """取任务存储：优先用上下文里的 db，缺失时回退到全局数据库。"""
    db = ctx.db
    if db is None:
        try:
            from ...storage.db import get_db

            db = get_db()
        except Exception:
            return None
    try:
        from ...storage.tasks import TaskStore

        return TaskStore(db)
    except Exception:
        return None


class GoalTool(Tool):
    name = "goal"
    # ★ 不碰工作区文件（只写数据库/状态或纯界面交互）→ 不算进工作区写租约判定
    touches_workspace = False
    group = "认知"
    description = (
        "管理跨轮次的长期目标。当用户交办一个需要多轮才能完成的大任务时，"
        "用 action=set 记录目标与最大轮次；每完成一轮用 action=progress 记录进展。"
    )
    parameters = {
        "action": {"type": "string", "enum": ["set", "get", "progress", "complete", "blocked", "list"], "description": "操作"},
        "objective": {"type": "string", "description": "set 时的目标描述"},
        "max_rounds": {"type": "integer", "description": "最大轮次限制"},
        "note": {"type": "string", "description": "progress 时的进展说明"},
        "reason": {"type": "string", "description": "blocked 时的阻塞原因"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", objective: str = "",
                  max_rounds: int | None = None, note: str = "", reason: str = "",
                  **_: Any) -> ToolResult:
        store = _task_store(ctx)
        if store is None:
            return ToolResult.fail("任务存储不可用")
        act = (action or "").lower()

        if act == "set":
            if not objective:
                return ToolResult.fail("set 需要 objective")
            g = store.set_goal(objective, session_id=ctx.session_id, max_rounds=max_rounds)
            ctx.emit("goal.update", {"goal": g})
            return ToolResult.text(
                f"已设定目标（ID {g['id']}，阶段 {g['phase']}，轮次 {g['rounds']}/{g['max_rounds'] or '∞'}）：\n{objective}",
                data=g,
            )

        if act == "get":
            g = store.get_goal_for(ctx.session_id) if ctx.session_id else None
            if g is None:
                goals = store.list_goals(active_only=True)
                g = goals[0] if goals else None
            if g is None:
                return ToolResult.text("当前没有进行中的目标。")
            return ToolResult.text(_goal_text(g), data=g)

        if act == "progress":
            g = store.get_goal_for(ctx.session_id)
            if g is None:
                return ToolResult.fail("当前没有进行中的目标，请先 set")
            store.bump_round(g["id"])
            if note:
                store.update_goal(g["id"], meta={"last_note": note})
            g = store.get_goal(g["id"])
            ctx.emit("goal.update", {"goal": g})
            return ToolResult.text(f"已记录进展：{note or '（无说明）'}\n\n{_goal_text(g)}", data=g)

        if act == "complete":
            g = store.get_goal_for(ctx.session_id)
            if g is None:
                return ToolResult.text("没有进行中的目标。")
            store.update_goal(g["id"], phase="completed")
            g = store.get_goal(g["id"])
            ctx.emit("goal.update", {"goal": g})
            return ToolResult.text(f"目标已完成：{g['objective']}", data=g)

        if act == "blocked":
            g = store.get_goal_for(ctx.session_id)
            if g is None:
                return ToolResult.text("没有进行中的目标。")
            store.update_goal(g["id"], phase="blocked", blocker=reason)
            g = store.get_goal(g["id"])
            ctx.emit("goal.update", {"goal": g})
            return ToolResult.text(f"目标已标记为阻塞：{reason or '（未说明原因）'}", data=g)

        if act == "list":
            goals = store.list_goals()
            if not goals:
                return ToolResult.text("没有目标记录。")
            lines = ["目标列表："]
            for g in goals[:20]:
                lines.append(
                    f"· [{g['phase']}] {truncate(g['objective'], 80)}（{g['rounds']} 轮，ID {g['id']}）"
                )
            return ToolResult.text("\n".join(lines), data={"goals": goals[:20]})

        return ToolResult.fail(f"不支持的操作：{action}")


def _goal_text(g: dict) -> str:
    lines = [
        f"目标：{g['objective']}",
        f"阶段：{g['phase']}　轮次：{g['rounds']}/{g['max_rounds'] or '∞'}",
    ]
    if g.get("blocker"):
        lines.append(f"阻塞原因：{g['blocker']}")
    meta = g.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    if meta.get("last_note"):
        lines.append(f"最近进展：{meta['last_note']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 技能
# --------------------------------------------------------------------------

class SkillTool(Tool):
    name = "skill"
    # ★ 不碰工作区文件（只写数据库/状态或纯界面交互）→ 不算进工作区写租约判定
    touches_workspace = False
    group = "认知"
    description = (
        "技能管理。action 可为：list（列出全部技能）、load（读取某个技能全文并按其执行）、"
        "search（按关键词找技能）、info（查看技能详情）。"
        "当用户的请求与某个技能匹配时，应先 load 该技能再按其指引操作。"
    )
    parameters = {
        "action": {"type": "string", "enum": ["list", "load", "search", "info"], "description": "操作"},
        "name": {"type": "string", "description": "技能名（load/info）"},
        "query": {"type": "string", "description": "搜索关键词（search）"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", name: str = "", query: str = "",
                  **_: Any) -> ToolResult:
        skills = ctx.skills
        if skills is None:
            return ToolResult.fail("技能系统未启用")
        act = (action or "").lower()

        if act == "list":
            items = skills.list_all()
            if not items:
                return ToolResult.text("没有可用技能。可在「技能」面板添加，或把 SKILL.md 放进技能目录。")
            lines = [f"共 {len(items)} 个技能："]
            for s in items:
                mark = "✓" if s.get("enabled", True) else "✗"
                lines.append(f"{mark} {s['name']}　[{s.get('source', '?')}]　{truncate(s.get('description', ''), 90)}")
            return ToolResult.text("\n".join(lines), data={"skills": items})

        if act == "search":
            hits = skills.search(query or name)
            if not hits:
                return ToolResult.text(f"没有匹配 “{query}” 的技能。")
            lines = [f"找到 {len(hits)} 个相关技能："]
            for s in hits:
                lines.append(f"· {s['name']}　{truncate(s.get('description', ''), 100)}")
            return ToolResult.text("\n".join(lines), data={"skills": hits})

        if act in ("load", "info"):
            if not name:
                return ToolResult.fail(f"{act} 需要提供技能名")
            detail = skills.load(name)
            if detail is None:
                candidates = [s["name"] for s in skills.list_all()][:30]
                return ToolResult.fail(f"没有找到技能 “{name}”。可用技能：{', '.join(candidates)}")
            if act == "info":
                return ToolResult.text(
                    f"技能：{detail['name']}\n来源：{detail.get('source')}\n路径：{detail.get('path')}\n"
                    f"说明：{detail.get('description')}\n字数：{len(detail.get('body') or '')}",
                    data=detail,
                )
            skills.mark_used(name)
            return ToolResult(
                content=(
                    f"已加载技能「{detail['name']}」。请严格按以下指引完成用户的请求"
                    f"（该技能可能带有脚本/资源，路径见 SKILL_DIR）：\n\n"
                    f"SKILL_DIR = {detail.get('dir', '')}\n\n"
                    f"{detail.get('body', '')}"
                ),
                display=f"加载技能「{detail['name']}」",
                data={"name": detail["name"], "dir": detail.get("dir")},
            )

        return ToolResult.fail(f"不支持的操作：{action}")


# --------------------------------------------------------------------------
# 子智能体
# --------------------------------------------------------------------------

class SubAgentTool(Tool):
    name = "subagent"
    group = "认知"
    dangerous = True
    description = (
        "派生一个子智能体去独立完成一项任务（拥有独立上下文与工具集），只把最终结论返回。"
        "适合：探索陌生代码库、并行研究多个方案、独立评审代码、搜索大量信息。"
        "内置类型：general（通用）、explore（探索）、research（研究）、review（评审）、"
        "security-review（安全审计）、test（测试）、plan（计划）。"
        "也支持 action=parallel 并行跑多个任务、action=debate 让多个子代理辩论。"
        "action=fleet 可按依赖图跑（任务可带 depends_on 指向其它任务的名字或序号，"
        "上游结论会自动带进下游上下文，'先调研再实现'不必重复调研）。"
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["run", "parallel", "chain", "fleet", "debate", "list"],
            "description": "run 单个 / parallel 并行 / chain 顺序链 / fleet 按依赖图跑 / debate 辩论 / list 列出类型",
        },
        "task": {"type": "string", "description": "交给子智能体的任务描述（要具体、自包含）"},
        "agent_type": {"type": "string", "description": "子智能体类型，默认 general"},
        "tasks": {
            "type": "array",
            "items": {"type": "object"},
            "description": "parallel/chain 时的任务列表：[{task, agent_type, name}]",
        },
        "question": {"type": "string", "description": "debate 时的议题"},
        "models": {"type": "array", "items": {"type": "string"}, "description": "debate 时各参与者的模型"},
        "model": {"type": "string", "description": "指定子智能体使用的模型（provider/model）"},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "限定可用工具名"},
        "budget_tokens": {"type": "integer", "description": "token 预算"},
        "timeout": {"type": "number", "description": "超时秒数"},
        "context": {"type": "string", "description": "额外背景信息"},
    }
    required = ["action"]

    async def run(self, ctx: ToolContext, action: str = "", task: str = "", agent_type: str = "general",
                  tasks: list[dict] | None = None, question: str = "", models: list[str] | None = None,
                  model: str = "", tools: list[str] | None = None, budget_tokens: int = 0,
                  timeout: float = 0, context: str = "", **_: Any) -> ToolResult:
        runner = ctx.subagent_runner
        if runner is None:
            return ToolResult.fail("子智能体系统未启用")
        act = (action or "").lower()

        if act == "list":
            infos = runner.list_types()
            lines = ["可用子智能体类型："]
            for i in infos:
                lines.append(f"· {i['name']}　{i['label']}　可用工具：{len(i.get('tools') or [])} 个\n  {i['description']}")
            return ToolResult.text("\n".join(lines), data={"types": infos})

        kwargs: dict[str, Any] = {}
        if model:
            kwargs["model"] = model
        if tools:
            kwargs["tools"] = tools
        if budget_tokens:
            kwargs["budget_tokens"] = int(budget_tokens)
        if timeout:
            kwargs["timeout"] = float(timeout)

        if act == "run":
            if not task:
                return ToolResult.fail("run 需要 task")
            res = await runner.run(task, agent_type=agent_type, context=context, **kwargs)
            return _subagent_result(res, label=f"子智能体（{agent_type}）")

        if act == "parallel":
            if not tasks:
                return ToolResult.fail("parallel 需要 tasks")
            res = await runner.run_parallel(tasks, context=context, **kwargs)
            return _subagent_result(res, label=f"并行子智能体（{len(tasks)} 个）")

        if act == "chain":
            if not tasks:
                return ToolResult.fail("chain 需要 tasks")
            res = await runner.run_chain(tasks, context=context, **kwargs)
            return _subagent_result(res, label=f"顺序链（{len(tasks)} 步）")

        if act == "fleet":
            # ★ 舰队：按依赖图跑，依赖边**携带上游答案**给下游。
            #   与 parallel（互不依赖全平行）/ chain（严格一线串行）的区别：
            #   允许任意 DAG —— 每个任务只等自己那几个上游，其余并行。
            #   "先调研再实现"这种结构因此不必把调研结论重讲一遍（省一次钱）。
            if not tasks:
                return ToolResult.fail("fleet 需要 tasks（可带 depends_on）")
            res = await runner.run_fleet(
                tasks, context=context,
                concurrency=int(kwargs.get("concurrency") or 0),
                fail_fast=bool(kwargs.get("fail_fast")),
            )
            return _subagent_result(res, label=f"子代理舰队（{len(tasks)} 个）")

        if act == "debate":
            topic = question or task
            if not topic:
                return ToolResult.fail("debate 需要 question 或 task")
            res = await runner.debate(topic, models=models or [], context=context)
            return _subagent_result(res, label="多智能体辩论")

        return ToolResult.fail(f"不支持的操作：{action}")


def _subagent_result(res: Any, *, label: str) -> ToolResult:
    if isinstance(res, dict):
        ok = res.get("ok", True)
        content = res.get("content") or res.get("result") or ""
        return ToolResult(
            ok=ok,
            content=content,
            error=res.get("error"),
            display=f"{label} 完成",
            data=res,
        )
    return ToolResult.text(str(res), display=f"{label} 完成")


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------

class PlanTool(Tool):
    name = "plan"
    group = "认知"
    read_only = True
    description = (
        "让模型先做一份结构化计划（不执行任何操作），返回给当前对话参考，"
        "同时把计划写入任务清单。用于把复杂需求拆成可验证的步骤。"
    )
    parameters = {
        "objective": {"type": "string", "description": "要达成的目标"},
        "constraints": {"type": "string", "description": "约束条件/偏好"},
        "max_steps": {"type": "integer", "description": "最多步骤数，默认 8"},
        "write_todo": {"type": "boolean", "description": "是否把计划写入任务清单，默认 true"},
    }
    required = ["objective"]

    async def run(self, ctx: ToolContext, objective: str = "", constraints: str = "",
                  max_steps: int = 8, write_todo: bool = True, **_: Any) -> ToolResult:
        if not objective:
            return ToolResult.fail("objective 不能为空")
        llm = ctx.llm
        if llm is None:
            return ToolResult.fail("模型不可用，无法生成计划")
        from ...llm.types import Message

        prompt = (
            f"目标：{objective}\n"
            + (f"约束与偏好：{constraints}\n" if constraints else "")
            + f"\n请把目标拆解为不超过 {int(max_steps)} 个可执行步骤。"
            "每一步都要：一句话说明做什么、怎么验证做完了、可能的风险。"
            "用 Markdown 有序列表输出，不要输出任何多余前言。"
        )
        resp = await llm.chat(
            [Message.system("你是严谨的任务规划器，输出简洁、可执行、可验证的计划。"),
             Message.user(prompt)],
            temperature=0.2,
            max_tokens=2048,
        )
        if resp.error:
            return ToolResult.fail(f"生成计划失败：{resp.error}")
        text = (resp.content or "").strip()
        steps = _parse_plan_steps(text)
        if write_todo and steps:
            store = _task_store(ctx)
            if store is not None and (not store.list(session_id=ctx.session_id)):
                for s in steps:
                    store.add(s["title"], detail=s.get("detail", ""), session_id=ctx.session_id)
                ctx.emit("task.update", {"action": "set", "summary": store.summary(ctx.session_id)})
        return ToolResult(
            content=f"计划（{len(steps)} 步）：\n\n{text}",
            display=f"生成计划（{len(steps)} 步）",
            data={"steps": steps, "plan": text},
        )


def _parse_plan_steps(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for line in (text or "").split("\n"):
        m = None
        import re

        m = re.match(r"^\s*(?:\d+[.、)]|[-*])\s+(.+)$", line)
        if not m:
            continue
        body = m.group(1).strip()
        if not body:
            continue
        title = body.split("：")[0].split(":")[0].strip()
        out.append({"title": title[:120], "detail": body[:400]})
        if len(out) >= 20:
            break
    return out


# --------------------------------------------------------------------------
# 提问
# --------------------------------------------------------------------------

class AskTool(Tool):
    name = "ask_user"
    group = "认知"
    # ★ 不碰工作区文件（纯界面交互：问一句、等用户选）→ 不算进工作区写租约判定。
    touches_workspace = False
    description = (
        "向用户提问以澄清需求（当信息不足、且无法用合理默认值推进时使用）。"
        "提供 2-4 个候选项让用户直接选，比开放式提问更省事。"
        "注意：能自己决定的事不要问；只在真正需要用户决策时使用。"
    )
    parameters = {
        "question": {"type": "string", "description": "要问用户的问题"},
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "候选选项（2-4 个，推荐项放第一个）",
        },
        "multi_select": {"type": "boolean", "description": "是否允许多选"},
        "context": {"type": "string", "description": "为什么需要问（帮助用户判断）"},
    }
    required = ["question"]

    async def run(self, ctx: ToolContext, question: str = "", options: list[str] | None = None,
                  multi_select: bool = False, context: str = "", **_: Any) -> ToolResult:
        from ...utils import new_id

        if not question:
            return ToolResult.fail("question 不能为空")
        qid = new_id("q")
        payload = {
            "id": qid,
            "question": question,
            "options": options or [],
            "multi_select": bool(multi_select),
            "context": context,
        }
        # 若设置了答案回调（CLI/Web 会注册），等待用户回答
        asker = ctx.extra.get("asker")
        ctx.emit("ask.user", payload)
        if asker is None:
            return ToolResult(
                content=(
                    f"（当前没有交互通道，无法等待用户回答）问题已记录：{question}"
                    + (f"\n候选：{' / '.join(options or [])}" if options else "")
                ),
                display="提问（无交互通道）",
                data=payload,
            )
        try:
            answer = await asyncio.wait_for(asker(payload), timeout=float(ctx.extra.get("ask_timeout", 600)))
        except asyncio.TimeoutError:
            return ToolResult(ok=False, content="用户未在时限内回答。", display="提问超时")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return ToolResult.fail(f"提问失败：{e}")
        if answer is None:
            return ToolResult(ok=False, content="用户未作答。", display="提问未作答")
        ctx.emit("ask.done", {"id": qid, "answer": answer})
        return ToolResult(
            content=f"用户的回答：{answer}",
            display="已获得用户回答",
            data={"id": qid, "answer": answer},
        )


class ContextBudgetTool(Tool):
    name = "context_budget"
    group = "认知"
    read_only = True
    description = (
        "查看当前会话还剩多少上下文空间（估算）。"
        "返回窗口大小、已用 token、剩余量与已用比例。"
        "当你在长会话里准备再读一批文件、或想把大段内容塞进上下文之前，先查一下，"
        "避免盲目重读导致上下文爆掉。"
    )
    parameters = {
        "detail": {
            "type": "boolean",
            "description": "是否附带各来源的占用明细（默认 false）",
        },
    }
    required: list[str] = []

    async def run(self, ctx: ToolContext, detail: bool = False, **_: Any) -> ToolResult:
        agent = ctx.agent
        if agent is None or not hasattr(agent, "_context_budget"):
            return ToolResult.fail("当前环境不支持查询上下文预算")
        window, unlimited = agent._context_budget(ctx.session_id or "")
        # 已用量取「最后一次上游调用的输入 token」——那才是此刻真实占用
        used = 0
        try:
            sess = agent.sessions.get(ctx.session_id or "") or {}
            used = int(sess.get("input_tokens") or 0)
        except Exception:
            used = 0
        if unlimited or window <= 0:
            return ToolResult(
                content=(f"上下文窗口：未限制（由上游决定）\n"
                         f"当前已用：约 {used} tokens\n"
                         f"提示：未设置窗口时本地不做压缩，请自行控制单次读取量。"),
                display="上下文未限制",
                data={"window": 0, "unlimited": True, "used": used},
            )
        left = max(0, window - used)
        pct = round(used / window * 100, 1) if window else 0.0
        lines = [
            f"上下文窗口：{window} tokens",
            f"已用：{used} tokens（{pct}%）",
            f"剩余：{left} tokens",
        ]
        if detail:
            try:
                acc = int((sess or {}).get("output_tokens") or 0)
                lines.append(f"明细：输入 {used} / 输出 {acc}")
            except Exception:
                pass
        if pct >= 92:
            lines.append("⚠ 已接近上限，建议先压缩或收敛范围，不要再大批量读文件。")
        elif pct >= 75:
            lines.append("提示：已用超过四分之三，注意控制后续读取量。")
        return ToolResult(
            content="\n".join(lines),
            display=f"上下文 {pct}%",
            data={"window": window, "unlimited": False, "used": used,
                  "left": left, "pct": pct},
        )


class SearchDocsTool(Tool):
    name = "search_docs"
    group = "认知"
    read_only = True
    description = (
        "检索本应用自带的说明文档（README / 使用指南 / 架构说明）。"
        "当用户问「这个版本有什么新东西」「怎么配置某个功能」「某个能力在哪」时，"
        "先用它查文档再回答，避免凭印象编造。"
    )
    parameters = {
        "query": {"type": "string", "description": "检索关键词（中文或英文）"},
        "limit": {"type": "integer", "description": "最多返回几个片段，默认 5"},
    }
    required = ["query"]

    async def run(self, ctx: ToolContext, query: str = "", limit: int = 5, **_: Any) -> ToolResult:
        q = (query or "").strip()
        if not q:
            return ToolResult.fail("query 不能为空")
        hits = _search_bundled_docs(q, limit=max(1, min(12, int(limit or 5))))
        if not hits:
            return ToolResult(
                content=f"文档里没有找到与「{q}」相关的内容。可先用 list_dir 看 docs/ 目录。",
                display="文档无匹配",
                data={"hits": []},
            )
        lines = [f"文档检索「{q}」，命中 {len(hits)} 段：", ""]
        for h in hits:
            lines.append(f"## {h['file']}（第 {h['line']} 行）")
            lines.append(h["text"])
            lines.append("")
        lines.append("（以上为文档原文片段；如需全上下文可用 read_file 打开对应文件）")
        return ToolResult(
            content="\n".join(lines),
            display=f"文档命中 {len(hits)} 段",
            data={"hits": hits},
        )


def _doc_roots() -> list[Any]:
    """应用自带文档的位置（源码树与打包后都尽量能取到）。

    ★ 为什么这样做：打包成 exe 后仓库结构不一定还在，所以按「可能存在」
    依次探测，取到几个算几个 —— 取不到就返回空，工具会如实说「没找到」，
    而不是抛错。
    ★ 实测踩到的坑：`repo_root()` 在源码树下返回的是 **cli 目录**，
      而 `docs/` 在它的上一级（仓库根）。只查 repo_root() 会漏掉
      docs/architecture.md 与 docs/getting-started.md —— 检索等于半个残废。
      因此这里把「上一级」也纳入探测。
    """
    from pathlib import Path as _P

    from ... import paths as _paths

    cands: list[Any] = []
    try:
        root = _paths.repo_root()
        cands.extend([root / "docs", root, root / "cli"])
        cands.extend([root.parent / "docs", root.parent])
    except Exception:
        pass
    try:
        pkg = _paths.package_root()
        cands.extend([pkg / "docs", pkg.parent.parent / "docs", pkg.parent.parent])
    except Exception:
        pass
    out = []
    seen = set()
    for c in cands:
        try:
            p = _P(c)
            if p.is_dir() and str(p) not in seen:
                seen.add(str(p))
                out.append(p)
        except Exception:
            continue
    return out


def _search_bundled_docs(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """在自带文档里做关键词检索，返回带文件名与行号的片段。

    实现刻意保持简单：逐文件按行扫，命中的行连同**前后各一行**一起返回
    （文档里结论常在标题行，正文在下一行，只看命中行往往读不懂）。
    中文按字符匹配即可，不需要分词 —— 文档量小，够用且零依赖。
    """
    q = query.strip().lower()
    if not q:
        return []
    # 中文查询：按字符切出候选词，命中任一即算（避免长句整串匹配不上）
    terms = [q]
    if any("\u4e00" <= ch <= "\u9fff" for ch in q) and len(q) > 2:
        terms = [q[i:i + 2] for i in range(len(q) - 1)]
    hits: list[dict[str, Any]] = []
    for root in _doc_roots():
        try:
            files = sorted(root.glob("*.md"))
        except Exception:
            continue
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                low = line.lower()
                if not any(t in low for t in terms):
                    continue
                ctx = "\n".join(lines[max(0, i - 1): min(len(lines), i + 2)]).strip()
                hits.append({"file": f.name, "line": i + 1, "text": truncate(ctx, 500),
                             "path": str(f)})
                if len(hits) >= limit * 3:
                    break
    return hits[:limit]


TOOLS = [
    MemoryTool,
    TodoTool,
    GoalTool,
    SkillTool,
    SubAgentTool,
    PlanTool,
    AskTool,
    ContextBudgetTool,
    SearchDocsTool,
]
