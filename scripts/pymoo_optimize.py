"""Multi-objective pymoo NSGA-II for sculpture water-cascade design.

Per-module sequential staging (top-down mod 0 -> 3). Each stage runs an
8-gene NSGA-II against a 2-objective Pareto front:

  f1 = splash_frac = (total - in_positive) / total       (min)
  f2 = -dist_from_nozzle_mm                              (min, == max dist)

Within a stage: fully automated. Between stages: user picks one Pareto
point interactively to freeze for that module.

Per-evaluation pipeline:
  gene -> experiments/test_NN.json
       -> build_modules_combined_stl -> iter_test_NN/_parametric_input.stl
       -> fx3d_run.run_experiment(stl_path=...) -> result.json + DB
       -> read retention -> (f1, f2)

State persisted to experiments/pymoo_state.json (resumable).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.callback import Callback
from pymoo.core.problem import ElementwiseProblem
from pymoo.core.repair import Repair
from pymoo.operators.crossover.sbx import SBX
from pymoo.operators.mutation.pm import PM
from pymoo.operators.sampling.lhs import LHS
from pymoo.optimize import minimize

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from module_geometry import (DEFAULTS, GENE_BOUNDS, GENE_ORDER,
                              build_modules_combined_stl)
from fx3d_run import run_experiment

# Inflow XY position (m) - source of truth: scripts/fx3d_visualize_in_rhino.py
INFLOW_CX_M = 4.0
INFLOW_CY_M = -2.4

EXPERIMENTS = PROJECT / "experiments"
RUNS = PROJECT / "runs"
STATE_FILE = EXPERIMENTS / "pymoo_state.json"
MODULES_JSON = RUNS / "_collider_modules.json"

# Used in coupling repair (same baseline as old ga_sequential.py)
DP_MM = 200.0
FAIL_F = [1.0, 0.0]  # worst splash + zero dist when a sim fails


# ---- coupling repair ---------------------------------------------------------

def _enforce_couplings(x: np.ndarray) -> np.ndarray:
    """Clip move_z + rotation_x to respect bowl-depth and water-holding.

    1. cap_height = radius - move_z must lie in [800, 2000] mm.
    2. rotation_x clipped so the rotated bowl can still hold water:
       cap_depth*cos(rx) - r_rim*sin(rx) >= 3*dp
    Finally clip every gene to its GENE_BOUNDS range.
    """
    x = np.asarray(x, dtype=float).copy()
    i_r = GENE_ORDER.index("radius")
    i_mz = GENE_ORDER.index("move_z")
    i_rx = GENE_ORDER.index("rotation_x")

    r = x[i_r]
    min_mz = r - 2000.0
    max_mz = r - 800.0
    if x[i_mz] < min_mz:
        x[i_mz] = min_mz
    elif x[i_mz] > max_mz:
        x[i_mz] = max_mz

    mz = x[i_mz]
    cap_depth = r - mz
    r_rim = math.sqrt(max(r * r - mz * mz, 1.0))
    K = 3.0 * DP_MM
    hyp = math.sqrt(cap_depth * cap_depth + r_rim * r_rim)
    phi = math.atan2(r_rim, cap_depth)
    if hyp <= K:
        max_rx = 0.0
    else:
        max_rx = math.degrees(math.acos(K / hyp) - phi)
    max_rx = max(max_rx, 0.0)
    if x[i_rx] > max_rx:
        x[i_rx] = max_rx
    if x[i_rx] < 0.0:
        x[i_rx] = 0.0

    for i, name in enumerate(GENE_ORDER):
        lo, hi = GENE_BOUNDS[name]
        if x[i] < lo:
            x[i] = lo
        elif x[i] > hi:
            x[i] = hi
    return x


class CouplingRepair(Repair):
    def _do(self, problem, X, **kwargs):
        for i in range(len(X)):
            X[i] = _enforce_couplings(X[i])
        return X


# ---- helpers -----------------------------------------------------------------

def next_test_nn() -> int:
    EXPERIMENTS.mkdir(parents=True, exist_ok=True)
    nums = []
    for p in EXPERIMENTS.glob("test_*.json"):
        try:
            nums.append(int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return (max(nums) + 1) if nums else 2  # test_01 reserved for sanity eval


def _default_module_gene() -> dict:
    """8-key dict for a not-yet-optimized module (no offset)."""
    return {**DEFAULTS, "tx": 0.0, "ty": 0.0, "tz": 0.0}


def _ind_to_dict(x: np.ndarray) -> dict:
    return {GENE_ORDER[i]: float(x[i]) for i in range(len(GENE_ORDER))}


def compose_params(stage_idx: int, ind_dict: dict, prior_bests: dict,
                   test_id: str, note: str) -> dict:
    modules = []
    for i in range(4):
        if i in prior_bests:
            cfg = prior_bests[i]
        elif i == stage_idx:
            cfg = ind_dict
        else:
            cfg = _default_module_gene()
        modules.append({"index": i, **cfg})
    return {
        "test_id": test_id,
        "note": note,
        "global": {
            "dp": 0.08,
            "timemax": 4.0,
            "timeout": 0.05,
            "inflow_radius_m": 1.0,
            "inflow_z_top_m": 30.0,
            "inflow_velocity_mps": -4.0,
            "use_gpu": True,
        },
        "modules": modules,
    }


def dist_from_nozzle_mm(stage_idx: int, ind_dict: dict,
                         modules_info: list) -> float:
    """Horizontal distance (mm) from the nozzle's vertical line to this
    module's post-offset XY anchor."""
    base = modules_info[stage_idx]["base_point_mm"]
    mx = base[0] + ind_dict["tx"]
    my = base[1] + ind_dict["ty"]
    nx = INFLOW_CX_M * 1000.0
    ny = INFLOW_CY_M * 1000.0
    return math.hypot(mx - nx, my - ny)


