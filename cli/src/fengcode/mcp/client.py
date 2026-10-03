"""MCP 客户端：连接外部 MCP 服务器并调用其工具。

为什么自研而不直接用官方 SDK
----------------------------
官方 ``mcp`` SDK 的 stdio 传输依赖 ``AsyncExitStack`` 管理生命周期，版本间
API 有变动，且把"一个服务器一个上下文"的模型强加给调用方。这里只需要
**JSON-RPC 2.0 请求/响应**这一层，自己实现更稳、更可控（约 400 行），
也便于同时支持 stdio / HTTP / SSE 三种传输。

支持的传输
----------
- ``stdio``           ：启动子进程，按行收发 JSON（最常用）
- ``http``            ：POST JSON-RPC 到 endpoint，支持普通 JSON 与 SSE 响应
- ``sse``             ：先 GET 建立 SSE 通道拿到 message endpoint，再 POST
- ``streamable-http`` ：等同 http（现代 MCP 规范名）

Windows 注意：``npx`` / ``npm`` 这类 .cmd 脚本无法直接 execv，
需要经 ``cmd /c`` 包装（本机执行策略会拦 .ps1）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..llm.types import ToolSpec
from ..utils import is_windows, redact

PROTOCOL_VERSION = "2025-06-18"
FALLBACK_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"]


class MCPError(Exception):
    """MCP 通信或协议错误。"""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None,
                 kind: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        # ★ 错误按身份编码：光有一句错误文本，界面无法区分
        #   「配置写错了」和「进程崩了」和「只是超时」—— 三类该有完全不同的提示与处置。
        #   kind 就是「错误身份」，调用方据此决定是提示用户改配置、还是自动重启。
        self.kind = kind or "mcp_error"


@dataclass
class MCPTool:
    """远端工具的描述。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    server: str = ""
    # 带命名空间的完整名（避免不同服务器工具重名）
    full_name: str = ""

    def to_spec(self) -> ToolSpec:
        schema = self.input_schema or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        schema.setdefault("properties", {})
        return ToolSpec(
            name=self.full_name,
            description=(f"[MCP:{self.server}] {self.description}")[:4000],
            parameters=schema,
            source=f"mcp:{self.server}",
            group=f"MCP · {self.server}",
        )


@dataclass
class ServerState:
    """一个 MCP 服务器的运行状态。"""

    name: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True
    auto_start: bool = True
    namespace: str = ""
    tool_timeout: dict[str, float] = field(default_factory=dict)
    # 运行期
    status: str = "stopped"      # stopped / starting / ready / error
    error: str = ""
    tools: list[MCPTool] = field(default_factory=list)
    start_time: float = 0.0
    calls: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "enabled": self.enabled,
            "status": self.status,
            "error": self.error or self.last_error,
            "tools": [t.name for t in self.tools],
            "tool_count": len(self.tools),
            "calls": self.calls,
            "uptime": round(time.time() - self.start_time, 1) if self.start_time else 0,
        }


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------

