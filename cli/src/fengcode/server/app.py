"""HTTP + WebSocket 服务端：桌面端、Web UI、CLI 共用的后端。

用纯 starlette（不引入 FastAPI），依赖更少、启动更快。
所有接口挂在 ``/api`` 下；``/ws`` 是事件流；``/`` 托管零构建单页 UI。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re as _re
import time
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from ..config.manager import get_manager
from ..core.agent import Agent
from ..core.prompts import normalize_mode
from ..events import Ev, get_bus
from ..mcp.client import MCPManager
from ..plugins.manager import PluginManager
from ..utils import new_id, scrub_secrets, to_plain, truncate
from ..version import __version__

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(to_plain(data), status_code=status)


def _err(message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


async def _body(request: Any) -> dict[str, Any]:
    """安全读取 JSON body。"""
    try:
        raw = await request.body()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {"_value": data}
    except (ValueError, UnicodeError):
        return {}


def _parse_attachments(raw: Any) -> list[Any]:
    """把前端传来的附件解析成 Attachment 列表（★ 多模态链路的关键一环）。

    前端两种来源：
      · 桌面端/网页版拖入的文件：带 **真实绝对路径**（path），图片另有 dataURL（data）；
      · 网页版没路径时：只有 dataURL。
    `data` 是 `data:image/png;base64,...` 形式，这里解出 bytes 放进 Attachment.data，
    供 openai/anthropic/gemini 客户端转成各自的多模态格式发给模型。

    落库时 `storage.sessions._att_json` 会剔除 data（只留 path），避免把几 MB 的
    图片写进 SQLite。因此这里保留 data 只用于**当次**请求。
    """
    from ..llm.types import Attachment

    out: list[Any] = []
    if not isinstance(raw, list):
        return out
    for a in raw:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("kind") or "file")
        if kind not in ("image", "audio", "file"):
            kind = "file"
        name = str(a.get("name") or "")
        path = str(a.get("path") or "") or None
        mime = str(a.get("mime") or "") or ("image/png" if kind == "image" else "application/octet-stream")
        data: bytes | None = None
        d = a.get("data") or ""
        if isinstance(d, str) and d.startswith("data:") and ";base64," in d:
            import base64

            head, b64 = d.split(";base64,", 1)
            if not mime or mime == "application/octet-stream":
                mime = head[5:] or mime
            try:
                data = base64.b64decode(b64, validate=False)
            except Exception:
                data = None
        out.append(Attachment(kind=kind, path=path, mime=mime, name=name, data=data))
    return out


def _vision_supported(model_ref: str | None) -> bool:
    """当前模型是否支持图片输入（ 门控用）。

    ★ 只有**明确知道该模型不支持**时才拒绝，其余一律放过。
      为什么必须这样：`model_info()` 对没登记过的模型会把 vision 兜底写成 False，
      若照搬它做门控，用户接的第三方模型全都会被判成「不支持图片」，
      本来能用的图片反被本地拦死（本版实测踩到了这个坑）。
    ★ 更进一步：门控的意义是「提前拦住必然失败的请求」，而不是「替用户代管」。
      所以这里只在**双方都明确**时才敢拦：模型必须出现在内置能力表里，
      且该表把 vision 标为 False。只要有一丝不确定（模型未知、没配供应商、
      取不到配置），就放行 —— 让上游按自己的规则处理，用户不会因为本地保守而吃亏。
    """
    try:
        mgr = get_manager()
        ref = model_ref or mgr.default_model_ref()
        if not ref:
            return True                     # 连模型都没定 → 不拦
        provider_name, model_name = mgr.parse_model_ref(ref)
        if not model_name:
            return True                     # 解析不出 → 不拦
        from ..config import catalog as _catalog

        entry = _catalog.lookup(model_name)
        if not entry:
            return True                     # 本地不认识这个模型 → 不拦
        prov = mgr.get_provider(provider_name) if provider_name else None
        if prov is not None and model_name in (prov.vision_models or []):
            return True                     # 供应商明确列为视觉模型
        # 只有在「内置表明确标了 vision=False」时才拒绝
        return bool(entry.get("vision", True))
    except Exception:
        return True


def _check_images(atts: list[Any], *, model_ref: str | None) -> str:
    """图片接入前的两道闸：能力门控 + 超尺寸拦截。

    返回空串表示通过；否则返回给用户看的说明。

    ★ 为什么要门控：给不支持图片的模型发图，上游往往直接 400，
      整轮对话就废了 —— 本地先挡住并说清楚，比事后报错便宜得多。
    ★ 为什么要拦超尺寸：一张几十 MB 的大图会瞬间吃光上下文、甚至撑爆请求，
      毁掉整个会话（对方实测过）。本地先拦下并告知怎么处理。
    """
    images = [a for a in atts if getattr(a, "kind", "") == "image"]
    if not images:
        return ""
    if not _vision_supported(model_ref):
        return "当前模型不支持图片输入，请换一个支持视觉的模型，或去掉图片后再发。"
    # 单图上限 8MB（base64 前）：超过就不往上游发了
    LIMIT = 8 * 1024 * 1024
    for a in images:
        data = getattr(a, "data", None)
        if data is None:
            continue
        if len(data) > LIMIT:
            return (f"图片「{getattr(a, 'name', '') or '未命名'}」约 "
                    f"{len(data) / 1024 / 1024:.1f} MB，超过单图上限 "
                    f"{LIMIT // 1024 // 1024} MB。请压缩后再发，或改用文件路径引用。")
    return ""


def _auth_ok(request: Any) -> bool:
    """校验访问令牌（若配置了）。"""
    mgr = get_manager()
    token = (mgr.config.server.token or "").strip()
    if not token:
        return True
    got = (
        request.headers.get("x-fengcode-token")
        or request.query_params.get("token")
        or ""
    )
    if got == token:
        return True
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") and auth[7:].strip() == token:
        return True
    return False


# --------------------------------------------------------------------------
# 应用状态
# --------------------------------------------------------------------------

class AppState:
    """服务端的共享状态。"""

    def __init__(self) -> None:
        self.manager = get_manager()
        self.bus = get_bus()
        self.agents: dict[str, Agent] = {}
        self.default_agent: Agent | None = None
        self.mcp: MCPManager | None = None
        self.plugins: PluginManager | None = None
        self.access = getattr(self.manager.config, "access", None)
        self.started_at = time.time()
        self._scheduler = None
        self._lock = asyncio.Lock()
        # 待回答的提问：{qid: Future}。ask_user 工具把问题发到界面后在此等用户选择。
        self._pending_asks: dict[str, asyncio.Future] = {}
        # ★ 会话写入租约（轻量版）：同一会话同时只允许一个回合在写。
        #   为什么需要：`agent()` 里的 `_lock` 只保护「创建 Agent」这一步，
        #   建好之后两个请求完全可以同时进入 `ag.run()` —— 两个回合交错往
        #   同一个会话追加消息，历史顺序会乱、上下文统计也会互相覆盖。
        #   这里用「会话 → 持锁者标识」记录当前写者，冲突时**明确报错**而不是静默交错。
        self._session_writers: dict[str, str] = {}
        # ★ 工作区写入租约：同一**目录**同时只允许一个会话在写，其余排队接力。
        #   与会话级租约的区别：同会话并发写没有正确的处理办法（只能拒绝），
        #   而同工作区的两个对话是「接力」关系 —— 用户在 B 对话发消息的本意
        #   就是「等那个跑完接着做」，所以这里等待而不是报错。
        self._ws_writers: dict[str, dict[str, str]] = {}   # 工作区键 → {session_id, holder}
        self._ws_waiters: dict[str, list[dict[str, Any]]] = {}   # 工作区键 → FIFO 队列

    def _make_asker(self, ag: Agent):
        """构造 ask_user 的等待通道：发出问题 → 等界面回填答案。

        ★ 没有这个通道时，`ask_user` 只能返回「当前没有交互通道，无法等待用户回答」，
          界面上什么也不会出现。
        """

        async def _asker(payload: dict[str, Any]) -> str | None:
            qid = str(payload.get("id") or new_id("q"))
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending_asks[qid] = fut
            try:
                # 超时交给 AskTool 自己管（它用 ask_timeout 包了 wait_for），
                # 这里再给一层兜底，避免 future 永久悬挂。
                return await asyncio.wait_for(fut, timeout=900)
            except asyncio.TimeoutError:
                return None
            finally:
                self._pending_asks.pop(qid, None)

        return _asker

    def resolve_ask(self, qid: str, answer: Any) -> bool:
        """界面回填 ask_user 的答案。"""
        fut = self._pending_asks.get(qid)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True

    # ---- Agent ---------------------------------------------------------
    @staticmethod
    def _norm_ws(p: Any) -> str:
        """把工作目录路径归一化后比较（大小写、斜杠、结尾分隔符都不该算不同项目）。"""
        try:
            return str(Path(str(p)).expanduser().resolve()).rstrip("\\/").lower()
        except Exception:
            return str(p or "").rstrip("\\/").lower()

    def session_workspace(self, session_id: str | None) -> str | None:
        """会话所属项目在磁盘上的真实目录。

        ★ 为什么必须有这一步：会话表里的 ``workspace`` 此前**只用于侧栏分组**，
          从没传给 Agent —— 于是不管在哪个项目里新建会话，文件都写进同一个
          默认工作目录（实测症状：在 B 项目里说「建个文件」，它落到默认工作区）。
          工作区隔离要从「会话 → 目录」这一环补齐，后面的写租约才有意义。

        会话里存的是工作区**路径**（前端新建会话时传的就是它）；老数据/手填的
        也可能是项目名，这里按名字回查一次。
        """
        if not session_id:
            return None
        raw = ""
        try:
            from ..storage.sessions import SessionStore

            s = SessionStore().get(session_id)
            raw = str((s or {}).get("workspace") or "").strip()
        except Exception:
            return None
        if not raw:
            return None
        try:
            p = Path(raw).expanduser()
            if p.is_absolute():
                return str(p)
        except Exception:
            pass
        try:
            from ..storage.workspaces import get_workspaces

            ws = get_workspaces().find_by_name(raw)
            if ws and ws.get("path"):
                return str(ws["path"])
        except Exception:
            pass
        return None

    async def agent(self, session_id: str | None = None, *, workspace: str | None = None) -> Agent:
        async with self._lock:
            if session_id and session_id in self.agents:
                ag = self.agents[session_id]
                if getattr(ag, "_asker", None) is None:
                    ag._asker = self._make_asker(ag)
                # ★ 会话的目录在别处被改过（换项目 / 改了项目路径）时就地跟随，
                #   否则这个会话会一直用旧目录干活，而侧栏已经把它归到新项目下。
                want = workspace or self.session_workspace(session_id)
                if want and self._norm_ws(want) != self._norm_ws(ag.workspace):
                    try:
                        ag.set_workspace(want)
                    except Exception:
                        pass
                return ag
            if session_id is None and self.default_agent is not None:
                ag = self.default_agent
                if getattr(ag, "_asker", None) is None:
                    ag._asker = self._make_asker(ag)
                return ag
            from .. import paths

            ag = Agent(
                workspace=(workspace or self.session_workspace(session_id)
                           or self.manager.config.agent.workspace_override
                           or paths.workspace_dir()),
                session_id=session_id,
                config=self.manager.config,
            )
            ag.approval.set_responder(self.bus)
            ag._mcp = self.mcp
            ag._plugins = self.plugins
            ag._asker = self._make_asker(ag)
            self.agents[ag.session_id] = ag
            if session_id is None:
                self.default_agent = ag
            return ag

    def drop_agent(self, session_id: str) -> None:
        ag = self.agents.pop(session_id, None)
        if ag is not None:
            ag.approval.cancel_all("会话已关闭")
            if self.default_agent is ag:
                self.default_agent = None
        self._session_writers.pop(session_id, None)
        # ★ 关会话要把它的工作区锁和队位一起还掉：会话对象没了，再没人替它
        #   release —— 留着的话这个工作区会永久判为「有人在写」，
        #   以后所有对话都排在一个不存在的持有者后面。
        self.cancel_workspace_waiter(session_id)
        for key, cur in list(self._ws_writers.items()):
            if cur.get("session_id") == session_id:
                self.release_workspace(key, cur.get("holder") or "")

    # ---- 会话写入租约（轻量版）------------------------------------------
    def acquire_session(self, session_id: str, holder: str) -> tuple[bool, str]:
        """尝试取得某会话的写租约。

        返回 ``(是否成功, 说明)``。同一会话已有写者时返回 ``(False, 占用者描述)``，
        调用方据此**明确报错**（而不是让两个回合交错写同一条历史）。

        ★ 为什么不做阻塞等待：等下去会让两个回合串成一条看不出因果的长流，
          用户更想要的是「告诉我这个会话正在写，别插队」。
        ★ 持有者标识用回合 id（不是连接 id）：同一个人开两个窗口也算冲突，
          因为真正互斥的是「对同一条历史的写入」。
        """
        cur = self._session_writers.get(session_id)
        if cur and cur != holder:
            return False, cur
        self._session_writers[session_id] = holder
        return True, ""

    def release_session(self, session_id: str, holder: str) -> None:
        """释放写租约（只释放自己持有的那把，避免误清掉别人的）。"""
        if self._session_writers.get(session_id) == holder:
            self._session_writers.pop(session_id, None)

    def session_busy(self, session_id: str) -> str:
        """该会话当前是否有人在写；返回占用者描述（空串表示空闲）。"""
        return self._session_writers.get(session_id, "")

    # ---- 工作区写入租约（跨会话）----------------------------------------
    def workspace_key(self, session_id: str | None = None, ag: Agent | None = None) -> str:
        """会话对应的工作区键（归一化后的目录路径）。

        ★ 为什么用目录而不是工作区 id：项目可以改名、可以删了重建，但目录还是
          同一个 —— 真正互斥的是「对同一批文件的写入」。按 id 算会出现
          「删掉项目再建一个同名项目，两个会话同时写同一个目录」。
        """
        if ag is not None:
            return self._norm_ws(getattr(ag, "workspace", "") or "")
        p = self.session_workspace(session_id)
        if not p:
            from .. import paths

            p = self.manager.config.agent.workspace_override or paths.workspace_dir()
        return self._norm_ws(p)

    def workspace_busy(self, key: str) -> str:
        """该工作区当前是否有人在写；返回占用者**会话 id**（空串表示空闲）。

        ★ 返回会话 id 而不是那个回合的 turn id：turn id（``turn-xxxx``）是内部
          标识，用户看不懂 —— 界面要拿它去显示「由『某某对话』占用」。
          回合 id 只在 release 时用来精确比对，见 _ws_writers 存的两份信息。
        """
        cur = self._ws_writers.get(key)
        return str((cur or {}).get("session_id") or "") if cur else ""

    def _session_label(self, session_id: str) -> str:
        """把会话 id 变成人看得懂的名字（给排队提示用）。"""
        if not session_id:
            return "另一个对话"
        try:
            from ..storage.sessions import SessionStore

            s = SessionStore().get(session_id) or {}
            return str(s.get("title") or "").strip() or f"对话 {session_id[-4:]}"
        except Exception:
            return f"对话 {session_id[-4:]}"

    def workspace_queue_state(self, session_id: str) -> dict[str, Any]:
        """查这个会话当前在工作区队列里的状态（给界面用）。

        ``{waiting: bool, position: int, busy_by: str, busy_by_label: str}``。
        没在排队时 position=0、busy_by 为空 —— 「没有排队」和「排在第一个」
        必须分得清，否则界面会把「排到了」显示成「排队中」。
        """
        for key, q in self._ws_waiters.items():
            for i, item in enumerate(q):
                if item.get("session_id") != session_id:
                    continue
                # ★ 占用者要取**会话 id**（cur["session_id"]），不是那个回合的
                #   turn id —— 后者只是内部标识，拿它查会话名会查出空、界面
                #   只能显示「另一个对话」。这个结构踩过一次（dict 当字符串用）。
                cur = self._ws_writers.get(key) or {}
                busy_sid = str(cur.get("session_id") or "")
                return {
                    "waiting": True,
                    "position": i + 1,
                    "workspace": key,
                    "busy_by": busy_sid,
                    "busy_by_label": self._session_label(busy_sid),
                }
        return {"waiting": False, "position": 0, "workspace": "", "busy_by": "", "busy_by_label": ""}

    def _notify_workspace_queue(self, session_id: str) -> None:
        """把排队状态推给这个会话自己的界面。"""
        try:
            self.bus.emit(Ev.WORKSPACE_QUEUE, self.workspace_queue_state(session_id),
                          session_id=session_id)
        except Exception:
            pass

    async def acquire_workspace(
        self,
        key: str,
        *,
        session_id: str,
        holder: str,
        on_state: Callable[[], None] | None = None,
    ) -> bool:
        """取得工作区写租约；被占用时**排队等待**（先来先服务）。

        返回 True = 拿到了可以开写；False = 等待期间被取消（用户点了停止、会话被关）。

        ★ 为什么这里等待、而会话级租约直接报错：两者语义不同。
          同一会话被并发写是「同一份历史的交错」，没有正确的处理办法，只能拒绝；
          同一工作区被两个对话并发写是「接力」关系 —— 用户在 B 对话里发消息时
          本意是「等我那个跑完接着做」，报错只会逼他盯着前一个手动发。
        ★ 排序用 FIFO（列表尾部入队），并由 release 直接**转交**给队首，
          不搞「清空后大家抢」—— 那样先来后到的公平性取决于事件循环调度顺序。
        """
        cur = self._ws_writers.get(key)
        if not cur or cur.get("holder") == holder:
            self._ws_writers[key] = {"session_id": session_id, "holder": holder}
            return True
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        item = {"session_id": session_id, "holder": holder, "fut": fut}
        q = self._ws_waiters.setdefault(key, [])
        q.append(item)
        if on_state is not None:
            try:
                on_state()
            except Exception:
                pass
        try:
            got = await fut
            return got == "go"
        except asyncio.CancelledError:
            return False
        finally:
            try:
                q.remove(item)
            except ValueError:
                pass
            if not q:
                self._ws_waiters.pop(key, None)

    def release_workspace(self, key: str, holder: str) -> None:
        """释放工作区写租约，并把它直接转交给排队最前面的那位。"""
        cur = self._ws_writers.get(key)
        if not cur or cur.get("holder") != holder:
            return
        q = self._ws_waiters.get(key) or []
        while q:
            item = q.pop(0)
            fut = item.get("fut")
            if fut is not None and not fut.done():
                # 先把锁记在它名下再唤醒：它醒来就不必再抢一次（也不会被插队）
                self._ws_writers[key] = {
                    "session_id": item.get("session_id") or "",
                    "holder": item["holder"],
                }
                fut.set_result("go")
                self._notify_workspace_queue(item.get("session_id") or "")
                return
        self._ws_writers.pop(key, None)

    def cancel_workspace_waiter(self, session_id: str) -> bool:
        """把这个会话从工作区队列里撤下来（用户点停止 / 会话关闭）。"""
        hit = False
        for key, q in list(self._ws_waiters.items()):
            for item in list(q):
                if item.get("session_id") != session_id:
                    continue
                fut = item.get("fut")
                if fut is not None and not fut.done():
                    fut.set_result("cancel")
                    hit = True
                try:
                    q.remove(item)
                except ValueError:
                    pass
            if not q:
                self._ws_waiters.pop(key, None)
        if hit:
            self._notify_workspace_queue(session_id)
        return hit

    def release_workspace_holder(self, holder: str) -> None:
        """按持有者标识释放工作区写租约（调用方不必知道 key）。

        ★ 为什么需要它：租约改成「按需、在回合中途」获取后，key 是在获取那一刻
          由 Agent 的当前工作目录算出来的；释放时若再算一次，万一目录在中途被
          改过就找不到那一把锁了 —— 用持有者标识反查最稳。
        """
        for key, cur in list(self._ws_writers.items()):
            if cur.get("holder") == holder:
                self.release_workspace(key, holder)

    def make_workspace_gate(self, ag: Agent, holder: str, session_id: str):
        """构造「按需获取工作区写租约」的门，交给 Agent 在真要写工作区时调用。

        ★ 语义（这是本版的关键改动）：一个回合**不再**一开始就抢工作区写租约，
          而是先自由地跑（读文件、查资料、纯文字问答），只有当某一步真的出现
          「会写工作区的工具调用」时，才在这里排队等锁。
          为什么：旧实现让同目录下的每个回合都从第一步开始排队，于是新对话里
          哪怕只是问一句话也要等另一个对话跑完 —— 用户看到的就是「连基础对话
          都要排队」，而真正需要互斥的只有「对同一批文件的写入」。

        返回一个 ``async () -> bool``：True = 拿到（或本就不需要），False = 等待
        期间被取消（用户点停止 / 会话关闭），调用方据此安静收场。
        """

        async def _gate() -> bool:
            key = self.workspace_key(ag=ag)
            got = await self.acquire_workspace(
                key,
                session_id=session_id,
                holder=holder,
                on_state=lambda: self._notify_workspace_queue(session_id),
            )
            if got:
                # 排到了：告诉界面「轮到你了」，前端据此把「排队中」换成「生成中」。
                try:
                    self.bus.emit(
                        Ev.WORKSPACE_QUEUE,
                        {"waiting": False, "granted": True},
                        session_id=session_id,
                    )
                except Exception:
                    pass
            return bool(got)

        return _gate

    # ---- 后台服务 ------------------------------------------------------
    async def startup(self) -> None:
        cfg = self.manager.config
        # 插件
        try:
            from ..storage.db import get_db
            from ..tools.base import get_registry

            reg = get_registry()
            if not reg.all():
                from ..tools import register_builtin

                register_builtin(reg)
            self.plugins = PluginManager(config=cfg, db=get_db(), bus=self.bus, registry=reg)
            if cfg.plugins.auto_load:
                self.plugins.load_all()
        except Exception as e:
            self.bus.emit(Ev.LOG, {"level": "error", "message": f"插件加载失败：{e}"})
        # MCP
        if cfg.mcp.enabled:
            try:
                self.mcp = MCPManager(config=cfg, bus=self.bus)
                # ★ 后台冷启动：MCP 服务器可能要几十秒才连上
                #   （npx 首次下载、Python 服务加载），阻塞在这里会让「打开软件」
                #   转很久。改成后台连接：界面立刻可用，连上的陆续把工具挂出来。
                await self.mcp.start_all(background=True)
            except Exception as e:
                self.bus.emit(Ev.LOG, {"level": "error", "message": f"MCP 启动失败：{e}"})
        # 定时任务
        if cfg.scheduler.enabled:
            try:
                from ..scheduler.cron import Scheduler
                from ..storage.db import get_db
                from ..storage.tasks import JobStore

                self._scheduler = Scheduler(
                    store=JobStore(get_db()), bus=self.bus,
                    runner=lambda job: self._run_job(job),
                )
                await self._scheduler.start()
            except Exception as e:
                self.bus.emit(Ev.LOG, {"level": "error", "message": f"调度器启动失败：{e}"})
        # ★ 1-O：会话索引自愈（后台执行）——元数据坏了就从 messages 权威数据重建。
        #   放后台是为了不拖慢冷启动（对齐 2-H 的方向：历史整理不该阻塞启动）。
        try:
            asyncio.create_task(self._repair_session_index())
        except Exception:
            pass
        self.bus.emit(Ev.LOG, {"level": "info", "message": "服务端已就绪"})

    async def _repair_session_index(self) -> None:
        """后台跑一次会话索引自愈；出错只记日志，绝不影响服务启动。"""
        try:
            from ..storage.sessions import SessionStore

            r = await asyncio.to_thread(SessionStore().repair_index)
            if r.get("repaired"):
                self.bus.emit(
                    Ev.LOG,
                    {"level": "info",
                     "message": f"会话索引自愈：修复 {r['repaired']} / {r['scanned']} 个会话的缺失元数据"},
                )
        except Exception as e:
            self.bus.emit(Ev.LOG, {"level": "warn", "message": f"会话索引自愈跳过：{e}"})

    async def _run_job(self, job: dict) -> str:
        kind = job.get("kind") or "prompt"
        payload = job.get("payload") or {}
        if kind == "prompt":
            text = str(payload.get("prompt") or "")
            if not text:
                return "（任务没有 prompt）"
            ag = await self.agent()
            res = await ag.run(text)
            return res.content or "(无输出)"
        if kind == "tool":
            from ..tools.base import get_registry

            ctx = None
            ag = await self.agent()
            ctx = ag.tool_context()
            r = await get_registry().execute(
                str(payload.get("tool") or ""), payload.get("arguments") or {}, ctx
            )
            return r.content if r.ok else f"失败：{r.error}"
        if kind == "shell":
            from ..security.sandbox import LocalSandbox

            ag = await self.agent()
            sb = LocalSandbox(cwd=ag.workspace, timeout=float(payload.get("timeout") or 300))
            r = await sb.shell(str(payload.get("command") or ""))
            return r.stdout if r.ok else f"失败({r.returncode})：{r.stderr}"
        if kind == "workflow":
            from ..storage.tasks import WorkflowStore
            from ..workflow.engine import WorkflowRunner, from_dict

            wid = str(payload.get("workflow_id") or "")
            wf = WorkflowStore().get(wid)
            if wf is None:
                return f"（没有找到工作流 {wid}）"
            ag = await self.agent()
            runner = WorkflowRunner(
                agent_runner=lambda p: ag.run(p),
                tool_registry=getattr(ag, "registry", None),
                ctx=ag.tool_context(),
                bus=self.bus,
                subagent_runner=ag.subagent_runner,
            )
            out = await runner.run(from_dict(wf.get("dag") or {}), workflow_id=wid)
            return out.get("summary") or "(工作流完成)"
        return f"（未知任务类型：{kind}）"

    async def shutdown(self) -> None:
        if self._scheduler is not None:
            with contextlib.suppress(Exception):
                await self._scheduler.stop()
        if self.mcp is not None:
            with contextlib.suppress(Exception):
                await self.mcp.close()
        if self.plugins is not None:
            with contextlib.suppress(Exception):
                self.plugins.shutdown()
        for ag in list(self.agents.values()):
            with contextlib.suppress(Exception):
                await ag.llm.aclose()

    def status(self) -> dict[str, Any]:
        cfg = self.manager.config
        info: dict[str, Any] = {
            "app": "Fengcode",
            "version": __version__,
            "uptime": round(time.time() - self.started_at, 1),
            "sessions": len(self.agents),
            "workspace": str(self.manager.config.agent.workspace_override or ""),
            "model": self.manager.default_model_ref(),
            "providers": len(cfg.providers),
            "providers_with_key": sum(
                1 for p in cfg.providers if self.manager.resolve_api_key(p)
            ),
            "first_run_done": bool(cfg.first_run_done),
        }
        # 关于页需要：数据位置与访问地址（全部是本机路径，不含密钥）
        try:
            from .. import paths as _p
            info["paths"] = {
                "config": str(_p.config_file()) if hasattr(_p, "config_file") else "",
                "data": str(_p.data_dir()),
                "logs": str(_p.logs_dir()) if hasattr(_p, "logs_dir") else "",
                "workspace": str(_p.workspace_dir()),
            }
        except Exception:
            info["paths"] = {}
        info["base_url"] = f"http://{cfg.server.host}:{cfg.server.port}" if getattr(cfg, "server", None) else ""
        if self.mcp is not None:
            st = self.mcp.status()
            info["mcp"] = {
                "total": len(st),
                "ready": sum(1 for s in st if s.get("connected")),
                "tools": sum(s.get("tool_count", 0) for s in st),
            }
        if self.plugins is not None:
            info["plugins"] = self.plugins.stats()
        # ★ 1-G：权限规则校验 —— 把「写了却永远匹配不到」的规则报给界面。
        #   安全规则静默失效比没有规则更危险（禁掉了，其实没有）。
        try:
            from ..security.approval import validate_rules

            perm = getattr(cfg, "permissions", None)
            bad = list(validate_rules(getattr(perm, "deny_patterns", None) or []))
            # rules 是结构化规则（[{pattern, action, ...}]），取其 pattern 一起校验
            for r in (getattr(perm, "rules", None) or []):
                pat = getattr(r, "pattern", None)
                if pat is None and isinstance(r, dict):
                    pat = r.get("pattern")
                if pat:
                    bad += validate_rules([pat])
            if bad:
                info["permission_warnings"] = bad
        except Exception:
            pass
        return info


STATE = AppState()


# --------------------------------------------------------------------------
# 路由：基础
# --------------------------------------------------------------------------

async def health(request: Any) -> Response:
    return _json({"ok": True, "app": "Fengcode", "version": __version__, "time": time.time()})


async def api_status(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    return _json(STATE.status())


async def api_bootstrap(request: Any) -> Response:
    """界面初始化用：一次性返回状态、模型列表、工具分组等。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    mgr = STATE.manager
    from ..llm.router import model_choices
    from ..tools import tool_groups
    from ..tools.base import get_registry

    reg = get_registry()
    if not reg.all():
        from ..tools import register_builtin

        register_builtin(reg)
    cfg = mgr.config
    data = {
        "status": STATE.status(),
        "models": model_choices(),
        "default_model": mgr.default_model_ref(),
        "providers": [scrub_provider(p) for p in cfg.providers],
        "tool_groups": tool_groups(reg),
        "skills": _skills_list(),
        "ui": cfg.ui.model_dump(),
        "agent": {
            "max_steps": cfg.agent.max_steps,
            "reflection": cfg.agent.reflection,
            "submit_checklist": cfg.agent.submit_checklist,
            "subagents": cfg.agent.subagents.model_dump(),
            "reasoning_language": cfg.agent.reasoning_language,
            "planner_model": cfg.agent.planner_model,
            "subagent_model": cfg.agent.subagent_model,
            "vision_model": cfg.agent.vision_model,
            "search_model": cfg.agent.search_model,
            "subagent_effort": cfg.agent.subagent_effort,
            "temperature": cfg.agent.temperature,
            "system_prompt_extra": cfg.agent.system_prompt_extra,
        },
        "permissions": {
            "mode": cfg.permissions.mode,
            "write_paths": cfg.permissions.write_paths,
            "read_paths": cfg.permissions.read_paths,
            "deny_patterns": cfg.permissions.deny_patterns,
        },
        "memory": cfg.memory.model_dump(),
        "llm": {
            "temperature": cfg.llm.temperature,
            "compact_ratio": cfg.llm.compact_ratio,
            "keep_recent_turns": cfg.llm.keep_recent_turns,
            "stream": cfg.llm.stream,
        },
        "scheduler": {"enabled": cfg.scheduler.enabled},
        "remote": {
            "enabled": cfg.remote.enabled,
            "hosts": [
                {"name": h.name, "host": h.host, "port": h.port, "user": h.user}
                for h in cfg.remote.hosts
            ],
        },
        "first_run_done": cfg.first_run_done,
        # 账号（可选账号源）：只带本地快照，不发网络请求 —— 首屏不该被外部站点拖慢
        "account": _account_snapshot(),
    }
    return _json(data)


