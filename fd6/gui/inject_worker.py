"""Background worker that runs FH6 injection on a QThread and emits progress + colored status signals.

Severity codes for the `status` signal:
  "info"    — neutral (use default text color)
  "success" — green (operation completed OK)
  "warning" — yellow (completed but with caveats)
  "error"   — red (operation failed)
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal


class InjectionWorker(QObject):
    scan_progress = Signal(int, int, int)   # scanned_regions, total_regions, hits_so_far
    write_progress = Signal(int, int)       # written_shapes, total_shapes
    status = Signal(str, str)               # message, severity ("info"|"success"|"warning"|"error")
    done = Signal()

    def __init__(self, json_path: Path) -> None:
        super().__init__()
        self.json_path = Path(json_path)

    def run(self) -> None:
        from fd6.inject import FH6Injector, patterns_are_populated
        from fd6.io.exporter import load_json

        if not patterns_are_populated():
            self.status.emit("Patterns file not populated. Re-derive via discovery workflow.", "error")
            self.done.emit()
            return

        try:
            doc = load_json(str(self.json_path))
            shapes = doc.materialize_shapes()
        except Exception as exc:
            self.status.emit(f"Could not load JSON: {type(exc).__name__}: {exc}", "error")
            self.done.emit()
            return

        n_shapes = len(shapes)
        self.status.emit(f"Loaded {n_shapes} shapes from {self.json_path.name}.", "info")

        inj = FH6Injector()
        try:
            self.status.emit("Attaching to FH6...", "info")
            inj.attach()
            self.status.emit(
                f"Attached. Scanning FH6 memory for LiveryGroup with {n_shapes} layers "
                f"(this can take several minutes the first time)...", "info",
            )
            # Pass n_shapes as preferred layer_count so we try the matching template first.
            handle = inj.find_active_vinyl_group(progress_cb=self._on_scan_progress, layer_count=n_shapes)
            slots = handle.layer_count
            if n_shapes > slots:
                self.status.emit(
                    f"Template has {slots} shape slots but JSON has {n_shapes}. "
                    f"Load a larger template (e.g., {n_shapes}-sphere vinyl group) and re-inject.",
                    "warning",
                )
                self.done.emit()
                return
            self.status.emit(f"Found {slots} shape slots. Writing {n_shapes} shapes...", "info")
            # Pass image_size so the injector can center coords + invert Y
            img_w, img_h = doc.image_size if doc.image_size else (0, 0)
            image_size = (img_w, img_h) if img_w > 0 and img_h > 0 else None
            result = inj.inject(
                shapes, handle, progress_cb=self._on_write_progress,
                image_size=image_size, coord_scale=1.0,
            )
            if result.success:
                self.status.emit(
                    f"Injected {result.shapes_written} shapes successfully. {result.message}",
                    "success",
                )
            else:
                self.status.emit(f"Injection failed: {result.message}", "error")
        except Exception as exc:
            self.status.emit(f"Injection error: {type(exc).__name__}: {exc}", "error")
        finally:
            try:
                inj.detach()
            except Exception:
                pass
            self.done.emit()

    def _on_scan_progress(self, scanned: int, total: int, hits: int) -> None:
        self.scan_progress.emit(scanned, total, hits)

    def _on_write_progress(self, written: int, total: int) -> None:
        self.write_progress.emit(written, total)
