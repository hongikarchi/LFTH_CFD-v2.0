"""Optimize a steel ground-structure under the CFD modules."""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fea import AnalysisResult, analyze_structure
from ground_structure import (DEFAULT_CASE, DEFAULT_CONTEXT,
                              design_from_encoded, generate_ground_structure,
                              load_case, load_context, seed_connected_design,
                              write_graph)
from profiles import SteelMaterial, load_profiles, profile_by_name

RUNS = MODULE_ROOT / "runs"
DEFAULT_OUT = RUNS / "structure_solution.json"
DEFAULT_GRAPH_OUT = RUNS / "ground_structure_graph.json"
OPT_LOG = MODULE_ROOT / "_structure_optimization_log.jsonl"


def _material_from_case(case: dict) -> SteelMaterial:
    mat = case.get("material", {})
    return SteelMaterial(
        name=str(mat.get("name", "Steel")),
        E_Pa=float(mat.get("E_Pa", 200.0e9)),
        nu=float(mat.get("nu", 0.3)),
        density_kgpm3=float(mat.get("density_kgpm3", 7850.0)),
        Fy_Pa=float(mat.get("Fy_Pa", 275.0e6)),
    )


def _constraint_values(result: AnalysisResult, case: dict) -> dict[str, float]:
    cons = case.get("constraints", {})
    max_util = float(cons.get("max_utilization", 1.0))
    max_slender = float(cons.get("max_slenderness", 200.0))
    return {
        "stable": 0.0 if result.stable else 1.0,
        "connected": 0.0 if result.connected else 1.0,
        "utilization": result.max_utilization / max(max_util, 1.0e-9) - 1.0,
        "deflection": result.max_displacement_mm / max(result.displacement_limit_mm, 1.0e-9) - 1.0,
        "slenderness": result.max_slenderness / max(max_slender, 1.0e-9) - 1.0,
    }


def _penalty(result: AnalysisResult, case: dict) -> float:
    constraints = _constraint_values(result, case)
    excess = sum(max(0.0, v) for v in constraints.values())
    if result.stable and result.connected:
        return result.mass_kg + 100000.0 * excess
    return 1000000000.0 + result.mass_kg + 100000.0 * excess


def _is_feasible(result: AnalysisResult, case: dict) -> bool:
    return all(v <= 0.0 for v in _constraint_values(result, case).values())


def _better(a: tuple[list[int], AnalysisResult] | None,
            b: tuple[list[int], AnalysisResult],
            case: dict) -> tuple[list[int], AnalysisResult]:
    if a is None:
        return b
    _, ar = a
    _, br = b
    af = _is_feasible(ar, case)
    bf = _is_feasible(br, case)
    if bf and not af:
        return b
    if af and not bf:
        return a
    if bf and af:
        return b if br.mass_kg < ar.mass_kg else a
    return b if _penalty(br, case) < _penalty(ar, case) else a


def evaluate_encoded(encoded: list[int], graph, profiles, material, case,
                     engine: str) -> AnalysisResult:
    names = [p.name for p in profiles]
    selected = design_from_encoded(graph, encoded, names)
    return analyze_structure(graph, selected, profile_by_name(profiles),
                             material, case, engine=engine)


def _append_log(row: dict) -> None:
    OPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with OPT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _mutate(base: list[int], profile_count: int, rng: random.Random) -> list[int]:
    out = list(base)
    if not out:
        return out
    changes = max(1, int(len(out) * rng.uniform(0.02, 0.08)))
    for _ in range(changes):
        idx = rng.randrange(len(out))
        cur = out[idx]
        r = rng.random()
        if cur == 0:
            out[idx] = rng.randint(1, profile_count) if r < 0.35 else 0
        elif r < 0.25:
            out[idx] = 0
        elif r < 0.65:
            out[idx] = max(1, cur - 1)
        else:
            out[idx] = min(profile_count, cur + 1)
    return out


def _random_design(length: int, profile_count: int, rng: random.Random,
                   activation_prob: float = 0.08) -> list[int]:
    return [
        rng.randint(1, profile_count) if rng.random() < activation_prob else 0
        for _ in range(length)
    ]


def _encoded_for_kinds(graph, allowed_kinds: set[str], profile_index: int) -> list[int]:
    return [
        profile_index if member.kind in allowed_kinds else 0
        for member in graph.members
    ]


