<div align="center">

# Fengcode

**本地运行的中文 AI Agent 平台 —— 桌面端 / 命令行 / Web 界面**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Node](https://img.shields.io/badge/Node-20%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![License](https://img.shields.io/badge/License-MIT-2EA44F)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-4493F8)]()
[![中文](https://img.shields.io/badge/%E7%95%8C%E9%9D%A2-%E4%B8%AD%E6%96%87-red)]()

[快速上手](docs/getting-started.md) · [架构说明](docs/architecture.md) · [参与开发](CONTRIBUTING.md) · [安全边界](SECURITY.md)

</div>

---

Fengcode 是一个在本地运行的 AI Agent 平台。它可直接读写文件、执行命令、检索网络、
操作数据库并维护跨会话的长期记忆，也支持把复杂任务分派给多个子智能体并行处理。

> **安全声明**
>
> Fengcode 具备真实执行能力：读写文件、执行命令、访问网络。
> 项目提供审批门、路径白名单、本地沙箱等防护机制，可降低风险但不构成风险消除。
> 请遵循最小权限原则，不要将不可信内容置于其工作目录中。
> 详见 [SECURITY.md](SECURITY.md)。

---

## 目录结构

```
Fengcode/
├── cli/          命令行端 + 本地服务 + Web 界面（Python）
├── desktop/      桌面端（Electron 外壳）
└── docs/         文档
```

桌面端不包含业务逻辑，仅负责启动 `cli` 的后端并加载其界面，因此两者功能始终一致。

---

## 下载

见 [Releases](https://github.com/imcreazy/DeepSeek-Fengcode/releases/latest)：

| 文件 | 说明 |
|---|---|
| `Fengcode-<版本>-setup.exe` | 安装程序，创建快捷方式，可在「应用和功能」中卸载 |
| `Fengcode-<版本>-win64.zip` | 解压即用，无需安装 Python，启动最快 |
| `Fengcode-<版本>-source.zip` | 源码包，需 Python 3.10+ |

三者的功能范围一致，差异仅在部署方式。

---

## 从源码运行

```bash
git clone https://github.com/imcreazy/DeepSeek-Fengcode.git
cd Fengcode

# 命令行端 / Web 界面
cd cli
python -m pip install -e ".[dev]"
fengcode serve --open     # 启动 Web 界面
fengcode chat             # 或进入命令行对话

# 桌面端
cd ../desktop
npm install               # 首次需下载 Electron 运行时
npm start
```

Windows 亦可直接运行 `cli/启动.bat`（自动安装依赖并启动）。

---

## 快速上手

1. 启动后进入 **设置 → 模型服务**
2. 选择厂商预设（如内置的万象 API，地址已预填），填入 API Key
3. 点击「获取模型」，勾选需要的模型并保存
4. 返回「对话」页开始使用

支持任何 **OpenAI 兼容接口**（DeepSeek、智谱、通义、Kimi、豆包、MiMo、MiniMax、
百川、阶跃星辰，以及自建中转站），并原生支持 Anthropic 与 Gemini。
内置 10 家厂商预设，通常只需粘贴 API Key。

API Key 仅保存在本机 `config.toml`，不会上传至任何服务。

完整走查见 [快速上手](docs/getting-started.md)。

---

## 核心特性

### 工作模式

| 状态 | 行为 |
|---|---|
| **自动**（默认） | 由模型依据任务复杂度决定计划或目标策略 |
| **计划模式** | 先输出分步计划，确认后执行 |
| **目标模式** | 围绕长期目标跨轮推进，保留进度 |

计划与目标均为可反选，取消后回到自动模式。

### 外观

- 6 套基础配色：石墨 / 极光 / 板岩 / 松林 / 琥珀 / 玫瑰，与明暗模式正交
- 10 套图片主题：全屏背景，进入对话后自动降低不透明度以保证可读性
- 支持上传自定义背景图（PNG / JPG / WebP）
- 可调项：字体、字号（13–22，五档）、会话宽度、界面动画

### 模型服务

「添加供应商」→ 选择预设（万象 API / DeepSeek / 智谱 / MiMo / 通义 / Kimi / 豆包 /
MiniMax / 百川 / 阶跃星辰）→ 填入 API Key → 「测试并获取模型」→ 勾选保存。
亦可选择「自定义」接入任意 OpenAI 兼容服务。填入 Key 时可一并声明上下文窗口与输出上限。

### 项目（工作区）

左侧栏为「项目 → 会话」两级结构。每个项目绑定独立工作目录，会话与文件归属于该项目。
可通过 **+** 新建项目（目录自动创建），支持重命名、迁移目录与删除；
删除项目不会影响磁盘上的文件。

### 权限与安全

- **三档权限**：只读 / 工作区可写（推荐）/ 完全权限
- **细粒度规则**：`tool:shell`、`cmd:git push`、`path:**/.ssh/*`、`risk:<关键词>`，
  优先级为 `deny > ask > allow`
- **审计日志**：每次工具调用与审批决策落库，密钥自动脱敏
- **沙箱**：子进程执行，超时终止整棵进程树，环境变量最小化

### 记忆

分为**全局**与**项目**两级作用域，采用全文与向量混合召回，跨会话持久。
界面提供四个视图：背景记忆 / 归档记忆 / 指令文件（`AGENTS.md` 等，每轮自动注入）/ 召回记录。

### 工具与扩展

- **50+ 内置工具**：文件读写编辑与搜索、Shell、代码执行、网页抓取、HTTP、
  Git、SQLite、PDF / Office / 图片解析、压缩包、定时任务、进程管理、SSH 远程
- **技能**：Markdown `SKILL.md`（兼容 Claude Skills 规范）与 Python 类技能，内置 9 个中文技能
- **插件**：`manifest.yaml` 声明 + 本地目录加载 + 热启用
- **MCP**：双向支持 —— 连接外部 MCP 服务器，或自身作为 MCP 服务器对外提供能力
- **子智能体**：独立上下文与预算，支持并行扇出、顺序链与辩论

### 恢复助手

位于 **设置 → 高级 → 恢复助手**：可运行诊断（逐项检查数据目录、依赖、数据库、
模型可用性）、备份配置、一键回滚。回滚前会自动备份当前配置。

---

## 命令行参考

```bash
fengcode                       # 进入对话
fengcode chat                  # 交互式对话（--simple 使用朴素模式）
fengcode run "任务"            # 单次执行（--json / --quiet / --approve）
fengcode serve                 # 启动服务（--open / --port / --no-mcp）
fengcode doctor                # 环境自检
fengcode version               # 版本信息
fengcode tools                 # 列出全部工具

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

## 数据目录

| 部署形态 | 位置 |
|---|---|
| 桌面端（安装版） | `%APPDATA%\Fengcode\` 及后端 exe 同级的 `fengcode-data\` |
| 命令行版（exe） | exe 同级的 `fengcode-data\` |
| 源码运行 | 项目目录下的 `.fengcode\` |

其中包含 `config\config.toml`（配置）、`data\fengcode.db`（会话 / 记忆 / 统计）、
`workspace\`（工作区）。可通过环境变量 `FENGCODE_HOME` 修改位置。

---

## 开发

```bash
cd cli

python -m pytest tests/ -q                # 单元测试
python scripts/smoke.py                   # 端到端冒烟测试
python scripts/check_js_syntax.py         # 前端语法检查
python scripts/check_frontend_refs.py     # 检查前端调用但后端未实现的接口
python scripts/check_config_fields.py     # 检查前后端配置字段不一致
python scripts/check_settings_sweep.py    # 遍历全部设置分类
```

后三项一致性检查用于发现「已定义但实际不可用」的功能，修改前端后建议执行。

常用操作亦可通过 `make` 执行：`make test` / `make check` / `make build-cli` / `make build-desktop`。

---

## 许可证

[MIT](LICENSE)

---

<div align="center">
<sub>Fengcode</sub>
</div>
