"""Ellipsoid oblique-cut parametric module generator.

Builds bowl-like modules from the lower cap of an ellipsoid. The ellipsoid uses
independent X/Y/Z axes, is tilted first, then cut by a plane hinged at the +X
vertical-tangent point. The simulation STL is a closed solid: inner cap, outer
normal-offset cap, and stitched rim. All geometry is authored in millimeters;
STL export scales vertices to meters for FluidX3D.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

Vec3 = tuple[float, float, float]
Face = tuple[int, int, int]

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent

DEFAULT_MODULES_JSON = REPO_ROOT / "env_fx3d" / "runs" / "_collider_modules.json"
DEFAULT_SOLID_STL = REPO_ROOT / "env_fx3d" / "runs" / "_ellipsoid_parametric.stl"
DEFAULT_VIZ_STL = REPO_ROOT / "env_fx3d" / "runs" / "_ellipsoid_parametric_viz.stl"
DEFAULT_META_JSON = REPO_ROOT / "env_fx3d" / "runs" / "_ellipsoid_parametric_meta.json"


@dataclass
class Mesh:
    vertices: list[Vec3]
    faces: list[Face]


@dataclass
class ModuleParams:
    axis_x_mm: float
    axis_y_mm: float
    axis_z_mm: float
    ellipsoid_tilt_deg: float = 0.0
    cut_drop_deg: float = 0.0
    wall_thickness_mm: float = 500.0
    rim_lift_mm: float = 0.0
    rotation_z_deg: float = 0.0
    tx_mm: float = 0.0
    ty_mm: float = 0.0
    tz_mm: float = 0.0


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _normalize(a: Vec3) -> Vec3:
    n = _length(a)
    if n <= 1.0e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / n, a[1] / n, a[2] / n)


def _bbox(vertices: list[Vec3]) -> list[list[float]]:
    if not vertices:
        return [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    return [
        [min(v[i] for v in vertices) for i in range(3)],
        [max(v[i] for v in vertices) for i in range(3)],
    ]


def _bbox_extents(bounds: list[list[float]]) -> list[float]:
    return [bounds[1][i] - bounds[0][i] for i in range(3)]


def _round_nested(value, digits: int = 3):
    if isinstance(value, (list, tuple)):
        return [_round_nested(v, digits) for v in value]
    if isinstance(value, float):
        return round(value, digits)
    return value


def _validate_params(params: ModuleParams) -> None:
    if params.axis_x_mm <= 0.0:
        raise ValueError("axis_x_mm must be positive")
    if params.axis_y_mm <= 0.0:
        raise ValueError("axis_y_mm must be positive")
    if params.axis_z_mm <= 0.0:
        raise ValueError("axis_z_mm must be positive")
    if not (0.0 <= params.ellipsoid_tilt_deg < 60.0):
        raise ValueError("ellipsoid_tilt_deg must be in [0, 60)")
    if not (0.0 <= params.cut_drop_deg <= 35.0):
        raise ValueError("cut_drop_deg must be in [0, 35]")
    if params.ellipsoid_tilt_deg + params.cut_drop_deg >= 75.0:
        raise ValueError("ellipsoid_tilt_deg + cut_drop_deg must be < 75")
    if params.wall_thickness_mm <= 0.0:
        raise ValueError("wall_thickness_mm must be positive")
    if params.rim_lift_mm < 0.0:
        raise ValueError("rim_lift_mm must be non-negative")


def derived_geometry(params: ModuleParams) -> dict[str, float | tuple[float, float, float]]:
    """Return internal ellipsoid axes and local cut plane values."""
    _validate_params(params)
    axis_x = float(params.axis_x_mm)
    axis_y = float(params.axis_y_mm)
    axis_z = float(params.axis_z_mm)
    tilt = math.radians(float(params.ellipsoid_tilt_deg))
    drop = math.radians(float(params.cut_drop_deg))
    local_cut_angle = tilt + drop
    tangent_slope = math.tan(tilt)
    slope = math.tan(local_cut_angle)
    drop_slope = math.tan(drop)
    denom = math.sqrt(axis_x * axis_x + axis_z * axis_z * tangent_slope * tangent_slope)
    tangent_x = axis_x * axis_x / denom
    tangent_z = axis_z * axis_z * tangent_slope / denom
    cut_world_z = -tangent_x * math.sin(tilt) + tangent_z * math.cos(tilt)
    cut_offset_z = tangent_z - slope * tangent_x
    plane_normal = _normalize((-slope, 0.0, 1.0))
    world_tangent_x = tangent_x * math.cos(tilt) + tangent_z * math.sin(tilt)
    return {
        "axis_x_mm": axis_x,
        "axis_y_mm": axis_y,
        "axis_z_mm": axis_z,
        "center_z_mm": 0.0,
        "vertical_tangent_point_mm": (tangent_x, 0.0, tangent_z),
        "world_tangent_point_mm": (world_tangent_x, 0.0, cut_world_z),
        "local_cut_angle_deg": math.degrees(local_cut_angle),
        "cut_slope_x": slope,
        "cut_slope_y": 0.0,
        "cut_offset_z_mm": cut_offset_z,
        "cut_drop_slope": drop_slope,
        "cut_world_z_mm": cut_world_z,
        "cut_plane_normal": plane_normal,
    }


def _cut_plane_z(params: ModuleParams, x: float, y: float) -> float:
    geom = derived_geometry(params)
    return (
        float(geom["cut_slope_x"]) * x
        + float(geom["cut_slope_y"]) * y
        + float(geom["cut_offset_z_mm"])
    )


def _cut_plane_normal(params: ModuleParams) -> Vec3:
    return derived_geometry(params)["cut_plane_normal"]  # type: ignore[return-value]


def _rim_intersection_p(params: ModuleParams, theta: float, geom: dict[str, float | tuple[float, float, float]]) -> float:
    """Find latitude p where the ellipsoid ray intersects the oblique cut plane."""
    a = float(geom["axis_x_mm"])
    b = float(geom["axis_y_mm"])
    c = float(geom["axis_z_mm"])
    center_z = float(geom["center_z_mm"])
    sx = float(geom["cut_slope_x"])
    sy = float(geom["cut_slope_y"])
    offset_z = float(geom["cut_offset_z_mm"])
    ct = math.cos(theta)
    st = math.sin(theta)

    def f(p: float) -> float:
        radius_factor = math.sqrt(max(0.0, 1.0 - p * p))
        x = a * radius_factor * ct
        y = b * radius_factor * st
        z = center_z + c * p
        return z - (sx * x + sy * y + offset_z)

    eps = max(1.0e-7, c * 1.0e-12)
    lo = -1.0 + eps
    hi = 1.0 - eps
    f_lo = f(lo)
    f_hi = f(hi)
    if f_lo >= 0.0:
        return lo
    if f_hi <= 0.0:
        return hi
    for _ in range(52):
        mid = (lo + hi) * 0.5
        if f(mid) >= 0.0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) * 0.5


def default_params_from_module(
    module_info: dict,
    *,
    wall_thickness_mm: float = 500.0,
    rim_lift_mm: float = 0.0,
) -> ModuleParams:
    """Create a bbox-matched preset from one Rhino-extracted module entry."""
    bounds = module_info["bbox_mm"]
    ext = [bounds[1][i] - bounds[0][i] for i in range(3)]
    wall = min(float(wall_thickness_mm), max(50.0, ext[2] * 0.35))
    axis_x = max(100.0, (ext[0] - 2.0 * wall) * 0.5)
    axis_y = max(100.0, (ext[1] - 2.0 * wall) * 0.5)
    axis_z = max(100.0, ext[2] - wall - float(rim_lift_mm))
    return ModuleParams(
        axis_x_mm=axis_x,
        axis_y_mm=axis_y,
        axis_z_mm=axis_z,
        wall_thickness_mm=wall,
        rim_lift_mm=float(rim_lift_mm),
    )


def _inner_cap(
    params: ModuleParams,
    *,
    theta_segments: int,
    radial_segments: int,
) -> tuple[list[Vec3], list[Face], list[Vec3], list[int]]:
    """Build an open lower ellipsoid cap with outward ellipsoid normals."""
    _validate_params(params)
    if theta_segments < 8:
        raise ValueError("theta_segments must be >= 8")
    if radial_segments < 3:
        raise ValueError("radial_segments must be >= 3")

    geom = derived_geometry(params)
    a = float(geom["axis_x_mm"])
    b = float(geom["axis_y_mm"])
    c = float(geom["axis_z_mm"])
    center_z = float(geom["center_z_mm"])
    thetas = [2.0 * math.pi * (t / theta_segments) for t in range(theta_segments)]
    rim_ps = [_rim_intersection_p(params, theta, geom) for theta in thetas]

    vertices: list[Vec3] = [(0.0, 0.0, center_z - c)]
    normals: list[Vec3] = [(0.0, 0.0, -1.0)]
    rings: list[list[int]] = [[0]]

    for r in range(1, radial_segments + 1):
        ring: list[int] = []
        for t in range(theta_segments):
            theta = thetas[t]
            p = -1.0 + (rim_ps[t] + 1.0) * (r / radial_segments)
            p = max(-1.0, min(1.0, p))
            radius_factor = math.sqrt(max(0.0, 1.0 - p * p))
            z = center_z + c * p
            x = a * radius_factor * math.cos(theta)
            y = b * radius_factor * math.sin(theta)
            vertices.append((x, y, z))
            gradient = (x / (a * a), y / (b * b), (z - center_z) / (c * c))
            normals.append(_normalize(gradient))
            ring.append(len(vertices) - 1)
        rings.append(ring)

    faces: list[Face] = []
    first = rings[1]
    for t in range(theta_segments):
        faces.append((0, first[(t + 1) % theta_segments], first[t]))

    for r in range(1, radial_segments):
        lower = rings[r]
        upper = rings[r + 1]
        for t in range(theta_segments):
            a0 = lower[t]
            b0 = lower[(t + 1) % theta_segments]
            c0 = upper[(t + 1) % theta_segments]
            d0 = upper[t]
            faces.append((a0, b0, c0))
            faces.append((a0, c0, d0))

    return vertices, faces, normals, rings[-1]


def _rim_horizontal_normal(v: Vec3, params: ModuleParams) -> Vec3:
    geom = derived_geometry(params)
    axis_x = float(geom["axis_x_mm"])
    axis_y = float(geom["axis_y_mm"])
    nx = v[0] / (axis_x * axis_x)
    ny = v[1] / (axis_y * axis_y)
    h = _normalize((nx, ny, 0.0))
    if _length(h) > 1.0e-12:
        return h
    return _normalize((v[0], v[1], 0.0))


def build_module_mesh(
    params: ModuleParams,
    *,
    anchor_mm: Vec3 = (0.0, 0.0, 0.0),
    theta_segments: int = 64,
    radial_segments: int = 16,
    solid: bool = True,
) -> Mesh:
    """Build one transformed module mesh in millimeters.

    If solid=True, the result is watertight. If solid=False, only the open
    inner surface is returned for visual comparison.
    """
    inner_v, inner_f, normals, rim_loop = _inner_cap(
        params, theta_segments=theta_segments, radial_segments=radial_segments
    )

    if not solid:
        vertices = list(inner_v)
        faces = list(inner_f)
        if params.rim_lift_mm > 1.0e-9:
            lift_normal = _cut_plane_normal(params)
            lip_start = len(vertices)
            for idx in rim_loop:
                vertices.append(_add(inner_v[idx], _mul(lift_normal, params.rim_lift_mm)))
            n = len(rim_loop)
            for t in range(n):
                a = rim_loop[t]
                b = rim_loop[(t + 1) % n]
                al = lip_start + t
                bl = lip_start + ((t + 1) % n)
                faces.append((a, b, bl))
                faces.append((a, bl, al))
        return Mesh(_transform_vertices(vertices, anchor_mm, params), faces)

    wall = float(params.wall_thickness_mm)
    outer_v = [_add(v, _mul(n, wall)) for v, n in zip(inner_v, normals)]
    n_inner = len(inner_v)
    vertices = list(inner_v) + outer_v

    # Inner cap is the fluid-facing side of the shell, so reverse it. The outer
    # cap keeps ellipsoid-outward winding.
    faces: list[Face] = [(a, c, b) for a, b, c in inner_f]
    faces.extend((a + n_inner, b + n_inner, c + n_inner) for a, b, c in inner_f)

    if params.rim_lift_mm <= 1.0e-9:
        _stitch_two_loops(faces, rim_loop, [i + n_inner for i in rim_loop])
    else:
        n = len(rim_loop)
        inner_lip: list[int] = []
        outer_lip: list[int] = []
        lift_normal = _cut_plane_normal(params)

        for idx in rim_loop:
            inner_lip.append(len(vertices))
            vertices.append(_add(inner_v[idx], _mul(lift_normal, params.rim_lift_mm)))

        for idx in rim_loop:
            base = vertices[inner_lip[len(outer_lip)]]
            h = _rim_horizontal_normal(inner_v[idx], params)
            outer_lip.append(len(vertices))
            vertices.append(_add(base, _mul(h, wall)))

        # Inner raised lip, outer raised lip, and top rim band.
        for t in range(n):
            a = rim_loop[t]
            b = rim_loop[(t + 1) % n]
            ao = a + n_inner
            bo = b + n_inner
            al = inner_lip[t]
            bl = inner_lip[(t + 1) % n]
            aol = outer_lip[t]
            bol = outer_lip[(t + 1) % n]

            faces.append((a, bl, b))
            faces.append((a, al, bl))
            faces.append((ao, bo, bol))
            faces.append((ao, bol, aol))
            faces.append((al, aol, bol))
            faces.append((al, bol, bl))

    return Mesh(_transform_vertices(vertices, anchor_mm, params), faces)


def build_module_rim_points(
    params: ModuleParams,
    *,
    anchor_mm: Vec3 = (0.0, 0.0, 0.0),
    theta_segments: int = 64,
) -> list[Vec3]:
    """Return transformed samples of the ellipsoid/cut-plane intersection curve."""
    vertices, _, _, rim_loop = _inner_cap(
        params, theta_segments=theta_segments, radial_segments=3
    )
    transformed = _transform_vertices(vertices, anchor_mm, params)
    return [transformed[idx] for idx in rim_loop]


def _stitch_two_loops(faces: list[Face], inner_loop: list[int], outer_loop: list[int]) -> None:
    if len(inner_loop) != len(outer_loop):
        raise ValueError("loop sizes must match")
    n = len(inner_loop)
    for t in range(n):
        a = inner_loop[t]
        b = inner_loop[(t + 1) % n]
        ao = outer_loop[t]
        bo = outer_loop[(t + 1) % n]
        faces.append((a, b, bo))
        faces.append((a, bo, ao))


def _transform_vertices(vertices: list[Vec3], anchor_mm: Vec3, params: ModuleParams) -> list[Vec3]:
    ry = math.radians(params.ellipsoid_tilt_deg)
    rz = math.radians(params.rotation_z_deg)
    sy, cy = math.sin(ry), math.cos(ry)
    sz, cz = math.sin(rz), math.cos(rz)
    tx = anchor_mm[0] + params.tx_mm
    ty = anchor_mm[1] + params.ty_mm
    tz = anchor_mm[2] + params.tz_mm

    out: list[Vec3] = []
    for x, y, z in vertices:
        x, z = x * cy + z * sy, -x * sy + z * cy
        x, y = x * cz - y * sz, x * sz + y * cz
        out.append((x + tx, y + ty, z + tz))
    return out


def combine_meshes(meshes: list[Mesh]) -> Mesh:
    vertices: list[Vec3] = []
    faces: list[Face] = []
    for mesh in meshes:
        offset = len(vertices)
        vertices.extend(mesh.vertices)
        faces.extend((a + offset, b + offset, c + offset) for a, b, c in mesh.faces)
    return Mesh(vertices, faces)


def edge_incidence(mesh: Mesh) -> Counter[tuple[int, int]]:
    counts: Counter[tuple[int, int]] = Counter()
    for a, b, c in mesh.faces:
        for u, v in ((a, b), (b, c), (c, a)):
            if u < v:
                counts[(u, v)] += 1
            else:
                counts[(v, u)] += 1
    return counts


def is_watertight(mesh: Mesh) -> bool:
    counts = edge_incidence(mesh)
    return bool(counts) and all(v == 2 for v in counts.values())


def _face_normal(v0: Vec3, v1: Vec3, v2: Vec3) -> Vec3:
    return _normalize(_cross(_sub(v1, v0), _sub(v2, v0)))


def write_binary_stl(path: Path, mesh: Mesh, *, unit_scale: float = 0.001) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"LFTH ellipsoid-cut parametric module".ljust(80, b"\0")
    with path.open("wb") as f:
        f.write(header)
        f.write(struct.pack("<I", len(mesh.faces)))
        for a, b, c in mesh.faces:
            v0 = _mul(mesh.vertices[a], unit_scale)
            v1 = _mul(mesh.vertices[b], unit_scale)
            v2 = _mul(mesh.vertices[c], unit_scale)
            n = _face_normal(v0, v1, v2)
            f.write(struct.pack("<12fH", *(n + v0 + v1 + v2), 0))


def read_modules(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "modules" not in payload or not isinstance(payload["modules"], list):
        raise ValueError(f"{path} does not contain a modules list")
    return payload["modules"]


def build_modules_from_infos(
    modules_info: list[dict],
    *,
    indices: set[int] | None = None,
    params_by_index: dict[int, ModuleParams] | None = None,
    wall_thickness_mm: float = 500.0,
    rim_lift_mm: float = 0.0,
    ellipsoid_tilt_deg: float = 0.0,
    cut_drop_deg: float = 0.0,
    theta_segments: int = 64,
    radial_segments: int = 16,
) -> tuple[Mesh, Mesh, list[dict]]:
    solid_meshes: list[Mesh] = []
    viz_meshes: list[Mesh] = []
    meta_modules: list[dict] = []
    params_by_index = params_by_index or {}

    for module in modules_info:
        idx = int(module["index"])
        if indices is not None and idx not in indices:
            continue
        params = params_by_index.get(idx)
        if params is None:
            params = default_params_from_module(
                module,
                wall_thickness_mm=wall_thickness_mm,
                rim_lift_mm=rim_lift_mm,
            )
            params.ellipsoid_tilt_deg = float(ellipsoid_tilt_deg)
            params.cut_drop_deg = float(cut_drop_deg)
        anchor = tuple(float(v) for v in module["base_point_mm"])
        solid = build_module_mesh(
            params,
            anchor_mm=anchor,
            theta_segments=theta_segments,
            radial_segments=radial_segments,
            solid=True,
        )
        viz = build_module_mesh(
            params,
            anchor_mm=anchor,
            theta_segments=theta_segments,
            radial_segments=radial_segments,
            solid=False,
        )
        solid_meshes.append(solid)
        viz_meshes.append(viz)

        target_bbox = module["bbox_mm"]
        solid_bbox = _bbox(solid.vertices)
        viz_bbox = _bbox(viz.vertices)
        rim_points = build_module_rim_points(
            params,
            anchor_mm=anchor,
            theta_segments=theta_segments,
        )
        derived = derived_geometry(params)
        meta_modules.append(
            {
                "index": idx,
                "source_name": module.get("name"),
                "anchor_mm": list(anchor),
                "params": asdict(params),
                "derived_ellipsoid_axes_mm": _round_nested(
                    [
                        float(derived["axis_x_mm"]),
                        float(derived["axis_y_mm"]),
                        float(derived["axis_z_mm"]),
                    ]
                ),
                "vertical_tangent_point_mm": _round_nested(
                    derived["vertical_tangent_point_mm"]
                ),
                "world_tangent_point_mm": _round_nested(
                    derived["world_tangent_point_mm"]
                ),
                "cut_plane": {
                    "equation_local_before_tilt": "z = sx*x + offset",
                    "equation_after_tilt": "z = cut_world_z + drop_slope*(x - tangent_x)",
                    "local_cut_angle_deg": round(float(derived["local_cut_angle_deg"]), 6),
                    "slope_x": round(float(derived["cut_slope_x"]), 6),
                    "slope_y": 0.0,
                    "offset_z_mm": round(float(derived["cut_offset_z_mm"]), 6),
                    "drop_slope": round(float(derived["cut_drop_slope"]), 6),
                    "world_z_mm": round(float(derived["cut_world_z_mm"]), 6),
                    "normal": _round_nested(derived["cut_plane_normal"], 6),
                },
                "target_bbox_mm": _round_nested(target_bbox),
                "solid_bbox_mm": _round_nested(solid_bbox),
                "rim_curve_mm": _round_nested(rim_points),
                "rim_bbox_mm": _round_nested(_bbox(rim_points)),
                "solid_bbox_error_mm": _round_nested(
                    [
                        [solid_bbox[0][i] - target_bbox[0][i] for i in range(3)],
                        [solid_bbox[1][i] - target_bbox[1][i] for i in range(3)],
                    ]
                ),
                "solid_extent_error_mm": _round_nested(
                    [
                        _bbox_extents(solid_bbox)[i] - _bbox_extents(target_bbox)[i]
                        for i in range(3)
                    ]
                ),
                "viz_bbox_mm": _round_nested(viz_bbox),
                "solid_vertices": len(solid.vertices),
                "solid_triangles": len(solid.faces),
                "viz_vertices": len(viz.vertices),
                "viz_triangles": len(viz.faces),
                "watertight": is_watertight(solid),
            }
        )

    return combine_meshes(solid_meshes), combine_meshes(viz_meshes), meta_modules


def _parse_indices(raw: str | None) -> set[int] | None:
    if raw is None or raw.strip() == "":
        return None
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


def _push_to_rhino(solid_path: Path, viz_path: Path) -> list[str]:
    messages: list[str] = []
    try:
        sys.path.insert(0, str(REPO_ROOT / "env_fx3d" / "scripts"))
        from rhino_mcp import push_stl_to_rhino_layer

        push_stl_to_rhino_layer(
            solid_path,
            "parametric::ellipsoid_solid",
            (170, 110, 50),
            obj_name="ellipsoid_parametric_solid",
        )
        push_stl_to_rhino_layer(
            viz_path,
            "parametric::ellipsoid_viz",
            (60, 130, 220),
            obj_name="ellipsoid_parametric_viz",
        )
        messages.append("pushed solid/viz STL to Rhino")
    except Exception as exc:
        messages.append(f"Rhino push skipped: {exc}")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules-json", default=str(DEFAULT_MODULES_JSON))
    parser.add_argument("--out", default=str(DEFAULT_SOLID_STL), help="binary closed solid STL")
    parser.add_argument("--viz-out", default=str(DEFAULT_VIZ_STL), help="open inner-surface viz STL")
    parser.add_argument("--meta-out", default=str(DEFAULT_META_JSON))
    parser.add_argument("--indices", default=None, help="comma-separated module indices; default=all")
    parser.add_argument("--theta-segments", type=int, default=64)
    parser.add_argument("--radial-segments", type=int, default=16)
    parser.add_argument("--wall-thickness-mm", type=float, default=500.0)
    parser.add_argument("--rim-lift-mm", type=float, default=0.0)
    parser.add_argument(
        "--ellipsoid-tilt-deg",
        "--cut-tilt-deg",
        dest="ellipsoid_tilt_deg",
        type=float,
        default=0.0,
        help="tilt the ellipsoid before applying the hinged cut plane",
    )
    parser.add_argument(
        "--cut-drop-deg",
        type=float,
        default=0.0,
        help="rotate the cut plane downward around the vertical-tangent point",
    )
    parser.add_argument("--push-rhino", action="store_true")
    args = parser.parse_args(argv)

    modules_path = Path(args.modules_json).resolve()
    solid_path = Path(args.out).resolve()
    viz_path = Path(args.viz_out).resolve()
    meta_path = Path(args.meta_out).resolve()
    indices = _parse_indices(args.indices)

    modules = read_modules(modules_path)
    solid, viz, meta_modules = build_modules_from_infos(
        modules,
        indices=indices,
        wall_thickness_mm=args.wall_thickness_mm,
        rim_lift_mm=args.rim_lift_mm,
        ellipsoid_tilt_deg=args.ellipsoid_tilt_deg,
        cut_drop_deg=args.cut_drop_deg,
        theta_segments=args.theta_segments,
        radial_segments=args.radial_segments,
    )

    write_binary_stl(solid_path, solid)
    write_binary_stl(viz_path, viz)

    edge_counts = edge_incidence(solid)
    bad_edges = sum(1 for count in edge_counts.values() if count != 2)
    meta = {
        "generator": "ellipsoid-cut parametric module",
        "source_modules_json": str(modules_path).replace("\\", "/"),
        "solid_stl": str(solid_path).replace("\\", "/"),
        "viz_stl": str(viz_path).replace("\\", "/"),
        "units_internal": "mm",
        "stl_units": "m",
        "binary_stl": True,
        "theta_segments": args.theta_segments,
        "radial_segments": args.radial_segments,
        "module_count": len(meta_modules),
        "solid_vertices": len(solid.vertices),
        "solid_triangles": len(solid.faces),
        "viz_vertices": len(viz.vertices),
        "viz_triangles": len(viz.faces),
        "solid_bbox_mm": _round_nested(_bbox(solid.vertices)),
        "viz_bbox_mm": _round_nested(_bbox(viz.vertices)),
        "watertight": is_watertight(solid),
        "nonmanifold_or_boundary_edges": bad_edges,
        "modules": meta_modules,
    }

    if args.push_rhino:
        meta["rhino"] = _push_to_rhino(solid_path, viz_path)

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"solid: {solid_path}")
    print(f"viz:   {viz_path}")
    print(f"meta:  {meta_path}")
    print(
        f"modules={len(meta_modules)} solid_tris={len(solid.faces)} "
        f"watertight={meta['watertight']} bad_edges={bad_edges}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
