# 参与开发

感谢愿意一起把它做好。这份文档说明怎么把项目跑起来、怎么验证改动。

---

## 环境要求

| 用途 | 需要什么 |
|---|---|
| 命令行端 / 网页界面 | Python 3.10+（推荐 3.12） |
| 桌面端（Electron） | Node.js 20+ 与 npm |
| 打包 Windows 安装包 | 上面两个都要 |

本项目**不依赖 git 之外的外部服务**，模型走你自己配的任意 OpenAI 兼容接口。

---

## 目录结构

```
Fengcode/
├── cli/                    命令行端 + 本地服务 + 网页界面（Python）
│   ├── src/fengcode/       主程序
│   ├── tests/              单元测试
│   ├── scripts/            验证与构建脚本
│   └── fengcode.spec       PyInstaller 打包配置
├── desktop/                桌面端（Electron 窗口外壳）
│   ├── main.js             主进程：拉后端、托盘、快捷键、窗口
│   └── package.json        electron-builder 配置
└── README.md
```

桌面端不含业务逻辑，它启动 `cli` 的后端并加载其界面，所以两边**功能永远一致**。

---

## 跑起来

### 命令行端

```bash
cd cli
python -m pip install -e ".[dev]"
fengcode serve --open      # 网页界面
fengcode chat              # 命令行对话
```

### 桌面端

```bash
# 先构建后端（桌面端会把它一起打进去）
cd cli
python -m PyInstaller fengcode.spec --noconfirm

# 再跑桌面端
cd ../desktop
npm install                # 首次需要，下载 Electron 运行时
npm start
```

打包成安装包：

```bash
cd desktop
npm run dist               # 产出在 desktop/dist/
```

---

## 提交前请跑一遍验证

改动不同地方，要跑的检查不同。**别默认跑全套**，按改动面选：

| 改了什么 | 跑这些 |
|---|---|
| Python 逻辑 | `python -m pytest tests/ -q` |
| 网页界面（`index.html`） | `python scripts/check_js_syntax.py`、`python scripts/check_refs.py` |
| 新增/改了前端调用的接口 | `python scripts/check_frontend_refs.py`（会抓「前端调了但后端没有」的接口） |
| 改了配置字段 | `python scripts/check_config_fields.py`（会抓前后端字段不一致） |
| 设置页相关 | `python scripts/check_settings_sweep.py`（逐个点开所有设置分类） |
| 桌面端 | `python scripts/check_desktop_backend.py` |
| 端到端 | `python scripts/smoke.py` |

关于两个「防呆」检查——它们专门用来抓**写了但实际用不了**的功能
（前端调了不存在的接口、字段前端提交但后端 schema 没有、设置面板渲染了但按钮点不动），
改前端时强烈建议跑。

---

## 代码约定

- **注释写「为什么」，不写「是什么」**。代码本身说明做什么。
- **不做没有验证的声称**。测试没过就说没过，别用「应该可以」。
- **改行为就改对应的测试**，并在提交信息里说明原因。
- **前端事件用事件委托**。设置中心是「复制 innerHTML」渲染的，
  直接绑节点会绑到副本上导致点了没反应——这类坑已踩过多次。
- Python 用 4 空格缩进，字符串统一 UTF-8。

---

## 提交信息

用中文，一句话说清改了什么：

```
修复设置中心保存按钮点了没反应
新增工作模式（计划/目标可不选）
```

涉及行为变更时，正文里补一段「为什么」。

---

## 许可证

提交即表示你同意按 [MIT](LICENSE) 授权你的贡献。
