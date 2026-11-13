"""Media_Wall 桌面控制面板 —— 程序入口。

等价于原来的 ``启动.bat``（控制台启动），但以 PySide6 GUI 形式运行：

* 负责启动 / 停止 ``server.py`` 服务（子进程，逻辑与 启动.bat 一致）；
* 显示服务状态、访问地址、二维码与**扫描进度**；
* 海报墙前端完全不变：本机用 ``http://127.0.0.1:<端口>/`` 打开，
  手机 / 平板继续用浏览器访问 ``http://<局域网IP>:<端口>/``。

用法::

    python  Media_Wall.py                  # 读取 gui_settings.json 的端口，自动启动服务
    python  Media_Wall.py --port 9000      # 临时指定端口
    python  Media_Wall.py --no-autostart   # 只打开面板，手动点「启动服务」

图标：程序目录存在 ``icon.ico`` 才使用，不存在则跳过（不硬编码、不报错）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mediawall.paths import program_dir

PROGRAM_DIR = program_dir()


def _dispatch_hidden_mode(argv: list[str]) -> int | None:
    """编译成 exe 后的自调用入口（同一个 exe 兼作服务进程与扫描进程）。

    为什么要这个
    ------------
    Nuitka 只打包 ``Media_Wall.py`` 一个入口，产物里**没有** ``server.py`` /
    ``scanner.py`` 文件。而 GUI 启服务、服务扫目录原本都是
    ``sys.executable + 脚本路径`` 起子进程——打包后 ``sys.executable``
    变成 exe 自己，传脚本路径会被当成 exe 的启动参数而失败。

    所以约定两个隐藏模式，由 exe 自己再开一个进程：
      ``Media_Wall.exe --serve [server 参数]``   → 跑 HTTP 服务
      ``Media_Wall.exe --scan  [scanner 参数]``  → 跑一次扫描

    调用方见 ``gui_service.ServiceManager.start`` 与 ``mediawall.scan``。
    源码态同样可用（便于不打包时验证这条链路）。
    """
    if not argv:
        return None
    mode, rest = argv[0], argv[1:]
    if mode == "--serve":
        from server import main as server_main

        return server_main(rest)
    if mode == "--scan":
        from scanner import main as scanner_main

        return scanner_main(rest)
    if mode == "--watchdog":
        # v2.7.0：编译态下看门狗由 exe 自调用（watchdog.vbs 编译态调 exe --watchdog）
        from mediawall.watchdog import main as watchdog_main

        return watchdog_main(rest)
    return None


def _fatal(message: str) -> int:
    """无 GUI 环境下的错误提示（例如缺 PySide6 时）。"""
    sys.stderr.write(message + "\n")
    try:  # Windows 下双击运行时补一个消息框，避免窗口一闪而过
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Media_Wall 控制面板", 0x10)
    except Exception:  # noqa: BLE001
        pass
    return 2


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    hidden = _dispatch_hidden_mode(raw)
    if hidden is not None:
        return hidden

    parser = argparse.ArgumentParser(description="Media_Wall 桌面控制面板")
    parser.add_argument("--host", default="0.0.0.0",
                        help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None,
                        help="监听端口（默认读取 gui_settings.json，缺省 8787）")
    parser.add_argument("--no-autostart", action="store_true",
                        help="只打开面板，不自动启动服务")
    args = parser.parse_args(raw)

    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return _fatal(
            "缺少 PySide6，无法启动桌面面板。\n\n"
            "请先安装：python -m pip install PySide6\n"
            "（本次要求的桌面面板用 PySide6 实现。）\n\n"
            "如只想临时用旧的控制台方式，直接运行 启动.bat 即可。")

    from gui_config import load_gui_settings

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Media_Wall")
    app.setApplicationDisplayName("Media_Wall 控制面板")

    # 图标：程序目录没有 icon.ico 时自动生成；生成失败就跳过（不影响启动）
    from mediawall.icon import ensure_icon
    _icon_file = ensure_icon()
    if _icon_file is not None:
        app.setWindowIcon(QIcon(str(_icon_file)))

    from gui_window import MainWindow

    settings = load_gui_settings()
    port = args.port if args.port is not None else int(settings["port"])

    window = MainWindow(port=port, host=args.host,
                        autostart=not args.no_autostart)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
