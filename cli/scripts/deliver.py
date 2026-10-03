# -*- coding: utf-8 -*-
"""交付整理：把构建产物收拢到 D:\\Fengcode\\交付\\。

  安装版\\      Fengcode-安装版.exe       （Electron NSIS 安装包，可卸载）
  解压即用\\    Fengcode.exe ...          （Electron 免安装目录，启动最快）
  命令行版\\    Fengcode.exe ...          （PyInstaller onedir，起服务/命令行）
  源码\\        pyproject.toml ...        （纯源码，需装 Python 3.10+）
  使用说明.txt

用法：
  python scripts/deliver.py            # 预演，只打印不执行
  python scripts/deliver.py --apply    # 实际执行
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent     # D:\Fengcode\cli
PROJ = ROOT.parent                                # D:\Fengcode
DELIVER = PROJ / "交付"

CMD_DIST = ROOT / "dist"                          # PyInstaller 产物
DESK_DIST = PROJ / "desktop" / "dist"             # electron-builder 产物

APPLY = "--apply" in sys.argv
IGNORE_DIRS = {"fengcode-data", ".fengcode", "__pycache__", ".pytest_cache", ".git"}


def log(msg: str) -> None:
    print(("  " if APPLY else "[预演] ") + msg)


def copy_tree(src: Path, dst: Path) -> bool:
    if not src.is_dir():
        log(f"跳过（不存在）：{src}")
        return False
    log(f"复制目录 {src.name}/ -> {dst.relative_to(PROJ)}/")
    if APPLY:
        dst.mkdir(parents=True, exist_ok=True)

        def _ignore(dirpath, names):
            return {n for n in names if n in IGNORE_DIRS or n.endswith((".pyc", ".log"))}

        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)
    return True


def copy_file(src: Path, dst: Path) -> bool:
    if not src.is_file():
        log(f"跳过（不存在）：{src}")
        return False
    log(f"复制 {src.name} -> {dst.relative_to(PROJ)}")
    if APPLY:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True


def _ver_tuple(path: Path) -> tuple:
    """从文件名里抠出版本号并转成可比较的元组（1.0.2 -> (1,0,2)）。"""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", path.name)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _newest_installer(dist_dir: Path) -> Path | None:
    """取 desktop/dist 里版本号最大的安装包（不能用字符串排序，见调用处注释）。"""
    cands = list(dist_dir.glob("Fengcode-安装版-*.exe"))
    if not cands:
        return None
    return max(cands, key=_ver_tuple)

USAGE = r"""Fengcode 使用说明
================================================================

三个版本（功能完全一样，区别只在怎么装）：

  安装版\Fengcode-安装版.exe
      双击安装，有桌面快捷方式和开始菜单项，能在「应用和功能」里卸载。
      适合长期在主力电脑上用。

  解压即用\
      文件夹形式，复制到哪都能跑，启动最快。双击 Fengcode.exe 即可。

  命令行版\
      同一个程序，但没有图形窗口：起本地服务、当服务器用、或当 MCP 服务端。
      也支持 Fengcode.exe chat / Fengcode.exe doctor 等子命令。

  源码\
      纯 Python 源码，体积最小，但需要本机装好 Python 3.10+。

----------------------------------------------------------------

第一次用
  1. 打开应用，会有一个三步向导：认识功能 -> 配模型 -> 选权限。每步都可跳过。
  2. 到「设置 -> 模型服务」填供应商地址与密钥（支持任何 OpenAI 兼容接口）。
  3. 回来就能对话了。

界面速览
  · 左栏：项目 > 会话 两层。点 + 可新建空白项目（目录自动创建）。
  · 输入框上方：工作模式（可不选，由 AI 自己判断）、权限等级。
  · 右侧面板：用量信息 / 项目文件浏览编辑。
  · 输入 / 会弹出命令与技能菜单。
  · 左下角齿轮进设置中心（左分类 + 右单页）。

几个要点
  · 工作模式：计划模式（先给方案再动手）/ 目标模式（跨轮记住长期目标）。
    两个都不选 = 由 AI 自己判断，这是推荐用法。
  · 权限三档：只看不改 / 工作区可改（推荐）/ 完全权限。
    还能在「权限」页配细粒度规则，如 tool:shell、cmd:git push、path:**/.ssh/*。
  · 记忆：分「背景记忆 / 归档的记忆 / 指令文件 / 召回记录」四个视角。
  · 配置改坏了：设置 -> 高级 -> 恢复助手，可运行诊断、备份、一键回滚。
  · 关闭窗口 = 最小化到托盘静默后台运行（不弹通知）。要真退出请用托盘菜单。

数据位置
  桌面端：%APPDATA%\Fengcode\（配置、会话、记忆）
  命令行版：exe 同级的 fengcode-data\
  源码版：项目下的 .fengcode\（可用 FENGCODE_HOME 改）

安全
  所有数据都在本机，不上传。密钥只存在你本机的 config.toml。
  它会真正读写文件、执行命令——请授予最小权限，不要指向不可信的数据。

================================================================
"""


def main() -> int:
    print("=" * 64)
    print("Fengcode 交付整理" + ("（实际执行）" if APPLY else "（预演模式，加 --apply 才真写入）"))
    print("=" * 64)

    if APPLY and DELIVER.exists():
        log(f"清空旧交付目录：{DELIVER}")
        shutil.rmtree(DELIVER, ignore_errors=True)

    print("\n[1] 安装版（Electron 单文件）")
    # 版本号随发版变化，这里按前缀找，不写死。
    # ⚠️ 必须按「版本号数字」取最大，不能用 sorted() 字符串排序 ——
    #    字符串排序会把 1.0.0 排在 1.0.2 前面，导致交付的是最旧的包。
    inst = _newest_installer(DESK_DIST)
    if inst:
        copy_file(inst, DELIVER / "安装版" / "Fengcode-安装版.exe")
    else:
        log("找不到安装版 exe（请先在 desktop/ 里跑 npm run dist）")
    print("\n[2] 解压即用（Electron 免安装目录）")
    copy_tree(DESK_DIST / "win-unpacked", DELIVER / "解压即用")

    print("\n[3] 命令行版（PyInstaller onedir）")
    copy_tree(CMD_DIST / "Fengcode", DELIVER / "命令行版")

    print("\n[4] 源码（需装 Python）")
    for f in ("pyproject.toml", "README.md", "SECURITY.md", "LICENSE", "start.ps1",
              "启动.bat", "命令行.bat", "构建exe.bat", "fengcode.spec"):
        copy_file(ROOT / f, DELIVER / "源码" / f)
    copy_tree(ROOT / "src", DELIVER / "源码" / "src")
    copy_tree(ROOT / "scripts", DELIVER / "源码" / "scripts")

    print("\n[5] 说明文件")
    if APPLY:
        (DELIVER / "使用说明.txt").write_text(USAGE, encoding="utf-8")
    log("写入 使用说明.txt")

    print()
    print("=" * 64)
    print("整理完成，产物在：" + str(DELIVER) if APPLY else "预演完成。确认无误后加 --apply 执行。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