def _account_snapshot() -> dict[str, Any]:
    """账号的**本地**快照（不发网络）。任何异常都退回未登录态，不影响首屏。"""
    try:
        from ..core.account import get_account

        return get_account().snapshot()
    except Exception:  # noqa: BLE001
        return {"logged_in": False}


def scrub_provider(p: Any) -> dict[str, Any]:
    """把 provider 给前端时抹掉密钥（只告诉"有没有"）。"""
    d = p.model_dump(exclude_none=True)
    key = d.pop("api_key", None)
    d["has_key"] = bool(key)
    d["api_key_masked"] = (key[:6] + "…" + key[-4:]) if isinstance(key, str) and len(key) > 12 else ""
    # userinfo / token 类字段也清掉
    for k in ("extra", "headers"):
        if isinstance(d.get(k), dict):
            d[k] = {
                kk: ("***" if any(s in kk.lower() for s in ("key", "token", "secret", "auth")) else vv)
                for kk, vv in d[k].items()
            }
    return d


async def api_config(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    mgr = STATE.manager
    if request.method == "GET":
        return _json(mgr.to_dict())
    # PATCH：深合并补丁
    patch = await _body(request)
    if not patch:
        return _err("请求体为空")
    try:
        mgr.update(patch)
    except Exception as e:
        return _err(f"配置保存失败：{e}", 400)
    # 配置变了：让运行中的 Agent 拿到新值
    _reload_runtime()
    return _json({"ok": True, "config": mgr.to_dict()})


def _reload_runtime() -> None:
    """配置变更后刷新运行期对象。"""
    mgr = STATE.manager
    for ag in list(STATE.agents.values()):
        ag.config = mgr.config
        ag.guard = ag.guard.__class__(
            workspace=ag.workspace,
            write_paths=mgr.config.permissions.write_paths,
            read_paths=mgr.config.permissions.read_paths,
            deny_patterns=mgr.config.permissions.deny_patterns or None,
            mode=mgr.config.permissions.mode,
        )
        ag.approval.update_config(mgr.config.permissions)
    try:
        from ..llm.router import get_llm

        get_llm(mgr).invalidate()
    except Exception:
        pass


async def api_config_raw(request: Any) -> Response:
    """直接读写 config.toml 原文（高级用户用）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    path = STATE.manager.path
    if request.method == "GET":
        text = ""
        with contextlib.suppress(OSError):
            text = Path(path).read_text(encoding="utf-8")
        return _json({"path": str(path), "text": text})
    body = await _body(request)
    text = body.get("text")
    if not isinstance(text, str):
        return _err("需要 text 字段")
    # 校验 TOML 合法性再落盘
    try:
        import tomllib

        tomllib.loads(text)
    except Exception as e:
        return _err(f"TOML 语法错误，未保存：{e}", 400)
    try:
        Path(path).write_text(text, encoding="utf-8", newline="\n")
        STATE.manager.reload()
        _reload_runtime()
    except Exception as e:
        return _err(f"写入失败：{e}", 500)
    return _json({"ok": True})


# --------------------------------------------------------------------------
# 路由：账号（可选账号源）
# --------------------------------------------------------------------------

async def api_account(request: Any) -> Response:
    """账号相关：本地快照 / 登录 / 登出 / 刷新余额 / 记住"问过了"。

    ★★ 脱敏原则：返回给界面的一律只有身份与余额，
      **绝不下发**密码、会话刷新凭证、access token。
      凭证只存在服务端的 ``config/account.json``。
    ★ 为什么走服务端代理而不是界面直连站点：站点的用户接口不返跨域头
      （预检有头、真实响应没有），浏览器拿不到数据；顺带也让凭证不出后端。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..core.account import AccountError, get_account

    acc = get_account()
    if request.method == "GET":
        # 只读本地：不发网络请求，界面首屏/状态栏用
        return _json(acc.snapshot())

    body = await _body(request)
    action = str(body.get("action") or "").strip()

    if action == "login":
        try:
            info = await acc.login(
                str(body.get("username") or ""),
                str(body.get("password") or ""),
            )
        except AccountError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return _json({"ok": False, "error": f"登录失败：{e}"}, 400)
        return _json({"ok": True, "account": info})

    if action == "refresh":
        try:
            info = await acc.self()
        except AccountError as e:
            # 凭证失效时 AccountError 已把本地清干净，界面据此回到未登录态
            return _json({"ok": False, "error": str(e), "account": acc.snapshot()}, 400)
        except Exception as e:  # noqa: BLE001
            return _json({"ok": False, "error": f"读取失败：{e}"}, 400)
        return _json({"ok": True, "account": info})

    if action == "logout":
        await acc.logout()
        return _json({"ok": True, "account": acc.snapshot()})

    if action == "prompt_done":
        # 用户对"要不要登录"表过态了（选了否也算）—— 之后不再打扰
        try:
            STATE.manager.update({"account": {"prompt_done": True}})
        except Exception as e:  # noqa: BLE001
            return _err(f"保存失败：{e}", 400)
        return _json({"ok": True})

    if action == "bind":
        # 一键绑定：确保账号下有一条密钥，并写进「万象账号」供应商，随后返回可用模型
        return await _account_bind(acc, body)

    if action == "models":
        try:
            models = await acc.user_models()
        except AccountError as e:
            return _json({"ok": False, "error": str(e), "account": acc.snapshot()}, 400)
        except Exception as e:  # noqa: BLE001
            return _json({"ok": False, "error": f"读取模型失败：{e}"}, 400)
        return _json({"ok": True, "models": models})

    return _err(f"未知操作：{action}")


async def _account_bind(acc: Any, body: dict[str, Any]) -> Response:
    """账号绑定：建（或复用）密钥 → 写进供应商 → 返回可用模型。

    ★★ 这条链路只在「账号」页提供：它要用登录态换来的密钥，
      没有登录态就没有密钥可绑，所以不登录时直接拒绝。
    ★ 写进的是**单独的**供应商（默认 ``wanxiang-account``），
      不动用户自己手填的「万象 API」预设 —— 免得覆盖人家的密钥。
    ★ 供应商的模型列表**只放用户勾选启用的**；没勾选的不写进去。
    """
    from ..core.account import AccountError

    if not acc.logged_in():
        return _json({"ok": False, "error": "请先登录账号"}, 400)
    try:
        got = await acc.ensure_key()
    except AccountError as e:
        return _json({"ok": False, "error": str(e), "account": acc.snapshot()}, 400)
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"绑定失败：{e}"}, 400)

    try:
        models = await acc.user_models()
    except Exception:  # noqa: BLE001
        models = []

    prefs = acc.prefs()
    pname = str(prefs.get("provider_name") or "wanxiang-account")
    enabled = [str(m) for m in (body.get("enabled") or []) if str(m).strip()]
    mgr = STATE.manager

    # 站点地址要去掉结尾的 /v1，供应商的 base_url 由它自己拼
    base = str(got.get("base_url") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]

    from ..config.catalog import presets as _presets

    preset = _presets().get("wanxiang") or {}
    prov = mgr.get_provider(pname)
    patch: dict[str, Any] = {
        "name": pname,
        "kind": "openai",
        "display_name": "万象账号",
        "base_url": base,
        "models_url": f"{base}/v1/models",
        "api_key": got["api_key"],
        "enabled": True,
    }
    # 思考参数必须带上，否则模型完全不思考（见 catalog 里万象预设的说明）
    for k in ("default_effort", "effort_style"):
        if preset.get(k):
            patch[k] = preset[k]
    if preset.get("effort_style"):
        patch.setdefault("extra", {})["effort_style"] = preset["effort_style"]
    if models:
        patch["models"] = enabled or models
        patch["default"] = (enabled or models)[0]

    try:
        if prov is None:
            mgr.add_provider(
                pname,
                kind="openai",
                base_url=base,
                api_key=got["api_key"],
                models=enabled or models,
                default=(enabled or models)[0] if (enabled or models) else None,
                enabled=True,
            )
            prov = mgr.get_provider(pname)
        if prov is not None:
            data = prov.model_dump(exclude_none=False)
            data.update(patch)
            from ..config.schema import Provider as _Provider

            mgr.upsert_provider(_Provider(**data))
    except Exception as e:  # noqa: BLE001
        return _json({"ok": False, "error": f"写入供应商失败：{e}"}, 400)

    _reload_runtime()
    return _json({
        "ok": True,
        "provider": pname,
        "models": models,
        "enabled": enabled,
        "group": got.get("group") or "",
        "account": acc.snapshot(),
    })


