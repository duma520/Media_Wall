"""mediawall/restart.py —— 服务自重启（浏览器端「重启服务」按钮的服务端实现）。

为什么需要"自重启"
------------------
浏览器没有权限启动本机进程，所以网页上点「重启」只能由**服务进程自己**
拉起一个新实例，然后把旧进程关掉。本模块就负责这件事，并保证
「新进程起不来时，旧服务不受影响」——这是本设计最重要的性质。

握手协议（三个文件，都在 ``data/``）
----------------------------------
1. 旧进程收到 ``POST /api/restart`` → 写 ``restart.flag``（含旧 PID），
   然后 spawn 新进程，**自己继续正常服务**（此时不关 socket）；
2. 新进程启动时看到 ``restart.flag`` 存在 → 知道自己是被重启拉起的，
   立刻写 ``restart.ready``（含新 PID），然后带 ``--wait-port`` 重试绑定端口；
3. 旧进程轮询到 ``restart.ready`` → 关闭监听 socket 并退出；
4. 新进程下一次重试绑定成功 → 正常服务。

这样安排的原因：**旧进程直到确认新进程真的起来了才让出端口**，
所以新进程崩溃（语法错、依赖缺失）时旧服务完全不受影响，
不会出现"重启一下服务全没了"的情况。若 20 秒内没等到 ready，
旧进程直接放弃本次重启并把原因写进 ``data/restart.log``。

线程安全
--------
``request_restart`` 只允许一个重启在途（``_lock`` + ``_in_progress``），
重复点击返回 False，不会拉起第二个进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .paths import is_frozen, program_dir, pythonw, self_exe

#: 旧进程写、新进程读：存在即表示「本次启动是重启拉起的」
RESTART_FLAG = "restart.flag"
#: 新进程写、旧进程读：存在即表示「新进程已经起来了，可以退出让位」
RESTART_READY = "restart.ready"
#: 重启过程的日志（旧进程追加写，便于事后排查）
RESTART_LOG = "restart.log"

#: 等新进程 ready 的上限（秒）。Nuitka 冷启动 + 端口重试，20 秒足够宽裕。
READY_TIMEOUT = 20.0
#: 新进程等待端口释放的上限（秒）
WAIT_PORT_SECONDS = 30

_lock = threading.Lock()
_in_progress = False


def _data_dir() -> Path:
    return program_dir() / "data"


def _path(name: str) -> Path:
    return _data_dir() / name


def _append_log(text: str) -> None:
    """向 data/restart.log 追加一行；失败静默（不能因为写日志把重启搞挂）。"""
    try:
        line = "%s pid=%d %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), text)
        with open(_path(RESTART_LOG), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def build_command(host: str, port: int) -> list[str]:
    """构造新进程的启动命令，与 GUI / 启动.bat 的口径保持一致。

    编译态：Nuitka standalone 下 ``sys.executable`` 指向不存在的 ``python.exe``，
    必须用 ``self_exe()`` 自调用 ``--serve`` 隐藏模式（见 mediawall/paths.py）。
    源码态：显式传 ``server.py`` 路径。
    """
    if is_frozen():
        head = [self_exe(), "--serve"]
    else:
        # v2.5.1：用 pythonw.exe 拉起新实例，避免网页端「重启服务」时弹黑色控制台窗口
        head = [pythonw(), str(program_dir() / "server.py")]
    # v2.3.0：网页端「重启服务」拉起的新实例同样带 --no-browser，
    # 避免服务一重启就把用户的浏览器弹出来。
    # v2.5.3：带 --port-max，端口被占时新实例自动递增（与 GUI 启动口径一致）。
    return head + ["--host", host, "--port", str(port),
                   "--wait-port", str(WAIT_PORT_SECONDS),
                   "--port-max", str(port + 20),
                   "--no-browser"]


def mark_ready() -> bool:
    """**新进程**启动时调用：若本次是被重启拉起的，写 ready 告知旧进程让位。

    返回 True 表示「本次启动是重启流程的一部分」。
    """
    flag = _path(RESTART_FLAG)
    if not flag.exists():
        return False
    try:
        _path(RESTART_READY).write_text(str(os.getpid()), encoding="utf-8")
        flag.unlink(missing_ok=True)
        _append_log("新进程已启动并让位（ready 已写）")
        return True
    except OSError as exc:  # noqa: BLE001 - 握手失败不应阻止服务启动
        _append_log("写 ready 失败：%s" % exc)
        return False


def request_restart(srv, host: str, port: int) -> bool:
    """**旧进程**调用：拉起新进程并在其就绪后退出。

    幂等：已有重启在途时直接返回 False。真正的动作放在后台线程，
    这样 HTTP 响应能先返回给浏览器（浏览器据此显示"正在重启"）。
    """
    global _in_progress
    with _lock:
        if _in_progress:
            return False
        _in_progress = True

    try:
        _path(RESTART_READY).unlink(missing_ok=True)
        _path(RESTART_FLAG).write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        _append_log("写 flag 失败，放弃重启：%s" % exc)
        with _lock:
            _in_progress = False
        return False

    threading.Thread(
        target=_restart_worker, args=(srv, host, port),
        name="mw-restart", daemon=True,
    ).start()
    return True


def _restart_worker(srv, host: str, port: int) -> None:
    global _in_progress
    cmd = build_command(host, port)
    _append_log("开始重启：%s" % " ".join(cmd))

    log_fh = None
    proc = None
    try:
        try:
            log_fh = open(_path(RESTART_LOG), "a", encoding="utf-8", errors="replace")
        except OSError:
            log_fh = None

        # DETACHED_PROCESS：新进程不随旧进程的控制台/作业对象一起结束
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        proc = subprocess.Popen(
            cmd,
            cwd=str(program_dir()),
            stdin=subprocess.DEVNULL,
            stdout=log_fh or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        _append_log("新进程已派生 pid=%s" % proc.pid)

        if not _wait_ready(proc):
            _append_log("等待新进程就绪超时（%.0fs），放弃重启，旧服务继续运行。" % READY_TIMEOUT)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - 尽力而为
                pass
            _cleanup_flags()
            with _lock:
                _in_progress = False
            return

        _append_log("新进程就绪，旧进程退出让位。")
        # 先停 accept，再释放监听 socket，新进程随后绑定成功
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
        # 直接退出：main 已经无法回到 serve_forever，且此时新进程接管了端口
        time.sleep(0.3)
        os._exit(0)
    except Exception as exc:  # noqa: BLE001 - 任何失败都不能影响旧服务
        _append_log("重启失败：%r" % exc)
        _cleanup_flags()
        with _lock:
            _in_progress = False
    finally:
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass


def _wait_ready(proc: subprocess.Popen) -> bool:
    """等新进程写 ready；期间若新进程已退出则立即判失败。"""
    ready = _path(RESTART_READY)
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if ready.exists():
            return True
        if proc.poll() is not None:
            _append_log("新进程提前退出（returncode=%s）" % proc.returncode)
            return False
        time.sleep(0.2)
    return False


def _cleanup_flags() -> None:
    for name in (RESTART_FLAG, RESTART_READY):
        try:
            _path(name).unlink(missing_ok=True)
        except OSError:
            pass
