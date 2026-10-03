"""MCP 服务器：把 Fengcode 自身的能力暴露给外部 MCP 客户端。

用途：让 Claude Desktop、Cursor、其它 Agent 直接调用 Fengcode 的工具与记忆。

传输：
- ``stdio``：被外部客户端作为子进程启动（最常用）
- ``http``：独立 HTTP 服务（``POST /mcp``）

运行：``fengcode mcp serve``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class FengcodeMCPServer:
    """把工具注册表 + 记忆 + 技能包装成 MCP 服务。"""

    def __init__(self, *, registry: Any = None, memory: Any = None, skills: Any = None,
                 agent: Any = None, name: str = "fengcode", version: str = "1.0.0") -> None:
        self.registry = registry
        self.memory = memory
        self.skills = skills
        self.agent = agent
        self.name = name
        self.version = version
        self.initialized = False

    # ---- 工具清单 ------------------------------------------------------
    def tool_definitions(self, *, include: list[str] | None = None,
                         exclude: list[str] | None = None) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        out: list[dict[str, Any]] = []
        allow = set(include) if include else None
        deny = set(exclude or [])
        for spec in self.registry.specs():
            if allow is not None and spec.name not in allow:
                continue
            if spec.name in deny:
                continue
            desc = spec.description
            if spec.dangerous:
                desc += "（注意：这是高危操作，外部调用会触发 Fengcode 的审批策略）"
            out.append(
                {
                    "name": spec.name,
                    "description": desc,
                    "inputSchema": spec.parameters or {"type": "object", "properties": {}},
                }
            )
        # 暴露记忆检索为独立工具（即使主工具表里没有）
        if self.memory is not None and (allow is None or "memory_search" in allow):
            out.append(
                {
                    "name": "memory_search",
                    "description": "在 Fengcode 的长期记忆中检索相关信息。",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "检索关键词"},
                            "top_k": {"type": "integer", "description": "返回条数，默认 5"},
                        },
                        "required": ["query"],
                    },
                }
            )
        # 暴露技能目录
        if self.skills is not None and (allow is None or "skill_list" in allow):
            out.append(
                {
                    "name": "skill_list",
                    "description": "列出 Fengcode 可用的技能。",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            )
        return out

    # ---- 工具执行 ------------------------------------------------------
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "memory_search":
                if self.memory is None:
                    return _error("记忆系统未启用")
                items = await self.memory.recall(
                    str(arguments.get("query") or ""), top_k=int(arguments.get("top_k") or 5)
                )
                text = "\n\n".join(
                    f"【{it.kind}】{it.title}\n{it.content}" for it in items
                ) or "（没有找到相关记忆）"
                return _text(text)

            if name == "skill_list":
                if self.skills is None:
                    return _error("技能系统未启用")
                items = self.skills.list_all()
                text = "\n".join(f"· {s['name']}：{s['description']}" for s in items) or "（没有技能）"
                return _text(text)

            if self.registry is None:
                return _error("工具注册表未初始化")
            from ..tools.base import ToolContext

            tool = self.registry.get(name)
            if tool is None:
                return _error(f"未知工具：{name}")
            ctx = ToolContext(workspace=_default_workspace(), config=None)
            result = await self.registry.execute(name, arguments or {}, ctx, skip_approval=True)
            payload: dict[str, Any] = {
                "content": [{"type": "text", "text": result.content or ""}],
                "isError": not result.ok,
            }
            if result.error and not result.content:
                payload["content"] = [{"type": "text", "text": f"错误：{result.error}"}]
            return payload
        except Exception as e:
            return _error(f"{type(e).__name__}: {e}")

    # ---- JSON-RPC 分发 -------------------------------------------------
    async def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 请求；通知类返回 None。"""
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            self.initialized = True
            return _ok(
                mid,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {},
                        "prompts": {},
                    },
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": (
                        "这是 Fengcode 的 MCP 接口。它把本地 Agent 的文件、Shell、网页、"
                        "文档、数据库、记忆等能力暴露出来。高危工具（shell/write_file 等）"
                        "需要外部客户端自行确认。"
                    ),
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": self.tool_definitions()})
        if method == "tools/call":
            out = await self.call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            return _ok(mid, out)
        if method == "resources/list":
            return _ok(mid, {"resources": []})
        if method == "prompts/list":
            return _ok(mid, {"prompts": []})
        if method == "shutdown":
            return _ok(mid, {})
        return _err(mid, -32601, f"不支持的方法：{method}")

    # ---- stdio 主循环 --------------------------------------------------
    async def serve_stdio(self) -> int:
        """在 stdin/stdout 上服务（供外部客户端作为子进程启动）。"""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        writer_transport, writer_protocol = await asyncio.get_running_loop().connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_running_loop())

        while True:
            try:
                line = await reader.readline()
            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            resp = await self.handle(msg)
            if resp is None:
                continue
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            with contextlib.suppress(Exception):
                await writer.drain()
        return 0


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _default_workspace():
    from .. import paths

    return paths.workspace_dir()


def build_default_server() -> FengcodeMCPServer:
    """按全局配置组装一个可用的 MCP 服务器实例。"""
    from ..config import get_config
    from ..memory import get_memory
    from ..skills import get_skills
    from ..storage.db import get_db
    from ..tools import register_builtin
    from ..tools.base import get_registry

    cfg = get_config()
    reg = get_registry()
    if not reg.all():
        register_builtin(reg)
    db = get_db()
    mem = get_memory(cfg.memory)
    mem.bind_config_manager(__import__("fengcode.config", fromlist=["get_manager"]).get_manager())
    skills = get_skills(cfg, db)
    return FengcodeMCPServer(registry=reg, memory=mem, skills=skills,
                             name=getattr(cfg.mcp, "server_name", "fengcode"))


__all__ = ["FengcodeMCPServer", "build_default_server", "PROTOCOL_VERSION"]
