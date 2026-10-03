# 安全说明

[English](#english) | 中文

## 请先阅读

Fengcode 是一个**能真正执行操作的 AI Agent**：它会读写你的文件、执行命令、访问网络、
连接远程主机。这带来了真实的能力，也带来了真实的风险。

**它不是沙箱，也不能替代沙箱。**

## 风险来源

| 风险 | 说明 |
|---|---|
| **模型输出错误** | 模型可能写出错误的命令、改错文件、误删数据 |
| **提示注入** | 你让它读取的网页/文档里可能藏有指令，诱导它执行危险操作 |
| **配置错误** | 比如把工作区设成整个磁盘根目录 |
| **第三方扩展** | MCP 服务器与插件是外部代码，不受本项目控制 |
| **依赖漏洞** | 上游库的缺陷会影响本项目的安全性 |

## 内置的防护措施

Fengcode 提供多层防护，但**它们降低风险，不保证消除风险**：

| 机制 | 作用 | 局限 |
|---|---|---|
| **审批门** | 危险命令执行前弹窗确认 | 你点「允许」后它就会执行；判断力在你 |
| **路径白名单** | 写入限定在允许目录内 | 白名单里的内容仍可被改坏 |
| **本地沙箱** | 子进程执行、超时终止、环境变量清理 | 不是容器隔离，不能防恶意代码逃逸 |
| **只读模式** | 一键拒绝所有写操作 | 需要你主动开启 |
| **审计日志** | 记录每次工具调用与审批决定 | 事后追溯，不能事前阻止 |

## 建议的使用方式

1. **授予最小权限**。工作区只指向当前项目目录，不要指向用户主目录或磁盘根。
2. **敏感操作先看再批**。审批弹窗里看清命令内容再点允许——特别是 `rm`、`del`、
   `git push --force`、格式化、改系统配置这类不可逆操作。
3. **重要数据先备份**。任何自动化工具都可能出错。
4. **谨慎对待第三方扩展**。
   - MCP 服务器：确认来源，它是独立进程，能访问你授权的资源
   - 插件：看一眼 `plugin.py` 在做什么，注意 `manifest.yaml` 里的 `permissions` 声明
   - 技能：Markdown 技能是提示词，可能诱导模型做意外的事
5. **不要把它指向不可信的工作负载**。如果让 Agent 处理来源不明的代码/文档，
   优先在**一次性虚拟机或容器**里跑。
6. **别把密钥给它**。它会把敏感信息读进上下文，进而可能进入日志或记忆。
   （本项目已自动清理子进程中的 `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*` 环境变量，
   但这不是万无一失的。）

## 已知的边界

- 沙箱是**进程级**的，不是容器级。它做超时、进程树终止、环境变量清理，
  但不隔离文件系统与网络。
- 网络访问是**全放行**的（除非你关掉 `sandbox.network`），
  Agent 可以访问任何能连通的地址。
- 审批门的判定基于**模式匹配**（正则匹配危险命令）。它认识常见的危险模式，
  但不是完备的——绕过的写法它可能识别不出来。
- 记忆系统会持久化信息。虽然它会自动脱敏明显的密钥格式，
  但**不要主动把密码告诉它**。

## 免责声明

本项目按 MIT 许可证提供，**不附带任何保证**。

在适用法律允许的最大范围内，作者与贡献者不对以下后果承担责任：
计算机损坏、数据丢失或泄露、文件误删、经济损失、
或因使用本软件造成的任何其他损害。

**请在充分理解上述风险的前提下使用。**
如果这些风险无法接受，请不要运行本项目，或仅在隔离环境中使用。

---

## English

Fengcode is an AI agent that **actually executes operations**: it reads and writes your files,
runs shell commands, accesses the network, and connects to remote hosts. This grants real
capability — and real risk.

**It is not a sandbox, and cannot replace one.**

### Risks

- **Model errors** — the model may write wrong commands, modify the wrong file, or delete data
- **Prompt injection** — web pages or documents it reads may contain instructions that induce
  dangerous actions
- **Misconfiguration** — e.g. setting the workspace to an entire drive root
- **Third-party extensions** — MCP servers and plugins are external code, not controlled by this
  project
- **Dependency vulnerabilities** — upstream library flaws affect this project's security

### Built-in safeguards (mitigations, not guarantees)

- **Approval gate** — prompts before dangerous commands (but if you click allow, it runs)
- **Path allowlist** — writes are restricted to permitted directories
- **Local sandbox** — subprocess execution, timeout termination, environment scrubbing
  (process-level, *not* container isolation)
- **Read-only mode** — a single switch that rejects all writes
- **Audit log** — records every tool call and approval decision (after the fact, not preventive)

### Recommended usage

1. Grant least privilege — point the workspace at your project folder, not your home directory.
2. Read the approval dialog before clicking allow, especially for irreversible operations.
3. Back up important data. Any automation can fail.
4. Vet third-party extensions. Check the source of MCP servers; skim `plugin.py` and its declared
   `permissions`.
5. Do not point it at untrusted workloads. Use a disposable VM or container instead.
6. Don't hand it secrets.

### Disclaimer

Provided under the MIT License, **without warranty of any kind**. To the maximum extent permitted
by applicable law, the authors and contributors are not liable for computer damage, data loss or
leakage, deleted files, financial loss, or any other damages arising from use of this software.

**Use it with full understanding of these risks.** If they are unacceptable, do not run this
project, or run it only in an isolated environment.
