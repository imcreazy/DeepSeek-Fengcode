"""读数被清空（后端）的复现与防回归。

现象：会话跑了很久、累计 46 万 tokens，但右侧「上下文窗口 / 命中率」一直是
0 / 「尚无调用记录」，**退出重进也一样**。

根因（实测坐实）：
  · `agent.py` 的用量累计块写的是 `if resp.usage:`，而 `resp.usage` 是一个 **dataclass**，
    **没有 `__bool__`** → 一个全 0 的空 Usage 对象在 `if` 里同样为 True。
  · 调用失败时（连接断开、上游没发 usage 事件）拿到的正是这种空对象，
    于是 `last_usage = {全 0}` 把**几十万的真实读数覆盖成 0**，并随会话落库。
  · 退出重进读的是库里那个 0，所以「重进也没用」。

本文件锁定两件事：
  ① `Usage.has_data()` 能正确区分「空用量」与「真实用量」；
  ② `agent.py` 的覆盖分支用的是 `has_data()`，而不是裸 `if resp.usage:`
     （后者是本次 bug 的形态，绝不能回退）。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

_P = Path(__file__).resolve().parent.parent / "src"
os.environ.setdefault("FENGCODE_HOME", tempfile.mkdtemp(prefix="fengcode-usage-"))
if str(_P) not in sys.path:
    sys.path.insert(0, str(_P))

AGENT = _P / "fengcode" / "core" / "agent.py"


def test_empty_usage_has_no_data():
    """空用量必须判定为「没有数据」——这是本次 bug 的核心判据。"""
    from fengcode.llm.types import Usage

    u = Usage()
    assert u.has_data() is False, "空 Usage 必须 has_data() == False"
    # ★ 同时确认 dataclass 本身在 if 里是 True —— 这正是必须用 has_data 的原因
    assert bool(u) is True, (
        "dataclass 在 if 里为 True 是已知事实；正因为如此才不能写 `if resp.usage:`"
    )


def test_real_usage_has_data():
    from fengcode.llm.types import Usage

    assert Usage(prompt_tokens=13022).has_data() is True
    assert Usage(completion_tokens=5).has_data() is True
    assert Usage(total_tokens=100).has_data() is True
    # 只有 cached/miss/reasoning 而没有 prompt/completion/total 的，不算「有输入量」
    assert Usage(cached_tokens=100).has_data() is False


def test_only_cache_fields_do_not_count_as_real_read():
    """★ 关键边界：一次**失败**的调用可能带回来 cached=0/miss=0，必须仍判无数据。"""
    from fengcode.llm.types import Usage

    u = Usage(cached_tokens=0, cache_miss_tokens=0, reasoning_tokens=0)
    assert u.has_data() is False, "全 0（含各细分字段为 0）必须判为无数据"


def test_agent_guard_uses_has_data_not_bare_truthiness():
    """防止回退：覆盖快照前必须真的有 has_data 判定（否则失败调用会抹掉真实读数）。"""
    import re

    def guard_present(src: str) -> bool:
        """`if resp.usage:` 到 `last_usage = {` 之间是否出现了真实的 has_data 判定。

        为什么要「剥注释再找」：注释里往往会提到 has_data 这个字样，
        若不剥掉，一个只有注释、没有实际判定的写法会被误判为「有保护」。
        """
        i = src.find("if resp.usage:")
        if i < 0:
            return False
        j = src.find("last_usage = {", i)
        if j < 0:
            return False
        seg = re.sub(r"#.*", "", src[i:j])
        return "has_data" in seg

    src = io.open(AGENT, encoding="utf-8").read()
    assert guard_present(src), (
        "agent.py 的用量覆盖分支必须落在 has_data() 判定之内；"
        "退回 `if resp.usage:` 直接覆盖，会让失败调用的全 0 抹掉几十万的真实读数"
    )

    # ★ 自证判据有牙齿（三种形态，正确的要过、两种坏的都要被抓住）。
    #   这样即便将来有人改动本测试，也能立刻看出判据是否还有效。
    good = "if resp.usage:\n    u = resp.usage\n    if u.has_data():\n        last_usage = {"
    bad_no_guard = "if resp.usage:\n    u = resp.usage\n    last_usage = {"
    bad_comment_only = "if resp.usage:\n    u = resp.usage\n    # 这里本该有 has_data 判定\n    last_usage = {"
    assert guard_present(good) is True, "正确写法应判为有保护"
    assert guard_present(bad_no_guard) is False, "删掉 has_data 判定必须被抓住"
    assert guard_present(bad_comment_only) is False, "注释里提到不算，必须有真实判定"


def test_frontend_treats_all_zero_snapshot_as_invalid():
    """前端也要把「存在但全 0」的快照视为无效，否则已污染的会话无法自愈。"""
    app = _P / "fengcode" / "server" / "static" / "app.js"
    s = io.open(app, encoding="utf-8").read()
    assert "luValid" in s, "applySessionUsage 必须有 luValid 判定"
    # ★ 判据已收敛到**唯一入口** usageSnapshotHasData（原先散在两处内联判断，
    #   改一处漏一处正是这条 bug 反复出现的原因）。
    assert "function usageSnapshotHasData(" in s, (
        "必须存在统一的用量快照判据函数 usageSnapshotHasData"
    )
    assert "usageSnapshotHasData(lu)" in s, "applySessionUsage 必须调用统一判据"
    assert "usageSnapshotHasData(luRaw)" in s, "usage 分支必须调用统一判据"
    # ★★ 反例形态不得回归：用「字段是否存在」判定（0 != null 为真 → 把真实读数覆盖成 0）
    import re
    bad = re.search(r"prompt_tokens\s*!=\s*null\s*&&", s)
    assert bad is None, (
        "检测到「字段存在性」判据 —— 全 0 快照里字段是存在的，会被误判成有数据"
    )


def test_unified_guard_semantics_match_backend():
    """前端统一判据的语义必须与后端 Usage.has_data() 一致（两端不同步即再现此 bug）。

    直接抽出 app.js 里那个函数体，用 Node 跑一遍真实语义。
    """
    import json
    import subprocess

    app = _P / "fengcode" / "server" / "static" / "app.js"
    s = io.open(app, encoding="utf-8").read()
    i = s.find("function usageSnapshotHasData(snap) {")
    assert i > 0, "找不到 usageSnapshotHasData"
    # 截到函数结束（第一个单独一行 "}" 处）
    j = s.find("\n}", i)
    fn_src = s[i:j + 2]

    script = fn_src + """
const cases = [
  [null, false], [{}, false],
  [{prompt_tokens:0,completion_tokens:0,total_tokens:0,cached_tokens:0}, false],
  [{prompt_tokens:13022}, true],
  [{completion_tokens:5}, true],
  [{total_tokens:100}, true],
  [{prompt_tokens:0,cached_tokens:100}, false]
];
const bad = [];
for (const [inp, want] of cases) {
  const got = usageSnapshotHasData(inp);
  if (got !== want) bad.push(JSON.stringify(inp) + ' => ' + got + ' (want ' + want + ')');
}
console.log(JSON.stringify(bad));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"node 执行失败: {out.stderr}"
    bad = json.loads(out.stdout.strip().splitlines()[-1])
    assert not bad, "前后端判据语义不一致：" + "；".join(bad)
