# opt_pymoo — pymoo NSGA-II 다목적 최적화

조형물 4 모듈 형상 파라미터를 sequential GA로 탐색. 각 stage = 8-gene NSGA-II 한 모듈.

## 진입점

```powershell
# 단일 stage
python opt_pymoo/scripts/pymoo_run.py --stage 0 --pop 16 --n_gen 10

# 연속 stage 0→3 (top-down)
python opt_pymoo/scripts/pymoo_run.py --stages 0,1,2,3 --pop 16 --n_gen 10 --seed 42

# 진행 상태 (resumable)
python opt_pymoo/scripts/pymoo_run.py --status
```

`experiments/pymoo_state.json` 가 stage·gen·population 상태 저장. CLI 중단되어도 재실행 시 이어감.

## 두 가지 목표 (NSGA-II Pareto)

1. **splash_frac** (minimize) = `(total - in_positive) / total` — 물이 positive 영역 밖으로 새는 비율
2. **-dist_from_nozzle** (minimize) = 노즐에서 모듈까지 거리의 음수 — 가까울수록 점수↑

Pareto front에서 stage 끝에 한 점 인터랙티브 선택 → 다음 stage의 `prior_bests` 로 fixed.

## Gene 구조

`pymoo_gen_module.py` 의 GENE_ORDER:
```python
['radius_xy_m', 'radius_z_m', 'rotation_z_deg',
 'offset_dist_m', 'tx_mm', 'ty_mm', 'tz_mm', 'shape_param']
```
- bounds + defaults 는 동일 파일 GENE_BOUNDS / DEFAULTS 에서 import.
- 8 floats per individual → NSGA-II SBX crossover + PM mutation + LHS sampling.

## 학습된 함정

### DEAP → pymoo 마이그레이션 이유

- DEAP 의 NSGA-II 구현이 multi-thread 평가에서 race 발생 (pymoo_state.json 동시 쓰기).
- pymoo는 sequential evaluator 명시 + `Callback` 으로 클린한 snapshot 훅.
- 마이그레이션 후 cross-stage resume 안정화.

### Cross-package import

`pymoo_run.py` 는 fx3d_run import 필요 (실험 평가). sys.path 양쪽 추가:
```python
sys.path.insert(0, str(SCRIPT_DIR))                          # for pymoo_gen_module
sys.path.insert(0, str(REPO_ROOT / "env_fx3d" / "scripts"))  # for fx3d_run
from fx3d_run import run_experiment
```

### runs/ 분리

GA 평가가 `run_experiment(runs_root=RUNS)` 로 호출 → `opt_pymoo/runs/iter_*/` 출력. dashboard interactive 실험은 `env_fx3d/runs/` 로 — **섞이지 않음**.

### Sequential staging 이유

4 모듈 동시 (32-gene) 탐색 시 search space 폭발 + 위 모듈이 아래 모듈 결과 결정. Top-down staging (mod0 → mod1 → mod2 → mod3) 각 stage 마다 prior_bests fix → 8-gene problem 4번.

### FAIL_F = [1.0, 0.0]

fx3d_run 실패 시 (STL invalid, 시뮬 crash 등) 최악 점수 [splash=1.0, dist=0] 주입. NSGA-II selection이 fail 개체를 자동 후순위로 밀어냄.

## _optimization_log.jsonl 스키마

`_settings_log.jsonl` 패턴 미러. 매 개체 평가마다 한 줄 (append-only):
```json
{
  "ts": "ISO timestamp",
  "stage": 0,
  "test_id": "stg0_ind00",
  "genes": {"radius_xy_m": ..., "tx_mm": ...},
  "objectives": [0.42, -1840.0],   // [splash_frac, -dist_mm]
  "splash_frac": 0.42,
  "dist_mm": 1840.0,
  "wall_s": 38.2,
  "failed": false,
  "stage_fail": null,
  "error": null,
  "issue": null,   // post-hoc: e.g. "no_water_M3"
  "notes": null
}
```

`issue` / `notes` 는 비워둠 → 분석 후 채움. 같은 실패 반복 안 하기 위함.

## 파일 가이드

| 파일 | 역할 |
|---|---|
| `scripts/pymoo_run.py` | NSGA-II 진입점, MyProblem class, append_optimization_log helper |
| `scripts/pymoo_gen_module.py` | gene→STL 빌더. GENE_ORDER/BOUNDS/DEFAULTS 정의. trimesh-based. |
| `experiments/pymoo_state.json` | stage·gen·population 진행 snapshot (resumable) |
| `experiments/test_NN.json` | 개체별 compose_params 결과 (input) |
| `_optimization_log.jsonl` | append-only DB |
| `runs/iter_*/` | 실험 산출물 (env_fx3d/runs와 분리, gitignore) |
| `runs/_collider_modules.json` | extract_targets.py 가 만든 module bbox + cap info |
