"""工作流：把任务组织成 DAG 并执行。

节点类型
--------
- ``prompt``   : 让主 Agent 执行一段提示词（可引用上游输出 {{step_id}}）
- ``agent``    : 派生指定类型的子智能体
- ``tool``     : 直接调用某个工具
- ``shell``    : 执行 Shell 命令
- ``condition``: 按上游输出决定是否继续（简化：表达式命中则跳过后续依赖）
- ``merge``    : 汇总多个上游输出

边表示依赖；同层的节点并行执行。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..utils import new_id, truncate

_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)\s*\}\}")


@dataclass
class Step:
    """工作流节点。"""

    id: str
    name: str = ""
    kind: str = "prompt"
    config: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    when: str = ""          # 简化条件：上游输出的子串（空=总是执行）
    timeout: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "kind": self.kind,
            "config": self.config,
            "depends_on": list(self.depends_on),
            "when": self.when,
            "timeout": self.timeout,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Step":
        return Step(
            id=str(d.get("id") or new_id("s")),
            name=str(d.get("name") or ""),
            kind=str(d.get("kind") or "prompt"),
            config=dict(d.get("config") or {}),
            depends_on=[str(x) for x in (d.get("depends_on") or [])],
            when=str(d.get("when") or ""),
            timeout=float(d.get("timeout") or 0),
        )


@dataclass
class StepResult:
    """节点执行结果。"""

    step_id: str
    ok: bool = True
    output: str = ""
    error: str | None = None
    duration: float = 0.0
    skipped: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "duration": round(self.duration, 3),
            "skipped": self.skipped,
            "data": self.data,
        }


def validate(steps: list[Step]) -> tuple[bool, str]:
    """校验 DAG：节点 ID 唯一、依赖存在、无环。"""
    ids = [s.id for s in steps]
    if len(ids) != len(set(ids)):
        return False, "存在重复的节点 ID"
    id_set = set(ids)
    for s in steps:
        for d in s.depends_on:
            if d not in id_set:
                return False, f"节点 {s.id} 依赖了不存在的节点 {d}"
            if d == s.id:
                return False, f"节点 {s.id} 依赖了自己"
    # 环检测
    graph = {s.id: set(s.depends_on) for s in steps}
    temp: set[str] = set()
    done: set[str] = set()
    cycle: list[str] = []

    def visit(n: str, path: list[str]) -> bool:
        if n in done:
            return True
        if n in temp:
            cycle.extend(path[path.index(n):] + [n] if n in path else [n])
            return False
        temp.add(n)
        path.append(n)
        for d in graph.get(n, ()):
            if not visit(d, path):
                return False
        path.pop()
        temp.discard(n)
        done.add(n)
        return True

    for n in graph:
        if not visit(n, []):
            return False, "存在循环依赖：" + " → ".join(cycle)
    return True, ""


def topo_layers(steps: list[Step]) -> list[list[Step]]:
    """按依赖分层，同层可并行。"""
    by_id = {s.id: s for s in steps}
    remaining = {s.id: set(s.depends_on) for s in steps}
    layers: list[list[Step]] = []
    placed: set[str] = set()
    while remaining:
        layer_ids = [sid for sid, deps in remaining.items() if not (deps - placed)]
        if not layer_ids:
            break  # 有环，交给 validate 报错
        layers.append([by_id[i] for i in layer_ids])
        for i in layer_ids:
            placed.add(i)
            remaining.pop(i, None)
    return layers


def render_template(text: str, outputs: dict[str, str]) -> str:
    """把 ``{{step_id}}`` 替换成该步骤的输出。"""
    if not text:
        return text

    def sub(m: re.Match) -> str:
        key = m.group(1)
        return outputs.get(key, m.group(0))

    return _TEMPLATE_RE.sub(sub, text)


class WorkflowRunner:
    """执行工作流。"""

    def __init__(self, *, agent_runner: Any = None, tool_registry: Any = None, ctx: Any = None,
                 bus: Any = None, subagent_runner: Any = None) -> None:
        self.agent_runner = agent_runner
        self.tool_registry = tool_registry
        self.ctx = ctx
        self.bus = bus
        self.subagent_runner = subagent_runner

    def _emit(self, type: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            try:
                self.bus.emit(type, data)
            except Exception:
                pass

    async def run(self, steps: list[Step], *, inputs: dict[str, str] | None = None,
                  workflow_id: str = "") -> dict[str, Any]:
        """按层执行工作流，返回汇总结果。"""
        ok, err = validate(steps)
        if not ok:
            return {"ok": False, "error": err, "results": {}}
        outputs: dict[str, str] = dict(inputs or {})
        results: dict[str, StepResult] = {}
        layers = topo_layers(steps)
        t0 = time.time()
        self._emit("workflow.start", {"workflow_id": workflow_id, "steps": len(steps), "layers": len(layers)})

        for li, layer in enumerate(layers, 1):
            coros = [self._run_step(s, outputs) for s in layer]
            layer_results = await asyncio.gather(*coros, return_exceptions=True)
            for step, res in zip(layer, layer_results):
                if isinstance(res, Exception):
                    res = StepResult(step_id=step.id, ok=False, error=f"{type(res).__name__}: {res}")
                results[step.id] = res
                outputs[step.id] = res.output
                self._emit(
                    "workflow.step",
                    {
                        "workflow_id": workflow_id,
                        "layer": li,
                        "step_id": step.id,
                        "name": step.name or step.id,
                        "ok": res.ok,
                        "skipped": res.skipped,
                        "error": res.error,
                        "duration": res.duration,
                        "preview": truncate(res.output, 400),
                    },
                )

        failed = [r.step_id for r in results.values() if not r.ok and not r.skipped]
        total = time.time() - t0
        self._emit(
            "workflow.end",
            {"workflow_id": workflow_id, "ok": not failed, "failed": failed, "duration": total},
        )
        summary = self._summarize(steps, results)
        return {
            "ok": not failed,
            "failed": failed,
            "duration": total,
            "results": {k: v.to_dict() for k, v in results.items()},
            "summary": summary,
            "outputs": outputs,
        }

    async def _run_step(self, step: Step, outputs: dict[str, str]) -> StepResult:
        t0 = time.time()
        # 条件跳过
        if step.when:
            cond = render_template(step.when, outputs)
            upstream = "\n".join(outputs.get(d, "") for d in step.depends_on)
            if cond and cond not in upstream:
                return StepResult(
                    step_id=step.id, ok=True, skipped=True,
                    output="", duration=time.time() - t0,
                    error=None,
                )
        try:
            coro = self._dispatch(step, outputs)
            out = await asyncio.wait_for(coro, timeout=step.timeout) if step.timeout else await coro
            if isinstance(out, StepResult):
                out.duration = time.time() - t0
                return out
            return StepResult(step_id=step.id, ok=True, output=str(out or ""), duration=time.time() - t0)
        except asyncio.TimeoutError:
            return StepResult(step_id=step.id, ok=False, error=f"步骤超时（{step.timeout}s）",
                              duration=time.time() - t0)
        except Exception as e:
            return StepResult(step_id=step.id, ok=False, error=f"{type(e).__name__}: {e}",
                              duration=time.time() - t0)

    async def _dispatch(self, step: Step, outputs: dict[str, str]) -> Any:
        cfg = {k: render_template(str(v), outputs) if isinstance(v, str) else v
               for k, v in (step.config or {}).items()}
        kind = step.kind

        if kind == "shell":
            cmd = cfg.get("command") or cfg.get("cmd") or ""
            if not cmd:
                raise ValueError("shell 节点需要 config.command")
            sandbox = getattr(self.ctx, "sandbox", None)
            if sandbox is None:
                raise ValueError("沙箱不可用")
            res = await sandbox.shell(cmd, cwd=getattr(self.ctx, "workspace", None))
            return res.stdout if res.ok else f"（失败，退出码 {res.returncode}）\n{res.stdout}\n{res.stderr}"

        if kind == "tool":
            name = cfg.get("tool") or cfg.get("name") or ""
            if not name or self.tool_registry is None:
                raise ValueError("tool 节点需要 config.tool，且工具注册表可用")
            args = cfg.get("arguments") or cfg.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            res = await self.tool_registry.execute(name, args, self.ctx)
            return res.content if res.ok else f"（工具失败：{res.error}）\n{res.content}"

        if kind == "agent":
            if self.subagent_runner is None:
                raise ValueError("子智能体不可用")
            prompt = cfg.get("prompt") or cfg.get("task") or ""
            atype = cfg.get("agent_type") or "general"
            res = await self.subagent_runner.run(prompt, agent_type=atype)
            if isinstance(res, dict):
                return res.get("content") or res.get("result") or ""
            return str(res)

        if kind == "merge":
            parts = []
            for d in step.depends_on:
                if outputs.get(d):
                    parts.append(f"### {d}\n{outputs[d]}")
            header = cfg.get("header") or "汇总"
            return f"## {header}\n\n" + "\n\n".join(parts)

        if kind == "condition":
            expr = cfg.get("contains") or cfg.get("expr") or ""
            upstream = "\n".join(outputs.get(d, "") for d in step.depends_on)
            hit = expr in upstream if expr else False
            return f"条件 {'命中' if hit else '未命中'}：{expr}"

        # 默认：prompt
        prompt = cfg.get("prompt") or cfg.get("text") or cfg.get("task") or ""
        if not prompt:
            raise ValueError("prompt 节点需要 config.prompt")
        if self.agent_runner is None:
            raise ValueError("主 Agent 不可用")
        res = self.agent_runner(prompt)
        if asyncio.iscoroutine(res):
            res = await res
        return res if isinstance(res, str) else str(res)

    @staticmethod
    def _summarize(steps: list[Step], results: dict[str, StepResult]) -> str:
        lines = ["工作流执行结果："]
        for s in steps:
            r = results.get(s.id)
            if r is None:
                lines.append(f"· {s.name or s.id}：未执行")
                continue
            if r.skipped:
                lines.append(f"· {s.name or s.id}：已跳过（条件不满足）")
            elif r.ok:
                lines.append(f"· {s.name or s.id}：✓ 完成（{r.duration:.1f}s）")
            else:
                lines.append(f"· {s.name or s.id}：✗ 失败 — {r.error}")
        return "\n".join(lines)


def from_dict(dag: dict[str, Any]) -> list[Step]:
    """从 dict 构造步骤列表。"""
    raw = dag.get("steps") if isinstance(dag, dict) else None
    if raw is None and isinstance(dag, list):
        raw = dag
    return [Step.from_dict(s) for s in (raw or []) if isinstance(s, dict)]


def sample_dag() -> dict[str, Any]:
    """示例工作流（供界面"新建"时预填）。"""
    return {
        "name": "代码改动验证流程",
        "description": "先探索 → 并行做实现与测试计划 → 运行测试 → 汇总",
        "steps": [
            {"id": "explore", "name": "探索代码库", "kind": "agent",
             "config": {"agent_type": "explore", "prompt": "了解项目结构与关键文件"}},
            {"id": "impl", "name": "实现改动", "kind": "prompt",
             "config": {"prompt": "根据以下探索结论实现改动：\n{{explore}}"},
             "depends_on": ["explore"]},
            {"id": "test_plan", "name": "制定测试计划", "kind": "prompt",
             "config": {"prompt": "针对以下探索结论，列出应该运行的测试：\n{{explore}}"},
             "depends_on": ["explore"]},
            {"id": "run_tests", "name": "运行测试", "kind": "tool",
             "config": {"tool": "run_tests", "arguments": {"path": "."}},
             "depends_on": ["impl"]},
            {"id": "report", "name": "汇总报告", "kind": "merge",
             "config": {"header": "验证报告"},
             "depends_on": ["run_tests", "test_plan"]},
        ],
    }


__all__ = [
    "Step",
    "StepResult",
    "WorkflowRunner",
    "validate",
    "topo_layers",
    "render_template",
    "from_dict",
    "sample_dag",
]
