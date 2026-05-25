# LFTH_CFD v2.0 — CFD-driven Parametric Sculpture Design

**작성일:** 2026-05-25
**저자:** Claude (Opus 4.7, 1M context) — 사용자: hongikarchi / life@lifethings.in

---

## 0. 프로젝트 목표

> 위에서 노즐을 통해 떨어지는 물이 조형물을 거쳐 가장 아래 연못에 도달.
> 연못 밖으로 튀는 물(splash)을 최소화하도록 조형물의 크기·각도·높이를 GA로 자동 최적화.

**Loop:** Rhino/Grasshopper 파라메트릭 조형물 → DualSPHysics 유체 시뮬레이션 → splash 정량화 → 디자인 자동 수정.

**End state:** Grasshopper에서 slider 조절 + 시뮬 실행 + Rhino에 시뮬 결과 시각화까지 동작하는 통합 파이프라인.

---

## 1. 환경 검증 (완료 2026-05-25)

| 항목 | 상태 | 비고 |
|------|------|------|
| OS | Windows 11 Pro 10.0.26200 | |
| GPU | NVIDIA RTX 5070 Ti (16GB) | Blackwell. CUDA 13.2 driver 596.21 |
| DualSPHysics | v5.4.3 추출됨 | `C:\Users\user\Downloads\DualSPHysics_v5.4.3\DualSPHysics_v5.4\` |
| Windows binaries | 확인됨 | `GenCase_win64.exe`, `DualSPHysics5.4_win64.exe` (GPU), `PartVTK_win64.exe`, `PartVTKOut_win64.exe` |
| Rhino + Grasshopper | 사용자 보유 (확인 필요) | Rhino 8 권장 (CPython3 GHPython 지원) |
| GA 엔진 | Wallacei X 권장 | Galapagos는 sync slider sweep 한계 |
| Git remote | `https://github.com/hongikarchi/LFTH_CFD-v2.0` (public, main) | |

**검증 명령 (참고):**
```cmd
nvidia-smi
where GenCase_win64
```

---

## 2. 핵심 기술 결정

| 항목 | 선택 | 이유 |
|------|------|------|
| CFD 엔진 | DualSPHysics 5.4 GPU 빌드 | SPH = free-surface splash 정확. SPH는 mesh-free → 디자인 변경마다 mesh 재생성 불필요 |
| Inlet 방식 | `<inout>` zone + `<imposevelocity mode="0">` | `examples/inletoutlet/08_ImpingingJet` 패턴. 유속을 fixed value로 제어 |
| 노즐 모델링 | 위쪽 (z=z_nozzle)에서 inlet box 정의, 방향벡터로 각도 표현 | XML `<rotate>` + `<imposevelocity>` 벡터 |
| 조형물 import | STL → `<drawfilestl>` GenCase 명령 | GH에서 export 후 path 참조 |
| 연못 catchment | `<setmkbound>` rectangular box (pond) + AABB constraint | particle z<pond_top && pond_x_range 안 = "도달" |
| Splash 정의 | 시뮬 종료 시점에 catchment AABB 밖에 있는 fluid particle 수 | PartVTKOut 또는 PartVTK CSV 후처리 |
| 단위계 | SI (m, s, kg) | DualSPHysics default |
| 해상도 (탐색 권장) | `dp = 0.020 m` (2cm) | RTX 5070 Ti로 cube STL 약 60-90초/eval 예상 |
| 해상도 (정밀 탐색) | `dp = 0.015 m` (1.5cm) | **실측 138초/eval** (cube, timemax=4.0s) |
| 해상도 (refine) | `dp = 0.005 m` (5mm) | 최종 후보만 |
| 시뮬 시간 | `timemax = 4.0 s` (정밀) / 2.5 s (탐색) | splash 패턴 안정화 |
| TimeOut (저장 간격) | `0.05 s` → 80 frame | refine 시 0.02s |

---

## 3. 디렉토리 구조

```
C:\Users\user\Documents\LFTH_CFD v2.0\
├── PLAN.md                                ← 이 문서
├── README.md                              (later)
├── .gitignore
├── templates\
│   └── case_sculpture_template.xml        ← 노즐+연못+조형물 XML 템플릿
├── runs\                                  ← 시뮬레이션 출력 (gitignore)
│   └── iter_<id>\
│       ├── sculpture.stl
│       ├── case.xml
│       ├── Out\                           ← DualSPHysics output
│       ├── splash_out.csv                 ← PartVTKOut output
│       └── fitness.json                   ← {splash_count, total_particles, ...}
├── scripts\
│   ├── run_case.py                        ← 자동화 메인 스크립트
│   ├── fitness.py                         ← splash 카운트 계산
│   └── smoke_test.py                      ← DualSPHysics 동작 검증
├── gh\
│   ├── sculpture_design.gh                (사용자가 작성 - placeholder)
│   └── dualsphysics_eval_ghpy.py          ← GHPython3 컴포넌트
├── rhino_import\
│   └── load_vtk_particles.py              ← Rhino script — VTK → point cloud
└── memory\                                (Claude internal)
```

---

