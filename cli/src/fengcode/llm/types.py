"""统一的 LLM 消息与工具类型定义。

设计目标：用一个中间表示（IR）屏蔽 OpenAI / Anthropic / Gemini
三家的差异，上层编排器只跟这里打交道。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]

#: 出现在对话中间的 system 提示在发送前的包裹标签。
REMINDER_TAG = "system-reminder"


def normalize_roles(messages: list["Message"]) -> list["Message"]:
    """规范化消息角色序列，满足 OpenAI 兼容接口的硬约束。

    真实上游（OpenAI / DeepSeek / 万象等）要求 ``system`` 只能出现在**开头**；
    一旦对话中间混入 ``role="system"``，多数网关会直接返回 400
    （``bad_response_status_code`` / ``openai_error``）。

    但为了 prompt cache 命中率，动态提示（当前时间、自检清单、反思建议）
    又必须放在**尾部**而不是系统提示里。两者兼顾的做法：

    - 开头的多条 ``system`` 合并为一条，保持前缀逐字节稳定 → 缓存友好；
    - 中间/尾部的 ``system`` 改写成 ``user``，并用 ``<system-reminder>`` 包裹，
      语义不变、模型可识别（Claude Code 同款写法），但不会触发 400。
    """
    out: list[Message] = []
    seen_non_system = False
    for m in messages:
        if m.role == "system":
            if not seen_non_system:
                if out and out[0].role == "system":
                    merged = ((out[0].content or "") + "\n\n" + (m.content or "")).strip()
                    out[0] = replace(out[0], content=merged)
                else:
                    out.append(m)
                continue
            note = m.content or ""
            if f"<{REMINDER_TAG}>" not in note:
                note = f"<{REMINDER_TAG}>\n{note}\n</{REMINDER_TAG}>"
            out.append(replace(m, role="user", content=note))
            continue
        seen_non_system = True
        out.append(m)
    return out


@dataclass
class ToolSpec:
    """一个可被模型调用的工具声明。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    # 来源标记：builtin / mcp:xxx / plugin:xxx / skill:xxx
    source: str = "builtin"
    # 是否危险（需要审批）
    dangerous: bool = False
    # 展示用分组
    group: str = ""

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_gemini(self) -> dict[str, Any]:
        params = _gemini_schema(self.parameters)
        return {"name": self.name, "description": self.description, "parameters": params}


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """把 JSON Schema 转成 Gemini 的 OpenAPI 子集（去掉不支持的字段）。"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out: dict[str, Any] = {}
    t = schema.get("type")
    if t:
        out["type"] = {"object": "OBJECT", "string": "STRING", "number": "NUMBER",
                       "integer": "INTEGER", "boolean": "BOOLEAN", "array": "ARRAY"}.get(t, str(t).upper())
    if schema.get("description"):
        out["description"] = schema["description"]
    props = schema.get("properties")
    if isinstance(props, dict):
        out["properties"] = {k: _gemini_schema(v) for k, v in props.items()}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    items = schema.get("items")
    if isinstance(items, dict):
        out["items"] = _gemini_schema(items)
    if schema.get("enum"):
        out["enum"] = [str(x) for x in schema["enum"]]
    fmt = schema.get("format")
    if fmt in ("date-time", "date", "int32", "int64", "float", "double", "byte", "enum"):
        out["format"] = fmt
    return out


@dataclass
class ToolCall:
    """模型请求调用某个工具。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    index: int = 0
    # 流式拼接用
    _partial: str = ""

    @staticmethod
    def parse_arguments(raw: str) -> dict[str, Any]:
        """宽松解析模型给出的参数 JSON（容忍代码块、尾逗号、单引号）。"""
        if not raw or not raw.strip():
            return {}
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:]
            s = s.strip()
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {"value": v}
        except ValueError:
            pass
        # 常见修复：去掉尾随逗号
        fixed = _strip_trailing_commas(s)
        try:
            v = json.loads(fixed)
            return v if isinstance(v, dict) else {"value": v}
        except ValueError:
            pass
        # 再不行：尝试从首个 { 到末个 } 截取
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                v = json.loads(_strip_trailing_commas(s[i : j + 1]))
                return v if isinstance(v, dict) else {"value": v}
            except ValueError:
                pass
        return {"_raw": raw}

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False),
            },
        }


def _strip_trailing_commas(s: str) -> str:
    out = []
    in_str = False
    esc = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
                out.append(c)
            elif c == ",":
                j = i + 1
                while j < len(s) and s[j] in " \t\r\n":
                    j += 1
                if j < len(s) and s[j] in "}]":
                    i += 1
                    continue
                out.append(c)
            else:
                out.append(c)
        i += 1
    return "".join(out)


