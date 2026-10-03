"""验证插件系统：发现/加载/工具注册/钩子/权限/安装卸载/面板。"""
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
HOME = Path(tempfile.mkdtemp(prefix="plugin-test-"))
os.environ["FENGCODE_HOME"] = str(HOME)

PASS, FAIL = [], []


def check(name):
    def deco(fn):
        async def wrapper(*a, **kw):
            try:
                await fn(*a, **kw)
                PASS.append(name)
                print(f"  [OK]   {name}")
            except Exception as e:
                import traceback
                FAIL.append((name, f"{type(e).__name__}: {e}"))
                print(f"  [FAIL] {name} -> {type(e).__name__}: {e}")
                if os.environ.get("TRACE"):
                    traceback.print_exc()
        return wrapper
    return deco


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("插件系统验证")
    print("=" * 62)
    print()

    from fengcode import paths
    from fengcode.config import get_config
    from fengcode.plugins import PluginManager
    from fengcode.security.paths import PathGuard
    from fengcode.security.sandbox import LocalSandbox
    from fengcode.storage.db import get_db
    from fengcode.tools import register_builtin
    from fengcode.tools.base import ToolContext, get_registry

    reg = get_registry()
    register_builtin(reg)
    db = get_db()
    ws = HOME / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    pm = PluginManager(config=get_config(), db=db, registry=reg)

    @check("1. 发现内置插件")
    async def _discover():
        mans = pm.discover()
        names = [m.name for m in mans]
        assert "system-monitor" in names, f"没发现内置插件：{names}"
        sm = next(m for m in mans if m.name == "system-monitor")
        assert sm.builtin is True
        assert sm.version == "1.0.0"
        assert "tools" in sm.permissions and "hooks" in sm.permissions
        assert len(sm.tools) == 3, f"manifest 里声明了 {len(sm.tools)} 个工具"
        print(f"         发现 {len(mans)} 个插件：{names}")

    @check("2. 加载插件并注册工具")
    async def _load():
        r = pm.load_all()
        assert "system-monitor" in r["loaded"], r
        assert not r["failed"], r["failed"]
        tools = pm.tool_names()
        assert "system-monitor__snapshot" in tools, tools
        assert "system-monitor__top_processes" in tools
        assert "system-monitor__check_pressure" in tools
        assert "system-monitor__tool_stats" in tools, tools
        print(f"         注册了 {len(tools)} 个插件工具")
        # 全局注册表里也要能看到
        assert reg.get("system-monitor__snapshot") is not None

    @check("3. 插件工具真实执行")
    async def _exec():
        ctx = ToolContext(
            workspace=ws, config=get_config(),
            guard=PathGuard(workspace=ws, write_paths=[str(ws)], deny_patterns=[]),
            sandbox=LocalSandbox(cwd=ws),
        )
        r = await reg.execute("system-monitor__snapshot", {"detail": "full"}, ctx, skip_approval=True)
        assert r.ok, r.error
        assert "cpu" in r.content, r.content[:300]
        print(f"         snapshot 返回 {len(r.content)} 字符")

        r2 = await reg.execute(
            "system-monitor__top_processes", {"by": "memory", "count": 3}, ctx, skip_approval=True
        )
        assert r2.ok, r2.error
        print(f"         top_processes 正常")

        r3 = await reg.execute("system-monitor__check_pressure", {}, ctx, skip_approval=True)
        assert r3.ok, r3.error
        assert "healthy" in r3.content
        print(f"         check_pressure 正常")

    @check("4. 钩子被触发（after_tool 统计耗时）")
    async def _hooks():
        # 前面已执行过工具；此时统计里应有数据
        assert "after_tool" in pm._hooks, pm._hooks.keys()
        assert len(pm._hooks["after_tool"]) >= 1
        # 手动触发一次钩子
        res = pm.fire("after_tool", {"tool": "read_file", "ok": True, "duration": 1.5})
        assert isinstance(res, dict)
        ctx = ToolContext(
            workspace=ws, config=get_config(),
            guard=PathGuard(workspace=ws, write_paths=[str(ws)], deny_patterns=[]),
            sandbox=LocalSandbox(cwd=ws),
        )
        st = await reg.execute("system-monitor__tool_stats", {}, ctx, skip_approval=True)
        assert st.ok
        assert "read_file" in st.content, st.content[:400]
        print(f"         钩子统计工作正常")

    @check("5. 插件注入系统提示")
    async def _prompt():
        parts = pm.prompt_parts()
        assert parts, "没有提示片段"
        joined = "\n".join(parts)
        assert "系统监控" in joined
        assert "check_pressure" in joined
        print(f"         注入了 {len(joined)} 字符提示")

    @check("6. 权限不足时报错（最小权限原则）")
    async def _perm():
        # 造一个没声明 ui 权限的插件，却想注册面板
        d = paths.plugins_dir() / "noperm"
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.yaml").write_text(
            "name: noperm\nversion: 1.0.0\ndescription: 权限测试\n"
            "permissions: [tools]\nmain: plugin.py\n",
            encoding="utf-8",
        )
        (d / "plugin.py").write_text(
            "def setup(api):\n"
            "    api.register_panel('p', '面板')\n",
            encoding="utf-8",
        )
        try:
            pm.load("noperm")
            raise AssertionError("未声明 ui 权限却允许注册面板")
        except PermissionError as e:
            assert "ui" in str(e), str(e)
            print(f"         正确拒绝：{str(e)[:70]}")

    @check("7. 安装 / 启用 / 禁用 / 卸载")
    async def _lifecycle():
        # 造一个外部插件目录
        src = HOME / "myplug"
        src.mkdir(parents=True, exist_ok=True)
        (src / "manifest.yaml").write_text(
            "name: myplug\nversion: 2.1.0\ndescription: 外部插件\n"
            "permissions: [tools, hooks]\nmain: plugin.py\n"
            "tools:\n  - name: hello\n    description: 打招呼\n"
            "    parameters: {who: {type: string}}\n",
            encoding="utf-8",
        )
        (src / "plugin.py").write_text(
            "def setup(api):\n"
            "    def hello(who='世界'):\n"
            "        return f'你好，{who}！'\n"
            "    api.register_tool('hello', hello, description='打招呼',\n"
            "                      parameters={'who': {'type': 'string'}})\n"
            "    api.on('before_tool', lambda p: {'seen': True})\n",
            encoding="utf-8",
        )
        res = pm.install(src)
        assert res["ok"], res
        assert "myplug" in [x["name"] for x in pm.list()], "安装后未出现在列表"
        print(f"         安装成功：{res['name']}")

        # 执行它的工具
        ctx = ToolContext(
            workspace=ws, config=get_config(),
            guard=PathGuard(workspace=ws, write_paths=[str(ws)], deny_patterns=[]),
            sandbox=LocalSandbox(cwd=ws),
        )
        r = await reg.execute("myplug__hello", {"who": "测试"}, ctx, skip_approval=True)
        assert r.ok, r.error
        assert "你好，测试" in r.content, r.content
        print(f"         工具执行：{r.content.strip()}")

        # 禁用 → 工具应被移除
        ok = pm.enable("myplug", False)
        assert ok, "禁用失败"
        assert reg.get("myplug__hello") is None, "禁用后工具仍在"
        print("         禁用后工具已移除")

        # 重新启用
        ok2 = pm.enable("myplug", True)
        assert ok2, "启用失败"
        assert reg.get("myplug__hello") is not None, "启用后工具没回来"
        print("         重新启用成功")

        # 卸载
        ok3 = pm.uninstall("myplug")
        assert ok3, "卸载失败"
        assert "myplug" not in [x["name"] for x in pm.list()], "卸载后仍在列表"
        print("         卸载成功")

    @check("8. 内置插件不允许删除")
    async def _protect():
        ok = pm.uninstall("system-monitor")
        assert not ok, "内置插件竟然被删除了"
        assert pm.plugins.get("system-monitor") is not None
        print("         内置插件受保护")

    @check("9. 声明式插件（只有 manifest 无代码）")
    async def _declarative():
        d = paths.plugins_dir() / "declonly"
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.yaml").write_text(
            "name: declonly\nversion: 1.0.0\ndescription: 声明式\n"
            "permissions: [tools]\ntools:\n"
            "  - name: stub\n    description: 桩工具\n"
            '    parameters: {x: {type: string}}\n',
            encoding="utf-8",
        )
        p = pm.load("declonly")
        assert p.manifest.name == "declonly"
        assert reg.get("declonly__stub") is not None, "声明式工具未注册"
        ctx = ToolContext(
            workspace=ws, config=get_config(),
            guard=PathGuard(workspace=ws, write_paths=[str(ws)], deny_patterns=[]),
            sandbox=LocalSandbox(cwd=ws),
        )
        r = await reg.execute("declonly__stub", {"x": "1"}, ctx, skip_approval=True)
        assert not r.ok and "没有绑定处理函数" in (r.error or ""), r.error
        print("         声明式插件注册成功，执行时给出明确提示")

    @check("10. 状态统计与清单")
    async def _stats():
        s = pm.stats()
        assert s["loaded"] >= 1
        assert s["tools"] >= 4
        items = pm.list()
        assert all("name" in i and "version" in i for i in items)
        print(f"         统计：{s['loaded']} 个已加载，{s['tools']} 个工具，{s['hooks']} 个钩子")

    for fn in (_discover, _load, _exec, _hooks, _prompt, _perm, _lifecycle, _protect,
               _declarative, _stats):
        await fn()

    print()
    print("=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n, e in FAIL:
        print(f"  ✗ {n}\n      {e}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
