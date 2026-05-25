"""
Integration test: generate placeholder cube STL + call run_case.py end-to-end.
Validates XML template + automation pipeline before connecting to Grasshopper.

Run:
    python integration_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def write_cube_stl(stl_path: Path, size: float = 0.5) -> int:
    """Write a unit-cube ASCII STL centered at origin (sized=size)."""
    h = size / 2.0
    # 8 cube corners
    v = [
        (-h, -h, -h), ( h, -h, -h), ( h,  h, -h), (-h,  h, -h),  # bottom
        (-h, -h,  h), ( h, -h,  h), ( h,  h,  h), (-h,  h,  h),  # top
    ]
    # 12 triangles (2 per face) with outward normals
    tris = [
        # bottom (-Z)
        ((0, 0, -1), v[0], v[2], v[1]),
        ((0, 0, -1), v[0], v[3], v[2]),
        # top (+Z)
        ((0, 0,  1), v[4], v[5], v[6]),
        ((0, 0,  1), v[4], v[6], v[7]),
        # front (-Y)
        ((0, -1, 0), v[0], v[1], v[5]),
        ((0, -1, 0), v[0], v[5], v[4]),
        # right (+X)
        (( 1, 0, 0), v[1], v[2], v[6]),
        (( 1, 0, 0), v[1], v[6], v[5]),
        # back (+Y)
        ((0,  1, 0), v[2], v[3], v[7]),
        ((0,  1, 0), v[2], v[7], v[6]),
        # left (-X)
        ((-1, 0, 0), v[3], v[0], v[4]),
        ((-1, 0, 0), v[3], v[4], v[7]),
    ]
    lines = ["solid cube"]
    for n, a, b, c in tris:
        lines.append(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}")
        lines.append("    outer loop")
        for p in (a, b, c):
            lines.append(f"      vertex {p[0]:.6e} {p[1]:.6e} {p[2]:.6e}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid cube")
    stl_path.write_text("\n".join(lines), encoding="ascii")
    return len(tris)


def main():
    cube_stl = PROJECT_ROOT / "runs" / "_cube_test.stl"
    cube_stl.parent.mkdir(parents=True, exist_ok=True)
    n_tri = write_cube_stl(cube_stl, size=0.5)
    print(f"Wrote cube STL ({n_tri} triangles): {cube_stl}")

    params = {
        "sculpture_size": 0.6,
        "sculpture_angle": 15.0,
        "sculpture_height": 0.7,
        "nozzle_x": 0.0,
        "nozzle_y": 0.0,
        "nozzle_z": 2.0,
        "nozzle_diameter": 0.04,
        "nozzle_angle_x": 0.0,
        "nozzle_angle_y": 0.0,
        "flow_velocity": 3.0,
    }

    import run_case
    result = run_case.evaluate(
        params=params,
        stl_path=cube_stl,
        iter_id="cube_test",
        dp=0.025,           # coarser for fast test
        timemax=1.5,        # short for quick validation
        timeout=0.05,
        use_gpu=True,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "log"}, indent=2))
    if not result.get("ok"):
        print("--- LOG ---")
        print(result.get("log", ""))


if __name__ == "__main__":
    main()
