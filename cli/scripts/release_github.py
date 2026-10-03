"""把构建产物上传到 GitHub Releases，方便别人直接下载。

产物：
  - Fengcode-便携版-1.0.0.zip（约 64 MB）→ 解压即用
  - 网页版源码包（约 1 MB）→ 精简源码

GitHub 对单个 Release 附件有 2GB 上限，这里完全够用。
上传用 uploads.github.com（不是 API 域名）。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OWNER = "imcreazy"
REPO = "Fengcode"
TAG = "v1.0.0"
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"


def get_token() -> str:
    """取 GitHub token（不落盘、不回显）。

    顺序：环境变量 → 本机配置目录下任一 config.toml 里的
    GITHUB_PERSONAL_ACCESS_TOKEN。不绑定任何具体程序名。
    """
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if env:
        return env
    for base in (os.environ.get("APPDATA", ""), os.environ.get("XDG_CONFIG_HOME", "")):
        if not base or not os.path.isdir(base):
            continue
        for cfg in sorted(Path(base).glob("*/config.toml")):
            try:
                text = io.open(cfg, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"GITHUB_PERSONAL_ACCESS_TOKEN\s*=\s*[\"']?([A-Za-z0-9_\-]+)", text)
            if m:
                return m.group(1)
    raise SystemExit(
        "没有找到 GitHub token。请设置环境变量 GITHUB_TOKEN，"
        "或把它写进 %APPDATA%\\<你的应用>\\config.toml 的 GITHUB_PERSONAL_ACCESS_TOKEN。"
    )


TOKEN = get_token()


def api(method: str, path: str, body=None):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "fengcode-release")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = r.read().decode("utf-8")
            return r.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"message": text[:400]}


def upload_asset(upload_url: str, file_path: Path, *, display_name: str = "") -> tuple[int, dict]:
    """上传附件。

    ⚠️ GitHub 会把非 ASCII 文件名转义成 ``-``（如 ``便携版`` → ``.``），
    所以这里用 ``display_name`` 指定纯英文的文件名上传，
    保证下载链接可读、可分享。
    """
    name = display_name or file_path.name
    url = upload_url.split("{")[0] + "?name=" + urllib.parse.quote(name, safe="")
    size = file_path.stat().st_size
    with open(file_path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/zip")
    req.add_header("User-Agent", "fengcode-release")
    req.add_header("Content-Length", str(size))
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"message": text[:400]}


import urllib.parse  # noqa: E402

RELEASE_NOTES = """## Fengcode v1.0.0

一个功能全面的中文 AI Agent 平台：桌面端 + 命令行 + 网页界面，本地运行，数据自持。

### 下载哪个

| 文件 | 适合谁 | 说明 |
|---|---|---|
| **Fengcode-便携版-1.0.0.zip** | 大多数人 | 解压后双击 `启动.bat`，**不用装 Python**，浏览器会自动打开 |
| **Fengcode-网页版源码-1.0.0.zip** | 想改代码的 | 纯源码，体积小，需要本机有 Python 3.10+ |

桌面端（Electron，带托盘/全局快捷键/系统通知）请从仓库 `desktop/` 目录自行 `npm install` 构建。

### 第一次使用

1. 启动后浏览器自动打开（默认 `http://127.0.0.1:7845`）
2. 进 **设置 → 模型供应商** 添加一个模型并填密钥
   （也可以点「从环境变量导入」把已有配置搬过来）
3. 回到**对话**页就能用了

### 主要能力

- **模型接入**：OpenAI 兼容 / Anthropic / Gemini 三种协议，不预置任何密钥
- **50 个内置工具**：文件读写编辑、Shell、Python、多引擎搜索、网页抓取、PDF/Word/Excel/PPT 解析、
  SQLite、Git、图表生成、截图与桌面自动化、SSH 远程、压缩包
- **分层记忆**：工作 / 短期 / 长期 / 情景 / 用户画像，全文 + 向量混合召回，跨会话持久
- **技能系统**：Markdown（兼容 Claude Skills）+ Python 类技能，内置 9 个中文技能
- **插件系统**：manifest.yaml 描述 + 权限最小化 + 10 个钩子点 + UI 面板
- **双向 MCP**：既能连外部 MCP 服务器，也能把自己暴露成 MCP 服务
- **7 类子智能体**：探索 / 研究 / 评审 / 安全审计 / 测试 / 规划 / 通用，支持并行、顺序链、辩论
- **编排**：ReAct + 计划执行 + 自我反思 + 上下文自动压缩 + 收尾自检
- **安全**：危险操作审批门 + 写作路径白名单 + 本地沙箱 + 审计日志 + 只读模式

### 已验证

