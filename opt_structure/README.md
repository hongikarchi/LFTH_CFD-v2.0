# opt_structure - steel support optimization

This module builds a first-pass steel support frame under the CFD-optimized
water modules.

The pipeline is:

1. Extract Rhino context from `env::structure_start`, `structure`, and the
   current module metadata.
2. Generate a ground-structure graph: fixed support nodes, module support
   targets, intermediate nodes, and candidate beam members.
3. Optimize member activation and KS/JIS H-section profile choices.
4. Analyze the candidate with Python FEA. PyNite is used when installed; a
   pure-Python 3D truss fallback keeps smoke tests runnable without packages.
5. Bake the chosen centerlines, profile metadata, load markers, and H-section
   mesh previews back into Rhino.

## Quick start

```powershell
# Optional, in a Python 3.12 venv for pymoo + PyNite:
python -m pip install -r opt_structure/requirements.txt

# 1) Rhino extraction, requires Rhino MCP running
python opt_structure/scripts/extract_structure.py

# 2) Optimize. Uses pymoo when available, otherwise random-search fallback.
python opt_structure/scripts/optimize_structure.py --n-eval 80

# 3) Bake final result back to Rhino
python opt_structure/scripts/bake_structure.py

# 4) Structure optimization dashboard
python opt_structure/scripts/structure_dashboard.py
# -> http://localhost:8082

# 5) Local smoke tests
python -m unittest opt_structure.tests.smoke_tests
```

## Important assumptions

- This is an exploratory design tool, not a construction-ready structural
  sign-off.
- Steel defaults are intentionally conservative: `E=200 GPa`,
  `Fy=275 MPa`, `density=7850 kg/m3`.
- Water load uses `water_dynamic_factor=2.0` until CFD time-history pressure
  loads are wired in.
- `structure_case.json` defaults to `analysis.engine = "pynite"`. If PyNite is
  not installed, use `--engine frame` for the pure-Python 3D frame fallback.
- The bundled KS/JIS H-section CSV is a seed library. Replace it with a
  certified fabricator/Tekla export before final engineering checks.

## Optimization setup knobs

Before large runs, calibrate these inputs with a few small `--n-eval` tests:

- `loads.module_dead_kg`, `loads.module_water_kg`, and
  `loads.water_dynamic_factor`: load magnitude usually dominates member size.
- `analysis.support_model`: current extraction treats `env::structure_start`
  and existing structure endpoints as fixed supports.
- `ground_structure.intermediate_levels`: adding bracing levels can reduce
  slenderness more effectively than simply using heavier H-sections.
- `constraints.max_deflection_mm`, `constraints.deflection_span_ratio`,
  `constraints.max_slenderness`: these decide whether the optimizer spends
  material on stiffness, strength, or unbraced length.
- Profile CSV contents: use the final KS/JIS/Tekla profile list before trusting
  the ranking of alternatives.
