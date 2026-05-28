# env_fx3d — FluidX3D 시뮬레이션 모듈

LBM+VOF (Lattice Boltzmann + Volume of Fluid) GPU 시뮬레이션을 통한 조형물 splash CFD.

## 1. 셋업

### FluidX3D 빌드

`external/FluidX3D/` 는 vendor clone (자체 .git 보유, gitignore). 빌드 산출물:
- `bin/FluidX3D.exe` — PNG mode (headless, frames + VTK 저장)
- `bin/FluidX3D_interactive.exe` — Interactive mode (GUI, P/WASD)

빌드 트리거:
```powershell
python env_fx3d/scripts/build_fluidx3d.py
```

이는 `config/build.json` 을 읽어 `external/FluidX3D/src/defines.hpp` 를 임시 패치 → msbuild 2회 (PNG + Interactive) → defines.hpp 복원. ~30s on VS 2026 v145.

### case.json 의미

`config/case.json` 상단의 `_doc` 블록이 캐노니컬 정의:
- 키마다 `"단위 | 효과 | 범위 | 기본값"`
- 새 키 추가 시 `_doc` 도 같이.
- `_` 접두어 키는 fx3d_run.py가 strip하므로 setup.cpp가 못 봄 (런타임 영향 없음).

## 2. 실행 사이클

```powershell
# A) Rhino → 추출 (디자인 변경 시)
python env_fx3d/scripts/rhino_export/extract_targets.py
python env_fx3d/scripts/thicken_collider.py 0.20

# B) 단일 sim
python env_fx3d/scripts/fx3d_run.py --test-id myrun
# → env_fx3d/runs/iter_myrun/ 생성
# → env_fx3d/_settings_log.jsonl 한 줄 append

# C) 대시보드
python env_fx3d/scripts/dashboard.py
# → http://localhost:8080
# → Settings 탭에서 슬라이더 + Save+Run
```

## 3. 학습된 함정 (chat 1차 자료 + kernel.cpp 검증)

### Inflow: TYPE_F | TYPE_E 가 캐노니컬 패턴

SURFACE LBM에서 sustained inflow를 만들려면:
- **TYPE_E** (kernel.cpp:1485): stream-collide가 매 step 고정 rho/u 적용 → 압력·속도 source
- **TYPE_F** (kernel.cpp:1411-1414): surface_3에서 phi=1.0 자동 유지 → 표면 fluid 보존
- 둘 다 비트 set 필요. TYPE_E만 set하면 kernel.cpp:1690 mass-exchange가 TYPE_E를 sink로 취급 → 물 사라짐.

### surface_2 demotion → host-side refill 필수

`surface_2` (kernel.cpp:1729-1739): 인접 IG (interface→gas) transition 발생 시 TYPE_F 비트를 strip → 셀이 TYPE_I|TYPE_E 로 demoted → phi=0 으로 drain.

대응: setup.cpp 메인 while-loop 에서 매 frame 간격으로 host-side refill — `top_cells` 와 `seed_cells` 를 TYPE_F|TYPE_E로 다시 stamp.

### STL은 BINARY 만

FluidX3D는 ASCII STL 못 읽음. trimesh `export(file_type='stl')` 는 BINARY 기본 → OK. ASCII export 사용 안 함.

### 점성 & 안정성 envelope

- `lbm_u_ref ≤ 0.1` 안정 envelope. 0.1 초과 시 lattice tau ≈ 0.5 근접 → SRT 발산.
- 실제 물 `viscosity_m2ps=1e-6` 적용 시 lattice ν ≈ 4e-7 → τ ≈ 0.5. **TRT collision 권장** (`config/build.json` collision=TRT).
- 디버깅용 hack 이었던 `1e-5` 는 안정화는 되지만 비현실적 흐름.

### 노즐 vz 자동 계산

`fx3d_run.py:nozzle_vz_from_lpm()`: `v = Q/A`, LBM 안정성 위해 1 m/s floor. 230 노즐 × 60 LPM → per-nozzle v≈0.1 m/s → floor 1 m/s → 단면 dp²

### 빌드 vs 런타임

- `case.json` 만 바뀜 → fx3d_run.py 재실행으로 충분 (case.txt만 다시 씀).
- `setup.cpp` / `defines.hpp` 바뀜 → **msbuild 재빌드 필요** (~30s).
- `build.json` 만 바뀜 → build_fluidx3d.py 재실행 → defines.hpp 패치 + msbuild.

## 4. 파일 가이드

| 파일 | 역할 |
|---|---|
| `scripts/fx3d_run.py` | 통합 runner. `run_experiment(test_id, ..., runs_root=None)` 라이브러리 API |
| `scripts/fx3d_postprocess.py` | VTK → result.json (in_pos/in_neg/splash/total) |
| `scripts/fx3d_visualize_in_rhino.py` | iter_*/sculpture.stl 을 Rhino 레이어에 push |
| `scripts/dashboard.py` | Flask 서버, `dashboard.html` (repo root) serve |
| `scripts/build_fluidx3d.py` | defines.hpp 패치 + msbuild |
| `scripts/thicken_collider.py` | open mesh → closed manifold (CLI: `... 0.20` = 두께 m) |
| `scripts/rhino_mcp.py` | Rhino MCP socket helper |
| `scripts/rhino_export/extract_targets.py` | Rhino layer → JSON+STL |

## 5. _settings_log.jsonl 스키마

매 실험 한 줄 (append-only). 키:
- `ts`, `test_id`, `engine` — 식별
- `dp_m`, `timemax_s`, `dt_out_s`, `side_walls`, `seed_col_h` — case 파라미터
- `surface_tension_Npm`, `viscosity_m2ps`, `density_kgpm3`, `gravity_mps2` — 물성
- `n_nozzles`, `nozzle_LPM`, `nozzle_vz_mps`, `total_Q_m3ps` — 노즐
- `thicken_thickness_m`, `collider_stl` — preprocess
- `score`, `in_positive`, `in_negative`, `in_column`, `splash`, `total`, `retention_rate` — 결과
- `wall_s`, `iter_dir`, `frames_dir` — 비용·경로
- **`issue`, `notes`** — post-hoc 주석 (실패 원인, 관찰 등). 같은 실수 반복 안 하기 위함.
