"""分层记忆管理器。

五层记忆
--------
=========  ==========  ================================================
层         存储位置     说明
=========  ==========  ================================================
工作记忆   内存        当前会话的即时信息（最近若干轮 + 当前任务清单）
短期记忆   DB messages 完整会话消息（可回放、可压缩）
长期记忆   DB memories 跨会话的稳定事实、知识、偏好（向量 + 全文索引）
情景记忆   DB memories 带时间与场景的"某次发生了什么"（kind=episode）
用户画像   DB memories kind=profile 的结构化键值 + 自由描述
=========  ==========  ================================================

检索：全文（FTS5）+ 向量（余弦）混合打分，再叠加重要度/新鲜度/命中次数。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..config.schema import MemoryConfig
from ..llm.types import Message
from ..storage.db import Database, build_match_query, get_db, tokenize_for_fts
from ..utils import estimate_tokens, new_id, truncate
from .embedding import Embedder, cosine, pack, unpack

# 记忆种类
KIND_FACT = "fact"          # 事实/知识
KIND_PREFERENCE = "preference"  # 用户偏好
KIND_EPISODE = "episode"    # 情景（某次对话/任务的经过）
KIND_PROFILE = "profile"    # 用户画像条目
KIND_TASK = "task"          # 任务相关结论
KIND_DECISION = "decision"  # 决策记录

VALID_KINDS = {KIND_FACT, KIND_PREFERENCE, KIND_EPISODE, KIND_PROFILE, KIND_TASK, KIND_DECISION}

# ★ 记忆易变性三档：决定召回时对「新鲜度」的看重程度。
#   permanent 几乎不变（用户偏好、项目约定）· stable 稳定（架构、技术选型）
#   · volatile 易变（当前任务、临时状态）。
VALID_VOLATILITY = {"permanent", "stable", "volatile"}


@dataclass
class MemoryItem:
    """一条记忆。"""

    id: str = ""
    scope: str = "global"
    kind: str = KIND_FACT
    title: str = ""
    description: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""
    session_id: str | None = None
    importance: float = 0.5
    confidence: float = 1.0
    pinned: bool = False
    archived: bool = False
    revision: int = 1
    access_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    accessed_at: float = 0.0
    score: float = 0.0
    reason: str = ""
    # ---- 记忆维度补强 ----
    # ★ 易变性：这条事实有多容易过时。
    #   permanent（几乎不变：用户偏好、项目约定）/ stable（稳定：架构、技术选型）
    #   / volatile（易变：当前任务、临时状态）。召回时易变的要更看重「新鲜度」。
    volatility: str = "stable"
    # ★ 过期日（时间戳，0 = 永不过期）。到点后召回降权并提示，而不是突然消失。
    expires_at: float = 0.0
    # ★ 主题键：同一主题只保留一个「活跃值」，避免两条相互矛盾的记忆同时召回。
    #   例如 topic_key="用户时区" 的记忆被再次写入时，直接覆盖旧值。
    topic_key: str = ""

    @property
    def body(self) -> str:
        """用于检索与嵌入的完整文本。"""
        parts = [self.title, self.description, self.content]
        return "\n".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "content": self.content,
            "tags": list(self.tags),
            "source": self.source,
            "session_id": self.session_id,
            "importance": self.importance,
            "confidence": self.confidence,
            "pinned": self.pinned,
            "archived": self.archived,
            "revision": self.revision,
            "access_count": self.access_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "accessed_at": self.accessed_at,
            "score": round(self.score, 4),
            "reason": self.reason,
            # 记忆维度补强：易变性 / 过期日 / 主题键
            "volatility": self.volatility,
            "expires_at": self.expires_at,
            "topic_key": self.topic_key,
        }

    @staticmethod
    def from_row(row: Any) -> "MemoryItem":
        d = dict(row)
        try:
            tags = json.loads(d.get("tags") or "[]")
        except ValueError:
            tags = []
        return MemoryItem(
            id=d.get("id", ""),
            scope=d.get("scope", "global"),
            kind=d.get("kind", KIND_FACT),
            title=d.get("title", ""),
            description=d.get("description", ""),
            content=d.get("content", ""),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            source=d.get("source", ""),
            session_id=d.get("session_id"),
            importance=float(d.get("importance") or 0.5),
            confidence=float(d.get("confidence") or 1.0),
            pinned=bool(d.get("pinned")),
            archived=bool(d.get("archived")),
            revision=int(d.get("revision") or 1),
            access_count=int(d.get("access_count") or 0),
            created_at=float(d.get("created_at") or 0),
            updated_at=float(d.get("updated_at") or 0),
            accessed_at=float(d.get("accessed_at") or 0),
            volatility=str(d.get("volatility") or "stable"),
            expires_at=float(d.get("expires_at") or 0),
            topic_key=str(d.get("topic_key") or ""),
        )


class MemoryManager:
    """分层记忆的统一入口。"""

    def __init__(
        self,
        config: MemoryConfig | None = None,
        *,
        db: Database | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.db = db or get_db()
        self.config = config or MemoryConfig()
        self._embedder = embedder
        # 工作记忆（内存）
        self._working: dict[str, list[Message]] = {}
        self._notes: dict[str, list[str]] = {}

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(
                provider=self.config.embedding_provider,
                model=self.config.embedding_model,
                dim=self.config.embedding_dim,
                manager=getattr(self, "manager_ref", None),
                local_only=not self.config.use_vector,
            )
        return self._embedder

    def bind_config_manager(self, manager: Any) -> None:
        """注入配置管理器（用于解析 embedding 供应商）。"""
        self.manager_ref = manager
        if self._embedder is not None:
            self._embedder.manager = manager

    def update_config(self, config: MemoryConfig) -> None:
        self.config = config

    # ================= 工作记忆 =================

    def working_append(self, session_id: str, msg: Message, *, limit: int = 40) -> None:
        buf = self._working.setdefault(session_id, [])
        buf.append(msg)
        if len(buf) > limit:
            del buf[: len(buf) - limit]

    def working_get(self, session_id: str) -> list[Message]:
        return list(self._working.get(session_id, []))

    def working_clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._working.clear()
            self._notes.clear()
        else:
            self._working.pop(session_id, None)
            self._notes.pop(session_id, None)

    def note_add(self, session_id: str, note: str) -> None:
        """在当前会话上贴一张"便签"（下一步必须记住的临时信息）。"""
        buf = self._notes.setdefault(session_id, [])
        if note and note not in buf:
            buf.append(note)
        if len(buf) > 24:
            del buf[: len(buf) - 24]

    def notes(self, session_id: str) -> list[str]:
        return list(self._notes.get(session_id, []))

    # ================= 长期记忆写入 =================

    async def remember(
        self,
        content: str,
        *,
        title: str = "",
        description: str = "",
        kind: str = KIND_FACT,
        tags: list[str] | None = None,
        scope: str = "global",
        session_id: str | None = None,
        source: str = "agent",
        importance: float = 0.6,
        confidence: float = 1.0,
        pinned: bool = False,
        mem_id: str | None = None,
        db_ref: Any = None,
        remember_meta: dict[str, Any] | None = None,
        dedupe: bool = True,
        volatility: str = "stable",
        expires_at: float = 0.0,
        topic_key: str = "",
    ) -> MemoryItem:
        """写入一条记忆（自动去重、生成向量与检索 tokens）。"""
        if not content.strip() and not title.strip():
            raise ValueError("记忆内容不能为空")
        kind = kind if kind in VALID_KINDS else KIND_FACT
        tags = [str(t) for t in (tags or [])]
        volatility = volatility if volatility in VALID_VOLATILITY else "stable"

        # ★ 主题键优先：同一主题只留一个活跃值。
        #   用户先说「时区 +8」后说「改成 +9」时，若两条都留着，召回会相互矛盾。
        #   有 topic_key 就直接覆盖那条旧记忆（保留其 id，历史仍可通过修订记录回溯）。
        if topic_key and not mem_id:
            same = self._find_by_topic(topic_key, scope=scope)
            if same is not None:
                mem_id = same.id

        if dedupe and not mem_id:
            dup = self._find_duplicate(content, scope=scope, kind=kind)
            if dup is not None:
                return await self.remember(
                    content,
                    title=title or dup.title,
                    description=description or dup.description,
                    kind=kind,
                    tags=sorted(set(tags) | set(dup.tags)),
                    scope=scope,
                    session_id=session_id,
                    source=source,
                    importance=max(importance, dup.importance),
                    confidence=confidence,
                    pinned=pinned or dup.pinned,
                    mem_id=dup.id,
                    dedupe=False,
                )

        item = MemoryItem(
            id=mem_id or new_id("m"),
            scope=scope,
            kind=kind,
            title=title or _auto_title(content),
            description=description,
            content=content,
            tags=tags,
            source=source,
            session_id=session_id,
            importance=max(0.0, min(1.0, float(importance))),
            confidence=max(0.0, min(1.0, float(confidence))),
            pinned=bool(pinned),
            volatility=volatility,
            expires_at=float(expires_at or 0),
            topic_key=(topic_key or "").strip(),
        )
        now = time.time()
        exists = self.db.query_one("SELECT revision, created_at FROM memories WHERE id=?", (item.id,))
        if exists:
            item.revision = int(exists["revision"]) + 1
            item.created_at = float(exists["created_at"])
        else:
            item.revision = 1
            item.created_at = now
        item.updated_at = now

        blob: bytes | None = None
        embed_model = ""
        if self.config.long_term_enabled:
            try:
                vec = await self.embedder.embed(item.body)
                blob = pack(vec)
                embed_model = self.embedder.backend
            except Exception:
                blob = None

        tokens = tokenize_for_fts(item.body)
        meta = {"remember_meta": remember_meta} if remember_meta else {}
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO memories(id, scope, kind, title, description, content, tags, source,"
                " session_id, importance, confidence, pinned, archived, revision, access_count,"
                " created_at, updated_at, accessed_at, tokens, embedding, embed_model, meta,"
                " volatility, expires_at, topic_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET scope=excluded.scope, kind=excluded.kind,"
                " title=excluded.title, description=excluded.description, content=excluded.content,"
                " tags=excluded.tags, importance=excluded.importance, confidence=excluded.confidence,"
                " pinned=excluded.pinned, revision=excluded.revision, updated_at=excluded.updated_at,"
                " tokens=excluded.tokens, embedding=excluded.embedding, embed_model=excluded.embed_model,"
                " meta=excluded.meta, volatility=excluded.volatility, expires_at=excluded.expires_at,"
                " topic_key=excluded.topic_key",
                (
                    item.id, item.scope, item.kind, item.title, item.description, item.content,
                    json.dumps(item.tags, ensure_ascii=False), item.source, item.session_id,
                    item.importance, item.confidence, 1 if item.pinned else 0, 0, item.revision,
                    item.access_count, item.created_at, item.updated_at, item.accessed_at,
                    tokens, blob, embed_model, json.dumps(meta, ensure_ascii=False),
                    item.volatility, item.expires_at, item.topic_key,
                ),
            )
            self._fts_upsert(c, item, tokens)
        return item

    def _fts_upsert(self, conn: Any, item: MemoryItem, tokens: str) -> None:
        if not self.db.fts_enabled:
            return
        try:
            conn.execute("DELETE FROM memories_fts WHERE mem_id=?", (item.id,))
            conn.execute(
                "INSERT INTO memories_fts(mem_id, title, body, tokens) VALUES(?,?,?,?)",
                (item.id, item.title, item.content, tokens),
            )
        except Exception:
            pass

    def _find_duplicate(self, content: str, *, scope: str, kind: str) -> MemoryItem | None:
        """按内容查重：归一化后完全相同即视为同一条。

        用 SQL 精确定位而非 Python 遍历（遍历受 LIMIT 限制，数据一多就漏）。
        归一化只做空白折叠与大小写统一，不做语义近似——语义相近应当保留为
        两条不同的记忆，只有"一字不差"才是同一件事。
        """
        norm = " ".join((content or "").split())
        if len(norm) < 4:
            # 太短的内容（如"好的"）信息量不足，不参与去重，避免误合并
            return None
        rows = self.db.query(
            "SELECT * FROM memories WHERE scope=? AND kind=? AND archived=0"
            " AND LENGTH(content) BETWEEN ? AND ?"
            " ORDER BY updated_at DESC LIMIT 50",
            (scope, kind, max(1, len(content) - 40), len(content) + 40),
        )
        low = norm.lower()
        for row in rows:
            existing = MemoryItem.from_row(row)
            if " ".join((existing.content or "").split()).lower() == low:
                return existing
        return None

    def _find_by_topic(self, topic_key: str, *, scope: str) -> MemoryItem | None:
        """按主题键找「同一主题的活跃值」。

        ★ 解决什么问题：用户先后说过「我的时区是 +8」和「我的时区改成 +9」，
        两条都进库就会相互矛盾，召回时不知道信哪条。有了 topic_key，
        同一主题只保留一个活跃值 —— 写入新值时直接覆盖旧的。
        """
        key = (topic_key or "").strip()
        if not key:
            return None
        row = self.db.query_one(
            "SELECT * FROM memories WHERE topic_key=? AND scope=? AND archived=0"
            " ORDER BY updated_at DESC LIMIT 1",
            (key, scope),
        )
        return MemoryItem.from_row(row) if row else None

    # ================= 检索 =================

    async def recall(
        self,
        query: str,
        *,
        top_k: int | None = None,
        kinds: list[str] | None = None,
        scope: str | None = None,
        session_id: str | None = None,
        min_score: float | None = None,
        include_archived: bool = False,
    ) -> list[MemoryItem]:
        """混合召回：全文 + 向量，返回按综合分排序的记忆。"""
        if not self.config.enabled or not query.strip():
            return []
        cfg = self.config
        k = int(top_k or cfg.recall_top_k or 6)
        threshold = cfg.recall_min_score if min_score is None else min_score

        # ---- 全文召回 ----
        fulltext_scores: dict[str, float] = {}
        match = build_match_query(query)
        if match and self.db.fts_enabled:
            try:
                rows = self.db.query(
                    "SELECT mem_id, bm25(memories_fts) AS rank FROM memories_fts"
                    " WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, k * 6),
                )
                for r in rows:
                    # bm25 越小越相关，转成 0-1 的分数
                    fulltext_scores[r["mem_id"]] = 1.0 / (1.0 + abs(float(r["rank"])))
            except Exception:
                fulltext_scores = {}
        if not fulltext_scores:
            # 降级：LIKE 关键词
            toks = [t for t in tokenize_for_fts(query).split() if len(t) >= 2][:6]
            for tok in toks:
                rows = self.db.query(
                    "SELECT id FROM memories WHERE content LIKE ? OR title LIKE ? LIMIT ?",
                    (f"%{tok}%", f"%{tok}%", k * 4),
                )
                for r in rows:
                    fulltext_scores[r["id"]] = max(fulltext_scores.get(r["id"], 0.0), 0.5)

        # ---- 候选集 ----
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if not include_archived:
            sql += " AND archived=0"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if scope:
            sql += " AND (scope=? OR scope='global')"
            params.append(scope)
        sql += " ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT 1500"
        rows = self.db.query(sql, params)
        if not rows:
            return []
        candidates = [MemoryItem.from_row(r) for r in rows]
        by_id = {c.id: c for c in candidates}

        # ---- 向量召回 ----
        vec_scores: dict[str, float] = {}
        if self.config.use_vector and any(r["embedding"] for r in rows):
            try:
                qvec = await self.embedder.embed(query)
                for r in rows:
                    if not r["embedding"]:
                        continue
                    sim = cosine(qvec, unpack(r["embedding"]))
                    if sim > 0:
                        vec_scores[r["id"]] = sim
            except Exception:
                vec_scores = {}

        # ---- 综合打分 ----
        now = time.time()
        half_life = max(1.0, float(cfg.decay_half_life_days or 45)) * 86400
        results: list[MemoryItem] = []
        for cid, item in by_id.items():
            ft = fulltext_scores.get(cid, 0.0)
            vs = vec_scores.get(cid, 0.0)
            if ft <= 0 and vs <= 0:
                # 无任何检索命中：仅当被置顶/高重要度时才考虑
                if not (item.pinned or item.importance >= 0.85):
                    continue
            age = max(0.0, now - (item.updated_at or item.created_at))
            freshness = math.exp(-age / half_life)
            importance = item.importance
            pin_bonus = 0.35 if item.pinned else 0.0
            access_bonus = min(0.15, 0.02 * math.log1p(item.access_count))
            kind_bonus = 0.06 if item.kind == KIND_PREFERENCE else 0.0
            scope_bonus = 0.05 if session_id and item.session_id == session_id else 0.0
            # ★ 易变性调制：易变的事实更依赖「新鲜度」，永久的则几乎不受影响。
            #   为什么：用户半年前说的「我在用 Python 3.11」可能早过期，
            #   而「我偏好简洁回复」放一年也成立 —— 两者不该用同一套衰减。
            if item.volatility == "volatile":
                freshness *= 2.0      # 放大新鲜度权重（相对看更新近的）
            elif item.volatility == "permanent":
                freshness = max(freshness, 0.5)   # 永久事实不因时间被压到很低
            # ★ 过期降权：已过期的记忆不删除（用户可能还要看），但明显降权，
            #   免得它继续冒充「当前的真相」。
            if item.expires_at and now > item.expires_at:
                freshness *= 0.25
            score = (
                0.42 * ft
                + 0.34 * vs
                + 0.14 * importance
                + 0.10 * freshness
                + pin_bonus
                + access_bonus
                + kind_bonus
                + scope_bonus
            ) * item.confidence
            item.score = score
            item.reason = _reason(ft, vs, importance, freshness, item)
            results.append(item)

        results.sort(key=lambda x: (-x.score, -x.updated_at))
        picked = [r for r in results if r.score >= threshold][:k]
        if not picked and results:
            picked = results[: min(3, len(results))]
        if picked:
            self._mark_accessed([p.id for p in picked])
        return picked

    def _mark_accessed(self, ids: list[str]) -> None:
        if not ids:
            return
        now = time.time()
        try:
            with self.db.transaction() as c:
                for i in ids:
                    c.execute(
                        "UPDATE memories SET access_count=access_count+1, accessed_at=? WHERE id=?",
                        (now, i),
                    )
        except Exception:
            pass

    # ================= 管理 =================

    def get(self, mem_id: str) -> MemoryItem | None:
        row = self.db.query_one("SELECT * FROM memories WHERE id=?", (mem_id,))
        return MemoryItem.from_row(row) if row else None

    def list(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        limit: int = 200,
        offset: int = 0,
        include_archived: bool = False,
        search: str | None = None,
        sort: str = "updated",
    ) -> list[MemoryItem]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if not include_archived:
            sql += " AND archived=0"
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if search:
            sql += " AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
            params.extend([f"%{search}%"] * 3)
        order = {
            "updated": "updated_at DESC",
            "created": "created_at DESC",
            "importance": "importance DESC, updated_at DESC",
            "accessed": "access_count DESC, accessed_at DESC",
        }.get(sort, "updated_at DESC")
        sql += f" ORDER BY pinned DESC, {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [MemoryItem.from_row(r) for r in self.db.query(sql, params)]

    def update(self, mem_id: str, **fields: Any) -> MemoryItem | None:
        allowed = {"title", "description", "content", "kind", "scope", "importance", "confidence",
                   "pinned", "archived", "tags", "source"}
        # ★ 改之前先快照旧值：这样「历史」里看到的就是「改之前长什么样」，
        #   撤回时直接把它写回去（记忆改了能回退」）。
        self._snapshot(mem_id, reason="update")
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "tags":
                v = json.dumps([str(t) for t in (v or [])], ensure_ascii=False)
            elif k in ("pinned", "archived"):
                v = 1 if v else 0
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return self.get(mem_id)
        sets.append("updated_at=?")
        sets.append("revision=revision+1")
        params.extend([time.time(), mem_id])
        self.db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", params)
        item = self.get(mem_id)
        if item is not None:
            self._reindex(item)
        return item

    def _reindex(self, item: MemoryItem) -> None:
        tokens = tokenize_for_fts(item.body)
        self.db.execute("UPDATE memories SET tokens=? WHERE id=?", (tokens, item.id))
        if self.db.fts_enabled:
            try:
                with self.db.transaction() as c:
                    self._fts_upsert(c, item, tokens)
            except Exception:
                pass

    # ---- 修订历史（可回退 / 撤回）--------------------------------------
    def _snapshot(self, mem_id: str, *, reason: str = "") -> None:
        """把该记忆的**当前值**存成一条修订记录（写入新值之前调用）。

        ★ 存旧值而不是新值：这样「历史」列表天然就是「改之前长什么样」，
        回退时直接取某条快照写回去即可，语义最直观。
        """
        try:
            row = self.db.query_one("SELECT * FROM memories WHERE id=?", (mem_id,))
            if not row:
                return
            self.db.execute(
                "INSERT INTO memory_revisions(mem_id, revision, scope, kind, title, description,"
                " content, tags, importance, confidence, pinned, archived, reason, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mem_id, int(row["revision"]), row["scope"], row["kind"], row["title"],
                    row["description"], row["content"], row["tags"], float(row["importance"]),
                    float(row["confidence"]), int(row["pinned"]), int(row["archived"]),
                    reason, time.time(),
                ),
            )
        except Exception:
            # 快照失败不能阻断主流程（记忆写入本身更重要）
            pass

    def revisions(self, mem_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """取某条记忆的修订历史（新的在前）。"""
        rows = self.db.query(
            "SELECT * FROM memory_revisions WHERE mem_id=? ORDER BY revision DESC, id DESC LIMIT ?",
            (mem_id, int(limit)),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except Exception:
                d["tags"] = []
            out.append(d)
        return out

    def restore(self, mem_id: str, revision: int | None = None) -> MemoryItem | None:
        """把记忆回退到某个修订版本（不传 revision 则回退到上一版）。

        返回回退后的记忆；找不到目标版本或记忆不存在则返回 None。
        """
        cur = self.db.query_one("SELECT revision FROM memories WHERE id=?", (mem_id,))
        if not cur:
            return None
        target = int(revision) if revision is not None else int(cur["revision"]) - 1
        if target < 1:
            return None
        row = self.db.query_one(
            "SELECT * FROM memory_revisions WHERE mem_id=? AND revision=? ORDER BY id DESC LIMIT 1",
            (mem_id, target),
        )
        if not row:
            return None
        # 回退前先把「当前值」也存一份，保证回退本身也能被再回退
        self._snapshot(mem_id, reason=f"回退到 r{target} 前的自动备份")
        now = time.time()
        with self.db.transaction() as c:
            c.execute(
                "UPDATE memories SET scope=?, kind=?, title=?, description=?, content=?, tags=?,"
                " importance=?, confidence=?, pinned=?, archived=?, revision=revision+1,"
                " updated_at=? WHERE id=?",
                (
                    row["scope"], row["kind"], row["title"], row["description"], row["content"],
                    row["tags"], float(row["importance"]), float(row["confidence"]),
                    int(row["pinned"]), int(row["archived"]), now, mem_id,
                ),
            )
        item = self.get(mem_id)
        if item is not None:
            self._reindex(item)
        return item

    def forget(self, mem_id: str) -> bool:
        ok = self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,)).rowcount > 0
        if ok and self.db.fts_enabled:
            try:
                self.db.execute("DELETE FROM memories_fts WHERE mem_id=?", (mem_id,))
            except Exception:
                pass
        return ok

    def archive(self, mem_id: str, archived: bool = True) -> bool:
        cur = self.db.execute(
            "UPDATE memories SET archived=?, updated_at=? WHERE id=?",
            (1 if archived else 0, time.time(), mem_id),
        )
        return cur.rowcount > 0

    def stats(self) -> dict[str, Any]:
        total = int(self.db.scalar("SELECT COUNT(*) FROM memories", default=0) or 0)
        by_kind = {
            r["kind"]: int(r["n"])
            for r in self.db.query("SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind")
        }
        by_scope = {
            r["scope"]: int(r["n"])
            for r in self.db.query("SELECT scope, COUNT(*) AS n FROM memories GROUP BY scope")
        }
        return {
            "total": total,
            "active": int(self.db.scalar("SELECT COUNT(*) FROM memories WHERE archived=0", default=0) or 0),
            "pinned": int(self.db.scalar("SELECT COUNT(*) FROM memories WHERE pinned=1", default=0) or 0),
            "by_kind": by_kind,
            "by_scope": by_scope,
            "fts": self.db.fts_enabled,
            "embedding_backend": self.embedder.backend if self.config.use_vector else "disabled",
            "working_sessions": len(self._working),
        }

    def reindex_all(self) -> int:
        """重建全部 FTS 索引（换分词策略后使用）。"""
        n = 0
        for row in self.db.query("SELECT * FROM memories"):
            item = MemoryItem.from_row(row)
            self._reindex(item)
            n += 1
        return n

    async def forget_old(self, *, days: float = 180, keep_important: bool = True) -> int:
        cutoff = time.time() - days * 86400
        sql = "SELECT id, importance, pinned FROM memories WHERE updated_at < ? AND archived=0"
        ids = [
            r["id"] for r in self.db.query(sql, (cutoff,))
            if not (keep_important and (r["pinned"] or float(r["importance"]) >= 0.8))
        ]
        for i in ids:
            self.forget(i)
        return len(ids)

    # ================= 用户画像 =================

    def profile_get(self, key: str | None = None) -> dict[str, Any] | str | None:
        rows = self.db.query(
            "SELECT * FROM memories WHERE kind=? AND archived=0 ORDER BY updated_at DESC", (KIND_PROFILE,)
        )
        data: dict[str, Any] = {}
        for r in rows:
            item = MemoryItem.from_row(r)
            data[item.title or item.id] = item.content
        if key:
            return data.get(key)
        return data

    async def profile_set(self, key: str, value: str, *, session_id: str | None = None) -> MemoryItem:
        """设置用户画像中的一项（同名覆盖）。"""
        existing = self.db.query_one(
            "SELECT id FROM memories WHERE kind=? AND title=? AND scope='global' LIMIT 1",
            (KIND_PROFILE, key),
        )
        return await self.remember(
            f"{key}：{value}",
            title=key,
            kind=KIND_PROFILE,
            scope="global",
            session_id=session_id,
            source="profile",
            importance=0.9,
            pinned=True,
            mem_id=existing["id"] if existing else None,
            dedupe=False,
        )

    # ================= 供提示词使用 =================

    async def context_block(
        self,
        query: str,
        *,
        session_id: str | None = None,
        max_tokens: int | None = None,
        top_k: int | None = None,
    ) -> str:
        """生成注入系统提示的记忆片段。"""
        items = await self.recall(query, session_id=session_id, top_k=top_k)
        if not items:
            return ""
        # ★ 单条正文字数 / 整段预算都改成读配置（默认 1200 字 / 3200 token）。
        #   旧实现两条都写死（`truncate(it.content, 400)`、max_tokens=1600）：
        #   400 字常常只够写半条经验，模型照着半截去用；而且单条一放宽，
        #   写死的预算又会让第一条把钱花光、后面一条都进不去 —— 两者必须一起调。
        body_chars = max(200, int(getattr(self.config, "recall_body_chars", 1200) or 1200))
        budget = int(max_tokens if max_tokens is not None
                     else (getattr(self.config, "recall_budget_tokens", 3200) or 3200))
        lines = ["<memory>", "以下是与当前话题相关的历史记忆（仅作提示，可能过时，必要时先核实）："]
        for it in items:
            block = f"· [{_kind_label(it.kind)}] {it.title or _auto_title(it.content)}"
            if it.description:
                block += f"（{it.description}）"
            body = truncate(it.content, body_chars)
            block += f"\n  {body}"
            cost = estimate_tokens(block)
            if cost > budget:
                break
            budget -= cost
            lines.append(block)
        lines.append("</memory>")
        return "\n".join(lines) if len(lines) > 3 else ""

    def working_block(self, session_id: str) -> str:
        notes = self.notes(session_id)
        if not notes:
            return ""
        return "<notes>\n当前会话便签：\n" + "\n".join(f"· {n}" for n in notes) + "\n</notes>"


def _auto_title(content: str) -> str:
    first = " ".join((content or "").split())
    return truncate(first, 40)


def _kind_label(kind: str) -> str:
    return {
        KIND_FACT: "事实",
        KIND_PREFERENCE: "偏好",
        KIND_EPISODE: "经历",
        KIND_PROFILE: "画像",
        KIND_TASK: "任务",
        KIND_DECISION: "决策",
    }.get(kind, kind)


def _reason(ft: float, vs: float, importance: float, freshness: float, item: MemoryItem) -> str:
    parts = []
    if ft > 0.05:
        parts.append(f"文本命中{ft:.2f}")
    if vs > 0.05:
        parts.append(f"语义相近{vs:.2f}")
    if item.pinned:
        parts.append("已置顶")
    if item.importance >= 0.8:
        parts.append("重要")
    return "、".join(parts) or "相关"


# ---- 全局单例 ------------------------------------------------------------

_memory: MemoryManager | None = None


def get_memory(config: MemoryConfig | None = None) -> MemoryManager:
    global _memory
    if _memory is None or (config is not None and _memory.config is not config):
        _memory = MemoryManager(config)
    return _memory


def reset_memory() -> None:
    global _memory
    _memory = None


__all__ = [
    "MemoryManager",
    "MemoryItem",
    "get_memory",
    "reset_memory",
    "KIND_FACT",
    "KIND_PREFERENCE",
    "KIND_EPISODE",
    "KIND_PROFILE",
    "KIND_TASK",
    "KIND_DECISION",
    "VALID_KINDS",
]
