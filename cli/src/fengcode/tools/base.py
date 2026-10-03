"""工具基类与上下文。

每个工具都是一个 ``Tool`` 子类，声明名称、说明、JSON Schema 与处理函数。
``ToolContext`` 提供运行期依赖（工作区、沙箱、审批、事件、记忆…），
让工具实现保持无状态、易测试。
"""

from __future__ import annotations

import abc
import asyncio
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..llm.types import ToolSpec


@dataclass
class ToolResult:
    """工具执行结果。"""

    ok: bool = True
    content: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration: float = 0.0
    display: str = ""          # 给界面展示的简短描述（如"写入 1.2 KB"）
    files: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_model_text(self) -> str:
        """转成喂给模型的文本。"""
        if self.error and not self.content:
            return f"【错误】{self.error}"
        text = self.content or ""
        if self.error:
            text = f"【错误】{self.error}\n{text}".strip()
        return text or "（无输出）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "duration": round(self.duration, 3),
            "display": self.display,
            "files": self.files,
            "truncated": self.truncated,
            "data": self.data,
        }

    @staticmethod
    def fail(message: str, **data: Any) -> "ToolResult":
        return ToolResult(ok=False, error=message, content="", data=data)

    @staticmethod
    def text(content: str, **data: Any) -> "ToolResult":
        return ToolResult(ok=True, content=content, data=data)


@dataclass
class ToolContext:
    """工具执行上下文。"""

    workspace: Path
    config: Any = None
    sandbox: Any = None
    guard: Any = None
    approval: Any = None
    bus: Any = None
    session_id: str | None = None
    memory: Any = None
    skills: Any = None
    llm: Any = None
    mcp: Any = None
    scheduler: Any = None
    remote: Any = None
    db: Any = None
    agent: Any = None
    subagent_runner: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    cancel: asyncio.Event | None = None
    depth: int = 0

    def emit(self, type: str, data: dict[str, Any] | None = None) -> None:
        if self.bus is not None:
            try:
                self.bus.emit(type, data or {}, session_id=self.session_id)
            except Exception:
                pass

    def sub_context(self, **overrides: Any) -> "ToolContext":
        """派生一个子上下文（子智能体 / 后台任务用）。"""
        import dataclasses

        kw = {
            "workspace": self.workspace,
            "config": self.config,
            "sandbox": self.sandbox,
            "guard": self.guard,
            "approval": self.approval,
            "bus": self.bus,
            "session_id": self.session_id,
            "memory": self.memory,
            "skills": self.skills,
            "llm": self.llm,
            "mcp": self.mcp,
            "scheduler": self.scheduler,
            "remote": self.remote,
            "db": self.db,
            "agent": self.agent,
            "subagent_runner": self.subagent_runner,
            "extra": dict(self.extra),
            "cancel": self.cancel,
            "depth": self.depth,
        }
        kw.update(overrides)
        return ToolContext(**kw)


class Tool(abc.ABC):
    """工具基类。"""

    name: str = ""
    description: str = ""
    group: str = "通用"
    dangerous: bool = False
    # 是否可在只读模式下使用
    read_only: bool = False
    # 是否需要审批才执行（交给审批门判断时置 False，由危险模式自动判定）
    requires_approval: bool = False
    # 参数 schema（JSON Schema 的 properties 部分）
    parameters: dict[str, Any] = {}
    required: list[str] = []
    # 单次输出上限
    max_output_chars: int = 40_000

    def spec(self) -> ToolSpec:
        params = self.parameters or {}
        # ★ 允许两种写法，避免「双重包装」：
        #   1) 纯 properties：{"path": {"type": "string"}}
        #   2) 完整 JSON Schema：{"type": "object", "properties": {...}}
        # 插件注册工具时给的是完整 schema（见 PluginTool.__init__），
        # 这里若再包一层就会变成
        #   {"type":"object","properties":{"type":"object","properties":{...}}}
        # 上游校验时报 `"object" is not of types "boolean", "object"` 直接 400。
        # 判据必须严格：只有 type 明确等于字符串 "object" 且带 properties 才算完整 schema，
        # 否则「参数名叫 type」的工具会被误判。
        is_full_schema = isinstance(params, dict) and (
            (params.get("type") == "object" and "properties" in params)
            or "$schema" in params
        )
        if is_full_schema:
            schema = dict(params)
            schema.setdefault("type", "object")
            if self.required and "required" not in schema:
                schema["required"] = list(self.required)
        else:
            schema = {"type": "object", "properties": params}
            if self.required:
                schema["required"] = list(self.required)
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=schema,
            source="builtin",
            dangerous=self.dangerous,
            group=self.group,
        )

    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        """执行工具。"""

    # ---- 辅助 ---------------------------------------------------------
    def _truncate(self, text: str) -> tuple[str, bool]:
        limit = self.max_output_chars
        if len(text) <= limit:
            return text, False
        from ..utils import truncate_middle

        return truncate_middle(text, limit), True

    def _arg(self, kwargs: dict[str, Any], key: str, default: Any = None) -> Any:
        v = kwargs.get(key, default)
        return default if v is None else v


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

