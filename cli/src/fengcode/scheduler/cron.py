"""定时任务：cron 表达式与固定间隔两种调度，到点后触发 prompt / 工作流 / Shell / 工具。

调度器运行在服务端的 asyncio 循环里（每 15 秒扫一次），任务定义持久化在 SQLite。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

from ..storage.tasks import JobStore
from ..utils import human_duration, truncate

# ---- cron 解析 -----------------------------------------------------------

_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]  # 分 时 日 月 周


def _parse_field(field: str, lo: int, hi: int) -> set[int] | None:
    """解析一个 cron 字段，返回允许的取值集合；``*`` 返回 None。"""
    field = field.strip()
    if field in ("*", "?"):
        return None
    out: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            try:
                step = max(1, int(s))
            except ValueError:
                step = 1
        if part in ("*", "?"):
            rng = range(lo, hi + 1, step)
        elif "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            rng = range(max(lo, start), min(hi, end) + 1, step)
        else:
            try:
                v = int(part)
            except ValueError:
                continue
            # 支持 */n 与 "5/10" 形式
            if step > 1:
                rng = range(max(lo, v), hi + 1, step)
            else:
                rng = [v]
        for v in rng:
            if lo <= v <= hi:
                out.add(v)
    return out


def parse_cron(expr: str) -> list[set[int] | None] | None:
    """解析 5 段 cron（分 时 日 月 周）；无效返回 None。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    out: list[set[int] | None] = []
    for i, p in enumerate(parts):
        lo, hi = _FIELD_RANGES[i]
        parsed = _parse_field(p, lo, hi)
        if parsed is None and p not in ("*", "?"):
            return None
        out.append(parsed)
    return out


def cron_matches(cron: list[set[int] | None], when: time.struct_time) -> bool:
    """判断某时刻是否命中 cron。星期：cron 0=周日，Python tm_wday 0=周一。"""
    minute, hour, dom, month, dow = cron
    if minute is not None and when.tm_min not in minute:
        return False
    if hour is not None and when.tm_hour not in hour:
        return False
    if month is not None and when.tm_mon not in month:
        return False
    # 周：cron 的 0/7 都是周日
    py_dow = (when.tm_wday + 1) % 7
    if dow is not None and py_wday_set(py_dow) & dow == set():
        return False
    if dom is not None and dow is None and when.tm_mday not in dom:
        return False
    if dom is not None and dow is not None:
        # 两者都指定：任一命中即可（标准 cron 语义）
        if when.tm_mday not in dom and not (py_wday_set(py_dow) & dow):
            return False
    return True


def py_wday_set(d: int) -> set[int]:
    return {d, 7} if d == 0 else {d}


