# Fengcode 代码地图

> **给读代码的人（尤其是刚接手这个工程、手上没有上下文的 AI）用。**
> 用途：不用通读全部源码，就能定位「某个功能写在哪、要改该动哪个文件」。
> 读法：先看 §0 的入口，再按需查对应章节。**只记 `文件::符号`，不记行号**——行号随版本漂移，
> 符号名可靠，且有 `scripts/check_code_map.py` 自动对账（符号被改名/删除会报错）。
>
> 工程根 `D:\Fengcode`。**改功能只改 `cli/`**；`desktop/` 只是 Electron 外壳（无业务逻辑）。
> 源码规模：约 77 个源码文件 / 3.9 万行。其中 `server/static/app.js` 单文件约 8500 行——
> **不要整文件通读**，按下面的表定位到函数再读。

---

## 0. 先读这些（新窗口上手顺序）

| 顺序 | 看什么 | 为什么 |
|---|---|---|
| 1 | 本文件 §1「改代码先知道的三件事」 | 避免踩已知的架构约束 |
| 2 | `AGENTS.md`（仓库根） | 项目硬性约定（事件委托、配置字段一致性、验证链） |
| 3 | `docs/architecture.md` | 数据流：一次对话从输入到落库经过哪些层 |
| 4 | `CONTRIBUTING.md` | 怎么跑、改动面 → 该跑哪些检查 |
| 5 | 本文件对应章节 | 定位到具体函数 |

**信息获取工具顺序**（提示词里也这么规定）：`list_dir`/`glob` 看有什么 → `code_map` 看结构 → `grep` 精确定位 → 最后才整文件读。

---

## 1. 改代码先知道的三件事

**① 前端是三个文件，没有构建步骤。**
`cli/src/fengcode/server/static/` 下只有 `index.html`（结构）、`app.css`（样式）、`app.js`（全部逻辑）。
改完必须有语法检查：`python scripts/check_js_syntax.py`。

**② 前端事件必须用事件委托。**
设置中心是「搬真实 DOM 节点」渲染的（`replaceChildren`），直接给节点绑 `onclick` 会绑到副本上、点了没反应。
用 `document.addEventListener` + `closest()`。

**③ 提示词前缀必须逐字节稳定（缓存机制）。**
`prompt cache` 是「从头逐字节比对」，前缀一变、后面全部重新计费。
所以 `core/prompts.py::build_system_prompt` 里：**静态块集中在前，动态块按「变化频率从慢到快」排在后面**；
时间戳/随机数/会话 id 一律不许进前缀（当前时间由 `agent.build_messages` 作尾部消息注入）。
`tests/test_prompt_stability.py` 锁死了这条规则——改动后它必须继续通过。

> ★ 注：代码注释里有「命中后输入便宜 ~90%」的说法，那是早先写的**无出处的约数**，
> 不要当依据。真实价格由你配置的价目表决定（见 §7「用量与计费」），代码里没有任何折扣系数。

---

## 2. 一次对话的完整链路（最重要的一节）

```
浏览器输入
  └─ app.js::send()                        组装请求、插占位气泡、开 SSE
       └─ POST /api/chat                   server/app.py::api_chat
            └─ Agent.run()                 core/agent.py::run      ← 回合主循环
                 ├─ build_messages()       core/agent.py::build_messages
                 │    └─ build_system_prompt()   core/prompts.py   ← 系统提示词在这组装
                 ├─ LLM 请求                llm/router.py::LLMClient
                 ├─ 流式事件 → 事件总线      events.py::EventBus
                 └─ 工具执行                tools/base.py::ToolRegistry.execute
                     └─ 审批门              security/approval.py::ApprovalGate
  └─ 事件回到浏览器
       └─ app.js::handleEvent()             按事件类型渲染（见 §3）
```

**关键点**：前端收到的是**事件流**，不是完整回复。所以「界面不对」既可能是渲染问题、也可能是后端没发对事件——
先查是「后端没发」还是「前端没渲染」（§3 有分工表）。

---

## 3. 消息与界面渲染（前端，改动最频繁）

文件：`cli/src/fengcode/server/static/app.js`

### 3.1 消息怎么渲染

