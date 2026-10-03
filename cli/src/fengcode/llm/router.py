"""LLM 门面：模型引用解析、客户端缓存、调用、用量与成本统计。

上层只需 ``await llm.chat(messages, tools=...)`` 或 ``async for ev in llm.chat_stream(...)``，
不需要关心具体是 OpenAI / Anthropic / Gemini。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..config.catalog import lookup as catalog_lookup
from ..config.manager import ConfigManager, get_manager
from ..config.schema import Provider
from .anthropic_client import AnthropicClient
from .base import BaseLLMClient, LLMError
from .gemini_client import GeminiClient
from .openai_client import OpenAIClient, OpenAIResponsesClient
from .types import LLMResponse, Message, StreamEvent, ToolSpec, Usage

_CLIENT_CLASSES: dict[str, type[BaseLLMClient]] = {
    "openai": OpenAIClient,
    # ★ 供应商协议矩阵：Responses 协议的请求/响应结构与 chat 不同，
    #   单独一个客户端，避免把 chat 的 body 打过去导致 400。
    "openai-responses": OpenAIResponsesClient,
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
}


@dataclass
class CallRecord:
    """一次调用的记账信息。"""

    provider: str
    model: str
    usage: Usage
    cost: float = 0.0
    currency: str = "¥"
    duration: float = 0.0
    error: str | None = None
    # ★ 缓存未命中的归因信息（见 CacheDiagnostics）。
    #   为什么要带着它：命中率只有一个数字时，「为什么这次没命中」只能靠猜 ——
    #   是系统提示变了？工具定义变了？还是历史被重写过？带上归因后就能直接回答。
    diagnostics: "CacheDiagnostics | None" = None


@dataclass
class CacheDiagnostics:
    """本次调用相对上一次调用，**可缓存前缀**是否发生了变化、变在哪。

    上游的前缀缓存是「从头逐字节比对」，一旦某处不同、之后全部失效。所以能命中多少，
    取决于「这次请求的前缀与上次相同到第几个字节」。这里把前缀拆成几个组成部分分别取指纹，
    就能回答「是哪个部分变了导致的未命中」：

      · system —— 系统提示词（含工作区、模式、技能目录等）
      · tools  —— 工具定义（增删工具、改了描述都会变）
      · log    —— 历史消息内容（被压缩重写过、或消息被删改）
      · body   —— 除前缀外的其余正文

    三者都没变却仍然未命中 → 那是**服务商侧**的原因（缓存过期、路由到了别的节点），
    不是本地能修的，据此可以少走弯路。
    """

    prefix_hash: str = ""
    prefix_changed: bool = False
    change_reasons: tuple[str, ...] = ()
    system_hash: str = ""
    tools_hash: str = ""
    body_hash: str = ""
    # 上一次与本轮**共同携带**的消息条数 —— 越少说明历史被改动得越多
    carried_messages: int = 0
    # 工具定义的规模（token 估算），便于判断「是不是工具太多把前缀撑爆了」
    tool_schema_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix_changed": self.prefix_changed,
            "change_reasons": list(self.change_reasons),
            "system_hash": self.system_hash,
            "tools_hash": self.tools_hash,
            "carried_messages": self.carried_messages,
            "tool_schema_tokens": self.tool_schema_tokens,
        }


def _in_peak_hours(spec: str, when: float | None = None) -> bool:
    """当前是否处于「高峰时段」。

    ``spec`` 形如 ``"08:30-00:30"``；跨零点自动识别（如 08:30-00:30 表示
    从 08:30 到次日 00:30 都算高峰）。空串 → 视为全天高峰（即：不启用谷价）。
    """
    s = (spec or "").strip()
    if not s:
        return True
    try:
        import re as _re
        import time as _t

        m = _re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", s)
        if not m:
            return True
        sh, sm, eh, em = (int(m.group(i)) for i in range(1, 5))
        if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
            return True
        lt = _t.localtime(when or _t.time())
        now_min = lt.tm_hour * 60 + lt.tm_min
        a, b = sh * 60 + sm, eh * 60 + em
        if a == b:
            return True                       # 退化成一个点 → 当作不限制
        if a < b:
            return a <= now_min < b
        return now_min >= a or now_min < b    # 跨零点
    except Exception:
        return True


def compute_cost(usage: Usage, price: dict[str, Any] | None, unit: int = 1_000_000,
                 *, when: float | None = None) -> tuple[float, str]:
    """按价格表算钱，返回 ``(金额, 币种)``。

    ★ 峰谷价：价格表里给了 ``off_peak_input`` / ``off_peak_output``
      且当前不在 ``peak_hours`` 内 → 用低谷单价。没给谷价则一切照旧，
      不会改变既有账目。
    """
    if not price:
        return 0.0, "¥"
    try:
        pin = float(price.get("input") or 0)
        pout = float(price.get("output") or 0)
        pcache = float(price.get("cache_hit") or 0)
        unit = int(price.get("unit") or unit) or unit
    except (TypeError, ValueError):
        return 0.0, "¥"
    # 峰谷切换：只有填了谷价才生效
    try:
        op_in = float(price.get("off_peak_input") or 0)
        op_out = float(price.get("off_peak_output") or 0)
        if (op_in or op_out) and not _in_peak_hours(str(price.get("peak_hours") or ""), when):
            if op_in:
                pin = op_in
            if op_out:
                pout = op_out
    except (TypeError, ValueError):
        pass
    cached = max(0, min(usage.cached_tokens, usage.prompt_tokens))
    fresh = max(0, usage.prompt_tokens - cached)
    amount = (
        fresh * pin / unit
        + cached * (pcache if pcache else pin) / unit
        + usage.completion_tokens * pout / unit
    )
    return amount, str(price.get("currency") or "¥")


def price_phase(price: dict[str, Any] | None, when: float | None = None) -> str:
    """当前计价处于「峰」还是「谷」（供界面显示标签）。

    没配谷价时返回空串 —— 不显示标签，避免给没启用峰谷的用户增加噪音。
    """
    if not price:
        return ""
    try:
        op_in = float(price.get("off_peak_input") or 0)
        op_out = float(price.get("off_peak_output") or 0)
    except (TypeError, ValueError):
        return ""
    if not (op_in or op_out):
        return ""
    return "peak" if _in_peak_hours(str(price.get("peak_hours") or ""), when) else "off_peak"


class LLMClient:
    """对外统一的 LLM 门面。"""

    def __init__(self, manager: ConfigManager | None = None) -> None:
        self.manager = manager or get_manager()
        self._clients: dict[str, BaseLLMClient] = {}
        self._lock = threading.RLock()
        self.records: list[CallRecord] = []
        self._on_record: list[Any] = []
        # ★ 上一次调用的前缀指纹（用于判断本轮前缀有没有变、变在哪）。
        #   按 (供应商, 模型) 分别记 —— 换模型后前缀本来就不同，不该算「变化」。
        self._last_prefix: dict[tuple[str, str], dict[str, Any]] = {}

    # ---- 客户端管理 ----------------------------------------------------
    def _make_client(self, provider: Provider) -> BaseLLMClient:
        cls = _CLIENT_CLASSES.get(provider.kind, OpenAIClient)
        api_key = self.manager.resolve_api_key(provider)
        return cls(provider, api_key)

    def client_for(self, provider_name: str | None) -> tuple[BaseLLMClient, Provider]:
        """取（或创建）某供应商的客户端。"""
        cfg = self.manager.config
        prov = self.manager.get_provider(provider_name)
        if prov is None:
            enabled = [p for p in cfg.providers if p.enabled] or cfg.providers
            if not enabled:
                raise LLMError("尚未配置任何模型供应商，请在设置中添加")
            prov = enabled[0]
        with self._lock:
            key = f"{prov.name}:{prov.kind}:{prov.base_url}"
            client = self._clients.get(key)
            if client is None:
                client = self._make_client(prov)
                self._clients[key] = client
            return client, prov

    def invalidate(self, provider_name: str | None = None) -> None:
        """配置变更后清掉缓存的客户端（密钥/地址可能已改）。"""
        with self._lock:
            if provider_name is None:
                for c in self._clients.values():
                    asyncio_ensure_close(c)
                self._clients.clear()
                return
            for key in [k for k in self._clients if k.startswith(provider_name + ":")]:
                asyncio_ensure_close(self._clients.pop(key))

    async def aclose(self) -> None:
        with self._lock:
            for c in self._clients.values():
                try:
                    await c.aclose()
                except Exception:
                    pass
            self._clients.clear()

    # ---- 解析模型引用 --------------------------------------------------
    def resolve(self, model_ref: str | None) -> tuple[BaseLLMClient, Provider, str, str]:
        """把 ``provider/model`` 解析成 ``(客户端, 供应商, 供应商名, 模型名)``。"""
        ref = model_ref or self.manager.default_model_ref()
        if not ref:
            raise LLMError("尚未选择模型，请在设置中添加供应商并选择默认模型")
        pname, mname = self.manager.parse_model_ref(ref)
        client, prov = self.client_for(pname)
        model = mname or prov.default or (prov.models[0] if prov.models else ref)
        return client, prov, prov.name, model

    def model_capabilities(self, model_ref: str | None) -> dict[str, Any]:
        ref = model_ref or self.manager.default_model_ref()
        if not ref:
            return {}
        pname, mname = self.manager.parse_model_ref(ref)
        prov = self.manager.get_provider(pname)
        model = mname or (prov.default if prov else None) or (ref or "")
        return self.manager.model_info(model, prov)

    # ---- 调用 ----------------------------------------------------------
    def _call_kwargs(self, provider: Provider, model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        cfg = self.manager.config
        out = dict(kwargs)
        out.setdefault("temperature", cfg.llm.temperature)
        # 输出上限：优先级 调用方显式传值 > 配置值 > 模型自报值 > 不传（交给供应商默认）
        # 为什么去掉 min(模型值, 配置值)：配置默认 None 表示「不填即不限」，
        # 旧写法 int(info.get(...) or cfg.llm.max_tokens) 在 cfg 为 None 时会抛
        # TypeError，且 min(..., 8192) 会把模型实际能输出的更长内容截掉。
        if out.get("max_tokens") is None:
            limit = cfg.llm.max_tokens
            if limit is None:
                info = self.manager.model_info(model, provider)
                limit = info.get("max_output_tokens")
            if limit:
                out["max_tokens"] = int(limit)
        out.setdefault("effort", provider.default_effort)
        return out

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        tools: list[ToolSpec] | None = None,
        fallback: bool = True,
        **kwargs: Any,
    ) -> LLMResponse:
        import time

        cfg = self.manager.config
        attempts = [model] if model else []
        if fallback:
            attempts.extend(cfg.llm.fallback_models or [])
        if not attempts:
            attempts = [None]  # type: ignore[list-item]

        last_err: Exception | None = None
        for ref in attempts:
            try:
                client, prov, pname, mname = self.resolve(ref)
            except LLMError as e:
                last_err = e
                continue
            t0 = time.time()
            # ★ 发起前先算缓存前缀归因（此时才知道这次带的是什么），
            #   供「命中率为什么掉了」定位用。
            try:
                diag = self._diagnose(pname, mname, messages, tools)
            except Exception:
                diag = None
            try:
                resp = await client.chat(
                    messages, model=mname, tools=tools,
                    **self._call_kwargs(prov, mname, kwargs),
                )
                self._record(pname, mname, resp.usage, time.time() - t0, prov,
                             error=resp.error, diagnostics=diag)
                if resp.error and fallback and len(attempts) > 1:
                    last_err = LLMError(resp.error)
                    continue
                return resp
            except LLMError as e:
                self._record(pname, mname, Usage(), time.time() - t0, prov,
                             error=str(e), diagnostics=diag)
                last_err = e
                continue
            except Exception as e:  # pragma: no cover
                self._record(pname, mname, Usage(), time.time() - t0, prov,
                             error=str(e), diagnostics=diag)
                last_err = e
                continue
        if isinstance(last_err, LLMError):
            raise last_err
        raise LLMError(f"模型调用失败：{last_err}")

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        import time

        client, prov, pname, mname = self.resolve(model)
        t0 = time.time()
        usage = Usage()
        err: str | None = None
        # ★ 与 chat 同理：发起前先算前缀归因，随记账一起留下。
        try:
            diag = self._diagnose(pname, mname, messages, tools)
        except Exception:
            diag = None
        try:
            async for ev in client.chat_stream(
                messages, model=mname, tools=tools, **self._call_kwargs(prov, mname, kwargs)
            ):
                if ev.type == "usage" and ev.usage:
                    usage = ev.usage
                    # ★ 把归因挂在用量事件上带给上层 —— 界面据此解释「为什么没命中」。
                    if diag is not None:
                        try:
                            ev.diagnostics = diag.to_dict()
                        except Exception:
                            pass
                if ev.type == "error":
                    err = ev.error
                yield ev
        except LLMError as e:
            err = str(e)
            yield StreamEvent(type="error", error=err)
        finally:
            self._record(pname, mname, usage, time.time() - t0, prov,
                         error=err, diagnostics=diag)

    def _record(
        self,
        provider: str,
        model: str,
        usage: Usage,
        duration: float,
        prov: Provider,
        *,
        error: str | None = None,
        diagnostics: "CacheDiagnostics | None" = None,
    ) -> None:
        info = self.manager.model_info(model, prov)
        price = info.get("price") or {}
        cost, currency = compute_cost(usage, price)
        rec = CallRecord(
            provider=provider,
            model=model,
            usage=usage,
            cost=cost,
            currency=currency,
            duration=duration,
            error=error,
            diagnostics=diagnostics,
        )
        self.records.append(rec)
        # 只保留最近 2000 条内存记录，持久化交给 stats 模块
        if len(self.records) > 2000:
            del self.records[:1000]
        for cb in list(self._on_record):
            try:
                cb(rec)
            except Exception:
                pass

    # ---- 缓存前缀归因 --------------------------------------------------
    def _fingerprint(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
    ) -> dict[str, Any]:
        """把请求拆成几个组成部分分别取指纹，用于回答「前缀为什么变了」。

        为什么拆开取而不是整体算一个哈希：整体哈希只能告诉你「变了」，
        拆开才能告诉你「变在哪」—— 是系统提示、工具定义、还是历史内容。
        这决定了下一步该去查哪里。
        """
        import hashlib
        import json

        def h(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

        # 系统消息视为前缀的头部（各供应商都把 system 放在最前）
        sys_parts = [m.content or "" for m in messages if m.role == "system"]
        system_hash = h("\x00".join(sys_parts))

        # 工具定义：名字 + 描述 + 参数结构，任一变化都会让前缀失效
        try:
            tools_blob = json.dumps(
                [
                    {"name": getattr(t, "name", ""), "desc": getattr(t, "description", ""),
                     "params": getattr(t, "parameters", None)}
                    for t in (tools or [])
                ],
                ensure_ascii=False, sort_keys=True, default=str,
            )
        except Exception:
            tools_blob = str(tools or "")
        tools_hash = h(tools_blob)

        # 历史正文（非 system 的消息），按顺序拼接
        body_parts = [f"{m.role}:{m.content or ''}" for m in messages if m.role != "system"]
        body_hash = h("\x00".join(body_parts))

        from ..utils import estimate_tokens

        return {
            "system": system_hash,
            "tools": tools_hash,
            "body": body_hash,
            "prefix": h(system_hash + tools_hash),
            "n_messages": len(messages),
            "tool_schema_tokens": estimate_tokens(tools_blob),
            "body_parts": body_parts,
        }

    def _diagnose(
        self,
        provider: str,
        model: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
    ) -> "CacheDiagnostics":
        """算出本次调用相对上一次的归因结论，并更新记忆的前缀状态。"""
        fp = self._fingerprint(messages, tools)
        key = (provider, model)
        prev = self._last_prefix.get(key)

        reasons: list[str] = []
        carried = 0
        changed = True
        if prev is not None:
            if prev.get("system") != fp["system"]:
                reasons.append("system")
            if prev.get("tools") != fp["tools"]:
                reasons.append("tools")
            if prev.get("body") != fp["body"]:
                reasons.append("log")
            changed = bool(reasons)
            # 共同前缀的消息条数：两边从头比，遇到不同就停
            prev_parts = prev.get("body_parts") or []
            cur_parts = fp["body_parts"]
            n = 0
            for a, b in zip(prev_parts, cur_parts):
                if a != b:
                    break
                n += 1
            carried = n

        diag = CacheDiagnostics(
            prefix_hash=fp["prefix"],
            prefix_changed=changed,
            change_reasons=tuple(reasons),
            system_hash=fp["system"],
            tools_hash=fp["tools"],
            body_hash=fp["body"],
            carried_messages=carried,
            tool_schema_tokens=fp["tool_schema_tokens"],
        )
        self._last_prefix[key] = fp
        return diag

    def on_record(self, callback: Any) -> None:
        self._on_record.append(callback)

    # ---- 探测 ----------------------------------------------------------
    async def list_models(self, provider_name: str) -> list[str]:
        client, prov = self.client_for(provider_name)
        return await client.list_models()

    async def test_connection(self, provider_name: str, model: str | None = None) -> dict[str, Any]:
        """连通性测试：发一条极短消息，返回耗时与结果。"""
        import time

        client, prov = self.client_for(provider_name)
        m = model or prov.default or (prov.models[0] if prov.models else "")
        if not m:
            return {"ok": False, "error": "该供应商未配置任何模型"}
        t0 = time.time()
        try:
            resp = await client.chat([Message.user("你好，请只回复两个字：可用")], model=m, max_tokens=32)
            dt = time.time() - t0
            if resp.error:
                return {"ok": False, "error": resp.error, "duration": dt, "model": m}
            return {
                "ok": True,
                "model": m,
                "duration": dt,
                "reply": (resp.content or resp.reasoning or "")[:200],
                "usage": resp.usage.to_dict(),
            }
        except LLMError as e:
            return {"ok": False, "error": str(e), "duration": time.time() - t0, "model": m}
        except Exception as e:  # pragma: no cover
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "model": m}

    async def check_balance(self, provider_name: str) -> dict[str, Any] | None:
        client, _ = self.client_for(provider_name)
        return await client.check_balance()


def asyncio_ensure_close(client: BaseLLMClient) -> None:
    """在没有事件循环时同步忽略关闭（连接由 httpx 自行回收）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(client.aclose())


