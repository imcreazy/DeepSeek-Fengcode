"""内置模型能力表：主流模型的上下文窗口、价格与能力。

用于「自动推断」：用户在界面填了模型名却没填能力时，从这里兜底。
价格单位统一为 **每百万 token**，币种按条目声明。

数据来源为各厂商公开定价页（2026 年口径），仅供估算，用户可覆盖。
"""

from __future__ import annotations

from typing import Any

# kind: openai / anthropic / gemini
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    # ---- OpenAI ----
    "gpt-4o": {"context_window": 128_000, "max_output_tokens": 16_384, "vision": True, "tools": True, "price": {"input": 2.5, "output": 10, "cache_hit": 1.25, "currency": "$"}},
    "gpt-4o-mini": {"context_window": 128_000, "max_output_tokens": 16_384, "vision": True, "tools": True, "price": {"input": 0.15, "output": 0.6, "cache_hit": 0.075, "currency": "$"}},
    "gpt-4.1": {"context_window": 1_047_576, "max_output_tokens": 32_768, "vision": True, "tools": True, "price": {"input": 2.0, "output": 8.0, "cache_hit": 0.5, "currency": "$"}},
    "gpt-4.1-mini": {"context_window": 1_047_576, "max_output_tokens": 32_768, "vision": True, "tools": True, "price": {"input": 0.4, "output": 1.6, "cache_hit": 0.1, "currency": "$"}},
    "gpt-5": {"context_window": 400_000, "max_output_tokens": 128_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 1.25, "output": 10, "cache_hit": 0.125, "currency": "$"}},
    "gpt-5-mini": {"context_window": 400_000, "max_output_tokens": 128_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 0.25, "output": 2.0, "cache_hit": 0.025, "currency": "$"}},
    "o3": {"context_window": 200_000, "max_output_tokens": 100_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 2.0, "output": 8.0, "cache_hit": 0.5, "currency": "$"}},
    "o4-mini": {"context_window": 200_000, "max_output_tokens": 100_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 1.1, "output": 4.4, "cache_hit": 0.275, "currency": "$"}},
    # ---- Anthropic ----
    "claude-opus-4-1": {"context_window": 200_000, "max_output_tokens": 32_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 15, "output": 75, "cache_hit": 1.5, "cache_write": 18.75, "currency": "$"}},
    "claude-opus-4": {"context_window": 200_000, "max_output_tokens": 32_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 15, "output": 75, "cache_hit": 1.5, "cache_write": 18.75, "currency": "$"}},
    "claude-sonnet-4-5": {"context_window": 200_000, "max_output_tokens": 64_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 3, "output": 15, "cache_hit": 0.3, "cache_write": 3.75, "currency": "$"}},
    "claude-sonnet-4": {"context_window": 200_000, "max_output_tokens": 64_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 3, "output": 15, "cache_hit": 0.3, "cache_write": 3.75, "currency": "$"}},
    "claude-haiku-4-5": {"context_window": 200_000, "max_output_tokens": 32_000, "vision": True, "tools": True, "thinking": True, "price": {"input": 1, "output": 5, "cache_hit": 0.1, "cache_write": 1.25, "currency": "$"}},
    "claude-3-5-haiku": {"context_window": 200_000, "max_output_tokens": 8_192, "vision": True, "tools": True, "price": {"input": 0.8, "output": 4, "cache_hit": 0.08, "cache_write": 1, "currency": "$"}},
    # ---- Google ----
    "gemini-2.5-pro": {"context_window": 1_048_576, "max_output_tokens": 65_536, "vision": True, "tools": True, "thinking": True, "price": {"input": 1.25, "output": 10, "cache_hit": 0.31, "currency": "$"}},
    "gemini-2.5-flash": {"context_window": 1_048_576, "max_output_tokens": 65_536, "vision": True, "tools": True, "thinking": True, "price": {"input": 0.3, "output": 2.5, "cache_hit": 0.075, "currency": "$"}},
    "gemini-2.0-flash": {"context_window": 1_048_576, "max_output_tokens": 8_192, "vision": True, "tools": True, "price": {"input": 0.1, "output": 0.4, "currency": "$"}},
    # ---- DeepSeek ----
    "deepseek-chat": {"context_window": 128_000, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 0.27, "output": 1.1, "cache_hit": 0.07, "currency": "$"}},
    "deepseek-reasoner": {"context_window": 128_000, "max_output_tokens": 65_536, "vision": False, "tools": False, "thinking": True, "price": {"input": 0.55, "output": 2.19, "cache_hit": 0.14, "currency": "$"}},
    "deepseek-v3": {"context_window": 131_072, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 0.27, "output": 1.1, "currency": "$"}},
    # ---- 阿里通义 ----
    "qwen-max": {"context_window": 131_072, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 2.4, "output": 9.6, "currency": "¥"}},
    "qwen-plus": {"context_window": 131_072, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 0.8, "output": 2, "currency": "¥"}},
    "qwen-turbo": {"context_window": 1_000_000, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 0.3, "output": 0.6, "currency": "¥"}},
    "qwen3-max": {"context_window": 262_144, "max_output_tokens": 32_768, "vision": False, "tools": True, "thinking": True, "price": {"input": 2.5, "output": 10, "currency": "¥"}},
    "qwen-vl-max": {"context_window": 131_072, "max_output_tokens": 8_192, "vision": True, "tools": True, "price": {"input": 3, "output": 9, "currency": "¥"}},
    # ---- 智谱 GLM ----
    "glm-4-plus": {"context_window": 131_072, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 5, "output": 5, "currency": "¥"}},
    "glm-4-air": {"context_window": 131_072, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 0.5, "output": 0.5, "currency": "¥"}},
    "glm-4v-plus": {"context_window": 8_192, "max_output_tokens": 4_096, "vision": True, "tools": True, "price": {"input": 4, "output": 4, "currency": "¥"}},
    "glm-4.6": {"context_window": 204_800, "max_output_tokens": 131_072, "vision": False, "tools": True, "thinking": True, "price": {"input": 4, "output": 16, "currency": "¥"}},
    "glm-4.5": {"context_window": 131_072, "max_output_tokens": 98_304, "vision": False, "tools": True, "thinking": True, "price": {"input": 2, "output": 8, "currency": "¥"}},
    "glm-4.5-air": {"context_window": 131_072, "max_output_tokens": 98_304, "vision": False, "tools": True, "thinking": True, "price": {"input": 0.8, "output": 2, "currency": "¥"}},
    # ---- Moonshot / Kimi ----
    "kimi-k2": {"context_window": 262_144, "max_output_tokens": 32_768, "vision": False, "tools": True, "price": {"input": 4, "output": 16, "currency": "¥"}},
    "kimi-k2-turbo-preview": {"context_window": 262_144, "max_output_tokens": 32_768, "vision": False, "tools": True, "price": {"input": 4, "output": 16, "currency": "¥"}},
    "moonshot-v1-128k": {"context_window": 128_000, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 60, "output": 60, "currency": "¥"}},
    # ---- 豆包 / 火山方舟 ----
    "doubao-seed-1-6": {"context_window": 262_144, "max_output_tokens": 32_768, "vision": True, "tools": True, "thinking": True, "price": {"input": 0.8, "output": 2, "currency": "¥"}},
    "doubao-1-5-pro-32k": {"context_window": 32_768, "max_output_tokens": 12_288, "vision": False, "tools": True, "price": {"input": 0.8, "output": 2, "currency": "¥"}},
    # ---- MiMo（小米）----
    "mimo-7b-rl": {"context_window": 131_072, "max_output_tokens": 32_768, "vision": False, "tools": True, "thinking": True, "price": {"input": 0, "output": 0, "currency": "¥"}},
    # ---- 百川 / MiniMax ----
    "abab6.5s-chat": {"context_window": 245_760, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 1, "output": 1, "currency": "¥"}},
    "MiniMax-Text-01": {"context_window": 1_000_000, "max_output_tokens": 8_192, "vision": False, "tools": True, "price": {"input": 1, "output": 8, "currency": "¥"}},
    "MiniMax-M2": {"context_window": 204_800, "max_output_tokens": 131_072, "vision": False, "tools": True, "thinking": True, "price": {"input": 2.1, "output": 8.4, "currency": "¥"}},
    # ---- 阶跃星辰 Step ----
    "step-3": {"context_window": 65_536, "max_output_tokens": 32_768, "vision": True, "tools": True, "thinking": True, "price": {"input": 1, "output": 3, "currency": "¥"}},
}

