"""Convert FluidX3D phi VTK frames into a DSPH-compatible result.json.

Each FluidX3D run writes:
  runs/iter_NN/fx3d_out/vtk/phi-XXXXXXXXX.vtk   binary STRUCTURED_POINTS
  runs/iter_NN/fx3d_out/frames/image-XXXXXXXXX.png
  runs/iter_NN/case.json                        params used (written by experiment_runner)

This script reads the phi VTK sequence, classifies each fluid cell against pond
and per-module SI bboxes, writes runs/iter_NN/result.json with the
`retention` dict that pymoo_run.py reads (in_positive, in_negative,
in_column, splash, total).

Cell counts are used (not absolute mass) so ratios match DSPH semantics. The
primary score is time-integrated over all frames so transient floor arrivals are
not lost when the last frame contains mostly falling/splashing water.

Usage:
  python scripts/fx3d_postprocess.py <iter_dir>
  python scripts/fx3d_postprocess.py runs/iter_test_22

Reads case.json from iter_dir for pond + module bboxes. If case.json missing,
falls back to defaults from _collider_modules.json + pond floor at z<0.5m.
"""

from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent


def parse_vtk_structured_points(vtk_path: Path) -> tuple[np.ndarray, tuple[int, int, int],
                                                          tuple[float, float, float],
                                                          tuple[float, float, float]]:
    """Read FluidX3D-style binary STRUCTURED_POINTS VTK with one float32 LOOKUP_TABLE.

    Returns: (data[Nz, Ny, Nx], (Nx, Ny, Nz), origin_xyz, spacing_xyz)
    Data already in SI units (FluidX3D writes convert_to_si_units=True by default).
    """
    with vtk_path.open("rb") as f:
        raw = f.read()

    # parse ASCII header lines until LOOKUP_TABLE
    def find_after(needle: bytes, start: int = 0) -> int:
        i = raw.find(needle, start)
        return -1 if i < 0 else raw.find(b"\n", i) + 1

    # DIMENSIONS
    m = re.search(rb"DIMENSIONS\s+(\d+)\s+(\d+)\s+(\d+)", raw)
    if not m:
        raise ValueError(f"DIMENSIONS not found in {vtk_path}")
    Nx, Ny, Nz = int(m.group(1)), int(m.group(2)), int(m.group(3))

    m = re.search(rb"ORIGIN\s+([-\d.Ee+]+)\s+([-\d.Ee+]+)\s+([-\d.Ee+]+)", raw)
    ox, oy, oz = (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else (0.0, 0.0, 0.0)

    m = re.search(rb"SPACING\s+([-\d.Ee+]+)\s+([-\d.Ee+]+)\s+([-\d.Ee+]+)", raw)
    sx, sy, sz = (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else (1.0, 1.0, 1.0)

    # find binary section after LOOKUP_TABLE default\n
    idx = raw.find(b"LOOKUP_TABLE default\n")
    if idx < 0:
        raise ValueError(f"LOOKUP_TABLE marker missing in {vtk_path}")
    bin_start = idx + len(b"LOOKUP_TABLE default\n")
    arr = np.frombuffer(raw[bin_start:], dtype=">f4", count=Nx * Ny * Nz)
    if arr.size != Nx * Ny * Nz:
        raise ValueError(f"truncated VTK: got {arr.size} expected {Nx*Ny*Nz}")
    return arr.reshape(Nz, Ny, Nx), (Nx, Ny, Nz), (ox, oy, oz), (sx, sy, sz)


def cells_inside_bbox(grid_shape: tuple[int, int, int],
                      origin: tuple[float, float, float],
                      spacing: tuple[float, float, float],
                      bbox_m: list[list[float]]) -> np.ndarray:
    """Return a boolean mask (Nz, Ny, Nx) of cells whose center is inside bbox."""
    Nx, Ny, Nz = grid_shape
    ox, oy, oz = origin
    sx, sy, sz = spacing
    (x0, y0, z0), (x1, y1, z1) = bbox_m
    xs = ox + (np.arange(Nx) + 0.5) * sx
    ys = oy + (np.arange(Ny) + 0.5) * sy
    zs = oz + (np.arange(Nz) + 0.5) * sz
    mx = (xs >= x0) & (xs <= x1)
    my = (ys >= y0) & (ys <= y1)
    mz = (zs >= z0) & (zs <= z1)
    # broadcast to (Nz, Ny, Nx)
    return mz[:, None, None] & my[None, :, None] & mx[None, None, :]


DEFAULT_SCORE_SLAB_M = 1.0  # extrude flat positive/negative footprints into a vertical slab of this thickness
DEFAULT_MIN_FLOOR_CELLS_PER_FRAME = 10
DEFAULT_MIN_FLOOR_CONTACT_FRAMES = 5
DEFAULT_MIN_FLOOR_TOTAL = 100
DEFAULT_TOP_LOCK_MIN_DROP_M = 10.0
DEFAULT_TOP_RETENTION_MAX_FINAL_RATIO = 0.35
DEFAULT_TOP_RETENTION_MAX_FINAL_CELLS = 1000
DEFAULT_MIN_MODULE_TOUCH_CELLS = 25
DEFAULT_MIN_MODULES_WITH_FLUID = 3
DEFAULT_SOURCE_TAIL_MIN_LATE_FRAMES = 3


def _inflate_to_slab(bbox: list, slab_m: float) -> list:
    """If a bbox has zero z-extent (Rhino Brep surface), inflate to a slab from
    the bbox z-level up by slab_m. Otherwise pass through.
    """
    (x0, y0, z0), (x1, y1, z1) = bbox
    if abs(z1 - z0) < 1e-6:
        return [[x0, y0, z0], [x1, y1, z0 + slab_m]]
    return [[x0, y0, z0], [x1, y1, z1]]


def load_bboxes(iter_dir: Path) -> tuple[list, list, list[list]]:
    """Return (positive_bbox_m, negative_bbox_m, module_bboxes_m_or_empty).

    Prefer case.json. Backwards-compat: if only pond_bbox_m exists, treat as
    positive with empty negative.
    """
    case_path = iter_dir / "case.json"
    slab = DEFAULT_SCORE_SLAB_M
    if case_path.exists():
        case = json.loads(case_path.read_text(encoding="utf-8"))
        slab = max(float(case.get("score_slab_thickness_m", DEFAULT_SCORE_SLAB_M)),
                   DEFAULT_SCORE_SLAB_M)
        if "positive_bbox_m" in case:
            pos = _inflate_to_slab(case["positive_bbox_m"], slab)
            neg = _inflate_to_slab(case["negative_bbox_m"], slab) if "negative_bbox_m" in case else None
            mods = case.get("module_bboxes_m", []) or _load_module_bboxes_fallback(iter_dir)
            return pos, neg, mods
        if "pond_bbox_m" in case:
            return case["pond_bbox_m"], None, case.get("module_bboxes_m", []) or _load_module_bboxes_fallback(iter_dir)

    # Last-resort fallback: pull positive/negative from runs/_real_targets.json
    targets = MODULE_ROOT / "runs" / "_real_targets.json"
    if targets.exists():
        t = json.loads(targets.read_text(encoding="utf-8"))
        pos = _inflate_to_slab(t["positive_bbox_m"], slab)
        neg = _inflate_to_slab(t["negative_bbox_m"], slab)
        return pos, neg, _load_module_bboxes_fallback(iter_dir)
    raise FileNotFoundError(f"no case.json with positive/negative/pond in {iter_dir} and no fallback targets")


def _load_module_bboxes_fallback(iter_dir: Path | None = None) -> list:
    candidates = []
    if iter_dir is not None:
        candidates.append(iter_dir.parent / "_collider_modules.json")
    candidates.append(MODULE_ROOT / "runs" / "_collider_modules.json")
    modules_path = next((p for p in candidates if p.exists()), None)
    if modules_path is None:
        return []
    data = json.loads(modules_path.read_text(encoding="utf-8"))
    out = []
    for module in data.get("modules", []):
        bbox_mm = module.get("bbox_mm")
        if not bbox_mm:
            continue
        out.append([[float(v) / 1000.0 for v in bbox_mm[0]],
                    [float(v) / 1000.0 for v in bbox_mm[1]]])
    return out


def latest_phi_vtk(vtk_dir: Path) -> Path:
    files = sorted(vtk_dir.glob("phi-*.vtk"))
    if not files:
        raise FileNotFoundError(f"no phi-*.vtk in {vtk_dir}")
    return files[-1]


def load_nozzles(case: dict) -> list[list[float]]:
    path = case.get("nozzles_file")
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 3:
            continue
        try:
            out.append([float(parts[0]), float(parts[1]), float(parts[2])])
        except ValueError:
            continue
    return out


def postprocess(iter_dir: Path, fluid_threshold: float | None = None) -> dict:
    vtk_dir = iter_dir / "fx3d_out" / "vtk"
    phi_path = latest_phi_vtk(vtk_dir)
    phi, shape, vtk_origin, spacing = parse_vtk_structured_points(phi_path)

    # If threshold not given, read from iter_dir/case.json (key fluid_threshold)
    if fluid_threshold is None:
        case_path = iter_dir / "case.json"
        if case_path.exists():
            try:
                fluid_threshold = float(json.loads(case_path.read_text(encoding="utf-8")).get("fluid_threshold", 0.5))
            except Exception:
                fluid_threshold = 0.5
        else:
            fluid_threshold = 0.5

    positive_bbox, negative_bbox, module_bboxes = load_bboxes(iter_dir)

    # FluidX3D writes VTK with origin centered around (0,0,0) in lattice space
    # (independent of our setup.cpp's si_offset). Override the origin to match
    # our physical SI frame: cell (0,0,0) sits at domain_bbox_m[0:3].
    case_path = iter_dir / "case.json"
    case = {}
    if case_path.exists():
        case = json.loads(case_path.read_text(encoding="utf-8"))
        dbb = case.get("domain_bbox_m")
        if dbb is not None and len(dbb) >= 3:
            if isinstance(dbb[0], list):
                origin = tuple(dbb[0])
            else:
                origin = (float(dbb[0]), float(dbb[1]), float(dbb[2]))
        else:
            origin = vtk_origin
    else:
        origin = vtk_origin

    floor_band_cfg = case.get("floor_hit_band_m", case.get("score_slab_thickness_m", DEFAULT_SCORE_SLAB_M))
    try:
        floor_band_m = max(float(floor_band_cfg or 0.0), 1.5 * abs(spacing[2]), DEFAULT_SCORE_SLAB_M)
    except Exception:
        floor_band_m = max(1.5 * abs(spacing[2]), DEFAULT_SCORE_SLAB_M)

    fluid_mask = phi >= fluid_threshold
    total = int(fluid_mask.sum())
    if total:
        zz, yy, xx = np.where(fluid_mask)
        ox, oy, oz = origin
        sx, sy, sz = spacing
        xs = ox + (xx + 0.5) * sx
        ys = oy + (yy + 0.5) * sy
        zs = oz + (zz + 0.5) * sz
        floor_hit_count = int((zs <= oz + floor_band_m).sum())
        fluid_extent_m = {
            "x": [float(xs.min()), float(xs.max())],
            "y": [float(ys.min()), float(ys.max())],
            "z": [float(zs.min()), float(zs.max())],
            "mean_z": float(zs.mean()),
        }
    else:
        floor_hit_count = 0
        fluid_extent_m = None
    final_reached_floor = floor_hit_count > 0

    positive_mask = cells_inside_bbox(shape, origin, spacing, positive_bbox)
    in_positive = int((fluid_mask & positive_mask).sum())

    if negative_bbox is not None:
        negative_mask_full = cells_inside_bbox(shape, origin, spacing, negative_bbox)
        # negative excludes positive footprint (positive sits inside negative)
        negative_mask = negative_mask_full & ~positive_mask
    else:
        negative_mask = np.zeros_like(fluid_mask)
    in_negative = int((fluid_mask & negative_mask).sum())

    # module retention (water held on sculpture, not yet at floor)
    in_module_each = []
    on_module = {}
    in_column_mask = np.zeros_like(fluid_mask)
    module_masks = [cells_inside_bbox(shape, origin, spacing, bbox)
                    for bbox in module_bboxes]
    for i, mod_mask in enumerate(module_masks):
        cnt = int((fluid_mask & mod_mask).sum())
        in_module_each.append(cnt)
        on_module[str(i)] = cnt
        in_column_mask |= mod_mask
    in_column = int((fluid_mask & in_column_mask & ~positive_mask & ~negative_mask).sum())

    splash = int((fluid_mask & ~positive_mask & ~negative_mask & ~in_column_mask).sum())

    all_phi = sorted(vtk_dir.glob("phi-*.vtk"))
    floor_z_mask = (origin[2] + (np.arange(shape[2]) + 0.5) * spacing[2]) <= origin[2] + floor_band_m
    floor_mask = floor_z_mask[:, None, None]

    # Primary score: time-integrated arrival on the floor target. The previous
    # final-frame-only score missed cases where water reached the floor earlier
    # and drained/left the scoring slab by the last VTK frame.
    min_floor_cells_per_frame = int(case.get("arrival_min_floor_cells_per_frame",
                                             DEFAULT_MIN_FLOOR_CELLS_PER_FRAME))
    min_floor_contact_frames = int(case.get("arrival_min_floor_contact_frames",
                                            DEFAULT_MIN_FLOOR_CONTACT_FRAMES))
    min_floor_total = int(case.get("arrival_min_floor_total",
                                   DEFAULT_MIN_FLOOR_TOTAL))
    top_lock_min_drop_m = float(case.get("top_lock_min_drop_m",
                                         DEFAULT_TOP_LOCK_MIN_DROP_M))
    top_retention_max_final_ratio = float(case.get("top_retention_max_final_ratio",
                                                   DEFAULT_TOP_RETENTION_MAX_FINAL_RATIO))
    top_retention_max_final_cells = int(case.get("top_retention_max_final_cells",
                                                DEFAULT_TOP_RETENTION_MAX_FINAL_CELLS))
    min_module_touch_cells = int(case.get("cascade_min_module_touch_cells",
                                          DEFAULT_MIN_MODULE_TOUCH_CELLS))
    min_modules_with_fluid = int(case.get("cascade_min_modules_with_fluid",
                                          DEFAULT_MIN_MODULES_WITH_FLUID))
    nozzles_m = load_nozzles(case)
    dp_m = float(case.get("dp_m", abs(spacing[0])) or abs(spacing[0]))
    refill_col_h = int(case.get("nozzle_refill_col_h", 3) or 3)
    emit_col_h = int(case.get("nozzle_emit_col_h", refill_col_h) or refill_col_h)
    nozzle_area_cells = int(case.get("nozzle_area_cells", 1) or 1)
    source_radius_m = float(case.get(
        "source_tail_radius_m",
        max(0.25, (nozzle_area_cells // 2 + 2) * dp_m),
    ))
    source_anchor_depth_m = max((refill_col_h + 1) * dp_m, 0.25)
    source_tail_depth_m = max((emit_col_h + 6) * dp_m, source_anchor_depth_m + 0.75)
    source_tail_min_cells = int(case.get("source_tail_min_cells",
                                        max(3, len(nozzles_m) // 2 if nozzles_m else 3)))
    source_tail_min_late_frames = int(case.get("source_tail_min_late_frames",
                                              DEFAULT_SOURCE_TAIL_MIN_LATE_FRAMES))
    top_module_idx = None
    if module_bboxes:
        top_module_idx = max(range(len(module_bboxes)),
                             key=lambda i: float(module_bboxes[i][1][2]))

    arrival_positive = 0
    arrival_negative = 0
    arrival_floor = 0
    arrival_frames = 0
    frame_stats = []
    z_min_first = None
    z_min_lowest = None
    max_floor_cells_per_frame = 0
    max_module_counts = [0] * len(module_masks)
    final_top_module_cells = int(in_module_each[top_module_idx]) if top_module_idx is not None else 0
    top_module_final_ratio = final_top_module_cells / max(total, 1)
    source_tail_frames = 0
    source_tail_late_frames = 0
    max_source_tail_cells = 0
    for p in all_phi:
        phi_t, _, _, _ = parse_vtk_structured_points(p)
        fl_t = phi_t >= fluid_threshold
        pos_t = int((fl_t & positive_mask).sum())
        neg_t = int((fl_t & negative_mask).sum())
        floor_t = int((fl_t & floor_mask).sum())
        module_counts_t = [int((fl_t & m).sum()) for m in module_masks]
        for i, cnt_t in enumerate(module_counts_t):
            max_module_counts[i] = max(max_module_counts[i], cnt_t)
        z_min_t = None
        z_max_t = None
        source_anchor_t = 0
        source_tail_t = 0
        if fl_t.any():
            zz_t = np.where(fl_t)[0]
            z_cells = origin[2] + (zz_t + 0.5) * spacing[2]
            z_min_t = float(z_cells.min())
            z_max_t = float(z_cells.max())
            if z_min_first is None:
                z_min_first = z_min_t
            z_min_lowest = z_min_t if z_min_lowest is None else min(z_min_lowest, z_min_t)
            if nozzles_m:
                # Use the full fluid coordinate arrays for source bands; this
                # is stricter than counting the fixed top source cells alone.
                zidx, yidx, xidx = np.where(fl_t)
                xs_t = origin[0] + (xidx + 0.5) * spacing[0]
                ys_t = origin[1] + (yidx + 0.5) * spacing[1]
                zs_t = origin[2] + (zidx + 0.5) * spacing[2]
                for nx, ny, nz in nozzles_m:
                    xy = (np.abs(xs_t - nx) <= source_radius_m) & (np.abs(ys_t - ny) <= source_radius_m)
                    source_anchor_t += int((xy & (zs_t >= nz - source_anchor_depth_m) & (zs_t <= nz + 0.2)).sum())
                    source_tail_t += int((xy & (zs_t >= nz - source_tail_depth_m) & (zs_t < nz - source_anchor_depth_m)).sum())
        arrival_positive += pos_t
        arrival_negative += neg_t
        arrival_floor += floor_t
        max_floor_cells_per_frame = max(max_floor_cells_per_frame, floor_t)
        if floor_t > 0:
            arrival_frames += 1
        if source_tail_t >= source_tail_min_cells:
            source_tail_frames += 1
        max_source_tail_cells = max(max_source_tail_cells, source_tail_t)
        frame_stats.append({
            "frame": p.name,
            "floor_cells": floor_t,
            "target_cells": pos_t + neg_t,
            "source_anchor_cells": source_anchor_t,
            "source_tail_cells": source_tail_t,
            "module_cells": module_counts_t,
            "z_min_m": z_min_t,
            "z_max_m": z_max_t,
        })

    final_score = in_positive / max(in_positive + in_negative, 1)
    arrival_total = arrival_positive + arrival_negative
    score = arrival_positive / max(arrival_total, 1)
    arrival_reached_floor = arrival_floor > 0
    z_drop_m = 0.0
    if z_min_first is not None and z_min_lowest is not None:
        z_drop_m = max(0.0, z_min_first - z_min_lowest)
    top_locked = bool(z_min_first is not None and z_drop_m < top_lock_min_drop_m
                      and max_floor_cells_per_frame == 0)
    strong_floor_arrival = (
        arrival_frames >= min_floor_contact_frames
        and arrival_floor >= min_floor_total
        and max_floor_cells_per_frame >= min_floor_cells_per_frame
    )
    modules_with_fluid = sum(1 for c in max_module_counts if c >= min_module_touch_cells)
    top_retention_failure = bool(
        top_module_idx is not None
        and final_top_module_cells >= top_retention_max_final_cells
        and top_module_final_ratio >= top_retention_max_final_ratio
    )
    late_stats = frame_stats[len(frame_stats) // 2:]
    if nozzles_m:
        source_tail_late_frames = sum(
            1 for s in late_stats if int(s.get("source_tail_cells") or 0) >= source_tail_min_cells
        )
    continuous_source = bool(
        not nozzles_m
        or source_tail_late_frames >= source_tail_min_late_frames
    )

    if not continuous_source:
        issue = "source_not_continuous"
        notes = "No moving source-tail fluid was detected near the nozzle in enough late VTK frames."
    elif top_retention_failure:
        issue = "top_retention_failure"
        notes = ("Fluid remains concentrated in the top module at the final frame; "
                 "this is not a visual cascade even if droplets reached the floor.")
    elif not arrival_reached_floor:
        issue = "top_locked" if top_locked else "no_floor_contact"
        notes = ("Fluid z-min did not drop enough from the initial source band."
                 if issue == "top_locked"
                 else "No fluid cells reached the floor band.")
    elif not strong_floor_arrival:
        issue = "weak_arrival"
        notes = ("Fluid reached the floor band, but contact was too sparse for a "
                 "usable cascade/optimization signal.")
    elif module_bboxes and modules_with_fluid < min_modules_with_fluid:
        issue = "insufficient_cascade_progress"
        notes = "Fluid did not distribute across enough modules to count as a cascade."
    elif arrival_positive <= 0 or arrival_total <= 0:
        issue = "target_miss"
        notes = "Strong floor arrival exists, but no positive target arrival was counted."
    else:
        issue = None
        notes = None

    # per-frame touch metric: did any fluid pass through each module's z-slab?
    per_slab_touch = [0] * len(module_bboxes)
    n_touched_all = 0
    if module_bboxes:
        for p in all_phi:
            phi_t, _, _, _ = parse_vtk_structured_points(p)
            fl_t = phi_t >= fluid_threshold
            slab_hits = []
            for i, mod_mask in enumerate(module_masks):
                hit = bool((fl_t & mod_mask).any())
                slab_hits.append(hit)
                if hit:
                    per_slab_touch[i] += 1
            if all(slab_hits):
                n_touched_all += 1
    touch_all_ratio = n_touched_all / max(len(all_phi), 1)

    result = {
        "test_id": iter_dir.name.replace("iter_", ""),
        "engine": "fluidx3d",
        "phi_threshold": fluid_threshold,
        "phi_source": phi_path.name,
        "score": score,                                  # primary GA fitness
        "arrival_score": score,
        "final_score": final_score,
        "retention": {
            "total": total,
            "in_positive": in_positive,                  # water on target pond
            "in_negative": in_negative,                  # water on forbidden splash zone
            "in_column": in_column,                      # held on sculpture
            "splash": splash,                            # outside everything (mid-air / escaped)
            "on_module": on_module,
            "settled_in_place": 0,
            "retained": in_positive + in_column,
            "retention_rate": (in_positive + in_column) / max(total, 1),
            # legacy alias for any consumer still expecting in_pond
            "in_pond": in_positive,
        },
        "touch": {
            "n_total_frames": len(all_phi),
            "per_slab_touch": per_slab_touch,
            "n_touched_all": n_touched_all,
            "touch_all_ratio": touch_all_ratio,
        },
        "arrival": {
            "in_positive": arrival_positive,
            "in_negative": arrival_negative,
            "floor_total": arrival_floor,
            "target_total": arrival_total,
            "frames_with_floor_contact": arrival_frames,
            "max_floor_cells_per_frame": max_floor_cells_per_frame,
            "max_module_counts": max_module_counts,
            "modules_with_fluid": modules_with_fluid,
            "source_tail_frames": source_tail_frames,
            "source_tail_late_frames": source_tail_late_frames,
            "max_source_tail_cells": max_source_tail_cells,
            "continuous_source": continuous_source,
            "min_floor_cells_per_frame": min_floor_cells_per_frame,
            "min_floor_contact_frames": min_floor_contact_frames,
            "min_floor_total": min_floor_total,
            "strong_floor_arrival": strong_floor_arrival,
            "score": score,
        },
        "wall_time_s": float(case.get("wall_time_s", 0.0)),
        "params": case,
        "positive_bbox_m": positive_bbox,
        "negative_bbox_m": negative_bbox,
        "module_bboxes_m": module_bboxes,
        "diagnostics": {
            "fluid_extent_m": fluid_extent_m,
            "floor_hit_band_m": floor_band_m,
            "floor_hit_count": floor_hit_count,
            "final_reached_floor": final_reached_floor,
            "arrival_floor_total": arrival_floor,
            "arrival_frames_with_floor_contact": arrival_frames,
            "arrival_score": score,
            "max_floor_cells_per_frame": max_floor_cells_per_frame,
            "strong_floor_arrival": strong_floor_arrival,
            "max_module_counts": max_module_counts,
            "modules_with_fluid": modules_with_fluid,
            "min_modules_with_fluid": min_modules_with_fluid,
            "n_nozzles_scored": len(nozzles_m),
            "source_tail_frames": source_tail_frames,
            "source_tail_late_frames": source_tail_late_frames,
            "source_tail_min_cells": source_tail_min_cells,
            "source_tail_min_late_frames": source_tail_min_late_frames,
            "max_source_tail_cells": max_source_tail_cells,
            "continuous_source": continuous_source,
            "top_module_idx": top_module_idx,
            "top_module_final_cells": final_top_module_cells,
            "top_module_final_ratio": top_module_final_ratio,
            "top_retention_failure": top_retention_failure,
            "top_retention_max_final_cells": top_retention_max_final_cells,
            "top_retention_max_final_ratio": top_retention_max_final_ratio,
            "arrival_reached_floor": arrival_reached_floor,
            "reached_floor": arrival_reached_floor,
            "lowest_fluid_z_m": None if fluid_extent_m is None else fluid_extent_m["z"][0],
            "initial_lowest_fluid_z_m": z_min_first,
            "lowest_fluid_z_seen_m": z_min_lowest,
            "z_drop_m": z_drop_m,
            "top_locked": top_locked,
            "top_lock_min_drop_m": top_lock_min_drop_m,
            "frame_stats": frame_stats,
        },
        "issue": issue,
        "notes": notes,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return result


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    iter_dir = Path(argv[1]).resolve()
    if not iter_dir.is_dir():
        print(f"ERROR: {iter_dir} not a directory")
        return 1
    result = postprocess(iter_dir)
    out_path = iter_dir / "result.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    r = result["retention"]
    print(f"Wrote {out_path}")
    print(f"  total={r['total']}  in_positive={r['in_positive']}  in_negative={r['in_negative']}  "
          f"in_column={r['in_column']}  splash={r['splash']}")
    print(f"  score={result['score']:.4f}  retention_rate={r['retention_rate']:.3f}  "
          f"touch_all_ratio={result['touch']['touch_all_ratio']:.3f}")
    a = result.get("arrival", {}) or {}
    print(f"  arrival_floor={a.get('floor_total', 0)}  arrival_target={a.get('target_total', 0)}  "
          f"frames_floor={a.get('frames_with_floor_contact', 0)}  final_score={result.get('final_score', 0):.4f}")
    print(f"  arrival_score={a.get('score', 0):.4f}  max_floor_frame={a.get('max_floor_cells_per_frame', 0)}  "
          f"issue={result.get('issue') or 'none'}")
    d = result.get("diagnostics", {}) or {}
    print(f"  top_module_final={d.get('top_module_final_cells', 0)}  "
          f"top_ratio={d.get('top_module_final_ratio', 0):.3f}  "
          f"modules_with_fluid={d.get('modules_with_fluid', 0)}")
    print(f"  source_tail_late={d.get('source_tail_late_frames', 0)}  "
          f"max_source_tail={d.get('max_source_tail_cells', 0)}  "
          f"continuous_source={d.get('continuous_source')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