class ToolRegistry:
    """工具注册表：注册、查找、生成 schema、执行（含审批与审计）。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}
        self._disabled: set[str] = set()

    # ---- 注册 ----------------------------------------------------------
    def register(self, tool: Tool, *, alias: list[str] | None = None) -> Tool:
        if not tool.name:
            raise ValueError("工具必须有名称")
        self._tools[tool.name] = tool
        for a in alias or []:
            self._aliases[a] = tool.name
        return tool

    def unregister(self, name: str) -> bool:
        real = self._aliases.pop(name, name)
        return self._tools.pop(real, None) is not None

    def get(self, name: str) -> Tool | None:
        if name in self._tools:
            return self._tools[name]
        real = self._aliases.get(name)
        if real:
            return self._tools.get(real)
        # 容忍 MCP 风格前缀与大小写差异
        low = name.lower()
        for k, v in self._tools.items():
            if k.lower() == low:
                return v
        return None

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def disable(self, name: str) -> None:
        self._disabled.add(name)

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for t in self._tools.values():
            out.setdefault(t.group or "通用", []).append(t.name)
        return {k: sorted(v) for k, v in out.items()}

    # ---- schema --------------------------------------------------------
    def specs(
        self,
        *,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        groups: list[str] | None = None,
        include_mcp: bool = True,
    ) -> list[ToolSpec]:
        allow_set = set(allow) if allow else None
        deny_set = set(deny or [])
        group_set = set(groups) if groups else None
        out: list[ToolSpec] = []
        for t in self._tools.values():
            if t.name in self._disabled or t.name in deny_set:
                continue
            if allow_set is not None and t.name not in allow_set:
                continue
            if group_set is not None and (t.group or "通用") not in group_set:
                continue
            out.append(t.spec())
        return out

    # ---- 执行 ----------------------------------------------------------
    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        ctx: ToolContext,
        *,
        skip_approval: bool = False,
    ) -> ToolResult:
        """执行工具：审批 → 执行 → 审计。"""
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail(f"未知工具：{name}（可用工具：{', '.join(self.names()[:40])}）")
        if tool.name in self._disabled:
            return ToolResult.fail(f"工具已被禁用：{tool.name}")

        args = _coerce_args(tool, arguments or {})
        t0 = time.time()

        # 审批
        if not skip_approval and ctx.approval is not None:
            command = str(args.get("command") or args.get("cmd") or "")
            path = str(args.get("path") or args.get("file_path") or args.get("target") or "")
            allowed, reason, req = ctx.approval.evaluate(
                tool.name,
                command=command,
                path=path,
                dangerous=tool.dangerous or tool.requires_approval,
                session_id=ctx.session_id,
            )
            if not allowed:
                if req is None:
                    ctx.emit("audit", {"action": tool.name, "target": command or path,
                                       "decision": "deny", "reason": reason})
                    return ToolResult.fail(f"操作被安全策略拒绝：{reason}")
                ctx.emit("approval.request", req.to_dict())
                dec = await ctx.approval.request(req)
                ctx.emit("approval.done", {"id": req.id, **dec.to_dict()})
                if not dec.allowed:
                    return ToolResult.fail(f"用户未批准该操作：{dec.reason}")

        ctx.emit("tool.start", {"name": tool.name, "arguments": args, "group": tool.group})

        try:
            result = await tool.run(ctx, **args)
        except asyncio.CancelledError:
            ctx.emit("tool.end", {"name": tool.name, "ok": False, "error": "已取消"})
            raise
        except Exception as e:
            result = ToolResult.fail(f"{type(e).__name__}: {e}")
        result.duration = time.time() - t0

        # 输出截断
        text, trunc = self._apply_limit(tool, result.content)
        if trunc:
            result.content = text
            result.truncated = True

        ctx.emit(
            "tool.end",
            {
                "name": tool.name,
                "ok": result.ok,
                "error": result.error,
                "duration": result.duration,
                "display": result.display,
                "preview": result.content[:2000],
                # ★ 真实绝对路径（files 是 resolve 后的绝对路径）。
                #   模型侧仍看 content 里的 ~/xxx（省 token），但**界面**必须给出
                #   完整路径：用户实测「AI 说写好了，我去桌面找不到」——
                #   因为 ~ 其实是 fengcode-data\workspace，只是显示成了 ~。
                "files": list(result.files or []),
            },
        )
        self._audit(ctx, tool, args, result)
        return result

    @staticmethod
    def _apply_limit(tool: Tool, text: str) -> tuple[str, bool]:
        limit = tool.max_output_chars or 40_000
        if len(text) <= limit:
            return text, False
        # 头尾保留（提示语留在两端，防止被后续更紧的截断二次切掉）
        from ..tools.builtin.files import truncate_head_tail

        return truncate_head_tail(text, limit, reason="工具输出过长"), True

    @staticmethod
    def _audit(ctx: ToolContext, tool: Tool, args: dict[str, Any], result: ToolResult) -> None:
        if ctx.db is None:
            return
        try:
            from ..storage.stats import AuditStore

            target = str(args.get("command") or args.get("path") or args.get("query") or "")[:500]
            AuditStore(ctx.db).log(
                action=tool.name,
                target=target,
                decision="allow",
                reason="执行完成" if result.ok else (result.error or "执行失败"),
                detail=result.content[:4000],
                session_id=ctx.session_id,
                ok=result.ok,
            )
        except Exception:
            pass


def _coerce_args(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """把模型给的参数做轻度归一化：类型纠正、别名映射。"""
    if not isinstance(args, dict):
        return {}
    props = tool.parameters or {}
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k == "_raw" and isinstance(v, str):
            # 参数解析失败的原始串：整体当作第一个必填参数
            if tool.required:
                out[tool.required[0]] = v
            continue
        spec = props.get(k)
        if spec is not None:
            t = spec.get("type")
            if t == "integer" and isinstance(v, str) and v.strip().lstrip("-").isdigit():
                v = int(v)
            elif t == "number" and isinstance(v, str):
                try:
                    v = float(v)
                except ValueError:
                    pass
            elif t == "boolean" and isinstance(v, str):
                v = v.strip().lower() in ("true", "1", "yes", "是", "y")
            elif t == "array" and isinstance(v, str):
                v = [x.strip() for x in v.split(",") if x.strip()]
            elif t == "string" and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        out[k] = v
    # 常见别名
    for alias, real in _ARG_ALIASES.items():
        if alias in out and real not in out and real in props:
            out[real] = out.pop(alias)
    return out


_ARG_ALIASES = {
    "file": "path",
    "filepath": "path",
    "filename": "path",
    "file_path": "path",
    "dir": "path",
    "directory": "path",
    "cmd": "command",
    "shell_command": "command",
    "pattern": "query",
    "keyword": "query",
    "text": "content",
    "body": "content",
    "old": "old_string",
    "new": "new_string",
}


# ---- 全局注册表 ----------------------------------------------------------

_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None


def tool(
    name: str | None = None,
    *,
    description: str = "",
    group: str = "通用",
    dangerous: bool = False,
    read_only: bool = False,
    parameters: dict[str, Any] | None = None,
    required: list[str] | None = None,
    registry: ToolRegistry | None = None,
) -> Callable:
    """把普通函数注册为工具的装饰器。

    用法::

        @tool("echo", description="回显", parameters={"text": {"type": "string"}})
        async def echo(ctx, text=""):
            return ToolResult.text(text)
    """

    def deco(fn: Callable) -> Callable:
        tname = name or fn.__name__
        is_async = inspect.iscoroutinefunction(fn)

        class _FnTool(Tool):
            pass

        def _make_sig(f: Callable) -> tuple[dict[str, Any], list[str]]:
            if parameters is not None:
                return dict(parameters), list(required or [])
            sig = inspect.signature(f)
            props: dict[str, Any] = {}
            req: list[str] = []
            type_map = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
            for pname, p in sig.parameters.items():
                if pname in ("ctx", "self"):
                    continue
                ann = p.annotation
                ptype = type_map.get(ann, "string") if ann is not inspect.Parameter.empty else "string"
                props[pname] = {"type": ptype, "description": pname}
                if p.default is inspect.Parameter.empty:
                    req.append(pname)
            return props, req

        props, req = _make_sig(fn)

        class FnTool(_FnTool):
            def __init__(self) -> None:
                self.name = tname
                self.description = description or (fn.__doc__ or "").strip().split("\n")[0]
                self.group = group
                self.dangerous = dangerous
                self.read_only = read_only
                self.parameters = props
                self.required = req

            async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
                sig = inspect.signature(fn)
                call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
                if is_async:
                    res = await fn(ctx, **call_kwargs)
                else:
                    res = fn(ctx, **call_kwargs)
                if isinstance(res, ToolResult):
                    return res
                if res is None:
                    return ToolResult.text("（完成）")
                return ToolResult.text(str(res))

        reg = registry or get_registry()
        reg.register(FnTool())
        return fn

    return deco


__all__ = [
    "Tool",
    "ToolResult",
    "ToolContext",
    "ToolRegistry",
    "get_registry",
    "reset_registry",
    "tool",
]