# ---- pymoo problem -----------------------------------------------------------

class SculptureProblem(ElementwiseProblem):
    def __init__(self, stage_idx: int, prior_bests: dict,
                 modules_info: list, ga_state: dict):
        xl = np.array([GENE_BOUNDS[g][0] for g in GENE_ORDER], dtype=float)
        xu = np.array([GENE_BOUNDS[g][1] for g in GENE_ORDER], dtype=float)
        super().__init__(n_var=8, n_obj=2, n_ieq_constr=0, xl=xl, xu=xu)
        self.stage_idx = stage_idx
        self.prior_bests = prior_bests
        self.modules_info = modules_info
        self.ga_state = ga_state

    def _evaluate(self, x, out, *args, **kwargs):
        ind_dict = _ind_to_dict(x)
        test_n = next_test_nn()
        test_id = f"test_{test_n:02d}"
        note = (f"pymoo stage{self.stage_idx} "
                f"r={ind_dict['radius']:.0f} mz={ind_dict['move_z']:.0f} "
                f"rx={ind_dict['rotation_x']:.1f} "
                f"rz={ind_dict['rotation_z']:.1f} "
                f"off={ind_dict['offset_dist']:.0f} "
                f"T=({ind_dict['tx']:.0f},{ind_dict['ty']:.0f},"
                f"{ind_dict['tz']:.0f})")

        # 1. experiments/test_NN.json
        params = compose_params(self.stage_idx, ind_dict, self.prior_bests,
                                 test_id, note)
        (EXPERIMENTS / f"{test_id}.json").write_text(
            json.dumps(params, indent=2), encoding="utf-8")

        # 2. build parametric STL
        iter_dir = RUNS / f"iter_{test_id}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        stl_in = iter_dir / "_parametric_input.stl"
        genes_by_index = {
            m["index"]: {k: v for k, v in m.items() if k != "index"}
            for m in params["modules"]
        }

        eval_entry = {
            "stage": self.stage_idx,
            "test_id": test_id,
            "ind": ind_dict,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.ga_state.setdefault("evaluations", []).append(eval_entry)
        STATE_FILE.write_text(json.dumps(self.ga_state, indent=2),
                                encoding="utf-8")

        try:
            build_modules_combined_stl(self.modules_info, genes_by_index,
                                        stl_in, also_save_viz=False)
        except Exception as e:
            print(f"  [{test_id}] STL build FAIL: {e}")
            eval_entry.update({"failed": True, "stage_fail": "stl_build",
                                "error": str(e), "F": FAIL_F})
            STATE_FILE.write_text(json.dumps(self.ga_state, indent=2),
                                    encoding="utf-8")
            out["F"] = list(FAIL_F)
            return

        # 3. FluidX3D run + postprocess + DB append (all inside run_experiment)
        print(f"  [{test_id}] stage{self.stage_idx} running ...",
              end=" ", flush=True)
        t0 = time.time()
        try:
            result = run_experiment(test_id, stl_path=stl_in)
        except Exception as e:
            dt = time.time() - t0
            print(f"FX3D FAIL ({dt:.0f}s): {e}")
            eval_entry.update({
                "completed": time.strftime("%Y-%m-%d %H:%M:%S"),
                "wall_s": round(dt, 1), "failed": True,
                "stage_fail": "fx3d_run", "error": str(e), "F": FAIL_F,
            })
            STATE_FILE.write_text(json.dumps(self.ga_state, indent=2),
                                    encoding="utf-8")
            out["F"] = list(FAIL_F)
            return
        dt = time.time() - t0

        # 4. compute objectives
        retention = (result.get("retention") or {})
        total = max(int(retention.get("total", 0)), 1)
        in_pos = int(retention.get("in_positive", 0))
        splash_frac = float(total - in_pos) / float(total)
        dist_mm = dist_from_nozzle_mm(self.stage_idx, ind_dict,
                                        self.modules_info)
        f1 = splash_frac
        f2 = -dist_mm

        eval_entry.update({
            "completed": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wall_s": round(dt, 1),
            "splash_frac": round(splash_frac, 4),
            "dist_mm": round(dist_mm, 1),
            "in_positive": in_pos,
            "total": total,
            "F": [f1, f2],
        })
        STATE_FILE.write_text(json.dumps(self.ga_state, indent=2),
                                encoding="utf-8")
        print(f"splash={splash_frac:.3f} dist={dist_mm:.0f}mm ({dt:.0f}s)")
        out["F"] = [f1, f2]


# ---- callback ----------------------------------------------------------------

class DumpStateCallback(Callback):
    def __init__(self, stage_idx: int, ga_state: dict):
        super().__init__()
        self.stage_idx = stage_idx
        self.ga_state = ga_state

    def notify(self, algorithm):
        gen = algorithm.n_gen
        pop = algorithm.pop
        X = pop.get("X").tolist()
        F = pop.get("F").tolist()
        skey = str(self.stage_idx)
        st = self.ga_state.setdefault("stages", {}).setdefault(skey, {})
        st["last_gen"] = int(gen)
        st["last_pop"] = [{"x": x, "f": f} for x, f in zip(X, F)]
        STATE_FILE.write_text(json.dumps(self.ga_state, indent=2),
                                encoding="utf-8")


# ---- stage runner ------------------------------------------------------------

def _print_pareto(F: np.ndarray, X: np.ndarray) -> None:
    hdr_cols = ["idx", "splash", "dist_mm"] + GENE_ORDER
    widths = [4, 8, 9] + [10] * len(GENE_ORDER)
    line = " | ".join(f"{c:>{w}}" for c, w in zip(hdr_cols, widths))
    print(line)
    print("-" * len(line))
    for i, (f, x) in enumerate(zip(F, X)):
        cells = [f"{i:>4}", f"{f[0]:>8.4f}", f"{-f[1]:>9.0f}"]
        cells += [f"{x[j]:>10.1f}" for j in range(len(GENE_ORDER))]
        print(" | ".join(cells))


def run_stage(stage_idx: int, prior_bests: dict, modules_info: list,
               ga_state: dict, pop_size: int, n_gen: int,
               seed: int) -> dict:
    print(f"\n=== STAGE {stage_idx} (module {stage_idx}) ===")
    print(f"prior_bests: {sorted(prior_bests.keys())}")

    problem = SculptureProblem(stage_idx, prior_bests, modules_info, ga_state)
    algorithm = NSGA2(
        pop_size=pop_size,
        sampling=LHS(),
        crossover=SBX(prob=0.9, eta=15),
        mutation=PM(eta=20),
        repair=CouplingRepair(),
        eliminate_duplicates=True,
    )
    callback = DumpStateCallback(stage_idx, ga_state)

    res = minimize(problem, algorithm, ("n_gen", n_gen),
                    seed=seed, verbose=True, callback=callback)

    F = np.atleast_2d(res.F)
    X = np.atleast_2d(res.X)
    order = np.argsort(F[:, 0])
    F = F[order]
    X = X[order]

    print(f"\n=== STAGE {stage_idx} COMPLETE -- Pareto front "
          f"({len(F)} solutions) ===")
    _print_pareto(F, X)

    while True:
        try:
            sel = input(f"\n  module {stage_idx} idx 선택 "
                          f"[0-{len(F)-1}]: ").strip()
            idx = int(sel)
            if 0 <= idx < len(F):
                break
            print(f"  out of range")
        except ValueError:
            print(f"  not an integer")
        except (EOFError, KeyboardInterrupt):
            print("  aborted by user")
            raise

    picked_dict = _ind_to_dict(X[idx])
    print(f"\n>>> STAGE {stage_idx} PICKED idx={idx}: "
          f"splash={F[idx][0]:.4f}  dist={-F[idx][1]:.0f}mm")
    print(f"    {picked_dict}")
    return picked_dict


# ---- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="0,1,2,3",
                     help="comma-sep module indices to run sequentially")
    ap.add_argument("--stage", type=int, default=None,
                     help="single stage (overrides --stages)")
    ap.add_argument("--pop", type=int, default=16, help="NSGA-II pop size")
    ap.add_argument("--n_gen", type=int, default=10, help="generations / stage")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--status", action="store_true",
                     help="print state file summary and exit")
    args = ap.parse_args()

    EXPERIMENTS.mkdir(parents=True, exist_ok=True)

    ga_state = {}
    if STATE_FILE.exists():
        try:
            ga_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            ga_state = {}
    ga_state.setdefault("stages", {})
    ga_state.setdefault("evaluations", [])

    if args.status:
        print(f"State file: {STATE_FILE}")
        stages = ga_state.get("stages", {})
        done = [k for k, v in stages.items() if v.get("done")]
        inprog = [k for k, v in stages.items() if not v.get("done")]
        print(f"Completed stages: {sorted(done)}")
        print(f"In-progress stages: {sorted(inprog)}")
        print(f"Total evaluations logged: {len(ga_state.get('evaluations', []))}")
        for k in sorted(stages.keys(), key=int):
            v = stages[k]
            if v.get("done"):
                bp = v["best_params"]
                print(f"  module {k} FROZEN: "
                      f"r={bp['radius']:.0f} mz={bp['move_z']:.0f} "
                      f"rx={bp['rotation_x']:.1f} rz={bp['rotation_z']:.1f} "
                      f"off={bp['offset_dist']:.0f} "
                      f"T=({bp['tx']:.0f},{bp['ty']:.0f},{bp['tz']:.0f})")
            else:
                print(f"  module {k} in_progress: "
                      f"last_gen={v.get('last_gen')} "
                      f"pop={len(v.get('last_pop', []))}")
        return

    if not MODULES_JSON.exists():
        print(f"ERROR: {MODULES_JSON} missing. Run extract_targets.py first.")
        return
    modules_info = json.loads(
        MODULES_JSON.read_text(encoding="utf-8"))["modules"]

    prior_bests: dict = {}
    for k, v in ga_state["stages"].items():
        if v.get("done") and v.get("best_params"):
            prior_bests[int(k)] = v["best_params"]

    if args.stage is not None:
        targets = [args.stage]
    else:
        targets = [int(s) for s in args.stages.split(",") if s.strip()]

    print(f"pymoo NSGA-II: POP={args.pop} N_GEN={args.n_gen} "
          f"stages={targets} seed={args.seed}")
    print(f"prior_bests (completed): {sorted(prior_bests.keys())}")

    for stage_idx in targets:
        skey = str(stage_idx)
        if skey in ga_state["stages"] and ga_state["stages"][skey].get("done"):
            print(f"Stage {stage_idx} already done -- skipping.")
            continue
        # Distinct seed per stage so LHS samples differ across modules
        picked = run_stage(stage_idx, prior_bests, modules_info, ga_state,
                            args.pop, args.n_gen, args.seed + stage_idx)
        ga_state["stages"][skey] = {
            "done": True,
            "best_params": picked,
        }
        prior_bests[stage_idx] = picked
        STATE_FILE.write_text(json.dumps(ga_state, indent=2),
                                encoding="utf-8")

    print("\n=== ALL STAGES COMPLETE ===")
    for k in sorted(ga_state["stages"].keys(), key=int):
        v = ga_state["stages"][k]
        if not v.get("done"):
            continue
        bp = v["best_params"]
        print(f"  module {k}: "
              f"r={bp['radius']:.0f} mz={bp['move_z']:.0f} "
              f"rx={bp['rotation_x']:.1f} rz={bp['rotation_z']:.1f} "
              f"off={bp['offset_dist']:.0f} "
              f"T=({bp['tx']:.0f},{bp['ty']:.0f},{bp['tz']:.0f})")


if __name__ == "__main__":
    main()
