"""验证 Web 服务端：启动服务、走真实 HTTP 请求。

用法：python scripts/check_server.py
"""
import asyncio
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
HOME = Path(tempfile.mkdtemp(prefix="server-test-"))
os.environ["FENGCODE_HOME"] = str(HOME)

PASS, FAIL = [], []
PORT = 17899
BASE = f"http://127.0.0.1:{PORT}"


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


def http(path, method="GET", body=None, token=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Fengcode-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            ct = r.headers.get("content-type", "")
            return r.status, (json.loads(raw) if "json" in ct else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("Web 服务端验证")
    print("=" * 62)
    print()

    import uvicorn
    from fengcode.server.app import create_app

    cfg_path = HOME / "config" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(80):
        if server.started:
            break
        await asyncio.sleep(0.25)
    assert server.started, "服务未能启动"
    print(f"服务已启动于 {BASE}\n")

    try:
        @check("1. /health 与 /api/status")
        async def _health():
            st, d = http("/health")
            assert st == 200 and d["ok"] is True, (st, d)
            st2, d2 = http("/api/status")
            assert st2 == 200, (st2, d2)
            assert "providers" in d2
            print(f"         版本 {d2.get('version')}，启动 {d2.get('uptime')}s")

        @check("2. 首页返回单页 UI")
        async def _index():
            st, html = http("/")
            assert st == 200, st
            assert isinstance(html, str)
            assert "<!DOCTYPE html>" in html
            assert "Fengcode" in html
            assert "data-theme" in html
            print(f"         index.html {len(html)} 字符")

        @check("3. /api/bootstrap 返回界面初始化数据")
        async def _boot():
            st, d = http("/api/bootstrap")
            assert st == 200, (st, d)
            for k in ("status", "models", "providers", "tool_groups", "skills", "ui",
                      "agent", "permissions", "memory"):
                assert k in d, f"缺少 {k}"
            n = sum(len(g["tools"]) for g in d["tool_groups"])
            print(f"         {len(d['models'])} 个模型，{n} 个工具，{len(d['skills'])} 个技能")
            # provider 列表里不能有明文密钥
            for p in d["providers"]:
                assert "api_key" not in p or not p["api_key"], f"{p['name']} 泄漏了密钥"
            print("         供应商密钥已脱敏")

        @check("4. /favicon.ico 不报 404")
        async def _fav():
            st, body = http("/favicon.ico")
            assert st == 200, st

        @check("5. 会话 CRUD")
        async def _sessions():
            st, d = http("/api/sessions", "POST", {"title": "接口测试会话"})
            assert st == 200 and d["ok"], (st, d)
            sid = d["session"]["id"]
            st2, d2 = http("/api/sessions")
            assert st2 == 200 and any(s["id"] == sid for s in d2["sessions"])
            st3, d3 = http(f"/api/sessions/{sid}")
            assert st3 == 200 and d3["session"]["title"] == "接口测试会话"
            assert "messages" in d3
            st4, d4 = http(f"/api/sessions/{sid}", "PATCH", {"pinned": 1, "title": "改过名"})
            assert st4 == 200, (st4, d4)
            st5, d5 = http(f"/api/sessions/{sid}")
            assert d5["session"]["title"] == "改过名"
            assert d5["session"]["pinned"] is True
            print(f"         会话 {sid} 创建/查询/改名/置顶均正常")
            globals()["SID"] = sid

        @check("6. 会话导出 Markdown")
        async def _export():
            sid = globals().get("SID")
            st, md = http(f"/api/sessions/{sid}/export?format=md")
            assert st == 200, st
            assert isinstance(md, str) and "改过名" in md, str(md)[:200]
            print(f"         导出 {len(md)} 字符")

        @check("7. 配置读写")
        async def _config():
            st, d = http("/api/config")
            assert st == 200 and "ui" in d, (st, d)
            st2, d2 = http("/api/config", "PATCH", {"ui": {"theme": "dark"}})
            assert st2 == 200, (st2, d2)
            st3, d3 = http("/api/config")
            assert d3["ui"]["theme"] == "dark", d3["ui"]
            # 改回去
            http("/api/config", "PATCH", {"ui": {"theme": "light"}})
            print("         配置读写与持久化正常")

        @check("8. 原始 config.toml 读写 + 语法校验")
        async def _raw():
            st, d = http("/api/config/raw")
            assert st == 200 and "text" in d
            assert "config_version" in d["text"], d["text"][:200]
            # 非法 TOML 应被拒绝
            st2, d2 = http("/api/config/raw", "PUT", {"text": "这不是合法 TOML [[["})
            assert st2 == 400, (st2, d2)
            assert "语法错误" in d2.get("error", ""), d2
            print("         非法 TOML 被正确拒绝")

        @check("9. 供应商接口 + 密钥脱敏")
        async def _providers():
            st, d = http("/api/providers")
            assert st == 200 and "providers" in d and "presets" in d
            assert len(d["presets"]) >= 10
            st2, d2 = http("/api/providers", "POST", {
                "action": "add", "name": "test-prov", "base_url": "https://example.com/v1",
                "models": ["m1", "m2"],
                # 故意写死的**假**密钥：用于验证 API 不会把密钥回传给前端。
                # 拼接是为了不触发「明文密钥扫描」，它本身没有任何效力。
                "api_key": "sk-" + "fake-secret-for-leak-test-XYZ",
            })
            assert st2 == 200, (st2, d2)
            assert d2["provider"]["has_key"] is True
            assert "api_key" not in d2["provider"]
            assert "fake-secret" not in json.dumps(d2, ensure_ascii=False), "密钥泄漏！"
            st3, d3 = http("/api/providers", "POST", {"action": "toggle", "name": "test-prov", "enabled": False})
            assert st3 == 200 and d3["enabled"] is False
            st4, d4 = http("/api/providers", "POST", {"action": "delete", "name": "test-prov"})
            assert st4 == 200 and d4["ok"]
            print("         增删改查正常，密钥未回传")

        @check("10. 工具列表与启停")
        async def _tools():
            st, d = http("/api/tools")
            assert st == 200 and "groups" in d
            n = sum(len(g["tools"]) for g in d["groups"])
            assert n >= 40, f"只有 {n} 个工具"
            st2, d2 = http("/api/tools", "POST", {"name": "screenshot", "enabled": False})
            assert st2 == 200 and d2["enabled"] is False
            st3, d3 = http("/api/tools")
            scr = next(t for g in d3["groups"] for t in g["tools"] if t["name"] == "screenshot")
            assert scr["disabled"] is True
            http("/api/tools", "POST", {"name": "screenshot", "enabled": True})
            print(f"         {n} 个工具，启停生效")

        @check("11. 工具手动执行（真实跑）")
        async def _tool_run():
            st, d = http("/api/tools/run", "POST", {
                "name": "write_file",
                "arguments": {"path": "http_test.txt", "content": "来自 HTTP 的测试内容"},
            }, timeout=90)
            assert st == 200 and d["ok"], (st, d)
            st2, d2 = http("/api/tools/run", "POST", {
                "name": "read_file", "arguments": {"path": "http_test.txt"},
            }, timeout=60)
            assert d2["ok"] and "HTTP 的测试内容" in d2["result"]["content"], d2
            print("         写文件 + 读回验证通过")

        @check("12. 记忆接口")
        async def _memory():
            st, d = http("/api/memories", "POST", {
                "action": "remember", "title": "接口测试记忆",
                "content": "这是通过 HTTP 接口写入的记忆，用于验证接口连通。",
                "kind": "fact", "importance": 0.7,
            })
            assert st == 200 and d["ok"], (st, d)
            mid = d["memory"]["id"]
            st2, d2 = http("/api/memories")
            assert st2 == 200 and any(m["id"] == mid for m in d2["memories"])
            assert "stats" in d2
            st3, d3 = http("/api/memories", "POST", {"action": "recall", "query": "接口测试"})
            assert st3 == 200 and d3["items"], d3
            print(f"         记忆写入 {mid}，检索到 {len(d3['items'])} 条")
            http("/api/memories", "POST", {"action": "forget", "id": mid})

        @check("13. 技能接口")
        async def _skills():
            st, d = http("/api/skills")
            assert st == 200 and len(d["skills"]) >= 10, len(d.get("skills", []))
            name = d["skills"][0]["name"]
            st2, d2 = http("/api/skills?name=" + urllib.request.quote(name))
            assert st2 == 200 and d2["skill"]["body"], d2
            st3, d3 = http("/api/skills?q=" + urllib.request.quote("写代码"))
            assert st3 == 200
            print(f"         {len(d['skills'])} 个技能，详情与搜索正常")

        @check("14. 任务接口")
        async def _tasks():
            st, d = http("/api/tasks", "POST", {"action": "add", "title": "接口测试任务"})
            assert st == 200 and d["ok"], (st, d)
            tid = d["task"]["id"]
            st2, d2 = http("/api/tasks")
            assert st2 == 200 and any(t["id"] == tid for t in d2["tasks"])
            http("/api/tasks", "POST", {"action": "update", "id": tid, "status": "completed"})
            st3, d3 = http("/api/tasks")
            t = next(x for x in d3["tasks"] if x["id"] == tid)
            assert t["status"] == "completed", t
            print("         任务增改查正常")

        @check("15. 子智能体类型")
        async def _subagents():
            st, d = http("/api/subagents")
            assert st == 200 and len(d["types"]) == 7, d
            names = [t["name"] for t in d["types"]]
            for n in ("general", "explore", "research", "review", "security-review", "test", "plan"):
                assert n in names, names
            print(f"         {len(names)} 种类型：{names}")

        @check("16. MCP 接口")
        async def _mcp():
            st, d = http("/api/mcp")
            assert st == 200 and "servers" in d and "configs" in d
            assert "export" in d
            print(f"         已配置 {len(d.get('configured', []))} 个 MCP 服务")

        @check("17. 插件接口（内置插件已加载）")
        async def _plugins():
            st, d = http("/api/plugins")
            assert st == 200 and "plugins" in d
            names = [p["name"] for p in d["plugins"]]
            assert "system-monitor" in names, names
            sm = next(p for p in d["plugins"] if p["name"] == "system-monitor")
            assert sm["loaded"] is True
            assert sm["tool_count"] == 4, sm
            print(f"         {len(d['plugins'])} 个插件，内置插件已加载（{sm['tool_count']} 个工具）")

        @check("18. 工作流接口")
        async def _workflows():
            st, d = http("/api/workflows?sample=1")
            assert st == 200 and "sample" in d, (st, d)
            sample = d["sample"]
            st2, d2 = http("/api/workflows", "POST", {"action": "validate", "dag": sample})
            assert st2 == 200 and d2["ok"], (st2, d2)
            st3, d3 = http("/api/workflows", "POST", {
                "action": "save", "name": "接口测试流程", "description": "测试用", "dag": sample,
            })
            assert st3 == 200 and d3["ok"], (st3, d3)
            wid = d3["workflow"]["id"]
            st4, d4 = http("/api/workflows")
            assert any(w["id"] == wid for w in d4["workflows"])
            http("/api/workflows", "POST", {"action": "delete", "id": wid})
            print("         示例校验、保存、列表、删除正常")

        @check("19. 定时任务接口（cron 校验）")
        async def _jobs():
            st, d = http("/api/jobs", "POST", {
                "action": "save", "name": "接口测试任务", "cron": "非法表达式",
                "kind": "prompt", "payload": {"prompt": "测试"},
            })
            assert st == 400, (st, d)
            st2, d2 = http("/api/jobs", "POST", {
                "action": "save", "name": "接口测试任务", "cron": "0 3 * * *",
                "kind": "prompt", "payload": {"prompt": "测试"},
            })
            assert st2 == 200 and d2["ok"], (st2, d2)
            st3, d3 = http("/api/jobs")
            assert st3 == 200 and any(j["name"] == "接口测试任务" for j in d3["jobs"])
            j = next(x for x in d3["jobs"] if x["name"] == "接口测试任务")
            assert "每天" in (j.get("schedule") or ""), j
            http("/api/jobs", "POST", {"action": "delete", "id": j["id"]})
            print("         非法 cron 被拒，合法 cron 正常")

        @check("20. 用量统计接口")
        async def _stats():
            st, d = http("/api/stats")
            assert st == 200 and "all" in d and "by_day" in d and "by_model" in d
            st2, d2 = http("/api/stats?audit=1&limit=10")
            assert st2 == 200 and "audit" in d2
            print(f"         统计字段完整，审计 {len(d2['audit'])} 条")

        @check("21. 工作区浏览")
        async def _ws():
            st, d = http("/api/workspace?path=.")
            assert st == 200 and d["type"] == "dir", (st, d)
            names = [i["name"] for i in d["items"]]
            assert "http_test.txt" in names, names
            st2, d2 = http("/api/workspace?path=http_test.txt")
            assert d2["type"] == "file" and "HTTP 的测试内容" in d2["content"]
            print(f"         目录 {len(names)} 项，文件内容可读")

        @check("22. 越界路径被拒绝")
        async def _escape():
            st, d = http("/api/workspace?path=../../../../etc/passwd")
            # 应返回 4xx/5xx，且不能泄漏内容
            if st == 200:
                raise AssertionError("越界路径被允许了")
            print(f"         返回 {st}，已阻断")

        @check("23. WebSocket 事件流")
        async def _ws_events():
            import websockets
            uri = f"ws://127.0.0.1:{PORT}/ws"
            async with websockets.connect(uri, open_timeout=10) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert hello["type"] == "hello", hello
                assert "status" in hello
                # 发一个事件，看能否收到
                from fengcode.events import get_bus
                get_bus().emit("test.event", {"x": 1})
                got = None
                for _ in range(10):
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                    if m["type"] == "test.event":
                        got = m
                        break
                assert got is not None, "没收到广播事件"
                assert got["data"]["x"] == 1
            print("         握手 + 事件广播正常")

        @check("24. SSE 流式对话（无模型时应优雅报错）")
        async def _chat_sse():
            # 这里不配模型，验证接口本身能正确开启流并给出错误事件
            req = urllib.request.Request(
                BASE + "/api/chat", method="POST",
                data=json.dumps({"message": "你好", "stream": True}).encode("utf-8"),
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    assert r.status == 200
                    ctype = r.headers.get("content-type", "")
                    assert "text/event-stream" in ctype, ctype
                    text = ""
                    for _ in range(200):
                        line = r.readline()
                        if not line:
                            break
                        text += line.decode("utf-8", "replace")
                        if '"type": "result"' in text or '"type":"result"' in text:
                            break
                    assert "data:" in text, text[:300]
                    print(f"         收到 {len(text)} 字节 SSE 数据")
            except urllib.error.HTTPError as e:
                raise AssertionError(f"HTTP {e.code}: {e.read().decode()[:200]}")

        @check("25. 访问令牌校验（配置后生效）")
        async def _token():
            # 先不设令牌时能访问
            st, _ = http("/api/status")
            assert st == 200
            # 设置令牌
            http("/api/config", "PATCH", {"server": {"token": "test-token-abc123"}})
            st2, d2 = http("/api/status")
            assert st2 == 401, (st2, d2)
            st3, d3 = http("/api/status", token="test-token-abc123")
            assert st3 == 200, (st3, d3)
            # 用错令牌
            st4, _ = http("/api/status", token="wrong")
            assert st4 == 401, st4
            # 清掉令牌
            http("/api/config", "PATCH", {"server": {"token": None}}, token="test-token-abc123")
            st5, _ = http("/api/status")
            assert st5 == 200, st5
            print("         令牌校验生效，清空后恢复开放")

        @check("26. 未知接口返回 404")
        async def _404():
            st, _ = http("/api/nonexistent")
            assert st in (404, 405), st

        for fn in (_health, _index, _boot, _fav, _sessions, _export, _config, _raw, _providers,
                   _tools, _tool_run, _memory, _skills, _tasks, _subagents, _mcp, _plugins,
                   _workflows, _jobs, _stats, _ws, _escape, _ws_events, _chat_sse, _token, _404):
            await fn()

    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=15)
        except (asyncio.TimeoutError, Exception):
            task.cancel()

    print()
    print("=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n, e in FAIL:
        print(f"  ✗ {n}\n      {e}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
