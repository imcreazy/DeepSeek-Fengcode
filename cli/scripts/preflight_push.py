"""推送前安全检查：确认没有密钥/敏感文件会被上传。"""
import io
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# 自适应定位项目根（本脚本位于 <项目根>/scripts/）
ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "build", "dist", "node_modules", ".fengcode", "_shots", ".agents",
    # ★ 与 push_github.py / release.py 对齐：本地截图与交付目录都不推送。
    #   漏了它们只会让「将上传 N 个文件」的概览偏大（安全检查本身不受影响），
    #   但三处规则必须一致，否则迟早会漏掉真的该挡的目录。
    "_ui_shots", "shots", "交付", "发布版", "win-unpacked", "fengcode-data", "release",
}
SKIP_DIR_PREFIXES = (".fengcode", "_tmp_", "_probe_", "_test_", "_guard_")
SKIP_DIR_SUFFIXES = (".egg-info",)
SKIP_EXT = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log", ".zip"}
# ★★ 密钥与凭证类文件（与 push_github.py / release.py 的规则保持一致）
SKIP_NAME_PREFIXES = (".env",)
SKIP_NAMES = {
    "config.toml", "credentials", "secrets.toml", "auth.json",
    "account.json", "tokens.json", "id_rsa", "id_ed25519",
}
SKIP_NAME_SUFFIX_EXTS = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk"}

SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9_\-]{24,}", "OpenAI 风格密钥"),
    (r"oc_sk_[A-Za-z0-9_\-]{20,}", "opencode 密钥"),
    (r"sk_live_[A-Za-z0-9_\-]{20,}", "Stripe live 密钥"),
    (r"ghp_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"gh[opsu]_[A-Za-z0-9_\-]{30,}", "GitHub token 变体"),
    (r"AKLT[A-Za-z0-9]{16,}", "火山 access key"),
    (r"ark-[A-Za-z0-9_\-]{24,}", "火山 Ark 密钥"),
    (r"nvapi-[A-Za-z0-9_\-]{30,}", "NVIDIA 密钥"),
    (r"user_[A-Za-z0-9_\-]{60,}", "user_ 长令牌"),
    (r"hf_[A-Za-z0-9]{30,}", "HuggingFace token"),
    (r"sk-ant-[A-Za-z0-9_\-]{24,}", "Anthropic 密钥"),
    (r"AIza[A-Za-z0-9_\-]{30,}", "Google API key"),
    # 通用兜底：任何形如 "xxx_sk_<长串>" 或 "<32+ 位无空格随机串>=" 的赋值
    (r"[A-Za-z0-9]{2,}_sk_[A-Za-z0-9_\-]{20,}", "疑似厂商密钥（_sk_ 形式）"),
]

# 真实口令的片段（只用于本地检测，不会上传——检出后本文件也不会被推送）
# ★★ 真实口令的检测（2026-10-03 修正一个真实泄露）
#   旧写法把口令片段**硬编码在本文件里**，而本文件本身会被推送 →
#   三个片段（腾讯云密码 / 另一个密码 / 邮箱授权码）就这样公开在 GitHub 上了。
#   现改为**从本机 .env 读取**（该文件在仓库之外，永不推送）：
#     · 逐条取环境变量值的前后片段作为探测串；
#     · 取不到就跳过（不报错，不阻断）。
#   ★ 绝不要把真实口令写回本文件。
def _load_local_secrets() -> list[tuple[str, str]]:
    """从本机配置目录下的 .env 与进程环境读敏感值，用于本地检测。

    ★ 不写任何具体键名 —— 旧版把 4 个键名（含主机 ID 片段）硬编码在这里，
      等于把「基础设施结构」也一起公开了。现改为**按关键词动态筛选**：
      凡是键名含 PASSWORD / KEY / TOKEN / SECRET / PASSWD 的一律纳入。
      这样既是零硬编码，覆盖面又比原来那 4 个固定键大得多。
    """
    import os
    from pathlib import Path as _P

    HINT = ("PASSWORD", "PASSWD", "KEY", "TOKEN", "SECRET", "CREDENTIAL")
    vals: dict[str, str] = {}

    for k, v in os.environ.items():
        if v and any(h in k.upper() for h in HINT):
            vals[k] = v

    # 本机 .env 的位置：优先取显式配置的环境变量，否则用本地约定目录。
    # 本机 .env 的位置：只认显式配置的目录，不写死任何产品名。
    #   取不到就直接跳过（检测能力降级，但不报错、不阻断）。
    _env_dir = (os.environ.get("FENGCODE_ENV_DIR") or "").strip()
    envp = _P(_env_dir) / ".env" if _env_dir else None
    if envp is not None and envp.is_file():
        try:
            for line in envp.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if v and any(h in k.upper() for h in HINT):
                    vals[k] = v
        except OSError:
            pass

    out: list[tuple[str, str]] = []
    for v in vals.values():
        # 只取长度足够、有辨识度的串（太短会满世界命中）
        if len(v) < 6:
            continue
        # ★ 跳过「短小的纯小写单词」——实测踩过：.env 里有个键的值就是 `ollama`，
        #   而测试文件里正好有同名字符串（已删预设名），造成假警报。
        #   真实密钥几乎不会是小写单词：它们含数字/混合大小写/符号，或长度很长。
        if len(v) <= 12 and v.isalpha() and v.islower():
            continue
        out.append((re.escape(v), "疑似明文凭据"))
        core = v[1:-1] if len(v) > 8 else v
        if len(core) >= 6 and not (len(core) <= 12 and core.isalpha() and core.islower()):
            out.append((re.escape(core), "疑似明文凭据片段"))
    return out