class StdioTransport:
    """子进程 stdin/stdout 上的行分隔 JSON-RPC。"""

    def __init__(self, command: str, args: list[str], env: dict[str, str], cwd: str = "") -> None:
        self.command = command
        self.args = list(args)
        self.env = dict(env)
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr_buf: list[str] = []

    def _resolve_argv(self) -> list[str]:
        """把命令解析成可执行的 argv，处理 Windows 的 .cmd/.bat 包装。"""
        cmd = self.command
        exe = shutil.which(cmd)
        if exe is None and is_windows():
            for ext in (".cmd", ".bat", ".exe", ".ps1"):
                cand = shutil.which(cmd + ext)
                if cand:
                    exe = cand
                    break
        if exe is None:
            # 允许直接给绝对路径
            if os.path.isabs(cmd) and os.path.exists(cmd):
                exe = cmd
            else:
                raise MCPError(
                    f"找不到可执行文件：{cmd}"
                    + ("（Windows 上若是 npm/npx 脚本，请确认已安装 Node.js）" if is_windows() else "")
                )
        argv = [exe, *self.args]
        # .cmd/.bat 必须经 cmd.exe 执行；.ps1 经 powershell -File
        low = exe.lower()
        if is_windows():
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            if low.endswith((".cmd", ".bat")):
                argv = [comspec, "/c", exe, *self.args]
            elif low.endswith(".ps1"):
                ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
                argv = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, *self.args]
        return argv

    async def start(self) -> None:
        from ..security.sandbox import build_env

        env = build_env(self.env)
        argv = self._resolve_argv()
        kwargs: dict[str, Any] = {}
        if is_windows():
            import subprocess

            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd or None,
                **kwargs,
            )
        except (OSError, FileNotFoundError) as e:
            raise MCPError(f"启动 MCP 进程失败（{' '.join(argv[:3])}…）：{e}", kind="spawn") from e
        if self.proc.stderr is not None:
            asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                self._stderr_buf.append(line.decode("utf-8", "replace").rstrip())
                if len(self._stderr_buf) > 200:
                    del self._stderr_buf[:100]
        except Exception:
            pass

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_buf[-20:])

    async def send(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("MCP 进程未启动", kind="not_started")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise MCPError(f"MCP 进程已退出（{self.stderr_tail[-300:]}）", kind="crashed") from e

    async def recv(self, timeout: float) -> dict[str, Any] | None:
        """读一条 JSON-RPC 消息；超时返回 None（由调用方决定是继续等还是放弃）。"""
        if self.proc is None or self.proc.stdout is None:
            raise MCPError("MCP 进程未启动")
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if not line:
            rc = self.proc.returncode
            raise MCPError(
                f"MCP 进程输出结束（退出码 {rc}）"
                + (f"\nstderr：{self.stderr_tail[-500:]}" if self._stderr_buf else ""),
                kind="crashed",
            )
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except ValueError:
            # 有些服务器会往 stdout 打日志，跳过非 JSON 行
            return None
        return obj if isinstance(obj, dict) else None

    async def close(self) -> None:
        if self.proc is None:
            return
        proc = self.proc
        self.proc = None
        with contextlib.suppress(Exception):
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=4)
            except (asyncio.TimeoutError, Exception):
                with contextlib.suppress(Exception):
                    proc.kill()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


class HTTPTransport:
    """HTTP / SSE 上行，响应可能是 JSON 或 SSE 流。"""

    def __init__(self, url: str, headers: dict[str, str], *, prefer_sse: bool = False) -> None:
        self.url = url
        self.headers = dict(headers)
        self.prefer_sse = prefer_sse
        self.session_id: str = ""
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=20.0), follow_redirects=True,
            )
        return self._client

    async def start(self) -> None:
        await self._get_client()

    async def request(self, message: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        client = await self._get_client()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        try:
            resp = await client.post(
                self.url, json=message, headers=headers, timeout=timeout,
            )
        except Exception as e:
            raise MCPError(f"HTTP 请求失败：{type(e).__name__}: {e}", kind="transport") from e
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code}：{resp.text[:400]}", code=resp.status_code)
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # 从 SSE 流里取第一个含 result/error 的消息
            for line in resp.text.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    return obj
            return None
        if not resp.text.strip():
            return None
        try:
            obj = resp.json()
        except ValueError:
            raise MCPError(f"响应不是合法 JSON：{resp.text[:300]}", kind="protocol")
        return obj if isinstance(obj, dict) else None

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------

