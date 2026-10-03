# -*- coding: utf-8 -*-
"""发布 GitHub Release。

上传三类资产：
  Fengcode-安装版-<版本>.exe     NSIS 安装包
  Fengcode-<版本>-win64.zip      解压即用（Electron 免安装目录）
  Fengcode-<版本>-source.zip     纯源码

用法：
  python scripts/release.py --dry-run     # 只打包，不发布
  python scripts/release.py               # 打包并发布
"""
from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import push_github as P  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent      # D:\Fengcode\cli
PROJ = ROOT.parent                                  # D:\Fengcode
DELIVER = PROJ / "交付"
OUT = PROJ / "build" / "release"                    # 待上传的资产

def _read_version() -> str:
    """从 fengcode/version.py 读版本号，避免发版时忘了同步这里（曾因此把 1.0.2 发成 1.0.1）。"""
    vf = Path(__file__).resolve().parent.parent / "src" / "fengcode" / "version.py"
    m = re.search(r'__version__\s*=\s*"([^"]+)"', vf.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"✗ 无法从 {vf} 读到 __version__")
    return m.group(1)


VERSION = _read_version()
TAG = f"v{VERSION}"
NAME = f"Fengcode {VERSION}"

DRY = "--dry-run" in sys.argv
IGNORE = {"fengcode-data", ".fengcode", "__pycache__", ".pytest_cache", ".git",
          "node_modules", "_ui_shots"}

# ★★ 保险层（2026-10-03 用户要求「不要把测试实例和密钥文件打包上传」）
#   IGNORE 是「精确名」匹配，只能挡住名字完全一致的目录；而测试实例的目录名
#   带后缀（.fengcode-dbg / .fengcode-exe / .fengcode-test…），精确匹配挡不住。
#   这里补「前缀 / 后缀 / 扩展名」三类规则，与 push_github.py 的排除规则保持一致。
IGNORE_PREFIXES = (
    ".fengcode",                    # 所有调试/测试实例的数据目录
    "_tmp_", "_probe_", "_test_", "_guard_",   # 临时测试目录
)
IGNORE_SUFFIXES = (".egg-info",)
# 密钥与凭证类文件：一律不进任何发行包
SECRET_NAMES = {
    ".env", "credentials", "secrets.toml", "config.toml",
    "auth.json", "account.json", "tokens.json",
    # SSH / 常见私钥（无扩展名，必须按文件名挡）
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".pgpass", ".npmrc", ".pypirc", "known_hosts",
}
SECRET_EXTS = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks"}
SECRET_NAME_PREFIXES = (".env.",)          # .env.local / .env.production …
EXTRA_SKIP_EXTS = {".pyc", ".pyo", ".log", ".db", ".db-wal", ".db-shm"}


def _is_excluded(rel) -> bool:
    """该相对路径是否应从发行包里排除。"""
    name = rel.name
    low = name.lower()
    parts = rel.parts
    if set(parts) & IGNORE:
        return True
    if any(part.startswith(IGNORE_PREFIXES) for part in parts):
        return True
    if any(part.endswith(IGNORE_SUFFIXES) for part in parts):
        return True
    # 密钥与凭证
    if low in SECRET_NAMES or low.startswith(SECRET_NAME_PREFIXES):
        return True
    if rel.suffix.lower() in SECRET_EXTS:
        return True
    # 运行产物
    if rel.suffix.lower() in EXTRA_SKIP_EXTS:
        return True
    return False


