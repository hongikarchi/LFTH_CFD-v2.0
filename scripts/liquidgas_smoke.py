"""
Verify the LiquidGas multi-phase solver works on this machine, end-to-end.
Copies the ObstacleImpact example into our runs/ tree and runs it.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DSPH = Path(r"C:\Users\user\Downloads\DualSPHysics_v5.4.3\DualSPHysics_v5.4")
DSPH_BIN = DSPH / "bin" / "windows"
EX = DSPH / "examples" / "mphase_liquidgas" / "02_ObstacleImpact"

WORK = Path(r"C:\Temp\liquidgas_smoke")


def run(cmd, cwd, label, timeout=900):
    print(f"\n=== {label} ===")
    t0 = time.time()
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    dt = time.time() - t0
    if p.stdout:
        print(p.stdout[-1500:])
    if p.stderr:
        print("[stderr]", p.stderr[-500:])
    print(f"--- {label} exit={p.returncode} elapsed={dt:.2f}s")
    return p.returncode, dt


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    case_def_src = EX / "CaseObstacleImpact_LiquidGas_Def.xml"
    case_def_local = WORK / case_def_src.name
    shutil.copy(case_def_src, case_def_local)
    # also copy any aux files
    for aux in EX.glob("*.txt"):
        shutil.copy(aux, WORK / aux.name)

    name = "CaseObstacleImpact_LiquidGas"
    out_dir = WORK / f"{name}_out"
    out_dir.mkdir()
    data_dir = out_dir / "data"

    # 1. GenCase
    rc, _ = run([DSPH_BIN / "GenCase_win64.exe", WORK / f"{name}_Def", out_dir / name, "-save:all"],
                cwd=WORK, label="GenCase")
    if rc != 0:
        sys.exit(1)

    # 2. DualSPHysics 4.0 LiquidGas GPU
    solver = DSPH_BIN / "DualSPHysics4.0_LiquidGas_win64.exe"
    rc, dt_sim = run([solver, "-gpu", out_dir / name, out_dir],
                     cwd=WORK, label="DualSPHysics LiquidGas GPU", timeout=1800)
    if rc != 0:
        print("GPU failed, trying CPU...")
        solver_cpu = DSPH_BIN / "DualSPHysics4.0_LiquidGasCPU_win64.exe"
        rc, dt_sim = run([solver_cpu, "-cpu", out_dir / name, out_dir],
                         cwd=WORK, label="DualSPHysics LiquidGas CPU", timeout=1800)
        if rc != 0:
            sys.exit(1)

    # 3. PartVTK fluid
    rc, _ = run([DSPH_BIN / "PartVTK_win64.exe",
                 "-dirdata", data_dir,
                 "-savevtk", out_dir / "PartFluid",
                 "-onlytype:-all,+fluid"],
                cwd=WORK, label="PartVTK", timeout=120)

    vtks = sorted(out_dir.glob("PartFluid*.vtk"))
    print("\n=== SMOKE SUMMARY ===")
    print(f"Output dir: {out_dir}")
    print(f"VTK frames: {len(vtks)}")
    print(f"Sim wall:   {dt_sim:.2f}s")
    if vtks:
        print(f"First/last: {vtks[0].name} / {vtks[-1].name}")
    print("\n[OK] LiquidGas pipeline works." if rc == 0 else "[?] check logs")


if __name__ == "__main__":
    main()
