"""内置浏览器操控工具：让 AI 直接驱动桌面端的内置浏览器。

为什么要这么绕一层
------------------
内置浏览器用的是 Electron 的 ``WebContentsView``，它活在**桌面外壳的渲染进程**里；
而 AI 工具跑在**后端 Python 进程**里。两者之间没有直接调用关系，因此用一条
「指令队列」把它们接起来：

    AI 工具 → 后端队列（HTTP）→ 主进程轮询取出 → 执行 → 回填结果 → 工具拿到内容

好处是不依赖界面当前开着哪个标签、也不要求渲染进程在渲染页面上做任何事；
主进程是这条链路上唯一真正的执行者，安全边界（协议白名单）也仍然落在它那里。

网页版（在系统浏览器里打开 Fengcode）没有桌面外壳，队列永远不会被取走，
此时工具会明确告诉模型「当前环境没有内置浏览器」，而不是一直等到超时。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from ..base import Tool, ToolContext, ToolResult

# --------------------------------------------------------------------------
# 指令队列（进程内单例）
# --------------------------------------------------------------------------


class BrowserBridge:
    """后端与桌面主进程之间的浏览器指令队列。

    单条指令的生命周期：
      ``pending`` → 主进程取出（``running``）→ 回填 ``done`` / ``error``。
    超时未回填即判定为「没有可用的内置浏览器」。
    """

    def __init__(self, *, timeout: float = 45.0) -> None:
        self._lock = asyncio.Lock()
        self._pending: list[dict[str, Any]] = []
        self._results: dict[str, dict[str, Any]] = {}
        self._timeout = timeout
        # 主进程最近一次来取指令的时刻。用来判断「桌面端在不在」——
        # 网页版永远不会有这个心跳，于是工具能立刻给出准确提示而不是干等。
        self.last_poll: float = 0.0
        self.last_poll_ok: float = 0.0

    # ---- 主进程侧 ----------------------------------------------------

    async def take(self) -> dict[str, Any] | None:
        """主进程取一条待执行指令（没有则返回 None）。"""
        self.last_poll = time.time()
        async with self._lock:
            if not self._pending:
                return None
            return self._pending.pop(0)

    async def settle(self, cmd_id: str, payload: dict[str, Any]) -> bool:
        """主进程回填执行结果。"""
        self.last_poll = time.time()
        self.last_poll_ok = time.time()
        async with self._lock:
            self._results[cmd_id] = payload
        return True

    def desktop_alive(self, *, window: float = 6.0) -> bool:
        """桌面外壳最近是否来取过指令。"""
        return (time.time() - self.last_poll_ok) < window

    # ---- 工具侧 ------------------------------------------------------

    async def submit(self, action: str, **params: Any) -> dict[str, Any]:
        """提交一条指令并等待结果。"""
        cmd_id = uuid.uuid4().hex[:16]
        cmd = {"id": cmd_id, "action": action, "params": params}
        async with self._lock:
            self._pending.append(cmd)
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            async with self._lock:
                got = self._results.pop(cmd_id, None)
            if got is not None:
                return got
            await asyncio.sleep(0.15)
        # 超时：把还没被取走的指令撤掉，避免桌面端稍后连上来又执行一条过时指令。
        async with self._lock:
            self._pending = [c for c in self._pending if c["id"] != cmd_id]
        return {
            "ok": False,
            "error": "内置浏览器没有响应。桌面端未运行、或未打开内置浏览器面板。"
                     "如果当前是在系统浏览器里使用网页版，请改用 web_fetch 抓取网页。",
            "code": "no_desktop",
        }


_BRIDGE: BrowserBridge | None = None


def get_browser_bridge() -> BrowserBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = BrowserBridge()
    return _BRIDGE


# --------------------------------------------------------------------------
# 地址归一化（与桌面端保持同一套规则，避免两边行为不一致）
# --------------------------------------------------------------------------

_SEARCH_PREFIX = "https://cn.bing.com/search?q="


def normalize_url(raw: str) -> tuple[str, str]:
    """返回 ``(最终地址, 说明)``。

    规则与桌面端一致：
      · 不写协议 → 按 https；
      · localhost / 127.0.0.1 → 按 http；
      · 明显的整句（含空格或没有点号）→ 当作搜索词。
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    if s.startswith(("http://", "https://")):
        return s, ""
    if s.startswith(("file://", "javascript:", "data:")):
        # 协议白名单只放 http / https，这里先挡住并说明，避免到了主进程才报错
        return "", f"不支持 {s.split(':', 1)[0]}:// 协议，内置浏览器只允许 http / https"
    # ★ 本地地址要先于「搜索词」判断：localhost:8080 / 127.0.0.1 这类既没有点号
    #   也不含空格的写法，会被「看着不像域名就当搜索词」的规则误吞成一次搜索。
    if s.startswith(("localhost", "127.0.0.1", "[::1]")):
        return "http://" + s, ""
    if " " in s or ("." not in s and "/" not in s):
        import urllib.parse

        return _SEARCH_PREFIX + urllib.parse.quote(s), f"按搜索词处理：{s}"
    return "https://" + s, ""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


