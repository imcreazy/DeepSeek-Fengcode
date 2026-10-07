"""可选账号源：登录后在客户端里直接看到账户余额。

定位
----
**可选增强**，不是唯一入口。Fengcode 不登录也能完整使用（自己填供应商密钥）；
登录只是把「余额」这类账户信息接进界面，省得再去网页上看。
因此这里任何失败都**只影响显示**，绝不影响对话能力。

凭证怎么放
----------
密码**不落盘**（登录时用完即弃）。落盘的只有站点下发的**会话刷新凭证**，
存在数据目录的 ``config/account.json``，仅服务端可读、不下发界面。

为什么存刷新凭证而不是 access token
-----------------------------------
登录会同时下发两样东西：

* ``access_token`` —— 15 分钟就过期，太短，存下来很快就不能用；
* 刷新凭证（HttpOnly cookie）—— 30 天有效，且每次刷新会轮换。

所以只存后者，需要时现换一个 ``access_token`` 来用。

★ 为什么不去调「生成用户级 token」那个接口：它会**覆写**用户在站点上的
  ``access_token``，把用户在其他地方正在用的凭证顶掉。这里只读余额，
  没有理由动它。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from .. import paths
from ..utils import read_json, write_json

DEFAULT_BASE_URL = "https://liufengzi.qd.je"
DEFAULT_QUOTA_PER_UNIT = 5_000_000
DEFAULT_CURRENCY = "Ψ"

_TIMEOUT = 20.0
# access_token 提前多久算过期（留出余量，避免边界上刚好失效）
_SKEW = 60.0


class AccountError(Exception):
    """账号操作失败。消息是给人看的，可直接显示。"""


def _cred_file() -> Path:
    return paths.config_dir() / "account.json"


def _payload(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _cookie_of(resp: httpx.Response, name: str) -> str:
    """从响应的 Set-Cookie 里取某个 cookie 的值（拿不到就返回空串）。"""
    try:
        v = resp.cookies.get(name)
        if v:
            return str(v)
    except Exception:  # noqa: BLE001
        pass
    for raw in resp.headers.get_list("set-cookie"):
        head = raw.split(";", 1)[0]
        if "=" in head:
            k, _, val = head.partition("=")
            if k.strip() == name:
                return val.strip()
    return ""


def _friendly(message: Any) -> str:
    """把站点返回的英文提示换成人话。"""
    msg = str(message or "").strip()
    low = msg.lower()
    if not msg:
        return "登录失败，请稍后重试"
    if "username or password" in low or "password is incorrect" in low:
        return "账号或密码不正确"
    if "banned" in low:
        return "该账号已被封禁"
    if "require 2fa" in low or "2fa" in low:
        return "该账号启用了两步验证"
    if "rate" in low and "limit" in low:
        return "尝试太频繁，请稍后再试"
    if "disabled" in low:
        return "该站点未开放密码登录"
    return msg


class AccountManager:
    """账号状态与凭证管理（单例由 :func:`get_account` 提供）。"""

    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    # ---- 本地凭证 --------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            raw = read_json(_cred_file(), {})
            self._data = raw if isinstance(raw, dict) else {}
        return self._data

    def _save(self, data: dict[str, Any]) -> None:
        self._data = data
        try:
            write_json(_cred_file(), data)
        except OSError:
            # 写不进去不影响本次会话内的使用
            pass

    def clear(self) -> None:
        """清掉本地凭证（登出 / 凭证失效）。"""
        self._data = {}
        try:
            _cred_file().unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 站点地址 --------------------------------------------------------

    def base_url(self) -> str:
        d = self._load()
        url = str(d.get("base_url") or "").strip()
        if url:
            return url.rstrip("/")
        try:
            from ..config.manager import get_manager

            cfg = getattr(get_manager().config, "account", None)
            url = str(getattr(cfg, "base_url", "") or "").strip()
        except Exception:  # noqa: BLE001
            url = ""
        return (url or DEFAULT_BASE_URL).rstrip("/")

    # ---- 给界面的脱敏快照 ------------------------------------------------

    def logged_in(self) -> bool:
        return bool(str(self._load().get("refresh_token") or "").strip())

    def snapshot(self) -> dict[str, Any]:
        """**纯本地**读取：不发网络请求，用于界面首屏与状态栏。

        余额是**上次成功读取**的值（界面会标注时间），要最新值走 :meth:`self`。
        """
        d = self._load()
        out = (
            {"logged_in": False, "base_url": self.base_url()}
            if not self.logged_in()
            else self._public(d, None)
        )
        out.update(self.prefs())
        return out

    def prefs(self) -> dict[str, Any]:
        """界面需要的两个开关（读配置；读不到就给安全默认值）。"""
        prompt_done, show_balance = False, True
        try:
            from ..config.manager import get_manager

            cfg = getattr(get_manager().config, "account", None)
            prompt_done = bool(getattr(cfg, "prompt_done", False))
            show_balance = bool(getattr(cfg, "show_balance", True))
        except Exception:  # noqa: BLE001
            pass
        return {"prompt_done": prompt_done, "show_balance": show_balance}

    def _public(self, d: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
        """组装**可下发界面**的字段：只有身份与余额，绝不含任何凭证。"""
        u = user or {}
        per = int(d.get("quota_per_unit") or DEFAULT_QUOTA_PER_UNIT) or DEFAULT_QUOTA_PER_UNIT
        cur = str(d.get("currency") or DEFAULT_CURRENCY)

        def pick(key: str, fallback: Any = None) -> Any:
            """实时值优先，没有就用上次缓存的值。"""
            if key in u and u.get(key) is not None:
                return u.get(key)
            return d.get(key, fallback)

        out: dict[str, Any] = {
            "logged_in": True,
            "base_url": d.get("base_url") or DEFAULT_BASE_URL,
            "currency": cur,
            "username": pick("username") or d.get("username") or "",
            "display_name": pick("display_name") or "",
            "logged_in_at": d.get("logged_in_at") or 0,
            "updated_at": d.get("last_ok_at") or 0,
        }
        quota = pick("quota")
        if quota is not None:
            out["balance"] = round(float(quota) / per, 4)
            out["used"] = round(float(pick("used_quota", 0) or 0) / per, 4)
            rc = pick("request_count")
            if rc is not None:
                out["request_count"] = rc
        return out

    # ---- 网络 ------------------------------------------------------------

    async def _site_info(self, cli: httpx.AsyncClient, base: str) -> dict[str, Any]:
        """读站点的公开配置，拿「多少积分算一个单位」和币种符号。"""
        try:
            resp = await cli.get(f"{base}/api/status")
            d = _payload(resp).get("data") or {}
            per = int(d.get("quota_per_unit") or DEFAULT_QUOTA_PER_UNIT)
            return {
                "quota_per_unit": per or DEFAULT_QUOTA_PER_UNIT,
                "currency": str(d.get("custom_currency_symbol") or DEFAULT_CURRENCY),
            }
        except Exception:  # noqa: BLE001
            return {"quota_per_unit": DEFAULT_QUOTA_PER_UNIT, "currency": DEFAULT_CURRENCY}

    async def login(self, username: str, password: str) -> dict[str, Any]:
        """登录并保存凭证。密码只用于这一次请求，不落盘。"""
        username = (username or "").strip()
        if not username or not password:
            raise AccountError("请填写账号与密码")
        base = self.base_url()
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
            try:
                resp = await cli.post(
                    f"{base}/api/user/login",
                    json={"username": username, "password": password},
                )
            except httpx.HTTPError as e:
                raise AccountError(f"连不上账号站点（{e.__class__.__name__}）") from e

            body = _payload(resp)
            if not body.get("success"):
                raise AccountError(_friendly(body.get("message")))
            data = body.get("data") or {}
            if data.get("require_2fa"):
                raise AccountError("该账号启用了两步验证，请先在网页端登录一次再试")

            refresh = _cookie_of(resp, "new_api_refresh")
            if not refresh:
                raise AccountError("站点未返回会话凭证，无法保持登录")

            site = await self._site_info(cli, base)
            user = data.get("user") or {}
            now = time.time()
            self._save({
                "base_url": base,
                "username": user.get("username") or username,
                "display_name": user.get("display_name") or "",
                "refresh_token": refresh,
                "access_token": data.get("access_token") or "",
                "access_expires_at": float(data.get("access_expires_at") or (now + 600)),
                "quota": user.get("quota"),
                "used_quota": user.get("used_quota"),
                "request_count": user.get("request_count"),
                "quota_per_unit": site["quota_per_unit"],
                "currency": site["currency"],
                "logged_in_at": now,
                "last_ok_at": now,
            })
            return self._public(self._load(), user)

    async def _access_token(self) -> str:
        """取一个当前可用的 access_token（过期的先用刷新凭证换新的）。"""
        d = self._load()
        tok = str(d.get("access_token") or "").strip()
        exp = float(d.get("access_expires_at") or 0)
        if tok and exp - _SKEW > time.time():
            return tok

        rt = str(d.get("refresh_token") or "").strip()
        if not rt:
            raise AccountError("尚未登录")
        base = d.get("base_url") or self.base_url()
        sid = rt.split(".", 1)[0]
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
            try:
                resp = await cli.post(
                    f"{base}/api/user/auth/refresh",
                    headers={"Cookie": f"new_api_refresh={rt}", "X-Auth-Session": sid},
                )
            except httpx.HTTPError as e:
                raise AccountError(f"连不上账号站点（{e.__class__.__name__}）") from e

            body = _payload(resp)
            if not body.get("success"):
                # 刷新凭证也失效了 → 清掉，让界面回到未登录态
                self.clear()
                raise AccountError("登录已过期，请重新登录")
            data = body.get("data") or {}
            new_tok = str(data.get("access_token") or "").strip()
            if not new_tok:
                raise AccountError("站点未返回有效凭证")
            new_rt = _cookie_of(resp, "new_api_refresh") or rt
            d = self._load()
            d.update({
                "access_token": new_tok,
                "access_expires_at": float(data.get("access_expires_at") or (time.time() + 600)),
                "refresh_token": new_rt,
            })
            self._save(d)
            return new_tok

    async def self(self) -> dict[str, Any]:
        """拉一次账户信息（含余额），并更新本地快照。"""
        async with self._lock:
            if not self.logged_in():
                return {"logged_in": False, "base_url": self.base_url()}
            tok = await self._access_token()
            d = self._load()
            base = d.get("base_url") or self.base_url()
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
                try:
                    resp = await cli.get(
                        f"{base}/api/user/self",
                        headers={"Authorization": "Bearer " + tok},
                    )
                except httpx.HTTPError as e:
                    raise AccountError(f"连不上账号站点（{e.__class__.__name__}）") from e

                body = _payload(resp)
                if resp.status_code == 401 or body.get("code") == "AUTH_UNAUTHORIZED":
                    self.clear()
                    raise AccountError("登录已过期，请重新登录")
                if not body.get("success"):
                    raise AccountError(_friendly(body.get("message")))

                user = body.get("data") or {}
                d = self._load()
                for k in ("username", "display_name", "quota", "used_quota", "request_count"):
                    if user.get(k) is not None:
                        d[k] = user.get(k)
                d["last_ok_at"] = time.time()
                self._save(d)
                return self._public(d, user)

    async def logout(self) -> None:
        """登出：尽力通知站点释放会话，然后清本地凭证。"""
        d = self._load()
        rt = str(d.get("refresh_token") or "").strip()
        tok = str(d.get("access_token") or "").strip()
        base = d.get("base_url") or self.base_url()
        if rt:
            try:
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as cli:
                    await cli.post(
                        f"{base}/api/user/auth/logout",
                        headers={
                            "Cookie": f"new_api_refresh={rt}",
                            "X-Auth-Session": rt.split(".", 1)[0],
                            **({"Authorization": "Bearer " + tok} if tok else {}),
                        },
                    )
            except Exception:  # noqa: BLE001
                # 站点没收到也无妨，本地凭证照样清掉
                pass
        self.clear()


_account: AccountManager | None = None


def get_account() -> AccountManager:
    """取全局账号管理器。"""
    global _account
    if _account is None:
        _account = AccountManager()
    return _account
