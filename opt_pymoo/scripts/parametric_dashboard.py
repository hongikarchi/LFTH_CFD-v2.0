"""Parametric module dashboard server.

Run from the repo root:
    python opt_pymoo/scripts/parametric_dashboard.py
    -> http://localhost:8081
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from flask import Flask, Response, abort, jsonify, request
    HAS_FLASK = True
except ModuleNotFoundError:
    HAS_FLASK = False
    Flask = Response = abort = jsonify = request = None

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent
sys.path.insert(0, str(SCRIPT_DIR))

import parametric_module as pm

HTML_PATH = MODULE_ROOT / "parametric_dashboard.html"
RUNS = MODULE_ROOT / "runs"
MODULES_JSON = REPO_ROOT / "env_fx3d" / "runs" / "_collider_modules.json"
BOUNDARY_JSON = REPO_ROOT / "env_fx3d" / "runs" / "_boundary.json"
PARAMS_PATH = RUNS / "_ui_ellipsoid_params.json"
SOLID_STL = RUNS / "_ui_ellipsoid_parametric.stl"
VIZ_STL = RUNS / "_ui_ellipsoid_parametric_viz.stl"
META_PATH = RUNS / "_ui_ellipsoid_parametric_meta.json"
PORT = 8081

PARAM_KEYS = [
    "axis_x_mm",
    "axis_y_mm",
    "axis_z_mm",
    "ellipsoid_tilt_deg",
    "cut_drop_deg",
    "wall_thickness_mm",
    "rim_lift_mm",
    "rotation_z_deg",
    "tx_mm",
    "ty_mm",
    "tz_mm",
]
BOUNDARY_LAYER = "env::boundary"

if HAS_FLASK:
    app = Flask(__name__, static_folder=None)
else:
    class _NoFlaskApp:
        def route(self, *args, **kwargs):
            def _decorator(fn):
                return fn
            return _decorator

    app = _NoFlaskApp()


def _log(message: str) -> None:
    try:
        if sys.stdout is not None:
            print(message)
    except Exception:
        pass


def _load_modules() -> list[dict]:
    return pm.read_modules(MODULES_JSON)


def _default_state() -> dict:
    modules = _load_modules()
    params_by_index = {
        str(int(m["index"])): asdict(pm.default_params_from_module(m))
        for m in modules
    }
    return {
        "selected_index": "all",
        "theta_segments": 64,
        "radial_segments": 16,
        "modules": params_by_index,
    }


def _module_summary(modules: list[dict]) -> list[dict]:
    out = []
    for module in modules:
        out.append(
            {
                "index": int(module["index"]),
                "name": module.get("name") or f"module {module['index']}",
                "anchor_mm": module.get("base_point_mm"),
                "target_bbox_mm": module.get("bbox_mm"),
            }
        )
    return out


def _read_saved_state() -> dict | None:
    if not PARAMS_PATH.exists():
        return None
    try:
        data = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
        return _normalize_state(data)
    except Exception:
        return None


def _point_in_polygon_xy(x: float, y: float, polygon: list[list[float]]) -> bool:
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / max(yj - yi, 1.0e-30) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_segment_distance_xy(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    denom = vx * vx + vy * vy
    if denom <= 1.0e-30:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    qx, qy = ax + t * vx, ay + t * vy
    return math.hypot(px - qx, py - qy)


def _polygon_distance_xy(x: float, y: float, polygon: list[list[float]]) -> float:
    if len(polygon) < 2:
        return 0.0
    best = float("inf")
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        best = min(best, _point_segment_distance_xy(x, y, a[0], a[1], b[0], b[1]))
    return 0.0 if best == float("inf") else best


def _points_boundary_check(points: list[list[float]], boundary: dict) -> dict:
    polygon = boundary.get("xy_curve_mm") or []
    z_min = float(boundary.get("z_min_mm", -float("inf")))
    z_max = float(boundary.get("z_max_mm", float("inf")))
    outside_xy = 0
    outside_z = 0
    max_xy_violation = 0.0
    max_z_violation = 0.0
    for point in points:
        x, y, z = float(point[0]), float(point[1]), float(point[2])
        if not _point_in_polygon_xy(x, y, polygon):
            outside_xy += 1
            max_xy_violation = max(max_xy_violation, _polygon_distance_xy(x, y, polygon))
        z_violation = max(z_min - z, z - z_max, 0.0)
        if z_violation > 0.0:
            outside_z += 1
            max_z_violation = max(max_z_violation, z_violation)
    max_violation = max(max_xy_violation, max_z_violation)
    return {
        "mode": "rim_curve_xy_extrusion",
        "inside": max_violation <= 1.0e-6,
        "sample_count": len(points),
        "outside_xy_count": outside_xy,
        "outside_z_count": outside_z,
        "max_xy_violation_mm": round(max_xy_violation, 3),
        "max_z_violation_mm": round(max_z_violation, 3),
        "max_violation_mm": round(max_violation, 3),
    }


def _read_boundary() -> dict | None:
    if not BOUNDARY_JSON.exists():
        return None
    try:
        data = json.loads(BOUNDARY_JSON.read_text(encoding="utf-8"))
        polygon = data.get("xy_curve_mm")
        if (
            data.get("mode") == "xy_curve_extrusion"
            and isinstance(polygon, list)
            and len(polygon) >= 3
        ):
            data["xy_curve_mm"] = [[float(v[0]), float(v[1])] for v in polygon]
            data["z_min_mm"] = float(data["z_min_mm"])
            data["z_max_mm"] = float(data["z_max_mm"])
            if "bbox_mm" in data:
                data["bbox_mm"] = [[float(v) for v in row] for row in data["bbox_mm"]]
            return data
    except Exception:
        return None
    return None


def _fetch_boundary_from_rhino() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "env_fx3d" / "scripts"))
    from rhino_mcp import mcp_call

    code = f'''
import scriptcontext as sc
import Rhino, System, json, math
doc = sc.doc
target = "{BOUNDARY_LAYER}"

def layer_full_path(layer_index):
    layer = doc.Layers[layer_index]
    names = [layer.Name]
    parent_id = layer.ParentLayerId
    while parent_id != System.Guid.Empty:
        parent = doc.Layers.FindId(parent_id)
        if parent is None:
            break
        names.append(parent.Name)
        parent_id = parent.ParentLayerId
    return "::".join(reversed(names))
bb_min = [1e30, 1e30, 1e30]
bb_max = [-1e30, -1e30, -1e30]
cnt = 0
curves = []
flat_curve_groups = {{}}

def flat_group_key(c):
    cb = c.GetBoundingBox(True)
    if not cb.IsValid or abs(cb.Max.Z - cb.Min.Z) >= 5.0:
        return None
    return int(round(((cb.Max.Z + cb.Min.Z) * 0.5) / 10.0))

def collect_flat_curve(c):
    key = flat_group_key(c)
    if key is None:
        return
    if key not in flat_curve_groups:
        flat_curve_groups[key] = []
    flat_curve_groups[key].append(c)

def add_curve(c, source):
    if c is None:
        return
    try:
        length = c.GetLength()
    except Exception:
        length = 0.0
    if length <= 1.0:
        return
    n = max(48, min(512, int(length / 150.0)))
    params = c.DivideByCount(n, True)
    if not params:
        dom = c.Domain
        params = [dom.T0 + (dom.T1 - dom.T0) * i / n for i in range(n)]
    pts = []
    zmin = 1e30
    zmax = -1e30
    for t in params:
        p = c.PointAt(t)
        pts.append([float(p.X), float(p.Y), float(p.Z)])
        zmin = min(zmin, float(p.Z))
        zmax = max(zmax, float(p.Z))
    endpoint_gap = 1e30
    if len(pts) >= 2:
        dx = pts[0][0] - pts[-1][0]
        dy = pts[0][1] - pts[-1][1]
        dz = pts[0][2] - pts[-1][2]
        endpoint_gap = math.sqrt(dx*dx + dy*dy + dz*dz)
        if endpoint_gap < 1.0:
            pts = pts[:-1]
    if (not c.IsClosed) and endpoint_gap > 5.0:
        return
    if len(pts) < 3:
        return
    area = 0.0
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        area += p[0] * q[1] - q[0] * p[1]
    curves.append({{
        "points": pts,
        "xy_area": area * 0.5,
        "length": length,
        "z_min": zmin,
        "z_max": zmax,
        "closed": bool(c.IsClosed),
        "source": source
    }})

settings = Rhino.DocObjects.ObjectEnumeratorSettings()
settings.NormalObjects = True
settings.LockedObjects = True
settings.HiddenObjects = True
settings.ReferenceObjects = True
settings.IncludeLights = False

for o in doc.Objects.GetObjectList(settings):
    fp = layer_full_path(o.Attributes.LayerIndex)
    if fp != target and not fp.startswith(target + "::"):
        continue
    cnt += 1
    b = o.Geometry.GetBoundingBox(True)
    if b.IsValid:
        for k, v in enumerate([b.Min.X, b.Min.Y, b.Min.Z]):
            bb_min[k] = min(bb_min[k], v)
        for k, v in enumerate([b.Max.X, b.Max.Y, b.Max.Z]):
            bb_max[k] = max(bb_max[k], v)
    g = o.Geometry
    if isinstance(g, Rhino.Geometry.Curve):
        c = g.DuplicateCurve()
        if c.IsClosed:
            add_curve(c, "curve")
        else:
            collect_flat_curve(c)
    elif isinstance(g, Rhino.Geometry.Extrusion):
        g = g.ToBrep()
        for edge in g.Edges:
            c = edge.DuplicateCurve()
            if c:
                collect_flat_curve(c)
    elif isinstance(g, Rhino.Geometry.Brep):
        for edge in g.Edges:
            c = edge.DuplicateCurve()
            if c:
                collect_flat_curve(c)

for key, group in flat_curve_groups.items():
    try:
        joined = Rhino.Geometry.Curve.JoinCurves(group, 20.0)
    except Exception:
        joined = []
    for c in joined:
        if c and c.IsClosed:
            add_curve(c, "joined_edges")

payload = {{
    "object_count": cnt,
    "bbox_mm": [bb_min, bb_max],
    "curves": curves
}}
print("BOUNDARY_CURVE_JSON " + json.dumps(payload))
'''
    result = mcp_call(code, timeout=60)
    if result.get("status") != "success":
        raise RuntimeError(f"Rhino MCP boundary fetch failed: {result}")
    out = result.get("result", {}).get("output", "")
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("BOUNDARY_CURVE_JSON "):
            continue
        payload = json.loads(line[len("BOUNDARY_CURVE_JSON "):])
        count = int(payload.get("object_count", 0))
        if count <= 0:
            raise RuntimeError(f"{BOUNDARY_LAYER} has no objects")
        curves = payload.get("curves") or []
        if not curves:
            raise RuntimeError(f"{BOUNDARY_LAYER} has no curve geometry")
        curve = max(curves, key=lambda c: abs(float(c.get("xy_area", 0.0))))
        points3 = curve.get("points") or []
        polygon = [[float(p[0]), float(p[1])] for p in points3]
        if len(polygon) < 3:
            raise RuntimeError(f"{BOUNDARY_LAYER} curve has too few samples")
        bbox = payload.get("bbox_mm")
        z_min = float(bbox[0][2])
        z_max = float(bbox[1][2])
        boundary = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "layer": BOUNDARY_LAYER,
            "source": "rhino_mcp",
            "mode": "xy_curve_extrusion",
            "object_count": count,
            "curve_count": len(curves),
            "sample_count": len(polygon),
            "xy_curve_mm": polygon,
            "xy_area_mm2": round(float(curve.get("xy_area", 0.0)), 3),
            "selected_curve_source": curve.get("source", ""),
            "selected_curve_closed": bool(curve.get("closed", False)),
            "selected_curve_length_mm": round(float(curve.get("length", 0.0)), 3),
            "z_min_mm": z_min,
            "z_max_mm": z_max,
            "bbox_mm": bbox,
            "path": str(BOUNDARY_JSON).replace("\\", "/"),
        }
        BOUNDARY_JSON.parent.mkdir(parents=True, exist_ok=True)
        BOUNDARY_JSON.write_text(json.dumps(boundary, indent=2), encoding="utf-8")
        return boundary
    raise RuntimeError(f"no BOUNDARY_CURVE_JSON line in Rhino output: {out[-500:]}")


def _boundary_payload(refresh: bool = False) -> dict:
    if refresh:
        boundary = _fetch_boundary_from_rhino()
    else:
        boundary = _read_boundary()
    if boundary is None:
        return {
            "ok": False,
            "boundary": None,
            "path": str(BOUNDARY_JSON).replace("\\", "/"),
            "layer": BOUNDARY_LAYER,
            "error": "boundary cache missing; refresh from Rhino env::boundary",
        }
    return {"ok": True, "boundary": boundary}


def _coerce_float(payload: dict, key: str, default: float) -> float:
    value = payload.get(key, default)
    if value is None or value == "":
        value = default
    return float(value)


def _coerce_int(payload: dict, key: str, default: int) -> int:
    value = payload.get(key, default)
    if value is None or value == "":
        value = default
    return int(float(value))


def _migrate_old_module_params(raw_params: dict, default_params: dict) -> dict:
    """Best-effort migration from earlier param sets to xyz-axis tangent-cut params."""
    migrated = dict(default_params)
    migrated.update({k: raw_params[k] for k in PARAM_KEYS if k in raw_params})
    if (
        "axis_x_mm" in raw_params
        and "axis_y_mm" in raw_params
        and "axis_z_mm" in raw_params
        and "ellipsoid_tilt_deg" in raw_params
        and "cut_drop_deg" in raw_params
    ):
        return migrated

    try:
        if "b_mm" in raw_params and "h_mm" in raw_params:
            b = float(raw_params["b_mm"])
            h = float(raw_params["h_mm"])
            migrated["axis_x_mm"] = max(1.0, b)
            migrated["axis_y_mm"] = max(1.0, h)
            migrated["axis_z_mm"] = max(1.0, h)
        elif "opening_x_mm" in raw_params and "opening_y_mm" in raw_params:
            opening_x = float(raw_params["opening_x_mm"])
            opening_y = float(raw_params["opening_y_mm"])
            migrated["axis_x_mm"] = max(1.0, opening_x * 0.5)
            migrated["axis_y_mm"] = max(1.0, opening_y * 0.5)
            migrated["axis_z_mm"] = max(
                1.0, float(raw_params.get("bowl_depth_mm", default_params["axis_z_mm"]))
            )
        elif {"axis_x_mm", "axis_y_mm", "axis_z_mm"} <= set(raw_params):
            migrated["axis_x_mm"] = max(1.0, float(raw_params["axis_x_mm"]))
            migrated["axis_y_mm"] = max(1.0, float(raw_params["axis_y_mm"]))
            migrated["axis_z_mm"] = max(1.0, float(raw_params["axis_z_mm"]))

        if "ellipsoid_tilt_deg" in raw_params:
            migrated["ellipsoid_tilt_deg"] = min(
                35.0, max(0.0, float(raw_params["ellipsoid_tilt_deg"]))
            )
        elif "cut_tilt_deg" in raw_params:
            migrated["ellipsoid_tilt_deg"] = min(35.0, max(0.0, float(raw_params["cut_tilt_deg"])))
        elif "tilt_y_deg" in raw_params or "tilt_x_deg" in raw_params:
            tx = float(raw_params.get("tilt_x_deg", 0.0))
            ty = float(raw_params.get("tilt_y_deg", 0.0))
            migrated["ellipsoid_tilt_deg"] = min(35.0, math.hypot(tx, ty))
            if math.hypot(tx, ty) > 1.0e-9:
                direction = (math.degrees(math.atan2(-tx, ty)) + 360.0) % 360.0
                migrated["rotation_z_deg"] = (
                    float(migrated.get("rotation_z_deg", 0.0)) + direction
                ) % 360.0
        else:
            sx = float(raw_params.get("cut_slope_x_mm", 0.0))
            sy = float(raw_params.get("cut_slope_y_mm", 0.0))
            scale = max(float(migrated.get("axis_x_mm", default_params["axis_x_mm"])), 1.0)
            slope = math.hypot(sx / scale, sy / scale)
            if slope > 1.0e-12:
                migrated["ellipsoid_tilt_deg"] = min(35.0, math.degrees(math.atan(slope)))

        if "cut_drop_deg" in raw_params:
            migrated["cut_drop_deg"] = min(35.0, max(0.0, float(raw_params["cut_drop_deg"])))
        else:
            migrated["cut_drop_deg"] = 0.0

        old_azimuth = float(raw_params.get("cut_azimuth_deg", 0.0))
        if abs(old_azimuth) > 1.0e-9:
            migrated["rotation_z_deg"] = (
                float(migrated.get("rotation_z_deg", 0.0)) + old_azimuth
            ) % 360.0
    except Exception:
        pass
    return migrated


def _normalize_state(payload: dict | None) -> dict:
    defaults = _default_state()
    if not isinstance(payload, dict):
        return defaults

    state = {
        "selected_index": str(payload.get("selected_index", defaults["selected_index"])),
        "theta_segments": max(8, _coerce_int(payload, "theta_segments", defaults["theta_segments"])),
        "radial_segments": max(3, _coerce_int(payload, "radial_segments", defaults["radial_segments"])),
        "modules": {},
    }

    incoming_modules = payload.get("modules") if isinstance(payload.get("modules"), dict) else {}
    for idx, default_params in defaults["modules"].items():
        raw_params = incoming_modules.get(idx, default_params)
        if not isinstance(raw_params, dict):
            raw_params = default_params
        raw_params = _migrate_old_module_params(raw_params, default_params)
        state["modules"][idx] = {
            key: _coerce_float(raw_params, key, float(default_params[key]))
            for key in PARAM_KEYS
        }

    if state["selected_index"] != "all" and state["selected_index"] not in state["modules"]:
        state["selected_index"] = defaults["selected_index"]
    return state


def _state_to_module_params(state: dict) -> dict[int, pm.ModuleParams]:
    params_by_index = {}
    for idx, params in state["modules"].items():
        params_by_index[int(idx)] = pm.ModuleParams(**{key: float(params[key]) for key in PARAM_KEYS})
    return params_by_index


def _selected_indices(state: dict) -> set[int] | None:
    selected = str(state.get("selected_index", "all"))
    if selected == "all":
        return None
    return {int(selected)}


def _write_meta(meta: dict) -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _build_preview(state: dict) -> dict:
    modules = _load_modules()
    boundary = _read_boundary()
    params_by_index = _state_to_module_params(state)
    solid, viz, meta_modules = pm.build_modules_from_infos(
        modules,
        indices=_selected_indices(state),
        params_by_index=params_by_index,
        theta_segments=int(state["theta_segments"]),
        radial_segments=int(state["radial_segments"]),
    )
    pm.write_binary_stl(SOLID_STL, solid)
    pm.write_binary_stl(VIZ_STL, viz)

    edge_counts = pm.edge_incidence(solid)
    bad_edges = sum(1 for count in edge_counts.values() if count != 2)
    solid_bbox = pm._bbox(solid.vertices)
    viz_bbox = pm._bbox(viz.vertices)
    boundary_check = None
    if boundary:
        all_rim_points: list[list[float]] = []
        for module_meta in meta_modules:
            rim_points = module_meta.get("rim_curve_mm") or []
            all_rim_points.extend(rim_points)
            module_meta["boundary"] = _points_boundary_check(rim_points, boundary)
        boundary_check = _points_boundary_check(all_rim_points, boundary)
    meta = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generator": "ui ellipsoid-cut parametric module",
        "selected_index": state["selected_index"],
        "solid_stl": str(SOLID_STL).replace("\\", "/"),
        "viz_stl": str(VIZ_STL).replace("\\", "/"),
        "meta_path": str(META_PATH).replace("\\", "/"),
        "units_internal": "mm",
        "stl_units": "m",
        "binary_stl": True,
        "theta_segments": int(state["theta_segments"]),
        "radial_segments": int(state["radial_segments"]),
        "module_count": len(meta_modules),
        "solid_vertices": len(solid.vertices),
        "solid_triangles": len(solid.faces),
        "viz_vertices": len(viz.vertices),
        "viz_triangles": len(viz.faces),
        "solid_bbox_mm": pm._round_nested(solid_bbox),
        "viz_bbox_mm": pm._round_nested(viz_bbox),
        "watertight": pm.is_watertight(solid),
        "nonmanifold_or_boundary_edges": bad_edges,
        "boundary": boundary,
        "boundary_check": boundary_check,
        "boundary_ok": None if boundary_check is None else boundary_check["inside"],
        "modules": meta_modules,
    }
    _write_meta(meta)
    return meta


def _defaults_payload() -> dict:
    modules = _load_modules()
    return {
        "ok": True,
        "params": _default_state(),
        "modules": _module_summary(modules),
        "boundary": _read_boundary(),
        "outputs": {
            "solid_stl": str(SOLID_STL).replace("\\", "/"),
            "viz_stl": str(VIZ_STL).replace("\\", "/"),
            "meta": str(META_PATH).replace("\\", "/"),
            "params": str(PARAMS_PATH).replace("\\", "/"),
            "boundary": str(BOUNDARY_JSON).replace("\\", "/"),
        },
    }


def _params_payload() -> dict:
    modules = _load_modules()
    state = _read_saved_state() or _default_state()
    return {
        "ok": True,
        "params": state,
        "modules": _module_summary(modules),
        "boundary": _read_boundary(),
    }


def _save_params_payload(payload: dict | None) -> dict:
    state = _normalize_state(payload or {})
    PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PARAMS_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"ok": True, "params": state, "saved": str(PARAMS_PATH).replace("\\", "/")}


def _preview_payload(payload: dict | None) -> dict:
    state = _normalize_state(payload or {})
    return {"ok": True, "meta": _build_preview(state)}


def _bake_payload(payload: dict | None) -> dict:
    state = _normalize_state(payload or {})
    meta = _build_preview(state)
    meta["rhino"] = pm._push_to_rhino(SOLID_STL, VIZ_STL)
    _write_meta(meta)
    return {"ok": True, "meta": meta}


def _meta_payload() -> dict:
    if not META_PATH.exists():
        raise FileNotFoundError(str(META_PATH))
    return {"ok": True, "meta": json.loads(META_PATH.read_text(encoding="utf-8"))}


@app.route("/")
def index():
    if not HTML_PATH.exists():
        return Response(f"missing {HTML_PATH}", status=500, mimetype="text/plain")
    return Response(HTML_PATH.read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/api/defaults")
def api_defaults():
    return jsonify(_defaults_payload())


@app.route("/api/params", methods=["GET"])
def api_params_get():
    return jsonify(_params_payload())


@app.route("/api/params", methods=["POST"])
def api_params_post():
    return jsonify(_save_params_payload(request.get_json(force=True) or {}))


@app.route("/api/preview", methods=["POST"])
def api_preview():
    try:
        out = _preview_payload(request.get_json(force=True) or {})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(out)


@app.route("/api/bake", methods=["POST"])
def api_bake():
    try:
        out = _bake_payload(request.get_json(force=True) or {})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(out)


@app.route("/api/meta")
def api_meta():
    try:
        return jsonify(_meta_payload())
    except FileNotFoundError:
        abort(404)


@app.route("/api/boundary", methods=["GET", "POST"])
def api_boundary():
    try:
        return jsonify(_boundary_payload(refresh=request.method == "POST"))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


class _StdlibHandler(BaseHTTPRequestHandler):
    server_version = "ParametricDashboard/1.0"

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:
        _log("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_bytes(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/defaults":
                self._send_json(200, _defaults_payload())
            elif path == "/api/params":
                self._send_json(200, _params_payload())
            elif path == "/api/meta":
                self._send_json(200, _meta_payload())
            elif path == "/api/boundary":
                self._send_json(200, _boundary_payload(refresh=False))
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except FileNotFoundError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/params":
                self._send_json(200, _save_params_payload(payload))
            elif path == "/api/preview":
                self._send_json(200, _preview_payload(payload))
            elif path == "/api/bake":
                self._send_json(200, _bake_payload(payload))
            elif path == "/api/boundary":
                self._send_json(200, _boundary_payload(refresh=True))
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})


def open_browser_later(port: int) -> None:
    time.sleep(1.0)
    try:
        webbrowser.open(f"http://localhost:{port}/")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    if not MODULES_JSON.exists():
        _log(f"ERROR: missing {MODULES_JSON}")
        return 1
    if not HTML_PATH.exists():
        _log(f"ERROR: missing {HTML_PATH}")
        return 1
    RUNS.mkdir(parents=True, exist_ok=True)

    engine = "Flask" if HAS_FLASK else "stdlib http.server"
    _log(f"Parametric module dashboard at http://localhost:{args.port}/ ({engine})")
    _log(f"  params: {PARAMS_PATH}")
    _log(f"  output: {SOLID_STL}")
    if not args.no_open:
        import threading

        threading.Thread(target=open_browser_later, args=(args.port,), daemon=True).start()
    if HAS_FLASK:
        app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True, use_reloader=False)
    else:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), _StdlibHandler)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
