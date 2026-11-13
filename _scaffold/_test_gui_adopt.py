"""GUI 探活容错测试（v2.2.0）。

场景：网页端「重启服务」后，GUI 管理的旧子进程退出，但新进程（外部接管）
已把端口占了。GUI 必须判为「仍运行」而不是「已停止」。

做法：真实起一个服务（旧），然后独立另起一个服务占同一端口（模拟“新实例接管”），
再让 GUI 的 ServiceManager 指向旧进程；当旧进程退出时，_poll_loop 发现 /api/ping
仍通 → 应保持 running。

全程临时目录 + 独立端口，不碰真实 NAS / data / 8787。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8812

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except Exception:
        return False


def wait_port_open(timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_open():
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    if port_open():
        print(f"端口 {PORT} 已被占用，先停掉再跑")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="mw_adopt_"))
    media = tmp / "media"
    os.makedirs(media, exist_ok=True)
    for name in ("server.py", "scanner.py", "index.html"):
        src = PROG / name
        if src.exists():
            shutil.copy2(src, tmp / name)
    shutil.copytree(PROG / "mediawall", tmp / "mediawall",
                    ignore=shutil.ignore_patterns("__pycache__"))

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        # 1) 起“旧”服务（GUI 将要管理的进程）
        old = subprocess.Popen(
            [PY, "server.py", "--root", str(media), "--port", str(PORT), "--no-browser"],
            cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
        if not wait_port_open():
            print("旧服务没起来")
            return 3

        # 2) 再起一个“新”进程占同一个端口（模拟网页端重启后的外部接管）
        #    新进程带 --wait-port：等旧进程退出后自动接管。
        new = subprocess.Popen(
            [PY, "server.py", "--root", str(media), "--port", str(PORT),
             "--no-browser", "--wait-port", "20"],
            cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)

        # 3) 模拟网页端重启：旧进程收到 shutdown 退出；新进程接管端口
        try:
            import urllib.request

            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/shutdown", data=b"{}",
                method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

        # 等旧进程退出、新进程接管端口
        t0 = time.time()
        while time.time() - t0 < 20 and old.poll() is None:
            time.sleep(0.2)
        check("旧进程已退出", old.poll() is not None, "poll=" + str(old.poll()))
        # 新进程带 --wait-port，会等端口释放后绑定；等端口重新可连
        t0 = time.time()
        while time.time() - t0 < 25 and not port_open():
            time.sleep(0.3)
        check("新进程已接管端口", port_open(), "新进程接管失败")

        # 4) GUI 用 ServiceManager 模拟：指向“已退出的旧进程”，端口被新进程接管
        sys.path.insert(0, str(PROG))
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        app = QApplication([])
        from gui_service import ServiceManager

        sm = ServiceManager(port=PORT, host="127.0.0.1")
        states: list[str] = []
        sm.state_changed.connect(states.append)
        # 伪造：GUI 曾启动过旧进程，现在旧进程已死、端口已被新进程接管
        sm._set_proc(old)
        sm._start_polling()
        # Qt 信号是队列投递：必须让主线程事件循环跑起来，states 才能收到
        t0 = time.time()
        while time.time() - t0 < 6 and "running" not in states and "stopped" not in states:
            app.processEvents()
            time.sleep(0.3)
        app.processEvents()

        check("探活容错：发出 running 状态（不误判 stopped）",
              "running" in states and "stopped" not in states,
              "states=" + str(states))
        check("GUI 已放弃管理旧子进程", sm._proc is None,
              "proc=" + str(sm._proc))

        # 5) 清理新进程
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/shutdown", data=b"{}",
                method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
        time.sleep(2)
        if new.poll() is None:
            new.terminate(); new.wait(timeout=5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\n结果: FAIL %s" % ", ".join(fails))
        return 1
    print("\nGUI_ADOPT_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
