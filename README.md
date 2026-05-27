# LFTH_CFD v2.0

서부터미널 광장 조형물 (4-dish cascade → 연못) 의 물 splash CFD 시뮬레이션 + GA 형상 최적화.

엔진: **FluidX3D** (Lattice Boltzmann + Volume-of-Fluid on GPU). DSPH에서 마이그레이션 (2026-05-27).

## 한 번 셋업

```powershell
# (a) FluidX3D 빌드 (setup.cpp / defines.hpp 변경시 재실행)
& 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\MSBuild\Current\Bin\MSBuild.exe' `
  'C:\Users\user\Downloads\FluidX3D\FluidX3D.vcxproj' /p:Configuration=Release /p:Platform=x64 /m

# (b) Rhino 추출 (collider/positive/negative/nozzle, Rhino MCP 서버 가동 중일때)
python scripts/extract_targets.py

# (c) collider mesh 닫기 (Rhino에서 surface인 경우 임시 우회)
python scripts/thicken_collider.py 0.20
```

## 일반 실행 사이클

```powershell
# 1. config/case.json 편집 (dp, timemax, side_walls, nozzle_LPM 등)
# 2. 실행
python scripts/fx3d_run.py --test-id myrun
# → runs/iter_myrun/ 에 case.txt+nozzles.txt 자동 생성 + FluidX3D 실행 + result.json
# → runs/_settings_log.jsonl 한 줄 append
# → settings_compare.html 자동 새로고침
```

## Dashboard

```powershell
# 정적 HTML 새로 생성 (FluidX3D 안 돌리고 DB만 reload)
python scripts/update_settings_compare.py
# settings_compare.html 브라우저에서 열기
```

## GA 최적화 (현재 비활성, 추후 활성화)

```powershell
python scripts/ga_sequential.py --stages 0 --pop 8 --n_gen 10
```

## 파일 구조

```
LFTH_CFD v2.0/
├── README.md
├── settings_compare.html               ← dashboard (auto-refresh 60s)
│
├── config/
│   └── case.json                       ← 캐노니컬 파라미터 (직접 편집 또는 dashboard)
│
├── runs/                               [gitignored]
│   ├── _real_targets.json              Rhino 추출: pos/neg bbox + 230 nozzles
│   ├── _real_collider.stl              Rhino mesh (open)
│   ├── _real_collider_thickened.stl    closed manifold
│   ├── _settings_log.jsonl             append-only DB
│   └── iter_*/                         실험마다 한 폴더
│       ├── case.txt                    (runtime input, fx3d_run.py가 config/case.json에서 생성)
│       ├── nozzles.txt                 (runtime input, rule = _real_targets + LPM)
│       ├── sculpture.stl               (collider 카피)
│       ├── case.json                   확장 메타
│       ├── result.json                 postprocess 결과
│       ├── fx3d_stdout.log
│       └── fx3d_out/{frames,vtk}/
│
├── scripts/
│   ├── rhino_mcp_helpers.py            Rhino MCP socket
│   ├── extract_targets.py              Rhino → JSON+STL
│   ├── thicken_collider.py             open mesh → closed
│   ├── fx3d_run.py                     ◀ 통합 runner (CLI + library)
│   ├── fx3d_postprocess.py             VTK → result.json
│   ├── fx3d_visualize_in_rhino.py      결과 Rhino push
│   ├── update_settings_compare.py      DB → dashboard
│   ├── ga_sequential.py                GA 루프 (현재 비활성)
│   ├── module_geometry.py              parametric STL gen (추후 GA용)
│   └── export_paraview.py              VTK 헬퍼
│
└── 외부: C:\Users\user\Downloads\FluidX3D\
    ├── src/{defines.hpp,setup.cpp,...}  ← C++ 소스 (변경시 재빌드 필요)
    └── bin/FluidX3D.exe                 ← 컴파일된 바이너리
```

## 입력 파일 종류

| 파일 | 종류 | 변경시 절차 |
|---|---|---|
| `config/case.json` | JSON 파라미터 | 그냥 fx3d_run.py 재실행 |
| `_real_targets.json` | Rhino 추출 | extract_targets.py 재실행 (Rhino 디자인 바뀌면) |
| `_real_collider_thickened.stl` | mesh | thicken_collider.py 재실행 (두께 바꾸거나 디자인 바뀌면) |
| `setup.cpp` / `defines.hpp` | C++ 소스 | **msbuild 재빌드 (~30s)** |

## 성능 (RTX 5070 Ti)

| dp | cells | wall (12s sim) |
|---|---|---|
| 0.08 m | 24M | ~36s |
| 0.05 m | 60M | ~5 min |
| 0.04 m | 192M | ~10 min (VRAM 13GB) |
