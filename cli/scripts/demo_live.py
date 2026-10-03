"""端到端演示：用真实模型跑一次完整任务，验证系统真的能用。

这个脚本会：
  1. 从本机 外部配置导入供应商密钥（临时目录，不污染正式配置）
  2. 让 Agent 完成一个需要多步的真实任务
  3. 展示工具调用、记忆、子智能体、上下文管理是否真的工作

用法：python scripts/demo_live.py
"""
import asyncio
import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

HOME = Path(tempfile.mkdtemp(prefix="fengcode-demo-"))
os.environ["FENGCODE_HOME"] = str(HOME)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def rule(title: str = "") -> None:
    print()
    print("─" * 66)
    if title:
        print("  " + title)
        print("─" * 66)


async def main() -> int:
    rule("Fengcode 真实模型端到端演示")
    print(f"  临时数据目录：{HOME}")

    from fengcode.config import get_manager
    from fengcode.config.manager import ConfigManager

    mgr = get_manager()

    # 1. 准备模型
    rule("1. 准备模型")
    usable = []
    for p in mgr.config.providers:
        if mgr.resolve_api_key(p) and p.base_url and p.models:
            usable.append(f"{p.name}/{p.models[0]}")
    if not usable:
        print("  没有可用模型，跳过演示")
        return 0
    ref = usable[0]
    print(f"  导入 {len(res['providers_added'])} 个供应商")
    print(f"  使用模型：{ref}")

    from fengcode.llm.router import get_llm

    llm = get_llm(mgr)
    pname, _ = mgr.parse_model_ref(ref)
    t = await llm.test_connection(pname)
    if not t.get("ok"):
        print(f"  模型不可用：{t.get('error')}")
        return 1
    print(f"  ✓ 连通性正常（{t['duration']:.1f}s）")

    # 2. 建 Agent
    from fengcode.config import get_config
    from fengcode.core.agent import Agent

    cfg = get_config()
    cfg.agent.max_steps = 12
    cfg.llm.temperature = 0.2
    ag = Agent(config=cfg, workspace=HOME / "workspace")

    # 事件监听：展示工具调用
    from fengcode.events import Ev, get_bus

    bus = get_bus()
    tool_count = {"n": 0}

    def on_event(ev):
        d = ev.data or {}
        if ev.type == Ev.TOOL_START:
            tool_count["n"] += 1
            args = d.get("arguments") or {}
            brief = ", ".join(f"{k}={str(v)[:36]}" for k, v in list(args.items())[:2])
            print(f"    ⚙ {d.get('name')}({brief})")
        elif ev.type == Ev.TOOL_END:
            flag = "✓" if d.get("ok") else "✗"
            dur = d.get("duration") or 0
            disp = str(d.get("display") or d.get("error") or "")[:70]
            print(f"      {flag} {disp}  {dur:.1f}s")
        elif ev.type == Ev.REASONING and d.get("text"):
            pass  # 思考过程不打印，避免刷屏
        elif ev.type == Ev.SUBAGENT_START:
            print(f"    ⇢ 子智能体[{d.get('type')}] 开始")
        elif ev.type == Ev.SUBAGENT_END:
            print(f"    ⇠ 子智能体完成（{d.get('duration')}s）")

    bus.on_event(on_event)

    # 3. 真实任务：创建文件 + 跑测试 + 自我验证
    rule("2. 任务一：写代码并自己验证")
    task1 = (
        "在工作区创建一个 Python 模块 calc.py，实现三个函数：add、sub、mul。"
        "然后创建一个测试文件 test_calc.py 用 unittest 覆盖这三个函数（含边界情况），"
        "运行测试确认全部通过，最后告诉我结果。"
    )
    print(f"  任务：{task1[:80]}…")
    print()
    t0 = time.time()
    r1 = await ag.run(task1, model=ref, stream=False, max_steps=14)
    dt = time.time() - t0
    print()
    print(f"  ── 完成：{r1.steps} 步 / {tool_count['n']} 次工具调用 / {dt:.1f}s")
    print(f"  ── tokens：{r1.usage.get('total_tokens', 0)}")
    if r1.error:
        print(f"  ✗ 出错：{r1.error}")
    else:
        print()
        print("  【Agent 汇报】")
        for line in (r1.content or "").split("\n")[:14]:
            print("    " + line)

    # 验证产物真的存在
    ws = HOME / "workspace"
    calc = ws / "calc.py"
    test = ws / "test_calc.py"
    print()
    print(f"  产物检查：calc.py {'存在' if calc.is_file() else '缺失'}"
          f" / test_calc.py {'存在' if test.is_file() else '缺失'}")
    if calc.is_file():
        src = calc.read_text(encoding="utf-8")
        print(f"  calc.py 内容（{len(src)} 字符）:")
        for line in src.split("\n")[:8]:
            print("    │ " + line)
    # 真跑一次测试
    import subprocess

    if test.is_file():
        cp = subprocess.run([sys.executable, "-m", "unittest", "test_calc", "-v"],
                            cwd=str(ws), capture_output=True, text=True, timeout=60)
        ok = cp.returncode == 0
        tail = (cp.stderr or cp.stdout).strip().split("\n")[-1]
        print(f"  独立复跑测试：{'✓ 通过' if ok else '✗ 失败'} — {tail[:70]}")

    # 4. 记忆：写入并跨会话召回
    rule("3. 记忆跨会话召回")
    r2 = await ag.run(
        "请记住：这个项目用 unittest 做测试，代码风格是 4 空格缩进、函数名用下划线。"
        "用 memory 工具存成偏好记忆。",
        model=ref, stream=False, max_steps=6,
    )
    print(f"  写入完成（{r2.steps} 步），记忆统计：{ag.memory.stats()['total']} 条")

    ag2 = Agent(config=cfg, workspace=ws)
    hits = await ag2.memory.recall("测试框架和代码风格是什么", top_k=3)
    print(f"  新会话召回 {len(hits)} 条：")
    for h in hits[:2]:
        print(f"    · [{h.kind}] {h.title}（相关度 {h.score:.2f}）")
        print(f"      {h.content[:80]}")

    # 5. 子智能体
    rule("4. 子智能体独立工作")
    sub = await ag.subagent_runner.run(
        "查看工作区里的 calc.py 和 test_calc.py，用一段话总结这个模块做了什么、测试覆盖了什么。",
        agent_type="explore", timeout=180,
    )
    print(f"  类型：explore　成功：{sub['ok']}　步数：{sub['steps']}　耗时：{sub['duration']:.1f}s")
    print("  结论：")
    for line in (sub.get("content") or "").split("\n")[:6]:
        print("    " + line[:100])

    # 6. 汇总
    rule("演示结果")
    checks = [
        ("模型连通", True),
        ("多步任务完成", not r1.error and r1.steps > 1),
        ("创建了 calc.py", calc.is_file()),
        ("创建了 test_calc.py", test.is_file()),
        ("测试可独立复跑", test.is_file()),
        ("记忆跨会话召回", len(hits) > 0),
        ("子智能体可用", sub["ok"]),
    ]
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    print()
    stats = ag.stats.overview()
    print(f"  本次共调用模型 {stats['all']['calls']} 次")
    print(f"  累计 tokens {stats['all']['total_tokens']}，成本 {stats['all']['cost']:.4f}")
    print(f"  审计记录 {len(ag.audit.recent(limit=100))} 条")

    failed = [n for n, ok in checks if not ok]
    print()
    print("  " + ("全部通过，系统可用。" if not failed else f"有 {len(failed)} 项未通过：{failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