def run_fallback_search(graph, profiles, material, case, *,
                        n_eval: int, seed: int, engine: str):
    rng = random.Random(seed)
    profile_count = len(profiles)
    base = seed_connected_design(
        graph,
        profile_count,
        supports_per_target=int(case.get("ground_structure", {}).get("nearest_supports_per_target", 4)),
        profile_index=profile_count,
    )
    best: tuple[list[int], AnalysisResult] | None = None
    evaluated = 0
    topology_seeds = [
        base,
        _encoded_for_kinds(
            graph,
            {"support_to_intermediate", "intermediate_to_target",
             "module_target_tie", "inter_module_brace"},
            profile_count,
        ),
        _encoded_for_kinds(
            graph,
            {"support_to_target", "support_to_intermediate",
             "intermediate_to_target", "module_target_tie",
             "inter_module_brace"},
            profile_count,
        ),
    ]

    # First try deterministic direct and braced topologies with progressively
    # lighter profiles. The braced seed is important for tall supports: direct
    # support-to-target members can pass stress/deflection while failing
    # slenderness because their unbraced lengths are too long.
    for seed_topology in topology_seeds:
        for pidx in range(profile_count, 0, -1):
            if evaluated >= n_eval:
                break
            trial = [pidx if value > 0 else 0 for value in seed_topology]
            result = evaluate_encoded(trial, graph, profiles, material, case, engine)
            best = _better(best, (trial, result), case)
            evaluated += 1
            _append_log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": "fallback",
                "eval": evaluated,
                "mass_kg": result.mass_kg,
                "feasible": _is_feasible(result, case),
                "constraints": _constraint_values(result, case),
                "error": result.error,
            })
            print(f"fallback {evaluated:04d}: mass={result.mass_kg:.1f} feasible={_is_feasible(result, case)} err={result.error}")

    while evaluated < n_eval:
        if best and rng.random() < 0.75:
            trial = _mutate(best[0], profile_count, rng)
        else:
            trial = _random_design(len(graph.members), profile_count, rng)
            # Keep a feasible-ish backbone in most random probes.
            if rng.random() < 0.70:
                for i, value in enumerate(base):
                    if value > 0 and trial[i] == 0:
                        trial[i] = rng.randint(max(1, profile_count - 2), profile_count)
        result = evaluate_encoded(trial, graph, profiles, material, case, engine)
        best = _better(best, (trial, result), case)
        evaluated += 1
        _append_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": "fallback",
            "eval": evaluated,
            "mass_kg": result.mass_kg,
            "feasible": _is_feasible(result, case),
            "constraints": _constraint_values(result, case),
            "error": result.error,
        })
        if evaluated % 10 == 0 or evaluated == n_eval:
            assert best is not None
            print(
                f"fallback {evaluated:04d}: best_mass={best[1].mass_kg:.1f} "
                f"best_feasible={_is_feasible(best[1], case)}"
            )
    assert best is not None
    return best


def _pymoo_available() -> bool:
    try:
        import numpy  # noqa: F401
        import pymoo  # noqa: F401
        return True
    except Exception:
        return False


def run_pymoo(graph, profiles, material, case, *, pop: int, n_gen: int,
              seed: int, engine: str):
    try:
        import numpy as np
        from pymoo.algorithms.soo.nonconvex.ga import GA
        from pymoo.core.problem import ElementwiseProblem
        from pymoo.operators.crossover.sbx import SBX
        from pymoo.operators.mutation.pm import PM
        from pymoo.operators.repair.rounding import RoundingRepair
        from pymoo.operators.sampling.rnd import IntegerRandomSampling
        from pymoo.optimize import minimize
    except Exception as exc:
        print(f"pymoo unavailable ({exc}); using fallback search")
        return run_fallback_search(
            graph, profiles, material, case,
            n_eval=int(case.get("optimization", {}).get("fallback_n_eval", 120)),
            seed=seed, engine=engine,
        )

    records: list[tuple[list[int], AnalysisResult]] = []
    profile_count = len(profiles)
    base = seed_connected_design(graph, profile_count, profile_index=profile_count)

    class StructureProblem(ElementwiseProblem):
        def __init__(self):
            super().__init__(
                n_var=len(graph.members),
                n_obj=1,
                n_ieq_constr=5,
                xl=np.zeros(len(graph.members), dtype=int),
                xu=np.full(len(graph.members), profile_count, dtype=int),
                vtype=int,
            )

        def _evaluate(self, x, out, *args, **kwargs):
            encoded = [int(round(v)) for v in x]
            # Keep sparse random individuals from being entirely disconnected.
            if not any(encoded):
                encoded[:] = base
            result = evaluate_encoded(encoded, graph, profiles, material, case, engine)
            records.append((encoded, result))
            c = _constraint_values(result, case)
            out["F"] = [result.mass_kg if result.stable and result.connected else _penalty(result, case)]
            out["G"] = [c["stable"], c["connected"], c["utilization"], c["deflection"], c["slenderness"]]

    algorithm = GA(
        pop_size=pop,
        sampling=IntegerRandomSampling(),
        crossover=SBX(prob=0.9, eta=15, repair=RoundingRepair()),
        mutation=PM(eta=20, repair=RoundingRepair()),
        eliminate_duplicates=True,
    )
    minimize(StructureProblem(), algorithm, ("n_gen", n_gen), seed=seed, verbose=True)
    best: tuple[list[int], AnalysisResult] | None = None
    for item in records:
        best = _better(best, item, case)
    if best is None:
        raise RuntimeError("pymoo produced no evaluations")
    return best


