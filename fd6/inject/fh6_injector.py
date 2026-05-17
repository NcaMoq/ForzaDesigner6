"""FH6 memory injector — proper LiveryGroup + layer_table implementation.

CREDITS: discovery approach inspired by bvzrays/forza-painter-fh6 (MIT).
Adapted for FD6's pipeline.

Algorithm:
  1. Scan writable private heap for u16 == layer_count (the count field of
     LiveryGroup at offset COUNT_OFF).
  2. For each candidate: treat it as count_address. Compute group_address =
     count_address - COUNT_OFF. Read layer_table_address from group_address +
     TABLE_OFF.
  3. Validate the table contains valid layer pointers (each with sane position,
     scale, color, shape_id, mask at known offsets).
  4. Best candidate -> layer table -> each layer pointer is a heap address with
     fields at fixed offsets.
  5. To inject: for each layer, write position (X, -Y) at +POS_OFF, scale
     (w/63, h/63) at +SCALE_OFF, rotation (360-deg) at +ROT_OFF, color (RGBA)
     at +COLOR_OFF, shape_id at +SHAPE_ID_OFF, mask at +MASK_OFF.

  No UI commit step required — writes to the actual Layer struct propagate to
  render instantly. This solves the lazy-allocation problem that hit our prior
  POSITION-signature-based approach.
"""

from __future__ import annotations

import ctypes
import json
import struct
from ctypes import wintypes
from pathlib import Path

from fd6.inject import Injector, VinylGroupHandle, InjectResult
from fd6.inject.patterns_io import DEFAULT_PATTERNS_PATH, load_patterns
from fd6.inject.win_process import ProcessHandle, find_process_id


PATTERNS_FILE = DEFAULT_PATTERNS_PATH

# Target FH6 build this injector's offsets are confirmed against. If the game
# patches and breaks injection, this needs re-derivation. Surfaced in the GUI
# (window title + About dialog) so users know which build the EXE matches.
FH6_TARGET_BUILD = "354.221"


# LiveryGroup struct offsets (CONFIRMED working for current FH6 build; may shift on patches)
COUNT_OFF = 0x5A   # u16 layer count
TABLE_OFF = 0x78   # u64 pointer to layer table (array of u64 layer pointers, 8-byte stride)

# Layer struct offsets (within each Layer instance)
LAYER_POS_OFF = 0x18      # 2 x f32: x, y
LAYER_SCALE_OFF = 0x28    # 2 x f32: scale_x, scale_y
LAYER_ROT_OFF = 0x50      # f32: rotation degrees
LAYER_COLOR_OFF = 0x74    # 4 bytes: R, G, B, alpha (alpha must be 0 or 255)
LAYER_MASK_OFF = 0x78     # u8: mask flag (0 or 1)
LAYER_SHAPE_ID_OFF = 0x7A # u8: shape type id (102 = ellipse, 101 = other)

# Scale divisors (per bvzrays)
SCALE_DIVISOR_ELLIPSE = 63.0
SCALE_DIVISOR_OTHER = 127.0
SHAPE_ID_ELLIPSE = 102
SHAPE_ID_OTHER = 101


def patterns_are_populated() -> bool:
    """Always True now — we no longer rely on a static patterns file for color storage.
    LiveryGroup + layer_table approach finds shapes dynamically."""
    return True


def _get_module_base(pid: int, module_name: str) -> int | None:
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    LIST_MODULES_ALL = 0x03

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ]
    psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    psapi.GetModuleBaseNameW.restype = wintypes.DWORD

    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        modules = (ctypes.c_void_p * 1024)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(h, modules, ctypes.sizeof(modules), ctypes.byref(needed), LIST_MODULES_ALL):
            return None
        count = needed.value // ctypes.sizeof(ctypes.c_void_p)
        target = module_name.lower()
        for i in range(count):
            mod = modules[i]
            if mod is None:
                continue
            buf = ctypes.create_unicode_buffer(260)
            n = psapi.GetModuleBaseNameW(h, mod, buf, 260)
            if n and buf.value.lower() == target:
                return int(mod)
        return None
    finally:
        k32.CloseHandle(h)


def _is_user_ptr(val: int) -> bool:
    return 0x000001000000 < val < 0x800000000000


def _read_u64(proc: ProcessHandle, addr: int) -> int:
    b = proc.try_read(addr, 8)
    return struct.unpack('<Q', b)[0] if b and len(b) == 8 else 0