| 功能 | 符号 | 说明 |
|---|---|---|
| Markdown → HTML | `md()` | **手写实现，不引第三方库**。改渲染规则改这里 |
| 新增一条消息气泡 | `addMessage()` | 所有气泡（用户/AI/系统）都走它；带 `data-user-msg` 的是轮次锚点 |
| **消息容器（每会话一个）** | `msgBox()` / `paneOf()` / `setPane()` / `dropPane()` | ★ 见 3.5；`msgBox()` 返回**当前上下文会话**的 pane，不是 `#messages` |
| 会话作用域查询 | `paneQ()` / `paneQA()` | 查「本会话」的消息用它们；直接写 `$("#messages …")` 会数进隐藏会话的消息 |
| 从库里重画历史 | `renderStored()` | 切换会话、重启后走这条；**不经过 `handleEvent`** |
| 事件总入口 | `handleEvent()` | 流式事件的唯一分发点，见下表 |
| 清空消息区 | `clearMessages()` | 切会话/新建时调用；会一并恢复未答复的审批卡与提问卡 |

### 3.2 `handleEvent` 里的分支（按事件类型）

| 事件 | 干什么 | 注意 |
|---|---|---|
| `case "text"` | 正文流式渲染 | 写入走 `flushStream()` 同步落盘，防正文丢失 |
| `case "reasoning"` | 思考块（`<details>`）分段落块 | 「一回合思考→工具→再思考」必须新开块，见 `markReasoningDone()` |
| `case "tool.start"` | 工具卡片**就地插入** | 靠「先封存当前文字气泡」实现「文字→工具卡→后续文字」的顺序 |
| `case "tool.end"` | 回填工具卡结果 | 带真实绝对路径（界面要能「打开位置」） |
| `case "tool_delta"` | 参数生成中的占位 | ★ 事件名是**下划线**，后端 `events.py::Ev.TOOL_DELTA` 必须同名 |
| `case "usage"` | 刷新读数 | ★ 判据用 `usageSnapshotHasData()`，见 §6 |
| `case "approval.request"` | 弹审批确认卡 | `showApproval()`，按请求 id 幂等 |
| `case "ask.user"` | 弹 AI 提问卡 | `showAsk()`，按 id 幂等；★ 与审批卡一样画在**对话流里**（宽度跟随外层 `.msg-wrap`，不要自带 max-width），并登记进 `pendingAsks` 以便重建后恢复。★ 选项为「先选中、可补充、再提交」；「不回答，你自己决定」是与「提交」同行的实体按钮（回填明确说明而非空值） |
| `case "ask.done"` | 收尾提问卡 | ★ 已答复的卡**保留成记录**（`freezeAskCard()` 已就地冻结，底部一行「已答复：…」），只清 `pendingAsks` 登记；未冻结的才 `removeAskCard()` |
| `case "workspace.queue"` | 刷新工作区排队提示 | `renderWsLock()`；SSE 与 WS 两条通道都接 |
| `case "result"` | 回合收尾 | 渲染定稿文字、回执卡、刷新待办与读数 |

### 3.3 轮次折叠与工具卡归并

| 功能 | 符号 |
|---|---|
| 折叠/展开一轮 | `toggleTurn()` → `setTurnCollapsed()` |
| 找某节点所属轮次 | `turnAnchorOf()` / `lastTurnWrap()` |
| 连续同类工具卡折叠成组 | `_groupToolCard()` / `_refreshToolGroupBar()` / `_ungroupIfFailed()` |
| 回合收尾复位气泡 | `finalizeTurnBubbles()` |

★ 待确认的审批卡（`.msg-wrap[data-approval]`）**不参与折叠**——它要用户立刻处理，被收起等于把请求藏了。

### 3.4 界面其他部分

| 功能 | 符号 |
|---|---|
| 页面路由 / 页面注册表 | `go()` / `PAGES` |
| 通用下拉菜单（权限/模式/模型共用） | `openChoiceMenu()` / `openChoiceMenuAt()` |
| 权限档位 | `setPerm()` / `PERMS` / `paintPermChip()` |
| 待办面板 | `renderTodoPanel()` / `refreshTodos()` / `syncTodoBlockVar()` |
| 待办面板的占位高度 | `syncTodoBlockVar()` —— 写 `#chat-page` 的 `--todo-block-h`，叠加进 `#messages` 的底部留白（缺它就复现「滚到底仍被面板挡住」），并在高度变化时把贴底的视图收敛回底部 |
| 右侧信息栏 | `renderInfoPanel()` |
| 底部状态栏 | `renderStatusBar()` |
| 设置中心外壳 | `SS`（渲染宿主 `#settings-render`，搬节点用 `replaceChildren`） |
| 输入区 / 拖拽手柄 | `setupComposer()` / `setupResizers()` |
| 指令排队 | `renderQueue()` / `takeBackQueued()` / `sendQueuedNow()` |
| 内置浏览器（右侧栏） | `tools/builtin/browser.py` + `app.js` 的 `IP_TAB` 相关 |
| 主题（配色/图片主题） | `applyTheme()` / `applySkin()` / `applyImageTheme()` / `syncImageThemeDim()` |
| 字号缩放 | `applyFontSize()`（写 `html[data-fs]`，样式全部 `calc(Npx * var(--ui-scale))`） |

