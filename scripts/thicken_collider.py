"""Thicken an open-shell mesh into a closed manifold so FluidX3D
voxelize_mesh_on_device (point-in-mesh test) can register it as TYPE_S.

Approach: for each vertex, offset along -vertex_normal by `thickness_m` to make
an inner shell. Stitch the outer<->inner shells along boundary (naked) edges.
Watertight if input is manifold with naked edges (open surface).

Input:  runs/_real_collider.stl (binary STL in meters)
Output: runs/_real_collider_thickened.stl
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent
IN_STL = PROJECT / "runs" / "_real_collider.stl"
OUT_STL = PROJECT / "runs" / "_real_collider_thickened.stl"
DEFAULT_THICKNESS_M = 0.1  # 100 mm (slightly > dp=0.08 so a cell sits fully inside)


def boundary_edges(mesh: trimesh.Trimesh) -> list[tuple[int, int]]:
    """Edges that appear in exactly one face = naked edges."""
    sorted_edges = np.sort(mesh.edges, axis=1)
    counter = Counter(map(tuple, sorted_edges))
    return [e for e, c in counter.items() if c == 1]


def thicken_shell(mesh: trimesh.Trimesh, thickness_m: float,
                  inward: bool = True) -> trimesh.Trimesh:
    """Build a closed manifold by offsetting the surface along vertex normals
    and stitching boundary edges. `inward=True` offsets along -normal."""
    sign = -1.0 if inward else 1.0
    outer = mesh.vertices.copy()
    inner = outer + sign * mesh.vertex_normals * thickness_m
    V = np.vstack([outer, inner])
    N0 = len(outer)

    outer_faces = mesh.faces.copy()
    # inner faces: same triangle but reversed winding so its outward normal
    # points in the opposite direction (the resulting shell is closed)
    inner_faces = mesh.faces[:, ::-1] + N0

    # stitch every naked edge with two triangles forming a quad strip
    bnd = boundary_edges(mesh)
    stitch = []
    for v0, v1 in bnd:
        stitch.append([v0, v1, v1 + N0])
        stitch.append([v0, v1 + N0, v0 + N0])
    stitch_arr = np.asarray(stitch, dtype=np.int64) if stitch else np.zeros((0, 3), dtype=np.int64)

    F = np.vstack([outer_faces, inner_faces, stitch_arr])
    out = trimesh.Trimesh(vertices=V, faces=F, process=True)
    return out


def main(argv: list[str]) -> int:
    thickness = DEFAULT_THICKNESS_M
    if len(argv) >= 2:
        try:
            thickness = float(argv[1])
        except ValueError:
            print(f"usage: {argv[0]} [thickness_m={DEFAULT_THICKNESS_M}]")
            return 1
    if not IN_STL.exists():
        print(f"ERROR: {IN_STL} missing. Run extract_targets.py first.")
        return 1
    mesh = trimesh.load(IN_STL)
    print(f"loaded {IN_STL}")
    print(f"  verts={len(mesh.vertices)} faces={len(mesh.faces)} closed={mesh.is_watertight}")
    if mesh.is_watertight:
        print("  already watertight — copying through without thickening")
        mesh.export(OUT_STL, file_type="stl")
        return 0

    # process per connected component so each shell keeps its own inside/outside
    components = mesh.split(only_watertight=False)
    print(f"  split into {len(components)} connected components")

    thickened_parts = []
    for i, comp in enumerate(components):
        if len(comp.faces) < 4:
            print(f"  comp {i}: skipping (too few faces: {len(comp.faces)})")
            continue
        if comp.is_watertight:
            thickened_parts.append(comp)
            print(f"  comp {i}: already closed, kept as-is ({len(comp.faces)} faces)")
            continue
        try:
            # Build BOTH shells; pick the one whose volume is closest to
            # surface_area * thickness (= true shell). The other direction
            # tends to flood-fill the concave interior of dishes, turning a
            # bowl into a solid cap that blocks water.
            shell_in = thicken_shell(comp, thickness, inward=True)
            shell_out = thicken_shell(comp, thickness, inward=False)
            # use abs(.volume) directly -- trimesh returns it even if is_volume=False
            try:
                vol_in = abs(float(shell_in.volume))
            except Exception:
                vol_in = float("inf")
            try:
                vol_out = abs(float(shell_out.volume))
            except Exception:
                vol_out = float("inf")
            target_vol = float(comp.area) * thickness
            pick_out = abs(vol_out - target_vol) < abs(vol_in - target_vol)
            shell = shell_out if pick_out else shell_in
            tag = "outward" if pick_out else "inward"
            print(f"  comp {i}: thickened {len(comp.faces)} -> {len(shell.faces)} faces  "
                  f"dir={tag}  vol_in={vol_in:.3f} vol_out={vol_out:.3f} target={target_vol:.3f}  "
                  f"watertight={shell.is_watertight}")
            thickened_parts.append(shell)
        except Exception as e:
            print(f"  comp {i}: thicken FAILED ({e}), keeping original")
            thickened_parts.append(comp)

    combined = trimesh.util.concatenate(thickened_parts)
    n_water = sum(1 for c in combined.split(only_watertight=True))
    n_total = sum(1 for c in combined.split(only_watertight=False))
    print(f"combined: verts={len(combined.vertices)} faces={len(combined.faces)} "
          f"watertight_components={n_water}/{n_total}")
    combined.export(OUT_STL, file_type="stl")
    print(f"wrote {OUT_STL}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
