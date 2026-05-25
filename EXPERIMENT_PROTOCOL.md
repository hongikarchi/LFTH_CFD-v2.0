# 실험 프로토콜 — Collider Tuning

> CFD-driven sculpture tuning. 각 collider 모듈을 변형하면서 end_point 도달
> polyline 수를 최대화한다.

---

## 0. 목표 (Objective)

```
maximize    caught_count = (end_point AABB 안에서 끝나는 polyline 수)
                          또는 그에 비례한 splash_ratio 감소
subject to  6개 collider 모듈의 회전/위치/스케일/마찰 변경
            노즐 위치는 사용자 설계 고정 (start_point plate 안 5 hole)
```

---

## 1. 모듈 식별 (Module Identification)

`runs/_collider_modules.json` 에 기록됨 (z 중심 내림차순으로 0..5 인덱스).
현재 인덱싱 (2026-05-26):

| Index | center z (m) | 위치 |
|-------|-------------|------|
| 0 | +21.65 | 최상단 shell |
| 1 | +15.99 | 2번째 |
| 2 | +10.13 | 3번째 |
| 3 | +5.56  | 4번째 |
| 4 | -1.50  | 바닥 shell A |
| 5 | -1.53  | 바닥 shell B |

원본 GUID는 JSON에 보관. 변형은 항상 **원본 mesh의 사본**에 적용 (사용자 원본 doc 비파손).

---

## 2. 변경 가능 변수 (Per-Test Variables)

### 2-1. 모듈별 변수 (각 모듈 0..5에 대해)
- `rotation_deg`: `[rx, ry, rz]` — degrees, world axes 기준 회전
- `translation_m`: `[tx, ty, tz]` — 원본 위치에서의 이동 (m)
- `scale`: float (uniform scale, 모듈 중심 기준)

### 2-2. 전역 변수
- `viscobound`: float — sculpture 표면 마찰 (default 1.0).
  값 ↑ = 더 끈적, 표면 따라 흐름 ↑.
  값 ↓ = 더 미끄러움, 튕김 ↑.
  **제한:** 현재 단상 SPH에서는 모듈별 마찰 불가. 전체 sculpture에 동일 적용.
- (기타 sim 파라미터는 §3 기본값 고정 권장. 변경 시 명시.)

---

## 3. 고정 시뮬레이션 파라미터 (Default — 변경 시 명시 필수)

| 항목 | 값 | 이유 |
|------|-----|------|
| `dp` | 0.10 m | 해상도 vs 속도 균형 |
| `timemax` | **10.0 s** | 자유낙하 끝까지 + pond에 안착 시간 |
| `timeout` | 0.10 s | frame 저장 간격 |
| `v_inlet` (burst) | 2.0 m/s | 노즐 box 통과 + free fall dominant (impact ~9 m/s, 종단속도 근사) |
| `burst_end` | 0.06 s | 짧은 emission window |
| `nozzle_diameter` | 0.15 m | 모듈 1개당 hole 직경 |
| `nozzle_holes` | 5 (start_point plate 안 dice-5) | 5개 노즐, 균등 분산 |
| `speedsound` | 250 m/s | 자유낙하 후 최대 속도 ~15 m/s 의 10x+ 안전 마진 |
| `cflnumber` | 0.30 | 시간적분 안정 |
| `DensityDT` | 1 (Molteni) | 빠르고 안정 |
| `Solver` | DualSPHysics5.4 **CPU** | 우리 입자 규모에서 CPU 우세 |
| `inlet_layers` | 4 | 표준 |

이 값들은 `scripts/experiment_runner.py` 안 `DEFAULTS` dict에 코드화됨.

---

## 4. Test ID 규칙

- 형식: `test_NN` (NN = zero-padded 2자리, 순차 증가)
- 사용된 ID: `test_01` (v_inlet=10, 원본 sculpture), `test_02` (v_inlet=2, 원본).
- 다음 사용 가능: `test_03` 부터.

---

## 5. 파라미터 파일 (Per-Test Input)

위치: `experiments/test_NN.json`

스키마:
```json
{
  "test_id": "test_03",
  "note": "최상단 shell 30도 회전 시도",
  "global": {
    "v_inlet": 2.0,
    "dp": 0.10,
    "timemax": 10.0,
    "viscobound": 1.0
  },
  "modules": [
    {"index": 0, "rotation_deg": [0, 0, 30], "translation_m": [0, 0, 0], "scale": 1.0},
    {"index": 1, "rotation_deg": [0, 0, 0],  "translation_m": [0, 0, 0], "scale": 1.0},
    {"index": 2, "rotation_deg": [0, 0, 0],  "translation_m": [0, 0, 0], "scale": 1.0},
    {"index": 3, "rotation_deg": [0, 0, 0],  "translation_m": [0, 0, 0], "scale": 1.0},
    {"index": 4, "rotation_deg": [0, 0, 0],  "translation_m": [0, 0, 0], "scale": 1.0},
    {"index": 5, "rotation_deg": [0, 0, 0],  "translation_m": [0, 0, 0], "scale": 1.0}
  ]
}
```

