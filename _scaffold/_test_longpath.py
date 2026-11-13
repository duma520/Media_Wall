# -*- coding: utf-8 -*-
r"""验证 common.long_path / stat_path / open_path 能处理 NAS 上的超长路径。

背景：扫描器已收录 106 个全路径 >=259 字符的视频，但播放端直接 open
会 FileNotFoundError（系统找不到指定的路径），表现为「点开就 404」。
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from mediawall.common import long_path, open_path, stat_path  # noqa: E402

root = json.load(io.open(os.path.join(HERE, "config.json"), encoding="utf-8"))["media_root"]
lib = json.load(io.open(os.path.join(HERE, "data", "library.json"), encoding="utf-8"))

print("root =", root)
print("long_path(root) =", long_path(root))

target = None
for it in lib["items"]:
    for v in it.get("videos") or []:
        p = os.path.join(root, v["path"].replace("/", os.sep))
        if len(p) >= 259:
            target = p
            break
    if target:
        break

print("\n超长样本长度:", len(target))
print("尾部:", target[-50:])

print("\n[1] os.stat 原样:", end=" ")
try:
    print("OK", os.stat(target).st_size)
except OSError as e:
    print("FAIL", e.strerror)

print("[2] stat_path:", end=" ")
try:
    print("OK", stat_path(target).st_size)
except OSError as e:
    print("FAIL", e.strerror)

print("[3] open_path:", end=" ")
try:
    with open_path(target) as f:
        print("OK, head =", f.read(4))
except OSError as e:
    print("FAIL", e.strerror)

print("\n[4] 短路径不受影响（回归）:", end=" ")
short = os.path.join(root, "config.json")
try:
    print(stat_path(os.path.join(HERE, "config.json")).st_size, "字节")
except OSError as e:
    print("FAIL", e.strerror)

print("\n[5] 不存在的超长路径仍报错（不能吞掉）:", end=" ")
try:
    stat_path(target + ".no-such-file")
    print("FAIL：居然没报错")
except OSError as e:
    print("OK，正确抛出", type(e).__name__)
