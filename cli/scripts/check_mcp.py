"""验证 MCP：用一个内置的最小 stdio server 做真实往返测试。"""
import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault('FENGCODE_HOME', tempfile.mkdtemp(prefix='mcp-test-'))

PASS, FAIL = [], []


def check(name):
    def deco(fn):
        async def wrapper(*a, **kw):
            try:
                await fn(*a, **kw)
                PASS.append(name)
                print('  [OK]  ', name)
            except Exception as e:
                import traceback
                FAIL.append((name, f'{type(e).__name__}: {e}'))
                print('  [FAIL]', name, '->', type(e).__name__, e)
                if os.environ.get('TRACE'):
                    traceback.print_exc()
        wrapper.__name__ = getattr(fn, '__name__', 'w')
        return wrapper
    return deco


# 写一个最小 MCP server 脚本
server_py = r'''
import json, sys

TOOLS = [
    {"name": "echo", "description": "回显输入文本",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "两数相加",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "boom", "description": "总是失败", "inputSchema": {"type": "object", "properties": {}}},
]

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m = msg.get("method")
    mid = msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-server", "version": "0.1.0"},
            "instructions": "测试服务器"}})
    elif m == "notifications/initialized":
        continue
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif m == "tools/call":
        p = msg.get("params") or {}
        n = p.get("name"); a = p.get("arguments") or {}
        if n == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "回显：" + str(a.get("text",""))}]}})
        elif n == "add":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "和=" + str(a.get("a",0)+a.get("b",0))}]}})
        elif n == "boom":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "炸了"}], "isError": True}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "未知工具"}})
    elif m == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "不支持 " + str(m)}})
'''

tmp = Path(tempfile.mkdtemp(prefix='mcp-srv-'))
srv = tmp / 'srv.py'
srv.write_text(server_py, encoding='utf-8')