def zip_dir(src: Path, dst: Path, *, top: str = "") -> None:
    """把目录压成 zip。top 非空时，包内多一层该名字的目录。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    skipped: list[str] = []
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in src.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            if _is_excluded(rel):
                skipped.append(str(rel))
                continue
            arc = str(Path(top) / rel) if top else str(rel)
            z.write(p, arc)
            n += 1
    print(f"  打包 {dst.name}（{n} 个文件，{dst.stat().st_size / 1048576:.1f} MB）")
    if skipped:
        print(f"    （已排除 {len(skipped)} 个测试实例/密钥类文件）")
        for s in skipped[:8]:
            print(f"      - {s}")
        if len(skipped) > 8:
            print(f"      … 另有 {len(skipped) - 8} 个")


def build_assets() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    assets: list[Path] = []

    # 1) 安装版
    inst_src = DELIVER / "安装版" / "Fengcode-安装版.exe"
    if inst_src.is_file():
        dst = OUT / f"Fengcode-{VERSION}-setup.exe"
        dst.write_bytes(inst_src.read_bytes())
        print(f"  复制 {dst.name}（{dst.stat().st_size / 1048576:.1f} MB）")
        assets.append(dst)

    # 2) 解压即用
    unpacked = DELIVER / "解压即用"
    if unpacked.is_dir():
        dst = OUT / f"Fengcode-{VERSION}-win64.zip"
        zip_dir(unpacked, dst, top="Fengcode")
        assets.append(dst)

    # 3) 源码
    src = DELIVER / "源码"
    if src.is_dir():
        dst = OUT / f"Fengcode-{VERSION}-source.zip"
        zip_dir(src, dst, top=f"Fengcode-{VERSION}")
        assets.append(dst)

    return assets


def _load_version_notes() -> str:
    """release 正文优先取「桌面\\每个版本更新\\v<版本>.md」。

    ★ 用户要求：每次发版都要把详细改动写成文档，并让这份文档出现在 GitHub Release 上
    （每个版本都写清「重点 / 改进 / 修复」）。因此说明不再写死在脚本里，
    而是读桌面文档；读不到才退回下面的兜底模板。
    """
    from pathlib import Path

    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for cand in (
        home / "Desktop" / "每个版本更新" / f"v{VERSION}.md",
        PROJ.parent / "每个版本更新" / f"v{VERSION}.md",
    ):
        try:
            if cand.is_file():
                txt = cand.read_text(encoding="utf-8").strip()
                if txt:
                    return txt
        except Exception:
            pass
    return f"""Fengcode {VERSION} —— 本地运行的中文 AI Agent

## 本版说明

本版未附带详细说明文档，完整改动见仓库提交历史。
"""


NOTES = _load_version_notes() + f"""

---

## 下载

| 文件 | 说明 |
|---|---|
| `Fengcode-{VERSION}-setup.exe` | 安装程序，创建桌面快捷方式，可在「应用和功能」中卸载 |
| `Fengcode-{VERSION}-win64.zip` | 解压即用，双击 `Fengcode.exe` 启动，无需安装 Python |
| `Fengcode-{VERSION}-source.zip` | 源码包，需 Python 3.10+ |

三者功能范围一致，差异仅在部署方式。

---

## 主题与外观

**6 套基础配色** —— 石墨 / 极光 / 板岩 / 松林 / 琥珀 / 玫瑰，一键切换主色与背景。
明暗模式与配色正交，深色模式下每套配色均有独立强调色。

**6 张纯色主题** —— 纯白 / 米白 / 墨黑 / 薰衣草紫 / 青绿 / 浅灰蓝，
带细腻纸纹质感，体积较小（全套 215 KB），切换即时生效。

**10 张图片主题** —— 玫瑰晨光、鸿运工坊、赤曜新城、鼠尾草清风、灵感手账、
紫曜星夜、青岚舞台、黑金序曲、水墨远山、霓虹港湾。
图片铺满全屏，进入对话后自动降低不透明度以保证文字可读性。

**自定义图片主题** —— 设置 → 外观 → 「上传自己的图片」，支持 PNG / JPG / WebP（≤6 MB）。

## 界面

- 正文字体：微软雅黑；字号 13 / 15 / 17 / 19 / 22 五档，默认 17，全站（含设置）同步缩放
- 消息布局：用户消息为右侧气泡，AI 回复为左侧通栏，头像分侧
- 设置中心覆盖 18 个分类，整页呈现 + 左侧分组导航

## 能力

- 50 个内置工具（文件 / 命令 / 联网 / 数据库 / Office / 图表 / 自动化等）
- MCP、插件、技能、子智能体（7 种内置角色）、工作流、定时任务
- 分层记忆（工作记忆 / 长期 / 情景 / 画像）+ 上下文自动压缩
- 三道安全防护：审批门、路径白名单、沙箱
- 支持万象 / DeepSeek / 智谱 / MiMo / 通义 / Kimi / 豆包 / MiniMax / 百川 / 阶跃星辰，
  以及任意 OpenAI 兼容接口

