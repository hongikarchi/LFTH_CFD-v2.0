"""
Smoke test: run examples/main/01_DamBreak with GPU and report timing.
Verifies DualSPHysics + 5070 Ti GPU build works end-to-end.

Run:
    python smoke_test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DSPH_ROOT = Path(r"C:\Users\user\Downloads\DualSPHysics_v5.4.3\DualSPHysics_v5.4")
DSPH_BIN = DSPH_ROOT / "bin" / "windows"
EXAMPLE_SRC = DSPH_ROOT / "examples" / "main" / "01_DamBreak"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SMOKE_DIR = PROJECT_ROOT / "runs" / "_smoke_dambreak"


def run(cmd, cwd, label, timeout=900):
    print(f"\n=== {label} ===")
    print("CMD:", " ".join(str(c) for c in cmd))
    t0 = time.time()
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd),
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    dt = time.time() - t0
    if proc.stdout:
        print(proc.stdout[-2000:])
    if proc.stderr:
        print("[stderr]", proc.stderr[-1000:])
    print(f"--- {label} exit={proc.returncode} elapsed={dt:.2f}s")
    return proc.returncode, dt


def main():
    if SMOKE_DIR.exists():
        shutil.rmtree(SMOKE_DIR)
    SMOKE_DIR.mkdir(parents=True)

    # Find the case definition XML in the example
    candidate_xmls = list(EXAMPLE_SRC.glob("*_Def.xml"))
    if not candidate_xmls:
        candidate_xmls = list(EXAMPLE_SRC.glob("Case*.xml"))
    if not candidate_xmls:
        print(f"!! No case XML found in {EXAMPLE_SRC}")
        sys.exit(2)
    src_xml = candidate_xmls[0]
    print(f"Using case XML: {src_xml.name}")

    # Copy XML into smoke dir
    smoke_xml = SMOKE_DIR / src_xml.name
    shutil.copyfile(src_xml, smoke_xml)

    # GenCase expects input WITHOUT .xml extension (it appends .xml internally)
    case_def_stub = SMOKE_DIR / src_xml.stem            # ...CaseDambreakVal2D_Def
    case_name = src_xml.stem.replace("_Def", "")        # CaseDambreakVal2D
    out_dir = SMOKE_DIR / f"{case_name}_out"
    data_dir = out_dir / "data"

    # 1. GenCase  --  args: <case_def_stub_no_ext> <out_stub> -save:all
    rc, _ = run(
        [DSPH_BIN / "GenCase_win64.exe", case_def_stub, out_dir / case_name, "-save:all"],
        cwd=SMOKE_DIR, label="GenCase",
    )
    if rc != 0:
        print("FAIL: GenCase")
        sys.exit(1)

    # 2. DualSPHysics GPU  --  args: -gpu <case_stub> <out_dir>
    rc, dt_sim = run(
        [DSPH_BIN / "DualSPHysics5.4_win64.exe", "-gpu", out_dir / case_name, out_dir],
        cwd=SMOKE_DIR, label="DualSPHysics GPU", timeout=900,
    )
    if rc != 0:
        print("FAIL: DualSPHysics GPU")
        sys.exit(1)

    # 3. PartVTK  --  reads from <out>/data/
    rc, _ = run(
        [DSPH_BIN / "PartVTK_win64.exe",
         "-dirdata", data_dir,
         "-savevtk", out_dir / "PartFluid",
         "-onlytype:-all,+fluid"],
        cwd=SMOKE_DIR, label="PartVTK",
    )

    # Summary
    vtks = sorted(out_dir.glob("PartFluid*.vtk"))
    print("\n=== SMOKE TEST SUMMARY ===")
    print(f"Output dir: {out_dir}")
    print(f"VTK frames generated: {len(vtks)}")
    print(f"GPU simulation time: {dt_sim:.2f}s")
    if vtks:
        print(f"First VTK: {vtks[0].name}")
        print(f"Last VTK : {vtks[-1].name}")
    print("\n[OK] DualSPHysics + 5070 Ti pipeline works." if rc == 0 else "[?] Check logs")


if __name__ == "__main__":
    main()
