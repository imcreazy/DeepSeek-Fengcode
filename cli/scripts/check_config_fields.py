# -*- coding: utf-8 -*-
"""全面审查（后端）：前端提交的配置字段，后端 schema 是否都接得住。

用括号配平精确切出 `xxx: { ... }` 段，避免正则贪婪把后续段落的字段算进来。
"""
import io
import re
import sys

sys.path.insert(0, "src")

from fengcode.config.schema import (  # noqa: E402
    AgentConfig, LLMConfig, MemoryConfig, PermissionsConfig, SandboxConfig,
    ToolsConfig, UIConfig,
)

STATIC = "src/fengcode/server/static"
# 前端已拆成 index.html + app.js：配置字段都写在 app.js 里（老版本内联在 index.html）。
html = io.open(STATIC + "/app.js", encoding="utf-8").read()
if not html:
    html = io.open(STATIC + "/index.html", encoding="utf-8").read()

bad = 0
MAP = [
    ("llm", LLMConfig),
    ("agent", AgentConfig),
    ("permissions", PermissionsConfig),
    ("sandbox", SandboxConfig),
    ("tools", ToolsConfig),
    ("memory", MemoryConfig),
    ("ui", UIConfig),
]


def fields_of(cls):
    try:
        return set(cls.model_fields.keys())
    except AttributeError:
        return set(getattr(cls, "__fields__", {}).keys())


def extract_block(text, key):
    """从 `key: {` 开始做括号配平，返回花括号内的内容。"""
    m = re.search(r"\b" + re.escape(key) + r"\s*:\s*\{", text)
    if not m:
        return None
    start = m.end() - 1   # 指向 '{'
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return None


for section, cls in MAP:
    body = extract_block(html, section)
    if body is None:
        print(f"SKIP {section}: 前端没找到该段")
        continue
    # 只看顶层 key：跳过嵌套花括号内的内容
    depth = 0
    top_keys = []
    for line in body.splitlines():
        stripped = line.strip()
        before = depth
        depth += stripped.count("{") - stripped.count("}")
        if before == 0:
            km = re.match(r"([a-z_][a-z0-9_]*)\s*:", stripped)
            if km:
                top_keys.append(km.group(1))
    known = fields_of(cls)
    unknown = sorted(set(k for k in top_keys if k not in known))
    if unknown:
        bad += len(unknown)
        print(f"FAIL {section}: 前端提交了 schema 没有的字段 -> {unknown}")
    else:
        print(f"OK   {section}: 顶层字段全部被 schema 接受（{len(set(top_keys))} 个）")

# 额外：agent.subagents 子结构
sub = extract_block(html, "subagents:")
if sub is not None:
    depth = 0
    keys = []
    for line in sub.splitlines():
        stripped = line.strip()
        before = depth
        depth += stripped.count("{") - stripped.count("}")
        if before == 0:
            km = re.match(r"([a-z_][a-z0-9_]*)\s*:", stripped)
            if km:
                keys.append(km.group(1))
    try:
        sub_cls = type(AgentConfig.model_fields["subagents"].annotation)
        known = set()
        ann = AgentConfig.model_fields["subagents"].annotation
        for cand in (ann, getattr(ann, "__args__", (None,))[0] if hasattr(ann, "__args__") else None):
            if cand is not None and hasattr(cand, "model_fields"):
                known = set(cand.model_fields.keys())
                break
        if known:
            unknown = sorted(k for k in set(keys) if k not in known)
            if unknown:
                bad += len(unknown)
                print(f"FAIL agent.subagents: 前端提交了 schema 没有的字段 -> {unknown}")
            else:
                print(f"OK   agent.subagents: 字段全部被接受（{len(set(keys))} 个）")
        else:
            print(f"SKIP agent.subagents: 拿不到子 schema（前端提交 {sorted(set(keys))}）")
    except Exception as e:
        print(f"SKIP agent.subagents: {e}")

print()
print(f"FAILED: {bad}" if bad else "ALL PASS")
sys.exit(1 if bad else 0)
