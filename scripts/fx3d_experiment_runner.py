"""Run one FluidX3D experiment for the LFTH_CFD sculpture pipeline.

End-to-end per experiment:
  1. Build sculpture STL (binary) via module_geometry.build_modules_combined_stl.
  2. Compute domain bbox from sculpture bbox + padding; collect inflow location
     from runs/_real_geom.json (nozzle centroid + z_top).
  3. Write case.txt that the FluidX3D generic binary reads from cwd.
  4. Run FluidX3D.exe with cwd = iter_dir.
  5. Run fx3d_postprocess to make result.json (retention + touch metrics).
  6. Push sculpture STL into Rhino layer fluidx3d::<test_id>::sculpture.
  7. Return result dict.

Usage as a library (called by ga_sequential.evaluate or similar):
    from fx3d_experiment_runner import run_experiment
    run_experiment(test_id, params_dict)

Usage as a CLI:
    python scripts/fx3d_experiment_runner.py experiments/test_22.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from module_geometry import build_modules_combined_stl
from rhino_mcp_helpers import push_stl_to_rhino_layer
from fx3d_postprocess import postprocess

RUNS = PROJECT / "runs"
EXPERIMENTS_DIR = PROJECT / "experiments"
GEOM_JSON = RUNS / "_real_geom.json"
MODULES_JSON = RUNS / "_collider_modules.json"

FLUIDX3D_EXE = Path("C:/Users/user/Downloads/FluidX3D/bin/FluidX3D.exe")

DOMAIN_PAD_M = 3.0           # padding around sculpture bbox in each direction
DEFAULT_DP_M = 0.08
DEFAULT_TIMEMAX_S = 4.0
DEFAULT_DT_OUT_S = 0.05
DEFAULT_INFLOW_RADIUS_M = 1.0
DEFAULT_INFLOW_Z_TOP_M  = 30.0
DEFAULT_INFLOW_VEL_MPS  = -4.0
INFLOW_COLUMN_HEIGHT_M  = 6.0


def _load_modules_info() -> list[dict]:
    return json.loads(MODULES_JSON.read_text(encoding="utf-8"))["modules"]


def _load_nozzle_centroid_m() -> tuple[float, float, float]:
    geom = json.loads(GEOM_JSON.read_text(encoding="utf-8"))
    holes = geom.get("nozzle_holes_m", [])
    if not holes:
        return 4.0, -2.4, 29.0
    arr = np.asarray(holes, dtype=float)
    return float(arr[:, 0].mean()), float(arr[:, 1].mean()), float(arr[:, 2].max())


def _module_bboxes_m(modules_info: list[dict],
                     per_module_bboxes_m: list[dict] | None = None) -> list[list[list[float]]]:
    """Prefer per-module sculpture bboxes (from build_modules_combined_stl) over
    static collider bboxes."""
    if per_module_bboxes_m:
        out = []
        for m in sorted(per_module_bboxes_m, key=lambda x: x.get("index", 0)):
            out.append([list(m["bbox_m"][0]), list(m["bbox_m"][1])])
        return out
    out = []
    for m in sorted(modules_info, key=lambda x: x["index"]):
        (x0, y0, z0), (x1, y1, z1) = m["bbox_mm"]
        out.append([[x0 / 1000.0, y0 / 1000.0, z0 / 1000.0],
                    [x1 / 1000.0, y1 / 1000.0, z1 / 1000.0]])
    return out


def _domain_from_sculpture(bbox_m: list[list[float]],
                           pad_m: float) -> tuple[list[float], list[float]]:
    (x0, y0, z0), (x1, y1, z1) = bbox_m
    lo = [x0 - pad_m, y0 - pad_m, 0.0]                 # floor at z=0
    hi = [x1 + pad_m, y1 + pad_m, max(z1, 29.0) + pad_m]
    return lo, hi


def _write_case_txt(case_path: Path, payload: dict) -> None:
    lines = []
    for k, v in payload.items():
        if isinstance(v, (list, tuple)):
            lines.append(k + " " + " ".join(f"{x:.6f}" for x in v))
        elif isinstance(v, str):
            lines.append(f"{k} {v}")
        elif isinstance(v, bool):
            lines.append(f"{k} {1 if v else 0}")
        else:
            lines.append(f"{k} {v}")
    case_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_experiment(test_id: str, params: dict, *,
                   push_to_rhino: bool = True,
                   timeout_s: float = 600.0) -> dict:
    """Run a single experiment end-to-end. Returns result dict (also written to
    runs/iter_<test_id>/result.json)."""
    iter_dir = RUNS / f"iter_{test_id}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "fx3d_out" / "frames").mkdir(parents=True, exist_ok=True)
    (iter_dir / "fx3d_out" / "vtk").mkdir(parents=True, exist_ok=True)

    # 1) sculpture STL
    modules_info = _load_modules_info()
    genes_by_index = {m["index"]: m for m in params.get("modules", [])}
    stl_path = iter_dir / "sculpture.stl"
    stl_info = build_modules_combined_stl(modules_info, genes_by_index, stl_path)
    m = trimesh.load(stl_path)
    bbox_m = m.bounds.tolist()
    stl_info["bbox_m"] = bbox_m

    # 2) domain + inflow
    glb = params.get("global", {})
    dp_m = float(glb.get("dp", DEFAULT_DP_M))
    timemax_s = float(glb.get("timemax", DEFAULT_TIMEMAX_S))
    dt_out_s = float(glb.get("timeout", DEFAULT_DT_OUT_S))
    inflow_r = float(glb.get("inflow_radius_m", DEFAULT_INFLOW_RADIUS_M))
    inflow_z_top = float(glb.get("inflow_z_top_m", DEFAULT_INFLOW_Z_TOP_M))
    inflow_v = float(glb.get("inflow_velocity_mps", DEFAULT_INFLOW_VEL_MPS))

    cx, cy, _ = _load_nozzle_centroid_m()
    inflow_z_bot = inflow_z_top - INFLOW_COLUMN_HEIGHT_M
    domain_lo, domain_hi = _domain_from_sculpture(bbox_m, DOMAIN_PAD_M)
    domain_hi[2] = max(domain_hi[2], inflow_z_top + 1.0)

    module_bboxes_m = _module_bboxes_m(modules_info, stl_info.get("per_module"))
    # pond = floor slab spanning combined sculpture footprint
    pond_bbox_m = [[domain_lo[0], domain_lo[1], 0.0],
                   [domain_hi[0], domain_hi[1], 0.5]]

    # 3) case.txt
    case = {
        "stl_path": str(stl_path).replace("\\", "/"),
        "out_dir": str(iter_dir / "fx3d_out").replace("\\", "/") + "/",
        "domain_bbox_m": domain_lo + domain_hi,
        "dp_m": dp_m,
        "timemax_s": timemax_s,
        "dt_out_s": dt_out_s,
        "inflow_center_m": [cx, cy, inflow_z_bot],
        "inflow_radius_m": inflow_r,
        "inflow_z_top_m": inflow_z_top,
        "inflow_velocity_mps": inflow_v,
        "camera": [20.0, 15.0, 60.0, 1.0],
    }
    _write_case_txt(iter_dir / "case.txt", case)

    # also persist a richer case.json for postprocess + replay
    (iter_dir / "case.json").write_text(json.dumps({
        **case,
        "pond_bbox_m": pond_bbox_m,
        "module_bboxes_m": module_bboxes_m,
        "test_id": test_id,
        "stl_info": stl_info,
    }, indent=2), encoding="utf-8")

    # 4) FluidX3D
    t0 = time.time()
    proc = subprocess.run([str(FLUIDX3D_EXE)],
                          cwd=str(iter_dir),
                          capture_output=True, text=True, timeout=timeout_s)
    wall_s = time.time() - t0
    (iter_dir / "fx3d_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (iter_dir / "fx3d_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    if proc.returncode != 0:
        print(f"FluidX3D returncode={proc.returncode}, see fx3d_stderr.log")

    # 5) postprocess
    result = postprocess(iter_dir)
    result["wall_time_s"] = round(wall_s, 1)
    (iter_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    r = result["retention"]
    print(f"[{test_id}] wall={wall_s:.1f}s  total={r['total']}  in_pond={r['in_pond']}  "
          f"in_column={r['in_column']}  splash={r['splash']}  retention={r['retention_rate']:.3f}")

    # 6) Rhino push
    if push_to_rhino:
        layer = f"fluidx3d::{test_id}::sculpture"
        push_stl_to_rhino_layer(stl_path, layer, (140, 90, 30), obj_name=f"sculpture_{test_id}")

    return result


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    params_path = Path(argv[1]).resolve()
    if not params_path.is_file():
        print(f"ERROR: {params_path} not found")
        return 1
    params = json.loads(params_path.read_text(encoding="utf-8"))
    test_id = params.get("test_id") or params_path.stem
    run_experiment(test_id, params)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
