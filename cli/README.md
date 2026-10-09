# Fengcode

> 本地运行的中文 AI Agent 平台 —— 桌面端 + 命令行 + Web 界面，数据自持。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)]()
[![中文](https://img.shields.io/badge/%E7%95%8C%E9%9D%A2-%E4%B8%AD%E6%96%87-red)]()

Fengcode 是一个本地运行的 AI Agent 平台，可直接读写文件、执行命令、联网检索、
操作数据库并维护长期记忆，也支持派生多个子智能体并行处理任务。
所有数据存储于本机，模型供应商由使用者自行配置。

> **安全声明**
>
> 请先阅读 [SECURITY.md](SECURITY.md)。Fengcode 具备真实执行能力：读写文件、执行命令、访问网络。
> 项目提供审批门、路径白名单、本地沙箱等防护机制，可降低风险但不构成风险消除。
> 请遵循最小权限原则，不要将其指向不可信的工作负载。

---

## 目录

- [能力范围](#能力范围)
- [工作模式](#工作模式)
- [项目（工作区）](#项目工作区)
- [设置中心](#设置中心)
- [快速开始](#快速开始)
- [三种使用方式](#三种使用方式)
- [配置模型](#配置模型)
- [核心能力](#核心能力)
- [安全机制](#安全机制)
- [命令行用法](#命令行用法)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [参与开发](#参与开发)
- [许可证](#许可证)

---

## 能力范围

| 场景 | 实际表现 |
|---|---|
| **代码修改** | 解析项目结构 → 修改实现 → 自行运行测试 → 如实报告结果 |
| **资料检索** | 多引擎检索 → 抓取原文 → 交叉验证 → 输出附来源的结论 |
| **文档处理** | PDF / Word / Excel / PPT 解析，CSV 统计分析，生成图表 |
| **运维排查** | SSH 连接服务器查看日志、检查资源、修改配置并验证 |
| **数据处理** | SQLite 查询、数据清洗、生成报告与图表 |
| **自动化** | 定时任务（cron）、工作流 DAG、Windows 桌面操作 |
| **长期协作** | 记录使用者偏好与项目背景，跨会话生效 |
| **多项目并行** | 每个项目绑定独立工作目录，会话按项目归类，互不干扰 |

Fengcode 并非仅提供文本回复：它实际执行文件操作、命令与测试，并在失败时依据报错自行修正。

---

## 工作模式

输入框上方的模式选择器可以不选择任何项：

| 状态 | 行为 |
|---|---|
| **自动**（不选） | 由模型依据任务复杂度决定使用计划或目标策略；简单任务直接执行，复杂任务先列清单 |
| **计划模式** | 先输出分步计划供确认，确认后再执行 |
| **目标模式** | 围绕长期目标跨轮推进，保留进度，不重复已完成的工作 |

计划与目标均可反选（点击已选项即取消，回到自动）。

---

## 项目（工作区）

左侧栏为「项目 → 会话」两级结构：

- 点击 **+** 新建项目，目录自动创建
- 每个项目绑定一个独立工作目录，会话与文件归属于该项目
- 支持重命名、迁移目录、删除；删除项目不影响磁盘上的文件
- 默认项目受保护，不可删除

---

## 设置中心

左侧分组导航 + 右侧单面板布局，分类组织如下：

**偏好设置**（通用 / 外观）· **模型**（模型服务 / 模型偏好 / 用量统计）·
**集成与连接**（MCP 与工具 / 远程 SSH）· **能力扩展**（技能 / 子智能体 / 插件）·
**记忆与上下文**（记忆）· **自动化与开发者**（定时任务 / 审计日志）·
**安全与控制**（权限 / 沙箱）· **应用**（高级 / 关于）

主要页面：

- **权限**：三档权限等级与细粒度规则。规则支持 `tool:shell`、`cmd:git push`、
  `path:**/.ssh/*`、`risk:<关键词>` 四种写法，优先级为 deny > ask > allow
- **沙箱**：探测本机可用的 Shell（pwsh / Windows PowerShell / Git Bash / WSL）并列出路径，
  同时显示文件工具的写入范围
- **记忆**：记忆条目列表与召回参数（开关、条数、最低分、遗忘半衰期），
  可检索与删除单条记忆
- **高级**：含恢复助手 —— 运行诊断、备份配置、一键回滚（回滚前自动再备份一份）

---

## 快速开始

### Windows

```bat
:: 运行：自动安装依赖、启动服务、打开浏览器
启动.bat
```

或使用命令行：

```powershell
git clone https://github.com/imcreazy/DeepSeek-Fengcode.git
cd Fengcode
.\start.ps1
```

### Linux / macOS

```bash
git clone https://github.com/imcreazy/DeepSeek-Fengcode.git
cd Fengcode
chmod +x install.sh && ./install.sh run
```

### 获取预构建版本

见 [Releases](https://github.com/imcreazy/DeepSeek-Fengcode/releases/latest)：

| 下载 | 说明 |
|---|---|
| `Fengcode-<版本>-setup.exe`（约 132 MB） | 安装程序，创建快捷方式，可在「应用和功能」卸载 |
| `Fengcode-<版本>-win64.zip` | 解压即用，无需安装 Python，启动最快 |
| `Fengcode-<版本>-source.zip` | 源码包，需 Python 3.10+ |

三者功能范围一致，差异仅在部署方式。

### 自行打包

```bat
构建exe.bat
```

产物分为两部分：

| 位置 | 说明 |
|---|---|
| `cli/dist/Fengcode/` | PyInstaller 产物（后端可执行文件 `Fengcode.exe`） |
| `desktop/dist/` | Electron 产物（安装程序 exe 与 `win-unpacked/` 解压即用目录） |

统一整理至 `交付/` 目录：

```bash
python scripts/deliver.py --apply
```

打包细节见 `cli/fengcode.spec`。

### 手动安装

```bash
python -m pip install -e .
fengcode serve --open      # 启动 Web 界面
fengcode chat              # 或进入命令行对话
```

**前置要求**：Python 3.10+（推荐 3.12）。Windows 无需额外依赖；Linux 建议安装 `python3-venv`。

---

## 三种使用方式

### 1. Web 界面

```bash
fengcode serve --open
```

浏览器打开 `http://127.0.0.1:7845`，包含以下面板：

| 面板 | 用途 |
|---|---|
| **对话** | 主交互区：流式输出、工具调用可视化、审批对话框、文件拖入 |
| **会话记录** | 历史会话管理、置顶、归档、导出 Markdown |
| **记忆** | 浏览 / 编辑 / 检索长期记忆，按类型筛选 |
| **技能** | 技能库管理，可新建自定义 `SKILL.md` |
| **工具** | 查看全部内置工具，可单独停用 |
| **MCP 服务** | 接入外部 MCP 工具生态，支持连通性测试 |
| **插件** | 插件管理，支持 UI 面板扩展 |
| **工作流** | 可视化编辑 DAG，同层并行执行 |
| **定时任务** | cron 表达式调度 |
| **远程主机** | SSH 主机管理 |
| **用量统计** | token 与费用趋势、按模型排行 |
| **审计日志** | 每次工具执行与审批决策（密钥自动脱敏） |
| **设置** | 模型供应商、Agent 行为、安全策略、界面主题 |

**主题**：浅色（默认）/ 深色 / 跟随系统。

### 2. 命令行对话

```bash
fengcode chat              # 全屏 TUI（需 textual）
fengcode chat --simple     # 朴素 REPL（兼容能力较弱的终端）
```

TUI 快捷键：`Ctrl+Q` 退出 · `Ctrl+N` 新会话 · `Ctrl+L` 清屏 · `Ctrl+R` 只读切换 · `F1` 帮助

斜杠命令：`/new` `/model` `/todo` `/memory` `/remember` `/tools` `/skills` `/compact` `/status`

### 3. 脚本调用

```bash
# 单次任务，结果输出到 stdout，适合管道与自动化
fengcode run "统计当前目录有多少个 Python 文件"

# JSON 输出，便于程序消费
fengcode run --json "把 data.csv 按月份汇总"

# 非交互场景自动批准（请谨慎使用）
fengcode run --approve auto "清理临时文件"

# 从 stdin 读取任务
echo "解释一下这个报错" | fengcode run
```

---

## 配置模型

Fengcode **不预置任何密钥**。首次启动后进入「设置 → 模型服务」添加，或使用命令行：

### 从环境变量导入

若密钥已存在于环境变量（如 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`），
可让 Fengcode 自动识别并生成对应的供应商配置：

```bash
fengcode config provider add
```

也可在配置中写 `api_key_env = "DEEPSEEK_API_KEY"` 只引用变量名，密钥不落盘。

Web 界面：**设置 → 模型服务 → 添加服务**。

### 手动添加

```bash
# 使用预设模板（wanxiang / deepseek / zhipu / mimo / dashscope / moonshot /
#                doubao / minimax / baichuan / stepfun / custom）
fengcode config provider --add zhipu --preset zhipu --api-key "<API Key>"

# 自建中转站
fengcode config provider --add myapi --base-url "https://your-host/v1" \
    --api-key "sk-xxx" --models "gpt-4o,claude-sonnet-4-5"

# 测试连通性并拉取可用模型
fengcode config test myapi --list-models
```

### 支持的协议

| 协议 | 说明 |
|---|---|
| **OpenAI 兼容** | 官方接口、DeepSeek、智谱、通义、Kimi、各类中转站 |
| **Anthropic** | Claude 系列（支持扩展思考） |
| **Google Gemini** | 原生 API（支持 1M 上下文） |

每个供应商可单独声明：上下文窗口、最大输出、是否支持视觉、思考档位、价格。
未声明项将按内置能力表推断。

---

## 核心能力

### 工具系统（50 个内置）

- **文件**：读写、精确编辑、多处替换、应用补丁、搜索、目录树、历史回滚
- **执行**：Shell（PowerShell / Bash）、Python、脚本、后台任务、进程管理
- **网络**：多引擎检索、网页抓取（可渲染 JS）、HTTP 请求、文件下载
- **文档**：PDF / Word / Excel / PPT 解析，图片识别，图表生成，Office 生成
- **数据**：SQLite 查询与导入导出、数据统计分析
- **开发**：Git 操作、仓库概览、代码结构地图、依赖图、测试运行、静态检查
- **认知**：记忆、任务清单、目标、技能、子智能体、计划、提问
- **自动化**：截图、鼠标键盘模拟、窗口管理、剪贴板、系统通知、系统信息
- **远程**：SSH 执行、上传下载、日志查看
- **归档**：压缩包读取 / 解压 / 打包

### 记忆系统

分为五层，跨会话持久：

| 层 | 作用 |
|---|---|
| 工作记忆 | 当前会话的即时信息 |
| 短期记忆 | 完整对话历史（可回放） |
| 长期记忆 | 稳定事实与知识 |
| 情景记忆 | 具体事件的记录 |
| 用户画像 | 使用者偏好与习惯 |

检索采用**全文与向量混合召回**，并叠加重要度、新鲜度、访问频率综合排序。
向量优先使用 API embedding，失败时自动降级为本地哈希向量，离线环境下同样可用。

### 技能系统

Markdown 格式（兼容 Claude Skills 规范）：

```
<技能目录>/代码改动规范/SKILL.md
```

```markdown
---
name: 代码改动规范
description: 安全修改现有代码库的标准流程
when_to_use: 需要改已有项目、修 bug、加功能时
tags: [开发, 重构]
---

# 步骤
1. 先理解再动手：code_map 看结构，grep 找引用
2. 最小改动：优先 edit_file，不重写整个文件
3. 改完必须验证：跑测试，如实报告
```

内置 9 个中文技能：代码改动规范、代码评审、深度研究、数据分析、文档写作、
网页前端设计、服务器运维、任务分解、记忆管理。亦支持 Python 类技能（继承 `BaseSkill`）。

### MCP 双向支持

**作为客户端**：接入任意 MCP 服务器（stdio / SSE / HTTP）。

```bash
fengcode mcp list
fengcode mcp test github
```

**作为服务器**：将自身能力提供给其他 MCP 客户端。

```bash
fengcode mcp serve --transport stdio
```

客户端配置示例：

```json
{
  "mcpServers": {
    "fengcode": {
      "command": "fengcode",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

### 子智能体

具备独立上下文、独立工具集与独立预算，仅将结论回传主 Agent，以降低 token 消耗并隔离干扰。

内置 7 种：`general` / `explore`（代码探索）/ `research`（研究调研）/ `review`（代码评审）/
`security-review`（安全审计）/ `test`（测试验证）/ `plan`（任务规划）。

编排方式：单个 / 并行扇出 / 顺序链（上一步结论作为下一步背景）/ 辩论（多模型独立作答后由裁判综合）。
支持嵌套派生，受 `max_depth` 限制。

### 插件系统

```yaml
# <插件目录>/my-plugin/manifest.yaml
name: my-plugin
version: 1.0.0
description: 示例插件
permissions: [tools, hooks]
tools:
  - name: hello
    description: 打个招呼
    parameters: {who: {type: string}}
hooks: [before_tool, after_tool]
```

```python
# plugin.py
def setup(api):
    def hello(who="世界"):
        return f"你好，{who}！"
    api.register_tool("hello", hello, description="打招呼")
    api.on("after_tool", lambda ev: print("工具执行完成"))
```

可扩展点：工具、钩子（10 个时机）、供应商、UI 面板、技能、子智能体、命令。
未在 `permissions` 中声明的能力将被拒绝。

---

## 安全机制

| 机制 | 说明 |
|---|---|
| **审批门** | 危险命令（如 `rm -rf /`、格式化、清理日志）执行前需确认；支持「仅本次 / 本会话 / 始终」 |
| **路径白名单** | 写入限定在允许目录内，防目录穿越；`C:\Windows`、`.ssh` 等默认禁止 |
| **本地沙箱** | 子进程执行，超时终止整棵进程树，环境变量最小化（密钥不会泄露给被执行的脚本） |
| **审计日志** | 每次工具调用与审批决策落库，密钥自动脱敏 |
| **只读模式** | 一键切换，全部写操作直接拒绝 |

---

## 命令行用法

```bash
fengcode                       # 进入对话
fengcode chat                  # 交互式对话（--simple 使用朴素模式）
fengcode run "任务"            # 单次执行（--json / --quiet / --approve）
fengcode serve                 # 启动服务（--open / --port / --no-mcp）
fengcode doctor                # 环境自检
fengcode version               # 版本信息
fengcode tools                 # 列出全部工具（--group 过滤）

fengcode mcp list|test|start|serve
fengcode skill list|show|search|reload
fengcode plugin list|enable|install|uninstall|reload
fengcode memory list|search|add|rm|stats|reindex
fengcode task list|goal
fengcode workflow list|sample|run
fengcode job list|add|rm
fengcode config show|set|get|path|provider|test
```

命令输出为中文；加 `--json` 可切换为机器可读格式。

---

## 项目结构

```
Fengcode/
├── 启动.bat / 命令行.bat        # Windows 快捷启动
├── start.ps1                   # PowerShell 启动
├── install.sh                  # Linux / macOS
├── pyproject.toml
├── src/fengcode/
│   ├── config/                 # 配置管理（多供应商、预设、能力表）
│   ├── llm/                    # 模型适配层（3 种协议 + 流式）
│   ├── tools/                  # 工具系统（50 个内置）
│   ├── storage/                # SQLite 持久化
│   ├── memory/                 # 分层记忆 + 混合召回 + 压缩
│   ├── skills/                 # 技能系统 + 内置技能
│   ├── plugins/                # 插件系统 + 内置插件
│   ├── mcp/                    # MCP 客户端与服务端
│   ├── security/               # 审批门 / 路径守卫 / 沙箱
│   ├── agents/                 # 子智能体
│   ├── core/                   # 编排内核 + 提示词
│   ├── workflow/               # 工作流 DAG
│   ├── scheduler/              # cron 调度
│   ├── server/                 # HTTP + WebSocket + Web 界面
│   └── cli/                    # 命令行（TUI + REPL）
├── scripts/                    # 验证脚本（冒烟 / 端到端 / 各子系统）
└── tests/
```

**数据目录**：默认位于项目下的 `.fengcode/`（可通过环境变量 `FENGCODE_HOME` 修改）。
包含 `config/config.toml`、`data/fengcode.db`、`workspace/`、`logs/`。

---

## 常见问题

**Q：启动报「没有配置模型」**
进入「设置 → 模型服务」添加，或执行 `fengcode config provider add` 导入已有配置。

**Q：找不到 `fengcode` 命令**
`python -m pip install -e .` 未成功，或 Scripts 目录不在 PATH 中。可用绝对路径调用：
`python -m fengcode.cli.main <子命令>`。

**Q：端口 7845 被占用**
执行 `fengcode serve --port 9000`，或在设置中修改。

**Q：PowerShell 报「禁止运行脚本」**
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
或直接使用 `启动.bat`。

**Q：执行命令时报权限错误**
默认仅允许在**工作区目录**内读写。可在「设置 → 安全」中把目标目录加入白名单。

**Q：安装 playwright 失败**
它是**可选**依赖，仅在抓取需要 JS 渲染的页面时使用，不安装也可正常运行。

**Q：控制台中文乱码**
Windows 控制台执行 `chcp 65001`；启动脚本已自动处理。

**Q：密钥安全**
密钥仅保存在本机 `config.toml`（或指定的 `.env`），不会上传。
也可将 `api_key_env` 填为变量名（如 `DEEPSEEK_API_KEY`），使密钥不落盘。

---

## 参与开发

```bash
python -m pip install -e ".[dev]"

python scripts/smoke.py           # 冒烟测试
python scripts/check_agent.py     # 端到端（需真实模型）
python scripts/check_server.py    # 服务端
python scripts/check_mcp.py       # MCP
python scripts/check_plugins.py   # 插件
python scripts/check_improve.py   # 边界场景

python -m pytest tests/ -v        # 单元测试
```

欢迎提交 Issue 与 PR。提交前请执行相关验证脚本。

---

## 许可证

[MIT](LICENSE)
