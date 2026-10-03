"""用量统计与成本核算，以及审计日志落库。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from ..llm.types import Usage
from ..utils import new_id, scrub_secrets
from .db import Database, get_db


class StatsStore:
    """token / 费用 / 耗时的记录与聚合。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()
        # ★ 2-R：overview 的短时缓存。它一次要跑 10 来个聚合查询（today/24h/week/
        #   month/all/by_model/by_day/by_hour/tools…），而面板每次打开、每次点
        #   会话都会调一次 —— 用户侧表现为「点开用量面板要等」。用量数据按秒级
        #   变化，缓存 8 秒既省掉重复聚合，又不会让读数看着过期。
        self._ov_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._ov_ttl = 8.0

    def record(
        self,
        *,
        provider: str,
        model: str,
        usage: Usage,
        cost: float = 0.0,
        currency: str = "¥",
        duration: float = 0.0,
        session_id: str | None = None,
        kind: str = "chat",
        error: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO usage_log(ts, session_id, provider, model, prompt_tokens, output_tokens,"
            " cached_tokens, reasoning_tokens, cache_miss_tokens, cost, currency, duration, kind, error)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                session_id,
                provider,
                model,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.cached_tokens,
                usage.reasoning_tokens,
                # ★ 未命中量随之落库，命中率才能按「命中 /（命中 + 未命中）」核账
                getattr(usage, "cache_miss_tokens", 0),
                cost,
                currency,
                duration,
                kind,
                scrub_secrets(error or "")[:800] if error else None,
            ),
        )

    # ---- 聚合 ----------------------------------------------------------
    def summary(self, *, since: float | None = None,
                session_id: str | None = None) -> dict[str, Any]:
        where, params = self._where(since, session_id)
        row = self.db.query_one(
            f"SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS pt,"
            f" COALESCE(SUM(output_tokens),0) AS ot, COALESCE(SUM(cached_tokens),0) AS ct,"
            f" COALESCE(SUM(cache_miss_tokens),0) AS cm,"
            f" COALESCE(SUM(reasoning_tokens),0) AS rt, COALESCE(SUM(cost),0) AS cost,"
            f" COALESCE(SUM(duration),0) AS dur FROM usage_log {where}",
            params,
        )
        d = dict(row) if row else {}
        calls = int(d.get("calls") or 0)
        return {
            "calls": calls,
            "prompt_tokens": int(d.get("pt") or 0),
            "output_tokens": int(d.get("ot") or 0),
            "cached_tokens": int(d.get("ct") or 0),
            "cache_miss_tokens": int(d.get("cm") or 0),
            "reasoning_tokens": int(d.get("rt") or 0),
            "total_tokens": int(d.get("pt") or 0) + int(d.get("ot") or 0),
            "cost": round(float(d.get("cost") or 0), 6),
            "duration": round(float(d.get("dur") or 0), 3),
        }

    def by_model(self, *, since: float | None = None, limit: int = 50,
                 session_id: str | None = None) -> list[dict[str, Any]]:
        where, params = self._where(since, session_id)
        rows = self.db.query(
            f"SELECT provider, model, COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS pt,"
            f" COALESCE(SUM(output_tokens),0) AS ot, COALESCE(SUM(cost),0) AS cost,"
            f" COALESCE(SUM(duration),0) AS dur, MAX(currency) AS currency"
            f" FROM usage_log {where} GROUP BY provider, model ORDER BY cost DESC, calls DESC LIMIT ?",
            params + [limit],
        )
        return [
            {
                "provider": r["provider"],
                "model": r["model"],
                "calls": int(r["calls"]),
                "prompt_tokens": int(r["pt"]),
                "output_tokens": int(r["ot"]),
                "total_tokens": int(r["pt"]) + int(r["ot"]),
                "cost": round(float(r["cost"]), 6),
                "currency": r["currency"] or "¥",
                "avg_duration": round(float(r["dur"]) / max(1, int(r["calls"])), 3),
            }
            for r in rows
        ]

    def by_day(self, *, days: int = 14) -> list[dict[str, Any]]:
        start = (datetime.now() - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = self.db.query(
            "SELECT ts, prompt_tokens, output_tokens, cost, duration FROM usage_log WHERE ts>=?",
            (start.timestamp(),),
        )
        buckets: dict[str, dict[str, Any]] = {}
        for i in range(days):
            key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            buckets[key] = {"date": key, "calls": 0, "prompt_tokens": 0, "output_tokens": 0,
                            "total_tokens": 0, "cost": 0.0}
        for r in rows:
            key = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
            b = buckets.get(key)
            if not b:
                continue
            b["calls"] += 1
            b["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            b["output_tokens"] += int(r["output_tokens"] or 0)
            b["total_tokens"] = b["prompt_tokens"] + b["output_tokens"]
            b["cost"] = round(b["cost"] + float(r["cost"] or 0), 6)
        return list(buckets.values())

    def by_hour(self, *, hours: int = 24) -> list[dict[str, Any]]:
        start = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
        rows = self.db.query(
            "SELECT ts, prompt_tokens, output_tokens, cost FROM usage_log WHERE ts>=?",
            (start.timestamp(),),
        )
        buckets: dict[str, dict[str, Any]] = {}
        for i in range(hours):
            key = (start + timedelta(hours=i)).strftime("%m-%d %H:00")
            buckets[key] = {"hour": key, "calls": 0, "total_tokens": 0, "cost": 0.0}
        for r in rows:
            key = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:00")
            b = buckets.get(key)
            if not b:
                continue
            b["calls"] += 1
            b["total_tokens"] += int(r["prompt_tokens"] or 0) + int(r["output_tokens"] or 0)
            b["cost"] = round(b["cost"] + float(r["cost"] or 0), 6)
        return list(buckets.values())

    def by_kind(self, *, since: float | None = None,
                session_id: str | None = None) -> list[dict[str, Any]]:
        """按来源（kind）统计用量。

        ★ 为什么要看来源：用户想知道「这些钱花在哪了」—— 正常对话、子代理、
        压缩摘要、定时任务各自花了多少。kind 字段本来就记着，只是此前没人聚合它。
        """
        where, params = self._where(since, session_id)
        rows = self.db.query(
            "SELECT kind, COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS pt,"
            " COALESCE(SUM(output_tokens),0) AS ot, COALESCE(SUM(cost),0) AS cost,"
            " MAX(currency) AS currency"
            f" FROM usage_log {where} GROUP BY kind ORDER BY cost DESC, calls DESC",
            params,
        )
        return [
            {
                "kind": r["kind"] or "chat",
                "calls": int(r["calls"]),
                "prompt_tokens": int(r["pt"]),
                "output_tokens": int(r["ot"]),
                "total_tokens": int(r["pt"]) + int(r["ot"]),
                "cost": round(float(r["cost"]), 6),
                "currency": r["currency"] or "¥",
            }
            for r in rows
        ]

    def heatmap(self, *, days: int = 35) -> dict[str, Any]:
        """日历热力图数据：每天一个格子，值用 token 数。

        返回 ``{days: [{date, total_tokens, calls, cost, level}], max, total_calls}``。
        `level` 是 0-4 的分档，方便界面直接上色而不再自己算阈值。
        """
        rows = self.by_day(days=days)
        mx = max([int(r.get("total_tokens") or 0) for r in rows] or [0])
        out = []
        for r in rows:
            tok = int(r.get("total_tokens") or 0)
            if tok <= 0:
                lvl = 0
            elif mx <= 0:
                lvl = 1
            else:
                ratio = tok / mx
                lvl = 1 if ratio <= 0.25 else 2 if ratio <= 0.5 else 3 if ratio <= 0.75 else 4
            out.append({
                "date": r.get("date") or "",
                "total_tokens": tok,
                "calls": int(r.get("calls") or 0),
                "cost": round(float(r.get("cost") or 0), 6),
                "level": lvl,
            })
        return {"days": out, "max": mx,
                "total_calls": sum(int(r.get("calls") or 0) for r in rows)}

    def trend(self, *, days: int = 14) -> dict[str, Any]:
        """趋势：与「上一个同样长的周期」对比，给出涨跌。

        为什么这样比：单看一根柱子说不出「今天是不是比平常多」，
        和上一周期对比才有判断依据（用户看的是趋势，不是绝对数）。
        """
        cur = self.by_day(days=days)
        prev_rows = self._by_day_range(offset_days=days, days=days)
        cur_tok = sum(int(r.get("total_tokens") or 0) for r in cur)
        cur_cost = sum(float(r.get("cost") or 0) for r in cur)
        cur_calls = sum(int(r.get("calls") or 0) for r in cur)
        prev_tok = sum(int(r.get("total_tokens") or 0) for r in prev_rows)
        prev_cost = sum(float(r.get("cost") or 0) for r in prev_rows)
        prev_calls = sum(int(r.get("calls") or 0) for r in prev_rows)

        def _pct(a: float, b: float) -> float | None:
            if b <= 0:
                return None if a <= 0 else 100.0
            return round((a - b) / b * 100, 1)

        return {
            "days": days,
            "current": {"total_tokens": cur_tok, "cost": round(cur_cost, 6), "calls": cur_calls},
            "previous": {"total_tokens": prev_tok, "cost": round(prev_cost, 6), "calls": prev_calls},
            "change_pct": {
                "total_tokens": _pct(cur_tok, prev_tok),
                "cost": _pct(cur_cost, prev_cost),
                "calls": _pct(cur_calls, prev_calls),
            },
        }

    def _by_day_range(self, *, offset_days: int, days: int) -> list[dict[str, Any]]:
        """取「往前 offset_days 天」那一整段（用于趋势对比）。"""
        end = (datetime.now() - timedelta(days=offset_days)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        start = (end - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = self.db.query(
            "SELECT ts, prompt_tokens, output_tokens, cost FROM usage_log WHERE ts>=? AND ts<=?",
            (start.timestamp(), end.timestamp()),
        )
        buckets: dict[str, dict[str, Any]] = {}
        for i in range(days):
            key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            buckets[key] = {"date": key, "calls": 0, "prompt_tokens": 0, "output_tokens": 0,
                            "total_tokens": 0, "cost": 0.0}
        for r in rows:
            key = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
            b = buckets.get(key)
            if not b:
                continue
            b["calls"] += 1
            b["prompt_tokens"] += int(r["prompt_tokens"] or 0)
            b["output_tokens"] += int(r["output_tokens"] or 0)
            b["total_tokens"] = b["prompt_tokens"] + b["output_tokens"]
            b["cost"] = round(b["cost"] + float(r["cost"] or 0), 6)
        return list(buckets.values())

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM usage_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def overview(self, session_id: str | None = None) -> dict[str, Any]:
        # ★ 2-R：短时缓存 —— 面板反复打开/切会话时不再重复跑同一批聚合查询。
        key = session_id or ""
        c = getattr(self, "_ov_cache", None)
        if c is not None:
            hit = c.get(key)
            if hit and (time.time() - hit[0]) < getattr(self, "_ov_ttl", 8.0):
                return hit[1]
        out = self._overview_uncached(session_id)
        if c is not None:
            c[key] = (time.time(), out)
            # 缓存别无限增长（会话切换多了会累积）
            if len(c) > 32:
                oldest = sorted(c.items(), key=lambda kv: kv[1][0])[:16]
                for k, _ in oldest:
                    c.pop(k, None)
        return out

    def _overview_uncached(self, session_id: str | None = None) -> dict[str, Any]:
        now = time.time()
        day_ago = now - 86400
        week_ago = now - 7 * 86400
        month_ago = now - 30 * 86400
        out = {
            "today": self.summary(since=self._day_start()),
            "last_24h": self.summary(since=day_ago),
            "week": self.summary(since=week_ago),
            "month": self.summary(since=month_ago),
            "all": self.summary(),
            "by_model": self.by_model(since=month_ago, limit=20),
            "by_day": self.by_day(days=14),
            "by_hour": self.by_hour(hours=24),
            "tools": self.tool_stats(since=month_ago, limit=12),
            # ★ 用量统计面板：来源分布 / 热力图 / 趋势对比
            "by_kind": self.by_kind(since=month_ago),
            "heatmap": self.heatmap(days=35),
            "trend": self.trend(days=14),
        }
        # ★ 当前会话维度：界面上的「累计 tokens / 请求数 / 费用」应该是这个会话的，
        #   而不是全库历史总和 —— 否则用户会把自己会话的用量误读成整个安装的量。
        if session_id:
            out["session"] = self.summary(session_id=session_id)
            out["session_by_model"] = self.by_model(session_id=session_id, limit=20)
        return out

    def tool_stats(self, *, since: float | None = None, limit: int = 12) -> list[dict[str, Any]]:
        """按工具名统计调用次数与成功率（取自审计日志的 tool 类记录）。

        审计表里工具调用记成 action='tool'，target 是工具名，ok 表示是否成功。
        表可能不存在（老库），一律容错返回空列表。
        """
        try:
            conds, params = self._conds(since)
            conds.append("action='tool' AND target IS NOT NULL AND target<>''")
            where = "WHERE " + " AND ".join(conds)
            rows = self.db.query(
                "SELECT target AS name, COUNT(*) AS calls,"
                " COALESCE(SUM(CASE WHEN ok THEN 1 ELSE 0 END),0) AS ok"
                f" FROM audit_log {where} GROUP BY target ORDER BY calls DESC LIMIT ?",
                (*params, limit),
            )
            return [{"name": r["name"], "calls": int(r["calls"] or 0),
                     "ok": int(r["ok"] or 0)} for r in rows]
        except Exception:
            return []

    @staticmethod
    def _day_start() -> float:
        dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return dt.timestamp()

    @staticmethod
    def _since_clause(since: float | None) -> tuple[str, list[Any]]:
        if since is None:
            return "", []
        return "WHERE ts>=?", [since]

    @staticmethod
    def _conds(since: float | None = None,
               session_id: str | None = None) -> tuple[list[str], list[Any]]:
        """把过滤条件拆成「不带 WHERE 关键字的条件列表 + 参数」。

        ★ 为什么不让各处自己拼 "WHERE ..."：审计表的工具统计曾把时间条件
        （本身已带 WHERE）直接 `AND` 到另一个 WHERE 后面，拼出
        `... AND WHERE ts>=?` 这种语法错误，被 except 吞掉后工具统计**永远为空**。
        统一成条件列表后由这里拼装，就不会再出这种拼接错。
        """
        conds: list[str] = []
        params: list[Any] = []
        if since is not None:
            conds.append("ts>=?")
            params.append(since)
        if session_id:
            conds.append("session_id=?")
            params.append(session_id)
        return conds, params

    @classmethod
    def _where(cls, since: float | None = None,
               session_id: str | None = None) -> tuple[str, list[Any]]:
        conds, params = cls._conds(since, session_id)
        return ("WHERE " + " AND ".join(conds), params) if conds else ("", [])

    def purge(self, before: float) -> int:
        cur = self.db.execute("DELETE FROM usage_log WHERE ts<?", (before,))
        return cur.rowcount


class AuditStore:
    """审计日志：记录每一次工具调用与审批决定。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    def log(
        self,
        *,
        action: str,
        target: str = "",
        decision: str = "allow",
        reason: str = "",
        detail: str = "",
        session_id: str | None = None,
        actor: str = "agent",
        ok: bool = True,
    ) -> None:
        self.db.execute(
            "INSERT INTO audit_log(ts, session_id, actor, action, target, decision, reason, detail, ok)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                session_id,
                actor,
                action,
                target[:500],
                decision,
                reason[:500],
                scrub_secrets(detail or "")[:8000],
                1 if ok else 0,
            ),
        )

    def recent(self, limit: int = 100, *, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            rows = self.db.query(
                "SELECT * FROM audit_log WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def count(self, since: float | None = None) -> int:
        if since:
            return int(self.db.scalar("SELECT COUNT(*) FROM audit_log WHERE ts>=?", (since,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM audit_log", default=0))

    def purge(self, before: float) -> int:
        cur = self.db.execute("DELETE FROM audit_log WHERE ts<?", (before,))
        return cur.rowcount


__all__ = ["StatsStore", "AuditStore"]
