"""程序目录定位：源码运行与 Nuitka 编译后运行两种形态统一口径。

为什么需要这个模块
------------------
打包成 exe 之后有三处**不能**再用 ``Path(__file__).parent`` 或
``sys.executable + 脚本路径`` 的写法：

1. ``index.html`` / ``data/`` / ``config.json`` 必须落在 **exe 所在目录**，
   而不是模块文件所在目录（Nuitka standalone 下模块被编进 exe，
   ``__file__`` 是合成路径，不同版本行为不完全一致）；
2. GUI 拉起服务子进程时，``sys.executable`` 已经不是 ``python.exe`` 而是
   **这个 exe 自己**，传 ``server.py`` 参数会被当成 exe 的启动参数，直接崩；
3. 扫描器同理：没有 ``scanner.py`` 文件可传，只能让 exe 自己以扫描模式再跑一个进程。

因此统一在这里判定「是否编译态」，并由 :func:`program_dir` 给出程序目录，
再配合 ``Media_Wall.py`` 里的 ``--serve`` / ``--scan`` 隐藏模式让 exe 自调用。

判定依据
--------
Nuitka 会在每个被编译模块的 globals 里注入 ``__compiled__`` 对象；
PyInstaller 等则设置 ``sys.frozen``。两者都判，源码态两者皆无。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """当前是否运行在编译产物里（Nuitka / PyInstaller 等）。"""
    if globals().get("__compiled__") is not None:
        return True
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """程序目录：源码态是项目根（``mediawall/`` 的上一级），编译态是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def self_exe() -> str:
    """可执行文件路径，用于「exe 自调用另一模式起子进程」。

    编译态**不能用** ``sys.executable``：Nuitka standalone 下它指向
    ``<dist>\\python.exe``，而那个文件根本不存在，``subprocess`` 会直接抛
    ``FileNotFoundError: [WinError 2]``（实测于 4.0.7，见 v1.2.4 条目）。
    真实路径在 ``sys.argv[0]``，它是被双击/被调起时的 exe 全路径。

    源码态返回 ``sys.executable``（``python.exe``），配合脚本路径使用。
    """
    if is_frozen():
        return os.path.abspath(sys.argv[0])
    return sys.executable


def pythonw() -> str:
    """无控制台窗口的 Python 解释器路径（源码态启动子进程用）。

    为什么需要：GUI 控制面板（``Media_Wall.py``）用 ``python.exe`` 拉起
    ``server.py`` / ``scanner.py`` 子进程时，**每个子进程都会弹一个黑色控制台
    窗口**（服务启动时闪一下）。换成与 ``python.exe`` 同目录的 ``pythonw.exe``
    后子进程不再创建控制台窗口，日志仍通过管道进 GUI 日志区，功能完全一致。

    编译态不需要：exe 自己就是无控制台程序（Nuitka ``--windows-console-mode=disable``），
    子进程用 ``self_exe()`` 自调用，没有独立控制台。
    """
    if is_frozen():
        return self_exe()
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    if w.is_file():
        return str(w)
    # 找不到 pythonw 时回退 python.exe（有控制台，但至少能跑）
    return sys.executable


__all__ = ["is_frozen", "program_dir", "self_exe", "pythonw"]
