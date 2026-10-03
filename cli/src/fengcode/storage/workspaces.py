"""工作区存储。

一个「工作区」= 一个名字 + 一个目录。会话归属于某个工作区，
切换工作区时会话列表跟着变 —— 这样可以同时在不同目录里干活。

默认工作区指向应用数据目录下的 workspace/，用户也可以新建指向任意目录。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import paths
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


class WorkspaceStore:
    """工作区的持久化。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    # ---- 读 ------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        """列出全部工作区（默认工作区排最前，其余按更新时间倒序）。"""
        rows = self.db.query(
            "SELECT * FROM workspaces ORDER BY is_default DESC, updated_at DESC"
        )
        out = [self._row_to_dict(r) for r in rows]
        if not out:
            out = [self.ensure_default()]
        return out

    def get(self, ws_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM workspaces WHERE id=?", (ws_id,))
        return self._row_to_dict(row) if row else None

    def find_by_path(self, path: str) -> dict[str, Any] | None:
        if not path:
            return None
        row = self.db.query_one(
            "SELECT * FROM workspaces WHERE path=?", (str(path),)
        )
        return self._row_to_dict(row) if row else None

    def find_by_name(self, name: str) -> dict[str, Any] | None:
        if not name:
            return None
        row = self.db.query_one(
            "SELECT * FROM workspaces WHERE name=?", (str(name),)
        )
        return self._row_to_dict(row) if row else None

    def default(self) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM workspaces WHERE is_default=1 ORDER BY created_at LIMIT 1"
        )
        return self._row_to_dict(row) if row else self.ensure_default()

    def ensure_default(self) -> dict[str, Any]:
        """保证默认工作区存在，返回它。"""
        existing = self.db.query_one(
            "SELECT * FROM workspaces WHERE is_default=1 ORDER BY created_at LIMIT 1"
        )
        if existing:
            return self._row_to_dict(existing)
        ws_dir = paths.workspace_dir()
        try:
            ws_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return self.create(name="默认工作区", path=str(ws_dir), is_default=True)

    # ---- 写 ------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        path: str = "",
        is_default: bool = False,
        meta: dict | None = None,
    ) -> dict[str, Any]:
        ws_id = new_id("w")
        t = time.time()
        name = (name or "").strip() or "未命名工作区"
        # 同名则加序号，避免混淆
        base, n = name, 2
        while self.find_by_name(name):
            name = f"{base} {n}"
            n += 1
        self.db.execute(
            "INSERT INTO workspaces(id, name, path, created_at, updated_at, is_default, meta)"
            " VALUES(?,?,?,?,?,?,?)",
            (ws_id, name, str(path or ""), t, t, 1 if is_default else 0,
             _dumps(meta or {})),
        )
        return {
            "id": ws_id, "name": name, "path": str(path or ""),
            "created_at": t, "updated_at": t, "is_default": bool(is_default),
            "meta": meta or {},
        }

    def update(self, ws_id: str, **fields: Any) -> bool:
        allowed = {"name", "path", "is_default", "meta"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "meta":
                v = _dumps(v or {})
            elif k == "is_default":
                v = 1 if v else 0
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at=?")
        vals.append(time.time())
        vals.append(ws_id)
        self.db.execute(f"UPDATE workspaces SET {', '.join(sets)} WHERE id=?", tuple(vals))
        return True

    def touch(self, ws_id: str) -> None:
        """标记工作区最近使用过（用于排序）。"""
        with_suppress = False
        try:
            self.db.execute(
                "UPDATE workspaces SET updated_at=? WHERE id=?",
                (time.time(), ws_id),
            )
        except Exception:
            pass

    def delete(self, ws_id: str) -> bool:
        """删除工作区（默认工作区不可删）。不删磁盘上的文件。"""
        ws = self.get(ws_id)
        if not ws or ws.get("is_default"):
            return False
        self.db.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
        return True

    def rename(self, ws_id: str, name: str) -> bool:
        return self.update(ws_id, name=(name or "").strip() or "未命名工作区")

    # ---- 转换 ----------------------------------------------------------
    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        d = dict(row)
        d["is_default"] = bool(d.get("is_default"))
        d["meta"] = _loads(d.get("meta"), {})
        return d

    # ---- 便捷：目录信息 -------------------------------------------------
    def stat(self, ws_id: str) -> dict[str, Any]:
        """工作区目录的基本信息（文件数、是否可写）。"""
        ws = self.get(ws_id)
        if not ws:
            return {"ok": False, "error": "工作区不存在"}
        p = Path(ws.get("path") or "")
        info: dict[str, Any] = {
            "ok": True,
            "id": ws["id"],
            "name": ws["name"],
            "path": str(p),
            "exists": p.is_dir(),
            "writable": False,
            "entries": 0,
        }
        if info["exists"]:
            try:
                info["entries"] = sum(1 for _ in p.iterdir())
            except Exception:
                pass
            try:
                probe = p / ".fengcode-write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                info["writable"] = True
            except Exception:
                info["writable"] = False
        return info


_store: WorkspaceStore | None = None


def get_workspaces() -> WorkspaceStore:
    global _store
    if _store is None:
        _store = WorkspaceStore()
    return _store


def reset_workspaces() -> None:
    global _store
    _store = None
