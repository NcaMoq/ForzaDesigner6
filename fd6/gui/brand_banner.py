"""Floating brand banner that lives in the bottom-left of the MainWindow.

Default: expanded panel showing the FD6 logo + "Forza Designer 6" title.
Click anywhere on the panel to collapse it. When collapsed, a small icon-only
button remains in the same corner; click it to re-expand.

Both states stay anchored to the bottom-left corner across window resizes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QPixmap, QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
)


def _bundle_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent.parent  # FD6/


def badge_path(filename: str) -> Path | None:
    """Return absolute path to a badge PNG, or None if missing. Bundle-aware."""
    root = _bundle_root()
    p = root / filename
    if p.exists():
        return p
    # Legacy fallbacks
    for cand in (root / "tools" / "fd6_128.png", root / "Logo.png"):
        if cand.exists():
            return cand
    return None


def _logo_path() -> Path | None:
    """Default badge — pink for the Default theme."""
    return badge_path("Pink.png") or badge_path("AppIconTransparent.png")


class BrandBanner(QWidget):
    """Brand banner that sits in the bottom-left corner. Click panel to collapse / click pill to expand."""

    MARGIN = 12
    BANNER_HEIGHT = 56
    BANNER_WIDTH = 240
    PILL_SIZE = 40

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)

        logo = _logo_path()
        self._pix: QPixmap | None = None
        if logo:
            pm = QPixmap(str(logo))
            if not pm.isNull():
                self._pix = pm

        # ---- expanded panel
        self.panel = QFrame(self)
        self.panel.setObjectName("brandPanel")
        self.panel.setStyleSheet(
            "#brandPanel { background: rgba(20, 20, 24, 230); border: 1px solid #333; border-radius: 8px; }"
            "#brandPanel:hover { background: rgba(30, 30, 36, 240); }"
        )
        self.panel.setCursor(Qt.PointingHandCursor)
        self.panel.setFixedSize(self.BANNER_WIDTH, self.BANNER_HEIGHT)

        ph = QHBoxLayout(self.panel)
        ph.setContentsMargins(10, 8, 10, 8)
        ph.setSpacing(10)
        self.icon_label = QLabel(self.panel)
        self.icon_label.setFixedSize(40, 40)
        self.icon_label.setAlignment(Qt.AlignCenter)
        if self._pix:
            self.icon_label.setPixmap(self._pix.scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        ph.addWidget(self.icon_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)
        self.title_label = QLabel("Forza Designer 6", self.panel)
        tf = QFont(); tf.setBold(True); tf.setPointSize(10)
        self.title_label.setFont(tf)
        self.title_label.setStyleSheet("color: #f0f0f0;")
        self.sub_label = QLabel("Click to hide", self.panel)
        self.sub_label.setStyleSheet("color: #888; font-size: 10px;")
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.sub_label)
        ph.addLayout(text_col, stretch=1)

        # ---- collapsed pill (icon-only button)
        self.pill = QPushButton(self)
        self.pill.setFixedSize(self.PILL_SIZE, self.PILL_SIZE)
        self.pill.setCursor(Qt.PointingHandCursor)
        self.pill.setToolTip("Show FD6 banner")
        self.pill.setStyleSheet(
            "QPushButton { background: rgba(20, 20, 24, 230); border: 1px solid #333; border-radius: 20px; }"
            "QPushButton:hover { background: rgba(30, 30, 36, 240); border-color: #555; }"
        )
        if self._pix:
            self.pill.setIcon(QIcon(self._pix.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            self.pill.setIconSize(QSize(28, 28))
        self.pill.clicked.connect(self.show_panel)

        # Make panel clickable to collapse
        self.panel.mousePressEvent = self._panel_clicked  # type: ignore

        # Start expanded
        self.pill.hide()
        self.panel.show()

        # Size of THIS widget covers the larger of the two states
        self.setFixedSize(self.BANNER_WIDTH, self.BANNER_HEIGHT)
        self.reposition()

    def set_badge(self, png_path: Path | str | None) -> None:
        """Swap the displayed badge — used when theme changes."""
        if not png_path:
            return
        pm = QPixmap(str(png_path))
        if pm.isNull():
            return
        self._pix = pm
        self.icon_label.setPixmap(pm.scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.pill.setIcon(QIcon(pm.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)))

    def _panel_clicked(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.hide_panel()

    def hide_panel(self) -> None:
        self.panel.hide()
        self.setFixedSize(self.PILL_SIZE, self.PILL_SIZE)
        self.pill.show()
        self.pill.move(0, 0)
        self.reposition()

    def show_panel(self) -> None:
        self.pill.hide()
        self.setFixedSize(self.BANNER_WIDTH, self.BANNER_HEIGHT)
        self.panel.show()
        self.panel.move(0, 0)
        self.reposition()

    def reposition(self) -> None:
        """Anchor to bottom-left corner of parent widget with MARGIN."""
        parent = self.parentWidget()
        if parent is None:
            return
        x = self.MARGIN
        y = parent.height() - self.height() - self.MARGIN
        self.move(x, max(0, y))
        self.raise_()
