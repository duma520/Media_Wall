"""Media_Wall 桌面控制面板 —— 服务进程管理（PySide6）。

本模块只负责「控制服务」这一件事，不碰海报墙前端，也不改服务端逻辑：

* **启动**：以本机 Python 启动 ``server.py``（等价 启动.bat），把
  stdout/stderr 收进 GUI 日志区；
* **端口清理**：复用 启动.bat 的精确匹配口径（``:PORT `` + ``LISTENING``），
  只结束真正监听该端口的进程，**绝不**用宽容正则误杀系统进程
  （历史事故：``:8787.*LISTENING`` 会连带匹配 7/9/80/135/445/3389 端口，
  把 PID 4 (System)、RPC、SMB 等系统服务当成目标）；
* **状态轮询**：后台线程每 1.5s 调 ``GET /api/status``，解析服务与扫描状态，
  并通过信号回主线程刷新界面（耗时/网络操作不阻塞 UI）；
* **扫描进度**：``scanner.py`` 每扫完一个分区写 ``data/scan_progress.json``，
  服务端 ``/api/status`` 的 ``scan.progress`` 暴露它；本模块据此换算百分比；
* **停止**：优先 ``POST /api/shutdown``（仅 localhost 可用），超时再 kill。

线程模型：本类在 GUI 主线程创建；读子进程输出与 HTTP 轮询都在独立
Python 线程里跑，结果一律用 Qt 信号回主线程（Qt 信号跨线程是队列投递，
天然线程安全）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from mediawall.common import no_window
from mediawall.paths import is_frozen, program_dir, pythonw, self_exe

PROGRAM_DIR = program_dir()
SERVER_PY = PROGRAM_DIR / "server.py"
#: 编译成 exe 后没有 server.py 可传，改为让 exe 自己以 --serve 模式再起一个进程
FROZEN = is_frozen()

#: 系统关键 PID（System / 内核），任何情况下都不允许被本程序结束。
_PROTECTED_PIDS = frozenset({0, 4})


# ---------------------------------------------------------------------------
# 端口工具（复刻 启动.bat 的精确匹配，避免误杀）
# ---------------------------------------------------------------------------

def find_listening_pids(port: int) -> list[int]:
    """精确找出监听指定端口的 PID。

    匹配口径与 启动.bat 完全一致：行内必须出现 ``:PORT ``（端口后紧跟空格），
    且该行含 ``LISTENING``。这样 ``:8787 `` 不会匹配 ``:18787 ``，也不会
    匹配 ``:87870 ``，从而不会波及无关端口。
    """
    try:
        raw = subprocess.run(
            ["netstat", "-ano"], capture_output=True, timeout=20,
            creationflags=no_window(),
        ).stdout
        out = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 端口探测失败时按「无占用」处理
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


def kill_port(port: int, log: Callable[[str], None] | None = None) -> None:
    """结束占用指定端口的旧进程（等价 启动.bat 的重启清理）。"""
    for pid in find_listening_pids(port):
        if pid in _PROTECTED_PIDS:
            if log:
                log(f"[gui] 跳过受保护系统进程 PID={pid}")
            continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=15,
                creationflags=no_window(),
            )
            if log:
                log(f"[gui] 已结束占用端口 {port} 的旧进程 PID={pid}")
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"[gui] 结束 PID={pid} 失败：{exc}")


def port_in_use(port: int) -> bool:
    """端口当前是否已有进程在监听。"""
    return bool(find_listening_pids(port))


# ---------------------------------------------------------------------------
# 二维码
# ---------------------------------------------------------------------------

def qr_matrix(url: str) -> list[list[bool]] | None:
    """生成二维码 0/1 矩阵；qrcode 库缺失或失败时返回 None（界面降级为纯 URL）。"""
    try:
        import qrcode  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.make(fit=True)
        return [[bool(cell) for cell in row] for row in q.get_matrix()]
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 服务管理
# ---------------------------------------------------------------------------

class ServiceManager(QObject):
    """管理 server.py 子进程：启停、日志、状态轮询、扫描进度。"""

    log_line = Signal(str)             # 一行日志文本
    status_changed = Signal(dict)      # /api/status 的 JSON payload
    state_changed = Signal(str)        # starting/running/stopping/stopped/failed
    scan_progress = Signal(int, str)   # (百分比 0-100；-1=不定进度, 说明文本)
    port_changed = Signal(int)         # v2.5.3：端口被占自动升级后通知 GUI 刷新 URL

    def __init__(self, port: int = 8787, host: str = "0.0.0.0",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.port = int(port)
        self.host = host
        self._proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()
        self._polling = False
        self._poll_thread: threading.Thread | None = None
        self._last_scan_state: tuple[Any, Any] = (None, None)

    # ---- 基本属性 ----

    @property
    def running(self) -> bool:
        with self._proc_lock:
            return self._proc is not None and self._proc.poll() is None

    def _log(self, text: str) -> None:
        self.log_line.emit(text)

    def _set_proc(self, proc: subprocess.Popen[str] | None) -> None:
        with self._proc_lock:
            self._proc = proc

    def _server_base(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}"

    # ---- 启动 / 停止 ----

    def _find_free_port(self, start: int | None = None, max_try: int = 20) -> int:
        """从起始端口开始递增，返回第一个空闲端口（v2.5.3 端口自动升级）。

        被占用的端口（8787 → 8788 → 8789 …）自动跳过；全部占用返回起始端口
        （由 server.py 的 --port-max 兜底再试）。
        """
        start = start or self.port
        for p in range(start, start + max_try):
            if not port_in_use(p):
                return p
        return start

    def start(self) -> bool:
        """同步启动（调用方应在工作线程里调，避免阻塞 UI）。"""
        if self.running:
            self._log("[gui] 服务已在运行。")
            return True
        if not FROZEN and not SERVER_PY.is_file():
            self._log(f"[gui] 找不到 server.py：{SERVER_PY}")
            self.state_changed.emit("failed")
            return False

        self.state_changed.emit("starting")
        # 1) 清理旧进程（等价 启动.bat）
        kill_port(self.port, log=self._log)
        time.sleep(0.5)
        # 2) 端口自动升级：从当前端口开始找第一个空闲的（v2.5.3）
        actual = self._find_free_port()
        if actual != self.port:
            self._log(f"[gui] 端口 {self.port} 被占用，改用端口 {actual}。")
            self.port = actual
            self.port_changed.emit(actual)
        if port_in_use(self.port):
            self._log(f"[gui] 端口 {self.port} 仍被占用（可能属于其他账户），无法启动。")
            self.state_changed.emit("failed")
            return False

        # 3) 启动子进程（UTF-8 环境与 启动.bat 保持一致）
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 编译态：用 self_exe() 自调用隐藏模式 --serve（sys.executable 在 Nuitka
        # standalone 下指向不存在的 <dist>\python.exe，见 paths.self_exe 注释）；
        # 源码态：用 pythonw.exe 启动 server.py（v2.5.1：避免弹黑色控制台窗口）。
        head = [self_exe(), "--serve"] if FROZEN else [pythonw(), str(SERVER_PY)]
        # v2.3.0：GUI 从控制面板启动服务时**不**自动打开浏览器——
        # 浏览器由用户在面板「打开浏览器」按钮或客户端 URL 自行打开。
        # v2.5.3：传 --port-max，让 server.py 在启动竞态下也能自动递增。
        cmd = head + ["--host", self.host, "--port", str(self.port),
                      "--port-max", str(self.port + 20), "--no-browser"]
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(PROGRAM_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, encoding="utf-8", errors="replace",
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"[gui] 启动失败：{exc}")
            self.state_changed.emit("failed")
            return False
        self._set_proc(proc)
        self._log(f"[gui] 服务进程已启动 PID={proc.pid}")

        # 4) 读输出线程
        threading.Thread(
            target=self._read_output, args=(proc,),
            name="mw-gui-reader", daemon=True,
        ).start()

        # 5) 状态轮询线程
        self._start_polling()

        # 6) 短暂等待，确认进程没有立刻退出（端口冲突等）
        time.sleep(1.0)
        if proc.poll() is not None:
            self._log("[gui] 服务进程启动后立即退出，请查看上方日志。")
            self._set_proc(None)
            self.state_changed.emit("failed")
            return False
        self.state_changed.emit("running")
        return True

    def stop(self) -> None:
        """停止服务：优先 /api/shutdown，超时再 kill 子进程。"""
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            self._set_proc(None)
            self.state_changed.emit("stopped")
            return

        self.state_changed.emit("stopping")
        self._polling = False
        # 优雅关闭（仅 localhost 有效）
        try:
            req = urllib.request.Request(
                self._server_base() + "/api/shutdown", data=b"{}",
                method="POST", headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:  # noqa: BLE001 - 关闭失败就走强杀
            pass
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self._log("[gui] 优雅关闭超时，强制结束进程。")
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._set_proc(None)
        self._log("[gui] 服务已停止。")
        self.state_changed.emit("stopped")

    # ---- 后台线程 ----

    def _read_output(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                self.log_line.emit(line.rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass

    def _start_polling(self) -> None:
        if self._polling:
            return
        self._polling = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="mw-gui-poll", daemon=True,
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        while self._polling:
            with self._proc_lock:
                proc = self._proc
            if proc is None:
                break
            if proc.poll() is not None:
                # v2.2.0 探活容错：服务可能已被「重启服务」接管（旧子进程退出、
                # 新实例在跑）。此时若 /api/ping 仍可达，就认为服务仍在运行，
                # 只是由外部进程接管 —— 不能把 GUI 状态错标成「已停止」。
                try:
                    with urllib.request.urlopen(
                        self._server_base() + "/api/ping", timeout=2.5,
                    ) as resp:
                        if resp.status == 200:
                            self._log("[gui] 服务进程已退出，但端口仍由新进程接管（可能是网页端重启），状态保持运行中。")
                            self._polling = False
                            self._set_proc(None)   # 不再管理旧子进程
                            self.state_changed.emit("running")
                            break
                except Exception:  # noqa: BLE001 - 端口也不通，确认是停了
                    pass
                self._polling = False
                self._set_proc(None)
                self.log_line.emit("[gui] 服务进程已退出。")
                self.state_changed.emit("stopped")
                break
            try:
                with urllib.request.urlopen(
                    self._server_base() + "/api/status", timeout=2.5,
                ) as resp:
                    payload: dict[str, Any] = json.loads(
                        resp.read().decode("utf-8", "replace"))
                self.status_changed.emit(payload)
                self._emit_scan_progress(payload)
            except Exception:  # noqa: BLE001 - 服务未就绪/瞬断，忽略
                pass
            time.sleep(1.5)

    def _emit_scan_progress(self, payload: dict[str, Any]) -> None:
        """把 /api/status 的 scan 字段翻译成进度条信号（去重，少刷 UI）。"""
        scan = payload.get("scan") or {}
        running = bool(scan.get("running"))
        prog = scan.get("progress") or {}
        pct = -1
        text = "未扫描"
        if running:
            total = 0
            done = 0
            if isinstance(prog, dict):
                try:
                    total = int(prog.get("total") or 0)
                    done = int(prog.get("done") or 0)
                except (TypeError, ValueError):
                    total = done = 0
            if total > 0:
                pct = max(0, min(100, int(done * 100 / total)))
                cat = str(prog.get("category") or "")
                text = f"扫描中 {done}/{total}" + (f"：{cat}" if cat else "")
            else:
                text = "扫描中…"
        elif scan.get("ok") is True:
            pct = 100
            text = "扫描完成"
        elif scan.get("ok") is False:
            text = "扫描失败：" + str(scan.get("error") or "未知错误")
        key = (pct, text)
        if key == self._last_scan_state:
            return
        self._last_scan_state = key
        self.scan_progress.emit(pct, text)

    # ---- 附加动作 ----

    def trigger_scan(self, category: str | None = None, force: bool = False) -> bool:
        """触发后台扫描（等价页面里的「重新扫描」）。

        ``force=True`` 时向服务端发 ``{"force": true}`` → 全量重建
        （忽略增量缓存，比增量慢很多，仅在缓存异常时用）。
        """
        body: dict[str, Any] = {}
        if category:
            body["category"] = category
        if force:
            body["force"] = True
        try:
            req = urllib.request.Request(
                self._server_base() + "/api/scan",
                data=json.dumps(body).encode("utf-8"),
                method="POST", headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            self._log("[gui] 已触发扫描（后台进行，进度见进度条）。")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"[gui] 触发扫描失败：{exc}")
            return False
