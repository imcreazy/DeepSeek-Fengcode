"""对话摘要与上下文压缩。

策略
----
- 超过阈值时，把"中间段"消息交给模型压缩成结构化摘要（保留事实/决策/待办）
- 摘要写回会话（作为一条 system 消息），并抽取关键事实进长期记忆
- 失败时退化为"截断 + 关键行保留"，保证不阻塞主流程
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..llm.types import Message
from ..utils import estimate_tokens, truncate

SUMMARY_PROMPT = """你是一个对话压缩器。请把下面这段对话历史压缩成简洁的结构化摘要。

要求：
1. 只保留对后续对话有用的信息：用户目标、已达成的结论、重要事实/数据、做出的决定、未完成事项、关键文件路径与命令。
2. 丢弃寒暄、重复、失败的尝试细节（除非失败原因重要）。
3. 用中文，用 Markdown 分节：【用户目标】【已完成】【关键结论】【重要事实】【未完成】【关键路径与命令】。
4. 总字数不超过 {max_words} 字。不要添加原文没有的信息。

对话历史：
{history}

请直接输出摘要正文，不要任何前言。"""


def render_messages(messages: list[Message], *, max_chars: int = 60_000) -> str:
    """把消息列表渲染成便于压缩的文本。"""
    lines: list[str] = []
    for m in messages:
        role = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}.get(m.role, m.role)
        if m.role == "tool":
            lines.append(f"[工具 {m.tool_name or ''}] {truncate(m.content, 800)}")
            continue
        head = f"[{role}]"
        if m.tool_calls:
            calls = ", ".join(f"{c.name}({_short_args(c.arguments)})" for c in m.tool_calls)
            head += f" 调用工具：{calls}"
        body = truncate(m.content or "", 2000)
        if m.reasoning:
            body = f"（思考：{truncate(m.reasoning, 400)}）\n{body}"
        lines.append(f"{head}\n{body}")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars // 2] + "\n…（中间省略）…\n" + text[-max_chars // 2 :]
    return text


def _short_args(args: dict | None) -> str:
    if not args:
        return ""
    parts = []
    for k, v in list(args.items())[:4]:
        sv = str(v).replace("\n", " ")[:60]
        parts.append(f"{k}={sv}")
    return ", ".join(parts)


def split_for_compaction(
    messages: list[Message],
    *,
    keep_recent: int = 6,
    keep_first: int = 1,
) -> tuple[list[Message], list[Message], list[Message]]:
    """把消息切成 ``(保留的开头段, 待压缩段, 最近保留段)``。

    ``keep_first``：会话最开头的消息（系统提示 + 用户最初的诉求）
    是本次任务的"锚点"，压缩掉会让模型忘记原始目标，必须原样保留。
    """
    if not messages:
        return [], [], []

    # 开头段：优先保留系统消息；若不足 keep_first 则继续往后取普通消息
    head_count = max(0, int(keep_first))
    systems: list[Message] = []
    i = 0
    while i < len(messages) and len(systems) < head_count:
        m = messages[i]
        if m.role == "system":
            systems.append(m)
            i += 1
            continue
        # 第一条非系统消息也算锚点（通常是用户最初的任务描述）
        if not systems:
            systems.append(m)
            i += 1
            continue
        break
    # 若系统提示之外还有配额，把紧随其后的第一条用户消息也纳入锚点
    if len(systems) < head_count + 1 and i < len(messages) and messages[i].role == "user":
        systems.append(messages[i])
        i += 1

    rest = list(messages[i:])
    if len(rest) <= keep_recent:
        return systems, [], rest
    # 不要把 tool 消息与其对应的 assistant 调用拆开
    cut = len(rest) - keep_recent
    while cut > 0 and rest[cut].role == "tool":
        cut -= 1
    return systems, rest[:cut], rest[cut:]


def fallback_summary(messages: list[Message], *, max_chars: int = 3000) -> str:
    """无模型可用时的兜底摘要：抽取关键行。"""
    keep: list[str] = []
    for m in messages:
        if m.role == "user":
            keep.append(f"· 用户提到：{truncate(m.content, 200)}")
        elif m.role == "assistant" and m.content:
            first = m.content.strip().split("\n")[0]
            if len(first) > 10:
                keep.append(f"· 助手结论：{truncate(first, 180)}")
        elif m.role == "tool" and len(keep) < 12:
            keep.append(f"· 调用 {m.tool_name or '工具'}：{truncate(m.content, 120)}")
    text = "\n".join(keep[:40])
    return f"【自动摘要（未使用模型）】\n{truncate(text, max_chars)}"


async def summarize(
    messages: list[Message],
    llm: Any,
    *,
    model: str | None = None,
    max_words: int = 800,
    timeout: float = 120.0,
    prefix: list[Message] | None = None,
    tools: list[Any] | None = None,
) -> dict[str, Any]:
    """调用模型生成摘要。返回 ``{text, usage, ok, error}``。

    ★ ``prefix`` / ``tools``：让这次摘要调用**复用主对话的缓存前缀**。
      摘要调用本身也要花一次模型费用。若把「主对话的 system 提示 + 工具定义」
      原样带上，再在其后追加「待压缩的历史 + 压缩指令」，这次请求就成了主对话
      请求的一个**真前缀**，可以直接命中上游的前缀缓存 —— 只有末尾的压缩指令
      是全新输入。不带前缀则整段都要按未命中计价（实测差异可达数倍）。

      为什么工具定义也要带：它在请求体的前缀里，漏掉就与主对话的字节对不上，
      缓存自然无从命中。
    """
    history = render_messages(messages)
    if not history.strip():
        return {"text": "", "ok": True, "usage": None, "error": None}
    prompt = SUMMARY_PROMPT.format(max_words=max_words, history=history)
    if llm is None:
        return {"text": fallback_summary(messages), "ok": False, "usage": None,
                "error": "未配置模型，使用兜底摘要"}
    # ★ 组装顺序即缓存顺序：主对话前缀（system + 工具）在前，新内容在最后。
    head: list[Message] = list(prefix or [])
    try:
        resp = await llm.chat(
            [*head, Message.system("你是一个精确的信息压缩器。"), Message.user(prompt)],
            model=model,
            tools=tools or None,
            max_tokens=max(512, min(4096, max_words * 3)),
            temperature=0.1,
        )
        if resp.error or not (resp.content or "").strip():
            return {"text": fallback_summary(messages), "ok": False, "usage": resp.usage,
                    "error": resp.error or "模型返回空摘要"}
        return {"text": resp.content.strip(), "ok": True, "usage": resp.usage, "error": None}
    except Exception as e:
        return {"text": fallback_summary(messages), "ok": False, "usage": None,
                "error": f"{type(e).__name__}: {e}"}


def split_by_tokens(
    messages: list[Message],
    *,
    chunk_tokens: int = 24_000,
    overlap: int = 2,
) -> list[list[Message]]:
    """按估算 token 把消息切成若干块（★ 2-B）。

    `overlap` 是相邻块之间的**重叠条数**：边界处常有「助手说要调用 X / 工具返回 Y」
    这种成对信息，硬切会把因果关系切断（只留半句），重叠两条能保住。
    """
    chunks: list[list[Message]] = []
    cur: list[Message] = []
    cur_tok = 0
    for m in messages:
        t = estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "")
        if cur and cur_tok + t > chunk_tokens:
            chunks.append(cur)
            cur = cur[-overlap:] if overlap > 0 else []
            cur_tok = sum(
                estimate_tokens(x.content or "") + estimate_tokens(x.reasoning or "") for x in cur
            )
        cur.append(m)
        cur_tok += t
    if cur:
        chunks.append(cur)
    return chunks


async def summarize_chunked(
    messages: list[Message],
    llm: Any,
    *,
    model: str | None = None,
    max_words: int = 800,
    chunk_tokens: int = 24_000,
    overlap: int = 2,
    fanout: int = 4,
    prefix: list[Message] | None = None,
    tools: list[Any] | None = None,
) -> dict[str, Any]:
    """分块重放 + **树形归并**的压缩。

    要解决的问题：早前是「把整个中段塞进一次摘要调用」。历史一长，那一次调用
    本身就可能超上下文、或者慢到超时 —— 于是**整段压缩失败**，历史只能原样留着。

    现在的做法：
      1. 按 token 分块（带重叠），**每块单独摘要** —— 单块失败只影响那一块；
      2. 块摘要若还有多个，按 fanout 多路**逐层归并**（树），直到只剩一条。

    ★ ``prefix`` / ``tools`` 透传给每次摘要调用，用于**复用主对话的缓存前缀**
      （含义见 ``summarize``）。首块摘要带上前缀最划算 —— 它就是主对话的真前缀；
      后续块与归并轮次同样带上，多一层命中机会，不命中也没有额外损失。

    返回结构与 `summarize` 一致：``{text, ok, usage, error}``。
    """
    if not messages:
        return {"text": "", "ok": True, "usage": None, "error": None}
    total = sum(
        estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "") for m in messages
    )
    # 小到一次能装下 → 直接用原有单次摘要，别把简单事复杂化
    if total <= chunk_tokens:
        return await summarize(messages, llm, model=model, max_words=max_words,
                               prefix=prefix, tools=tools)

    chunks = split_by_tokens(messages, chunk_tokens=chunk_tokens, overlap=overlap)
    parts: list[str] = []
    degraded = False
    for i, ch in enumerate(chunks, 1):
        r = await summarize(ch, llm, model=model, max_words=max(200, max_words // 2),
                            prefix=prefix, tools=tools)
        if not r.get("ok"):
            degraded = True
        txt = (r.get("text") or "").strip()
        if txt:
            parts.append(f"[第 {i}/{len(chunks)} 段]\n{txt}")

    # 树形归并：多路合并，每轮把块数缩到 1/fanout
    level = parts
    rounds = 0
    while len(level) > 1 and rounds < 6:
        rounds += 1
        merged: list[str] = []
        for i in range(0, len(level), fanout):
            group = level[i : i + fanout]
            if len(group) == 1:
                merged.append(group[0])
                continue
            joined = "\n\n".join(group)
            r = await summarize(
                [Message.user(f"以下是同一段历史的分块摘要，请合并成一条完整摘要：\n\n{joined}")],
                llm,
                model=model,
                max_words=max(300, max_words),
                prefix=prefix, tools=tools,
            )
            if not r.get("ok"):
                degraded = True
            merged.append((r.get("text") or joined).strip())
        level = merged

    text = level[0] if level else ""
    if not text.strip():
        return {"text": fallback_summary(messages), "ok": False, "usage": None,
                "error": "分块摘要全部为空，使用兜底摘要"}
    return {
        "text": text,
        "ok": not degraded,
        "usage": None,
        "error": None if not degraded else "部分分块摘要降级（已尽力保留其余内容）",
    }


FACT_PATTERNS = [
    re.compile(r"(?:记住|注意|切记|请记住|别忘了)[：:，, ]?(.{4,120})"),
    re.compile(r"(?:我(?:的)?(?:偏好|习惯|要求|规则)是)[：:，, ]?(.{4,120})"),
    re.compile(r"(?:以后|下次)(?:都|请|要)(.{4,120})"),
]


def extract_facts(text: str, *, limit: int = 5) -> list[str]:
    """从文本中抽取"值得记住"的句子（用于自动记忆）。"""
    out: list[str] = []
    for pat in FACT_PATTERNS:
        for m in pat.finditer(text or ""):
            v = " ".join(m.group(1).split())
            v = v.strip("。，,.;;！!？?")
            if 4 <= len(v) <= 160 and v not in out:
                out.append(v)
            if len(out) >= limit:
                return out
    # 兜底：用户的祈使句
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith(("请", "帮我", "记住")) and 6 <= len(s) <= 120 and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def should_compact(messages: list[Message], *, context_window: int, ratio: float = 0.75,
                   reserve_output: int = 8192) -> tuple[bool, int]:
    """判断是否需要压缩，返回 ``(是否需要, 当前估算 token)``。"""
    total = sum(estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "") for m in messages)
    limit = int(context_window * ratio) - reserve_output
    return total > max(2048, limit), total


# --------------------------------------------------------------------------
# 压缩策略（分块摘要 + 观测遮罩）：补强
# --------------------------------------------------------------------------

# 触发类型：SOFT 可推迟，HARD 必须立刻处理（否则请求会超窗报错）
TRIGGER_SOFT = "soft"
TRIGGER_HARD = "hard"


def detect_trigger(
    messages: list[Message],
    *,
    context_window: int,
    ratio: float = 0.75,
    hard_ratio: float = 0.92,
    reserve_output: int = 8192,
) -> tuple[str | None, int]:
    """判断压缩触发类型。

    - ``hard``：已经接近上下文上限，**必须**立刻压缩，否则下一次请求会失败
    - ``soft``：超过软阈值，例行整理（可推迟到消息结构不被打断的时机）
    - ``None``：不需要压缩

    HARD/SOFT 区分：硬触发源于资源上限，软触发是例行维护。
    分开的价值在于——软触发可以等一个更合适的时机（比如工具调用结束后），
    避免在 assistant/tool 消息对中间切断导致请求非法。
    """
    total = sum(
        estimate_tokens(m.content or "") + estimate_tokens(m.reasoning or "")
        for m in messages
    )
    soft_limit = max(2048, int(context_window * ratio) - reserve_output)
    hard_limit = max(3072, int(context_window * hard_ratio) - reserve_output)
    if total >= hard_limit:
        return TRIGGER_HARD, total
    if total > soft_limit:
        return TRIGGER_SOFT, total
    return None, total


def mask_observations(
    messages: list[Message],
    *,
    keep_recent: int = 8,
    max_chars: int = 600,
) -> list[Message]:
    """观测遮罩：保留工具调用的"骨架"，把旧的冗长工具输出替换为占位摘要。

    ObservationMaskingCondenser。好处是**不需要额外调用模型**，
    却能省下大量 token：模型仍能看到"调用过什么工具、成功还是失败"的脉络，
    只是看不到旧的原始输出。

    只对**较旧**的工具消息生效，最近若干条保持原样。
    """
    if not messages:
        return messages
    # 找出所有工具消息的位置
    tool_idx = [i for i, m in enumerate(messages) if m.role == "tool"]
    if len(tool_idx) <= keep_recent:
        return messages
    to_mask = set(tool_idx[:-keep_recent])
    out: list[Message] = []
    for i, m in enumerate(messages):
        if i not in to_mask:
            out.append(m)
            continue
        body = m.content or ""
        if len(body) <= max_chars:
            out.append(m)
            continue
        # 保留开头（通常是最关键的结果/报错）与极简提示
        head = body[: max(0, max_chars - 80)].rstrip()
        note = f"\n…（输出共 {len(body)} 字符，已折叠；如需重看请重新调用该工具）"
        out.append(
            Message(
                role="tool",
                content=head + note,
                tool_call_id=m.tool_call_id,
                tool_name=m.tool_name,
                meta={**(m.meta or {}), "masked": True, "original_chars": len(body)},
            )
        )
    return out


def should_proceed(
    before_tokens: int, after_tokens: int, *, minimum_progress: float = 0.1
) -> tuple[bool, float]:
    """压缩收益检查：缩减不足 ``minimum_progress`` 就认为不值得。

    ``minimum_progress``（默认 0.1）。若压缩只减少 3% 的 token，
    那这次压缩的模型调用成本可能高于它省下的钱，而且会破坏 prompt 缓存——
    应当放弃并用其它手段（遮罩、截断工具输出）来处理。

    返回 ``(是否值得, 实际缩减比例)``。
    """
    if before_tokens <= 0:
        return False, 0.0
    progress = 1.0 - (after_tokens / before_tokens)
    return progress >= minimum_progress, progress


def summary_is_smaller(
    summary_tokens: int, shadowed_tokens: int
) -> tuple[bool, str]:
    """★ 摘要净收益校验：摘要本身必须**比被压掉的内容小**。

    为什么单靠 `should_proceed` 不够：那是拿「整段压缩前后的总账」比，
    只保证总体变小。但完全可能出现——总账变小了，而**摘要这一块本身**
    比它顶替掉的内容还长（摘要写得啰嗦、或原内容本来就不长）。
    那种情况下这次摘要调用纯属白花钱，还把信息换成了更长的一段文字。

    这里把「摘要」与「被它顶替的内容」单独比一次，把这种情形挡在替换之前。

    返回 ``(是否通过, 说明)``。
    """
    if shadowed_tokens <= 0:
        # 没有可比较的基数（内容为空）→ 无从判断，不拦。
        return True, ""
    if summary_tokens >= shadowed_tokens:
        return False, (
            f"摘要未变短（摘要约 {summary_tokens} tokens ≥ 被压内容约 {shadowed_tokens} tokens）"
        )
    return True, ""


# ---- 结构化压缩：丢弃「已被取代的状态块」---------------------------------
# ★ 要解决什么：长会话里同一份状态会被反复重述 ——
#   比如「待办清单」「当前计划」「某文件的内容」在每一轮都完整贴一遍。
#   这些块**后出现的取代先出现的**，早期副本纯属占位，却和最新副本一样占 token。
#   把它们丢掉，压缩率明显提升，而且不丢信息（最新那份还在）。
#
# 判据（保守，宁可少丢）：
#   · 同一「块标题」在待压缩段里出现 ≥2 次 → 只保留最后一次；
#   · 只处理明确带标题的块（<todos> / 【待办】 / ## 某个固定小标题），
#     不对自由正文做任何「相似即丢弃」的猜测 —— 那会误伤。
_STATE_BLOCK_RES = [
    re.compile(r"<todos>[\s\S]*?</todos>", re.I),
    re.compile(r"<goal>[\s\S]*?</goal>", re.I),
    re.compile(r"【待办】[\s\S]*?(?=\n\n|$)"),
    re.compile(r"【当前计划】[\s\S]*?(?=\n\n|$)"),
    re.compile(r"【执行轨迹】[\s\S]*?(?=\n\n|$)"),
]


def drop_superseded_blocks(messages: list[Message]) -> list[Message]:
    """丢掉「被后续同款块取代」的旧状态块，只保留每个标题的最后一次出现。

    只对带明确标题的结构化块生效（见 ``_STATE_BLOCK_RES``）；自由正文不动。
    返回新的消息列表；没有可丢的就原样返回。
    """
    if not messages:
        return messages
    # 先统计每种块在全文里的最后出现位置
    last_at: dict[int, int] = {}
    for i, m in enumerate(messages):
        body = m.content or ""
        for bi, pat in enumerate(_STATE_BLOCK_RES):
            if pat.search(body):
                last_at[bi] = i
    if not last_at:
        return messages
    out: list[Message] = []
    changed = False
    for i, m in enumerate(messages):
        body = m.content or ""
        new_body = body
        for bi, pat in enumerate(_STATE_BLOCK_RES):
            if last_at.get(bi) == i:
                continue          # 这是该块的最后一次出现，保留
            # 该块在更后面还会出现 → 这里的是旧副本，删掉
            stripped = pat.sub("", new_body).strip()
            if stripped != new_body:
                new_body = stripped
                changed = True
        if not new_body.strip():
            # 整条消息只有旧状态块 → 这条可以整条丢掉
            changed = True
            continue
        out.append(m if new_body == body else Message(
            role=m.role, content=new_body, reasoning=m.reasoning,
            tool_calls=m.tool_calls, tool_call_id=m.tool_call_id,
            tool_name=m.tool_name, attachments=m.attachments,
            meta={**(m.meta or {}), "superseded_trimmed": True},
        ))
    return out if changed else messages


def render_skeleton(messages: list[Message], *, max_items: int = 120) -> str:
    """渲染"执行骨架"：只保留谁调用了什么工具、结果成败，不保留输出正文。

    用于压缩后生成的脉络摘要，让模型知道"之前做过哪些尝试"，
    避免它重复已经失败过的路径。
    """
    lines: list[str] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            names = "、".join(c.name for c in m.tool_calls)
            lines.append(f"· 调用工具：{names}")
        elif m.role == "tool":
            body = (m.content or "").strip()
            failed = body.startswith("【错误】") or "错误" in body[:20]
            flag = "失败" if failed else "成功"
            brief = truncate(body.replace("\n", " "), 100)
            lines.append(f"  - {m.tool_name or '工具'}：{flag}（{brief}）")
        if len(lines) >= max_items:
            lines.append("…（更早的记录已省略）")
            break
    return "\n".join(lines)


__all__ = [
    "summarize",
    "summarize_chunked",
    "split_by_tokens",
    "should_compact",
    "should_proceed",
    "summary_is_smaller",
    "detect_trigger",
    "mask_observations",
    "drop_superseded_blocks",
    "render_skeleton",
    "split_for_compaction",
    "render_messages",
    "fallback_summary",
    "extract_facts",
    "SUMMARY_PROMPT",
    "TRIGGER_SOFT",
    "TRIGGER_HARD",
]
