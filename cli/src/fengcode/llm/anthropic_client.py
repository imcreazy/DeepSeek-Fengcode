"""Anthropic Claude 客户端（``/v1/messages``）。

要点：
- 系统提示走顶层 ``system`` 字段，不在 messages 里
- 工具调用为 content block 里的 ``tool_use``，结果用 user 的 ``tool_result``
- 支持 ``thinking``（扩展思考）与 prompt caching
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import BaseLLMClient, iter_sse_lines, sse_json
from .types import Attachment, LLMResponse, Message, StreamEvent, ToolCall, ToolSpec, Usage, normalize_roles

ANTHROPIC_VERSION = "2023-06-01"
REQUIRED_BETA = "prompt-caching-2024-07-31,interleaved-thinking-2025-05-14,context-1m-2025-08-07"


class AnthropicClient(BaseLLMClient):
    kind = "anthropic"

    def _endpoint(self) -> str:
        base = (self.provider.base_url or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        h["anthropic-version"] = ANTHROPIC_VERSION
        h["anthropic-beta"] = REQUIRED_BETA
        h["Accept"] = "text/event-stream, application/json"
        if self.api_key:
            h["x-api-key"] = self.api_key
        for k, v in (self.provider.headers or {}).items():
            h[k] = v
        return h

    # ---- 转换 ----------------------------------------------------------
    def _content_blocks(self, msg: Message) -> Any:
        if msg.role == "tool":
            return [
                {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id or "",
                    "content": msg.content or "",
                }
            ]
        blocks: list[dict[str, Any]] = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        for att in msg.attachments:
            if att.kind != "image":
                continue
            url = att.url
            if url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                continue
            b64 = att.base64()
            if b64:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": att.mime or "image/png",
                            "data": b64,
                        },
                    }
                )
        if msg.role == "assistant":
            for tc in msg.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments or {}}
                )
        return blocks or [{"type": "text", "text": ""}]

    def _split_system(self, messages: list[Message]) -> tuple[list[dict], list[dict] | str]:
        system_parts: list[dict] = []
        converted: list[dict] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append({"type": "text", "text": m.content})
                continue
            # Anthropic 不支持连续同角色；合并相邻 user
            role = "user" if m.role in ("user", "tool") else "assistant"
            if converted and converted[-1]["role"] == role:
                prev = converted[-1]["content"]
                if not isinstance(prev, list):
                    prev = [{"type": "text", "text": str(prev)}]
                cur = self._content_blocks(m)
                if not isinstance(cur, list):
                    cur = [{"type": "text", "text": str(cur)}]
                if role == "user" or all(b.get("type") != "tool_result" for b in cur):
                    prev.extend(cur)
                    converted[-1]["content"] = prev
                    continue
                # 含 tool_result 时不合并，另起一条
            converted.append({"role": role, "content": self._content_blocks(m)})
        return converted, (system_parts if system_parts else "")

    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        msgs, system = self._split_system(normalize_roles(messages))
        payload: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "max_tokens": int(kwargs.get("max_tokens") or self.provider.max_output_tokens or 8192),
        }
        if system:
            payload["system"] = system
            # 允许缓存系统提示（成本优化）
            if self.provider.extra.get("prompt_cache", True) and isinstance(system, list):
                payload["system"] = [
                    {**system[0], "cache_control": {"type": "ephemeral"}},
                    *system[1:],
                ]
        if kwargs.get("temperature") is not None:
            payload["temperature"] = float(kwargs["temperature"])
        if kwargs.get("top_p") is not None:
            payload["top_p"] = float(kwargs["top_p"])
        if tools:
            payload["tools"] = [t.to_anthropic() for t in tools]
        if kwargs.get("stop"):
            payload["stop_sequences"] = kwargs["stop"]
        if stream:
            payload["stream"] = True
        effort = kwargs.get("effort")
        if effort and effort != "disabled" and kwargs.get("thinking", self.provider.thinking):
            budget = {"low": 2048, "medium": 6144, "high": 16384, "max": 32768}.get(effort, 8192)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # 扩展思考时不接受 temperature
            payload.pop("temperature", None)
        elif effort == "disabled":
            payload["thinking"] = {"type": "disabled"}
        for k, v in (self.provider.extra.get("default_params") or {}).items():
            payload.setdefault(k, v)
        if kwargs.get("extra_params"):
            payload.update(kwargs["extra_params"])
        return self._endpoint(), payload, self._headers()

    # ---- 解析 ----------------------------------------------------------
    @staticmethod
    def _blocks_to_parts(blocks: list[dict]) -> tuple[str, str, list[ToolCall]]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, b in enumerate(blocks or []):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                text_parts.append(b.get("text") or "")
            elif t == "thinking":
                reasoning_parts.append(b.get("thinking") or "")
            elif t == "redacted_thinking":
                reasoning_parts.append("【思考内容已加密】")
            elif t == "tool_use":
                calls.append(
                    ToolCall(
                        id=b.get("id") or f"call_{i}",
                        name=b.get("name") or "",
                        arguments=b.get("input") if isinstance(b.get("input"), dict) else {},
                        raw_arguments=json.dumps(b.get("input") or {}, ensure_ascii=False),
                        index=i,
                    )
                )
        return "".join(text_parts), "".join(reasoning_parts), [c for c in calls if c.name]

    @staticmethod
    def _usage(u: dict | None) -> Usage:
        u = u or {}
        prompt = int(u.get("input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        cached = int((u.get("cache_read_input_tokens") or 0))
        return Usage(prompt, out, prompt + out, cached, 0)

    def parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        if data.get("type") == "error":
            err = data.get("error") or {}
            return LLMResponse(model=model, error=str(err.get("message") or err), raw=data)
        content, reasoning, calls = self._blocks_to_parts(data.get("content") or [])
        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=calls,
            usage=self._usage(data.get("usage")),
            finish_reason=data.get("stop_reason"),
            model=data.get("model") or model,
            raw=data,
        )

    async def parse_stream(self, resp: httpx.Response, model: str) -> AsyncIterator[StreamEvent]:
        acc: dict[int, dict] = {}
        usage = Usage()
        finish: str | None = None
        cur_block: dict[str, Any] = {}
        block_idx = 0
        async for line in iter_sse_lines(resp):
            data = sse_json(line)
            if data is None:
                continue
            typ = data.get("type")
            if typ == "error":
                err = data.get("error") or {}
                yield StreamEvent(type="error", error=str(err.get("message") or err))
                return
            if typ == "message_start":
                msg = data.get("message") or {}
                usage = self._usage(msg.get("usage"))
            elif typ == "content_block_start":
                block_idx = int(data.get("index") or 0)
                cb = data.get("content_block") or {}
                cur_block = dict(cb)
                if cb.get("type") == "tool_use":
                    acc[block_idx] = {
                        "id": cb.get("id") or f"call_{block_idx}",
                        "name": cb.get("name") or "",
                        "args": "",
                    }
            elif typ == "content_block_delta":
                d = data.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta":
                    yield StreamEvent(type="text", text=d.get("text") or "")
                elif dt == "thinking_delta":
                    yield StreamEvent(type="reasoning", text=d.get("thinking") or "")
                elif dt == "input_json_delta":
                    chunk = d.get("partial_json") or ""
                    slot = acc.setdefault(block_idx, {"id": "", "name": "", "args": ""})
                    if not slot.get("id"):
                        slot["id"] = cur_block.get("id") or f"call_{block_idx}"
                        slot["name"] = cur_block.get("name") or ""
                    slot["args"] += chunk
                    yield StreamEvent(
                        type="tool_delta", text=chunk,
                        meta={"index": block_idx, "name": slot.get("name")},
                    )
                elif dt == "signature_delta":
                    pass
            elif typ == "message_delta":
                d = data.get("delta") or {}
                if d.get("stop_reason"):
                    finish = d["stop_reason"]
                u = data.get("usage")
                if u:
                    usage = Usage(
                        usage.prompt_tokens,
                        int(u.get("output_tokens") or usage.completion_tokens),
                        usage.prompt_tokens + int(u.get("output_tokens") or usage.completion_tokens),
                        usage.cached_tokens,
                        0,
                    )
            elif typ == "message_stop":
                break
        for idx in sorted(acc.keys()):
            slot = acc[idx]
            if not slot.get("name"):
                continue
            yield StreamEvent(
                type="tool_call",
                tool_call=ToolCall(
                    id=slot.get("id") or f"call_{idx}",
                    name=slot["name"],
                    raw_arguments=slot.get("args") or "",
                    arguments=ToolCall.parse_arguments(slot.get("args") or ""),
                    index=idx,
                ),
            )
        yield StreamEvent(type="usage", usage=usage)
        yield StreamEvent(type="done", finish_reason=finish or ("tool_use" if acc else "end_turn"))

    async def list_models(self) -> list[str]:
        base = (self.provider.base_url or "https://api.anthropic.com").rstrip("/")
        url = base + ("/models" if base.endswith("/v1") else "/v1/models")
        try:
            resp = await self._request("GET", url, headers=self._headers())
            data = resp.json()
        except Exception:
            return []
        out = []
        for it in (data.get("data") if isinstance(data, dict) else None) or []:
            if isinstance(it, dict) and it.get("id"):
                out.append(str(it["id"]))
        return sorted(set(out))
