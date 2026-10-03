"""任务清单、目标、键值、工作流、插件与技能状态、定时任务的持久化。"""

from __future__ import annotations

import json
import time
from typing import Any

from ..utils import new_id
from .db import Database, get_db


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _loads(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


class TaskStore:
    """待办清单（todo）与跨轮目标（goal）。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    # ---- 任务 ----------------------------------------------------------
    def add(self, title: str, *, detail: str = "", session_id: str | None = None,
            priority: int = 0, owner: str = "main", task_id: str | None = None) -> dict[str, Any]:
        tid = task_id or new_id("t")
        pos = int(self.db.scalar(
            "SELECT COALESCE(MAX(position),0)+1 FROM tasks WHERE session_id IS ?",
            (session_id,), 1,
        ) or 1)
        t = time.time()
        self.db.execute(
            "INSERT INTO tasks(id, session_id, title, detail, status, priority, position, owner,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, session_id, title, detail, "pending", priority, pos, owner, t, t),
        )
        return self.get(tid)  # type: ignore[return-value]

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return dict(row) if row else None

    def list(self, *, session_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id IS ?"
            params.append(session_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY position, created_at"
        return [dict(r) for r in self.db.query(sql, params)]

    def update(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"title", "detail", "status", "priority", "position", "owner", "meta"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            params.append(_dumps(v) if k == "meta" and not isinstance(v, str) else v)
        if not sets:
            return self.get(task_id)
        sets.append("updated_at=?")
        params.extend([time.time(), task_id])
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
        return self.get(task_id)

    def set_status(self, task_id: str, status: str) -> bool:
        cur = self.db.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, time.time(), task_id)
        )
        return cur.rowcount > 0

    def delete(self, task_id: str) -> bool:
        return self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount > 0

    def clear(self, session_id: str | None = None, *, only_done: bool = False) -> int:
        if only_done:
            sql = "DELETE FROM tasks WHERE status='completed'"
            params: list[Any] = []
            if session_id is not None:
                sql += " AND session_id IS ?"
                params.append(session_id)
        elif session_id is None:
            sql = "DELETE FROM tasks"
            params = []
        else:
            sql = "DELETE FROM tasks WHERE session_id IS ?"
            params = [session_id]
        return self.db.execute(sql, params).rowcount

    def summary(self, session_id: str | None = None) -> dict[str, int]:
        rows = self.db.query(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE session_id IS ? GROUP BY status",
            (session_id,),
        )
        out = {"pending": 0, "in_progress": 0, "completed": 0, "blocked": 0, "cancelled": 0}
        for r in rows:
            out[r["status"]] = int(r["n"])
        out["total"] = sum(out.values())
        return out

    def render(self, session_id: str | None = None) -> str:
        items = self.list(session_id=session_id)
        if not items:
            return "（任务清单为空）"
        marks = {
            "pending": "[ ]",
            "in_progress": "[~]",
            "completed": "[x]",
            "blocked": "[!]",
            "cancelled": "[-]",
        }
        lines = []
        for i, t in enumerate(items, 1):
            mark = marks.get(t["status"], "[ ]")
            line = f"{mark} {i}. {t['title']}"
            if t.get("detail"):
                line += f"\n      {t['detail'][:160]}"
            lines.append(line)
        s = self.summary(session_id)
        lines.append(f"\n共 {s['total']} 项：完成 {s['completed']}，进行中 {s['in_progress']}，待办 {s['pending']}")
        return "\n".join(lines)

    # ---- 目标 ----------------------------------------------------------
    def set_goal(self, objective: str, *, session_id: str | None = None,
                 max_rounds: int | None = None, goal_id: str | None = None,
                 phase: str = "active") -> dict[str, Any]:
        t = time.time()
        gid = goal_id or new_id("g")
        exist = self.get_goal_for(session_id)
        if exist and not goal_id:
            self.db.execute(
                "UPDATE goals SET objective=?, phase=?, max_rounds=?, updated_at=? WHERE id=?",
                (objective, phase, max_rounds, t, exist["id"]),
            )
            return self.get_goal(exist["id"])  # type: ignore[return-value]
        self.db.execute(
            "INSERT INTO goals(id, session_id, objective, phase, rounds, max_rounds, created_at, updated_at)"
            " VALUES(?,?,?,?,0,?,?,?)",
            (gid, session_id, objective, phase, max_rounds, t, t),
        )
        return self.get_goal(gid)  # type: ignore[return-value]

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM goals WHERE id=?", (goal_id,))
        return dict(row) if row else None

    def get_goal_for(self, session_id: str | None) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM goals WHERE session_id IS ? AND phase='active' ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        )
        return dict(row) if row else None

    def list_goals(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM goals"
        if active_only:
            sql += " WHERE phase='active'"
        sql += " ORDER BY updated_at DESC"
        return [dict(r) for r in self.db.query(sql)]

    def update_goal(self, goal_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"objective", "phase", "rounds", "max_rounds", "blocker", "meta"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            params.append(_dumps(v) if k == "meta" and not isinstance(v, str) else v)
        if not sets:
            return self.get_goal(goal_id)
        sets.append("updated_at=?")
        params.extend([time.time(), goal_id])
        self.db.execute(f"UPDATE goals SET {', '.join(sets)} WHERE id=?", params)
        return self.get_goal(goal_id)

    def bump_round(self, goal_id: str) -> None:
        self.db.execute(
            "UPDATE goals SET rounds=rounds+1, updated_at=? WHERE id=?", (time.time(), goal_id)
        )


class KVStore:
    """简单键值存储（设置项、界面状态等）。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT v FROM kv WHERE k=?", (key,))
        if row is None:
            return default
        return _loads(row["v"], default)

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv(k, v, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, _dumps(value), time.time()),
        )

    def delete(self, key: str) -> bool:
        return self.db.execute("DELETE FROM kv WHERE k=?", (key,)).rowcount > 0

    def all(self) -> dict[str, Any]:
        return {r["k"]: _loads(r["v"], None) for r in self.db.query("SELECT k, v FROM kv")}

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT v FROM meta WHERE k=?", (key,))
        return row["v"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )


