"""生成 Fengcode 应用图标（抽象图形风）。

设计：一个「折线 + 圆点」的抽象标记 —— 三条渐次上升的折线构成字母 F 的骨架，
右下角一个实心点收尾，寓意"从起点推进到终点"。
配色沿用界面的主色（靛蓝 → 青）。

产出：
  desktop/icon.png     256x256
  desktop/tray.png     32x32
  ../../命令端/src/fengcode/server/static/favicon.ico（可选）
  ../../命令端/src/fengcode/server/static/logo.svg

用法：python scripts/make_logo.py
"""
import struct
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
DESK = ROOT / "desktop"
STATIC = ROOT / "cli" / "src" / "fengcode" / "server" / "static"

# 主色
C1 = (79, 70, 229)      # #4f46e5 靛蓝
C2 = (14, 165, 233)     # #0ea5e9 天蓝

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#4f46e5"/>
      <stop offset="1" stop-color="#0ea5e9"/>
    </linearGradient>
  </defs>
  <rect width="256" height="256" rx="56" fill="url(#g)"/>
  <!-- 三条折线：抽象 F 骨架 + 进度感 -->
  <g fill="none" stroke="#ffffff" stroke-linecap="round" stroke-linejoin="round">
    <path d="M72 172 L72 84" stroke-width="18" opacity="0.95"/>
    <path d="M72 84 L172 84" stroke-width="18" opacity="0.95"/>
    <path d="M72 126 L140 126" stroke-width="18" opacity="0.75"/>
  </g>
  <!-- 收尾圆点 -->
  <circle cx="180" cy="172" r="20" fill="#ffffff" opacity="0.92"/>
</svg>
"""


def png_from_svg_fallback(size: int, path: Path) -> bool:
    """没有 cairosvg/PIL 时的降级：用纯 Python 画一个简单图形。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.22)
    # 渐变底：逐行填色
    for y in range(size):
        t = y / max(1, size - 1)
        col = (
            int(C1[0] + (C2[0] - C1[0]) * t),
            int(C1[1] + (C2[1] - C1[1]) * t),
            int(C1[2] + (C2[2] - C1[2]) * t),
            255,
        )
        d.line([(0, y), (size, y)], fill=col)
    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    img.putalpha(mask)

    d = ImageDraw.Draw(img)
    w = max(2, int(size * 0.070))
    s = size / 256.0
    # 三条线
    d.line([(72 * s, 172 * s), (72 * s, 84 * s)], fill=(255, 255, 255, 242), width=w)
    d.line([(72 * s, 84 * s), (172 * s, 84 * s)], fill=(255, 255, 255, 242), width=w)
    d.line([(72 * s, 126 * s), (140 * s, 126 * s)], fill=(255, 255, 255, 200), width=w)
    # 圆点
    cr = 20 * s
    d.ellipse([180 * s - cr, 172 * s - cr, 180 * s + cr, 172 * s + cr],
              fill=(255, 255, 255, 235))
    img.save(path)
    return True


def main() -> int:
    DESK.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)

    # 1) SVG 源文件（界面可直接引用）
    svg_path = STATIC / "logo.svg"
    svg_path.write_text(SVG, encoding="utf-8", newline="")
    print(f"✓ {svg_path.relative_to(ROOT)}")

    # 2) PNG（PIL 生成）
    made = []
    for size, target in [(256, DESK / "icon.png"), (32, DESK / "tray.png"),
                         (256, STATIC / "logo.png")]:
        if png_from_svg_fallback(size, target):
            made.append(target)
            print(f"✓ {target.relative_to(ROOT)}  ({size}x{size})")
        else:
            print(f"✗ 生成失败（缺 PIL）：{target}")

    # 3) favicon.ico（PIL 打包多尺寸）
    try:
        from PIL import Image

        ico_src = DESK / "icon.png"
        if ico_src.is_file():
            img = Image.open(ico_src)
            ico = STATIC / "favicon.ico"
            img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
            print(f"✓ {ico.relative_to(ROOT)}")
    except Exception as e:
        print(f"! favicon 生成跳过：{e}")

    print()
    print("=" * 56)
    print(f"完成，生成 {len(made) + 2} 个文件")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
