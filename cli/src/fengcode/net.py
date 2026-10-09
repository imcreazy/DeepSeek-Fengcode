# -*- coding: utf-8 -*-
"""出网代理：把「设置 → 网络」的三档选择真正作用到 httpx 客户端。

★ 为什么需要单独一个模块
------------------------
以前项目里**没有任何代理配置**，httpx 默认读进程环境变量（``HTTP_PROXY`` 等）。
桌面端是主进程拉起的子进程，用户改不了它的环境，于是「公司里必须走代理才能用」
这件事在界面上无处设置、也说不清为什么连不上。这里把三档统一成一次调用：

    · ``system`` 跟随系统 —— 返回 ``None``，让 httpx 自己读环境变量（原行为）
    · ``manual`` 手动    —— 返回组装好的代理 URL，强制所有请求走它
    · ``direct`` 直连    —— 显式关闭代理，**忽略**环境变量里的代理

用法：给每个 ``httpx.AsyncClient(...)`` 加一行 ``proxy=proxy_for(...)`` 即可，
不改动任何请求逻辑。``amain``/``sync`` 两种客户端都适用。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(s: str) -> str:
    """展开 ``${VAR}`` 引用（密码不想明文写进配置时用）。"""
    return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), s or "")


def config_of(mgr: Any = None) -> Any:
    """尽量拿到 NetworkConfig；拿不到就返回 None（等同于「跟随系统」）。"""
    try:
        if mgr is None:
            from .config.manager import get_manager

            mgr = get_manager()
        return getattr(mgr.config, "network", None)
    except Exception:
        return None


def build_url(net: Any) -> str:
    """把 NetworkConfig 的字段拼成代理 URL；信息不全时返回空串。"""
    if net is None:
        return ""
    host = str(getattr(net, "proxy_host", "") or "").strip()
    port = int(getattr(net, "proxy_port", 0) or 0)
    if not host or not port:
        return ""
    kind = str(getattr(net, "proxy_type", "") or "http")
    user = str(getattr(net, "proxy_user", "") or "").strip()
    pwd = _expand(str(getattr(net, "proxy_pass", "") or ""))
    auth = ""
    if user:
        auth = quote(user, safe="")
        if pwd:
            auth += ":" + quote(pwd, safe="")
        auth += "@"
    return f"{kind}://{auth}{host}:{port}"


def proxy_for(mgr: Any = None) -> Any:
    """给 httpx 客户端用的 proxy 参数。

    返回 ``None`` = 「不传这个参数」（由 ``apply_to_env`` 决定实际走向）；
    返回 URL 字符串 = 显式指定代理。
    ★★★ 实测踩到的坑：``direct`` 档**不能**返回空串。httpx 拿到 ``proxy=""``
      会抛 ``ValueError: Unknown scheme for proxy URL URL('')``（真发请求时才炸）。
      「直连」的正确表达是「清空代理环境变量 + 设 ``NO_PROXY=*`` + 不传 proxy」，
      也就是这里返回 ``None`` —— 见 ``apply_to_env``。
    """
    net = config_of(mgr)
    mode = str(getattr(net, "mode", "system") or "system")
    if mode == "manual":
        url = build_url(net)
        # 手动档但信息不全 → 明确报错，不要静默退回直连
        #  （用户以为设了代理，实际没走，那种沉默最难查）
        if not url:
            raise ValueError("网络设置为「手动代理」，但代理地址或端口为空")
        return url
    # system / direct：都不显式传代理（direct 靠环境变量已清空来实现）
    return None


def no_proxy_list(net: Any) -> str:
    """返回 no_proxy 字符串（给 httpx 的 ``NO_PROXY`` 用）。"""
    return str(getattr(net, "no_proxy", "") or "")


# 本程序管理的环境变量名（改档位时要整体重设或清除）
_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")


def apply_to_env(mgr: Any = None) -> str:
    """把当前设置写进**进程环境变量**，返回生效方式的说明。

    ★★★ 为什么用环境变量而不是逐个客户端传参：httpx 默认 ``trust_env=True``，
      会读 ``HTTP_PROXY`` 等变量。项目里有十几处独立的 ``httpx.AsyncClient(...)``
      （模型、MCP、向量、网页抓取、账号…），逐个传代理容易漏、以后新增的还会忘。
      写进环境变量则**一处生效、全部受益**，新增客户端自动跟随。
    ★ 桌面端是主进程拉起的子进程，它的初始环境用户改不了 —— 所以「跟随系统」
      指的是**这台机器上已有的代理环境变量**，而手动/直连由本程序在这里覆写。
    """
    net = config_of(mgr)
    mode = str(getattr(net, "mode", "system") or "system")
    if mode == "system":
        return "跟随系统（不改环境变量）"
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    if mode == "direct":
        # 显式清空 → httpx 不会用任何代理，也就是「直连」
        for k in ("NO_PROXY", "no_proxy"):
            os.environ[k] = "*"
        return "直连（已忽略系统代理）"
    url = build_url(net)
    if not url:
        return "手动代理：地址或端口为空，未生效"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[k] = url
    np = no_proxy_list(net)
    if np:
        os.environ["NO_PROXY"] = np
        os.environ["no_proxy"] = np
    return f"手动代理：{url.split('@')[-1]}"


async def test_connection(mgr: Any = None, timeout: float = 15.0) -> dict[str, Any]:
    """按当前设置发一次真实请求，验证出网是否通。

    ★ 必须是**真请求**：只检查配置对不对说明不了任何事，用户要的是
      「现在到底能不能连上」。失败时把「中断在哪一段」说清楚。
    """
    import time

    import httpx

    net = config_of(mgr)
    url = str(getattr(net, "test_url", "") or "").strip() or "https://www.bing.com/"
    t0 = time.time()
    try:
        proxy = proxy_for(mgr)
    except ValueError as e:
        return {"ok": False, "url": url, "error": str(e), "stage": "配置"}
    try:
        kw: dict[str, Any] = {"timeout": httpx.Timeout(timeout, connect=8.0),
                              "follow_redirects": True}
        if proxy is not None:
            kw["proxy"] = proxy
        async with httpx.AsyncClient(**kw) as cli:
            r = await cli.get(url)
        return {"ok": True, "url": url, "status": r.status_code,
                "duration": round(time.time() - t0, 3),
                "via": proxy if isinstance(proxy, str) and proxy else "(直连)",
                "stage": ""}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}",
                "duration": round(time.time() - t0, 3), "stage": "连接"}
