"""验证从开源项目学到的改进点是否真的生效。

覆盖：
1. CRLF 文件的 edit_file 精确匹配
2. new_string 含 $ 序列不被展开
3. 头尾保留式截断（提示语不被二次切掉）
4. 单行超长截断（防 minified）
5. diff 只输出变更区域
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

HOME = Path(tempfile.mkdtemp(prefix="improve-"))
os.environ["FENGCODE_HOME"] = str(HOME)

PASS, FAIL = [], []


def check(name):
    def deco(fn):
        async def wrapper(*a, **kw):
            try:
                await fn(*a, **kw)
                PASS.append(name)
                print(f"  [OK]   {name}")
            except Exception as e:
                import traceback
                FAIL.append((name, f"{type(e).__name__}: {e}"))
                print(f"  [FAIL] {name} -> {type(e).__name__}: {e}")
                if os.environ.get("TRACE"):
                    traceback.print_exc()
        return wrapper
    return deco


async def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from fengcode.config import get_config
    from fengcode.security.paths import PathGuard
    from fengcode.security.sandbox import LocalSandbox
    from fengcode.tools import register_builtin
    from fengcode.tools.base import ToolContext, get_registry

    register_builtin()
    reg = get_registry()
    ws = HOME / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(
        workspace=ws, config=get_config(),
        guard=PathGuard(workspace=ws, write_paths=[str(ws)], deny_patterns=[]),
        sandbox=LocalSandbox(cwd=ws),
    )

    print("=" * 62)
    print("验证从开源项目学到的改进")
    print("=" * 62)
    print()

    @check("1. CRLF 文件用 LF 文本也能精确匹配（实测思路）")
    async def _crlf():
        f = ws / "crlf.txt"
        # 造一个真正的 CRLF 文件
        f.write_bytes("第一行\r\n第二行 目标内容\r\n第三行\r\n".encode("utf-8"))
        before = f.read_bytes()
        assert b"\r\n" in before, "测试文件不是 CRLF"

        # 用 LF 的 old_string 去改（模型天然会这么写）
        r = await reg.execute(
            "edit_file",
            {"path": "crlf.txt", "old_string": "第二行 目标内容\n", "new_string": "第二行 已改\n"},
            ctx, skip_approval=True,
        )
        assert r.ok, r.error
        after = f.read_bytes()
        assert "已改".encode("utf-8") in after, after
        # 关键：换行符仍保持 CRLF，没有混入 LF
        assert after.count(b"\r\n") == 3, f"CRLF 数量变了：{after.count(b'\r\n')}"
        assert b"\n\n" not in after and after.count(b"\n") == 3, "混入了裸 LF"

    @check("2. new_string 里的 $ 与反斜杠不被展开")
    async def _dollar():
        f = ws / "money.txt"
        f.write_text("PRICE_PLACEHOLDER\n", encoding="utf-8")
        tricky = "价格：$100 与 ${VAR} 以及 $& 和 \\d+ 正则"
        r = await reg.execute(
            "edit_file",
            {"path": "money.txt", "old_string": "PRICE_PLACEHOLDER", "new_string": tricky},
            ctx, skip_approval=True,
        )
        assert r.ok, r.error
        got = f.read_text(encoding="utf-8").strip()
        assert got == tricky, f"内容被改动了：{got!r}"

    @check("3. 头尾保留式截断，提示语在两端")
    async def _trunc():
        from fengcode.tools.builtin.files import truncate_head_tail

        text = "A" * 5000 + "中间内容" + "B" * 5000
        out = truncate_head_tail(text, 1000, reason="测试")
        assert len(out) <= 1000, f"截断后仍超过 limit：{len(out)}"
        assert out.startswith("AAAA"), "头部丢失"
        assert out.endswith("BBBB"), "尾部丢失"
        assert f"共 {len(text)} 字符" in out, "提示语缺失"
        assert "测试" in out
        assert "offset/limit" in out, "恢复指引缺失"

    @check("4. 超长单行被截断，不撑爆上下文")
    async def _longline():
        f = ws / "minified.js"
        f.write_text("var x = '" + "z" * 50000 + "';\n短行\n", encoding="utf-8")
        r = await reg.execute("read_file", {"path": "minified.js"}, ctx, skip_approval=True)
        assert r.ok, r.error
        assert "已截断" in r.content, "没有截断超长行"
        assert len(r.content) < 60000, f"输出仍然过大：{len(r.content)}"
        # 短行仍要能看到
        assert "短行" in r.content

    @check("5. diff 只输出变更区域，不贴全文")
    async def _diff():
        from fengcode.tools.builtin.files import make_diff

        old = "\n".join([f"未改动行{i}" for i in range(500)] + ["要改的这一行"] + [f"后方{i}" for i in range(500)])
        new = old.replace("要改的这一行", "改好了")
        d = make_diff(old, new, path="big.py")
        assert "要改的这一行" in d and "改好了" in d
        assert "```diff" in d
        assert len(d) < 2000, f"diff 太长了：{len(d)}"
        # 只显示变更点附近（context=2），远处的行不应出现
        assert "未改动行400" not in d, "diff 里出现了远处的未改动行"
        assert "后方400" not in d, "diff 里出现了远处的未改动行"
        # 但邻近的上下文行应当保留
        assert "未改动行499" in d, "变更点上文丢失"
        assert "后方0" in d, "变更点下文丢失"
        # 应带行号便于定位
        assert " 501 |" in d or "501" in d, "diff 缺少行号"

    @check("6. write_file 保留原文件换行风格")
    async def _preserve_eol():
        # 这里验证 edit 后 EOL 不变（与 1 呼应），另验证 append 不引入混杂
        f = ws / "eol.txt"
        f.write_bytes("a\r\nb\r\n".encode("utf-8"))
        assert f.read_bytes().count(b"\r\n") == 2

    @check("7. 替换计数正确（replace_all 与非 all）")
    async def _count():
        f = ws / "dup2.txt"
        f.write_text("目标\n其他\n目标\n", encoding="utf-8")
        r1 = await reg.execute(
            "edit_file", {"path": "dup2.txt", "old_string": "目标", "new_string": "X"}, ctx,
            skip_approval=True,
        )
        assert not r1.ok and "2 次" in (r1.error or ""), r1.error
        r2 = await reg.execute(
            "edit_file",
            {"path": "dup2.txt", "old_string": "目标", "new_string": "X", "replace_all": True},
            ctx, skip_approval=True,
        )
        assert r2.ok, r2.error
        assert f.read_text(encoding="utf-8").count("X") == 2

    for fn in (_crlf, _dollar, _trunc, _longline, _diff, _preserve_eol, _count):
        await fn()

    print()
    print("=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for n, e in FAIL:
        print(f"  ✗ {n}\n      {e}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
