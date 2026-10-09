"""网络工具：网页搜索、网页抓取、HTTP 请求、文件下载。

搜索后端支持多引擎（按配置顺序尝试，任一成功即返回）：
- ``ddgs``：DuckDuckGo（需 ``ddgs`` 包，优先，免密钥）
- ``bing``：解析 Bing 搜索页（免密钥，中文效果尚可）
- ``searxng``：自建/公共 SearXNG 实例（免密钥，可配置）
- ``zhipu``：智谱 Web Search API（需密钥，中文效果最好）

抓取默认用 httpx 拉取并转纯文本；``render=true`` 时尝试用 Playwright 渲染（可选依赖）。
"""

from __future__ import annotations

import asyncio
import gzip
import html as htmllib
import io
import json
import os
import re
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

import httpx

from ...security.paths import resolve
from ...utils import human_size, sanitize_filename, truncate_middle, uniq_filename
from ..base import Tool, ToolContext, ToolResult

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _client(timeout: float = 25.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15.0),
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )


# --------------------------------------------------------------------------
# HTML → 文本
# --------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_BLOCK_RE = re.compile(r"(?i)</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|pre)[^>]*>")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_MULTINL_RE = re.compile(r"\n{3,}")


def html_to_text(html: str, *, keep_links: bool = False) -> str:
    """把 HTML 转成可读纯文本（不依赖 bs4）。"""
    if not html:
        return ""
    s = _SCRIPT_RE.sub(" ", html)
    if keep_links:
        s = re.sub(r'(?is)<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", s)
    s = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = htmllib.unescape(s)
    s = _SPACE_RE.sub(" ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    lines = [ln for ln in lines if ln]
    return _MULTINL_RE.sub("\n\n", "\n".join(lines)).strip()


def extract_title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return htmllib.unescape(m.group(1)).strip() if m else ""


def extract_meta_description(html: str) -> str:
    m = re.search(
        r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html or ""
    ) or re.search(
        r'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']', html or ""
    )
    return htmllib.unescape(m.group(1)).strip() if m else ""


# --------------------------------------------------------------------------
# 搜索后端
# --------------------------------------------------------------------------

async def search_ddgs(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore
            except ImportError:
                return []
        out = []
        try:
            with DDGS(timeout=timeout) as d:
                for it in d.text(query, max_results=limit, region="cn-zh"):
                    out.append(
                        {
                            "title": it.get("title", ""),
                            "url": it.get("href") or it.get("url", ""),
                            "snippet": it.get("body") or it.get("snippet", ""),
                            "engine": "ddgs",
                        }
                    )
        except Exception:
            return []
        return out

    return await asyncio.to_thread(_run)


async def search_bing(query: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    url = "https://www.bing.com/search"
    params = {"q": query, "setlang": "zh-CN", "count": str(limit + 5), "form": "QBLH"}
    try:
        async with _client(timeout) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            html = r.text
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for block in re.findall(r'(?is)<li class="b_algo".*?</li>', html):
        m = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        link = htmllib.unescape(m.group(1))
        title = html_to_text(m.group(2))
        sm = re.search(r'(?is)<p[^>]*>(.*?)</p>', block)
        snippet = html_to_text(sm.group(1)) if sm else html_to_text(block)[:300]
        out.append({"title": title, "url": link, "snippet": snippet, "engine": "bing"})
        if len(out) >= limit:
            break
    if not out:
        # 兜底：从 HTML 里抓所有外链标题
        for m in re.finditer(r'(?is)<h2[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', html):
            out.append(
                {
                    "title": html_to_text(m.group(2)),
                    "url": htmllib.unescape(m.group(1)),
                    "snippet": "",
                    "engine": "bing",
                }
            )
            if len(out) >= limit:
                break
    return out


async def search_searxng(query: str, limit: int, timeout: float, base: str) -> list[dict[str, Any]]:
    try:
        async with _client(timeout) as c:
            r = await c.get(
                base.rstrip("/") + "/search",
                params={"q": query, "format": "json", "language": "zh-CN"},
            )
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception:
        return []
    out = []
    for it in (data.get("results") or [])[:limit]:
        out.append(
            {
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "snippet": it.get("content", ""),
                "engine": "searxng",
            }
        )
    return out


async def search_zhipu(query: str, limit: int, timeout: float, api_key: str) -> list[dict[str, Any]]:
    if not api_key:
        return []
    payload = {
        "search_engine": "search_std",
        "search_query": query,
        "count": min(50, max(1, limit)),
        "content_size": "medium",
    }
    try:
        async with _client(timeout) as c:
            r = await c.post(
                "https://open.bigmodel.cn/api/paas/v4/web_search",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception:
        return []
    out = []
    for it in (data.get("search_result") or [])[:limit]:
        out.append(
            {
                "title": it.get("title", ""),
                "url": it.get("link") or it.get("url", ""),
                "snippet": it.get("content") or it.get("snippet", ""),
                "published": it.get("publish_date", ""),
                "engine": "zhipu",
            }
        )
    return out


class WebSearchTool(Tool):
    name = "web_search"
    group = "网络"
    read_only = True
    description = (
        "联网搜索网页，返回标题、链接与摘要。支持多引擎（DuckDuckGo/Bing/SearXNG/智谱），"
        "按配置顺序自动尝试。搜索中文内容时建议在关键词里加上限定词（如“文档”“教程”）。"
    )
    parameters = {
        "query": {"type": "string", "description": "搜索关键词"},
        "max_results": {"type": "integer", "description": "最多返回条数，默认 8"},
        "engine": {
            "type": "string",
            "description": "指定引擎（ddgs/bing/searxng/zhipu/auto），默认 auto",
        },
        "freshness": {"type": "string", "description": "时效偏好：day/week/month/year（部分引擎支持）"},
    }
    required = ["query"]

    async def run(self, ctx: ToolContext, query: str = "", max_results: int = 8, engine: str = "auto",
                  freshness: str = "", **_: Any) -> ToolResult:
        if not query.strip():
            return ToolResult.fail("query 不能为空")
        cfg = getattr(ctx.config, "web_search", None)
        limit = int(max_results or (getattr(cfg, "max_results", 8) or 8))
        timeout = float(getattr(cfg, "timeout_seconds", 25) or 25)
        engines = [engine.lower()] if engine and engine.lower() != "auto" else list(
            getattr(cfg, "engines", None) or ["ddgs", "bing", "searxng", "zhipu"]
        )
        zhipu_key = ""
        if cfg is not None:
            zhipu_key = getattr(cfg, "zhipu_api_key", None) or ""
            if not zhipu_key and getattr(cfg, "zhipu_api_key_env", ""):
                zhipu_key = os.environ.get(cfg.zhipu_api_key_env, "")
                if not zhipu_key and ctx.llm is not None:
                    zhipu_key = ctx.llm.manager.env_get(cfg.zhipu_api_key_env) or ""

        results: list[dict[str, Any]] = []
        tried: list[str] = []
        for eng in engines:
            tried.append(eng)
            try:
                if eng == "ddgs":
                    results = await search_ddgs(query, limit, timeout)
                elif eng == "bing":
                    results = await search_bing(query, limit, timeout)
                elif eng == "searxng":
                    base = getattr(cfg, "searxng_url", "") or "https://searx.be"
                    results = await search_searxng(query, limit, timeout, base)
                elif eng == "zhipu":
                    results = await search_zhipu(query, limit, timeout, zhipu_key)
                else:
                    continue
            except Exception:
                results = []
            if results:
                break

        if not results:
            hint = ""
            if "zhipu" in engines and not zhipu_key:
                hint = "\n提示：智谱搜索需要 API Key，可在「设置 → 网络搜索」里填写。"
            return ToolResult.fail(f"搜索 “{query}” 没有结果（已尝试：{', '.join(tried)}）。{hint}")

        lines = [f"搜索 “{query}” 共 {len(results)} 条结果（引擎：{results[0].get('engine')}）：", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title') or '(无标题)'}")
            lines.append(f"   {r.get('url')}")
            sn = (r.get("snippet") or "").strip()
            if sn:
                lines.append(f"   {truncate_middle(sn, 400)}")
            lines.append("")
        content = "\n".join(lines)
        # ★ 1.5.0：「网页搜索」模型接到真实调用上（此前界面上可设、存进配置后从不被读）。
        #   配置了就让它把结果归纳成一段摘要，摆在原始结果前面。
        summary = ""
        try:
            from ..llm.types import Message as _Msg

            _sm = str(getattr(getattr(ctx.config, "agent", None), "search_model", "") or "").strip()
            if _sm and ctx.llm is not None:
                _resp = await ctx.llm.chat(
                    [
                        _Msg.system("把下面的搜索结果归纳成要点，保留关键事实与来源编号，不要编造。"),
                        _Msg.user(content[:12000]),
                    ],
                    model=_sm, max_tokens=800,
                )
                summary = (_resp.content or "").strip()
        except Exception:
            summary = ""
        if summary:
            content = f"结果摘要（由搜索模型归纳）：\n{summary}\n\n原始结果：\n{content}"
        return ToolResult(
            content=content,
            display=f"搜索 “{truncate_middle(query, 30)}” → {len(results)} 条",
            data={"results": results, "engine": results[0].get("engine"), "query": query},
        )


class WebFetchTool(Tool):
    name = "web_fetch"
    group = "网络"
    read_only = True
    description = (
        "抓取一个网页并转成纯文本（自动去脚本/样式，按需截断）。"
        "render=true 时尝试用浏览器渲染 JavaScript 页面（需要 playwright）。"
        "也支持直接抓取 JSON/纯文本接口。"
    )
    parameters = {
        "url": {"type": "string", "description": "要抓取的网址"},
        "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 20000"},
        "render": {"type": "boolean", "description": "是否用浏览器渲染（处理 JS 页面）"},
        "selector": {"type": "string", "description": "仅提取匹配该 CSS 选择器的内容（需 bs4）"},
        "raw": {"type": "boolean", "description": "是否返回原始 HTML 而不转文本"},
        "timeout": {"type": "number", "description": "超时秒数，默认 25"},
    }
    required = ["url"]

    async def run(self, ctx: ToolContext, url: str = "", max_chars: int = 20000, render: bool = False,
                  selector: str = "", raw: bool = False, timeout: float = 25, **_: Any) -> ToolResult:
        if not url:
            return ToolResult.fail("url 不能为空")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        text = ""
        title = ""
        used_render = False
        if render:
            text, title = await _render_page(url, timeout)
            used_render = bool(text)
        if not text:
            try:
                async with _client(timeout) as c:
                    r = await c.get(url, headers={"Accept": "text/html,application/json,*/*"})
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "")
                    body = r.text
            except httpx.HTTPStatusError as e:
                return ToolResult.fail(f"抓取失败：HTTP {e.response.status_code}（{url}）")
            except Exception as e:
                return ToolResult.fail(f"抓取失败：{type(e).__name__}: {e}（{url}）")
            if "json" in ctype.lower():
                try:
                    text = json.dumps(r.json(), ensure_ascii=False, indent=2)
                except Exception:
                    text = body
            elif "html" in ctype.lower() or body.lstrip()[:1] == "<":
                title = extract_title(body)
                desc = extract_meta_description(body)
                if selector:
                    picked = _select_text(body, selector)
                    body = picked or body
                text = body if raw else html_to_text(body)
                if desc and not raw:
                    text = f"（页面描述：{desc}）\n\n{text}"
            else:
                text = body

        limit = int(max_chars or 20000)
        truncated = len(text) > limit
        shown = truncate_middle(text, limit) if truncated else text
        head = f"来源：{url}"
        if title:
            head += f"\n标题：{title}"
        if used_render:
            head += "\n（已用浏览器渲染）"
        if truncated:
            head += f"\n（原文 {len(text)} 字符，已截断显示）"
        return ToolResult(
            content=f"{head}\n\n{shown}",
            display=f"抓取 {url[:60]}（{len(text)} 字符）",
            truncated=truncated,
            data={"url": url, "title": title, "length": len(text), "rendered": used_render},
        )


async def _render_page(url: str, timeout: float) -> tuple[str, str]:
    """尝试用 Playwright 渲染页面（可选依赖，失败返回空）。"""
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        return "", ""
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=UA, locale="zh-CN")
                await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                await page.wait_for_timeout(1200)
                html = await page.content()
                title = await page.title()
                return html_to_text(html), title
            finally:
                await browser.close()
    except Exception:
        return "", ""


def _select_text(html: str, selector: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(selector)
        if node is None:
            return ""
        return node.get_text("\n", strip=True)
    except Exception:
        return ""


class HttpRequestTool(Tool):
    name = "http_request"
    group = "网络"
    dangerous = True
    description = (
        "发送任意 HTTP 请求（GET/POST/PUT/PATCH/DELETE），可自定义头部、参数与 body，"
        "用于调用 API。返回状态码、响应头与响应体。"
    )
    parameters = {
        "url": {"type": "string", "description": "请求地址"},
        "method": {"type": "string", "description": "HTTP 方法，默认 GET"},
        "headers": {"type": "object", "description": "请求头键值对"},
        "params": {"type": "object", "description": "URL 查询参数"},
        "json_body": {"type": "object", "description": "JSON 请求体"},
        "data": {"type": "string", "description": "原始字符串请求体"},
        "timeout": {"type": "number", "description": "超时秒数，默认 30"},
        "max_chars": {"type": "integer", "description": "响应体最多返回字符数，默认 20000"},
    }
    required = ["url"]

    async def run(self, ctx: ToolContext, url: str = "", method: str = "GET",
                  headers: dict | None = None, params: dict | None = None, json_body: Any = None,
                  data: str = "", timeout: float = 30, max_chars: int = 20000, **_: Any) -> ToolResult:
        if not url:
            return ToolResult.fail("url 不能为空")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        m = (method or "GET").upper()
        h = {"User-Agent": UA}
        h.update(headers or {})
        try:
            async with _client(timeout) as c:
                r = await c.request(
                    m, url, headers=h, params=params,
                    json=json_body if json_body is not None else None,
                    content=data.encode("utf-8") if (data and json_body is None) else None,
                )
        except Exception as e:
            return ToolResult.fail(f"请求失败：{type(e).__name__}: {e}")
        body = r.text
        ctype = r.headers.get("content-type", "")
        if "json" in ctype.lower():
            try:
                body = json.dumps(r.json(), ensure_ascii=False, indent=2)
            except Exception:
                pass
        limit = int(max_chars or 20000)
        truncated = len(body) > limit
        shown = body[:limit] + (f"\n…（响应体 {len(body)} 字符，已截断）" if truncated else "")
        head = f"{m} {url}\n状态码：{r.status_code}\n耗时：{r.elapsed.total_seconds():.2f}s\n"
        head += "响应头：\n" + "\n".join(f"  {k}: {v}" for k, v in list(r.headers.items())[:20])
        ok = 200 <= r.status_code < 400
        return ToolResult(
            ok=ok,
            content=f"{head}\n\n响应体：\n{shown}",
            error=None if ok else f"HTTP {r.status_code}",
            display=f"{m} {url[:60]} → {r.status_code}",
            truncated=truncated,
            data={"status": r.status_code, "headers": dict(r.headers), "body": body[:limit]},
        )


class DownloadFileTool(Tool):
    name = "download_file"
    group = "网络"
    dangerous = True
    description = "下载文件到工作区（或指定目录），支持断点续传式重试与大小限制。"
    parameters = {
        "url": {"type": "string", "description": "下载地址"},
        "path": {"type": "string", "description": "保存目录或完整文件名，默认工作区"},
        "filename": {"type": "string", "description": "另存为的文件名"},
        "max_mb": {"type": "number", "description": "最大允许下载的 MB 数，默认 200"},
        "timeout": {"type": "number", "description": "超时秒数，默认 120"},
    }
    required = ["url"]

    async def run(self, ctx: ToolContext, url: str = "", path: str = "", filename: str = "",
                  max_mb: float = 200, timeout: float = 120, **_: Any) -> ToolResult:
        if not url:
            return ToolResult.fail("url 不能为空")
        dest = resolve(path, workspace=ctx.workspace) if path else ctx.workspace
        if dest.suffix and not dest.is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            target = dest
        else:
            dest.mkdir(parents=True, exist_ok=True)
            name = filename or _name_from_url(url)
            target = uniq_filename(dest, name)
        max_bytes = int(max_mb * 1024 * 1024)
        try:
            async with _client(timeout) as c:
                async with c.stream("GET", url) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    if total and total > max_bytes:
                        return ToolResult.fail(f"文件过大（{human_size(total)} > {max_mb} MB），已拒绝下载")
                    written = 0
                    with open(target, "wb") as f:
                        async for chunk in r.aiter_bytes(65536):
                            written += len(chunk)
                            if written > max_bytes:
                                f.close()
                                target.unlink(missing_ok=True)
                                return ToolResult.fail(f"下载超过限制 {max_mb} MB，已中止")
                            f.write(chunk)
        except Exception as e:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return ToolResult.fail(f"下载失败：{type(e).__name__}: {e}")
        return ToolResult(
            content=f"已下载到 {target}（{human_size(target.stat().st_size)}）",
            display=f"下载 {target.name} {human_size(target.stat().st_size)}",
            files=[str(target)],
            data={"path": str(target), "bytes": target.stat().st_size},
        )


def _name_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path) or "download"
    if "." not in name:
        name += ".bin"
    return sanitize_filename(name)


TOOLS = [WebSearchTool, WebFetchTool, HttpRequestTool, DownloadFileTool]
