from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget
)


@dataclass
class QueueItem:
    path: Path
    status: str = "queued"  # queued | running | done | error


class QueuePanel(QWidget):
    cleared = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        header = QHBoxLayout()
        header.addWidget(QLabel("Queue:"))
        header.addStretch()
        self.clear_btn = QPushButton("Clear done")
        self.clear_btn.clicked.connect(self._clear_done)
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        self.list = QListWidget(self)
        self.list.setStyleSheet("QListWidget { background: #181818; border: 1px solid #2a2a2a; }")
        layout.addWidget(self.list, stretch=1)

        self._items: list[QueueItem] = []

    def add(self, path: Path) -> None:
        item = QueueItem(path=path)
        self._items.append(item)
        li = QListWidgetItem(f"⏳ {path.name}")
        li.setData(Qt.UserRole, str(path))
        self.list.addItem(li)

    def set_status(self, path: Path, status: str) -> None:
        for idx, it in enumerate(self._items):
            if it.path == path:
                it.status = status
                icon = {"queued": "⏳", "running": "▶", "done": "✓", "error": "✗"}.get(status, "?")
                self.list.item(idx).setText(f"{icon} {path.name}")
                return

    def pop_next_queued(self) -> Path | None:
        for it in self._items:
            if it.status == "queued":
                return it.path
        return None

    def _clear_done(self) -> None:
        i = 0
        while i < len(self._items):
            if self._items[i].status in ("done", "error"):
                self.list.takeItem(i)
                self._items.pop(i)
            else:
                i += 1
        self.cleared.emit()
