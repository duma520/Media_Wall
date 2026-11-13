# -*- coding: utf-8 -*-
"""一次性验收：编译产物 Media_Wall.dist\\Media_Wall.exe 实跑。

覆盖：
1) --serve 隐藏模式起 HTTP 服务（临时媒体根，不碰 NAS 与真实 data/）
2) /api/status 版本、GET / 的 PAGE_VER 是否为本次版本
3) POST /api/shutdown → 进程退出、端口释放、无残留
4) GUI 无头（offscreen）启动 12s 不崩溃
最后清理 dist 里被 exe 自动创建的 data/、config.json、gui_settings.json。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

DIST = os.path.join("build_output", "Media_Wall.dist")
EXE = os.path.join(DIST, "Media_Wall.exe")
PORT = 8791
BASE = "http://127.0.0.1:%d" % PORT
# 期望版本不再写死：直接读源码里的 mediawall.VERSION，避免每次发版都要改脚本
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mediawall import VERSION as VER  # noqa: E402


def get(path, timeout=5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read()


def port_open(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port(port, up=True, limit=30.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        if port_open(port) == up:
            return True
        time.sleep(0.2)
    return False


def main():
    assert os.path.isfile(EXE), "找不到 %s" % EXE
    errors = []

    def check(name, cond, detail=""):
        print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else " | " + str(detail)))
        if not cond:
            errors.append(name)

    tmp = tempfile.mkdtemp(prefix="mw_exe_")
    media = os.path.join(tmp, "media")
    item = os.path.join(media, "01 \u7535\u5f71", "\u6d4b\u8bd5\u7247")
    os.makedirs(item)
    with open(os.path.join(item, "a.mp4"), "wb") as f:
        f.write(b"\x00" * 4096)

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"

    print("[1] --serve 起服务（临时媒体根）")
    proc = subprocess.Popen(
        [EXE, "--serve", "--port", str(PORT), "--root", media, "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        check("端口 %d 监听" % PORT, wait_port(PORT, True, 40))
        st, body = get("/api/ping")
        check("GET /api/ping 200", st == 200 and json.loads(body)["ok"] is True, body[:120])
        st, body = get("/api/status")
        j = json.loads(body)
        check("version=%s" % VER, j.get("version") == VER, j.get("version"))
        check("root 指向临时媒体根", os.path.normcase(j.get("root", "")) == os.path.normcase(media), j.get("root"))
        st, body = get("/")
        html = body.decode("utf-8", "replace")
        check("GET / 200", st == 200)
        check('PAGE_VER="%s"' % VER, ('PAGE_VER="%s"' % VER) in html)
        check("index.html 已打包进 dist",
              len(html) > 50000 and 'id="tabs"' in html, len(html))
        print("[2] POST /api/shutdown")
        req = urllib.request.Request(
            BASE + "/api/shutdown", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            check("shutdown 200", r.status == 200)
        check("端口释放", wait_port(PORT, False, 20))
        code = proc.wait(timeout=20)
        check("进程退出码 0", code == 0, code)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    print("[3] GUI 无头启动 12s")
    gui = subprocess.Popen([EXE, "--no-autostart"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=env)
    time.sleep(12)
    alive = gui.poll() is None
    check("GUI 12s 内存活（无崩溃）", alive, gui.poll())
    if alive:
        gui.kill()
        gui.wait()
    out = gui.stdout.read().decode("utf-8", "replace")
    tail = out.strip()[-300:]
    if tail:
        print("  GUI 输出尾部:", tail)

    print("[4] 清理 dist 运行时产物 + 临时目录")
    for name in ("data", "config.json", "gui_settings.json"):
        p = os.path.join(DIST, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    shutil.rmtree(tmp, ignore_errors=True)
    print("  dist 内 data/config/gui_settings:", os.path.exists(os.path.join(DIST, "data")))

    print("\n结果: %s" % ("EXE_OK" if not errors else "EXE_FAIL %s" % errors))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
