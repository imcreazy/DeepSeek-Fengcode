"""事件总线：内核 → 界面（CLI / Web / 桌面端）的单向事件流。

一次对话会产生大量中间事件（思考增量、工具开始/结束、审批请求、任务变更…），
界面通过订阅这些事件实现实时呈现。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .utils import new_id


@dataclass
class Event:
    """一条事件。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: new_id("e"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "data": self.data,
            "session_id": self.session_id,
            "ts": self.ts,
        }


class EventBus:
    """支持多订阅者与历史回放的事件总线。"""

    def __init__(self, history: int = 500) -> None:
        self._subs: list[asyncio.Queue] = []
        self._callbacks: list[Callable[[Event], None]] = []
        self._history: deque[Event] = deque(maxlen=history)
        self._lock = threading.RLock()

    # ---- 发布 ----------------------------------------------------------
    def emit(self, type: str, data: dict[str, Any] | None = None, *, session_id: str | None = None) -> Event:
        ev = Event(type=type, data=data or {}, session_id=session_id)
        with self._lock:
            self._history.append(ev)
            subs = list(self._subs)
            cbs = list(self._callbacks)
        for q in subs:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # 慢消费者：丢最旧的，保证新事件能进
                try:
                    q.get_nowait()
                    q.put_nowait(ev)
                except Exception:
                    pass
        for cb in cbs:
            try:
                cb(ev)
            except Exception:
                pass
        return ev

    # ---- 订阅 ----------------------------------------------------------
    def subscribe(self, maxsize: int = 2000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def on_event(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def off_event(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    # ---- 历史 ----------------------------------------------------------
    def history(self, *, limit: int = 200, session_id: str | None = None,
                types: list[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        if session_id:
            items = [e for e in items if e.session_id == session_id]
        if types:
            items = [e for e in items if e.type in types]
        return [e.to_dict() for e in items[-limit:]]

    def clear(self) -> None:
        with self._lock:
            self._history.clear()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_bus() -> EventBus:
    global _bus
    with _bus_lock:
        if _bus is None:
            _bus = EventBus()
        return _bus


def reset_bus() -> None:
    global _bus
    with _bus_lock:
        _bus = None


# ---- 常用事件名（集中定义，避免拼错）------------------------------------

class Ev:
    TURN_START = "turn.start"
    TURN_END = "turn.end"
    STEP = "step"
    TEXT = "text"
    REASONING = "reasoning"
    TOOL_START = "tool.start"
    TOOL_END = "tool.end"
    # ★ 必须与前端 handleEvent 的 case 名一致（app.js 里是 "tool_delta"，下划线）。
    #   历史坑：这里曾写作 "tool.delta"（点号），而前端只认下划线 ——
    #   SSE 原样输出 ev.type、不做归一化，于是工具参数增量事件被前端**静默丢弃**：
    #   ①「思考结束后要等十几秒才冒出 write_file 卡片」（占位提示永不创建）；
    #   ②「这十几秒里思考计时还在跳」（停表逻辑就在该分支里，分支永不执行）。
    #   改名前请先确认 app.js 已有对应 case。
    TOOL_DELTA = "tool_delta"
    APPROVAL = "approval.request"
    APPROVAL_DONE = "approval.done"
    TASK_UPDATE = "task.update"
    GOAL_UPDATE = "goal.update"
    SUBAGENT_START = "subagent.start"
    SUBAGENT_END = "subagent.end"
    USAGE = "usage"
    DONE = "done"
    ERROR = "error"
    LOG = "log"
    MEMORY = "memory"
    PROGRESS = "progress"
    STATUS = "status"
    NOTIFY = "notify"
    # ★ 工作区写租约的排队播报：告诉界面「本工作区谁在用、你排第几」。
    #   为什么单独一个事件而不是复用 log/status：排队是有状态的过程
    #   （排队中 → 排到了 → 结束），前端要按状态改输入区的提示，
    #   塞进普通日志里既刷屏又拿不到结构化的位置信息。
    #   事件带的是**排队者自己**的 session_id，只有它的界面收得到。
    WORKSPACE_QUEUE = "workspace.queue"


__all__ = ["Event", "EventBus", "get_bus", "reset_bus", "Ev"]