SECRET_PATTERNS += _load_local_secrets()

# 这些文件本身是检测器/测试，含示例串属正常，跳过
SELF_SKIP = {"preflight_push.py"}

# 允许的"故意假密钥"标记：含这些字样视为测试数据
ALLOW_MARKERS = ("fake-secret", "test-only", "example", "placeholder", "your-key", "xxxx")

files, total = [], 0
for p in sorted(ROOT.rglob("*")):
    if not p.is_file():
        continue
    rel = p.relative_to(ROOT)
    if set(rel.parts) & SKIP_DIRS:
        continue
    if any(part.startswith(SKIP_DIR_PREFIXES) for part in rel.parts):
        continue
    if any(part.endswith(SKIP_DIR_SUFFIXES) for part in rel.parts):
        continue
    if p.suffix.lower() in SKIP_EXT:
        continue
    # ★ 密钥与凭证类文件（文件名 / 前缀 / 扩展名三道）
    low = p.name.lower()
    if low in SKIP_NAMES or low.startswith(SKIP_NAME_PREFIXES):
        continue
    if p.suffix.lower() in SKIP_NAME_SUFFIX_EXTS:
        continue
    if rel.name.startswith("_") and rel.suffix in (".py", ".log", ".ps1", ".txt", ".json"):
        continue
    try:
        size = p.stat().st_size
    except OSError:
        continue
    if size > 8 * 1024 * 1024:
        print("  过大跳过：%s (%.1fMB)" % (rel, size / 1048576))
        continue
    files.append((rel, p, size))
    total += size

print("=" * 62)
print("推送前安全检查")
print("=" * 62)
print("将上传 %d 个文件，共 %.2f MB" % (len(files), total / 1048576))
print()

# 1. 文件名检查
risky_names = []
for rel, p, size in files:
    n = rel.name.lower()
    if n in (".env", ".env.local", "credentials", "secrets.toml") or n.startswith(".env."):
        risky_names.append(rel)
    if n.endswith((".pem", ".key", ".pfx", ".p12")):
        risky_names.append(rel)

# 2. 内容检查（只查文本文件）
hits = []
checked = 0
for rel, p, size in files:
    if size > 2 * 1024 * 1024:
        continue
    if p.name in SELF_SKIP:
        continue                      # 检测器自身含规则字符串，跳过
    try:
        raw = p.read_bytes()
    except OSError:
        continue
    if b"\x00" in raw[:4096]:
        continue          # 二进制跳过
    checked += 1
    text = raw.decode("utf-8", "replace")
    for pat, label in SECRET_PATTERNS:
        for m in re.finditer(pat, text):
            sample = m.group(0)
            # 明显的测试假密钥放行（如 sk-fake-secret-...）
            low = sample.lower()
            if any(mk in low for mk in ALLOW_MARKERS):
                continue
            # 也检查所在行的上下文，避免把注释里的示例当真实密钥
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start: line_end if line_end > 0 else len(text)].lower()
            if any(mk in line for mk in ALLOW_MARKERS):
                continue
            masked = sample[:6] + "…" + sample[-4:] if len(sample) > 12 else "…"
            hits.append((rel, label, masked))

print("已扫描 %d 个文本文件" % checked)
print()

problems = 0
if risky_names:
    problems += len(risky_names)
    print("✗ 发现敏感文件名：")
    for r in risky_names:
        print("    %s" % r)
else:
    print("✓ 无敏感文件名")

if hits:
    problems += len(hits)
    print()
    print("✗ 文件内容里发现疑似密钥：")
    for rel, label, masked in hits:
        print("    %s  →  %s  %s" % (rel, label, masked))
else:
    print("✓ 文件内容无明文密钥")

# 3. 列出将要上传的目录概览
print()
print("目录概览：")
from collections import Counter
c = Counter(str(r.parts[0]) if len(r.parts) > 1 else "（根目录）" for r, _, _ in files)
for k, v in sorted(c.items(), key=lambda x: -x[1]):
    print("  %-22s %d 个文件" % (k, v))

print()
print("=" * 62)
if problems:
    print("发现 %d 处需要处理，已中止" % problems)
    sys.exit(1)
print("检查通过，可以推送")
sys.exit(0)
