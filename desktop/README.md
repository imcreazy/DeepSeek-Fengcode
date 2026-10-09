# Fengcode 桌面端

Fengcode 的 Windows 桌面应用外壳。**独立程序窗口**，不打开浏览器。

本目录只含 Electron 外壳：窗口、托盘、菜单、内置浏览器视图，以及后端进程的启动与守护。
业务逻辑全在 `../cli/`（Python 后端）。

## 直接使用

安装包与便携包由 `npm run dist` 产出，位于本目录 `dist/`：

| 产物 | 说明 |
|---|---|
| `Fengcode-安装版-<版本>.exe` | 安装到系统，创建桌面快捷方式，可卸载 |
| `Fengcode-<版本>-win64.zip` | 解压即用，复制到哪都能运行，启动最快 |
| `win-unpacked\` | 未压缩的目录形态，与上面 zip 同源 |

## 从源码运行

```powershell
cd cli
pip install -e .            # 首次需要，安装后端依赖

cd ..\desktop
npm install                 # 首次需要，会下载 Electron 运行时（约 100MB）
npm start
```

开发态会自动拉起 `../cli` 的 Python 后端。

## 打包

顺序不可颠倒 —— 桌面端会把后端一起打进应用：

```powershell
# 1. 先构建后端（产物在 cli/dist/Fengcode，会被 electron-builder 收进应用）
cd cli
python -m PyInstaller fengcode.spec --noconfirm

# 2. 再打包桌面端
cd ..\desktop
npm run dist
```

只跑第 1 步、不跑第 2 步等于没更新用户实例：`extraResources` 取的是
`cli/dist/Fengcode`，最终交付物来自 electron-builder 的 `win-unpacked`。

## 架构

```
Electron 主进程  ──HTTP/WebSocket──►  内置后端（cli 的 Fengcode.exe）
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

`icon.png` / `tray.png` 由 `cli/scripts/make_logo.py` 生成。
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
