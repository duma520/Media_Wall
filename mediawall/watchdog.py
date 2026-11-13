"""mediawall/watchdog.py —— 常驻看门狗：网页端启动 / 重启 / 停止 Media_Wall 服务。

为什么需要看门狗
----------------
浏览器只能访问「正在运行」的服务：Media_Wall 服务没启动时，任何 HTML 都无法
从浏览器加载（先有鸡还是先有蛋）。所以加一个**独立常驻**的小 HTTP 服务
（固定端口 8786），它永远在跑，提供：

* ``GET  /api/status``   —— Media_Wall 服务是否在跑（探测 8787 端口）+ 本机信息
* ``POST /api/start``    —— 拉起 Media_Wall 服务（server.py，pythonw 无窗口）
* ``POST /api/restart``  —— 重启（先 stop 再 start；或复用自重启协议）
* ``POST /api/stop``     —— 停止 Media_Wall 服务（POST /api/shutdown 或 kill）

配合方案 A 的 ``server.html``（由 Media_Wall 自己静态提供），用户在任何设备
打开 ``http://<IP>:8786/`` 也能控制；打开 ``http://<IP>:8787/server.html``
（服务已运行时）走方案 A 的 API。

安全
----
写操作（start/restart/stop）仅允许本机 + 局域网私有网段（与 /api/restart 同口径），
并要求同源；公网来源一律拒绝。端口 8786 与 8787 不同，但写操作门禁一致。

常驻方式
--------
独立进程运行（由 watchdog.vbs 无窗口拉起，或注册为开机自启）。它不随
Media_Wall 服务启停，始终在跑。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import VERSION
from .common import DEFAULT_PORT, detect_lan_ip, no_window
from .paths import program_dir, pythonw, self_exe

#: 看门狗自身端口（固定，避开 Media_Wall 的 8787；可用环境变量 MEDIAWALL_WD_PORT 覆盖）
WATCHDOG_PORT = int(os.environ.get("MEDIAWALL_WD_PORT", "8786"))
#: Media_Wall 服务端口（默认 8787；可用环境变量 MEDIAWALL_PORT 覆盖）
MEDIA_PORT = int(os.environ.get("MEDIAWALL_PORT", str(DEFAULT_PORT)))
#: Media_Wall 服务启动后的探测超时（秒）
_START_WAIT = 15.0
#: 看门狗 HTTP 端口绑定失败时的报错（不静默）

PROGRAM_DIR = program_dir()
SERVER_PY = PROGRAM_DIR / "server.py"


# ---------------------------------------------------------------------------
# 端口探测 / 进程管理
# ---------------------------------------------------------------------------

def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否已监听（本机探测）。"""
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def _find_pids(port: int) -> list[int]:
    """找出监听指定端口的 PID（精确匹配 :PORT ）。"""
    try:
        raw = subprocess.run(
            ["netstat", "-ano"], capture_output=True, timeout=20,
            creationflags=no_window(),
        ).stdout
        out = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    needle = f":{port} "
    pids: list[int] = []
    for line in out.splitlines():
        if needle not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid and pid not in pids:
            pids.append(pid)
    return pids


def _kill_port(port: int) -> None:
    """结束占用端口的进程（跳过受保护 PID）。"""
    for pid in _find_pids(port):
        if pid in (0, 4):  # System / 内核
            continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=15, creationflags=no_window(),
            )
        except Exception:  # noqa: BLE001
            pass


def _server_alive() -> bool:
    """Media_Wall 服务是否在跑（探测默认端口）。"""
    return _port_open(MEDIA_PORT)


def _server_cmd() -> list[str]:
    """构造 Media_Wall 服务的启动命令（与 GUI 口径一致：pythonw 无窗口 + --no-browser）。"""
    if getattr(sys, "frozen", False) or globals().get("__compiled__") is not None:
        head = [self_exe(), "--serve"]
    else:
        head = [pythonw(), str(SERVER_PY)]
    return head + ["--host", "0.0.0.0", "--port", str(MEDIA_PORT),
                   "--port-max", str(MEDIA_PORT + 20), "--no-browser"]


def start_server() -> dict[str, Any]:
    """启动 Media_Wall 服务（幂等：已在跑则返回 already_running）。"""
    if _server_alive():
        return {"ok": True, "already_running": True}
    try:
        proc = subprocess.Popen(
            _server_cmd(), cwd=str(PROGRAM_DIR),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=no_window(),
        )
    except OSError as exc:
        return {"ok": False, "error": f"启动失败：{exc}"}
    # 等待端口就绪
    deadline = time.time() + _START_WAIT
    while time.time() < deadline:
        if _server_alive():
            return {"ok": True, "pid": proc.pid}
        if proc.poll() is not None:
            return {"ok": False, "error": "服务进程启动后立即退出"}
        time.sleep(0.3)
    return {"ok": False, "error": "启动超时，端口未就绪"}


def stop_server() -> dict[str, Any]:
    """停止 Media_Wall 服务：先 /api/shutdown 优雅关闭，超时再 kill。"""
    if not _server_alive():
        return {"ok": True, "already_stopped": True}
    import urllib.request

    try:
        r = urllib.request.Request(
            f"http://127.0.0.1:{MEDIA_PORT}/api/shutdown", data=b"{}",
            method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=3)
    except Exception:  # noqa: BLE001
        pass
    # 等端口释放
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _server_alive():
            return {"ok": True}
        time.sleep(0.3)
    # 超时强杀
    _kill_port(MEDIA_PORT)
    time.sleep(1.0)
    return {"ok": not _server_alive(), "error": None if not _server_alive() else "无法停止"}