### 3.5 ★★ 会话级运行状态与「多对话并行」（改这几块前必读）

同一工作区里可以同时跑多个对话，切走的那条流**继续在后台产出**、切回来能接上。
支撑它的是两层隔离，都在 `app.js` 里：

| 机制 | 符号 | 说明 |
|---|---|---|
| **按会话分桶的运行状态** | `sessDefaults()` / `SESS` / `sessState()` | streaming / 计时 / 速率 / 中止句柄 / 队列 / 本轮用量 / 本轮消息序号，每个对话各一份 |
| 状态访问器 | `S` 上由 `Object.keys(sessDefaults())` 统一挂的 getter/setter | 读 `S.streaming` 即「**当前上下文会话**的这一份」，全站老写法不用改 |
| **当前上下文会话** | `CUR.box` / `activeSid()` | 空 = 跟随界面显示的会话 |
| **切换上下文** | `withBox(sid, fn)` | ★ 回调是 async 时**等它落定**才还原（否则收尾段全在 `await` 之后，会写到别的会话上） |
| 是否眼前这个会话 | `isCurrentSid(sid)` | 全局面板（状态栏 / 右侧栏 / 主题遮罩 / 滚动）只在它为真时才刷新 |
| 每会话消息容器 | `paneOf()` / `setPane()` / `msgBox()` / `paneQ()` / `paneQA()` | 一个 `.msg-pane` 一个对话；隐藏的用 `display:none`，DOM 保留 |
| **滚动宿主** | `scrollHost()` | ★ 永远是 `#messages`，**不是** `msgBox()`（pane 自己不滚动） |
| 回合的显式会话参数 | `send(opts)` / `dispatchNextQueued(sid)` / `syncQueue(sid)` | 后台接力时**不读也不写**当前输入框（会发出/覆盖别人的内容） |

★★ **两个最容易踩的点**：
1. 一个 async 函数里若跨越 `await`，状态读写必须**钉在发起时的会话**上（`const T = sessState(sid)`），
   不能靠 `S.*` 访问器 —— 用户中途切走后 `S.*` 会解析到**新**会话。
2. `handleEvent()` 入口会用 `withBox` 把事件划进**它自己那条流**的上下文；它不再是「切走即丢弃」。

---

## 4. 滚动怎么跟随（★ 有唯一入口，别在别处改）

文件：`app.js`

| 功能 | 符号 | 说明 |
|---|---|---|
| 跟随标志 | `FOLLOW_TAIL` | 模块级变量，决定新内容是否把视图拉到底 |
| 贴底 / 停止跟随 | `scrollDown()` | 只在 `force \|\| FOLLOW_TAIL` 时贴底 |
| **恢复跟随（唯一入口）** | `updateFollowTailByPosition()` | ★ 只挂在 `#messages` 的**真实 scroll 事件**上 |
| 按距底判断离开底部 | `updateJumpBottom()` | ★ **只单向置 false**，绝不反向恢复 |
| 用户主动上滑检测 | `watchUserScroll()` | ★ 内层滚动区（`.rc` / `.tbody pre`）还有余量时**不改外层状态** |
| 回到底部 | `jumpToBottom()` / `updateJumpBottom()` 的按钮显隐 |

**为什么这么设计**（踩过的坑，别改回去）：
- **恢复跟随不能读「滚动生效前」的位置**：滚轮事件触发时浏览器还没应用 `scrollTop`，
  按此刻的距底判断会把「刚上滑一格」误判成「还在底部」，于是每次流式新增都把视图拽回底——表现就是「滚轮锁死」。
  所以恢复跟随只认真实 scroll 事件（那时距底才是真值）。
- **内层滚动区要挡住冒泡**：工具详情 `<pre>`、思考区 `.rc` 自己能滚，滚轮事件仍会冒泡到消息区，
  不挡住就会把「在内层往上翻」当成「外层停止跟随」。

思考区（`.reasoning` 里的 `.rc`）另有自己的 `rc._followTail`，由 `document` 级 capture 的 scroll 监听维护（scroll 不冒泡）。