# 供应商预设：界面「添加供应商」时的一键模板
#
# ★ 只收「厂商官方」与「用户自己的万象 API」两类，**不收 OpenRouter 这类第三方聚合**（用户明确要求）。
# ★ 不做本地部署项（Ollama / LM Studio / vLLM 一律不预置，相关客户端已从代码移除）。
# 每个预设只需用户填 key；base_url / kind / models_url 都已给全。
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    # ---- 第 1 个：用户自己的万象 API ----
    "wanxiang": {
        "label": "万象 API",
        "kind": "openai",
        "base_url": "https://liufengzi.qd.je/v1",
        "chat_url": "https://liufengzi.qd.je/v1/chat/completions",
        "models_url": "https://liufengzi.qd.je/v1/models",
        "models": [],
        "billing_currency": "CNY",
        "api_key_env": "WANXIANG_API_KEY",
        "description": "你自己的中转站，填上密钥、点「测试并获取模型」即可用",
        "homepage": "https://liufengzi.qd.je",
        # ★ 思考参数：实测（2026-10-01）万象只认 reasoning_effort，
        #   thinking / enable_thinking 一律无效（返回 reasoning_content=无）。
        #   不配 default_effort 时 router 会 setdefault(None)，客户端直接跳过，
        #   请求里不带任何思考参数 → 模型完全不思考。这是「深度思考被关了」的根因。
        "default_effort": "medium",
        "effort_style": "reasoning_effort",
    },
    # ---- 官方厂商（按用户指定顺序：DeepSeek → 智谱 → MiMo）----
    "deepseek": {
        "label": "DeepSeek 官方",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models_url": "https://api.deepseek.com/v1/models",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "billing_currency": "CNY",
        "api_key_env": "DEEPSEEK_API_KEY",
        "description": "深度求索官方 API",
        "homepage": "https://platform.deepseek.com",
    },
    "zhipu": {
        "label": "智谱 AI（GLM）",
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models_url": "https://open.bigmodel.cn/api/paas/v4/models",
        "models": ["glm-4.6", "glm-4.5", "glm-4.5-air"],
        "billing_currency": "CNY",
        "api_key_env": "ZHIPUAI_API_KEY",
        "description": "智谱官方 API，GLM 系列",
        "homepage": "https://open.bigmodel.cn",
    },
    "mimo": {
        "label": "MiMo（小米）",
        "kind": "openai",
        "base_url": "https://api.xiaomimimo.com/v1",
        "models_url": "https://api.xiaomimimo.com/v1/models",
        "models": ["mimo-7b-rl"],
        "billing_currency": "CNY",
        "api_key_env": "MIMO_API_KEY",
        "description": "小米 MiMo 官方 API",
        "homepage": "https://xiaomimimo.com",
    },
    "dashscope": {
        "label": "阿里通义千问",
        "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "models": ["qwen3-max", "qwen-max", "qwen-plus"],
        "billing_currency": "CNY",
        "api_key_env": "DASHSCOPE_API_KEY",
        "description": "阿里云百炼（DashScope）官方 API",
        "homepage": "https://bailian.console.aliyun.com",
    },
    "moonshot": {
        "label": "月之暗面 Kimi",
        "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models_url": "https://api.moonshot.cn/v1/models",
        "models": ["kimi-k2-turbo-preview", "moonshot-v1-128k"],
        "billing_currency": "CNY",
        "api_key_env": "MOONSHOT_API_KEY",
        "description": "Moonshot 官方 API，Kimi 系列",
        "homepage": "https://platform.moonshot.cn",
    },
    "doubao": {
        "label": "字节豆包（火山方舟）",
        "kind": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models_url": "https://ark.cn-beijing.volces.com/api/v3/models",
        "models": ["doubao-seed-1-6"],
        "billing_currency": "CNY",
        "api_key_env": "ARK_API_KEY",
        "description": "火山引擎方舟官方 API，豆包系列",
        "homepage": "https://console.volcengine.com/ark",
    },
    "minimax": {
        "label": "MiniMax",
        "kind": "openai",
        "base_url": "https://api.minimax.chat/v1",
        "models_url": "https://api.minimax.chat/v1/models",
        "models": ["MiniMax-M2", "MiniMax-Text-01"],
        "billing_currency": "CNY",
        "api_key_env": "MINIMAX_API_KEY",
        "description": "MiniMax 官方 API",
        "homepage": "https://platform.minimaxi.com",
    },
    "baichuan": {
        "label": "百川智能",
        "kind": "openai",
        "base_url": "https://api.baichuan-ai.com/v1",
        "models_url": "https://api.baichuan-ai.com/v1/models",
        "models": ["abab6.5s-chat"],
        "billing_currency": "CNY",
        "api_key_env": "BAICHUAN_API_KEY",
        "description": "百川智能官方 API",
        "homepage": "https://platform.baichuan-ai.com",
    },
    "stepfun": {
        "label": "阶跃星辰 Step",
        "kind": "openai",
        "base_url": "https://api.stepfun.com/v1",
        "models_url": "https://api.stepfun.com/v1/models",
        "models": ["step-3"],
        "billing_currency": "CNY",
        "api_key_env": "STEPFUN_API_KEY",
        "description": "阶跃星辰官方 API",
        "homepage": "https://platform.stepfun.com",
    },
    # ---- 自定义（兜底，任何 OpenAI 兼容服务）----
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "kind": "openai",
        "base_url": "",
        "models": [],
        "billing_currency": "CNY",
        "api_key_env": "",
        "description": "任何 OpenAI 兼容的服务，自己填地址与密钥",
    },
}

