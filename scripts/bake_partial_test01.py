"""Bake partial test_01 simulation results into Rhino (sim was stopped early).

Reads the BI4 files DSPH has already written (~5.6 s sim time), converts to
CSV via PartVTK, builds trails, classifies, and pushes the streamline
polylines + collider STL into Rhino on the test_01 layer.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from run_case import PARTVTK
from run_streamline import parse_csv_with_idp
from experiment_runner import (
    classify_trails, touch_metric,
    push_polylines, push_stl_to_rhino_layer,
)


def main():
    iter_dir = PROJECT / "runs" / "iter_test_01"
    out_dir = iter_dir / "Out"
    data_dir = out_dir / "data"
    stl_path = iter_dir / "sculpture.stl"
    params = json.loads((PROJECT / "experiments" / "test_01.json").read_text(encoding="utf-8"))
    geom = json.loads((PROJECT / "runs" / "_real_geom.json").read_text(encoding="utf-8"))
    modules_info = json.loads((PROJECT / "runs" / "_collider_modules.json")
                               .read_text(encoding="utf-8"))["modules"]
    pond_min = geom["pond_bbox_m"][0]; pond_max = geom["pond_bbox_m"][1]
    dp = params.get("global", {}).get("dp", 0.04)

    n_parts = len(list(data_dir.glob("Part_*.bi4")))
    print(f"[1/4] PartVTK on {n_parts} BI4 files...")
    t0 = time.time()
    proc = subprocess.run([
        str(PARTVTK), "-dirdata", str(data_dir), "-savecsv",
        str(out_dir / "PartFluid"), "-onlytype:-all,+fluid",
    ], cwd=str(iter_dir), capture_output=True, text=True, timeout=300)
    print(f"  PartVTK exit={proc.returncode} ({time.time()-t0:.1f}s)")
    if proc.returncode != 0:
        print(proc.stdout[-500:]); print(proc.stderr[-300:])

    print("[2/4] parsing CSVs into trails")
    csvs = sorted(out_dir.glob("PartFluid_*.csv"))
    trails: dict = {}
    for cp in csvs:
        for idp, x, y, z in parse_csv_with_idp(cp):
            trails.setdefault(idp, []).append((x, y, z))
    (iter_dir / "trails.json").write_text(
        json.dumps({str(k): v for k, v in trails.items()}), encoding="utf-8")
    print(f"  {len(trails)} unique particles, {len(csvs)} csv frames")

    print("[3/4] classify + touch metric")
    pond_top_z = 0.5 + 3 * dp
    classify = classify_trails(
        trails,
        [pond_min[0], pond_min[1], 0.0],
        [pond_max[0], pond_max[1], 0.0],
        pond_top_z,
    )
    # touch metric uses genes_by_index for tz offsets; build from params
    genes_by_index = {int(m["index"]): {"tz": m.get("tz", 0.0)} for m in params["modules"]}
    touch = touch_metric(trails, modules_info, half_slab_m=3.0,
                          genes_by_index=genes_by_index)

    result = {
        "test_id": "test_01",
        "partial": True,
        "sim_time_s_reached": 5.6,
        **classify,
        "touch": touch,
        "stl_triangles": 16384,
        "params": params,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (iter_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  caught={classify['caught']}  moved={classify['moved']}  "
          f"stuck={classify['stuck']}  total={classify['total']}")
    print(f"  catch_rate={classify['catch_rate_moved']}  "
          f"touch_all={touch['touch_all_ratio']:.3f}  per_slab={touch['per_slab_touch']}")

    print("[4/4] push to Rhino")
    print("  collider STL -> test_01::collider_01")
    push_stl_to_rhino_layer(stl_path, "test_01::collider_01", (140, 90, 30))
    print("  streamlines -> test_01::stream")
    push_polylines({k: v for k, v in trails.items()}, geom["nozzle_holes_m"], "test_01")
    print("done.")


if __name__ == "__main__":
    main()
