"""
Run CFD on the real Rhino-extracted sculpture (architectural scale).
Reads runs/_real_geom.json + runs/_real_sculpture.stl, configures
domain/pond/nozzle to match, picks dp scaled for the ~30m height.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

GEOM_JSON = PROJECT / "runs" / "_real_geom.json"
STL_PATH = PROJECT / "runs" / "_real_sculpture.stl"


def main():
    geom = json.loads(GEOM_JSON.read_text(encoding="utf-8"))

    # Collider bbox -> domain (add safety margin)
    coll_min = geom["collider_bbox_m"][0]
    coll_max = geom["collider_bbox_m"][1]
    pond_min = geom["pond_bbox_m"][0]
    pond_max = geom["pond_bbox_m"][1]
    nozzle_center = geom["nozzle_center_m"]

    domain_xmin = min(coll_min[0], pond_min[0]) - 3.0
    domain_xmax = max(coll_max[0], pond_max[0]) + 3.0
    domain_ymin = min(coll_min[1], pond_min[1]) - 3.0
    domain_ymax = max(coll_max[1], pond_max[1]) + 3.0
    domain_zmin = pond_min[2] - 1.0
    domain_zmax = max(nozzle_center[2], coll_max[2]) + 2.0

    domain_override = {
        "domain_xmin": domain_xmin, "domain_xmax": domain_xmax,
        "domain_ymin": domain_ymin, "domain_ymax": domain_ymax,
        "domain_zmin": domain_zmin, "domain_zmax": domain_zmax,
    }

    # Pond AABB from end_point
    pond_override = {
        "pond_xmin": pond_min[0],
        "pond_ymin": pond_min[1],
        "pond_xsize": pond_max[0] - pond_min[0],
        "pond_ysize": pond_max[1] - pond_min[1],
        "pond_thickness": 0.5,   # thicker so SPH can build a basin
    }

    # Nozzle params: place at recovered XY, drop a small inlet right below the
    # 28.92m plate. Use a small diameter (we are not the size of the plate).
    free_fall_h = max(0.0, nozzle_center[2] - pond_max[2])
    import math
    impact_v = math.sqrt(2 * 9.81 * free_fall_h)
    sound = max(80.0, 12.0 * impact_v)   # SPH speedsound rule of thumb

    # Multi-hole nozzle (B option): 5 holes distributed across start_point plate
    holes = geom.get("nozzle_holes_m") or [nozzle_center]
    # Shift each hole slightly below the plate so the inlet box clears the boundary
    holes = [(h[0], h[1], h[2] - 0.05) for h in holes]

    params = {
        "sculpture_size": 1.0,
        "sculpture_angle": 0.0,
        "sculpture_height": 0.0,
        "nozzle_x": holes[0][0],   # legacy (first hole)
        "nozzle_y": holes[0][1],
        "nozzle_z": holes[0][2],
        "nozzle_holes": holes,     # multi-hole
        "nozzle_diameter": 0.10,   # 10cm per hole
        "nozzle_angle_x": 0.0,
        "nozzle_angle_y": 0.0,
        "flow_velocity": 10.0,     # terminal velocity for droplet rain (single-phase SPH approx)
    }

    # dp scaled for architectural domain
    dp = 0.10
    timemax = 6.0
    timeout = 0.10

    extra_patches = {
        "speedsound": int(sound),
        "cflnumber": 0.30,
        "DensityDT": 1,
        "Visco": 0.05,
    }

    print(f"Domain (m):       [{domain_xmin:.1f}, {domain_xmax:.1f}] x "
          f"[{domain_ymin:.1f}, {domain_ymax:.1f}] x [{domain_zmin:.1f}, {domain_zmax:.1f}]")
    print(f"Free fall:        {free_fall_h:.2f} m  -> impact {impact_v:.1f} m/s")
    print(f"Speedsound:       {extra_patches['speedsound']} m/s")
    print(f"dp:               {dp} m")
    print(f"Nozzle XY/Z:      ({params['nozzle_x']:.2f}, {params['nozzle_y']:.2f}, {params['nozzle_z']:.2f})")
    print(f"Pond AABB (m):    {pond_override}")

    from run_case import evaluate
    t0 = time.time()
    result = evaluate(
        params=params,
        stl_path=STL_PATH,
        iter_id="real_sculpture_v1",
        dp=dp,
        timemax=timemax,
        timeout=timeout,
        use_gpu=False,                # CPU still wins at these particle counts
        mode="fast",
        domain_override=domain_override,
        pond_override=pond_override,
        extra_xml_patches=extra_patches,
    )
    print(f"\nWall time: {time.time()-t0:.1f}s")
    print(json.dumps({k: v for k, v in result.items() if k != "log"}, indent=2))
    if not result.get("ok"):
        print("--- LOG tail ---")
        print(result.get("log", "")[-2000:])


if __name__ == "__main__":
    main()
