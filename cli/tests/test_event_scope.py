"""防止「模块级函数引用外层函数局部变量」导致的静默失效。

★★★ 实测过的真实事故（1.2.14 发布后仍在）：
  · 现象：发消息后「上下文窗口 / 命中率 / 累计 tokens」全是 0；
    但**切换会话再切回来就正常**。
  · 根因：`handleEvent` 是**模块级函数**，而 `case "usage"` 里引用了
    `send()` 的局部 const `streamOwnerSid`。在 "use strict" 下抛
    `ReferenceError: streamOwnerSid is not defined`，被 processPart 外层的
    空 catch 静默吞掉 → **整个 usage 分支不执行**。
  · 为什么切换会话正常：那条路走 `applySessionUsage`（loadSession），不经 handleEvent。
  · 为什么只有读数坏：text / tool 等分支不经过那行，所以照常工作。

本文件把这条教训固化为两道防线：
  ① 静态：handleEvent 函数体内不得出现「send() 的局部变量」；
  ② 动态：用 Node 在严格模式下真跑一次 usage 事件，必须无异常且写入读数。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_P = Path(__file__).resolve().parent.parent / "src"
os.environ.setdefault("FENGCODE_HOME", tempfile.mkdtemp(prefix="fengcode-scope-"))
if str(_P) not in sys.path:
    sys.path.insert(0, str(_P))

APP = _P / "fengcode" / "server" / "static" / "app.js"


def _fn_range(lines: list[str], name: str):
    """按大括号配平找出模块级函数体的行范围（1-based）。"""
    for i, l in enumerate(lines):
        if re.match(r"^(async\s+)?function\s+%s\s*\(" % re.escape(name), l.strip()):
            depth, started = 0, False
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if "{" in lines[j]:
                    started = True
                if started and depth == 0:
                    return i + 1, j + 1
    return None


def _send_locals(lines: list[str]) -> set[str]:
    r = _fn_range(lines, "send")
    assert r, "找不到 send()"
    names = set()
    for l in lines[r[0] - 1 : r[1]]:
        m = re.match(r"\s{2,}(?:const|let|var)\s+([A-Za-z_$][\w$]*)", l)
        if m:
            names.add(m.group(1))
    return names


def test_handle_event_does_not_reference_send_locals():
    """handleEvent 是模块级函数，绝不能读 send() 的局部变量（会抛 ReferenceError）。"""
    lines = io.open(APP, encoding="utf-8").read().splitlines()
    r = _fn_range(lines, "handleEvent")
    assert r, "找不到 handleEvent"
    body = "\n".join(lines[r[0] - 1 : r[1]])

    own = set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)", body)) | {"ev", "c"}
    code = re.sub(r"//.*", "", body)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    # 剥字符串字面量：`case "text":` 里的 text 不是变量引用
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)
    code = re.sub(r"'(?:[^'\\]|\\.)*'", "''", code)
    code = re.sub(r"`(?:[^`\\]|\\.)*`", "``", code, flags=re.S)

    bad = [n for n in sorted(_send_locals(lines) - own)
           if re.search(r"(?<![\w$.])%s(?![\w$])" % re.escape(n), code)]
    assert not bad, (
        "handleEvent 引用了 send() 的局部变量（会抛 ReferenceError 并被静默吞掉，"
        "导致整个分支不执行）：" + "、".join(bad)
    )


def test_usage_event_actually_runs_in_strict_mode():
    """在严格模式下真跑一次 usage 事件：不得抛异常，且必须写入读数。

    ★ 为什么必须动态跑：静态检查只能看「有没有引用坏名字」，
    而这条事故的本质是「异常被静默吞掉」—— 只有真跑一遍才能确认分支确实执行了。
    """
    src = io.open(APP, encoding="utf-8").read()
    i = src.find("function handleEvent(ev, c) {")
    assert i > 0, "找不到 handleEvent"
    j = src.find("\n}", i)
    fn = src[i : j + 2]

    stub = '''
"use strict";
const S = { turnUsage: {}, sessionId: "s1", contextLimit: 0, compactPct: 0.8, streaming: false };
// ★ 运行状态按会话隔离后，handleEvent 会用这几个**模块级**助手判定归属。
//   它们不是 send() 的局部变量（本用例要防的正是那种引用），所以沙箱照常提供，
//   与下面的 usageSnapshotHasData / fmtNum 同类。
let __box = "s1";
function activeSid() { return __box; }
function isCurrentSid(sid) { return (sid || "") === "s1"; }
function withBox(sid, fn) { const p = __box; __box = sid || ""; try { return fn(); } finally { __box = p; } }
function usageSnapshotHasData(s) {
  if (!s || typeof s !== "object") return false;
  return (Number(s.prompt_tokens || 0) > 0 || Number(s.completion_tokens || 0) > 0
    || Number(s.total_tokens || 0) > 0);
}
function fmtNum(n) { return String(n); }
function fmtCost() { return "0"; }
function renderInfoPanel() {}
function renderStatusBar() {}
const _hint = { textContent: "" };
const $ = (s) => (s === "#usage-hint" ? _hint : null);
'''
    tail = '''
const ev = { type: "usage", session_id: "s1", data: {
  usage: { prompt_tokens: 13084, completion_tokens: 2, total_tokens: 13086, cached_tokens: 0, cache_miss_tokens: 13084 },
  last_usage: { prompt_tokens: 13084, completion_tokens: 2, total_tokens: 13086, cached_tokens: 0, cache_miss_tokens: 13084 }
}};
let err = null;
try { handleEvent(ev, { ownerSid: "s1" }); } catch (e) { err = String(e); }
console.log(JSON.stringify({ err, prompt: S.turnUsage.prompt_tokens, miss: S.turnUsage.cache_miss_tokens }));
'''
    out = subprocess.run(["node", "-e", stub + fn + tail],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"node 执行失败: {out.stderr}"
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["err"] is None, f"handleEvent 抛异常（会被静默吞掉）：{r['err']}"
    assert r["prompt"] == 13084, f"usage 事件未写入读数，prompt={r['prompt']}"
    assert r["miss"] == 13084, f"未命中量未写入，miss={r['miss']}"
