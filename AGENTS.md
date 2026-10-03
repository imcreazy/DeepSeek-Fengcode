# Fengcode

给 AI 编码助手读的项目约定。人看的文档在 [README.md](README.md) 和 [docs/](docs/)。

## 这是什么

本地运行的中文 AI Agent。两个形态：

- `cli/` —— Python 后端 + 网页界面 + 命令行（**业务逻辑全在这**）
- `desktop/` —— Electron 窗口外壳（不含业务逻辑，只启动后端并加载界面）

## 改代码前必读

- 架构与数据流：[docs/architecture.md](docs/architecture.md)
- 怎么跑、怎么验证：[CONTRIBUTING.md](CONTRIBUTING.md)

## 硬性约定

1. **前端事件必须用事件委托**。设置中心是「复制面板 innerHTML」渲染的，
   直接绑节点会绑到副本上、点了没反应。用 `document.addEventListener` + `closest()`。
   同类坑已踩过多次（规则增删、沙箱探测、保存按钮、恢复助手）。

2. **前端提交的配置字段必须在 schema 里存在**。前端提交了但后端
   `config/schema.py` 没有的字段会被静默丢弃 —— 用户改了却不生效。
   改完跑 `python scripts/check_config_fields.py`。

3. **前端调用的接口必须真实存在**。改完跑 `python scripts/check_frontend_refs.py`。
   它还会检查图标是否定义、设置导航的 page/tab 是否有对应面板。

4. **不做没有验证的声称**。测试没过就说没过，不要用「应该可以」掩盖。

5. **注释写「为什么」，不写「是什么」**。不要复述代码。

## 常用命令

```bash
cd cli

python -m pytest tests/ -q                # 单元测试
python scripts/smoke.py                   # 端到端冒烟
python scripts/check_js_syntax.py         # 前端内联脚本语法
python scripts/check_refs.py              # DOM id 引用完整性
python scripts/check_frontend_refs.py     # 接口/图标/面板一致性
python scripts/check_config_fields.py     # 前后端配置字段一致性
python scripts/check_settings_sweep.py    # 逐个点开所有设置分类（需服务在跑）
```

## 目录速查

| 路径 | 内容 |
|---|---|
| `cli/src/fengcode/core/` | 编排内核、系统提示词 |
| `cli/src/fengcode/tools/` | 工具系统（注册表 + 内置工具） |
| `cli/src/fengcode/security/` | 审批门、路径守卫、沙箱 |
| `cli/src/fengcode/storage/` | SQLite 持久化（会话/工作区/任务） |
| `cli/src/fengcode/memory/` | 分层记忆与召回 |
| `cli/src/fengcode/server/` | HTTP 路由 + 单文件前端 |
| `cli/src/fengcode/server/static/index.html` | 全部界面（无构建步骤） |
| `desktop/main.js` | Electron 主进程 |
