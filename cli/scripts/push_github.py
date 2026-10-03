"""推送 Fengcode 到 GitHub（imcreazy/DeepSeek-Fengcode）。

使用 GitHub REST API（本机无 git 可执行文件）。
- 自动创建仓库（若不存在）
- 批量上传文件（逐个 commit，用 Git Data API 一次性提交更高效）
- 跳过 .gitignore 排除项与大文件
"""
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent   # D:\Fengcode（含 命令端 + 桌面端）
OWNER = "imcreazy"
REPO = "DeepSeek-Fengcode"
BRANCH = "main"
API = "https://api.github.com"


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


def api(method, path, body=None, *, raw=False, upload=False, content_type=None):
    """GitHub REST 调用。

    upload=True 时：body 按原始字节发送（用于上传 Release 资产），
    时长放宽到 30 分钟 —— 安装包有一百多兆。
    """
    url = API + path if path.startswith("/") else path
    if upload:
        data = body if isinstance(body, (bytes, bytearray)) else b""
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "fengcode-push")
    if data:
        req.add_header("Content-Type", content_type or
                       ("application/octet-stream" if upload else "application/json"))
    timeout = 1800 if upload else 90
    # 网络抖动（SSL EOF、连接重置）在本机很常见，自动重试；
    # HTTP 状态码属于服务端明确答复，不重试。
    last_err = None
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8")
                return resp.status, (payload if raw else (json.loads(payload) if payload else {}))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body_text)
            except ValueError:
                return e.code, {"message": body_text[:400]}
        except Exception as e:                      # URLError / SSL / 超时
            last_err = e
            if attempt < 4:
                time.sleep(2 * attempt)
                continue
    raise last_err if last_err else RuntimeError("请求失败")


# ---------------- 收集要上传的文件 ----------------

SKIP_DIRS = {
    ".git", ".github/workflows/__none__", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", "venv", "build", "dist",
    "node_modules", ".fengcode", "_shots", ".agents",
    "交付", "发布版", "win-unpacked", "fengcode-data", ".pytest_cache",
    "_ui_shots", "shots", "egg-info", "release",
}
# 前缀/后缀规则（比逐个列举更可靠）：
#   .fengcode* —— 所有调试/测试实例的数据目录（含 .fengcode-dbg / .fengcode-exe）
#   _tmp_* / _probe_* / _test_* —— 临时测试目录（构建/验证时临时建的）
#   *.egg-info —— pip install -e . 生成的构建元数据
# ★ 注意：不挡「所有 _ 开头目录」——`_internal` 是 PyInstaller 的必需目录，
#   虽然它只在 交付/ 下（那本就不推送），但规则写松一点更安全，避免将来误伤。
SKIP_DIR_PREFIXES = (".fengcode", "_tmp_", "_probe_", "_test_", "_guard_")
SKIP_DIR_SUFFIXES = (".egg-info",)
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# ★★ 密钥与凭证类文件（2026-10-03 用户要求「不要把密钥文件打包上传」）：
#   按「文件名 / 文件名前缀 / 扩展名」三类挡，与 release.py 的规则保持一致。
#   为什么必须显式列：这些文件多数没有特殊扩展名（.env / config.toml 是纯文本），
#   只靠 SKIP_EXT 挡不住，一旦有人把本地配置放进工程就会被推上去。
SKIP_NAME_PREFIXES = (".env",)
SKIP_NAMES = {
    "config.toml", "credentials", "secrets.toml", "auth.json",
    "account.json", "tokens.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".pgpass", ".npmrc", ".pypirc", "known_hosts",
}
SKIP_NAME_SUFFIX_EXTS = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk"}
SKIP_EXT = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log", ".zip"}
MAX_SIZE = 8 * 1024 * 1024          # GitHub API 单文件上限较宽松，这里保守取 8MB
MAX_TOTAL = 900                      # 文件数上限（防意外）


def collect():
    files = []
    total = 0
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        parts = set(rel.parts)
        if parts & SKIP_DIRS:
            continue
        # 前缀规则：任何以 .fengcode 开头的目录都跳过（.fengcode-dbg / .fengcode-exe …）
        if any(part.startswith(SKIP_DIR_PREFIXES) for part in rel.parts):
            continue
        # 后缀规则：xxx.egg-info 之类的构建元数据
        if any(part.endswith(SKIP_DIR_SUFFIXES) for part in rel.parts):
            continue
        if p.name in SKIP_FILES:
            continue
        # ★ 密钥与凭证类文件（文件名 / 前缀 / 扩展名三道）
        low = p.name.lower()
        if low in SKIP_NAMES or low.startswith(SKIP_NAME_PREFIXES):
            continue
        if p.suffix.lower() in SKIP_NAME_SUFFIX_EXTS:
            continue
        if p.suffix.lower() in SKIP_EXT:
            continue
        # 显式忽略根目录下的临时脚本
        if (
            (rel.name.startswith("_") and rel.suffix in
             (".py", ".log", ".ps1", ".txt", ".json", ".png", ".jpg", ".jpeg", ".webp"))
            or rel.name.endswith(".pid")
        ):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > MAX_SIZE:
            print("  跳过（过大 %.1fMB）：%s" % (size / 1048576, rel))
            continue
        files.append((rel, p, size))
        total += size
        if len(files) > MAX_TOTAL:
            raise SystemExit("文件数异常（>%d），中止以免误传" % MAX_TOTAL)
    return files, total


