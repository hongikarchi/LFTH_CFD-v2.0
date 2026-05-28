"""Extract structural optimization context from Rhino.

Reads:
  - env::structure_start curves as fixed support candidate curves
  - structure layer curves as existing beam/connectivity hints
  - env_fx3d/runs/_collider_modules.json as CFD module metadata

Writes:
  opt_structure/runs/_structure_context.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "env_fx3d" / "scripts"))

from rhino_mcp import mcp_call

RUNS = MODULE_ROOT / "runs"
DEFAULT_OUT = RUNS / "_structure_context.json"
DEFAULT_MODULES_JSON = REPO_ROOT / "env_fx3d" / "runs" / "_collider_modules.json"

STRUCTURE_START_LAYER = "env::structure_start"
EXISTING_STRUCTURE_LAYER = "structure"


def _mcp_python(code: str, timeout: float = 60.0) -> str:
    r = mcp_call(code, timeout=timeout)
    if r.get("status") != "success":
        raise RuntimeError(f"MCP call failed: {r}")
    return r.get("result", {}).get("output", "")


def fetch_structure_layers(sample_step_mm: float) -> dict:
    code = f'''
import json, math
import Rhino, scriptcontext as sc
doc = sc.doc
start_layer = "{STRUCTURE_START_LAYER}"
structure_root = "{EXISTING_STRUCTURE_LAYER}"
sample_step = float({sample_step_mm})

def layer_full_path(idx):
    try:
        return doc.Layers[idx].FullPath
    except Exception:
        return ""

def is_on_layer(o, target, include_children=False):
    fp = layer_full_path(o.Attributes.LayerIndex)
    if include_children:
        return fp == target or fp.startswith(target + "::")
    return fp == target

def point_tuple(p):
    return [float(p.X), float(p.Y), float(p.Z)]

def curve_points(curve):
    if curve is None:
        return []
    pts = []
    try:
        if sample_step > 0 and curve.GetLength() > sample_step:
            params = curve.DivideByLength(sample_step, True) or []
            for t in params:
                pts.append(point_tuple(curve.PointAt(t)))
        else:
            pts = [point_tuple(curve.PointAtStart), point_tuple(curve.PointAtEnd)]
    except Exception:
        try:
            pts = [point_tuple(curve.PointAtStart), point_tuple(curve.PointAtEnd)]
        except Exception:
            pts = []
    if len(pts) == 1:
        pts.append(pts[0])
    return pts

support_curves = []
existing_beams = []
for o in doc.Objects:
    g = o.Geometry
    curve = None
    if isinstance(g, Rhino.Geometry.Curve):
        curve = g
    elif isinstance(g, Rhino.Geometry.LineCurve):
        curve = g
    if curve is None:
        continue
    if is_on_layer(o, start_layer, False):
        pts = curve_points(curve)
        if pts:
            support_curves.append({{
                "guid": str(o.Id),
                "name": o.Attributes.Name or "",
                "layer": layer_full_path(o.Attributes.LayerIndex),
                "length_mm": float(curve.GetLength()),
                "points_mm": pts,
            }})
    elif is_on_layer(o, structure_root, True):
        pts = curve_points(curve)
        if len(pts) >= 2:
            existing_beams.append({{
                "guid": str(o.Id),
                "name": o.Attributes.Name or "",
                "layer": layer_full_path(o.Attributes.LayerIndex),
                "length_mm": float(curve.GetLength()),
                "points_mm": pts,
                "start_mm": pts[0],
                "end_mm": pts[-1],
            }})

print("STRUCTURE_JSON " + json.dumps({{
    "support_curves": support_curves,
    "existing_beams": existing_beams,
}}))
'''
    out = _mcp_python(code, timeout=90)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("STRUCTURE_JSON "):
            return json.loads(line[len("STRUCTURE_JSON "):])
    raise RuntimeError(f"no STRUCTURE_JSON line in MCP output: {out[-500:]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--modules-json", default=str(DEFAULT_MODULES_JSON))
    parser.add_argument("--sample-step-mm", type=float, default=1500.0)
    args = parser.parse_args(argv)

    modules_path = Path(args.modules_json)
    modules = []
    if modules_path.exists():
        payload = json.loads(modules_path.read_text(encoding="utf-8"))
        modules = payload.get("modules", [])

    extracted = fetch_structure_layers(args.sample_step_mm)
    context = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "units": "mm",
        "source_layers": {
            "structure_start": STRUCTURE_START_LAYER,
            "existing_structure": EXISTING_STRUCTURE_LAYER,
        },
        "modules_source": str(modules_path).replace("\\", "/"),
        "modules": modules,
        **extracted,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(context, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print(f"support_curves={len(context['support_curves'])} existing_beams={len(context['existing_beams'])} modules={len(modules)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

