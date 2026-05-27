"""Unified FluidX3D experiment driver.

Reads canonical config (config/case.json) on each invocation. Each call:
  1. Decides STL (default: runs/_real_collider_thickened.stl, or runs/_real_collider.stl)
  2. Generates iter_dir/case.txt + nozzles.txt + case.json (snapshot)
  3. Runs FluidX3D.exe with cwd=iter_dir
  4. Postprocesses → result.json
  5. Appends to runs/_settings_log.jsonl
  6. (optional) Pushes STL into Rhino
  7. Refreshes settings_compare.html

Callable as:
  CLI:  python scripts/fx3d_run.py [--test-id ID] [--stl PATH] [--config PATH]
  Lib:  from fx3d_run import run_experiment
        run_experiment(test_id, stl_path=..., config_overrides={...})
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from fx3d_postprocess import postprocess
from rhino_mcp_helpers import push_stl_to_rhino_layer

FLUIDX3D_DIR = PROJECT / "external" / "FluidX3D"
FLUIDX3D_EXE = FLUIDX3D_DIR / "bin" / "FluidX3D.exe"
FLUIDX3D_EXE_INTERACTIVE = FLUIDX3D_DIR / "bin" / "FluidX3D_interactive.exe"

CONFIG_DEFAULT = PROJECT / "config" / "case.json"
RUNS = PROJECT / "runs"
TARGETS_JSON = RUNS / "_real_targets.json"
SETTINGS_LOG = RUNS / "_settings_log.jsonl"

DEFAULT_COLLIDER_THICK = RUNS / "_real_collider_thickened.stl"
DEFAULT_COLLIDER_RAW = RUNS / "_real_collider.stl"


# --- helpers -----------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"missing canonical config {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # strip leading underscore (comment) keys
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def load_targets() -> dict:
    if not TARGETS_JSON.exists():
        raise FileNotFoundError(f"{TARGETS_JSON} missing. Run extract_targets.py first.")
    return json.loads(TARGETS_JSON.read_text(encoding="utf-8"))


def pick_default_stl() -> Path:
    if DEFAULT_COLLIDER_THICK.exists():
        return DEFAULT_COLLIDER_THICK
    if DEFAULT_COLLIDER_RAW.exists():
        return DEFAULT_COLLIDER_RAW
    raise FileNotFoundError("no collider STL — run extract_targets.py (+ thicken_collider.py)")


def nozzle_vz_from_lpm(lpm: float, dp_m: float) -> float:
    """Per-nozzle initial downward velocity from volumetric flow.
    v = Q/A, floored at -1 m/s (LBM stability — very low LPM still produces
    non-zero motion). Returns negative (downward)."""
    q_m3s = lpm * 1.0e-3 / 60.0
    area = dp_m * dp_m
    return -max(q_m3s / area, 1.0)


def write_nozzles_txt(path: Path, nozzles_m: list, vz: float) -> None:
    lines = ["# x_m y_m z_m vz_mps  (auto-generated from _real_targets.json + nozzle_LPM)"]
    for nz in nozzles_m:
        lines.append(f"{nz[0]:.6f} {nz[1]:.6f} {nz[2]:.6f} {vz:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_case_txt(path: Path, payload: dict) -> None:
    lines = []
    for k, v in payload.items():
        if isinstance(v, (list, tuple)):
            lines.append(k + " " + " ".join(f"{x:.6f}" for x in v))
        elif isinstance(v, bool):
            lines.append(f"{k} {1 if v else 0}")
        else:
            lines.append(f"{k} {v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compute_domain(stl_bbox: list, pad_m: float, nozzles_m: list) -> tuple[list, list]:
    (x0, y0, z0), (x1, y1, z1) = stl_bbox
    nozzle_z_top = max(p[2] for p in nozzles_m) if nozzles_m else z1
    lo = [x0 - pad_m, y0 - pad_m, 0.0]            # floor at z=0
    hi = [x1 + pad_m, y1 + pad_m, max(z1, nozzle_z_top + 1.0)]
    return lo, hi


def append_settings_log(test_id: str, case: dict, result: dict,
                         iter_dir: Path, wall_s: float,
                         collider_stl: Path, cfg: dict) -> None:
    r = result.get("retention", {}) or {}
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "test_id": test_id,
        "engine": result.get("engine", "fluidx3d"),
        # case.txt group
        "dp_m": case.get("dp_m"),
        "timemax_s": case.get("timemax_s"),
        "dt_out_s": case.get("dt_out_s"),
        "surface_tension_Npm": case.get("surface_tension_Npm"),
        "viscosity_m2ps": case.get("viscosity_m2ps"),
        "density_kgpm3": case.get("density_kgpm3"),
        "gravity_mps2": case.get("gravity_mps2"),
        "side_walls": case.get("side_walls"),
        "seed_col_h": case.get("seed_col_h"),
        "domain_bbox_m": case.get("domain_bbox_m"),
        # nozzles group
        "n_nozzles": cfg.get("_n_nozzles"),
        "nozzle_LPM": cfg.get("nozzle_LPM"),
        "nozzle_vz_mps": cfg.get("_nozzle_vz"),
        "total_Q_m3ps": cfg.get("_total_Q_m3ps"),
        # preprocess
        "thicken_thickness_m": cfg.get("thicken_thickness_m"),
        "collider_stl": str(collider_stl).replace("\\", "/"),
        # results
        "score": result.get("score"),
        "in_positive": r.get("in_positive"),
        "in_negative": r.get("in_negative"),
        "in_column": r.get("in_column"),
        "splash": r.get("splash"),
        "total": r.get("total"),
        "retention_rate": r.get("retention_rate"),
        # cost / paths
        "wall_s": round(wall_s, 2),
        "iter_dir": str(iter_dir).replace("\\", "/"),
        "frames_dir": str(iter_dir / "fx3d_out" / "frames").replace("\\", "/"),
    }
    SETTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --- main API ----------------------------------------------------------------

def run_experiment(test_id: str,
                   *,
                   stl_path: Path | None = None,
                   config_overrides: dict | None = None,
                   config_path: Path = CONFIG_DEFAULT,
                   push_rhino: bool | None = None,
                   interactive: bool = False,
                   timeout_s: float = 7200.0) -> dict:
    cfg = load_config(config_path)
    if config_overrides:
        cfg.update(config_overrides)
    if stl_path is None:
        stl_path = pick_default_stl()
    if push_rhino is None:
        push_rhino = bool(cfg.get("push_to_rhino", True))

    iter_dir = RUNS / f"iter_{test_id}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "fx3d_out" / "frames").mkdir(parents=True, exist_ok=True)
    (iter_dir / "fx3d_out" / "vtk").mkdir(parents=True, exist_ok=True)

    # STL copy (snapshot in iter dir, plus FluidX3D reads from absolute path)
    local_stl = iter_dir / "sculpture.stl"
    shutil.copy(stl_path, local_stl)
    if local_stl.stat().st_size < 100:
        raise RuntimeError(
            f"STL {stl_path} is empty/corrupt ({local_stl.stat().st_size} bytes). "
            "Regenerate via extract_targets.py + thicken_collider.py, or "
            "make_hemisphere.py, or build parametric STL via module_geometry.")
    loaded = trimesh.load(local_stl, force="mesh")
    if not isinstance(loaded, trimesh.Trimesh) or loaded.vertices is None or len(loaded.vertices) == 0:
        raise RuntimeError(f"STL {stl_path} did not load as a usable mesh "
                            f"(type={type(loaded).__name__}). Likely multi-component "
                            "Scene or zero-byte file.")
    m = loaded
    stl_bbox = m.bounds.tolist()

    # Targets (positive/negative + nozzles)
    targets = load_targets()
    nozzles_m = targets["nozzles_m"]
    vz = nozzle_vz_from_lpm(cfg["nozzle_LPM"], cfg["dp_m"])
    write_nozzles_txt(iter_dir / "nozzles.txt", nozzles_m, vz)

    # Domain auto-compute (sculpture + nozzle + pad)
    domain_lo, domain_hi = compute_domain(stl_bbox, cfg["domain_pad_m"], nozzles_m)

    # case.txt for FluidX3D
    case = {
        "stl_path": str(local_stl).replace("\\", "/"),
        "out_dir": str(iter_dir / "fx3d_out").replace("\\", "/") + "/",
        "nozzles_file": str(iter_dir / "nozzles.txt").replace("\\", "/"),
        "domain_bbox_m": domain_lo + domain_hi,
        "dp_m": cfg["dp_m"],
        "timemax_s": cfg["timemax_s"],
        "dt_out_s": cfg["dt_out_s"],
        "seed_col_h": cfg["seed_col_h"],
        "lbm_u_ref": cfg.get("lbm_u_ref", 0.05),
        "nozzle_rho_inflow": cfg.get("nozzle_rho_inflow", 1.0),
        "nozzle_area_cells": cfg.get("nozzle_area_cells", 1),
        "pond_prefill_z_m": cfg.get("pond_prefill_z_m", 0),
        "pond_prefill_z_bot_m": cfg.get("pond_prefill_z_bot_m", 0),
        "pond_prefill_xy_bbox_m": cfg.get("pond_prefill_xy_bbox_m", [0, 0, 0, 0]),
        "side_walls": cfg["side_walls"],
        "floor_type": cfg.get("floor_type", "S"),
        "visualization_modes": cfg.get("visualization_modes", "PHI_RAYTRACE,FLAG_SURFACE"),
        "surface_tension_Npm": cfg["surface_tension_Npm"],
        "viscosity_m2ps": cfg["viscosity_m2ps"],
        "density_kgpm3": cfg["density_kgpm3"],
        "gravity_mps2": cfg["gravity_mps2"],
        "camera": cfg["camera"],
    }
    write_case_txt(iter_dir / "case.txt", case)

    # case.json (richer snapshot for postprocess + history)
    (iter_dir / "case.json").write_text(json.dumps({
        **case,
        "test_id": test_id,
        "positive_bbox_m": targets["positive_bbox_m"],
        "negative_bbox_m": targets["negative_bbox_m"],
        "module_bboxes_m": [],
        "score_slab_thickness_m": cfg["score_slab_thickness_m"],
        "fluid_threshold": cfg.get("fluid_threshold", 0.5),
        "n_nozzles": len(nozzles_m),
        "nozzle_LPM": cfg["nozzle_LPM"],
        "nozzle_vz_mps": vz,
        "total_Q_m3ps": len(nozzles_m) * cfg["nozzle_LPM"] * 1e-3 / 60.0,
        "thicken_thickness_m": cfg.get("thicken_thickness_m"),
        "collider_stl_source": str(stl_path).replace("\\", "/"),
    }, indent=2), encoding="utf-8")

    # Run FluidX3D (interactive uses the GUI variant — viz only, no PNG/VTK)
    exe = FLUIDX3D_EXE_INTERACTIVE if interactive else FLUIDX3D_EXE
    if not exe.exists():
        raise FileNotFoundError(f"{exe} missing. Run scripts/build_fluidx3d.py to build it.")
    mode_tag = "INTERACTIVE" if interactive else "PNG"
    print(f"[{test_id}] FluidX3D launching ({mode_tag} mode, dp={cfg['dp_m']} timemax={cfg['timemax_s']} side_walls={cfg['side_walls']})")
    # PNG mode: suppress console window (fully background); INTERACTIVE: keep window
    creation_flags = 0
    startup_info = None
    if not interactive and sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = 0  # SW_HIDE
    t0 = time.time()
    proc = subprocess.run([str(exe)],
                          cwd=str(iter_dir),
                          capture_output=True, text=True, timeout=timeout_s,
                          creationflags=creation_flags, startupinfo=startup_info)
    wall = time.time() - t0
    (iter_dir / "fx3d_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (iter_dir / "fx3d_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    print(f"[{test_id}] wall={wall:.1f}s rc={proc.returncode}")
    if proc.returncode != 0:
        print("  stdout tail:\n  " + "\n  ".join(proc.stdout.splitlines()[-10:]))

    # Interactive mode: no PNG/VTK output, so skip postprocess + DB + Rhino push.
    if interactive:
        print(f"  (interactive run — DB skipped)")
        return {"engine": "fluidx3d-interactive", "wall_time_s": round(wall, 1),
                 "score": None, "retention": {}}

    # Postprocess
    result = postprocess(iter_dir)
    result["wall_time_s"] = round(wall, 1)
    (iter_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    r = result["retention"]
    print(f"  score={result['score']:.4f}  in_pos={r['in_positive']}  in_neg={r['in_negative']}  "
          f"in_col={r['in_column']}  splash={r['splash']}  total={r['total']}")

    # Settings DB
    cfg_for_log = {**cfg,
                   "_n_nozzles": len(nozzles_m),
                   "_nozzle_vz": vz,
                   "_total_Q_m3ps": len(nozzles_m) * cfg["nozzle_LPM"] * 1e-3 / 60.0}
    append_settings_log(test_id, case, result, iter_dir, wall, stl_path, cfg_for_log)

    # Dashboard (Flask) serves DB live -- no static refresh needed.

    # Rhino push
    if push_rhino:
        try:
            push_stl_to_rhino_layer(local_stl, f"fluidx3d::{test_id}::sculpture",
                                    (140, 90, 30), obj_name=f"sculpture_{test_id}")
        except Exception as e:
            print(f"  (Rhino push skipped: {e})")

    return result


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test-id", default="real", help="iter_<test_id>/ directory under runs/")
    ap.add_argument("--stl", default=None, help="path to collider STL (default: _real_collider_thickened.stl)")
    ap.add_argument("--config", default=str(CONFIG_DEFAULT), help="canonical config json")
    ap.add_argument("--no-push", action="store_true", help="skip Rhino push")
    ap.add_argument("--interactive", action="store_true", help="use FluidX3D_interactive.exe (GUI window, no DB)")
    args = ap.parse_args(argv[1:])

    stl_path = Path(args.stl).resolve() if args.stl else None
    config_path = Path(args.config).resolve()
    run_experiment(args.test_id,
                    stl_path=stl_path,
                    config_path=config_path,
                    push_rhino=False if args.no_push else None,
                    interactive=args.interactive)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
