# LFTH_CFD v2.0 — Project Guide

서부터미널 광장 조형물 (4-dish cascade → 연못) 의 물 splash 시뮬레이션 + GA 형상 최적화. 엔진은 **FluidX3D** (Lattice Boltzmann + Volume-of-Fluid on GPU). DSPH에서 2026-05-27 마이그레이션.

## 디렉토리 맵

```
LFTH_CFD v2.0/
├── CLAUDE.md                            이 문서 (프로젝트 가이드)
├── README.md                            한 줄 요약 + 모듈 포인터
├── dashboard.py                         Flask 서버 진입점 (포트 8080)
├── dashboard.html                       대시보드 UI (dashboard.py가 serve)
│
├── env_fx3d/                            FluidX3D 시뮬레이션 모듈
│   ├── README.md                        FluidX3D 셋업 + 학습된 함정
│   ├── config/
│   │   ├── case.json                    런타임 파라미터 (캐노니컬, _doc 블록)
│   │   └── build.json                   컴파일타임 파라미터 (precision 등)
│   ├── runs/                            대시보드 실험 출력 (gitignore)
│   │   ├── _real_*.{stl,json}           Rhino에서 추출된 collider/타겟
│   │   └── iter_*/                      실험마다 1 폴더
│   ├── _settings_log.jsonl              실험 append-only DB
│   ├── external/FluidX3D/               vendor clone (gitignore, ~100MB)
│   └── scripts/
│       ├── fx3d_run.py                  통합 runner (CLI + library)
│       ├── fx3d_postprocess.py          VTK → result.json
│       ├── fx3d_visualize_in_rhino.py   결과 Rhino push
│       ├── build_fluidx3d.py            msbuild wrapper
│       ├── thicken_collider.py          open mesh → closed manifold
│       ├── rhino_mcp.py                 Rhino MCP socket helper
│       └── rhino_export/extract_targets.py   Rhino → JSON+STL
│
├── opt_pymoo/                           pymoo NSGA-II 최적화 모듈
│   ├── README.md                        GA 셋업 + 학습된 함정
│   ├── experiments/
│   │   ├── pymoo_state.json             진행 상태 (resumable)
│   │   └── test_NN.json                 개체별 평가 결과
│   ├── runs/                            GA 실험 출력 (gitignore, env_fx3d/runs와 분리)
│   ├── _optimization_log.jsonl          개체별 append-only DB
│   └── scripts/
│       ├── pymoo_run.py                 NSGA-II 진입점
│       └── pymoo_gen_module.py          parametric STL 생성 (gene→geometry)
│
└── _error/                              에러 스크린샷
```

## 진입점

```powershell
# 대시보드 (실험·세팅 탭, 포트 8080, 브라우저 자동 오픈)
python dashboard.py

# 단일 시뮬 (CLI)
python env_fx3d/scripts/fx3d_run.py --test-id myrun

# pymoo NSGA-II 최적화
python opt_pymoo/scripts/pymoo_run.py --stage 0 --pop 16 --n_gen 10

# FluidX3D 재빌드 (setup.cpp/defines.hpp 변경 후)
python env_fx3d/scripts/build_fluidx3d.py

# Rhino에서 collider/positive/negative/nozzle 추출
python env_fx3d/scripts/rhino_export/extract_targets.py
python env_fx3d/scripts/thicken_collider.py 0.20
```

## Path 패턴

각 스크립트는 동일한 3-anchor 패턴 사용:
```python
SCRIPT_DIR  = Path(__file__).resolve().parent       # env_fx3d/scripts/ 등
MODULE_ROOT = SCRIPT_DIR.parent                     # env_fx3d/ 또는 opt_pymoo/
REPO_ROOT   = MODULE_ROOT.parent                    # repo 루트
```

cross-package import (opt_pymoo → env_fx3d):
```python
sys.path.insert(0, str(REPO_ROOT / "env_fx3d" / "scripts"))
from fx3d_run import run_experiment
```

## 학습 인덱스

상세 학습은 모듈 README:
- **시뮬 함정**: `env_fx3d/README.md` (TYPE_F|TYPE_E inflow, surface_2 demotion, STL binary, viscosity envelope, lbm_u_ref 안정성)
- **최적화 함정**: `opt_pymoo/README.md` (DEAP→pymoo 사유, NSGA-II 다목적, stage staging 이유)

채팅에서 반복되는 핵심:
- **변수 변경시 단일 진리원**: `env_fx3d/config/case.json` 의 `_doc` 블록 (런타임 파라미터) 와 `env_fx3d/config/build.json` (컴파일 파라미터). 새 키 추가 시 두 곳을 모두 갱신.
- **빌드 vs 런타임**: `setup.cpp` / `defines.hpp` 변경 = msbuild 필요 (~30s). `case.json` 변경 = 그냥 fx3d_run.py 재실행.
- **runs/ 분리**: dashboard 실험 → `env_fx3d/runs/`. pymoo GA → `opt_pymoo/runs/`. 섞이지 않음 (`run_experiment(runs_root=...)` 파라미터).
- **`_*_log.jsonl` 스키마**: 행마다 `issue` / `notes` 필드. 같은 실수 반복 안 하려고 post-hoc 채움.

## 성능 (RTX 5070 Ti)

| dp | cells | 12s sim wall |
|---|---|---|
| 0.08 m | 24M | ~36s |
| 0.05 m | 60M | ~5 min |
| 0.04 m | 192M | ~10 min (VRAM 13GB) |

## 보조 도구

- `rhino_mcp.py` Rhino MCP socket 호출 (Rhino 8 + MCP 서버 가동 시)
- `_error/` 디버깅 시 스크린샷 저장 위치
