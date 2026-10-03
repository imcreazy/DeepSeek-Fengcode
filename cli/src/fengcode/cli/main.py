"""CLI 入口：fengcode 命令与全部子命令。

子命令
------
chat      进入交互式对话（TUI）
run       执行一次性任务（适合脚本/管道）
serve     启动 HTTP 服务（桌面端与 Web UI 的后端）
mcp       管理 MCP 服务器；``serve`` 时把自身作为 MCP 服务暴露
skill     管理技能
plugin    管理插件
memory    管理记忆
task      任务与目标
config    配置管理
import    从环境变量文件导入
doctor    环境自检
version   版本信息

设计要点：所有输出中文、支持 ``--json`` 便于脚本消费、非交互场景自动跳过询问。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

from ..version import __app_name__, __version__

app = typer.Typer(
    name="fengcode",
    help=f"{__app_name__} —— 中文 AI Agent 平台（桌面端 / CLI / Web UI）",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

mcp_app = typer.Typer(help="MCP 服务器管理", no_args_is_help=True)
skill_app = typer.Typer(help="技能管理", no_args_is_help=True)
plugin_app = typer.Typer(help="插件管理", no_args_is_help=True)
memory_app = typer.Typer(help="记忆管理", no_args_is_help=True)
task_app = typer.Typer(help="任务与目标", no_args_is_help=True)
config_app = typer.Typer(help="配置管理", no_args_is_help=True)
wf_app = typer.Typer(help="工作流", no_args_is_help=True)
job_app = typer.Typer(help="定时任务", no_args_is_help=True)

app.add_typer(mcp_app, name="mcp")
app.add_typer(skill_app, name="skill")
app.add_typer(plugin_app, name="plugin")
app.add_typer(memory_app, name="memory")
app.add_typer(task_app, name="task")
app.add_typer(config_app, name="config")
app.add_typer(wf_app, name="workflow")
app.add_typer(job_app, name="job")

# 控制台输出编码（Windows 下默认 GBK 会炸）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------

def out(text: str = "") -> None:
    try:
        typer.echo(text)
    except UnicodeEncodeError:
        typer.echo(text.encode("utf-8", "replace").decode("utf-8", "replace"))


def ok(msg: str) -> None:
    typer.secho("✓ " + msg, fg=typer.colors.GREEN)


def warn(msg: str) -> None:
    typer.secho("! " + msg, fg=typer.colors.YELLOW)


def err(msg: str) -> None:
    typer.secho("✗ " + msg, fg=typer.colors.RED, err=True)


def emit_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def die(msg: str, code: int = 1) -> None:
    err(msg)
    raise typer.Exit(code)


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


# --------------------------------------------------------------------------
# 依赖装配
# --------------------------------------------------------------------------

def build_runtime(*, with_mcp: bool = False, load_plugins: bool = True):
    """装配运行时：配置、数据库、工具、记忆、技能、插件。"""
    from .. import paths
    from ..config import get_config, get_manager

    paths.ensure_all()
    cfg = get_config()
    mgr = get_manager()
    try:
        from ..storage.db import get_db

        db = get_db()
    except Exception as e:  # pragma: no cover
        die(f"数据库初始化失败：{e}")
        raise

    from ..tools import register_builtin
    from ..tools.base import get_registry

    reg = get_registry()
    if not reg.all():
        res = register_builtin(reg)
        for mod, e in (res.get("errors") or {}).items():
            warn(f"工具模块加载失败 {mod}：{e}")
    for name in cfg.tools.disabled or []:
        reg.disable(name)

    from ..memory.manager import get_memory

    mem = get_memory(cfg.memory)
    mem.bind_config_manager(mgr)

    from ..skills.manager import get_skills

    skills = get_skills(cfg, db)

    plugins = None
    if load_plugins:
        try:
            from ..plugins.manager import PluginManager

            plugins = PluginManager(config=cfg, db=db, registry=reg)
            if cfg.plugins.auto_load:
                plugins.load_all()
        except Exception as e:
            warn(f"插件加载失败：{e}")

    mcp = None
    if with_mcp and cfg.mcp.enabled:
        try:
            from ..mcp.client import MCPManager

            mcp = MCPManager(config=cfg)
        except Exception as e:
            warn(f"MCP 初始化失败：{e}")

    return {
        "config": cfg, "manager": mgr, "db": db, "registry": reg,
        "memory": mem, "skills": skills, "plugins": plugins, "mcp": mcp,
    }


def make_agent(rt: dict, *, session_id: str | None = None, workspace: str | None = None):
    from ..core.agent import Agent

    ag = Agent(session_id=session_id, workspace=workspace, config=rt["config"])
    ag.approval.set_responder(None)  # CLI 下由 _ask_approval 单独处理
    ag._mcp = rt.get("mcp")
    ag._plugins = rt.get("plugins")
    return ag


# --------------------------------------------------------------------------
# 根命令
# --------------------------------------------------------------------------

@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-v", help="显示版本"),
) -> None:
    """Fengcode 命令行。不带子命令时进入交互式对话。"""
    if version:
        out(f"{__app_name__} v{__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # 不带子命令 → 直接进对话（见 cli_main 的兜底逻辑）
        pass


# --------------------------------------------------------------------------
# chat —— 交互式对话
# --------------------------------------------------------------------------

@app.command("chat")
def chat(
    session: str = typer.Option("", "--session", "-s", help="继续指定会话 ID"),
    model: str = typer.Option("", "--model", "-m", help="指定模型（provider/model）"),
    workspace: str = typer.Option("", "--workspace", "-w", help="工作区目录"),
    simple: bool = typer.Option(False, "--simple", help="使用朴素 REPL 而不是全屏 TUI"),
) -> None:
    """进入交互式对话。"""
    rt = build_runtime(with_mcp=True)
    ws = workspace or rt["config"].agent.workspace_override or None
    ag = make_agent(rt, session_id=session or None, workspace=ws)
    if model:
        ag.sessions.update(ag.session_id, model=model)

    if simple or not sys.stdout.isatty():
        from .repl import run_simple_repl

        asyncio.run(run_simple_repl(ag, rt))
        return
    try:
        from .tui import run_tui

        run_tui(ag, rt)
    except ImportError as e:
        warn(f"全屏 TUI 不可用（{e}），改用朴素模式")
        from .repl import run_simple_repl

        asyncio.run(run_simple_repl(ag, rt))


@app.command("run")
def run_cmd(
    prompt: list[str] = typer.Argument(None, help="任务描述（可多段；省略则从 stdin 读）"),
    model: str = typer.Option("", "--model", "-m", help="指定模型"),
    workspace: str = typer.Option("", "--workspace", "-w", help="工作区"),
    session: str = typer.Option("", "--session", "-s", help="会话 ID（用于多轮）"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出结果"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="不打印过程，只输出结果"),
    approve: str = typer.Option(
        "ask", "--approve",
        help="审批策略：ask（询问）/ auto（自动允许）/ deny（一律拒绝）",
    ),
    max_steps: int = typer.Option(0, "--max-steps", help="最大步数"),
) -> None:
    """执行一次性任务（适合脚本与管道）。"""
    text = " ".join(prompt) if prompt else ""
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    text = (text or "").strip()
    if not text:
        die("请提供任务描述，例如：fengcode run \"统计当前目录的文件数\"")

    rt = build_runtime(with_mcp=True)
    if approve == "auto":
        rt["config"].permissions.mode = "allow"
    elif approve == "deny":
        rt["config"].permissions.mode = "deny"
    ag = make_agent(rt, session_id=session or None, workspace=workspace or None)

    if not quiet and not json_out:
        from ..utils import human_tokens

        out(f"模型：{model or rt['manager'].default_model_ref() or '（未配置）'}")
        out(f"工作区：{ag.workspace}")
        out("")

    def on_event(ev) -> None:
        if quiet or json_out:
            return
        if ev.type == "reasoning" and ev.text:
            typer.secho(ev.text, fg=typer.colors.BRIGHT_BLACK, nl=False)
        elif ev.type == "text" and ev.text:
            typer.echo(ev.text, nl=False)

    res = asyncio.run(
        ag.run(text, model=model or None, stream=not json_out, on_event=on_event,
               max_steps=max_steps or None)
    )

    if json_out:
        emit_json(res.to_dict())
    else:
        if not quiet:
            out("")
        out(res.content or (f"（失败：{res.error}）" if res.error else "（无输出）"))
        if not quiet:
            from ..utils import human_duration, human_money, human_tokens

            out("")
            out(f"— 用时 {human_duration(res.duration)}　"
                f"{res.steps} 步　{human_tokens(res.usage.get('total_tokens', 0))} tokens　"
                f"{human_money(res.cost, res.currency)}")
    raise typer.Exit(1 if res.error else 0)


# --------------------------------------------------------------------------
# serve —— 启动服务
# --------------------------------------------------------------------------

def _pid_name(pid: int) -> str:
    """取进程名（小写）。取不到返回空串。"""
    if not pid or pid <= 0:
        return ""
    try:
        import psutil  # type: ignore

        return (psutil.Process(int(pid)).name() or "").lower()
    except Exception:
        pass
    if os.name == "nt":
        try:
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=8,
            ).stdout or ""
            if "," in out:
                return out.split(",")[0].strip().strip('"').lower()
        except Exception:
            return ""
    else:
        try:
            with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as f:
                return f.read().strip().lower()
        except Exception:
            return ""
    return ""


def _pid_is_fengcode(pid: int) -> bool:
    """该 pid 是否确实是本程序的进程。

    ★ 为什么必须校验：**操作系统的 pid 会被回收复用**。
      若锁只判断「这个 pid 还活着」，那么上一次异常退出留下的 pid
      很可能已被分配给任意系统进程，于是每次都认定「已有实例在运行」，
      用户将**再也无法启动程序**（实测踩到：旧 pid 变成了 SearchFilterHost）。
      这里改为核对进程名/命令行，只有确实是本程序才算「冲突」。
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        p = psutil.Process(int(pid))
        name = (p.name() or "").lower()
        if "fengcode" in name:
            return True
        try:
            cmd = " ".join(p.cmdline() or []).lower()
        except Exception:
            cmd = ""
        # 源码运行 / 命令行启动时，进程名可能是 python.exe，靠命令行识别
        return "fengcode" in cmd
    except Exception:
        pass
    name = _pid_name(pid)
    if not name:
        return False          # 取不到名字 → 不敢拦，宁可放行
    return "fengcode" in name