## 4. 노즐+연못 시나리오 설계

**좌표계:**
- Origin (0,0,0): 연못 바닥 중심
- +Z: 위 (gravity 방향 -Z)
- +X, +Y: 수평

**도메인 (예시 default):**
- AABB: X[-1.0, 1.0], Y[-1.0, 1.0], Z[0, 3.0] (단위 m)
- Pond: 바닥 박스 X[-0.4, 0.4], Y[-0.4, 0.4], Z[0, 0.05]
- Sculpture: 연못 위, Z[0.05, ~1.5] 영역 (parametric size/angle/height로 정의됨)
- Nozzle inlet: Z=2.5 부근. 위치 (nozzle_x, nozzle_y) + 방향 (각도 → velocity vector)

**Parametric variables (Grasshopper sliders):**
| 변수 | 단위 | 범위 (default) | 설명 |
|------|------|----------------|------|
| `sculpture_size` | m | 0.3 - 0.8 | 조형물 전체 스케일 |
| `sculpture_angle` | deg | -30 - 30 | 조형물 회전 또는 표면 각도 |
| `sculpture_height` | m | 0.5 - 1.5 | 조형물 z 위치/높이 |
| `nozzle_x` | m | -0.3 - 0.3 | 노즐 X 위치 |
| `nozzle_y` | m | -0.3 - 0.3 | 노즐 Y 위치 |
| `nozzle_angle_x` | deg | -30 - 30 | 노즐 분사 방향 X-tilt |
| `nozzle_angle_y` | deg | -30 - 30 | 노즐 분사 방향 Y-tilt |
| `flow_velocity` | m/s | 1.0 - 5.0 | 유속 (유량 = velocity × inlet area) |
| `nozzle_diameter` | m | 0.02 - 0.05 | 노즐 직경 (유량 영향) |

**유량(Q) = π × (nozzle_diameter/2)² × flow_velocity**

**Fitness (splash escape ratio):**
```
splash = (catchment AABB 밖 fluid particle 수) / (전체 fluid particle 수)
fitness = splash   # 최소화
```

---

## 5. Phase별 실행 계획

### Phase 1 — Smoke Test (Day 1, 진행 중)
**목표:** `examples/main/01_DamBreak` GPU 실행 → 시간 측정 → GPU 빌드 동작 확인.

**Actions:**
1. `scripts/smoke_test.py` 실행 (이번 세션에서 작성+실행)
2. 출력 로그 + 실행 시간 기록
3. CUDA 빌드 작동 검증

**Success criteria:**
- DualSPHysics 종료 코드 0
- VTK 출력 파일 생성됨
- 시뮬 시간 < 5분 (작은 예제)

### Phase 2 — XML Template (Day 1-2)
**목표:** `templates/case_sculpture_template.xml` 작성. placeholder 변수 + STL import + nozzle inlet + pond.

**산출:** XML이 GenCase에 통과되어 case가 빌드되는 상태. STL은 placeholder cube 사용.

### Phase 3 — Automation Scripts (Day 2-3)
**`scripts/run_case.py`:**
```python
def run_case(iter_id, params, stl_path, dp=0.015, timemax=4.0):
    """
    params: dict — sculpture_size, sculpture_angle, sculpture_height,
                   nozzle_x, nozzle_y, nozzle_angle_x, nozzle_angle_y,
                   flow_velocity, nozzle_diameter
    returns: dict — {'splash_ratio': float, 'splash_count': int, ...}
    """
```

XML placeholder 치환 (e.g., `{{nozzle_x}}` → `0.15`) 후 GenCase → DualSPHysics → PartVTKOut.

### Phase 4 — Rhino Visualization (Day 3-4)
**`rhino_import/load_vtk_particles.py`:** Rhino Python 스크립트.
- DualSPHysics output 디렉토리에서 frame VTK 읽음
- Particle → point object (또는 mesh sphere) Rhino에 import
- Color by velocity (선택)
- Animation: frame slider로 timestep 이동

### Phase 5 — GH 컴포넌트 + GA (Week 2)
**`gh/dualsphysics_eval_ghpy.py`:** GHPython3 컴포넌트 — sliders/STL 입력 → `run_case.py` subprocess 호출 → fitness 출력.

**Wallacei X budget (실측 기반):**
| Eval 시간 / Pop / Gen | 총 evals | 벽시간 (실측 138s/eval 기준) |
|----------------------|---------|-----------------------------|
| coarse: 60s × 20 × 15 | 300 | 5시간 |
| 권장: 90s × 20 × 20 | 400 | 10시간 |
| 정밀: 140s × 30 × 25 | 750 | 29시간 |

- Genes: 9개 slider
- Objective: minimize splash_ratio
- 첫 GA run은 `dp=0.020, timemax=2.5s`로 시작 (~5-7시간 overnight) 권장

### Phase 6 — Refinement (Week 3)
Pareto front 상위 5-10 후보 → `dp=0.005`로 재시뮬 → 최종 선택 → Rhino에서 high-res 시각화.

---

## 6. 실행 자동화 명령 체인

