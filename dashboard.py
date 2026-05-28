"""LFTH_CFD main dashboard server.

Run:
    python dashboard.py    # from repo root
    -> opens browser at http://localhost:8080

Main UI:
    Files      project file structure + roles

Legacy CFD APIs are kept for now so the CFD setting UI can be split into its
own worktree-specific app without losing the existing backend behavior.

API:
    GET  /api/config              -> env_fx3d/config/case.json
    POST /api/config              -> write env_fx3d/config/case.json (validated)
    GET  /api/runs                -> list of settings-log entries
    POST /api/run                 -> trigger env_fx3d/scripts/fx3d_run.py (background subprocess)
    POST /api/thicken             -> trigger env_fx3d/scripts/thicken_collider.py with given thickness
    GET  /thumb/<idx>             -> last PNG frame of run #idx
    GET  /api/structure           -> static file-structure metadata
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_file, Response, abort

REPO_ROOT = Path(__file__).resolve().parent
ENV_FX3D = REPO_ROOT / "env_fx3d"

CONFIG_PATH = ENV_FX3D / "config" / "case.json"
BUILD_CONFIG_PATH = ENV_FX3D / "config" / "build.json"
SETTINGS_LOG = ENV_FX3D / "_settings_log.jsonl"
SCRIPTS = ENV_FX3D / "scripts"
HTML_PATH = REPO_ROOT / "dashboard.html"
DOCS_MAIN = Path(r"C:\Users\user\Documents\LFTH_CFD v2.0")

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
            {"key": "nozzle_refill_dt_s", "label": "source refill", "unit": "s", "step": 0.005, "type": "number",
             "desc": "노즐 source cell을 다시 TYPE_F|TYPE_E로 찍는 간격. dt_out보다 짧아야 수도꼭지처럼 연속 유입됨."},
            {"key": "nozzle_refill_col_h", "label": "source throat", "unit": "cells", "step": 1, "type": "number",
             "desc": "리필 때 계속 채우는 노즐 아래 짧은 목 길이. seed_col_h 전체를 채우면 물기둥이 고정되어 배수가 막힘."},
            {"key": "nozzle_emit_col_h", "label": "emit slug", "unit": "cells", "step": 1, "type": "number",
             "desc": "고정 throat 아래로 반복 생성하는 짧은 물줄기 길이. seed_col_h보다 작게 유지해야 전체 물기둥 고정이 되지 않음."},
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
             "desc": "legacy 유량 fallback. nozzle_velocity_mps가 0일 때만 vz = Q/A 계산에 사용."},
            {"key": "nozzle_mode", "label": "nozzle mode", "unit": "", "type": "select", "options": ["downstream_edge", "centroid", "index", "stride", "all"],
             "desc": "Rhino nozzle 샘플 처리 방식. downstream_edge=다음 모듈 방향 보간, centroid=중앙, index=특정 샘플, all=모든 샘플."},
            {"key": "nozzle_downstream_blend", "label": "edge blend", "unit": "", "step": 0.05, "type": "number",
             "desc": "downstream_edge 위치 보간. 0=노즐 중앙, 1=다음 모듈 방향 림. 중앙 고임과 옆으로 빗나감을 조절."},
            {"key": "nozzle_index", "label": "nozzle index", "unit": "", "step": 1, "type": "number",
             "desc": "nozzle_mode=index일 때 사용할 Rhino nozzle 샘플 번호."},
            {"key": "nozzle_max_points", "label": "stride points", "unit": "pts", "step": 1, "type": "number",
             "desc": "nozzle_mode=stride일 때 사용할 최대 source 샘플 수."},
            {"key": "nozzle_velocity_mps", "label": "inlet velocity", "unit": "m/s", "step": 0.25, "type": "number",
             "desc": "직접 지정하는 하향 유입 속도. Rhino nozzle layer는 독립 노즐 230개가 아니라 sampled inlet surface라 이 값이 기본 제어값."},
            {"key": "nozzle_velocity_floor_mps", "label": "velocity floor", "unit": "m/s", "step": 0.25, "type": "number",
             "desc": "nozzle_LPM에서 속도를 계산할 때 적용하는 최소 하향 속도."},
            {"key": "nozzle_horizontal_mps", "label": "horizontal speed", "unit": "m/s", "step": 0.25, "type": "number",
             "desc": "다음 모듈 방향의 수평 유입 속도. 0이면 완전 수직 낙하, 값이 있으면 첫 보울에서 빠져나가는 운동량을 줌."},
            {"key": "nozzle_horizontal_mode", "label": "horizontal mode", "unit": "", "type": "select", "options": ["toward_next_module", "none", "vector"],
             "desc": "수평 속도 방향. toward_next_module=상단 모듈에서 다음 모듈 중심 방향."},
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
            {"key": "floor_hit_band_m", "label": "floor hit band", "unit": "m", "step": 0.05, "type": "number",
             "desc": "[POSTPROCESS ONLY] reached_floor 진단에 쓰는 바닥 위 판정 밴드."},
            {"key": "fluid_threshold", "label": "phi cutoff", "unit": "", "step": 0.05, "type": "number",
             "desc": "[POSTPROCESS ONLY] sim 결과 변경 없음.\n"
                     "fx3d_postprocess.py에서 어느 phi 값부터 '물 셀'로 셀지 (0~1)."},
            {"key": "domain_pad_m", "label": "domain padding", "unit": "m", "step": 0.5, "type": "number",
             "desc": "[POSTPROCESS ONLY] sim 결과 변경 없음 (setup.cpp가 안 읽음).\n"
                     "fx3d_run.py가 domain_bbox_m 계산 시 sculpture 주변 padding으로만 사용."},
            {"key": "collider_stl_path", "label": "collider STL", "unit": "", "type": "text",
             "desc": "Optional STL override for fx3d_run.py. Relative paths resolve from repo root."},
            {"key": "module_bboxes_path", "label": "module bboxes", "unit": "", "type": "text",
             "desc": "Optional module bbox JSON matching collider_stl_path, used for cascade scoring."},
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
            {"key": "collision", "label": "collision operator", "type": "select", "options": ["SRT"],
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


_LEGACY_FILE_STRUCTURE = [
    {"path": "dashboard.py", "role": "이 서버 (Flask). 포트 8080. repo root."},
    {"path": "dashboard.html", "role": "대시보드 UI markup. dashboard.py가 GET /로 serve."},
    {"path": "CLAUDE.md", "role": "프로젝트 가이드 (디렉토리 맵 + 진입점 + 학습 인덱스)."},
    {"path": "env_fx3d/config/case.json", "role": "캐노니컬 runtime 파라미터. Settings 탭에서 편집. fx3d_run.py가 매 실행마다 iter_dir/case.txt로 복사 (재빌드 불필요)."},
    {"path": "env_fx3d/config/build.json", "role": "캐노니컬 compile-time 파라미터 (precision/velocity_set/SUBGRID/graphics 상수). Build 탭에서 편집 + Rebuild 버튼."},
    {"path": "env_fx3d/_settings_log.jsonl", "role": "append-only DB. 매 실험 한 줄. History 탭이 이 파일을 읽음. issue/notes 필드 post-hoc 채움."},
    {"path": "env_fx3d/runs/_real_targets.json", "role": "Rhino에서 추출한 positive/negative bbox + 230 nozzle 좌표. extract_targets.py로 생성."},
    {"path": "env_fx3d/runs/_real_collider.stl", "role": "Rhino env::collider 원본 STL (open mesh)."},
    {"path": "env_fx3d/runs/_real_collider_thickened.stl", "role": "thicken_collider.py가 만든 closed manifold. fx3d_run.py가 우선 사용."},
    {"path": "env_fx3d/runs/iter_*/", "role": "dashboard 실험마다 한 폴더 (case.txt + nozzles.txt + sculpture.stl + result.json + fx3d_out/{frames,vtk}/)."},
    {"path": "env_fx3d/scripts/fx3d_run.py", "role": "통합 runner. CLI + 라이브러리 함수 run_experiment(runs_root=...). --interactive 플래그 = GUI 변종 사용."},
    {"path": "env_fx3d/scripts/build_fluidx3d.py", "role": "build.json 읽어 defines.hpp 패치 → msbuild 2회 → PNG + Interactive 두 binary 생성."},
    {"path": "env_fx3d/scripts/fx3d_postprocess.py", "role": "VTK → result.json (in_pos, in_neg, score 계산). case.json의 fluid_threshold 사용."},
    {"path": "env_fx3d/scripts/thicken_collider.py", "role": "open mesh → closed manifold. 인자 = 두께(m)."},
    {"path": "env_fx3d/scripts/fx3d_visualize_in_rhino.py", "role": "iter_real STL을 Rhino 레이어에 푸시."},
    {"path": "env_fx3d/scripts/rhino_mcp.py", "role": "Rhino MCP socket 호출 헬퍼."},
    {"path": "env_fx3d/scripts/rhino_export/extract_targets.py", "role": "Rhino MCP → runs/_real_targets.json + _real_collider.stl."},
    {"path": "env_fx3d/external/FluidX3D/src/setup.cpp", "role": "FluidX3D 시뮬 로직. case.txt를 cwd에서 읽음. 수정시 Build 탭 → Rebuild."},
    {"path": "env_fx3d/external/FluidX3D/src/defines.hpp", "role": "compile-time 매크로 baseline. build_fluidx3d.py가 build.json대로 임시 패치 후 복구."},
    {"path": "env_fx3d/external/FluidX3D/bin/FluidX3D.exe", "role": "PNG 모드 binary (백그라운드, frames + VTK 저장)."},
    {"path": "env_fx3d/external/FluidX3D/bin/FluidX3D_interactive.exe", "role": "Interactive 모드 binary (실시간 GUI 윈도우, P/WASD 조작, DB 안 남김)."},
    {"path": "opt_pymoo/scripts/pymoo_run.py", "role": "pymoo NSGA-II 멀티오브젝티브 최적화 루프 (splash_frac, -dist_from_nozzle). 모듈별 sequential staging + 인터랙티브 Pareto 픽."},
    {"path": "opt_pymoo/scripts/pymoo_gen_module.py", "role": "parametric STL 생성 (gene → geometry, trimesh-based)."},
    {"path": "opt_pymoo/experiments/pymoo_state.json", "role": "GA 진행 snapshot (resumable). stage·gen·population."},
    {"path": "opt_pymoo/runs/iter_*/", "role": "GA 실험 산출물 (env_fx3d/runs와 분리)."},
    {"path": "opt_pymoo/_optimization_log.jsonl", "role": "append-only 개체별 DB. issue/notes 필드."},
]


FILE_STRUCTURE = [
    {"path": "dashboard.py", "role": "main dashboard Flask server. Port 8080. Files-only UI."},
    {"path": "dashboard.html", "role": "main dashboard markup. Shows project file structure only."},
    {"path": ".codex/config.toml", "role": "project-local Codex connector config."},
    {"path": "env_fx3d/config/case.json", "role": "CFD runtime settings. Treat as experiment data; edit from the CFD setting worktree."},
    {"path": "env_fx3d/config/build.json", "role": "CFD compile-time settings. Requires FluidX3D rebuild when changed."},
    {"path": "env_fx3d/_settings_log.jsonl", "role": "CFD experiment log written by setting/run workflows."},
    {"path": "env_fx3d/runs/_real_targets.json", "role": "Rhino-exported positive/negative targets and nozzle coordinates."},
    {"path": "env_fx3d/runs/_real_collider.stl", "role": "Rhino env::collider source STL."},
    {"path": "env_fx3d/runs/_real_collider_thickened.stl", "role": "Closed collider STL produced by thicken_collider.py."},
    {"path": "env_fx3d/runs/iter_*/", "role": "CFD run outputs: case snapshot, geometry, result JSON, frames, and VTK."},
    {"path": "env_fx3d/patches/fluidx3d_lfth_source.patch", "role": "FluidX3D source patch for the LFTH inlet/source behavior."},
    {"path": "env_fx3d/scripts/fx3d_run.py", "role": "CFD runner and experiment orchestration entrypoint."},
    {"path": "env_fx3d/scripts/build_fluidx3d.py", "role": "Applies build config and builds PNG/interactive FluidX3D binaries."},
    {"path": "env_fx3d/scripts/fx3d_postprocess.py", "role": "Postprocesses VTK output into scoring and diagnostics JSON."},
    {"path": "env_fx3d/scripts/fx3d_acceptance.py", "role": "CFD acceptance and source/cascade smoke scenarios."},
    {"path": "env_fx3d/scripts/thicken_collider.py", "role": "Converts open collider meshes into closed manifolds."},
    {"path": "env_fx3d/scripts/rhino_export/extract_targets.py", "role": "Exports targets and collider geometry from Rhino through MCP."},
    {"path": "env_fx3d/external/FluidX3D/src/setup.cpp", "role": "FluidX3D simulation setup logic. Rebuild after source changes."},
    {"path": "env_fx3d/external/FluidX3D/src/defines.hpp", "role": "FluidX3D compile-time macro baseline managed by build_fluidx3d.py."},
    {"path": "env_fx3d/external/FluidX3D/bin/FluidX3D.exe", "role": "Background PNG/VTK FluidX3D binary."},
    {"path": "env_fx3d/external/FluidX3D/bin/FluidX3D_interactive.exe", "role": "Interactive FluidX3D binary for GUI inspection."},
    {"path": "opt_structure/config/structure_case.json", "role": "Structure optimization settings and load assumptions."},
    {"path": "opt_structure/scripts/optimize_structure.py", "role": "Structure optimization runner."},
    {"path": "opt_structure/scripts/fea.py", "role": "Structure FEA helpers and fallback analysis path."},
    {"path": "opt_structure/scripts/structure_dashboard.py", "role": "Structure setting dashboard server."},
    {"path": "opt_structure/structure_dashboard.html", "role": "Structure setting dashboard UI."},
    {"path": "opt_structure/tests/smoke_tests.py", "role": "Structure smoke tests."},
    {"path": "opt_pymoo/scripts/parametric_module.py", "role": "Parametric module geometry generator."},
    {"path": "opt_pymoo/scripts/parametric_dashboard.py", "role": "Module setting dashboard server."},
    {"path": "opt_pymoo/parametric_dashboard.html", "role": "Module setting dashboard UI."},
    {"path": "opt_pymoo/scripts/pymoo_run.py", "role": "pymoo optimization loop."},
    {"path": "opt_pymoo/experiments/pymoo_state.json", "role": "Resumable optimization state snapshot."},
    {"path": "opt_pymoo/runs/iter_*/", "role": "Module/optimization run outputs."},
    {"path": "opt_pymoo/_optimization_log.jsonl", "role": "Optimization candidate log."},
]


# ---------- git dashboard helpers ----------

SAFE_CODEX_BRANCH = re.compile(r"^codex/[A-Za-z0-9._/-]+$")
_git_jobs: dict[str, dict] = {}
_git_jobs_lock = threading.Lock()
_git_action_lock = threading.Lock()


def _creationflags() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_cmd(args: list[str], cwd: Path | str = REPO_ROOT,
            timeout: int = 30) -> dict:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_creationflags(),
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "cmd": args,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": f"timed out after {timeout}s",
            "cmd": args,
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": str(exc),
            "cmd": args,
        }


def cmd_text(args: list[str], cwd: Path | str = REPO_ROOT,
             timeout: int = 30) -> str:
    result = run_cmd(args, cwd, timeout)
    if not result["ok"]:
        raise RuntimeError((result["stderr"] or result["stdout"]).strip())
    return result["stdout"].strip()


def run_logged(log, args: list[str], cwd: Path | str = REPO_ROOT,
               timeout: int = 120) -> dict:
    log("$ " + " ".join(args))
    result = run_cmd(args, cwd, timeout)
    out = (result["stdout"] + result["stderr"]).strip()
    if out:
        log(out[-4000:])
    if not result["ok"]:
        raise RuntimeError(f"command failed ({result['returncode']}): {' '.join(args)}")
    return result


def is_safe_codex_branch(branch: str) -> bool:
    if not SAFE_CODEX_BRANCH.fullmatch(branch or ""):
        return False
    return not any(token in branch for token in ("..", "@{", "\\", "//")) and not branch.endswith(("/", "."))


def check_summary(rollup) -> dict:
    if not rollup:
        return {"state": "none", "total": 0, "blocking": False}
    failing = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
    pending = False
    failed = False
    total = 0
    for item in rollup:
        total += 1
        conclusion = str(item.get("conclusion") or "").upper()
        status = str(item.get("status") or item.get("state") or "").upper()
        if conclusion in failing:
            failed = True
        elif status and status not in {"COMPLETED", "SUCCESS"}:
            pending = True
        elif not conclusion and status not in {"SUCCESS", "COMPLETED"}:
            pending = True
    if failed:
        return {"state": "failing", "total": total, "blocking": True}
    if pending:
        return {"state": "pending", "total": total, "blocking": True}
    return {"state": "passing", "total": total, "blocking": False}


def parse_json_cmd(args: list[str], cwd: Path | str = REPO_ROOT,
                   timeout: int = 60):
    result = run_cmd(args, cwd, timeout)
    if not result["ok"]:
        return None, (result["stderr"] or result["stdout"]).strip()
    try:
        return json.loads(result["stdout"] or "null"), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def parse_worktrees() -> dict[str, dict]:
    result = run_cmd(["git", "worktree", "list", "--porcelain"], REPO_ROOT)
    if not result["ok"]:
        return {}
    out: dict[str, dict] = {}
    current: dict[str, str] = {}
    for line in result["stdout"].splitlines() + [""]:
        if not line.strip():
            branch = current.get("branch", "")
            if branch.startswith("refs/heads/"):
                out[branch.replace("refs/heads/", "", 1)] = {
                    "path": current.get("worktree"),
                    "head": current.get("HEAD"),
                }
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return out


def branch_refs(prefix: str) -> dict[str, dict]:
    fmt = "%(refname:short)|%(objectname:short)|%(committerdate:iso8601)|%(subject)"
    result = run_cmd(["git", "for-each-ref", prefix, f"--format={fmt}"], REPO_ROOT)
    refs: dict[str, dict] = {}
    if not result["ok"]:
        return refs
    for line in result["stdout"].splitlines():
        parts = (line.split("|", 3) + ["", "", "", ""])[:4]
        name, sha, date, subject = parts
        branch = name.replace("origin/", "", 1) if name.startswith("origin/codex/") else name
        if branch.startswith("codex/"):
            refs[branch] = {"ref": name, "sha": sha, "date": date, "subject": subject}
    return refs


def ahead_behind(ref: str) -> tuple[int | None, int | None]:
    result = run_cmd(["git", "rev-list", "--left-right", "--count",
                      f"origin/main...{ref}"], REPO_ROOT)
    if not result["ok"]:
        return None, None
    parts = result["stdout"].split()
    if len(parts) != 2:
        return None, None
    return int(parts[0]), int(parts[1])


def worktree_clean(path: str | None) -> tuple[bool | None, str]:
    if not path:
        return None, "no worktree"
    result = run_cmd(["git", "status", "--porcelain"], Path(path))
    if not result["ok"]:
        return None, (result["stderr"] or result["stdout"]).strip()
    return result["stdout"].strip() == "", result["stdout"].strip()


def repo_full_name() -> str:
    url = cmd_text(["git", "remote", "get-url", "origin"], REPO_ROOT)
    if url.startswith("https://github.com/"):
        name = url.replace("https://github.com/", "", 1)
    elif url.startswith("git@github.com:"):
        name = url.replace("git@github.com:", "", 1)
    else:
        raise RuntimeError(f"unsupported origin URL: {url}")
    if name.endswith(".git"):
        name = name[:-4]
    if "/" not in name:
        raise RuntimeError(f"cannot parse origin URL: {url}")
    return name


def collect_prs() -> tuple[list[dict], str | None]:
    fields = (
        "number,title,headRefName,baseRefName,isDraft,mergeable,"
        "statusCheckRollup,url,updatedAt"
    )
    data, error = parse_json_cmd(
        ["gh", "pr", "list", "--state", "open", "--base", "main",
         "--json", fields],
        REPO_ROOT,
        60,
    )
    if error:
        return [], error
    prs = []
    for pr in data or []:
        if not str(pr.get("headRefName", "")).startswith("codex/"):
            continue
        checks = check_summary(pr.get("statusCheckRollup"))
        pr["checks"] = checks
        pr["actionable"] = (
            not pr.get("isDraft")
            and pr.get("baseRefName") == "main"
            and is_safe_codex_branch(pr.get("headRefName", ""))
            and pr.get("mergeable") == "MERGEABLE"
            and not checks["blocking"]
        )
        prs.append(pr)
    return prs, None


def docs_main_status() -> dict:
    status = {"path": str(DOCS_MAIN), "exists": DOCS_MAIN.exists()}
    if not DOCS_MAIN.exists():
        status.update({"clean": None, "error": "Documents main path missing"})
        return status
    branch = run_cmd(["git", "branch", "--show-current"], DOCS_MAIN)
    head = run_cmd(["git", "rev-parse", "--short", "HEAD"], DOCS_MAIN)
    porcelain = run_cmd(["git", "status", "--porcelain"], DOCS_MAIN)
    status.update({
        "branch": branch["stdout"].strip() if branch["ok"] else None,
        "head": head["stdout"].strip() if head["ok"] else None,
        "clean": porcelain["ok"] and porcelain["stdout"].strip() == "",
        "dirty": porcelain["stdout"].strip() if porcelain["ok"] else "",
        "error": None if branch["ok"] and head["ok"] and porcelain["ok"] else (
            branch["stderr"] or head["stderr"] or porcelain["stderr"]
        ).strip(),
    })
    return status


def collect_git_status() -> dict:
    prs, gh_error = collect_prs()
    pr_by_branch = {pr["headRefName"]: pr for pr in prs}
    local = branch_refs("refs/heads/codex")
    remote = branch_refs("refs/remotes/origin/codex")
    worktrees = parse_worktrees()
    branches = []
    for branch in sorted(set(local) | set(remote) | set(pr_by_branch)):
        ref = branch if branch in local else f"origin/{branch}"
        behind, ahead = ahead_behind(ref)
        wt = worktrees.get(branch)
        clean, dirty = worktree_clean(wt.get("path") if wt else None)
        sync_blocked = ""
        if not wt:
            sync_blocked = "no local worktree"
        elif clean is not True:
            sync_blocked = "dirty worktree"
        elif ahead is None:
            sync_blocked = "cannot compare with origin/main"
        elif ahead > 0:
            sync_blocked = "branch has unique commits"
        elif behind == 0:
            sync_blocked = "already up to date"
        branches.append({
            "name": branch,
            "local": branch in local,
            "remote": branch in remote,
            "sha": (local.get(branch) or remote.get(branch) or {}).get("sha"),
            "subject": (local.get(branch) or remote.get(branch) or {}).get("subject"),
            "worktree": wt.get("path") if wt else None,
            "clean": clean,
            "dirty": dirty,
            "behind_main": behind,
            "ahead_main": ahead,
            "pr_number": pr_by_branch.get(branch, {}).get("number"),
            "sync_allowed": bool(wt and clean is True and ahead == 0 and (behind or 0) > 0),
            "sync_blocked": sync_blocked,
        })
    origin_main = run_cmd(["git", "rev-parse", "--short", "origin/main"], REPO_ROOT)
    head = run_cmd(["git", "rev-parse", "--short", "HEAD"], REPO_ROOT)
    with _git_jobs_lock:
        jobs = sorted(_git_jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)
        last_job = {k: v for k, v in jobs[0].items() if k != "log"} if jobs else None
    return {
        "ok": True,
        "repo": {
            "path": str(REPO_ROOT),
            "head": head["stdout"].strip() if head["ok"] else None,
            "origin_main": origin_main["stdout"].strip() if origin_main["ok"] else None,
        },
        "documents_main": docs_main_status(),
        "prs": prs,
        "pr_count": len(prs),
        "gh_error": gh_error,
        "branches": branches,
        "last_job": last_job,
    }


def append_job(job_id: str, message: str) -> None:
    with _git_jobs_lock:
        job = _git_jobs.get(job_id)
        if not job:
            return
        ts = time.strftime("%H:%M:%S")
        job["log"] += f"[{ts}] {message}\n"
        job["updated_at"] = int(time.time())


def start_git_job(kind: str, target: str, runner) -> dict:
    job_id = uuid.uuid4().hex[:12]
    with _git_jobs_lock:
        _git_jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "target": target,
            "status": "running",
            "ok": None,
            "log": "",
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }

    def _thread():
        acquired = _git_action_lock.acquire(blocking=False)
        if not acquired:
            append_job(job_id, "blocked reason: another Git job is already running")
            with _git_jobs_lock:
                _git_jobs[job_id]["status"] = "failed"
                _git_jobs[job_id]["ok"] = False
            return
        try:
            runner(lambda msg: append_job(job_id, msg))
            with _git_jobs_lock:
                _git_jobs[job_id]["status"] = "done"
                _git_jobs[job_id]["ok"] = True
        except Exception as exc:
            append_job(job_id, f"blocked reason: {exc}")
            with _git_jobs_lock:
                _git_jobs[job_id]["status"] = "failed"
                _git_jobs[job_id]["ok"] = False
        finally:
            _git_action_lock.release()

    threading.Thread(target=_thread, daemon=True).start()
    return {"ok": True, "job_id": job_id}


def ff_documents_main(log) -> None:
    log(f"Checking Documents main at {DOCS_MAIN}")
    if not DOCS_MAIN.exists():
        raise RuntimeError("Documents main path missing")
    branch = cmd_text(["git", "branch", "--show-current"], DOCS_MAIN)
    if branch != "main":
        raise RuntimeError(f"Documents worktree is on {branch!r}, not main")
    dirty = cmd_text(["git", "status", "--porcelain"], DOCS_MAIN)
    if dirty:
        raise RuntimeError("Documents main has uncommitted changes")
    run_logged(log, ["git", "fetch", "origin", "main"], DOCS_MAIN, 120)
    run_logged(log, ["git", "merge", "--ff-only", "origin/main"], DOCS_MAIN, 120)
    log("Documents main fast-forward complete.")


def gh_pr_info(number: int) -> dict:
    fields = (
        "number,title,headRefName,baseRefName,isDraft,mergeable,"
        "statusCheckRollup,url,updatedAt,headRefOid"
    )
    data, error = parse_json_cmd(
        ["gh", "pr", "view", str(number), "--json", fields],
        REPO_ROOT,
        60,
    )
    if error:
        raise RuntimeError(error)
    data["checks"] = check_summary(data.get("statusCheckRollup"))
    return data


def review_merge_job(number: int, log) -> None:
    pr = gh_pr_info(number)
    branch = pr.get("headRefName") or ""
    log(f"Reviewing PR #{number}: {pr.get('title')}")
    if pr.get("baseRefName") != "main":
        raise RuntimeError("PR base is not main")
    if not is_safe_codex_branch(branch):
        raise RuntimeError("PR head branch is not an allowed codex/* branch")
    if pr.get("isDraft"):
        raise RuntimeError("PR is draft")
    if pr.get("mergeable") != "MERGEABLE":
        raise RuntimeError(f"PR is not mergeable: {pr.get('mergeable')}")
    if pr["checks"]["blocking"]:
        raise RuntimeError(f"PR checks are {pr['checks']['state']}")
    head_sha = pr.get("headRefOid")
    if not head_sha:
        raise RuntimeError("PR head SHA missing")
    run_logged(log, ["git", "fetch", "origin", "main",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
               REPO_ROOT, 120)
    tmp_root = Path(tempfile.mkdtemp(prefix=f"lfth_pr_{number}_"))
    tmp_path = tmp_root / "checkout"
    try:
        run_logged(log, ["git", "worktree", "add", "--detach", str(tmp_path), head_sha],
                   REPO_ROOT, 120)
        run_logged(log, ["git", "diff", "--check", "origin/main...HEAD"], tmp_path, 120)
        run_logged(log, [sys.executable, "-m", "compileall", "dashboard.py",
                         "env_fx3d", "opt_pymoo", "opt_structure"], tmp_path, 180)
        run_logged(log, [sys.executable, "-m", "unittest",
                         "opt_structure.tests.smoke_tests"], tmp_path, 180)
        full_name = repo_full_name()
        run_logged(
            log,
            [
                "gh", "api", "-X", "PUT",
                f"repos/{full_name}/pulls/{number}/merge",
                "-f", "merge_method=merge",
                "-f", f"sha={head_sha}",
                "-f", f"commit_title=Merge PR #{number}: {pr.get('title')}",
                "-f", "commit_message=Validated by dashboard Git gate.",
            ],
            REPO_ROOT,
            120,
        )
        log("PR merge complete.")
    finally:
        run_cmd(["git", "worktree", "remove", "--force", str(tmp_path)], REPO_ROOT, 120)
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)
    try:
        ff_documents_main(log)
    except Exception as exc:
        log(f"Documents main not updated: {exc}")


def sync_branch_job(branch: str, log) -> None:
    if not is_safe_codex_branch(branch):
        raise RuntimeError("branch is not an allowed codex/* branch")
    worktrees = parse_worktrees()
    wt = worktrees.get(branch)
    if not wt or not wt.get("path"):
        raise RuntimeError("no local worktree for branch")
    path = Path(wt["path"])
    current = cmd_text(["git", "branch", "--show-current"], path)
    if current != branch:
        raise RuntimeError(f"worktree is on {current!r}, not {branch!r}")
    dirty = cmd_text(["git", "status", "--porcelain"], path)
    if dirty:
        raise RuntimeError("worktree has uncommitted changes")
    run_logged(log, ["git", "fetch", "origin", "main"], path, 120)
    counts = cmd_text(["git", "rev-list", "--left-right", "--count",
                       "origin/main...HEAD"], path)
    behind_s, ahead_s = counts.split()
    behind, ahead = int(behind_s), int(ahead_s)
    if ahead > 0:
        raise RuntimeError("branch has unique commits; open/merge its PR first")
    if behind == 0:
        log("Branch already matches origin/main.")
        return
    run_logged(log, ["git", "merge", "--ff-only", "origin/main"], path, 120)
    run_logged(log, ["git", "push", "origin", f"HEAD:refs/heads/{branch}"], path, 120)
    log(f"{branch} synced to origin/main.")


# ---------- API routes ----------

@app.route("/")
def index():
    if not HTML_PATH.exists():
        return Response(f"dashboard.html not found at {HTML_PATH}", status=500, mimetype="text/plain")
    return Response(HTML_PATH.read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/api/git/status", methods=["GET"])
def api_git_status():
    return jsonify(collect_git_status())


@app.route("/api/git/pr/<int:number>/review_merge", methods=["POST"])
def api_git_review_merge(number: int):
    return jsonify(start_git_job(
        "review_merge",
        f"PR #{number}",
        lambda log: review_merge_job(number, log),
    ))


@app.route("/api/git/main/ff", methods=["POST"])
def api_git_main_ff():
    return jsonify(start_git_job(
        "main_ff",
        "Documents main",
        ff_documents_main,
    ))


@app.route("/api/git/branch/sync", methods=["POST"])
def api_git_branch_sync():
    data = request.get_json(force=True) or {}
    branch = str(data.get("branch") or "")
    if not is_safe_codex_branch(branch):
        return jsonify({"ok": False, "error": "expected safe codex/* branch"}), 400
    return jsonify(start_git_job(
        "branch_sync",
        branch,
        lambda log: sync_branch_job(branch, log),
    ))


@app.route("/api/git/job/<job_id>", methods=["GET"])
def api_git_job(job_id: str):
    with _git_jobs_lock:
        job = _git_jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "job not found"}), 404
        out = dict(job)
    out["log_tail"] = out.get("log", "")[-8000:]
    out.pop("log", None)
    return jsonify({"ok": True, "job": out})


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
        log_path = ENV_FX3D / "runs" / "_dashboard_build.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT))
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({"ok": True, "cmd": " ".join(cmd)})


@app.route("/api/build_status")
def api_build_status():
    log_path = ENV_FX3D / "runs" / "_dashboard_build.log"
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
        log_path = ENV_FX3D / "runs" / f"_dashboard_run_{test_id}.log"
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
        log_path = ENV_FX3D / "runs" / "_dashboard_thicken.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return jsonify({"ok": True, "thickness_m": thickness, "cmd": " ".join(cmd)})


@app.route("/api/run_status/<test_id>")
def api_run_status(test_id):
    log_path = ENV_FX3D / "runs" / f"_dashboard_run_{test_id}.log"
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
