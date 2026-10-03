"""配置数据模型（pydantic）。

字段风格对齐常见的 ``config.toml`` 写法，便于手动粘贴已有配置，
同时保持向后兼容：未知字段一律保留，不做严格校验失败。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProviderKind = Literal["openai", "openai-responses", "anthropic", "gemini"]
EffortLevel = Literal["disabled", "low", "medium", "high", "max"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Price(_Base):
    """模型计价（每百万 token，单位由 currency 决定）。"""

    input: float = 0.0
    output: float = 0.0
    cache_hit: float = 0.0
    cache_write: float = 0.0
    currency: str = "¥"
    unit: int = 1_000_000
    # ★ 峰谷价格：部分供应商在特定时段打折（如 DeepSeek 的夜间优惠）。
    #   填了 off_peak_* 才生效；不填就一律按上面的常价算，不改变既有行为。
    #   peak_hours：按本地时间判断的"高峰时段"描述，形如 "08:30-00:30"
    #   （跨零点自动识别）；off_peak_input / off_peak_output 是低谷单价。
    off_peak_input: float = 0.0
    off_peak_output: float = 0.0
    peak_hours: str = ""


class ModelOverride(_Base):
    """单个模型的覆盖声明。"""

    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    vision: bool | None = None
    thinking: bool | None = None
    tools: bool | None = None
    # 逐模型开关：None = 未显式设置（跟随供应商启用状态），True/False = 用户在
    # 「模型服务」页手动勾选/取消。用户要求：模型前面有勾选框，勾上才启用。
    enabled: bool | None = None
    default_effort: EffortLevel | None = None
    supported_efforts: list[EffortLevel] | None = None
    price: Price | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class Provider(_Base):
    """一个模型供应商（中转站 / 官方 API / 本地服务）。"""

    name: str
    kind: ProviderKind = "openai"
    display_name: str | None = None

    base_url: str = ""
    chat_url: str | None = None
    request_url: str | None = None
    models_url: str | None = None
    balance_url: str | None = None

    api_key: str | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    models: list[str] = Field(default_factory=list)
    default: str | None = None
    vision_models: list[str] = Field(default_factory=list)

    context_window: int | None = None
    max_output_tokens: int | None = None
    thinking: bool | None = None
    web_search: bool | None = None
    default_effort: EffortLevel | None = None
    supported_efforts: list[EffortLevel] | None = None

    billing_currency: str = "CNY"
    billing_mode: str | None = None
    price: Price | None = None

    enabled: bool = True
    timeout_seconds: float = 300.0
    max_retries: int = 2
    extra: dict[str, Any] = Field(default_factory=dict)

    model_overrides: dict[str, ModelOverride] = Field(default_factory=dict)
    prices: dict[str, Price] = Field(default_factory=dict)

    @field_validator("thinking", "web_search", mode="before")
    @classmethod
    def _coerce_bool(cls, v: Any) -> Any:
        """容忍 thinking="enabled"/"disabled" 这类字符串写法。"""
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("enabled", "on", "true", "yes", "1", "enable"):
                return True
            if low in ("disabled", "off", "false", "no", "0", "disable", "none"):
                return False
            return None
        return v


class LLMConfig(_Base):
    default_provider: str | None = None
    default_model: str | None = None
    fallback_models: list[str] = Field(default_factory=list)
    # 0.6 = DeepSeek V3.1 model card 对「编码智能体」的推荐值
    temperature: float = 0.6
    # 输出上限。None = 不填即不限：交给模型/供应商自己决定，不做本地截断。
    # 为什么默认 None：原先固定 8192 会在 router 里形成一道硬上限
    # （min(模型自报值, 8192)），把模型实际能输出的更长内容截掉。
    max_tokens: int | None = None
    stream: bool = True
    request_timeout: float = 300.0
    max_retries: int = 2
    # ★ 流式**整体**时长上限（秒）。上下文整理/流式都有统一上限。
    #   为什么必须单独有它：request_timeout 对 httpx 流式只是「块间超时」——
    #   只要每块都在超时内到达，整条流可以跑到天荒地老。用户实测过一次调用
    #   651 秒（点停止像没反应、看着像卡死）。0 = 关闭该上限。
    stream_total_timeout: float = 600.0
    # ★ 2-B：压缩时的分块大小（估算 token）。历史超过这个值就分块摘要再树形归并，
    #   避免一次性摘要吃不下整段而**整段失败**。0 = 不分块（用旧的单次摘要）。
    compact_chunk_tokens: int = 24_000
    # 上下文管理
    context_window_override: int | None = None
    # 上下文压缩阈值（占上下文窗口的比例）。0.8 = 推荐档，
    # 平衡连续性与缓存命中；0.7 = 更早释放上下文。
    compact_ratio: float = 0.8
    # 上下文「经济上限」：即便模型声明的窗口很大（如 1M），也在这么多 token 时
    # 就触发一次整理（默认 160000）。
    # ★ 为什么必须要它：1M 窗口 × 0.8 = 838K 的触发点，一次真实会话根本到不了，
    #   于是**压缩永远不会触发**，直到上游直接报「上下文超限」把这一轮打断
    #   （用户实测：窗口设 1M 后，对话跑到一半就出问题）。设成负数 = 关闭这个上限。
    context_soft_limit_tokens: int = 160_000
    keep_recent_turns: int = 6
    # 压缩时永远原样保留的开头消息数（系统提示 + 最初诉求），keep_first
    keep_first_messages: int = 2
    # 压缩收益低于此比例就跳过，避免为了省 3% 的 token 花一次模型调用
    minimum_progress: float = 0.1
    # 是否对旧的冗长工具输出做"观测遮罩"（保留调用骨架、折叠正文，不额外花模型调用）
    mask_observations: bool = True
    # 摘要里是否附"执行轨迹"（调用过哪些工具、成败如何），防止模型重走弯路
    keep_skeleton: bool = True


class AgentConfig(_Base):
    # 默认工作区：留空则用 <数据目录>/workspace
    workspace_override: str = ""
    # 各用途的模型分配（空 = 跟随默认模型）
    planner_model: str | None = None
    subagent_model: str | None = None
    vision_model: str | None = None
    search_model: str | None = None
    # 子代理推理强度
    subagent_effort: str = ""          # "" = 继承默认
    reasoning_language: str = "zh"
    language: str = "zh"
    temperature: float = 0.6
    # 单轮任务最多连续多少步工具调用。50 够跑大重构，又不至于死循环烧钱。
    # 不暴露在界面上。
    max_steps: int = 50
    max_parallel_subagents: int = 4
    reflection: bool = True
    routing: bool = True
    # 任务收尾前做一次强制自检（提交前审查）
    submit_checklist: bool = True
    show_reasoning: bool = True
    show_turn_usage: bool = True
    system_prompt_extra: str = ""
    # 子代理模型映射：{类型: 模型}
    subagent_models: dict[str, str] = Field(default_factory=dict)

    class Subagents(_Base):
        enabled: bool = True
        max_depth: int = 3
        default_budget_tokens: int = 120_000
        default_timeout: float = 900.0

    subagents: Subagents = Field(default_factory=Subagents)


class MemoryConfig(_Base):
    enabled: bool = True
    short_term_max_turns: int = 60
    long_term_enabled: bool = True
    episodic_enabled: bool = True
    profile_enabled: bool = True
    auto_summarize: bool = True
    summarize_threshold_tokens: int = 6000
    recall_top_k: int = 6
    recall_min_score: float = 0.22
    # 向量：优先 API embedding，失败自动降级为本地哈希向量
    embedding_provider: str | None = None
    embedding_model: str = ""
    embedding_dim: int = 512
    use_vector: bool = True
    decrypt_limit: int = 5000
    decay_half_life_days: float = 45.0


class ToolsConfig(_Base):
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    shell_timeout_seconds: float = 120.0
    mcp_call_timeout_seconds: float = 300.0
    mcp_startup_timeout_seconds: float = 30.0
    max_output_chars: int = 60_000

    class Shell(_Base):
        default_shell: str = ""  # 空 = 自动（Windows: pwsh/powershell；POSIX: bash）
        block_patterns: list[str] = Field(default_factory=list)

    class BackgroundJobs(_Base):
        enabled: bool = True
        stalled_warning_seconds: float = 900.0
        max_concurrent: int = 8

    shell: Shell = Field(default_factory=Shell)
    background_jobs: BackgroundJobs = Field(default_factory=BackgroundJobs)


class ApprovalRule(_Base):
    """危险操作的审批规则。"""

    pattern: str = ""
    action: Literal["allow", "ask", "deny"] = "ask"
    reason: str = ""


class PermissionsConfig(_Base):
    """审批门策略。mode: allow=全放行 / ask=危险操作需批准 / deny=只读。"""

    mode: Literal["allow", "ask", "deny"] = "ask"
    write_paths: list[str] = Field(default_factory=list)
    read_paths: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)
    rules: list[ApprovalRule] = Field(default_factory=list)
    approval_timeout_seconds: float = 300.0
    audit_enabled: bool = True


class SandboxConfig(_Base):
    mode: Literal["local", "subprocess", "off"] = "local"
    network: bool = True
    bash: Literal["auto", "on", "off"] = "auto"
    timeout_seconds: float = 120.0
    memory_limit_mb: int = 2048
    max_file_write_mb: int = 64
    cpu_limit: float = 0.0


class McpServerConfig(_Base):
    """MCP 服务器声明；兼容 ``[[plugins]]`` 与官方 ``mcpServers`` 两种写法。"""

    name: str
    type: Literal["stdio", "sse", "http", "streamable-http", "websocket"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    cwd: str | None = None
    tool_timeout_seconds: dict[str, float] = Field(default_factory=dict)
    namespace: str | None = None
    auto_start: bool = True
    trusted: bool = False


class McpConfig(_Base):
    enabled: bool = True
    auto_discover: bool = True
    expose_self: bool = False
    server_name: str = "fengcode"
    servers: list[McpServerConfig] = Field(default_factory=list)


class SkillConfig(_Base):
    paths: list[str] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    auto_match: bool = True
    max_loaded: int = 40


class PluginConfig(_Base):
    dirs: list[str] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    auto_load: bool = True


class SchedulerJob(_Base):
    name: str
    cron: str = ""
    interval_seconds: float = 0.0
    kind: Literal["prompt", "workflow", "shell", "tool"] = "prompt"
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_run: float = 0.0
    next_run: float = 0.0


class SchedulerConfig(_Base):
    enabled: bool = False
    jobs: list[SchedulerJob] = Field(default_factory=list)


class RemoteHost(_Base):
    name: str
    host: str
    port: int = 22
    user: str = "root"
    password: str | None = None
    password_env: str | None = None
    key_file: str | None = None
    workspace: str = "~"


class RemoteConfig(_Base):
    enabled: bool = False
    hosts: list[RemoteHost] = Field(default_factory=list)


class UIConfig(_Base):
    theme: Literal["light", "dark", "auto"] = "light"
    accent: str = "indigo"
    font_size: int = 17
    show_turn_usage: bool = True
    markdown: bool = True
    code_highlight: bool = True
    diff_review: bool = True
    streaming: bool = True
    language: Literal["zh", "en"] = "zh"
    tray: bool = True
    global_shortcut: str = "Ctrl+Alt+F"
    desktop_notifications: bool = True
    drag_drop: bool = True
    autostart: bool = False
    sound: bool = False


class ServerConfig(_Base):
    host: str = "127.0.0.1"
    port: int = 7845
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    token: str | None = None
    open_browser: bool = False


class WebSearchConfig(_Base):
    engines: list[str] = Field(default_factory=lambda: ["ddg", "bing", "searxng", "zhipu"])
    searxng_url: str = "https://searx.be"
    zhipu_api_key: str | None = None
    zhipu_api_key_env: str = "GLM_API_KEY"
    max_results: int = 8
    timeout_seconds: float = 25.0
    render_js: bool = True


class AutomationConfig(_Base):
    windows_automation: bool = True
    screenshot_dir: str = ""
    allow_input_simulation: bool = True


class Config(_Base):
    """Fengcode 顶层配置。"""

    config_version: int = 1
    language: str = "zh"
    default_model: str | None = None
    first_run_done: bool = False
    # 额外的密钥来源文件（如导入某个 .env 后指向它）；
    # 这样密钥不必抄进本文件，运行时按变量名去这些文件里解析。
    external_env_files: list[str] = Field(default_factory=list)

    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    skills: SkillConfig = Field(default_factory=SkillConfig)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)

    providers: list[Provider] = Field(default_factory=list)
