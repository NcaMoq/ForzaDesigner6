from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy


class ImageView(QLabel):
    """QLabel that scales its pixmap on resize while preserving aspect ratio."""

    def __init__(self, placeholder: str = "—", parent=None) -> None:
        super().__init__(placeholder, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("QLabel { background: #181818; color: #555; border: 1px solid #2a2a2a; }")
        self._pix: QPixmap | None = None

    def set_numpy(self, arr: np.ndarray) -> None:
        if arr.ndim != 3 or arr.shape[2] != 3:
            return
        h, w, _ = arr.shape
        # QImage expects bytes contiguous; force a copy when not already.
        if not arr.flags["C_CONTIGUOUS"]:
            arr = np.ascontiguousarray(arr)
        img = QImage(arr.data, w, h, w * 3, QImage.Format_RGB888).copy()
        self._pix = QPixmap.fromImage(img)
        self._rescale()

    def set_path(self, path: str) -> None:
        pm = QPixmap(path)
        if not pm.isNull():
            self._pix = pm
            self._rescale()

    def clear_image(self) -> None:
        self._pix = None
        self.setText("—")

    def _rescale(self) -> None:
        if self._pix is None:
            return
        scaled = self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setPixmap(scaled)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()