async def main():
    from fengcode.config.schema import Config, McpConfig, McpServerConfig
    from fengcode.mcp.client import MCPManager, ServerState

    cfg = Config()
    cfg.mcp = McpConfig(servers=[
        McpServerConfig(name="test", type="stdio", command=sys.executable, args=[str(srv)], enabled=True),
        McpServerConfig(name="bad", type="stdio", command="definitely-not-a-real-cmd-xyz", args=[], enabled=True),
        McpServerConfig(name="off", type="stdio", command=sys.executable, args=[str(srv)], enabled=False),
    ])
    mgr = MCPManager(config=cfg)

    @check("MCP 配置读取（enabled 过滤）")
    async def _states():
        sts = mgr.state_map()
        assert set(sts) == {"test", "bad", "off"}, set(sts)
        assert sts["test"].namespace == "test"
        assert sts["off"].enabled is False

    @check("连通性测试（真实起进程）")
    async def _test():
        r = await mgr.test("test")
        assert r["ok"], r
        assert r["tool_count"] == 3, r
        assert "echo" in r["tools"]

    @check("start_all 跳过 disabled，坏的报错不影响好的")
    async def _all():
        res = await mgr.start_all()
        assert "test" in res and res["test"].startswith("ok"), res
        assert "bad" in res and "失败" in res["bad"], res

    @check("工具 schema 转 ToolSpec")
    async def _specs():
        specs = mgr.specs()
        names = {s.name for s in specs}
        assert names == {"test__echo", "test__add", "test__boom"}, names
        echo = next(s for s in specs if s.name == "test__echo")
        assert echo.parameters["type"] == "object"
        assert "MCP:test" in echo.description
        assert echo.source == "mcp:test"

    @check("调用工具（中文往返）")
    async def _call():
        ok, text, data = await mgr.call_tool("test__echo", {"text": "你好世界"})
        assert ok and "你好世界" in text, (ok, text)
        ok2, text2, _ = await mgr.call_tool("test__add", {"a": 3, "b": 4})
        assert ok2 and "7" in text2, (ok2, text2)

    @check("工具报错正确传递（isError）")
    async def _err():
        ok, text, _ = await mgr.call_tool("test__boom", {})
        assert not ok, "应标记为错误"
        assert "炸了" in text

    @check("未知工具与未知服务器")
    async def _unknown():
        ok, text, _ = await mgr.call_tool("test__nope", {})
        assert not ok and "没有找到" in text
        try:
            await mgr.start_server("nonexistent")
            raise AssertionError("应报错")
        except Exception as e:
            assert "没有配置" in str(e), e

    @check("状态报告与重启")
    async def _status():
        st = mgr.status()
        test_st = next(s for s in st if s["name"] == "test")
        assert test_st["connected"] is True
        assert test_st["status"] == "ready"
        assert test_st["tool_count"] == 3
        again = await mgr.restart("test")
        assert again.status == "ready"
        assert len(again.tools) == 3

    @check("优雅关闭")
    async def _close():
        await mgr.close()
        assert not mgr.connections or all(not c.alive for c in mgr.connections.values())

    # ---- 导入器 ----
    @check("MCP 配置导入（TOML plugins 形式）")
    async def _imp_plugins_cfg():
        from fengcode.mcp.importers import from_plugins_config
        data = {"plugins": [
            {"name": "github", "type": "stdio", "command": "npx",
             "args": ["-y", "@modelcontextprotocol/server-github"],
             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test123"}},
            {"name": "高德地图", "type": "sse", "url": "https://mcp.amap.com/sse?key=abc"},
            {"name": "ctx", "type": "http", "url": "https://mcp.context7.com/mcp"},
            {"name": "zhipu", "type": "stdio", "command": "node", "args": ["/x.js"],
             "env": {"ZHIPU_API_KEY": "${ZHIPU_KEY}"},
             "tool_timeout_seconds": {"text-to-image": 240}},
        ]}
        got = from_plugins_config(data, env={"ZHIPU_KEY": "real-key-here"})
        assert len(got) == 4, len(got)
        gh = next(s for s in got if s.name == "github")
        assert gh.type == "stdio" and gh.command == "npx"
        assert gh.env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test123"
        amap = next(s for s in got if s.name == "高德地图")
        assert amap.type == "sse" and "key=abc" in amap.url
        zp = next(s for s in got if s.name == "zhipu")
        assert zp.env["ZHIPU_API_KEY"] == "real-key-here", zp.env
        assert zp.tool_timeout["text-to-image"] == 240.0

    @check("官方 mcp.json 导入 + 导出往返")
    async def _imp_json():
        from fengcode.mcp.importers import export_mcp_json, from_mcp_json
        data = {"mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
            "remote": {"type": "http", "url": "https://example.com/mcp", "headers": {"X-A": "1"}},
            "disabled-one": {"command": "x", "args": [], "disabled": True},
        }}
        got = from_mcp_json(data)
        assert len(got) == 3
        assert next(s for s in got if s.name == "filesystem").type == "stdio"
        assert next(s for s in got if s.name == "remote").type == "http"
        assert next(s for s in got if s.name == "disabled-one").enabled is False
        back = export_mcp_json(got)
        assert "filesystem" in back["mcpServers"]
        assert back["mcpServers"]["remote"]["url"] == "https://example.com/mcp"

    @check("scan_default_configs 不崩")
    async def _scan():
        from fengcode.mcp.importers import scan_default_configs
        r = scan_default_configs()
        assert "candidates" in r

    # ---- MCP Server 端 ----
    @check("Fengcode 作为 MCP Server：initialize/tools/list/call")
    async def _server_side():
        from fengcode.mcp.server import FengcodeMCPServer
        from fengcode.tools.base import reset_registry
        from fengcode.tools import register_builtin
        reset_registry()
        reg = register_builtin() and __import__("fengcode.tools.base", fromlist=["get_registry"]).get_registry()
        srv_obj = FengcodeMCPServer(registry=None)
        d = srv_obj.tool_definitions()
        assert d == [], "无注册表时不该有工具"

        from fengcode.tools.base import get_registry
        srv2 = FengcodeMCPServer(registry=get_registry())
        init = await srv2.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init["result"]["serverInfo"]["name"] == "fengcode"
        assert "tools" in init["result"]["capabilities"]

        tl = await srv2.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in tl["result"]["tools"]}
        assert "read_file" in names and "shell" in names
        assert all(t["inputSchema"]["type"] == "object" for t in tl["result"]["tools"])

        # 真正执行一个工具
        call = await srv2.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "system_info", "arguments": {"detail": "basic"}}})
        txt = call["result"]["content"][0]["text"]
        assert "Python" in txt, txt[:200]
        assert call["result"]["isError"] is False

        # 未知方法
        bad = await srv2.handle({"jsonrpc": "2.0", "id": 4, "method": "nope", "params": {}})
        assert bad["error"]["code"] == -32601

        # 通知类返回 None
        n = await srv2.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert n is None

    for fn in (_states, _test, _all, _specs, _call, _err, _unknown, _status, _close,
               _imp_plugins_cfg, _imp_json, _scan, _server_side):
        await fn()

    print()
    print('=' * 56)
    print(f'通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
    for n, e in FAIL:
        print(f'  ✗ {n}\n      {e}')
    print('=' * 56)
    return 1 if FAIL else 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
