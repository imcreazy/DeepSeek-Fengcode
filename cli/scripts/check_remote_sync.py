# -*- coding: utf-8 -*-
"""对比本地与远程仓库：找出漏推的、残留的、内容不一致的文件。

用 git blob sha 精确比对内容（sha1("blob <len>\\0" + bytes)），
不依赖网络下载，能准确回答「哪些文件没更新」。
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import push_github as P  # noqa: E402


def git_blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def loose_sha(data: bytes) -> str:
    """按 LF 归一化后算 sha —— 与 .gitattributes 里 `* text=auto eol=lf` 的效果一致。

    Windows 上本地文件是 CRLF，GitHub 存的是 LF，直接用原始字节比较会全部误报。
    """
    return git_blob_sha(data.replace(b"\r\n", b"\n"))


def main() -> int:
    # 本地
    files, _ = P.collect()
    local = {}
    BINARY_EXT = {".png", ".ico", ".jpg", ".jpeg", ".gif", ".webp", ".exe", ".dll", ".db",
                  ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf", ".zip", ".gz",
                  ".sqlite", ".sqlite3", ".so", ".dylib", ".bin", ".pyc"}
    for rel, path, _size in files:
        posix = str(rel).replace("\\", "/")
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        # 二进制文件不做换行归一化；文本按 LF（与 .gitattributes 一致）。
        # ★ 二进制扩展名要列全：漏掉的会被当文本做 LF 归一化，而二进制里恰好
        #   可能含 \r\n 字节序列（实测 .woff2 就是），归一化后哈希必然对不上，
        #   于是每次核对都报「内容不一致」的假警报。
        #   另加一道与扩展名无关的兜底：前 8KB 里出现 NUL 的一定是二进制，
        #   文本文件不会有 NUL —— 扩展名列表总有漏网，这层更可靠。
        is_bin = path.suffix.lower() in BINARY_EXT or b"\x00" in raw[:8192]
        local[posix] = git_blob_sha(raw) if is_bin else loose_sha(raw)

    # 远程
    st, ref = P.api("GET", f"/repos/{P.OWNER}/{P.REPO}/git/ref/heads/{P.BRANCH}")
    if st != 200:
        print("拿不到 HEAD")
        return 1
    head = ref["object"]["sha"]
    st, res = P.api("GET", f"/repos/{P.OWNER}/{P.REPO}/git/trees/{head}?recursive=1")
    if st != 200:
        print("读不到目录树")
        return 1
    remote = {e["path"]: e["sha"] for e in res.get("tree", []) if e.get("type") == "blob"}

    print(f"本地 {len(local)} 个 / 远程 {len(remote)} 个\n")

    only_local = sorted(set(local) - set(remote))
    only_remote = sorted(set(remote) - set(local))
    differ = sorted(p for p in (set(local) & set(remote)) if local[p] != remote[p])

    print(f"【漏推】本地有、远程没有（{len(only_local)}）")
    for p in only_local[:20]:
        print("   " + p)
    if len(only_local) > 20:
        print(f"   … 其余 {len(only_local) - 20} 个")

    print(f"\n【残留】远程有、本地没有（{len(only_remote)}）")
    for p in only_remote[:25]:
        print("   " + p)
    if len(only_remote) > 25:
        print(f"   … 其余 {len(only_remote) - 25} 个")

    print(f"\n【内容不一致】两边都有但不同（{len(differ)}）")
    for p in differ[:25]:
        print("   " + p)
    if len(differ) > 25:
        print(f"   … 其余 {len(differ) - 25} 个")

    total_bad = len(only_local) + len(differ)
    print(f"\n需要推送的：{total_bad} 个（漏推 {len(only_local)} + 内容旧 {len(differ)}）")
    print(f"需要删除的：{len(only_remote)} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