class BrowserOpenTool(Tool):
    name = "browser_open"
    description = (
        "在内置浏览器里打开一个网址（或搜索词）。内置浏览器是应用右侧栏里的真实浏览器，"
        "支持 http/https，也支持 localhost/127.0.0.1 上的本地服务。"
        "不写协议时按 https 处理；传一句自然语言会当成搜索词。"
        "打开后可用 browser_read 读正文、browser_screenshot 看画面。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "网址或搜索词"},
            "wait_ms": {
                "type": "integer",
                "description": "打开后额外等待的毫秒数（等页面渲染完再读，默认 1200）",
            },
        },
        "required": ["url"],
    }
    dangerous = False

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raw = str(kwargs.get("url") or "")
        url, note = normalize_url(raw)
        if not url:
            return ToolResult.fail(note or "网址不能为空")
        wait_ms = int(kwargs.get("wait_ms") or 1200)
        bridge = get_browser_bridge()
        res = await bridge.submit("open", url=url, wait_ms=wait_ms)
        if not res.get("ok"):
            return ToolResult.fail(str(res.get("error") or "打开失败"))
        title = res.get("title") or ""
        final = res.get("url") or url
        head = f"已在内置浏览器打开：{final}"
        if title:
            head += f"\n页面标题：{title}"
        if note:
            head += f"\n（{note}）"
        return ToolResult(
            content=head,
            display=f"打开 {final[:80]}",
            data={"url": final, "title": title},
        )


class BrowserReadTool(Tool):
    name = "browser_read"
    description = (
        "读取内置浏览器当前页面的正文（可见文本），用于阅读网页内容。"
        "这是真实渲染后的页面，登录态、脚本渲染的内容都能读到。"
        "读之前建议先用 browser_open 打开目标页。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "最多返回多少字符（默认 12000，上限 60000）",
            },
        },
        "required": [],
    }
    dangerous = False

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        n = int(kwargs.get("max_chars") or 12000)
        n = max(500, min(n, 60000))
        bridge = get_browser_bridge()
        res = await bridge.submit("read", max_chars=n)
        if not res.get("ok"):
            return ToolResult.fail(str(res.get("error") or "读取失败"))
        text = str(res.get("text") or "").strip()
        title = res.get("title") or ""
        url = res.get("url") or ""
        if not text:
            return ToolResult.fail(
                "当前页没有可读正文（可能是纯图片页、需要登录，或页面还没渲染完）。"
                "可以先用 browser_screenshot 看画面。"
            )
        body = f"{title}\n{url}\n\n{text}" if title or url else text
        if res.get("truncated"):
            body += "\n\n（正文过长，已截断）"
        return ToolResult(
            content=body,
            display=f"读取 {url[:60]}（{len(text)} 字）",
            data={"url": url, "title": title, "chars": len(text)},
        )


class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = (
        "截取内置浏览器当前页面的画面，并把图片交给你自己查看。"
        "适合判断页面排版、验证自己做的网页效果、读取以图片呈现的内容。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "save_path": {
                "type": "string",
                "description": "可选：把截图另存到该路径（相对路径按工作区解析）",
            },
        },
        "required": [],
    }
    dangerous = False

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        bridge = get_browser_bridge()
        res = await bridge.submit("screenshot")
        if not res.get("ok"):
            return ToolResult.fail(str(res.get("error") or "截图失败"))
        data_url = str(res.get("dataUrl") or "")
        title = res.get("title") or ""
        url = res.get("url") or ""
        if not data_url.startswith("data:image"):
            return ToolResult.fail("截图没有返回有效图片")
        # ★ 图片要通过 attachments 交给模型（走已有的多模态链路），
        #   只塞一段 base64 文本模型是「看不见」的。
        from ...llm.types import Attachment

        import base64

        raw_b64 = data_url.split(",", 1)[1] if "," in data_url else ""
        att = Attachment(
            kind="image",
            name=f"内置浏览器截图-{time.strftime('%H%M%S')}.png",
            mime="image/png",
            data=data_url,
        )
        saved: list[str] = []
        save_path = str(kwargs.get("save_path") or "").strip()
        if save_path:
            try:
                from ...security.paths import resolve as _resolve

                target = _resolve(save_path, ctx.workspace, must_exist=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(raw_b64))
                saved.append(str(target))
            except Exception as e:
                return ToolResult.fail(f"保存截图失败：{type(e).__name__}: {e}")
        where = url or "当前页面"
        note = f"已截取内置浏览器画面（{where}"
        note += f" · {title}）" if title else "）"
        if saved:
            note += f"\n已保存到：{saved[0]}"
        return ToolResult(
            content=note,
            display=f"截图 {where[:60]}",
            files=saved,
            attachments=[att],
            data={"url": url, "title": title, "bytes": len(raw_b64)},
        )


class BrowserActionTool(Tool):
    name = "browser_action"
    description = (
        "控制内置浏览器：back（后退）、forward（前进）、reload（刷新）、"
        "status（看当前地址与标题）、close（关闭页面）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["back", "forward", "reload", "status", "close"],
                "description": "要执行的动作",
            },
        },
        "required": ["action"],
    }
    dangerous = False

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        act = str(kwargs.get("action") or "").strip()
        if act not in ("back", "forward", "reload", "status", "close"):
            return ToolResult.fail(f"不支持的动作：{act}")
        bridge = get_browser_bridge()
        res = await bridge.submit("action", name=act)
        if not res.get("ok"):
            return ToolResult.fail(str(res.get("error") or "操作失败"))
        if act == "status":
            url = res.get("url") or "（空白页）"
            title = res.get("title") or ""
            text = f"当前页：{url}" + (f"\n标题：{title}" if title else "")
            return ToolResult(content=text, display=f"状态 {url[:50]}",
                              data={"url": url, "title": title})
        names = {"back": "后退", "forward": "前进", "reload": "刷新", "close": "关闭页面"}
        return ToolResult(content=f"已{names.get(act, act)}", display=f"浏览器{names.get(act, act)}")


TOOLS = [BrowserOpenTool, BrowserReadTool, BrowserScreenshotTool, BrowserActionTool]
