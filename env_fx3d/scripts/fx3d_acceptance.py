"""FluidX3D acceptance runs for LFTH_CFD.

These runs guard the exact failure mode we saw: water appearing near the
nozzle while never producing a usable floor-arrival signal.

Scenarios:
  source_smoke    simple empty domain, pass if z_min <= 0.3m and floor cells exist
  cascade_short   validated cascade module, pass if source, modules, floor, target all pass
  cascade_visual  reports middle/final PNG paths for human inspection

Usage:
  python env_fx3d/scripts/fx3d_acceptance.py --scenario all
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
RUNS = MODULE_ROOT / "runs"
FLUIDX3D_EXE = MODULE_ROOT / "external" / "FluidX3D" / "bin" / "FluidX3D.exe"

sys.path.insert(0, str(SCRIPT_DIR))
from fx3d_postprocess import postprocess


def write_case_txt(path: Path, payload: dict) -> None:
    lines = []
    for key, value in payload.items():
        if isinstance(value, (list, tuple)):
            lines.append(key + " " + " ".join(f"{float(x):.6f}" for x in value))
        else:
            lines.append(f"{key} {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_nozzles_txt(path: Path, nozzles: list[list[float]],
                      vx: float = 0.0, vy: float = 0.0, vz: float = -12.0) -> None:
    lines = ["# x_m y_m z_m vx_mps vy_mps vz_mps"]
    for x, y, z in nozzles:
        lines.append(f"{x:.6f} {y:.6f} {z:.6f} {vx:.6f} {vy:.6f} {vz:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stride_nozzles(nozzles: list[list[float]], max_points: int = 8) -> list[list[float]]:
    if len(nozzles) <= max_points:
        return nozzles
    step = max(len(nozzles) / float(max_points), 1.0)
    return [nozzles[min(int(round(i * step)), len(nozzles) - 1)]
            for i in range(max_points)]


def load_module_bboxes(path: Path) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for module in data.get("modules", []):
        bbox_m = module.get("bbox_m")
        if bbox_m:
            out.append(bbox_m)
            continue
        bbox_mm = module.get("bbox_mm")
        if bbox_mm:
            out.append([[float(v) / 1000.0 for v in bbox_mm[0]],
                        [float(v) / 1000.0 for v in bbox_mm[1]]])
    return out


def write_dummy_cube_stl(path: Path) -> None:
    """Write a tiny valid binary STL far outside the smoke-test domain."""
    lo = (20.0, 20.0, 20.0)
    hi = (20.1, 20.1, 20.1)
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    verts = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6),
        (0, 4, 5), (0, 5, 1), (1, 5, 6), (1, 6, 2),
        (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0),
    ]
    with path.open("wb") as f:
        f.write(b"LFTH acceptance dummy STL".ljust(80, b" "))
        f.write(struct.pack("<I", len(faces)))
        for face in faces:
            f.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for idx in face:
                f.write(struct.pack("<3f", *verts[idx]))
            f.write(struct.pack("<H", 0))


def read_stl_bbox(path: Path) -> tuple[list[float], list[float]]:
    raw = path.read_bytes()
    points: list[tuple[float, float, float]] = []
    if len(raw) >= 84:
        n_tri = struct.unpack("<I", raw[80:84])[0]
        expected = 84 + n_tri * 50
        if expected <= len(raw):
            for i in range(n_tri):
                base = 84 + i * 50 + 12
                for j in range(3):
                    points.append(struct.unpack("<3f", raw[base + j * 12:base + (j + 1) * 12]))
    if not points:
        for line in raw.decode("utf-8", errors="ignore").splitlines():
            parts = line.strip().split()
            if len(parts) == 4 and parts[0].lower() == "vertex":
                try:
                    points.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    pass
    if not points:
        raise ValueError(f"Could not read STL vertices from {path}")
    xs, ys, zs = zip(*points)
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


def base_case(iter_dir: Path, stl_path: Path, nozzles_path: Path,
              domain_bbox: list[float], timemax_s: float) -> dict:
    return {
        "stl_path": str(stl_path).replace("\\", "/"),
        "out_dir": str(iter_dir / "fx3d_out").replace("\\", "/") + "/",
        "nozzles_file": str(nozzles_path).replace("\\", "/"),
        "domain_bbox_m": domain_bbox,
        "dp_m": 0.1,
        "timemax_s": timemax_s,
        "dt_out_s": 0.1,
        "nozzle_refill_dt_s": 0.05,
        "nozzle_refill_col_h": 3,
        "nozzle_emit_col_h": 8,
        "nozzle_pulse_dt_s": 0.1,
        "nozzle_pulse_depth_h": 7,
        "nozzle_pulse_layers": 0,
        "seed_col_h": 15,
        "lbm_u_ref": 0.05,
        "nozzle_rho_inflow": 1.2,
        "nozzle_area_cells": 3,
        "pond_prefill_z_m": 0,
        "pond_prefill_z_bot_m": 0,
        "pond_prefill_xy_bbox_m": [0, 0, 0, 0],
        "side_walls": "E",
        "floor_type": "S",
        "visualization_modes": "PHI_RASTERIZE,FLAG_SURFACE,FIELD,STREAMLINES",
        "surface_tension_Npm": 0,
        "viscosity_m2ps": 1e-5,
        "density_kgpm3": 1000,
        "gravity_mps2": 9.81,
        "camera": [200, 15, 60, 1],
    }


def run_case(iter_dir: Path, case: dict, case_json_extra: dict) -> dict:
    if not FLUIDX3D_EXE.exists():
        raise FileNotFoundError(f"{FLUIDX3D_EXE} missing. Run build_fluidx3d.py first.")
    (iter_dir / "fx3d_out" / "frames").mkdir(parents=True, exist_ok=True)
    (iter_dir / "fx3d_out" / "vtk").mkdir(parents=True, exist_ok=True)
    write_case_txt(iter_dir / "case.txt", case)
    snapshot = {
        **case,
        **case_json_extra,
        "fluid_threshold": 0.1,
        "score_slab_thickness_m": 1.0,
        "floor_hit_band_m": 1.0,
        "arrival_min_floor_cells_per_frame": 10,
        "arrival_min_floor_contact_frames": 5,
        "arrival_min_floor_total": 100,
        "top_lock_min_drop_m": 10.0,
        "top_retention_max_final_ratio": 0.35,
        "top_retention_max_final_cells": 1000,
        "cascade_min_module_touch_cells": 25,
        "cascade_min_modules_with_fluid": 3,
        "source_tail_min_cells": 3,
        "source_tail_min_late_frames": 3,
        "source_pulse_min_late_events": 5,
        "source_pulse_min_depths": 2,
        "nozzle_velocity_mps": 12.0,
        "solver_collision": "SRT",
    }
    (iter_dir / "case.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    t0 = time.time()
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run([str(FLUIDX3D_EXE)], cwd=str(iter_dir),
                          capture_output=True, text=True,
                          creationflags=creation_flags)
    wall_s = time.time() - t0
    (iter_dir / "fx3d_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (iter_dir / "fx3d_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-20:])
        raise RuntimeError(f"FluidX3D failed rc={proc.returncode}\n{tail}")
    result = postprocess(iter_dir)
    result["wall_time_s"] = round(wall_s, 1)
    (iter_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def image_paths(iter_dir: Path) -> dict:
    frames = sorted((iter_dir / "fx3d_out" / "frames").glob("image-*.png"))
    if not frames:
        return {"mid_png": None, "final_png": None}
    return {
        "mid_png": str(frames[len(frames) // 2].resolve()),
        "final_png": str(frames[-1].resolve()),
    }


def source_smoke(runs_root: Path) -> dict:
    iter_dir = runs_root / "iter_accept_source_smoke"
    if iter_dir.exists():
        shutil.rmtree(iter_dir)
    iter_dir.mkdir(parents=True, exist_ok=True)
    stl_path = iter_dir / "dummy_cube.stl"
    nozzles_path = iter_dir / "nozzles.txt"
    write_dummy_cube_stl(stl_path)
    write_nozzles_txt(nozzles_path, [[0.0, 0.0, 10.0]])
    case = base_case(iter_dir, stl_path, nozzles_path,
                     [-3.0, -3.0, 0.0, 3.0, 3.0, 12.0], 4.0)
    case["seed_col_h"] = 8
    result = run_case(iter_dir, case, {
        "test_id": "accept_source_smoke",
        "positive_bbox_m": [[-2.0, -2.0, 0.0], [2.0, 2.0, 0.0]],
        "negative_bbox_m": [[-3.0, -3.0, 0.0], [3.0, 3.0, 0.0]],
        "module_bboxes_m": [],
    })
    z_min = result.get("diagnostics", {}).get("lowest_fluid_z_seen_m")
    floor_total = int(result.get("arrival", {}).get("floor_total", 0) or 0)
    passed = z_min is not None and z_min <= 0.3 and floor_total > 0
    return {
        "scenario": "source_smoke",
        "passed": passed,
        "z_min_m": z_min,
        "floor_total": floor_total,
        "issue": result.get("issue"),
        "iter_dir": str(iter_dir.resolve()),
        **image_paths(iter_dir),
    }


def cascade_short(runs_root: Path, name: str = "accept_cascade_short") -> dict:
    iter_dir = runs_root / f"iter_{name}"
    targets_path = RUNS / "_real_targets.json"
    if not targets_path.exists():
        raise FileNotFoundError(f"{targets_path} missing. Run extract_targets.py first.")
    collider = RUNS / "_cascade_sx0_y1.stl"
    modules_path = RUNS / "_cascade_sx0_y1_modules.json"
    if not collider.exists():
        collider = RUNS / "_real_collider_thickened.stl"
        modules_path = RUNS / "_collider_modules.json"
    if not collider.exists():
        collider = RUNS / "_real_collider.stl"
    if not collider.exists():
        raise FileNotFoundError("No real collider STL found in env_fx3d/runs")

    targets = json.loads(targets_path.read_text(encoding="utf-8"))
    nozzles = stride_nozzles(targets["nozzles_m"], 8)
    if iter_dir.exists():
        shutil.rmtree(iter_dir)
    iter_dir.mkdir(parents=True, exist_ok=True)
    local_stl = iter_dir / "sculpture.stl"
    shutil.copy(collider, local_stl)
    nozzles_path = iter_dir / "nozzles.txt"
    write_nozzles_txt(nozzles_path, nozzles)

    bb_lo, bb_hi = read_stl_bbox(local_stl)
    pad = 3.0
    nozzle_top = max(n[2] for n in nozzles) if nozzles else bb_hi[2]
    domain = [
        bb_lo[0] - pad, bb_lo[1] - pad, 0.0,
        bb_hi[0] + pad, bb_hi[1] + pad, max(bb_hi[2], nozzle_top + 1.0),
    ]
    case = base_case(iter_dir, local_stl, nozzles_path, domain, 8.0)
    result = run_case(iter_dir, case, {
        "test_id": name,
        "positive_bbox_m": targets["positive_bbox_m"],
        "negative_bbox_m": targets["negative_bbox_m"],
        "module_bboxes_m": load_module_bboxes(modules_path),
        "module_bboxes_path": str(modules_path).replace("\\", "/") if modules_path.exists() else None,
    })
    arrival = result.get("arrival", {}) or {}
    diag = result.get("diagnostics", {}) or {}
    frames = int(arrival.get("frames_with_floor_contact", 0) or 0)
    floor_total = int(arrival.get("floor_total", 0) or 0)
    z_drop_m = float(diag.get("z_drop_m", 0.0) or 0.0)
    passed = (
        result.get("issue") is None
        and frames >= 5
        and floor_total >= 100
        and z_drop_m >= 10.0
        and bool(diag.get("continuous_source"))
    )
    return {
        "scenario": "cascade_short",
        "passed": passed,
        "frames_with_floor_contact": frames,
        "floor_total": floor_total,
        "max_floor_cells_per_frame": arrival.get("max_floor_cells_per_frame"),
        "arrival_score": arrival.get("score"),
        "z_drop_m": z_drop_m,
        "issue": result.get("issue"),
        "iter_dir": str(iter_dir.resolve()),
        **image_paths(iter_dir),
    }


def cascade_visual(runs_root: Path) -> dict:
    report = cascade_short(runs_root, "accept_cascade_visual")
    report["scenario"] = "cascade_visual"
    report["passed"] = bool(report["passed"] and report.get("mid_png") and report.get("final_png"))
    report["human_check"] = "Inspect mid_png and final_png; top-only fixed water means failure."
    return report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", choices=["all", "source_smoke", "cascade_short", "cascade_visual"],
                    default="all")
    ap.add_argument("--runs-root", default=str(RUNS))
    args = ap.parse_args(argv[1:])
    runs_root = Path(args.runs_root).resolve()

    reports = []
    if args.scenario == "all":
        print("[acceptance] running source_smoke ...", flush=True)
        reports.append(source_smoke(runs_root))
        print("[acceptance] running cascade_short ...", flush=True)
        cascade_report = cascade_short(runs_root)
        reports.append(cascade_report)
        visual_report = {**cascade_report, "scenario": "cascade_visual"}
        visual_report["passed"] = bool(cascade_report["passed"]
                                       and cascade_report.get("mid_png")
                                       and cascade_report.get("final_png"))
        visual_report["human_check"] = "Inspect mid_png and final_png; top-only fixed water means failure."
        reports.append(visual_report)
        print(json.dumps(reports, indent=2), flush=True)
        return 0 if all(r.get("passed") for r in reports) else 1

    for scenario in [args.scenario]:
        print(f"[acceptance] running {scenario} ...", flush=True)
        if scenario == "source_smoke":
            reports.append(source_smoke(runs_root))
        elif scenario == "cascade_short":
            reports.append(cascade_short(runs_root))
        elif scenario == "cascade_visual":
            reports.append(cascade_visual(runs_root))
    print(json.dumps(reports, indent=2), flush=True)
    return 0 if all(r.get("passed") for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
