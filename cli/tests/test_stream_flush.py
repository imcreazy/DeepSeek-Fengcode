"""防止「流式正文丢失」回归：缓冲必须在丢弃前同步落盘。

★★★ 实测事故（用户报，1.2.16 之后仍在）：
  · 刚开始对话时，每条思考后都有十几字的说明文字；
  · 任务做完后，那些说明文字**全部消失**，最终答复也没了；
  · 但**换对话再换回来**，文字又都显示了（因为那条路走 renderStored 从库里重画）。

★ 根因：`paintStream` 用 `requestAnimationFrame` **异步**写 DOM，而同一个网络分片里的
  多条 SSE 事件是被 processPart 的 for 循环**同步连续处理**的 —— 期间 rAF 一次都不执行。
  于是 `text(A) → tool.start → text(B)` 这个序列里，`tool.start` 会**同步**把
  assistEl / streamBuf 置空，而 A 还躺在缓冲里没画出来 → **永久丢失**。

★ 修法：凡是**要丢弃当前缓冲**的地方（tool.start 封存、result 收尾、finally 判空删气泡），
  先调 `flush()` 把缓冲同步写进 DOM。

本文件两道防线：
  ① 静态：那三处丢弃点之前必须出现 flush 调用；
  ② 动态：用 Node 复刻渲染状态机，验证「同分片多事件」下不再丢文字。
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
os.environ.setdefault("FENGCODE_HOME", tempfile.mkdtemp(prefix="fengcode-flush-"))
if str(_P) not in sys.path:
    sys.path.insert(0, str(_P))

APP = _P / "fengcode" / "server" / "static" / "app.js"


def test_flush_is_called_before_discarding_buffer():
    """三处「会丢弃缓冲」的位置之前，都必须先同步落盘。"""
    src = io.open(APP, encoding="utf-8").read()
    lines = src.splitlines()

    # ① tool.start：封存文字气泡（c.assist = null）之前
    i = src.find('case "tool.start": {')
    assert i > 0, "找不到 tool.start 分支"
    seg = src[i : src.find('case "tool.end": {', i)]
    assert "c.flush()" in seg, "tool.start 封存气泡前未同步落盘 → 会丢正文"
    assert seg.find("c.flush()") < seg.find("c.assist = null"), \
        "flush 必须在 c.assist = null **之前**"

    # ② result：收尾分支开头
    j = src.find('case "result": {')
    assert j > 0, "找不到 result 分支"
    seg2 = src[j : j + 400]
    assert "c.flush()" in seg2, "result 收尾前未同步落盘"

    # ③ finally：判空删气泡之前
    k = src.find("if (assistEl && !assistEl.textContent.trim())")
    assert k > 0, "找不到「空气泡移除」逻辑"
    before = src[max(0, k - 500) : k]
    assert "flush()" in before, \
        "判空删气泡之前未同步落盘 → rAF 未执行时会误删有内容的气泡（最终答复消失）"

    # ④ flush 必须挂在 streamContext 上（handleEvent 是模块级函数，只能经参数访问）
    assert "flush: flushStream" in src, "flush 未挂到 streamContext，模块级函数读不到"


def test_same_chunk_events_keep_all_text():
    """动态：同一分片内 text → tool.start → text → tool.start → text，三段文字都要保住。"""
    sim = r"""
"use strict";
let streamBuf = "", rafPending = false, rafQueue = [];
const requestAnimationFrame = (fn) => rafQueue.push(fn);
const flushRAF = () => { const q = rafQueue; rafQueue = []; q.forEach((f) => f()); };
let assistEl = null;
const bubbles = [];
const md = (t) => t;
const addBubble = () => { const b = { html: "" }; bubbles.push(b); return b; };

// 与 app.js 一致：flush 同步写，paint 走 rAF
const flushStream = () => { rafPending = false; if (assistEl && streamBuf) assistEl.html = md(streamBuf); };
const paintStream = () => {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; if (assistEl) assistEl.html = md(streamBuf); });
};
const onText = (t) => { if (!assistEl) assistEl = addBubble(); streamBuf += t; paintStream(); };
const onToolStart = () => {
  if (assistEl || (streamBuf && streamBuf.trim())) {
    flushStream();              // ★ 修复点
    assistEl = null; streamBuf = "";
  }
};

bubbles.length = 0; assistEl = null; streamBuf = ""; rafPending = false; rafQueue = [];
onText("说明A"); onToolStart();
onText("说明B"); onToolStart();
onText("说明C");
flushRAF();
console.log(JSON.stringify(bubbles.map((b) => b.html)));
"""
    out = subprocess.run(["node", "-e", sim], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    assert out.returncode == 0, f"node 失败: {out.stderr}"
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == ["说明A", "说明B", "说明C"], \
        f"同分片多事件下正文丢失：{got}（期望 ['说明A','说明B','说明C']）"


def test_no_extra_flush_when_buffer_empty():
    """空缓冲时 flush 不应产生空气泡（避免出现一堆空气泡）。"""
    sim = r"""
"use strict";
let streamBuf = "", rafPending = false;
let assistEl = null;
const bubbles = [];
const md = (t) => t;
const flushStream = () => { rafPending = false; if (assistEl && streamBuf) assistEl.innerHTML = md(streamBuf); };
flushStream();                      // 空缓冲 + 无气泡 → 不应创建任何东西
console.log(JSON.stringify({ bubbles: bubbles.length, assist: assistEl }));
"""
    out = subprocess.run(["node", "-e", sim], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    assert out.returncode == 0, f"node 失败: {out.stderr}"
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["bubbles"] == 0 and r["assist"] is None, "空缓冲时不应创建气泡"