class MCPConnection:
    """一个 MCP 服务器的完整会话（初始化 + 工具发现 + 调用）。"""

    def __init__(self, state: ServerState) -> None:
        self.state = state
        self.transport: StdioTransport | HTTPTransport | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str = ""
        # 服务器主动发来的通知（如日志）
        self.notifications: list[dict[str, Any]] = []

    # ---- 起停 ----------------------------------------------------------
    async def start(self, *, timeout: float = 30.0) -> None:
        st = self.state
        st.status = "starting"
        st.error = ""
        try:
            if st.type in ("http", "streamable-http", "websocket"):
                if not st.url:
                    raise MCPError("HTTP 类型必须提供 url", kind="config")
                self.transport = HTTPTransport(st.url, st.headers, prefer_sse=st.type == "websocket")
            elif st.type == "sse":
                if not st.url:
                    raise MCPError("SSE 类型必须提供 url", kind="config")
                self.transport = HTTPTransport(st.url, st.headers, prefer_sse=True)
            else:
                if not st.command:
                    raise MCPError("stdio 类型必须提供 command", kind="config")
                self.transport = StdioTransport(st.command, st.args, st.env, st.cwd)
            await self.transport.start()
            if isinstance(self.transport, StdioTransport):
                self._reader_task = asyncio.create_task(self._read_loop())
            await self._initialize(timeout)
            await self.refresh_tools()
            st.status = "ready"
            st.start_time = time.time()
        except MCPError as e:
            st.status = "error"
            st.error = str(e)
            await self.close()
            raise
        except Exception as e:
            st.status = "error"
            st.error = f"{type(e).__name__}: {e}"
            await self.close()
            raise

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("连接已关闭"))
        self._pending.clear()
        if self.transport is not None:
            with contextlib.suppress(Exception):
                await self.transport.close()
            self.transport = None
        if self.state.status != "error":
            self.state.status = "stopped"

    @property
    def alive(self) -> bool:
        if isinstance(self.transport, StdioTransport):
            return self.transport.alive
        return self.transport is not None

    # ---- 底层收发 ------------------------------------------------------
    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    async def _read_loop(self) -> None:
        """stdio：持续读响应并回填 Future；同时收集服务器通知。"""
        assert isinstance(self.transport, StdioTransport)
        try:
            while True:
                try:
                    msg = await self.transport.recv(3600.0)
                except MCPError:
                    break
                if msg is None:
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            try:
                mid = int(msg["id"])
            except (TypeError, ValueError):
                return
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        if "method" in msg:
            self.notifications.append(msg)
            if len(self.notifications) > 200:
                del self.notifications[:100]

    async def _send(self, method: str, params: dict[str, Any] | None = None,
                    *, timeout: float = 60.0, notify: bool = False) -> dict[str, Any] | None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if notify:
            if isinstance(self.transport, StdioTransport):
                await self.transport.send(msg)
            elif isinstance(self.transport, HTTPTransport):
                with contextlib.suppress(Exception):
                    await self.transport.request(msg, timeout=10)
            return None

        mid = self._new_id()
        msg["id"] = mid
        if isinstance(self.transport, HTTPTransport):
            resp = await self.transport.request(msg, timeout=timeout)
            if resp is None:
                raise MCPError(f"{method} 未返回结果（服务器可能不支持该 HTTP 传输）")
            if "error" in resp:
                err = resp["error"] or {}
                raise MCPError(
                    f"{method} 失败：{err.get('message') or err}", code=err.get("code"), data=err.get("data")
                )
            return resp.get("result") or {}

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        async with self._write_lock:
            await self.transport.send(msg)  # type: ignore[union-attr]
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise MCPError(f"{method} 超时（{timeout}s）", kind="timeout") from None
        if "error" in resp:
            err = resp["error"] or {}
            raise MCPError(
                f"{method} 失败：{err.get('message') or err}", code=err.get("code"), data=err.get("data")
            )
        return resp.get("result") or {}

    # ---- 协议 ----------------------------------------------------------
    async def _initialize(self, timeout: float) -> None:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "roots": {"listChanged": False},
                "sampling": {},
            },
            "clientInfo": {"name": "Fengcode", "version": "1.0.0"},
        }
        result = await self._send("initialize", params, timeout=timeout)
        assert result is not None
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = result.get("instructions") or ""
        # 协议版本不一致时给出提示（不中断）
        server_ver = result.get("protocolVersion")
        if server_ver and server_ver not in FALLBACK_VERSIONS:
            self.state.last_error = f"协议版本差异：服务器 {server_ver}，客户端 {PROTOCOL_VERSION}"
        await self._send("notifications/initialized", {}, notify=True)

    async def refresh_tools(self) -> list[MCPTool]:
        """拉取工具列表。"""
        st = self.state
        tools: list[MCPTool] = []
        cursor: str | None = None
        for _ in range(20):  # 分页保护
            params = {"cursor": cursor} if cursor else {}
            try:
                result = await self._send("tools/list", params, timeout=30.0)
            except MCPError as e:
                st.last_error = str(e)
                break
            if not result:
                break
            for t in result.get("tools") or []:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                raw_name = str(t["name"])
                full = raw_name
                if st.namespace != "":
                    ns = st.namespace or st.name
                    full = f"{ns}__{raw_name}" if ns else raw_name
                tools.append(
                    MCPTool(
                        name=raw_name,
                        description=str(t.get("description") or ""),
                        input_schema=t.get("inputSchema") or {},
                        server=st.name,
                        full_name=full,
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        st.tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """调用工具，返回 ``(成功, 文本内容, 原始结果)``。"""
        st = self.state
        timeout = float(st.tool_timeout.get(name) or st.tool_timeout.get("*") or 120.0)
        st.calls += 1
        try:
            result = await self._send(
                "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
            )
        except MCPError as e:
            st.last_error = str(e)
            return False, f"MCP 调用失败：{e}", {}
        assert result is not None
        parts: list[str] = []
        images: list[dict[str, Any]] = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            t = item.get("type")
            if t == "text":
                parts.append(str(item.get("text") or ""))
            elif t == "image":
                images.append({"mime": item.get("mimeType", "image/png"), "data": item.get("data", "")})
                parts.append(f"（图片内容，{item.get('mimeType', 'image/png')}）")
            elif t == "resource":
                res = item.get("resource") or {}
                parts.append(str(res.get("text") or res.get("uri") or "（资源）"))
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:1000])
        text = "\n".join(p for p in parts if p)
        is_error = bool(result.get("isError"))
        data: dict[str, Any] = {"server": st.name, "tool": name}
        if images:
            data["_images"] = images
        if result.get("structuredContent"):
            data["structured"] = result["structuredContent"]
            if not text:
                text = json.dumps(result["structuredContent"], ensure_ascii=False)
        return (not is_error), text or "（无输出）", data

    async def list_resources(self) -> list[dict[str, Any]]:
        try:
            result = await self._send("resources/list", {}, timeout=20.0)
        except MCPError:
            return []
        out = []
        for r in (result or {}).get("resources") or []:
            if isinstance(r, dict):
                out.append(r)
        return out

    async def read_resource(self, uri: str) -> str:
        try:
            result = await self._send("resources/read", {"uri": uri}, timeout=60.0)
        except MCPError as e:
            return f"读取资源失败：{e}"
        parts = []
        for item in (result or {}).get("contents") or []:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("uri") or ""))
        return "\n".join(parts) or "（资源为空）"

    async def list_prompts(self) -> list[dict[str, Any]]:
        try:
            result = await self._send("prompts/list", {}, timeout=20.0)
        except MCPError:
            return []
        return [p for p in ((result or {}).get("prompts") or []) if isinstance(p, dict)]

    async def get_prompt(self, name: str, args: dict[str, Any] | None = None) -> str:
        try:
            result = await self._send("prompts/get", {"name": name, "arguments": args or {}}, timeout=60.0)
        except MCPError as e:
            return f"获取提示词失败：{e}"
        parts = []
        for m in (result or {}).get("messages") or []:
            if isinstance(m, dict):
                c = m.get("content") or {}
                if isinstance(c, dict):
                    parts.append(str(c.get("text") or ""))
                elif isinstance(c, str):
                    parts.append(c)
        return "\n".join(parts)


