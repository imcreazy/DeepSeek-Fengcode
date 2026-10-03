# 架构说明

## 整体形态

Fengcode 有两个可执行形态，**共用同一套业务逻辑**：

```
┌─────────────────────┐        HTTP / WebSocket        ┌──────────────────────┐
│  desktop/           │  ──────────────────────────►   │  cli/                │
│  Electron 窗口外壳   │                                │  Python 后端 + 界面   │
│  · 窗口 / 托盘 / 菜单 │  ◄──────────────────────────   │  · Agent 编排         │
│  · 全局快捷键        │        加载后端托管的界面        │  · 工具执行 / 记忆     │
│  · 启动并守护后端     │                                │  · 会话 / 配置        │
└─────────────────────┘                                └──────────────────────┘
```

桌面端**不含业务逻辑**，它做三件事：启动后端进程、开一个窗口加载后端托管的网页界面、
提供托盘与快捷键。所以两边功能不会出现不一致。

---

## 后端分层

```
cli/src/fengcode/
├── core/           编排内核
│   ├── agent.py        ReAct 主循环、工具调用、上下文组装、压缩
│   └── prompts.py      系统提示词（含工作模式、子智能体角色）
├── llm/            模型适配层
│   ├── router.py       供应商路由、模型解析
│   └── ...             OpenAI / Anthropic / Gemini 三种协议
├── tools/          工具系统（50+ 内置）
│   ├── registry.py     注册表 + JSON Schema 生成
│   └── builtin/        文件、Shell、网络、文档、数据、开发…
├── storage/        SQLite 持久化
│   ├── db.py           建表与迁移
│   ├── sessions.py     会话与消息
│   ├── workspaces.py   项目（工作区）
│   └── tasks.py        待办与目标
├── memory/         分层记忆
│   ├── manager.py      读写、召回、重要度打分
│   └── ...             全文检索 + 向量混合召回
├── security/       安全
│   ├── approval.py     审批门（模式 + 细粒度规则）
│   ├── paths.py        路径守卫（白名单 + 防穿越）
│   └── sandbox.py      子进程沙箱（超时 / 环境最小化）
├── skills/         技能（SKILL.md + Python 类）
├── plugins/        插件（manifest.yaml + 钩子）
├── mcp/            MCP 客户端与服务端
├── agents/         子智能体（独立上下文与预算）
├── server/         HTTP + WebSocket + 网页界面
│   ├── app.py          全部路由
│   └── static/         单文件前端（index.html）
└── cli/            命令行入口（TUI / REPL）
```

---

## 一次对话的生命周期

1. **请求进入**：前端 `POST /api/chat`，带 `message`、`model`、`mode`。
2. **组装上下文**：`agent.build_messages()` 拼系统提示 + 记忆召回 + 历史，
   超预算则先压缩（`compact()`）。
3. **主循环**：把消息和工具 schema 发给模型；模型返回工具调用就执行，
   结果回填后继续，直到模型不再调工具。
4. **工具执行**：每个工具调用都过 `ApprovalGate.evaluate()` ——
   按「模式 + 细粒度规则」判定放行 / 拒绝 / 弹窗询问。
5. **流式回传**：全过程通过事件总线推 SSE，前端实时渲染。
6. **落库**：消息、用量、审计记录写入 SQLite。

---

## 工作模式（计划 / 目标 / 不选）

`mode` 有三种取值，注入不同的系统提示段：

| 值 | 行为 |
|---|---|
| `""`（空） | 不指定。提示词引导模型按任务复杂度自行判断 |
| `"plan"` | 先出分步计划并等确认，再动手 |
| `"goal"` | 用 `goal` 工具记录长期目标，跨轮推进 |

归一化在 `core/prompts.py::normalize_mode()` ——
历史值 `"chat"` 会被视为「不选」，保证向后兼容。

---

## 权限模型

两层判定，**规则优先于模式**：

```
细粒度规则（deny > ask > allow）
        ↓ 没命中
模式（deny=只读 / ask=危险操作需批准 / allow=全放行）
        ↓
路径守卫（白名单 + 禁止通配符）
```

规则语法（`security/approval.py::_match_rule`）：

| 写法 | 语义 |
|---|---|
| `tool:shell` | 按工具名匹配，支持通配 `tool:read_*` |
| `cmd:git push` | 匹配命令。**无通配符时按前缀匹配** |
| `path:**/.ssh/*` | 匹配路径。无通配符时按子串匹配 |
| `risk:删除` | 在目标文本里找关键词 |

---

## 前端为什么是单文件

`cli/src/fengcode/server/static/index.html` 是一个自带样式的单文件应用
（无构建步骤、无外部依赖）。这样：

- 打包简单：`PyInstaller` 直接带进去，不需要 Node 工具链
- 桌面端加载的就是同一份，不存在两套前端

**代价**：所有面板共享一个文档，跨面板通信要小心。
典型坑是「设置中心复制面板的 `innerHTML`」——
复制后的副本上，**直接绑的事件会失效**，所以前端统一用**事件委托**
（`document.addEventListener` + `closest()`）。改前端时请沿用这个模式。

---

## 数据流与存储

| 数据 | 位置 | 说明 |
|---|---|---|
| 配置 | `config/config.toml` | 供应商、密钥、权限、界面偏好 |
| 会话与消息 | `data/fengcode.db` | SQLite，含全文索引 |
| 记忆 | 同库 `memories` 表 | 分全局 / 本项目两层作用域 |
| 审计 | 同库 `audit_log` 表 | 工具调用与审批决定，密钥脱敏 |
| 工作区文件 | `workspace/` | 默认项目的目录 |

数据目录由 `FENGCODE_HOME` 决定；未设置时自动选合适位置
（源码运行放项目下 `.fengcode/`，打包运行放 exe 同级 `fengcode-data/`）。
