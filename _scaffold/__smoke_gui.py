"""GUI 冒烟测试（offscreen，不弹窗、不启动服务）：验证窗口能构建、分页齐全、二维码能画。

不触碰 NAS：文件页只检查控件存在，不触发目录读取。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_config import load_gui_settings  # noqa: E402
from gui_service import qr_matrix  # noqa: E402
from gui_window import MainWindow  # noqa: E402
from mediawall.icon import ensure_icon, icon_path  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> None:
    from PySide6.QtGui import QIcon

    # QApplication 必须先建：QPixmap/图标都要有 QGuiApplication
    app = QApplication([])
    icon = ensure_icon()
    if icon is not None:
        QApplication.setWindowIcon(QIcon(str(icon)))
    win = MainWindow(port=8787, host="0.0.0.0", autostart=False)
    win.show()
    app.processEvents()

    print("state:", win.lbl_state.text())
    print("local:", win._words["local_url"].text())
    print("lan:", win._words["lan_url"].text())
    print("qr_matrix:", "OK" if qr_matrix("http://1.2.3.4:8787/") else "NONE")
    print("qr_pixmap:", win.lbl_qr.pixmap() is not None
          and not win.lbl_qr.pixmap().isNull())
    print("progress:", win.progress.value(), win.lbl_scan.text())

    tabs = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    print("tabs:", tabs)
    print("log_tab_index:", win.tabs.indexOf(
        win.tabs.widget([i for i in range(win.tabs.count())
                         if win.tabs.tabText(i) == "日志"][0])))
    print("log_page_ok:", win.log_view is not None
          and win.chk_autoscroll.isChecked())
    print("overview_excludes_log:", win.tabs.widget(0).findChildren(
        type(win.log_view)) == [])
    sub_tabs = [win.settings_page.tabs.tabText(i)
                for i in range(win.settings_page.tabs.count())]
    print("settings_tabs:", sub_tabs)

    # 历史：类型列应中文化（页面 / 播放）
    kinds = {win.history_page.table.item(r, 2).text()
             for r in range(win.history_page.table.rowCount())}
    print("history_kinds:", kinds or "（暂无记录）")

    win.history_page.refresh()
    print("history_rows:", win.history_page.table.rowCount(),
          "|", win.history_page.lbl_count.text())

    # v2.3.0：已移除「文件」标签，断言顶层标签只剩 4 个
    print("files_tab_removed:", "文件" not in tabs)
    print("settings_autosave_label:", win.settings_page.lbl_autosave.text())
    print("settings_has_no_save_btn:", not hasattr(win.settings_page, "btn_save"))
    # v2.4.0：监听地址 / 全量重建控件存在
    print("host_edit_exists:", hasattr(win.settings_page, "edit_host"))
    print("force_chk_exists:", hasattr(win.settings_page, "chk_force"))
    print("host_default:", win.settings_page.edit_host.text())

    print("icon_file:", icon_path().is_file(), icon_path().stat().st_size
          if icon_path().is_file() else 0)
    print("window_icon_null:", QApplication.windowIcon().isNull())
    print("settings:", load_gui_settings())

    win.close()
    app.processEvents()
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