## 许可证

MIT License。模型密钥仅保存在本机，不会上传。
"""

# 历史版本说明归档（不再作为当前版本正文）
_OLD_101 = """
## v1.0.1 修复内容（历史）

设置中心内按钮无法点击（复制 HTML 导致事件失效，改为搬移真实节点）；
保存设置后页面变透明、分隔线消失（空值写入 `data-theme`，现已增加判空）；
「添加服务」等页面级按钮不显示（工具栏按页面判断）；
权限、沙箱、恢复助手在设置中心内渲染异常；
`cmd:` 规则不支持前缀匹配（`cmd:git push` 现可命中 `git push origin main`）。

## v1.0.0（历史）

工作模式可不选择（模型自主判断，计划 / 目标可反选，无强制「对话模式」）；
项目（工作区）两级结构，每个项目绑定独立工作目录；
设置中心重做（左侧分组导航 + 右侧单面板，18 个分类）；
首次启动向导与恢复助手；桌面端默认全屏，关窗后静默驻留托盘。
"""


def main() -> int:
    print("=" * 62)
    print(f"发布 {NAME}" + ("（预演，不发布）" if DRY else ""))
    print("=" * 62)

    print("\n[1] 打包资产")
    if not DELIVER.is_dir():
        print(f"  找不到交付目录 {DELIVER}，请先跑：python scripts/deliver.py --apply")
        return 1
    assets = build_assets()
    if not assets:
        print("  没有可发布的资产")
        return 1

    if DRY:
        print("\n预演完成。去掉 --dry-run 即发布。")
        return 0

    print("\n[2] 创建或复用 Release")
    st, rel = P.api("POST", f"/repos/{P.OWNER}/{P.REPO}/releases", {
        "tag_name": TAG,
        "name": NAME,
        "body": NOTES,
        "draft": False,
        "prerelease": False,
    })
    if st == 422:
        # 标签已存在：取出现有的，更新说明并复用
        print("  标签已存在，复用现有 Release")
        st, rel = P.api("GET", f"/repos/{P.OWNER}/{P.REPO}/releases/tags/{TAG}")
        if st != 200:
            print(f"  取不到现有 Release：{str(rel)[:200]}")
            return 1
        st, rel = P.api("PATCH", f"/repos/{P.OWNER}/{P.REPO}/releases/{rel['id']}",
                        {"name": NAME, "body": NOTES, "draft": False, "prerelease": False})
        if st not in (200, 201):
            print(f"  更新失败：{str(rel)[:200]}")
            return 1
    elif st not in (200, 201):
        print(f"  创建失败（{st}）：{str(rel)[:200]}")
        return 1
    print(f"  Release：{rel.get('html_url')}")
    upload_url = (rel.get("upload_url") or "").split("{")[0]
    release_id = rel.get("id")

    # 已有同名资产先删掉，避免重复上传报错
    st, existing = P.api("GET", f"/repos/{P.OWNER}/{P.REPO}/releases/{release_id}/assets")
    if st == 200:
        for a in existing:
            P.api("DELETE", f"/repos/{P.OWNER}/{P.REPO}/releases/assets/{a['id']}")
            print(f"  移除旧资产 {a['name']}")

    print("\n[3] 上传资产")
    for a in assets:
        data = a.read_bytes()
        qname = urllib.parse.quote(a.name, safe="")
        ok = False
        # 大文件上传容易被网络抖动打断，重试几次
        for attempt in range(1, 4):
            print(f"  上传 {a.name} …（第 {attempt} 次，{len(data)/1048576:.1f} MB）")
            try:
                st2, res = P.api("POST", f"{upload_url}?name={qname}", data, upload=True)
            except Exception as e:
                print(f"    网络异常：{type(e).__name__}: {str(e)[:120]}")
                time.sleep(3)
                continue
            if st2 in (200, 201):
                print(f"    OK  {res.get('browser_download_url')}")
                ok = True
                break
            print(f"    失败（{st2}）：{str(res)[:160]}")
            time.sleep(3)
        if not ok:
            print(f"    {a.name} 最终未能上传")

    print("\n完成：" + str(rel.get("html_url")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
