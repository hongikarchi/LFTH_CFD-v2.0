"""Run the REAL Rhino sculpture (runs/_real_sculpture.stl) through FluidX3D.

Not a parametric GA case — uses the as-built designer geometry directly.
Output: runs/iter_real/ with case.txt + case.json + fx3d_out/{frames,vtk}/
        + result.json.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import trimesh

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from fx3d_postprocess import postprocess
from rhino_mcp_helpers import push_stl_to_rhino_layer

FLUIDX3D_EXE = Path("C:/Users/user/Downloads/FluidX3D/bin/FluidX3D.exe")

REAL_STL = PROJECT / "runs" / "_real_sculpture.stl"
ITER_DIR = PROJECT / "runs" / "iter_real"
GEOM_JSON = PROJECT / "runs" / "_real_geom.json"
MODULES_JSON = PROJECT / "runs" / "_collider_modules.json"


def main() -> int:
    if not REAL_STL.exists():
        print(f"ERROR: {REAL_STL} missing"); return 1

    ITER_DIR.mkdir(parents=True, exist_ok=True)
    (ITER_DIR / "fx3d_out" / "frames").mkdir(parents=True, exist_ok=True)
    (ITER_DIR / "fx3d_out" / "vtk").mkdir(parents=True, exist_ok=True)

    # copy sculpture into iter dir so case.txt path is absolute + stable
    stl_local = ITER_DIR / "sculpture.stl"
    shutil.copy(REAL_STL, stl_local)
    m = trimesh.load(stl_local)
    bbox = m.bounds  # 2x3 in meters
    print(f"sculpture bbox m: {bbox.tolist()}  faces={len(m.faces)}")

    geom = json.loads(GEOM_JSON.read_text(encoding="utf-8"))
    nh = geom["nozzle_holes_m"]
    cx = sum(h[0] for h in nh) / len(nh)
    cy = sum(h[1] for h in nh) / len(nh)
    nz = max(h[2] for h in nh)
    print(f"nozzle centroid: ({cx:.2f}, {cy:.2f}), z_top={nz:.2f} ({len(nh)} holes)")

    # Domain: clip floor to z=0 (pond), reach above inflow with margin
    pad = 3.0
    domain_lo = [bbox[0, 0] - pad, bbox[0, 1] - pad, 0.0]
    domain_hi = [bbox[1, 0] + pad, bbox[1, 1] + pad, nz + 2.0]
    print(f"domain m: lo={domain_lo} hi={domain_hi}")

    dp = 0.08
    timemax = 6.0
    dt_out = 0.05
    inflow_r = 1.0
    inflow_z_top = nz
    inflow_z_bot = nz - 4.0
    inflow_v = -4.0

    # case.txt for FluidX3D binary
    case_txt = ITER_DIR / "case.txt"
    case_txt.write_text(
        f"stl_path {stl_local.as_posix()}\n"
        f"out_dir {(ITER_DIR / 'fx3d_out').as_posix()}/\n"
        f"domain_bbox_m {' '.join(f'{v:.6f}' for v in domain_lo + domain_hi)}\n"
        f"dp_m {dp}\n"
        f"timemax_s {timemax}\n"
        f"dt_out_s {dt_out}\n"
        f"inflow_center_m {cx:.6f} {cy:.6f} {inflow_z_bot:.6f}\n"
        f"inflow_radius_m {inflow_r}\n"
        f"inflow_z_top_m {inflow_z_top}\n"
        f"inflow_velocity_mps {inflow_v}\n"
        f"camera 20 15 60 1\n",
        encoding="utf-8",
    )

    # case.json for postprocess (pond + module bboxes)
    cm = json.loads(MODULES_JSON.read_text(encoding="utf-8"))
    module_bboxes_m = []
    for mod in sorted(cm["modules"], key=lambda x: x["index"]):
        (x0, y0, z0), (x1, y1, z1) = mod["bbox_mm"]
        module_bboxes_m.append([[x0 / 1000.0, y0 / 1000.0, z0 / 1000.0],
                                [x1 / 1000.0, y1 / 1000.0, z1 / 1000.0]])
    pond_bbox_m = [[domain_lo[0], domain_lo[1], 0.0],
                   [domain_hi[0], domain_hi[1], 0.5]]
    (ITER_DIR / "case.json").write_text(json.dumps({
        "test_id": "real",
        "stl_path": stl_local.as_posix(),
        "out_dir": (ITER_DIR / "fx3d_out").as_posix() + "/",
        "domain_bbox_m": domain_lo + domain_hi,
        "dp_m": dp,
        "timemax_s": timemax,
        "dt_out_s": dt_out,
        "inflow_center_m": [cx, cy, inflow_z_bot],
        "inflow_radius_m": inflow_r,
        "inflow_z_top_m": inflow_z_top,
        "inflow_velocity_mps": inflow_v,
        "pond_bbox_m": pond_bbox_m,
        "module_bboxes_m": module_bboxes_m,
    }, indent=2), encoding="utf-8")

    print(f"running FluidX3D ({FLUIDX3D_EXE.name}) in {ITER_DIR}...")
    t0 = time.time()
    proc = subprocess.run([str(FLUIDX3D_EXE)],
                          cwd=str(ITER_DIR),
                          capture_output=True, text=True, timeout=900)
    wall = time.time() - t0
    (ITER_DIR / "fx3d_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (ITER_DIR / "fx3d_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    print(f"  wall={wall:.1f}s returncode={proc.returncode}")
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-10:])
        print(f"  stdout tail:\n{tail}")
        return 1

    result = postprocess(ITER_DIR)
    result["wall_time_s"] = round(wall, 1)
    (ITER_DIR / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    r = result["retention"]
    print(f"  total={r['total']}  in_pond={r['in_pond']}  in_column={r['in_column']}  "
          f"splash={r['splash']}  retention={r['retention_rate']:.3f}")
    print(f"  on_module={r['on_module']}  touch_all_ratio={result['touch']['touch_all_ratio']:.3f}")

    push_stl_to_rhino_layer(stl_local, "fluidx3d::real::sculpture",
                            (140, 90, 30), obj_name="sculpture_real")
    print(f"\nFrames: {ITER_DIR / 'fx3d_out' / 'frames'}")
    print(f"VTK:    {ITER_DIR / 'fx3d_out' / 'vtk'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