def next_run_time(cron: list[set[int] | None], after: float | None = None, *, limit_days: int = 400) -> float:
    """计算下一次命中时间（最多往后找 limit_days 天）。"""
    import calendar

    t = time.localtime(after or time.time())
    start = calendar.timegm(t) - time.timezone
    minute0 = int((start // 60 + 1) * 60)
    for i in range(limit_days * 24 * 60):
        ts = minute0 + i * 60
        if cron_matches(cron, time.localtime(ts)):
            return float(ts)
    return 0.0


# ---- 调度器 --------------------------------------------------------------

class Scheduler:
    """后台定时任务调度器。"""

    def __init__(
        self,
        *,
        store: JobStore,
        runner: Callable[[dict], Any] | None = None,
        bus: Any = None,
        tick: float = 15.0,
    ) -> None:
        self.store = store
        self.runner = runner
        self.bus = bus
        self.tick = tick
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.running_jobs: set[str] = set()
        self.last_tick: float = 0.0

    def _emit(self, type: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            try:
                self.bus.emit(type, data)
            except Exception:
                pass

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        self._emit("scheduler.start", {"tick": self.tick})

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._emit("scheduler.stop", {})

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._emit("scheduler.error", {"error": f"{type(e).__name__}: {e}"})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass

    async def tick_once(self, now: float | None = None) -> list[str]:
        """扫描一次到期任务，返回触发过的任务 ID。"""
        self.last_tick = now or time.time()
        fired: list[str] = []
        for job in self.store.list(enabled_only=True):
            if not self._due(job, self.last_tick):
                continue
            if job["id"] in self.running_jobs:
                continue
            fired.append(job["id"])
            asyncio.create_task(self._run_job(job))
        return fired

    def _due(self, job: dict, now: float) -> bool:
        # ★ 时间窗口：不在允许时段内就一律不触发。
        #   放在最前面判断：窗口外连「算下次」都不必做。
        if not _in_window(job, now):
            return False
        interval = float(job.get("interval_seconds") or 0)
        cron_expr = (job.get("cron") or "").strip()
        next_run = float(job.get("next_run") or 0)
        if interval > 0:
            base = float(job.get("last_run") or 0) or float(job.get("created_at") or 0)
            return now - base >= interval
        if cron_expr:
            parsed = parse_cron(cron_expr)
            if not parsed:
                return False
            if next_run and now >= next_run:
                return True
            # 首次：算下一次并写回
            nxt = next_run_time(parsed, now)
            self.store.save(
                name=job["name"], cron=cron_expr, interval_seconds=job["interval_seconds"],
                kind=job["kind"], payload=job["payload"], enabled=job["enabled"],
                job_id=job["id"], last_run=job["last_run"], next_run=nxt,
            )
            return False
        return False

    async def _run_job(self, job: dict) -> None:
        jid = job["id"]
        self.running_jobs.add(jid)
        t0 = time.time()
        status = "ok"
        output = ""
        try:
            self._emit("scheduler.job.start", {"job": {k: v for k, v in job.items() if k != "payload"}})
            if self.runner is None:
                output = "（没有配置任务执行器）"
                status = "skip"
            else:
                result = self.runner(job)
                if asyncio.iscoroutine(result):
                    result = await result
                output = str(result)[:8000] if result is not None else ""
        except Exception as e:
            status = "error"
            output = f"{type(e).__name__}: {e}"
        finally:
            dt = time.time() - t0
            cron_expr = (job.get("cron") or "").strip()
            nxt = 0.0
            if cron_expr:
                parsed = parse_cron(cron_expr)
                if parsed:
                    nxt = next_run_time(parsed, time.time())
            elif float(job.get("interval_seconds") or 0) > 0:
                nxt = time.time() + float(job["interval_seconds"])
            self.store.mark_run(jid, status=status, output=output, duration=dt, next_run=nxt)
            self.running_jobs.discard(jid)
            self._emit(
                "scheduler.job.end",
                {"job_id": jid, "name": job["name"], "status": status,
                 "duration": dt, "output": truncate(output, 500)},
            )

    async def run_now(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job is None:
            return {"ok": False, "error": f"未找到任务 {job_id}"}
        await self._run_job(job)
        return {"ok": True, "job": self.store.get(job_id)}

    def status(self) -> dict[str, Any]:
        jobs = self.store.list()
        return {
            "running": self._task is not None and not self._task.done(),
            "tick": self.tick,
            "last_tick": self.last_tick,
            "jobs": len(jobs),
            "enabled": sum(1 for j in jobs if j["enabled"]),
            "active": sorted(self.running_jobs),
        }


def describe_schedule(job: dict) -> str:
    """把调度描述成中文。"""
    cron_expr = (job.get("cron") or "").strip()
    interval = float(job.get("interval_seconds") or 0)
    if cron_expr:
        parsed = parse_cron(cron_expr)
        label = _cron_label(parsed) if parsed else "（cron 表达式无效）"
        return f"cron：{cron_expr}　{label}"
    if interval > 0:
        return f"每 {human_duration(interval)}"
    return "（无调度规则）"


def _in_window(job: dict, when: float) -> bool:
    """当前时刻是否落在任务允许的「时间窗口」内。

    ★ 为什么要时间窗口：光有「每 30 分钟」不够用 ——
      用户要的是「每 30 分钟，但只在 9:00-18:00 之间跑」（工作时间），
      或者「每天一次，但避开凌晨维护时段」。
      用 ``window_start`` / ``window_end`` 两个「HH:MM」表达，跨零点自动识别
      （如 22:00-06:00）。
      两个都为空 → 不限制，返回 True。
    """
    start = str(job.get("window_start") or "").strip()
    end = str(job.get("window_end") or "").strip()
    # ★ API 把窗口存在 payload._window 里（jobs 表已有 payload 列，不额外加列）；
    #   这里兜底读取，保证「存进去的窗口」和「判定用的窗口」是同一份。
    if not start and not end:
        try:
            w = (job.get("payload") or {}).get("_window") or {}
            start = str(w.get("start") or "").strip()
            end = str(w.get("end") or "").strip()
        except Exception:
            pass
    if not start and not end:
        return True
    try:
        lt = time.localtime(when)
        now_min = lt.tm_hour * 60 + lt.tm_min

        def _to_min(s: str) -> int | None:
            m = re.match(r"^(\d{1,2}):(\d{2})$", s or "")
            if not m:
                return None
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return h * 60 + mi
            return None

        s_min = _to_min(start)
        e_min = _to_min(end)
        if s_min is None and e_min is None:
            return True
        if s_min is None:
            return now_min <= e_min      # 只给了结束 → 从 00:00 起
        if e_min is None:
            return now_min >= s_min      # 只给了开始 → 到 24:00 止
        if s_min <= e_min:
            return s_min <= now_min <= e_min
        # 跨零点（如 22:00-06:00）
        return now_min >= s_min or now_min <= e_min
    except Exception:
        return True


def next_run_describe(job: dict, *, now: float | None = None) -> str:
    """给出「下次什么时候跑」的中文说明。

    ★ 为什么单列一个函数：光写「每 30 分钟」用户仍不知道「下一次具体几点」，
      尤其叠加时间窗口后更难心算。这里直接算出下次时刻并写成「还有 12 分」。
    """
    now = now or time.time()
    nxt = float(job.get("next_run") or 0)
    if nxt <= 0:
        # 没有记录就现算一次（不写库，纯展示）
        cron_expr = (job.get("cron") or "").strip()
        interval = float(job.get("interval_seconds") or 0)
        if cron_expr:
            parsed = parse_cron(cron_expr)
            if parsed:
                nxt = next_run_time(parsed, now)
        elif interval > 0:
            base = float(job.get("last_run") or 0) or float(job.get("created_at") or 0) or now
            nxt = base + interval
            while nxt <= now:
                nxt += interval
    if nxt <= 0:
        return "—"
    if not job.get("enabled", True):
        return "已停用"
    delta = nxt - now
    when = time.strftime("%m-%d %H:%M", time.localtime(nxt))
    if delta <= 0:
        return f"即将执行（{when}）"
    if delta < 3600:
        return f"{when}（还有 {int(delta // 60)} 分）"
    if delta < 86400:
        return f"{when}（还有 {delta / 3600:.1f} 小时）"
    return f"{when}（还有 {int(delta // 86400)} 天）"


def _cron_label(cron: list[set[int] | None] | None) -> str:
    if not cron:
        return ""
    minute, hour, dom, month, dow = cron
    if minute is not None and len(minute) == 1 and hour is not None and len(hour) == 1:
        m = next(iter(minute))
        h = next(iter(hour))
        base = f"每天 {h:02d}:{m:02d}"
    elif minute is not None and hour is not None and len(hour) == 24 and len(minute) == 1:
        base = f"每小时第 {next(iter(minute))} 分"
    elif minute is not None and len(minute) == 1:
        base = f"每小时第 {next(iter(minute))} 分"
    elif minute is not None and len(minute) > 1:
        base = f"每小时的第 {len(minute)} 个指定分钟"
    else:
        base = "每分钟"
    if dow is not None:
        names = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
        base += "，仅 " + "、".join(names.get(d, str(d)) for d in sorted(dow))
    return base


__all__ = ["Scheduler", "parse_cron", "cron_matches", "next_run_time", "describe_schedule"]
