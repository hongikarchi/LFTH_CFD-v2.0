"""LFTH_CFD dashboard server (Flask + sliders).

Run:
    python scripts/dashboard_server.py
    -> opens browser at http://localhost:8080

Tabs:
    Settings   edit config/case.json with sliders + Save / Save & Run buttons
    History    sortable table of runs/_settings_log.jsonl
    Charts     scatter plots (dp vs score, etc)
    Files      project file structure + roles

API:
    GET  /api/config              -> config/case.json
    POST /api/config              -> write config/case.json (validated)
    GET  /api/runs                -> list of settings-log entries
    POST /api/run                 -> trigger fx3d_run.py (background subprocess)
    POST /api/thicken             -> trigger thicken_collider.py with given thickness
    GET  /thumb/<idx>             -> last PNG frame of run #idx
    GET  /api/structure           -> static file-structure metadata
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_file, Response, abort

PROJECT = Path(__file__).resolve().parent.parent

CONFIG_PATH = PROJECT / "config" / "case.json"
BUILD_CONFIG_PATH = PROJECT / "config" / "build.json"
SETTINGS_LOG = PROJECT / "runs" / "_settings_log.jsonl"
SCRIPTS = PROJECT / "scripts"

PORT = 8080

app = Flask(__name__, static_folder=None)


# ---------- field metadata (drives the slider UI) ----------

FIELD_GROUPS = [
    {
        "title": "Simulation",
        "fields": [
            {"key": "dp_m", "label": "dp (cell size)", "unit": "m", "min": 0.02, "max": 0.20, "step": 0.005, "type": "number"},
            {"key": "timemax_s", "label": "timemax", "unit": "s", "min": 1, "max": 120, "step": 1, "type": "number"},
            {"key": "dt_out_s", "label": "dt_out (frame interval)", "unit": "s", "min": 0.01, "max": 0.5, "step": 0.01, "type": "number"},
            {"key": "nozzle_refill_dt_s", "label": "nozzle refill dt", "unit": "s", "min": 0.005, "max": 0.5, "step": 0.005, "type": "number"},
            {"key": "lbm_u_ref", "label": "LBM u_ref (stability)", "unit": "lattice", "min": 0.005, "max": 0.2, "step": 0.005, "type": "number", "desc": "larger = less stable + more dissipative; smaller = slower / stall risk"},
            {"key": "side_walls", "label": "side walls", "unit": "", "type": "select", "options": ["E", "S"], "desc": "E = equilibrium (absorb), S = solid (reflect)"},
            {"key": "floor_type", "label": "floor", "unit": "", "type": "select", "options": ["S", "E"], "desc": "Bottom plane (z=0)"},
        ],
    },
    {
        "title": "Inflow (nozzles)",
        "fields": [
            {"key": "nozzle_LPM", "label": "per-nozzle flow", "unit": "L/min", "min": 1, "max": 500, "step": 1, "type": "number"},
            {"key": "seed_col_h", "label": "seed column height", "unit": "cells", "min": 1, "max": 30, "step": 1, "type": "number"},
        ],
    },
    {
        "title": "Physics",
        "fields": [
            {"key": "surface_tension_Npm", "label": "surface tension", "unit": "N/m", "min": 0, "max": 0.1, "step": 0.005, "type": "number"},
            {"key": "viscosity_m2ps", "label": "viscosity", "unit": "m²/s", "min": 1e-7, "max": 1e-3, "step": 1e-7, "type": "number"},
            {"key": "density_kgpm3", "label": "density", "unit": "kg/m³", "min": 500, "max": 2000, "step": 10, "type": "number"},
            {"key": "gravity_mps2", "label": "gravity", "unit": "m/s²", "min": 0, "max": 20, "step": 0.1, "type": "number"},
        ],
    },
    {
        "title": "Geometry / scoring",
        "fields": [
            {"key": "thicken_thickness_m", "label": "collider thicken thickness", "unit": "m", "min": 0.05, "max": 1.0, "step": 0.01, "type": "number"},
            {"key": "score_slab_thickness_m", "label": "score slab (pos/neg slab z)", "unit": "m", "min": 0.05, "max": 5.0, "step": 0.05, "type": "number"},
            {"key": "fluid_threshold", "label": "phi cutoff (fluid cell threshold)", "unit": "", "min": 0.1, "max": 0.95, "step": 0.05, "type": "number"},
            {"key": "domain_pad_m", "label": "domain padding", "unit": "m", "min": 0.5, "max": 10, "step": 0.5, "type": "number"},
        ],
    },
    {
        "title": "View / output",
        "fields": [
            {"key": "visualization_modes", "label": "PNG viz modes (comma-sep)", "unit": "", "type": "text",
             "desc": "PHI_RAYTRACE,FLAG_SURFACE,FLAG_LATTICE,Q_CRITERION,FIELD,STREAMLINES,PHI_RASTERIZE,PARTICLES"},
            {"key": "camera", "label": "camera (rx ry fov zoom)", "unit": "", "type": "vec4"},
            {"key": "push_to_rhino", "label": "push STL to Rhino after run", "unit": "", "type": "bool"},
        ],
    },
]


BUILD_FIELD_GROUPS = [
    {
        "title": "Solver (compile-time)",
        "fields": [
            {"key": "precision", "label": "precision", "type": "select", "options": ["FP32", "FP16S", "FP16C"],
             "desc": "FP32=baseline 2x VRAM, FP16S=default (2x speed, half VRAM), FP16C=accurate FP16"},
            {"key": "velocity_set", "label": "velocity set", "type": "select", "options": ["D3Q19", "D3Q27"],
             "desc": "D3Q19=default, D3Q27=more accurate (27% slower)"},
            {"key": "subgrid", "label": "LES subgrid (high Re)", "type": "bool",
             "desc": "Smagorinsky-Lilly turbulence model"},
        ],
    },
    {
        "title": "Graphics (compile-time)",
        "fields": [
            {"key": "graphics_u_max", "label": "viz u_max (color scale)", "min": 0.01, "max": 1.0, "step": 0.01, "type": "number"},
            {"key": "graphics_rho_delta", "label": "rho coloring range", "min": 0.0001, "max": 0.1, "step": 0.0001, "type": "number"},
            {"key": "graphics_raytracing_transmittance", "label": "raytracing transmittance", "min": 0, "max": 1, "step": 0.05, "type": "number"},
            {"key": "graphics_raytracing_color", "label": "water color (hex)", "type": "text", "desc": "e.g. 0x005F7F"},
            {"key": "graphics_frame_width", "label": "frame width", "min": 320, "max": 3840, "step": 32, "type": "number"},
            {"key": "graphics_frame_height", "label": "frame height", "min": 240, "max": 2160, "step": 32, "type": "number"},
        ],
    },
]


FILE_STRUCTURE = [
    {"path": "config/case.json", "role": "캐노니컬 runtime 파라미터. Settings 탭에서 편집. fx3d_run.py가 매 실행마다 iter_dir/case.txt로 복사 (재빌드 불필요)."},
    {"path": "config/build.json", "role": "캐노니컬 compile-time 파라미터 (precision/velocity_set/SUBGRID/graphics 상수). Build 탭에서 편집 + Rebuild 버튼."},
    {"path": "runs/_real_targets.json", "role": "Rhino에서 추출한 positive/negative bbox + 230 nozzle 좌표. extract_targets.py로 생성."},
    {"path": "runs/_real_collider.stl", "role": "Rhino env::collider 원본 STL (open mesh)."},
    {"path": "runs/_real_collider_thickened.stl", "role": "thicken_collider.py가 만든 closed manifold. fx3d_run.py가 우선 사용."},
    {"path": "runs/_settings_log.jsonl", "role": "append-only DB. 매 실험 한 줄. History 탭이 이 파일을 읽음."},
    {"path": "runs/iter_*/", "role": "실험마다 한 폴더 (case.txt + nozzles.txt + sculpture.stl + result.json + fx3d_out/{frames,vtk}/)."},
    {"path": "scripts/dashboard_server.py", "role": "이 서버 (Flask). 포트 8080."},
    {"path": "scripts/fx3d_run.py", "role": "통합 runner. CLI + 라이브러리 함수 run_experiment(). --interactive 플래그 = GUI 변종 사용."},
    {"path": "scripts/build_fluidx3d.py", "role": "build.json 읽어 defines.hpp 패치 → msbuild 2회 → PNG + Interactive 두 binary 생성."},
    {"path": "scripts/fx3d_postprocess.py", "role": "VTK → result.json (in_pos, in_neg, score 계산). case.json의 fluid_threshold 사용."},
    {"path": "scripts/extract_targets.py", "role": "Rhino MCP → runs/_real_targets.json + _real_collider.stl."},
    {"path": "scripts/thicken_collider.py", "role": "open mesh → closed manifold. 인자 = 두께(m)."},
    {"path": "scripts/fx3d_visualize_in_rhino.py", "role": "iter_real STL을 Rhino 레이어에 푸시."},
    {"path": "scripts/rhino_mcp_helpers.py", "role": "Rhino MCP socket 호출 헬퍼."},
    {"path": "scripts/ga_sequential.py", "role": "DEAP GA loop (현재 비활성, 추후 활성화)."},
    {"path": "scripts/module_geometry.py", "role": "parametric STL 생성 (추후 신규 design용)."},
    {"path": "external/FluidX3D/src/setup.cpp", "role": "FluidX3D 시뮬 로직. case.txt를 cwd에서 읽음. 수정시 Build 탭 → Rebuild."},
    {"path": "external/FluidX3D/src/defines.hpp", "role": "compile-time 매크로 baseline. build_fluidx3d.py가 build.json대로 임시 패치 후 복구."},
    {"path": "external/FluidX3D/bin/FluidX3D.exe", "role": "PNG 모드 binary (백그라운드, frames + VTK 저장)."},
    {"path": "external/FluidX3D/bin/FluidX3D_interactive.exe", "role": "Interactive 모드 binary (실시간 GUI 윈도우, P/WASD 조작, DB 안 남김)."},
]


# ---------- API routes ----------

@app.route("/")
def index():
    return Response(HTML_TEMPLATE, mimetype="text/html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return jsonify({"config": cfg, "groups": FIELD_GROUPS})


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "expected JSON object"}), 400
    # preserve _comment + other underscore keys from existing
    existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for k, v in existing.items():
        if k.startswith("_") and k not in data:
            data[k] = v
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "saved": list(data.keys())})


@app.route("/api/build_config", methods=["GET"])
def api_build_get():
    cfg = json.loads(BUILD_CONFIG_PATH.read_text(encoding="utf-8"))
    return jsonify({"config": cfg, "groups": BUILD_FIELD_GROUPS})


@app.route("/api/build_config", methods=["POST"])
def api_build_post():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "expected JSON object"}), 400
    existing = json.loads(BUILD_CONFIG_PATH.read_text(encoding="utf-8"))
    for k, v in existing.items():
        if k.startswith("_") and k not in data:
            data[k] = v
    BUILD_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/build", methods=["POST"])
def api_build():
    cmd = [sys.executable, str(SCRIPTS / "build_fluidx3d.py")]
    def _runner():
        log_path = PROJECT / "runs" / "_dashboard_build.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(PROJECT))
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True, "cmd": " ".join(cmd)})


@app.route("/api/build_status")
def api_build_status():
    log_path = PROJECT / "runs" / "_dashboard_build.log"
    out = log_path.read_text(encoding="utf-8")[-3000:] if log_path.exists() else ""
    return jsonify({"log_tail": out})


@app.route("/api/runs", methods=["GET"])
def api_runs():
    entries = []
    if SETTINGS_LOG.exists():
        for line in SETTINGS_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return jsonify({"entries": entries, "count": len(entries)})


_run_locks = {}  # test_id -> threading.Lock-ish flag


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True) or {}
    test_id = data.get("test_id") or f"run_{int(time.time())}"
    no_push = bool(data.get("no_push", False))
    interactive = bool(data.get("interactive", False))

    cmd = [sys.executable, str(SCRIPTS / "fx3d_run.py"), "--test-id", test_id]
    if no_push:
        cmd.append("--no-push")
    if interactive:
        cmd.append("--interactive")

    def _runner():
        log_path = PROJECT / "runs" / f"_dashboard_run_{test_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                   cwd=str(PROJECT))
        _run_locks.pop(test_id, None)

    if test_id in _run_locks:
        return jsonify({"ok": False, "error": f"run {test_id} already in progress"}), 409
    _run_locks[test_id] = True
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return jsonify({"ok": True, "test_id": test_id, "cmd": " ".join(cmd)})


@app.route("/api/thicken", methods=["POST"])
def api_thicken():
    data = request.get_json(force=True) or {}
    thickness = float(data.get("thickness_m", 0.20))
    cmd = [sys.executable, str(SCRIPTS / "thicken_collider.py"), str(thickness)]

    def _runner():
        log_path = PROJECT / "runs" / "_dashboard_thicken.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(PROJECT))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return jsonify({"ok": True, "thickness_m": thickness, "cmd": " ".join(cmd)})


@app.route("/api/run_status/<test_id>")
def api_run_status(test_id):
    log_path = PROJECT / "runs" / f"_dashboard_run_{test_id}.log"
    running = test_id in _run_locks
    out = ""
    if log_path.exists():
        try:
            out = log_path.read_text(encoding="utf-8")[-2000:]
        except Exception:
            out = ""
    return jsonify({"running": running, "log_tail": out})


@app.route("/thumb/<int:idx>")
def thumb(idx: int):
    entries = []
    if SETTINGS_LOG.exists():
        for line in SETTINGS_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    if idx < 0 or idx >= len(entries):
        abort(404)
    frames_dir = entries[idx].get("frames_dir")
    if not frames_dir:
        abort(404)
    p = Path(frames_dir)
    if not p.is_dir():
        abort(404)
    pngs = sorted(p.glob("*.png"))
    if not pngs:
        abort(404)
    return send_file(str(pngs[-1]), mimetype="image/png")


@app.route("/api/structure")
def api_structure():
    return jsonify({"files": FILE_STRUCTURE})


# ---------- HTML (single page, tab-switching) ----------

HTML_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>LFTH_CFD · dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #f6f8fa; --panel: #ffffff; --border: #d0d7de; --border-soft: #e5e8eb;
  --text: #1f2328; --text-soft: #57606a; --text-faint: #8c959f;
  --accent: #0969da; --accent-soft: #ddf4ff;
  --green: #1a7f37; --green-bg: #dafbe1;
  --grey: #6e7781; --grey-bg: #eaeef2;
  --amber: #bf8700; --amber-bg: #fff8c5;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5; }
code, .mono { font-family: var(--mono); }

header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 18px 28px 14px; }
header .title { font-size: 19px; font-weight: 600; margin: 0 0 6px; }
header .meta { display: flex; flex-wrap: wrap; gap: 6px 10px; font-size: 12.5px; color: var(--text-soft); }
header .chip { display: inline-flex; align-items: center; gap: 5px; background: var(--bg);
               border: 1px solid var(--border-soft); border-radius: 999px; padding: 2px 9px; }
header .chip b { color: var(--text); font-weight: 600; }

.wrap { max-width: 1200px; margin: 0 auto; padding: 0 28px 60px; }

.tabbar { display: flex; flex-wrap: wrap; gap: 2px; border-bottom: 1px solid var(--border);
          position: sticky; top: 0; background: var(--bg); z-index: 5; padding-top: 14px; }
.tab-btn { appearance: none; border: none; background: transparent; font-family: var(--sans);
           font-size: 13.5px; font-weight: 500; color: var(--text-soft);
           padding: 9px 14px 8px; cursor: pointer; border-bottom: 2.5px solid transparent;
           margin-bottom: -1px; border-radius: 6px 6px 0 0; }
.tab-btn:hover { background: var(--panel); color: var(--text); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); background: var(--panel); }
.panel { display: none; padding-top: 22px; }
.panel.active { display: block; }

.section-title { font-size: 12px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
                 color: var(--text-faint); margin: 26px 0 10px; }
.section-title:first-child { margin-top: 0; }

.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }

/* form */
.fld { display: grid; grid-template-columns: 220px 1fr 100px 60px; align-items: center; gap: 12px; padding: 7px 0; border-bottom: 1px dashed var(--border-soft); }
.fld:last-child { border-bottom: none; }
.fld label { color: var(--text-soft); font-size: 13px; }
.fld .desc { color: var(--text-faint); font-size: 11.5px; }
.fld input[type=range] { width: 100%; }
.fld input[type=number], .fld input[type=text], .fld select {
  font-family: var(--mono); font-size: 12.5px;
  border: 1px solid var(--border); border-radius: 5px; padding: 4px 6px; background: var(--panel); color: var(--text); width: 100%; }
.fld .unit { font-family: var(--mono); color: var(--text-faint); font-size: 11.5px; }

.btnrow { display: flex; gap: 10px; margin: 20px 0 12px; flex-wrap: wrap; }
button.action { background: var(--accent); color: white; border: none; border-radius: 6px;
                padding: 8px 16px; font-weight: 600; cursor: pointer; }
button.action:hover { filter: brightness(1.1); }
button.action.secondary { background: var(--grey-bg); color: var(--text); }
button.action.secondary:hover { background: var(--border); }
button.action.danger { background: #cf222e; }
.status-bar { font-family: var(--mono); font-size: 12px; color: var(--text-soft); padding: 6px 10px;
              background: var(--grey-bg); border-radius: 5px; margin-top: 12px; min-height: 22px; }

table.grid { width: 100%; border-collapse: collapse; background: var(--panel);
             border: 1px solid var(--border); border-radius: 8px; overflow: hidden; font-size: 12.5px; }
table.grid th, table.grid td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
table.grid th:first-child, table.grid td:first-child,
table.grid th:nth-child(2), table.grid td:nth-child(2) { text-align: left; }
table.grid th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
                color: var(--text-faint); background: var(--bg); cursor: pointer; user-select: none; }
table.grid tr.best td { background: var(--green-bg); font-weight: 600; }
table.grid tr:hover td { background: var(--bg); }
table.grid img { height: 50px; border-radius: 3px; vertical-align: middle; }

.charts { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-top: 20px; }
.charts h3 { margin: 0 0 6px 0; font-size: 12px; font-weight: 700;
             color: var(--text-faint); text-transform: uppercase; letter-spacing: 0.06em; }
.charts canvas { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; max-height: 260px; }

.filebar { font-family: var(--mono); font-size: 12.5px; color: var(--accent); font-weight: 600; }
.fileitem { margin: 6px 0 12px 0; }
.fileitem .role { color: var(--text-soft); font-size: 13px; margin-top: 3px; }

.q { padding: 4px 8px; font-size: 13px; border: 1px solid var(--border); border-radius: 5px; width: 280px; }
</style>
</head>
<body>
<header>
  <h1 class="title">LFTH_CFD · dashboard</h1>
  <div class="meta">
    <span class="chip">port <b class="mono">8080</b></span>
    <span class="chip">config <b class="mono">config/case.json</b></span>
    <span class="chip">db <b class="mono">runs/_settings_log.jsonl</b></span>
  </div>
</header>

<div class="wrap">
  <nav class="tabbar">
    <button class="tab-btn active" data-tab="settings">Settings</button>
    <button class="tab-btn" data-tab="build">Build</button>
    <button class="tab-btn" data-tab="history">History</button>
    <button class="tab-btn" data-tab="charts">Charts</button>
    <button class="tab-btn" data-tab="files">Files</button>
  </nav>

  <section class="panel active" id="panel-settings">
    <div id="form_groups"></div>
    <div class="btnrow">
      <button class="action" id="btn_save">Save config</button>
      <button class="action" id="btn_save_run">Save + Run (simulation)</button>
      <button class="action" id="btn_save_run_int" style="background:#bf8700">Save + Run (interactive)</button>
      <button class="action secondary" id="btn_thicken">Re-thicken collider</button>
      <button class="action secondary" id="btn_reload">Reload from disk</button>
    </div>
    <div class="status-bar" id="status_bar">ready.</div>
  </section>

  <section class="panel" id="panel-build">
    <div id="build_form_groups"></div>
    <div class="btnrow">
      <button class="action" id="btn_build_save">Save build config</button>
      <button class="action" id="btn_build_run">Save + Rebuild (~1 min)</button>
      <button class="action secondary" id="btn_build_reload">Reload from disk</button>
    </div>
    <div class="status-bar" id="build_status">ready.</div>
    <pre id="build_log" style="margin-top:10px; font-family:var(--mono); font-size:11.5px; background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:10px; max-height:240px; overflow:auto; white-space:pre-wrap;"></pre>
  </section>

  <section class="panel" id="panel-history">
    <div class="btnrow">
      <input class="q" id="q_history" placeholder="filter (case-insensitive)" />
      <button class="action secondary" id="btn_refresh_runs">Refresh</button>
      <span class="status-bar" id="history_count" style="margin: 0;"></span>
    </div>
    <div id="history_table"></div>
  </section>

  <section class="panel" id="panel-charts">
    <div class="btnrow">
      <button class="action secondary" id="btn_refresh_charts">Refresh</button>
    </div>
    <div class="charts">
      <div><h3>dp vs score</h3><canvas id="c_dp"></canvas></div>
      <div><h3>timemax vs score</h3><canvas id="c_t"></canvas></div>
      <div><h3>nozzle_LPM vs score</h3><canvas id="c_l"></canvas></div>
      <div><h3>thicken vs score</h3><canvas id="c_th"></canvas></div>
    </div>
  </section>

  <section class="panel" id="panel-files">
    <div class="section-title">Project structure</div>
    <div id="file_list"></div>
  </section>
</div>

<script>
// ---------- tab switching ----------
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'history') loadRuns();
    if (btn.dataset.tab === 'charts') loadCharts();
    if (btn.dataset.tab === 'files') loadFiles();
    if (btn.dataset.tab === 'build') loadBuildConfig();
  });
});

// ---------- helpers ----------
function setStatus(msg) { document.getElementById('status_bar').textContent = msg; }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// ---------- settings form ----------
let CURRENT_GROUPS = [];
let CURRENT_CONFIG = {};

async function loadConfig() {
  const r = await fetch('/api/config'); const j = await r.json();
  CURRENT_CONFIG = j.config; CURRENT_GROUPS = j.groups; renderForm();
}

function renderForm() {
  const root = document.getElementById('form_groups'); root.innerHTML = '';
  for (const grp of CURRENT_GROUPS) {
    const sec = document.createElement('div');
    sec.innerHTML = `<div class="section-title">${esc(grp.title)}</div>`;
    const card = document.createElement('div'); card.className = 'card';
    for (const f of grp.fields) {
      const val = CURRENT_CONFIG[f.key];
      const row = document.createElement('div'); row.className = 'fld';
      row.innerHTML = renderField(f, val);
      card.appendChild(row);
    }
    sec.appendChild(card); root.appendChild(sec);
  }
  // wire up sliders -> number sync
  document.querySelectorAll('.fld input[type=range]').forEach(el => {
    el.addEventListener('input', e => {
      const num = document.querySelector('input[type=number][data-key="' + el.dataset.key + '"]');
      if (num) num.value = el.value;
    });
  });
  document.querySelectorAll('.fld input[type=number]').forEach(el => {
    el.addEventListener('input', e => {
      const slider = document.querySelector('input[type=range][data-key="' + el.dataset.key + '"]');
      if (slider) slider.value = el.value;
    });
  });
}

function renderField(f, val) {
  let label = `<label>${esc(f.label)}${f.desc ? '<div class="desc">' + esc(f.desc) + '</div>' : ''}</label>`;
  if (f.type === 'number') {
    return label + `
      <input type="range" data-key="${f.key}" min="${f.min}" max="${f.max}" step="${f.step}" value="${val ?? f.min}">
      <input type="number" data-key="${f.key}" min="${f.min}" max="${f.max}" step="${f.step}" value="${val ?? ''}">
      <span class="unit">${esc(f.unit || '')}</span>`;
  }
  if (f.type === 'select') {
    const opts = f.options.map(o => `<option value="${esc(o)}"${val === o ? ' selected' : ''}>${esc(o)}</option>`).join('');
    return label + `<select data-key="${f.key}">${opts}</select><span></span><span class="unit">${esc(f.unit || '')}</span>`;
  }
  if (f.type === 'bool') {
    return label + `<input type="checkbox" data-key="${f.key}"${val ? ' checked' : ''}><span></span><span></span>`;
  }
  if (f.type === 'vec4') {
    const arr = Array.isArray(val) ? val : [0,0,0,0];
    return label + `<div style="display:flex; gap:6px;">
      <input type="number" data-key="${f.key}__0" value="${arr[0]}" style="width:60px">
      <input type="number" data-key="${f.key}__1" value="${arr[1]}" style="width:60px">
      <input type="number" data-key="${f.key}__2" value="${arr[2]}" style="width:60px">
      <input type="number" data-key="${f.key}__3" value="${arr[3]}" style="width:60px">
    </div><span></span><span></span>`;
  }
  return label + `<input type="text" data-key="${f.key}" value="${esc(val)}"><span></span><span></span>`;
}

function collectFormValues() {
  const out = {};
  for (const grp of CURRENT_GROUPS) {
    for (const f of grp.fields) {
      if (f.type === 'number') {
        const el = document.querySelector('input[type=number][data-key="' + f.key + '"]');
        if (el && el.value !== '') out[f.key] = parseFloat(el.value);
      } else if (f.type === 'select') {
        const el = document.querySelector('select[data-key="' + f.key + '"]');
        if (el) out[f.key] = el.value;
      } else if (f.type === 'bool') {
        const el = document.querySelector('input[type=checkbox][data-key="' + f.key + '"]');
        out[f.key] = !!(el && el.checked);
      } else if (f.type === 'vec4') {
        out[f.key] = [0,1,2,3].map(i => {
          const el = document.querySelector('input[data-key="' + f.key + '__' + i + '"]');
          return el ? parseFloat(el.value) : 0;
        });
      }
    }
  }
  return out;
}

async function saveConfig() {
  const payload = collectFormValues();
  setStatus('saving...');
  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const j = await r.json();
  setStatus(j.ok ? 'saved ' + j.saved.length + ' keys.' : 'save FAILED: ' + esc(j.error));
}

async function triggerRun(interactive) {
  await saveConfig();
  const prefix = interactive ? 'iact_' : 'dash_';
  const test_id = prefix + Math.floor(Date.now() / 1000);
  setStatus('launching ' + (interactive ? 'INTERACTIVE ' : '') + 'run ' + test_id + ' ...');
  const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify({test_id, interactive})});
  const j = await r.json();
  if (!j.ok) { setStatus('run launch FAILED: ' + esc(j.error)); return; }
  pollRun(test_id);
}

async function pollRun(test_id) {
  setStatus('running ' + test_id + ' ...');
  const interval = setInterval(async () => {
    const r = await fetch('/api/run_status/' + test_id);
    const j = await r.json();
    if (!j.running) {
      clearInterval(interval);
      setStatus('run ' + test_id + ' done. log tail: ' + (j.log_tail || '').slice(-120));
      loadRuns();
    } else {
      setStatus('running ' + test_id + ' ... tail: ' + (j.log_tail || '').slice(-120));
    }
  }, 3000);
}

async function triggerThicken() {
  const thickField = document.querySelector('input[type=number][data-key="thicken_thickness_m"]');
  const t = thickField ? parseFloat(thickField.value) : 0.2;
  setStatus('re-thickening collider at ' + t + 'm ...');
  const r = await fetch('/api/thicken', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({thickness_m: t})});
  const j = await r.json();
  setStatus(j.ok ? 'thicken launched (' + j.thickness_m + 'm).' : 'FAILED: ' + esc(j.error));
}

document.getElementById('btn_save').addEventListener('click', saveConfig);
document.getElementById('btn_save_run').addEventListener('click', () => triggerRun(false));
document.getElementById('btn_save_run_int').addEventListener('click', () => triggerRun(true));
document.getElementById('btn_thicken').addEventListener('click', triggerThicken);
document.getElementById('btn_reload').addEventListener('click', loadConfig);

// ---------- build (compile-time) form ----------
let BUILD_GROUPS = [];
let BUILD_CONFIG = {};

async function loadBuildConfig() {
  const r = await fetch('/api/build_config'); const j = await r.json();
  BUILD_CONFIG = j.config; BUILD_GROUPS = j.groups; renderBuildForm();
}

function renderBuildForm() {
  const root = document.getElementById('build_form_groups'); root.innerHTML = '';
  for (const grp of BUILD_GROUPS) {
    const sec = document.createElement('div');
    sec.innerHTML = `<div class="section-title">${esc(grp.title)}</div>`;
    const card = document.createElement('div'); card.className = 'card';
    for (const f of grp.fields) {
      const val = BUILD_CONFIG[f.key];
      const row = document.createElement('div'); row.className = 'fld';
      row.innerHTML = renderBuildField(f, val);
      card.appendChild(row);
    }
    sec.appendChild(card); root.appendChild(sec);
  }
  document.querySelectorAll('#build_form_groups input[type=range]').forEach(el => {
    el.addEventListener('input', e => {
      const num = document.querySelector('#build_form_groups input[type=number][data-key="' + el.dataset.key + '"]');
      if (num) num.value = el.value;
    });
  });
  document.querySelectorAll('#build_form_groups input[type=number]').forEach(el => {
    el.addEventListener('input', e => {
      const slider = document.querySelector('#build_form_groups input[type=range][data-key="' + el.dataset.key + '"]');
      if (slider) slider.value = el.value;
    });
  });
}

function renderBuildField(f, val) {
  let label = `<label>${esc(f.label)}${f.desc ? '<div class="desc">' + esc(f.desc) + '</div>' : ''}</label>`;
  if (f.type === 'number') {
    return label + `
      <input type="range" data-key="${f.key}" min="${f.min}" max="${f.max}" step="${f.step}" value="${val ?? f.min}">
      <input type="number" data-key="${f.key}" min="${f.min}" max="${f.max}" step="${f.step}" value="${val ?? ''}">
      <span class="unit">${esc(f.unit || '')}</span>`;
  }
  if (f.type === 'select') {
    const opts = f.options.map(o => `<option value="${esc(o)}"${val === o ? ' selected' : ''}>${esc(o)}</option>`).join('');
    return label + `<select data-key="${f.key}">${opts}</select><span></span><span></span>`;
  }
  if (f.type === 'bool') {
    return label + `<input type="checkbox" data-key="${f.key}"${val ? ' checked' : ''}><span></span><span></span>`;
  }
  if (f.type === 'text') {
    return label + `<input type="text" data-key="${f.key}" value="${esc(val)}"><span></span><span></span>`;
  }
  return '';
}

function collectBuildValues() {
  const out = {};
  for (const grp of BUILD_GROUPS) {
    for (const f of grp.fields) {
      if (f.type === 'number') {
        const el = document.querySelector('#build_form_groups input[type=number][data-key="' + f.key + '"]');
        if (el && el.value !== '') out[f.key] = parseFloat(el.value);
      } else if (f.type === 'select') {
        const el = document.querySelector('#build_form_groups select[data-key="' + f.key + '"]');
        if (el) out[f.key] = el.value;
      } else if (f.type === 'bool') {
        const el = document.querySelector('#build_form_groups input[type=checkbox][data-key="' + f.key + '"]');
        out[f.key] = !!(el && el.checked);
      } else if (f.type === 'text') {
        const el = document.querySelector('#build_form_groups input[type=text][data-key="' + f.key + '"]');
        if (el) out[f.key] = el.value;
      }
    }
  }
  return out;
}

async function saveBuildConfig() {
  const payload = collectBuildValues();
  document.getElementById('build_status').textContent = 'saving...';
  const r = await fetch('/api/build_config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const j = await r.json();
  document.getElementById('build_status').textContent = j.ok ? 'saved.' : 'save FAILED: ' + esc(j.error);
}

async function triggerBuild() {
  await saveBuildConfig();
  document.getElementById('build_status').textContent = 'rebuilding (~1 min)...';
  document.getElementById('build_log').textContent = '';
  await fetch('/api/build', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
  const interval = setInterval(async () => {
    const r = await fetch('/api/build_status'); const j = await r.json();
    document.getElementById('build_log').textContent = j.log_tail || '(building...)';
    if ((j.log_tail || '').includes('DONE:')) {
      clearInterval(interval);
      document.getElementById('build_status').textContent = 'build done. both binaries refreshed.';
    } else if ((j.log_tail || '').match(/FAILED|ERROR/)) {
      clearInterval(interval);
      document.getElementById('build_status').textContent = 'build FAILED — see log below.';
    }
  }, 3000);
}

document.getElementById('btn_build_save').addEventListener('click', saveBuildConfig);
document.getElementById('btn_build_run').addEventListener('click', triggerBuild);
document.getElementById('btn_build_reload').addEventListener('click', loadBuildConfig);

// ---------- history table ----------
let CACHED_RUNS = [];

async function loadRuns() {
  const r = await fetch('/api/runs'); const j = await r.json();
  CACHED_RUNS = j.entries || [];
  document.getElementById('history_count').textContent = j.count + ' runs';
  renderHistoryTable();
}

const HIST_COLS = [
  ['ts', 'ts'], ['test_id', 'id'], ['side_walls','walls'],
  ['dp_m','dp'], ['timemax_s','tmax'], ['nozzle_LPM','LPM'], ['thicken_thickness_m','thick'],
  ['score','score'], ['in_positive','pos'], ['in_negative','neg'], ['in_column','col'],
  ['splash','splash'], ['total','total'], ['wall_s','wall'],
];

function fmt(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
  return esc(v);
}

function renderHistoryTable() {
  const root = document.getElementById('history_table');
  if (!CACHED_RUNS.length) { root.innerHTML = '<p>No runs logged yet. Click <b>Save + Run</b> in Settings.</p>'; return; }
  // best score row
  const best_i = CACHED_RUNS.reduce((b, e, i) => (e.score ?? -1) > (CACHED_RUNS[b].score ?? -1) ? i : b, 0);

  const thead = '<tr>' + HIST_COLS.map(([k,l]) => '<th data-key="'+k+'">'+esc(l)+'</th>').join('') + '<th>preview</th></tr>';
  const rows = CACHED_RUNS.map((e, i) => {
    const cls = i === best_i ? ' class="best"' : '';
    const cells = HIST_COLS.map(([k]) => '<td data-num="' + (typeof e[k] === 'number' ? e[k] : '') + '">' + fmt(e[k]) + '</td>').join('');
    return '<tr' + cls + '>' + cells + '<td><img src="/thumb/' + i + '" onerror="this.style.display=\'none\'"></td></tr>';
  }).join('');

  root.innerHTML = '<table class="grid"><thead>' + thead + '</thead><tbody>' + rows + '</tbody></table>';

  // sort + filter
  document.querySelectorAll('#history_table thead th').forEach((th, idx) => {
    let asc = false;
    th.addEventListener('click', () => {
      const rows = Array.from(document.querySelectorAll('#history_table tbody tr'));
      rows.sort((a, b) => {
        const av = a.cells[idx].dataset.num || a.cells[idx].innerText;
        const bv = b.cells[idx].dataset.num || b.cells[idx].innerText;
        const an = parseFloat(av), bn = parseFloat(bv);
        const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
        return asc ? cmp : -cmp;
      });
      asc = !asc;
      const tbody = document.querySelector('#history_table tbody');
      rows.forEach(r => tbody.appendChild(r));
    });
  });
}

document.getElementById('q_history').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#history_table tbody tr').forEach(r => {
    r.style.display = r.innerText.toLowerCase().includes(q) ? '' : 'none';
  });
});
document.getElementById('btn_refresh_runs').addEventListener('click', loadRuns);

// ---------- charts ----------
const CHART_INSTANCES = {};
async function loadCharts() {
  if (!CACHED_RUNS.length) { await loadRuns(); }
  mkScatter('c_dp', 'dp_m');
  mkScatter('c_t', 'timemax_s');
  mkScatter('c_l', 'nozzle_LPM');
  mkScatter('c_th', 'thicken_thickness_m');
}
function mkScatter(canvasId, key) {
  if (CHART_INSTANCES[canvasId]) CHART_INSTANCES[canvasId].destroy();
  const pts = CACHED_RUNS.filter(d => d[key] !== null && d[key] !== undefined && d.score !== null)
                          .map(d => ({x: d[key], y: d.score, label: d.test_id}));
  CHART_INSTANCES[canvasId] = new Chart(document.getElementById(canvasId), {
    type: 'scatter',
    data: {datasets: [{data: pts, backgroundColor: '#0969da', pointRadius: 5}]},
    options: {
      plugins: {tooltip: {callbacks: {label: c => c.raw.label + ': (' + c.raw.x + ', ' + (c.raw.y || 0).toFixed(4) + ')'}}, legend: {display: false}},
      scales: {x: {title: {display: true, text: key}}, y: {title: {display: true, text: 'score'}, min: 0, max: 1}}
    }
  });
}
document.getElementById('btn_refresh_charts').addEventListener('click', loadCharts);

// ---------- files ----------
async function loadFiles() {
  const r = await fetch('/api/structure'); const j = await r.json();
  document.getElementById('file_list').innerHTML = j.files.map(f =>
    '<div class="fileitem"><div class="filebar">' + esc(f.path) + '</div><div class="role">' + esc(f.role) + '</div></div>'
  ).join('');
}

// ---------- init ----------
loadConfig();
</script>
</body>
</html>
"""


def open_browser_later():
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://localhost:{PORT}/")
    except Exception:
        pass


def main():
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found")
        sys.exit(1)
    print(f"LFTH_CFD dashboard at http://localhost:{PORT}/")
    print(f"  config: {CONFIG_PATH}")
    print(f"  db:     {SETTINGS_LOG}")
    threading.Thread(target=open_browser_later, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
