"""MCP 配置导入：兼容 TOML 配置与官方 mcp.json 结构。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..utils import read_json
from ..utils import toml as tomlutil
from .client import ServerState


def from_mcp_json(data: dict[str, Any], *, env: dict[str, str] | None = None) -> list[ServerState]:
    """把官方 ``mcpServers`` 结构转成 ServerState 列表。"""
    env = env or {}
    servers = data.get("mcpServers") or data.get("servers") or {}
    out: list[ServerState] = []
    if isinstance(servers, list):
        items = [(str(s.get("name") or f"server{i}"), s) for i, s in enumerate(servers) if isinstance(s, dict)]
    elif isinstance(servers, dict):
        items = list(servers.items())
    else:
        return out
    for name, raw in items:
        if not isinstance(raw, dict):
            continue
        typ = str(raw.get("type") or ("stdio" if raw.get("command") else "http")).lower()
        if typ not in ("stdio", "sse", "http", "streamable-http", "websocket"):
            typ = "stdio"
        env_map: dict[str, str] = {}
        for k, v in (raw.get("env") or {}).items():
            val = "" if v is None else str(v)
            m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", val)
            if m:
                val = env.get(m.group(1)) or os.environ.get(m.group(1), "")
            env_map[str(k)] = val
        out.append(
            ServerState(
                name=str(name),
                type=typ,
                command=str(raw.get("command") or ""),
                args=[str(a) for a in (raw.get("args") or [])],
                env=env_map,
                url=str(raw.get("url") or ""),
                headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
                enabled=not bool(raw.get("disabled")),
                namespace=str(raw.get("namespace") or name),
            )
        )
    return out


def load_from_file(path: os.PathLike | str) -> dict[str, Any]:
    """从文件导入（自动识别 .json / .toml 与两种格式）。"""
    p = Path(path)
    result: dict[str, Any] = {"source": str(p), "servers": [], "errors": []}
    if not p.is_file():
        result["errors"].append("文件不存在")
        return result
    env: dict[str, str] = {}
    env_file = p.parent / ".env"
    if env_file.is_file():
        try:
            from ..config.manager import parse_env_file

            env = parse_env_file(env_file)
        except Exception:
            env = {}
    try:
        if p.suffix.lower() == ".toml":
            data = tomlutil.loadf(p)
            result["servers"] = from_toml_plugins(data, env=env)
        else:
            data = read_json(p, default={}) or {}
            result["servers"] = from_mcp_json(data, env=env)
    except Exception as e:
        result["errors"].append(f"{type(e).__name__}: {e}")
    return result


def scan_default_configs() -> dict[str, Any]:
    """列出可导入的 MCP 配置文件位置（只看本项目与本应用自己的，不翻其它程序的目录）。"""
    cands: list[tuple[str, Path]] = [
        ("项目内 .mcp.json", Path.cwd() / ".mcp.json"),
        ("项目内 mcp.json", Path.cwd() / "mcp.json"),
    ]
    # 本应用数据目录下的配置
    try:
        from .. import paths as _paths

        home_dir = _paths.home()
        cands += [
            ("Fengcode mcp.json", home_dir / "mcp.json"),
            ("Fengcode config.toml", _paths.config_file()),
        ]
    except Exception:
        pass

    found = []
    for label, p in cands:
        try:
            exists = p.is_file()
        except OSError:
            exists = False
        if exists:
            found.append({"label": label, "path": str(p)})
    return {"candidates": found}


def export_mcp_json(states: list[ServerState]) -> dict[str, Any]:
    """导出成官方 ``mcpServers`` 结构（便于粘贴给别人）。"""
    servers: dict[str, Any] = {}
    for s in states:
        entry: dict[str, Any] = {}
        if s.type == "stdio":
            entry = {"command": s.command, "args": list(s.args)}
            if s.env:
                entry["env"] = dict(s.env)
        else:
            entry = {"type": s.type, "url": s.url}
            if s.headers:
                entry["headers"] = dict(s.headers)
        servers[s.name] = entry
    return {"mcpServers": servers}


def from_toml_plugins(data: dict[str, Any], *, env: dict[str, str] | None = None) -> list[ServerState]:
    """把 TOML 形式的 MCP 配置（``[[plugins]]``）转成 ServerState。

    兼容常见写法：
      [[plugins]]
      name = "github"
      type = "stdio"          # 或 mcp / sse / http
      command = "npx"
      args = ["-y", "@modelcontextprotocol/server-github"]
      env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }

    也接受 ``[mcp_servers.xxx]`` 与 ``[mcp.servers.xxx]`` 两种通用表结构。
    """
    env = env or {}
    out: list[ServerState] = []

    entries = data.get("plugins")
    if isinstance(entries, list):
        out += _states_from_entries(entries, env=env)

    for key in ("mcp_servers", "mcp", "servers"):
        node = data.get(key)
        if isinstance(node, dict):
            # [mcp.servers.xxx] 多一层
            inner = node.get("servers")
            src = inner if isinstance(inner, dict) else node
            out += _states_from_entries(
                [dict(v, name=k) if isinstance(v, dict) else {} for k, v in src.items()],
                env=env,
            )
        elif isinstance(node, list):
            out += _states_from_entries(node, env=env)

    # 按名字去重（先出现的优先）
    seen: set[str] = set()
    uniq: list[ServerState] = []
    for s in out:
        if s.name in seen:
            continue
        seen.add(s.name)
        uniq.append(s)
    return uniq


def _states_from_entries(entries: list[Any], *, env: dict[str, str]) -> list[ServerState]:
    """把一组插件/服务器配置项转成 ServerState（内部使用）。"""
    out: list[ServerState] = []
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("id") or f"server{i}")
        # 只收 MCP 类型的插件；没有 type 但带 command/url 的也视为 MCP
        typ_raw = str(raw.get("type") or raw.get("transport") or "").lower()
        has_endpoint = bool(raw.get("command") or raw.get("url"))
        if typ_raw and typ_raw not in ("stdio", "sse", "http", "streamable-http",
                                       "streamable_http", "websocket", "mcp"):
            if not has_endpoint:
                continue
        if not has_endpoint and typ_raw not in ("stdio", "sse", "http",
                                                "streamable-http", "streamable_http"):
            continue

        typ = typ_raw.replace("_", "-")
        if typ in ("", "mcp"):
            typ = "stdio" if raw.get("command") else "http"
        if typ not in ("stdio", "sse", "http", "streamable-http", "websocket"):
            typ = "stdio"

        env_map: dict[str, str] = {}
        for k, v in (raw.get("env") or {}).items():
            if v is None:
                env_map[str(k)] = ""
                continue
            val = str(v)
            m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", val)
            if m:
                val = env.get(m.group(1)) or os.environ.get(m.group(1), "")
            env_map[str(k)] = val

        # 每个工具的超时覆盖（tool_timeout_seconds）
        timeout_raw = raw.get("tool_timeout_seconds") or raw.get("tool_timeout") or {}
        tool_timeout: dict[str, float] = {}
        if isinstance(timeout_raw, dict):
            for tk, tv in timeout_raw.items():
                try:
                    tool_timeout[str(tk)] = float(tv)
                except (TypeError, ValueError):
                    continue

        out.append(
            ServerState(
                name=name,
                type=typ,
                command=str(raw.get("command") or ""),
                args=[str(a) for a in (raw.get("args") or [])],
                env=env_map,
                url=str(raw.get("url") or raw.get("endpoint") or ""),
                headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
                enabled=bool(raw.get("enabled", not raw.get("disabled", False))),
                namespace=str(raw.get("namespace") or name),
                tool_timeout=tool_timeout,
            )
        )
    return out


def from_plugins_config(data: dict[str, Any] | os.PathLike | str,
                        *, env: dict[str, str] | None = None) -> list[ServerState]:
    """从 TOML/JSON 形式的 MCP 配置里挑出服务器定义。

    入参可以是**已解析的 dict**（``{"plugins": [...]}``），也可以是
    **config.toml / mcp.json 的路径**（会自动读取同目录 ``.env``
    来展开 ``${VAR}`` 占位符）。

    支持 ``[[plugins]]`` 与 ``[mcp_servers.*]`` 两种常见写法，
    便于把别处写好的 MCP 配置粘过来直接用。
    """
    env_map: dict[str, str] = dict(env or {})

    # ---- 入参是路径：读文件 + 读同目录 .env ----
    if isinstance(data, (str, os.PathLike)):
        p = Path(data)
        if not p.is_file():
            return []
        if not env:
            env_file = p.parent / ".env"
            if env_file.is_file():
                try:
                    from ..config.manager import parse_env_file

                    env_map = parse_env_file(env_file)
                except Exception:
                    env_map = {}
        try:
            if p.suffix.lower() == ".toml":
                loaded = tomlutil.loadf(p)
            else:
                loaded = read_json(p, default={}) or {}
        except Exception:
            return []
        if not isinstance(loaded, dict):
            return []
        data = loaded

    if not isinstance(data, dict):
        return []

    # 官方 mcpServers 结构直接走另一条路
    if "mcpServers" in data and "plugins" not in data:
        return from_mcp_json(data, env=env_map)

    return from_toml_plugins(data, env=env_map)


__all__ = [
    "from_mcp_json",
    "from_toml_plugins",
    "from_plugins_config",
    "load_from_file",
    "scan_default_configs",
    "export_mcp_json",
]
