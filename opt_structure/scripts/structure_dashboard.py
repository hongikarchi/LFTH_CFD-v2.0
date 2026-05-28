"""Structure optimization dashboard server.

Run from the repo root:
    python opt_structure/scripts/structure_dashboard.py
    -> http://localhost:8082
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import webbrowser
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
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

HTML_PATH = MODULE_ROOT / "structure_dashboard.html"
RUNS = MODULE_ROOT / "runs"
CASE_PATH = MODULE_ROOT / "config" / "structure_case.json"
CONTEXT_PATH = RUNS / "_structure_context.json"
GRAPH_PATH = RUNS / "ground_structure_graph.json"
SOLUTION_PATH = RUNS / "structure_solution.json"
LOG_PATH = RUNS / "_structure_dashboard.log"
PORT = 8082

RUN_PRESET = {
    "engine": "pynite",
    "pop": 8,
    "n_gen": 2,
    "seed": 42,
}

FIELD_GROUPS = [
    {
        "key": "loads",
        "title": "Loads",
        "fields": [
            {
                "key": "loads.module_dead_kg",
                "label": "module dead",
                "unit": "kg",
                "type": "number",
                "min": 0,
                "max": 5000,
                "step": 10,
                "desc": "Dead load assigned to each CFD module until measured mass is available.",
            },
            {
                "key": "loads.module_water_kg",
                "label": "water mass",
                "unit": "kg",
                "type": "number",
                "min": 0,
                "max": 5000,
                "step": 10,
                "desc": "Quasi-static water mass per module before the dynamic allowance.",
            },
            {
                "key": "loads.water_dynamic_factor",
                "label": "dynamic x",
                "unit": "",
                "type": "number",
                "min": 1,
                "max": 5,
                "step": 0.1,
                "desc": "Multiplier on estimated water mass. The initial conservative value is 2.0.",
            },
        ],
    },
    {
        "key": "constraints",
        "title": "Constraints",
        "fields": [
            {
                "key": "constraints.max_deflection_mm",
                "label": "max deflect",
                "unit": "mm",
                "type": "number",
                "min": 1,
                "max": 100,
                "step": 1,
                "desc": "Absolute serviceability limit for module support target displacement.",
            },
            {
                "key": "constraints.deflection_span_ratio",
                "label": "span ratio",
                "unit": "L/n",
                "type": "number",
                "min": 100,
                "max": 1000,
                "step": 10,
                "desc": "Span-based limit. The active limit is min(max deflect, longest member / ratio).",
            },
            {
                "key": "constraints.max_slenderness",
                "label": "slenderness",
                "unit": "L/r",
                "type": "number",
                "min": 50,
                "max": 500,
                "step": 5,
                "desc": "Compression member slenderness limit used as the main early bracing check.",
            },
        ],
    },
    {
        "key": "ground",
        "title": "Ground Structure",
        "fields": [
            {
                "key": "ground_structure.support_sample_step_mm",
                "label": "support step",
                "unit": "mm",
                "type": "number",
                "min": 250,
                "max": 5000,
                "step": 50,
                "desc": "Sampling interval along env::structure_start during Rhino extraction.",
            },
            {
                "key": "ground_structure.intermediate_levels",
                "label": "brace levels",
                "unit": "",
                "type": "text",
                "desc": "Comma-separated levels between support centroid and module targets, e.g. 0.33,0.66.",
            },
            {
                "key": "ground_structure.nearest_supports_per_target",
                "label": "near supports",
                "unit": "count",
                "type": "number",
                "min": 1,
                "max": 12,
                "step": 1,
                "desc": "Number of closest start/support nodes connected to each module target.",
            },
            {
                "key": "ground_structure.target_to_target_max_mm",
                "label": "target tie max",
                "unit": "mm",
                "type": "number",
                "min": 1000,
                "max": 30000,
                "step": 250,
                "desc": "Maximum candidate member length for module ties and inter-module braces.",
            },
        ],
    },
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

_JOB_LOCK = threading.Lock()
_JOB = {
    "running": False,
    "name": None,
    "started": None,
    "finished": None,
    "returncode": None,
    "error": None,
}


def _log(message: str) -> None:
    try:
        print(message)
    except Exception:
        pass


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _get_path(payload: dict, dotted: str, default=None):
    cur = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(payload: dict, dotted: str, value) -> None:
    cur = payload
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _all_fields() -> list[dict]:
    out = []
    for group in FIELD_GROUPS:
        out.extend(group["fields"])
    return out


def _coerce_field(field: dict, value):
    if field.get("key") == "ground_structure.intermediate_levels":
        if isinstance(value, str):
            raw = [v.strip() for v in value.replace(";", ",").split(",")]
            return [float(v) for v in raw if v]
        if isinstance(value, list):
            return [float(v) for v in value]
        return []
    if field.get("type") == "number":
        num = float(value)
        if "min" in field:
            num = max(float(field["min"]), num)
        if "max" in field:
            num = min(float(field["max"]), num)
        if float(field.get("step", 0)) == 1 or field.get("key", "").endswith("nearest_supports_per_target"):
            return int(round(num))
        return num
    return value


def _load_case() -> dict:
    return _read_json(CASE_PATH) or {}


def _save_case_updates(values: dict) -> dict:
    case = _load_case()
    for field in _all_fields():
        key = field["key"]
        if key not in values:
            continue
        _set_path(case, key, _coerce_field(field, values[key]))
    _write_json(CASE_PATH, case)
    return case


def _file_record(path: Path) -> dict:
    return {
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)) if path.exists() else None,
    }


def _summary(solution: dict | None) -> dict | None:
    if not solution:
        return None
    result = solution.get("result", {})
    selected = solution.get("selected_members", [])
    return {
        "ts": solution.get("ts"),
        "engine": result.get("engine"),
        "feasible": result.get("feasible"),
        "stable": result.get("stable"),
        "connected": result.get("connected"),
        "mass_kg": result.get("mass_kg"),
        "max_displacement_mm": result.get("max_displacement_mm"),
        "displacement_limit_mm": result.get("displacement_limit_mm"),
        "max_utilization": result.get("max_utilization"),
        "max_slenderness": result.get("max_slenderness"),
        "error": result.get("error"),
        "member_count": len(selected),
    }


def _context_meta(context: dict | None) -> dict | None:
    if not context:
        return None
    return {
        "ts": context.get("ts"),
        "support_curves": len(context.get("support_curves", [])),
        "existing_beams": len(context.get("existing_beams", [])),
        "modules": len(context.get("modules", [])),
    }


def _read_log_tail(lines: int = 160) -> str:
    if not LOG_PATH.exists():
        return ""
    data = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def _job_snapshot() -> dict:
    with _JOB_LOCK:
        return dict(_JOB)


def _state_payload() -> dict:
    context = _read_json(CONTEXT_PATH)
    graph = _read_json(GRAPH_PATH)
    solution = _read_json(SOLUTION_PATH)
    return {
        "ok": True,
        "job": _job_snapshot(),
        "run_preset": RUN_PRESET,
        "context_meta": _context_meta(context),
        "graph_meta": (graph or {}).get("meta") if graph else None,
        "summary": _summary(solution),
        "graph": graph,
        "solution": solution,
        "files": [
            _file_record(CASE_PATH),
            _file_record(CONTEXT_PATH),
            _file_record(GRAPH_PATH),
            _file_record(SOLUTION_PATH),
            _file_record(LOG_PATH),
        ],
    }


def _config_payload() -> dict:
    return {
        "ok": True,
        "config": _load_case(),
        "groups": FIELD_GROUPS,
        "case_path": str(CASE_PATH).replace("\\", "/"),
    }


def _python_exe() -> str:
    venv_python = REPO_ROOT / ".venv-structure" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _write_job_line(handle, message: str) -> None:
    handle.write(message.rstrip() + "\n")
    handle.flush()


def _run_command(handle, cmd: list[str]) -> None:
    _write_job_line(handle, "$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd,
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"command failed with exit code {proc.returncode}")


def _generate_graph(handle) -> None:
    from ground_structure import generate_ground_structure, load_case, load_context, write_graph

    context = load_context(CONTEXT_PATH)
    case = load_case(CASE_PATH)
    graph = generate_ground_structure(context, case)
    write_graph(GRAPH_PATH, graph)
    _write_job_line(
        handle,
        "wrote {path} nodes={nodes} members={members} meta={meta}".format(
            path=GRAPH_PATH,
            nodes=len(graph.nodes),
            members=len(graph.members),
            meta=graph.meta,
        ),
    )


def _start_job(name: str, fn) -> dict:
    with _JOB_LOCK:
        if _JOB["running"]:
            return {"ok": False, "error": f"{_JOB['name']} is already running"}
        _JOB.update({
            "running": True,
            "name": name,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished": None,
            "returncode": None,
            "error": None,
        })
        RUNS.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8")

    def _runner() -> None:
        error = None
        returncode = 0
        try:
            with LOG_PATH.open("a", encoding="utf-8", errors="replace") as handle:
                _write_job_line(handle, f"== {name} started {time.strftime('%Y-%m-%dT%H:%M:%S')} ==")
                fn(handle)
                _write_job_line(handle, f"== {name} finished {time.strftime('%Y-%m-%dT%H:%M:%S')} ==")
        except Exception as exc:
            error = str(exc)
            returncode = 1
            try:
                with LOG_PATH.open("a", encoding="utf-8", errors="replace") as handle:
                    _write_job_line(handle, f"ERROR: {error}")
            except Exception:
                pass
        with _JOB_LOCK:
            _JOB.update({
                "running": False,
                "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "returncode": returncode,
                "error": error,
            })

    threading.Thread(target=_runner, daemon=True).start()
    return {"ok": True, "job": _job_snapshot()}


def _extract_job(handle) -> None:
    case = _load_case()
    step = _get_path(case, "ground_structure.support_sample_step_mm", 1500.0)
    cmd = [
        _python_exe(),
        str(SCRIPT_DIR / "extract_structure.py"),
        "--out",
        str(CONTEXT_PATH),
        "--sample-step-mm",
        str(float(step)),
    ]
    _run_command(handle, cmd)


def _optimize_job(handle, payload: dict | None) -> None:
    run = {**RUN_PRESET, **(payload or {})}
    cmd = [
        _python_exe(),
        str(SCRIPT_DIR / "optimize_structure.py"),
        "--context",
        str(CONTEXT_PATH),
        "--case",
        str(CASE_PATH),
        "--out",
        str(SOLUTION_PATH),
        "--graph-out",
        str(GRAPH_PATH),
        "--engine",
        str(run.get("engine", "pynite")),
        "--pop",
        str(int(run.get("pop", 8))),
        "--n-gen",
        str(int(run.get("n_gen", 2))),
        "--seed",
        str(int(run.get("seed", 42))),
    ]
    _run_command(handle, cmd)


def _bake_job(handle) -> None:
    cmd = [
        _python_exe(),
        str(SCRIPT_DIR / "bake_structure.py"),
        "--solution",
        str(SOLUTION_PATH),
    ]
    _run_command(handle, cmd)


def _save_config_payload(payload: dict | None) -> dict:
    values = (payload or {}).get("values", payload or {})
    config = _save_case_updates(values)
    return {"ok": True, "config": config, "saved": str(CASE_PATH).replace("\\", "/")}


@app.route("/")
def index():
    if not HTML_PATH.exists():
        return Response(f"missing {HTML_PATH}", status=500, mimetype="text/plain")
    return Response(HTML_PATH.read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(_config_payload())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    return jsonify(_save_config_payload(request.get_json(force=True) or {}))


@app.route("/api/state")
def api_state():
    return jsonify(_state_payload())


@app.route("/api/solution")
def api_solution():
    solution = _read_json(SOLUTION_PATH)
    if solution is None:
        abort(404)
    return jsonify({"ok": True, "solution": solution})


@app.route("/api/log")
def api_log():
    return jsonify({"ok": True, "log_tail": _read_log_tail()})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    return jsonify(_start_job("extract Rhino context", _extract_job))


@app.route("/api/graph", methods=["POST"])
def api_graph():
    return jsonify(_start_job("generate ground structure", _generate_graph))


@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    payload = request.get_json(force=True) or {}
    return jsonify(_start_job("short PyNite optimization", lambda handle: _optimize_job(handle, payload)))


@app.route("/api/bake", methods=["POST"])
def api_bake():
    return jsonify(_start_job("bake to Rhino", _bake_job))


class _StdlibHandler(BaseHTTPRequestHandler):
    server_version = "StructureDashboard/1.0"

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
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:
        _log("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_bytes(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/config":
                self._send_json(200, _config_payload())
            elif path == "/api/state":
                self._send_json(200, _state_payload())
            elif path == "/api/solution":
                solution = _read_json(SOLUTION_PATH)
                if solution is None:
                    self._send_json(404, {"ok": False, "error": "missing solution"})
                else:
                    self._send_json(200, {"ok": True, "solution": solution})
            elif path == "/api/log":
                self._send_json(200, {"ok": True, "log_tail": _read_log_tail()})
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/config":
                self._send_json(200, _save_config_payload(payload))
            elif path == "/api/extract":
                self._send_json(200, _start_job("extract Rhino context", _extract_job))
            elif path == "/api/graph":
                self._send_json(200, _start_job("generate ground structure", _generate_graph))
            elif path == "/api/optimize":
                self._send_json(200, _start_job("short PyNite optimization", lambda handle: _optimize_job(handle, payload)))
            elif path == "/api/bake":
                self._send_json(200, _start_job("bake to Rhino", _bake_job))
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

    if not CASE_PATH.exists():
        _log(f"ERROR: missing {CASE_PATH}")
        return 1
    if not HTML_PATH.exists():
        _log(f"ERROR: missing {HTML_PATH}")
        return 1
    RUNS.mkdir(parents=True, exist_ok=True)

    engine = "Flask" if HAS_FLASK else "stdlib http.server"
    _log(f"Structure optimization dashboard at http://localhost:{args.port}/ ({engine})")
    _log(f"  case: {CASE_PATH}")
    _log(f"  solution: {SOLUTION_PATH}")
    if not args.no_open:
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