def _selected_member_rows(graph, encoded, profiles, result: AnalysisResult) -> list[dict]:
    pmap = profile_by_name(profiles)
    names = [p.name for p in profiles]
    selected = design_from_encoded(graph, encoded, names)
    nodes = graph.node_map()
    by_member_result = {m.member_id: m for m in result.member_results}
    rows = []
    for member in graph.members:
        pname = selected.get(member.id)
        if not pname:
            continue
        mres = by_member_result.get(member.id)
        rows.append({
            "id": member.id,
            "i": member.i,
            "j": member.j,
            "start_mm": list(nodes[member.i].xyz_mm),
            "end_mm": list(nodes[member.j].xyz_mm),
            "length_mm": member.length_mm,
            "kind": member.kind,
            "profile": pmap[pname].to_dict(),
            "analysis": mres.to_dict() if mres else None,
        })
    return rows


def write_solution(path: Path | str, graph, encoded, profiles, result,
                   case: dict, context_path: Path, profile_path: Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "context": str(context_path).replace("\\", "/"),
        "profile_csv": str(profile_path).replace("\\", "/"),
        "case": case,
        "graph_meta": graph.meta,
        "result": result.to_dict(),
        "nodes": [n.to_dict() for n in graph.nodes],
        "selected_members": _selected_member_rows(graph, encoded, profiles, result),
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default=str(DEFAULT_CONTEXT))
    parser.add_argument("--case", default=str(DEFAULT_CASE))
    parser.add_argument("--profiles", default=str(MODULE_ROOT / "data" / "ks_jis_h_profiles.csv"))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--graph-out", default=str(DEFAULT_GRAPH_OUT))
    parser.add_argument("--engine", choices=["auto", "pynite", "frame", "truss"], default="auto")
    parser.add_argument("--pop", type=int, default=None)
    parser.add_argument("--n-gen", type=int, default=None)
    parser.add_argument("--n-eval", type=int, default=None,
                        help="fallback search evaluations when pymoo is unavailable")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--force-fallback", action="store_true")
    args = parser.parse_args(argv)

    context_path = Path(args.context)
    case_path = Path(args.case)
    profile_path = Path(args.profiles)
    case = load_case(case_path)
    context = load_context(context_path)
    profiles = load_profiles(profile_path)
    material = _material_from_case(case)
    graph = generate_ground_structure(context, case)
    write_graph(args.graph_out, graph)

    opt_cfg = case.get("optimization", {})
    pop = int(args.pop or opt_cfg.get("default_pop", 32))
    n_gen = int(args.n_gen or opt_cfg.get("default_n_gen", 30))
    n_eval = int(args.n_eval or opt_cfg.get("fallback_n_eval", 120))
    seed = int(args.seed if args.seed is not None else opt_cfg.get("seed", 42))

    print(f"graph: nodes={len(graph.nodes)} members={len(graph.members)} meta={graph.meta}")
    print(f"profiles: {len(profiles)} from {profile_path}")

    if args.force_fallback or not _pymoo_available():
        print("using fallback random/local search")
        best_encoded, best_result = run_fallback_search(
            graph, profiles, material, case, n_eval=n_eval, seed=seed,
            engine=args.engine,
        )
    else:
        print(f"using pymoo GA pop={pop} n_gen={n_gen}")
        best_encoded, best_result = run_pymoo(
            graph, profiles, material, case, pop=pop, n_gen=n_gen,
            seed=seed, engine=args.engine,
        )

    write_solution(args.out, graph, best_encoded, profiles, best_result,
                   case, context_path, profile_path)
    print(f"wrote {args.out}")
    print(
        f"best: mass={best_result.mass_kg:.1f}kg feasible={_is_feasible(best_result, case)} "
        f"disp={best_result.max_displacement_mm:.2f}/{best_result.displacement_limit_mm:.2f}mm "
        f"util={best_result.max_utilization:.3f} slender={best_result.max_slenderness:.1f} "
        f"engine={best_result.engine} err={best_result.error}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

