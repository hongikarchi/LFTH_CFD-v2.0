"""Sweep 8x10 Rhino grid with sequential tests.

For each grid cell (gen 1..10, var 1..8):
  - random valid M0 params, defaults for M1-M3
  - run experiment_runner
  - quality check: leak/prefill/sloshing/wall_time
  - stop and report on first failure

Usage:  python scripts/run_grid_tests.py [--seed 123] [--start_iter 1]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from ga_sequential import _enforce_couplings, GENE_ORDER, BOUNDS
from module_geometry import DEFAULTS

EXPERIMENTS = PROJECT / "experiments"
RUNS = PROJECT / "runs"
BATCH_LAYER = "test_01"   # overridden by --batch in main()

# Cache base_point_mm per module (M0..M3) for nozzle-aligned tx/ty bias.
_modules_json = json.loads((PROJECT / "runs" / "_collider_modules.json")
                            .read_text(encoding="utf-8"))
MODULES_BASE = {int(m["index"]): m["base_point_mm"] for m in _modules_json["modules"]}


def next_test_n() -> int:
    nums = []
    for p in EXPERIMENTS.glob("test_*.json"):
        try:
            nums.append(int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return (max(nums) + 1) if nums else 1


def quality_check(test_id: str) -> list[str]:
    iter_dir = RUNS / f"iter_{test_id}"
    r = json.loads((iter_dir / "result.json").read_text(encoding="utf-8"))
    trails = json.loads((iter_dir / "trails.json").read_text(encoding="utf-8"))

    issues = []
    wt = r.get("wall_time_s", 999)
    if wt > 180:
        issues.append(f"wall={wt}s>180")

    # Leak = a PREFILL particle (z0 < 28; nozzles sit at z=29) ended near or
    # below the pond floor. Nozzle particles that splash sideways are not
    # leaks; they're just lost water.
    leak = sum(1 for v in trails.values()
               if v and v[0][2] < 28 and v[-1][2] < 0.5)
    if leak > 0:
        issues.append(f"prefill_leak={leak}")

    # Nozzle bounce: a nozzle particle that fell more than 1 m and then came
    # back up by > 1 m indicates an SPH pressure-wave artifact (trampoline off
    # the prefill water surface). Physical splashes don't return to the
    # original height in 5 s.
    bouncers = 0
    for v in trails.values():
        if not v or v[0][2] < 28.5:
            continue
        zs = [p[2] for p in v]
        min_z = min(zs)
        min_i = zs.index(min_z)
        max_after = max(zs[min_i + 1:]) if min_i < len(zs) - 1 else min_z
        if (v[0][2] - min_z) > 1.0 and (max_after - min_z) > 1.0:
            bouncers += 1
    if bouncers > 5:    # tolerate up to ~5/58 = 9% noise
        issues.append(f"nozzle_bounce={bouncers}")

    # Prefill present. Threshold tied to the cap depth — narrow/tilted caps
    # naturally hold less water. Below 200 is "no water at all".
    total = r.get("retention", {}).get("total", 0)
    if total <= 200:
        issues.append(f"no_prefill={total}")

    # Sloshing (PREFILL particles only — nozzle particles can naturally splash
    # off colliders, that's correct physics).  Prefill particles start with
    # z0 < 28 m (the nozzles sit at z=29 m).
    max_lat = 0.0
    for v in trails.values():
        if not v:
            continue
        x0, y0, z0 = v[0]
        if z0 >= 28.0:
            continue
        for p in v:
            d = math.hypot(p[0] - x0, p[1] - y0)
            if d > max_lat:
                max_lat = d
    if max_lat > 5:
        issues.append(f"prefill_sloshing={max_lat:.1f}m")

    return issues


def make_params(test_id: str, gen_idx: int, var_idx: int,
                rng: random.Random) -> dict:
    """Each test gets independently-random params for ALL 4 modules so the
    cascading sculpture actually interacts with the nozzle stream. tx/ty are
    biased so the cap sits under the nozzle column (xy ~ (4, -3)) plus some
    spread for variety."""
    # nozzle xy centre (from runs/_real_geom.json: ~4.13, -3.20)
    NOZZLE_X_MM = 4130.0
    NOZZLE_Y_MM = -3201.0

    modules = []
    for m_idx in range(4):
        ind = [rng.uniform(*BOUNDS[g]) for g in GENE_ORDER]
        ind = _enforce_couplings(ind)
        d = {GENE_ORDER[j]: ind[j] for j in range(len(GENE_ORDER))}

        # Stack all 4 caps directly under the nozzle column with mild jitter
        # (+/-300 mm) so each cap reliably catches water that overflows from
        # the cap above. Tighter bias (+/-150) did NOT reduce far_ends.
        base = MODULES_BASE[m_idx]
        d["tx"] = rng.uniform(-300, 300) + (NOZZLE_X_MM - base[0])
        d["ty"] = rng.uniform(-300, 300) + (NOZZLE_Y_MM - base[1])
        d["tz"] = rng.uniform(-200, 200)
        modules.append({"index": m_idx, **d})

    return {
        "test_id": test_id,
        "note": f"grid sweep gen_{gen_idx:02d}-var_{var_idx:02d}",
        "global": {
            "v_inlet": 0.0, "dp": 0.20, "timemax": 10.0, "viscobound": 1.2,
            "nozzle_diameter": 0.20, "nozzle_thickness": 0.20,
            "cflnumber": 0.30, "use_gpu": True, "timeout": 0.05,
        },
        "prefill": True,
        "grid_cell": {"gen": gen_idx, "var": var_idx},
        "batch_layer": BATCH_LAYER,
        "modules": modules,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=123)
    ap.add_argument("--start_iter", type=int, default=1)
    ap.add_argument("--end_iter", type=int, default=80,
                     help="stop after this iter (inclusive). e.g. 8 = one generation.")
    ap.add_argument("--batch", default="test_01",
                     help="parent layer in Rhino (e.g. test_01, test_02). All "
                          "cells of this sweep go onto batch::collider and "
                          "batch::stream with per-cell Object.Name.")
    ap.add_argument("--stop_on_fail", action="store_true",
                     help="stop the sweep on the first QC failure")
    args = ap.parse_args()

    global BATCH_LAYER
    BATCH_LAYER = args.batch
    rng = random.Random(args.seed)
    # Burn the RNG up to start_iter so re-runs are deterministic
    for _ in range((args.start_iter - 1) * len(GENE_ORDER)):
        rng.uniform(0, 1)

    failed = []
    t_global0 = time.time()
    for i in range(args.start_iter, args.end_iter + 1):
        gen_idx = ((i - 1) // 8) + 1
        var_idx = ((i - 1) % 8) + 1
        test_n = next_test_n()
        test_id = f"test_{test_n:02d}"

        params = make_params(test_id, gen_idx, var_idx, rng)
        path = EXPERIMENTS / f"{test_id}.json"
        path.write_text(json.dumps(params, indent=2), encoding="utf-8")

        elapsed_min = (time.time() - t_global0) / 60.0
        print(f"\n=== iter {i}/80  {test_id} -> "
              f"gen_{gen_idx:02d}-var_{var_idx:02d}  (elapsed {elapsed_min:.1f}m) ===")
        t0 = time.time()
        rc = subprocess.run([sys.executable,
                              str(PROJECT / "scripts" / "experiment_runner.py"),
                              str(path)]).returncode
        dt = time.time() - t0
        if rc != 0:
            print(f"  RUN FAILED ({dt:.0f}s)")
            failed.append((test_id, "run_failed"))
            if args.stop_on_fail:
                break
            continue

        issues = quality_check(test_id)
        if issues:
            print(f"  QC FAIL: {', '.join(issues)}")
            failed.append((test_id, ",".join(issues)))
            if args.stop_on_fail:
                break
        else:
            print(f"  QC PASS ({dt:.0f}s)")

    print(f"\n=== SWEEP DONE: {len(failed)} failures of {i - args.start_iter + 1} runs ===")
    for tid, why in failed:
        print(f"  {tid}: {why}")


if __name__ == "__main__":
    main()
