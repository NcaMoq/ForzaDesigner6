from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import ctypes
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import shared_memory

import numpy as np

from fd6.shapegen.profile import Profile
from fd6.shapegen.scoring import (
    composite,
    compute_edge_weight,
    precompute_canvas_error,
    rms_error,
    score_shape,
)
from fd6.shapegen.shapes import Shape, random_shape


def _available_ram_mb() -> int:
    """Best-effort free physical RAM in MB. Falls back to 4096 if detection fails."""
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        try:
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullAvailPhys // (1024 * 1024))
        except Exception:
            return 4096
    # Non-Windows fallback (best-effort): read /proc/meminfo
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 4096


def _safe_worker_count(user_requested: int, random_samples: int) -> int:
    """Pick a worker count that won't crash low-end machines and won't waste cycles on overhead.

    Caps simultaneously by:
      - CPU: leave 1 thread free on 4-core boxes, 2 on bigger to keep system responsive
      - RAM: each worker process needs ~250 MB; reserve 2 GB for main app + system
      - Workload: each worker needs >= 64 random samples or IPC overhead dominates
    """
    cpu = os.cpu_count() or 1
    headroom = 1 if cpu <= 4 else 2
    cpu_cap = max(1, cpu - headroom)

    free_mb = _available_ram_mb()
    # Reserve 2 GB for main app + system; budget 250 MB per worker process.
    ram_budget_mb = max(0, free_mb - 2048)
    ram_cap = max(1, ram_budget_mb // 250)

    # Workload-size cap: small per-iteration budgets don't amortize spawn/IPC cost.
    work_cap = max(1, random_samples // 64)

    # User explicit override (profile.max_threads > 0) is honored but still safety-capped.
    requested = user_requested if user_requested > 0 else cpu_cap
    return max(1, min(requested, cpu_cap, ram_cap, work_cap))


@dataclass
class EngineConfig:
    profile: Profile
    seed: int = 0  # 0 → time-based


@dataclass
class EngineEvent:
    """Event emitted at preview/save points. The worker translates these into Qt signals."""
    kind: str  # "shape_committed" | "checkpoint" | "preview" | "done" | "error"
    shape_count: int = 0
    rms: float = 0.0
    canvas: np.ndarray | None = None  # uint8 (H, W, 3); only set for preview/done
    message: str = ""


# ── Worker-side globals + functions ──────────────────────────────────────────
# These live at module top-level so they survive pickling across spawn().
# Each ProcessPoolExecutor worker calls _init_worker once at startup, then
# _worker_independent_search per task. The canvas lives in shared memory so
# the main process can mutate it in place between tasks without re-sending.

_W_TARGET: np.ndarray | None = None
_W_ALPHA: np.ndarray | None = None
_W_EDGE_WEIGHT: np.ndarray | None = None  # ndarray view onto _W_EDGE_SHM (LIVE — engine rewrites periodically)
_W_EDGE_SHM: shared_memory.SharedMemory | None = None
_W_CANVAS_SHM: shared_memory.SharedMemory | None = None
_W_CANVAS: np.ndarray | None = None


def _init_worker(
    target_bytes: bytes, target_shape: tuple,
    canvas_shm_name: str, canvas_shape: tuple,
    alpha_bytes: bytes | None, alpha_shape: tuple | None,
    edge_shm_name: str | None, edge_shape: tuple | None,
) -> None:
    """Subprocess startup hook. Wires up shared canvas + immutable target/alpha + LIVE edge weight."""
    global _W_TARGET, _W_ALPHA, _W_EDGE_WEIGHT, _W_EDGE_SHM, _W_CANVAS_SHM, _W_CANVAS
    _W_TARGET = np.frombuffer(target_bytes, dtype=np.uint8).reshape(target_shape).copy()
    if alpha_bytes is not None and alpha_shape is not None:
        _W_ALPHA = np.frombuffer(alpha_bytes, dtype=np.uint8).reshape(alpha_shape).copy()
    else:
        _W_ALPHA = None
    if edge_shm_name is not None and edge_shape is not None:
        # Attach to the LIVE edge-weight shared memory — workers see periodic
        # residual updates from the main process between iterations without
        # needing per-iteration IPC.
        _W_EDGE_SHM = shared_memory.SharedMemory(name=edge_shm_name)
        _W_EDGE_WEIGHT = np.ndarray(edge_shape, dtype=np.float32, buffer=_W_EDGE_SHM.buf)
    else:
        _W_EDGE_SHM = None
        _W_EDGE_WEIGHT = None
    _W_CANVAS_SHM = shared_memory.SharedMemory(name=canvas_shm_name)
    _W_CANVAS = np.ndarray(canvas_shape, dtype=np.uint8, buffer=_W_CANVAS_SHM.buf)


def _worker_independent_search(args: tuple) -> tuple:
    """One worker's independent (random search + hill-climb) sequence.

    Reads canvas directly from shared memory; no per-task copy. Returns the
    best (score, color, shape) it found. Main picks the global best.

    Speed path: precomputes the full-canvas squared-error scalar ONCE at the
    start of the batch and reuses it for all 1000+ candidate evaluations.
    Without this, every score_shape call recomputed a 4096×4096×3 sum from
    scratch, which dominated the per-shape cost at high max_resolution.
    Result is mathematically identical; just no longer recomputed N times.
    """
    (types, n_random, n_mutate, w, h, seed, max_size_frac) = args
    canvas = _W_CANVAS
    target = _W_TARGET
    alpha = _W_ALPHA
    edge_w = _W_EDGE_WEIGHT
    rng = random.Random(seed)

    # Precompute once for this batch — see precompute_canvas_error docstring.
    canvas_full_sq, canvas_norm = precompute_canvas_error(canvas, target, alpha, edge_w)

    # Random search
    best_score = float("inf")
    best_color = None
    best_shape = None
    for _ in range(max(1, n_random)):
        s = random_shape(rng, w, h, types, max_size_frac=max_size_frac)
        score, color = score_shape(s, canvas, target, alpha,
                                   canvas_full_sq=canvas_full_sq,
                                   canvas_norm=canvas_norm,
                                   edge_weight=edge_w)
        if score < best_score:
            best_score, best_color, best_shape = score, color, s
    if best_shape is None:
        return (float("inf"), None, None)

    # Hill climb on the local best
    best_shape.color = best_color
    no_improve = 0
    cap = max(1, n_mutate)
    for _ in range(cap):
        cand = best_shape.mutate(rng, w, h)
        score, color = score_shape(cand, canvas, target, alpha,
                                   canvas_full_sq=canvas_full_sq,
                                   canvas_norm=canvas_norm,
                                   edge_weight=edge_w)
        if score < best_score:
            best_score, best_color, best_shape = score, color, cand
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= max(20, cap // 4):
                break
    if best_color is not None:
        best_shape.color = best_color
    return (best_score, best_color, best_shape)


# ── Engine ───────────────────────────────────────────────────────────────────

class Engine:
    """Image → shapes generator. Stateless w.r.t. external I/O — callers handle JSON/preview emission.

    Parallelism: each iteration dispatches N independent (random+hill_climb)
    searches to a ProcessPoolExecutor (N = profile.max_threads or cpu_count).
    Workers read the current canvas from shared memory; main thread mutates
    the canvas in place after committing the global best shape. This sidesteps
    Python's GIL.
    """

    def __init__(self, target_rgb: np.ndarray, config: EngineConfig, alpha_mask: np.ndarray | None = None) -> None:
        if target_rgb.ndim != 3 or target_rgb.shape[2] != 3:
            raise ValueError("target_rgb must be HxWx3 RGB uint8")
        self.target = target_rgb.astype(np.uint8)
        self.config = config
        self.profile = config.profile
        self.h, self.w = self.target.shape[:2]
        self.alpha_mask = alpha_mask if alpha_mask is not None else None
        if self.alpha_mask is not None:
            mask3 = (self.alpha_mask > 0)[:, :, None]
            self.target = self.target * mask3.astype(np.uint8)
            initial_canvas = np.full((self.h, self.w, 3), 40, dtype=np.uint8)
        else:
            avg = self.target.reshape(-1, 3).mean(axis=0).astype(np.uint8)
            initial_canvas = np.tile(avg, (self.h, self.w, 1)).astype(np.uint8)

        # Allocate the shared canvas. Workers attach to this same buffer by name.
        self._canvas_shm: shared_memory.SharedMemory | None = shared_memory.SharedMemory(
            create=True, size=initial_canvas.nbytes,
        )
        self.canvas = np.ndarray(initial_canvas.shape, dtype=np.uint8, buffer=self._canvas_shm.buf)
        self.canvas[:] = initial_canvas

        self.shapes: list[Shape] = []

        # Edge-weighted importance map: built ONCE from the target so the
        # scoring functions can boost contribution from edges (eyes, mouths,
        # thin outlines). Folds the alpha mask in too so transparent-buffer
        # pixels stay 0. Stored as `_base_edge_weight` (immutable); the LIVE
        # `edge_weight` shared-memory buffer starts at the base and is
        # periodically reblended with the residual error map below so unfinished
        # regions get boosted late in generation.
        self._base_edge_weight: np.ndarray = compute_edge_weight(self.target, self.alpha_mask).astype(np.float32)
        self._edge_weight_shm: shared_memory.SharedMemory | None = shared_memory.SharedMemory(
            create=True, size=self._base_edge_weight.nbytes,
        )
        self.edge_weight = np.ndarray(
            self._base_edge_weight.shape, dtype=np.float32, buffer=self._edge_weight_shm.buf,
        )
        self.edge_weight[:] = self._base_edge_weight

        # self.rms is the user-facing "how close is the canvas to the target"
        # number that shows in the GUI progress bar. Compute it WITHOUT the
        # edge-weight so the displayed scale stays comparable to prior versions
        # of FD6. The edge weight is still active inside score_shape — that's
        # where it actually drives shape selection.
        self.rms = rms_error(self.canvas, self.target, self.alpha_mask)
        self.start_rms = self.rms
        self._stop = False
        self._pause = False
        seed = config.seed or int(time.time() * 1000) & 0xFFFFFFFF
        self.rng = random.Random(seed)

        self._n_workers = _safe_worker_count(
            user_requested=self.profile.max_threads,
            random_samples=self.profile.random_samples,
        )
        target_bytes = self.target.tobytes()
        alpha_bytes = self.alpha_mask.tobytes() if self.alpha_mask is not None else None
        alpha_shape = self.alpha_mask.shape if self.alpha_mask is not None else None
        self._executor = ProcessPoolExecutor(
            max_workers=self._n_workers,
            initializer=_init_worker,
            initargs=(
                target_bytes, self.target.shape,
                self._canvas_shm.name, self.canvas.shape,
                alpha_bytes, alpha_shape,
                self._edge_weight_shm.name, self.edge_weight.shape,
            ),
        )

    def request_stop(self) -> None:
        self._stop = True

    def set_pause(self, paused: bool) -> None:
        self._pause = paused

    def _preview_canvas(self) -> np.ndarray:
        """Return the canvas as RGB or RGBA for emit-to-preview events.

        In sticker mode we attach the alpha mask as the 4th channel so the
        preview pane renders transparent outside the silhouette — otherwise
        the user sees a solid grey rectangle around the painted shape area
        that doesn't match the (transparent) source PNG.
        """
        if self.alpha_mask is not None:
            return np.dstack([self.canvas, self.alpha_mask]).copy()
        return self.canvas.copy()

    def seed_shapes(self, shapes: list[Shape]) -> None:
        """Resume mode: replay shapes onto the canvas before generation starts."""
        for s in shapes:
            new_canvas, new_rms = composite(self.canvas, s, self.target, self.alpha_mask, self.edge_weight)
            self.canvas[:] = new_canvas  # write into shared memory
            self.rms = new_rms
            self.shapes.append(s)

    # Residual reblend disabled in v0.4.0 — the size-schedule + edge-weight
    # combination already moves enough budget into detail regions on its own;
    # leaving the residual on top biased the back-half of generation toward
    # smearing big shapes over high-error areas (the opposite of what we
    # want). Flip RESIDUAL_REFRESH_EVERY back to a finite value to re-enable.
    RESIDUAL_REFRESH_EVERY = 0
    RESIDUAL_BOOST = 4.0

    def _refresh_residual_weight(self) -> None:
        """Reblend `self.edge_weight` (shared memory) with current residual error.

        Per-pixel residual = mean abs diff(target, canvas) across RGB, in [0..1].
        New weight = base * (1 + (RESIDUAL_BOOST - 1) * residual). Areas where
        the canvas is already close to target stay at the base edge weight;
        unfinished regions get up to RESIDUAL_BOOST× their original weight so
        subsequent workers preferentially place shapes there.
        """
        diff = np.abs(self.canvas.astype(np.float32) - self.target.astype(np.float32)).mean(axis=2) / 255.0
        boost = 1.0 + (self.RESIDUAL_BOOST - 1.0) * diff.astype(np.float32)
        self.edge_weight[:] = self._base_edge_weight * boost

    def _max_size_frac_for_progress(self, progress: float) -> float:
        """Shape-size schedule. Monotonically decreasing across iteration progress.

        Per-candidate scoring cost is O(bbox_area) and bbox area scales with
        `max_size_frac²`, so an early tier with `max_size_frac=1.0` (canvas-
        spanning shapes) is ~16× more expensive than the legacy `0.25`
        default. The values below keep T1 noticeably larger than legacy (for
        tonal coverage) without exploding scoring cost at higher
        max_resolutions (4K / 8K targets).
        """
        if progress < 0.25:
            return 0.30        # 0–25%: ~30% canvas — modest bump over legacy for tonal blocks
        if progress < 0.50:
            return 0.22        # 26–50%: ~22% canvas
        if progress < 0.75:
            return 0.15        # 51–75%: ~15% canvas
        return 0.10            # 76–100%: 10% canvas — fine detail only

    def _parallel_search(self, types: list[str], n_random: int, n_mutate: int,
                         max_size_frac: float | None = None) -> tuple[float, Shape | None]:
        """Dispatch N independent FULL searches in parallel; return (best_score, best_shape).

        Each worker does the FULL `random_samples` random search (not a slice of
        it), picks its own local best, hill-climbs that, and returns. Main picks
        the global best across all workers.

        This preserves v0.2.0-equivalent per-iteration quality (each worker
        matches what the old single-chain code did) and adds parallel restarts
        on top: with N workers we get N independent attempts and keep the best.
        Splitting `random_samples` across workers (what an earlier rev did)
        gave each chain a much worse starting point and visibly degraded early
        shape selection — that's the regression we're correcting here.
        """
        n_random = max(1, n_random)
        n_mutate = max(1, n_mutate)
        args_list = [
            (types, n_random, n_mutate, self.w, self.h,
             self.rng.randint(0, 2**31 - 1), max_size_frac)
            for _ in range(self._n_workers)
        ]
        best_score = float("inf")
        best_shape: Shape | None = None
        for (score, color, shape) in self._executor.map(_worker_independent_search, args_list):
            if shape is not None and score < best_score:
                shape.color = color
                best_score, best_shape = score, shape
        return best_score, best_shape

    def run(self) -> Iterable[EngineEvent]:
        p = self.profile
        types = [t for t in p.shape_types if t]
        if not types:
            types = ["rotated_ellipse"]
        # Per-iteration type rotation. Without this, every worker picks a type
        # at random and ellipses (which fit organic content best) win the
        # fitness comparison nearly every iteration, so checked rectangle /
        # rotated_rectangle types produce zero shapes in the final JSON. With
        # rotation, each iteration is locked to a single type so every
        # checked type gets dedicated commit slots in proportion to how many
        # types are enabled.
        type_cursor = 0
        save_at = set(p.save_at)
        try:
            consecutive_skips = 0
            MAX_CONSECUTIVE_SKIPS = 80
            while len(self.shapes) < p.stop_at and not self._stop:
                while self._pause and not self._stop:
                    time.sleep(0.05)

                iter_types = [types[type_cursor % len(types)]]
                type_cursor += 1

                progress = len(self.shapes) / max(1, p.stop_at)
                size_cap = self._max_size_frac_for_progress(progress)

                refined_score, refined = self._parallel_search(
                    iter_types, max(1, p.random_samples), max(1, p.mutated_samples),
                    max_size_frac=size_cap,
                )

                # Sticker mode: refined must fit essentially entirely inside
                # the opaque region. If it doesn't, retry up to 5 times then
                # skip this iteration.
                if self.alpha_mask is not None:
                    sticker_attempts = 0
                    while sticker_attempts < 5:
                        if refined is not None and refined_score != float("inf"):
                            break
                        refined_score, refined = self._parallel_search(
                            iter_types, max(1, p.random_samples), max(1, p.mutated_samples),
                            max_size_frac=size_cap,
                        )
                        sticker_attempts += 1
                    else:
                        consecutive_skips += 1
                        if consecutive_skips >= MAX_CONSECUTIVE_SKIPS:
                            yield EngineEvent(
                                kind="done",
                                shape_count=len(self.shapes),
                                rms=self.rms,
                                canvas=self._preview_canvas(),
                                message=(
                                    f"Stopped early at {len(self.shapes)} shapes — couldn't "
                                    f"fit any more inside the opaque region after {MAX_CONSECUTIVE_SKIPS} "
                                    "consecutive attempts. Try increasing 'Random samples' or "
                                    "enabling smaller shape types."
                                ),
                            )
                            return
                        continue
                    consecutive_skips = 0

                if refined is None:
                    continue

                # Commit. Update shared canvas in place so next iteration's
                # workers see the new state on their next read.
                new_canvas, new_rms = composite(self.canvas, refined, self.target, self.alpha_mask, self.edge_weight)
                self.canvas[:] = new_canvas
                self.rms = new_rms
                self.shapes.append(refined)
                count = len(self.shapes)

                # Periodic completeness check (currently disabled — see
                # RESIDUAL_REFRESH_EVERY note). When > 0, recomputes the
                # importance map so under-painted regions get extra weight on
                # the next batch of worker searches.
                if self.RESIDUAL_REFRESH_EVERY > 0 and count > 0 and count % self.RESIDUAL_REFRESH_EVERY == 0:
                    self._refresh_residual_weight()

                yield EngineEvent(kind="shape_committed", shape_count=count, rms=self.rms)

                if p.preview_every and (count % p.preview_every == 0):
                    yield EngineEvent(kind="preview", shape_count=count, rms=self.rms, canvas=self._preview_canvas())

                if count in save_at or (p.save_every and count % p.save_every == 0):
                    yield EngineEvent(kind="checkpoint", shape_count=count, rms=self.rms)

            yield EngineEvent(kind="done", shape_count=len(self.shapes), rms=self.rms, canvas=self._preview_canvas())
        except Exception as exc:
            yield EngineEvent(kind="error", message=f"{type(exc).__name__}: {exc}")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            if self._canvas_shm is not None:
                self._canvas_shm.close()
                self._canvas_shm.unlink()
                self._canvas_shm = None
        except Exception:
            pass
        try:
            if self._edge_weight_shm is not None:
                self._edge_weight_shm.close()
                self._edge_weight_shm.unlink()
                self._edge_weight_shm = None
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self._shutdown()
        except Exception:
            pass
