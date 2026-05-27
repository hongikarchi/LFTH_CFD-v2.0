"""
Sequential per-module GA — top-down (modules 0, 1, 2, 3) on the parametric
sphere-cap module geometry.

For each stage (module N):
  - Variables: 8 genes — radius, move_z, rotation_x, rotation_z, offset_dist,
    tx, ty, tz  (shape + position).
  - Prior modules 0..N-1 frozen at their best
  - Modules N+1..3 keep their DEFAULT geometry (no offset)
  - Fitness: module-local (slab z-bucket) velocity + horizontal spread,
    PLUS hard touch-constraint penalty if <50% of trails reach all 4 module
    slabs (sculpture-bypass guard).
  - GA: DEAP, pop=8, gen=4 -> 32 evals/stage

State stored in experiments/sequential_state.json so a stage can resume.
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

from module_geometry import GENE_ORDER, GENE_BOUNDS, DEFAULTS

EXPERIMENTS = PROJECT / "experiments"
RUNS = PROJECT / "runs"
STATE_FILE = EXPERIMENTS / "sequential_state.json"
MODULES_JSON = RUNS / "_collider_modules.json"

# Genes: shape (5) + position offset (3) = 8.
# GENE_ORDER and GENE_BOUNDS come from module_geometry to keep them in sync.
BOUNDS = GENE_BOUNDS  # alias

POP = 8
N_GEN = 10            # 8 var x 10 gen = 80 evals to fill the Rhino grid
CXPB = 0.7
MUTPB = 0.3
TOURN_K = 2

# Composite fitness (maximised score; DEAP minimises -score):
#   score = W_CATCH  * catch_rate                # particles in pond / total
#         + W_TILT   * mean(rotation_x_norm)     # average tilt across modules
#         + W_COLUMN * column_rate               # in_column (not pond) / total
#         (splash is penalised implicitly: catch + column + splash = 1)
W_CATCH = 1.0
W_TILT = 0.5
W_COLUMN = 0.1
NO_TRAIL_PENALTY = 1.0e6


# --- DEAP setup ---------------------------------------------------------------
def _setup_deap():
    if "FitnessMin" in creator.__dict__:
        del creator.FitnessMin
        del creator.Individual
    creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
    creator.create("Individual", list, fitness=creator.FitnessMin)


def _gene_sample(name: str) -> float:
    lo, hi = BOUNDS[name]
    return random.uniform(lo, hi)


def _clip(name: str, value: float) -> float:
    lo, hi = BOUNDS[name]
    return max(lo, min(hi, value))


def _enforce_couplings(ind: list) -> list:
    """Apply gene-coupling constraints in-place.

    1. cap_height = radius - move_z in [800, 2000] mm.
       Lower 800 = 4*dp at dp=0.20: guarantees water can sit + cap not too shallow.
       Upper 2000: prevents giant hemispheres swallowing the nozzles.
    2. rotation_x clipped so the cap can still hold water at this tilt:
       r_rim * tan(rotation_x) <= cap_height - 1*dp_mm
       (1*dp safety so at least one layer of water remains).
    """
    import math
    DP_MM = 200.0   # current sim dp = 0.20 m
    i_r = GENE_ORDER.index("radius")
    i_mz = GENE_ORDER.index("move_z")
    i_rx = GENE_ORDER.index("rotation_x")

    r = ind[i_r]
    min_mz = r - 2000.0
    max_mz = r - 800.0
    if ind[i_mz] < min_mz:
        ind[i_mz] = _clip("move_z", min_mz)
    elif ind[i_mz] > max_mz:
        ind[i_mz] = _clip("move_z", max_mz)

    mz = ind[i_mz]
    cap_depth = r - mz
    r_rim = math.sqrt(max(r * r - mz * mz, 1.0))
    # Water-holding constraint with margin: cap depth at deepest world point
    # must exceed water level by enough to fit 1 layer of fluid plus safety
    # margins (0.5*dp above cap surface + 1*dp below rim free surface).
    #   cap_depth*cos(rx) - r_rim*sin(rx) >= 3*dp
    K = 3.0 * DP_MM
    hyp = math.sqrt(cap_depth * cap_depth + r_rim * r_rim)
    phi = math.atan2(r_rim, cap_depth)
    if hyp <= K:
        max_rx = 0.0
    else:
        max_rx = math.degrees(math.acos(K / hyp) - phi)
    if max_rx < 0.0:
        max_rx = 0.0
    if ind[i_rx] > max_rx:
        ind[i_rx] = max_rx
    if ind[i_rx] < 0.0:
        ind[i_rx] = 0.0
    return ind


def _ind_dict(ind) -> dict:
    _enforce_couplings(ind)
    return {GENE_ORDER[i]: ind[i] for i in range(len(GENE_ORDER))}


# --- next test id -------------------------------------------------------------
def next_test_nn() -> int:
    existing = list(EXPERIMENTS.glob("test_*.json"))
    nums = []
    for p in existing:
        try:
            nums.append(int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return (max(nums) + 1) if nums else 2  # test_01 is the sanity eval


# --- compose test_NN.json -----------------------------------------------------
def _default_module_gene() -> dict:
    """Identity placeholder for not-yet-optimized modules — uses GH defaults."""
    return {**DEFAULTS, "tx": 0.0, "ty": 0.0, "tz": 0.0}


def compose_params(stage_module_idx: int, ind_dict: dict,
                   prior_bests: dict, test_id: str, note: str,
                   grid_cell: dict | None = None) -> dict:
    """prior_bests: {module_idx: 8-gene dict}
    grid_cell: {"gen": 1..10, "var": 1..8} for Rhino bake placement."""
    modules = []
    for i in range(4):
        if i in prior_bests:
            cfg = prior_bests[i]
        elif i == stage_module_idx:
            cfg = ind_dict
        else:
            cfg = _default_module_gene()
        modules.append({"index": i, **cfg})
    out = {
        "test_id": test_id,
        "note": note,
        "global": {
            "v_inlet": 0.0,
            "dp": 0.20,
            "timemax": 5.0,
            "viscobound": 1.0,
            "nozzle_diameter": 0.20,
            "nozzle_thickness": 0.20,
            "cflnumber": 0.30,
            "use_gpu": True,
            "timeout": 0.10,
        },
        "modules": modules,
    }
    if grid_cell:
        out["grid_cell"] = grid_cell
    return out


# --- module slab z range (in METERS) ------------------------------------------
def module_slab(module_idx: int, ind_dict: dict, modules_info: list,
                 half_height_m: float = 3.0) -> tuple[float, float]:
    """Slab around module N's base z (after tz offset). Note tz is in mm in the
    gene, base_point_mm is in mm; convert to m."""
    m = modules_info[module_idx]
    base_z_mm = m["base_point_mm"][2]
    tz_mm = ind_dict.get("tz", 0.0)
    cz_m = (base_z_mm + tz_mm) * 0.001
    return cz_m + half_height_m, cz_m - half_height_m


# --- module-local fitness from trails.json ------------------------------------
def module_local_fitness(trails_path: Path, z_top: float, z_bottom: float,
                          dt: float = 0.10) -> dict:
    """For each trail that crosses the slab [z_bottom, z_top]:
       - enter = first frame inside slab, exit = last frame inside slab
       - spread = horizontal displacement between enter/exit
       - velocity = magnitude at exit (slower = better)
       fitness = W_VELOCITY*v_med + W_SPREAD*s_med."""
    if not trails_path.exists():
        return {"n_passing": 0, "fitness": NO_TRAIL_PENALTY,
                "v_med": None, "s_med": None}
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
        a = pts[enter_i]; b = pts[exit_i]
        spread = math.hypot(b[0] - a[0], b[1] - a[1])
        ie = max(enter_i + 1, exit_i)
        if ie - 1 < 0 or ie >= len(pts):
            continue
        v = pts[ie]; u = pts[ie - 1]
        vx = (v[0] - u[0]) / dt
        vy = (v[1] - u[1]) / dt
        vz = (v[2] - u[2]) / dt
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        velocities.append(speed)
        spreads.append(spread)

    if not velocities:
        return {"n_passing": 0, "fitness": NO_TRAIL_PENALTY,
                "v_med": None, "s_med": None}
    velocities.sort(); spreads.sort()
    n = len(velocities)
    v_med = velocities[n // 2]
    s_med = spreads[n // 2]
    fitness = W_VELOCITY * v_med + W_SPREAD * s_med
    return {"n_passing": n, "fitness": fitness, "v_med": v_med, "s_med": s_med}


# --- one evaluation -----------------------------------------------------------
def evaluate(ind, stage_module_idx, prior_bests, modules_info, ga_state,
              grid_cell: dict | None = None):
    ind_d = _ind_dict(ind)
    test_n = next_test_nn()
    test_id = f"test_{test_n:02d}"
    ga_state.setdefault("evaluations", []).append({
        "stage": stage_module_idx,
        "test_id": test_id,
        "grid_cell": grid_cell,
        "ind": ind_d,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    gc_tag = (f" grid({grid_cell['gen']:02d},{grid_cell['var']:02d})"
              if grid_cell else "")
    note = (f"GA stage{stage_module_idx} module{stage_module_idx}{gc_tag} "
            f"r={ind_d['radius']:.0f} mz={ind_d['move_z']:.0f} "
            f"rx={ind_d['rotation_x']:.1f} rz={ind_d['rotation_z']:.1f} "
            f"off={ind_d['offset_dist']:.0f} "
            f"T=({ind_d['tx']:.0f},{ind_d['ty']:.0f},{ind_d['tz']:.0f})")
    params = compose_params(stage_module_idx, ind_d, prior_bests, test_id, note,
                              grid_cell=grid_cell)
    params_path = EXPERIMENTS / f"{test_id}.json"
    params_path.write_text(json.dumps(params, indent=2), encoding="utf-8")

    cmd = [sys.executable, str(PROJECT / "scripts" / "experiment_runner.py"),
            str(params_path)]
    print(f"  [{test_id}] running ...", end=" ", flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    dt = time.time() - t0
    if proc.returncode != 0:
        print(f"FAILED ({dt:.0f}s)")
        ga_state["evaluations"][-1].update({
            "completed": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wall_s": round(dt, 1),
            "failed": True,
            "fitness": NO_TRAIL_PENALTY,
        })
        STATE_FILE.write_text(json.dumps(ga_state, indent=2), encoding="utf-8")
        return (NO_TRAIL_PENALTY,)

    result_path = RUNS / f"iter_{test_id}" / "result.json"
    if not result_path.exists():
        print(f"NO RESULT.JSON ({dt:.0f}s)")
        return (NO_TRAIL_PENALTY,)
    rj = json.loads(result_path.read_text(encoding="utf-8"))
    retention = rj.get("retention", {}) or {}
    total = max(int(retention.get("total", 0)), 1)
    catch_rate = float(retention.get("in_pond", 0)) / total
    column_rate = float(retention.get("in_column", 0)) / total
    splash_rate = float(retention.get("splash", 0)) / total

    # Tilt score averaged over all 4 modules. Stage GA freezes prior modules,
    # so M0..N-1 carry prior-best rotation_x and MN carries the candidate's.
    all_mods = []
    for m in rj.get("params", {}).get("modules", []):
        all_mods.append(float(m.get("rotation_x", 0)))
    tilt_score = (sum(all_mods) / (len(all_mods) * 90.0)) if all_mods else 0.0

    score = (W_CATCH * catch_rate
              + W_TILT * tilt_score
              + W_COLUMN * column_rate)
    total_fit = -float(score)

    ga_state["evaluations"][-1].update({
        "completed": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wall_s": round(dt, 1),
        "catch_rate": round(catch_rate, 4),
        "tilt_score": round(tilt_score, 4),
        "column_rate": round(column_rate, 4),
        "splash_rate": round(splash_rate, 4),
        "score": round(score, 4),
        "in_pond": retention.get("in_pond"),
        "in_column": retention.get("in_column"),
        "splash": retention.get("splash"),
        "on_module": retention.get("on_module"),
        "total": retention.get("total"),
        "fitness": total_fit,
    })
    STATE_FILE.write_text(json.dumps(ga_state, indent=2), encoding="utf-8")
    try:
        subprocess.run([sys.executable,
                        str(PROJECT / "scripts" / "update_ga_dashboard.py")],
                        capture_output=True, timeout=30)
    except Exception:
        pass
    print(f"score={score:.3f} (catch={catch_rate:.3f} tilt={tilt_score:.3f} "
          f"col={column_rate:.3f} splash={splash_rate:.3f}) "
          f"pond={retention.get('in_pond')} col={retention.get('in_column')} "
          f"splash={retention.get('splash')}/{retention.get('total')} ({dt:.0f}s)")
    return (total_fit,)


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

    def eval_one(ind, gen_idx, var_idx):
        gc = {"gen": gen_idx, "var": var_idx}
        return evaluate(ind, stage_module_idx, prior_bests, modules_info,
                          ga_state, grid_cell=gc)

    pop = toolbox.population(n=POP)
    for ind in pop:
        _enforce_couplings(ind)
    print(f"-- generation 1 (initial) --")
    for var_idx, ind in enumerate(pop, start=1):
        ind.fitness.values = eval_one(ind, 1, var_idx)

    best_so_far = tools.selBest(pop, 1)[0]
    print(f"  gen1 best score: {-best_so_far.fitness.values[0]:.3f}")

    for gen in range(2, N_GEN + 1):
        print(f"-- generation {gen} --")
        offspring = toolbox.select(pop, POP)
        offspring = [creator.Individual(list(o)) for o in offspring]
        for i in range(0, len(offspring) - 1, 2):
            if random.random() < CXPB:
                toolbox.mate(offspring[i], offspring[i + 1])
                del offspring[i].fitness.values
                del offspring[i + 1].fitness.values
        for ind in offspring:
            if random.random() < MUTPB:
                toolbox.mutate(ind)
                if ind.fitness.valid:
                    del ind.fitness.values
        for ind in offspring:
            for i, name in enumerate(GENE_ORDER):
                ind[i] = _clip(name, ind[i])
            _enforce_couplings(ind)
        for var_idx, ind in enumerate(offspring, start=1):
            if not ind.fitness.valid:
                ind.fitness.values = eval_one(ind, gen, var_idx)
        pop = offspring
        gen_best = tools.selBest(pop, 1)[0]
        if gen_best.fitness.values[0] < best_so_far.fitness.values[0]:
            best_so_far = gen_best
        print(f"  gen{gen} best score: {-best_so_far.fitness.values[0]:.3f}")

    best = best_so_far
    best_d = _ind_dict(best)
    print(f"\n>>> STAGE {stage_module_idx} BEST: score={-best.fitness.values[0]:.3f}")
    print(f"    params: {best_d}")
    return best_d, float(best.fitness.values[0])


# --- main ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="0,1,2,3",
                     help="comma-sep module indices to run sequentially")
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
    for k, v in ga_state["stages"].items():
        if v.get("best_params"):
            prior_bests[int(k)] = v["best_params"]

    targets = [int(s) for s in args.stages.split(",")]
    for stage_idx in targets:
        if str(stage_idx) in ga_state["stages"] and ga_state["stages"][str(stage_idx)].get("done"):
            print(f"Stage {stage_idx} already done — skipping.")
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
        print(f"  module {k}: score={-v['best_fitness']:.3f}  "
              f"r={bp['radius']:.0f} mz={bp['move_z']:.0f} "
              f"rx={bp['rotation_x']:.1f} rz={bp['rotation_z']:.1f} "
              f"off={bp['offset_dist']:.0f} "
              f"T=({bp['tx']:.0f},{bp['ty']:.0f},{bp['tz']:.0f})")


if __name__ == "__main__":
    main()