# 界面「预置供应商」的展示顺序（用户指定：万象 → DeepSeek → 智谱 → MiMo → 其余）
PRESET_ORDER: list[str] = [
    "wanxiang",
    "deepseek",
    "zhipu",
    "mimo",
    "dashscope",
    "moonshot",
    "doubao",
    "minimax",
    "baichuan",
    "stepfun",
]

# .env 变量名 → 预设 id 的推断表（供 CLI 的 --preset 参数与自动识别使用）
ENV_KEY_HINTS: dict[str, str] = {
    "WANXIANG_API_KEY": "wanxiang",
    "DEEPSEEK_API_KEY": "deepseek",
    "GLM_API_KEY": "zhipu",
    "ZHIPU_API_KEY": "zhipu",
    "ZHIPUAI_API_KEY": "zhipu",
    "MIMO_API_KEY": "mimo",
    "DASHSCOPE_API_KEY": "dashscope",
    "QWEN_API_KEY": "dashscope",
    "MOONSHOT_API_KEY": "moonshot",
    "KIMI_API_KEY": "moonshot",
    "ARK_API_KEY": "doubao",
    "DOUBAO_API_KEY": "doubao",
    "MINIMAX_API_KEY": "minimax",
    "BAICHUAN_API_KEY": "baichuan",
    "STEPFUN_API_KEY": "stepfun",
    "CUSTOM_API_KEY": "custom",
}


def lookup(model: str) -> dict[str, Any]:
    """按模型名查能力；支持前缀/后缀模糊匹配（如 ``xxx/gpt-4o``）。"""
    if not model:
        return {}
    m = model.strip()
    if m in MODEL_CATALOG:
        return dict(MODEL_CATALOG[m])
    low = m.lower()
    # 去掉供应商前缀，如 "openai/gpt-4o"、"z-ai/glm-5.3"
    tail = low.split("/")[-1]
    if tail in MODEL_CATALOG:
        return dict(MODEL_CATALOG[tail])
    # 去掉 :free / :latest 之类标记
    base = tail.split(":")[0]
    if base in MODEL_CATALOG:
        return dict(MODEL_CATALOG[base])
    # 模糊：最长公共前缀匹配
    best_key, best_len = None, 0
    for key in MODEL_CATALOG:
        if base.startswith(key) or key.startswith(base):
            n = min(len(key), len(base))
            if n > best_len:
                best_key, best_len = key, n
    if best_key and best_len >= 4:
        return dict(MODEL_CATALOG[best_key])
    return {}


def known_models() -> list[str]:
    return sorted(MODEL_CATALOG.keys())


def presets() -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in PROVIDER_PRESETS.items()}