---

## 5. 后端：核心编排

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| **回合主循环** | `core/agent.py::Agent.run()` | 一次对话的全部逻辑：调模型 → 执行工具 → 循环 → 收尾 |
| 组装请求消息 | `core/agent.py::Agent.build_messages()` | 系统提示 + 历史 + 尾部动态（时间提醒） |
| **组装系统提示词** | `core/prompts.py::build_system_prompt()` | ★ 静态块在前、动态块按变化频率在后（见 §1③） |
| 尾部交付自检 / 收尾要求 | `core/prompts.py::SUBMIT_CHECKLIST` | 「做完要说清改了啥」由它驱动 |
| 子智能体 | `agents/runner.py::SubAgentRunner` | 主 Agent 派子任务（看不到主对话历史） |
| 假工具调用兜底解析 | `core/agent.py::_extract_fake_tool_calls()` | 模型把工具调用写在正文里时的补救 |
| 重复输出检测 | `core/agent.py::_count_runaway_repeat()` | 模型陷入复读时截断 |

---

## 6. 读数：上下文 / 命中率 / 用量（★ 有唯一判据）

★ **改这里前必读**：这套读数前后出过**四个独立根因**（显示层、数据层都有），排查方法是**先查库**。

| 功能 | 文件::符号 |
|---|---|
| **「有没有数据」唯一判据（两端）** | 前端 `app.js::usageSnapshotHasData()` ＝ 后端 `llm/types.py::Usage.has_data()` |
| 命中率计算 | `app.js::hitRatePct()` |
| 右侧信息栏渲染 | `app.js::renderInfoPanel()` |
| 底部状态栏渲染 | `app.js::renderStatusBar()` |
| 上下文上限显示（三来源判定） | `app.js::applyContextLimit()` |
| 切会话恢复读数 | `app.js::applySessionUsage()` |
| 换模型重算上限 | `app.js::onModelPicked()` |
| 后端广播读数 | `core/agent.py::run()` 里的 `Ev.USAGE`（**收尾时无条件补发一次**） |
| 用量落库 | `storage/stats.py::StatsStore` |
| **单次调用耗时/会话跨度** | `stats.py::StatsStore.summary()` 的 `span` = 最后一次 − 第一次调用（★ `duration` 是各次耗时之和，**别拿它当「运行时间」**） |
| 会话读数快照 | `storage/sessions.py`（`meta.last_usage`） |
| 缓存未命中归因 | `llm/router.py::CacheDiagnostics` |

★★ **铁律**：判「有没有数据」**不能**写 `if obj:`（dataclass 无 `__bool__`），**也不能**写「字段是否存在」（全 0 快照里字段是存在的）。
**一律用「数值之和 > 0」的显式函数，且前后端同口径。**
★ **「切会话再切回来就正常」是重要线索**：那条路走 `applySessionUsage`，**不经过 `handleEvent`**。

---

## 7. 用量与计费

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| **算钱** | `llm/router.py::compute_cost()` | 按价目表算；**无缓存折扣系数**——`cache_hit` 为空时退回用 `input` 单价 |
| 峰谷价 | `llm/router.py::_in_peak_hours()` / `price_phase()` | 只有价目表填了谷价才生效 |
| 价目表结构 | `config/schema.py::Price` | `input` / `output` / `cache_hit` / `off_peak_*` / `unit` / `currency` |
| 用量类型 | `llm/types.py::Usage` | `prompt_tokens` / `cached_tokens` / `completion_tokens` |
| **按模型汇总** | `storage/stats.py::StatsStore.by_model()` | ★ 带 `HAVING` 过滤：token 与费用全为 0 的条目（失败调用）**不列出**，与前端 `renderUsageBreakdown()` 同口径 |
| 用量占比展示 | `app.js::renderUsageBreakdown()` | 占比不足 1% 显示「<1%」而非四舍五入成 0%（否则「用过但很少」看起来像「没用过」） |

---

## 8. 工具系统

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| **工具基类与注册表** | `tools/base.py::Tool` / `ToolRegistry` | 每个工具声明名称/说明/JSON Schema/处理函数 |
| **执行入口（含审批）** | `tools/base.py::ToolRegistry.execute()` | 审批 → 执行 → 审计。★ 审批事件只由审批门发，**这里不要再发** |
| 工具上下文 | `tools/base.py::ToolContext` | 运行期依赖（工作区/沙箱/审批/事件/记忆…） |
| 注册内置工具 | `tools/__init__.py::register_builtin()` | |
| 工具分组 | `tools/__init__.py::tool_groups()` | 设置页按组分列 |

