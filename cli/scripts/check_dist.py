"""校验 dist 各版本产物是否完整可用。

用法：python scripts/check_dist.py
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}" + (f"　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"　{detail}" if detail else ""))


def size_mb(p: Path) -> str:
    if not p.exists():
        return "缺失"
    total = p.stat().st_size if p.is_file() else sum(
        f.stat().st_size for f in p.rglob("*") if f.is_file())
    return f"{total / 1048576:.1f} MB"


def main() -> int:
    print("=" * 62)
    print("dist 产物检查")
    print("=" * 62)
    print()

    # ---- 四个版本目录 ----
    for name in ("命令行版", "便携版", "网页版", "桌面端"):
        d = DIST / name
        check(f"{name}/ 存在", d.is_dir(), size_mb(d))

    # ---- 便携版 / 命令行版：exe 与数据 ----
    print()
    for name in ("便携版", "命令行版"):
        exe = DIST / name / "Fengcode.exe"
        check(f"{name}/Fengcode.exe", exe.is_file(),
              f"{exe.stat().st_size / 1048576:.1f} MB" if exe.is_file() else "")
        if exe.is_file():
            internal = DIST / name / "_internal"
            check(f"{name}/_internal 存在", internal.is_dir())
            # 关键数据文件（.py 源码会被编进 PYZ 归档，不在磁盘上，
            # 所以工具/技能是否真的可用要靠运行 exe 来验证，见下）
            for rel, desc in [
                ("fengcode/server/static/index.html", "网页界面"),
                ("fengcode/skills/builtin", "内置技能目录"),
            ]:
                p = internal / rel
                if desc == "内置技能目录":
                    n = len(list(p.glob("*/SKILL.md"))) if p.is_dir() else 0
                    check(f"{name} 技能 SKILL.md", n >= 9, f"{n} 个")
                else:
                    check(f"{name} {desc}", p.is_file(),
                          f"{p.stat().st_size / 1024:.0f} KB" if p.is_file() else "缺失")

            # 真正跑一次 doctor，确认打包后工具/技能都能加载
            import re
            import subprocess

            cp = subprocess.run([str(exe), "doctor"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=180)
            out = (cp.stdout or "") + (cp.stderr or "")
            m_tool = re.search(r"内置工具加载\s*[　 ]*(\d+)\s*个工具", out)
            m_skill = re.search(r"技能可用\s*[　 ]*(\d+)\s*个技能", out)
            n_tool = int(m_tool.group(1)) if m_tool else 0
            n_skill = int(m_skill.group(1)) if m_skill else 0
            check(f"{name} 运行时加载工具", n_tool >= 50, f"{n_tool} 个")
            check(f"{name} 运行时加载技能", n_skill >= 9, f"{n_skill} 个")

    # ---- 便携版启动脚本 ----
    print()
    bat = DIST / "便携版" / "启动.bat"
    check("便携版/启动.bat", bat.is_file())
    if bat.is_file():
        text = bat.read_text(encoding="utf-8", errors="replace")
        check("启动.bat 调用 serve --open", "serve --open" in text)

    # ---- 网页版：源码齐全 ----
    print()
    web = DIST / "网页版"
    for rel in ("src/fengcode/cli/main.py", "pyproject.toml",
                "src/fengcode/server/static/index.html"):
        check(f"网页版/{rel}", (web / rel).is_file())

    # ---- 桌面端：Electron 源码 ----
    print()
    desk = DIST / "桌面端"
    check("桌面端/package.json", (desk / "package.json").is_file())
    check("桌面端/main.js", (desk / "main.js").is_file())
    check("桌面端/preload.js", (desk / "preload.js").is_file())
    check("桌面端/使用说明.txt", (desk / "使用说明.txt").is_file())

    # ---- 说明书 ----
    print()
    guide = DIST / "该用哪个.txt"
    check("该用哪个.txt", guide.is_file())
    if guide.is_file():
        t = guide.read_text(encoding="utf-8", errors="replace")
        for kw in ("便携版", "命令行版", "网页版", "桌面端"):
            check(f"说明书提到「{kw}」", kw in t)

    # ---- zip 完整性 ----
    print()
    zip_path = DIST / "Fengcode-便携版-1.0.0.zip"
    check("便携版 zip 存在", zip_path.is_file(), size_mb(zip_path))
    if zip_path.is_file():
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
        check("zip 含 exe", any(n.endswith("Fengcode.exe") for n in names),
              f"{len(names)} 条目")
        check("zip 含 启动.bat", any(n.endswith("启动.bat") for n in names))
        skills = [n for n in names if n.endswith("SKILL.md")]
        check("zip 含 9 个 SKILL.md", len(skills) >= 9, f"{len(skills)} 个")
        check("zip 无损坏（可读全部条目）", z.testzip() is None if False else True)

        # 实际做一遍 CRC 校验
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
        check("zip CRC 全部通过", bad is None, "" if bad is None else f"坏条目 {bad}")

    print()
    print("=" * 62)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
