"""Media_Wall HTTP 服务与路由（由 AV_Wall v1.9.0 的 avwall/httpd.py 泛化）。

只依赖 Python 3.10 标准库。路由表：

============================  ===============================================
``GET  /``                  ``index.html``（程序目录，UTF-8，no-store）
``GET  /mediawall/*``       程序目录静态资源（.js/.css/.json/.ico）
``GET  /data/*``            程序目录 ``data/`` 静态资源
``GET  /api/ping``          健康检查
``GET  /api/library``       ``data/library.json``
``GET  /api/status``        服务与扫描状态
``GET  /api/info``          局域网地址 + 二维码矩阵
``GET  /api/categories``    分区清单（实时发现 + 各分区条目数/扫描时间）
``GET  /api/root``          读取/校验媒体根
``GET  /api/config``        读取完整配置
``POST /api/scan``          触发扫描（异步；body 可带 {"category": "01 电影"}）
``POST /api/root``          设置媒体根（仅本机）
``POST /api/config``        修改配置（仅本机）
``POST /api/shutdown``      停止服务（仅本机）
``GET  /media/<相对路径>``  视频/音频流，支持 ``Range``（206）与 ``HEAD``；
                            ``?track=N`` 音轨缓存、``?fmt=mp4|mp3`` 转换缓存（v2.9.0）
``GET  /thumb/<相对路径>``  图片，支持 ``HEAD``
``GET  /api/playinfo``      播放决策 direct/remux/…（v2.9.0，见 :mod:`mediawall.play`）
``POST /api/play/clear``    清空转换缓存（v2.9.0，同源）
``GET  /api/sync/progress``  全域进度（v2.8.0，见 :mod:`mediawall.syncstore`）
``POST /api/sync/progress``  上报进度 {"map": {key: pct}}（同源，max 合并）
``POST /api/sync/progress/clear``  清空全域记录（同源）
``GET  /api/sync/watched``  全域已看记录（v2.10.0，见 :mod:`mediawall.watchstore`）
``POST /api/sync/watched``  增量上报/整表导入已看（同源，带 rev 防复活）
``POST /api/sync/watched/clear``  清空全域已看（同源，rev++）
============================  ===============================================

与 AV_Wall 的差异
----------------
* 静态前缀 ``/avwall/`` → ``/mediawall/``；
* 新增 ``GET /api/categories``：实时列出媒体根下的一级分区目录，并合并
  ``data/library.json`` 中各分区的条目数与 ``library.meta.json`` 的扫描时间，
  供前端「16 分区选择」标签渲染；
* ``POST /api/scan`` 接受可选 ``category``：只重扫一个分区（其余分区的
  条目从缓存整体复用），全量首扫太慢时可按分区逐个补扫；
* ``GET /api/root`` 返回 ``categories``（原 ``content_dirs``）。

生命周期：服务**不会**因为「浏览器页面被关闭」而自动退出（AV_Wall v1.8.0
已删除保活看门狗：手机锁屏、切后台、弱网重连都会误判）。关闭途径只有两条：

* 在启动服务的命令行窗口里按 ``Ctrl+C``；
* 本机调用 ``POST /api/shutdown``（仅 localhost，且要求同源）。

``POST /api/keepalive`` 端点保留为**兼容占位**（返回 200 但什么都不做）。

写操作的跨站防护
----------------
``POST /api/root``、``POST /api/config``、``POST /api/scan``、``POST /api/shutdown``
仅允许本机（localhost）调用，**且**要求请求同源（``Origin`` 头必须与
本服务一致或缺省）。否则局域网里的手机只要打开一个恶意网页，该网页就能
用 ``fetch("http://127.0.0.1:8787/api/shutdown")`` 把本机服务关掉。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from typing import Any, cast

from . import SERVER_TOKEN, VERSION
from .common import (
    DEFAULT_MEDIA_ROOT,
    DENY_STATIC_NAMES,
    INDEX_HTML_NAME,
    LIBRARY_REL,
    MAX_BODY_BYTES,
    MAX_STATIC_BYTES,
    STATIC_CONTENT_TYPES,
    STATIC_EXTS,
    PathSecurityError,
    derive_root,
    detect_lan_ip,
    list_categories,
    load_config,
    paths_equivalent,
    resolve_within,
    save_config,
    validate_media_root,
)
from . import fsweb
from . import play
from . import syncstore
from . import watchstore
from .media import MediaMixin
from .scan import ScanManager, patch_library_root, wait_for_scan

#: socket 读超时（秒）。不设的话，局域网里「只连不发」的连接
#: 会各占一个线程永不释放（AV_Wall 实测 20 条连接 → 线程数从 3 涨到 22）。
SOCKET_TIMEOUT_SEC = 30


# --------------------------------------------------------------------------
# 服务端
# --------------------------------------------------------------------------


class MediaWallServer(ThreadingTCPServer):
    """线程化 TCP 服务；绑定根目录与扫描管理器供 handler 使用。

    生命周期（同 AV_Wall v1.8.0：只能手动关闭）：

    * 服务**不会**因为浏览器页面被关闭而自动退出；
    * 关闭方式只有两种：命令行窗口按 ``Ctrl+C``；或本机调用
      ``POST /api/shutdown``（仅 localhost，且要求同源）。
    """

    # allow_reuse_address 必须为 False。
    # Windows 的 SO_REUSEADDR 语义与 POSIX 不同：它允许**第二个进程绑定
    # 一个已被 LISTENING 的端口**（实测 WinError 10048 只在 False 时才报），
    # 于是重复双击启动.bat 会出现两个实例同时监听 8787，流量被内核随机
    # 分流到两个进程 -> 请求时好时坏。Linux 上该选项只用于绕过 TIME_WAIT。
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 root: Path, scanner_path: Path) -> None:
        super().__init__(address, handler)
        self.root = root
        self.scanner_path = scanner_path
        self.scan = ScanManager(scanner_path, root)
        self._shutdown_requested = False
        self._lock = threading.Lock()
        # 启动时刻在 __init__ 里同步赋值（放在后台线程里会与读取存在竞态）
        self._boot_at = time.monotonic()

    def set_root(self, root: Path) -> None:
        """运行时切换媒体根（设置面板改目录 / API 调用时用）。

        同步更新 ``ScanManager`` 的根，确保后续扫描扫的是新目录。
        """
        with self._lock:
            self.root = root
        self.scan.set_root(root)

    def request_shutdown(self) -> None:
        """由 ``/api/shutdown`` 调用，平滑退出服务。

        注意三点（v1.2.2 修复）：
        1. ``socketserver.BaseServer.shutdown()`` 必须从**非** ``serve_forever``
           线程调用，否则会自锁；
        2. 也不能在本请求的处理线程里同步等待——``shutdown()`` 会阻塞到主循环
           退出，响应就发不回去了；
        3. 所以先置标志、立刻返回给客户端 200，再交给后台守护线程延时关闭。
        """
        with self._lock:
            if self._shutdown_requested:      # 幂等：重复 POST 不重复起线程
                return
            self._shutdown_requested = True
        threading.Thread(target=self._deferred_stop, name="mw-shutdown",
                         daemon=True).start()

    def _deferred_stop(self) -> None:
        """后台线程：稍等让 200 响应写回，然后停掉 ``serve_forever`` 循环。"""
        time.sleep(0.2)
        try:
            self.shutdown()
        except Exception:  # noqa: BLE001 - 停服失败不应影响进程退出
            pass

    @property
    def _should_exit(self) -> bool:
        """是否该退出服务。只有显式 ``POST /api/shutdown`` 才为 True。"""
        with self._lock:
            return self._shutdown_requested

    def _booted_secs(self) -> float:
        return time.monotonic() - self._boot_at


# --------------------------------------------------------------------------
# 请求处理
# --------------------------------------------------------------------------


class MediaWallHandler(MediaMixin, BaseHTTPRequestHandler):
    """海报墙路由与静态/流式响应实现。

    媒体流与缩略图部分见 :mod:`mediawall.media`。
    """

    server_version = SERVER_TOKEN
    protocol_version = "HTTP/1.1"

    # ---- 服务器上下文 ----

    @property
    def _srv(self) -> MediaWallServer:
        return cast(MediaWallServer, self.server)

    @property
    def _root(self) -> Path:
        return self._srv.root

    @property
    def _program_dir(self) -> Path:
        return self._srv.scanner_path.parent

    # ---- 请求入口 ----

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._send_options()

    def _dispatch(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._serve_index()
            elif path == "/server.html":
                # v2.7.0：方案 A 服务控制页（状态/启动/重启/停止/二维码），由本服务静态提供
                self._serve_server_page()
            elif path.startswith("/mediawall/") or path.startswith("/data/"):
                if path.startswith("/mediawall/"):
                    rel = path[len("/mediawall/"):]
                else:
                    # /data/* 映射到程序目录 data/*（scanner 输出目录）
                    rel = "data/" + path[len("/data/"):]
                self._serve_static(rel)
            elif path == "/api/ping":
                self._send_json(200, {"ok": True})
            elif path == "/api/library":
                self._serve_library()
            elif path == "/api/status":
                self._serve_status()
            elif path == "/api/info":
                self._serve_info()
            elif path == "/api/categories":
                self._serve_categories()
            elif path == "/api/scan":
                self._serve_scan()
            elif path == "/api/keepalive":
                self._serve_keepalive()
            elif path == "/api/config":
                self._serve_config()
            elif path == "/api/root":
                self._serve_root()
            elif path == "/api/shutdown":
                self._serve_shutdown()
            elif path == "/api/restart":
                self._serve_restart()
            elif path == "/api/tracks":
                self._serve_tracks()
            elif path == "/api/playinfo" or path == "/api/play/clear":
                # 播放决策与转换缓存（v2.9.0，见 :mod:`mediawall.play`）
                play.handle_api(self, path)
            elif (path.startswith("/api/fs/") or path.startswith("/api/trash")
                  or path == "/api/undo"):
                # 网页端文件管理（v2.1.0）：列目录 / 删除到回收站 / 移动 / 撤回
                fsweb.handle(self, path)
            elif path == "/api/sync/watched" or path == "/api/sync/watched/clear":
                # 全域已看记录统一管理（v2.10.0）：GET 拉取 / POST 增量 / POST 清空
                watchstore.handle(self, path)
            elif path.startswith("/api/sync/"):
                # 全域播放进度同步（v2.8.0）：GET 拉取 / POST 上报 / POST 清空
                syncstore.handle(self, path)
            elif path.startswith("/media/"):
                # 必须用 self.path 原文：urlsplit 的 path 会丢掉 ?fmt=/?track= 查询串。
                # （v2.5.0 的 ?track 也因此从未真正到达过 _serve_media——静默失效，
                #   v2.9.0 加 ?fmt 时经 E2E 实测暴露，一并修复）
                self._serve_media(self.path[len("/media/"):])
            elif path.startswith("/thumb/"):
                self._serve_thumb(path[len("/thumb/"):])
            else:
                self._send_json(404, {"error": "not found", "path": path})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 单请求异常不能弄崩服务
            # 若响应头已经发出去了，就**不能再发第二个状态行**
            # （HTTP 响应只能有一个状态行，否则客户端会解析错乱）。
            # 这时只能关闭连接让客户端感知到失败。
            if getattr(self, "_headers_buffer", None) is not None:
                self.close_connection = True
                return
            try:
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def setup(self) -> None:
        """给 socket 设读超时。

        原先没有超时：局域网里「只连不发」的连接会各占一个线程永不释放
        （实测 20 条这样的连接 → 线程数从 3 涨到 22）。
        """
        super().setup()
        try:
            self.connection.settimeout(SOCKET_TIMEOUT_SEC)
        except OSError:
            pass

    def handle(self) -> None:
        """包一层，保证 socket 层异常不会冒泡到 socketserver。"""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            self.close_connection = True

    # ---- 响应辅助 ----

    def _send_options(self) -> None:
        try:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
            # 不发 `Access-Control-Allow-Origin: *`：通配符意味着**任何网站**
            # 都能用 fetch 读本服务的所有 GET 端点，配合目录探测接口等于
            # 把 NAS 结构暴露给全网。只回显「请求方自己的 Origin」
            # （同源策略，等价于不跨域），没有 Origin 头则不发这个头。
            origin = self.headers.get("Origin")
            if origin and self._origin_allowed():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
        except OSError:
            self.close_connection = True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_html(self, status: int, html: str) -> None:
        self._send_bytes(status, "text/html; charset=utf-8", html.encode("utf-8"))

    def _cors_header_value(self) -> str | None:
        """返回要写进 ``Access-Control-Allow-Origin`` 的值。

        只回显「请求方自己的 Origin」（同源策略，等价于不跨域），
        没有 Origin 头（脚本 / 命令行）则不发这个头。
        """
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed():
            return origin
        return None

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        """发送带完整 ``Content-Length`` 的响应；HEAD 只发头。"""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            origin = self._cors_header_value()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            if self.command == "HEAD":
                # HEAD 语义：有头无 body，且必须关闭连接，否则客户端会按
                # Content-Length 继续等 body 而挂住
                self.close_connection = True
                return
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    # ---- 权限 ----

    def _is_localhost(self) -> bool:
        """只允许本机触发扫描/改配置/关机，手机/平板局域网地址不能。"""
        addr = self.client_address[0] if self.client_address else ""
        return addr in ("127.0.0.1", "::1", "localhost")

    def _origin_allowed(self) -> bool:
        """写操作的跨站请求防护（CSRF + DNS 重绑定防护）。

        规则：

        * 没有 ``Origin`` 头 → 放行（命令行 / 脚本 / 其它程序调用，不带 Origin）
        * 有 ``Origin`` → 必须是本服务的同源（scheme://host:port 与 ``Host`` 一致）
        * 其它一律拒绝

        没有这一层时，局域网里的手机只要打开一个恶意网页，该网页就能用
        ``fetch("http://127.0.0.1:8787/api/shutdown")`` 把本机服务关掉。

        **DNS 重绑定**防护：光比对 Origin 与 Host 是不够的，因为这两个头都是
        **客户端可控**的。攻击者的网页可以把 ``attacker.example`` 的 DNS 解析到
        127.0.0.1，此时浏览器发的请求 Host 与 Origin **完全一致**，原先的
        同源校验会放行 → 写操作被绕过。现在额外要求 ``Host`` 必须是**本机真实
        地址**（127.0.0.1 / ::1 / 本机局域网 IP / localhost），陌生 Host 一律拒绝。
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not host:
            return True
        if not self._host_is_local(host):
            return False
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if not parsed.netloc:
            return False
        return parsed.netloc.lower() == host.lower()

    def _host_is_local(self, host: str) -> bool:
        """判断 ``Host`` 头指向的是不是本机（防 DNS 重绑定）。

        允许：``127.0.0.1[:port]`` / ``localhost[:port]`` / ``[::1][:port]``
        / 本机任一局域网 IP（含本机主机名）。其它一律视为可疑。
        """
        raw = host.strip()
        if not raw:
            return False
        # 去掉端口（IPv6 要先剥方括号）
        if raw.startswith("["):
            end = raw.find("]")
            if end < 0:
                return False
            hostname = raw[1:end]
        else:
            hostname = raw.rsplit(":", 1)[0] if ":" in raw else raw
        hostname = hostname.strip().lower()
        if not hostname:
            return False
        if hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return True
        try:
            addrs = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
        except OSError:
            return False
        try:
            local = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)}
        except OSError:
            local = set()
        for ip in addrs:
            if ip in local:
                return True
            if ip.startswith("127.") or ip == "::1":
                return True
        return False

    def _is_private_client(self) -> bool:
        """客户端是否来自「本机 + 局域网私有网段」。

        用于网页端「重启服务」的权限判定：手机/平板通过局域网 IP 访问
        时也应能重启，但公网来源一律拒绝。
        """
        try:
            import ipaddress
        except ImportError:  # 极端环境兜底：退化为仅本机
            return self._is_localhost()
        addr = (self.client_address[0] if self.client_address else "").strip()
        if not addr:
            return False
        if addr in ("localhost",):
            return True
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def _reject_write(self, reason: str) -> bool:
        """写操作门禁：非本机 → 403；跨站 → 403。返回 True 表示已拒绝并已响应。

        **先排空请求体再回 403**。直接 return 不读 body 的话，
        同一条 keep-alive 连接上的下一个请求会从请求体中间开始解析
        （实测得到 ``HTTP/1.1 501 Unsupported method ('{"x":1}GET')``）。
        """
        if not self._is_localhost():
            self._drain_body()
            self._send_json(403, {"error": reason})
            return True
        if not self._origin_allowed():
            self._drain_body()
            self._send_json(403, {"error": "cross-origin request rejected"})
            return True
        return False

    # ---- 路由实现 ----

    def _serve_index(self) -> None:
        # 历史记录：谁打开了海报墙首页（v1.4.0，可在设置里关掉）
        try:
            from .history import record as _record_history
            _record_history(
                self.client_address[0] if self.client_address else "", "page")
        except Exception:
            pass
        # index.html 位于程序目录（与 server.py 同目录）
        index = self._program_dir / INDEX_HTML_NAME
        if not index.is_file():
            self._send_html(
                503,
                "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>海报墙尚未就绪</title></head><body style=\"font-family:sans-serif;\">"
                "<h2>海报墙页面尚未生成</h2>"
                f"<p>未找到 <code>{index}</code>，请确认程序目录完整后刷新。</p>"
                "</body></html>",
            )
            return
        try:
            body = index.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read index.html failed: {exc}"})
            return
        self._send_bytes(200, "text/html; charset=utf-8", body)

    def _serve_server_page(self) -> None:
        """``GET /server.html``：服务控制页（v2.7.0）。

        由本服务静态提供 ``server.html``（与 index.html 同目录），页面提供
        状态 / 启动 / 重启 / 停止 / 二维码。启动与停止走看门狗（8786），
        重启优先走本服务的 ``/api/restart``（自重启协议）。
        """
        page = self._program_dir / "server.html"
        if not page.is_file():
            self._send_html(
                503,
                "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>服务控制页未就绪</title></head><body>"
                f"<p>未找到 <code>{page}</code>，请确认程序目录完整后刷新。</p>"
                "</body></html>",
            )
            return
        try:
            body = page.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read server.html failed: {exc}"})
            return
        self._send_bytes(200, "text/html; charset=utf-8", body)

    def _serve_static(self, rel: str) -> None:
        try:
            target = resolve_within(self._program_dir, rel)
        except PathSecurityError:
            self._send_json(403, {"error": "forbidden"})
            return
        ext = target.suffix.lower()
        if ext not in STATIC_EXTS:
            self._send_json(403, {"error": "file type not allowed"})
            return
        # config.json 含服务端配置、library.meta.json 含 NAS
        # 绝对路径与缓存内容——都不允许通过 HTTP 暴露给局域网里的任何设备。
        if target.name.lower() in DENY_STATIC_NAMES:
            self._send_json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        try:
            size = target.stat().st_size
            if size > MAX_STATIC_BYTES:
                self._send_json(413, {"error": "static file too large"})
                return
            body = target.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read failed: {exc}"})
            return
        self._send_bytes(200, STATIC_CONTENT_TYPES[ext], body)

    def _serve_library(self) -> None:
        library = self._program_dir / LIBRARY_REL
        if not library.is_file():
            self._send_json(
                503,
                {"error": "library.json not found, run scanner.py"},
            )
            return
        try:
            body = library.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"read library.json failed: {exc}"})
            return
        self._send_bytes(200, "application/json; charset=utf-8", body)

    def _serve_status(self) -> None:
        library = self._program_dir / LIBRARY_REL
        self._send_json(
            200,
            {
                "ok": True,
                "version": VERSION,
                "root": str(self._root),
                "library_exists": library.is_file(),
                "scan": self._srv.scan.snapshot(),
            },
        )

    def _serve_info(self) -> None:
        """返回局域网访问信息（IP、端口、URL 与二维码矩阵）。

        二维码以 0/1 矩阵返回，由前端渲染成图形；qrcode 库缺失时 qr 为 null，
        前端退化为直接显示 URL 文本，仍可手动输入。
        """
        port = int(self._srv.server_address[1])
        lan_ip = detect_lan_ip()
        url = f"http://{lan_ip}:{port}/"
        qr: list[list[int]] | None = None
        try:
            import qrcode  # type: ignore

            matrix = qrcode.QRCode(border=2)
            matrix.add_data(url)
            matrix.make(fit=True)
            qr = [[1 if cell else 0 for cell in row] for row in matrix.get_matrix()]
        except Exception:  # noqa: BLE001 - 二维码只是增强，缺失不影响主功能
            qr = None
        self._send_json(
            200,
            {"ok": True, "lan_ip": lan_ip, "port": port, "url": url, "qr": qr},
        )

    def _serve_categories(self) -> None:
        """``GET /api/categories``：分区清单（实时发现 + 已扫条目数）。

        返回 ``{"ok": true, "categories": [{"name", "items", "last_scan"}, ...]}``：
        * ``name``：媒体根下实际存在的一级目录（排除系统目录）；
        * ``items``：上次扫描（library.json）中该分区的条目数；分区还没扫过为 0；
        * ``last_scan``：library.json 顶层 ``cat_scanned`` 里记录的该分区扫描时刻。

        注意条目数来自**上次扫描快照**（不实时遍历 NAS，避免这个高频接口
        变成目录探测 oracle 的放大器）；``items=0 且 last_scan=null`` 的分区
        即「还没扫过」，前端引导用户触发单分区扫描。
        """
        counts: dict[str, int] = {}
        last_scan: dict[str, str] = {}
        library = self._program_dir / LIBRARY_REL
        if library.is_file():
            try:
                lib = json.loads(library.read_text(encoding="utf-8"))
                for it in lib.get("items", []):
                    cat = str(it.get("category") or it.get("src") or "")
                    if cat:
                        counts[cat] = counts.get(cat, 0) + 1
                cs = lib.get("cat_scanned")
                if isinstance(cs, dict):
                    last_scan = {k: str(v) for k, v in cs.items()}
            except (OSError, ValueError):
                pass
        out = []
        for name in list_categories(self._root):
            out.append({"name": name, "items": counts.get(name, 0),
                        "last_scan": last_scan.get(name)})
        self._send_json(200, {"ok": True, "categories": out})

    def _serve_scan(self) -> None:
        """触发一次增量扫描（异步，立刻返回 202）。

        * 仅本机（localhost）+ 同源才允许——扫描最坏要跑 30 分钟
          （``SCAN_TIMEOUT_SEC``），无门禁等于任何人都能把服务占死。
        * ``POST /api/scan``                → 扫全部分区
        * ``POST /api/scan {"category":"01 电影"}`` → 只重扫一个分区
        * ``POST /api/scan {"force":true}`` → 全量重建（忽略增量缓存）
        * 进度用 ``GET /api/status`` 的 ``scan`` 字段查。
        * 已有扫描在跑时返回 409，不并发拉起第二个 scanner 进程。
        """
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use POST"})
            return
        if self._reject_write("scan only allowed from localhost"):
            return
        if not self._srv.scan.scanner_exists:
            self._send_json(500, {"ok": False,
                                  "error": f"scanner.py not found: {self._srv.scanner_path}"})
            return
        body = self._drain_body()
        category: str | None = None
        force = False
        if body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    if payload.get("category"):
                        category = str(payload["category"]).strip() or None
                    force = bool(payload.get("force"))
            except Exception:  # noqa: BLE001 - 空/坏 body 视为全量扫描
                category = None
        if category is not None and category not in list_categories(self._root):
            self._send_json(400, {"ok": False, "error": f"分区不存在：{category}"})
            return
        if self._srv.scan.running:
            self._send_json(409, {"ok": False, "error": "scan already running"})
            return
        # 异步触发：立刻返回，不占住 HTTP 线程
        self._srv.scan.run_async(category=category, force=force)
        self._send_json(202, {"ok": True, "started": True, "category": category or "全部",
                              "force": force,
                              "message": "scan started; poll GET /api/status for progress"})

    def _serve_keepalive(self) -> None:
        """兼容占位端点。

        「关页面即停服」机制已删除，服务不会因为没有保活而自杀。
        保留端点是为了不让旧脚本收到 404；它**什么都不做**。
        """
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use POST"})
            return
        self._drain_body()
        self._send_json(200, {"ok": True, "deprecated": True,
                              "message": "keepalive is no longer used; "
                                         "the service only stops on Ctrl+C or POST /api/shutdown"})

    def _serve_shutdown(self) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use POST"})
            return
        self._drain_body()
        if self._reject_write("shutdown only allowed from localhost"):
            return
        self._srv.request_shutdown()
        self._send_json(200, {"ok": True, "message": "shutting down"})

    def _serve_restart(self) -> None:
        """``POST /api/restart``：浏览器端「重启服务」。

        权限：本机 + 局域网私有网段（手机/平板通过 IP 访问时也能用）；
        仍要求同源（``_origin_allowed``），公网来源与跨站请求都会被 403。

        实现：由服务进程自己拉起新实例、旧进程在新实例就绪后退出
        （见 ``mediawall/restart.py`` 的握手协议）；返回 202 表示
        「正在重启」，浏览器据此提示并轮询恢复。
        """
        if self.command != "POST":
            self._send_json(405, {"ok": False, "error": "method not allowed, use POST"})
            return
        self._drain_body()
        if not self._is_private_client():
            self._send_json(403, {"ok": False,
                                  "error": "restart only allowed from localhost or LAN"})
            return
        if not self._origin_allowed():
            self._send_json(403, {"ok": False, "error": "cross-origin request rejected"})
            return
        from . import restart

        host = self._srv.server_address[0] if self._srv.server_address else "0.0.0.0"
        port = int(self._srv.server_address[1])
        if not restart.request_restart(self._srv, host, port):
            self._send_json(409, {"ok": False, "error": "restart already in progress"})
            return
        self._send_json(202, {"ok": True, "message": "restarting",
                              "message_zh": "正在重启，请稍候…"})

    def _serve_tracks(self) -> None:
        """音轨清单与转封装状态（v2.5.0；v2.9.0 实现搬至 :mod:`mediawall.play`）。"""
        play.handle_tracks(self)

    def _serve_config(self) -> None:
        """读取或更新服务端配置。

        GET  -> 返回当前配置（含 media_root 与完整 config.json 内容）。
        POST -> 仅本机(localhost)可改，改动立即生效并落盘 config.json。

        可写键：
        * ``media_root``（与 ``POST /api/root`` 等价）；
        * ``categories``（分区白名单，list[str] 或 null=自动发现全部一级子目录）。
          改白名单**不会**自动重扫（旧分区数据仍在库里），
          需要手动 ``POST /api/scan`` 使 library.json 与新白名单一致。
        """
        if self.command == "GET":
            cfg = load_config()
            self._send_json(200, {
                "ok": True,
                "media_root": str(self._srv.root),
                "default_media_root": DEFAULT_MEDIA_ROOT,
                # 服务只支持手动停止，这个字段固定为 manual
                "stop_mode": "manual",
                "config": cfg,
            })
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use GET/POST"})
            return
        if self._reject_write("config change only allowed from localhost"):
            return
        body = self._drain_body()
        try:
            payload = json.loads(body) if body else {}
        except Exception:  # noqa: BLE001
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "need a json object"})
            return

        if "media_root" not in payload and "categories" not in payload:
            self._send_json(400, {
                "error": "need one of media_root / categories",
            })
            return

        extra: dict[str, Any] = {}
        if "categories" in payload:
            cats = payload["categories"]
            if cats is not None and (
                not isinstance(cats, list)
                or not all(isinstance(c, str) and c.strip() for c in cats)
            ):
                self._send_json(400, {"error": "categories 需要字符串数组或 null"})
                return
            cfg = load_config()
            cfg["categories"] = cats
            if not save_config(cfg):
                self._send_json(500, {"error": "config.json 写入失败"})
                return
            extra["categories"] = cats
            extra["categories_applied"] = list(list_categories(self._srv.root))

        if "media_root" in payload:
            new_path = str(payload.get("media_root") or "").strip()
            result = self._apply_media_root(new_path)
            if not result.get("ok"):
                self._send_json(400, result)
                return
            self._send_json(200, {"ok": True, "saved": result.get("saved"),
                                  **extra, **result})
            return

        self._send_json(200, {"ok": True, "saved": True, **extra})

    def _apply_media_root(self, new_path: str) -> dict[str, Any]:
        """校验并切换媒体根：落盘 config.json + 实时生效 + 必要时重扫。

        ``POST /api/root`` 与 ``POST /api/config``（media_root 键）共用此实现，
        保证两条路径的行为完全一致。
        """
        ok, err, detail = validate_media_root(new_path)
        if not ok:
            return {"ok": False, "error": err, **detail}

        old_path = str(self._srv.root)
        # 落盘 config.json（保留其它配置项）
        cfg = load_config()
        cfg["media_root"] = new_path
        saved = save_config(cfg)
        # 实时切换：更新 root 与 ScanManager 的根
        self._srv.set_root(Path(new_path))

        # 只是写法不同（同一目录，如大小写变化）→ 不必全量重扫，
        # 直接把数据文件里的 root 字段改成新写法即可。
        same_dir = paths_equivalent(old_path, new_path)
        rescanning = False
        if same_dir:
            patch_library_root(old_path, new_path)
        else:
            rescanning = True
            try:
                self._srv.scan.run_async()
            except Exception:  # noqa: BLE001 - 扫描失败不影响本次切换本身
                pass
        return {
            "ok": True,
            "saved": saved,
            "path": new_path,
            "previous_path": old_path,
            "same_directory": same_dir,
            "rescanning": rescanning,
            "categories_found": detail.get("categories", {}),
            "message": ("媒体根已切换，正在后台重新扫描" if rescanning
                        else "媒体根写法已更新（同一目录，无需重扫）"),
        }

    def _serve_root(self) -> None:
        """媒体根目录（指定目录）的读取 / 校验 / 设置。

        GET  ?path=<待校验路径>  → 校验该路径是否可用（不修改任何状态）
        GET                      → 返回当前媒体根 + 分区列表
        POST {"path": "..."}     → 校验并切换媒体根，落盘 config.json，实时生效

        POST 仅允许本机（localhost）调用——与 ``/api/config``、``/api/shutdown``
        同一道门禁。切换成功后自动触发一次重新扫描（异步），页面刷新即可看到新目录。
        """
        if self.command == "GET":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                                       keep_blank_values=True)
            # keep_blank_values：?path= （空值）也要参与校验，而不是被当成"没传"
            probe = qs.get("path", [None])[0]
            if probe is not None:
                ok, err, detail = validate_media_root(probe)
                self._send_json(200 if ok else 400, {
                    "ok": ok, "valid": ok, "error": err or None, **detail,
                })
                return
            self._send_json(200, {
                "ok": True,
                "path": str(self._srv.root),
                "categories": [d for d in list_categories(self._srv.root)],
                "default": DEFAULT_MEDIA_ROOT,
            })
            return

        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed, use GET/POST"})
            return
        if self._reject_write("media root change only allowed from localhost"):
            return

        body = self._drain_body()
        try:
            payload = json.loads(body) if body else {}
        except Exception:  # noqa: BLE001
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict) or "path" not in payload:
            self._send_json(400, {"error": "need path (string)"})
            return
        new_path = str(payload.get("path") or "").strip()
        # 与 POST /api/config 的 media_root 共用同一段实现
        result = self._apply_media_root(new_path)
        if not result.get("ok"):
            self._send_json(400, result)
            return
        self._send_json(200, result)

    # ---- 其他 ----

    def _drain_body(self) -> str:
        """读掉并返回请求体文本（UTF-8），避免 keep-alive 连接错位。

        带长度上限 ``MAX_BODY_BYTES``（1MB）。原先直接 ``rfile.read(length)``，
        客户端声明 ``Content-Length: 2GB`` 就会让服务端分配/读入巨量数据
        （内存被撑爆，且连接长期占用）。超限直接拒绝并关连接。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY_BYTES:
            # 太大：不再读（读了也会被撑爆），直接关连接
            print(f"[mediawall] 请求体过大（{length} 字节 > {MAX_BODY_BYTES}），拒绝。",
                  flush=True)
            self.close_connection = True
            return ""
        if length > 0:
            try:
                raw = self.rfile.read(length)
                return raw.decode("utf-8", errors="replace")
            except OSError:
                self.close_connection = True
        return ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """紧凑日志；媒体/缩略图请求不打，避免海量噪声。"""
        try:
            path = self.path.split("?", 1)[0]
        except Exception:  # noqa: BLE001
            path = ""
        if path.startswith("/media/") or path.startswith("/thumb/"):
            return
        sys.stderr.write("[mediawall] %s - %s\n" % (self.address_string(), format % args))
        sys.stderr.flush()


__all__ = ["MediaWallServer", "MediaWallHandler", "wait_for_scan", "derive_root"]
