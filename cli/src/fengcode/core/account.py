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
        """界面需要的账号设置（读配置；读不到就给安全默认值）。"""
        prompt_done = False
        provider_name, key_name = "wanxiang-account", "Fengcode 客户端"
        try:
            from ..config.manager import get_manager

            cfg = getattr(get_manager().config, "account", None)
            prompt_done = bool(getattr(cfg, "prompt_done", False))
            provider_name = str(getattr(cfg, "provider_name", "") or provider_name)
            key_name = str(getattr(cfg, "key_name", "") or key_name)
        except Exception:  # noqa: BLE001
            pass
        return {
            "prompt_done": prompt_done,
            "provider_name": provider_name,
            "key_name": key_name,
        }

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
                # ★ 用户自己的分组：建密钥时必须用它（见 ensure_key 的说明）。
                "group": user.get("group") or "",
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

    # ---- 模型绑定 --------------------------------------------------------

    def _pref(self, key: str, default: str) -> str:
        return str(self.prefs().get(key) or default)

    async def _get(self, cli: httpx.AsyncClient, path: str, tok: str, **params: Any) -> dict[str, Any]:
        """带登录态 GET，并把站点错误翻成人话。"""
        d = self._load()
        base = d.get("base_url") or self.base_url()
        try:
            resp = await cli.get(
                f"{base}{path}",
                headers={"Authorization": "Bearer " + tok},
                params=params or None,
            )
        except httpx.HTTPError as e:
            raise AccountError(f"连不上账号站点（{e.__class__.__name__}）") from e
        body = _payload(resp)
        if resp.status_code == 401 or body.get("code") == "AUTH_UNAUTHORIZED":
            self.clear()
            raise AccountError("登录已过期，请重新登录")
        if not body.get("success"):
            raise AccountError(_friendly(body.get("message")))
        return body

    async def _usable_groups(self, cli: httpx.AsyncClient, tok: str) -> list[str]:
        """账号能用哪些分组（仅在拿不到账号自身分组时兜底）。"""
        body = await self._get(cli, "/api/user/groups", tok)
        groups = body.get("data") or {}
        names = [g for g in groups if g]
        # ★ 不用 "auto"：它需要额外的自动分组参数，空着建出来的密钥路由不确定。
        real = [g for g in names if g != "auto"]
        return real or names

    async def _account_group(self, cli: httpx.AsyncClient, tok: str) -> str:
        """账号**自身**的分组。

        ★★ 建密钥必须用它，不能用「可用分组列表的第一个」。
          踩过的坑：可用分组是按字典序排的，「免费模型」恰好排在最前 ——
          用它建出来的密钥只能调那 4 个免费模型，而用户账号实际在
          「通用模型组」里、本来能用全部模型。实测症状：
          用户在客户端选付费模型，站点回「免费模型下没有可用渠道」。
        """
        try:
            body = await self._get(cli, "/api/user/self", tok)
        except AccountError:
            return ""
        return str((body.get("data") or {}).get("group") or "").strip()

    async def _fix_key_group(self, cli: httpx.AsyncClient, tok: str, mine: dict[str, Any],
                             want: str) -> str:
        """把已有密钥的分组纠正到 ``want``，返回纠正后的分组。

        ★ 站点上的「更新密钥」是**整体覆盖**式：只传 group 会把别的字段清掉，
          所以要把当前值一并带回去。纠正失败不中断流程（密钥本身还能用）。
        """
        cur = str(mine.get("group") or "")
        if not want or not cur or cur == want:
            return cur
        kid = int(mine.get("id") or 0)
        if not kid:
            return cur
        payload = {
            "id": kid,
            "name": mine.get("name") or "",
            "status": mine.get("status", 1),
            "expired_time": mine.get("expired_time", -1),
            "remain_quota": mine.get("remain_quota", 0),
            "unlimited_quota": bool(mine.get("unlimited_quota", True)),
            "model_limits_enabled": bool(mine.get("model_limits_enabled", False)),
            "model_limits": mine.get("model_limits") or "",
            "allow_ips": mine.get("allow_ips") or "",
            "group": want,
        }
        base = self._load().get("base_url") or self.base_url()
        try:
            resp = await cli.put(
                f"{base}/api/token/",
                headers={"Authorization": "Bearer " + tok},
                json=payload,
            )
        except httpx.HTTPError:
            return cur
        body = _payload(resp)
        if not body.get("success"):
            return cur
        return str((body.get("data") or {}).get("group") or want)

    async def _list_keys(self, cli: httpx.AsyncClient, tok: str) -> list[dict[str, Any]]:
        body = await self._get(cli, "/api/token/", tok, page=1, page_size=100)
        items = ((body.get("data") or {}).get("items")) or []
        return [it for it in items if isinstance(it, dict)]

    async def ensure_key(self) -> dict[str, Any]:
        """确保账号下有一条可用的密钥，返回它的明文与站点地址。

        ★ 为什么要「确保」而不是每次新建：站点每用户密钥数量有上限，
          而且**用户在官网的「密钥」页能看到这些记录** —— 复用一条比堆一串垃圾好。
        ★ 为什么要多一步查列表：建密钥的接口**只返回成功，不返回 key 也不返回 id**，
          所以建完必须回查才能拿到 id，再用 id 换取明文。
        """
        async with self._lock:
            if not self.logged_in():
                raise AccountError("请先登录账号")
            tok = await self._access_token()
            key_name = self._pref("key_name", "Fengcode 客户端")
            d = self._load()
            base = d.get("base_url") or self.base_url()

            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
                keys = await self._list_keys(cli, tok)
                mine = next((k for k in keys if str(k.get("name") or "") == key_name), None)

                # ★★ 建密钥要用**账号自己的分组**（它能用全部模型）。
                #   拿不到才退回可用分组列表（这时列表第一个可能只是「免费模型」）。
                want_group = await self._account_group(cli, tok)
                if not want_group:
                    groups = await self._usable_groups(cli, tok)
                    if not groups:
                        raise AccountError("该账号没有任何可用分组，无法创建密钥")
                    want_group = groups[0]

                if mine is None:
                    try:
                        resp = await cli.post(
                            f"{base}/api/token/",
                            headers={"Authorization": "Bearer " + tok},
                            json={
                                "name": key_name,
                                "group": want_group,
                                # ★ 密钥本身不限额度：钱从**账号余额**里扣（见方案说明）。
                                #   设成固定额度会变成「密钥里有钱、账号余额不动」，不是我们要的。
                                "unlimited_quota": True,
                                "expired_time": -1,
                                "remain_quota": 0,
                            },
                        )
                    except httpx.HTTPError as e:
                        raise AccountError(f"连不上账号站点（{e.__class__.__name__}）") from e
                    body = _payload(resp)
                    if not body.get("success"):
                        raise AccountError(_friendly(body.get("message")))
                    # 回查拿 id
                    keys = await self._list_keys(cli, tok)
                    mine = next((k for k in keys if str(k.get("name") or "") == key_name), None)
                    if mine is None:
                        raise AccountError("密钥已创建，但未能读回，请稍后重试")
                else:
                    # ★ 已有密钥：若分组是旧版建错的（如「免费模型」），顺手纠正过来。
                    #   不纠正的话，用户选了付费模型会一直报「没有可用渠道」。
                    fixed = await self._fix_key_group(cli, tok, mine, want_group)
                    if fixed:
                        mine = dict(mine)
                        mine["group"] = fixed

                kid = int(mine.get("id") or 0)
                if not kid:
                    raise AccountError("未能取到密钥编号，请稍后重试")
                try:
                    resp = await cli.post(
                        f"{base}/api/token/{kid}/key",
                        headers={"Authorization": "Bearer " + tok},
                    )
                except httpx.HTTPError as e:
                    raise AccountError(f"连不上账号站点（{e.__class__.__name__}）") from e
                body = _payload(resp)
                if not body.get("success"):
                    raise AccountError(_friendly(body.get("message")))
                raw = str((body.get("data") or {}).get("key") or "").strip()
                if not raw:
                    raise AccountError("未能取到密钥内容，请稍后重试")

                # 记下 id 与分组，界面据此判断「绑定过了」
                d = self._load()
                d["key_id"] = kid
                d["key_group"] = str(mine.get("group") or "")
                d["key_ready"] = True
                self._save(d)
                return {
                    "api_key": raw,
                    "key_id": kid,
                    "group": d["key_group"],
                    "base_url": base,
                }

    async def user_models(self) -> list[str]:
        """账号能用哪些模型（按可用分组并集，站点已过滤内部模型）。"""
        if not self.logged_in():
            raise AccountError("请先登录账号")
        tok = await self._access_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as cli:
            body = await self._get(cli, "/api/user/models", tok)
        data = body.get("data")
        if isinstance(data, dict):
            # 兼容「按分组返回」的形状
            out: list[str] = []
            for v in data.values():
                if isinstance(v, list):
                    out.extend(str(x) for x in v if x)
            return list(dict.fromkeys(out))
        if isinstance(data, list):
            return [str(x) for x in data if x]
        return []




_account: AccountManager | None = None


def get_account() -> AccountManager:
    """取全局账号管理器。"""
    global _account
    if _account is None:
        _account = AccountManager()
    return _account