_llm: LLMClient | None = None
_llm_lock = threading.Lock()


def get_llm(manager: ConfigManager | None = None) -> LLMClient:
    global _llm
    with _llm_lock:
        if _llm is None or (manager is not None and manager is not _llm.manager):
            _llm = LLMClient(manager)
        return _llm


def reset_llm() -> None:
    global _llm
    with _llm_lock:
        _llm = None


def model_choices() -> list[dict[str, Any]]:
    """列出所有可用模型（供界面下拉框使用）。

    ★ ``enabled`` 现在是**逐模型**的开关（存在 ``provider.model_overrides[m].enabled``），
    不再只是供应商级。需求：在「模型服务」页勾选哪些模型可用，
    勾上的才进下拉框；未勾的保留在配置里但不参与路由。
    兼容：老配置里没有任何逐模型开关时，视为「供应商启用即全部启用」。
    """
    mgr = get_manager()
    out: list[dict[str, Any]] = []
    for p in mgr.config.providers:
        models = list(p.models)
        for m in p.model_overrides:
            if m not in models:
                tail = m.split("/")[-1]
                if not any(x.split("/")[-1] == tail for x in models):
                    models.append(m)
        if not models and p.default:
            models = [p.default]
        listed = [m for m in models if _model_enabled(p, m)]
        # 一个都没勾选时不要静默清空下拉框：退回「全部可用」并在界面提示，
        # 否则用户会以为模型全没了。
        if models and not listed:
            listed = models
        for m in models:
            info = mgr.model_info(m, p)
            ov = p.model_overrides.get(m)
            explicit = ov.enabled if (ov is not None and ov.enabled is not None) else None
            out.append(
                {
                    "ref": f"{p.name}/{m}",
                    "provider": p.name,
                    "provider_display": p.display_name or p.name,
                    "model": m,
                    "kind": p.kind,
                    "enabled": p.enabled and _model_enabled(p, m),
                    "provider_enabled": p.enabled,
                    "explicit_enabled": explicit,
                    "listed": m in listed,
                    "has_key": bool(mgr.resolve_api_key(p)),
                    "vision": bool(info.get("vision")),
                    "tools": bool(info.get("tools", True)),
                    "thinking": bool(info.get("thinking")),
                    "context_window": info.get("context_window"),
                    "context_window_source": info.get("context_window_source", ""),
                    "max_output_tokens": info.get("max_output_tokens"),
                    "price": info.get("price"),
                }
            )
    return out


def _model_enabled(p: Provider, model: str) -> bool:
    """逐模型开关：读 ``model_overrides[m].enabled``，没设置则默认启用。"""
    try:
        ov = p.model_overrides.get(model)
    except Exception:
        ov = None
    if ov is not None and getattr(ov, "enabled", None) is not None:
        return bool(ov.enabled)
    return True


__all__ = [
    "LLMClient",
    "LLMError",
    "CallRecord",
    "get_llm",
    "reset_llm",
    "compute_cost",
    "model_choices",
    "BaseLLMClient",
    "OpenAIClient",
    "AnthropicClient",
    "GeminiClient",
]
