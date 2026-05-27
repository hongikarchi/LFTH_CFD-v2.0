"""Inspect one generation in Rhino MCP + verify cascade realism.

Usage:  python scripts/inspect_generation.py 1     # check gen_01

For each var (1..8) in the given gen, computes:
  - end_dist_to_mesh for nozzle particles (median should be < 1 m to indicate
    the falling water actually interacts with a module)
  - cascade depth = how far the nozzle stream descends in z
  - bounce count
"""
import json
import socket
import sys
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent

HOST, PORT = "127.0.0.1", 1999


def mcp(code: str, timeout: float = 60) -> dict:
    payload = json.dumps({"type": "execute_rhinoscript_python_code",
                          "params": {"code": code}}).encode()
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.sendall(payload)
        buf = b""
        while True:
            try:
                c = s.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            buf += c
            try:
                return json.loads(buf.decode("utf-8", "ignore"))
            except json.JSONDecodeError:
                continue
    return {}


def cell_test_id(gen: int, var: int) -> str | None:
    """Find test_NN that corresponds to (gen, var) by reading params files."""
    for p in (PROJECT / "experiments").glob("test_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        gc = d.get("grid_cell")
        if gc and int(gc.get("gen", 0)) == gen and int(gc.get("var", 0)) == var:
            return d["test_id"]
    return None


def inspect_cell(gen: int, var: int) -> dict:
    tid = cell_test_id(gen, var)
    iter_dir = PROJECT / "runs" / f"iter_{tid}" if tid else None
    if not iter_dir or not iter_dir.exists():
        return {"gen": gen, "var": var, "error": "no_data"}
    trails = json.loads((iter_dir / "trails.json").read_text(encoding="utf-8"))
    mesh = trimesh.load(iter_dir / "sculpture_viz.stl")

    nozzle = [pts for pts in trails.values() if pts and pts[0][2] >= 28]
    if not nozzle:
        return {"gen": gen, "var": var, "test_id": tid, "error": "no_nozzle_trails"}

    nozzle_ends = np.array([p[-1] for p in nozzle])
    _, end_dist, _ = mesh.nearest.on_surface(nozzle_ends)
    end_dist = sorted(end_dist.tolist())

    descents = [p[0][2] - min(q[2] for q in p) for p in nozzle]
    bouncers = 0
    for p in nozzle:
        zs = [q[2] for q in p]
        mn = min(zs); mn_i = zs.index(mn)
        if mn_i < len(zs) - 1 and (zs[0] - mn) > 1 and (max(zs[mn_i + 1:]) - mn) > 1:
            bouncers += 1
    n = len(end_dist)
    return {
        "gen": gen, "var": var, "test_id": tid,
        "n_nozzle": n,
        "end_dist_med_m": round(end_dist[n // 2], 2),
        "end_dist_p90_m": round(end_dist[int(n * 0.9)], 2),
        "end_dist_max_m": round(end_dist[-1], 2),
        "interacting": sum(1 for d in end_dist if d < 1.5),
        "descent_med_m": round(sorted(descents)[len(descents) // 2], 2),
        "descent_max_m": round(max(descents), 2),
        "bouncers": bouncers,
    }


def main():
    gen = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"=== Inspecting gen_{gen:02d} (8 cells) ===\n")
    rows = []
    for var in range(1, 9):
        info = inspect_cell(gen, var)
        rows.append(info)
        if "error" in info:
            print(f"  var_{var:02d}: {info.get('error')}")
        else:
            n = info["n_nozzle"]
            inter = info["interacting"]
            print(f"  var_{var:02d} ({info['test_id']}): "
                  f"nozzle interacting/total={inter}/{n}  "
                  f"end_dist med={info['end_dist_med_m']:.2f} max={info['end_dist_max_m']:.2f}  "
                  f"descent med={info['descent_med_m']:.1f}m  "
                  f"bounce={info['bouncers']}")

    valid_cells = [r for r in rows if "error" not in r]
    if valid_cells:
        # Realism = nozzle stream actually interacts with a module (end_dist
        # close to mesh) AND no bouncers. Descent isn't required since
        # cascading water SHOULD settle on modules, not fall through to floor.
        bad_cells = [r for r in valid_cells
                     if r["interacting"] / max(r["n_nozzle"], 1) < 0.5
                        or r["bouncers"] > 5]
        print(f"\nGen verdict: {len(bad_cells)}/{len(valid_cells)} cells fail realism check")
        if bad_cells:
            for r in bad_cells:
                why = []
                ratio = r["interacting"] / max(r["n_nozzle"], 1)
                if ratio < 0.5:
                    why.append(f"interact_ratio={ratio:.2f}")
                if r["bouncers"] > 5:
                    why.append(f"bounce={r['bouncers']}")
                if r["descent_med_m"] < 5:
                    why.append(f"low_descent={r['descent_med_m']}m")
                print(f"  {r['test_id']}: {', '.join(why)}")


if __name__ == "__main__":
    main()