@dataclass
class Attachment:
    """多模态附件（图片/音频/文件）。"""

    kind: Literal["image", "audio", "file"] = "image"
    path: str | None = None
    url: str | None = None
    data: bytes | None = None
    mime: str = "image/png"
    name: str = ""

    def base64(self) -> str:
        import base64

        if self.data is not None:
            return base64.b64encode(self.data).decode("ascii")
        if self.path:
            from pathlib import Path

            return base64.b64encode(Path(self.path).read_bytes()).decode("ascii")
        return ""


@dataclass
class Message:
    """统一消息体。"""

    role: Role
    content: str = ""
    name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    # 推理过程（思维链）与附件
    reasoning: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    # 是否被压缩折叠（仅用于展示）
    meta: dict[str, Any] = field(default_factory=dict)

    # ---- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.reasoning:
            d["reasoning"] = self.reasoning
        if self.attachments:
            d["attachments"] = [
                {"kind": a.kind, "path": a.path, "url": a.url, "mime": a.mime, "name": a.name}
                for a in self.attachments
            ]
        if self.meta:
            d["meta"] = self.meta
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Message":
        tcs = []
        for tc in d.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tcs.append(
                ToolCall(
                    id=tc.get("id") or "",
                    name=fn.get("name") or tc.get("name") or "",
                    raw_arguments=fn.get("arguments") or "",
                    arguments=ToolCall.parse_arguments(fn.get("arguments") or ""),
                )
            )
        atts = []
        for a in d.get("attachments") or []:
            atts.append(
                Attachment(
                    kind=a.get("kind", "image"),
                    path=a.get("path"),
                    url=a.get("url"),
                    mime=a.get("mime", "image/png"),
                    name=a.get("name", ""),
                )
            )
        return Message(
            role=d.get("role", "user"),
            content=d.get("content") or "",
            name=d.get("name"),
            tool_calls=tcs,
            tool_call_id=d.get("tool_call_id"),
            tool_name=d.get("tool_name"),
            reasoning=d.get("reasoning") or "",
            attachments=atts,
            meta=d.get("meta") or {},
        )

    # ---- 便捷构造 -----------------------------------------------------
    @staticmethod
    def system(text: str) -> "Message":
        return Message(role="system", content=text)

    @staticmethod
    def user(text: str, attachments: list[Attachment] | None = None) -> "Message":
        return Message(role="user", content=text, attachments=attachments or [])

    @staticmethod
    def assistant(text: str = "", tool_calls: list[ToolCall] | None = None) -> "Message":
        return Message(role="assistant", content=text, tool_calls=tool_calls or [])

    @staticmethod
    def tool_result(call_id: str, name: str, text: str) -> "Message":
        return Message(role="tool", content=text, tool_call_id=call_id, tool_name=name)


@dataclass
class Usage:
    """token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    # ★ 未命中缓存的输入量。
    #   为什么要单独留一个字段、而不是用 prompt - cached 反推：
    #   命中率的分母必须是「真正计过价的输入」（命中 + 未命中），而 prompt_tokens
    #   在不同供应商那里口径不一（有的含命中部分，有的另有字段）。
    #   分开记之后，命中率 = 命中 /（命中 + 未命中），口径可直接核对，
    #   也能算出「没命中的那部分多花了多少钱」。上游不返回时保持 0，
    #   由调用方按「未报即未知」处理，不硬凑。
    cache_miss_tokens: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
        }


@dataclass
class LLMResponse:
    """一次模型调用的完整结果。"""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    model: str = ""
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_message(self) -> Message:
        return Message(
            role="assistant",
            content=self.content,
            reasoning=self.reasoning,
            tool_calls=self.tool_calls,
            meta={"provider": self.provider, "model": self.model},
        )


# ---- 流式事件 ------------------------------------------------------------

@dataclass
class StreamEvent:
    """流式输出的增量事件。

    type:
      - ``text``        : content 增量
      - ``reasoning``   : 思维链增量
      - ``tool_call``   : 完整的一次工具调用（参数已拼完）
      - ``tool_delta``  : 工具调用参数增量（供界面显示"正在生成参数"）
      - ``usage``       : 用量
      - ``done``        : 结束
      - ``error``       : 错误
    """

    type: str
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # ★ 缓存前缀归因（仅 usage 事件会带）。
    #   用普通 dict 而非 CacheDiagnostics：types 层不该反过来依赖 router 层
    #   （router 已经 import 了 types，反向依赖会成环）。
    diagnostics: dict[str, Any] | None = None


__all__ = [
    "Role",
    "ToolSpec",
    "ToolCall",
    "Attachment",
    "Message",
    "Usage",
    "LLMResponse",
    "StreamEvent",
]
