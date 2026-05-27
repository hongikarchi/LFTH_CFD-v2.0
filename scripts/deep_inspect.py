"""Deep visual-realism inspector for sweep cells.

For each test_NN/trails.json + sculpture_viz.stl, flag:
  - PENETRATION  : polyline points INSIDE the closed shell volume
  - BELOW_MESH   : polyline points below mesh z_min within its xy footprint
                   (i.e. fluid "leaked" out the bottom face)
  - GHOST_BEND   : polyline direction changes > 30 deg at a point > 1 m
                   from any mesh face (looks like impact but nothing's there)
  - FAR_END     : final polyline point > 1.5 m from any mesh face AND inside
                   pond xy column (should have settled or reached pond)

Usage:  python scripts/deep_inspect.py [gen_idx]
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent


def find_test_ids(gen: int) -> list:
    out = []
    for p in (PROJECT / "experiments").glob("test_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        gc = d.get("grid_cell")
        if gc and int(gc.get("gen", 0)) == gen:
            out.append((int(gc["var"]), d["test_id"]))
    out.sort()
    return [t for _, t in out]


def deep_inspect(test_id: str) -> dict:
    iter_dir = PROJECT / "runs" / f"iter_{test_id}"
    if not iter_dir.exists():
        return {"test_id": test_id, "error": "no_dir"}
    trails = json.loads((iter_dir / "trails.json").read_text(encoding="utf-8"))
    # Load SOLID mesh (closed shell) for inside-tests; fallback to viz.
    solid = iter_dir / "sculpture.stl"
    mesh = trimesh.load(solid if solid.exists() else iter_dir / "sculpture_viz.stl")

    # Penetration metric tracks NOZZLE particles only (the visible stream).
    # Prefill water sliding over a tilted rim briefly crosses the wall
    # geometry on its way out — physically correct, but my analytic check
    # flags it. Filter prefill (z0 < 28; nozzles sit at z=29) out.
    all_pts = []
    trail_ids = []
    is_nozzle = []
    for idp, pts in trails.items():
        if not pts: continue
        step = max(1, len(pts) // 12)
        nozzle = pts[0][2] >= 28.0
        for i in range(0, len(pts), step):
            all_pts.append(pts[i]); trail_ids.append(idp); is_nozzle.append(nozzle)
    all_pts = np.array(all_pts)
    is_nozzle = np.array(is_nozzle)

    # PENETRATION (analytic): for each module, find points BELOW the inner
    # cap surface that share the cap's xy footprint. These have "fallen
    # through" the bowl floor — a clear SPH leak.
    params = json.loads((PROJECT / "experiments" / f"{test_id}.json")
                        .read_text(encoding="utf-8"))
    mods = json.loads((PROJECT / "runs" / "_collider_modules.json")
                       .read_text(encoding="utf-8"))["modules"]
    base_by_idx = {m["index"]: m["base_point_mm"] for m in mods}
    is_penetrating = np.zeros(len(all_pts), dtype=bool)
    for m_cfg in params["modules"]:
        idx = m_cfg["index"]
        b = base_by_idx[idx]
        P = np.array([(b[0] + m_cfg["tx"]) / 1000.0,
                       (b[1] + m_cfg["ty"]) / 1000.0,
                       (b[2] + m_cfg["tz"]) / 1000.0])
        R = m_cfg["radius"] / 1000.0
        MZ = m_cfg["move_z"] / 1000.0
        OFF = m_cfg.get("offset_dist", 500.0) / 1000.0
        rx_rad = math.radians(m_cfg["rotation_x"])
        rz_rad = math.radians(m_cfg["rotation_z"])
        rel = all_pts - P
        cz, sz = math.cos(rz_rad), math.sin(rz_rad)
        rel_xy = np.stack([rel[:, 0] * cz + rel[:, 1] * sz,
                            -rel[:, 0] * sz + rel[:, 1] * cz,
                            rel[:, 2]], axis=1)
        cx, sx = math.cos(rx_rad), math.sin(rx_rad)
        local = np.stack([rel_xy[:, 0],
                          rel_xy[:, 1] * cx + rel_xy[:, 2] * sx,
                          -rel_xy[:, 1] * sx + rel_xy[:, 2] * cx], axis=1)
        r2 = local[:, 0] ** 2 + local[:, 1] ** 2
        r2_rim = R * R - MZ * MZ
        inside_rim = r2 < r2_rim
        # cap surface z in local (only defined where r2 <= R^2)
        safe_r2 = np.minimum(r2, R * R)
        z_surf = MZ - np.sqrt(np.maximum(R * R - safe_r2, 0))
        # Penetrating: between inner cap surface and outer cap surface (shell
        # thickness = OFF), AND inside rim circle.
        below_cap = ((local[:, 2] < z_surf - 0.05) & inside_rim
                      & (local[:, 2] > z_surf - OFF))
        is_penetrating |= below_cap
    # Nozzle-only penetration (the meaningful one — see comment above).
    noz_penet = is_penetrating & is_nozzle
    n_penetration = int(noz_penet.sum())
    n_traj_penetrating = len(set(trail_ids[i] for i in range(len(noz_penet))
                                   if noz_penet[i]))
    # Prefill spill (over-rim) reported separately for context.
    pre_spill = is_penetrating & ~is_nozzle
    n_prefill_spill = int(pre_spill.sum())

    # Distance from each point to mesh surface (unsigned)
    _, dists, _ = mesh.nearest.on_surface(all_pts)

    # BELOW_MESH: points below mesh z_min within xy footprint of mesh bbox
    mb = mesh.bounds
    in_xy = ((all_pts[:, 0] >= mb[0][0]) & (all_pts[:, 0] <= mb[1][0]) &
             (all_pts[:, 1] >= mb[0][1]) & (all_pts[:, 1] <= mb[1][1]))
    below = (all_pts[:, 2] < mb[0][2] - 0.5) & in_xy  # 0.5 m tolerance
    n_below = int(below.sum())

    # GHOST_BEND: per trail, count angle changes >30 deg between consecutive
    # full-trail segments. For the "distance to mesh at the bend point" we
    # compute fresh signed distance on bend points only.
    bend_pts = []
    for idp, pts in trails.items():
        if len(pts) < 3:
            continue
        pts_a = np.array(pts)
        segs = pts_a[1:] - pts_a[:-1]
        seg_lens = np.linalg.norm(segs, axis=1)
        valid = seg_lens > 0.01
        for i in range(len(segs) - 1):
            if not (valid[i] and valid[i + 1]):
                continue
            cos_a = np.dot(segs[i], segs[i + 1]) / (seg_lens[i] * seg_lens[i + 1])
            cos_a = max(-1.0, min(1.0, cos_a))
            ang_deg = math.degrees(math.acos(cos_a))
            if ang_deg > 30:
                bend_pts.append(pts[i + 1])
    ghost_bends = 0
    if bend_pts:
        bend_arr = np.array(bend_pts)
        _, bend_d, _ = mesh.nearest.on_surface(bend_arr)
        ghost_bends = int((bend_d > 1.0).sum())

    # FAR_END: final point > 1.5 m from mesh AND inside pond xy column
    pond_min = -5.20
    pond_max = 5.20
    end_pts = []
    end_meta = []
    for idp, pts in trails.items():
        if not pts: continue
        end = pts[-1]
        end_pts.append(end)
        end_meta.append(end)
    far_ends = 0
    if end_pts:
        end_arr = np.array(end_pts)
        _, end_d, _ = mesh.nearest.on_surface(end_arr)
        for i, e in enumerate(end_meta):
            in_pond_xy = pond_min <= e[0] <= pond_max and pond_min <= e[1] <= pond_max
            if end_d[i] > 1.5 and in_pond_xy and e[2] > 1.0:
                far_ends += 1

    return {
        "test_id": test_id,
        "n_total_points": len(all_pts),
        "n_nozzle_points": int(is_nozzle.sum()),
        "n_penetrating_points": n_penetration,
        "n_trajectories_penetrating": n_traj_penetrating,
        "n_prefill_spill": n_prefill_spill,
        "n_below_mesh_points": n_below,
        "n_ghost_bends": ghost_bends,
        "n_far_ends": far_ends,
    }


def main():
    gen = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    ids = find_test_ids(gen)
    print(f"=== Deep inspect gen_{gen:02d} ({len(ids)} cells) ===")
    print(f"{'cell':10s} {'pts':>6s} {'noz':>5s} {'NPENET':>6s} {'NPENtr':>6s} {'spill':>5s} {'below':>5s} {'ghost':>5s} {'farEnd':>6s}")
    for tid in ids:
        r = deep_inspect(tid)
        if r.get("error"):
            print(f"{tid:10s}  {r['error']}")
            continue
        print(f"{tid:10s} {r['n_total_points']:>6d} {r['n_nozzle_points']:>5d} "
              f"{r['n_penetrating_points']:>6d} {r['n_trajectories_penetrating']:>6d} "
              f"{r['n_prefill_spill']:>5d} {r['n_below_mesh_points']:>5d} "
              f"{r['n_ghost_bends']:>5d} {r['n_far_ends']:>6d}")


if __name__ == "__main__":
    main()
