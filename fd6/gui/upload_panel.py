from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QListWidget, QPushButton, QVBoxLayout, QWidget, QLabel
)

from fd6.gui.widgets import DropZone
from fd6.gui.widgets.drop_zone import SUPPORTED_EXTS


class UploadPanel(QWidget):
    files_selected = Signal(list)        # list[Path] — image files chosen for generation
    json_loaded = Signal(Path)           # User uploaded a JSON: load + show preview (do NOT inject)
    download_json_requested = Signal()   # User wants to save the most-recent generated JSON

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._recent: list[Path] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.upload_btn = QPushButton("Upload Image…")
        self.upload_btn.setMinimumHeight(40)
        self.upload_btn.clicked.connect(self._on_upload_clicked)
        layout.addWidget(self.upload_btn)

        self.drop = DropZone(self)
        self.drop.files_dropped.connect(self._on_files_dropped)
        layout.addWidget(self.drop)

        layout.addSpacing(6)
        # JSON Upload (for re-injecting) + Download (export generated)
        json_row = QHBoxLayout()
        self.upload_json_btn = QPushButton("Upload JSON…")
        self.upload_json_btn.setToolTip(
            "Load a previously-generated FD6 shapes JSON and preview it in the canvas. "
            "Click 'Inject into FH6' afterwards when you're ready to push it into the game."
        )
        self.upload_json_btn.clicked.connect(self._on_upload_json_clicked)
        self.download_json_btn = QPushButton("Download JSON")
        self.download_json_btn.setEnabled(False)
        self.download_json_btn.setToolTip("No generated JSON yet — finish generating an image first")
        self.download_json_btn.clicked.connect(self.download_json_requested.emit)
        self._download_default_style = self.download_json_btn.styleSheet()
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setSingleShot(True)
        self._pulse_timer.timeout.connect(self._end_download_pulse)
        json_row.addWidget(self.upload_json_btn)
        json_row.addWidget(self.download_json_btn)
        layout.addLayout(json_row)

        layout.addSpacing(4)
        layout.addWidget(QLabel("Recent:"))
        self.recent_list = QListWidget(self)
        self.recent_list.setStyleSheet("QListWidget { background: #181818; border: 1px solid #2a2a2a; }")
        self.recent_list.itemDoubleClicked.connect(self._on_recent_dbl)
        layout.addWidget(self.recent_list, stretch=1)

    def _on_upload_clicked(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Pick image(s) for FD6", "", f"Images ({exts});;All files (*)"
        )
        if paths:
            self._emit([Path(p) for p in paths])

    def mark_json_ready(self, json_path: Path | None = None) -> None:
        """Called by MainWindow when a generation finishes. Enables the Download JSON button
        and briefly pulses it green so the user notices it's now actionable.
        """
        self.download_json_btn.setEnabled(True)
        tip = "Save the most-recent generated shapes JSON to a location of your choice"
        if json_path:
            tip = f"Save '{json_path.name}' to a location of your choice"
        self.download_json_btn.setToolTip(tip)
        # Pulse to draw attention
        self.download_json_btn.setStyleSheet(
            "QPushButton { background: #1f6f3a; color: white; font-weight: bold; "
            "border: 2px solid #2ecc71; border-radius: 4px; padding: 6px 10px; }"
            "QPushButton:hover { background: #258245; }"
        )
        self._pulse_timer.start(3000)  # revert styling after 3 sec

    def _end_download_pulse(self) -> None:
        self.download_json_btn.setStyleSheet(self._download_default_style)

    def _on_upload_json_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick FD6 shapes JSON to load for preview", "", "FD6 shapes (*.json);;All files (*)"
        )
        if path:
            self.json_loaded.emit(Path(path))

    def _on_files_dropped(self, paths: list[Path]) -> None:
        self._emit(paths)

    def _on_recent_dbl(self, item) -> None:
        p = Path(item.data(Qt.UserRole))
        if p.exists():
            self._emit([p])

    def _emit(self, paths: list[Path]) -> None:
        for p in paths:
            if p not in self._recent:
                self._recent.insert(0, p)
                self.recent_list.insertItem(0, p.name)
                self.recent_list.item(0).setData(Qt.UserRole, str(p))
                while self.recent_list.count() > 12:
                    self.recent_list.takeItem(12)
                self._recent = self._recent[:12]
        self.files_selected.emit(paths)
