"""v2.3.0 设置页「修改即保存」验证：控件变更立即落盘，无需点保存按钮。

offscreen 模式，不弹窗；备份并恢复 config.json / gui_settings.json。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_config import (  # noqa: E402
    GUI_SETTINGS_PATH, load_gui_settings, service_config,
)
from gui_pages import SettingsPage  # noqa: E402

CFG = ROOT / "config.json"
fails: list[str] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)

def main() -> int:
    app = QApplication([])

    # 备份现有配置
    cfg_bak = CFG.read_bytes() if CFG.is_file() else None
    gui_bak = GUI_SETTINGS_PATH.read_bytes() if GUI_SETTINGS_PATH.is_file() else None
    orig_cfg = service_config()
    orig_gui = load_gui_settings()
    try:
        page = SettingsPage(port=orig_gui.get("port", 8787))
        page.show()
        app.processEvents()

        # 1) 媒体根：输入 + 回车 → 立即落盘 config.json
        page.edit_root.setText("D:/Media_Test_Root")
        page._on_root_edited()
        app.processEvents()
        check("根目录修改即落盘", service_config().get("media_root") == "D:/Media_Test_Root",
              str(service_config().get("media_root")))

        # 2) 端口：改 spinbox → 立即落盘 gui_settings.json 并发出 saved
        got: list[dict] = []
        page.saved.connect(lambda p: got.append(p))
        page.spin_port.setValue(9999)
        app.processEvents()
        check("端口修改即落盘 gui_settings", load_gui_settings().get("port") == 9999,
              str(load_gui_settings().get("port")))
        check("端口修改发出 saved 信号", bool(got) and got[-1].get("port") == 9999,
              str(got[-1] if got else None))

        # 3) 历史保留条数：改 spin → 立即落盘 config.json
        page.spin_hist.setValue(123)
        app.processEvents()
        check("历史条数修改即落盘", service_config().get("history_limit") == 123,
              str(service_config().get("history_limit")))

        # 4) 记录首页开关：勾选 → 立即落盘
        page.chk_pages.setChecked(False)
        app.processEvents()
        check("记录首页开关即落盘", service_config().get("history_record_pages") is False,
              str(service_config().get("history_record_pages")))

        # 5) 自动启动开关：勾选 → 立即落盘 gui_settings
        page.chk_auto.setChecked(False)
        app.processEvents()
        check("自动启动开关即落盘", load_gui_settings().get("auto_start") is False,
              str(load_gui_settings().get("auto_start")))

        # 6) 分区白名单：输入 + 回车 → 立即落盘 config.json
        page.edit_cats.setText("01 电影, 20 综艺")
        page._on_categories_changed()
        app.processEvents()
        check("白名单修改即落盘", service_config().get("categories") == ["01 电影", "20 综艺"],
              str(service_config().get("categories")))

        # 7) 窗口尺寸记忆不被设置页保存冲掉（v2.3.0 合并写修复）
        from gui_config import save_gui_settings
        save_gui_settings({"win_w": 1111, "win_h": 888})
        page.spin_port.setValue(7777)
        app.processEvents()
        s = load_gui_settings()
        check("合并写保留窗口尺寸", s.get("win_w") == 1111 and s.get("win_h") == 888,
              f"win={s.get('win_w')}x{s.get('win_h')} port={s.get('port')}")

        # 8) 监听地址：输入 + 回车 → 立即落盘 gui_settings（v2.4.0）
        page.edit_host.setText("127.0.0.1")
        page._on_host_edited()
        app.processEvents()
        check("监听地址修改即落盘", load_gui_settings().get("host") == "127.0.0.1",
              str(load_gui_settings().get("host")))
        check("监听地址发出 saved 信号", bool(got) and got[-1].get("host") == "127.0.0.1",
              str(got[-1] if got else None))

        # 9) 监听地址非法值回退 0.0.0.0
        page.edit_host.setText("999.999.1.1")
        page._on_host_edited()
        app.processEvents()
        check("监听地址非法回退 0.0.0.0",
              load_gui_settings().get("host") == "0.0.0.0"
              and page.edit_host.text() == "0.0.0.0",
              str(load_gui_settings().get("host")))

        # 10) 全量重建偏好：勾选 → 立即落盘（v2.4.0）
        page.chk_force.setChecked(True)
        app.processEvents()
        check("全量重建偏好即落盘", load_gui_settings().get("scan_force") is True,
              str(load_gui_settings().get("scan_force")))
    finally:
        page.close()
        # 恢复配置
        if cfg_bak is not None:
            CFG.write_bytes(cfg_bak)
        else:
            CFG.unlink(missing_ok=True)
        if gui_bak is not None:
            GUI_SETTINGS_PATH.write_bytes(gui_bak)
        else:
            GUI_SETTINGS_PATH.unlink(missing_ok=True)

    print(f"\n结果: PASS {10 - len(fails)} / FAIL {len(fails)}")
    if fails:
        print("失败项:", "；".join(fails))
        return 1
    print("AUTOSAVE_OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
