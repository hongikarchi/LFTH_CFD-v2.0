# LFTH_CFD v2.0

서부터미널 광장 조형물 (4-dish cascade → 연못) 의 물 splash CFD + 형상 최적화.

엔진: **FluidX3D** (Lattice Boltzmann + Volume-of-Fluid on GPU). DSPH→LBM 마이그레이션 (2026-05-27).

## 모듈

- [`CLAUDE.md`](CLAUDE.md) — 디렉토리 맵, 진입점, 학습 인덱스
- [`env_fx3d/`](env_fx3d/README.md) — FluidX3D 시뮬레이션 (case.json 파라미터, kernel 학습)
- [`opt_pymoo/`](opt_pymoo/README.md) — pymoo NSGA-II 다목적 GA (sequential staging)

## 빠른 시작

```powershell
# 대시보드 (Settings · History · Charts · Files 탭, 포트 8080)
python dashboard.py

# 단일 sim
python env_fx3d/scripts/fx3d_run.py --test-id myrun

# 최적화
python opt_pymoo/scripts/pymoo_run.py --stage 0 --pop 16 --n_gen 10
```

상세는 위 모듈 README 참조.
