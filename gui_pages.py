"""历史记录页与设置页（分页标签内容）。

两个页面都是纯展示 + 编辑控件，不直接操作服务进程；保存后的副作用
（端口变更需重启服务等）由主窗口统一处理，见 ``gui_window.MainWindow``。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui_config import (
    DEFAULT_HISTORY_LIMIT,
    categories_to_text,
    history_settings,
    save_gui_settings,
    save_service_config,
    service_config,
    text_to_categories,
)
from mediawall import history as hist
from mediawall.paths import program_dir

_KIND_TEXT = {"play": "播放", "page": "访问首页"}


class HistoryPage(QWidget):
    """历史记录：谁、什么时候、从哪个 IP、看了什么。"""

    notify = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._auto_refresh)
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_clear = QPushButton("清空历史")
        self.btn_clear.clicked.connect(self._clear)
        self.chk_auto = QCheckBox("自动刷新（2 秒）")
        self.chk_auto.setChecked(True)
        self.chk_auto.stateChanged.connect(self._on_auto_toggled)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_clear)
        bar.addWidget(self.chk_auto)
        bar.addStretch(1)
        self.lbl_count = QLabel("")
        bar.addWidget(self.lbl_count)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["时间", "来源 IP", "类型", "分区", "文件"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: #666;")
        layout.addWidget(self.lbl_hint)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        try:
            events = hist.list_events(limit=500)
            total = hist.event_count()
        except Exception as exc:  # noqa: BLE001 - 历史读失败不影响主功能
            self.lbl_hint.setText(f"读取历史失败：{exc}")
            return
        self.table.setRowCount(0)
        for event in events:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                str(event.get("ts") or "").replace("T", " ")[:19],
                str(event.get("ip") or "—"),
                _KIND_TEXT.get(str(event.get("kind")), str(event.get("kind") or "")),
                str(event.get("category") or "—"),
                str(event.get("name") or event.get("rel") or ""),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.lbl_count.setText(f"显示 {len(events)} / 共 {total} 条")
        limit, record_pages = history_settings()
        parts = [
            "保留：" + ("不限制" if limit <= 0 else f"最近 {limit} 条"),
            "记录首页访问：" + ("开" if record_pages else "关"),
            "同一 IP 同一文件 30 秒内只记一次",
        ]
        self.lbl_hint.setText("；".join(parts))

    def _auto_refresh(self) -> None:
        if self.isVisible():
            self.refresh()

    def _on_auto_toggled(self, state: int) -> None:
        if state:
            self._timer.start()
        else:
            self._timer.stop()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if self.chk_auto.isChecked():
            self._timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().hideEvent(event)
        self._timer.stop()

    def _clear(self) -> None:
        answer = QMessageBox.question(
            self, "清空历史",
            "确定清空全部历史记录？此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            hist.clear()
        except OSError as exc:
            QMessageBox.warning(self, "清空失败", str(exc))
            return
        self.notify.emit("[gui] 历史记录已清空")
        self.refresh()


class SettingsPage(QWidget):
    """设置页：内部再分页（基本 / 历史 / 扫描 / 关于）。

    v2.3.0 起改为**修改即保存**：任何控件变更立即落盘并生效，
    不再需要点「保存设置」。端口 / 媒体根等需要重启服务才生效的项，
    变更时会询问是否重启。
    """

    #: 保存完成，payload 含 port / media_root / categories / auto_start
    saved = Signal(dict)

    def __init__(self, port: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_port = int(port)
        self._building = True  # 构建期屏蔽变更信号
        self._build_ui()
        self._building = False
        self.reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_basic_tab(), "基本")
        self.tabs.addTab(self._build_history_tab(), "历史")
        self.tabs.addTab(self._build_scan_tab(), "扫描")
        self.tabs.addTab(self._build_about_tab(), "关于")
        layout.addWidget(self.tabs, 1)

        bar = QHBoxLayout()
        self.lbl_autosave = QLabel("所有设置修改后立即自动保存并生效。")
        self.lbl_autosave.setStyleSheet("color: #666;")
        bar.addWidget(self.lbl_autosave)
        bar.addStretch(1)
        layout.addLayout(bar)

    # ---- 基本 -------------------------------------------------------

    def _build_basic_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.edit_root = QLineEdit()
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self.edit_root, 1)
        root_row.addWidget(browse)
        root_box = QWidget()
        root_box.setLayout(root_row)
        form.addRow("媒体根目录：", root_box)
        self.edit_root.editingFinished.connect(self._on_root_edited)
        self.edit_root.returnPressed.connect(self._on_root_edited)

        self.spin_port = QSpinBox()
        self.spin_port.setRange(1024, 65535)
        self.spin_port.valueChanged.connect(self._on_port_changed)
        form.addRow("监听端口：", self.spin_port)

        # v2.4.0：监听地址（对应 server.py --host）
        self.edit_host = QLineEdit()
        self.edit_host.setPlaceholderText("0.0.0.0")
        self.edit_host.setToolTip(
            "0.0.0.0 = 所有网卡（手机/平板可访问，默认）\n"
            "127.0.0.1 = 仅本机\n"
            "或填本机局域网 IP（如 192.168.x.x）")
        self.edit_host.editingFinished.connect(self._on_host_edited)
        self.edit_host.returnPressed.connect(self._on_host_edited)
        form.addRow("监听地址：", self.edit_host)

        self.chk_auto = QCheckBox("启动桌面面板时自动启动服务")
        self.chk_auto.stateChanged.connect(self._on_auto_changed)
        form.addRow("", self.chk_auto)

        hint = QLabel("提示：媒体根可以是 NAS 共享目录（如 \\\\NAS\\Share\\Download），也可以是本地盘（如 D:\\Media）。"
                      "修改端口、监听地址或媒体根后需重启服务才生效。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        form.addRow("", hint)
        return page

    # ---- 历史 -------------------------------------------------------

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.spin_hist = QSpinBox()
        self.spin_hist.setRange(0, 1000000)
        self.spin_hist.setSingleStep(100)
        self.spin_hist.setSpecialValueText("不限制")
        self.spin_hist.valueChanged.connect(self._on_history_changed)
        form.addRow("保留条数：", self.spin_hist)

        self.chk_pages = QCheckBox("记录网页首页访问（关闭后仍记录播放）")
        self.chk_pages.stateChanged.connect(self._on_history_changed)
        form.addRow("", self.chk_pages)

        hint = QLabel(
            f"默认保留 {DEFAULT_HISTORY_LIMIT} 条；填 0 表示不限制（文件会一直增长）。\n"
            "历史用于查看「谁在什么时候看了哪个文件」，只存在本机 data/history.json。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        form.addRow("", hint)
        return page

    # ---- 扫描 -------------------------------------------------------

    def _build_scan_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.edit_cats = QLineEdit()
        self.edit_cats.setPlaceholderText(
            "留空 = 自动发现全部分区；多个用逗号分隔，如：01 电影,20 综艺")
        self.edit_cats.editingFinished.connect(self._on_categories_changed)
        self.edit_cats.returnPressed.connect(self._on_categories_changed)
        form.addRow("分区白名单：", self.edit_cats)
        hint = QLabel("只扫描白名单里的分区，可显著缩短扫描时间。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        form.addRow("", hint)

        # v2.4.0：全量重建（--force）
        self.chk_force = QCheckBox("重新扫描时全量重建（忽略增量缓存）")
        self.chk_force.setToolTip(
            "勾选后，面板「重新扫描全部」会忽略增量缓存、从头遍历全部分区。\n"
            "比增量扫描慢很多（16 分区约 3~4 分钟），仅在缓存异常时开启。")
        self.chk_force.stateChanged.connect(self._on_force_changed)
        form.addRow("", self.chk_force)
        hint2 = QLabel("默认关闭 = 增量扫描（复用上次扫描缓存，通常十几秒到一分钟）。")
        hint2.setWordWrap(True)
        hint2.setStyleSheet("color: #666;")
        form.addRow("", hint2)
        return page

    # ---- 关于 -------------------------------------------------------

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.lbl_ver = QLabel("—")
        self.lbl_data = QLabel(str(program_dir() / "data"))
        self.lbl_data.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_trash = QLabel("—")
        self.lbl_trash.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_icon = QLabel("—")
        form.addRow("程序版本：", self.lbl_ver)
        form.addRow("数据目录：", self.lbl_data)
        form.addRow("回收站目录：", self.lbl_trash)
        form.addRow("图标文件：", self.lbl_icon)

        hint = QLabel(
            "回收站位于媒体根下的 .mediawall_trash，与媒体根同卷，删除不占额外空间；"
            "扫描会自动忽略该目录。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        form.addRow("", hint)
        return page

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def reload(self) -> None:
        self._building = True
        try:
            cfg = service_config()
            self.edit_root.setText(str(cfg.get("media_root") or ""))
            self.edit_cats.setText(categories_to_text(cfg.get("categories")))
            try:
                limit = int(cfg.get("history_limit", DEFAULT_HISTORY_LIMIT))
            except (TypeError, ValueError):
                limit = DEFAULT_HISTORY_LIMIT
            self.spin_hist.setValue(max(0, limit))
            self.chk_pages.setChecked(bool(cfg.get("history_record_pages", True)))
            self.spin_port.setValue(self._current_port)
            settings = _gui_settings()
            self.chk_auto.setChecked(bool(settings.get("auto_start", True)))
            # v2.4.0：监听地址 / 全量重建偏好
            self.edit_host.setText(str(settings.get("host") or "0.0.0.0"))
            self.chk_force.setChecked(bool(settings.get("scan_force", False)))

            from mediawall import VERSION
            self.lbl_ver.setText(str(VERSION))
            root = str(cfg.get("media_root") or "")
            self.lbl_trash.setText(
                str(program_dir() / "data") if not root
                else f"{root.rstrip(chr(92))}\\.mediawall_trash")
            icon = program_dir() / "icon.ico"
            self.lbl_icon.setText("已存在" if icon.is_file() else "不存在（程序会自动生成）")
        finally:
            self._building = False

    def set_port(self, port: int) -> None:
        self._current_port = int(port)
        self.spin_port.setValue(int(port))

    def _browse_root(self) -> None:
        start = self.edit_root.text().strip() or str(program_dir())
        path = QFileDialog.getExistingDirectory(self, "选择媒体根目录", start)
        if path:
            self.edit_root.setText(path)
            self._on_root_edited()

    # ------------------------------------------------------------------
    # 修改即保存（v2.3.0）
    # ------------------------------------------------------------------

    def _emit_saved(self) -> None:
        """把当前控件值汇总成 payload 并发出 saved 信号（主窗口处理副作用）。"""
        root = self.edit_root.text().strip()
        if not root:
            return
        payload = {
            "port": int(self.spin_port.value()),
            "host": str(self.edit_host.text() or "0.0.0.0").strip(),
            "media_root": root,
            "categories": text_to_categories(self.edit_cats.text()),
            "auto_start": self.chk_auto.isChecked(),
            "history_limit": int(self.spin_hist.value()),
            "history_record_pages": self.chk_pages.isChecked(),
            "scan_force": self.chk_force.isChecked(),
        }
        self._current_port = int(payload["port"])
        self.saved.emit(payload)

    def _on_root_edited(self) -> None:
        if self._building:
            return
        root = self.edit_root.text().strip()
        if not root:
            return
        try:
            save_service_config(root, None, None, None)
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, "设置", "写入配置文件失败，请检查程序目录是否可写。")
            return
        self._emit_saved()

    def _on_host_edited(self) -> None:
        """监听地址修改即保存（v2.4.0）。非法值回退 0.0.0.0。"""
        if self._building:
            return
        from gui_config import normalize_host
        host = normalize_host(self.edit_host.text())
        self.edit_host.setText(host)  # 归一显示
        save_gui_settings({
            "port": int(self.spin_port.value()),
            "host": host,
            "auto_start": self.chk_auto.isChecked(),
        })
        self._emit_saved()

    def _on_port_changed(self, value: int) -> None:
        if self._building:
            return
        save_gui_settings({
            "port": value,
            "host": str(self.edit_host.text() or "0.0.0.0").strip(),
            "auto_start": self.chk_auto.isChecked(),
        })
        self._emit_saved()

    def _on_auto_changed(self, state: int) -> None:
        if self._building:
            return
        save_gui_settings({
            "port": int(self.spin_port.value()),
            "host": str(self.edit_host.text() or "0.0.0.0").strip(),
            "auto_start": bool(state),
        })

    def _on_force_changed(self, state: int) -> None:
        """全量重建偏好（v2.4.0）：只存偏好，不立即触发扫描。"""
        if self._building:
            return
        save_gui_settings({"scan_force": bool(state)})

    def _on_history_changed(self) -> None:
        if self._building:
            return
        try:
            ok = save_service_config(
                self.edit_root.text().strip(),
                None,
                int(self.spin_hist.value()),
                self.chk_pages.isChecked(),
            )
        except (TypeError, ValueError):
            ok = False
        if not ok:
            QMessageBox.warning(self, "设置", "写入配置文件失败，请检查程序目录是否可写。")

    def _on_categories_changed(self) -> None:
        if self._building:
            return
        try:
            ok = save_service_config(
                self.edit_root.text().strip(),
                text_to_categories(self.edit_cats.text()),
                None,
                None,
            )
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            QMessageBox.warning(self, "设置", "写入配置文件失败，请检查程序目录是否可写。")


def _gui_settings() -> dict[str, Any]:
    """避免 gui_pages 直接依赖 gui_window 的加载顺序。"""
    from gui_config import load_gui_settings
    return load_gui_settings()
