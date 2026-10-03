"""SQLite 数据库封装：连接、建表、迁移、查询辅助。

设计要点
--------
- WAL 模式 + 外键约束，读写并发友好。
- 中文全文检索：FTS5 的 ``unicode61`` 分词器不切中文，因此我们在写入时
  自己做「中文切单字 + 英文/数字切词」的预处理，存进 token 列；
  查询时同样处理再拼 OR。若 FTS5 不可用，自动降级为 LIKE 检索。
- 向量以 float32 BLOB 存储，检索时在 Python 侧做余弦（数据量可控）。
- 所有时间戳用 float（Unix epoch 秒）。
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from .. import paths

SCHEMA_VERSION = 4   # v4：新增 memory_revisions（记忆修订历史，支持撤回）

DDL_STATEMENTS: list[str] = [
    # ---- 会话与消息 ----
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL DEFAULT '',
        workspace   TEXT NOT NULL DEFAULT '',
        model       TEXT,
        mode        TEXT NOT NULL DEFAULT 'chat',
        parent_id   TEXT,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        pinned      INTEGER NOT NULL DEFAULT 0,
        archived    INTEGER NOT NULL DEFAULT 0,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost          REAL NOT NULL DEFAULT 0,
        meta        TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   TEXT NOT NULL,
        role         TEXT NOT NULL,
        content      TEXT NOT NULL DEFAULT '',
        reasoning    TEXT NOT NULL DEFAULT '',
        tool_calls   TEXT NOT NULL DEFAULT '[]',
        tool_call_id TEXT,
        tool_name    TEXT,
        attachments  TEXT NOT NULL DEFAULT '[]',
        meta         TEXT NOT NULL DEFAULT '{}',
        tokens       INTEGER NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)",
    # ---- 记忆 ----
    """
    CREATE TABLE IF NOT EXISTS memories (
        id           TEXT PRIMARY KEY,
        scope        TEXT NOT NULL DEFAULT 'global',
        kind         TEXT NOT NULL DEFAULT 'fact',
        title        TEXT NOT NULL DEFAULT '',
        description  TEXT NOT NULL DEFAULT '',
        content      TEXT NOT NULL DEFAULT '',
        tags         TEXT NOT NULL DEFAULT '[]',
        source       TEXT NOT NULL DEFAULT '',
        session_id   TEXT,
        importance   REAL NOT NULL DEFAULT 0.5,
        confidence   REAL NOT NULL DEFAULT 1.0,
        pinned       INTEGER NOT NULL DEFAULT 0,
        archived     INTEGER NOT NULL DEFAULT 0,
        revision     INTEGER NOT NULL DEFAULT 1,
        access_count INTEGER NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL,
        accessed_at  REAL NOT NULL DEFAULT 0,
        tokens       TEXT NOT NULL DEFAULT '',
        embedding    BLOB,
        embed_model  TEXT NOT NULL DEFAULT '',
        meta         TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, archived, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)",
    # ---- 记忆修订历史 ----
    # ★ 为什么要单独一张表：记忆是会被反复改写的长期状态，改错了没法回退、
    #   也看不清「这条以前是什么样」。每次写入前把**旧值**快照存这里，
    #   需要时按 revision 回退即可（撤销 = 把某次快照写回 memories）。
    """
    CREATE TABLE IF NOT EXISTS memory_revisions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mem_id      TEXT NOT NULL,
        revision    INTEGER NOT NULL,
        scope       TEXT NOT NULL DEFAULT 'global',
        kind        TEXT NOT NULL DEFAULT 'fact',
        title       TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        content     TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '[]',
        importance  REAL NOT NULL DEFAULT 0.5,
        confidence  REAL NOT NULL DEFAULT 1.0,
        pinned      INTEGER NOT NULL DEFAULT 0,
        archived    INTEGER NOT NULL DEFAULT 0,
        reason      TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memrev_mem ON memory_revisions(mem_id, revision DESC)",
    # ---- 任务 / 目标 ----
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id          TEXT PRIMARY KEY,
        session_id  TEXT,
        title       TEXT NOT NULL,
        detail      TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'pending',
        priority    INTEGER NOT NULL DEFAULT 0,
        position    INTEGER NOT NULL DEFAULT 0,
        owner       TEXT NOT NULL DEFAULT 'main',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        meta        TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id, position)",
    """
    CREATE TABLE IF NOT EXISTS goals (
        id            TEXT PRIMARY KEY,
        session_id    TEXT,
        objective     TEXT NOT NULL,
        phase         TEXT NOT NULL DEFAULT 'active',
        rounds        INTEGER NOT NULL DEFAULT 0,
        max_rounds    INTEGER,
        blocker       TEXT NOT NULL DEFAULT '',
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL,
        meta          TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # ---- 用量 / 审计 ----
    """
    CREATE TABLE IF NOT EXISTS usage_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            REAL NOT NULL,
        session_id    TEXT,
        provider      TEXT NOT NULL DEFAULT '',
        model         TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cost          REAL NOT NULL DEFAULT 0,
        currency      TEXT NOT NULL DEFAULT '¥',
        duration      REAL NOT NULL DEFAULT 0,
        kind          TEXT NOT NULL DEFAULT 'chat',
        error         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_log(provider, model)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           REAL NOT NULL,
        session_id   TEXT,
        actor        TEXT NOT NULL DEFAULT 'agent',
        action       TEXT NOT NULL DEFAULT '',
        target       TEXT NOT NULL DEFAULT '',
        decision     TEXT NOT NULL DEFAULT 'allow',
        reason       TEXT NOT NULL DEFAULT '',
        detail       TEXT NOT NULL DEFAULT '',
        ok           INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC)",
    # ---- 扩展：插件 / 技能 / 工作流 / 定时 ----
    """
    CREATE TABLE IF NOT EXISTS plugins (
        name         TEXT PRIMARY KEY,
        version      TEXT NOT NULL DEFAULT '0.0.0',
        dir          TEXT NOT NULL DEFAULT '',
        enabled      INTEGER NOT NULL DEFAULT 0,
        builtin      INTEGER NOT NULL DEFAULT 0,
        manifest     TEXT NOT NULL DEFAULT '{}',
        installed_at REAL NOT NULL,
        updated_at   REAL NOT NULL,
        error        TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skills_state (
        name         TEXT PRIMARY KEY,
        path         TEXT NOT NULL DEFAULT '',
        enabled      INTEGER NOT NULL DEFAULT 1,
        source       TEXT NOT NULL DEFAULT 'builtin',
        use_count    INTEGER NOT NULL DEFAULT 0,
        last_used    REAL NOT NULL DEFAULT 0,
        updated_at   REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflows (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        dag         TEXT NOT NULL DEFAULT '{}',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        cron        TEXT NOT NULL DEFAULT '',
        interval_seconds REAL NOT NULL DEFAULT 0,
        kind        TEXT NOT NULL DEFAULT 'prompt',
        payload     TEXT NOT NULL DEFAULT '{}',
        enabled     INTEGER NOT NULL DEFAULT 1,
        last_run    REAL NOT NULL DEFAULT 0,
        last_status TEXT NOT NULL DEFAULT '',
        next_run    REAL NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_runs (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id   TEXT NOT NULL,
        ts       REAL NOT NULL,
        status   TEXT NOT NULL DEFAULT '',
        output   TEXT NOT NULL DEFAULT '',
        duration REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_runs ON job_runs(job_id, ts DESC)",
    # ---- 附件 / 键值 ----
    """
    CREATE TABLE IF NOT EXISTS attachments (
        id         TEXT PRIMARY KEY,
        session_id TEXT,
        name       TEXT NOT NULL DEFAULT '',
        path       TEXT NOT NULL DEFAULT '',
        mime       TEXT NOT NULL DEFAULT '',
        size       INTEGER NOT NULL DEFAULT 0,
        kind       TEXT NOT NULL DEFAULT 'file',
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        k          TEXT PRIMARY KEY,
        v          TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL DEFAULT '',
        path        TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        is_default  INTEGER NOT NULL DEFAULT 0,
        meta        TEXT NOT NULL DEFAULT '{}'
    )
    """,
]

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    mem_id UNINDEXED,
    title,
    body,
    tokens,
    tokenize='unicode61 remove_diacritics 2'
)
"""

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]*|\d+(?:\.\d+)?")


def tokenize_for_fts(text: str) -> str:
    """把文本转成 FTS 友好的 token 串。

    中文按**单字 + 相邻双字**输出，英文/数字按词输出。
    这样 unicode61 分词器也能对中文做有效检索。
    """
    if not text:
        return ""
    text = text.lower()
    out: list[str] = []
    # 英文/数字整词
    for m in _WORD_RE.finditer(text):
        w = m.group(0)
        if len(w) > 1:
            out.append(w)
    # 中文单字与双字
    cjk_chars = _CJK_RE.findall(text)
    out.extend(cjk_chars)
    # 连续中文片段生成双字
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", text):
        for i in range(len(run) - 1):
            out.append(run[i : i + 2])
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return " ".join(uniq)


def build_match_query(query: str, *, mode: str = "or") -> str:
    """构造 FTS5 MATCH 表达式。"""
    toks = tokenize_for_fts(query).split()
    if not toks:
        return ""
    # 转义双引号
    safe = ['"' + t.replace('"', '""') + '"' for t in toks[:24]]
    joiner = " OR " if mode == "or" else " AND "
    return joiner.join(safe)


class Database:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: os.PathLike | str | None = None) -> None:
        self.path = Path(path) if path else paths.db_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._fts = False
        self._init_schema()

    # ---- 连接 ----------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            with contextlib.suppress(Exception):
                c.close()
            self._local.conn = None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            c = self.conn
            try:
                c.execute("BEGIN")
                yield c
                c.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    c.execute("ROLLBACK")
                raise

    # ---- 基础操作 ------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._write_lock:
            self.conn.executemany(sql, seq)
            self.conn.commit()

    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        return row[0]

    @staticmethod
    def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
        return [dict(r) for r in rows]

    # ---- 建表与迁移 ----------------------------------------------------
    def _init_schema(self) -> None:
        with self._write_lock:
            c = self.conn
            for ddl in DDL_STATEMENTS:
                c.execute(ddl)
            c.commit()
            # FTS5
            try:
                c.execute(FTS_DDL)
                c.commit()
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
            self._migrate()
            c.execute(
                "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(SCHEMA_VERSION),),
            )
            c.commit()

    @property
    def fts_enabled(self) -> bool:
        return self._fts

    def _migrate(self) -> None:
        """为旧库补齐后加的列。"""
        c = self.conn
        want = {
            "sessions": {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "cost": "REAL NOT NULL DEFAULT 0",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "pinned": "INTEGER NOT NULL DEFAULT 0",
                "mode": "TEXT NOT NULL DEFAULT 'chat'",
                "parent_id": "TEXT",
            },
            "memories": {
                "tokens": "TEXT NOT NULL DEFAULT ''",
                "embedding": "BLOB",
                "embed_model": "TEXT NOT NULL DEFAULT ''",
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "confidence": "REAL NOT NULL DEFAULT 1.0",
                "accessed_at": "REAL NOT NULL DEFAULT 0",
                "access_count": "INTEGER NOT NULL DEFAULT 0",
                # ★ 记忆维度补强：易变性 / 过期日 / 主题键
                #   topic_key 用于「同一主题只留一个活跃值」，防止两条矛盾记忆同时召回。
                "volatility": "TEXT NOT NULL DEFAULT 'stable'",
                "expires_at": "REAL NOT NULL DEFAULT 0",
                "topic_key": "TEXT NOT NULL DEFAULT ''",
            },
            "usage_log": {
                "cached_tokens": "INTEGER NOT NULL DEFAULT 0",
                "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
                "kind": "TEXT NOT NULL DEFAULT 'chat'",
            },
            "jobs": {
                "last_status": "TEXT NOT NULL DEFAULT ''",
                "next_run": "REAL NOT NULL DEFAULT 0",
            },
        }
        for table, cols in want.items():
            try:
                existing = {
                    r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.OperationalError:
                continue
            for col, decl in cols.items():
                if col not in existing:
                    with contextlib.suppress(sqlite3.OperationalError):
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ---- 统计 ----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": str(self.path), "fts": self._fts}
        for table in (
            "sessions",
            "messages",
            "memories",
            "tasks",
            "usage_log",
            "audit_log",
            "plugins",
            "workflows",
            "jobs",
        ):
            try:
                out[table] = self.scalar(f"SELECT COUNT(*) FROM {table}", default=0)
            except sqlite3.OperationalError:
                out[table] = 0
        try:
            out["db_size"] = self.path.stat().st_size
        except OSError:
            out["db_size"] = 0
        return out

    def vacuum(self) -> None:
        with self._write_lock:
            self.conn.execute("VACUUM")
            self.conn.commit()


_db: Database | None = None
_db_lock = threading.Lock()


def get_db(path: os.PathLike | str | None = None) -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database(path)
        return _db


def reset_db() -> None:
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
        _db = None


def now() -> float:
    return time.time()


__all__ = [
    "Database",
    "get_db",
    "reset_db",
    "tokenize_for_fts",
    "build_match_query",
    "SCHEMA_VERSION",
]
