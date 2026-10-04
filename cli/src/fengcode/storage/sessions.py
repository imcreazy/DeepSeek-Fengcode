"""会话与消息存储。"""

from __future__ import annotations

import json
import time
from typing import Any

from ..llm.types import Attachment, Message, Usage
from ..utils import estimate_tokens, new_id, truncate
from .db import Database, get_db


def _att_json(a: Any) -> dict[str, Any]:
    """把 Attachment 转成可落库的 dict。

    ★ 必须**剔除 data(bytes)**：图片原图动辄几 MB，base64 后更大，
      存进 messages.attachments 会立刻把 DB 撑爆、还会拖慢每次会话读取。
      落库只保存 kind/path/url/mime/name —— 图片本体仍在磁盘上，需要时按 path 读。
    """
    d = dict(getattr(a, "__dict__", {}) or {})
    d.pop("data", None)
    return d


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _loads(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


class SessionStore:
    """会话与消息的持久化。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    # ---- 会话 ----------------------------------------------------------
    def create(
        self,
        *,
        title: str = "新对话",
        workspace: str = "",
        model: str | None = None,
        mode: str = "chat",
        parent_id: str | None = None,
        session_id: str | None = None,
        meta: dict | None = None,
    ) -> dict[str, Any]:
        sid = session_id or new_id("s")
        t = time.time()
        self.db.execute(
            "INSERT INTO sessions(id, title, workspace, model, mode, parent_id, created_at, updated_at, meta)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, title, workspace, model, mode, parent_id, t, t, _dumps(meta or {})),
        )
        return self.get(sid)  # type: ignore[return-value]

    def get(self, session_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return self._session_dict(row) if row else None

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_archived: bool = False,
        workspace: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if not include_archived:
            sql += " AND archived=0"
        if workspace:
            sql += " AND workspace=?"
            params.append(workspace)
        if search:
            sql += " AND (title LIKE ? OR id LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [self._session_dict(r) for r in self.db.query(sql, params)]

    def update(self, session_id: str, **fields: Any) -> None:
        allowed = {
            "title", "workspace", "model", "mode", "pinned", "archived",
            "input_tokens", "output_tokens", "cost", "meta", "parent_id",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "meta" and not isinstance(v, str):
                v = _dumps(v)
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(session_id)
        self.db.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", params)

    def touch(self, session_id: str) -> None:
        self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))

    def delete(self, session_id: str) -> bool:
        cur = self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0

    def add_usage(self, session_id: str, usage: Usage, cost: float = 0.0,
                  last_usage: dict[str, Any] | None = None) -> None:
        """累计本会话用量，并同时记录「最后一次调用」的用量快照。

        两个口径不能混用：
          · `input_tokens` / `output_tokens` 是**累计值**（一场对话总共消耗多少），
            用于「会话指标」里的累计 tokens 与费用；
          · `last_usage` 是**最后一次上游调用**的用量，用于「上下文占用」与「命中率」——
            一轮工具循环会把同一份上下文向上游重发十几次，累计值等于把同一份上下文
            重复计数，直接拿它当占用显示会得到一个远大于真实上下文的数字。

        快照写进 `meta.last_usage`（不改表结构，旧数据不受影响）。
        """
        row = self.db.query_one("SELECT meta FROM sessions WHERE id=?", (session_id,))
        meta: dict[str, Any] = {}
        if row and row["meta"]:
            try:
                loaded = _loads(row["meta"], {})
                if isinstance(loaded, dict):
                    meta = loaded
            except Exception:
                meta = {}
        if last_usage:
            meta["last_usage"] = {
                "prompt_tokens": int(last_usage.get("prompt_tokens") or 0),
                "completion_tokens": int(last_usage.get("completion_tokens") or 0),
                "total_tokens": int(last_usage.get("total_tokens") or 0),
                "cached_tokens": int(last_usage.get("cached_tokens") or 0),
                "reasoning_tokens": int(last_usage.get("reasoning_tokens") or 0),
                # ★ 未命中量随快照一起留存：重开会话时命中率仍能按原口径恢复，
                #   不必拿 prompt 反推（反推在不同供应商下口径不一）。
                "cache_miss_tokens": int(last_usage.get("cache_miss_tokens") or 0),
            }
        self.db.execute(
            "UPDATE sessions SET input_tokens=input_tokens+?, output_tokens=output_tokens+?,"
            " cost=cost+?, meta=?, updated_at=? WHERE id=?",
            (usage.prompt_tokens, usage.completion_tokens, cost,
             _dumps(meta), time.time(), session_id),
        )

    def auto_title(self, session_id: str, first_user_text: str) -> None:
        """用首条用户消息生成标题（若还是默认标题）。"""
        row = self.db.query_one("SELECT title FROM sessions WHERE id=?", (session_id,))
        if not row or row["title"] not in ("新对话", "", None):
            return
        t = " ".join((first_user_text or "").split())
        t = truncate(t, 28).strip()
        if t:
            self.update(session_id, title=t)

    def repair_index(self) -> dict[str, int]:
        """★ 1-O：会话索引自愈 —— 元数据坏了就从权威数据（messages）重建。

        侧栏会话索引应当能从权威文件重建，
        元数据损坏（全零/空白）**不该阻塞保存**，更不该让会话在侧栏里消失。

        修复两类问题：
          · `updated_at` 为 0/空 → 用该会话最后一条消息的时间（没有消息就用 created_at）；
          · `title` 为空白 → 用首条用户消息前 28 字（没有就用「（无标题）」）。
        返回 {repaired, scanned}。
        """
        repaired = 0
        sessions = self.db.query("SELECT id, title, created_at, updated_at FROM sessions")
        for s in sessions:
            sid = s["id"]
            sets: dict[str, Any] = {}
            if not s["updated_at"]:
                last = self.db.scalar(
                    "SELECT MAX(created_at) FROM messages WHERE session_id=?", (sid,), None
                )
                sets["updated_at"] = float(last or s["created_at"] or time.time())
            if not (s["title"] or "").strip():
                # ★ 注意：db.query_one 返回 sqlite3.Row，**不支持 .get()** ——
                #   用下标取值并显式处理 None，否则自愈功能一跑就 AttributeError。
                row = self.db.query_one(
                    "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1",
                    (sid,),
                )
                raw = ""
                if row is not None:
                    try:
                        raw = row["content"] or ""
                    except (KeyError, IndexError, TypeError):
                        raw = ""
                txt = truncate(" ".join(str(raw).split()), 28).strip()
                sets["title"] = txt or "（无标题）"
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                self.db.execute(
                    f"UPDATE sessions SET {cols} WHERE id=?", (*sets.values(), sid)
                )
                repaired += 1
        return {"repaired": repaired, "scanned": len(sessions)}

    def _last_usage_from_log(self, session_id: str) -> dict[str, int] | None:
        """从用量日志里取该会话**最近一次真正有输入量**的快照。

        ★ 为什么需要这个修复通道：调用失败（连接断开、上游没发 usage）时曾写入一个
        全 0 的快照覆盖掉真实读数，并随会话落库 —— 于是界面一直显示 0，
        用户「退出重进也一样」。真实的用量其实**一直躺在 usage_log 里**，
        所以这里按 `prompt_tokens > 0` 找回最近一条有效记录，重建快照，让旧会话自愈。
        """
        try:
            row = self.db.query_one(
                "SELECT prompt_tokens, output_tokens, cached_tokens, reasoning_tokens,"
                " cache_miss_tokens FROM usage_log"
                " WHERE session_id=? AND prompt_tokens > 0"
                " ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
        except Exception:
            return None
        if not row:
            return None
        p = int(row["prompt_tokens"] or 0)
        c = int(row["output_tokens"] or 0)
        cached = int(row["cached_tokens"] or 0)
        miss = row["cache_miss_tokens"]
        return {
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": p + c,
            "cached_tokens": cached,
            "reasoning_tokens": int(row["reasoning_tokens"] or 0),
            # 上游没记未命中量时按差值兜底，与别处口径一致
            "cache_miss_tokens": int(miss) if miss is not None else max(0, p - cached),
        }

    @staticmethod
    def _usage_snapshot_empty(lu: Any) -> bool:
        """快照是否「存在但没有任何有效数据」。

        ★ 判据看**字段和**而不是对象是否存在：全 0 的快照在 UI 上会渲染成
        「尚无调用记录」，与「真的没有数据」表现相同，但它其实是一次失败调用留下的脏值。
        """
        if not isinstance(lu, dict):
            return True
        try:
            return not (int(lu.get("prompt_tokens") or 0)
                        or int(lu.get("completion_tokens") or 0)
                        or int(lu.get("total_tokens") or 0))
        except Exception:
            return True

    def _session_dict(self, row) -> dict[str, Any]:
        d = dict(row)
        d["meta"] = _loads(d.get("meta"), {})
        d["pinned"] = bool(d.get("pinned"))
        d["archived"] = bool(d.get("archived"))
        # ★ 旧会话自愈：快照缺失或全 0 时，从用量日志重建真实读数。
        #   为什么放在这里：`_session_dict` 是所有读取路径的共同出口，
        #   在这里补一次，界面无论从哪条路读到会话都能看到正确读数。
        try:
            meta = d["meta"] if isinstance(d["meta"], dict) else {}
            sid = d.get("id") or ""
            if sid and self._usage_snapshot_empty(meta.get("last_usage")):
                rebuilt = self._last_usage_from_log(sid)
                if rebuilt is not None:
                    meta = dict(meta)
                    meta["last_usage"] = rebuilt
                    d["meta"] = meta
                    # 顺手写回库：下次读不必再查日志（失败不影响本次展示）
                    try:
                        self.db.execute(
                            "UPDATE sessions SET meta=? WHERE id=?",
                            (_dumps(meta), sid),
                        )
                    except Exception:
                        pass
        except Exception:
            pass
        return d

    # ---- 消息 ----------------------------------------------------------
    def append(self, session_id: str, msg: Message, *, tokens: int | None = None) -> int:
        t = time.time()
        tk = tokens if tokens is not None else estimate_tokens(msg.content)
        cur = self.db.execute(
            "INSERT INTO messages(session_id, role, content, reasoning, tool_calls, tool_call_id,"
            " tool_name, attachments, meta, tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                msg.role,
                msg.content or "",
                msg.reasoning or "",
                _dumps([tc.to_openai() for tc in msg.tool_calls]),
                msg.tool_call_id,
                msg.tool_name,
                _dumps([_att_json(a) for a in msg.attachments]),
                _dumps(msg.meta or {}),
                tk,
                t,
            ),
        )
        self.touch(session_id)
        return int(cur.lastrowid or 0)

    def append_many(self, session_id: str, msgs: list[Message]) -> None:
        rows = []
        t = time.time()
        for m in msgs:
            rows.append(
                (
                    session_id,
                    m.role,
                    m.content or "",
                    m.reasoning or "",
                    _dumps([tc.to_openai() for tc in m.tool_calls]),
                    m.tool_call_id,
                    m.tool_name,
                    _dumps([_att_json(a) for a in m.attachments]),
                    _dumps(m.meta or {}),
                    estimate_tokens(m.content),
                    t,
                )
            )
        self.db.executemany(
            "INSERT INTO messages(session_id, role, content, reasoning, tool_calls, tool_call_id,"
            " tool_name, attachments, meta, tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.touch(session_id)

    def messages(self, session_id: str, *, limit: int | None = None, after_id: int = 0) -> list[Message]:
        sql = "SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id"
        params: list[Any] = [session_id, after_id]
        if limit:
            sql = (
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=? AND id>? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id"
            )
            params.append(limit)
        rows = self.db.query(sql, params)
        return [self._msg_from_row(r) for r in rows]

    def raw_messages(self, session_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE session_id=? ORDER BY id"
        params: list[Any] = [session_id]
        if limit:
            sql = (
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?)"
                " ORDER BY id"
            )
            params.append(limit)
        out = []
        for r in self.db.query(sql, params):
            d = dict(r)
            d["tool_calls"] = _loads(d.get("tool_calls"), [])
            d["attachments"] = _loads(d.get("attachments"), [])
            d["meta"] = _loads(d.get("meta"), {})
            out.append(d)
        return out

    @staticmethod
    def _msg_from_row(row) -> Message:
        tcs = []
        from ..llm.types import ToolCall

        for tc in _loads(row["tool_calls"], []):
            fn = tc.get("function") or {}
            tcs.append(
                ToolCall(
                    id=tc.get("id") or "",
                    name=fn.get("name") or "",
                    raw_arguments=fn.get("arguments") or "",
                    arguments=ToolCall.parse_arguments(fn.get("arguments") or ""),
                )
            )
        return Message(
            role=row["role"],
            content=row["content"] or "",
            tool_calls=tcs,
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            reasoning=row["reasoning"] or "",
            # ★ 附件必须一起读回来：否则历史里的图片在下一轮模型调用中消失，
            #   模型只记得「用户说过话」，看不到图（实测过这类问题）。
            attachments=[
                Attachment(
                    kind=a.get("kind", "image"),
                    path=a.get("path"),
                    url=a.get("url"),
                    mime=a.get("mime", "image/png"),
                    name=a.get("name", ""),
                )
                for a in _loads(row["attachments"], [])
                if isinstance(a, dict)
            ],
            meta=_loads(row["meta"], {}),
        )

    def message_count(self, session_id: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,), 0))

    def delete_messages_from(self, session_id: str, message_id: int) -> int:
        cur = self.db.execute("DELETE FROM messages WHERE session_id=? AND id>=?", (session_id, message_id))
        return cur.rowcount

    def set_messages(self, session_id: str, msgs: list[Message]) -> None:
        """整体替换会话消息（用于上下文压缩后的写回）。"""
        with self.db.transaction() as c:
            c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        self.append_many(session_id, msgs)

    # ---- 导出 ----------------------------------------------------------
    def export_markdown(self, session_id: str) -> str:
        s = self.get(session_id)
        if not s:
            return ""
        from ..utils import short_time

        lines = [
            f"# {s['title']}",
            "",
            f"> 会话 ID：`{session_id}`  ",
            f"> 创建时间：{short_time(s['created_at'])}  ",
            f"> 模型：{s.get('model') or '—'}  ",
            f"> 导出时间：{short_time()}",
            "",
        ]
        for m in self.raw_messages(session_id):
            role = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}.get(
                m["role"], m["role"]
            )
            if m["role"] == "tool":
                lines.append(f"### 🛠 工具 `{m.get('tool_name') or ''}`")
                lines.append("```")
                lines.append(truncate(m["content"], 4000))
                lines.append("```")
            else:
                lines.append(f"### {role}")
                if m.get("reasoning"):
                    lines.append("")
                    lines.append("<details><summary>思考过程</summary>")
                    lines.append("")
                    lines.append(truncate(m["reasoning"], 4000))
                    lines.append("</details>")
                lines.append("")
                lines.append(m["content"])
            lines.append("")
        return "\n".join(lines)


__all__ = ["SessionStore"]
