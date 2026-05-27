"""Convert FluidX3D phi VTK frames into a DSPH-compatible result.json.

Each FluidX3D run writes:
  runs/iter_NN/fx3d_out/vtk/phi-XXXXXXXXX.vtk   binary STRUCTURED_POINTS
  runs/iter_NN/fx3d_out/frames/image-XXXXXXXXX.png
  runs/iter_NN/case.json                        params used (written by experiment_runner)

This script reads the LAST phi VTK, classifies each fluid cell against pond
and per-module SI bboxes, writes runs/iter_NN/result.json with the
`retention` dict that ga_sequential.py already understands.

Cell counts are used (not absolute mass) so ratios match DSPH semantics.

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

PROJECT = Path(__file__).resolve().parent.parent


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


DEFAULT_SCORE_SLAB_M = 0.5  # extrude flat positive/negative footprints into a vertical slab of this thickness


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
        slab = float(case.get("score_slab_thickness_m", DEFAULT_SCORE_SLAB_M))
        if "positive_bbox_m" in case:
            pos = _inflate_to_slab(case["positive_bbox_m"], slab)
            neg = _inflate_to_slab(case["negative_bbox_m"], slab) if "negative_bbox_m" in case else None
            mods = case.get("module_bboxes_m", []) or []
            return pos, neg, mods
        if "pond_bbox_m" in case:
            return case["pond_bbox_m"], None, case.get("module_bboxes_m", []) or []

    # Last-resort fallback: pull positive/negative from runs/_real_targets.json
    targets = PROJECT / "runs" / "_real_targets.json"
    if targets.exists():
        t = json.loads(targets.read_text(encoding="utf-8"))
        pos = _inflate_to_slab(t["positive_bbox_m"], slab)
        neg = _inflate_to_slab(t["negative_bbox_m"], slab)
        return pos, neg, []
    raise FileNotFoundError(f"no case.json with positive/negative/pond in {iter_dir} and no fallback targets")


def latest_phi_vtk(vtk_dir: Path) -> Path:
    files = sorted(vtk_dir.glob("phi-*.vtk"))
    if not files:
        raise FileNotFoundError(f"no phi-*.vtk in {vtk_dir}")
    return files[-1]


def postprocess(iter_dir: Path, fluid_threshold: float = 0.5) -> dict:
    vtk_dir = iter_dir / "fx3d_out" / "vtk"
    phi_path = latest_phi_vtk(vtk_dir)
    phi, shape, vtk_origin, spacing = parse_vtk_structured_points(phi_path)

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

    fluid_mask = phi >= fluid_threshold
    total = int(fluid_mask.sum())

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
    for i, bbox in enumerate(module_bboxes):
        mod_mask = cells_inside_bbox(shape, origin, spacing, bbox)
        cnt = int((fluid_mask & mod_mask).sum())
        in_module_each.append(cnt)
        on_module[str(i)] = cnt
        in_column_mask |= mod_mask
    in_column = int((fluid_mask & in_column_mask & ~positive_mask & ~negative_mask).sum())

    splash = int((fluid_mask & ~positive_mask & ~negative_mask & ~in_column_mask).sum())

    # primary score: ratio of water landed on positive vs (positive+negative).
    # Cells still in_column / splash (mid-air) don't count yet.
    score = in_positive / max(in_positive + in_negative, 1)

    # per-frame touch metric: did any fluid pass through each module's z-slab?
    per_slab_touch = [0] * len(module_bboxes)
    n_touched_all = 0
    all_phi = sorted(vtk_dir.glob("phi-*.vtk"))
    if module_bboxes:
        for p in all_phi:
            phi_t, _, _, _ = parse_vtk_structured_points(p)
            fl_t = phi_t >= fluid_threshold
            slab_hits = []
            for i, bbox in enumerate(module_bboxes):
                mod_mask = cells_inside_bbox(shape, origin, spacing, bbox)
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
        "wall_time_s": float(case.get("wall_time_s", 0.0)),
        "params": case,
        "positive_bbox_m": positive_bbox,
        "negative_bbox_m": negative_bbox,
        "module_bboxes_m": module_bboxes,
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