# --------------------------------------------------------------------------
# 管理器
# --------------------------------------------------------------------------

class MCPManager:
    """管理全部 MCP 服务器的连接与工具暴露。"""

    def __init__(self, *, config: Any = None, bus: Any = None) -> None:
        self.config = config
        self.bus = bus
        self.connections: dict[str, MCPConnection] = {}
        self._lock = asyncio.Lock()

    def _emit(self, type: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            with contextlib.suppress(Exception):
                self.bus.emit(type, data)

    # ---- 配置同步 ------------------------------------------------------
    def states(self) -> list[ServerState]:
        cfg = getattr(self.config, "mcp", None)
        out: list[ServerState] = []
        for s in (getattr(cfg, "servers", None) or []):
            out.append(
                ServerState(
                    name=s.name,
                    type=s.type,
                    command=s.command or "",
                    args=list(s.args or []),
                    env=dict(s.env or {}),
                    url=s.url or "",
                    headers=dict(s.headers or {}),
                    cwd=s.cwd or "",
                    enabled=bool(s.enabled),
                    auto_start=bool(s.auto_start),
                    namespace=s.namespace if s.namespace is not None else s.name,
                    tool_timeout={str(k): float(v) for k, v in (s.tool_timeout_seconds or {}).items()},
                )
            )
        return out

    def state_map(self) -> dict[str, ServerState]:
        return {s.name: s for s in self.states()}

    # ---- 连接管理 ------------------------------------------------------
    async def start_server(self, name: str, *, timeout: float = 45.0) -> ServerState:
        states = self.state_map()
        st = states.get(name)
        if st is None:
            raise MCPError(f"没有配置名为 “{name}” 的 MCP 服务器", kind="config")
        async with self._lock:
            old = self.connections.pop(name, None)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()
        conn = MCPConnection(st)
        async with self._lock:
            self.connections[name] = conn
        try:
            await conn.start(timeout=timeout)
            self._emit("mcp.ready", {"name": name, "tools": len(st.tools)})
        except Exception:
            self._emit("mcp.error", {"name": name, "error": st.error})
            raise
        return st

    async def stop_server(self, name: str) -> None:
        async with self._lock:
            conn = self.connections.pop(name, None)
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()
            self._emit("mcp.stopped", {"name": name})

    async def start_all(self, *, concurrency: int = 4, background: bool = False) -> dict[str, str]:
        """并发启动全部 enabled 且 auto_start 的服务器，返回 ``{名称: 状态}``。

        ★ background=True：**不阻塞启动**，后台慢慢连。
          为什么：MCP 冷启动可能要几十秒（npx 首次下载、Python 服务加载），
          把服务启动卡在这上面，用户看到的就是「打开软件转半天」。
          后台化之后界面立刻可用，连上的服务器再陆续把工具挂出来。
        """
        targets = [s for s in self.states() if s.enabled and s.auto_start]
        results: dict[str, str] = {}
        if background:
            # 只登记「正在启动」，真正的连接交给后台任务
            for s in targets:
                results[s.name] = "启动中（后台）"
            if targets:
                asyncio.create_task(self._background_start(targets, concurrency))
            return results
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(nm: str) -> None:
            async with sem:
                try:
                    st = await self.start_server(nm)
                    results[nm] = f"ok（{len(st.tools)} 个工具）"
                except Exception as e:
                    results[nm] = f"失败：{e}"

        await asyncio.gather(*(one(s.name) for s in targets)) if targets else None
        return results

    async def _background_start(self, targets: list[ServerState], concurrency: int) -> None:
        """后台把服务器一个个连起来；失败只记状态，不打断任何人。"""
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(st: ServerState) -> None:
            async with sem:
                try:
                    await self.start_server(st.name)
                    self._emit("mcp.ready", {"name": st.name, "tools": len(st.tools)})
                except Exception as e:
                    self._emit("mcp.error", {"name": st.name, "error": str(e)})

        with contextlib.suppress(Exception):
            await asyncio.gather(*(one(s) for s in targets))

    async def refresh_dir_tools(self) -> dict[str, int]:
        """目录变更后重新拉取各服务器的工具列表。

        ★ 要解决什么：服务器运行中新增/移除了工具（文件型服务器尤其常见），
          不重拉的话模型看到的还是旧清单 —— 要么调不到新工具，要么调到已删的。
          返回 ``{服务器名: 工具数}``，未连上的跳过（不因为一个没连上就整体失败）。
        """
        out: dict[str, int] = {}
        for name, conn in list(self.connections.items()):
            if not conn.alive:
                continue
            try:
                tools = await conn.refresh_tools()
                out[name] = len(tools)
            except Exception:
                continue
        if out:
            self._emit("mcp.refreshed", {"servers": out})
        return out

    async def stop_all(self) -> None:
        names = list(self.connections)
        for nm in names:
            await self.stop_server(nm)

    async def restart(self, name: str) -> ServerState:
        await self.stop_server(name)
        return await self.start_server(name)

    async def close(self) -> None:
        await self.stop_all()

    # ---- 工具 ----------------------------------------------------------
    def all_tools(self) -> list[MCPTool]:
        out: list[MCPTool] = []
        for conn in self.connections.values():
            out.extend(conn.state.tools)
        return out

    def specs(self) -> list[ToolSpec]:
        return [t.to_spec() for t in self.all_tools()]

    def find(self, full_name: str) -> tuple[MCPConnection, MCPTool] | None:
        for conn in self.connections.values():
            for t in conn.state.tools:
                if t.full_name == full_name:
                    return conn, t
        # 容忍未加命名空间的名字
        for conn in self.connections.values():
            for t in conn.state.tools:
                if t.name == full_name:
                    return conn, t
        return None

    async def call_tool(self, full_name: str, arguments: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        found = self.find(full_name)
        if found is None:
            return False, f"没有找到 MCP 工具：{full_name}", {}
        conn, tool = found
        # ★ stdio 会话复用：连接还在就直接用，**不每次重连**。
        #   为什么：stdio 服务器冷启动要几秒到几十秒（npx 首次下载/解释器加载）。
        #   旧写法每次调用前都判一次 alive，一旦被误判为「不活」就整条重启 ——
        #   用户看到的就是「每调一个工具就卡一下」。
        #   这里只在真的断开时才重连一次，并复用同一个连接对象。
        if not conn.alive:
            with contextlib.suppress(Exception):
                await self.start_server(conn.state.name)
                again = self.connections.get(conn.state.name)
                if again is not None:
                    conn = again
        try:
            return await conn.call_tool(tool.name, arguments)
        except MCPError as e:
            # 连接在调用途中断了 → 重连一次再试（典型场景：服务器空闲被回收）
            if e.kind in ("crashed", "not_started", "transport"):
                with contextlib.suppress(Exception):
                    await self.start_server(conn.state.name)
                    again = self.connections.get(conn.state.name)
                    if again is not None:
                        return await again.call_tool(tool.name, arguments)
            return False, f"MCP 调用失败：{e}", {}

    # ---- 状态 ----------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        configured = self.state_map()
        out = []
        for name, st in configured.items():
            conn = self.connections.get(name)
            if conn is not None and conn.alive:
                st.status = "ready" if st.tools else conn.state.status
                st.tools = conn.state.tools or st.tools
                st.calls = conn.state.calls
                st.start_time = conn.state.start_time
                st.last_error = conn.state.last_error
            d = st.to_dict()
            if conn is not None:
                d["server_info"] = conn.server_info
                d["capabilities"] = list((conn.capabilities or {}).keys())
                d["instructions"] = (conn.instructions or "")[:500]
            d["connected"] = bool(conn is not None and conn.alive)
            out.append(d)
        return out

    async def test(self, name: str) -> dict[str, Any]:
        """连通性测试：连上、列工具、断开。"""
        t0 = time.time()
        try:
            st = await self.start_server(name, timeout=45.0)
            tools = [t.name for t in st.tools]
            await self.stop_server(name)
            return {
                "ok": True,
                "duration": round(time.time() - t0, 2),
                "tools": tools,
                "tool_count": len(tools),
            }
        except Exception as e:
            return {"ok": False, "duration": round(time.time() - t0, 2), "error": str(e)}

    async def probe_config(self, state: ServerState, *, timeout: float = 45.0) -> dict[str, Any]:
        """在不影响现有连接的前提下试连一个配置（用于界面"测试"按钮）。"""
        conn = MCPConnection(state)
        t0 = time.time()
        try:
            await conn.start(timeout=timeout)
            tools = [t.name for t in conn.state.tools]
            return {
                "ok": True,
                "duration": round(time.time() - t0, 2),
                "tools": tools,
                "tool_count": len(tools),
                "server_info": conn.server_info,
                "capabilities": list((conn.capabilities or {}).keys()),
            }
        except Exception as e:
            return {"ok": False, "duration": round(time.time() - t0, 2), "error": str(e)}
        finally:
            with contextlib.suppress(Exception):
                await conn.close()


__all__ = [
    "MCPManager",
    "MCPConnection",
    "MCPTool",
    "ServerState",
    "MCPError",
    "StdioTransport",
    "HTTPTransport",
    "PROTOCOL_VERSION",
]