def _pid_alive(pid: int) -> bool:
    """判断某个 pid 是否还活着（跨平台）。

    ★ 为什么要自己判断：单实例锁不能只看「pid 文件存在」——进程异常退出时
      文件会留下，按「文件存在就拒绝启动」会把用户永久挡在门外。
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259                 # STILL_ACTIVE
                return True
            finally:
                k.CloseHandle(h)
        except Exception:
            return True      # 拿不准时宁可认为「活着」，避免漏判成多开
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


@app.command("serve")
def serve(
    host: str = typer.Option("", "--host", help="监听地址（默认取配置）"),
    port: int = typer.Option(0, "--port", "-p", help="端口（默认取配置）"),
    open_browser: bool = typer.Option(False, "--open", help="启动后打开浏览器"),
    reload: bool = typer.Option(False, "--reload", help="代码变更自动重载（开发用）"),
    no_mcp: bool = typer.Option(False, "--no-mcp", help="不启动 MCP 服务器"),
    force: bool = typer.Option(False, "--force", help="已有实例在运行时也强制启动（多开会抢同一批会话）"),
) -> None:
    """启动 HTTP 服务（桌面端 / Web UI / API 的后端）。"""
    from ..config import get_config
    from .. import paths

    paths.ensure_all()
    cfg = get_config()
    h = host or cfg.server.host or "127.0.0.1"
    p = int(port or cfg.server.port or 7845)

    # ★ 1-J：单实例锁（按**数据目录**，不是按 exe 路径）。
    #   为什么按数据目录：pid 文件就在 data_dir 下，两个进程抢的正是同一批
    #   会话与 DB（「桌面版 + 手动起的命令行版」是最常见的撞车场景）。
    #   ★ 用户经常同时开发/使用，所以必须留 --force，不能一刀切挡住。
    lock = paths.pid_file()
    if lock.exists() and not force:
        try:
            old = int((lock.read_text(encoding="utf-8") or "").strip() or 0)
        except Exception:
            old = 0
        # 判定冲突需同时满足「进程还在」且「确实是本程序」。
        # ★ 只判断前者会因为 pid 被系统回收复用而永久误锁（实测踩到），
        #   详见 _pid_is_fengcode 的说明。
        if old and old != os.getpid() and _pid_alive(old) and _pid_is_fengcode(old):
            die(f"已有一个实例在运行（pid {old}），数据目录：{paths.home()}")
            out("  如果是它已经卡住，先结束那个进程；确实要一起跑就加 --force。")
            raise SystemExit(1)
        # pid 文件是上次异常退出留下的僵尸（或该 pid 已被系统分配给别的进程）
        # → 直接接管，别挡住用户
    # 写入 PID，便于停止
    try:
        paths.pid_file().write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    if no_mcp:
        cfg.mcp.enabled = False

    url = f"http://{'127.0.0.1' if h in ('0.0.0.0', '::') else h}:{p}"
    ok(f"{__app_name__} 服务启动：{url}")
    out(f"  数据目录：{paths.home()}")
    out(f"  访问令牌：{'已设置' if cfg.server.token else '未设置（本机访问无限制）'}")
    out("  按 Ctrl+C 停止")
    out("")

    if open_browser:
        import threading
        import webbrowser

        def _open() -> None:
            import time

            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open, daemon=True).start()

    try:
        import uvicorn
    except ImportError:
        die("缺少 uvicorn：python -m pip install uvicorn")
        return

    from ..server.app import create_app

    if reload:
        uvicorn.run("fengcode.server.app:create_app", factory=True, host=h, port=p, reload=True)
    else:
        uvicorn.run(create_app(), host=h, port=p, log_level="info", access_log=False)
    try:
        paths.pid_file().unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# mcp
# --------------------------------------------------------------------------

@mcp_app.command("list")
def mcp_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """列出已配置的 MCP 服务器。"""
    rt = build_runtime(with_mcp=True, load_plugins=False)
    servers = rt["config"].mcp.servers
    if json_out:
        emit_json([s.model_dump(exclude_none=True) for s in servers])
        return
    if not servers:
        out("还没有配置 MCP 服务器。")
        return
    out(f"已配置 {len(servers)} 个 MCP 服务器：")
    for s in servers:
        target = s.command or s.url or "?"
        args = (" " + " ".join(s.args)) if s.args else ""
        flag = "✓" if s.enabled else "✗"
        out(f"  {flag} {s.name:<24} [{s.type:<12}] {target}{args}")


@mcp_app.command("test")
def mcp_test(
    name: str = typer.Argument(..., help="服务器名称"),
    timeout: float = typer.Option(45.0, "--timeout", help="超时秒数"),
) -> None:
    """测试某个 MCP 服务器能否连通并列出其工具。"""
    rt = build_runtime(with_mcp=True, load_plugins=False)
    if rt["mcp"] is None:
        die("MCP 未启用（检查配置 mcp.enabled）")
    res = asyncio.run(rt["mcp"].test(name))
    if res.get("ok"):
        ok(f"连接成功（{res['duration']}s），发现 {res['tool_count']} 个工具：")
        for t in res.get("tools") or []:
            out(f"  · {t}")
    else:
        die(f"连接失败：{res.get('error')}")


@mcp_app.command("start")
def mcp_start(name: str = typer.Argument(..., help="服务器名称")) -> None:
    """启动一个 MCP 服务器并列出工具。"""
    rt = build_runtime(with_mcp=True, load_plugins=False)
    if rt["mcp"] is None:
        die("MCP 未启用")
    st = asyncio.run(rt["mcp"].start_server(name))
    ok(f"已启动 {name}，发现 {len(st.tools)} 个工具")
    for t in st.tools:
        out(f"  · {t.full_name}：{t.description[:70]}")


@mcp_app.command("serve")
def mcp_serve(
    transport: str = typer.Option("stdio", "--transport", help="传输方式：stdio 或 http"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(7846, "--port"),
    include: str = typer.Option("", "--include", help="只暴露这些工具（逗号分隔）"),
    exclude: str = typer.Option("", "--exclude", help="排除这些工具（逗号分隔）"),
) -> None:
    """把 Fengcode 自身作为 MCP 服务器暴露给外部客户端。"""
    rt = build_runtime(with_mcp=False)
    from ..mcp.server import FengcodeMCPServer

    srv = FengcodeMCPServer(
        registry=rt["registry"], memory=rt["memory"], skills=rt["skills"],
        name=rt["config"].mcp.server_name or "fengcode",
    )
    inc = [x.strip() for x in include.split(",") if x.strip()]
    exc = [x.strip() for x in exclude.split(",") if x.strip()]
    srv._include = inc or None  # type: ignore[attr-defined]
    srv._exclude = exc or None  # type: ignore[attr-defined]
    tools = srv.tool_definitions(include=inc or None, exclude=exc or None)

    if transport == "stdio":
        warn(f"以 stdio 方式暴露 {len(tools)} 个工具（等待客户端连接…）")
        raise typer.Exit(asyncio.run(srv.serve_stdio()))

    # HTTP 方式
    warn(f"以 HTTP 方式暴露 {len(tools)} 个工具于 http://{host}:{port}/mcp")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    async def endpoint(request: Any) -> Response:
        try:
            msg = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            return JSONResponse({"error": {"code": -32700, "message": "解析失败"}}, status_code=400)
        resp = await srv.handle(msg)
        if resp is None:
            return Response("", status_code=202)
        return JSONResponse(resp)

    sapp = Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])
    import uvicorn

    uvicorn.run(sapp, host=host, port=port, log_level="info")


# --------------------------------------------------------------------------
# skill
# --------------------------------------------------------------------------

@skill_app.command("list")
def skill_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """列出全部技能。"""
    rt = build_runtime(load_plugins=False)
    items = rt["skills"].list_all()
    if json_out:
        emit_json(items)
        return
    if not items:
        out("没有可用技能。可把 SKILL.md 放进技能目录。")
        return
    out(f"共 {len(items)} 个技能：")
    for s in items:
        flag = "✓" if s["enabled"] else "✗"
        out(f"  {flag} {s['name']:<22} [{s['source']:<8}] {s['description'][:60]}")


@skill_app.command("show")
def skill_show(name: str = typer.Argument(..., help="技能名称")) -> None:
    """查看某个技能的完整内容。"""
    rt = build_runtime(load_plugins=False)
    d = rt["skills"].load(name)
    if d is None:
        die(f"没有找到技能：{name}")
    out(f"# {d['name']}（{d.get('path')}）")
    out("")
    out(d.get("body") or "")


@skill_app.command("search")
def skill_search(query: str = typer.Argument(..., help="搜索关键词")) -> None:
    """按关键词搜索技能。"""
    rt = build_runtime(load_plugins=False)
    hits = rt["skills"].search(query)
    if not hits:
        out("没有匹配的技能。")
        return
    for s in hits:
        out(f"· {s['name']}：{s['description'][:80]}")


@skill_app.command("reload")
def skill_reload() -> None:
    """重新扫描技能目录。"""
    rt = build_runtime(load_plugins=False)
    d = rt["skills"].reload()
    n = rt["skills"].sync_state()
    ok(f"发现 {d['count']} 个技能（已同步 {n} 条状态）")
    for p in d["dirs"]:
        out(f"  扫描：{p}")


# --------------------------------------------------------------------------
# plugin
# --------------------------------------------------------------------------

@plugin_app.command("list")
def plugin_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """列出全部插件。"""
    rt = build_runtime()
    if rt["plugins"] is None:
        die("插件系统不可用")
    items = rt["plugins"].list()
    if json_out:
        emit_json(items)
        return
    if not items:
        out("没有插件。")
        return
    out(f"共 {len(items)} 个插件：")
    for p in items:
        flag = "✓" if p.get("loaded") else ("·" if p.get("enabled") else "✗")
        tag = "内置" if p.get("builtin") else "用户"
        out(f"  {flag} {p['name']:<20} [{tag}] v{p['version']:<8} 工具 {p.get('tool_count', 0)}  {p['description'][:50]}")
        if p.get("error"):
            warn(f"      {p['error'][:120]}")


@plugin_app.command("enable")
def plugin_enable(name: str = typer.Argument(...), off: bool = typer.Option(False, "--off")) -> None:
    """启用/禁用插件。"""
    rt = build_runtime()
    okk = rt["plugins"].enable(name, not off)
    if okk:
        ok(f"已{'禁用' if off else '启用'}插件 {name}")
    else:
        die(f"操作失败：{name}")


@plugin_app.command("reload")
def plugin_reload() -> None:
    """重新加载所有插件。"""
    rt = build_runtime()
    res = rt["plugins"].load_all()
    ok(f"加载 {len(res['loaded'])} 个：{', '.join(res['loaded']) or '（无）'}")
    if res["failed"]:
        for f in res["failed"]:
            err(f"失败：{f}")


@plugin_app.command("install")
def plugin_install(
    path: str = typer.Argument(..., help="插件目录或 zip 路径"),
    as_name: str = typer.Option("", "--as", help="安装为指定名称"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    """安装本地插件。"""
    rt = build_runtime()
    res = rt["plugins"].install(path, name=as_name or None, overwrite=overwrite)
    if res.get("ok"):
        ok(f"已安装：{res['name']}")
    else:
        die(res.get("error") or "安装失败")


@plugin_app.command("uninstall")
def plugin_uninstall(name: str = typer.Argument(...)) -> None:
    """卸载插件（内置插件受保护）。"""
    rt = build_runtime()
    okk = rt["plugins"].uninstall(name)
    ok(f"已卸载 {name}") if okk else die(f"卸载失败（可能是内置插件或不存在）：{name}")


# --------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------

@memory_app.command("list")
def memory_list(
    limit: int = typer.Option(30, "--limit", "-n"),
    kind: str = typer.Option("", "--kind", help="按种类过滤"),
    search: str = typer.Option("", "--search", "-q"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """列出记忆。"""
    rt = build_runtime(load_plugins=False)
    items = rt["memory"].list(limit=limit, kind=kind or None, search=search or None)
    if json_out:
        emit_json([i.to_dict() for i in items])
        return
    if not items:
        out("记忆库为空。")
        return
    for m in items:
        pin = "📌" if m.pinned else "  "
        out(f"{pin} [{m.kind:<10}] {m.title}")
        out(f"   {m.content[:200]}")
        out(f"   ID {m.id}　重要度 {m.importance:.2f}　访问 {m.access_count} 次")


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="检索词"),
    top_k: int = typer.Option(6, "--top-k", "-k"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """检索记忆（混合召回）。"""
    rt = build_runtime(load_plugins=False)
    items = asyncio.run(rt["memory"].recall(query, top_k=top_k))
    if json_out:
        emit_json([i.to_dict() for i in items])
        return
    if not items:
        out("没有找到相关记忆。")
        return
    out(f"检索到 {len(items)} 条：")
    for m in items:
        out(f"· [{m.kind}] {m.title}（相关度 {m.score:.2f}，{m.reason}）")
        out(f"  {m.content[:300]}")
        out(f"  ID {m.id}")


@memory_app.command("add")
def memory_add(
    content: str = typer.Argument(..., help="记忆内容"),
    title: str = typer.Option("", "--title", "-t"),
    kind: str = typer.Option("fact", "--kind", "-k",
                             help="fact/preference/episode/profile/task/decision"),
    importance: float = typer.Option(0.6, "--importance", "-i"),
    pinned: bool = typer.Option(False, "--pin"),
    tags: str = typer.Option("", "--tags", help="逗号分隔"),
) -> None:
    """写入一条记忆。"""
    rt = build_runtime(load_plugins=False)
    item = asyncio.run(rt["memory"].remember(
        content, title=title, kind=kind, importance=importance, pinned=pinned,
        tags=[t.strip() for t in tags.split(",") if t.strip()], source="cli",
    ))
    ok(f"已记住：{item.title}（ID {item.id}）")


@memory_app.command("rm")
def memory_rm(mem_id: str = typer.Argument(..., help="记忆 ID")) -> None:
    """删除一条记忆。"""
    rt = build_runtime(load_plugins=False)
    okk = rt["memory"].forget(mem_id)
    ok("已删除") if okk else die(f"未找到：{mem_id}")


@memory_app.command("stats")
def memory_stats(json_out: bool = typer.Option(False, "--json")) -> None:
    """记忆统计。"""
    rt = build_runtime(load_plugins=False)
    s = rt["memory"].stats()
    if json_out:
        emit_json(s)
        return
    out(f"记忆总数：{s['total']}（生效 {s['active']}，置顶 {s['pinned']}）")
    out(f"全文索引：{'启用' if s['fts'] else '未启用'}")
    out(f"向量后端：{s['embedding_backend']}")
    if s["by_kind"]:
        out("按种类：" + "，".join(f"{k}={v}" for k, v in s["by_kind"].items()))
    if s["by_scope"]:
        out("按范围：" + "，".join(f"{k}={v}" for k, v in s["by_scope"].items()))


@memory_app.command("reindex")
def memory_reindex() -> None:
    """重建全文索引与向量。"""
    rt = build_runtime(load_plugins=False)
    n = rt["memory"].reindex_all()
    ok(f"已重建 {n} 条记忆的索引")


@memory_app.command("import")
def memory_import(
    path: str = typer.Argument(..., help="要导入的文本文件"),
    kind: str = typer.Option("fact", "--kind"),
) -> None:
    """把文本文件按段落导入为记忆。"""
    rt = build_runtime(load_plugins=False)
    p = Path(path)
    if not p.is_file():
        die(f"文件不存在：{path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    blocks = [b.strip() for b in text.split("\n\n") if len(b.strip()) > 20]

    async def go() -> int:
        n = 0
        for b in blocks:
            await rt["memory"].remember(b, kind=kind, source=f"import:{p.name}")
            n += 1
        return n

    n = asyncio.run(go())
    ok(f"已从 {p.name} 导入 {n} 条记忆")


# --------------------------------------------------------------------------
# task
# --------------------------------------------------------------------------

@task_app.command("list")
def task_list(
    session: str = typer.Option("", "--session", "-s"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """列出任务清单。"""
    rt = build_runtime(load_plugins=False)
    from ..storage.tasks import TaskStore

    store = TaskStore(rt["db"])
    if json_out:
        emit_json({"tasks": store.list(session_id=session or None),
                   "summary": store.summary(session or None)})
        return
    out(store.render(session or None))


@task_app.command("goal")
def task_goal(
    objective: str = typer.Option("", "--set", help="设置目标"),
    session: str = typer.Option("", "--session", "-s"),
    complete: bool = typer.Option(False, "--complete", help="标记完成"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """查看或设置跨轮目标。"""
    rt = build_runtime(load_plugins=False)
    from ..storage.tasks import TaskStore

    store = TaskStore(rt["db"])
    sid = session or None
    if objective:
        g = store.set_goal(objective, session_id=sid)
        ok(f"已设定目标（ID {g['id']}）")
        return
    g = store.get_goal_for(sid)
    if complete and g:
        store.update_goal(g["id"], phase="completed")
        ok("目标已标记完成")
        return
    if json_out:
        emit_json(g)
        return
    if g is None:
        out("当前没有进行中的目标。")
        return
    out(f"目标：{g['objective']}")
    out(f"阶段：{g['phase']}　轮次：{g['rounds']}/{g['max_rounds'] or '∞'}")


# --------------------------------------------------------------------------
# workflow / job
# --------------------------------------------------------------------------

@wf_app.command("list")
def wf_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """列出工作流。"""
    rt = build_runtime(load_plugins=False)
    from ..storage.tasks import WorkflowStore

    items = WorkflowStore(rt["db"]).list()
    if json_out:
        emit_json(items)
        return
    if not items:
        out("还没有工作流。用 `fengcode workflow sample` 看一个示例。")
        return
    for w in items:
        n = len((w.get("dag") or {}).get("steps") or [])
        out(f"· {w['name']:<24} {n} 步　{w['description'][:50]}")
        out(f"  ID {w['id']}")


@wf_app.command("sample")
def wf_sample() -> None:
    """打印一个示例工作流定义（可另存后修改）。"""
    from ..workflow.engine import sample_dag

    emit_json(sample_dag())


@wf_app.command("run")
def wf_run(
    wf_id: str = typer.Argument(..., help="工作流 ID"),
    session: str = typer.Option("", "--session", "-s"),
) -> None:
    """运行工作流。"""
    rt = build_runtime(with_mcp=True)
    from ..storage.tasks import WorkflowStore
    from ..workflow.engine import WorkflowRunner, from_dict

    wf = WorkflowStore(rt["db"]).get(wf_id)
    if wf is None:
        die(f"没有找到工作流：{wf_id}")
    ag = make_agent(rt, session_id=session or None)
    runner = WorkflowRunner(
        agent_runner=lambda p: ag.run(p),
        tool_registry=rt["registry"], ctx=ag.tool_context(), bus=None,
        subagent_runner=ag.subagent_runner,
    )
    res = asyncio.run(runner.run(from_dict(wf.get("dag") or {}), workflow_id=wf_id))
    out(res.get("summary") or "")
    if not res.get("ok"):
        raise typer.Exit(1)


@job_app.command("list")
def job_list(json_out: bool = typer.Option(False, "--json")) -> None:
    """列出定时任务。"""
    rt = build_runtime(load_plugins=False)
    from ..scheduler.cron import describe_schedule
    from ..storage.tasks import JobStore

    items = JobStore(rt["db"]).list()
    if json_out:
        emit_json(items)
        return
    if not items:
        out("还没有定时任务。")
        return
    for j in items:
        flag = "✓" if j["enabled"] else "✗"
        out(f"  {flag} {j['name']:<20} [{j['kind']:<8}] {describe_schedule(j)}")
        out(f"      ID {j['id']}　上次：{j.get('last_status') or '从未'}")


@job_app.command("add")
def job_add(
    name: str = typer.Argument(..., help="任务名"),
    prompt: str = typer.Option("", "--prompt", help="要执行的提示词"),
    cron: str = typer.Option("", "--cron", help="cron 表达式（5 段）"),
    interval: float = typer.Option(0, "--interval", help="固定间隔秒数"),
    shell: str = typer.Option("", "--shell", help="要执行的命令"),
    workflow: str = typer.Option("", "--workflow", help="要运行的工作流 ID"),
) -> None:
    """新增定时任务。"""
    rt = build_runtime(load_plugins=False)
    from ..scheduler.cron import parse_cron
    from ..storage.tasks import JobStore

    if cron and not parse_cron(cron):
        die("cron 表达式无效（需要 5 段，如 0 3 * * *）")
    if not cron and interval <= 0:
        die("需要提供 --cron 或 --interval 之一")
    if prompt:
        kind, payload = "prompt", {"prompt": prompt}
    elif shell:
        kind, payload = "shell", {"command": shell}
    elif workflow:
        kind, payload = "workflow", {"workflow_id": workflow}
    else:
        die("需要提供 --prompt / --shell / --workflow 之一")
    j = JobStore(rt["db"]).save(name=name, cron=cron, interval_seconds=interval,
                                kind=kind, payload=payload)
    ok(f"已新增任务（ID {j['id']}）；需在配置中开启 scheduler.enabled 才会实际调度")


@job_app.command("rm")
def job_rm(job_id: str = typer.Argument(...)) -> None:
    """删除定时任务。"""
    rt = build_runtime(load_plugins=False)
    from ..storage.tasks import JobStore

    okk = JobStore(rt["db"]).delete(job_id)
    ok("已删除") if okk else die(f"未找到：{job_id}")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

@config_app.command("show")
def config_show(
    raw: bool = typer.Option(False, "--raw", help="显示文件原文"),
) -> None:
    """显示当前配置。"""
    from .. import paths

    rt = build_runtime(load_plugins=False)
    if raw:
        p = rt["manager"].path
        out(f"# 配置文件：{p}")
        out("")
        out(Path(p).read_text(encoding="utf-8") if Path(p).is_file() else "（文件不存在）")
        return
    out(f"配置文件：{rt['manager'].path}")
    out(f"数据目录：{paths.home()}")
    out(f"默认模型：{rt['manager'].default_model_ref() or '（未配置）'}")
    out(f"供应商：{len(rt['config'].providers)} 个"
        f"（已配密钥 {sum(1 for p in rt['config'].providers if rt['manager'].resolve_api_key(p))} 个）")
    out(f"审批模式：{rt['config'].permissions.mode}")
    out(f"工作区：{rt['config'].agent.workspace_override or paths.workspace_dir()}")
    emit_json(rt["manager"].to_dict())


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="点号路径，如 llm.temperature"),
    value: str = typer.Argument(..., help="值（自动识别类型）"),
) -> None:
    """修改单个配置项（如 `fengcode config set llm.temperature 0.5`）。"""
    rt = build_runtime(load_plugins=False)
    parsed: Any = value
    low = value.strip().lower()
    if low in ("true", "yes", "1", "on"):
        parsed = True
    elif low in ("false", "no", "0", "off"):
        parsed = False
    else:
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                if value.startswith("[") or value.startswith("{"):
                    try:
                        parsed = json.loads(value)
                    except ValueError:
                        parsed = value
                else:
                    parsed = value
    try:
        rt["manager"].set_value(key, parsed)
    except Exception as e:
        die(f"设置失败：{e}")
    ok(f"已设置 {key} = {parsed!r}")


@config_app.command("get")
def config_get(key: str = typer.Argument(..., help="点号路径")) -> None:
    """读取单个配置项。"""
    rt = build_runtime(load_plugins=False)
    d: Any = rt["manager"].to_dict()
    for part in key.split("."):
        if isinstance(d, dict) and part in d:
            d = d[part]
        else:
            die(f"没有这个配置项：{key}")
    emit_json(d)


@config_app.command("path")
def config_path() -> None:
    """显示配置文件与数据目录位置。"""
    from .. import paths

    rt = build_runtime(load_plugins=False)
    out(f"配置文件：{rt['manager'].path}")
    out(f"数据目录：{paths.home()}")
    out(f"数据库：  {paths.db_file()}")
    out(f"工作区：  {paths.workspace_dir()}")
    out(f"技能目录：{paths.skills_dir()}")
    out(f"插件目录：{paths.plugins_dir()}")
    out(f"日志目录：{paths.logs_dir()}")


@config_app.command("provider")
def config_provider(
    list_all: bool = typer.Option(False, "--list", help="列出所有供应商"),
    add: str = typer.Option("", "--add", help="新增：名称"),
    base_url: str = typer.Option("", "--base-url"),
    api_key: str = typer.Option("", "--api-key"),
    models: str = typer.Option("", "--models", help="逗号分隔"),
    kind: str = typer.Option("openai", "--kind"),
    preset: str = typer.Option("", "--preset", help="用预设模板（wanxiang/deepseek/zhipu/mimo 等）"),
    rm: str = typer.Option("", "--rm", help="删除：名称"),
) -> None:
    """管理模型供应商。"""
    rt = build_runtime(load_plugins=False)
    mgr = rt["manager"]
    if add:
        try:
            p = mgr.add_provider(
                add, kind=kind, base_url=base_url, api_key=api_key or None,
                models=[m.strip() for m in models.split(",") if m.strip()],
                preset=preset or None,
            )
        except Exception as e:
            die(f"添加失败：{e}")
        ok(f"已添加供应商：{p.name}（{len(p.models)} 个模型）")
        return
    if rm:
        okk = mgr.remove_provider(rm)
        ok(f"已删除：{rm}") if okk else die(f"未找到：{rm}")
        return
    out(f"共 {len(rt['config'].providers)} 个供应商：")
    for p in rt["config"].providers:
        key = mgr.resolve_api_key(p)
        flag = "✓" if (key and p.enabled) else ("·" if p.enabled else "✗")
        keyinfo = "有密钥" if key else "无密钥"
        out(f"  {flag} {p.name:<28} [{p.kind:<10}] {keyinfo:<6} {len(p.models)} 模型")
        out(f"      {p.base_url or '（未填地址）'}")


@config_app.command("test")
def config_test(
    name: str = typer.Argument(..., help="供应商名称"),
    model: str = typer.Option("", "--model", help="指定模型"),
    list_models: bool = typer.Option(False, "--list-models", help="只拉取模型列表"),
) -> None:
    """测试供应商连通性。"""
    rt = build_runtime(load_plugins=False)
    from ..llm.router import get_llm

    llm = get_llm(rt["manager"])
    if list_models:
        models = asyncio.run(llm.list_models(name))
        if not models:
            die("没有拉取到模型列表（可能该站不支持 /v1/models，或地址/密钥有误）")
        ok(f"共 {len(models)} 个模型：")
        for m in models[:80]:
            out(f"  · {m}")
        return
    res = asyncio.run(llm.test_connection(name, model or None))
    if res.get("ok"):
        ok(f"连接成功：{res['model']}（{res['duration']:.2f}s）")
        out(f"  回复：{res.get('reply', '')}")
    else:
        die(f"连接失败：{res.get('error')}")


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# doctor / version
# --------------------------------------------------------------------------

@app.command("doctor")
def doctor(json_out: bool = typer.Option(False, "--json")) -> None:
    """环境自检：检查依赖、配置、常见问题。"""
    from .. import paths

    checks: list[dict[str, Any]] = []

    def add(name: str, okk: bool, detail: str = "", fix: str = "") -> None:
        checks.append({"name": name, "ok": okk, "detail": detail, "fix": fix})

    # 路径
    try:
        paths.ensure_all()
        add("数据目录可写", True, str(paths.home()))
    except Exception as e:
        add("数据目录可写", False, str(e), "检查目录权限或设置 FENGCODE_HOME")

    # Python
    import platform

    ver = sys.version_info
    add("Python 版本 ≥ 3.10", ver >= (3, 10), f"{platform.python_version()}", "升级 Python")

    # 核心依赖
    for mod, pkg in [
        ("httpx", "httpx"), ("pydantic", "pydantic"), ("yaml", "pyyaml"),
        ("typer", "typer"), ("starlette", "starlette"), ("uvicorn", "uvicorn"),
    ]:
        try:
            __import__(mod)
            add(f"依赖 {pkg}", True)
        except ImportError:
            add(f"依赖 {pkg}", False, f"缺少 {mod}", f"python -m pip install {pkg}")

    # 可选依赖
    for mod, pkg, why in [
        ("psutil", "psutil", "进程/资源监控"),
        ("bs4", "beautifulsoup4", "网页正文提取"),
        ("mcp", "mcp", "MCP 官方 SDK（本实现不依赖，仅备查）"),
        ("paramiko", "paramiko", "SSH 远程主机"),
        ("playwright", "playwright", "JS 页面渲染"),
    ]:
        try:
            __import__(mod)
            add(f"可选依赖 {pkg}", True, why)
        except ImportError:
            add(f"可选依赖 {pkg}", False, f"未安装（影响：{why}）",
                f"python -m pip install {pkg}")

    # 数据库
    try:
        from ..storage.db import get_db

        st = get_db().stats()
        add("数据库可用", True, f"{st['path']}（{st['sessions']} 会话，FTS={st['fts']}）")
    except Exception as e:
        add("数据库可用", False, str(e), "检查数据目录权限")

    # 配置
    try:
        from ..config import get_manager

        mgr = get_manager()
        n = len(mgr.config.providers)
        withkey = sum(1 for p in mgr.config.providers if mgr.resolve_api_key(p))
        add("配置文件可读", True, f"{mgr.path}（{n} 个供应商，{withkey} 个有密钥）")
        if withkey == 0:
            add("已配置可用模型", False, "没有任何供应商配了密钥",
                "在界面「设置→模型供应商」添加，或运行 fengcode import external_agent")
        else:
            ref = mgr.default_model_ref()
            add("已配置默认模型", bool(ref), ref or "未设置",
                "在设置里选择默认模型")
    except Exception as e:
        add("配置文件可读", False, str(e), "删除损坏的 config.toml 让程序重建")

    # 工具
    try:
        from ..tools import register_builtin
        from ..tools.base import get_registry

        reg = get_registry()
        res = register_builtin(reg)
        add("内置工具加载", not res["errors"], f"{len(res['registered'])} 个工具",
            f"检查报错模块：{res['errors']}")
        for mod, e in (res.get("errors") or {}).items():
            add(f"  工具模块 {mod.split('.')[-1]}", False, e, "检查该模块的依赖")
    except Exception as e:
        add("内置工具加载", False, str(e))

    # 技能
    try:
        from ..skills.manager import get_skills

        sm = get_skills()
        add("技能可用", len(sm.list_all()) > 0, f"{len(sm.list_all())} 个技能")
    except Exception as e:
        add("技能可用", False, str(e))

    # 外部工具
    import shutil as _sh

    for tool, why, fix in [
        ("git", "版本控制操作", "安装 Git（https://git-scm.com）"),
        ("node", "运行 npx 类 MCP 服务器", "安装 Node.js"),
        ("docker", "容器化部署（可选）", "安装 Docker Desktop"),
    ]:
        found = _sh.which(tool)
        add(f"外部工具 {tool}", bool(found), found or f"未找到（影响：{why}）", fix if not found else "")

    # 服务端口
    try:
        from ..config import get_config

        cfg = get_config()
        import socket

        s = socket.socket()
        try:
            s.bind((cfg.server.host, cfg.server.port))
            add("服务端口可用", True, f"{cfg.server.host}:{cfg.server.port}")
        except OSError:
            add("服务端口可用", False, f"{cfg.server.port} 已被占用",
                "在设置里改端口，或停掉占用它的进程")
        finally:
            s.close()
    except Exception as e:
        add("服务端口可用", False, str(e))

    if json_out:
        emit_json({"checks": checks, "failed": [c for c in checks if not c["ok"]]})
        raise typer.Exit(1 if any(not c["ok"] for c in checks) else 0)

    good = [c for c in checks if c["ok"]]
    bad = [c for c in checks if not c["ok"]]
    out(f"自检完成：{len(good)} 项通过，{len(bad)} 项需要注意")
    out("")
    for c in good:
        typer.secho(f"  ✓ {c['name']}", fg=typer.colors.GREEN, nl=False)
        if c["detail"]:
            typer.secho(f"　{c['detail']}", fg=typer.colors.BRIGHT_BLACK)
        else:
            out("")
    if bad:
        out("")
        out("需要处理：")
        for c in bad:
            typer.secho(f"  ✗ {c['name']}", fg=typer.colors.YELLOW, nl=False)
            typer.secho(f"　{c['detail']}", fg=typer.colors.YELLOW)
            if c["fix"]:
                out(f"      → {c['fix']}")
    raise typer.Exit(1 if bad else 0)


@app.command("version")
def version_cmd() -> None:
    """显示版本信息。"""
    import platform

    from .. import paths

    out(f"{__app_name__} v{__version__}")
    out(f"  Python：{platform.python_version()}（{sys.executable}）")
    out(f"  系统：  {platform.system()} {platform.release()}")
    out(f"  数据目录：{paths.home()}")


@app.command("tools")
def tools_cmd(
    group: str = typer.Option("", "--group", "-g", help="只看某个分组"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """列出全部可用工具。"""
    rt = build_runtime(load_plugins=False)
    from ..tools.base import get_registry

    reg = rt["registry"]
    groups = reg.groups()
    if json_out:
        emit_json(groups)
        return
    total = 0
    for g, names in sorted(groups.items()):
        if group and g != group:
            continue
        out(f"[{g}]（{len(names)}）")
        for n in names:
            t = reg.get(n)
            d = "（写）" if t and t.dangerous else ("（读）" if t and t.read_only else "")
            out(f"  · {n:<22}{d} {t.description[:64] if t else ''}")
            total += 1
        out("")
    out(f"共 {total} 个工具")


def cli_main() -> None:
    """控制台入口（pyproject 的 scripts 指向这里）。

    不带任何子命令时进入交互式对话，让 ``fengcode`` 双击/直接回车即用。
    """
    import sys as _sys

    argv = _sys.argv[1:]
    # 没有任何参数（也不带 --help 之类）时，默认进入 chat
    if not argv:
        _sys.argv = [_sys.argv[0], "chat"]
    try:
        app()
    except KeyboardInterrupt:
        out("")
        out("已中断。")
        raise SystemExit(130)


if __name__ == "__main__":
    cli_main()
