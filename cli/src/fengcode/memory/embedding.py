"""文本嵌入：优先调用 API，失败自动降级为本地确定性向量。

为什么需要降级方案
------------------
用户可能没配 embedding 模型、或中转站不支持 ``/embeddings``。
此时不能让记忆检索直接瘫痪——本地哈希向量（hashing trick + 字符 n-gram）
对**中文短语的相似度**仍有一定区分度，足以做召回排序兜底。

向量统一为 ``list[float]``（已 L2 归一化），存储时序列化为 float32 BLOB。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import struct
from typing import Any

import httpx

from ..llm.base import DEFAULT_USER_AGENT

DEFAULT_DIM = 512


# ---- 序列化 --------------------------------------------------------------

def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    if n == 0:
        return []
    return list(struct.unpack(f"<{n}f", blob[: n * 4]))


def normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        return vec
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（向量已归一化时即点积）。"""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        dot += a[i] * b[i]
        na += a[i] * a[i]
        nb += b[i] * b[i]
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return dot / math.sqrt(na * nb)


# ---- 本地哈希向量 --------------------------------------------------------

def hashing_embed(text: str, dim: int = DEFAULT_DIM) -> list[float]:
    """确定性哈希向量：字符 1/2/3-gram + 词，用哈希投影到固定维度。

    对中文有效（按字符切），对英文也有基本效果。无需任何依赖、零延迟。
    """
    if not text:
        return [0.0] * dim
    s = text.lower()
    vec = [0.0] * dim
    tokens: list[str] = []

    # 连续字符 n-gram（覆盖中英文）
    compact = "".join(ch for ch in s if not ch.isspace())
    for n in (1, 2, 3):
        for i in range(len(compact) - n + 1):
            tokens.append(compact[i : i + n])
    # 英文单词（权重更高）
    word = []
    for ch in s + " ":
        if ch.isalnum() or ch in "_-.":
            word.append(ch)
        else:
            if len(word) >= 2:
                tokens.append("".join(word))
                tokens.append("".join(word))
            word = []

    for tok in tokens:
        h = hashlib.md5(tok.encode("utf-8")).digest()
        idx = struct.unpack_from("<I", h, 0)[0] % dim
        sign = 1.0 if h[4] & 1 else -1.0
        # 短 token 权重略低，避免高频单字主导
        w = 1.0 if len(tok) >= 2 else 0.5
        vec[idx] += sign * w
    return normalize(vec)


# ---- API 嵌入 ------------------------------------------------------------

class Embedder:
    """统一的嵌入入口。"""

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str = "",
        dim: int = DEFAULT_DIM,
        manager: Any = None,
        local_only: bool = False,
    ) -> None:
        self.provider_name = provider
        self.model = model
        self.dim = dim
        self.manager = manager
        self.local_only = local_only
        self._client: httpx.AsyncClient | None = None
        self._api_ok: bool | None = None
        self._cache: dict[str, list[float]] = {}

    # ---- 客户端 --------------------------------------------------------
    async def _get_client(self, base_url: str) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ---- 主入口 --------------------------------------------------------
    async def embed(self, text: str) -> list[float]:
        key = text[:2000]
        if key in self._cache:
            return self._cache[key]
        vec: list[float] = []
        if not self.local_only and self._api_ok is not False:
            vec = await self._embed_api(text)
            if vec:
                self._api_ok = True
            else:
                self._api_ok = False
        if not vec:
            vec = await asyncio.to_thread(hashing_embed, text, self.dim)
        if len(self._cache) > 4000:
            self._cache.clear()
        self._cache[key] = vec
        return vec

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.local_only and self._api_ok is not False:
            vecs = await self._embed_api_batch(texts)
            if vecs and len(vecs) == len(texts):
                self._api_ok = True
                return vecs
            if self._api_ok is None:
                self._api_ok = False
        return [await asyncio.to_thread(hashing_embed, t, self.dim) for t in texts]

    @property
    def backend(self) -> str:
        if self.local_only or self._api_ok is False:
            return "local-hash"
        if self._api_ok is True:
            return f"api:{self.provider_name or 'default'}/{self.model or 'auto'}"
        return "unknown"

    # ---- API 实现 ------------------------------------------------------
    def _resolve(self) -> tuple[str, str, str] | None:
        """返回 ``(base_url, api_key, model)``；无法解析返回 None。"""
        if self.manager is None:
            return None
        try:
            prov = self.manager.get_provider(self.provider_name)
            if prov is None:
                for p in self.manager.enabled_providers():
                    if "embed" in " ".join(p.models).lower() or p.kind == "openai":
                        prov = p
                        break
            if prov is None:
                return None
            base = (prov.base_url or "").rstrip("/")
            if not base:
                return None
            key = self.manager.resolve_api_key(prov) or ""
            model = self.model or _guess_model(prov)
            if not model:
                return None
            return base, key, model
        except Exception:
            return None

    async def _embed_api(self, text: str) -> list[float]:
        got = await self._embed_api_batch([text])
        return got[0] if got else []

    async def _embed_api_batch(self, texts: list[str]) -> list[list[float]]:
        info = self._resolve()
        if not info:
            return []
        base, key, model = info
        url = base + "/embeddings" if base.endswith(("/v1", "/v4")) else base + "/v1/embeddings"
        headers = {"Content-Type": "application/json", "User-Agent": DEFAULT_USER_AGENT}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            client = await self._get_client(base)
            r = await client.post(url, json={"model": model, "input": texts}, headers=headers)
            if r.status_code != 200:
                return []
            data = r.json()
            items = data.get("data") or []
            out: list[list[float]] = []
            for it in items:
                emb = it.get("embedding") if isinstance(it, dict) else None
                if isinstance(emb, list) and emb:
                    out.append(normalize([float(x) for x in emb]))
            if len(out) != len(texts):
                return []
            if out:
                self.dim = len(out[0])
            return out
        except Exception:
            return []


def _guess_model(prov: Any) -> str:
    models = list(getattr(prov, "models", []) or [])
    for m in models:
        if "embed" in m.lower():
            return m
    # 常见 embedding 模型名兜底
    low = (getattr(prov, "base_url", "") or "").lower()
    if "openai" in low:
        return "text-embedding-3-small"
    if "dashscope" in low or "aliyun" in low:
        return "text-embedding-v3"
    if "bigmodel" in low or "zhipu" in low:
        return "embedding-3"
    return ""


def get_embedder(**kwargs: Any) -> Embedder:
    return Embedder(**kwargs)


__all__ = [
    "Embedder",
    "get_embedder",
    "hashing_embed",
    "pack",
    "unpack",
    "cosine",
    "normalize",
    "DEFAULT_DIM",
]
