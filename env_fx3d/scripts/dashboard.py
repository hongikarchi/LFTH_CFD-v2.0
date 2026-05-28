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

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent

CONFIG_PATH = MODULE_ROOT / "config" / "case.json"
BUILD_CONFIG_PATH = MODULE_ROOT / "config" / "build.json"
SETTINGS_LOG = MODULE_ROOT / "_settings_log.jsonl"
SCRIPTS = SCRIPT_DIR
HTML_PATH = REPO_ROOT / "dashboard.html"

PORT = 8080

app = Flask(__name__, static_folder=None)


# ---------- field metadata (drives the slider UI) ----------

FIELD_GROUPS = [
    {
        "title": "Simulation",
        "fields": [
            {"key": "dp_m", "label": "dp", "unit": "m", "step": 0.005, "type": "number",
             "desc": "격자 셀 크기. 작을수록 정확하지만 셀 수가 폭증 (dp/2 = 8배 셀, ~16배 시간)."},
            {"key": "timemax_s", "label": "timemax", "unit": "s", "step": 1, "type": "number",
             "desc": "시뮬 길이(초). 물이 도메인을 통과·정착할 시간."},
            {"key": "dt_out_s", "label": "dt_out", "unit": "s", "step": 0.01, "type": "number",
             "desc": "PNG/VTK 출력 간격. 작을수록 부드러운 영상 + 더 많은 디스크 사용."},
            {"key": "lbm_u_ref", "label": "LBM u_ref", "unit": "lattice", "step": 0.005, "type": "number",
             "desc": "LBM 속도 스케일 → relaxation time 결정. 크면 빠름·불안정, 작으면 느림·정체 위험."},
            {"key": "side_walls", "label": "side walls", "unit": "", "type": "select", "options": ["E", "S"],
             "desc": "시뮬 박스의 옆 4면 (xy 경계). E = 닿은 물 사라짐 (열림). S = 벽처럼 반사 (물 가둠). S여야 진짜 cascade 끝까지 보임."},
            {"key": "floor_type", "label": "floor", "unit": "", "type": "select", "options": ["S", "E"],
             "desc": "시뮬 박스의 바닥 (z=0). S = pond 역할 (반사). E = 흡수 (도메인 아래로 빠짐)."},
        ],
    },
    {
        "title": "Inflow (nozzles)",
        "fields": [
            {"key": "nozzle_LPM", "label": "per-nozzle flow", "unit": "L/min", "step": 1, "type": "number",
             "desc": "각 노즐의 유량(L/min). vz = Q/A 자동 변환.\n"
                     "60 LPM × dp=0.1 → 0.1 m/s → LBM stability floor 1.0 m/s 적용.\n"
                     "230 노즐 × 60 LPM = 0.23 m³/s 전체."},
            {"key": "nozzle_rho_inflow", "label": "nozzle TYPE_E rho", "unit": "lattice", "step": 0.05, "type": "number",
             "desc": "노즐 TYPE_E cell의 고정 밀도 (lattice unit).\n"
                     "1.0 = 중성 (자연 분출, 기본). >1 = 고압 → 강한 jet (예: 1.05~1.10).\n"
                     ">1.3은 LBM 불안정. 자연 동작에는 1.0 권장."},
            {"key": "nozzle_area_cells", "label": "nozzle area cells", "unit": "cells", "step": 1, "type": "number",
             "desc": "노즐 footprint. (2r+1)² 셀, r = val/2.\n"
                     "1 → 1×1 (1셀), 2/3 → 3×3 (9셀), 4/5 → 5×5 (25셀).\n"
                     "dp=0.1, val=2 → 30cm 직경 = 사실적 분수 노즐."},
            {"key": "seed_col_h", "label": "seed column height", "unit": "cells", "step": 1, "type": "number",
             "desc": "노즐 아래 warmup column 깊이 (init only, refill로 유지).\n"
                     "12 cells × dp=0.1 = 1.2m 가시 노즐 stream.\n"
                     "M0 top z=27.5 + col_h * dp가 모듈 안 들어가면 안 됨."},
        ],
    },
    {
        "title": "Pond pre-fill (optional)",
        "fields": [
            {"key": "pond_prefill_z_m", "label": "pre-fill z top", "unit": "m", "step": 0.1, "type": "number",
             "desc": "t=0에 z ≤ 이 값인 셀들 TYPE_F + phi=1로 미리 채움.\n"
                     "0 = 비활성. >0 = 연못 초기 수위 또는 모듈 미리 채우기.\n"
                     "Solid/wall 셀은 skip."},
            {"key": "pond_prefill_z_bot_m", "label": "pre-fill z bottom", "unit": "m", "step": 0.1, "type": "number",
             "desc": "band-style 하한 (선택). z ≥ 이 값 ∧ z ≤ pre-fill z top.\n"
                     "0 = half-open (전체 z ≤ top 채움).\n"
                     ">0 = band: 특정 z 범위만 채움 (예: M0 interior만)."},
            {"key": "pond_prefill_xy_bbox_m", "label": "pre-fill xy bbox", "unit": "m", "type": "vec4",
             "desc": "xy 제한 (선택, [x_lo, y_lo, x_hi, y_hi]).\n"
                     "[0,0,0,0] = 비활성, 전체 xy 채움.\n"
                     "valid bbox 입력 시 그 영역 내에서만 채움."},
        ],
    },
    {
        "title": "Physics",
        "fields": [
            {"key": "surface_tension_Npm", "label": "surface tension", "unit": "N/m", "step": 0.005, "type": "number",
             "desc": "물 표면장력 (실제 ~0.072 N/m at 20°C). 너무 크면 물방울이 안 깨짐."},
            {"key": "viscosity_m2ps", "label": "viscosity", "unit": "m²/s", "step": 1e-7, "type": "number",
             "desc": "동점성 (실제 물 ~1e-6 m²/s). 너무 작으면 발산, 크면 점도 흐름."},
            {"key": "density_kgpm3", "label": "density", "unit": "kg/m³", "step": 10, "type": "number",
             "desc": "유체 밀도 (물 1000 kg/m³)."},
            {"key": "gravity_mps2", "label": "gravity", "unit": "m/s²", "step": 0.1, "type": "number",
             "desc": "중력가속도 (지구 9.81). 0이면 무중력 = 흐름 안 생김."},
        ],
    },
    {
        "title": "Postprocess / Pipeline (does not affect simulation)",
        "fields": [
            {"key": "thicken_thickness_m", "label": "thicken thickness", "unit": "m", "step": 0.01, "type": "number",
             "desc": "[POSTPROCESS ONLY] sim 결과 변경 없음.\n"
                     "thicken_collider.py에서 open mesh 닫을 때 두께. dp의 2배 이상 권장."},
            {"key": "score_slab_thickness_m", "label": "score slab", "unit": "m", "step": 0.05, "type": "number",
             "desc": "[POSTPROCESS ONLY] sim 결과 변경 없음.\n"
                     "fx3d_postprocess.py에서 positive/negative 평면 Brep을 슬랩으로 확장하는 두께. score 판정 영역."},
            {"key": "fluid_threshold", "label": "phi cutoff", "unit": "", "step": 0.05, "type": "number",
             "desc": "[POSTPROCESS ONLY] sim 결과 변경 없음.\n"
                     "fx3d_postprocess.py에서 어느 phi 값부터 '물 셀'로 셀지 (0~1)."},
            {"key": "domain_pad_m", "label": "domain padding", "unit": "m", "step": 0.5, "type": "number",
             "desc": "[POSTPROCESS ONLY] sim 결과 변경 없음 (setup.cpp가 안 읽음).\n"
                     "fx3d_run.py가 domain_bbox_m 계산 시 sculpture 주변 padding으로만 사용."},
        ],
    },
    {
        "title": "View / output",
        "fields": [
            {"key": "visualization_modes", "label": "PNG viz modes", "unit": "", "type": "multi",
             "options": ["PHI_RAYTRACE", "PHI_RASTERIZE", "FLAG_SURFACE", "FLAG_LATTICE",
                          "Q_CRITERION", "FIELD", "STREAMLINES", "PARTICLES"],
             "desc": "여러 모드 동시 선택 가능 (체크된 칩 전부 합성됨)\n"
                     "PHI_RAYTRACE: 물 표면을 광선추적으로 사실적 렌더 (가장 예쁨, 느림)\n"
                     "PHI_RASTERIZE: 물 표면을 빠르게 raster 렌더 (광선추적 안 함)\n"
                     "FLAG_SURFACE: sculpture (TYPE_S)의 표면을 흰색 wireframe으로\n"
                     "FLAG_LATTICE: 격자 cell 경계 표시 (디버그용, 무거움)\n"
                     "Q_CRITERION: 와류(소용돌이) 등고면 표시\n"
                     "FIELD: 단면 색상으로 속도/밀도장 표시\n"
                     "STREAMLINES: 유선 (속도 흐름선) 표시\n"
                     "PARTICLES: 입자 (PARTICLES 확장 켜야 함)"},
            {"key": "camera", "label": "camera (rx ry fov zoom)", "unit": "", "type": "vec4",
             "desc": "rx/ry = 회전 각도(도), fov = 시야각, zoom = 확대. ex: 200 15 60 1 = 뒤편에서 살짝 위."},
            {"key": "push_to_rhino", "label": "Rhino push", "unit": "", "type": "bool",
             "desc": "sim 끝나면 sculpture STL을 Rhino MCP로 push (Rhino 켜져 있어야 함)."},
        ],
    },
]


