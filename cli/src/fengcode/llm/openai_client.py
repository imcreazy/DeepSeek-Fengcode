"""OpenAI 兼容客户端（覆盖官方 / DeepSeek / 智谱 / 通义 / 各类中转站）。

兼容点：
- ``/chat/completions``，支持 ``tools`` / ``tool_choice`` / 流式
- 思维链字段：``reasoning_content``（DeepSeek/通义）、``reasoning``（部分中转）
- 多模态：``image_url`` 支持 ``data:image/...;base64,`` 与 http URL
- effort 档位：按 provider 声明映射为 ``reasoning_effort`` / ``thinking``
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from ..config.schema import Provider
from .base import (
    BaseLLMClient,
    finalize_tool_calls,
    iter_sse_lines,
    merge_tool_call_delta,
    parse_usage_openai,
    sse_json,
)
from .types import Attachment, LLMResponse, Message, StreamEvent, ToolCall, ToolSpec, Usage, normalize_roles


class OpenAIClient(BaseLLMClient):
    kind = "openai"

    # ---- 地址 ----------------------------------------------------------
    def _chat_url(self) -> str:
        p = self.provider
        # ★ 供应商协议矩阵：完整 URL 优先于一切拼接。
        #   为什么：接第三方网关时 base_url 常常不是「.../v1」这种规整形状
        #   （可能是 /openai/deployments/x/chat/completions?api-version=...）。
        #   硬拼后缀必然拼错，所以显式给全路径时**原样使用**，一个字符都不动。
        if p.chat_url:
            return p.chat_url
        if p.request_url:
            return p.request_url
        base = (p.base_url or "").rstrip("/")
        if not base:
            base = "https://api.openai.com/v1"
        # 已写明端点 → 不重复拼。
        # ★ 用 `in` 而不是 endswith（ 实测踩到）：接第三方网关时
        #   base_url 常带 query（如 `.../chat/completions?api-version=2024`），
        #   endswith 判不出来，于是尾部又被拼了一次 /v1/chat/completions。
        if "chat/completions" in base:
            return base
        if "/responses" in base:     # Responses 协议的直接写法
            return base
        if base.endswith("/v1") or base.endswith("/v4") or base.endswith("/api/paas/v4"):
            return base + "/chat/completions"
        if "/v1" in base or "/v4" in base:
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _models_url(self) -> str | None:
        p = self.provider
        if p.models_url:
            return p.models_url
        base = (p.base_url or "").rstrip("/")
        if not base:
            return None
        if base.endswith("/v1") or base.endswith("/v4"):
            return base + "/models"
        return base + "/v1/models"

    # ---- 鉴权 ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        for k, v in (self.provider.headers or {}).items():
            h[k] = v
        return h

    # ---- 请求体 --------------------------------------------------------
    def _convert_content(self, msg: Message) -> Any:
        if not msg.attachments:
            return msg.content
        parts: list[dict[str, Any]] = []
        if msg.content:
            parts.append({"type": "text", "text": msg.content})
        for att in msg.attachments:
            if att.kind == "image":
                parts.append({"type": "image_url", "image_url": {"url": self._image_url(att)}})
            elif att.kind == "file":
                # 文本型文件直接内联，其余忽略
                txt = _read_text_attachment(att)
                if txt:
                    parts.append({"type": "text", "text": f"\n【文件 {att.name or att.path}】\n{txt}"})
        return parts or msg.content

    def _image_url(self, att: Attachment) -> str:
        if att.url:
            return att.url
        if att.kind == "image":
            b64 = att.base64()
            if b64:
                return f"data:{att.mime};base64,{b64}"
        return ""

    def _convert_message(self, msg: Message) -> dict[str, Any]:
        d: dict[str, Any] = {"role": msg.role}
        if msg.role == "tool":
            d["role"] = "tool"
            d["content"] = msg.content or ""
            if msg.tool_call_id:
                d["tool_call_id"] = msg.tool_call_id
            return d
        content = self._convert_content(msg)
        d["content"] = content
        if msg.role == "assistant" and msg.tool_calls:
            d["tool_calls"] = [tc.to_openai() for tc in msg.tool_calls]
        # 其它供应商专用的思维链字段回传（少数中转站要求）
        if msg.role == "assistant" and msg.reasoning and self.provider.extra.get("echo_reasoning"):
            d["reasoning_content"] = msg.reasoning
        return d

    def _apply_effort(self, payload: dict[str, Any], kwargs: dict[str, Any]) -> None:
        p = self.provider
        effort = kwargs.get("effort")
        if not effort or effort == "disabled":
            if effort == "disabled" and p.extra.get("disable_thinking_field"):
                payload[p.extra.get("disable_thinking_field", "thinking")] = {"type": "disabled"}
            return
        model = payload.get("model", "")
        ov = _model_extra(p, model)
        style = (ov.get("effort_style") or p.extra.get("effort_style") or "").lower()
        if style == "reasoning_effort" or p.extra.get("reasoning_effort"):
            payload["reasoning_effort"] = _map_reasoning_effort(effort)
        elif style == "thinking_budget":
            budget = {"low": 1024, "medium": 4096, "high": 12000, "max": 32000}.get(effort, 4096)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif style == "enable_thinking":
            payload["enable_thinking"] = True
            payload["thinking_budget"] = {"low": 1024, "medium": 4096, "high": 12000, "max": 32000}.get(effort, 4096)
        else:
            payload["reasoning_effort"] = _map_reasoning_effort(effort)

    def _send_stream_options(self) -> bool:
        """是否发送 `stream_options`（1.2.5 实测复现的兼容性缺陷）。

        ★ 为什么必须判断：`stream_options` 是 2024 年才加入的字段，它要的只是
        「返回用量统计」—— 一个 token 数，不是一回合必需的任何东西。但它会被
        **无条件发给每一个 OpenAI 兼容端点**：较老的网关，或转发时较真的中转，
        会直接拒掉带着它的整个请求。用户侧的现象是「这个中转站别的工具都能用，
        在 Fengcode 里每条消息都被拒」，且报错完全指不到真正的原因。

        做法：对每个端点问一次「你认这个字段吗」，而不是一律假设认。
          1. 供应商显式声明 `extra.stream_options`（true/false）→ 听它的；
          2. 目录里登记过的官方供应商 → 认（它们是现代端点）；
          3. 其余第三方/中转 → 默认**不发**（保守：少一个 token 统计，
             远好过整场对话被拒）；用户可用 `extra.stream_options = true` 强制打开。
        """
        extra = getattr(self.provider, "extra", None) or {}
        flag = extra.get("stream_options")
        if isinstance(flag, bool):
            return flag
        if self.provider.kind == "openai-responses":
            return True
        try:
            from ..config import catalog

            presets = catalog.presets() or {}
            if self.provider.name in presets:
                return True
            base = (self.provider.base_url or "").lower()
            for spec in presets.values():
                b = str((spec or {}).get("base_url") or "").lower()
                if b and b in base:
                    return True
        except Exception:
            pass
        return False

    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        payload: dict[str, Any] = {
            "model": model,
            # ★ 先规范化角色序列：OpenAI 兼容接口要求 system 只在开头，
            #   中间的动态提示改写成 <system-reminder> 的 user 消息，避免上游 400。
            "messages": [self._convert_message(m) for m in normalize_roles(messages)],
            "stream": bool(stream),
        }
        if kwargs.get("temperature") is not None:
            payload["temperature"] = float(kwargs["temperature"])
        mt = kwargs.get("max_tokens") or self.provider.max_output_tokens
        if mt:
            # 部分中转站只认 max_tokens，官方新 API 认 max_completion_tokens
            key = self.provider.extra.get("max_tokens_field", "max_tokens")
            payload[key] = int(mt)
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        if kwargs.get("response_format"):
            payload["response_format"] = kwargs["response_format"]
        if kwargs.get("stop"):
            payload["stop"] = kwargs["stop"]
        self._apply_effort(payload, kwargs)
        if stream and self._send_stream_options():
            payload["stream_options"] = {"include_usage": True}
        # 供应商额外参数
        for k, v in (self.provider.extra.get("default_params") or {}).items():
            payload.setdefault(k, v)
        if kwargs.get("extra_params"):
            payload.update(kwargs["extra_params"])
        return self._chat_url(), payload, self._headers()

    # ---- 响应解析 ------------------------------------------------------
    def parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            err = data.get("error")
            msg = ""
            if isinstance(err, dict):
                msg = str(err.get("message") or err)
            elif err:
                msg = str(err)
            return LLMResponse(model=model, error=msg or "响应中没有 choices 字段", raw=data)
        ch = choices[0] or {}
        msg = ch.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        content = content or ""
        reasoning = (
            msg.get("reasoning_content")
            or msg.get("reasoning")
            or msg.get("thinking")
            or ch.get("reasoning_content")
            or ""
        )
        if isinstance(reasoning, dict):
            reasoning = str(reasoning.get("content") or "")
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or ""
            if not name:
                continue
            raw = fn.get("arguments")
            if not isinstance(raw, str):
                raw = json.dumps(raw or {}, ensure_ascii=False)
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=name,
                    raw_arguments=raw,
                    arguments=ToolCall.parse_arguments(raw),
                    index=i,
                )
            )
        return LLMResponse(
            content=content,
            reasoning=reasoning or "",
            tool_calls=tool_calls,
            usage=parse_usage_openai(data.get("usage")),
            finish_reason=ch.get("finish_reason"),
            model=data.get("model") or model,
            raw=data,
        )

    async def parse_stream(self, resp: httpx.Response, model: str) -> AsyncIterator[StreamEvent]:
        acc: dict[int, dict] = {}
        usage = Usage()
        finish: str | None = None
        got_any = False
        async for line in iter_sse_lines(resp):
            data = sse_json(line)
            if data is None:
                continue
            got_any = True
            err = data.get("error")
            if err:
                msg = err.get("message") if isinstance(err, dict) else str(err)
                yield StreamEvent(type="error", error=str(msg))
                return
            u = data.get("usage")
            if u:
                usage = parse_usage_openai(u)
            for ch in data.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                delta = ch.get("delta") or {}
                if not isinstance(delta, dict):
                    delta = {}
                rc = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
                if isinstance(rc, dict):
                    rc = rc.get("content") or ""
                if rc:
                    yield StreamEvent(type="reasoning", text=str(rc))
                c = delta.get("content")
                if isinstance(c, list):
                    c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                if c:
                    yield StreamEvent(type="text", text=str(c))
                for ev in merge_tool_call_delta(acc, delta.get("tool_calls") or []):
                    yield ev
        if not got_any:
            yield StreamEvent(type="error", error="流式响应为空（上游可能不支持 stream）")
            return
        calls = finalize_tool_calls(acc)
        for tc in calls:
            yield StreamEvent(type="tool_call", tool_call=tc)
        yield StreamEvent(type="usage", usage=usage)
        yield StreamEvent(type="done", finish_reason=finish or ("tool_calls" if calls else "stop"))

    # ---- 模型列表（Responses 协议同用 /models） ------------------------
    async def list_models(self) -> list[str]:
        url = self._models_url()
        if not url:
            return []
        try:
            resp = await self._request("GET", url, headers=self._headers())
            data = resp.json()
        except Exception:
            return []
        items = data.get("data") if isinstance(data, dict) else None
        if items is None and isinstance(data, list):
            items = data
        out: list[str] = []
        for it in items or []:
            if isinstance(it, dict):
                mid = it.get("id") or it.get("name") or it.get("model")
                if mid:
                    out.append(str(mid))
            elif isinstance(it, str):
                out.append(it)
        # 有些中转站返回 {"models": [...]}
        if not out and isinstance(data, dict):
            for k in ("models", "result", "items"):
                arr = data.get(k)
                if isinstance(arr, list):
                    for it in arr:
                        if isinstance(it, str):
                            out.append(it)
                        elif isinstance(it, dict) and (it.get("id") or it.get("name")):
                            out.append(str(it.get("id") or it.get("name")))
        return sorted(set(out))


def _model_extra(provider: Provider, model: str) -> dict[str, Any]:
    ov = provider.model_overrides.get(model)
    if ov is None:
        tail = model.split("/")[-1]
        for k, v in provider.model_overrides.items():
            if k.split("/")[-1] == tail:
                ov = v
                break
    if ov is None:
        return {}
    extra = getattr(ov, "model_extra", None) or {}
    return dict(extra)


def _map_reasoning_effort(effort: str) -> str:
    return {"disabled": "none", "low": "low", "medium": "medium", "high": "high", "max": "high"}.get(
        effort, effort
    )


class OpenAIResponsesClient(OpenAIClient):
    """OpenAI Responses 协议（``/responses``）—— 供应商协议矩阵的一员。

    为什么单独一个类：`/responses` 的**请求体结构**与 `/chat/completions` 不同 ——
      · 消息不是 messages 数组，而是 ``input``；
      · system 走 ``instructions``；
      · 工具 schema 形状也不同（function 字段平铺）。
    直接复用 chat 的 body 打过去必然 400（用户接新协议时最常见的报错）。
    这里只覆盖差异部分，其余（重试、超时、事件解析）继续复用父类。
    """

    kind = "openai-responses"

    def _chat_url(self) -> str:
        p = self.provider
        if p.chat_url:
            return p.chat_url
        if p.request_url:
            return p.request_url
        base = (p.base_url or "").rstrip("/")
        if not base:
            base = "https://api.openai.com/v1"
        # ★ 已是完整端点就原样用（ 实测踩到：把带 query 的完整路径
        #   写在 base_url 时，旧写法仍在尾部又拼了一次 /v1/responses）。
        if "/responses" in base:
            return base
        if base.endswith("/v1"):
            return base + "/responses"
        return base + "/v1/responses" if "/v1" not in base else base + "/responses"


    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        # system → instructions；其余 → input 列表（role + content）
        sys_parts: list[str] = []
        items: list[dict[str, Any]] = []
        for m in messages:
            role = m.role
            if role == "system":
                if m.content:
                    sys_parts.append(m.content)
                continue
            text = m.content or ""
            if role == "tool":
                # 工具结果在 Responses 里也是一种 input item
                items.append({
                    "type": "function_call_output",
                    "call_id": m.tool_call_id or "",
                    "output": text,
                })
                continue
            content: list[dict[str, Any]] = []
            if text:
                content.append({"type": "input_text", "text": text})
            for att in (m.attachments or []):
                try:
                    if getattr(att, "kind", "") == "image":
                        url = _attachment_image_url(att)
                        if url:
                            content.append({"type": "input_image", "image_url": url})
                except Exception:
                    continue
            if not content:
                content = [{"type": "input_text", "text": ""}]
            items.append({"role": role, "content": content})

        payload: dict[str, Any] = {
            "model": model,
            "input": items,
            "stream": bool(stream),
        }
        if sys_parts:
            payload["instructions"] = "\n\n".join(sys_parts)
        mt = kwargs.get("max_tokens") or self.provider.max_output_tokens
        if mt:
            # Responses 用 max_output_tokens，不是 max_tokens
            payload["max_output_tokens"] = int(mt)
        if kwargs.get("temperature") is not None:
            payload["temperature"] = float(kwargs["temperature"])
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        for k, v in (self.provider.extra.get("default_params") or {}).items():
            payload.setdefault(k, v)
        if kwargs.get("extra_params"):
            payload.update(kwargs["extra_params"])
        return self._chat_url(), payload, self._headers()

    def parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        """解析 Responses 格式的返回体。

        ★ 与 chat 格式的差异：正文/思考/工具调用藏在 ``output`` 数组的**多个 item** 里
          （message / reasoning / function_call），而不是 choices[0].message 一处。
          按 item 类型分别收集，才不会「只拿到正文、丢掉思考与工具调用」。
        """
        err = data.get("error")
        if err:
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return LLMResponse(content="", error=str(msg), model=model)

        text_parts: list[str] = []
        reason_parts: list[str] = []
        calls: list[ToolCall] = []
        for item in (data.get("output") or []):
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "message":
                for c in (item.get("content") or []):
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                        text_parts.append(str(c.get("text") or ""))
            elif t == "reasoning":
                for s in (item.get("summary") or []):
                    if isinstance(s, dict) and s.get("text"):
                        reason_parts.append(str(s["text"]))
            elif t == "function_call":
                try:
                    calls.append(ToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        raw_arguments=str(item.get("arguments") or ""),
                        arguments=ToolCall.parse_arguments(str(item.get("arguments") or "")),
                    ))
                except Exception:
                    continue

        usage = parse_usage_openai(data.get("usage"))
        out = LLMResponse(
            content="".join(text_parts),
            reasoning="".join(reason_parts),
            tool_calls=calls,
            usage=usage,
        )
        out.model = model
        return out


def _attachment_image_url(att: Attachment) -> str:
    """把附件转成 Responses 可用的 image_url（dataURL 或 http 链接）。"""
    try:
        if getattr(att, "data", None):
            import base64 as _b64

            mime = att.mime or "image/png"
            return f"data:{mime};base64,{_b64.b64encode(att.data).decode()}"
        if getattr(att, "url", None):
            return str(att.url)
        p = getattr(att, "path", None)
        if p:
            data = Path(p).read_bytes()
            mime = att.mime or "image/png"
            import base64 as _b64

            return f"data:{mime};base64,{_b64.b64encode(data).decode()}"
    except Exception:
        return ""
    return ""


def _read_text_attachment(att: Attachment, limit: int = 200_000) -> str:
    path = att.path
    if not path:
        return ""
    p = Path(path)
    try:
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".exe"}:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""
