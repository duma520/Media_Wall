"""Media_Wall 看门狗（顶层入口）。

常驻小服务（端口 8786）：网页端启动 / 重启 / 停止 Media_Wall 服务。
实现见 ``mediawall/watchdog.py``；本文件只是薄入口，供
``pythonw watchdog.py``（watchdog.vbs 无窗口拉起）直接运行。
"""

from __future__ import annotations

import sys

from mediawall.watchdog import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
