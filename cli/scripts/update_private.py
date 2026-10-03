#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""更新用户的私人实例（桌面\\我自己用的\\）。

私人实例 = 用户日常在用的独立副本，与源码物理隔离：
程序文件来自 交付\\解压即用\\，用户数据（密钥/对话/设置）在
``resources/backend/fengcode-data/``，**绝不能被覆盖或删除**。

用法::

    python scripts/update_private.py            # 预演（默认，不写盘）
    python scripts/update_private.py --apply    # 真正执行

★ 安全设计（2026-09-30 踩过的坑，务必保留）::

    第一版脚本按「顶层名字」判断要保留什么（name in {"fengcode-data", ...}），
    但数据目录在 ``resources/backend/fengcode-data``，顶层是 ``resources``，
    于是 ``shutil.rmtree("resources")`` 把用户数据一起删了。
    —— 判断保留必须看**完整相对路径**，不能看顶层名字。

本脚本因此：
  1. 先把数据目录**复制到桌面备份**并校验指纹（文件数/总字节/config.toml md5）；
  2. 删除程序文件时用**白名单 + 显式排除数据路径**，并在删之前再断言一次数据仍在；
  3. 复制完成后比对数据指纹，不一致立即报警并提示从备份恢复；
  4. 全程不碰 ``fengcode-data`` 与 ``说明.txt``。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = Path(os.environ.get("USERPROFILE", "")) or Path.home()
TARGET = HOME / "Desktop" / "我自己用的"
SRC = Path(r"D:\Fengcode\交付\解压即用")

# 用户数据目录（相对 TARGET 的完整路径）—— 绝不可删/覆盖
DATA_REL = Path("resources") / "backend" / "fengcode-data"
KEEP_TOP = {"说明.txt"}          # 顶层保留项
DRY = "--apply" not in sys.argv


def fingerprint(root: Path) -> tuple[int, int, str]:
    """返回 (文件数, 总字节, config.toml 的 md5 前 12 位)。"""
    n = 0
    total = 0
    cfg_md5 = ""
    if not root.is_dir():
        return 0, 0, ""
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            total += p.stat().st_size
            n += 1
        except OSError:
            continue
        if p.name == "config.toml" and not cfg_md5:
            h = hashlib.md5()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            cfg_md5 = h.hexdigest()[:12].upper()
    return n, total, cfg_md5


def log(msg: str) -> None:
    print(("[预演] " if DRY else "      ") + msg)


