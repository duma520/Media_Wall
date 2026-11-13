"""窗口大小记忆测试（v2.2.1，offscreen）。

验证：
1. 默认尺寸 940x760；
2. 构造窗口并 resize 到 1100x800 → resizeEvent 落盘；
3. 重新加载 gui_settings.json → win_w/win_h 已更新；
4. 第二次构造 MainWindow 时按记忆尺寸恢复。
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_config import GUI_SETTINGS_PATH, load_gui_settings, save_gui_settings  # noqa: E402
from gui_window import MainWindow  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def main() -> int:
    # 备份当前设置
    backup = None
    if GUI_SETTINGS_PATH.is_file():
        backup = GUI_SETTINGS_PATH.read_bytes()

    app = QApplication([])
    try:
        # 1) 默认
        save_gui_settings({"port": 8787, "auto_start": True, "win_w": 940, "win_h": 760})
        s = load_gui_settings()
        check("默认尺寸 940x760", s["win_w"] == 940 and s["win_h"] == 760, str(s))

        # 2) resize 落盘（需 show 后 resize 才触发 resizeEvent）
        win = MainWindow(port=8787, host="0.0.0.0", autostart=False)
        win.show()
        app.processEvents()
        win.resize(1100, 800)
        app.processEvents()
        s = load_gui_settings()
        check("resize 后已落盘 1100x800",
              s["win_w"] == 1100 and s["win_h"] == 800, str(s))

        # 3) 第二次构造恢复记忆尺寸
        win2 = MainWindow(port=8787, host="0.0.0.0", autostart=False)
        win2.show()
        app.processEvents()
        check("第二次构造按记忆恢复 1100x800",
              win2.width() == 1100 and win2.height() == 800,
              f"{win2.width()}x{win2.height()}")

        # 4) 过小尺寸不记（低于最小）
        save_gui_settings({"port": 8787, "auto_start": True, "win_w": 500, "win_h": 400})
        s = load_gui_settings()
        check("过小尺寸被夹到最小 720x480",
              s["win_w"] == 720 and s["win_h"] == 480, str(s))

        # 5) 损坏 JSON 兜底
        GUI_SETTINGS_PATH.write_text("{bad json", encoding="utf-8")
        s = load_gui_settings()
        check("损坏 JSON 兜底到默认 940x760",
              s["win_w"] == 940 and s["win_h"] == 760, str(s))
    finally:
        # 恢复原设置
        if backup:
            GUI_SETTINGS_PATH.write_bytes(backup)
        elif GUI_SETTINGS_PATH.exists():
            GUI_SETTINGS_PATH.unlink()

    if fails:
        print("\n结果: FAIL %s" % ", ".join(fails))
        return 1
    print("\nWIN_SIZE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