**内置工具文件对照**（`tools/builtin/`）：

| 文件 | 提供什么工具 |
|---|---|
| `files.py` | 读/写/改文件、`list_dir`、`glob`、`grep`、文件操作、备份 |
| `shell.py` | `shell`、`python`、`run_script`、装包、后台任务、进程 |
| `design.py` | **`code_map`（生成代码结构地图，AST 精确）**、`run_tests`、`lint`、依赖图 |
| `assist.py` | 记忆、待办、目标、技能、子智能体、计划、`ask_user`、上下文预算、查内置文档 |
| `web.py` | 搜索（多引擎）、抓网页、HTTP 请求、下载 |
| `documents.py` | PDF/Word/Excel/PPT/CSV/图片解析、生成 Office 文件、画图 |
| `automation.py` | 截图、模拟输入、窗口、剪贴板、通知、系统信息 |
| `browser.py` | 内置浏览器（右侧栏）：打开/读取正文/截图/操作 |
| `archive.py` | 压缩包读/解/建 |
| `database.py` | SQLite 查询、数据分析 |
| `git_tools.py` | git 操作（本机无 git 时的降级见文件内 `find_git()`） |
| `remote.py` | SSH / SFTP |

★ **`code_map` 是理解陌生文件结构的最快手段**（比整文件读省得多），提示词里已列为第 2 步。

---

## 9. 安全与审批

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| **审批门** | `security/approval.py::ApprovalGate` | `evaluate()` 判定三档：allow / ask / deny |
| **「始终允许」记忆键** | `security/approval.py::ApprovalGate._memory_key()` | ★ 按「真正要干的事」记忆，见下 |
| **命令效果分类** | `security/paths.py::classify_command()` | read / write / repo（repo 类每次都问，不复用记忆） |
| **取首个有效程序名** | `security/paths.py::effective_program()` | ★ 跳过 `cd`/`pushd`/`set-location` 等纯导航段 |
| 危险命令模式表 | `security/paths.py::scan_dangerous()` + `DANGEROUS_SHELL_PATTERNS` | |
| 路径守卫 | `security/paths.py::PathGuard` | 读写白名单、符号链接逃逸、拒止模式 |
| 审批规则校验 | `security/approval.py::validate_rules()` | 规则写错会静默失效，所以有校验 |
| 沙箱 | `security/sandbox.py::LocalSandbox` | 子进程执行、超时、编码处理 |
| 权限档位 | `config/schema.py::PermissionsConfig` | `mode`：allow / ask / deny |

★★ **踩过的坑**：记忆键原来看命令的**第一个词**。模型爱写 `cd "<工作区>"` 换行再接真操作，
于是「始终允许」被记成 `cd` = 放行**整类**；同理 `cd X && git restore .` 会被判成「未知操作」**直接放行**，
绕过「改仓库状态每次都问」。修法：两处统一用 `effective_program()`。

---

## 10. 存储（SQLite）

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| 数据库连接 | `storage/db.py::Database` / `get_db()` | FTS 全文检索分词在 `tokenize_for_fts()` |
| 会话与消息 | `storage/sessions.py::SessionStore` | 会话历史、`meta.last_usage` 快照 |
| 统计与审计 | `storage/stats.py::StatsStore` / `AuditStore` | 用量日志、审批审计 |
| 任务/目标/工作流/定时/插件/技能/附件 | `storage/tasks.py`（多个 Store） | 统一放这一个文件 |
| 工作区 | `storage/workspaces.py::WorkspaceStore` | 名字 + 目录；★ 会话表里的 `workspace` 存的就是它的**路径** |
| **会话 → 工作目录** | `server/app.py::AppState.session_workspace()` | 会话记录的路径（或按项目名回查目录）；建 Agent 时按它决定工作目录 |
| **切换会话工作目录** | `core/agent.py::Agent.set_workspace()` | ★ 目录 / 路径守卫 / 审批门的 guard / 沙箱 cwd **必须一起换**，并写回会话记录 |

★ **复制 SQLite 库必须连 `-wal` / `-shm` 一起复制**，否则会看到「少了一些会话」的假象。

---

