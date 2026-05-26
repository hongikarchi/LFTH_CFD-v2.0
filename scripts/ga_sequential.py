"""
Sequential per-module GA — top-down (modules 0, 1, 2, 3).

For each stage (module N):
  - Variables: tx, ty, tz, rx, ry, rz, scale  (7 dims)
  - Prior modules 0..N-1 frozen at their best
  - Modules N+1..5 identity (untouched yet)
  - Fitness: minimize trail velocity AND horizontal spread in module N's z slab
            (penalty if no trails enter the slab — module bypassed)
  - GA: DEAP, pop=8, gen=4 → 32 evals/stage

State stored in experiments/sequential_state.json so a stage can resume.
Each individual evaluation creates the next sequential test_NN.json and runs
the standard experiment_runner.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path

from deap import base, creator, tools

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

EXPERIMENTS = PROJECT / "experiments"
RUNS = PROJECT / "runs"
STATE_FILE = EXPERIMENTS / "sequential_state.json"
MODULES_JSON = RUNS / "_collider_modules.json"

# Variable bounds for each gene
BOUNDS = {
    "tx":    (-5.0, 5.0),
    "ty":    (-5.0, 5.0),
    "tz":    (-3.0, 3.0),
    "rx":    (-30.0, 30.0),
    "ry":    (-30.0, 30.0),
    "rz":    (-30.0, 30.0),
    "scale": (0.6, 1.4),
}
GENE_ORDER = ["tx", "ty", "tz", "rx", "ry", "rz", "scale"]

POP = 8
N_GEN = 4
CXPB = 0.7
MUTPB = 0.3
TOURN_K = 2

# Module-local fitness weights
W_VELOCITY = 1.0
W_SPREAD = 1.0
NO_TRAIL_PENALTY = 1.0e6


# --- DEAP setup ----------------------------------------------------------------
def _setup_deap():
    if "FitnessMin" in creator.__dict__:
        # already created; clear so re-run does not error
        del creator.FitnessMin
        del creator.Individual
    creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
    creator.create("Individual", list, fitness=creator.FitnessMin)


def _gene_sample(name):
    lo, hi = BOUNDS[name]
    return random.uniform(lo, hi)


def _clip(name, value):
    lo, hi = BOUNDS[name]
    return max(lo, min(hi, value))


def _ind_dict(ind):
    return {GENE_ORDER[i]: ind[i] for i in range(len(GENE_ORDER))}


# --- next test_NN id -----------------------------------------------------------
def next_test_nn() -> int:
    existing = list(EXPERIMENTS.glob("test_*.json"))
    nums = []
    for p in existing:
        try:
            nums.append(int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return (max(nums) + 1) if nums else 3


# --- compose test_NN.json given current stage + ind ----------------------------
def compose_params(stage_module_idx: int, ind_dict: dict,
                   prior_bests: dict, test_id: str, note: str) -> dict:
    """prior_bests: {module_idx: {tx,ty,tz,rx,ry,rz,scale}}"""
    modules = []
    for i in range(6):
        if i in prior_bests:
            cfg = prior_bests[i]
        elif i == stage_module_idx:
            cfg = ind_dict
        else:
            cfg = {"tx": 0, "ty": 0, "tz": 0, "rx": 0, "ry": 0, "rz": 0, "scale": 1.0}
        modules.append({
            "index": i,
            "rotation_deg": [cfg["rx"], cfg["ry"], cfg["rz"]],
            "translation_m": [cfg["tx"], cfg["ty"], cfg["tz"]],
            "scale": cfg["scale"],
        })
    return {
        "test_id": test_id,
        "note": note,
        "global": {
            "v_inlet": 2.0,
            "burst_end": 0.5,
            "dp": 0.10,
            "timemax": 10.0,
            "viscobound": 0.2,
        },
        "modules": modules,
    }


# --- module slab z range -------------------------------------------------------
def module_slab(module_idx: int, ind_dict: dict, modules_info: list,
                 half_height_m: float = 3.0) -> tuple[float, float]:
    """Return (z_top, z_bottom) for module N AFTER its tz translation.
    half_height_m = vertical half-slab to integrate fitness inside."""
    m = modules_info[module_idx]
    cz_m = m["center_mm"][2] / 1000.0
    tz_applied = ind_dict.get("tz", 0.0)
    cz_eff = cz_m + tz_applied
    return cz_eff + half_height_m, cz_eff - half_height_m


# --- module-local fitness from trails.json ------------------------------------
def module_local_fitness(trails_path: Path, z_top: float, z_bottom: float,
                          dt: float = 0.10) -> dict:
    """For each trail that crosses the slab [z_bottom, z_top]:
       - find first frame inside slab (enter) and last frame inside slab (exit)
       - measure exit-vs-enter horizontal displacement (spread)
       - measure exit velocity vector magnitude (slower exit = better)
       Returns dict with mean/median + n_passing."""
    if not trails_path.exists():
        return {"n_passing": 0, "fitness": NO_TRAIL_PENALTY, "v_med": None, "s_med": None}
    trails = json.loads(trails_path.read_text(encoding="utf-8"))
    velocities = []
    spreads = []
    for idp, pts in trails.items():
        if len(pts) < 3:
            continue
        enter_i = None
        exit_i = None
        for i, p in enumerate(pts):
            if z_bottom <= p[2] <= z_top:
                if enter_i is None:
                    enter_i = i
                exit_i = i
        if enter_i is None or exit_i is None or exit_i <= enter_i:
            continue
        a = pts[enter_i]
        b = pts[exit_i]
        # horizontal displacement during traversal (spread)
        spread = math.hypot(b[0] - a[0], b[1] - a[1])
        # exit velocity (finite diff around exit_i)
        ie = max(enter_i + 1, exit_i)
        if ie - 1 < 0 or ie >= len(pts):
            continue
        v = pts[ie]
        u = pts[ie - 1]
        vx = (v[0] - u[0]) / dt
        vy = (v[1] - u[1]) / dt
        vz = (v[2] - u[2]) / dt
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        velocities.append(speed)
        spreads.append(spread)

    if not velocities:
        return {"n_passing": 0, "fitness": NO_TRAIL_PENALTY, "v_med": None, "s_med": None}
    velocities.sort()
    spreads.sort()
    n = len(velocities)
    v_med = velocities[n // 2]
    s_med = spreads[n // 2]
    fitness = W_VELOCITY * v_med + W_SPREAD * s_med
    return {"n_passing": n, "fitness": fitness, "v_med": v_med, "s_med": s_med}


# --- one evaluation ------------------------------------------------------------
def evaluate(ind, stage_module_idx, prior_bests, modules_info, ga_state):
    """Run one experiment, return (fitness,)."""
    ind_d = _ind_dict(ind)
    test_n = next_test_nn()
    test_id = f"test_{test_n:02d}"
    ga_state.setdefault("evaluations", []).append({
        "stage": stage_module_idx,
        "test_id": test_id,
        "ind": ind_d,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    note = (f"GA stage{stage_module_idx} module{stage_module_idx} "
            f"tx={ind_d['tx']:.2f} ty={ind_d['ty']:.2f} tz={ind_d['tz']:.2f} "
            f"R=({ind_d['rx']:.1f},{ind_d['ry']:.1f},{ind_d['rz']:.1f}) "
            f"s={ind_d['scale']:.2f}")
    params = compose_params(stage_module_idx, ind_d, prior_bests, test_id, note)
    params_path = EXPERIMENTS / f"{test_id}.json"
    params_path.write_text(json.dumps(params, indent=2), encoding="utf-8")

    cmd = [sys.executable, str(PROJECT / "scripts" / "experiment_runner.py"), str(params_path)]
    print(f"  [{test_id}] running ...", end=" ", flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    dt = time.time() - t0
    if proc.returncode != 0:
        print(f"FAILED ({dt:.0f}s)")
        return (NO_TRAIL_PENALTY,)
    trails_path = RUNS / f"iter_{test_id}" / "trails.json"
    z_top, z_bot = module_slab(stage_module_idx, ind_d, modules_info)
    fit = module_local_fitness(trails_path, z_top, z_bot)

    # Save the slab + fitness back into result.json for future reference
    result_path = RUNS / f"iter_{test_id}" / "result.json"
    if result_path.exists():
        r = json.loads(result_path.read_text(encoding="utf-8"))
        r["module_local"] = {
            "stage_module": stage_module_idx,
            "z_top": z_top, "z_bottom": z_bot,
            **fit,
        }
        result_path.write_text(json.dumps(r, indent=2), encoding="utf-8")

    # Also stash on GA state
    ga_state["evaluations"][-1].update({
        "completed": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wall_s": round(dt, 1),
        "n_passing": fit["n_passing"],
        "v_med": fit.get("v_med"),
        "s_med": fit.get("s_med"),
        "fitness": fit["fitness"],
    })
    STATE_FILE.write_text(json.dumps(ga_state, indent=2), encoding="utf-8")
    print(f"fit={fit['fitness']:.3f} n_pass={fit['n_passing']} ({dt:.0f}s)")
    return (fit["fitness"],)


# --- per-stage GA -------------------------------------------------------------
def run_stage(stage_module_idx, prior_bests, modules_info, ga_state):
    print(f"\n=== STAGE {stage_module_idx} (module {stage_module_idx}) ===")
    print(f"prior_bests: {list(prior_bests.keys())}")

    _setup_deap()
    toolbox = base.Toolbox()
    toolbox.register("gene", lambda i: _gene_sample(GENE_ORDER[i]))
    toolbox.register("individual",
                      lambda: creator.Individual([_gene_sample(g) for g in GENE_ORDER]))
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def eval_wrap(ind):
        return evaluate(ind, stage_module_idx, prior_bests, modules_info, ga_state)

    toolbox.register("evaluate", eval_wrap)
    toolbox.register("mate", tools.cxBlend, alpha=0.4)
    def mutate(ind, indpb=0.4, sigma_frac=0.2):
        for i in range(len(ind)):
            if random.random() < indpb:
                lo, hi = BOUNDS[GENE_ORDER[i]]
                sigma = (hi - lo) * sigma_frac
                ind[i] = _clip(GENE_ORDER[i], ind[i] + random.gauss(0, sigma))
        return (ind,)
    toolbox.register("mutate", mutate)
    toolbox.register("select", tools.selTournament, tournsize=TOURN_K)

    pop = toolbox.population(n=POP)
    # Evaluate initial
    print(f"-- generation 0 (initial) --")
    for ind in pop:
        ind.fitness.values = toolbox.evaluate(ind)

    best_so_far = tools.selBest(pop, 1)[0]
    print(f"  gen0 best fitness: {best_so_far.fitness.values[0]:.3f}")

    for gen in range(1, N_GEN):
        print(f"-- generation {gen} --")
        offspring = toolbox.select(pop, POP)
        offspring = [creator.Individual(list(o)) for o in offspring]
        # crossover
        for i in range(0, len(offspring) - 1, 2):
            if random.random() < CXPB:
                toolbox.mate(offspring[i], offspring[i + 1])
                del offspring[i].fitness.values
                del offspring[i + 1].fitness.values
        # mutation
        for ind in offspring:
            if random.random() < MUTPB:
                toolbox.mutate(ind)
                if ind.fitness.valid:
                    del ind.fitness.values
        # clip
        for ind in offspring:
            for i, name in enumerate(GENE_ORDER):
                ind[i] = _clip(name, ind[i])
        # evaluate invalid
        for ind in offspring:
            if not ind.fitness.valid:
                ind.fitness.values = toolbox.evaluate(ind)
        pop = offspring
        gen_best = tools.selBest(pop, 1)[0]
        if gen_best.fitness.values[0] < best_so_far.fitness.values[0]:
            best_so_far = gen_best
        print(f"  gen{gen} best fitness: {best_so_far.fitness.values[0]:.3f}")

    best = best_so_far
    best_d = _ind_dict(best)
    print(f"\n>>> STAGE {stage_module_idx} BEST: fitness={best.fitness.values[0]:.3f}")
    print(f"    params: {best_d}")
    return best_d, float(best.fitness.values[0])


# --- main ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="0,1,2,3", help="comma-sep module indices to run sequentially")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)

    modules_info = json.loads(MODULES_JSON.read_text(encoding="utf-8"))["modules"]

    ga_state = {}
    if STATE_FILE.exists():
        try:
            ga_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            ga_state = {}
    ga_state.setdefault("stages", {})

    prior_bests: dict = {}
    # Restore any already-completed stages
    for k, v in ga_state["stages"].items():
        if v.get("best_params"):
            prior_bests[int(k)] = v["best_params"]

    targets = [int(s) for s in args.stages.split(",")]
    for stage_idx in targets:
        if str(stage_idx) in ga_state["stages"] and ga_state["stages"][str(stage_idx)].get("done"):
            print(f"Stage {stage_idx} already done — skipping (best in prior_bests).")
            continue
        best_params, best_fit = run_stage(stage_idx, prior_bests, modules_info, ga_state)
        ga_state["stages"][str(stage_idx)] = {
            "done": True,
            "best_fitness": best_fit,
            "best_params": best_params,
        }
        prior_bests[stage_idx] = best_params
        STATE_FILE.write_text(json.dumps(ga_state, indent=2), encoding="utf-8")

    print("\n=== ALL STAGES COMPLETE ===")
    for k, v in sorted(ga_state["stages"].items(), key=lambda x: int(x[0])):
        bp = v["best_params"]
        print(f"  module {k}: fit={v['best_fitness']:.3f}  "
              f"T=({bp['tx']:.2f},{bp['ty']:.2f},{bp['tz']:.2f})  "
              f"R=({bp['rx']:.1f},{bp['ry']:.1f},{bp['rz']:.1f})  s={bp['scale']:.2f}")


if __name__ == "__main__":
    main()
