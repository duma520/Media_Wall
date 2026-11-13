"""Media_Wall/server.py —— 多分区媒体海报墙本地 HTTP 服务端（入口）。

本模块只依赖 Python 3.10 标准库。实现拆分为包 ``mediawall/``
（由 AV_Wall v1.9.0 泛化而来）：

- ``mediawall/common.py``       —— 配置、媒体根、**分区发现**、路径安全、Range 解析
- ``mediawall/scanner_core.py`` —— 叶子目录判定 + 增量缓存（扫描核心逻辑）
- ``mediawall/scan.py``         —— 扫描进程管理（支持按分区扫描）
- ``mediawall/httpd.py``        —— HTTP 服务与路由
- ``mediawall/media.py``        —— 视频/音频流与缩略图输出
- ``mediawall/banner.py``       —— 启动横幅与 ASCII 二维码

本文件只保留**入口**（参数解析 → 读配置 → 起服务 → 跑横幅），便于阅读与维护。

与 AV_Wall 的定位差异
--------------------
AV_Wall 只管一个媒体根（``99 其他``，5 个内容目录直接放番号）；
Media_Wall 管 ``\\\\NAS\\Share\\Download``（示例）下的 **16 个一级分区**
（01 电影 / 10 电视节目 / 20 综艺 / 30 动漫 / … / 99 其他）。
页面布局相应变成两级选择：先选分区，再选分区内子目录（年代/系列），
下面是和 AV_Wall 一致的海报墙。每个分区都是同一套设计，无需分别部署。

对外接口（详见同目录《API 说明.md》）
----------------------------------------------------------------
* ``GET  /``                ``index.html``（与 server.py 同目录，UTF-8，no-store）
* ``GET  /mediawall/*``     本目录静态资源（.js/.css/.json/.ico，no-store）
* ``GET  /data/*``          本目录 ``data/`` 静态资源（等价 /mediawall/data/*）
* ``GET  /api/ping``        健康检查 ``{"ok": true}``
* ``GET  /api/library``     ``data/library.json``（缺失返回 503）
* ``GET  /api/status``      服务与扫描状态
* ``GET  /api/info``        局域网地址 + 二维码矩阵
* ``GET  /api/categories``  分区清单（实时发现 + 各分区条目数 + 扫描时刻）
* ``GET  /api/root``        读取当前媒体根（指定目录）
* ``GET  /api/root?path=``  校验某目录是否可用（不修改任何状态）
* ``POST /api/root``        设置媒体根（落盘 + 实时生效 + 自动重扫）
* ``GET  /api/config``      读取完整配置（含 media_root / categories）
* ``POST /api/config``      修改配置（media_root / categories）
* ``POST /api/scan``        触发扫描（**仅本机**；异步触发立刻返回 202；
  body 可带 ``{"category": "01 电影"}`` 只重扫一个分区）
* ``POST /api/shutdown``    停止服务（仅 localhost）
* ``POST /api/keepalive``   **兼容占位**，返回 200 但什么都不做
* ``GET  /media/<相对路径>`` 视频/音频流，支持 ``Range``（206）与 ``HEAD``
* ``GET  /thumb/<相对路径>`` 图片，支持 ``HEAD``，不做 Range

**写操作（``POST /api/root``、``POST /api/config``、``POST /api/scan``、
``POST /api/shutdown``）仅允许本机（localhost）调用**；手机/平板的局域网
地址只能读。写操作还要求请求**同源**（``Origin`` 头必须与本服务一致或缺省），
且 ``Host`` 必须指向本机（防 DNS 重绑定）。
读操作（``GET``）局域网可用，方便手机端或其它程序查询。

生命周期：**只能手动关闭**。
1. 在运行服务的命令行窗口按 ``Ctrl+C``；
2. 本机调用 ``POST /api/shutdown``（局域网地址无权调用）。

目录布局
--------
程序目录与媒体目录**分离**：程序在自身目录，
媒体在 NAS 共享（如 ``\\\\NAS\\Share\\Download\\``，不随程序移动）。

- 媒体根（``/media``、``/thumb`` 的解析基准）来源优先级：
  ``--root`` 参数 → ``config.json`` 的 ``media_root`` → 写死 ``DEFAULT_MEDIA_ROOT``。
  运行中可在「设置 → 指定目录」或 ``POST /api/root`` 实时切换。
- 分区清单默认自动发现（媒体根下实际存在的一级子目录）；
  ``config.json`` 的 ``categories`` 可固定白名单顺序。
- 程序自己的文件（``index.html`` / ``data/`` / ``config.json``）一律取
  「与 server.py 同目录」，与媒体根无关。
- ``scanner.py`` 会被显式传入 ``--root <媒体根>``，输出写回程序目录的 ``data/``。

设计要点（承袭 AV_Wall 实测结论，改动时不可回退）：

1. ``http.server`` 手写 ``Range`` 解析，返回 ``206 Partial Content`` +
   ``Content-Range`` + ``Accept-Ranges: bytes``——手机播放/拖动进度条依赖此能力。
2. ``ThreadingTCPServer`` + ``allow_reuse_address=False``（Windows 语义）+
   ``daemon_threads=True``。
3. UNC/SMB 下流式读取大文件使用 1MB 分块；客户端断开时
   ``BrokenPipeError`` / ``ConnectionResetError`` 必须静默吞掉。
4. 目录穿越防护：逐段 unquote → 片段黑名单 → ``realpath`` + ``commonpath``。
5. 媒体流**先开文件再发头**；读不满强制断连（防客户端永久挂死）。
6. 扫描**异步触发**且仅本机可发起（防局域网任意设备把服务线程占死 30 分钟）。
7. 启动横幅打印局域网地址与 ASCII 二维码，方便手机/平板扫码访问。

命令行::

   python server.py [--host 0.0.0.0] [--port 8787] [--root <根目录>]
                    [--no-browser]

作者：server-dev；许可：随项目内部分发。
"""