def is_text(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    return True


def main():
    print("=" * 62)
    print("推送 Fengcode 到 GitHub")
    print("=" * 62)
    print()

    # 1. 确认身份
    st, me = api("GET", "/user")
    if st != 200:
        raise SystemExit("token 无效：%s" % me)
    print("账号：%s（%s）" % (me.get("login"), me.get("html_url")))

    # 2. 仓库是否存在
    st, repo = api("GET", f"/repos/{OWNER}/{REPO}")
    if st == 404:
        print("仓库不存在，创建中…")
        st2, created = api("POST", "/user/repos", {
            "name": REPO,
            "description": "Fengcode —— 功能全面的中文 AI Agent 平台（桌面端 + CLI + Web UI）",
            "private": False,
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
            "license_template": "mit",
        })
        if st2 not in (200, 201):
            raise SystemExit("创建仓库失败：%s" % created)
        repo = created
        print("已创建：%s" % repo.get("html_url"))
    elif st == 200:
        print("仓库已存在：%s" % repo.get("html_url"))
    else:
        raise SystemExit("查询仓库失败：%s" % repo)

    # 3. 收集文件
    files, total = collect()
    print("待上传：%d 个文件，共 %.2f MB" % (len(files), total / 1048576))

    # 4. 拿当前 HEAD（可能为空仓库）
    st, ref = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    parent_sha = None
    if st == 200:
        parent_sha = ref.get("object", {}).get("sha")
        print("当前分支 %s HEAD：%s" % (BRANCH, parent_sha[:8] if parent_sha else "无"))
    else:
        print("分支 %s 尚不存在，将创建" % BRANCH)

    # 5. 逐文件建 blob
    print()
    print("上传中…")
    tree = []
    t0 = time.time()
    for i, (rel, path, size) in enumerate(files, 1):
        posix = str(rel).replace("\\", "/")
        if is_text(path):
            content = path.read_text(encoding="utf-8", errors="replace")
            st, blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs", {
                "content": content, "encoding": "utf-8",
            })
        else:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            st, blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs", {
                "content": b64, "encoding": "base64",
            })
        if st not in (200, 201):
            print("  ✗ %s：%s" % (posix, blob.get("message", blob)))
            continue
        tree.append({
            "path": posix,
            "mode": "100755" if posix.endswith((".sh", ".bat")) else "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
        if i % 40 == 0 or i == len(files):
            print("  已上传 %d/%d（%.1fs）" % (i, len(files), time.time() - t0))

    print()
    print("创建目录树（%d 项）…" % len(tree))

    # 一次建完整棵树 + 一个提交。
    # （曾试过按目录分组提交，让每个文件的提交信息各不相同，
    #  但 GitHub 建树接口在连续调用时稳定返回 500，改为单次提交。）
    commit_msg = (
        "fix(cli): 修复设置中心所有按钮点不动、保存后样式丢失\n"
        "\n"
        "根因有四个：\n"
        "- paintRules 是 const 别名，在设置页调用时还在 TDZ 里，抛错中断整页渲染，\n"
        "  导致所有按钮都没绑上事件\n"
        "- 设置中心复制 innerHTML，副本上的 onclick 全部失效；改为搬真实 DOM 节点\n"
        "- 在非外观页保存时 #u-theme 不存在，val() 取到空串，applyTheme(\"\") 清空了\n"
        "  data-theme，整套 CSS 变量失效（页面变透明、分界线消失）\n"
        "- 顶部工具栏按 page 判断，设置中心里看不到「添加服务」这类页面级按钮\n"
        "\n"
        "另：新增 #settings-render 渲染宿主，避免 PAGES.settings 整页渲染时\n"
        "覆盖设置中心自己的容器；目录结构改为 cli/ + desktop/，移除免安装版。\n"
    )

    def post_tree(payload, label):
        """建树；网络抖动时返回空消息，重试几次。"""
        last = None
        for attempt in range(1, 5):
            st, res = api("POST", f"/repos/{OWNER}/{REPO}/git/trees", payload)
            if st in (200, 201) and isinstance(res, dict) and res.get("sha"):
                return res
            last = (st, res)
            print("    建树失败（%s，第 %d 次）：%s" % (label, attempt, str(res)[:80]))
            time.sleep(3)
        raise SystemExit("创建 tree 失败（%s）：%s" % (label, str(last)[:200]))

    built_tree = post_tree({"tree": tree}, "全部文件")

    cbody = {"message": commit_msg, "tree": built_tree["sha"]}
    if parent_sha:
        cbody["parents"] = [parent_sha]
    st, commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits", cbody)
    if st not in (200, 201):
        raise SystemExit("创建 commit 失败：%s" % str(commit)[:200])
    print("  提交：%s" % commit["sha"][:12])

    new_tree = built_tree

    print("更新分支 %s…" % BRANCH)
    for attempt in range(1, 5):
        st, r = api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}", {
            "sha": commit["sha"], "force": True,
        })
        if st in (200, 201):
            break
        print("    更新失败（第 %d 次）：%s" % (attempt, str(r)[:80]))
        time.sleep(3)
    else:
        raise SystemExit("更新分支失败：%s" % r)

    print()
    print("=" * 62)
    print("推送完成")
    print("  仓库：  %s" % repo.get("html_url"))
    print("  提交：  %s" % commit["sha"][:12])
    print("  文件：  %d 个" % len(tree))
    print("  分支：  %s" % BRANCH)
    print("=" * 62)

    # 顺手补上仓库话题标签，便于被发现
    st, _ = api("PUT", f"/repos/{OWNER}/{REPO}/topics", {
        "names": ["ai-agent", "llm", "mcp", "chinese", "desktop-app",
                  "cli", "python", "agent-framework"],
    })
    if st == 200:
        print("已设置仓库标签")


if __name__ == "__main__":
    main()
