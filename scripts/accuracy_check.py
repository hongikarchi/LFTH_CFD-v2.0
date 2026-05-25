"""
Ranking-stability spot check: 3 param variants x 2 dp levels.
Verify that coarse dp preserves the relative ordering needed by the GA,
even if absolute fitness values differ.

Cube STL is a poor stand-in for a real sculpture (water bounces off
instead of being channeled), so absolute splash_ratio values here will
all be high. What matters is whether the rank order is consistent across
dp levels.

Run:
    python accuracy_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
from benchmark import run_bench, write_cube_stl, CUBE_STL

if not CUBE_STL.exists():
    write_cube_stl(CUBE_STL, size=0.5)

# Three different design parameter sets
VARIANTS = [
    ("centered_mid",     {"sculpture_size": 0.6, "sculpture_angle": 15.0, "sculpture_height": 0.7,
                          "nozzle_x": 0.0, "nozzle_y": 0.0, "nozzle_z": 2.0,
                          "nozzle_diameter": 0.04, "nozzle_angle_x": 0.0, "nozzle_angle_y": 0.0,
                          "flow_velocity": 3.0}),
    ("small_low",        {"sculpture_size": 0.3, "sculpture_angle": 0.0,  "sculpture_height": 0.5,
                          "nozzle_x": 0.0, "nozzle_y": 0.0, "nozzle_z": 2.0,
                          "nozzle_diameter": 0.04, "nozzle_angle_x": 0.0, "nozzle_angle_y": 0.0,
                          "flow_velocity": 2.0}),
    ("offset_tilted",    {"sculpture_size": 0.5, "sculpture_angle": 30.0, "sculpture_height": 1.0,
                          "nozzle_x": 0.2, "nozzle_y": 0.0, "nozzle_z": 2.0,
                          "nozzle_diameter": 0.04, "nozzle_angle_x": 15.0, "nozzle_angle_y": 0.0,
                          "flow_velocity": 4.0}),
]

DP_LEVELS = [
    ("dp0.020", 0.020, {"cflnumber": 0.35, "speedsound": 60, "DensityDT": 1}, 2),
    ("dp0.045", 0.045, {"cflnumber": 0.45, "speedsound": 40, "DensityDT": 0}, 2),
]


def main():
    # Override benchmark module globals via env to use a 5s sim (compromise for time)
    import os
    os.environ["BENCH_TIMEMAX"] = "5.0"
    os.environ["BENCH_TIMEOUT_SAVE"] = "0.5"
    import importlib, benchmark
    importlib.reload(benchmark)
    from benchmark import run_bench, PARAMS

    table = {}
    for v_name, v_params in VARIANTS:
        table[v_name] = {}
        for dp_label, dp, patches, layers in DP_LEVELS:
            # patch global params for this variant
            for k, v in v_params.items():
                PARAMS[k] = v
            label = f"{v_name}__{dp_label}"
            print(f"running {label} ...", end=" ", flush=True)
            t0 = time.time()
            r = run_bench(label, dp, patches, layers, use_cpu=True)
            dt = time.time() - t0
            if r.get("ok"):
                table[v_name][dp_label] = r["splash_ratio"]
                print(f"sim={r['t_sim_bench_s']}s splash={r['splash_ratio']} (wall {dt:.1f}s)")
            else:
                table[v_name][dp_label] = None
                print(f"FAIL")

    # Compare rankings
    print("\n=== RESULTS ===")
    print(f"{'variant':24s} {'dp0.020':>10s} {'dp0.045':>10s}")
    for v, dps in table.items():
        print(f"{v:24s} {dps.get('dp0.020', 'n/a'):>10} {dps.get('dp0.045', 'n/a'):>10}")

    print("\n=== RANKINGS (lower is better) ===")
    for dp_label in ("dp0.020", "dp0.045"):
        ranked = sorted(table.items(), key=lambda kv: kv[1].get(dp_label, 999))
        print(f"  {dp_label}: " + " < ".join(v for v, _ in ranked))

    # Save
    out = PROJECT / "runs" / "accuracy_check.json"
    out.write_text(json.dumps(table, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