생략된 module 항목은 identity (변형 없음) 으로 처리. 생략된 `global` 항목은 §3 default 사용.

---

## 6. Rhino 출력 레이어 구조

각 test 후 Rhino doc에 다음 레이어 생성:
```
test_NN/
├── stream_nozzle_0    (빨강 polylines, 노즐 0번 출신)
├── stream_nozzle_1    (녹색)
├── stream_nozzle_2    (파랑)
├── stream_nozzle_3    (주황)
├── stream_nozzle_4    (보라)
└── collider_NN        (이 테스트에서 변형된 collider mesh — 모든 6모듈 합본)
```

원본 `collider`/`start_point`/`end_point` 레이어는 변경 없음.

---

## 7. 결과 파일

`runs/iter_test_NN/`:
- `params.json` — 사용된 파라미터 (실제 적용값)
- `case_Def.xml` — 생성된 DualSPHysics case
- `sculpture.stl` — 변형된 collider STL
- `velprof.dat` — burst velocity profile
- `Out/` — DualSPHysics raw output
  - `PartFluid_*.vtk` — frame별 fluid particle positions
  - `data/Part_*.bi4` — binary native output
- `trails.json` — Idp별 polyline 좌표
- `result.json` — fitness 결과 (caught/splash/total/ratio)
- `summary.png` — Rhino viewport capture (선택)

---

## 8. 대시보드 (experiments.html)

repo 루트의 `experiments.html` 가 모든 test 자동 집계.
열: Test ID / 변경 요약 / Caught / Splash / Total / Splash ratio / Wall time / Date.

매 테스트 후 `scripts/update_experiments_html.py` 가 호출됨 (runner가 자동).

---

## 9. 실행 워크플로우 (1 test)

```bash
# 1. 파라미터 파일 작성 (또는 사용자가 직접 지정)
experiments/test_03.json

# 2. Runner 호출
python scripts/experiment_runner.py experiments/test_03.json

# Runner가 자동으로:
#   a. 원본 collider 모듈 6개를 Rhino MCP로 추출
#   b. 각 모듈에 params의 transform 적용
#   c. test_NN/collider_NN 레이어에 변형된 mesh 배치
#   d. 변형된 STL 합본 export
#   e. case_Def.xml 렌더링 (velocity profile 포함)
#   f. GenCase + DualSPHysics CPU + PartVTK
#   g. trails.json + result.json 저장
#   h. test_NN/stream_nozzle_X 레이어에 polyline import (mm 변환)
#   i. experiments.html 갱신
#   j. summary.png 캡처 (옵션)
```

---

## 10. 측정 (Fitness)

- **Caught:** polyline 마지막 점이 end_point AABB 안에 있고 z ≤ POND_TOP_Z 인 trail 수
- **Splash:** 그 외 (도메인 밖으로 사라진 trail 포함)
- **Total:** Caught + Splash
- **splash_ratio:** Splash / Total ∈ [0, 1]. **낮을수록 좋음.**
- **caught_count:** Caught (절대값). 비교 시 보조 지표.

POND_TOP_Z = `pond_thickness(0.5m) + 3*dp = 0.8m` (자동).
End_point AABB는 `runs/_real_geom.json` 의 `end_bbox_m` 사용.

---

## 11. 보존·일관성 규칙

1. 한 번 정의된 test_NN은 **수정 금지**. 새 실험은 새 ID로.
2. params.json 의 default 값을 변경하려면 새 test로 분기 + note에 명시.
3. `runs/iter_test_NN/` 디렉토리는 result.json/params.json만 commit (대용량 VTK는 gitignore).
4. Rhino doc의 `test_NN/` 레이어는 사용자가 임의 삭제 가능 (재실행하면 재생성).
5. 원본 `collider` 레이어 객체는 **절대 수정하지 않음**. 변형은 항상 사본에.

---

## 12. 다음 GA 단계 (참고)

여러 test_NN 결과 누적되면 fitness landscape 형성. 추후 Wallacei 또는 Python DEAP로
자동 탐색 가능. 그 전까지는 사용자 가설 기반 수동 탐색이 유효.

(끝)
