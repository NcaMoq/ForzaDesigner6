from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout, QWidget
)

from fd6.shapegen.profile import Profile, load_profile_from_file, list_bundled_profiles


SHAPE_TYPE_CHOICES = [
    ("rotated_ellipse", "Rotated Ellipse (default)"),
    ("ellipse", "Ellipse"),
    ("circle", "Circle"),
    ("triangle", "Triangle"),
    ("rectangle", "Rectangle"),
    ("rotated_rectangle", "Rotated Rectangle"),
]


class SettingsPanel(QWidget):
    """Profile picker + advanced knobs. Emits profile_changed when the user edits anything."""

    profile_changed = Signal(object)  # Profile
    start_clicked = Signal()
    pause_clicked = Signal()
    stop_clicked = Signal()
    inject_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Profile picker
        prof_row = QHBoxLayout()
        prof_row.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox(self)
        self._populate_profiles()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        prof_row.addWidget(self.profile_combo, stretch=1)
        layout.addLayout(prof_row)

        # Advanced group
        adv = QGroupBox("Advanced", self)
        form = QFormLayout(adv)
        self.stop_at = QSpinBox(); self.stop_at.setRange(10, 50000); self.stop_at.setValue(3000)
        self.random_samples = QSpinBox(); self.random_samples.setRange(10, 50000); self.random_samples.setValue(1000)
        self.mutated_samples = QSpinBox(); self.mutated_samples.setRange(1, 5000); self.mutated_samples.setValue(200)
        self.max_resolution = QSpinBox(); self.max_resolution.setRange(100, 4096); self.max_resolution.setValue(1200)
        self.max_threads = QSpinBox(); self.max_threads.setRange(0, 64); self.max_threads.setValue(0)
        self.preview_every = QSpinBox(); self.preview_every.setRange(1, 100); self.preview_every.setValue(1)
        form.addRow("Stop at shapes", self.stop_at)
        form.addRow("Random samples", self.random_samples)
        form.addRow("Mutated samples", self.mutated_samples)
        form.addRow("Max resolution (px)", self.max_resolution)
        form.addRow("Threads (0=auto)", self.max_threads)
        form.addRow("Preview every N", self.preview_every)
        for w in (self.stop_at, self.random_samples, self.mutated_samples, self.max_resolution, self.max_threads, self.preview_every):
            w.valueChanged.connect(self._on_adv_changed)
        layout.addWidget(adv)

        # Sticker mode toggle
        sticker_group = QGroupBox("Image options", self)
        sg_layout = QVBoxLayout(sticker_group)
        self.sticker_mode_cb = QCheckBox("Add white background to transparent images", sticker_group)
        self.sticker_mode_cb.setChecked(True)  # ON = current default behavior (composite onto white)
        self.sticker_mode_cb.setToolTip(
            "ON  (default): transparent PNG areas are flattened to white before generation. "
            "Recommended for normal images.\n"
            "OFF (sticker mode): transparent areas stay transparent — shapes are only placed "
            "in opaque regions. Use for stickers / logos where you want a transparent backdrop."
        )
        sg_layout.addWidget(self.sticker_mode_cb)
        layout.addWidget(sticker_group)

        # Shape types
        types_group = QGroupBox("Shape types", self)
        tg_layout = QVBoxLayout(types_group)
        self._shape_checks: dict[str, QCheckBox] = {}
        for code, label in SHAPE_TYPE_CHOICES:
            cb = QCheckBox(label, types_group)
            cb.setChecked(code == "rotated_ellipse")
            cb.stateChanged.connect(self._on_adv_changed)
            tg_layout.addWidget(cb)
            self._shape_checks[code] = cb
        layout.addWidget(types_group)

        # Action buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start"); self.start_btn.setMinimumHeight(36)
        self.pause_btn = QPushButton("Pause"); self.pause_btn.setCheckable(True); self.pause_btn.setEnabled(False)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_clicked.emit)
        self.pause_btn.clicked.connect(self.pause_clicked.emit)
        self.stop_btn.clicked.connect(self.stop_clicked.emit)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        # Inject button (Phase 2)
        self.inject_btn = QPushButton("Inject into FH6")
        self.inject_btn.setEnabled(False)
        self.inject_btn.setToolTip(
            "Phase 2 feature. Requires FH6 memory patterns to be discovered and saved "
            "to fd6/inject/patterns/fh6_patterns.json. See README §Phase 2."
        )
        self.inject_btn.clicked.connect(self.inject_clicked.emit)
        layout.addWidget(self.inject_btn)

        layout.addStretch()

        # Apply initial profile
        self._on_profile_changed(self.profile_combo.currentIndex())

    def _populate_profiles(self) -> None:
        self.profile_combo.clear()
        for path in list_bundled_profiles():
            self.profile_combo.addItem(path.stem, str(path))
        if self.profile_combo.count() == 0:
            self.profile_combo.addItem("default", "")

    def _on_profile_changed(self, idx: int) -> None:
        path = self.profile_combo.itemData(idx)
        if not path:
            return
        try:
            prof = load_profile_from_file(path)
        except Exception:
            return
        # Mirror into advanced widgets without re-emitting per-spinbox.
        for w in (self.stop_at, self.random_samples, self.mutated_samples, self.max_resolution, self.max_threads, self.preview_every):
            w.blockSignals(True)
        self.stop_at.setValue(prof.stop_at)
        self.random_samples.setValue(prof.random_samples)
        self.mutated_samples.setValue(prof.mutated_samples)
        self.max_resolution.setValue(prof.max_resolution)
        self.max_threads.setValue(prof.max_threads)
        self.preview_every.setValue(prof.preview_every)
        for w in (self.stop_at, self.random_samples, self.mutated_samples, self.max_resolution, self.max_threads, self.preview_every):
            w.blockSignals(False)
        for code, cb in self._shape_checks.items():
            cb.blockSignals(True)
            cb.setChecked(code in prof.shape_types)
            cb.blockSignals(False)
        self.profile_changed.emit(self.build_profile())

    def _on_adv_changed(self, *_args) -> None:
        self.profile_changed.emit(self.build_profile())

    def build_profile(self) -> Profile:
        idx = self.profile_combo.currentIndex()
        path = self.profile_combo.itemData(idx) or ""
        base = Profile(name=self.profile_combo.itemText(idx) or "custom")
        if path:
            try:
                base = load_profile_from_file(path)
            except Exception:
                pass
        base.stop_at = self.stop_at.value()
        base.random_samples = self.random_samples.value()
        base.mutated_samples = self.mutated_samples.value()
        base.max_resolution = self.max_resolution.value()
        base.max_threads = self.max_threads.value()
        base.preview_every = self.preview_every.value()
        base.shape_types = [code for code, cb in self._shape_checks.items() if cb.isChecked()] or ["rotated_ellipse"]
        return base

    def set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
