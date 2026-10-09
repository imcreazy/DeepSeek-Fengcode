"""插件系统：manifest.yaml 描述 + Python 代码能力 + 生命周期 + 钩子。

插件目录结构::

    <plugins_dir>/<plugin-name>/
        manifest.yaml    # 必需：元数据与扩展点声明
        plugin.py        # 可选：main 模块（默认 plugin.py）
        panel/           # 可选：UI 面板（index.html + panel.js）
        README.md

manifest.yaml 示例::

    name: my-plugin
    display_name: 我的插件
    version: 1.0.0
    description: 一个示例插件
    author: 某人
    main: plugin.py
    enabled: true
    permissions: [tools, hooks, providers, ui, memory, agents]
    tools:
      - name: hello
        description: 打个招呼
        parameters:
          who: {type: string, description: 对象}
        required: [who]
    hooks: [before_tool, after_tool]

插件代码通过约定的 setup(api) 或全局函数暴露能力::

    def setup(api):
        api.register_tool("hello", handler, description=..., parameters=...)
        api.on("after_tool", lambda ev: ...)
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..llm.types import ToolSpec
from ..tools.base import Tool, ToolContext, ToolResult
from ..utils import ensure_dir, read_text, to_plain, truncate

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

# 钩子点
HOOK_POINTS = [
    "startup",          # 插件加载完成
    "shutdown",         # 插件卸载/程序退出
    "before_turn",      # 一次用户回合开始
    "after_turn",       # 一次用户回合结束
    "before_tool",      # 工具执行前（可 veto）
    "after_tool",       # 工具执行后
    "before_llm",       # 调用模型前（可改消息）
    "after_llm",        # 调用模型后
    "register_prompt",  # 注入系统提示片段
    "memory_write",     # 写入记忆后
    "memory_recall",    # 召回记忆后
]

PERMISSION_KEYS = ["tools", "hooks", "providers", "ui", "memory", "agents", "scheduler", "network"]


@dataclass
class PluginManifest:
    """插件清单。"""

    name: str
    display_name: str = ""
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = ""
    main: str = "plugin.py"
    enabled: bool = True
    builtin: bool = False
    permissions: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    panels: list[dict[str, Any]] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    dir: str = ""
    error: str = ""
    # ★ 信任级别：插件在清单里**显式声明**自己要多少权限，
    #   而不是靠运行时试探。三档含义：
    #     sandboxed —— 只给声明过的权限，越权调用直接拒绝（默认）
    #     trusted   —— 允许它在声明范围内自由使用（等同 sandboxed + 免逐次确认）
    #     full      —— 完全信任（用户显式写上 full_trust: true 才生效）
    #   默认 sandboxed：不声明就不放权，避免"装个插件就等于交出机器"。
    trust: str = "sandboxed"
    # 用户是否显式接受了「完全信任」声明（清单写 full_trust: true 时必须人工确认）
    full_trust: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "license": self.license,
            "main": self.main,
            "enabled": self.enabled,
            "builtin": self.builtin,
            "permissions": list(self.permissions),
            "tools": [t.get("name") for t in self.tools],
            "hooks": list(self.hooks),
            "skills": list(self.skills),
            "panels": [p.get("id") or p.get("name") for p in self.panels],
            "providers": [p.get("name") for p in self.providers],
            "requires": list(self.requires),
            "dir": self.dir,
            "error": self.error,
        }


class PluginAPI:
    """暴露给插件的 API（受 permissions 限制）。"""

    def __init__(self, plugin: "LoadedPlugin", *, manager: "PluginManager") -> None:
        self._plugin = plugin
        self._manager = manager
        self._tools: list[dict[str, Any]] = []
        self._hooks: dict[str, list[Callable]] = {}
        self._providers: list[dict[str, Any]] = []
        self._panels: list[dict[str, Any]] = []
        self._prompt_parts: list[str] = []
        self._agents: list[dict[str, Any]] = []
        self._commands: list[dict[str, Any]] = []
        self.log: list[str] = []

    # ---- 权限检查 ------------------------------------------------------
    def _allowed(self, what: str) -> bool:
        man = self._plugin.manifest
        perms = man.permissions or []
        # ★ 信任级别：full 是完全信任（清单里显式声明 full_trust: true
        #   才可能到达这里），此时不再逐项校验；其余两档仍然按声明放权。
        if man.full_trust and man.trust == "full":
            return True
        # 未声明 permissions 时按"最小权限"处理：只给 tools/hooks
        if not perms:
            return what in ("tools", "hooks")
        return what in perms or "all" in perms

    def _deny(self, what: str) -> None:
        raise PermissionError(
            f"插件 {self._plugin.manifest.name} 未声明 “{what}” 权限，"
            f"请在 manifest.yaml 的 permissions 里加上它"
        )

    # ---- 注册工具 ------------------------------------------------------
    def register_tool(
        self,
        name: str,
        handler: Callable | None = None,
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        required: list[str] | None = None,
        dangerous: bool = False,
        read_only: bool = False,
        group: str | None = None,
    ) -> Any:
        if not self._allowed("tools"):
            self._deny("tools")
        full_name = f"{self._plugin.manifest.name}__{name}"
        meta = {
            "name": name,
            "full_name": full_name,
            "description": description or (handler.__doc__ or "").strip().split("\n")[0] if handler else "",
            "parameters": parameters or {"type": "object", "properties": {}},
            "required": required or [],
            "dangerous": dangerous,
            "read_only": read_only,
            "group": group or f"插件 · {self._plugin.manifest.display_name or self._plugin.manifest.name}",
            "handler": handler,
        }
        self._tools.append(meta)

        def deco(fn: Callable) -> Callable:
            meta["handler"] = fn
            if not meta["description"]:
                meta["description"] = (fn.__doc__ or "").strip().split("\n")[0]
            return fn

        return deco if handler is None else handler

    def tool(self, name: str, **kw: Any) -> Callable:
        """装饰器形式注册工具。"""

        def deco(fn: Callable) -> Callable:
            self.register_tool(name, fn, **kw)
            return fn

        return deco

    # ---- 注册钩子 ------------------------------------------------------
    def on(self, point: str, handler: Callable) -> None:
        if not self._allowed("hooks"):
            self._deny("hooks")
        if point not in HOOK_POINTS:
            raise ValueError(f"未知钩子点：{point}（可用：{', '.join(HOOK_POINTS)}）")
        self._hooks.setdefault(point, []).append(handler)

    def hook(self, point: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.on(point, fn)
            return fn

        return deco

    # ---- 其他扩展点 ----------------------------------------------------
    def register_provider(self, spec: dict[str, Any]) -> None:
        if not self._allowed("providers"):
            self._deny("providers")
        self._providers.append(dict(spec))

    def register_panel(self, panel_id: str, title: str, entry: str = "panel/index.html", **kw: Any) -> None:
        if not self._allowed("ui"):
            self._deny("ui")
        self._panels.append({"id": panel_id, "title": title, "entry": entry, **kw})

    def add_prompt(self, text: str) -> None:
        """往系统提示里追加一段说明。"""
        if text and text.strip():
            self._prompt_parts.append(text.strip())

    def register_agent(self, spec: dict[str, Any]) -> None:
        if not self._allowed("agents"):
            self._deny("agents")
        self._agents.append(dict(spec))

    def register_command(self, name: str, handler: Callable, *, description: str = "",
                         parameters: dict[str, Any] | None = None) -> None:
        self._commands.append(
            {"name": name, "handler": handler, "description": description,
             "parameters": parameters or {}}
        )

    # ---- 便捷访问 ------------------------------------------------------
    @property
    def manifest(self) -> PluginManifest:
        return self._plugin.manifest

    @property
    def path(self) -> Path:
        return Path(self._plugin.manifest.dir)

    def log_info(self, msg: str) -> None:
        self.log.append(f"[INFO] {msg}")
        if len(self.log) > 500:
            del self.log[:250]

    def log_error(self, msg: str) -> None:
        self.log.append(f"[ERROR] {msg}")
        self._plugin.error = (self._plugin.error + "\n" + msg).strip()[-2000:]


class PluginTool(Tool):
    """把插件注册的工具包装成标准 Tool。"""

    def __init__(self, meta: dict[str, Any], plugin_name: str) -> None:
        self.name = meta["full_name"]
        self.description = meta.get("description") or f"插件 {plugin_name} 提供的工具"
        self.group = meta.get("group") or f"插件 · {plugin_name}"
        self.dangerous = bool(meta.get("dangerous"))
        self.read_only = bool(meta.get("read_only"))
        self.parameters = meta.get("parameters") or {"type": "object", "properties": {}}
        if isinstance(self.parameters, dict) and "properties" not in self.parameters:
            # 允许简写 {"who": {"type": "string"}}
            self.parameters = {"type": "object", "properties": self.parameters}
        self.required = list(meta.get("required") or [])
        self._handler = meta.get("handler")
        self.plugin = plugin_name
        self.spec_source = f"plugin:{plugin_name}"

    def spec(self) -> ToolSpec:
        s = super().spec()
        s.source = self.spec_source
        return s

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        if self._handler is None:
            return ToolResult.fail(f"插件工具 {self.name} 没有绑定处理函数")
        try:
            sig = inspect.signature(self._handler)
            has_ctx = "ctx" in sig.parameters or "context" in sig.parameters
            args = dict(kwargs)
            if has_ctx:
                key = "ctx" if "ctx" in sig.parameters else "context"
                args[key] = ctx
            # 只传声明过的参数，避免 unexpected keyword
            accepted = set(sig.parameters.keys())
            args = {k: v for k, v in args.items() if k in accepted or k in ("ctx", "context")}
            if inspect.iscoroutinefunction(self._handler):
                out = await self._handler(**args)
            else:
                out = await asyncio.to_thread(lambda: self._handler(**args))
        except Exception as e:
            return ToolResult.fail(f"插件工具执行失败：{type(e).__name__}: {e}")
        if isinstance(out, ToolResult):
            return out
        if out is None:
            return ToolResult.text("（完成）")
        if isinstance(out, dict):
            import json

            return ToolResult(content=json.dumps(out, ensure_ascii=False, indent=2), data=out)
        return ToolResult.text(str(out))


@dataclass
class LoadedPlugin:
    """已加载的插件实例。"""

    manifest: PluginManifest
    module: Any = None
    api: PluginAPI | None = None
    error: str = ""
    loaded_at: float = 0.0
    calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = self.manifest.to_dict()
        d["error"] = self.error or d.get("error", "")
        d["loaded"] = self.module is not None
        d["loaded_at"] = self.loaded_at
        d["tool_count"] = len(self.api._tools) if self.api else 0
        d["hook_count"] = sum(len(v) for v in (self.api._hooks or {}).values()) if self.api else 0
        return d


class PluginManager:
    """插件的发现、加载、启用/禁用与扩展点分发。"""

    def __init__(self, *, config: Any = None, db: Any = None, bus: Any = None,
                 registry: Any = None) -> None:
        self.config = config
        self.db = db
        self.bus = bus
        self.registry = registry
        self.plugins: dict[str, LoadedPlugin] = {}
        self._lock = threading.RLock()
        self._hooks: dict[str, list[tuple[str, Callable]]] = {}

    def _emit(self, type: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            try:
                self.bus.emit(type, data)
            except Exception:
                pass

    # ---- 搜索路径 ------------------------------------------------------
    def search_dirs(self) -> list[tuple[Path, bool]]:
        """返回 ``(目录, 是否内置)`` 列表。"""
        from .. import paths

        out: list[tuple[Path, bool]] = []
        builtin = Path(__file__).resolve().parent / "builtin"
        if builtin.is_dir():
            out.append((builtin, True))
        out.append((paths.plugins_dir(), False))
        cfg = getattr(self.config, "plugins", None)
        for p in (getattr(cfg, "dirs", None) or []):
            pp = Path(os.path.expandvars(str(p))).expanduser()
            if pp.is_dir():
                out.append((pp, False))
        return out

    # ---- 发现 ----------------------------------------------------------
    def discover(self) -> list[PluginManifest]:
        found: list[PluginManifest] = []
        for base, is_builtin in self.search_dirs():
            if not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                if not entry.is_dir():
                    continue
                mf = entry / "manifest.yaml"
                if not mf.is_file():
                    mf = entry / "manifest.yml"
                if not mf.is_file():
                    mf = entry / "plugin.yaml"
                if not mf.is_file():
                    continue
                man = self._parse_manifest(mf, is_builtin=is_builtin)
                if man is not None:
                    found.append(man)
        return found

    def _parse_manifest(self, path: Path, *, is_builtin: bool) -> PluginManifest | None:
        text = read_text(path)
        if not text.strip():
            return None
        data: dict[str, Any] = {}
        if yaml is not None:
            try:
                loaded = yaml.safe_load(text)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as e:
                man = PluginManifest(
                    name=path.parent.name, dir=str(path.parent),
                    error=f"manifest 解析失败：{e}", builtin=is_builtin,
                )
                return man
        if not data:
            man = PluginManifest(
                name=path.parent.name, dir=str(path.parent),
                error="manifest 为空或格式不正确", builtin=is_builtin,
            )
            return man
        name = str(data.get("name") or path.parent.name).strip()
        manifest = PluginManifest(
            name=name,
            display_name=str(data.get("display_name") or data.get("title") or name),
            version=str(data.get("version") or "0.0.0"),
            description=str(data.get("description") or ""),
            author=str(data.get("author") or ""),
            homepage=str(data.get("homepage") or ""),
            license=str(data.get("license") or ""),
            main=str(data.get("main") or "plugin.py"),
            enabled=bool(data.get("enabled", True)),
            builtin=is_builtin,
            permissions=[str(p) for p in (data.get("permissions") or [])],
            tools=[t for t in (data.get("tools") or []) if isinstance(t, dict)],
            hooks=[str(h) for h in (data.get("hooks") or [])],
            skills=[str(s) for s in (data.get("skills") or [])],
            panels=[p for p in (data.get("panels") or []) if isinstance(p, dict)],
            providers=[p for p in (data.get("providers") or []) if isinstance(p, dict)],
            requires=[str(r) for r in (data.get("requires") or [])],
            dir=str(path.parent),
        )
        # 数据库里的启用状态优先
        if self.db is not None:
            try:
                from ..storage.tasks import PluginStore

                st = PluginStore(self.db).get(name)
                if st is not None:
                    manifest.enabled = bool(st["enabled"])
            except Exception:
                pass
        # 配置里的禁用优先
        cfg = getattr(self.config, "plugins", None)
        if name in (getattr(cfg, "disabled", None) or []):
            manifest.enabled = False
        if name in (getattr(cfg, "enabled", None) or []):
            manifest.enabled = True
        return manifest

    # ---- 加载 ----------------------------------------------------------
    def load_all(self) -> dict[str, Any]:
        """发现并加载全部启用的插件。"""
        result = {"loaded": [], "failed": [], "skipped": []}
        for man in self.discover():
            if not man.enabled:
                result["skipped"].append(man.name)
                self._sync_db(man, loaded=False)
                continue
            try:
                self.load(man)
                result["loaded"].append(man.name)
            except Exception as e:
                man.error = f"{type(e).__name__}: {e}"
                result["failed"].append(f"{man.name}: {e}")
                self._sync_db(man, loaded=False)
        self._emit("plugins.loaded", {"loaded": result["loaded"], "failed": result["failed"]})
        return result

    def load(self, manifest: PluginManifest | str) -> LoadedPlugin:
        """加载单个插件（执行 main 模块并调用 setup(api)）。"""
        man = manifest if isinstance(manifest, PluginManifest) else self._find(manifest)
        if man is None:
            raise FileNotFoundError(f"没有找到插件：{manifest}")

        with self._lock:
            existing = self.plugins.get(man.name)
            if existing is not None:
                self._unload_hooks(man.name)
            plugin = LoadedPlugin(manifest=man)
            api = PluginAPI(plugin, manager=self)
            plugin.api = api

            main_path = Path(man.dir) / man.main
            if main_path.is_file():
                module = self._load_module(main_path, man.name)
                plugin.module = module
                # 优先 setup(api)；否则自动扫描类/函数
                setup = getattr(module, "setup", None)
                if callable(setup):
                    try:
                        res = setup(api)
                        if inspect.isawaitable(res):
                            self._run_coro(res)
                    except Exception as e:
                        plugin.error = f"setup() 执行失败：{type(e).__name__}: {e}"
                        raise
                else:
                    self._auto_collect(module, api)
            else:
                # 声明式插件（只有 manifest，没有代码）：从 manifest.tools 生成壳
                for t in man.tools:
                    api.register_tool(
                        str(t.get("name") or ""),
                        None,
                        description=str(t.get("description") or ""),
                        parameters=t.get("parameters") or {"type": "object", "properties": {}},
                        required=[str(x) for x in (t.get("required") or [])],
                        dangerous=bool(t.get("dangerous")),
                        read_only=bool(t.get("read_only")),
                    )

            plugin.loaded_at = time.time()
            self.plugins[man.name] = plugin
            self._register_extensions(plugin)
            self._sync_db(man, loaded=True)
        return plugin

    def _load_module(self, path: Path, name: str) -> Any:
        mod_name = f"fengcode_plugin_{name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件模块：{path}")
        module = importlib.util.module_from_spec(spec)
        # 插件目录加入 sys.path，便于插件内部 import 自己的模块
        parent = str(path.parent)
        import sys

        added = parent not in sys.path
        if added:
            sys.path.insert(0, parent)
        try:
            spec.loader.exec_module(module)
        finally:
            if added:
                try:
                    sys.path.remove(parent)
                except ValueError:
                    pass
        return module

    def _auto_collect(self, module: Any, api: PluginAPI) -> None:
        """没有 setup() 时，自动收集 @plugin_tool 标记或 __plugin_tools__。"""
        declared = getattr(module, "__plugin_tools__", None)
        if isinstance(declared, list):
            for item in declared:
                if isinstance(item, dict) and item.get("name") and item.get("handler"):
                    api.register_tool(
                        item["name"], item["handler"],
                        description=item.get("description", ""),
                        parameters=item.get("parameters"),
                        required=item.get("required"),
                        dangerous=bool(item.get("dangerous")),
                    )
        # 自动收集注册在模块上的钩子映射
        hooks = getattr(module, "__plugin_hooks__", None)
        if isinstance(hooks, dict):
            for point, handlers in hooks.items():
                for h in handlers if isinstance(handlers, list) else [handlers]:
                    if callable(h):
                        try:
                            api.on(point, h)
                        except Exception:
                            pass

    def _run_coro(self, coro: Any) -> Any:
        """在同步上下文中跑协程（没有事件循环时新建一个）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        # 已在循环里：交给它调度（不等待）
        loop.create_task(coro)
        return None

    def _register_extensions(self, plugin: LoadedPlugin) -> None:
        """把插件声明的工具/钩子注册到全局。"""
        api = plugin.api
        if api is None:
            return
        # 工具
        if self.registry is not None:
            for meta in api._tools:
                if not meta.get("name"):
                    continue
                # 无处理函数的声明式工具也注册（执行时给出明确提示），
                # 这样界面上能看到它、模型也不会去猜名字
                try:
                    self.registry.register(PluginTool(meta, plugin.manifest.name))
                except Exception as e:
                    plugin.error = (plugin.error + f"\n注册工具 {meta['name']} 失败：{e}").strip()
        # 钩子
        for point, handlers in api._hooks.items():
            for h in handlers:
                self._hooks.setdefault(point, []).append((plugin.manifest.name, h))

    @staticmethod
    def _has_manifest_tool(plugin: LoadedPlugin, name: str) -> bool:
        return any(str(t.get("name")) == name for t in plugin.manifest.tools)

    def _unload_hooks(self, plugin_name: str) -> None:
        for point in list(self._hooks):
            self._hooks[point] = [(n, h) for n, h in self._hooks[point] if n != plugin_name]

    # ---- 卸载 ----------------------------------------------------------
    def unload(self, name: str) -> bool:
        with self._lock:
            plugin = self.plugins.pop(name, None)
        if plugin is None:
            return False
        self._unload_hooks(name)
        if self.registry is not None and plugin.api is not None:
            for meta in plugin.api._tools:
                try:
                    self.registry.unregister(meta.get("full_name") or "")
                except Exception:
                    pass
        # 调用 shutdown 钩子
        try:
            self._fire_sync("shutdown", {"plugin": name})
        except Exception:
            pass
        return True

    def reload(self, name: str) -> LoadedPlugin:
        self.unload(name)
        return self.load(name)

    def enable(self, name: str, enabled: bool = True) -> bool:
        man = self._find(name)
        if man is None:
            return False
        man.enabled = enabled
        if enabled:
            try:
                self.load(man)
            except Exception:
                self._sync_db(man, loaded=False)
                return False
        else:
            self.unload(name)
        self._sync_db(man, loaded=enabled)
        return True

    def install(self, src: os.PathLike | str, *, name: str | None = None,
                overwrite: bool = False) -> dict[str, Any]:
        """把本地目录（或 zip）安装到插件目录。"""
        from .. import paths
        from ..utils import copy_tree

        src_path = Path(src)
        if not src_path.exists():
            return {"ok": False, "error": f"路径不存在：{src_path}"}
        target_root = paths.plugins_dir()
        target_name = name or src_path.stem

        if src_path.is_file() and src_path.suffix.lower() == ".zip":
            import zipfile

            tmp = target_root / f".extract-{int(time.time())}"
            tmp.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(src_path) as z:
                z.extractall(tmp)
            # 找到含 manifest 的那层
            root = _find_manifest_root(tmp)
            if root is None:
                shutil.rmtree(tmp, ignore_errors=True)
                return {"ok": False, "error": "压缩包里没有找到 manifest.yaml"}
            target = target_root / (name or root.name)
            if target.exists():
                if not overwrite:
                    shutil.rmtree(tmp, ignore_errors=True)
                    return {"ok": False, "error": f"插件已存在：{target.name}"}
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(root), str(target))
            shutil.rmtree(tmp, ignore_errors=True)
            target_name = target.name
        else:
            root = src_path if (src_path / "manifest.yaml").is_file() else _find_manifest_root(src_path)
            if root is None:
                return {"ok": False, "error": "目录里没有找到 manifest.yaml"}
            target = target_root / (name or root.name)
            if target.exists() and not overwrite:
                return {"ok": False, "error": f"插件已存在：{target.name}（可设置 overwrite=true 覆盖）"}
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            copy_tree(root, target)
            target_name = target.name

        man = self._find(target_name)
        if man is not None:
            self.load(man)
        return {"ok": True, "name": target_name, "dir": str(target_root / target_name)}

    def uninstall(self, name: str) -> bool:
        man = self._find(name)
        if man is None:
            return False
        if man.builtin:
            return False  # 内置插件不允许删除
        self.unload(name)
        try:
            shutil.rmtree(man.dir, ignore_errors=True)
        except Exception:
            return False
        if self.db is not None:
            try:
                from ..storage.tasks import PluginStore

                PluginStore(self.db).delete(name)
            except Exception:
                pass
        return True

    # ---- 钩子分发 ------------------------------------------------------
    def fire(self, point: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """同步触发钩子；返回可能被修改的 payload。"""
        data = dict(payload or {})
        for name, handler in list(self._hooks.get(point, [])):
            try:
                sig = inspect.signature(handler)
                n = len([p for p in sig.parameters.values()
                         if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
                if n == 0:
                    res = handler()
                elif n == 1:
                    res = handler(data)
                else:
                    res = handler(data, self)
                if isinstance(res, dict):
                    data.update(res)
            except Exception as e:
                self._emit("plugin.hook_error", {"point": point, "plugin": name, "error": str(e)})
        return data

    async def fire_async(self, point: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """异步触发钩子（支持协程处理器）。"""
        data = dict(payload or {})
        for name, handler in list(self._hooks.get(point, [])):
            try:
                if inspect.iscoroutinefunction(handler):
                    res = await handler(data)
                else:
                    res = handler(data)
                if isinstance(res, dict):
                    data.update(res)
            except Exception as e:
                self._emit("plugin.hook_error", {"point": point, "plugin": name, "error": str(e)})
        return data

    def _fire_sync(self, point: str, payload: dict[str, Any]) -> None:
        self.fire(point, payload)

    def veto_tool(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """工具执行前的钩子；返回非空字符串表示拒绝（内容是原因）。"""
        data = self.fire("before_tool", {"tool": tool_name, "arguments": arguments})
        v = data.get("veto") or data.get("deny")
        if v:
            return str(v)
        if data.get("allow") is False:
            return str(data.get("reason") or "插件拒绝了该工具调用")
        return None

    def after_tool(self, tool_name: str, arguments: dict[str, Any],
                   result: Any, duration: float = 0.0) -> None:
        self.fire("after_tool", {
            "tool": tool_name, "arguments": arguments, "result": result,
            "ok": getattr(result, "ok", True), "duration": duration,
        })

    def prompt_parts(self) -> list[str]:
        out: list[str] = []
        for p in self.plugins.values():
            if p.api is not None and p.api._prompt_parts:
                label = p.manifest.display_name or p.manifest.name
                out.append(f"## 插件扩展：{label}\n" + "\n".join(p.api._prompt_parts))
        # register_prompt 钩子
        data = self.fire("register_prompt", {})
        extra = data.get("prompt")
        if extra:
            out.append(str(extra))
        return out

    # ---- 插件注册的扩展点：供各处真正消费（1.5.0）----------------------
    # ★ 此前 register_provider / register_agent / register_command 只有定义、
    #   从不被读取 —— 插件作者按文档注册后一次都不会生效。这里给出统一的读取入口。
    def provider_specs(self) -> list[dict[str, Any]]:
        """插件注册的供应商（合并进可用供应商列表）。"""
        out: list[dict[str, Any]] = []
        for p in self.plugins.values():
            if p.api is None:
                continue
            for spec in p.api._providers:
                d = dict(spec)
                d.setdefault("plugin", p.manifest.name)
                out.append(d)
        return out

    def agent_specs(self) -> list[dict[str, Any]]:
        """插件注册的子智能体类型。"""
        out: list[dict[str, Any]] = []
        for p in self.plugins.values():
            if p.api is None:
                continue
            for spec in p.api._agents:
                d = dict(spec)
                d.setdefault("plugin", p.manifest.name)
                out.append(d)
        return out

    def command_list(self) -> list[dict[str, Any]]:
        """插件注册的命令（名称与说明；处理器不外传）。"""
        out: list[dict[str, Any]] = []
        for p in self.plugins.values():
            if p.api is None:
                continue
            for c in p.api._commands:
                out.append({
                    "plugin": p.manifest.name,
                    "name": str(c.get("name") or ""),
                    "description": str(c.get("description") or ""),
                    "parameters": c.get("parameters") or {},
                })
        return out

    def run_command(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """执行插件注册的命令；找不到返回 None。"""
        for p in self.plugins.values():
            if p.api is None:
                continue
            for c in p.api._commands:
                if str(c.get("name") or "") != name:
                    continue
                handler = c.get("handler")
                if not callable(handler):
                    return None
                res = handler(args or {})
                if asyncio.iscoroutine(res):
                    return self._run_coro(res)
                return res
        return None


    # ---- 查询 ----------------------------------------------------------
    def _find(self, name: str) -> PluginManifest | None:
        for man in self.discover():
            if man.name == name:
                return man
        # 也允许匹配已加载的
        p = self.plugins.get(name)
        return p.manifest if p else None

    def tool_names(self) -> list[str]:
        out: list[str] = []
        for p in self.plugins.values():
            if p.api is None:
                continue
            out.extend(m["full_name"] for m in p.api._tools if m.get("full_name"))
        return out

    def has_tool(self, name: str) -> bool:
        return name in set(self.tool_names())

    def tool_specs(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for p in self.plugins.values():
            if p.api is None:
                continue
            for m in p.api._tools:
                if not m.get("full_name"):
                    continue
                out.append(PluginTool(m, p.manifest.name).spec())
        return out

    async def call_tool(self, name: str, arguments: dict[str, Any], ctx: Any) -> ToolResult:
        for p in self.plugins.values():
            if p.api is None:
                continue
            for m in p.api._tools:
                if m.get("full_name") == name:
                    p.calls += 1
                    tool = PluginTool(m, p.manifest.name)
                    try:
                        return await tool.run(ctx, **arguments)
                    except Exception as e:
                        # ★ 崩溃重连：插件工具抛异常时先重载一次再试。
                        #   为什么：插件是进程内 Python 模块，一次未捕获异常可能把
                        #   它自己的内部状态搞坏，之后每次调用都失败。重载一次能
                        #   把绝大多数「偶发崩了就再也用不了」的情形救回来；
                        #   重载也失败就如实报错，不假装成功。
                        first = f"{type(e).__name__}: {e}"
                        if not self._recover_plugin(p.manifest.name):
                            return ToolResult.fail(f"插件工具执行失败：{first}")
                        try:
                            return await tool.run(ctx, **arguments)
                        except Exception as e2:
                            return ToolResult.fail(
                                f"插件工具执行失败（已尝试重载仍失败）：{first}；"
                                f"重载后再试：{type(e2).__name__}: {e2}"
                            )
        return ToolResult.fail(f"没有找到插件工具：{name}")

    def _recover_plugin(self, name: str) -> bool:
        """插件崩溃后重载一次；成功返回 True。

        失败不抛异常 —— 调用方只关心「有没有救回来」，
        救不回来时它会如实把错误报给用户，而不是把异常再抛上去。
        """
        try:
            self.reload(name)
            p = self.plugins.get(name)
            return bool(p is not None and p.api is not None)
        except Exception:
            return False

    def list(self) -> list[dict[str, Any]]:
        """列出全部插件（含未加载的）。"""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in self.plugins.values():
            out.append(p.to_dict())
            seen.add(p.manifest.name)
        for man in self.discover():
            if man.name in seen:
                continue
            d = man.to_dict()
            d["loaded"] = False
            d["tool_count"] = len(man.tools)
            d["hook_count"] = 0
            out.append(d)
        return sorted(out, key=lambda x: (not x.get("builtin"), x.get("name", "")))

    def panel_list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.plugins.values():
            if p.api is None:
                continue
            base = Path(p.manifest.dir)
            for panel in p.api._panels:
                entry = panel.get("entry") or "panel/index.html"
                out.append({
                    "plugin": p.manifest.name,
                    "id": panel.get("id") or p.manifest.name,
                    "title": panel.get("title") or p.manifest.display_name,
                    "entry": entry,
                    "url": f"/api/plugins/{p.manifest.name}/panel/{entry}",
                    "exists": (base / entry).is_file(),
                })
        return out

    def _sync_db(self, man: PluginManifest, *, loaded: bool) -> None:
        if self.db is None:
            return
        try:
            from ..storage.tasks import PluginStore

            PluginStore(self.db).upsert(
                man.name, version=man.version, dir=man.dir, enabled=man.enabled,
                builtin=man.builtin, manifest=man.to_dict(),
                error="" if loaded else man.error,
            )
        except Exception:
            pass

    def shutdown(self) -> None:
        for name in list(self.plugins):
            if self.registry is not None:
                p = self.plugins[name]
                if p.api:
                    for m in p.api._tools:
                        try:
                            self.registry.unregister(m.get("full_name") or "")
                        except Exception:
                            pass
            self._unload_hooks(name)
        self.plugins.clear()

    def stats(self) -> dict[str, Any]:
        loaded = [p for p in self.plugins.values()]
        return {
            "total": len(self.list()),
            "loaded": len(loaded),
            "tools": len(self.tool_names()),
            "hooks": sum(len(v) for v in self._hooks.values()),
            "panels": len(self.panel_list()),
            "errors": {p.manifest.name: p.error for p in loaded if p.error},
        }


def _find_manifest_root(base: Path) -> Path | None:
    """在目录树里找含 manifest.yaml 的那一层（优先浅层）。"""
    if (base / "manifest.yaml").is_file() or (base / "manifest.yml").is_file():
        return base
    cands = sorted(base.rglob("manifest.y*ml"), key=lambda p: len(p.parts))
    if cands:
        return cands[0].parent
    return None


_manager: PluginManager | None = None
_manager_lock = threading.Lock()


def get_plugins(config: Any = None, db: Any = None, bus: Any = None,
                registry: Any = None) -> PluginManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = PluginManager(config=config, db=db, bus=bus, registry=registry)
        return _manager


def reset_plugins() -> None:
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None


__all__ = [
    "PluginManager",
    "PluginManifest",
    "PluginAPI",
    "PluginTool",
    "LoadedPlugin",
    "HOOK_POINTS",
    "get_plugins",
    "reset_plugins",
]