# --------------------------------------------------------------------------
# 路由：供应商与模型
# --------------------------------------------------------------------------

async def api_providers(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    mgr = STATE.manager
    if request.method == "GET":
        return _json({"providers": [scrub_provider(p) for p in mgr.config.providers],
                      "presets": __import__("fengcode.config.catalog", fromlist=["presets"]).presets()})
    body = await _body(request)
    action = str(body.get("action") or "add")
    if action == "add":
        name = str(body.get("name") or "").strip()
        if not name:
            return _err("name 不能为空")
        try:
            prov = mgr.add_provider(
                name,
                kind=str(body.get("kind") or "openai"),
                base_url=str(body.get("base_url") or ""),
                api_key=body.get("api_key"),
                api_key_env=body.get("api_key_env"),
                models=[str(m) for m in (body.get("models") or [])],
                default=body.get("default"),
                enabled=bool(body.get("enabled", True)),
                preset=body.get("preset"),
            )
        except Exception as e:
            return _err(f"添加失败：{e}", 400)
        return _json({"ok": True, "provider": scrub_provider(prov)})
    if action == "update":
        name = str(body.get("name") or "")
        prov = mgr.get_provider(name)
        if prov is None:
            return _err(f"没有找到供应商：{name}", 404)
        patch = body.get("patch") or {}
        if isinstance(patch, dict):
            data = prov.model_dump(exclude_none=False)
            data.update(patch)
            try:
                from ..config.schema import Provider

                mgr.upsert_provider(Provider(**data))
            except Exception as e:
                return _err(f"更新失败：{e}", 400)
        return _json({"ok": True})
    if action == "delete":
        ok = mgr.remove_provider(str(body.get("name") or ""))
        return _json({"ok": ok})
    if action == "toggle":
        name = str(body.get("name") or "")
        prov = mgr.get_provider(name)
        if prov is None:
            return _err("未找到", 404)
        prov.enabled = bool(body.get("enabled", True))
        mgr.upsert_provider(prov)
        return _json({"ok": True, "enabled": prov.enabled})
    if action == "model_override":
        # 逐模型的「启用 / 上下文窗口 / 输出上限 / 支持图片」等覆盖设置。
        # ★ 需求：模型前面有勾选框，勾上才启用；启用后可单独配置这几项；
        #   输入留空 = 不限制（交给上游），不要拿默认值冒充用户设置。
        name = str(body.get("name") or "")
        model = str(body.get("model") or "")
        if not name or not model:
            return _err("name 与 model 不能为空")
        prov = mgr.get_provider(name)
        if prov is None:
            return _err(f"没有找到供应商：{name}", 404)
        from ..config.schema import ModelOverride

        ov_patch = body.get("override") or {}
        cur = prov.model_overrides.get(model)
        data = cur.model_dump(exclude_none=False) if cur is not None else {}
        data.update(ov_patch)
        # 全部字段都是 None/空 → 说明用户把这项清空了，直接删掉这条覆盖
        meaningful = {
            k: v for k, v in data.items()
            if v is not None and not (k == "tags" and not v)
        }
        if meaningful:
            prov.model_overrides[model] = ModelOverride(**data)
        else:
            prov.model_overrides.pop(model, None)
        mgr.upsert_provider(prov)
        return _json({"ok": True, "override": (
            prov.model_overrides[model].model_dump(exclude_none=True)
            if model in prov.model_overrides else None)})
    return _err(f"未知操作：{action}")


async def api_provider_test(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    body = await _body(request)
    name = str(body.get("name") or "")
    model = body.get("model")
    if not name:
        return _err("name 不能为空")
    from ..llm.router import get_llm

    llm = get_llm(STATE.manager)
    if body.get("list_models"):
        models = await llm.list_models(name)
        return _json({"ok": bool(models), "models": models})
    if body.get("balance"):
        bal = await llm.check_balance(name)
        return _json({"ok": bal is not None, "balance": bal})
    res = await llm.test_connection(name, model)
    if res.get("ok"):
        # 顺手把可用模型列表也带上
        with contextlib.suppress(Exception):
            res["models"] = await llm.list_models(name)
    return _json(res)


# --------------------------------------------------------------------------
# 路由：会话与对话
# --------------------------------------------------------------------------

async def api_sessions(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.sessions import SessionStore

    store = SessionStore()
    if request.method == "GET":
        limit = int(request.query_params.get("limit") or 100)
        search = request.query_params.get("search")
        include_archived = request.query_params.get("archived") == "1"
        return _json({"sessions": store.list(limit=limit, search=search,
                                             include_archived=include_archived)})
    body = await _body(request)
    s = store.create(
        title=str(body.get("title") or "新对话"),
        workspace=str(body.get("workspace") or ""),
        model=STATE.manager.default_model_ref(),
        mode=normalize_mode(body.get("mode")),
    )
    return _json({"ok": True, "session": s})


async def api_workspaces(request: Any) -> Response:
    """工作区管理：列出 / 新建 / 改名 / 改目录 / 删除。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.workspaces import get_workspaces

    store = get_workspaces()
    if request.method == "GET":
        ws_list = store.list()
        default = store.default()
        # 每个工作区带上会话数，便于界面展示
        from ..storage.sessions import SessionStore

        sess = SessionStore()
        counts: dict[str, int] = {}
        for s in sess.list(limit=9999):
            wp = s.get("workspace") or ""
            counts[wp] = counts.get(wp, 0) + 1
        for w in ws_list:
            w["sessions"] = counts.get(w.get("path") or "", 0)
        return _json({"workspaces": ws_list, "default_id": default.get("id")})

    body = await _body(request)
    action = str(body.get("action") or "create")

    if action == "create":
        name = str(body.get("name") or "").strip()
        path = str(body.get("path") or "").strip()
        if not name:
            return _err("工作区名字不能为空", 400)
        # 没给目录就自动在数据目录下建一个专属于它的空目录，
        # 这样「新建空白项目」是开箱即用的，不需要用户先手建文件夹。
        if not path:
            try:
                from .. import paths as _paths
                safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "project"
                base = _paths.data_dir() / "projects"
                base.mkdir(parents=True, exist_ok=True)
                candidate = base / safe
                n = 2
                while candidate.exists():
                    candidate = base / f"{safe}-{n}"
                    n += 1
                candidate.mkdir(parents=True, exist_ok=True)
                path = str(candidate)
            except Exception as e:
                return _err(f"创建项目目录失败：{e}", 500)
        else:
            # 用户指定了目录：不存在就创建，创建不了就明确报错
            try:
                from pathlib import Path as _P
                p = _P(path).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                path = str(p)
            except Exception as e:
                return _err(f"工作目录不可用：{e}", 400)
        w = store.create(name=name, path=path)
        _reload_runtime()
        return _json({"ok": True, "workspace": w})

    if action in ("rename", "update"):
        ws_id = str(body.get("id") or "")
        if not ws_id:
            return _err("缺少 id", 400)
        fields: dict[str, Any] = {}
        if body.get("name") is not None:
            fields["name"] = str(body["name"]).strip()
        if body.get("path") is not None:
            fields["path"] = str(body["path"]).strip()
        ok = store.update(ws_id, **fields) if fields else False
        _reload_runtime()
        return _json({"ok": ok, "workspace": store.get(ws_id)})

    if action == "delete":
        ws_id = str(body.get("id") or "")
        if not ws_id:
            return _err("缺少 id", 400)
        ok = store.delete(ws_id)
        if not ok:
            return _err("默认工作区不能删除", 400)
        _reload_runtime()
        return _json({"ok": True})

    return _err(f"未知操作：{action}", 400)


async def api_workspace_files(request: Any) -> Response:
    """浏览工作区目录（项目文件页用）。

    参数：
      ws      工作区 id（不传则用默认工作区）
      path    相对工作区根的子路径
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    import os as _os

    from ..storage.workspaces import get_workspaces

    store = get_workspaces()
    ws_id = request.query_params.get("ws") or ""
    ws = store.get(ws_id) if ws_id else store.default()
    if not ws:
        return _err("工作区不存在", 404)

    root = Path(ws.get("path") or "")
    rel = (request.query_params.get("path") or "").strip().replace("\\", "/")

    # 防目录穿越：规范化后必须在 root 之内
    base = Path(_os.path.abspath(str(root)))
    target = Path(_os.path.abspath(str(base / rel))) if rel else base
    try:
        target.relative_to(base)
    except ValueError:
        return _err("路径越界", 400)

    if not base.is_dir():
        return _json({"ok": False, "error": "工作区目录不存在", "root": str(base),
                      "path": rel, "entries": []})
    if not target.is_dir():
        return _err("不是目录", 404)

    skip = {"node_modules", "__pycache__", ".git", ".venv", "venv",
            ".pytest_cache", ".mypy_cache", "dist", "build", ".idea"}
    entries: list[dict[str, Any]] = []
    try:
        for it in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            name = it.name
            if name in skip or name.startswith(".fengcode-write"):
                continue
            try:
                st = it.stat()
                size = st.st_size
                mtime = st.st_mtime
            except OSError:
                size, mtime = 0, 0
            is_dir = it.is_dir()
            entries.append({
                "name": name,
                "is_dir": is_dir,
                "size": size,
                "mtime": mtime,
                "ext": (it.suffix.lstrip(".").lower() if not is_dir else ""),
            })
            if len(entries) >= 600:
                break
    except PermissionError:
        return _err("没有权限读取该目录", 403)

    return _json({
        "ok": True,
        "root": str(base),
        "path": rel,
        "parent": ("/" .join(rel.split("/")[:-1]) if rel else ""),
        "entries": entries,
    })


async def api_open_path(request: Any) -> Response:
    """用系统默认程序打开一个本地路径（用于「打开工作区目录」等按钮）。

    只允许打开本机真实存在的路径；路径由后端校验，避免前端传任意值。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    import os as _os
    import platform
    import subprocess as _sp

    body = await _body(request)
    raw = str(body.get("path") or "").strip()
    if not raw:
        return _err("缺少 path", 400)
    p = Path(_os.path.abspath(_os.path.expanduser(raw)))
    if not p.exists():
        # 目录不存在就尝试建出来，这样「打开数据目录」永远可用
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _err(f"路径不存在且无法创建：{e}", 404)
    try:
        system = platform.system()
        if system == "Windows":
            _os.startfile(str(p))  # type: ignore[attr-defined]
        elif system == "Darwin":
            _sp.Popen(["open", str(p)], close_fds=True)
        else:
            _sp.Popen(["xdg-open", str(p)], close_fds=True)
    except Exception as e:
        return _err(f"打开失败：{e}", 500)
    return _json({"ok": True, "path": str(p)})


async def api_recovery(request: Any) -> Response:
    """恢复助手：诊断 / 列出配置备份 / 回滚 / 修复。

    恢复助手：只做安全、可逆的动作，不擅自改动用户数据。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)

    import platform
    import shutil as _shutil
    import sys as _sys

    from .. import paths

    if request.method == "GET":
        checks: list[dict[str, Any]] = []

        def add(name: str, okk: bool, detail: str = "", fix: str = "") -> None:
            checks.append({"name": name, "ok": okk, "detail": detail, "fix": fix})

        # 数据目录
        try:
            paths.ensure_all()
            add("数据目录可写", True, str(paths.home()))
        except Exception as e:
            add("数据目录可写", False, str(e), "检查目录权限，或设置 FENGCODE_HOME 指向可写目录")

        # Python
        ver = _sys.version_info
        add("Python 版本 ≥ 3.10", ver >= (3, 10), platform.python_version(), "升级 Python 到 3.10+")

        # 核心依赖
        for mod, pkg in [("httpx", "httpx"), ("pydantic", "pydantic"), ("yaml", "pyyaml"),
                         ("typer", "typer"), ("starlette", "starlette"), ("uvicorn", "uvicorn")]:
            try:
                __import__(mod)
                add(f"依赖 {pkg}", True)
            except ImportError:
                add(f"依赖 {pkg}", False, f"缺少 {mod}", f"pip install {pkg}")

        # 配置文件可解析
        try:
            cfg_path = paths.config_file()
            if cfg_path.is_file():
                add("配置文件可解析", True, str(cfg_path))
            else:
                add("配置文件可解析", True, "尚未生成（使用默认配置）")
        except Exception as e:
            add("配置文件可解析", False, str(e), "用「回滚配置」恢复到上一个可用备份")

        # 数据库可打开
        try:
            from ..storage.db import get_db
            db = get_db()
            db.query("SELECT 1")
            add("数据库可打开", True, str(paths.db_file()))
        except Exception as e:
            add("数据库可打开", False, str(e), "检查文件权限；必要时删除 db 文件重建（会丢失历史）")

        # 模型可用性
        try:
            cfg = STATE.manager.config
            with_key = sum(1 for p in cfg.providers if STATE.manager.resolve_api_key(p))
            add("至少一个模型可用", with_key > 0,
                f"{with_key} 个供应商已配密钥" if with_key else "没有任何供应商配了密钥",
                "到「设置 → 模型服务」填入地址与密钥")
        except Exception as e:
            add("至少一个模型可用", False, str(e), "检查模型服务配置")

        backups = _list_config_backups()
        return _json({
            "ok": True,
            "checks": checks,
            "failed": sum(1 for c in checks if not c["ok"]),
            "backups": backups,
            "home": str(paths.home()),
        })

    body = await _body(request)
    action = str(body.get("action") or "")

    if action == "backup":
        try:
            p = _backup_config()
            return _json({"ok": bool(p), "path": p})
        except Exception as e:
            return _err(f"备份失败：{e}", 500)

    if action == "restore":
        name = str(body.get("name") or "")
        try:
            ok, msg = _restore_config(name)
            if not ok:
                return _err(msg, 400)
            _reload_runtime()
            return _json({"ok": True, "message": msg})
        except Exception as e:
            return _err(f"回滚失败：{e}", 500)

    if action == "reset_workspace":
        # 把工作区目录清空（仅限默认工作区，且要显式确认）
        if not body.get("confirm"):
            return _err("需要确认", 400)
        try:
            from ..storage.workspaces import get_workspaces
            ws = get_workspaces().default()
            root = Path(str(ws.get("path") or ""))
            removed = 0
            if root.is_dir():
                for it in list(root.iterdir()):
                    try:
                        if it.is_dir():
                            _shutil.rmtree(it, ignore_errors=True)
                        else:
                            it.unlink()
                        removed += 1
                    except OSError:
                        continue
            return _json({"ok": True, "removed": removed, "path": str(root)})
        except Exception as e:
            return _err(f"清理失败：{e}", 500)

    if action == "cleanup_backups":
        # ★ 2-P：清理两类备份 ——
        #   · `cache/deleted/*`：delete_file 移进来的回收副本（删文件不真删，先搬这里）；
        #   · 配置备份目录里的旧 toml。
        #   二者都会随时间越积越多。保留最近 N 个，其余删掉；带 dry_run 只统计不删。
        keep = int(body.get("keep") or 20)
        dry = bool(body.get("dry_run"))
        out: dict[str, Any] = {"ok": True, "dry_run": dry, "keep": keep}
        # (1) 回收站副本
        try:
            del_root = paths.cache_dir() / "deleted"
            items = []
            if del_root.is_dir():
                items = sorted(
                    (x for x in del_root.iterdir()),
                    key=lambda p: p.stat().st_mtime if p.exists() else 0,
                    reverse=True,
                )
            removed_n, freed = 0, 0
            for it in items[keep:]:
                try:
                    sz = it.stat().st_size if it.is_file() else 0
                    if it.is_dir():
                        sz = sum(f.stat().st_size for f in it.rglob("*") if f.is_file())
                    if not dry:
                        if it.is_dir():
                            _shutil.rmtree(it, ignore_errors=True)
                        else:
                            it.unlink()
                    removed_n += 1
                    freed += sz
                except OSError:
                    continue
            out["deleted_backups"] = {"total": len(items), "removed": removed_n, "freed_bytes": freed}
        except Exception as e:
            out["deleted_backups"] = {"error": str(e)}
        # (2) 配置备份
        try:
            bdir = _config_backup_dir()
            cfgs = sorted(
                (x for x in bdir.glob("*.toml")),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            removed_c, freed_c = 0, 0
            for it in cfgs[keep:]:
                try:
                    sz = it.stat().st_size
                    if not dry:
                        it.unlink()
                    removed_c += 1
                    freed_c += sz
                except OSError:
                    continue
            out["config_backups"] = {"total": len(cfgs), "removed": removed_c, "freed_bytes": freed_c}
        except Exception as e:
            out["config_backups"] = {"error": str(e)}
        if not dry:
            out["freed_total"] = int(
                (out.get("deleted_backups") or {}).get("freed_bytes", 0)
                + (out.get("config_backups") or {}).get("freed_bytes", 0)
            )
        return _json(out)

    return _err(f"未知操作：{action}", 400)


def _config_backup_dir() -> Path:
    from .. import paths
    d = paths.home() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_config() -> str | None:
    """把当前配置文件复制一份带时间戳的备份。"""
    import shutil as _shutil
    import time as _time

    from .. import paths
    src = paths.config_file()
    if not src.is_file():
        return None
    dst = _config_backup_dir() / f"config-{_time.strftime('%Y%m%d-%H%M%S')}.toml"
    _shutil.copy2(src, dst)
    return str(dst)


def _list_config_backups() -> list[dict[str, Any]]:
    """列出已有配置备份（新的在前）。"""
    import time as _time

    try:
        d = _config_backup_dir()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    try:
        for f in sorted(d.glob("config-*.toml"), reverse=True)[:20]:
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({
                "name": f.name,
                "path": str(f),
                "size": st.st_size,
                "created_at": st.st_mtime,
                "ago": _time.time() - st.st_mtime,
            })
    except Exception:
        pass
    return out


def _restore_config(name: str) -> tuple[bool, str]:
    """用指定备份覆盖当前配置；覆盖前先把现状再备份一次。"""
    import shutil as _shutil

    from .. import paths
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return False, "备份名不合法"
    src = _config_backup_dir() / name
    if not src.is_file():
        return False, "备份不存在"
    dst = paths.config_file()
    try:
        if dst.is_file():
            _shutil.copy2(dst, _config_backup_dir() / f"config-before-restore-{name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(src, dst)
    except OSError as e:
        return False, f"写入失败：{e}"
    return True, f"已回滚到 {name}"


async def api_instruction_files(request: Any) -> Response:
    """列出工作区里的「指令文件」（每轮对话自动注入的规矩文件）。

    对齐常见 agent 约定：AGENTS.md / .fengcode/AGENTS.md / CLAUDE.md / .cursorrules。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    import os as _os

    from ..storage.workspaces import get_workspaces

    store = get_workspaces()
    ws_id = request.query_params.get("ws") or ""
    ws = store.get(ws_id) if ws_id else store.default()
    root = Path(_os.path.abspath(str((ws or {}).get("path") or ".")))

    candidates = [
        ("AGENTS.md", "项目约定与规则（推荐）"),
        (".fengcode/AGENTS.md", "Fengcode 专用约定"),
        ("CLAUDE.md", "兼容 Claude Code 的约定文件"),
        (".cursorrules", "兼容 Cursor 的规则文件"),
    ]
    files = []
    for rel, purpose in candidates:
        p = root / rel
        try:
            exists = p.is_file()
            size = p.stat().st_size if exists else 0
        except OSError:
            exists, size = False, 0
        files.append({
            "name": rel, "path": rel, "purpose": purpose,
            "exists": exists, "size": size,
        })
    return _json({"ok": True, "root": str(root), "files": files})


async def api_sandbox_probe(request: Any) -> Response:
    """探测本机可用的 Shell 与常见依赖工具（对齐沙箱设置页）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    import os as _os
    import shutil as _shutil

    def which(name: str) -> str | None:
        try:
            return _shutil.which(name)
        except Exception:
            return None

    def first_existing(paths: list[str]) -> str | None:
        for p in paths:
            try:
                if _os.path.isfile(p):
                    return p
            except OSError:
                continue
        return None

    pwsh7 = first_existing([
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Program Files (x86)\PowerShell\7\pwsh.exe",
    ]) or which("pwsh")
    winps = first_existing([
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ]) or which("powershell")
    # Git Bash 必须来自 Git 安装目录：不能把 WSL 的 system32\bash.exe 当成 Git Bash
    git_bash = first_existing([
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ])
    if not git_bash:
        cand = which("bash") or ""
        if cand and "system32" not in cand.lower() and "windowsapps" not in cand.lower():
            git_bash = cand
    git = which("git")
    wsl = which("wsl")

    # 当前实际会用哪个 —— ★ 必须与 sandbox 的 bash 策略对齐，
    # 否则界面显示的「当前使用」与实际执行的解释器是两回事（用户改完看不到变化）。
    import platform
    mode = "auto"
    try:
        mode = str(getattr(get_manager().config.sandbox, "bash", "") or "auto")
    except Exception:
        pass
    if platform.system() == "Windows":
        if mode == "on" and git_bash:
            current, current_label = git_bash, "Git Bash"
        elif mode == "off":
            current = winps or pwsh7 or "cmd"
            current_label = "Windows PowerShell" if winps else ("PowerShell 7" if pwsh7 else "cmd")
        else:
            current = pwsh7 or winps or "cmd"
            current_label = "PowerShell 7" if pwsh7 else ("Windows PowerShell" if winps else "cmd")
    else:
        current = which("bash") or "/bin/sh"
        current_label = "bash"

    items = [
        {"key": "pwsh7", "label": "pwsh (PowerShell 7+)", "path": pwsh7 or "",
         "found": bool(pwsh7), "hint": "新版跨平台 PowerShell"},
        {"key": "winps", "label": "Windows PowerShell", "path": winps or "",
         "found": bool(winps), "hint": "系统自带 5.1"},
        {"key": "git_bash", "label": "Git Bash", "path": git_bash or "",
         "found": bool(git_bash), "hint": "安装 Git for Windows 后可用"},
        {"key": "git", "label": "Git（依赖工具）", "path": git or "",
         "found": bool(git), "hint": "版本控制与 Git 相关技能依赖"},
        {"key": "wsl", "label": "WSL", "path": wsl or "",
         "found": bool(wsl), "hint": "Windows 子系统 Linux"},
    ]
    return _json({
        "ok": True,
        "current": current,
        "current_label": current_label,
        "items": items,
        "os": platform.system(),
    })


async def api_workspace_file_read(request: Any) -> Response:
    """读取工作区里的一个文本文件（供文件预览）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    import os as _os

    from ..storage.workspaces import get_workspaces

    store = get_workspaces()
    ws_id = request.query_params.get("ws") or ""
    ws = store.get(ws_id) if ws_id else store.default()
    if not ws:
        return _err("工作区不存在", 404)
    root = Path(_os.path.abspath(str(ws.get("path") or "")))
    rel = (request.query_params.get("path") or "").strip().replace("\\", "/")
    if not rel:
        return _err("缺少 path", 400)
    target = Path(_os.path.abspath(str(root / rel)))
    try:
        target.relative_to(root)
    except ValueError:
        return _err("路径越界", 400)
    if not target.is_file():
        return _err("文件不存在", 404)
    # 限制大小，避免把大文件塞进界面
    try:
        if target.stat().st_size > 2 * 1024 * 1024:
            return _err("文件太大（超过 2MB），请用编辑器打开", 413)
    except OSError:
        pass
    # 二进制判定
    try:
        raw = target.read_bytes()[:4096]
        if b"\x00" in raw:
            return _err("这是二进制文件，无法预览", 415)
    except Exception:
        pass
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return _err(f"读取失败：{e}", 500)
    return _json({"ok": True, "path": rel, "content": text,
                  "size": target.stat().st_size})


async def api_workspace_file_write(request: Any) -> Response:
    """保存工作区里的文本文件（文件预览页可直接编辑）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    import os as _os

    from ..storage.workspaces import get_workspaces

    store = get_workspaces()
    ws_id = request.query_params.get("ws") or ""
    ws = store.get(ws_id) if ws_id else store.default()
    if not ws:
        return _err("工作区不存在", 404)
    root = Path(_os.path.abspath(str(ws.get("path") or "")))
    body = await _body(request)
    rel = str(body.get("path") or "").strip().replace("\\", "/")
    if not rel:
        return _err("缺少 path", 400)
    target = Path(_os.path.abspath(str(root / rel)))
    try:
        target.relative_to(root)
    except ValueError:
        return _err("路径越界", 400)
    content = str(body.get("content") or "")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    except Exception as e:
        return _err(f"写入失败：{e}", 500)
    return _json({"ok": True, "path": rel, "size": target.stat().st_size})


async def api_session_detail(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.sessions import SessionStore

    sid = request.path_params["sid"]
    store = SessionStore()
    s = store.get(sid)
    if s is None:
        return _err("会话不存在", 404)
    if request.method == "DELETE":
        store.delete(sid)
        STATE.drop_agent(sid)
        return _json({"ok": True})
    if request.method == "PATCH":
        body = await _body(request)
        allowed = {k: v for k, v in body.items()
                   if k in ("title", "pinned", "archived", "model", "mode", "workspace")}
        if allowed:
            store.update(sid, **allowed)
        return _json({"ok": True, "session": store.get(sid)})
    # GET：带消息
    limit = int(request.query_params.get("limit") or 500)
    # ★ 1-H：加载失败时给出**具体原因**，而不是一句笼统的提示（排查成本高）。
    try:
        msgs = store.raw_messages(sid, limit=limit)
    except Exception as e:
        code, hint = "load_failed", "读取会话历史时出错"
        name = type(e).__name__
        low = str(e).lower()
        if "json" in low or "decode" in low:
            code, hint = "corrupted", "会话数据损坏（JSON 解析失败）"
        elif "memory" in low or "too large" in low or "size" in low:
            code, hint = "too_large", "会话过大，读取超出可用内存"
        elif isinstance(e, (OSError, PermissionError)):
            code, hint = "io_error", "读取文件失败（可能在别处被占用或权限不足）"
        return _err(f"{hint}：{name}: {e}", 500, code=code, session_id=sid)
    # 顺带做一次完整性体检：会话记录里的角色应当是已知值
    unknown = {m.get("role") for m in msgs} - {"user", "assistant", "system", "tool"}
    if unknown:
        return _json({"session": s, "messages": msgs,
                      "warning": f"会话含未知角色 {sorted(unknown)}，可能来自更新/更旧的版本"})
    return _json({"session": s, "messages": msgs})


async def api_session_export(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.sessions import SessionStore

    sid = request.path_params["sid"]
    fmt = (request.query_params.get("format") or "md").lower()
    store = SessionStore()
    s = store.get(sid)
    if s is None:
        return _err("会话不存在", 404)
    if fmt == "json":
        return _json({"session": s, "messages": store.raw_messages(sid)})
    md = store.export_markdown(sid)
    import urllib.parse

    fname = urllib.parse.quote(f"{s['title'][:40]}.md")
    return Response(
        md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
    )


async def api_chat(request: Any) -> Response:
    """发起一次对话（支持流式 SSE 与非流式）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    body = await _body(request)
    text = str(body.get("message") or body.get("text") or "").strip()
    sid = body.get("session_id")
    model = body.get("model")
    stream = bool(body.get("stream", True))
    # 工作模式：'' / None = 不指定（AI 自主判断），'plan' = 计划，'goal' = 目标。
    # 兼容旧前端的 'chat' —— 已归一化为「不选」。
    raw_mode = body.get("mode")
    mode: str | None = None if raw_mode is None else normalize_mode(raw_mode)
    if not text and not body.get("attachments"):
        return _err("message 不能为空")
    # ★ 解析附件（图片/文件）。旧代码只拿它做非空校验就丢掉了，
    #   于是用户拖进来的图片**从来没有发给模型** —— 这是「假多模态」的关键断点。
    atts = _parse_attachments(body.get("attachments"))
    # ★ 图片两道闸：能力门控 + 超尺寸拦截。
    #   本地先挡住并说清原因，比让上游返回 400 把整轮对话废掉便宜得多。
    try:
        _why = _check_images(atts, model_ref=model or sid)
        if _why:
            return _err(_why, 400, code="image_rejected")
    except Exception:
        pass
    ag = await STATE.agent(sid)
    if model:
        ag.sessions.update(ag.session_id, model=model)

    # ★ 会话写入租约（轻量版）：同一会话同时只允许一个回合在写。
    #   为什么：agent() 的锁只管「创建 Agent」，建好之后两个请求能同时进 run()，
    #   交错往同一会话追加消息 —— 历史顺序会乱、用量统计互相覆盖。
    #   冲突时**明确报错**并给出占用者，而不是静默交错（用户要的是知道「有人在写」）。
    holder = new_id("turn")
    ok, busy_by = STATE.acquire_session(ag.session_id, holder)
    if not ok:
        # ★ 排队等待中的会话：它「忙」是正常的，报「正在执行上一轮」会让人以为
        #   前一个回合卡住了。这里如实说出它在等什么。
        qs = STATE.workspace_queue_state(ag.session_id)
        if qs.get("waiting"):
            return _err(
                f"这个对话正在排队等待工作区（第 {qs['position']} 位，"
                f"当前由「{qs['busy_by_label']}」占用）。要改主意请先点停止。",
                409,
                code="workspace_queued",
                session_id=ag.session_id,
                queue=qs,
            )
        return _err(
            f"这个会话正在执行上一轮（{busy_by}），请等它结束或先点停止再发。",
            409,
            code="session_busy",
            session_id=ag.session_id,
        )

    # ★ 工作区写租约：同一目录同时只允许一个会话在**写**，其余排队接力。
    #   键是**归一化后的目录**：项目改名、重建都还是同一个目录，
    #   而真正互斥的是「对同一批文件的写入」（见 AppState.workspace_key）。
    #   ★★ 注意（本版改动）：这里**不再**一开始就抢锁 —— 见下方按需门。
    # ★★★ 工作区写租约改为「按需获取」：把门交给 Agent，只有它某一步真的要执行
    #   会写工作区的工具时才排队等锁（见 Agent._ensure_workspace_lease）。
    #   为什么改：旧实现让**每个回合**从第一步就抢锁，于是同一目录下另一个对话
    #   哪怕只是问一句话、答一段字也会被排队 —— 用户看到的就是「新对话连基础
    #   对话都不能用，一发就显示排队」。真正需要互斥的只有「对同一批文件的写入」。
    ag._ws_gate = STATE.make_workspace_gate(ag, holder, ag.session_id)

    if not stream:
        try:
            res = await ag.run(text, model=model, stream=False, mode=mode, attachments=atts)
        finally:
            STATE.release_session(ag.session_id, holder)
            # ★ 按持有者释放：门是中途才取的锁，取没取到只有持有者标识能对上。
            STATE.release_workspace_holder(holder)
            ag._ws_gate = None
        return _json({"ok": not res.error, "result": res.to_dict(), "session_id": ag.session_id})

    # SSE 流式
    queue = STATE.bus.subscribe()
    target_sid = ag.session_id

    async def gen():
        task = None
        try:
            yield _sse({"type": "start", "session_id": target_sid})
            # ★ 不再在回合开头排队：直接开工，写操作到来时才由 Agent 取锁。
            task = asyncio.create_task(
                ag.run(text, model=model, stream=True, mode=mode, attachments=atts)
            )
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if task.done():
                        break
                    yield ": keepalive\n\n"
                    continue
                if ev.session_id and ev.session_id != target_sid:
                    continue
                yield _sse(ev.to_dict())
        finally:
            STATE.bus.unsubscribe(queue)
            # ★ 先释放会话写租约：无论成功、出错还是被中断，都必须还回去，
            #   否则这个会话会被永久判为「有人在写」，之后再也发不出消息。
            STATE.release_session(target_sid, holder)
            # ★ 工作区租约同样必须还：本回合可能中途取过锁，也可能根本没取。
            #   release_workspace_holder 按持有者反查，取没取到都安全。
            STATE.release_workspace_holder(holder)
            ag._ws_gate = None
            if task is None:
                return
            try:
                res = await task
                yield _sse({"type": "result", "data": res.to_dict(), "session_id": target_sid})
            except Exception as e:
                # ★ 出错也必须补一个「回合结束」事件。
                #   旧写法只发 error 就结束：前端收不到 result，流式气泡的 meta 会一直
                #   停在「生成中」，而状态行却已显示「已完成」—— 实测的现象正是
                #   「他先是已完成，但左边显示生成中，也没有输出文字」。
                #   现在 error 之后补 result（带 error 字段），前端统一走收尾逻辑。
                msg = f"{type(e).__name__}: {e}"
                yield _sse({"type": "error", "error": msg, "session_id": target_sid})
                yield _sse({"type": "result", "data": {"error": msg}, "session_id": target_sid})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


async def api_chat_stop(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    body = await _body(request)
    sid = body.get("session_id")
    ag = STATE.agents.get(sid) if sid else STATE.default_agent
    if ag is None:
        return _err("没有正在运行的会话", 404)
    ag.cancel()
    # ★ 排队中的会话还没建 Agent 任务，ag.cancel() 对它无效 —— 它卡在
    #   acquire_workspace 的 future 上。必须单独把它从队列里撤下来，
    #   否则用户点了停止，界面还在「排队中」，而且它会一直占着队位。
    STATE.cancel_workspace_waiter(ag.session_id)
    return _json({"ok": True})


async def api_chat_compact(request: Any) -> Response:
    """★ 2-A：手动压缩当前会话（`/compact` 命令）。

    与自动压缩互不影响：自动阈值、触发时机**都不变**；这只是给用户一个
    「现在就把历史整理一下」的显式入口。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    body = await _body(request)
    sid = body.get("session_id")
    ag = STATE.agents.get(sid) if sid else STATE.default_agent
    if ag is None:
        return _err("没有正在运行的会话", 404)
    # 防重复提交：同一会话同时只允许一个压缩在跑
    if getattr(ag, "_compacting", False):
        return _err("已经有一个压缩在进行中，请稍候", 409)
    ag._compacting = True
    try:
        r = await ag.compact_session(force=True)
    except Exception as e:
        return _err(f"压缩失败：{type(e).__name__}: {e}", 500)
    finally:
        ag._compacting = False
    return _json(r)


async def api_approval(request: Any) -> Response:
    """回填审批结果。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    body = await _body(request)
    rid = str(body.get("id") or "")
    if not rid:
        return _err("id 不能为空")
    allowed = bool(body.get("allowed", False))
    remember = str(body.get("remember") or "once")
    ag = STATE.default_agent
    if body.get("session_id"):
        ag = STATE.agents.get(str(body["session_id"])) or ag
    if ag is None:
        return _err("没有活跃会话", 404)
    ok = ag.approval.resolve_with_key(
        rid, allowed,
        action=str(body.get("action") or ""),
        target=str(body.get("target") or ""),
        session_id=ag.session_id,
        remember=remember,
    )
    return _json({"ok": ok})


async def api_pending_approvals(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    out = []
    for sid, ag in STATE.agents.items():
        for p in ag.approval.pending():
            out.append({**p, "session_id": sid})
    return _json({"pending": out})


async def api_ask(request: Any) -> Response:
    """回填 ask_user 的答案（界面上点选项 / 输入文字后调它）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    body = await _body(request)
    qid = str(body.get("id") or "")
    if not qid:
        return _err("id 不能为空")
    # 多选时 answers 是数组，单选时是字符串
    answer = body.get("answer")
    if answer is None and isinstance(body.get("answers"), list):
        answer = "、".join(str(a) for a in body["answers"])
    ok = STATE.resolve_ask(qid, answer)
    return _json({"ok": ok})


# --------------------------------------------------------------------------
# 路由：任务 / 目标 / 记忆 / 技能 / 工具
# --------------------------------------------------------------------------

async def api_tasks(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.tasks import TaskStore

    sid = request.query_params.get("session_id")
    store = TaskStore()
    if request.method == "GET":
        return _json({"tasks": store.list(session_id=sid), "summary": store.summary(sid),
                      "goals": store.list_goals(active_only=request.query_params.get("active") == "1")})
    body = await _body(request)
    action = str(body.get("action") or "add")
    if action == "add":
        t = store.add(str(body.get("title") or "未命名"), detail=str(body.get("detail") or ""),
                      session_id=sid)
        return _json({"ok": True, "task": t})
    if action == "update":
        t = store.update(str(body.get("id") or ""), **{k: v for k, v in body.items()
                                                       if k in ("title", "detail", "status", "priority")})
        return _json({"ok": t is not None, "task": t})
    if action == "delete":
        return _json({"ok": store.delete(str(body.get("id") or ""))})
    if action == "clear":
        n = store.clear(sid, only_done=bool(body.get("only_done")))
        return _json({"ok": True, "cleared": n})
    return _err(f"未知操作：{action}")


async def api_memories(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..memory.manager import get_memory

    mem = get_memory(STATE.manager.config.memory)
    mem.bind_config_manager(STATE.manager)
    if request.method == "GET":
        if request.query_params.get("stats") == "1":
            return _json(mem.stats())
        items = mem.list(
            scope=request.query_params.get("scope"),
            kind=request.query_params.get("kind"),
            limit=int(request.query_params.get("limit") or 200),
            offset=int(request.query_params.get("offset") or 0),
            include_archived=request.query_params.get("archived") == "1",
            search=request.query_params.get("search"),
            sort=request.query_params.get("sort") or "updated",
        )
        return _json({"memories": [i.to_dict() for i in items], "stats": mem.stats()})
    body = await _body(request)
    action = str(body.get("action") or "remember")
    if action == "remember":
        item = await mem.remember(
            str(body.get("content") or ""),
            title=str(body.get("title") or ""),
            kind=str(body.get("kind") or "fact"),
            tags=[str(t) for t in (body.get("tags") or [])],
            importance=float(body.get("importance") or 0.6),
            pinned=bool(body.get("pinned")),
            # ★ 记忆维度补强：易变性 / 过期日 / 主题键一并透传。
            #   topic_key 用于「同一主题只留一个活跃值」，避免两条矛盾记忆同时存在。
            volatility=str(body.get("volatility") or "stable"),
            expires_at=float(body.get("expires_at") or 0),
            topic_key=str(body.get("topic_key") or ""),
        )
        return _json({"ok": True, "memory": item.to_dict()})
    if action == "update":
        item = mem.update(str(body.get("id") or ""), **{k: v for k, v in body.items()
                                                        if k in ("title", "content", "tags",
                                                                 "importance", "pinned", "archived",
                                                                 "volatility", "expires_at",
                                                                 "topic_key")})
        return _json({"ok": item is not None, "memory": item.to_dict() if item else None})
    if action == "forget":
        return _json({"ok": mem.forget(str(body.get("id") or ""))})
    if action == "reindex":
        return _json({"ok": True, "count": mem.reindex_all()})
    # ★ 记忆修订历史与撤回（记忆改了能回退，也看得清改过什么」）
    if action == "revisions":
        mid = str(body.get("id") or "")
        if not mid:
            return _err("id 不能为空")
        return _json({"ok": True, "revisions": mem.revisions(mid, limit=int(body.get("limit") or 50))})
    if action == "restore":
        mid = str(body.get("id") or "")
        if not mid:
            return _err("id 不能为空")
        rev = body.get("revision")
        item = mem.restore(mid, int(rev) if rev is not None else None)
        if item is None:
            return _err("没有可回退的版本（可能已是第一版）", 404)
        return _json({"ok": True, "memory": item.to_dict()})
    if action == "purge":
        n = await mem.forget_old(days=float(body.get("days") or 180))
        return _json({"ok": True, "removed": n})
    if action == "recall":
        items = await mem.recall(str(body.get("query") or ""), top_k=int(body.get("top_k") or 6))
        return _json({"items": [i.to_dict() for i in items]})
    return _err(f"未知操作：{action}")


def _skills_list() -> list[dict[str, Any]]:
    try:
        from ..skills.manager import get_skills
        from ..storage.db import get_db

        sm = get_skills(STATE.manager.config, get_db())
        return sm.list_all()
    except Exception:
        return []


async def api_skills(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..skills.manager import get_skills
    from ..storage.db import get_db

    sm = get_skills(STATE.manager.config, get_db())
    if request.method == "GET":
        if request.query_params.get("reload") == "1":
            sm.reload()
            sm.sync_state()
        name = request.query_params.get("name")
        if name:
            d = sm.load(name)
            return _json({"skill": d}) if d else _err("未找到技能", 404)
        q = request.query_params.get("q")
        if q:
            return _json({"skills": sm.search(q)})
        return _json({"skills": sm.list_all()})
    body = await _body(request)
    action = str(body.get("action") or "")
    name = str(body.get("name") or "")
    if action == "reload":
        sm.reload()
        n = sm.sync_state()
        return _json({"ok": True, "count": n, "skills": sm.list_all()})
    if action == "toggle":
        ok = sm.set_enabled(name, bool(body.get("enabled", True)))
        return _json({"ok": ok})
    if action == "delete":
        from pathlib import Path as _P

        info = sm.get(name)
        if info is None:
            return _err("未找到", 404)
        if info.source == "builtin":
            return _err("内置技能不能删除", 400)
        with contextlib.suppress(Exception):
            _P(info.path).unlink()
        sm.unregister(name)
        return _json({"ok": True})
    if action == "create":
        return await _create_skill(body)
    return _err(f"未知操作：{action}")


async def _create_skill(body: dict[str, Any]) -> Response:
    """在用户技能目录新建一个 SKILL.md。"""
    from .. import paths
    from ..utils import sanitize_filename

    name = str(body.get("name") or "").strip()
    if not name:
        return _err("name 不能为空")
    content = str(body.get("content") or "")
    desc = str(body.get("description") or "")
    if not content:
        content = (
            f"---\nname: {name}\ndescription: {desc or '（请填写说明）'}\n"
            f"version: 1.0.0\ntags: []\n---\n\n# {name}\n\n（在这里写操作指引）\n"
        )
    d = paths.skills_dir() / sanitize_filename(name, "skill")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8", newline="\n")
    from ..skills.manager import get_skills
    from ..storage.db import get_db

    sm = get_skills(STATE.manager.config, get_db())
    sm.reload()
    sm.sync_state()
    return _json({"ok": True, "dir": str(d)})


async def api_tools(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..tools import tool_groups
    from ..tools.base import get_registry

    reg = get_registry()
    if not reg.all():
        from ..tools import register_builtin

        register_builtin(reg)
    if request.method == "GET":
        return _json({"groups": tool_groups(reg), "mcp": STATE.mcp.specs() if STATE.mcp else [],
                      "plugins": STATE.plugins.tool_specs() if STATE.plugins else []})
    body = await _body(request)
    name = str(body.get("name") or "")
    enabled = bool(body.get("enabled", True))
    if enabled:
        reg.enable(name)
    else:
        reg.disable(name)
    return _json({"ok": True, "enabled": not reg.is_disabled(name)})


async def api_tool_run(request: Any) -> Response:
    """手动执行一个工具（界面里的"试运行"）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    body = await _body(request)
    name = str(body.get("name") or "")
    args = body.get("arguments") or {}
    if not name:
        return _err("name 不能为空")
    ag = await STATE.agent(body.get("session_id"))
    ctx = ag.tool_context()
    if name.startswith("mcp__") or (STATE.mcp and STATE.mcp.find(name)):
        ok, text, data = await STATE.mcp.call_tool(name, args)
        return _json({"ok": ok, "content": text, "data": data})
    res = await ag.registry.execute(name, args, ctx)
    return _json({"ok": res.ok, "result": res.to_dict()})


# --------------------------------------------------------------------------
# 路由：MCP / 插件 / 工作流 / 定时 / 远程 / 用量
# --------------------------------------------------------------------------

async def api_mcp(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    mgr = STATE.manager
    if request.method == "GET":
        from ..mcp.importers import export_mcp_json, scan_default_configs

        st = STATE.mcp.status() if STATE.mcp else []
        servers = [
            {
                "name": s.name,
                "type": s.type,
                "command": s.command,
                "args": list(s.args or []),
                "url": s.url,
                "enabled": s.enabled,
            }
            for s in mgr.config.mcp.servers
        ]
        from ..mcp.client import ServerState

        exportable = [
            ServerState(
                name=s.name, type=s.type, command=s.command or "", args=list(s.args or []),
                url=s.url or "", env=dict(s.env or {}), headers=dict(s.headers or {}),
            )
            for s in mgr.config.mcp.servers
        ]
        return _json({
            "servers": st,
            "configured": servers,
            "configs": scan_default_configs(),
            "export": export_mcp_json(exportable),
            "enabled": mgr.config.mcp.enabled,
        })
    body = await _body(request)
    action = str(body.get("action") or "")
    if action == "add":
        from ..config.schema import McpServerConfig

        try:
            srv = McpServerConfig(
                name=str(body.get("name") or ""),
                type=str(body.get("type") or "stdio"),
                command=body.get("command") or None,
                args=[str(a) for a in (body.get("args") or [])],
                url=body.get("url") or None,
                env={str(k): str(v) for k, v in (body.get("env") or {}).items()},
                headers={str(k): str(v) for k, v in (body.get("headers") or {}).items()},
                enabled=bool(body.get("enabled", True)),
            )
        except Exception as e:
            return _err(f"配置不合法：{e}")
        mgr.config.mcp.servers.append(srv)
        mgr.save()
        if STATE.mcp is not None:
            with contextlib.suppress(Exception):
                await STATE.mcp.start_server(srv.name)
        return _json({"ok": True})
    if action == "delete":
        name = str(body.get("name") or "")
        if STATE.mcp is not None:
            with contextlib.suppress(Exception):
                await STATE.mcp.stop_server(name)
        mgr.config.mcp.servers = [s for s in mgr.config.mcp.servers if s.name != name]
        mgr.save()
        return _json({"ok": True})
    if action in ("start", "restart"):
        if STATE.mcp is None:
            return _err("MCP 未启用")
        try:
            st = await STATE.mcp.start_server(str(body.get("name") or ""))
            return _json({"ok": True, "server": st.to_dict()})
        except Exception as e:
            return _err(str(e), 500)
    if action == "stop":
        if STATE.mcp is not None:
            await STATE.mcp.stop_server(str(body.get("name") or ""))
        return _json({"ok": True})
    if action == "test":
        if STATE.mcp is None:
            return _err("MCP 未启用")
        res = await STATE.mcp.test(str(body.get("name") or ""))
        return _json(res)
    if action == "refresh_tools":
        # ★ 目录变更后重拉工具列表：服务器运行中新增/移除了工具时，
        #   不重拉模型看到的还是旧清单（调不到新工具、或调到已删的）。
        if STATE.mcp is None:
            return _err("MCP 未启用")
        res = await STATE.mcp.refresh_dir_tools()
        return _json({"ok": True, "servers": res})
    if action == "start_all":
        # ★ 后台冷启动：不阻塞界面，连上的陆续挂出工具。
        if STATE.mcp is None:
            return _err("MCP 未启用")
        res = await STATE.mcp.start_all(background=bool(body.get("background", True)))
        return _json({"ok": True, "results": res})
    if action == "import_file":
        from ..mcp.importers import load_from_file

        res = load_from_file(str(body.get("path") or ""))
        added = []
        for s in res.get("servers") or []:
            if any(x.name == s.name for x in mgr.config.mcp.servers):
                continue
            from ..config.schema import McpServerConfig

            mgr.config.mcp.servers.append(
                McpServerConfig(
                    name=s.name, type=s.type, command=s.command or None, args=list(s.args),
                    url=s.url or None, env=dict(s.env), headers=dict(s.headers), enabled=s.enabled,
                )
            )
            added.append(s.name)
        mgr.save()
        return _json({"ok": True, "added": added, "errors": res.get("errors") or []})
    return _err(f"未知操作：{action}")


async def api_plugins(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    if STATE.plugins is None:
        return _json({"plugins": [], "panels": [], "stats": {}})
    pm = STATE.plugins
    if request.method == "GET":
        return _json({"plugins": pm.list(), "panels": pm.panel_list(), "stats": pm.stats()})
    body = await _body(request)
    action = str(body.get("action") or "")
    name = str(body.get("name") or "")
    try:
        if action == "enable":
            ok = pm.enable(name, True)
        elif action == "disable":
            ok = pm.enable(name, False)
        elif action == "reload":
            pm.load_all()
            ok = True
        elif action == "load":
            pm.load(name)
            ok = True
        elif action == "unload":
            ok = pm.unload(name)
        elif action == "install":
            res = pm.install(str(body.get("path") or ""), name=body.get("as_name"),
                             overwrite=bool(body.get("overwrite")))
            return _json(res)
        elif action == "create":
            res = _create_plugin(body)
            return _json(res)
        elif action == "uninstall":
            ok = pm.uninstall(name)
        else:
            return _err(f"未知操作：{action}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}", 500)
    return _json({"ok": ok, "plugins": pm.list()})


def _create_plugin(body: dict[str, Any]) -> dict[str, Any]:
    """在用户插件目录新建一个插件骨架（manifest.yaml + plugin.py）。"""
    from .. import paths
    from ..utils import sanitize_filename

    name = str(body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "插件名不能为空"}
    display = str(body.get("display_name") or name).strip()
    desc = str(body.get("description") or "（请填写说明）").strip()
    slug = sanitize_filename(name, "plugin")

    d = paths.plugins_dir() / slug
    if d.exists() and not body.get("overwrite"):
        return {"ok": False, "error": f"插件已存在：{slug}"}
    d.mkdir(parents=True, exist_ok=True)

    manifest = (
        f"name: {slug}\n"
        f"display_name: {display}\n"
        f"version: 1.0.0\n"
        f"description: {desc}\n"
        f"author: 我自己\n"
        f"license: MIT\n"
        f"main: plugin.py\n"
        f"enabled: true\n"
        f"permissions: [tools]\n"
        f"tools:\n"
        f"  - name: hello\n"
        f"    description: 示例工具：返回一句问候\n"
        f"    parameters:\n"
        f"      who: {{type: string, description: 问候对象}}\n"
        f"    required: []\n"
    )
    (d / "manifest.yaml").write_text(manifest, encoding="utf-8", newline="\n")

    skeleton = (
        f'"""插件：{display}\n\n'
        f'{desc}\n\n'
        f'改这个文件即可扩展功能。setup(api) 是入口，\n'
        f'用 api.register_tool(...) 注册你自己的工具。\n'
        f'"""\n\n'
        f'from __future__ import annotations\n\n\n'
        f'def setup(api):  # noqa: ANN001\n'
        f'    """插件入口。"""\n\n'
        f'    def hello(who: str = "世界"):\n'
        f'        """示例工具：返回一句问候。"""\n'
        f'        return f"你好，{{who}}！这是来自插件「{display}」的问候。"\n\n'
        f'    api.register_tool(\n'
        f'        "hello",\n'
        f'        hello,\n'
        f'        description="示例工具：返回一句问候",\n'
        f'        parameters={{"who": {{"type": "string", "description": "问候对象"}}}},\n'
        f'    )\n'
    )
    (d / "plugin.py").write_text(skeleton, encoding="utf-8", newline="\n")

    return {"ok": True, "dir": str(d), "name": slug}


async def api_plugin_panel(request: Any) -> Response:
    """托管插件自带的 UI 面板静态文件。"""
    if STATE.plugins is None:
        return _err("插件未启用", 404)
    name = request.path_params["name"]
    rel = request.path_params.get("path") or "index.html"
    man = STATE.plugins._find(name)
    if man is None:
        return _err("插件不存在", 404)
    base = Path(man.dir).resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        return _err("文件不存在", 404)
    return FileResponse(target)


async def api_workflows(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.tasks import WorkflowStore

    store = WorkflowStore()
    if request.method == "GET":
        if request.query_params.get("sample") == "1":
            from ..workflow.engine import sample_dag

            return _json({"sample": sample_dag()})
        wid = request.query_params.get("id")
        if wid:
            wf = store.get(wid)
            return _json({"workflow": wf}) if wf else _err("未找到", 404)
        return _json({"workflows": store.list()})
    body = await _body(request)
    action = str(body.get("action") or "save")
    if action == "save":
        wf = store.save(
            str(body.get("name") or "未命名流程"),
            body.get("dag") or {},
            description=str(body.get("description") or ""),
            workflow_id=body.get("id"),
        )
        return _json({"ok": True, "workflow": wf})
    if action == "delete":
        return _json({"ok": store.delete(str(body.get("id") or ""))})
    if action == "run":
        wf = store.get(str(body.get("id") or ""))
        if wf is None:
            return _err("未找到工作流", 404)
        from ..workflow.engine import WorkflowRunner, from_dict

        ag = await STATE.agent(body.get("session_id"))
        runner = WorkflowRunner(
            agent_runner=lambda p: ag.run(p),
            tool_registry=ag.registry, ctx=ag.tool_context(), bus=STATE.bus,
            subagent_runner=ag.subagent_runner,
        )
        out = await runner.run(from_dict(wf.get("dag") or {}), workflow_id=wf["id"])
        return _json({"ok": out.get("ok"), "summary": out.get("summary"),
                      "results": out.get("results")})
    if action == "validate":
        from ..workflow.engine import from_dict, validate

        steps = from_dict(body.get("dag") or {})
        ok, err = validate(steps)
        return _json({"ok": ok, "error": err, "steps": len(steps)})
    return _err(f"未知操作：{action}")


async def api_jobs(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..scheduler.cron import describe_schedule, next_run_describe, parse_cron
    from ..storage.tasks import JobStore

    store = JobStore()
    if request.method == "GET":
        jobs = store.list()
        for j in jobs:
            j["schedule"] = describe_schedule(j)
            # ★ 下次运行描述：直接告诉用户「具体几点、还有多久」，
            #   而不是让他拿 cron 表达式自己心算。
            j["next_run_text"] = next_run_describe(j)
        return _json({"jobs": jobs, "enabled": STATE.manager.config.scheduler.enabled})
    body = await _body(request)
    action = str(body.get("action") or "save")
    if action == "save":
        cron_expr = str(body.get("cron") or "").strip()
        if cron_expr and not parse_cron(cron_expr):
            return _err("cron 表达式无效（需要 5 段，如 0 3 * * *）")
        # ★ 时间窗口：HH:MM 形式，可只填一端；跨零点自动识别。
        #   非法格式直接拒绝，避免「填了个看不懂的值、任务永远不跑」这种静默失效。
        w_start = str(body.get("window_start") or "").strip()
        w_end = str(body.get("window_end") or "").strip()
        for _w in (w_start, w_end):
            if _w and not _re.match(r"^\d{1,2}:\d{2}$", _w):
                return _err("时间窗口格式应为 HH:MM（如 09:00），可留空表示不限制")
        payload = dict(body.get("payload") or {})
        # 窗口随 payload 一起持久化（jobs 表已有 payload 列，不额外加列）
        if w_start or w_end:
            payload["_window"] = {"start": w_start, "end": w_end}
        else:
            payload.pop("_window", None)
        j = store.save(
            name=str(body.get("name") or "未命名任务"),
            cron=cron_expr,
            interval_seconds=float(body.get("interval_seconds") or 0),
            kind=str(body.get("kind") or "prompt"),
            payload=payload,
            enabled=bool(body.get("enabled", True)),
            job_id=body.get("id"),
        )
        return _json({"ok": True, "job": j})
    if action == "delete":
        return _json({"ok": store.delete(str(body.get("id") or ""))})
    if action == "run_now":
        if STATE._scheduler is None:
            return _err("调度器未启动", 400)
        res = await STATE._scheduler.run_now(str(body.get("id") or ""))
        return _json(res)
    if action == "runs":
        return _json({"runs": store.runs(str(body.get("id") or ""))})
    return _err(f"未知操作：{action}")


async def api_remote(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    mgr = STATE.manager
    if request.method == "GET":
        return _json({"hosts": [
            {"name": h.name, "host": h.host, "port": h.port, "user": h.user,
             "has_password": bool(h.password or h.password_env), "key_file": h.key_file}
            for h in mgr.config.remote.hosts
        ]})
    body = await _body(request)
    action = str(body.get("action") or "")
    if action == "add":
        from ..config.schema import RemoteHost

        h = RemoteHost(
            name=str(body.get("name") or body.get("host") or "主机"),
            host=str(body.get("host") or ""), port=int(body.get("port") or 22),
            user=str(body.get("user") or "root"), password=body.get("password"),
            password_env=body.get("password_env"), key_file=body.get("key_file"),
            workspace=str(body.get("workspace") or "~"),
        )
        mgr.config.remote.hosts.append(h)
        mgr.save()
        return _json({"ok": True})
    if action == "delete":
        name = str(body.get("name") or "")
        mgr.config.remote.hosts = [h for h in mgr.config.remote.hosts if h.name != name]
        mgr.save()
        return _json({"ok": True})
    if action == "test":
        from ..tools.builtin.remote import _connect, _get_host

        ag = await STATE.agent()
        ctx = ag.tool_context()
        ip, info = _get_host(ctx, str(body.get("name") or ""))
        if ip is None:
            return _err(str(info), 400)
        t0 = time.time()
        try:
            c = await asyncio.to_thread(_connect, info)
            await asyncio.to_thread(c.close)
            return _json({"ok": True, "duration": round(time.time() - t0, 2)})
        except Exception as e:
            return _json({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return _err(f"未知操作：{action}")


async def api_stats(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.stats import AuditStore, StatsStore

    st = StatsStore()
    if request.query_params.get("audit") == "1":
        limit = int(request.query_params.get("limit") or 100)
        return _json({"audit": AuditStore().recent(limit)})
    if request.query_params.get("recent") == "1":
        return _json({"recent": st.recent(int(request.query_params.get("limit") or 50))})
    # 带上会话 id：概览面板的「累计 tokens / 请求数 / 费用」要按当前会话统计，
    # 而不是全库历史总和（用户会把自己的用量误读成整个安装的量）。
    return _json(st.overview(session_id=request.query_params.get("session_id") or None))


async def api_workspace_lock(request: Any) -> Response:
    """查某个会话的工作区占用与排队状态。

    ★ 为什么需要一个「拉」的接口，而不是只靠事件推：事件只在状态**变化**时发。
      用户把界面切到别的对话再切回来时，这一路的排队事件早就发过了 ——
      没有这个接口，切回来的界面会一直显示「排队中」或干脆什么都不显示，
      直到服务端下一次状态变化（可能要等前一个回合跑完）。
    ★ ``running`` 一并给出：前端切回会话时据此决定要不要接回这条流的输出
      （在跑 = 有内容会来；只排队 = 还没开工）。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    sid = request.query_params.get("session_id") or ""
    if not sid:
        return _json({"ok": True, "state": {}})
    key = STATE.workspace_key(sid)
    holder = STATE.workspace_busy(key)
    qs = STATE.workspace_queue_state(sid)
    # ★ "在跑"用已有状态推导，不新增一份易失的标记：持有会话写租约、且不在
    #   排队队列里 = 这个会话的回合真的在跑（排队中的会话持有租约但没开工）。
    #   另起一个 self.running 字段迟早会与真实状态不同步。
    running = bool(STATE.session_busy(sid)) and not qs.get("waiting")
    state = {
        "workspace": key,
        # 持有者自己的界面不需要看到「被别人占用」的提示
        "held_by_me": bool(holder) and holder == sid,
        "busy_by": holder,
        "busy_by_label": STATE._session_label(holder) if holder else "",
        "waiting": bool(qs.get("waiting")),
        "position": int(qs.get("position") or 0),
        "running": running,
    }
    return _json({"ok": True, "state": state})


async def api_subagents(request: Any) -> Response:
    if not _auth_ok(request):
        return _err("未授权", 401)
    ag = await STATE.agent(request.query_params.get("session_id"))
    # ★ 2-K：一并给出运行态 —— 谁在排队、谁在跑，供界面展示
    runner = ag.subagent_runner
    live = runner.live() if hasattr(runner, "live") else []
    # ★ 运行图：额外给「谁在等谁、被哪条约束挡住」的图数据。
    #   没有依赖关系时 edges 为空，界面退化成列表 —— 不强制画图。
    graph = runner.graph() if hasattr(runner, "graph") else {"nodes": [], "edges": [], "counts": {}}
    return _json({"types": runner.list_types(), "live": live, "graph": graph})


async def api_queue(request: Any) -> Response:
    """待发队列的持久化（实测「排队指令一刷新就没了」）。

    ★ 为什么要有这个接口：排队只是前端内存里的数组，刷新/重开页面即丢。
      用户排了三条指令、手一抖刷新，三条全没了 —— 所以改成「先落盘再发」：
      前端每次增删改队列都同步到服务端，启动时按会话读回来。
    存储用 KVStore（一行 JSON），键按会话区分，避免多个会话互相串队。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..storage.tasks import KVStore

    kv = KVStore()
    sid = request.query_params.get("session_id") or ""
    key = f"pending_queue:{sid or '-'}"

    if request.method == "GET":
        items = kv.get(key, [])
        return _json({"ok": True, "items": items if isinstance(items, list) else []})

    body = await _body(request)
    action = str(body.get("action") or "set")
    if action == "set":
        items = body.get("items")
        if not isinstance(items, list):
            return _err("items 必须是数组")
        # 只保留必要字段，避免把大对象（如 base64 缩略图）写进库
        slim = []
        for it in items[:50]:
            if not isinstance(it, dict):
                continue
            slim.append({
                "id": str(it.get("id") or ""),
                "text": str(it.get("text") or "")[:20000],
                "created_at": float(it.get("created_at") or 0),
            })
        kv.set(key, slim)
        return _json({"ok": True, "count": len(slim)})
    if action == "clear":
        kv.delete(key)
        return _json({"ok": True})
    return _err(f"未知操作：{action}")


async def api_browser_bridge(request: Any) -> Response:
    """内置浏览器桥：桌面主进程与后端工具之间的指令通道。

    AI 工具（跑在后端进程）无法直接调用 Electron 的 WebContentsView（活在桌面
    渲染进程），所以走这条队列：

      · ``GET  /api/browser-bridge``            —— 主进程轮询取一条待执行指令；
      · ``POST /api/browser-bridge {id, ...}``  —— 主进程回填执行结果。

    桌面主进程是唯一真正的执行者，协议白名单等安全边界仍然落在它那里；
    这个接口只做搬运，不自己发起任何网络请求。
    """
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..tools.builtin.browser import get_browser_bridge

    bridge = get_browser_bridge()
    if request.method == "GET":
        cmd = await bridge.take()
        if cmd is None:
            return _json({"ok": True, "cmd": None})
        return _json({"ok": True, "cmd": cmd})
    body = await _body(request)
    cmd_id = str(body.get("id") or "")
    if not cmd_id:
        return _err("id 不能为空")
    await bridge.settle(cmd_id, body)
    return _json({"ok": True})


async def api_workspace(request: Any) -> Response:
    """工作区文件浏览（供界面文件树）。"""
    if not _auth_ok(request):
        return _err("未授权", 401)
    from ..security.paths import resolve

    ag = await STATE.agent()
    rel = request.query_params.get("path") or "."
    try:
        p = resolve(rel, workspace=ag.workspace)
    except Exception as e:
        return _err(str(e), 400)
    if not p.exists():
        return _err("路径不存在", 404)
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return _err(f"读取失败：{e}", 500)
        return _json({"type": "file", "path": str(p), "content": truncate(text, 500000)})
    items = []
    with contextlib.suppress(OSError):
        for it in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))[:500]:
            if it.name.startswith(".") and it.name not in (".env.example",):
                continue
            try:
                st = it.stat()
                items.append({
                    "name": it.name, "dir": it.is_dir(),
                    "size": st.st_size, "mtime": st.st_mtime,
                })
            except OSError:
                continue
    return _json({"type": "dir", "path": str(p), "items": items,
                  "parent": str(p.parent) if p.parent != p else None})


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

async def ws_events(websocket: Any) -> None:
    await websocket.accept()
    if not _auth_ok(websocket):
        await websocket.send_json({"type": "error", "error": "未授权"})
        await websocket.close()
        return
    queue = STATE.bus.subscribe()
    try:
        await websocket.send_json({"type": "hello", "version": __version__,
                                   "status": STATE.status()})
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await websocket.send_json({"type": "ping", "ts": time.time()})
                continue
            with contextlib.suppress(Exception):
                await websocket.send_json(ev.to_dict())
    except Exception:
        pass
    finally:
        STATE.bus.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await websocket.close()


# --------------------------------------------------------------------------
# 静态资源
# --------------------------------------------------------------------------

async def index(request: Any) -> Response:
    f = STATIC_DIR / "index.html"
    if not f.is_file():
        return HTMLResponse(
            "<h1>Fengcode 服务端已运行</h1>"
            "<p>前端静态文件缺失（src/fengcode/server/static/index.html）。</p>"
            "<p>API 可用：<a href='/api/status'>/api/status</a></p>",
            status_code=200,
        )
    return FileResponse(f, media_type="text/html; charset=utf-8")


async def favicon(request: Any) -> Response:
    f = STATIC_DIR / "favicon.ico"
    if f.is_file():
        return FileResponse(f)
    # 没有 ico 就返回一个最小 SVG（避免 404 噪音）
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#4f46e5"/>'
        '<text x="16" y="22" font-size="17" font-family="sans-serif" '
        'font-weight="bold" fill="#fff" text-anchor="middle">F</text></svg>'
    )
    return Response(svg, media_type="image/svg+xml")


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------

def create_app() -> Starlette:
    routes = [
        Route("/", index),
        Route("/favicon.ico", favicon),
        Route("/health", health),
        WebSocketRoute("/ws", ws_events),
        # 基础
        Route("/api/status", api_status),
        Route("/api/bootstrap", api_bootstrap),
        Route("/api/config", api_config, methods=["GET", "PATCH", "POST"]),
        Route("/api/config/raw", api_config_raw, methods=["GET", "PUT"]),
        # 供应商
        Route("/api/providers", api_providers, methods=["GET", "POST"]),
        Route("/api/providers/test", api_provider_test, methods=["POST"]),
        # 账号（可选账号源：登录后看余额）
        Route("/api/account", api_account, methods=["GET", "POST"]),
        # 会话
        Route("/api/sessions", api_sessions, methods=["GET", "POST"]),
        Route("/api/sessions/{sid}", api_session_detail, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/sessions/{sid}/export", api_session_export),
        # 工作区与项目文件
        Route("/api/workspaces", api_workspaces, methods=["GET", "POST"]),
        Route("/api/workspace-files", api_workspace_files, methods=["GET"]),
        Route("/api/instruction-files", api_instruction_files, methods=["GET"]),
        Route("/api/sandbox-probe", api_sandbox_probe, methods=["GET"]),
        Route("/api/recovery", api_recovery, methods=["GET", "POST"]),
        Route("/api/open", api_open_path, methods=["POST"]),
        Route("/api/workspace-file", api_workspace_file_read, methods=["GET"]),
        Route("/api/workspace-file", api_workspace_file_write, methods=["PUT", "POST"]),
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/chat/stop", api_chat_stop, methods=["POST"]),
        # 手动压缩（`/compact` 命令）
        Route("/api/chat/compact", api_chat_compact, methods=["POST"]),
        Route("/api/approval", api_approval, methods=["POST"]),
        Route("/api/approval/pending", api_pending_approvals),
        # 提问回填（ask_user 工具）
        Route("/api/ask", api_ask, methods=["POST"]),
        # 认知
        Route("/api/tasks", api_tasks, methods=["GET", "POST"]),
        Route("/api/memories", api_memories, methods=["GET", "POST"]),
        Route("/api/skills", api_skills, methods=["GET", "POST"]),
        Route("/api/tools", api_tools, methods=["GET", "POST"]),
        Route("/api/tools/run", api_tool_run, methods=["POST"]),
        Route("/api/subagents", api_subagents),
        # 扩展
        Route("/api/mcp", api_mcp, methods=["GET", "POST"]),
        Route("/api/plugins", api_plugins, methods=["GET", "POST"]),
        Route("/api/plugins/{name}/panel/{path:path}", api_plugin_panel),
        Route("/api/workflows", api_workflows, methods=["GET", "POST"]),
        Route("/api/jobs", api_jobs, methods=["GET", "POST"]),
        Route("/api/remote", api_remote, methods=["GET", "POST"]),
        # 其他
        Route("/api/stats", api_stats),
        Route("/api/workspace", api_workspace),
        # 工作区占用/排队状态（界面切回会话时拉一次，不必等下一个事件）
        Route("/api/workspace-lock", api_workspace_lock),
        # 待发队列持久化（刷新/重开页面后仍能恢复）
        Route("/api/queue", api_queue, methods=["GET", "POST"]),
        # 内置浏览器桥（桌面主进程轮询取指令 / 回填结果，供 AI 工具驱动浏览器）
        Route("/api/browser-bridge", api_browser_bridge, methods=["GET", "POST"]),
    ]
    # 静态目录（存在才挂）
    if STATIC_DIR.is_dir():
        routes.append(Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"))

    @contextlib.asynccontextmanager
    async def lifespan(app: Any):  # noqa: ANN001
        """新版 Starlette 用 lifespan 管理启动/关闭钩子。"""
        await STATE.startup()
        try:
            yield
        finally:
            await STATE.shutdown()

    try:
        app = Starlette(routes=routes, lifespan=lifespan)
    except TypeError:
        # 兼容只认 on_startup/on_shutdown 的旧版 Starlette
        app = Starlette(
            routes=routes,
            on_startup=[STATE.startup],
            on_shutdown=[STATE.shutdown],
        )
    return app


__all__ = ["create_app", "STATE"]
