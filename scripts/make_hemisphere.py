"""Generate a simple 4m hemisphere collider for testing FluidX3D physics
without the real sculpture's complexity.

Default: solid hemisphere, radius 4m, flat side down, dome top, centered at
the nozzle xy centroid (~4.13, -2.4) at z=8m (dome top at z=12m, ~17m below
nozzles at z=29m).

Output: runs/_hemi_collider.stl (binary, meters).

Run:  python scripts/make_hemisphere.py [radius_m=4] [cx_m=4.13] [cy_m=-2.4] [cz_m=8]
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent
OUT_STL = PROJECT / "runs" / "_hemi_collider.stl"


def main(argv: list[str]) -> int:
    r = float(argv[1]) if len(argv) > 1 else 4.0
    cx = float(argv[2]) if len(argv) > 2 else 4.13
    cy = float(argv[3]) if len(argv) > 3 else -2.4
    cz = float(argv[4]) if len(argv) > 4 else 8.0

    sphere = trimesh.creation.icosphere(subdivisions=4, radius=r)
    # cut bottom (z < 0 relative to sphere center) -> keep upper hemisphere
    half = trimesh.intersections.slice_mesh_plane(
        sphere, plane_normal=[0, 0, 1], plane_origin=[0, 0, 0], cap=True,
    )
    if half is None or len(half.faces) == 0:
        print("ERROR: hemisphere slice produced empty mesh")
        return 1
    if not half.is_watertight:
        print("WARNING: hemisphere not watertight after slice; attempting fix")
        half.fill_holes()

    # translate to (cx, cy, cz)
    half.apply_translation([cx, cy, cz])

    OUT_STL.parent.mkdir(parents=True, exist_ok=True)
    half.export(OUT_STL, file_type="stl")
    bb = half.bounds
    print(f"wrote {OUT_STL}")
    print(f"  radius={r}m  center=({cx},{cy},{cz})m")
    print(f"  bbox = [{bb[0]}] to [{bb[1]}]")
    print(f"  verts={len(half.vertices)} faces={len(half.faces)} watertight={half.is_watertight}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
