# -*- coding: utf-8 -*-
r"""验证：超长路径（>=259 字符）的视频能通过 HTTP Range 正常取流。

覆盖两个回归点：
1. 扫描器要把这类视频收进库（_absorb_media 用 scandir 句柄）；
2. 播放端要能打开（media.py 走 common.stat_path / open_path，加 \\?\UNC\ 前缀）。

用法：python _scaffold/_test_longmedia.py
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8801


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    lib = json.load(io.open(os.path.join(HERE, "data", "library.json"), encoding="utf-8"))
    target = None
    for it in lib["items"]:
        for v in it.get("videos") or []:
            if len(v["path"]) >= 240:
                target = v["path"]
                break
        if target:
            break
    if not target:
        print("FAIL: 库里没有超长路径视频，无法测试")
        return 1
    print("超长相对路径长度:", len(target))
    print("尾部:", target[-45:])

    port = free_port()
    py = sys.executable
    proc = subprocess.Popen(
        [py, os.path.join(HERE, "server.py"), "--port", str(port), "--no-browser"],
        cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/api/ping", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("FAIL: 服务没起来")
            return 1

        quoted = urllib.parse.quote(target, safe="/")
        url = f"{base}/media/{quoted}"
        req = urllib.request.Request(url, headers={"Range": "bytes=0-1023"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code = r.status
                body = r.read()
        except urllib.error.HTTPError as e:
            print(f"FAIL: HTTP {e.code} {e.read()[:200]}")
            return 1
        print(f"Range 请求: HTTP {code}, 收到 {len(body)} 字节")
        ok = code == 206 and len(body) == 1024
        print("结果:", "LONGPATH_MEDIA_OK" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        try:
            urllib.request.urlopen(base + "/api/shutdown", data=b"{}", timeout=5)
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
