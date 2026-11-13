"""文件管理页：浏览媒体根、删除到回收站、移动、撤回（Ctrl+Z）、回收站恢复。

安全边界全部由 :mod:`mediawall.fileops` 兜住：路径必须在媒体根内、
分区根与媒体根本身不可删除，回收站是本卷的 ``.mediawall_trash``（同卷
``move`` 秒完成，46 GB 的文件也不需要复制）。

耗时操作（进入大目录、清空回收站）走后台线程，主线程只刷新界面。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mediawall import fileops
from mediawall.fileops import FileOpError


def _human_size(num: int) -> str:
    """字节数 → 便于阅读的大小。"""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} B"


class FilesPage(QWidget):
    """媒体根文件浏览器 + 回收站入口。"""

    #: 状态提示（主窗口日志区复用）
    notify = Signal(str)

    def __init__(self, root_getter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._root_getter = root_getter   # 返回当前媒体根字符串
        self._rel = ""                    # 当前所在目录（相对媒体根）
        self._busy = False
        self._build_ui()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        self.btn_up = QPushButton("↑ 上级")
        self.btn_up.clicked.connect(self._go_up)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(self.btn_up)
        bar.addWidget(self.btn_refresh)
        self.lbl_path = QLabel("/")
        self.lbl_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_path.setStyleSheet("color: #444;")
        bar.addWidget(self.lbl_path, 1)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "大小"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self._enter)
        layout.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_delete = QPushButton("删除到回收站")
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_move = QPushButton("移动到…")
        self.btn_move.clicked.connect(self._move_selected)
        self.btn_undo = QPushButton("撤回 (Ctrl+Z)")
        self.btn_undo.clicked.connect(self._undo)
        self.btn_trash = QPushButton("回收站…")
        self.btn_trash.clicked.connect(self._open_trash)
        btns.addWidget(self.btn_delete)
        btns.addWidget(self.btn_move)
        btns.addWidget(self.btn_undo)
        btns.addStretch(1)
        btns.addWidget(self.btn_trash)
        layout.addLayout(btns)

        self.lbl_hint = QLabel("提示：删除只会移入回收站（同卷秒完成），可随时撤回或恢复。")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: #666;")
        layout.addWidget(self.lbl_hint)

    # ------------------------------------------------------------------
    # 目录读取
    # ------------------------------------------------------------------

    def _root(self) -> Path | None:
        raw = (self._root_getter() or "").strip()
        if not raw:
            return None
        return fileops.norm_root(raw)

    def refresh(self) -> None:
        """重新读取当前目录内容（后台线程，避免 NAS 卡顿 UI）。"""
        root = self._root()
        if root is None:
            self.lbl_path.setText("（未设置媒体根目录）")
            self.table.setRowCount(0)
            return
        rel = self._rel
        self._set_busy(True)
        threading.Thread(
            target=self._load_dir, args=(root, rel), daemon=True).start()

    def _load_dir(self, root: Path, rel: str) -> None:
        entries: list[tuple[str, str, str]] = []
        error = ""
        try:
            target = root if not rel else fileops.resolve(root, rel)
            if not target.is_dir():
                raise FileOpError(f"目录不存在：{rel}")
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if child.name.startswith("."):
                    continue  # 隐藏 .mediawall_trash 等内部目录
                try:
                    if child.is_dir():
                        entries.append((child.name, "目录", ""))
                    else:
                        entries.append(
                            (child.name, "文件", _human_size(child.stat().st_size)))
                except OSError:
                    entries.append((child.name, "文件", "?"))
        except (FileOpError, OSError) as exc:
            error = str(exc)
        self._apply_rows(rel, entries, error)

    def _apply_rows(self, rel: str, entries: list[tuple[str, str, str]],
                    error: str) -> None:
        self._rel = rel
        self.lbl_path.setText("/" + rel if rel else "/")
        self.lbl_path.setToolTip(str(self._root() or ""))
        self.table.setRowCount(0)
        if error:
            self.lbl_hint.setText(f"读取失败：{error}")
            self._set_busy(False)
            return
        for name, kind, size in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            first = QTableWidgetItem(name)
            first.setData(Qt.ItemDataRole.UserRole, name)
            self.table.setItem(row, 0, first)
            self.table.setItem(row, 1, QTableWidgetItem(kind))
            self.table.setItem(row, 2, QTableWidgetItem(size))
        self.lbl_hint.setText(
            f"共 {len(entries)} 项。双击目录进入；选中后删除或移动。")
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for btn in (self.btn_up, self.btn_refresh, self.btn_delete,
                    self.btn_move, self.btn_undo, self.btn_trash):
            btn.setEnabled(not busy)

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def _enter(self, index) -> None:
        row = index.row()
        item = self.table.item(row, 0)
        if item is None or self.table.item(row, 1).text() != "目录":
            return
        name = item.data(Qt.ItemDataRole.UserRole) or item.text()
        self._rel = f"{self._rel}/{name}" if self._rel else name
        self.refresh()

    def _go_up(self) -> None:
        if not self._rel:
            return
        parts = self._rel.split("/")[:-1]
        self._rel = "/".join(parts)
        self.refresh()

    def _selected_names(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        names = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                names.append(item.data(Qt.ItemDataRole.UserRole) or item.text())
        return names

    def _selected_rels(self) -> list[str]:
        out = []
        for name in self._selected_names():
            out.append(f"{self._rel}/{name}" if self._rel else name)
        return out

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def _delete_selected(self) -> None:
        root = self._root()
        rels = self._selected_rels()
        if root is None or not rels:
            QMessageBox.information(self, "删除", "请先选中要删除的文件或目录。")
            return
        preview = "、".join(rels[:5]) + ("…" if len(rels) > 5 else "")
        answer = QMessageBox.question(
            self, "删除到回收站",
            f"将以下 {len(rels)} 项移入回收站？\n\n{preview}\n\n"
            "回收站位于媒体根下的 .mediawall_trash，可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        threading.Thread(
            target=self._run_delete, args=(root, rels), daemon=True).start()

    def _run_delete(self, root: Path, rels: list[str]) -> None:
        done, failed = [], []
        for rel in rels:
            try:
                fileops.move_to_trash(root, rel)
                done.append(rel)
            except (FileOpError, OSError) as exc:
                failed.append(f"{rel}（{exc}）")
        self._after_op(done, failed, "删除")

    def _move_selected(self) -> None:
        root = self._root()
        rels = self._selected_rels()
        if root is None or not rels:
            QMessageBox.information(self, "移动", "请先选中要移动的文件或目录。")
            return
        target = QFileDialog.getExistingDirectory(
            self, "选择目标目录（必须在媒体根内）", str(root))
        if not target:
            return
        try:
            dst_rel = fileops.rel_of(root, fileops.norm_root(target))
        except (FileOpError, OSError) as exc:
            QMessageBox.warning(self, "移动", f"目标目录不可用：{exc}")
            return
        if dst_rel == "" and len(rels) == 1 and rels[0] == "":
            return
        preview = "、".join(rels[:5]) + ("…" if len(rels) > 5 else "")
        answer = QMessageBox.question(
            self, "移动到其他目录",
            f"将 {len(rels)} 项移动到：\n{dst_rel or '（媒体根）'}\n\n{preview}\n\n"
            "可用 Ctrl+Z 撤回。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        threading.Thread(
            target=self._run_move, args=(root, rels, dst_rel), daemon=True).start()

    def _run_move(self, root: Path, rels: list[str], dst_rel: str) -> None:
        done, failed = [], []
        for rel in rels:
            try:
                fileops.move_item(root, rel, dst_rel)
                done.append(rel)
            except (FileOpError, OSError) as exc:
                failed.append(f"{rel}（{exc}）")
        self._after_op(done, failed, "移动")

    def _undo(self) -> None:
        root = self._root()
        if root is None:
            return
        self._set_busy(True)
        threading.Thread(target=self._run_undo, args=(root,), daemon=True).start()

    def _run_undo(self, root: Path) -> None:
        try:
            message = fileops.undo_last(root)
            ok, failed = [message], []
        except (FileOpError, OSError) as exc:
            ok, failed = [], [str(exc)]
        self._after_op(ok, failed, "撤回")

    def _after_op(self, done: list[str], failed: list[str], label: str) -> None:
        """操作完成后回到主线程刷新界面并提示。"""
        lines = []
        if done:
            lines.append(f"{label}成功 {len(done)} 项")
        if failed:
            lines.append(f"{label}失败 {len(failed)} 项：" + "；".join(failed[:3]))
        text = "；".join(lines) or f"{label}完成"
        self.notify.emit(f"[gui] {text}")
        self.refresh()
        if failed:
            QMessageBox.warning(self, label, text)

    # ------------------------------------------------------------------
    # 回收站
    # ------------------------------------------------------------------

    def _open_trash(self) -> None:
        root = self._root()
        if root is None:
            return
        dialog = TrashDialog(root, parent=self)
        dialog.exec()
        self.notify.emit("[gui] 已查看回收站")
        self.refresh()


class TrashDialog(QDialog):
    """回收站：列出已删除项，支持恢复 / 清空。"""

    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.setWindowTitle("回收站")
        self.setMinimumSize(720, 480)
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["删除时间", "原名", "原位置", "大小"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        for col in range(4):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.lbl_count = QLabel("")
        layout.addWidget(self.lbl_count)

        bar = QHBoxLayout()
        self.btn_restore = QPushButton("恢复到原位置")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_empty = QPushButton("清空回收站（不可恢复）")
        self.btn_empty.clicked.connect(self._empty)
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        bar.addWidget(self.btn_restore)
        bar.addStretch(1)
        bar.addWidget(self.btn_empty)
        bar.addWidget(close_box)
        layout.addLayout(bar)

    def reload(self) -> None:
        entries = fileops.list_trash(self.root)
        self.table.setRowCount(0)
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                str(entry.get("deleted_at") or ""),
                str(entry.get("name") or ""),
                str(entry.get("orig_rel") or ""),
                _human_size(int(entry.get("size") or 0)),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.get("id"))
                self.table.setItem(row, col, item)
        self.lbl_count.setText(
            f"共 {len(entries)} 项，"
            + ("不占用额外空间（与媒体根同卷）" if entries else "回收站为空"))

    def _selected_id(self) -> str:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if not rows:
            return ""
        item = self.table.item(rows[0], 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _restore(self) -> None:
        entry_id = self._selected_id()
        if not entry_id:
            QMessageBox.information(self, "恢复", "请先选中要恢复的项。")
            return
        try:
            message = fileops.restore(self.root, entry_id)
        except (FileOpError, OSError) as exc:
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        QMessageBox.information(self, "恢复", message)
        self.reload()

    def _empty(self) -> None:
        entries = fileops.list_trash(self.root)
        if not entries:
            QMessageBox.information(self, "清空", "回收站已经是空的。")
            return
        answer = QMessageBox.question(
            self, "清空回收站",
            f"将永久删除回收站里的 {len(entries)} 项，此操作不可恢复。\n\n"
            "确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.btn_empty.setEnabled(False)
        self.btn_restore.setEnabled(False)
        threading.Thread(target=self._run_empty, daemon=True).start()

    def _run_empty(self) -> None:
        try:
            result = fileops.empty_trash(self.root)
        except (FileOpError, OSError) as exc:
            self.reload()
            QMessageBox.warning(self, "清空失败", str(exc))
            return
        self.reload()
        self.btn_empty.setEnabled(True)
        self.btn_restore.setEnabled(True)
        QMessageBox.information(
            self, "清空完成",
            f"已永久删除 {result.get('removed', 0)} 项，"
            f"释放 {_human_size(int(result.get('bytes') or 0))}。")
