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
# ★★ 密钥与凭证类文件（2026-10-03 「不要把密钥文件打包上传」）：
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


def _read_version() -> str:
    """从 src/fengcode/version.py 读版本号（与 release.py 同一来源，避免各处不一致）。"""
    vf = ROOT / "cli" / "src" / "fengcode" / "version.py"
    if not vf.is_file():
        vf = ROOT / "src" / "fengcode" / "version.py"
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', vf.read_text(encoding="utf-8"))
        return m.group(1) if m else "未知"
    except OSError:
        return "未知"


# 路径 → 变更分类（用于生成可读的提交摘要，而不是罗列 181 个文件名）
_AREAS = [
    ("cli/src/fengcode/server/static", "界面"),
    ("cli/src/fengcode/core", "Agent 内核"),
    ("cli/src/fengcode/llm", "模型接入"),
    ("cli/src/fengcode/tools", "工具系统"),
    ("cli/src/fengcode/storage", "存储"),
    ("cli/src/fengcode/memory", "记忆"),
    ("cli/src/fengcode/security", "安全与权限"),
    ("cli/src/fengcode/server", "服务端"),
    ("cli/src/fengcode/skills", "技能"),
    ("cli/src/fengcode/plugins", "插件"),
    ("cli/src/fengcode/mcp", "MCP"),
    ("cli/src/fengcode/agents", "子智能体"),
    ("cli/src/fengcode/scheduler", "定时任务"),
    ("cli/tests", "测试"),
    ("cli/scripts", "构建与发布脚本"),
    ("cli", "命令行端"),
    ("desktop", "桌面端"),
    ("docs", "文档"),
]


def _classify(posix: str) -> str:
    # 根目录的文档类文件归「文档」，否则会落到「仓库杂项」
    if "/" not in posix and posix.lower().endswith((".md", ".txt")):
        return "文档"
    for prefix, label in _AREAS:
        if posix.startswith(prefix + "/") or posix == prefix:
            return label
    return "仓库杂项"


def build_commit_message(changed: list[str], removed: list[str], added: list[str]) -> str:
    """按本次实际改动生成提交信息。

    ★ 为什么要这样写：旧版把一段固定文案（讲的是"修复设置中心"）写死，
      于是几十次提交的标题全都是那一句，与实际内容无关 —— 提交记录失去意义。
      现在按「读到的版本号 + 本次真正变动的文件分类」来拼，标题永远对得上内容。

    参数均为仓库相对路径（POSIX 风格）：
      changed —— 内容有变化的文件
      added   —— 新增的文件
      removed —— 被删除的文件
    """
    ver = _read_version()

    kinds = []
    if changed:
        kinds.append(f"更新 {len(changed)} 个文件")
    if added:
        kinds.append(f"新增 {len(added)} 个")
    if removed:
        kinds.append(f"删除 {len(removed)} 个")
    head = "chore: 同步仓库内容" if not kinds else "、" .join(kinds)

    # 主要涉及的区域（按文件数排序，最多 6 个）
    counts: dict[str, int] = {}
    for p in list(changed) + list(added) + list(removed):
        area = _classify(p)
        counts[area] = counts.get(area, 0) + 1
    areas = sorted(counts.items(), key=lambda x: -x[1])[:6]

    lines = [f"Fengcode {ver} —— {head}", ""]
    if areas:
        lines.append("涉及范围：")
        for area, n in areas:
            lines.append(f"- {area}（{n} 个文件）")
        lines.append("")

    # 关键文件点名（便于在提交页直接看出改了什么，最多 8 条）
    key = [p for p in list(changed) + list(added)
           if p.endswith((".py", ".js", ".css", ".html", ".md", ".toml", ".json"))]
    if key:
        lines.append("主要文件：")
        for p in key[:8]:
            lines.append(f"- {p}")
        if len(key) > 8:
            lines.append(f"- 其余 {len(key) - 8} 个略")
    return "\n".join(lines).rstrip() + "\n"


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
    # ★ 先取旧 tree，用于对比出「本次真正改了什么」（提交信息按它生成）。
    old_blobs: dict[str, str] = {}
    if parent_sha:
        st_old, old_commit = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{parent_sha}")
        if st_old == 200:
            old_tree_sha = old_commit.get("tree", {}).get("sha")
            if old_tree_sha:
                st_tree, ot = api(
                    "GET", f"/repos/{OWNER}/{REPO}/git/trees/{old_tree_sha}?recursive=1"
                )
                if st_tree == 200:
                    for item in ot.get("tree", []):
                        if item.get("type") == "blob":
                            old_blobs[item["path"]] = item["sha"]
    print("上传中…")
    tree = []
    changed_paths: list[str] = []
    added_paths: list[str] = []
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
        new_sha = blob["sha"]
        # 与旧树对比：新文件 / 内容变化 分别归类（提交信息用）
        if posix not in old_blobs:
            added_paths.append(posix)
        elif old_blobs[posix] != new_sha:
            changed_paths.append(posix)
        tree.append({
            "path": posix,
            "mode": "100755" if posix.endswith((".sh", ".bat")) else "100644",
            "type": "blob",
            "sha": new_sha,
        })
        if i % 40 == 0 or i == len(files):
            print("  已上传 %d/%d（%.1fs）" % (i, len(files), time.time() - t0))

    # 被删除的文件 = 旧树里有、这次没上传的
    new_paths = {str(r).replace("\\", "/") for (r, _p, _s) in files}
    removed_paths = [p for p in old_blobs if p not in new_paths]

    print()
    print("创建目录树（%d 项）…" % len(tree))

    # 一次建完整棵树 + 一个提交。
    # （曾试过按目录分组提交，让每个文件的提交信息各不相同，
    #  但 GitHub 建树接口在连续调用时稳定返回 500，改为单次提交。）
    #
    # ★ 提交信息按「本次实际改动」自动生成（2026-10-03 修正）。
    #   旧写法把一段固定文案写死在这里，于是几十次提交全是同一句
    #   「修复设置中心…」——与实际内容完全无关，翻提交记录毫无意义。
    #   现在改为：读 version.py 拿版本号，再用本次变更的文件路径拼出摘要。
    commit_msg = build_commit_message(changed_paths, removed_paths, added_paths)
    print("提交摘要：%s" % commit_msg.splitlines()[0])

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
