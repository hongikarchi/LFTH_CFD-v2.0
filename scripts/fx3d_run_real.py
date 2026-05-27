"""Run the REAL Rhino sculpture (runs/_real_collider.stl) through FluidX3D
with all 230 nozzles and the env::positive / env::negative bboxes from
runs/_real_targets.json (written by scripts/extract_targets.py).

Single-case driver (not GA). Useful as a baseline / regression check.
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

from fx3d_experiment_runner import (
    FLUIDX3D_EXE, DOMAIN_PAD_M, DEFAULT_DP_M, DEFAULT_TIMEMAX_S,
    DEFAULT_DT_OUT_S, DEFAULT_NOZZLE_LPM, DEFAULT_SLAB_THICKNESS_M,
    _load_targets, _domain_from_sculpture, _write_case_txt,
    nozzle_vz_from_lpm, write_nozzles_txt, append_settings_log,
)
from fx3d_postprocess import postprocess
from rhino_mcp_helpers import push_stl_to_rhino_layer

REAL_STL = PROJECT / "runs" / "_real_collider.stl"
ITER_DIR = PROJECT / "runs" / "iter_real"


def main() -> int:
    if not REAL_STL.exists():
        print(f"ERROR: {REAL_STL} missing — run scripts/extract_targets.py first")
        return 1

    targets = _load_targets()

    ITER_DIR.mkdir(parents=True, exist_ok=True)
    (ITER_DIR / "fx3d_out" / "frames").mkdir(parents=True, exist_ok=True)
    (ITER_DIR / "fx3d_out" / "vtk").mkdir(parents=True, exist_ok=True)

    stl_local = ITER_DIR / "sculpture.stl"
    shutil.copy(REAL_STL, stl_local)
    m = trimesh.load(stl_local)
    bbox = m.bounds.tolist()
    print(f"sculpture bbox m: {bbox}  faces={len(m.faces)}")

    nozzles_m = targets["nozzles_m"]
    nozzle_z_top = max(p[2] for p in nozzles_m)
    print(f"nozzles: {len(nozzles_m)} @ z_top={nozzle_z_top:.2f} m")

    dp = DEFAULT_DP_M
    timemax = DEFAULT_TIMEMAX_S
    dt_out = DEFAULT_DT_OUT_S
    lpm = DEFAULT_NOZZLE_LPM
    slab = DEFAULT_SLAB_THICKNESS_M
    vz = nozzle_vz_from_lpm(lpm, dp)
    print(f"per-nozzle vz: {vz:.3f} m/s (from {lpm} LPM, dp={dp})")

    domain_lo, domain_hi = _domain_from_sculpture(bbox, DOMAIN_PAD_M)
    domain_hi[2] = max(domain_hi[2], nozzle_z_top + 1.0)
    print(f"domain m: lo={domain_lo} hi={domain_hi}")

    write_nozzles_txt(ITER_DIR / "nozzles.txt", nozzles_m, vz)

    case = {
        "stl_path": stl_local.as_posix(),
        "out_dir": (ITER_DIR / "fx3d_out").as_posix() + "/",
        "nozzles_file": (ITER_DIR / "nozzles.txt").as_posix(),
        "domain_bbox_m": domain_lo + domain_hi,
        "dp_m": dp,
        "timemax_s": timemax,
        "dt_out_s": dt_out,
        "camera": [20.0, 15.0, 60.0, 1.0],
    }
    _write_case_txt(ITER_DIR / "case.txt", case)

    (ITER_DIR / "case.json").write_text(json.dumps({
        **case,
        "test_id": "real",
        "positive_bbox_m": targets["positive_bbox_m"],
        "negative_bbox_m": targets["negative_bbox_m"],
        "module_bboxes_m": [],
        "score_slab_thickness_m": slab,
        "n_nozzles": len(nozzles_m),
        "nozzle_LPM": lpm,
        "nozzle_vz_mps": vz,
    }, indent=2), encoding="utf-8")

    print(f"running {FLUIDX3D_EXE.name} in {ITER_DIR}...")
    t0 = time.time()
    proc = subprocess.run([str(FLUIDX3D_EXE)],
                          cwd=str(ITER_DIR),
                          capture_output=True, text=True, timeout=1800)
    wall = time.time() - t0
    (ITER_DIR / "fx3d_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (ITER_DIR / "fx3d_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    print(f"  wall={wall:.1f}s returncode={proc.returncode}")
    if proc.returncode != 0:
        print("  stdout tail:\n  " + "\n  ".join(proc.stdout.splitlines()[-10:]))
        return 1

    result = postprocess(ITER_DIR)
    result["wall_time_s"] = round(wall, 1)
    (ITER_DIR / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    r = result["retention"]
    print(f"  score={result['score']:.4f}  in_pos={r['in_positive']}  in_neg={r['in_negative']}  "
          f"in_col={r['in_column']}  splash={r['splash']}  total={r['total']}")

    case_json = json.loads((ITER_DIR / "case.json").read_text(encoding="utf-8"))
    append_settings_log("real", case_json, result, ITER_DIR, wall, collider_stl=stl_local)
    try:
        subprocess.run([sys.executable,
                        str(PROJECT / "scripts" / "update_settings_compare.py")],
                        capture_output=True, timeout=30)
    except Exception:
        pass

    try:
        push_stl_to_rhino_layer(stl_local, "fluidx3d::real::sculpture",
                                (140, 90, 30), obj_name="sculpture_real")
    except Exception as e:
        print(f"  (Rhino push skipped: {e})")

    print(f"\nFrames: {ITER_DIR / 'fx3d_out' / 'frames'}")
    print(f"VTK:    {ITER_DIR / 'fx3d_out' / 'vtk'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