def main() -> int:
    print("=" * 62)
    print("更新私人实例" + ("（预演，不写盘）" if DRY else ""))
    print("=" * 62)

    if not TARGET.is_dir():
        print(f"✗ 找不到私人实例：{TARGET}")
        return 1
    if not SRC.is_dir():
        print(f"✗ 找不到交付源：{SRC}")
        print("  请先跑：python scripts/deliver.py --apply")
        return 1

    data_dir = TARGET / DATA_REL
    before = fingerprint(data_dir)
    print(f"\n源  ：{SRC}")
    print(f"目标：{TARGET}")
    print(f"\n[0] 更新前数据指纹：{before[0]} 文件 / {before[1]} 字节 / config md5 {before[2]}")
    if before[0] == 0:
        print("  ⚠️ 没读到数据目录 —— 确认这个私人实例是否已初始化过")

    # ---- 1) 备份数据 ----
    # ★ 备份放在临时目录，不要放桌面 —— 之前放桌面会凭空多出「我自己用的-数据备份-xxx」
    #   文件夹，污染用户桌面（用户明确不满过）。更新成功且校验通过即自动删掉。
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(tempfile.gettempdir()) / "fengcode-private-backup" / stamp
    print(f"\n[1] 备份数据 → {backup}")
    if data_dir.is_dir():
        if DRY:
            log(f"会复制 {data_dir} → {backup}")
        else:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(data_dir, backup)
            bfp = fingerprint(backup)
            print(f"      备份指纹：{bfp[0]} 文件 / {bfp[1]} 字节 / md5 {bfp[2]}")
            if bfp != before:
                print("      ✗ 备份指纹与源不一致，中止！")
                return 1
            print("      ✅ 备份已校验")
    else:
        log("（无数据目录，跳过备份）")

    # ---- 2) 清理旧程序文件 ----
    print("\n[2] 清理旧程序文件（保留数据与说明）")
    data_top = DATA_REL.parts[0]                     # "resources"
    for name in sorted(os.listdir(TARGET)):
        if name in KEEP_TOP:
            log(f"保留 {name}")
            continue
        p = TARGET / name
        if name == data_top:
            # ★ 关键：整个 resources 里只有 fengcode-data 要留，其余是程序文件。
            #   不能直接 rmtree(resources)，否则数据一起没了（踩过）。
            for sub in sorted(os.listdir(p)):
                sp = p / sub
                if sub == "backend":
                    for be in sorted(os.listdir(sp)):
                        if be == DATA_REL.parts[-1]:
                            log(f"保留 {DATA_REL}")
                            continue
                        log(f"删除 resources/backend/{be}")
                        if not DRY:
                            (shutil.rmtree(sp / be, ignore_errors=True)
                             if (sp / be).is_dir() else os.remove(sp / be))
                else:
                    log(f"删除 resources/{sub}")
                    if not DRY:
                        (shutil.rmtree(sp, ignore_errors=True)
                         if sp.is_dir() else os.remove(sp))
            continue
        log(f"删除 {name}")
        if not DRY:
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else os.remove(p)

    # 断言：数据目录还在
    if not DRY and not data_dir.is_dir():
        print("\n✗ 严重：清理后数据目录不见了，立即从备份恢复！")
        print(f"   python -c \"import shutil;shutil.copytree(r'{backup}', r'{data_dir}')\"")
        return 1

    # ---- 3) 复制新版本 ----
    print("\n[3] 复制新版本")
    for name in sorted(os.listdir(SRC)):
        s = SRC / name
        d = TARGET / name
        if name == "fengcode-data":
            continue
        # 交付包里不含用户数据；resources 下的 fengcode-data 也要跳过
        if s.is_dir():
            if name == "resources":
                log("复制目录 resources/（跳过其中的 fengcode-data）")
                if not DRY:
                    d.mkdir(parents=True, exist_ok=True)
                    for sub in s.iterdir():
                        if sub.name == "backend" and sub.is_dir():
                            bd = d / "backend"
                            bd.mkdir(parents=True, exist_ok=True)
                            for be in sub.iterdir():
                                if be.name == DATA_REL.parts[-1]:
                                    log(f"跳过复制 resources/backend/{be.name}")
                                    continue
                                dest = bd / be.name
                                if be.is_dir():
                                    shutil.copytree(be, dest, dirs_exist_ok=True)
                                else:
                                    shutil.copy2(be, dest)
                        else:
                            dest = d / sub.name
                            if sub.is_dir():
                                shutil.copytree(sub, dest, dirs_exist_ok=True)
                            else:
                                shutil.copy2(sub, dest)
            else:
                log(f"复制目录 {name}/")
                if not DRY:
                    shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            log(f"复制 {name}")
            if not DRY:
                shutil.copy2(s, d)

    # ---- 4) 校验数据指纹 ----
    print("\n[4] 校验数据指纹（必须与更新前一致）")
    after = fingerprint(data_dir)
    print(f"      更新后：{after[0]} 文件 / {after[1]} 字节 / md5 {after[2]}")
    if after != before:
        print("      ✗ 数据指纹变了！请从备份恢复：")
        print(f"        {backup}")
        return 1
    print("      ✅ 数据完好无损")

    # ---- 5) 校验程序文件 ----
    print("\n[5] 校验程序文件")
    rel_static = Path("resources") / "backend" / "_internal" / "fengcode" / "server" / "static"
    for f in ("app.js", "app.css", "index.html"):
        p1 = SRC / rel_static / f
        p2 = TARGET / rel_static / f
        if p1.is_file() and p2.is_file():
            h1 = hashlib.md5(p1.read_bytes()).hexdigest()[:10]
            h2 = hashlib.md5(p2.read_bytes()).hexdigest()[:10]
            print(f"      {f:<11} {h2} {'✅' if h1 == h2 else '✗ 不一致'}")
    th = TARGET / rel_static / "themes"
    if th.is_dir():
        print(f"      主题图：{len(os.listdir(th))} 张")

    print("\n" + "=" * 62)
    print("预演完成。确认无误后加 --apply 执行。" if DRY else "更新完成 ✅")
    print("=" * 62)
    if not DRY:
        # 数据指纹已校验一致 → 备份使命完成，自动清掉（不留在用户机器上占地方）
        try:
            shutil.rmtree(backup, ignore_errors=True)
            print("\n数据已校验无误，临时备份已自动清理。")
        except Exception:
            print(f"\n（临时备份保留在：{backup}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