BUILD_FIELD_GROUPS = [
    {
        "title": "Solver (compile-time)",
        "fields": [
            {"key": "precision", "label": "precision", "type": "select", "options": ["FP32", "FP16S", "FP16C"],
             "desc": "수치 정밀도. FP32 = 정확하지만 VRAM 2배. FP16S = 기본 (속도 2배, VRAM 절반). FP16C = FP16의 정확 변종."},
            {"key": "velocity_set", "label": "velocity set", "type": "select", "options": ["D3Q19", "D3Q27"],
             "desc": "LBM 격자 모델. D3Q19 = 기본 (19 방향). D3Q27 = 더 정확하지만 27% 느림."},
            {"key": "collision", "label": "collision operator", "type": "select", "options": ["SRT", "TRT"],
             "desc": "충돌 연산자.\n"
                     "SRT (single relaxation) = 기본, 단순/빠름.\n"
                     "TRT (two relaxation) = 벽 근처 정확도 + 안정성 향상, 비용 거의 동일."},
            {"key": "subgrid", "label": "LES subgrid", "type": "bool",
             "desc": "Smagorinsky-Lilly LES 모델. 고 Reynolds 수에서 안정화 효과."},
            {"key": "particles", "label": "particles", "type": "bool",
             "desc": "Lagrangian tracer 입자 (단일 GPU).\n"
                     "물 흐름에 점 입자 표시 → 흐름 시각화 강화.\n"
                     "2-way coupling은 FORCE_FIELD 추가 필요 (현재 비활성)."},
        ],
    },
    {
        "title": "Graphics (compile-time)",
        "fields": [
            {"key": "graphics_u_max", "label": "viz u_max", "step": 0.01, "type": "number",
             "desc": "속도 컬러맵 최대값 (lattice 단위). 색상 스케일 조절."},
            {"key": "graphics_rho_delta", "label": "rho coloring range", "step": 0.0001, "type": "number",
             "desc": "밀도 컬러맵 범위. ±delta 만큼 펼침."},
            {"key": "graphics_raytracing_transmittance", "label": "raytracing transmittance", "step": 0.05, "type": "number",
             "desc": "물 통과 빛 비율 (0~1). 0.25 = 1/4 통과. 작을수록 진한 물."},
            {"key": "graphics_raytracing_color", "label": "water color (hex)", "type": "text",
             "desc": "물 흡수 색상. 0x005F7F = 청록. 0xFF0000 = 빨강."},
            {"key": "graphics_frame_width", "label": "frame width", "step": 32, "type": "number",
             "desc": "PNG 가로 픽셀. 1920이 일반."},
            {"key": "graphics_frame_height", "label": "frame height", "step": 32, "type": "number",
             "desc": "PNG 세로 픽셀. 1080이 일반."},
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
    {"path": "scripts/dashboard.py", "role": "이 서버 (Flask). 포트 8080."},
    {"path": "scripts/fx3d_run.py", "role": "통합 runner. CLI + 라이브러리 함수 run_experiment(). --interactive 플래그 = GUI 변종 사용."},
    {"path": "scripts/build_fluidx3d.py", "role": "build.json 읽어 defines.hpp 패치 → msbuild 2회 → PNG + Interactive 두 binary 생성."},
    {"path": "scripts/fx3d_postprocess.py", "role": "VTK → result.json (in_pos, in_neg, score 계산). case.json의 fluid_threshold 사용."},
    {"path": "scripts/extract_targets.py", "role": "Rhino MCP → runs/_real_targets.json + _real_collider.stl."},
    {"path": "scripts/thicken_collider.py", "role": "open mesh → closed manifold. 인자 = 두께(m)."},
    {"path": "scripts/fx3d_visualize_in_rhino.py", "role": "iter_real STL을 Rhino 레이어에 푸시."},
    {"path": "scripts/rhino_mcp.py", "role": "Rhino MCP socket 호출 헬퍼."},
    {"path": "scripts/pymoo_run.py", "role": "pymoo NSGA-II 멀티오브젝티브 최적화 루프 (splash_frac, -dist_from_nozzle). 모듈별 sequential staging + 인터랙티브 Pareto 픽."},
    {"path": "scripts/pymoo_gen_module.py", "role": "parametric STL 생성 (추후 신규 design용)."},
    {"path": "external/FluidX3D/src/setup.cpp", "role": "FluidX3D 시뮬 로직. case.txt를 cwd에서 읽음. 수정시 Build 탭 → Rebuild."},
    {"path": "external/FluidX3D/src/defines.hpp", "role": "compile-time 매크로 baseline. build_fluidx3d.py가 build.json대로 임시 패치 후 복구."},
    {"path": "external/FluidX3D/bin/FluidX3D.exe", "role": "PNG 모드 binary (백그라운드, frames + VTK 저장)."},
    {"path": "external/FluidX3D/bin/FluidX3D_interactive.exe", "role": "Interactive 모드 binary (실시간 GUI 윈도우, P/WASD 조작, DB 안 남김)."},
]


# ---------- API routes ----------

@app.route("/")
def index():
    if not HTML_PATH.exists():
        return Response(f"dashboard.html not found at {HTML_PATH}", status=500, mimetype="text/plain")
    return Response(HTML_PATH.read_text(encoding="utf-8"), mimetype="text/html")


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
        log_path = MODULE_ROOT / "runs" / "_dashboard_build.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT))
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True, "cmd": " ".join(cmd)})


@app.route("/api/build_status")
def api_build_status():
    log_path = MODULE_ROOT / "runs" / "_dashboard_build.log"
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
        log_path = MODULE_ROOT / "runs" / f"_dashboard_run_{test_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                   cwd=str(REPO_ROOT))
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
        log_path = MODULE_ROOT / "runs" / "_dashboard_thicken.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return jsonify({"ok": True, "thickness_m": thickness, "cmd": " ".join(cmd)})


@app.route("/api/run_status/<test_id>")
def api_run_status(test_id):
    log_path = MODULE_ROOT / "runs" / f"_dashboard_run_{test_id}.log"
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


# HTML markup lives in dashboard.html at repo root; served by index() above.



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
