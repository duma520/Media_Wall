# -*- coding: utf-8 -*-
"""诊断：磁盘上有视频、但库里完全没有收录的目录（只读，不改任何文件）。

用法：
    python _scaffold/_diag_unlisted_videos.py [最多输出条数]

判定口径：把库里所有条目的 videos[].path（相对媒体根）收进一个集合，
再遍历媒体根，找出「本层含视频、但本层视频一个都不在集合里」的目录。
这些目录就是「明明有视频却没出现在海报墙」的候选。

输出每条附：目录相对路径、视频数、扩展名分布、库里同前缀的条目（用于判断
是不是被上层条目吸收 / 被忽略规则跳过 / 扩展名不认识）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from mediawall.common import VIDEO_EXTS, load_config  # noqa: E402
from mediawall.scanner_core import (  # noqa: E402
    HARD_DEPTH,
    is_ignored_dir,
)


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    cfg = load_config()
    root = cfg["media_root"]
    lib_path = os.path.join(HERE, "data", "library.json")
    with open(lib_path, encoding="utf-8") as fh:
        lib = json.load(fh)

    known: set[str] = set()
    for item in lib["items"]:
        for vid in item.get("videos") or []:
            if vid.get("path"):
                known.add(str(vid["path"]).replace("\\", "/"))
    keys = [str(it.get("key") or "") for it in lib["items"]]

    print(f"媒体根: {root}")
    print(f"库中条目 {len(lib['items'])} 个，已收录视频路径 {len(known)} 条")
    print(f"开始遍历磁盘（只读）… HARD_DEPTH={HARD_DEPTH}")

    t0 = time.time()
    orphans: list[tuple[str, list[str], Counter]] = []
    deep: list[tuple[str, int]] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        # 剪枝：忽略目录不进入
        kept = []
        for d in dirnames:
            if is_ignored_dir(d):
                continue
            kept.append(d)
        dirnames[:] = kept
        if depth > HARD_DEPTH:
            if any(os.path.splitext(f)[1].lower() in VIDEO_EXTS for f in filenames):
                deep.append((rel_dir, depth))
            dirnames[:] = []
            continue

        vids = [f for f in filenames
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
        if not vids:
            continue
        scanned += 1
        prefix = "" if rel_dir == "." else rel_dir + "/"
        missing = [f for f in vids if (prefix + f) not in known]
        if len(missing) == len(vids):
            exts = Counter(os.path.splitext(f)[1].lower() for f in vids)
            orphans.append((rel_dir, vids, exts))

    print(f"遍历完成：含视频目录 {scanned} 个，用时 {time.time() - t0:.1f}s")
    print(f"其中「视频全部未收录」的目录: {len(orphans)} 个")
    print(f"超深（>{HARD_DEPTH} 层）被剪掉且含视频的目录: {len(deep)} 个")

    for rel_dir, vids, exts in orphans[:limit]:
        parent_keys = [k for k in keys if k and (k + "/") == rel_dir[: len(k) + 1]]
        print("\n" + "=" * 70)
        print(f"[未收录] {rel_dir or '(媒体根本身)'}")
        print(f"  本层视频 {len(vids)} 个，扩展名 {dict(exts)}")
        for name in vids[:4]:
            print(f"    - {name}")
        if len(vids) > 4:
            print(f"    … 其余 {len(vids) - 4} 个")
        if parent_keys:
            print(f"  库里同前缀条目: {parent_keys[:3]}")
        else:
            # 找一下有没有条目是本目录的祖先，说明原因大概率是「被上层条目吸收」
            anc = [k for k in keys if k and rel_dir.startswith(k + "/")]
            if anc:
                print(f"  被上层条目吸收: {sorted(anc, key=len)[-2:]}")
            else:
                print("  库里没有任何相关条目（连祖先都没有）")

    if deep[:limit]:
        print("\n" + "=" * 70)
        print("[超深剪枝] 这些目录里的视频不会进库：")
        for rel_dir, depth in deep[:limit]:
            print(f"  深度 {depth}: {rel_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