class WorkflowStore:
    """工作流（DAG）持久化。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    def save(self, name: str, dag: dict, *, description: str = "",
             workflow_id: str | None = None) -> dict[str, Any]:
        wid = workflow_id or new_id("wf")
        t = time.time()
        exist = self.get(wid)
        if exist:
            self.db.execute(
                "UPDATE workflows SET name=?, description=?, dag=?, updated_at=? WHERE id=?",
                (name, description, _dumps(dag), t, wid),
            )
        else:
            self.db.execute(
                "INSERT INTO workflows(id, name, description, dag, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (wid, name, description, _dumps(dag), t, t),
            )
        return self.get(wid)  # type: ignore[return-value]

    def get(self, workflow_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row:
            return None
        d = dict(row)
        d["dag"] = _loads(d.get("dag"), {})
        return d

    def list(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("SELECT * FROM workflows ORDER BY updated_at DESC"):
            d = dict(r)
            d["dag"] = _loads(d.get("dag"), {})
            out.append(d)
        return out

    def delete(self, workflow_id: str) -> bool:
        return self.db.execute("DELETE FROM workflows WHERE id=?", (workflow_id,)).rowcount > 0


class PluginStore:
    """插件安装状态。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    def upsert(self, name: str, *, version: str = "0.0.0", dir: str = "", enabled: bool = False,
               builtin: bool = False, manifest: dict | None = None, error: str = "") -> None:
        t = time.time()
        self.db.execute(
            "INSERT INTO plugins(name, version, dir, enabled, builtin, manifest, installed_at, updated_at, error)"
            " VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET version=excluded.version, dir=excluded.dir,"
            " enabled=excluded.enabled, builtin=excluded.builtin, manifest=excluded.manifest,"
            " updated_at=excluded.updated_at, error=excluded.error",
            (name, version, dir, 1 if enabled else 0, 1 if builtin else 0,
             _dumps(manifest or {}), t, t, error),
        )

    def get(self, name: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM plugins WHERE name=?", (name,))
        if not row:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["builtin"] = bool(d["builtin"])
        d["manifest"] = _loads(d.get("manifest"), {})
        return d

    def list(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("SELECT * FROM plugins ORDER BY builtin DESC, name"):
            d = dict(r)
            d["enabled"] = bool(d["enabled"])
            d["builtin"] = bool(d["builtin"])
            d["manifest"] = _loads(d.get("manifest"), {})
            out.append(d)
        return out

    def set_enabled(self, name: str, enabled: bool) -> bool:
        cur = self.db.execute(
            "UPDATE plugins SET enabled=?, updated_at=? WHERE name=?",
            (1 if enabled else 0, time.time(), name),
        )
        return cur.rowcount > 0

    def delete(self, name: str) -> bool:
        return self.db.execute("DELETE FROM plugins WHERE name=?", (name,)).rowcount > 0


class SkillStore:
    """技能启用状态与使用统计。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    def sync(self, name: str, path: str, *, source: str = "user", enabled: bool = True) -> None:
        self.db.execute(
            "INSERT INTO skills_state(name, path, enabled, source, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET path=excluded.path, source=excluded.source,"
            " updated_at=excluded.updated_at",
            (name, path, 1 if enabled else 0, source, time.time()),
        )

    def get(self, name: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM skills_state WHERE name=?", (name,))
        if not row:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return d

    def list(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("SELECT * FROM skills_state ORDER BY source, name"):
            d = dict(row)
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def set_enabled(self, name: str, enabled: bool) -> bool:
        cur = self.db.execute(
            "UPDATE skills_state SET enabled=?, updated_at=? WHERE name=?",
            (1 if enabled else 0, time.time(), name),
        )
        return cur.rowcount > 0

    def mark_used(self, name: str) -> None:
        self.db.execute(
            "UPDATE skills_state SET use_count=use_count+1, last_used=? WHERE name=?",
            (time.time(), name),
        )

    def delete(self, name: str) -> bool:
        return self.db.execute("DELETE FROM skills_state WHERE name=?", (name,)).rowcount > 0


class JobStore:
    """定时任务持久化。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    def save(self, *, name: str, cron: str = "", interval_seconds: float = 0,
             kind: str = "prompt", payload: dict | None = None, enabled: bool = True,
             job_id: str | None = None, last_run: float = 0, next_run: float = 0) -> dict[str, Any]:
        jid = job_id or new_id("j")
        t = time.time()
        exist = self.get(jid)
        if exist:
            self.db.execute(
                "UPDATE jobs SET name=?, cron=?, interval_seconds=?, kind=?, payload=?, enabled=?,"
                " next_run=?, updated_at=? WHERE id=?",
                (name, cron, float(interval_seconds), kind, _dumps(payload or {}),
                 1 if enabled else 0, next_run, t, jid),
            )
        else:
            self.db.execute(
                "INSERT INTO jobs(id, name, cron, interval_seconds, kind, payload, enabled,"
                " last_run, next_run, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (jid, name, cron, float(interval_seconds), kind, _dumps(payload or {}),
                 1 if enabled else 0, last_run, next_run, t, t),
            )
        return self.get(jid)  # type: ignore[return-value]

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            return None
        d = dict(row)
        d["payload"] = _loads(d.get("payload"), {})
        d["enabled"] = bool(d["enabled"])
        return d

    def list(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name"
        out = []
        for r in self.db.query(sql):
            d = dict(r)
            d["payload"] = _loads(d.get("payload"), {})
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def delete(self, job_id: str) -> bool:
        self.db.execute("DELETE FROM job_runs WHERE job_id=?", (job_id,))
        return self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,)).rowcount > 0

    def mark_run(self, job_id: str, *, status: str = "ok", output: str = "",
                 duration: float = 0.0, next_run: float = 0.0) -> None:
        t = time.time()
        self.db.execute(
            "UPDATE jobs SET last_run=?, last_status=?, next_run=?, updated_at=? WHERE id=?",
            (t, status, next_run, t, job_id),
        )
        self.db.execute(
            "INSERT INTO job_runs(job_id, ts, status, output, duration) VALUES(?,?,?,?,?)",
            (job_id, t, status, output[:8000], duration),
        )

    def runs(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM job_runs WHERE job_id=? ORDER BY ts DESC LIMIT ?", (job_id, limit)
            )
        ]


class AttachmentStore:
    """附件登记。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    def add(self, *, name: str, path: str, mime: str = "", size: int = 0, kind: str = "file",
            session_id: str | None = None) -> str:
        aid = new_id("att")
        self.db.execute(
            "INSERT INTO attachments(id, session_id, name, path, mime, size, kind, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (aid, session_id, name, path, mime, size, kind, time.time()),
        )
        return aid

    def list(self, *, session_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if session_id:
            rows = self.db.query(
                "SELECT * FROM attachments WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM attachments ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def get(self, aid: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM attachments WHERE id=?", (aid,))
        return dict(row) if row else None

    def delete(self, aid: str) -> bool:
        return self.db.execute("DELETE FROM attachments WHERE id=?", (aid,)).rowcount > 0


__all__ = [
    "TaskStore",
    "KVStore",
    "WorkflowStore",
    "PluginStore",
    "SkillStore",
    "JobStore",
    "AttachmentStore",
]