from __future__ import annotations

import argparse
import errno
import mimetypes
import sys
import webbrowser
from pathlib import Path

from mediawall import VERSION
from mediawall.banner import print_banner
from mediawall.common import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    derive_root,
    detect_lan_ip,
    list_categories,
    load_config,
)
from mediawall.httpd import MediaWallHandler, MediaWallServer
from mediawall.paths import program_dir as _program_dir

#: 供外部脚本读取
__version__ = VERSION


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Media_Wall 多分区海报墙本地 HTTP 服务端（仅标准库）",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"绑定地址，默认 {DEFAULT_HOST}（手机/平板需要）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--port-max", type=int, default=DEFAULT_PORT + 20,
                        help="端口被占用时自动递增尝试的上限（默认默认端口+20）")
    parser.add_argument("--root", default=None,
                        help="媒体根绝对路径（默认取 config.json 的 media_root，其次写死默认值）")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    parser.add_argument("--wait-port", type=int, default=0, metavar="SECONDS",
                        help="端口被占时等待其释放的最长秒数（服务自重启用，0=不等待）")
    parser.add_argument("--version", action="version", version=f"mediawall-server {VERSION}")
    return parser


def _wait_bind(server_cls, handler_cls, host: str, port: int,
               root, scanner_path, wait_seconds: int, port_max: int):
    """尝试绑定端口；被占用时**自动递增尝试**下一个端口（v2.5.3）。

    从 ``port`` 开始依次尝试，直到 ``port_max``（含）。每到一个端口先尝试
    绑定；被占用且指定了 ``--wait-port`` 时在该端口上轮询等待（重启握手场景）。

    返回 ``(server, actual_port)``；全部端口都不可用时返回 ``(None, None)``。
    """
    import time

    current = port
    while current <= port_max:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                srv = server_cls((host, current), handler_cls, root, scanner_path)
                return srv, int(srv.server_address[1])
            except OSError as exc:
                if exc.errno not in (errno.EADDRINUSE, 10048):
                    raise
                if wait_seconds <= 0 or time.monotonic() >= deadline:
                    break  # 该端口等不到 → 换下一个端口
                time.sleep(0.5)
        if current == port:
            print(f"[mediawall] 端口 {current} 被占用，尝试下一个端口…", flush=True)
        current += 1
    return None, None


def main(argv: list[str] | None = None) -> int:
    mimetypes.init()
    args = build_arg_parser().parse_args(argv)
    root = derive_root(args.root)
    if not root.is_dir():
        print(f"[mediawall] 媒体根目录不存在：{root}", file=sys.stderr, flush=True)
        print("[mediawall] 请在页面「设置 → 指定目录」里改，或用 --root 指定。",
              file=sys.stderr, flush=True)
        return 2
    # scanner 路径以**程序目录**为基准：源码态是本文件同目录，编译态是 exe 目录
    # （此时没有 scanner.py 文件，scan.py 会让 exe 自己以 --scan 模式再跑一个进程）
    program_dir = _program_dir()
    scanner_path = program_dir / "scanner.py"

    # 读取配置：媒体根（media_root）与分区白名单（categories）
    cfg = load_config()

    # 自重启握手：若本次是被 /api/restart 拉起的（存在 restart.flag），
    # 立刻写 restart.ready 让旧进程让位——必须放在「等端口绑定」之前，
    # 否则新进程要等旧进程释放端口、旧进程又等新进程 ready，形成死锁。
    # （见 mediawall/restart.py 的握手协议）
    try:
        from mediawall.restart import mark_ready

        mark_ready()
    except Exception:  # noqa: BLE001 - 握手失败绝不影响正常启动
        pass

    try:
        server, port = _wait_bind(
            MediaWallServer, MediaWallHandler, args.host, args.port,
            root, scanner_path, args.wait_port, args.port_max,
        )
        if server is None:
            print(f"[mediawall] 端口 {args.port}~{args.port_max} 均被占用——"
                  "可能服务已经启动了。", file=sys.stderr, flush=True)
            print("[mediawall] 请先关掉占用的程序，再重新启动。",
                  file=sys.stderr, flush=True)
            return 3
    except OSError as exc:
        # 端口已被占用时给出可执行的提示，而不是抛栈。
        if exc.errno in (errno.EADDRINUSE, 10048):
            print(f"[mediawall] 端口 {args.port} 已被占用——可能服务已经启动了。",
                  file=sys.stderr, flush=True)
            print("[mediawall] 请先关掉那个窗口（或在它里面按 Ctrl+C），再重新启动。",
                  file=sys.stderr, flush=True)
            return 3
        raise
    lan_ip = detect_lan_ip()

    cats = list_categories(root)
    if cats:
        print(f"[mediawall] 发现 {len(cats)} 个分区: {' / '.join(cats)}", flush=True)
    else:
        print("[mediawall] 媒体根下未发现任何分区目录，请检查共享是否可达。",
              file=sys.stderr, flush=True)

    # 首次启动必须跑一次增量扫描；scanner.py 缺失时只提示不崩溃
    scanning = server.scan.scanner_exists
    if scanning:
        server.scan.run_async()

    print_banner(args.host, port, root, lan_ip, scanning, program_dir=program_dir)

    if not args.no_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            print("[mediawall] 自动打开浏览器失败，请手动访问上面的地址。", flush=True)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n服务已停止（Ctrl+C）", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