def _read_2f(proc: ProcessHandle, addr: int) -> tuple[float, float] | None:
    b = proc.try_read(addr, 8)
    return struct.unpack('<2f', b) if b and len(b) == 8 else None


def _score_layer(proc: ProcessHandle, lptr: int) -> int:
    """Score a layer pointer by reading its fields (0-5). Stricter ranges than before.

    Returns the count of plausibility checks that passed. We use the *strict*
    criteria here — a sphere-template layer that hasn't been modified has very
    tight values (position within image canvas, scale ~32-64 / 63, rotation 0,
    color RGBA with alpha 255 or 0, shape_id == 102 for ellipse, mask == 0).
    """
    if not _is_user_ptr(lptr):
        return 0
    score = 0
    # Position: must be finite floats, plausible canvas range
    pos = _read_2f(proc, lptr + LAYER_POS_OFF)
    if pos and all(_is_finite_float(v) and -8192.0 <= v <= 8192.0 for v in pos):
        score += 1
    # Scale: must be finite floats, strictly positive, plausible range
    scale = _read_2f(proc, lptr + LAYER_SCALE_OFF)
    if scale and all(_is_finite_float(v) and 0.0 < abs(v) <= 64.0 for v in scale):
        score += 1
    # Color: just must be readable (any 4 bytes — even all-zero is valid for unset)
    color = proc.try_read(lptr + LAYER_COLOR_OFF, 4)
    if color and len(color) == 4:
        score += 1
    # Shape ID: must be a known FH6 shape id
    shape = proc.try_read(lptr + LAYER_SHAPE_ID_OFF, 1)
    if shape and shape[0] in (101, 102):
        score += 1
    # Mask: must be 0 or 1
    mask = proc.try_read(lptr + LAYER_MASK_OFF, 1)
    if mask and mask[0] in (0, 1):
        score += 1
    return score


def _is_finite_float(v: float) -> bool:
    import math
    return math.isfinite(v)


def locate_livery_group(
    proc: ProcessHandle, layer_count: int,
    progress_cb=None, max_candidates: int = 200000,
) -> tuple[int, int] | None:
    """Find LiveryGroup + layer table by scanning heap for u16 == layer_count.

    STRICT MODE (revised after a misidentified candidate caused FH6 to crash mid-write):
      - Each candidate is rejected unless ALL 16 sampled layer pointers score 5/5.
      - If no perfect candidate is found, refuse to return any (returns None).
      - Caller is expected to bail out cleanly rather than write to a wrong table.

    This is much safer: writing to a wrong heap object corrupts game state. A
    sphere-template layer table has uniform, valid fields across all entries,
    so a 16/16 perfect score is the right bar.
    """
    pattern = struct.pack('<H', layer_count)
    regions = [r for r in proc.enumerate_regions() if r.readable and r.writable and not r.is_image]
    regions.sort(key=lambda r: r.size, reverse=True)
    total = len(regions)
    candidates = 0
    perfect: list[tuple[int, int]] = []  # (group_addr, table_addr) all 16/16
    for i, r in enumerate(regions):
        data = proc.try_read(r.base, r.size)
        if data is None:
            if progress_cb: progress_cb(i + 1, total, candidates)
            continue
        start = 0
        while True:
            pos = data.find(pattern, start)
            if pos < 0:
                break
            start = pos + 1
            candidates += 1
            if candidates > max_candidates:
                if progress_cb: progress_cb(i + 1, total, candidates)
                return _pick_best_perfect(proc, perfect, layer_count)
            count_addr = r.base + pos
            group_addr = count_addr - COUNT_OFF
            if group_addr < r.base:
                continue
            table_addr = _read_u64(proc, group_addr + TABLE_OFF)
            if not _is_user_ptr(table_addr):
                continue
            # STRICT: require ALL 16 sampled layers to score 5/5.
            ok = True
            sample_n = min(layer_count, 16)
            for k in range(sample_n):
                lptr = _read_u64(proc, table_addr + k * 8)
                if _score_layer(proc, lptr) < 5:
                    ok = False
                    break
            if ok:
                perfect.append((group_addr, table_addr))
        if progress_cb: progress_cb(i + 1, total, len(perfect))
    return _pick_best_perfect(proc, perfect, layer_count)


