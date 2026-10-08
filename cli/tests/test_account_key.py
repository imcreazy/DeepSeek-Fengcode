"""防止「账号接入拿错密钥/列错模型」回归。

★★★ 实测事故（1.3.0 之前）：
  · 登录万象账号后点「获取模型」，列表里**没有免费模型**；
  · 而自己建的密钥绑到「通用模型组」，该组只有 12 个模型 —— 免费模型、
    生图模型全不在里面，于是「免费模型在 Fengcode 里用不了」。

★ 根因（两处叠加）：
  ① 客户端按「账号自身分组」自建密钥，而账号分组只覆盖部分模型；
  ② 「获取模型」用的是 `/api/user/models`（**用户可用分组的并集**），
     比密钥实际能访问的范围大，于是列表里出现选了必然报错的名字。

★ 事实（站点侧核实）：站点有一个隐藏的 `fengcode` 分组，**收录全部渠道的全部模型**，
  每个账号（含新注册）都静默持有该分组的一条密钥，站点留有
  `GET /api/user/fengcode-key` 专门供客户端以登录态换取。

本文件四道防线：
  ① 内置密钥取用正常 → 返回密钥并以密钥自述范围列模型；
  ② 站点没有该接口时**回退**老路径，不能整体失败；
  ③ 模型范围以密钥自述（`/v1/models`）为准并保序去重；
  ④ 两个 HTTP 动作都以「内置密钥优先」的顺序接入（静态断言，防回退）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

_P = Path(__file__).resolve().parent.parent / "src"
os.environ.setdefault("FENGCODE_HOME", tempfile.mkdtemp(prefix="fengcode-account-"))
if str(_P) not in sys.path:
    sys.path.insert(0, str(_P))

from fengcode.core import account as A  # noqa: E402

APP_PY = _P / "fengcode" / "server" / "app.py"


def _fresh(**over):
    """写一份「已登录」的本地凭证，返回 AccountManager。"""
    d = {
        "base_url": "https://site.test",
        "username": "tester",
        "refresh_token": "sid-1.token-value",
        "access_token": "tok-live",
        "access_expires_at": time.time() + 3600,
        "quota": 5_000_000,
    }
    d.update(over)
    A._cred_file().parent.mkdir(parents=True, exist_ok=True)
    A._cred_file().write_text(json.dumps(d), encoding="utf-8")
    acc = A.AccountManager()
    acc._data = None          # 丢弃可能存在的缓存
    return acc


def _patch_client(monkeypatch, handler):
    """把 account 内部自建的 httpx.AsyncClient 换成走假 transport 的实例。

    ★ 必须先抓住**原始类**再替换：`A.httpx` 就是 `httpx` 模块本身，
      替换它的 `AsyncClient` 属性后，工厂函数里若再调 `httpx.AsyncClient`
      拿到的就是工厂自己 —— 立刻无限递归（实测把输出刷到几百 KB）。
    """
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real(transport=httpx.MockTransport(handler), timeout=5.0)

    monkeypatch.setattr(A.httpx, "AsyncClient", factory)


async def test_builtin_key_returns_site_key_and_its_models(monkeypatch):
    """① 有内置密钥接口时：拿到密钥，模型范围以密钥自述为准（含免费模型）。"""
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path == "/api/user/fengcode-key":
            return httpx.Response(200, json={
                "success": True,
                "data": {"key": "sk-BUILTIN", "group": "fengcode", "name": "fengcode 内置密钥"},
            })
        if req.url.path == "/v1/models":
            assert req.headers.get("authorization") == "Bearer sk-BUILTIN"
            return httpx.Response(200, json={"data": [
                {"id": "glm-5.2-free"}, {"id": "deepseek-v4.1-flash"}, {"id": "glm-5.2-free"},
            ]})
        return httpx.Response(404, json={"success": False})

    _patch_client(monkeypatch, handler)
    acc = _fresh()
    got = await acc.builtin_key()
    assert got["api_key"] == "sk-BUILTIN"
    assert got["group"] == "fengcode"
    assert got["builtin"] is True

    models = await acc.models_for_key(got["api_key"])
    assert models == ["glm-5.2-free", "deepseek-v4.1-flash"], f"未保序去重：{models}"
    # 密钥本地留痕，界面据此判断「绑定过了」
    saved = json.loads(A._cred_file().read_text(encoding="utf-8"))
    assert saved["key_builtin"] is True and saved["key_group"] == "fengcode"


async def test_builtin_key_falls_back_when_site_lacks_it(monkeypatch):
    """② 站点没有该接口（旧版）→ 返回空字典回退，不抛错、不阻断整条链路。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "message": "not found"})

    _patch_client(monkeypatch, handler)
    acc = _fresh()
    assert await acc.builtin_key() == {}


async def test_models_for_key_is_empty_on_failure(monkeypatch):
    """③ 密钥无效 / 网络失败 → 返回空列表（调用方据此回退），不抛异常。"""
    _patch_client(monkeypatch, lambda req: httpx.Response(403, json={"error": "no"}))
    acc = _fresh()
    assert await acc.models_for_key("sk-bad") == []
    assert await acc.models_for_key("") == []


def test_bind_and_models_prefer_builtin_key():
    """④ 两个动作都必须以「内置密钥优先」接入，且绑定后按密钥自述范围过滤模型。"""
    src = APP_PY.read_text(encoding="utf-8")

    # 一键绑定：内置优先 → 取密钥自述范围 → 才退回账号分组并集
    i = src.find("async def _account_bind(")
    assert i > 0, "找不到 _account_bind"
    seg = src[i:src.find("async def ", i + 10)]
    assert "acc.builtin_key()" in seg, "绑定未优先取站点内置密钥"
    assert seg.find("acc.builtin_key()") < seg.find("acc.ensure_key()"), \
        "内置密钥必须排在自建密钥**之前**（自建只能绑到账号分组，不含免费模型）"
    assert "models_for_key" in seg and seg.find("models_for_key") < seg.find("acc.user_models()"), \
        "模型范围必须以密钥自述为准，账号分组并集只能兜底"

    # 获取模型：与绑定同一条路，避免「列得出来、选了却调不通」
    j = src.find('if action == "models":')
    assert j > 0, "找不到 models 动作"
    seg2 = src[j:j + 900]
    assert "builtin_key()" in seg2 and "models_for_key" in seg2, \
        "「获取模型」未与绑定共用同一条取密钥/取模型路径"