def restart_server() -> dict[str, Any]:
    """重启 Media_Wall 服务：stop → start。"""
    st = stop_server()
    if not st.get("ok"):
        return st
    return start_server()


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def _is_private(addr: str) -> bool:
    """客户端是否本机 / 局域网私有网段（与 /api/restart 同口径）。"""
    if not addr:
        return False
    if addr in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(addr.split("%")[0])
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


class WatchdogHandler(BaseHTTPRequestHandler):
    """看门狗 HTTP 处理。"""

    server_version = f"MediaWallWatchdog/{VERSION}"
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _reject(self) -> bool:
        """写操作门禁：非本机/局域网私有网段 → 403。返回 True 表示已拒绝。"""
        addr = self.client_address[0] if self.client_address else ""
        if not _is_private(addr):
            self._send_json(403, {"ok": False, "error": "only allowed from localhost or LAN"})
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._serve_index()
            elif path == "/api/status":
                self._serve_status()
            elif path == "/api/start":
                self._serve_start()
            elif path == "/api/restart":
                self._serve_restart()
            elif path == "/api/stop":
                self._serve_stop()
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except Exception:  # noqa: BLE001 - 单请求异常不能弄崩看门狗
            try:
                self._send_json(500, {"ok": False, "error": "internal error"})
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def _serve_index(self) -> None:
        """看门狗首页：极简控制台（内嵌 HTML，无需额外文件）。"""
        lan = detect_lan_ip()
        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Media_Wall 服务控制台</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:560px;margin:2rem auto;padding:0 1rem;background:#1a1d24;color:#e8eaed}}
h1{{font-size:1.4rem}} .card{{background:#242936;border:1px solid #39414f;border-radius:10px;padding:1rem;margin:1rem 0}}
.row{{display:flex;gap:.6rem;flex-wrap:wrap;margin:.8rem 0}}
button{{flex:1;min-width:130px;padding:.7rem;border:0;border-radius:8px;font-size:1rem;cursor:pointer;color:#fff}}
#st{{font-size:1.1rem;font-weight:600}} .ok{{color:#4ade80}} .bad{{color:#f87171}}
a{{color:#60a5fa}} code{{background:#1f2937;padding:2px 6px;border-radius:4px}}
</style></head><body>
<h1>Media_Wall 服务控制台 <span style="font-size:.8rem;color:#9ca3af">v{VERSION}</span></h1>
<div class="card"><div class="row"><span id="st">检测中…</span></div>
<div style="color:#9ca3af;font-size:.85rem">端口 <code>{MEDIA_PORT}</code> · 本机局域网 <code>{lan}</code></div></div>
<div class="card"><div class="row">
<button style="background:#2563eb" onclick="act('start')">▶ 启动服务</button>
<button style="background:#d97706" onclick="act('restart')">🔄 重启服务</button>
<button style="background:#dc2626" onclick="act('stop')">⏹ 停止服务</button>
</div></div>
<div class="card"><div style="margin-bottom:.5rem">访问地址</div>
<div><a href="http://{lan}:{MEDIA_PORT}/" target="_blank">http://{lan}:{MEDIA_PORT}/</a></div>
<div style="margin-top:.4rem"><a href="http://{lan}:{MEDIA_PORT}/server.html" target="_blank">server.html（服务运行时）</a></div>
</div>
<div class="card"><div id="msg" style="white-space:pre-wrap;font-size:.85rem;color:#9ca3af"></div></div>
<script>
var BASE=location.origin;
function poll(){{fetch(BASE+'/api/status').then(function(r){{return r.json()}}).then(function(j){{
var el=document.getElementById('st');
el.textContent=j.alive?'● 服务运行中':'○ 服务已停止';
el.className=j.alive?'ok':'bad';
}}).catch(function(){{document.getElementById('st').textContent='● 服务运行中';document.getElementById('st').className='ok';}})}}
function act(a){{var m=document.getElementById('msg');m.textContent='执行 '+a+' …';fetch(BASE+'/api/'+a,{{method:'POST'}}).then(function(r){{return r.json()}}).then(function(j){{
m.textContent=JSON.stringify(j,null,2);poll();setTimeout(poll,1500);
}}).catch(function(e){{m.textContent='请求失败: '+e.message}})}}
poll();setInterval(poll,3000);
</script></body></html>"""
        self._send_html(200, html)

    def _serve_status(self) -> None:
        self._send_json(200, {
            "ok": True,
            "alive": _server_alive(),
            "port": MEDIA_PORT,
            "watchdog_port": WATCHDOG_PORT,
            "lan_ip": detect_lan_ip(),
            "version": VERSION,
        })

    def _serve_start(self) -> None:
        if self.command != "POST":
            self._send_json(405, {"ok": False, "error": "use POST"})
            return
        if self._reject():
            return
        self._send_json(200, start_server())

    def _serve_restart(self) -> None:
        if self.command != "POST":
            self._send_json(405, {"ok": False, "error": "use POST"})
            return
        if self._reject():
            return
        self._send_json(200, restart_server())

    def _serve_stop(self) -> None:
        if self.command != "POST":
            self._send_json(405, {"ok": False, "error": "use POST"})
            return
        if self._reject():
            return
        self._send_json(200, stop_server())

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 看门狗不打请求日志（避免噪声；错误仍走 stderr）
        pass


def main(argv: list[str] | None = None) -> int:
    """看门狗入口：python watchdog.py（由 watchdog.vbs 无窗口拉起）。"""
    port = WATCHDOG_PORT
    if argv and argv[0].isdigit():
        port = int(argv[0])
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), WatchdogHandler)
    except OSError as exc:
        print(f"[watchdog] 端口 {port} 绑定失败：{exc}", file=sys.stderr, flush=True)
        return 1
    print(f"[watchdog] Media_Wall 看门狗 v{VERSION} 监听 :{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[watchdog] 已停止", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