## 11. 记忆与上下文压缩

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| 记忆管理 | `memory/manager.py::MemoryManager` | 分层记忆、召回、重要性/新鲜度 |
| **记忆注入提示词** | `memory/manager.py::context_block()` | 生成 `<memory>` 块；★ 单条字数与整段预算读配置 `recall_body_chars` / `recall_budget_tokens`（两者要一起调，否则放宽的单条会吃光预算） |
| 记忆条目正文 | `app.js::memoBodyHtml()` | 设置页与记忆页共用。★ 长记忆外面只留**一行摘要**（优先 `description`），全文整体收进 `<details>`——旧写法把预览放在 details 外面，展开后成了「预览 → 展开全文 → 完整正文」三段；短记忆直接全显 |
| 向量与相似度 | `memory/embedding.py` | `hashing_embed()` / `cosine()` |
| **压缩切点** | `memory/summarizer.py::split_for_compaction()` | ★ 配对保护（工具调用与结果不许拆开） |
| 压缩触发判定 | `memory/summarizer.py::should_compact()` / `detect_trigger()` | ★ 实际触发点是 **16 万 token**（`context_soft_limit_tokens`），不是窗口大小 |
| 分块摘要 | `memory/summarizer.py::summarize_chunked()` | 长历史分块摘要 |
| 摘要净收益校验 | `memory/summarizer.py::summary_is_smaller()` / `should_proceed()` | |

---

## 12. LLM 层

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| 统一客户端 | `llm/router.py::LLMClient` | 重试、降级、计价 |
| 模型选择 | `llm/router.py::model_choices()` | 返回上下文窗口**与来源**（model/provider/catalog/fallback） |
| OpenAI 兼容 | `llm/openai_client.py::OpenAIClient` | 含 Responses API 变体 |
| Anthropic | `llm/anthropic_client.py::AnthropicClient` | 缓存字段 `cache_read_input_tokens` |
| Gemini | `llm/gemini_client.py::GeminiClient` | 系统提示走 `systemInstruction` |
| 消息/工具/用量类型 | `llm/types.py` | `Message` / `ToolSpec` / `Usage` / `StreamEvent` |
| 角色规范化 | `llm/types.py::normalize_roles()` | ★ 动态提示不能以 system 插在对话中间 |
| 断线分层诊断 | `llm/base.py::diagnose_error()` | 区分 DNS / 拒连 / 超时 / 半途断 |
| 模型能力目录 | `config/catalog.py::lookup()` | 出厂兜底值（**不是**真实窗口） |

★ **上下文窗口判定顺序必须是 `model → provider → catalog → fallback`**，漏判 `model` 会让「填了逐模型窗口却显示未限制」。

---

