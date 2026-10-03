# Fengcode 桌面端

Fengcode 的 Windows 桌面应用。**独立程序窗口**，不打开浏览器。

## 直接使用

进 `发布版\`：

| 文件 | 说明 |
|---|---|
| `Fengcode-安装版-1.0.0.exe` | 双击安装，创建桌面快捷方式，可卸载 |
| `Fengcode-便携版-1.0.0.exe` | 单个文件，双击就跑，不装进系统 |
| `解压即用\` | 文件夹复制到哪都能跑，启动最快 |

## 从源码运行

```powershell
cd 桌面端\desktop
npm install          # 首次需要，会下载 Electron 运行时（约 100MB）
npm start
```

开发态会自动拉起 `..\..\命令端` 的 Python 后端。

## 打包

```powershell
# 1. 先构建后端（会被一起打进应用）
cd ..\..\命令端
python -m PyInstaller fengcode.spec --noconfirm

# 2. 再打包桌面端
cd ..\桌面端\desktop
npm run dist
```

产物在 `desktop\dist\`：

- `Fengcode-1.0.0-x64.exe` —— NSIS 安装版
- `Fengcode-便携版-1.0.0.exe` —— 便携版
- `win-unpacked\` —— 解压即用版

归置到 `发布版\`：

```powershell
cd ..\..\命令端
python scripts\organize_final.py
```

## 架构

```
Electron 主进程  ──HTTP/WebSocket──►  内置后端（命令端的 Fengcode.exe）
   │                                      │
   ├── 窗口 / 托盘 / 菜单 / 快捷键          ├── Agent 编排、工具执行
   └── 启动并守护后端，崩了自动重启          └── 数据落在 fengcode-data\
```

**打包态**用 `resources\backend\Fengcode.exe`（用户机器不需要装 Python）。
**开发态**用系统 Python 跑源码。

## 启动速度

后端启动只需约 0.5 秒。桌面端做了两件事让体感更快：

1. **先开窗口**（显示骨架屏），后端在后台并行启动
2. 后端就绪后无缝切到真实界面

## 应用图标

`icon.png` / `tray.png` 由 `命令端\scripts\make_logo.py` 生成。
设计是「折线 + 收尾圆点」的抽象标记，配色沿用界面主色（靛蓝 → 天蓝）。

## 环境变量

| 变量 | 作用 |
|---|---|
| `FENGCODE_PORT` | 指定端口（默认 7845，被占用会自动往后找） |
| `FENGCODE_PYTHON` | 指定 Python 解释器（开发态用） |
| `FENGCODE_BACKEND` | 指定后端 exe 路径 |
| `FENGCODE_NO_SPAWN=1` | 不自动拉起后端（连接已有服务） |

## 日志

`%APPDATA%\Fengcode\logs\desktop.log`

菜单「帮助 → 查看日志」可直接打开。
