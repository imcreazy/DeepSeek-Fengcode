"""LLM 客户端抽象基类与通用逻辑（重试、超时、流式解析工具）。"""

from __future__ import annotations

import abc
import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config.schema import Provider
from .types import LLMResponse, Message, StreamEvent, ToolSpec, Usage

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Fengcode/1.0"
)

# 「连接被掐断」这一类错误：
# 复用空闲连接被上游/网关静默关闭时，httpx 通常抛 RemoteProtocolError /
# ReadError / WriteError（都属 TransportError）或超时。只在这类错误且**零产出**时重发一次。
_CONN_DROP_ERRORS = (httpx.TimeoutException, httpx.TransportError, httpx.RemoteProtocolError)


class LLMError(Exception):
    """LLM 调用错误，携带是否可重试信息。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - 展示用
        base = super().__str__()
        if self.status:
            return f"[HTTP {self.status}] {base}"
        return base


def is_retryable_status(status: int) -> bool:
    return status in (408, 409, 425, 429, 500, 502, 503, 504, 522, 524)


# ---- 分层网络诊断 --------------------------------------------------------
# ★ 为什么需要（实测痛点）：断线时界面只有一句「网络错误：ConnectError」，
#   分不清是「本机网络 / 上游网关 / 模型服务」哪一层断的，只能干等超时。
#   这里把异常按发生位置归到五层，每层给一句人话 + 下一步动作。
_LAYER_DNS = "dns"          # 域名解析失败：本机 DNS / 域名写错
_LAYER_REFUSED = "refused"  # 连接被拒：地址或端口不对、服务没起
_LAYER_CONNECT = "connect"  # 连不上（超时/不可达）：本机网络或上游网关
_LAYER_MIDWAY = "midway"    # 连上了但读到一半断开：网关掐长连接（最常见）
_LAYER_UPSTREAM = "upstream"  # 上游返回错误状态码：模型服务自己的问题


def diagnose_error(exc: BaseException, *, got_events: int = 0) -> tuple[str, str]:
    """把一次网络异常归层，返回 ``(层名, 给用户看的一句话)``。

    分层依据是**异常类型 + 是否已有产出**，而不是猜：
      · DNS 失败 → httpx.ConnectError 且消息里含 getaddrinfo/Name or service
      · 连接被拒 → ConnectError 含 Connection refused / 10061
      · 读到一半断 → 已经收过事件（got_events>0）后抛 TransportError/RemoteProtocolError
      · 连不上   → 其余 ConnectError / ConnectTimeout / ReadTimeout
      · 上游错误 → 由调用方按状态码自行判断（这里给兜底描述）
    """
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()

    if isinstance(exc, httpx.ConnectError):
        if "getaddrinfo" in low or "name or service" in low or "no address" in low:
            return _LAYER_DNS, (
                f"域名解析失败（{name}）：本机 DNS 或域名写错了。"
                "请检查设置里的 base_url，或在浏览器里打开该域名试试。"
            )
        if "refused" in low or "10061" in low:
            return _LAYER_REFUSED, (
                f"连接被拒绝（{name}）：地址或端口不对、或对端服务没启动。"
                "请核对该供应商的 base_url 与端口。"
            )
        return _LAYER_CONNECT, (
            f"连不上供应商（{name}）：本机网络或上游网关不通。"
            "检查本机能否访问外网，或换一个供应商试试。"
        )

    # ★ 超时必须排在 TransportError 之前判断：httpx.ConnectTimeout 同时是
    #   TimeoutException 与 TransportError 的子类，先走 TransportError 分支会被
    #   误判成「连接被掐断」（midway），而它其实是「压根没连上」（connect）。
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return _LAYER_CONNECT, (
            f"请求超时（{name}）：网络慢或上游长时间没响应。"
            "可稍后重试；若长期如此，考虑调大超时或换供应商。"
        )

    if isinstance(exc, httpx.TimeoutException):
        return _LAYER_CONNECT, (
            f"连接超时（{name}）：没能连上供应商（本机网络或上游网关不通）。"
            "检查本机能否访问外网，或换一个供应商试试。"
        )

    if isinstance(exc, (httpx.RemoteProtocolError, httpx.TransportError)):
        if got_events > 0:
            return _LAYER_MIDWAY, (
                f"读到一半连接被断开（{name}）：通常是上游网关掐掉了长连接。"
                "已收到的内容不会丢；重试一般能续上。"
            )
        return _LAYER_MIDWAY, (
            f"连接建立后立即中断（{name}）：大概率是网关/代理的问题。"
            "可重试一次；持续出现请检查代理设置。"
        )

    if isinstance(exc, httpx.TimeoutException):
        return _LAYER_CONNECT, (
            f"网络超时（{name}）：没能在限定时间内拿到响应。请稍后重试。"
        )

    return _LAYER_UPSTREAM, f"上游调用出错（{name}）：{msg[:200]}"


class BaseLLMClient(abc.ABC):
    """所有供应商客户端的共同基类。"""

    kind = "base"

    def __init__(self, provider: Provider, api_key: str | None = None,
                 *, client: httpx.AsyncClient | None = None) -> None:
        self.provider = provider
        self.api_key = api_key
        self._client = client
        self._own_client = client is None

    # ---- HTTP ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.provider.timeout_seconds, connect=30.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            )
        return self._client

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        client = await self._get_client()
        h = dict(self._headers())
        if headers:
            h.update(headers)
        attempts = max(1, int(self.provider.max_retries) + 1)
        last: Exception | None = None
        for i in range(attempts):
            try:
                req = client.build_request(method, url, json=json_body, headers=h)
                if stream:
                    resp = await client.send(req, stream=True)
                else:
                    resp = await client.send(req)
                if resp.status_code >= 400:
                    body = ""
                    try:
                        if stream:
                            chunks = []
                            async for c in resp.aiter_bytes():
                                chunks.append(c)
                                if sum(len(x) for x in chunks) > 8192:
                                    break
                            body = b"".join(chunks).decode("utf-8", "replace")
                        else:
                            body = resp.text[:8192]
                    except Exception:
                        body = ""
                    if not stream:
                        pass
                    raise LLMError(
                        f"{resp.status_code} {_short_reason(body)}",
                        status=resp.status_code,
                        retryable=is_retryable_status(resp.status_code),
                        body=body,
                    )
                return resp
            except LLMError as e:
                last = e
                if not e.retryable or i == attempts - 1:
                    raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as e:
                last = e
                if i == attempts - 1:
                    # ★ 分层诊断：把「哪个环节断了」说清楚，而不是甩一句
                    #   「网络错误：ConnectError」（实际使用中就是分不清层次）。
                    layer, hint = diagnose_error(e)
                    raise LLMError(hint, retryable=True) from e
            # 指数退避 + 抖动
            delay = min(8.0, (1.5**i)) + random.uniform(0, 0.6)
            await asyncio.sleep(delay)
        raise LLMError(f"请求失败：{last}")

    # ---- 抽象接口 ------------------------------------------------------

    @abc.abstractmethod
    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        """返回 ``(url, payload, extra_headers)``。"""

    @abc.abstractmethod
    def parse_response(self, data: dict[str, Any], model: str) -> LLMResponse:
        """解析非流式响应。"""

    @abc.abstractmethod
    async def parse_stream(self, resp: httpx.Response, model: str) -> AsyncIterator[StreamEvent]:
        """解析流式响应。"""

    # ---- 公共调用入口 --------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        url, payload, headers = self.build_payload(
            messages, model=model, tools=tools, stream=False, **kwargs
        )
        resp = await self._request("POST", url, json_body=payload, headers=headers)
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"响应不是合法 JSON：{resp.text[:500]}") from e
        out = self.parse_response(data, model)
        out.provider = self.provider.name
        return out

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        url, payload, headers = self.build_payload(
            messages, model=model, tools=tools, stream=True, **kwargs
        )
        # ★ 整体时长上限。
        #   为什么必须有：`httpx.Timeout(provider.timeout_seconds)` 对**流式**是
        #   「块间超时」——只要每个块都在超时内到达，整条流可以无限跑下去。
        #   实测单次调用跑过 651 秒（点停止像没反应、看起来像卡死）。
        #   这里按「整条流的总时长」兜底；超时抛错，但**已 yield 的事件都已送达**，
        #   调用方（agent._stream_once）会保留这些片段，不会白跑。
        budget = self.stream_total_timeout()
        # ★ 1-B：空闲连接被上游/网关掐断后**换新连接重发一次**。
        #   只在「一个新事件都没收到」时重发 ——
        #   已经有内容产出还重发，会造成重复输出与重复计费，宁可让用户看到报错。
        for attempt in range(2):
            got = 0
            resp = None
            try:
                resp = await self._request(
                    "POST", url, json_body=payload, headers=headers, stream=True
                )
                agen = self.parse_stream(resp, model)
                deadline = time.monotonic() + budget
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LLMError(
                            f"流式整体超时（超过 {budget:.0f} 秒未结束）", retryable=False
                        )
                    try:
                        ev = await asyncio.wait_for(agen.__anext__(), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        # 单次等待（= 剩余总预算）耗尽，同样按整体超时处理。
                        raise LLMError(
                            f"流式整体超时（超过 {budget:.0f} 秒未结束）", retryable=False
                        ) from None
                    got += 1
                    yield ev
                return
            except _CONN_DROP_ERRORS as e:
                if got > 0 or attempt == 1:
                    # ★ 分层诊断：已经收过事件后断开 = 读到一半被掐（最常见），
                    #   一个事件都没收到就断 = 连接建立后立即中断。两者提示不同。
                    _layer, hint = diagnose_error(e, got_events=got)
                    raise LLMError(hint, retryable=True) from e
                # 零产出 → 认定是复用空闲连接被掐断，丢弃连接池换新连接再试一次
                await self.drop_connection()
                await asyncio.sleep(0.4)
                continue
            finally:
                if resp is not None:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass

    async def drop_connection(self) -> None:
        """丢弃连接池，强制下次请求新建连接。

        用于「复用空闲连接被掐断」的场景：httpx 的连接池会保留半死连接，
        不重建的话重试很可能又用上同一条坏连接。
        """
        client = self._client
        self._client = None
        if client is not None and self._own_client:
            try:
                await client.aclose()
            except Exception:
                pass

    def stream_total_timeout(self) -> float:
        """整条流的总时长上限（秒）。

        读取顺序：供应商级 extra 覆盖 → LLMConfig.stream_total_timeout → 600 秒兜底。
        ★ 默认给 600 秒而不是 300：实测过 651 秒的长思考，压到 300 会把正常
        的长推理误杀；600 秒既能拦住真正的「卡死」，又不会打断长思考。
        """
        override = None
        try:
            extra = getattr(self.provider, "extra", None) or {}
            if isinstance(extra, dict) and extra.get("stream_total_timeout"):
                override = float(extra["stream_total_timeout"])
        except Exception:
            override = None
        if override and override > 0:
            return override
        try:
            from ..config import get_config

            v = float(getattr(get_config().llm, "stream_total_timeout", 0) or 0)
            if v > 0:
                return v
        except Exception:
            pass
        return 600.0

    async def list_models(self) -> list[str]:
        """尝试拉取模型列表；不支持则返回空列表。"""
        return []

    async def check_balance(self) -> dict[str, Any] | None:
        """查询余额（可选）。"""
        url = self.provider.balance_url
        if not url:
            return None
        try:
            headers = self._headers()
            # DeepSeek 用 Bearer
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            resp = await self._request("GET", url, headers=headers)
            return resp.json()
        except Exception as e:
            return {"error": str(e)}


# ---- SSE 工具 ------------------------------------------------------------

async def iter_sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    """逐行产出 SSE 的 ``data:`` 内容（已去掉前缀）。"""
    buf = ""
    async for chunk in resp.aiter_text():
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip("\r")
            if not line:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                yield line[5:].strip()
            else:
                # 有些中转站不带 data: 前缀
                yield line
    if buf.strip():
        rest = buf.strip()
        if rest.startswith("data:"):
            rest = rest[5:].strip()
        yield rest


def sse_json(line: str) -> dict[str, Any] | None:
    if not line or line == "[DONE]":
        return None
    try:
        v = json.loads(line)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def _short_reason(body: str) -> str:
    """从错误 body 里提取一句可读的中文原因。"""
    if not body:
        return ""
    try:
        d = json.loads(body)
    except ValueError:
        return body[:300].replace("\n", " ")
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or ""
            code = err.get("code") or err.get("type") or ""
            return f"{code} {msg}".strip()[:300] if code else str(msg)[:300]
        if isinstance(err, str):
            return err[:300]
        for k in ("message", "msg", "detail", "error_msg", "reason"):
            if d.get(k):
                return str(d[k])[:300]
    return body[:300].replace("\n", " ")


def merge_tool_call_delta(acc: dict[int, dict], deltas: list[dict]) -> list[StreamEvent]:
    """把流式 tool_call 增量合并进累积器，返回需要抛出的事件。"""
    events: list[StreamEvent] = []
    from .types import ToolCall

    for d in deltas or []:
        idx = d.get("index", 0) or 0
        slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})
        announced = slot.get("_announced", False)
        if d.get("id"):
            slot["id"] = d["id"]
        fn = d.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        args = fn.get("arguments") or ""
        if args:
            slot["args"] += args
        # 事件触发条件：收到参数增量 **或** 这是该工具的第一次上报。
        # 旧实现只在 ``arguments`` 非空时才发事件。但模型/中转常把 function.name
        # 与 arguments 分在不同 chunk 到达，首个 chunk 只带 name —— 那段空窗里
        # 前端收不到任何事件，界面停在「思考完成」后毫无动静，直到参数真正开始
        # 流才突然冒出卡片（实测「思考完成后等了十几秒才出现 write_file 提示」）。
        # 现在只要工具名一出现就先发一条（text 为空），让界面立刻显示「正在生成参数」。
        if args or (slot["name"] and not announced):
            slot["_announced"] = True
            events.append(
                StreamEvent(type="tool_delta", text=args,
                            meta={"index": idx, "name": slot["name"]})
            )
    return events


def finalize_tool_calls(acc: dict[int, dict]) -> list[ToolCall]:
    from .types import ToolCall

    out: list[ToolCall] = []
    for idx in sorted(acc.keys()):
        slot = acc[idx]
        if not slot.get("name"):
            continue
        out.append(
            ToolCall(
                id=slot.get("id") or f"call_{idx}",
                name=slot["name"],
                raw_arguments=slot.get("args") or "",
                arguments=ToolCall.parse_arguments(slot.get("args") or ""),
                index=idx,
            )
        )
    return out


def parse_usage_openai(u: dict[str, Any] | None) -> Usage:
    if not isinstance(u, dict):
        return Usage()
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    total = int(u.get("total_tokens") or (prompt + completion))
    details = u.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0)
    cdetails = u.get("completion_tokens_details") or {}
    reasoning = int(cdetails.get("reasoning_tokens") or 0)
    # ★ 未命中量：优先取上游明确给出的字段；上游没给就按「输入 - 命中」推算。
    #   为什么要留一个推算的兜底：多数 OpenAI 兼容端点只回 prompt_tokens 与
    #   命中量，不回未命中量。缺了它命中率就只能拿 prompt 当分母 —— 而 prompt
    #   在部分供应商那里本身就等于「命中 + 未命中」，在另一些那里只算未命中，
    #   口径不一。这里统一成「真计过价的输入」，命中率才可比。
    miss_raw = (
        details.get("cache_miss_tokens")
        or u.get("prompt_cache_miss_tokens")
        or u.get("cache_miss_tokens")
    )
    if miss_raw is not None:
        cache_miss = int(miss_raw)
    else:
        cache_miss = max(0, prompt - cached)
    return Usage(prompt, completion, total, cached, reasoning, cache_miss)


__all__ = [
    "BaseLLMClient",
    "LLMError",
    "is_retryable_status",
    "iter_sse_lines",
    "sse_json",
    "merge_tool_call_delta",
    "finalize_tool_calls",
    "parse_usage_openai",
    "DEFAULT_USER_AGENT",
]
