"""全屏 TUI：基于 textual 的交互界面。

布局
----
┌──────────────┬──────────────────────────────────────┐
│ 左侧信息栏    │ 对话区（可滚动，Markdown 渲染）        │
│ · 模型       │                                      │
│ · 工作区     │                                      │
│ · 任务清单   │                                      │
│ · 工具调用   │                                      │
│ · 用量       ├──────────────────────────────────────┤
│              │ 输入框                                │
└──────────────┴──────────────────────────────────────┘

快捷键：Ctrl+Q 退出 · Ctrl+N 新会话 · Ctrl+L 清屏 · Ctrl+R 只读切换 · F1 帮助
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, RichLog, Static, Tree

from ..events import Ev, get_bus
from ..llm.types import StreamEvent
from ..utils import human_duration, human_money, human_tokens


class SidePanel(VerticalScroll):
    """左侧信息面板。"""

    def compose(self) -> ComposeResult:
        yield Static("", id="side-body")

    def refresh_panel(self, agent: Any, runtime: dict[str, Any]) -> None:
        mgr = runtime["manager"]
        cfg = runtime["config"]
        lines: list[str] = []
        ref = mgr.default_model_ref() or "（未配置）"
        lines.append(f"[b]模型[/b]\n{ref}")
        lines.append(f"[b]工作区[/b]\n{agent.workspace}")
        lines.append(f"[b]会话[/b]\n{agent.session_id}")
        mode = cfg.permissions.mode
        mode_txt = {"allow": "全部放行", "workspace": "工作区放行", "ask": "每次询问", "deny": "只读"}.get(mode, mode)
        lines.append(f"[b]审批[/b]\n{mode_txt}")

        try:
            s = agent.sessions.get(agent.session_id) or {}
            tk = (s.get("input_tokens") or 0) + (s.get("output_tokens") or 0)
            lines.append(f"[b]本次会话[/b]\n{human_tokens(tk)} tokens · {human_money(s.get('cost') or 0, '¥')}")
        except Exception:
            pass

        try:
            from ..storage.tasks import TaskStore

            store = TaskStore(agent.db)
            items = store.list(session_id=agent.session_id)
            if items:
                marks = {"pending": "○", "in_progress": "◐", "completed": "●",
                         "blocked": "!", "cancelled": "×"}
                body = "\n".join(
                    f"{marks.get(t['status'], '○')} {t['title'][:26]}" for t in items[:12]
                )
                lines.append(f"[b]任务清单[/b]\n{body}")
        except Exception:
            pass

        try:
            if agent._mcp is not None:
                st = agent._mcp.status()
                ready = sum(1 for x in st if x.get("connected"))
                tools = sum(x.get("tool_count", 0) for x in st)
                lines.append(f"[b]MCP[/b]\n{ready}/{len(st)} 已连 · {tools} 工具")
        except Exception:
            pass

        try:
            groups = agent.registry.groups()
            lines.append(f"[b]内置工具[/b]\n{sum(len(v) for v in groups.values())} 个")
        except Exception:
            pass

        body = "\n\n".join(lines)
        try:
            self.query_one("#side-body", Static).update(body)
        except Exception:
            pass


class ChatView(RichLog):
    """对话显示区。"""

    def on_mount(self) -> None:
        self.wrap = True
        self.markup = True


class ApprovalBar(Static):
    """审批提示条：显示待批准的操作。"""

    def on_mount(self) -> None:
        self.display = False


class FengTUI(App):
    """Fengcode 全屏终端界面。"""

    CSS = """
    Screen { layout: vertical; }
    #main-row { height: 1fr; }
    #side { width: 30; border-right: solid $panel; padding: 1; }
    #right { width: 1fr; }
    #chat { height: 1fr; border: none; padding: 0 1; }
    #approval { height: auto; max-height: 8; background: $warning 20%; color: $text;
                padding: 0 1; display: none; border: solid $warning; }
    #input-row { height: auto; padding: 0 1; }
    #prompt { border: tall $accent; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+n", "new_session", "新会话"),
        ("ctrl+l", "clear", "清屏"),
        ("ctrl+r", "toggle_readonly", "只读"),
        ("ctrl+t", "show_tools", "工具"),
        ("ctrl+s", "show_status", "状态"),
        ("f1", "help", "帮助"),
    ]

    def __init__(self, agent: Any, runtime: dict[str, Any]) -> None:
        super().__init__()
        self.agent = agent
        self.rt = runtime
        self.cfg = runtime.get("config")
        self._busy = False
        self._pending_approval = None
        self._stream_state: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-row"):
            yield SidePanel(id="side")
            with Vertical(id="right"):
                yield ChatView(id="chat", highlight=True, markup=True)
                yield ApprovalBar(id="approval")
        with Vertical(id="input-row"):
            yield Input(placeholder="输入消息…（Enter 发送，Ctrl+Q 退出，F1 帮助）", id="prompt")
        yield Static("", id="status")
        yield Footer()

    # ---- 生命周期 ------------------------------------------------------
    async def on_mount(self) -> None:
        self.title = "Fengcode"
        self.sub_title = self.rt["manager"].default_model_ref() or "未配置模型"
        self._welcome()
        self.query_one("#side", SidePanel).refresh_panel(self.agent, self.rt)
        self._update_status()

        bus = get_bus()
        bus.on_event(self._on_bus_event)
        # 关键：注册响应者，让审批门知道有交互通道
        self.agent.approval.set_responder(bus)
        self.query_one("#prompt", Input).focus()

    def _welcome(self) -> None:
        from ..version import __version__

        chat = self.query_one("#chat", ChatView)
        ref = self.rt["manager"].default_model_ref()
        chat.write(f"[b cyan]Fengcode[/b cyan] [dim]v{__version__}[/dim]　[dim]中文 AI Agent 终端界面[/dim]")
        chat.write(f"[dim]模型：{ref or '（未配置，请进「设置→模型供应商」添加）'}[/dim]")
        chat.write(f"[dim]工作区：{self.agent.workspace}[/dim]")
        chat.write("[dim]Ctrl+Q 退出 · F1 帮助 · 输入 / 开头的命令可管理会话与设置[/dim]")
        chat.write("")

    # ---- 事件桥接 ------------------------------------------------------
    def _on_bus_event(self, ev) -> None:
        """事件总线回调（可能在别的线程），转发到主线程处理。"""
        try:
            self.call_from_thread(self._handle_event, ev)
        except Exception:
            pass

    def _handle_event(self, ev) -> None:
        if ev.session_id and ev.session_id != self.agent.session_id:
            return
        t = ev.type
        d = ev.data or {}
        chat = self.query_one("#chat", ChatView)
        if t == Ev.TOOL_START:
            args = d.get("arguments") or {}
            brief = "，".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])
            chat.write(f"[cyan]  ⚙ {d.get('name')}[/cyan] [dim]({brief})[/dim]")
        elif t == Ev.TOOL_END:
            flag = "[green]✓[/green]" if d.get("ok") else "[red]✗[/red]"
            dur = d.get("duration")
            dur_s = f" {dur:.1f}s" if isinstance(dur, (int, float)) else ""
            disp = d.get("display") or d.get("error") or ""
            chat.write(f"  {flag} [dim]{str(disp)[:110]}{dur_s}[/dim]")
        elif t == Ev.SUBAGENT_START:
            chat.write(f"[blue]  ⇢ 子智能体[{d.get('type')}] 开始：{str(d.get('task'))[:70]}[/blue]")
        elif t == Ev.SUBAGENT_END:
            flag = "完成" if d.get("ok") else "失败"
            chat.write(f"[blue]  ⇠ 子智能体[{d.get('name')}] {flag}[/blue]")
        elif t == Ev.STATUS and d.get("message"):
            chat.write(f"[yellow]  … {d['message']}[/yellow]")
        elif t == Ev.LOG and d.get("level") in ("warn", "error"):
            color = "yellow" if d["level"] == "warn" else "red"
            chat.write(f"[{color}]  ! {d.get('message')}[/{color}]")
        elif t == Ev.APPROVAL:
            self._show_approval(d)

    def _show_approval(self, d: dict[str, Any]) -> None:
        self._pending_approval = d
        bar = self.query_one("#approval", ApprovalBar)
        risk = "高危" if d.get("risk") == "high" else "中等风险"
        target = str(d.get("target") or "")[:300]
        bar.update(
            f"[b yellow]⚠ 需要确认（{risk}）[/b yellow]　工具：[b]{d.get('action')}[/b]\n"
            f"[dim]{d.get('reason')}[/dim]\n{target}\n"
            "[b]按 y 允许　n 拒绝　a 本会话始终允许　A 永久允许[/b]"
        )
        bar.display = True

    # ---- 输入与执行 ----------------------------------------------------
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        inp = self.query_one("#prompt", Input)
        inp.value = ""
        if not text or self._busy:
            return

        # 审批待决时，输入被当作审批回答
        if self._pending_approval is not None:
            self._resolve_approval(text)
            return

        if text.startswith("/"):
            await self._command(text)
            return

        await self._send(text)

    def _resolve_approval(self, answer: str) -> None:
        d = self._pending_approval
        self._pending_approval = None
        bar = self.query_one("#approval", ApprovalBar)
        bar.display = False
        a = answer.strip().lower()
        allowed = a in ("y", "yes", "是", "a")
        remember = "session" if a == "a" else ("always" if answer.strip() == "A" else "once")
        self.agent.approval.resolve_with_key(
            d["id"], allowed,
            action=d.get("action", ""), target=d.get("target", ""),
            session_id=self.agent.session_id, remember=remember,
        )
        chat = self.query_one("#chat", ChatView)
        chat.write("[green]  → 已允许[/green]" if allowed else "[red]  → 已拒绝[/red]")

    async def _send(self, text: str) -> None:
        chat = self.query_one("#chat", ChatView)
        chat.write(f"\n[b green]你 ›[/b green] {text}")
        self._busy = True
        self._stream_state = {"started": False, "in_reasoning": False}
        chat.write("[b magenta]Fengcode ›[/b magenta] ", end="")
        t0 = time.time()

        def on_event(ev: StreamEvent) -> None:
            st = self._stream_state
            try:
                if ev.type == "reasoning" and ev.text:
                    if not st.get("in_reasoning"):
                        self.call_from_thread(chat.write, "[dim][思考] [/dim]", end="")
                        st["in_reasoning"] = True
                    self.call_from_thread(chat.write, f"[dim]{_md_escape(ev.text)}[/dim]", end="")
                elif ev.type == "text" and ev.text:
                    if st.get("in_reasoning"):
                        self.call_from_thread(chat.write, "")
                        st["in_reasoning"] = False
                    st["started"] = True
                    self.call_from_thread(chat.write, _md_escape(ev.text), end="")
            except Exception:
                pass

        try:
            res = await self.agent.run(text, stream=True, on_event=on_event)
        except Exception as e:
            chat.write(f"\n[red]  ✗ {type(e).__name__}: {e}[/red]")
            self._busy = False
            return

        if self._stream_state.get("in_reasoning") or self._stream_state.get("started"):
            chat.write("")
        if res.error:
            chat.write(f"[red]  ✗ {res.error}[/red]")
        elif not self._stream_state.get("started"):
            chat.write(_md_escape(res.content or "（无输出）"))

        chat.write(
            f"[dim]  ── {res.steps} 步 · {human_tokens(res.usage.get('total_tokens', 0))} tokens · "
            f"{human_duration(time.time() - t0)}"
            + (f" · {human_money(res.cost, res.currency)}" if res.cost else "")
            + "[/dim]"
        )
        chat.write("")
        self._busy = False
        self.query_one("#side", SidePanel).refresh_panel(self.agent, self.rt)
        self._update_status()

    def _update_status(self, extra: str = "") -> None:
        ref = self.rt["manager"].default_model_ref() or "未配置模型"
        mode = {"allow": "全放行", "ask": "危险询问", "deny": "只读"}.get(
            self.cfg.permissions.mode, self.cfg.permissions.mode
        )
        busy = "[yellow]生成中…[/yellow]" if self._busy else "[green]就绪[/green]"
        s = f"{busy}　[dim]模型 {ref}　审批 {mode}　{extra}[/dim]"
        try:
            self.query_one("#status", Static).update(s)
        except Exception:
            pass

    # ---- 斜杠命令 ------------------------------------------------------
    async def _command(self, line: str) -> None:
        chat = self.query_one("#chat", ChatView)
        parts = line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            self.exit()
        elif cmd == "/clear":
            chat.clear()
        elif cmd == "/new":
            r = await asyncio.to_thread(
                self.agent.sessions.create, title="新对话", workspace=str(self.agent.workspace)
            )
            self.agent.session_id = r["id"]
            chat.clear()
            self._welcome()
            self._update_status("已新建会话")
        elif cmd == "/model":
            if arg:
                await asyncio.to_thread(self.agent.sessions.update, self.agent.session_id, model=arg)
                self._update_status(f"模型已切换为 {arg}")
                chat.write(f"[green]  已切换模型：{arg}[/green]")
            else:
                from ..llm.router import model_choices

                usable = [m for m in model_choices() if m["has_key"]]
                chat.write("[b]可用模型：[/b]")
                for m in usable[:40]:
                    chat.write(f"  · [cyan]{m['ref']}[/cyan] [dim]{'视觉' if m['vision'] else ''}"
                               f"{' 思考' if m['thinking'] else ''}[/dim]")
                chat.write("[dim]  用 /model <引用> 切换[/dim]")
        elif cmd == "/todo":
            from ..storage.tasks import TaskStore

            chat.write(f"[b]任务清单[/b]\n{TaskStore(self.agent.db).render(self.agent.session_id)}")
        elif cmd == "/memory":
            items = await self.agent.memory.recall(arg or "最近", top_k=5)
            if not items:
                chat.write("[dim]  没有找到相关记忆[/dim]")
            for m in items:
                chat.write(f"  · [cyan][{m.kind}] {m.title}[/cyan]（{m.score:.2f}）")
                chat.write(f"[dim]    {m.content[:220]}[/dim]")
        elif cmd == "/remember":
            if arg:
                item = await self.agent.memory.remember(arg, kind="fact", source="cli-user")
                chat.write(f"[green]  已记住：{item.title}[/green]")
            else:
                chat.write("[yellow]  用法：/remember 内容[/yellow]")
        elif cmd == "/tools":
            self.action_show_tools()
        elif cmd == "/skills":
            items = self.agent.skills.list_all()
            chat.write(f"[b]技能（{len(items)}）[/b]")
            for s in items:
                chat.write(f"  · [cyan]{s['name']}[/cyan] [dim]{s['description'][:60]}[/dim]")
        elif cmd == "/compact":
            msgs = await asyncio.to_thread(self.agent.sessions.messages, self.agent.session_id)
            if len(msgs) < 4:
                chat.write("[dim]  消息太少，无需压缩[/dim]")
            else:
                out = await self.agent.compact(msgs, window=128000, session_id=self.agent.session_id)
                chat.write(f"[green]  已压缩：{len(msgs)} → {len(out)} 条[/green]")
        elif cmd == "/readonly":
            self.action_toggle_readonly()
        elif cmd == "/status":
            self.action_show_status()
        elif cmd == "/help":
            self.action_help()
        else:
            chat.write(f"[yellow]  未知命令：{cmd}（试试 /help）[/yellow]")
        self.query_one("#prompt", Input).focus()

    # ---- Actions -------------------------------------------------------
    def action_new_session(self) -> None:
        self.run_worker(self._command("/new"))

    def action_clear(self) -> None:
        self.query_one("#chat", ChatView).clear()

    def action_toggle_readonly(self) -> None:
        pm = self.cfg.permissions
        pm.mode = "workspace" if pm.mode == "deny" else "deny"
        self.agent.approval.update_config(pm)
        self._update_status()
        chat = self.query_one("#chat", ChatView)
        chat.write(f"[green]  审批模式：{pm.mode}[/green]")

    def action_show_tools(self) -> None:
        chat = self.query_one("#chat", ChatView)
        groups = self.agent.registry.groups()
        total = sum(len(v) for v in groups.values())
        chat.write(f"[b]内置工具（{total}）[/b]")
        for g, names in sorted(groups.items()):
            chat.write(f"  [cyan][{g}][/cyan] [dim]{', '.join(names)}[/dim]")
        if self.agent._mcp is not None:
            mt = self.agent._mcp.all_tools()
            chat.write(f"[b]MCP 工具（{len(mt)}）[/b]")
            if mt:
                chat.write(f"  [dim]{', '.join(t.full_name for t in mt[:30])}[/dim]")

    def action_show_status(self) -> None:
        chat = self.query_one("#chat", ChatView)
        s = self.agent.sessions.get(self.agent.session_id) or {}
        ov = self.agent.stats.overview()
        chat.write("[b]状态[/b]")
        chat.write(f"  会话：{s.get('title')}（{self.agent.session_id}）")
        chat.write(f"  累计：{human_tokens((s.get('input_tokens') or 0) + (s.get('output_tokens') or 0))} tokens"
                   f" · {human_money(s.get('cost') or 0, '¥')}")
        chat.write(f"  全局：{ov['all']['calls']} 次调用 · {human_tokens(ov['all']['total_tokens'])} tokens"
                   f" · {human_money(ov['all']['cost'], '¥')}")
        chat.write(f"  审批模式：{self.cfg.permissions.mode}")
        chat.write(f"  工作区：{self.agent.workspace}")

    def action_help(self) -> None:
        chat = self.query_one("#chat", ChatView)
        chat.write(
            "[b]快捷键[/b]\n"
            "  Ctrl+Q 退出　Ctrl+N 新会话　Ctrl+L 清屏　Ctrl+R 切换只读\n"
            "  Ctrl+T 查看工具　Ctrl+S 查看状态　F1 帮助\n\n"
            "[b]斜杠命令[/b]\n"
            "  /new /model [引用] /todo /memory <词> /remember <内容>\n"
            "  /tools /skills /compact /readonly /status /clear /quit\n\n"
            "[b]审批[/b]：出现确认条时直接输入 y / n / a / A 并回车"
        )

    def key_escape(self) -> None:
        """Esc 清掉待决审批提示。"""
        if self._pending_approval is not None:
            self._resolve_approval("n")


def _md_escape(text: str) -> str:
    """转义 Rich 标记，避免模型输出里的方括号被当作样式。"""
    return text.replace("[", r"\[")


def run_tui(agent: Any, runtime: dict[str, Any]) -> None:
    """启动全屏 TUI。"""
    app = FengTUI(agent, runtime)
    app.run()


__all__ = ["FengTUI", "run_tui"]
