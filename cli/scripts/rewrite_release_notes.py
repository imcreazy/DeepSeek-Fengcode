# -*- coding: utf-8 -*-
"""用清理后的桌面 md 重写**已发布**的 Release 正文（不改版本号、不动资产）。

用途：早期发行说明里带了不当措辞，需要用本地已清理的 md 覆盖线上正文。
用法：python scripts/rewrite_release_notes.py 1.2.3 1.2.4 1.2.5
      加 --dry 只打印不提交。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

OWNER = "imcreazy"
REPO = "DeepSeek-Fengcode"
API = "https://api.github.com"


def get_token() -> str:
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if env:
        return env
    for base in (os.environ.get("APPDATA", ""), os.environ.get("XDG_CONFIG_HOME", "")):
        if not base or not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fn != "config.toml":
                    continue
                try:
                    text = (Path(root) / fn).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                m = re.search(r"GITHUB_PERSONAL_ACCESS_TOKEN\s*=\s*[\"']?([A-Za-z0-9_\-]+)", text)
                if m:
                    return m.group(1)
    raise SystemExit("没有找到 GitHub token")


TOKEN = get_token()


def api(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "fengcode-release-notes")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def notes_for(version: str) -> str | None:
    home = Path.home()
    for cand in (home / "Desktop" / "每个版本更新" / f"v{version}.md",):
        if cand.is_file():
            t = cand.read_text(encoding="utf-8").strip()
            if t:
                return t
    return None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    if not args:
        print("用法：python scripts/rewrite_release_notes.py <版本> [版本…] [--dry]")
        return 1
    st, me = api("GET", "/user")
    print("账号：%s" % (me.get("login") if st == 200 else me))
    for v in args:
        tag = f"v{v}"
        notes = notes_for(v)
        if not notes:
            print("  x %s：本地找不到 每个版本更新\\v%s.md" % (tag, v))
            continue
        st, rel = api("GET", f"/repos/{OWNER}/{REPO}/releases/tags/{tag}")
        if st != 200:
            print("  x %s：取不到 Release（%s）%s" % (tag, st, str(rel)[:120]))
            continue
        rid = rel.get("id")
        old_len = len(rel.get("body") or "")
        print(f"  {tag}：正文 {old_len} → {len(notes)} 字符（release id {rid}）")
        if dry:
            print("     [dry] 未提交")
            continue
        st, res = api("PATCH", f"/repos/{OWNER}/{REPO}/releases/{rid}",
                      {"name": rel.get("name") or tag, "body": notes,
                       "draft": False, "prerelease": False})
        if st in (200, 201):
            print("     OK 已重写：%s" % res.get("html_url"))
        else:
            print("     失败：%s" % str(res)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