## 13. 服务端与接口

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| 应用装配 / 路由表 | `server/app.py::create_app()` | 所有 `/api/*` 在这里注册 |
| **对话接口（SSE）** | `server/app.py::api_chat()` | 订阅事件总线，逐条 `_sse()` 下发 |
| 会话写租约 | `server/app.py::AppState.acquire_session()` | 同一会话禁止两轮并发写 |
| **工作区写租约（跨会话）** | `server/app.py::AppState.acquire_workspace()` / `release_workspace()` / `release_workspace_holder()` | ★ 同一**目录**只允许一个对话在**写**，其余 **FIFO 排队**，前一个跑完直接转交（不靠抢占）；键由 `workspace_key()` 归一化目录得到。★ 租约是**按需获取**的：回合开头不抢，只在真要写工作区时才取（见下条） |
| **工作区写租约的按需门** | `server/app.py::AppState.make_workspace_gate()` + `core/agent.py::Agent._needs_workspace_lease()` / `_ensure_workspace_lease()` | ★★ 纯问答/纯读**不排队**；只有本步出现「会改工作区文件」的工具调用时才等锁。判据见 `tools/base.py::Tool.touches_workspace`（显式声明，None 时按 `read_only` 保守推断） |
| 工作区占用/排队状态 | `AppState.workspace_queue_state()` / `workspace_busy()` / `cancel_workspace_waiter()` | 关会话（`drop_agent()`）也会归还锁与队位 |
| **排队状态查询接口** | `server/app.py::api_workspace_lock()` | `GET /api/workspace-lock`：界面切回会话时**拉**一次（事件只在变化时推） |
| 审批回填 | `server/app.py::api_approval()` | 界面点按钮后回填 |
| 审批待确认列表 | `server/app.py::api_pending_approvals()` | |
| AI 提问回填 | `server/app.py::api_ask()` | 界面点选项/输入答复后回填 `ask_user` 的等待 |
| WebSocket 事件 | `server/app.py::ws_events()` | ★ 与 SSE 是**两条独立通道**，同一条事件会各送一份 |
| 静态页面 | `server/app.py::index()` | 托管 `static/index.html` |
| 指令文件列举 | `server/app.py::api_instruction_files()` | 列出工作区里的约定文件位置 |
| 排队事件 | `events.py::Ev.WORKSPACE_QUEUE` | 排队状态变化时按**排队者自己的** session_id 下发（只有它的界面收得到）；SSE 与 WS 两条通道都接 |
| **账号代理接口** | `server/app.py::api_account()` | `GET /api/account` 取本地快照（不发网络）；`POST` 的 `action` 为 `login` / `refresh` / `logout` / `prompt_done` / `bind` / `models` / `logs`。★ 走服务端代理的原因：站点的用户接口不返跨域头（**预检有头、真实响应没有**），界面直连拿不到数据；顺带让凭证不出后端 |
| **账号绑定（一键接入模型）** | `server/app.py::_account_bind()` | 未登录直接拒绝 → `ensure_key()` 拿密钥 → `user_models()` 取模型 → 写进**单独一条**「万象账号」供应商（默认 `wanxiang-account`，**不碰**用户手填的万象预设）→ 带思考参数 → 只写用户勾选启用的模型 |
| **账号首屏快照** | `server/app.py::_account_snapshot()` | 供 `/api/bootstrap` 的 `account` 字段；只读本地、任何异常退回未登录，不拖慢首屏 |
| **账号核心** | `core/account.py::AccountManager` | `login()` / `self()` / `logout()` / `snapshot()` / `ensure_key()` / `user_models()` / `usage_logs()`；凭证存 `config/account.json`（仅服务端可读），密码不落盘，存 30 天 refresh 凭证并按需换 15 分钟的 access token |
| **确保密钥** | `core/account.py::AccountManager.ensure_key()` | 先按名字复用已有密钥，没有才建；★ 建完**必须回查列表**才能拿 id（建密钥接口只返成功、不返 key 也不返 id），再凭 id 换取明文；设 `unlimited_quota=True` 使钱从**账号余额**扣 |

---

## 14. 配置

| 功能 | 文件::符号 | 说明 |
|---|---|---|
| **配置结构（唯一真相）** | `config/schema.py`（各类 `_Base`） | 前端提交的字段必须在这里存在，否则被静默丢弃 |
| 读取与迁移 | `config/manager.py::ConfigManager` | ★ 含 `_migrate_legacy_defaults()`：改默认值时必须迁移用户配置里的旧值 |
| 默认配置 | `config/manager.py::default_config()` | |
| 从 .env 导入 | `config/manager.py::parse_env_file()` | |
| **记忆召回上限** | `config/schema.py::MemoryConfig` 的 `recall_body_chars` / `recall_budget_tokens` | 单条注入字数（默认 1200）与整段 token 预算（默认 3200）；★ 两者必须一起调 |
| **账号（可选账号源）** | `config/schema.py::AccountConfig` | `base_url`（账号站点地址）/ `prompt_done`（问过「要不要登录」没有）/ `provider_name`（账号绑定写进哪个供应商，默认 `wanxiang-account`）/ `key_name`（静默创建的密钥在站点上叫什么）。★ 登录后绑定的那套模型能力只在「账号」页提供 —— 它要用登录态换来的密钥 |

★ **改默认值时必须同步迁移用户已写入的旧值**（已踩过两次：`max_tokens`、`max_steps`），否则用户升级了仍跑旧行为。
★ 改完跑 `python scripts/check_config_fields.py`（抓前后端字段不一致）。

---

## 15. 其余模块（改动不频繁，粗粒度即可）

| 模块 | 文件 | 干什么 |
|---|---|---|
| 技能 | `skills/manager.py` | 加载 `skills/` 下的技能（frontmatter 解析在 `parse_frontmatter()`） |
| 插件 | `plugins/manager.py` | 第三方工具扩展；`PluginTool` 包装成普通工具 |
| MCP 客户端 | `mcp/client.py` | 连接外部 MCP 服务器（stdio / HTTP），`MCPManager` |
| MCP 服务端 | `mcp/server.py` | 把 Fengcode 自己当 MCP 服务器暴露出去 |
| 定时任务 | `scheduler/cron.py` | cron 表达式解析（`parse_cron()`）与调度器 |
| 工作流 | `workflow/engine.py` | DAG 编排（`WorkflowRunner`、拓扑分层 `topo_layers()`） |
| 命令行 | `cli/main.py` | Typer 命令入口（`serve` / `chat` / `doctor` / `mcp` / `skill`…） |
| 交互式 REPL | `cli/repl.py` | 纯命令行对话 |
| 终端 UI | `cli/tui.py` | Textual 全屏界面 |
| 数据目录 | `paths.py` | `home()` = 数据根（`FENGCODE_HOME` > 仓库 `.fengcode` > `~/.fengcode`） |
| 工具函数 | `utils/__init__.py` | 时间/大小/token 格式化、`truncate()`、`read_text()`、`iter_files()` |
| TOML 读写 | `utils/toml.py` | 自己实现的 TOML 输出（**改 config.toml 用 python，别用 PowerShell**，中文会乱码） |

