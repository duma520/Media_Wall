"""端到端验收：网页端「重启服务」（真实进程，临时副本，不碰 NAS/data）。

验证点：
1. 起第一个服务（源码态）→ 记录 PID；
2. 通过 HTTP POST /api/restart（带同源 Origin）→ 应返回 202；
3. 等待：旧进程退出，新进程（不同 PID）接管同一端口，/api/ping 恢复；
4. 权限：跨站 Origin / 公网 IP 来源应被 403 拒绝。

注意：脚本内部用「伪造来源 IP」验证权限会非常绕（socket 层无法直接指定 peer IP），
所以权限校验拆成单元级：直接调用 Handler._is_private_client 的判定逻辑验证
回环/私有/公网三类地址。HTTP 层只验证同源放行与跨站拒绝。
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
import urllib.request
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8811

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


def get_pid_on_port() -> str:
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout
    for line in out.splitlines():
        if f":{PORT} " in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                return parts[-1]
    return ""


def post(path: str, body: dict | None = None, origin: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else b"{}"
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mw_restart_"))
    media = tmp / "media"
    os.makedirs(media, exist_ok=True)
    for name in ("server.py", "scanner.py", "index.html", "config.json"):
        src = PROG / name
        if src.exists():
            shutil.copy2(src, tmp / name)
    shutil.copytree(PROG / "mediawall", tmp / "mediawall",
                    ignore=shutil.ignore_patterns("__pycache__"))

    # 端口先确认空闲
    if port_open():
        print(f"端口 {PORT} 已被占用，先停掉再跑")
        return 2

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        # ---- 阶段 1：起旧服务 ----
        proc1 = subprocess.Popen(
            [PY, "server.py", "--root", str(media), "--port", str(PORT), "--no-browser"],
            cwd=str(tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
        t0 = time.time()
        while time.time() - t0 < 40 and not port_open():
            time.sleep(0.3)
        if not port_open():
            print("旧服务没起来：\n%s" % (proc1.stdout.read() if proc1.stdout else ""))
            return 3
        pid1 = get_pid_on_port()
        print(f"旧服务 PID={pid1} 端口 {PORT} 已监听")

        # ---- 阶段 2：权限单元验证 ----
        sys.path.insert(0, str(PROG))
        from mediawall.httpd import MediaWallHandler
        from unittest import mock

        def judge(addr):
            with mock.patch.object(MediaWallHandler, "client_address",
                                   new=(addr, 9999), create=True):
                h = object.__new__(MediaWallHandler)
                h.client_address = (addr, 9999)
                return h._is_private_client()

        check("本机 127.0.0.1 允许", judge("127.0.0.1") is True)
        check("私有网段 192.168.1.50 允许", judge("192.168.1.50") is True)
        check("私有网段 10.1.2.3 允许", judge("10.1.2.3") is True)
        check("公网 8.8.8.8 拒绝", judge("8.8.8.8") is False)
        check("公网 114.114.114.114 拒绝", judge("114.114.114.114") is False)

        # ---- 阶段 3：HTTP 同源放行 + 跨站拒绝 ----
        st, js = post("/api/restart", {}, origin="http://127.0.0.1:%d" % PORT)
        check("同源 POST /api/restart -> 202", st == 202 and js.get("ok"),
              f"status={st} {js}")
        # 注意：同源请求已触发重启，旧服务正在退出；跨站测试要在新服务起来后做

        # ---- 阶段 4：等旧进程退出、新进程接管 ----
        t0 = time.time()
        old_died = False
        new_pid = ""
        while time.time() - t0 < 60:
            p2 = get_pid_on_port()
            if p2 and p2 != pid1:
                new_pid = p2
                break
            if proc1.poll() is not None:
                old_died = True
            time.sleep(0.4)
        check("旧进程已退出", proc1.poll() is not None, "poll=" + str(proc1.poll()))
        check("新进程接管端口（PID 不同）", new_pid and new_pid != pid1,
              f"old={pid1} new={new_pid}")
        check("新进程 /api/ping 恢复",
              bool(new_pid) and port_open() and
              (lambda: (lambda r: r.status == 200)(
                  urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=3)))()
              is True)

        # ---- 阶段 5：新进程上测跨站拒绝 ----
        st, js = post("/api/restart", {}, origin="http://evil.example")
        check("跨站 Origin 被拒 403", st == 403, f"status={st} {js}")
        check("跨站请求未触发重启（PID 未变）",
              get_pid_on_port() == new_pid, "pid=" + get_pid_on_port())

        # ---- 阶段 6：清理新进程 ----
        try:
            post("/api/shutdown", {})
        except Exception:
            pass
        time.sleep(1)
        if port_open():
            # 兜底强杀
            subprocess.run(["taskkill", "/F", "/PID", new_pid],
                           capture_output=True, timeout=15)
        print("\nrestart.log 尾部：")
        log = tmp / "data" / "restart.log"
        if log.exists():
            print(log.read_text(encoding="utf-8", errors="replace")[-600:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\n结果: FAIL %s" % ", ".join(fails))
        return 1
    print("\nRESTART_E2E_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
