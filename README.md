# LFTH_CFD v2.0

서부터미널 광장 조형물 (4개 sphere-cap 모듈 cascade → 연못) 의 물 splash CFD 시뮬레이션 + GA 형상 최적화.

엔진: **FluidX3D** (Lattice Boltzmann + Volume-of-Fluid on GPU). 이전에는 DualSPHysics(SPH)를 썼으나 셋업 한계 + 처리량 문제로 2026-05-27 마이그레이션 (`reference_fluidx3d` 메모리 참조).

## 한 줄 셋업

```powershell
# 1. FluidX3D 빌드 (한 번만)
& 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\MSBuild\Current\Bin\MSBuild.exe' `
  'C:\Users\user\Downloads\FluidX3D\FluidX3D.vcxproj' /p:Configuration=Release /p:Platform=x64 /m

# 2. 단일 케이스 (test_22) 실행
python scripts/fx3d_experiment_runner.py experiments/_reference_test_22.json

# 3. Rhino 에 형상 + inflow marker push (Rhino MCP 서버 가동 중이어야 함)
python scripts/fx3d_visualize_in_rhino.py
```

## GA 형상 최적화

```powershell
# Smoke test (~2 min)
python scripts/ga_sequential.py --stages 0 --pop 3 --n_gen 2 --seed 100

# Full run (~100 min, 320 evals)
python scripts/ga_sequential.py --stages 0,1,2,3 --pop 8 --n_gen 10 --seed 42
```

상태는 `experiments/sequential_state.json`에 저장됨. 중단되면 동일 명령 재실행하면 완료된 stage는 skip.

## Pipeline

`scripts/fx3d_experiment_runner.run_experiment()` 한 번 호출:

1. **`module_geometry.build_modules_combined_stl`** — 8 유전자 × 4 모듈 → 결합 STL (binary, 미터 단위)
2. **case.txt** 작성 (STL 경로, 도메인 bbox, dp, 시뮬 시간, inflow 등)
3. **`FluidX3D.exe`** 호출 (cwd=iter_dir, case.txt 읽음) → PNG 프레임 + phi VTK 출력
4. **`fx3d_postprocess.postprocess`** — phi VTK → `retention.{in_pond, in_column, splash, on_module}` + touch 메트릭 → `result.json`
5. **`rhino_mcp_helpers.push_stl_to_rhino_layer`** — sculpture STL → Rhino 레이어 `fluidx3d::test_NN::sculpture`

`ga_sequential.py`는 DEAP 기반 top-down 진화: 모듈 0 최적화 → 고정 → 모듈 1 → ...

## 출력

```
runs/iter_test_NN/
  sculpture.stl            binary STL (m)
  case.txt                 FluidX3D 런타임 입력
  case.json                전체 케이스 메타 + bboxes
  result.json              메트릭
  fx3d_stdout.log          FluidX3D stdout
  fx3d_out/
    frames/image-*.png     raytraced PNG (cascade visualization)
    vtk/phi-*.vtk          binary STRUCTURED_POINTS (~14M cells at dp=0.08)
    vtk/u-*.vtk            velocity field
experiments/
  test_NN.json             GA가 작성한 per-eval params
  sequential_state.json    GA 진행 상태
ga_dashboard.html          GA 진화 그래프
experiments.html           per-cell 메트릭 그리드
```

## 성능 (RTX 5070 Ti, 16 GB)

- dp=0.08m, 4s 시뮬 → 14M cells, **~16s wall-clock**, ~8000 MLUPs/s
- GA 320 evals → 약 100 min
- VRAM ~3 GB (240M cells 최대까지 가능)

## 주요 디렉터리

- `scripts/` — Python 실행 코드
- `runs/` — gitignored. 시뮬 산출물
- `runs/_real_geom.json`, `_collider_modules.json`, `_real_sculpture.stl`, `_real_endpoint.stl` — Rhino에서 추출한 원본 기준 데이터 (커밋됨? `_*` 패턴이지만 gitignored 아님)
- `experiments/_reference_test_22.json` — 재현용 기준 파라미터

## FluidX3D 셋업 위치

별도 클론: `C:/Users/user/Downloads/FluidX3D/`
- `src/setup.cpp` — `case.txt`를 cwd에서 읽음. GA 루프가 케이스마다 재컴파일할 필요 없음
- `src/defines.hpp` — `VOLUME_FORCE + EQUILIBRIUM_BOUNDARIES + SURFACE + GRAPHICS + FP16S` 활성화
- `FluidX3D.vcxproj` — `PlatformToolset v145` (VS 2026 BuildTools)
- `bin/FluidX3D.exe` — 실행 파일
