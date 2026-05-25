"""
Grasshopper Python (Rhino 8 CPython3) component: dualsphysics_eval

Paste this into a Script component in Grasshopper.

INPUTS (configure on the component):
    mesh                : Mesh                — sculpture geometry (closed mesh, triangulated)
    sculpture_size      : float (Number)      — scale factor
    sculpture_angle     : float (Number)      — Z-axis rotation in degrees
    sculpture_height    : float (Number)      — Z translation in meters
    nozzle_x            : float (Number)      — nozzle X position (m)
    nozzle_y            : float (Number)      — nozzle Y position (m)
    nozzle_z            : float (Number)      — nozzle Z height (m), default 2.5
    nozzle_diameter     : float (Number)      — nozzle square side (m)
    nozzle_angle_x      : float (Number)      — tilt about X (deg)
    nozzle_angle_y      : float (Number)      — tilt about Y (deg)
    flow_velocity       : float (Number)      — inlet velocity magnitude (m/s)
    dp                  : float (Number)      — particle distance (m), default 0.015
    timemax             : float (Number)      — sim duration (s), default 4.0
    run                 : bool (Boolean)      — set True to trigger evaluation (default False)

OUTPUTS:
    fitness             : float               — splash_ratio in [0, 1], lower is better
                                                999 = sim failed
    splash_count        : int
    caught_count        : int
    total_fluid         : int
    elapsed_s           : float
    out_dir             : str
    log                 : str

This component:
  1. Writes the input mesh as ASCII STL into runs/iter_<id>/sculpture.stl
  2. Spawns a subprocess to run_case.py
  3. Parses fitness.json
  4. Returns fitness fields
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

# Adjust PROJECT_ROOT below if you cloned the repo elsewhere -------------------
PROJECT_ROOT = Path(r"C:\Users\user\Documents\LFTH_CFD v2.0")
RUN_CASE = PROJECT_ROOT / "scripts" / "run_case.py"
RUNS_DIR = PROJECT_ROOT / "runs"

# Python executable that has stdlib available — Rhino 8 ships its own CPython3,
# but for subprocess we want a system python (or just call run_case.py with the
# same interpreter the script is running under).
import sys
PYTHON_EXE = sys.executable


# --- helpers ------------------------------------------------------------------
def _mesh_to_ascii_stl(mesh, stl_path: Path, name: str = "sculpture") -> int:
    """Write a Rhino mesh as ASCII STL. Returns triangle count."""
    # Ensure triangulated
    if any(f.IsQuad for f in mesh.Faces):
        mesh = mesh.DuplicateMesh()
        mesh.Faces.ConvertQuadsToTriangles()

    verts = mesh.Vertices
    faces = mesh.Faces
    normals = mesh.FaceNormals
    if normals.Count != faces.Count:
        mesh.FaceNormals.ComputeFaceNormals()
        normals = mesh.FaceNormals

    lines = [f"solid {name}"]
    n_tri = 0
    for i in range(faces.Count):
        f = faces[i]
        n = normals[i]
        a = verts[f.A]
        b = verts[f.B]
        c = verts[f.C]
        lines.append(f"  facet normal {n.X:.6e} {n.Y:.6e} {n.Z:.6e}")
        lines.append("    outer loop")
        lines.append(f"      vertex {a.X:.6e} {a.Y:.6e} {a.Z:.6e}")
        lines.append(f"      vertex {b.X:.6e} {b.Y:.6e} {b.Z:.6e}")
        lines.append(f"      vertex {c.X:.6e} {c.Y:.6e} {c.Z:.6e}")
        lines.append("    endloop")
        lines.append("  endfacet")
        n_tri += 1
    lines.append(f"endsolid {name}")
    stl_path.write_text("\n".join(lines), encoding="ascii")
    return n_tri


def _build_params() -> dict:
    return {
        "sculpture_size": float(sculpture_size) if sculpture_size is not None else 0.5,
        "sculpture_angle": float(sculpture_angle) if sculpture_angle is not None else 0.0,
        "sculpture_height": float(sculpture_height) if sculpture_height is not None else 0.8,
        "nozzle_x": float(nozzle_x) if nozzle_x is not None else 0.0,
        "nozzle_y": float(nozzle_y) if nozzle_y is not None else 0.0,
        "nozzle_z": float(nozzle_z) if nozzle_z is not None else 2.5,
        "nozzle_diameter": float(nozzle_diameter) if nozzle_diameter is not None else 0.03,
        "nozzle_angle_x": float(nozzle_angle_x) if nozzle_angle_x is not None else 0.0,
        "nozzle_angle_y": float(nozzle_angle_y) if nozzle_angle_y is not None else 0.0,
        "flow_velocity": float(flow_velocity) if flow_velocity is not None else 2.0,
    }


# --- main ---------------------------------------------------------------------
fitness = None
splash_count = None
caught_count = None
total_fluid = None
elapsed_s = None
out_dir = None
log = None

if not run:
    log = "run=False — set the Boolean toggle to True to evaluate"
else:
    if mesh is None:
        log = "ERROR: no mesh input"
        fitness = 999.0
    else:
        try:
            iter_id = time.strftime("%Y%m%d_%H%M%S")
            iter_dir = RUNS_DIR / f"iter_{iter_id}"
            iter_dir.mkdir(parents=True, exist_ok=True)
            stl_path = iter_dir / "sculpture.stl"
            n_tri = _mesh_to_ascii_stl(mesh, stl_path)

            params = _build_params()
            params_json = iter_dir / "params.json"
            params_json.write_text(json.dumps(params, indent=2), encoding="utf-8")

            cmd = [
                PYTHON_EXE,
                str(RUN_CASE),
                "--params", str(params_json),
                "--stl", str(stl_path),
                "--iter-id", iter_id,
                "--dp", str(float(dp) if dp is not None else 0.015),
                "--timemax", str(float(timemax) if timemax is not None else 4.0),
            ]
            t0 = time.time()
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            elapsed_s = time.time() - t0

            fitness_json = iter_dir / "fitness.json"
            if fitness_json.exists():
                result = json.loads(fitness_json.read_text(encoding="utf-8"))
                fitness = result.get("splash_ratio", 999.0)
                splash_count = result.get("splash_count")
                caught_count = result.get("caught_count")
                total_fluid = result.get("total_fluid")
                out_dir = result.get("out_dir")
                log = (
                    f"STL triangles: {n_tri}\n"
                    f"subprocess exit: {proc.returncode}\n"
                    f"--- run_case stdout (tail) ---\n{proc.stdout[-1500:]}\n"
                    f"--- fitness.json ---\n"
                    f"splash_ratio={fitness} caught={caught_count} splash={splash_count}\n"
                )
            else:
                fitness = 999.0
                log = (
                    f"FAIL: no fitness.json produced.\n"
                    f"subprocess exit: {proc.returncode}\n"
                    f"stdout: {proc.stdout[-1000:]}\n"
                    f"stderr: {proc.stderr[-1000:]}\n"
                )
        except Exception as e:
            fitness = 999.0
            log = f"EXCEPTION: {type(e).__name__}: {e}"
