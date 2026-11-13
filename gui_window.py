"""Media_Wall 桌面控制面板 —— 主窗口（PySide6）。

界面是分页结构（顶层标签），避免窗口过高：

1. 概览：服务状态 / 访问方式（含二维码）/ 扫描进度 / 运行日志；
2. 历史：谁在什么时候从哪个 IP 看了什么（播放 + 首页访问）；
3. 日志：服务进程输出；
4. 设置：内部再分页（基本 / 历史 / 扫描 / 关于），修改即保存（v2.3.0）。

v2.3.0：移除「文件」标签（文件管理只在客户端网页端提供）。

线程模型：启动/停止/触发扫描都丢到后台线程执行，主线程只刷新
界面；``ServiceManager`` 的后台线程负责读子进程输出与轮询 ``/api/status``，
经 Qt 信号回到主线程，避免 UI 卡顿。
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui_config import load_gui_settings, save_gui_settings
from gui_pages import HistoryPage, SettingsPage
from gui_service import ServiceManager, qr_matrix
from mediawall.common import DEFAULT_PORT, detect_lan_ip
from mediawall.netfix import check_lan_access, describe_report, fix_lan_access

#: 日志区最多保留行数（防止长时间运行无限增长）
_MAX_LOG_LINES = 2000


def _render_qr(url: str, size: int = 180) -> QPixmap | None:
    """把二维码矩阵画成 QPixmap；qrcode 缺失时返回 None（界面降级）。"""
    matrix = qr_matrix(url)
    if not matrix:
        return None
    n = len(matrix)
    if n <= 0:
        return None
    px = max(1, size // n)
    side = n * px
    pixmap = QPixmap(side, side)
    pixmap.fill(QColor("white"))
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, cell in enumerate(row):
            if cell:
                painter.drawRect(x * px, y * px, px, px)
    painter.end()
    return pixmap.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


class MainWindow(QMainWindow):
    """桌面控制面板主窗口。"""

    #: 防火墙检测结果（说明文本, 是否被拦截）。检测要起 PowerShell 子进程，
    #: 放到工作线程执行，经信号回主线程刷新界面，避免卡住 UI。
    fw_checked = Signal(str, bool)

    def __init__(self, port: int | None = None, host: str | None = None,
                 autostart: bool = True) -> None:
        super().__init__()
        self.settings = load_gui_settings()
        self.port = int(port if port is not None else self.settings["port"])
        # v2.4.0：监听地址可从设置页改（--host）。调用方显式传 host 时优先，
        # 否则用 gui_settings.json 里保存的值（默认 0.0.0.0）。
        self.host = host if host is not None else str(self.settings.get("host") or "0.0.0.0")
        self.autostart = bool(self.settings.get("auto_start", True)) and autostart

        self.service = ServiceManager(port=self.port, host=self.host, parent=self)
        self.service.log_line.connect(self._append_log)
        self.service.status_changed.connect(self._on_status)
        self.service.state_changed.connect(self._on_state)
        self.service.scan_progress.connect(self._on_scan_progress)
        # v2.5.3：端口被占自动升级后刷新访问地址
        self.service.port_changed.connect(self._on_port_changed)

        self._lan_ip = detect_lan_ip()
        self._words: dict[str, QLabel] = {}
        # v2.10.2：防火墙检测在后台线程跑，结果经信号回主线程
        self.fw_checked.connect(self._on_fw_checked)

        self.setWindowTitle(f"Media_Wall 控制面板 · 端口 {self.port}")
        self.setMinimumSize(720, 480)
        # 窗口大小：记忆上次调整过的尺寸（v2.2.1），避免每次都要重新拖
        _saved_w = int(self.settings.get("win_w") or 940)
        _saved_h = int(self.settings.get("win_h") or 760)
        self.resize(_saved_w, _saved_h)
        self._build_ui()
        self._refresh_access_urls()
        self._on_state("stopped")
        # 界面起来后再查防火墙（不阻塞窗口出现）
        QTimer.singleShot(200, self._check_firewall_async)

        # 自动启动：窗口显示后触发（避免在构造期阻塞）
        if self.autostart:
            QTimer.singleShot(300, self._start_service)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_page(), "概览")
        self.history_page = HistoryPage(parent=self)
        self.history_page.notify.connect(self._append_log)
        self.tabs.addTab(self.history_page, "历史")
        # 运行日志单独一页（v2.0.2：原来挤在「概览」底部，拉起窗口时把概览压扁了）
        self.tabs.addTab(self._build_log_page(), "日志")
        self.settings_page = SettingsPage(port=self.port, parent=self)
        self.settings_page.saved.connect(self._on_settings_saved)
        self.tabs.addTab(self.settings_page, "设置")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        self.lbl_bottom = QLabel("")
        self.lbl_bottom.setStyleSheet("color: #888;")
        self.btn_quit = QPushButton("退出")
        self.btn_quit.clicked.connect(self.close)
        bottom.addWidget(self.lbl_bottom, 1)
        bottom.addWidget(self.btn_quit)
        root.addLayout(bottom)

        self.setCentralWidget(central)

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_service_group())
        layout.addWidget(self._build_access_group())
        layout.addWidget(self._build_scan_group())
        layout.addStretch(1)
        return page

    def _build_service_group(self) -> QGroupBox:
        box = QGroupBox("服务状态")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        self.lbl_state = QLabel("已停止")
        self.lbl_state.setStyleSheet("font-size: 20px; font-weight: bold;")
        row.addWidget(self.lbl_state)
        self.lbl_detail = QLabel("")
        self.lbl_detail.setStyleSheet("color: #666;")
        row.addWidget(self.lbl_detail, 1)
        layout.addLayout(row)

        info = QFormLayout()
        self._words["port"] = QLabel(str(self.port))
        self._words["root"] = QLabel("—")
        self._words["version"] = QLabel("—")
        info.addRow("监听端口：", self._words["port"])
        info.addRow("媒体根目录：", self._words["root"])
        info.addRow("服务版本：", self._words["version"])
        layout.addLayout(info)

        btns = QHBoxLayout()
        self.btn_start = QPushButton("启动服务")
        self.btn_start.clicked.connect(self._start_service)
        self.btn_stop = QPushButton("停止服务")
        self.btn_stop.clicked.connect(self._stop_service)
        self.btn_restart = QPushButton("重启服务")
        self.btn_restart.clicked.connect(self._restart_service)
        btns.addWidget(self.btn_start)
        btns.addWidget(self.btn_stop)
        btns.addWidget(self.btn_restart)
        btns.addStretch(1)
        layout.addLayout(btns)
        return box

    def _build_access_group(self) -> QGroupBox:
        box = QGroupBox("访问方式")
        layout = QHBoxLayout(box)

        left = QFormLayout()
        self._words["local_url"] = QLabel("—")
        self._words["lan_url"] = QLabel("—")
        self._words["lan_url"].setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addRow("本机打开：", self._words["local_url"])
        left.addRow("局域网访问：", self._words["lan_url"])

        # v2.10.2：防火墙状态行。编译态常因 exe 命中 Block 规则而无法被
        # 局域网访问（源码态有 python.exe 的放行规则所以正常），这里把状态
        # 直接摊开给用户看，并提供一键提权修复。
        self.lbl_fw = QLabel("检测中…（首次需数秒）")
        self.lbl_fw.setWordWrap(True)
        self.lbl_fw.setStyleSheet("color: #666;")
        left.addRow("防火墙：", self.lbl_fw)
        layout.addLayout(left, 1)

        btn_col = QVBoxLayout()
        self.btn_open = QPushButton("打开浏览器")
        self.btn_open.clicked.connect(self._open_browser)
        self.btn_copy = QPushButton("复制局域网地址")
        self.btn_copy.clicked.connect(self._copy_lan_url)
        # 只在被拦截时高亮提示；平时低调存在，不干扰正常用户
        self.btn_fixfw = QPushButton("修复局域网访问")
        self.btn_fixfw.setToolTip(
            "检测到防火墙阻止本程序时点击：\n"
            "会弹一次管理员授权（UAC），自动删除阻止规则并添加放行规则。")
        self.btn_fixfw.clicked.connect(self._fix_firewall)
        btn_col.addWidget(self.btn_open)
        btn_col.addWidget(self.btn_copy)
        btn_col.addWidget(self.btn_fixfw)
        btn_col.addStretch(1)
        layout.addLayout(btn_col)

        self.lbl_qr = QLabel("（二维码不可用）")
        self.lbl_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_qr.setFixedWidth(200)
        layout.addWidget(self.lbl_qr)
        return box

    def _build_scan_group(self) -> QGroupBox:
        box = QGroupBox("扫描进度")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        self.btn_scan = QPushButton("重新扫描全部")
        self.btn_scan.clicked.connect(self._trigger_scan)
        self.lbl_scan = QLabel("未扫描")
        self.lbl_scan.setStyleSheet("color: #666;")
        row.addWidget(self.btn_scan)
        row.addWidget(self.lbl_scan, 1)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setMinimumHeight(22)
        self.progress.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.progress)
        return box

    def _build_log_page(self) -> QWidget:
        """运行日志独立页（v2.0.2）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_log_group(), 1)
        return page

    def _build_log_group(self) -> QGroupBox:
        box = QGroupBox("运行日志")
        layout = QVBoxLayout(box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(_MAX_LOG_LINES)
        self.log_view.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        layout.addWidget(self.log_view)

        row = QHBoxLayout()
        self.chk_autoscroll = QCheckBox("自动滚动到最新")
        self.chk_autoscroll.setChecked(True)
        btn_copy = QPushButton("复制全部")
        btn_copy.clicked.connect(self._copy_log)
        btn_clear = QPushButton("清空日志")
        btn_clear.clicked.connect(self.log_view.clear)
        row.addWidget(self.chk_autoscroll)
        row.addStretch(1)
        row.addWidget(btn_copy)
        row.addWidget(btn_clear)
        layout.addLayout(row)
        return box

    def _copy_log(self) -> None:
        text = self.log_view.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    # ------------------------------------------------------------------
    # 状态与信号
    # ------------------------------------------------------------------

    def _on_tab_changed(self, index: int) -> None:
        # v2.3.0：已移除「文件」标签，无需在切页时刷新文件浏览。
        del index

    def _on_state(self, state: str) -> None:
        mapping = {
            "starting": ("启动中…", "#d97706"),
            "running": ("运行中", "#16a34a"),
            "stopping": ("停止中…", "#d97706"),
            "stopped": ("已停止", "#6b7280"),
            "failed": ("启动失败", "#dc2626"),
        }
        text, color = mapping.get(state, (state, "#6b7280"))
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {color};")

        running = state == "running"
        busy = state in ("starting", "stopping")
        self.btn_start.setEnabled(not running and not busy)
        self.btn_stop.setEnabled(running)
        self.btn_restart.setEnabled(running)
        self.btn_scan.setEnabled(running)
        self.btn_open.setEnabled(running)
        self.btn_copy.setEnabled(running)
        self.lbl_bottom.setText(
            "服务运行中" if running else ("服务未就绪" if busy else "服务已停止"))
        if state == "stopped":
            self._on_scan_progress(-1, "未扫描")
        if state == "failed":
            self.lbl_detail.setText("请查看运行日志了解失败原因。")
        else:
            self.lbl_detail.setText("")

    def _on_status(self, payload: dict[str, Any]) -> None:
        self._words["version"].setText(str(payload.get("version") or "—"))
        self._words["root"].setText(str(payload.get("root") or "—"))
        self._words["port"].setText(str(self.port))
        self.setWindowTitle(f"Media_Wall 控制面板 · 端口 {self.port} · 运行中")

    def _on_port_changed(self, new_port: int) -> None:
        """v2.5.3：端口被占自动升级后，刷新显示与访问地址。"""
        self.port = int(new_port)
        self.setWindowTitle(f"Media_Wall 控制面板 · 端口 {self.port}")
        self._words["port"].setText(str(self.port))
        self._refresh_access_urls()

    def _on_scan_progress(self, pct: int, text: str) -> None:
        self.lbl_scan.setText(text)
        if pct < 0:
            if text.startswith("扫描中"):
                # 没有分区总数时用忙碌状态，避免进度条假装有百分比
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)
        chk = getattr(self, "chk_autoscroll", None)
        if chk is not None and chk.isChecked():
            bar = self.log_view.verticalScrollBar()
            bar.setValue(bar.maximum())

    # ------------------------------------------------------------------
    # 访问地址 / 二维码
    # ------------------------------------------------------------------

    def _refresh_access_urls(self) -> None:
        local = f"http://127.0.0.1:{self.port}/"
        # v2.4.0：监听地址改了的话，局域网 URL 跟随设置。
        # 0.0.0.0 = 所有网卡 → 显示本机局域网 IP；127.0.0.1 = 仅本机 → 提示不可局域网访问。
        if self.host in ("0.0.0.0", "", None):
            lan_url = f"http://{self._lan_ip}:{self.port}/"
            lan_text = lan_url
        elif self.host in ("127.0.0.1", "localhost", "::1"):
            lan_url = ""
            lan_text = "（仅本机，手机/平板无法访问）"
        else:
            lan_url = f"http://{self.host}:{self.port}/"
            lan_text = lan_url
        self._words["local_url"].setText(local)
        self._words["lan_url"].setText(lan_text)
        pixmap = _render_qr(lan_url) if lan_url else None
        if pixmap is not None:
            self.lbl_qr.setPixmap(pixmap)
            self.lbl_qr.setToolTip(lan_url)
        else:
            self.lbl_qr.setText(
                "（二维码不可用，请手动输入局域网地址）" if lan_url
                else "（仅本机模式，无局域网二维码）")
            self.lbl_qr.setToolTip("")

    def _open_browser(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(f"http://127.0.0.1:{self.port}/"))

    def _copy_lan_url(self) -> None:
        url = self._words["lan_url"].text()
        if url and url != "—":
            QApplication.clipboard().setText(url)
            self._append_log(f"[gui] 已复制局域网地址：{url}")

    # ------------------------------------------------------------------
    # 防火墙状态 / 一键修复（v2.10.2）
    # ------------------------------------------------------------------

    def _check_firewall_async(self) -> None:
        """后台线程检测防火墙放行状态（要起 PowerShell，不能占主线程）。"""

        def _run() -> None:
            try:
                report = check_lan_access()
                text = describe_report(report)
                blocked = report.needs_fix
            except Exception as exc:  # noqa: BLE001 - 检测失败不该影响主功能
                text = f"检测失败：{exc}"
                blocked = False
            self.fw_checked.emit(text, blocked)

        threading.Thread(target=_run, name="mw-gui-fw", daemon=True).start()

    def _on_fw_checked(self, text: str, blocked: bool) -> None:
        """防火墙检测结果回到主线程后刷新界面。"""
        widget = getattr(self, "lbl_fw", None)
        if widget is None:
            return
        widget.setText(text)
        # 被拦截：红字高亮 + 按钮醒目；正常：灰字 + 按钮低调
        if blocked:
            widget.setStyleSheet("color: #c0392b; font-weight: bold;")
            self.btn_fixfw.setEnabled(True)
            self.btn_fixfw.setStyleSheet(
                "background: #c0392b; color: white; font-weight: bold; padding: 4px 8px;")
        else:
            widget.setStyleSheet("color: #666; font-weight: normal;")
            self.btn_fixfw.setEnabled(True)
            self.btn_fixfw.setStyleSheet("")

    def _fix_firewall(self) -> None:
        """一键修复：提权删 Block + 加 Allow（会弹 UAC）。"""
        self.btn_fixfw.setEnabled(False)
        self.lbl_fw.setText("正在修复，请在弹窗中允许管理员权限…")
        self.lbl_fw.setStyleSheet("color: #666;")
        self._append_log("[gui] 开始修复防火墙…")

        def _run() -> None:
            ok, msg = fix_lan_access()
            self.fw_checked.emit(msg if not ok else f"已修复：{msg}", not ok)

        threading.Thread(target=_run, name="mw-gui-fwfix", daemon=True).start()

    # ------------------------------------------------------------------
    # 服务启停（后台线程，避免阻塞 UI）
    # ------------------------------------------------------------------

    def _start_service(self) -> None:
        if self.service.running:
            return
        threading.Thread(
            target=self.service.start, name="mw-gui-start", daemon=True,
        ).start()

    def _stop_service(self) -> None:
        if not self.service.running:
            return
        threading.Thread(
            target=self.service.stop, name="mw-gui-stop", daemon=True,
        ).start()

    def _restart_service(self) -> None:
        def _run() -> None:
            self.service.stop()
            self.service.start()

        threading.Thread(target=_run, name="mw-gui-restart", daemon=True).start()

    def _trigger_scan(self) -> None:
        # v2.4.0：设置页「全量重建」勾选时，概览「重新扫描全部」走 --force
        force = bool(self.settings.get("scan_force", False))
        threading.Thread(
            target=lambda: self.service.trigger_scan(force=force),
            name="mw-gui-scan", daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # 设置与撤回
    # ------------------------------------------------------------------

    def _on_settings_saved(self, payload: dict[str, Any]) -> None:
        new_port = int(payload.get("port", self.port))
        port_changed = new_port != self.port
        new_host = str(payload.get("host") or "0.0.0.0")
        host_changed = new_host != self.host
        self.settings = load_gui_settings()
        if port_changed:
            self.port = new_port
            self.service.port = new_port
            self.setWindowTitle(f"Media_Wall 控制面板 · 端口 {self.port}")
            self._refresh_access_urls()
            self._words["port"].setText(str(self.port))
        if host_changed:
            self.host = new_host
            self.service.host = new_host
            self._refresh_access_urls()
        self._append_log("[gui] 设置已保存")
        # v2.4.0：端口 / 监听地址 / 媒体根变更需重启服务才生效
        if self.service.running and (
                port_changed or host_changed or payload.get("media_root")):
            answer = QMessageBox.question(
                self, "重启服务",
                "配置已保存。是否立即重启服务以使其生效？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._restart_service()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self.service.running:
            answer = QMessageBox.question(
                self, "退出",
                "服务仍在运行。退出面板时是否同时停止服务？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.StandardButton.Yes:
                self.service.stop()
            else:
                # 保持服务运行（子进程带 stdout 管道，面板退出后由服务自行存活）
                self._detach_service()
        event.accept()

    def _detach_service(self) -> None:
        """面板退出但服务保留：停止轮询，放弃对子进程的回收。"""
        self.service._polling = False
        self._append_log("[gui] 面板退出，服务保持运行。")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """窗口尺寸变化时记住（v2.2.1）。"""
        super().resizeEvent(event)
        w = self.width()
        h = self.height()
        # 最小尺寸约束：太小是用户拖过头，记下来也没意义
        if w < 720 or h < 480:
            return
        try:
            save_gui_settings({
                "port": self.port,
                "auto_start": bool(self.settings.get("auto_start", True)),
                "win_w": w,
                "win_h": h,
            })
        except Exception:  # noqa: BLE001 - 保存失败不影响使用
            pass
