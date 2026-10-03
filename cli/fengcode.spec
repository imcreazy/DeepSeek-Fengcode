# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 Fengcode 打成独立 exe。

两种产物：
  1. 文件夹版（onedir）：启动快、便于排查，内含 Fengcode.exe
  2. 单文件版（onefile）：只一个 exe，启动稍慢（需解压到临时目录）

用法：
    pyinstaller fengcode.spec --noconfirm --clean

关键点
------
- 必须打包 ``src/fengcode`` 的**数据文件**（技能 SKILL.md、插件 manifest、
  网页 UI 的 index.html），否则运行时找不到。
- 隐藏导入：uvicorn/starlette 的动态导入较多，需显式声明。
- 排除重型可选依赖（playwright/matplotlib 等），避免体积爆炸；
  用户需要时可在系统 Python 里装。
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH 是 spec 文件所在目录；统一解析成项目根
ROOT = Path(os.path.abspath(SPECPATH))
if (ROOT / "src" / "fengcode").is_dir():
    pass                                   # spec 就在项目根
elif (ROOT.parent / "src" / "fengcode").is_dir():
    ROOT = ROOT.parent
else:
    # 兜底：从当前工作目录找
    ROOT = Path.cwd()
SRC = ROOT / "src" / "fengcode"

# ---------------------------------------------------------------- 数据文件
# 注意：必须**递归收集**每个目录下的全部文件，并按相对包路径放置。
# 早先的写法用 rglob 匹配 pattern，对 "**/*" 的处理在不同版本行为不一致，
# 导致技能/插件只打进去一部分（表现为运行时"技能可用 1 个"）。
datas = []


def add_tree(local_dir: Path, pkg_rel: str) -> int:
    """把 local_dir 下的所有文件加进 datas，目标路径为 fengcode/<pkg_rel>/..."""
    if not local_dir.is_dir():
        return 0
    n = 0
    for p in sorted(local_dir.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(local_dir)
        target = os.path.join("fengcode", pkg_rel.replace("/", os.sep), os.path.dirname(str(rel)))
        datas.append((str(p), target))
        n += 1
    return n


counts = {
    "网页界面": add_tree(SRC / "server" / "static", "server/static"),
    "内置技能": add_tree(SRC / "skills" / "builtin", "skills/builtin"),
    "内置插件": add_tree(SRC / "plugins" / "builtin", "plugins/builtin"),
}

# 内置工具是运行期靠 importlib 动态导入的，静态分析看不到调用点。
# 必须在这里显式列出，否则打包后 tools/builtin 整个不会进产物
# （症状：源码跑 50 个工具，exe 里只剩十几个）。
BUILTIN_TOOL_MODULES = [
    "fengcode.tools.builtin.files",
    "fengcode.tools.builtin.shell",
    "fengcode.tools.builtin.web",
    "fengcode.tools.builtin.documents",
    "fengcode.tools.builtin.archive",
    "fengcode.tools.builtin.git_tools",
    "fengcode.tools.builtin.database",
    "fengcode.tools.builtin.design",
    "fengcode.tools.builtin.assist",
    "fengcode.tools.builtin.automation",
    "fengcode.tools.builtin.remote",
]

# ---------------------------------------------------------------- 隐藏导入
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "starlette.middleware",
    "starlette.middleware.errors",
    "starlette.middleware.exceptions",
    "anyio._backends._asyncio",
    "websockets.legacy",
    "websockets.legacy.server",
    "typer",
    "click",
    "rich",
    "pydantic",
    "httpx",
    "yaml",
    # 可选依赖：装了就打进去，没装则跳过（运行时优雅降级）
    "psutil",
    "paramiko",
    "bs4",
    "PIL",
]

# 显式声明内置工具模块（不依赖 collect_submodules 的成功与否）
hiddenimports += BUILTIN_TOOL_MODULES

for mod in ("fengcode", "fengcode.tools.builtin", "fengcode.cli",
            "fengcode.server", "fengcode.mcp", "fengcode.plugins"):
    try:
        hiddenimports += collect_submodules(mod)
    except Exception as e:
        print("[spec] collect_submodules(%s) 失败：%s" % (mod, e))

# 去重
seen = set()
hiddenimports = [m for m in hiddenimports if not (m in seen or seen.add(m))]

# ---------------------------------------------------------------- 排除项
# ⚠️ 不要排除 standard library 的核心模块（如 distutils/setuptools），
# 三方库会引用它们，排除后 PyInstaller 会报 "already imported as ExcludedModule"。
# 只排除确定用不到的重型可视化/科学计算库，避免体积爆炸。
excludes = [
    "tkinter",
    "matplotlib",
    "notebook",
    "IPython",
    "jupyter",
    "pytest",
    "scipy",
    "pandas",
    "torch",
    "tensorflow",
    "PyQt5",
    "PySide2",
    "PySide6",
    "wx",
    "lib2to3",
    "playwright",
]

# ---------------------------------------------------------------- 入口脚本
entry = ROOT / "scripts" / "exe_entry.py"

block_cipher = None

a = Analysis(
    [str(entry)],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 图标（有就用）
icon = ROOT.parent / "桌面端" / "desktop" / "icon.png"
icon_arg = str(icon) if icon.is_file() else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Fengcode",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台：服务端需要看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Fengcode",
)