def _pick_best_perfect(
    proc: ProcessHandle, perfect: list[tuple[int, int]], layer_count: int,
) -> tuple[int, int] | None:
    """Among perfect candidates, pick the one whose *full* table validates best.

    Reads ALL layer pointers (not just first 16) and counts how many score 5/5.
    The real LiveryGroup will have all (or nearly all) of its layers fully valid;
    accidental matches that happened to have valid first-16 will fall off here.
    """
    if not perfect:
        return None
    if len(perfect) == 1:
        # Single candidate — still validate the full table before accepting.
        group_addr, table_addr = perfect[0]
        valid_full = _count_valid_layers(proc, table_addr, layer_count)
        if valid_full >= layer_count * 0.95:  # >= 95% must be valid
            return (group_addr, table_addr)
        return None
    # Multiple — rank by full-table validation
    scored: list[tuple[int, int, int]] = []
    for group_addr, table_addr in perfect:
        valid_full = _count_valid_layers(proc, table_addr, layer_count)
        scored.append((valid_full, group_addr, table_addr))
    scored.sort(reverse=True)
    best_valid, group_addr, table_addr = scored[0]
    if best_valid >= layer_count * 0.95:
        return (group_addr, table_addr)
    return None


def _count_valid_layers(proc: ProcessHandle, table_addr: int, layer_count: int) -> int:
    """Walk the entire layer_table and count how many pointers resolve to 5/5 layers."""
    valid = 0
    for k in range(layer_count):
        lptr = _read_u64(proc, table_addr + k * 8)
        if _score_layer(proc, lptr) >= 5:
            valid += 1
    return valid


def _pack_color(shape_dict: dict) -> bytes:
    """Convert FD6 shape's color to RGBA 4 bytes with alpha forced to 255."""
    color = shape_dict.get("color")
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        return bytes([255, 255, 255, 255])
    r = int(color[0]) & 0xFF
    g = int(color[1]) & 0xFF
    b = int(color[2]) & 0xFF
    return bytes([r, g, b, 255])  # alpha must be 0 or 255; default to 255