매 GA 평가마다 실행되는 명령 sequence:

```cmd
:: 1. XML case build
GenCase_win64.exe "runs\iter_<id>\case.xml" "runs\iter_<id>\Out\case" -save:all

:: 2. SPH simulation (GPU)
DualSPHysics5.4_win64.exe "runs\iter_<id>\Out\case" "runs\iter_<id>\Out" -gpu

:: 3. Extract particles that exited domain
PartVTKOut_win64.exe -dirin "runs\iter_<id>\Out" -savecsv "runs\iter_<id>\splash_out.csv"

:: 4. (Optional) Extract all fluid particles last frame for fitness 후처리
PartVTK_win64.exe -dirin "runs\iter_<id>\Out" -savecsv "runs\iter_<id>\fluid_last.csv" -onlytype:-all,+fluid -last
```

---

## 7. 리스크 & Mitigation

| 리스크 | 영향 | 대응 |
|--------|------|------|
| 5070 Ti가 너무 새 GPU → DualSPHysics CUDA 빌드 incompat | Phase 1 실패 → CPU fallback (느림) | `DualSPHysics5.4CPU_win64.exe` 백업. 또는 source 재컴파일 (`src/VS/*_vs2022.sln`) |
| STL boolean fail (slider 극단값) | iter crash | GHPython try/except → fitness=999 반환 (GA penalty) |
| 시뮬 발산 (TimeMax 도달 못 함) | iter 시간 폭증 | XML `<parameter key="PartsOutMax" value="0.5">` + timeout subprocess (300s) |
| GH 멈춤 (subprocess wait) | UX 나쁨 | Wallacei async-tolerant. 1eval=수십초 OK |
| 디스크 폭증 (수십 iter × VTK output) | 수GB | `runs/iter_*` gitignore + iter cleanup (top-k만 보존) |
| 경로 공백 ("LFTH_CFD v2.0") | subprocess 일부에서 quote 문제 | Python `subprocess.run([...])` 리스트 인자로 자동 escape |

---

## 8. 의존성

**Rhino/GH 측:**
- Rhino 8 (CPython3 GHPython)
- Wallacei X (Food4Rhino 무료)
- (선택) Pufferfish, Lunchbox — STL export 보조

**Python 측 (Rhino 8 ScriptEditor):**
- 표준 라이브러리: `os`, `subprocess`, `shutil`, `json`, `xml.etree.ElementTree`
- 외부: `numpy` (Rhino 8 패키지 매니저로 설치)

**System:**
- DualSPHysics binaries (확보 완료)
- NVIDIA driver (확보 완료, 596.21)
- CUDA Toolkit 별도 설치 **불필요** (DualSPHysics 바이너리 자체 번들)

---

## 9. 이번 세션에서 작성된 산출물 (모두 commit + push 완료)

- `PLAN.md` (이 문서)
- `.gitignore`
- `templates/case_sculpture_template.xml`
- `scripts/run_case.py`, `scripts/fitness.py`, `scripts/smoke_test.py`, `scripts/integration_test.py`
- `gh/dualsphysics_eval_ghpy.py`
- `rhino_import/load_vtk_particles.py`

**검증된 실행:**
- `smoke_test.py`: DamBreak2D 예제, 67초 GPU 시뮬, 201 VTK frame
- `integration_test.py` (coarse dp=0.025, timemax=1.5s): 28초, 1656 particle, splash_ratio=0.89
- 정밀 설정 (dp=0.015, timemax=4.0s): **138초**, 12,864 particle, 2,253 caught (17.5%)

`runs/iter_timing_planned/Out/PartFluid_*.vtk` 81 frame이 Rhino import 대기 중.

Remote: `https://github.com/hongikarchi/LFTH_CFD-v2.0` (`main` branch)

---

## 10. 다음 액션 (사용자 측)

### 즉시 Rhino에서 시뮬 결과 확인 (GH 없이)
1. Rhino 8 실행
2. 명령 `_RunPythonScript` 또는 `_EditPythonScript`
3. `C:\Users\user\Documents\LFTH_CFD v2.0\rhino_import\load_vtk_particles.py` 열기/실행
4. `iter_timing_planned/Out/PartFluid_*.vtk` 81 frame이 layer별로 import됨
   - 마지막 frame은 caught(녹색) / splash(빨강) 분리됨
   - `pond_AABB` layer에 연못 박스 시각화
5. Layer panel에서 frame toggle로 애니메이션 확인

### GH 통합 (다음 작업 세션)
1. **Rhino 8 + Wallacei X 설치 확인** (없으면 설치)
2. `gh/sculpture_design.gh` 파일을 GH에서 새로 작성 — slider 9개 + 조형물 geometry 로직 + STL export
3. `gh/dualsphysics_eval_ghpy.py` 코드를 GH의 Script 컴포넌트에 붙여넣기
4. 첫 단일 평가 (slider 한 번 움직여 fitness 출력 확인)
5. Wallacei X로 GA 연결 — 첫 run은 coarse 설정 (`dp=0.020`, `timemax=2.5s`)으로 5-7시간 overnight

---

**문서 끝.**
