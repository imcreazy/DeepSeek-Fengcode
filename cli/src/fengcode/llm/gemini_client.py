"""Google Gemini 客户端（``generateContent`` / ``streamGenerateContent``）。

要点：
- 消息用 ``contents`` + ``parts``；系统提示走 ``systemInstruction``
- 工具用 ``functionDeclarations``；调用结果用 role=``tool``（官方叫 function response）
- 流式端点需 ``?alt=sse``
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import BaseLLMClient, iter_sse_lines, sse_json
from .types import LLMResponse, Message, StreamEvent, ToolCall, ToolSpec, Usage, normalize_roles

FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "stop",
}


class GeminiClient(BaseLLMClient):
    kind = "gemini"

    def _base(self) -> str:
        base = (self.provider.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        if base.endswith("/v1") or base.endswith("/v1beta"):
            return base
        return base + "/v1beta"

    def _endpoint(self, model: str, stream: bool) -> str:
        action = "streamGenerateContent" if stream else "generateContent"
        url = f"{self._base()}/models/{model}:{action}"
        if stream:
            url += "?alt=sse"
        return url

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        if self.api_key:
            h["x-goog-api-key"] = self.api_key
        for k, v in (self.provider.headers or {}).items():
            h[k] = v
        return h

    # ---- 转换 ----------------------------------------------------------
    def _parts(self, msg: Message) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        if msg.content:
            parts.append({"text": msg.content})
        for att in msg.attachments:
            if att.kind != "image":
                continue
            if att.url and not att.url.startswith("data:"):
                parts.append({"fileData": {"mimeType": att.mime or "image/png", "fileUri": att.url}})
            else:
                b64 = att.base64()
                if b64:
                    parts.append({"inlineData": {"mimeType": att.mime or "image/png", "data": b64}})
        # 函数调用
        for tc in msg.tool_calls:
            parts.append({"functionCall": {"name": tc.name, "args": tc.arguments or {}}})
        return parts or [{"text": ""}]

    def _contents(self, messages: list[Message]) -> tuple[list[dict], dict | None]:
        sys_instruction: dict | None = None
        contents: list[dict] = []
        for m in messages:
            if m.role == "system":
                sys_instruction = {"parts": [{"text": m.content}]}
                continue
            if m.role == "tool":
                fn_name = m.tool_name or "tool"
                try:
                    payload = json.loads(m.content) if m.content.strip().startswith(("{", "[")) else {"result": m.content}
                except ValueError:
                    payload = {"result": m.content}
                contents.append(
                    {"role": "user", "parts": [{"functionResponse": {"name": fn_name, "response": payload}}]}
                )
                continue
            role = "model" if m.role == "assistant" else "user"
            contents.append({"role": role, "parts": self._parts(m)})
        return contents, sys_instruction

    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        contents, sys_instruction = self._contents(normalize_roles(messages))
        payload: dict[str, Any] = {"contents": contents}
        if sys_instruction:
            payload["systemInstruction"] = sys_instruction
        gen: dict[str, Any] = {}
        if kwargs.get("temperature") is not None:
            gen["temperature"] = float(kwargs["temperature"])
        if kwargs.get("max_tokens"):
            gen["maxOutputTokens"] = int(kwargs["max_tokens"])
        if kwargs.get("top_p") is not None:
            gen["topP"] = float(kwargs["top_p"])
        if kwargs.get("stop"):
            gen["stopSequences"] = kwargs["stop"] if isinstance(kwargs["stop"], list) else [kwargs["stop"]]
        effort = kwargs.get("effort")
        if effort and effort != "disabled":
            budget = {"low": 2048, "medium": 8192, "high": 24576, "max": 32768}.get(effort, 8192)
            gen["thinkingConfig"] = {"thinkingBudget": budget, "includeThoughts": True}
        if gen:
            payload["generationConfig"] = gen
        if tools:
            payload["tools"] = [{"functionDeclarations": [t.to_gemini() for t in tools]}]
        for k, v in (self.provider.extra.get("default_params") or {}).items():
            payload.setdefault(k, v)
        if kwargs.get("extra_params"):
            payload.update(kwargs["extra_params"])
        return self._endpoint(model, stream), payload, self._headers()

    # ---- 解析 ----------------------------------------------------------
    def parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        if data.get("error"):
            err = data["error"]
            return LLMResponse(model=model, error=str(err.get("message") or err), raw=data)
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback") or {}
            reason = fb.get("blockReason")
            return LLMResponse(model=model, error=f"无候选结果（{reason}）" if reason else "无候选结果", raw=data)
        cand = cands[0] or {}
        text, reasoning, calls = self._read_parts((cand.get("content") or {}).get("parts") or [])
        um = data.get("usageMetadata") or {}
        return LLMResponse(
            content=text,
            reasoning=reasoning,
            tool_calls=calls,
            usage=Usage(
                int(um.get("promptTokenCount") or 0),
                int(um.get("candidatesTokenCount") or 0),
                int(um.get("totalTokenCount") or 0),
                int(um.get("cachedContentTokenCount") or 0),
                int(um.get("thoughtsTokenCount") or 0),
            ),
            finish_reason=FINISH_MAP.get(str(cand.get("finishReason") or ""), cand.get("finishReason")),
            model=model,
            raw=data,
        )

    @staticmethod
    def _read_parts(parts: list[dict]) -> tuple[str, str, list[ToolCall]]:
        text_parts: list[str] = []
        reason_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, p in enumerate(parts or []):
            if not isinstance(p, dict):
                continue
            if "text" in p:
                if p.get("thought"):
                    reason_parts.append(str(p["text"]))
                else:
                    text_parts.append(str(p["text"]))
            fc = p.get("functionCall")
            if isinstance(fc, dict):
                args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                calls.append(
                    ToolCall(
                        id=f"call_{i}",
                        name=str(fc.get("name") or ""),
                        arguments=args,
                        raw_arguments=json.dumps(args, ensure_ascii=False),
                        index=i,
                    )
                )
        return "".join(text_parts), "".join(reason_parts), [c for c in calls if c.name]

    async def parse_stream(self, resp: httpx.Response, model: str) -> AsyncIterator[StreamEvent]:
        calls: list[ToolCall] = []
        usage = Usage()
        finish: str | None = None
        seen_call_names: set[str] = set()
        async for line in iter_sse_lines(resp):
            data = sse_json(line)
            if data is None:
                continue
            if data.get("error"):
                err = data["error"]
                yield StreamEvent(type="error", error=str(err.get("message") or err))
                return
            um = data.get("usageMetadata")
            if um:
                usage = Usage(
                    int(um.get("promptTokenCount") or 0),
                    int(um.get("candidatesTokenCount") or 0),
                    int(um.get("totalTokenCount") or 0),
                    int(um.get("cachedContentTokenCount") or 0),
                    int(um.get("thoughtsTokenCount") or 0),
                )
            for cand in data.get("candidates") or []:
                if not isinstance(cand, dict):
                    continue
                if cand.get("finishReason"):
                    finish = FINISH_MAP.get(str(cand["finishReason"]), cand["finishReason"])
                parts = (cand.get("content") or {}).get("parts") or []
                for idx, p in enumerate(parts):
                    if not isinstance(p, dict):
                        continue
                    if "text" in p:
                        if p.get("thought"):
                            yield StreamEvent(type="reasoning", text=str(p["text"]))
                        else:
                            yield StreamEvent(type="text", text=str(p["text"]))
                    fc = p.get("functionCall")
                    if isinstance(fc, dict) and fc.get("name"):
                        name = str(fc["name"])
                        if name in seen_call_names:
                            continue
                        seen_call_names.add(name)
                        args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                        call = ToolCall(
                            id=f"call_{len(calls)}",
                            name=name,
                            arguments=args,
                            raw_arguments=json.dumps(args, ensure_ascii=False),
                            index=idx,
                        )
                        calls.append(call)
                        yield StreamEvent(type="tool_call", tool_call=call)
        yield StreamEvent(type="usage", usage=usage)
        yield StreamEvent(type="done", finish_reason=finish or ("tool_calls" if calls else "stop"))

    async def list_models(self) -> list[str]:
        url = f"{self._base()}/models"
        try:
            resp = await self._request("GET", url, headers=self._headers())
            data = resp.json()
        except Exception:
            return []
        out = []
        for it in (data.get("models") if isinstance(data, dict) else None) or []:
            if isinstance(it, dict):
                name = str(it.get("name") or "")
                if name.startswith("models/"):
                    name = name[len("models/"):]
                if name:
                    out.append(name)
        return sorted(set(out))
