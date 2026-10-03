"""检查源码里是否残留硬编码的绝对路径。

项目可能被放到任意目录（例如从 D:\\Fengcode 拆到 D:\\Fengcode\\命令端），
脚本里写死 ``D:\\Fengcode`` 会在换位置后静默失效。这里做静态扫描。

用法：python scripts/check_paths.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 允许出现的位置：注释里说明历史布局、以及测试用的示例文本
ALLOW_MARKERS = (
    "# 兼容两种布局",
    "# D:\\Fengcode\\桌面端",
    "我的项目路径是",
    "这样用户能直接在",   # paths.py 的文档字符串举例
)

PAT = re.compile(r"D:[\\/]+Fengcode", re.IGNORECASE)

SKIP_NAMES = {Path(__file__).name, "scan_hardcoded.py", "check_paths.py"}


def scan() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for base in (ROOT / "scripts", ROOT / "src"):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts or p.name in SKIP_NAMES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.split("\n"), 1):
                if not PAT.search(line):
                    continue
                if any(m in line for m in ALLOW_MARKERS):
                    continue
                hits.append((p.relative_to(ROOT), i, line.strip()))
    return hits


def main() -> int:
    print("=" * 62)
    print("硬编码路径检查")
    print("=" * 62)
    print()

    hits = scan()
    if not hits:
        print("  ✓ 没有发现硬编码的项目绝对路径")
        print()
        print("=" * 62)
        print("通过 1 项，失败 0 项")
        print("=" * 62)
        return 0

    print(f"  ✗ 发现 {len(hits)} 处硬编码路径（换目录后会失效）：")
    for rel, line_no, text in hits:
        print(f"      {rel}:{line_no}")
        print(f"        {text[:100]}")
    print()
    print("  建议改成基于 __file__ 的自适应定位，例如：")
    print("      ROOT = Path(__file__).resolve().parent.parent")
    print()
    print("=" * 62)
    print(f"通过 0 项，失败 {len(hits)} 项")
    print("=" * 62)
    return 1


if __name__ == "__main__":
    sys.exit(main())
