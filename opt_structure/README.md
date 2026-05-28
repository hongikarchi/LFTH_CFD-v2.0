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
# 1) Rhino extraction, requires Rhino MCP running
python opt_structure/scripts/extract_structure.py

# 2) Optimize. Uses pymoo when available, otherwise random-search fallback.
python opt_structure/scripts/optimize_structure.py --n-eval 80

# 3) Bake final result back to Rhino
python opt_structure/scripts/bake_structure.py

# 4) Local smoke tests
python -m unittest opt_structure.tests.smoke_tests
```

## Important assumptions

- This is an exploratory design tool, not a construction-ready structural
  sign-off.
- Steel defaults are intentionally conservative: `E=200 GPa`,
  `Fy=275 MPa`, `density=7850 kg/m3`.
- Water load uses `water_dynamic_factor=2.0` until CFD time-history pressure
  loads are wired in.
- The bundled KS/JIS H-section CSV is a seed library. Replace it with a
  certified fabricator/Tekla export before final engineering checks.

