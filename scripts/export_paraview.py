"""Export sim results to ParaView-friendly VTK series.

DualSPHysics writes binary Part_XXXX.bi4 files. ParaView reads VTK.
This script runs PartVTK_win64.exe on an existing run to produce
PartFluid_XXXX.vtk + PartBound_XXXX.vtk in runs/iter_<test_id>/Out/.

Usage:
    python scripts/export_paraview.py test_41
    python scripts/export_paraview.py test_41 --bound
    python scripts/export_paraview.py all
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
RUNS = PROJECT / "runs"
DSPH_DIR = Path(r"C:\Users\user\Downloads\DualSPHysics_v5.4.3\DualSPHysics_v5.4\bin\windows")
PARTVTK = DSPH_DIR / "PartVTK_win64.exe"


def export_one(test_id: str, with_bound: bool = True) -> bool:
    iter_dir = RUNS / f"iter_{test_id}"
    out_dir = iter_dir / "Out"
    data_dir = out_dir / "data"
    if not data_dir.exists():
        print(f"  SKIP {test_id}: no {data_dir}")
        return False

    # Fluid particles
    rc = subprocess.run([
        str(PARTVTK), "-dirdata", str(data_dir),
        "-savevtk", str(out_dir / "PartFluid"),
        "-onlytype:-all,+fluid",
    ], cwd=iter_dir).returncode
    if rc != 0:
        print(f"  FAIL fluid: {test_id}"); return False

    if with_bound:
        # Boundary particles (sculpture + pond + floor) — one VTK per frame.
        # Useful if you want the geometry to appear "wet" with the same
        # particle look. For most use, loading sculpture_viz.stl is enough.
        rc = subprocess.run([
            str(PARTVTK), "-dirdata", str(data_dir),
            "-savevtk", str(out_dir / "PartBound"),
            "-onlytype:-all,+bound",
        ], cwd=iter_dir).returncode
        if rc != 0:
            print(f"  FAIL bound: {test_id}"); return False

    print(f"  OK {test_id}: {out_dir}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="test_id (e.g. test_41) or 'all'")
    ap.add_argument("--no-bound", action="store_true",
                    help="skip boundary VTK export")
    args = ap.parse_args()

    if args.target == "all":
        ids = sorted(p.name.replace("iter_", "")
                     for p in RUNS.glob("iter_test_*")
                     if (p / "Out" / "data").exists())
    else:
        ids = [args.target]

    print(f"Exporting {len(ids)} run(s) -> VTK")
    n_ok = 0
    for tid in ids:
        if export_one(tid, with_bound=not args.no_bound):
            n_ok += 1
    print(f"\nDone: {n_ok}/{len(ids)} exported.")
    if n_ok:
        first = ids[0]
        print(f"\nOpen in ParaView:")
        print(f"  File > Open > runs/iter_{first}/Out/PartFluid_..vtk")
        print(f"  File > Open > runs/iter_{first}/sculpture_viz.stl")
        print(f"  Click 'Apply', then press Play (top toolbar).")


if __name__ == "__main__":
    main()
