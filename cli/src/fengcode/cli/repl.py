"""朴素 REPL：不依赖全屏控件的交互式对话，兼容差终端。

支持：流式输出、工具调用显示、审批询问、斜杠命令、会话持久化。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from ..events import Ev
from ..llm.types import StreamEvent

# 终端配色（不支持时自动降级为纯文本）
class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def c(text: str, *codes: str) -> str:
    if not _color_enabled() or not codes:
        return text
    return "".join(codes) + text + C.RESET


HELP = """
可用命令：
  /help              显示帮助
  /new               新建会话
  /session [ID]      切换会话（不带 ID 则列出）
  /model [引用]      查看或切换模型（如 provider/model）
  /todo              查看任务清单
  /memory <关键词>   检索记忆
  /remember <内容>   写入记忆
  /tools             列出可用工具个数
  /skills            列出技能
  /compact           手动压缩上下文
  /readonly          切换只读模式
  /status            显示状态信息
  /quit 或 /exit     退出（也可 Ctrl+D / Ctrl+C）

其他：
  · 直接输入内容即对话；Enter 发送（多行可粘贴）
  · 行首以 ! 开头会把该行当命令执行（如 !ls）
  · 支持管道：echo "问题" | fengcode run
"""


class SimpleRepl:
    """朴素交互循环。"""

    def __init__(self, agent: Any, runtime: dict[str, Any]) -> None:
        self.agent = agent
        self.rt = runtime
        self.cfg = runtime.get("config")
        self.quiet_tools = False

    # ---- 输出 ----------------------------------------------------------
    def banner(self) -> None:
        from ..version import __version__

        mgr = self.rt["manager"]
        ref = mgr.default_model_ref() or "（未配置模型）"
        print()
        print(c("  Fengcode", C.BOLD, C.CYAN) + c(f" v{__version__}", C.GRAY)
              + c("  · 中文 AI Agent", C.GRAY))
        print(c(f"  模型：{ref}", C.GRAY))
        print(c(f"  工作区：{self.agent.workspace}", C.GRAY))
        print(c(f"  会话：{self.agent.session_id}", C.GRAY))
        if not ref or ref == "（未配置模型）":
            print(c("  ⚠ 还没有配置模型。运行 fengcode config 或进界面「设置→模型供应商」添加。",
                    C.YELLOW))
        print(c("  输入 /help 看命令，/quit 退出", C.GRAY))
        print()

    def prompt_str(self) -> str:
        mode = ""
        try:
            if self.cfg and self.cfg.permissions.mode == "deny":
                mode = c(" [只读]", C.YELLOW)
        except Exception:
            pass
        return c("你", C.BOLD, C.GREEN) + mode + c(" › ", C.GRAY)

    # ---- 事件处理 ------------------------------------------------------
    def make_on_event(self, state: dict[str, Any]):
        def on_event(ev: StreamEvent) -> None:
            if ev.type == "reasoning" and ev.text:
                if not state.get("in_reasoning"):
                    print(c("\n[思考] ", C.GRAY), end="", flush=True)
                    state["in_reasoning"] = True
                print(c(ev.text, C.GRAY), end="", flush=True)
            elif ev.type == "text" and ev.text:
                if state.get("in_reasoning"):
                    print()
                    state["in_reasoning"] = False
                if not state.get("started"):
                    print(c("Fengcode", C.BOLD, C.MAGENTA) + c(" › ", C.GRAY), end="", flush=True)
                    state["started"] = True
                print(ev.text, end="", flush=True)

        return on_event

    def _bus_listener(self):
        """订阅事件总线，实时展示工具调用与审批请求。"""
        bus = self.rt.get("_bus")
        if bus is None:
            from ..events import get_bus

            bus = get_bus()
            self.rt["_bus"] = bus

        def handler(ev) -> None:
            if ev.session_id and ev.session_id != self.agent.session_id:
                return
            t = ev.type
            d = ev.data or {}
            if t == Ev.TOOL_START and not self.quiet_tools:
                args = d.get("arguments") or {}
                brief = "，".join(
                    f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3]
                )
                print(c(f"\n  ⚙ {d.get('name')}", C.CYAN) + c(f"({brief})", C.GRAY), flush=True)
            elif t == Ev.TOOL_END and not self.quiet_tools:
                flag = c("✓", C.GREEN) if d.get("ok") else c("✗", C.RED)
                dur = d.get("duration")
                dur_s = f" {dur:.1f}s" if isinstance(dur, (int, float)) else ""
                disp = d.get("display") or d.get("error") or ""
                print(c(f"  {flag} {str(disp)[:100]}{dur_s}", C.GRAY), flush=True)
            elif t == Ev.SUBAGENT_START:
                print(c(f"\n  ⇢ 子智能体[{d.get('type')}] 开始：{str(d.get('task'))[:80]}",
                        C.BLUE), flush=True)
            elif t == Ev.SUBAGENT_END:
                flag = "完成" if d.get("ok") else "失败"
                print(c(f"  ⇠ 子智能体[{d.get('name')}] {flag}", C.BLUE), flush=True)
            elif t == Ev.STATUS and d.get("message"):
                print(c(f"\n  … {d['message']}", C.YELLOW), flush=True)
            elif t == Ev.LOG and d.get("level") in ("warn", "error"):
                print(c(f"\n  ! {d.get('message')}", C.YELLOW if d.get("level") == "warn" else C.RED),
                      flush=True)

        bus.on_event(handler)
        return bus

    # ---- 审批 ----------------------------------------------------------
    def ask_approval(self, req) -> None:
        """同步询问用户是否允许某个操作（在事件回调里被调用）。"""
        print()
        print(c("  ⚠ 需要确认", C.BOLD, C.YELLOW)
              + c(f"（{ '高危' if req.risk == 'high' else '中等风险' }）", C.GRAY))
        print(c(f"  工具：{req.action}", C.GRAY))
        print(c(f"  原因：{req.reason}", C.GRAY))
        target = (req.target or "")[:400]
        if target:
            print(c("  目标：", C.GRAY) + target)
        print(c("  [y] 允许  [n] 拒绝  [a] 本会话始终允许  [A] 永久允许  ", C.GRAY), end="",
              flush=True)
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        allowed = ans in ("y", "yes", "是", "a")
        remember = "session" if ans == "a" else ("always" if ans == "A" else "once")
        self.agent.approval.resolve_with_key(
            req.id, allowed,
            action=req.action, target=req.target,
            session_id=self.agent.session_id, remember=remember,
        )
        print(c("  → " + ("已允许" if allowed else "已拒绝"), C.GREEN if allowed else C.RED))

    # ---- 命令 ----------------------------------------------------------
    async def handle_command(self, line: str) -> bool:
        """处理斜杠命令；返回 False 表示要退出。"""
        parts = line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            return False

        if cmd == "/help":
            print(c(HELP, C.GRAY))
            return True

        if cmd == "/new":
            r = await asyncio.to_thread(
                self.agent.sessions.create, title="新对话",
                workspace=str(self.agent.workspace),
            )
            self.agent.session_id = r["id"]
            print(c(f"  已新建会话 {r['id']}", C.GREEN))
            return True

        if cmd == "/session":
            if not arg:
                items = await asyncio.to_thread(self.agent.sessions.list, limit=15)
                print(c(f"  最近 {len(items)} 个会话：", C.GRAY))
                for s in items:
                    mark = "→" if s["id"] == self.agent.session_id else " "
                    print(c(f"  {mark} {s['id']}　{s['title'][:40]}　{s['updated_at'] and ''}", C.GRAY))
                print(c("  用 /session <ID> 切换", C.GRAY))
            else:
                s = await asyncio.to_thread(self.agent.sessions.get, arg)
                if s is None:
                    print(c(f"  没有找到会话 {arg}", C.RED))
                else:
                    self.agent.session_id = arg
                    print(c(f"  已切换到 {s['title']}（{arg}）", C.GREEN))
            return True

        if cmd == "/model":
            if not arg:
                cur = (await asyncio.to_thread(self.agent.sessions.get, self.agent.session_id) or {}).get("model")
                print(c(f"  当前：{cur or self.rt['manager'].default_model_ref() or '未配置'}", C.GRAY))
                from ..llm.router import model_choices

                choices = model_choices()
                usable = [m for m in choices if m["has_key"]]
                print(c(f"  可用模型（{len(usable)}）：", C.GRAY))
                for m in usable[:30]:
                    print(c(f"   · {m['ref']}", C.GRAY))
            else:
                ref = arg
                p, mm = self.rt["manager"].parse_model_ref(ref)
                if not p:
                    print(c(f"  无法识别的模型引用：{ref}", C.RED))
                else:
                    await asyncio.to_thread(self.agent.sessions.update, self.agent.session_id, model=ref)
                    print(c(f"  已切换到 {ref}", C.GREEN))
            return True

        if cmd == "/todo":
            from ..storage.tasks import TaskStore

            store = TaskStore(self.agent.db)
            print(c(store.render(self.agent.session_id), C.GRAY))
            return True

        if cmd == "/memory":
            items = await self.agent.memory.recall(arg or "最近", top_k=5)
            if not items:
                print(c("  没有找到相关记忆", C.GRAY))
            for m in items:
                print(c(f"  · [{m.kind}] {m.title}（{m.score:.2f}）", C.CYAN))
                print(c(f"    {m.content[:200]}", C.GRAY))
            return True

        if cmd == "/remember":
            if not arg:
                print(c("  用法：/remember 要记住的内容", C.YELLOW))
            else:
                item = await self.agent.memory.remember(arg, kind="fact", source="cli-user")
                print(c(f"  已记住：{item.title}", C.GREEN))
            return True

        if cmd == "/tools":
            groups = self.agent.registry.groups()
            total = sum(len(v) for v in groups.values())
            print(c(f"  内置工具 {total} 个：", C.GRAY))
            for g, names in sorted(groups.items()):
                print(c(f"   [{g}] {', '.join(names)}", C.GRAY))
            if self.agent._mcp is not None:
                mt = self.agent._mcp.all_tools()
                print(c(f"  MCP 工具 {len(mt)} 个：{', '.join(t.full_name for t in mt[:20])}", C.GRAY))
            return True

        if cmd == "/skills":
            items = self.agent.skills.list_all()
            print(c(f"  技能 {len(items)} 个：", C.GRAY))
            for s in items:
                print(c(f"   · {s['name']}：{s['description'][:60]}", C.GRAY))
            return True

        if cmd == "/compact":
            msgs = await asyncio.to_thread(self.agent.sessions.messages, self.agent.session_id)
            if len(msgs) < 4:
                print(c("  消息太少，无需压缩", C.GRAY))
            else:
                info = self.rt["manager"].model_info(
                    *(lambda r: (r[1] or "", self.rt["manager"].get_provider(r[0])))(
                        self.rt["manager"].parse_model_ref(None)
                    )
                )
                out = await self.agent.compact(
                    msgs, window=int(info.get("context_window") or 128000),
                    session_id=self.agent.session_id,
                )
                print(c(f"  已压缩：{len(msgs)} → {len(out)} 条", C.GREEN))
            return True

        if cmd == "/readonly":
            pm = self.cfg.permissions
            pm.mode = "workspace" if pm.mode == "deny" else "deny"
            self.agent.approval.update_config(pm)
            print(c(f"  审批模式：{pm.mode}", C.GREEN))
            return True

        if cmd == "/status":
            from ..utils import human_tokens

            s = await asyncio.to_thread(self.agent.sessions.get, self.agent.session_id) or {}
            ov = self.agent.stats.overview()
            print(c(f"  会话：{s.get('title')}（{self.agent.session_id}）", C.GRAY))
            print(c(f"  累计 tokens：{human_tokens((s.get('input_tokens') or 0) + (s.get('output_tokens') or 0))}"
                    f"　费用：{s.get('cost', 0):.4f}", C.GRAY))
            print(c(f"  全局：{ov['all']['calls']} 次调用，"
                    f"{human_tokens(ov['all']['total_tokens'])} tokens，"
                    f"成本 {ov['all']['cost']:.4f}", C.GRAY))
            print(c(f"  审批模式：{self.cfg.permissions.mode}", C.GRAY))
            if self.agent._mcp is not None:
                st = self.agent._mcp.status()
                ready = sum(1 for x in st if x.get("connected"))
                print(c(f"  MCP：{ready}/{len(st)} 已连接", C.GRAY))
            return True

        print(c(f"  未知命令：{cmd}（试试 /help）", C.YELLOW))
        return True

    # ---- 主循环 --------------------------------------------------------
    async def run(self) -> None:
        self.banner()
        bus = self._bus_listener()
        from ..events import Ev as _Ev

        def approval_watch(ev) -> None:
            if ev.type != _Ev.APPROVAL:
                return
            d = ev.data or {}
            if d.get("session_id") and d["session_id"] != self.agent.session_id:
                return
            from ..security.approval import ApprovalRequest

            self.ask_approval(ApprovalRequest(**{
                k: v for k, v in d.items()
                if k in ("id", "action", "target", "reason", "risk", "detail",
                         "session_id", "created_at", "preview")
            }))

        bus.on_event(approval_watch)
        # 关键：让审批门有"响应者"，否则会被判定为无交互通道而直接拒绝
        self.agent.approval.set_responder(bus)

        try:
            while True:
                try:
                    line = await asyncio.to_thread(input, self.prompt_str())
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                line = (line or "").strip()
                if not line:
                    continue
                if line.startswith("/"):
                    if not await self.handle_command(line):
                        break
                    continue
                if line.startswith("!"):
                    await self._run_shell(line[1:])
                    continue

                state: dict[str, Any] = {}
                try:
                    res = await self.agent.run(
                        line, stream=True, on_event=self.make_on_event(state)
                    )
                except KeyboardInterrupt:
                    self.agent.cancel()
                    print(c("\n  已中断", C.YELLOW))
                    continue
                if state.get("in_reasoning") or state.get("started"):
                    print()
                if res.error:
                    print(c(f"  ✗ {res.error}", C.RED))
                elif not state.get("started"):
                    print(res.content or "（无输出）")

                # 用量行
                try:
                    if self.cfg.ui.show_turn_usage:
                        from ..utils import human_duration, human_tokens

                        print(c(f"  ── {res.steps} 步 · "
                                f"{human_tokens(res.usage.get('total_tokens', 0))} tokens · "
                                f"{human_duration(res.duration)}"
                                + (f" · ¥{res.cost:.4f}" if res.cost else ""), C.GRAY))
                except Exception:
                    pass
        finally:
            try:
                bus.off_event(approval_watch)
            except Exception:
                pass
            print(c("\n  再见。", C.GRAY))

    async def _run_shell(self, cmd: str) -> None:
        from ..security.sandbox import LocalSandbox

        sb = LocalSandbox(cwd=self.agent.workspace, timeout=120)
        res = await sb.shell(cmd)
        if res.stdout:
            print(res.stdout, end="" if res.stdout.endswith("\n") else "\n")
        if res.stderr:
            print(c(res.stderr, C.RED), end="" if res.stderr.endswith("\n") else "\n")
        print(c(f"  （退出码 {res.returncode}）", C.GRAY))


async def run_simple_repl(agent: Any, runtime: dict[str, Any]) -> None:
    await SimpleRepl(agent, runtime).run()


__all__ = ["SimpleRepl", "run_simple_repl"]
