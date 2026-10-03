"""扫描所有 API 端点，找出返回 5xx 的问题（一次全查清）。"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7856"

# (方法, 路径, body) —— 覆盖服务端注册的全部路由
CASES = [
    ("GET", "/health", None),
    ("GET", "/", None),
    ("GET", "/favicon.ico", None),
    ("GET", "/api/status", None),
    ("GET", "/api/bootstrap", None),
    ("GET", "/api/config", None),
    ("GET", "/api/config/raw", None),
    ("GET", "/api/providers", None),
    ("GET", "/api/import/env", None),
    ("GET", "/api/sessions", None),
    ("GET", "/api/tasks", None),
    ("GET", "/api/memories", None),
    ("GET", "/api/memories?stats=1", None),
    ("GET", "/api/skills", None),
    ("GET", "/api/skills?q=代码", None),
    ("GET", "/api/tools", None),
    ("GET", "/api/subagents", None),
    ("GET", "/api/mcp", None),
    ("GET", "/api/plugins", None),
    ("GET", "/api/workflows", None),
    ("GET", "/api/workflows?sample=1", None),
    ("GET", "/api/jobs", None),
    ("GET", "/api/remote", None),
    ("GET", "/api/stats", None),
    ("GET", "/api/stats?audit=1&limit=5", None),
    ("GET", "/api/stats?recent=1&limit=5", None),
    ("GET", "/api/workspace?path=.", None),
    ("POST", "/api/providers/test", {"name": "不存在的供应商"}),
    ("POST", "/api/approval", {"id": "x"}),
    ("GET", "/api/approval/pending", None),
]


def call(method, path, body):
    # 路径里的中文必须做百分号编码，否则 urllib 会报 ascii 编码错误
    url = BASE + urllib.parse.quote(path, safe="/?&=%")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def main():
    bad = []
    print("扫描 %d 个端点 @ %s" % (len(CASES), BASE))
    print()
    for method, path, body in CASES:
        code, raw = call(method, path, body)
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        flag = "OK " if 200 <= code < 300 else ("4xx" if 400 <= code < 500 else "ERR")
        extra = ""
        if code >= 500 or code == 0:
            # 提取错误关键行
            for line in text.split("\n"):
                s = line.strip()
                if s and ("Error" in s or "error" in s[:40] or "Exception" in s):
                    if len(s) < 200:
                        extra = " ← " + s
                        break
            bad.append((method, path, code, text[:400]))
        print("  [%s] %-4s %-46s %s%s" % (flag, method, path, code, extra))

    print()
    if not bad:
        print("全部端点正常（无 5xx）")
        return 0
    print("发现 %d 个问题端点：" % len(bad))
    for m, p, c, t in bad:
        print()
        print("  ✗ %s %s → %d" % (m, p, c))
        print("    " + t.replace("\n", "\n    ")[:600])
    return 1


if __name__ == "__main__":
    sys.exit(main())