class FH6Injector(Injector):
    """Forza Horizon 6 injector — LiveryGroup + layer_table strategy."""

    game_label = "Forza Horizon 6"

    def __init__(self, pid: int | None = None, patterns_path: Path | str = PATTERNS_FILE) -> None:
        self.pid = pid
        self.patterns_path = Path(patterns_path)
        self._proc: ProcessHandle | None = None
        self._group_addr: int | None = None
        self._table_addr: int | None = None
        self._layer_count: int | None = None

    def attach(self) -> None:
        if self.pid is None:
            self.pid = find_process_id("forzahorizon6.exe")
            if self.pid is None:
                raise RuntimeError("forzahorizon6.exe is not running")
        self._proc = ProcessHandle(self.pid)
        self._proc.open()

    def detach(self) -> None:
        if self._proc:
            self._proc.close()
            self._proc = None

    def find_active_vinyl_group(self, progress_cb=None, layer_count: int | None = None,
                                color_progress_cb=None) -> VinylGroupHandle:
        """Find LiveryGroup by scanning for layer_count u16, validating, picking best."""
        if not self._proc:
            raise RuntimeError("Injector not attached. Call attach() first.")
        # Try the requested count first (exact match), then larger common templates
        # that could also host the JSON (a 1500-template can hold a 500-shape JSON).
        common = [500, 1500, 3000, 1000, 100, 50, 20, 10]
        if layer_count is not None:
            tries = [layer_count] + [c for c in common if c > layer_count]
        else:
            tries = common
        for count_try in tries:
            if count_try is None:
                continue
            result = locate_livery_group(self._proc, count_try, progress_cb=progress_cb)
            if result is not None:
                self._group_addr, self._table_addr = result
                self._layer_count = count_try
                # Read all layer pointers
                addrs = []
                for i in range(count_try):
                    lptr = _read_u64(self._proc, self._table_addr + i * 8)
                    addrs.append(lptr)
                return VinylGroupHandle(
                    base_addr=self._group_addr,
                    layer_count=count_try,
                    shape_array_addr=self._table_addr,
                    shape_stride=8,  # pointer stride in layer table
                    meta={
                        "group_addr": self._group_addr,
                        "table_addr": self._table_addr,
                        "layer_addrs": addrs,
                    },
                )
        raise RuntimeError(
            "No confident LiveryGroup match (strict 16/16 + 95% full-table validation). "
            "This is intentional — refusing to write to a low-confidence candidate would "
            "corrupt FH6 state. Make sure the vinyl editor is open with a fresh, unmodified "
            "template (500/1500/3000 spheres). If you've already edited the template's "
            "shapes/colors, reload it fresh and re-inject."
        )

    def inject(self, shapes: list, group: VinylGroupHandle, progress_cb=None,
               image_size: tuple[int, int] | None = None, coord_scale: float = 1.0) -> InjectResult:
        if not self._proc:
            raise RuntimeError("Injector not attached.")
        layer_addrs: list[int] = (group.meta or {}).get("layer_addrs") or []
        if not layer_addrs:
            return InjectResult(success=False, message="No layer addresses cached. Call find_active_vinyl_group first.")

        # Normalize shapes to dicts
        shape_dicts: list[dict] = []
        for s in shapes:
            if hasattr(s, "to_json"):
                shape_dicts.append(s.to_json())
            elif isinstance(s, dict):
                shape_dicts.append(s)
            else:
                raise TypeError(f"Unsupported shape type: {type(s)!r}")
        n = len(shape_dicts)
        if n > len(layer_addrs):
            return InjectResult(
                success=False, shapes_written=0,
                message=(f"Template has {len(layer_addrs)} layer slots, but JSON has {n} shapes. "
                         f"Load a larger template vinyl group."),
            )

        written = 0
        bytes_total = 0
        skipped = 0
        for i, sd in enumerate(shape_dicts):
            lptr = layer_addrs[i]
            # SAFETY: revalidate every pointer right before writing. If a layer
            # ever fails the 5/5 check (e.g., game freed/moved it, or scan picked
            # a near-miss), skip rather than writing through junk and crashing FH6.
            if not _is_user_ptr(lptr) or _score_layer(self._proc, lptr) < 5:
                skipped += 1
                if progress_cb:
                    progress_cb(written, n)
                continue
            shape_type = sd.get("type", "rotated_ellipse")
            is_ellipse = "ellipse" in shape_type or shape_type == "circle"
            scale_div = SCALE_DIVISOR_ELLIPSE if is_ellipse else SCALE_DIVISOR_OTHER

            try:
                # Position: X, -Y (Y negated per bvzrays)
                x = float(sd.get("x", 0.0))
                y = float(sd.get("y", 0.0))
                self._proc.write(lptr + LAYER_POS_OFF, struct.pack('<2f', x, -y))
                bytes_total += 8

                # Scale: w/divisor, h/divisor  (rx/ry for ellipse, r for circle)
                if "rx" in sd:
                    sx = float(sd["rx"]) / scale_div
                    sy = float(sd.get("ry", sd["rx"])) / scale_div
                elif "r" in sd:
                    sx = sy = float(sd["r"]) / scale_div
                else:
                    sx = sy = 1.0
                self._proc.write(lptr + LAYER_SCALE_OFF, struct.pack('<2f', sx, sy))
                bytes_total += 8

                # Rotation: 360 - degrees (bvzrays convention)
                angle = float(sd.get("angle", 0.0)) % 360.0
                self._proc.write(lptr + LAYER_ROT_OFF, struct.pack('<f', (360.0 - angle) % 360.0))
                bytes_total += 4

                # Color: RGBA bytes with alpha forced to 255
                self._proc.write(lptr + LAYER_COLOR_OFF, _pack_color(sd))
                bytes_total += 4

                # Shape ID: 102 for ellipse, 101 for other
                self._proc.write(lptr + LAYER_SHAPE_ID_OFF,
                                 bytes([SHAPE_ID_ELLIPSE if is_ellipse else SHAPE_ID_OTHER]))
                bytes_total += 1

                # Mask: 0
                self._proc.write(lptr + LAYER_MASK_OFF, bytes([0]))
                bytes_total += 1

                written += 1
            except OSError:
                # WriteProcessMemory failure for this one layer — skip and continue.
                skipped += 1

            if progress_cb:
                progress_cb(written, n)

        msg = (f"Wrote {written}/{n} shapes ({bytes_total} bytes) via LiveryGroup layer table.")
        if skipped:
            msg += f" Skipped {skipped} unsafe layer(s) (failed revalidation)."
        return InjectResult(
            success=written > 0,
            shapes_written=written,
            message=msg,
        )
