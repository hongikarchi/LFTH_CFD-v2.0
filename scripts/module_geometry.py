"""
Parametric module shape — Python translation of the Grasshopper definition.

GH graph (extracted via gh_read_via_rhinomcp.py):
    Inputs:  P (point), radius, move_z, rotation_x [deg], rotation_z [deg],
             offset_dist

    P_top = (P.x, P.y, P.z + move_z)
    Sphere   = sphere(P_top, radius)
    Disk     = horizontal disk at z=P.z, radius=radius (cutter plane)
    Pieces   = split(Sphere, Disk)         # 2 pieces along z=P.z
    Picked   = ListItem(Sort by centroid.Z asc, idx=0)
                                          = piece with LOWER centroid Z
                                          = bottom cap when move_z < radius
    M1       = rotate(Picked, axis=+X through P, angle=rotation_x)
    M2       = rotate(M1,      axis=+Z through P, angle=rotation_z)
    Output   = OffsetSurface(M2, distance=offset_dist,
                              both_sides=false, create_solid=true)

Parameters mapped to GA genes (default in parens):
    radius       [0, 10000]   (4000)
    move_z       [0, 10000]   (3368)
    rotation_x   [0, 90]      (23)
    rotation_z   [0, 360]     (0)
    offset_dist  [0, 100]     (50)

Units = millimeters (matches Rhino doc). Convert to SI elsewhere as needed.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import trimesh


def _thicken_open_mesh(
    open_mesh: trimesh.Trimesh,
    distance: float,
    normals: np.ndarray | None = None,
) -> trimesh.Trimesh:
    """Turn an open mesh (with boundary) into a closed thin-shell solid by
    offsetting along vertex normals and stitching the boundary loop.

    Matches GH OffsetSurface with `Both Sides=false, Create Solid=true` on an
    open surface input. The original (inner) surface keeps its connectivity,
    the offset (outer) surface is a copy shifted along outward normals, and the
    open boundary is bridged with a quad strip.

    normals: optional per-vertex unit normals. When the underlying surface has
             an analytic form (e.g., a sphere), passing exact normals avoids
             the slight volume mismatch from trimesh's area-weighted estimate.
    """
    inner_v = np.asarray(open_mesh.vertices, dtype=float)
    inner_f = np.asarray(open_mesh.faces, dtype=np.int64)
    if normals is None:
        normals = np.asarray(open_mesh.vertex_normals, dtype=float)
    else:
        normals = np.asarray(normals, dtype=float)

    outer_v = inner_v + normals * float(distance)
    n = len(inner_v)
    combined_v = np.vstack([inner_v, outer_v])

    # Inner surface: flip winding so its normals point INTO the solid wall
    # (so outward-facing normals on both shell sides point away from material).
    inner_f_flipped = inner_f[:, [0, 2, 1]]
    outer_f = inner_f + n

    # Boundary edges of the open mesh = edges incident to exactly one face.
    e = np.sort(open_mesh.edges, axis=1)
    cnt = Counter(map(tuple, e.tolist()))
    boundary = [pair for pair, c in cnt.items() if c == 1]

    # To consistently orient the stitch ribbon, walk each boundary edge in the
    # same direction as it appears in some face. Pull oriented edges from
    # `open_mesh.edges` (per-face triples) and keep only those whose sorted
    # form is in the boundary set.
    boundary_set = set(boundary)
    oriented = []
    for tri in inner_f:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (a, b) if a < b else (b, a)
            if key in boundary_set:
                oriented.append((int(a), int(b)))
    # Dedup while preserving direction
    seen = set()
    oriented_uniq = []
    for a, b in oriented:
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        oriented_uniq.append((a, b))

    stitch = []
    for a, b in oriented_uniq:
        ao, bo = a + n, b + n
        # Two triangles forming the quad (a -> b -> b_outer) + (a -> b_outer -> a_outer).
        # Winding chosen so the outward face points away from the inner mesh.
        stitch.append([a, b, bo])
        stitch.append([a, bo, ao])

    if stitch:
        combined_f = np.vstack([inner_f_flipped, outer_f, np.asarray(stitch, dtype=np.int64)])
    else:
        combined_f = np.vstack([inner_f_flipped, outer_f])

    out = trimesh.Trimesh(vertices=combined_v, faces=combined_f, process=True)
    # Fix winding globally so all face normals point outward.
    out.fix_normals()
    return out


def _build_spherical_cap(
    sphere_center: np.ndarray,
    sphere_radius: float,
    cut_z: float,
    take_below: bool,
    theta_segments: int = 64,
    phi_segments: int = 16,
) -> tuple[trimesh.Trimesh, np.ndarray]:
    """Analytically generate the spherical cap that lies on one side of a
    horizontal cut plane. Returns (mesh, per-vertex outward normals).

    The rim is placed EXACTLY at z = cut_z (closed loop of `theta_segments`
    boundary edges), avoiding the jagged sub-mesh boundary that
    icosphere-filtering produces. Mesh is OPEN (no disk lid) — the cap surface
    only.
    """
    cz = float(sphere_center[2])
    R = float(sphere_radius)
    cos_phi_cut = (cut_z - cz) / R
    cos_phi_cut = max(-1.0, min(1.0, cos_phi_cut))
    phi_cut = np.arccos(cos_phi_cut)  # angle from +Z to cut plane on the sphere

    if take_below:
        # Cap occupies phi in [phi_cut, pi]; pole at phi=pi (bottom of sphere)
        phis = np.linspace(phi_cut, np.pi, phi_segments + 1)
    else:
        # Cap occupies phi in [0, phi_cut]; pole at phi=0 (top of sphere)
        phis = np.linspace(0.0, phi_cut, phi_segments + 1)

    thetas = np.linspace(0.0, 2.0 * np.pi, theta_segments, endpoint=False)

    # Build vert grid: rows = phi (rim row first), columns = theta
    verts = []
    for phi in phis:
        sp, cp = np.sin(phi), np.cos(phi)
        for theta in thetas:
            x = sphere_center[0] + R * sp * np.cos(theta)
            y = sphere_center[1] + R * sp * np.sin(theta)
            z = sphere_center[2] + R * cp
            verts.append([x, y, z])
    # Pole vertex (last vert)
    if take_below:
        pole = [sphere_center[0], sphere_center[1], sphere_center[2] - R]
    else:
        pole = [sphere_center[0], sphere_center[1], sphere_center[2] + R]
    verts.append(pole)
    verts = np.asarray(verts, dtype=float)

    n_theta = theta_segments
    n_rows = phi_segments + 1
    pole_idx = len(verts) - 1

    faces = []
    # Quads between consecutive rings, triangulated
    for r in range(n_rows - 1):
        # The pole pole-side ring (last ring in the parameterization) shrinks
        # to the pole vertex; we will replace those quads with triangles.
        if r == n_rows - 2:
            # Triangles connecting the second-to-last ring to the pole vertex
            for t in range(n_theta):
                t1 = t
                t2 = (t + 1) % n_theta
                v_curr = r * n_theta + t1
                v_next = r * n_theta + t2
                # Winding so face normals point outward from sphere center
                if take_below:
                    faces.append([v_curr, v_next, pole_idx])
                else:
                    faces.append([v_curr, pole_idx, v_next])
        else:
            for t in range(n_theta):
                t1 = t
                t2 = (t + 1) % n_theta
                a = r * n_theta + t1
                b = r * n_theta + t2
                c = (r + 1) * n_theta + t2
                d = (r + 1) * n_theta + t1
                # Quad (a, b, c, d). Triangulate (a, b, c) + (a, c, d).
                if take_below:
                    faces.append([a, b, c])
                    faces.append([a, c, d])
                else:
                    faces.append([a, c, b])
                    faces.append([a, d, c])

    faces = np.asarray(faces, dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)

    # Analytic outward normals: (v - center) / |v - center|
    rel = verts - np.asarray(sphere_center, dtype=float)
    norms = np.linalg.norm(rel, axis=1, keepdims=True)
    normals = rel / np.where(norms > 1e-9, norms, 1.0)
    return mesh, normals


def build_module_mesh(
    point: tuple[float, float, float],
    radius: float,
    move_z: float,
    rotation_x_deg: float,
    rotation_z_deg: float,
    offset_dist: float = 50.0,
    theta_segments: int = 64,
    phi_segments: int = 16,
) -> trimesh.Trimesh:
    """Return the parametric module mesh in mm (Rhino doc units).

    Cap is generated analytically (theta x phi grid on the sphere), giving an
    exact rim at z = P.z. theta_segments / phi_segments control resolution.

    offset_dist:  OffsetSurface distance applied along analytic radial normals
                  after rotations. Matches GH OffsetSurface w/ Both Sides=false,
                  Create Solid=true on an open spherical cap (input from GH
                  SplitBrep is open — single face, no disk lid).
    """
    P = np.asarray(point, dtype=float)
    P_top = P + np.array([0.0, 0.0, float(move_z)])

    # 1+2+3) Generate both candidate spherical caps (below/above the cutter
    # plane) analytically with rim exactly at z=P.z, then pick the one with the
    # lower Z centroid — same selection rule as GH ListItem(SortList(Keys=Z))[0].
    bottom, n_bot = _build_spherical_cap(
        P_top, float(radius), cut_z=float(P[2]), take_below=True,
        theta_segments=theta_segments, phi_segments=phi_segments,
    )
    top, n_top = _build_spherical_cap(
        P_top, float(radius), cut_z=float(P[2]), take_below=False,
        theta_segments=theta_segments, phi_segments=phi_segments,
    )
    pieces = [(bottom, n_bot), (top, n_top)]
    pieces.sort(key=lambda x: float(x[0].centroid[2]))
    picked, analytic_normals = pieces[0]

    # 4) Rotate about world +X axis through P by rotation_x degrees
    rx = np.radians(float(rotation_x_deg))
    T_rx = trimesh.transformations.rotation_matrix(rx, [1.0, 0.0, 0.0], P)
    picked.apply_transform(T_rx)
    analytic_normals = analytic_normals @ T_rx[:3, :3].T

    # 5) Rotate about world +Z axis through P by rotation_z degrees
    rz = np.radians(float(rotation_z_deg))
    T_rz = trimesh.transformations.rotation_matrix(rz, [0.0, 0.0, 1.0], P)
    picked.apply_transform(T_rz)
    analytic_normals = analytic_normals @ T_rz[:3, :3].T

    # 6) OffsetSurface (Both Sides=false, Create Solid=true).
    # Open cap is thickened into a closed thin-shell solid by offsetting along
    # analytic outward normals and bridging the boundary loop.
    if abs(float(offset_dist)) > 1e-9:
        picked = _thicken_open_mesh(picked, float(offset_dist),
                                     normals=analytic_normals)

    return picked


GENE_BOUNDS = {
    "radius": (1000.0, 8000.0),
    "move_z": (500.0, 9500.0),
    "rotation_x": (0.0, 90.0),
    "rotation_z": (0.0, 360.0),
    # Shell must be thick enough to prevent fluid leaking through the mDBC
    # boundary kernel. Empirically >= 700 mm at dp=0.20 keeps the
    # below-cap penetration count under 1% of fluid particles.
    "offset_dist": (700.0, 1500.0),
    "tx": (-3000.0, 3000.0),
    "ty": (-3000.0, 3000.0),
    "tz": (-2000.0, 2000.0),
}
GENE_ORDER = ["radius", "move_z", "rotation_x", "rotation_z",
              "offset_dist", "tx", "ty", "tz"]


def build_module_fill_particles(
    point_mm: tuple,
    radius_mm: float,
    move_z_mm: float,
    rotation_x_deg: float,
    rotation_z_deg: float,
    dp_m: float,
) -> list:
    """Discrete fluid particles (in METERS) pre-filling the (possibly tilted)
    module bowl up to a horizontal water surface in the world frame.

    World-frame sampling: iterate a dp grid in world (x, y, z) around the cap,
    inverse-rotate each candidate to local, accept if it lies inside the bowl
    (above cap surface, below rim margin, within rim radius) AND below the
    world water level (lowest rim z in world).
    """
    P = np.asarray(point_mm, dtype=float)
    R = float(radius_mm); MZ = float(move_z_mm)
    if MZ >= R - 1.0:
        return []
    r_rim = np.sqrt(R * R - MZ * MZ)
    dp_mm = dp_m * 1000.0
    if r_rim < dp_mm * 0.6:
        return []

    rx = np.radians(float(rotation_x_deg))
    rz = np.radians(float(rotation_z_deg))
    sin_rx, cos_rx = np.sin(rx), np.cos(rx)
    sin_rz, cos_rz = np.sin(rz), np.cos(rz)

    # World water level (relative to P, after rotation): lowest rim - 1*dp.
    z_water_world_rel = -r_rim * abs(sin_rx) - 1.0 * dp_mm

    R2 = R * R
    rim_r2 = R2 - MZ * MZ - (1.0 * dp_mm) ** 2
    n_xy = int(np.ceil(2 * r_rim / dp_mm)) + 1
    coords = (np.arange(n_xy) + 0.5) * dp_mm - n_xy * 0.5 * dp_mm

    out = []
    for xl in coords:
        for yl in coords:
            r2 = xl * xl + yl * yl
            if r2 > rim_r2:
                continue
            z_surf = MZ - np.sqrt(max(R2 - r2, 0.0))
            # 0.5*dp above the inner cap (standard SPH first-fluid offset)
            zl_min = z_surf + 0.5 * dp_mm
            # 1*dp below the rim plane (avoids placing the top layer at the
            # free surface where SPH initial transients cause sloshing)
            zl_max = -1.0 * dp_mm
            # World water constraint
            if cos_rx > 1e-6:
                zl_water = (z_water_world_rel - yl * sin_rx) / cos_rx
                zl_max = min(zl_max, zl_water)
            range_zl = zl_max - zl_min
            if range_zl < dp_mm:
                continue
            n_z = int(np.floor(range_zl / dp_mm))
            for k in range(n_z):
                zl = zl_min + (k + 0.5) * dp_mm
                if zl > zl_max:
                    break
                y1 = yl * cos_rx - zl * sin_rx
                z1 = yl * sin_rx + zl * cos_rx
                x1 = xl
                xw = x1 * cos_rz - y1 * sin_rz
                yw = x1 * sin_rz + y1 * cos_rz
                zw = z1
                out.append(((P[0] + xw) * 0.001,
                            (P[1] + yw) * 0.001,
                            (P[2] + zw) * 0.001))
    return out


def _build_module_viz_mesh(point_mm, radius, move_z, rx_deg, rz_deg,
                            theta_segments=64, phi_segments=16,
                            sph_standoff_mm: float = 400.0):
    """Build the cap surface for Rhino visualisation, shifted INWARD (toward
    the sphere centre) by `sph_standoff_mm` so the visible mesh sits at the
    location where SPH fluid particles actually stop — i.e., the kernel
    standoff distance ~ 1*dp. Avoids the visual "fluid bounces in mid-air"
    artifact when comparing polylines against the bake.

    sph_standoff_mm: typical ~ dp (200 mm at dp=0.20 m).
    """
    P = np.asarray(point_mm, dtype=float)
    P_top = P + np.array([0.0, 0.0, float(move_z)])
    R_viz = max(float(radius) - float(sph_standoff_mm), 50.0)

    bottom, _ = _build_spherical_cap(
        P_top, R_viz, cut_z=float(P[2]), take_below=True,
        theta_segments=theta_segments, phi_segments=phi_segments,
    )
    top, _ = _build_spherical_cap(
        P_top, R_viz, cut_z=float(P[2]), take_below=False,
        theta_segments=theta_segments, phi_segments=phi_segments,
    )
    pieces = [bottom, top]
    pieces.sort(key=lambda m: float(m.centroid[2]))
    picked = pieces[0]
    rx = np.radians(float(rx_deg))
    rz = np.radians(float(rz_deg))
    picked.apply_transform(trimesh.transformations.rotation_matrix(rx, [1, 0, 0], P))
    picked.apply_transform(trimesh.transformations.rotation_matrix(rz, [0, 0, 1], P))
    return picked


def build_modules_combined_stl(
    modules_info: list,
    genes_by_index: dict,
    stl_out: Path,
    also_save_viz: bool = True,
) -> dict:
    """Build N parametric modules from genes, write a single ASCII STL of the
    combined mesh in METERS (DualSPHysics units).

    modules_info: list of dicts with keys `index` and `base_point_mm` (top-center
                  of the original collider bbox). The Python module is placed
                  at base_point_mm + (tx, ty, tz).
    genes_by_index: {int_idx: {radius, move_z, rotation_x, rotation_z,
                                offset_dist, tx, ty, tz}}
    Returns summary dict.
    """
    combined_verts = []
    combined_faces = []
    n_verts = 0
    per_module = []
    for m in modules_info:
        idx = m["index"]
        g = genes_by_index.get(idx)
        if g is None:
            continue
        base = np.asarray(m["base_point_mm"], dtype=float)
        P_mm = base + np.array([float(g["tx"]), float(g["ty"]), float(g["tz"])])
        mesh = build_module_mesh(
            tuple(P_mm),
            radius=float(g["radius"]),
            move_z=float(g["move_z"]),
            rotation_x_deg=float(g["rotation_x"]),
            rotation_z_deg=float(g["rotation_z"]),
            offset_dist=float(g["offset_dist"]),
        )
        # Convert mm -> meters
        verts_m = np.asarray(mesh.vertices, dtype=float) * 0.001
        faces = np.asarray(mesh.faces, dtype=np.int64) + n_verts
        combined_verts.append(verts_m)
        combined_faces.append(faces)
        n_verts += len(verts_m)
        per_module.append({
            "index": idx,
            "P_mm": P_mm.tolist(),
            "bbox_m": (mesh.bounds * 0.001).tolist(),
            "verts": int(len(verts_m)),
            "tris": int(len(mesh.faces)),
            "watertight": bool(mesh.is_watertight),
            "volume_mm3": float(mesh.volume),
        })

    V = np.vstack(combined_verts)
    F = np.vstack(combined_faces)
    big = trimesh.Trimesh(vertices=V, faces=F, process=False)
    big.fix_normals()
    big.export(stl_out, file_type="stl")  # binary STL (FluidX3D requires binary)

    out = {
        "stl_path": str(stl_out),
        "total_verts": int(len(V)),
        "total_tris": int(len(F)),
        "per_module": per_module,
    }

    if also_save_viz:
        viz_combined_v = []
        viz_combined_f = []
        nv = 0
        for m in modules_info:
            idx = m["index"]
            g = genes_by_index.get(idx)
            if g is None: continue
            base = np.asarray(m["base_point_mm"], dtype=float)
            P_mm = base + np.array([float(g["tx"]), float(g["ty"]), float(g["tz"])])
            try:
                viz = _build_module_viz_mesh(
                    tuple(P_mm), float(g["radius"]), float(g["move_z"]),
                    float(g["rotation_x"]), float(g["rotation_z"]),
                )
                vv = np.asarray(viz.vertices, dtype=float) * 0.001
                vf = np.asarray(viz.faces, dtype=np.int64) + nv
                viz_combined_v.append(vv); viz_combined_f.append(vf)
                nv += len(vv)
            except Exception:
                continue
        if viz_combined_v:
            VV = np.vstack(viz_combined_v); VF = np.vstack(viz_combined_f)
            viz_mesh = trimesh.Trimesh(vertices=VV, faces=VF, process=False)
            viz_path = stl_out.with_name(stl_out.stem + "_viz.stl")
            viz_mesh.export(viz_path, file_type="stl_ascii")
            out["viz_stl_path"] = str(viz_path)
    return out

DEFAULTS = {
    "radius": 6000.0,
    "move_z": 4500.0,       # cap_depth = 1500 mm
    "rotation_x": 0.0,      # flat — guarantees water holds
    "rotation_z": 0.0,
    "offset_dist": 500.0,   # >= 2*dp_mm at dp=0.20 to seal mDBC boundary
}


def _demo():
    """Build with GH defaults and write to runs/_module_demo.stl for visual check."""
    from pathlib import Path
    P = (0.0, 0.0, 0.0)
    m = build_module_mesh(
        P,
        radius=DEFAULTS["radius"],
        move_z=DEFAULTS["move_z"],
        rotation_x_deg=DEFAULTS["rotation_x"],
        rotation_z_deg=DEFAULTS["rotation_z"],
        offset_dist=DEFAULTS["offset_dist"],
    )
    out = Path(__file__).resolve().parent.parent / "runs" / "_module_demo.stl"
    m.export(out)
    print(f"verts={len(m.vertices)}  tris={len(m.faces)}  "
          f"bbox_min={m.bounds[0].tolist()}  bbox_max={m.bounds[1].tolist()}")
    print(f"wrote {out}")


if __name__ == "__main__":
    _demo()