**Electron 外壳**（`desktop/`，无业务逻辑）：`main.js` 拉后端、托盘、快捷键、右键菜单、窗口控制。

★★ **外壳日志的硬约束（1.2.26 事故后定的，别改回去）**：`log()` 写 stdout 必须自己兜住异常，崩溃处理器必须防重入。
踩过的坑：桌面端是用管道拉起的，stdout 一旦断开（EPIPE）写它就**抛异常**；而 `uncaughtException` 处理器又调 `log()` 去记录这个异常 → 「写日志抛错 → 处理器记日志 → 又抛错」**无限递归**，每轮往 `desktop.log` 追加一段堆栈。
实测后果：日志涨到 **7.7 GB**，主进程与磁盘 IO 被占满，**窗口点不动也关不掉**（后端本身是健康的）。
三条防线：① `log()` 里 `console.log` 包 `try/catch`；② `logCrash()` 用一次性标记防重入 + 兜底 try；③ 日志轮转（`LOG_MAX_BYTES` 8MB × `LOG_KEEP` 3 份）。
★ 回归防线：`desktop/tests/test_log_guard.cjs`（从 `main.js` 取真实实现来跑）。
★ 诊断线索：主进程 CPU 持续 ≈100% 单核而渲染进程/后端接近 0 → 先看 `%APPDATA%\fengcode-desktop\logs\desktop.log` 的大小与末尾内容。

---

## 16. 版本号与发布（改完要发版时）

| 事项 | 位置 |
|---|---|
| **版本号（三处必须同步）** | `cli/pyproject.toml` + `cli/src/fengcode/version.py` + `desktop/package.json` |
| 后端打包 | `cd cli && python -m PyInstaller fengcode.spec --noconfirm --clean` |
| 外壳打包 | `cd desktop && npm run dist`（★ **两个都要跑**，只跑前者改不到用户手上的版本） |
| 整理交付目录 | `python scripts/deliver.py --apply` |
| 更新私人实例 | `python scripts/update_private.py --apply`（★ 先停实例） |
| 推送 / 发版 | `scripts/push_github.py` / `scripts/release.py`（本机无 git，走 REST API；**必须后台跑**） |

**验证链**（改动面 → 该跑什么）：
```
python scripts/check_js_syntax.py      # 改了前端（必跑）
python scripts/check_refs.py           # DOM id 引用完整性
python scripts/check_frontend_refs.py  # 接口/图标/面板一致性
python scripts/check_config_fields.py  # 前后端配置字段
python scripts/check_code_map.py       # 本文件里的符号是否还存在
python -m pytest tests/ -q             # 单元测试
```

---

## 17. 这张地图怎么维护

- 地图只记 **`文件::符号`**，不记行号（行号会漂移）。
- 改代码导致符号改名/删除后，跑 `python scripts/check_code_map.py` 会指出对不上的条目。
- **加了重要功能就补一行**；不重要的模块保持粗粒度即可。

### ★★ 每版必做（与发版固定动作同步）

> 要求：**每版改动都要反映到地图里**；地图里没有的新功能，**直接新建条目**。

| 这版做了什么 | 地图怎么改 |
|---|---|
| 函数/类改名、挪了文件 | 更新对应条目的 `文件::符号` |
| 功能删了 | 删掉那条 |
| **新增了功能** | **直接新建条目**（放到对应章节；重要的写细，次要的写粗） |
| 只改实现、位置没变 | 不用改（对账脚本会确认） |

**收尾动作**：`cd D:\Fengcode\cli && python scripts/check_code_map.py` —— 符号对不上会指名报错。

★ **不把「每版改了啥」的历史写进这里**——那是 `v<版本>.md` 的职责。
本文件只回答「**现在**这个功能在哪个函数」，保持它是能直接查的索引，不是变更日志。