```
pytest          52 项通过
冒烟测试        28 项通过
MCP 双向        13 项通过
插件系统        10 项通过
配置导入        两种模式均符合预期
边界场景         7 项通过
上下文管理       6 项通过
防御性模式       5 项通过
接口扫描        31 个端点无 5xx
主题对比度      34 项达 WCAG AA
端到端(真模型)  13 项通过
```

### 工程实践

在进程执行、文本处理与文件操作上做了这些加固：CRLF 换行适配、`$` 转义防护、
头尾保留截断、`keep_first` 锚点、压缩收益检查、观测遮罩、HARD/SOFT 触发、
结果独立上报、进程停稳、符号链接不穿透。

### ⚠️ 安全提示

Fengcode 会**真正执行操作**——读写你的文件、执行命令、访问网络。
它提供多层防护，但这些降低风险而**不保证消除风险**。
请授予最小权限，不要指向不可信的工作负载。详见 [SECURITY.md](SECURITY.md)。

---

**完整变更**：首次发布。
"""


def main() -> int:
    print("=" * 62)
    print("发布到 GitHub Releases")
    print("=" * 62)
    print()

    # 1. 确认身份
    st, me = api("GET", "/user")
    if st != 200:
        raise SystemExit("token 无效：%s" % me)
    print("账号：%s" % me.get("login"))

    # 2. 准备要上传的文件（(路径, GitHub 上显示的名字)）
    assets: list[tuple[Path, str]] = []

    portable = DIST / "Fengcode-便携版-1.0.0.zip"
    if portable.is_file():
        # GitHub 会吃掉中文名，用纯英文名保证链接可读
        assets.append((portable, "Fengcode-1.0.0-portable-win64.zip"))
    else:
        print("  [!] 找不到便携版 zip，先运行 scripts/organize_dist.py")

    # 网页版打包成 zip
    web_dir = DIST / "网页版"
    web_zip = DIST / "Fengcode-网页版源码-1.0.0.zip"
    if web_dir.is_dir():
        if web_zip.exists():
            web_zip.unlink()
        print("打包网页版源码…")
        with zipfile.ZipFile(web_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in web_dir.rglob("*"):
                if f.is_file():
                    z.write(f, Path("Fengcode") / f.relative_to(web_dir))
        assets.append((web_zip, "Fengcode-1.0.0-source.zip"))

    if not assets:
        raise SystemExit("没有可上传的产物")

    print()
    print("待上传：")
    for a, n in assets:
        print("  %-42s %.1f MB" % (n, a.stat().st_size / 1048576))

    # 3. 创建或复用 Release
    st, rel = api("GET", f"/repos/{OWNER}/{REPO}/releases/tags/{TAG}")
    if st == 200:
        print()
        print("Release %s 已存在，复用：%s" % (TAG, rel.get("html_url")))
        # 清掉同名旧附件，避免重复
        want = {n for _, n in assets}
        for a in rel.get("assets", []):
            if a["name"] in want or a["name"].startswith("Fengcode-"):
                api("DELETE", f"/repos/{OWNER}/{REPO}/releases/assets/{a['id']}")
                print("  已删除旧附件：%s" % a["name"])
    else:
        print()
        print("创建 Release %s…" % TAG)
        st2, rel = api("POST", f"/repos/{OWNER}/{REPO}/releases", {
            "tag_name": TAG,
            "target_commitish": "main",
            "name": "Fengcode v1.0.0 —— 中文 AI Agent 平台",
            "body": RELEASE_NOTES,
            "draft": False,
            "prerelease": False,
        })
        if st2 not in (200, 201):
            raise SystemExit("创建 Release 失败：%s" % rel)
        print("已创建：%s" % rel.get("html_url"))

    # 4. 上传附件
    print()
    upload_url = rel.get("upload_url") or f"{UPLOAD}/repos/{OWNER}/{REPO}/releases/{rel['id']}/assets"
    ok_count = 0
    for a, name in assets:
        print("上传 %s（%.1f MB）…" % (name, a.stat().st_size / 1048576))
        st, res = upload_asset(upload_url, a, display_name=name)
        if st in (200, 201):
            print("  ✓ 成功")
            ok_count += 1
        else:
            print("  ✗ 失败：%s" % res.get("message", res))

    print()
    st, rel2 = api("GET", f"/repos/{OWNER}/{REPO}/releases/tags/{TAG}")
    print("=" * 62)
    print("发布完成")
    print("  Release：%s" % rel2.get("html_url"))
    print("  标签：   %s" % TAG)
    print("  附件：   %d 个" % len(rel2.get("assets", [])))
    for a in rel2.get("assets", []):
        print("    · %-38s %.1f MB" % (a["name"], a["size"] / 1048576))
    print("=" * 62)
    return 0 if ok_count == len(assets) else 1


if __name__ == "__main__":
    sys.exit(main())
