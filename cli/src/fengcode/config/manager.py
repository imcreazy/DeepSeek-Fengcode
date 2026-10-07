"""配置管理器：加载 / 保存 / 迁移 / 一键导入。

要点
----
- 默认配置**不含任何密钥**，但预置一组空壳 provider（填 key 即可用）。
- 支持从 ``config.toml`` / ``mcp.json`` 导入 provider 与 MCP 服务器。
- 支持从任意 ``.env`` 识别常见 key 并生成 provider。
- 写入一律 UTF-8 无 BOM；读取容忍 BOM 与损坏（损坏时保留备份）。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from .. import paths
from ..utils import deep_merge, ensure_dir, first_not_none, scrub_secrets, to_plain
from ..utils import toml as tomlutil
from . import catalog
from .schema import (
    Config,
    McpServerConfig,
    ModelOverride,
    Price,
    Provider,
    SchedulerJob,
    RemoteHost,
)


# --------------------------------------------------------------------------
# .env 解析
# --------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env_file(path: os.PathLike | str) -> dict[str, str]:
    """解析 .env 文件为字典；不修改 os.environ。"""
    out: dict[str, str] = {}
    try:
        with io.open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------

def _shell_provider(name: str, *, kind: str = "openai", **kw: Any) -> Provider:
    """构造一个不含密钥的空壳供应商。"""
    data: dict[str, Any] = {"name": name, "kind": kind, "enabled": True}
    data.update(kw)
    return Provider(**data)


def default_config() -> Config:
    """生成默认配置：开箱可用但**不含任何密钥**。"""
    cfg = Config()

    cfg.ui.theme = "light"
    cfg.ui.language = "zh"
    cfg.agent.reasoning_language = "zh"
    cfg.memory.embedding_dim = 512
    cfg.server.port = 7845

    presets = catalog.presets()
    empty_shells: list[Provider] = []
    # 只预置万象 API 一个入口（填 key + 点「获取模型」即可用）。
    # 不再预置一堆空模板 —— 那会让新手误以为已经拥有那些模型。
    p = presets.get("wanxiang")
    if p:
        prov = _shell_provider(
            name="wanxiang",
            kind=p.get("kind", "openai"),
            display_name=p.get("label"),
            base_url=p.get("base_url", ""),
            models=[],
            default=None,
            models_url=p.get("models_url"),
            api_key_env=p.get("api_key_env") or None,
            billing_currency=p.get("billing_currency", "CNY"),
            enabled=False,          # 填了 key 再启用
        )
        empty_shells.append(prov)

    cfg.providers = empty_shells

    # Windows 下默认沙箱与 Shell 设置
    if os.name == "nt":
        cfg.tools.shell.default_shell = ""
        cfg.sandbox.bash = "auto"

    # 默认权限：工作区可改（三档中的中间档，见权限等级说明）
    cfg.permissions.mode = "ask"
    cfg.permissions.write_paths = ["$WORKSPACE", "$TMP"]
    cfg.permissions.read_paths = ["$WORKSPACE", "$HOME", "$TMP"]
    return cfg


def _infer_overrides(models: list[str]) -> dict[str, ModelOverride]:
    out: dict[str, ModelOverride] = {}
    for m in models:
        info = catalog.lookup(m)
        if not info:
            continue
        ov = ModelOverride(
            context_window=info.get("context_window"),
            max_output_tokens=info.get("max_output_tokens"),
            vision=info.get("vision"),
            thinking=info.get("thinking"),
            tools=info.get("tools"),
        )
        price = info.get("price")
        if price:
            ov.price = Price(**price)
        out[m] = ov
    return out


# --------------------------------------------------------------------------
# 配置管理器
# --------------------------------------------------------------------------

class ConfigManager:
    """线程安全的配置管理器（单例式使用，但允许多实例用于测试）。"""

    def __init__(self, path: os.PathLike | str | None = None) -> None:
        self.path = Path(path) if path else paths.config_file()
        self._lock = threading.RLock()
        self._config: Config | None = None
        self._env: dict[str, str] = {}
        self._env_loaded = False

    # ---- 基本读写 ------------------------------------------------------

    @property
    def config(self) -> Config:
        if self._config is None:
            self.load()
        assert self._config is not None
        return self._config

    def load(self, *, force: bool = False) -> Config:
        with self._lock:
            if self._config is not None and not force:
                return self._config
            data = tomlutil.loadf(self.path)
            if not data:
                cfg = default_config()
                self._config = cfg
                # 首次运行：落盘一份，方便用户直接改
                if not self.path.exists():
                    ensure_dir(self.path.parent)
                    self.save()
                self._load_env()
                return cfg
            # ★ 迁移历史默认值：老版本把某些默认值写进了用户配置，之后代码改了默认值，
            #   但**已写进文件的旧值不会自动更新** —— 于是用户一直跑在过时的行为上。
            #   实测踩过两次：max_tokens=8192（输出被截断）、max_steps=30（长任务被掐断）。
            #   ★★ 必须在 `_coerce` **之前**清理 data —— 否则 cfg 已按旧值构造完毕，
            #   再删 data 里的字段就晚了（首次实现正是踩了这个顺序坑，验证时才发现）。
            migrated = self._migrate_legacy_defaults(data)
            cfg = self._coerce(data)
            self._config = cfg
            if migrated:
                try:
                    self.save(backup=True)
                except Exception:
                    pass   # 落盘失败不影响本次加载（内存里的 cfg 已是新默认值）
            self._load_env()
            return cfg

    # 旧默认值 → 说明。值是「老版本写进配置的默认」，不是用户有意设置。
    # ★ 只清理**恰好等于旧默认值**的项：用户若真想要那个数，它会和新默认不同，
    #   不会被误改（而这两个值历史上都不在界面上暴露，人为设置的可能性极低）。
    _LEGACY_DEFAULTS: dict[tuple[str, str], tuple[Any, str]] = {
        # (配置段, 字段) -> (旧默认值, 为什么要迁移)
        ("llm", "max_tokens"): (8192, "旧默认值会把输出截断在 8192，已改为不限制"),
        ("agent", "max_steps"): (30, "旧默认值会在 30 步硬掐断长任务，已改为不限步数"),
        # ★ 余额显示开关已取消（改为强制显示），把残留的键清掉，别让它继续留在配置里
        ("account", "show_balance"): (True, "余额显示已改为强制开启，开关取消"),
        # ★ 沙箱的四个摆设字段已删（全仓库没人读），清掉用户配置里的残留
        ("sandbox", "mode"): ("local", "该字段没有任何代码读，已删除"),
        ("sandbox", "memory_limit_mb"): (2048, "该字段没有任何代码读，已删除"),
        ("sandbox", "max_file_write_mb"): (64, "该字段没有任何代码读，已删除"),
        ("sandbox", "cpu_limit"): (0.0, "该字段没有任何代码读，已删除"),
    }

    def _migrate_legacy_defaults(self, data: dict) -> bool:
        """把配置里残留的「旧默认值」清掉，返回是否发生了改动。

        为什么必须做：代码改了默认值，但**已经写进 config.toml 的值不会变** ——
        用户明明装了新版，行为却还是旧的（实测两次：输出截断在 8192、长任务卡 30 步）。
        """
        changed = False
        for (section, field), (old, why) in self._LEGACY_DEFAULTS.items():
            sec = data.get(section)
            if not isinstance(sec, dict) or field not in sec:
                continue
            if sec[field] != old:
                continue
            del sec[field]
            changed = True
            # 不引入日志依赖：这个模块没有 logger，且迁移只在启动时发生一次，
            # 静默清理即可（需要追溯时看 config.toml.bak-* 备份）。
        return changed

    def _coerce(self, data: dict) -> Config:
        """把原始 dict 转成 Config；失败时降级为"默认 + 尽力合并"。"""
        try:
            return Config(**data)
        except Exception:
            # 结构不兼容（或含未知类型）时：以默认配置为底做深合并
            base = default_config().model_dump()
            merged = deep_merge(base, data)
            try:
                return Config(**merged)
            except Exception:
                return default_config()

    def save(self, *, backup: bool = False) -> Path:
        with self._lock:
            cfg = self.config
            ensure_dir(self.path.parent)
            if backup and self.path.exists():
                shutil.copy2(self.path, self.path.with_suffix(".toml.bak"))
            data = self.to_dict(cfg)
            tomlutil.dumpf(self.path, data)
            return self.path

    def to_dict(self, cfg: Config | None = None) -> dict:
        cfg = cfg or self.config
        d = cfg.model_dump(exclude_none=True)
        # providers 需要更紧凑的输出
        d["providers"] = [_dump_provider(p) for p in cfg.providers]
        return _prune_empty(d)

    def reload(self) -> Config:
        with self._lock:
            self._env_loaded = False
            return self.load(force=True)

    # ---- 修改 -----------------------------------------------------------

    def update(self, patch: dict) -> Config:
        """深合并一份补丁并保存。"""
        with self._lock:
            cur = self.to_dict(self.config)
            merged = deep_merge(cur, patch)
            self._config = self._coerce(merged)
            self.save()
            return self._config

    def set_value(self, dotted: str, value: Any) -> Config:
        """按 ``a.b.c`` 路径设置一个值。"""
        return self.update(_nest(dotted, value))

    # ---- 环境变量 -------------------------------------------------------

    def _load_env(self) -> None:
        if self._env_loaded:
            return
        env: dict[str, str] = {}
        for p in (paths.env_file(), paths.config_dir() / ".env.local"):
            env.update(parse_env_file(p))
        # 额外密钥来源：**只读用户显式登记过的文件**（如主动导入某个 .env 后
        # 写进 external_env_files 的路径）。不主动扫描其它程序的私有 .env——
        # 那是别人的配置，未经同意不该读取。
        for extra in list(getattr(self.config, "external_env_files", []) or []):
            try:
                ep = Path(os.path.expandvars(str(extra))).expanduser()
            except Exception:
                continue
            if ep.is_file():
                for k, v in parse_env_file(ep).items():
                    env.setdefault(k, v)
        self._env = env
        self._env_loaded = True

    def env(self) -> dict[str, str]:
        self._load_env()
        return dict(self._env)

    def env_get(self, key: str | None) -> str | None:
        if not key:
            return None
        self._load_env()
        if key in self._env:
            return self._env[key]
        return os.environ.get(key)

    def resolve_api_key(self, provider: Provider) -> str | None:
        """解析供应商密钥：显式 api_key 优先，其次 api_key_env。"""
        if provider.api_key:
            return provider.api_key.strip()
        return self.env_get(provider.api_key_env)

    def default_env_file(self) -> Path | None:
        """本应用自己的 ``.env`` 位置（数据目录下）。

        Fengcode 是独立应用，只读自己的 ``.env``，不去翻其它程序的目录。
        """
        c = paths.env_file()
        return c if c.is_file() else None

    # ---- 供应商管理 -----------------------------------------------------

    def get_provider(self, name: str | None) -> Provider | None:
        if not name:
            return None
        for p in self.config.providers:
            if p.name == name:
                return p
        # 允许用 display_name 匹配
        for p in self.config.providers:
            if p.display_name and p.display_name == name:
                return p
        return None

    def enabled_providers(self) -> list[Provider]:
        return [p for p in self.config.providers if p.enabled]

    def upsert_provider(self, provider: Provider, *, save: bool = True) -> Provider:
        with self._lock:
            cfg = self.config
            for i, p in enumerate(cfg.providers):
                if p.name == provider.name:
                    cfg.providers[i] = provider
                    break
            else:
                cfg.providers.append(provider)
            if save:
                self.save()
            return provider

    def remove_provider(self, name: str, *, save: bool = True) -> bool:
        """删除一个供应商。

        ★ 1-I：若删掉的正是**当前默认供应商**，不能只清空 default_provider ——
          那样会把默认模型悬空（引用到一个已不存在的供应商），下一次调用直接报错。
          正确做法是先挑一个还能用的供应商顶上，挑不到才置空。
        """
        with self._lock:
            cfg = self.config
            before = len(cfg.providers)
            cfg.providers = [p for p in cfg.providers if p.name != name]
            if len(cfg.providers) != before:
                if cfg.llm.default_provider == name:
                    nxt = self._first_usable_provider()
                    if nxt is not None:
                        cfg.llm.default_provider = nxt.name
                        cfg.llm.default_model = (nxt.models or [None])[0]
                    else:
                        cfg.llm.default_provider = None
                        cfg.llm.default_model = None
                if save:
                    self.save()
                return True
            return False

    def _first_usable_provider(self) -> "Provider | None":
        """挑一个「启用且有 key、且有模型」的供应商；退而求其次「有模型的」。"""
        cfg = self.config
        for p in cfg.providers:
            if p.enabled and p.api_key and p.models:
                return p
        for p in cfg.providers:
            if p.enabled and p.models:
                return p
        for p in cfg.providers:
            if p.models:
                return p
        return None

    # ---- 模型引用 -------------------------------------------------------

    def parse_model_ref(self, ref: str | None) -> tuple[str | None, str | None]:
        """解析 ``provider/model`` 形式的引用。

        模型名本身可能含 ``/``（如 ``qwen/qwen3.8-max:free``），
        因此优先按 **已知 provider 名** 拆分，其次按第一个 ``/``。
        """
        if not ref:
            return None, None
        ref = ref.strip()
        for p in self.config.providers:
            if ref.startswith(p.name + "/"):
                return p.name, ref[len(p.name) + 1 :]
        if "/" in ref:
            head, tail = ref.split("/", 1)
            if self.get_provider(head):
                return head, tail
        # 只有模型名：定位包含它的 provider
        for p in self.config.providers:
            if ref in p.models or ref in p.model_overrides:
                return p.name, ref
        return None, ref

    def default_model_ref(self) -> str | None:
        cfg = self.config
        if cfg.llm.default_provider and cfg.llm.default_model:
            return f"{cfg.llm.default_provider}/{cfg.llm.default_model}"
        if cfg.default_model:
            return cfg.default_model
        for p in cfg.providers:
            if p.enabled and p.models:
                return f"{p.name}/{p.models[0]}"
        for p in cfg.providers:
            if p.models:
                return f"{p.name}/{p.models[0]}"
        return None

    def model_info(self, model: str, provider: Provider | None = None) -> dict[str, Any]:
        """合并「内置能力表 → provider 级默认 → 模型级覆盖」得到模型能力。"""
        info: dict[str, Any] = dict(catalog.lookup(model) or {})
        if provider is not None:
            if provider.context_window:
                info.setdefault("context_window", provider.context_window)
            if provider.max_output_tokens:
                info.setdefault("max_output_tokens", provider.max_output_tokens)
            if provider.thinking is not None:
                info.setdefault("thinking", provider.thinking)
            if provider.default_effort:
                info.setdefault("default_effort", provider.default_effort)
            if provider.supported_efforts:
                info.setdefault("supported_efforts", provider.supported_efforts)
            if model in provider.vision_models:
                info["vision"] = True
            price = provider.prices.get(model) or provider.price
            if price:
                info["price"] = price.model_dump(exclude_none=True)
            ov = provider.model_overrides.get(model) or _find_override(provider, model)
            if ov:
                for k, v in ov.model_dump(exclude_none=True).items():
                    if k == "price" and isinstance(v, dict):
                        info["price"] = v
                    elif k in ("display_name", "description", "tags"):
                        info[k] = v
                    else:
                        info[k] = v
        # ★ 未声明时的默认上下文窗口：1M（1_048_576）。
        #   「各模型的上下文用户设置的多少他就显示多少；没填就默认 1M」——
        #   现在主流模型普遍 1M 起步，旧的 128K 兜底会让界面长期显示一个偏小的值。
        #   注意：这里只是**默认值**，用户在模型服务页逐模型填了就以他填的为准
        #   （下面的 context_window_source 判定保证 source=model 时走用户值）。
        info.setdefault("context_window", 1_048_576)
        # 标注上下文窗口的来源，供界面区分「用户设置 / 模型真报 / 内置表 / 默认值」。
        # 为什么需要：兜底值会让用户误以为自己模型的窗口就是这个数，
        # 实测就出现过显示值与真实能力不符的困惑（所以必须标来源）。
        if "context_window_source" not in info:
            # ★ 判定顺序必须把「逐模型覆盖」放最前：用户在模型服务页给某个模型
            #   单独填了上下文窗口，那就是他显式设置的值，来源应记为 model。
            #   之前漏了这一支，导致填完窗口界面仍按 fallback 判成「未限制」
            #   （实测反馈：设了上下文，右侧栏还是显示未限制）。
            ov_used = None
            if provider is not None:
                ov_used = provider.model_overrides.get(model) or _find_override(provider, model)
            if ov_used is not None and ov_used.context_window:
                info["context_window_source"] = "model"
            elif provider is not None and provider.context_window:
                info["context_window_source"] = "provider"
            elif catalog.lookup(model):
                info["context_window_source"] = "catalog"
            else:
                # 用户没填、内置表也没有 → 用默认值，来源标 default（不再是「未限制」）。
                info["context_window_source"] = "default"
        # max_output_tokens 不再兜底 8192：不填即不限，交给供应商默认。
        # 为什么去掉兜底：内置表/供应商都没声明时，8_192 会变成一道隐性上限，
        # 让「不填即不限」名存实亡（router 会拿它当模型自报值用）。
        # 只有 info 里确实带了值（内置表或供应商声明）时才保留。
        info.setdefault("vision", False)
        info.setdefault("tools", True)
        info.setdefault("thinking", False)
        return info

    # ---- 从用户明确指定的 .env 导入 -------------------------------

    def import_from_env(
        self, path: os.PathLike | str | None = None, *, overwrite: bool = False
    ) -> dict[str, Any]:
        """从 .env 识别常见密钥并生成/补全 provider。"""
        src = Path(path) if path else (self.default_env_file() or paths.env_file())
        result: dict[str, Any] = {
            "source": str(src) if src else None,
            "added": [],
            "updated": [],
            "skipped": [],
            "unknown": [],
        }
        if not src or not Path(src).is_file():
            result["skipped"].append("未找到 .env 文件")
            return result

        env = parse_env_file(src)
        presets = catalog.presets()

        # 1) 命中已知品牌
        matched: dict[str, str] = {}
        for key, val in env.items():
            if not val or not val.strip():
                continue
            pid = catalog.ENV_KEY_HINTS.get(key)
            if pid and key not in matched:
                matched[key] = pid

        with self._lock:
            for env_key, pid in matched.items():
                preset = presets.get(pid)
                if not preset:
                    continue
                val = env[env_key].strip()
                target_name = pid
                existing = self.get_provider(target_name)
                if existing and existing.api_key and not overwrite:
                    result["skipped"].append(target_name)
                    continue
                prov = Provider(
                    name=target_name,
                    kind=preset.get("kind", "openai"),  # type: ignore[arg-type]
                    display_name=preset.get("label"),
                    base_url=preset.get("base_url", ""),
                    models=list(preset.get("models") or []),
                    default=preset.get("default") or None,
                    models_url=preset.get("models_url"),
                    balance_url=preset.get("balance_url"),
                    api_key=val,
                    api_key_env=env_key,
                    billing_currency=preset.get("billing_currency", "CNY"),
                    enabled=True,
                )
                prov.model_overrides = _infer_overrides(prov.models)
                self.upsert_provider(prov, save=False)
                (result["updated"] if existing else result["added"]).append(target_name)

            # 2) 未识别但像密钥的变量 → 生成自定义 provider
            for env_key, val in env.items():
                if not val or not val.strip():
                    continue
                if env_key in matched:
                    continue
                if not _looks_like_key(env_key, val):
                    continue
                name = _custom_name_from_env(env_key)
                if self.get_provider(name) and not overwrite:
                    result["skipped"].append(name)
                    continue
                prov = Provider(
                    name=name,
                    kind="openai",
                    display_name=f"自定义（{env_key}）",
                    base_url="",
                    models=[],
                    api_key=val.strip(),
                    api_key_env=env_key,
                    enabled=False,
                )
                self.upsert_provider(prov, save=False)
                result["unknown"].append(f"{name} ← {env_key}")

            self.save()

        # 导入后要让 env 缓存失效，否则解析不到新密钥
        self._env_loaded = False
        self._env = {}

        return result

    # ---- 便捷构造 -------------------------------------------------------

    def add_provider(
        self,
        name: str,
        *,
        kind: str = "openai",
        base_url: str = "",
        api_key: str | None = None,
        api_key_env: str | None = None,
        models: list[str] | None = None,
        default: str | None = None,
        enabled: bool = True,
        preset: str | None = None,
    ) -> Provider:
        preset_data = catalog.presets().get(preset or "", {}) if preset else {}
        models = list(models or preset_data.get("models") or [])
        # ★ 思考档位必须从 preset 带过来，否则 Provider.default_effort 为 None，
        #   router 的 setdefault 拿到 None，客户端直接跳过 → 请求不带思考参数 →
        #   模型完全不思考（实测万象就是这样）。effort_style 走 extra。
        preset_extra: dict[str, Any] = {}
        if preset_data.get("effort_style"):
            preset_extra["effort_style"] = preset_data["effort_style"]
        prov = Provider(
            name=name,
            kind=kind or preset_data.get("kind", "openai"),  # type: ignore[arg-type]
            display_name=preset_data.get("label"),
            base_url=base_url or preset_data.get("base_url", ""),
            models=models,
            default=default or preset_data.get("default") or (models[0] if models else None),
            models_url=preset_data.get("models_url"),
            balance_url=preset_data.get("balance_url"),
            api_key=api_key,
            api_key_env=api_key_env,
            billing_currency=preset_data.get("billing_currency", "CNY"),
            enabled=enabled,
            default_effort=preset_data.get("default_effort"),
            extra=preset_extra,
        )
        prov.model_overrides = _infer_overrides(models)
        return self.upsert_provider(prov)

    def ensure_defaults(self) -> None:
        """补齐可能缺失的关键字段（首次运行向导后调用）。"""
        cfg = self.config
        if not cfg.llm.default_provider:
            ref = self.default_model_ref()
            if ref:
                p, m = self.parse_model_ref(ref)
                cfg.llm.default_provider = p
                cfg.llm.default_model = m


def _find_override(provider: Provider, model: str) -> ModelOverride | None:
    if model in provider.model_overrides:
        return provider.model_overrides[model]
    tail = model.split("/")[-1]
    for k, v in provider.model_overrides.items():
        if k.split("/")[-1] == tail:
            return v
    return None


def _env_field_to_name(field: str) -> str:
    """``foo_token_env`` → ``FOO_TOKEN``。"""
    base = field[: -len("_env")]
    return base.upper()


def _looks_like_key(env_key: str, value: str) -> bool:
    if len(value) < 20:
        return False
    if not re.search(r"(?i)(key|token|secret)", env_key):
        return False
    if re.search(r"(?i)(password|smtp|admin|session|remote|connection|workspace)", env_key):
        return False
    return bool(re.match(r"^[A-Za-z0-9_\-\.]{20,}$", value))


def _custom_name_from_env(env_key: str) -> str:
    base = re.sub(r"(?i)_?api_?key$", "", env_key)
    base = re.sub(r"(?i)_?key$", "", base)
    base = re.sub(r"^CUSTOM_?", "", base, flags=re.I) or base
    return (base or env_key).lower()[:40]


def _looks_secret(key: str, value: str) -> bool:
    """判断某个 env 键值是否像密钥（用于导入时的安全提示）。"""
    if not value or len(value) < 12:
        return False
    k = (key or "").upper()
    if any(h in k for h in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")):
        return True
    # 值本身长得像密钥
    return bool(
        re.match(r"^(sk-|ghp_|gho_|ark-|AKLT|nvapi-|user_|Bearer )", value)
    )


def _nest(dotted: str, value: Any) -> dict:
    parts = [p for p in dotted.split(".") if p]
    out: Any = value
    for p in reversed(parts):
        out = {p: out}
    return out


def _merge_unique(existing: list[str], item: str) -> list[str]:
    """往列表里加一项（不存在才加），保持顺序。"""
    out = list(existing)
    if item and item not in out:
        out.append(item)
    return out


def _dump_provider(p: Provider) -> dict:
    d = p.model_dump(exclude_none=True)
    # 清理空容器与默认值，保持配置文件可读
    for k in list(d.keys()):
        if d[k] in ([], {}):
            del d[k]
    if d.get("enabled") is True:
        d.pop("enabled", None)
    if d.get("timeout_seconds") == 300.0:
        d.pop("timeout_seconds", None)
    if d.get("max_retries") == 2:
        d.pop("max_retries", None)
    if d.get("billing_currency") == "CNY":
        d.pop("billing_currency", None)
    # 若已有 api_key，去掉 api_key_env 避免混淆（保留其一）
    if d.get("api_key"):
        d.pop("api_key_env", None)
    return d


def _prune_empty(obj: Any) -> Any:
    """递归删除空容器与 None，但保留 False / 0 / ""。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            vv = _prune_empty(v)
            if vv is None:
                continue
            if isinstance(vv, (dict, list)) and not vv:
                continue
            out[k] = vv
        return out
    if isinstance(obj, list):
        return [_prune_empty(x) for x in obj]
    return obj


# --------------------------------------------------------------------------
# 全局单例
# --------------------------------------------------------------------------

_manager: ConfigManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> ConfigManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ConfigManager()
        return _manager


def get_config() -> Config:
    return get_manager().config


def reload_config() -> Config:
    return get_manager().reload()


__all__ = [
    "ConfigManager",
    "get_manager",
    "get_config",
    "reload_config",
    "default_config",
    "parse_env_file",
]
