"""
Sweep DualSPHysics settings to find a configuration that runs a 10s
simulation in < 10s of wall time. Uses cube STL placeholder.

Strategy: run a short benchmark (timemax_short) for each config,
then extrapolate linearly to a 10s target.

Run:
    python benchmark.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
from run_case import (
    GENCASE, DUALSPH_GPU, PARTVTK, PARTVTKOUT,
    TEMPLATE_PATH, RUNS_DIR, build_mapping, render_template,
)
from integration_test import write_cube_stl
from fitness import compute_fitness_from_vtk

CUBE_STL = PROJECT / "runs" / "_cube_test.stl"

# Short benchmark sim time. Wall time extrapolated to 10s linearly.
BENCH_TIMEMAX = float(os.environ.get("BENCH_TIMEMAX", 2.0))
TARGET_SIM = float(os.environ.get("BENCH_TARGET", 10.0))
TIMEOUT_SAVE = float(os.environ.get("BENCH_TIMEOUT_SAVE", 0.10))

PARAMS = {
    "sculpture_size": 0.6,
    "sculpture_angle": 15.0,
    "sculpture_height": 0.7,
    "nozzle_x": 0.0,
    "nozzle_y": 0.0,
    "nozzle_z": 2.0,
    "nozzle_diameter": 0.04,
    "nozzle_angle_x": 0.0,
    "nozzle_angle_y": 0.0,
    "flow_velocity": 3.0,
}


def patch_xml(text, patches):
    """Apply key=value patches to the XML by replacing <parameter key=...> values."""
    out = text
    for key, val in patches.items():
        # Match <parameter key="K" value="V"...
        import re
        pattern = r'(<parameter key="' + re.escape(key) + r'" value=")[^"]*(")'
        out, n = re.subn(pattern, r'\g<1>' + str(val) + r'\g<2>', out)
        if n == 0 and key in ("speedsound", "cflnumber"):
            # constantsdef forms: <speedsound value="..."/>, <cflnumber value="..."/>
            pattern2 = r'(<' + key + r'\s+value=")[^"]*(")'
            out, n2 = re.subn(pattern2, r'\g<1>' + str(val) + r'\g<2>', out)
    return out


def run_bench(label, dp, patches=None, inlet_layers=None, use_cpu=False, step_alg=None):
    """Run one benchmark configuration. Returns dict with timing + fitness."""
    iter_id = "bench_" + label.replace(".", "p").replace(" ", "_").replace("=", "")
    iter_dir = RUNS_DIR / ("iter_" + iter_id)
    if iter_dir.exists():
        shutil.rmtree(iter_dir)
    iter_dir.mkdir(parents=True)
    out_dir = iter_dir / "Out"
    out_dir.mkdir()
    data_dir = out_dir / "data"

    stl_local = iter_dir / "sculpture.stl"
    shutil.copyfile(CUBE_STL, stl_local)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    mapping = build_mapping(PARAMS, stl_local, dp, BENCH_TIMEMAX, TIMEOUT_SAVE)
    xml_text = render_template(template, mapping)
    if patches:
        xml_text = patch_xml(xml_text, patches)
    if inlet_layers is not None:
        xml_text = xml_text.replace('<layers value="4"', f'<layers value="{inlet_layers}"')
    if step_alg is not None:
        xml_text = patch_xml(xml_text, {"StepAlgorithm": step_alg})

    case_def_xml = iter_dir / "case_Def.xml"
    case_def_xml.write_text(xml_text, encoding="utf-8")
    case_def_stub = iter_dir / "case_Def"
    case_stub = out_dir / "case"

    def _run(cmd, label):
        return subprocess.run([str(c) for c in cmd], cwd=str(iter_dir),
                              capture_output=True, text=True, timeout=900)

    # GenCase
    t0 = time.time()
    r = _run([GENCASE, case_def_stub, case_stub, "-save:all"], "GenCase")
    t_gen = time.time() - t0
    if r.returncode != 0:
        return {"label": label, "ok": False, "stage": "GenCase",
                "err": r.stdout[-500:] + r.stderr[-500:]}

    # DualSPHysics GPU or CPU
    from run_case import DUALSPH_CPU
    solver = DUALSPH_CPU if use_cpu else DUALSPH_GPU
    flag = "-cpu" if use_cpu else "-gpu"
    t0 = time.time()
    r = _run([solver, flag, case_stub, out_dir], "DSPH")
    t_sim = time.time() - t0
    if r.returncode != 0:
        return {"label": label, "ok": False, "stage": "DSPH",
                "err": r.stdout[-500:] + r.stderr[-500:], "t_sim": t_sim}

    # PartVTK (fluid frames)
    t0 = time.time()
    _run([PARTVTK, "-dirdata", data_dir, "-savevtk", out_dir / "PartFluid",
          "-onlytype:-all,+fluid"], "PartVTK")
    t_pv = time.time() - t0

    # PartVTKOut (splash csv)
    splash_csv = iter_dir / "splash_out"
    _run([PARTVTKOUT, "-dirdata", data_dir, "-savecsv", splash_csv,
          "-SaveResume", iter_dir / "_ResumeOut"], "PartVTKOut")

    # Fitness
    fluid_vtks = sorted(out_dir.glob("PartFluid_*.vtk"))
    last_vtk = fluid_vtks[-1] if fluid_vtks else None
    sp_csv = None
    for p in iter_dir.glob("splash_out*.csv"):
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(2048)
        if "Pos.x" in head:
            sp_csv = p
            break
    fit = compute_fitness_from_vtk(
        last_fluid_vtk=last_vtk, splash_csv=sp_csv,
        pond_aabb=(-0.4, -0.4, 0.4, 0.4),
        pond_top_z=0.32,
    )

    # Particle count peak (last frame)
    last_count = fit["total_fluid"]
    extrapolated = t_sim * (TARGET_SIM / BENCH_TIMEMAX)

    return {
        "label": label,
        "ok": True,
        "dp": dp,
        "t_sim_bench_s": round(t_sim, 2),
        "t_gen_s": round(t_gen, 2),
        "t_postproc_s": round(t_pv, 2),
        "extrapolated_for_10s_sim": round(extrapolated, 2),
        "particles_last": last_count,
        "splash_ratio": round(fit["splash_ratio"], 3),
        "patches": patches or {},
        "inlet_layers": inlet_layers,
    }


def main():
    if not CUBE_STL.exists():
        # ensure cube STL exists
        write_cube_stl(CUBE_STL, size=0.5)
        print(f"Created cube STL: {CUBE_STL}")

    print("== Benchmark - {0}s sim, extrapolated to {1}s ==\n".format(BENCH_TIMEMAX, TARGET_SIM))

    # CPU at the speed/accuracy sweet spot + repeat baseline for ranking-stability check
    configs = [
        ("dp0.035_CPU_ALL",      0.035, {"cflnumber": 0.35, "speedsound": 60, "DensityDT": 1}, 2, True,  None),
        ("dp0.040_CPU_ALL",      0.040, {"cflnumber": 0.35, "speedsound": 60, "DensityDT": 1}, 2, True,  None),
        ("dp0.040_CPU_extreme",  0.040, {"cflnumber": 0.45, "speedsound": 40, "DensityDT": 0}, 2, True,  None),
        ("dp0.045_CPU_extreme",  0.045, {"cflnumber": 0.45, "speedsound": 40, "DensityDT": 0}, 2, True,  None),
    ]

    results = []
    for cfg in configs:
        label, dp, patches, layers, use_cpu, step_alg = cfg
        print(f"running {label} ...", end=" ", flush=True)
        try:
            r = run_bench(label, dp, patches, layers, use_cpu=use_cpu, step_alg=step_alg)
        except Exception as e:
            r = {"label": label, "ok": False, "err": str(e)}
        if r.get("ok"):
            print(f"sim={r['t_sim_bench_s']}s  extrap10s={r['extrapolated_for_10s_sim']}s  "
                  f"N={r['particles_last']}  splash={r['splash_ratio']}")
        else:
            print(f"FAIL ({r.get('stage', '?')}): {r.get('err', '')[:120]}")
        results.append(r)

    out_json = PROJECT / "runs" / "benchmark_results.json"
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out_json}")

    # Summary table
    print("\n=== SUMMARY ===")
    print(f"{'label':36s} {'10s_wall':>10s} {'speedup':>9s} {'N_last':>8s} {'splash':>7s}")
    baseline = next((r for r in results if r.get("label") == "baseline_dp0.015" and r.get("ok")), None)
    base_t = baseline["extrapolated_for_10s_sim"] if baseline else None
    for r in results:
        if not r.get("ok"):
            print(f"{r['label']:36s}   FAIL")
            continue
        spd = (base_t / r["extrapolated_for_10s_sim"]) if base_t else 0.0
        flag = "  <-- meets <10s goal" if r["extrapolated_for_10s_sim"] < 10 else ""
        print(f"{r['label']:36s} {r['extrapolated_for_10s_sim']:10.2f} {spd:8.2f}x "
              f"{r['particles_last']:8d} {r['splash_ratio']:7.3f}{flag}")


if __name__ == "__main__":
    main()
