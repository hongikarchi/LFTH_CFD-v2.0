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
PARAMS_PATH = RUNS / "_ui_ellipsoid_params.json"
SOLID_STL = RUNS / "_ui_ellipsoid_parametric.stl"
VIZ_STL = RUNS / "_ui_ellipsoid_parametric_viz.stl"
META_PATH = RUNS / "_ui_ellipsoid_parametric_meta.json"
PORT = 8081

PARAM_KEYS = [
    "b_mm",
    "h_mm",
    "ellipsoid_tilt_deg",
    "wall_thickness_mm",
    "rim_lift_mm",
    "rotation_z_deg",
    "tx_mm",
    "ty_mm",
    "tz_mm",
]

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
    """Best-effort migration from earlier param sets to b/h tangent-cut params."""
    migrated = dict(default_params)
    migrated.update({k: raw_params[k] for k in PARAM_KEYS if k in raw_params})
    if "b_mm" in raw_params and "h_mm" in raw_params and "ellipsoid_tilt_deg" in raw_params:
        return migrated

    try:
        if "opening_x_mm" in raw_params and "opening_y_mm" in raw_params:
            opening_x = float(raw_params["opening_x_mm"])
            opening_y = float(raw_params["opening_y_mm"])
            migrated["b_mm"] = max(1.0, (opening_x + opening_y) * 0.25)
            migrated["h_mm"] = max(1.0, float(raw_params.get("bowl_depth_mm", default_params["h_mm"])))
        elif {"axis_x_mm", "axis_y_mm", "axis_z_mm", "cap_depth_mm"} <= set(raw_params):
            axis_x = float(raw_params["axis_x_mm"])
            axis_y = float(raw_params["axis_y_mm"])
            axis_z = float(raw_params["axis_z_mm"])
            cap_depth = float(raw_params["cap_depth_mm"])
            center_z = axis_z - cap_depth
            rim_p = (0.0 - center_z) / max(axis_z, 1.0)
            factor = math.sqrt(max(0.0, 1.0 - rim_p * rim_p)) or 1.0
            opening_x = 2.0 * axis_x * factor
            opening_y = 2.0 * axis_y * factor
            migrated["b_mm"] = max(1.0, (opening_x + opening_y) * 0.25)
            migrated["h_mm"] = max(1.0, cap_depth)

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
            scale = max(float(migrated.get("b_mm", default_params["b_mm"])), 1.0)
            slope = math.hypot(sx / scale, sy / scale)
            if slope > 1.0e-12:
                migrated["ellipsoid_tilt_deg"] = min(35.0, math.degrees(math.atan(slope)))

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
        "solid_bbox_mm": pm._round_nested(pm._bbox(solid.vertices)),
        "viz_bbox_mm": pm._round_nested(pm._bbox(viz.vertices)),
        "watertight": pm.is_watertight(solid),
        "nonmanifold_or_boundary_edges": bad_edges,
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
        "outputs": {
            "solid_stl": str(SOLID_STL).replace("\\", "/"),
            "viz_stl": str(VIZ_STL).replace("\\", "/"),
            "meta": str(META_PATH).replace("\\", "/"),
            "params": str(PARAMS_PATH).replace("\\", "/"),
        },
    }


def _params_payload() -> dict:
    modules = _load_modules()
    state = _read_saved_state() or _default_state()
    return {"ok": True, "params": state, "modules": _module_summary(modules)}


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
