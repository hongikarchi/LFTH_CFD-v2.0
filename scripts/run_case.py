"""
LFTH_CFD v2.0 — run a single DualSPHysics evaluation.

Usage (CLI):
    python run_case.py --params params.json [--dp 0.015] [--timemax 4.0]

Usage (import):
    from run_case import evaluate
    result = evaluate(params_dict, stl_path, iter_id="0001", dp=0.015)

`params` keys (all SI units, deg for angles):
    sculpture_size, sculpture_angle, sculpture_height
    nozzle_x, nozzle_y, nozzle_z, nozzle_diameter
    nozzle_angle_x, nozzle_angle_y
    flow_velocity

Returns dict:
    {
        "iter_id": str,
        "splash_count": int,
        "total_fluid": int,
        "splash_ratio": float,    # fitness — minimize
        "elapsed_s": float,
        "out_dir": str,
        "case_xml": str,
        "log": str
    }
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- paths --------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "case_sculpture_template.xml"
RUNS_DIR = PROJECT_ROOT / "runs"

DSPH_DIR = Path(r"C:\Users\user\Downloads\DualSPHysics_v5.4.3\DualSPHysics_v5.4\bin\windows")
GENCASE = DSPH_DIR / "GenCase_win64.exe"
DUALSPH_GPU = DSPH_DIR / "DualSPHysics5.4_win64.exe"
DUALSPH_CPU = DSPH_DIR / "DualSPHysics5.4CPU_win64.exe"
PARTVTK = DSPH_DIR / "PartVTK_win64.exe"
PARTVTKOUT = DSPH_DIR / "PartVTKOut_win64.exe"

# --- defaults -----------------------------------------------------------------
DEFAULT_DOMAIN = {
    "domain_xmin": -1.0, "domain_xmax": 1.0,
    "domain_ymin": -1.0, "domain_ymax": 1.0,
    "domain_zmin": 0.0,  "domain_zmax": 3.0,
}
DEFAULT_POND = {
    "pond_xmin": -0.4, "pond_ymin": -0.4,
    "pond_xsize": 0.8, "pond_ysize": 0.8,
    "pond_thickness": 0.02,
}
NOZZLE_THICKNESS = 0.01  # vertical thickness of inlet box
DEFAULT_NOZZLE_Z = 2.5


# --- helpers ------------------------------------------------------------------
def angles_to_velocity_vector(magnitude: float,
                              tilt_x_deg: float,
                              tilt_y_deg: float) -> tuple[float, float, float]:
    """
    Convert (magnitude, tilt_x, tilt_y) to a Cartesian velocity vector.
    Base direction: -Z (straight down).
    tilt_x: rotation about X axis (positive tilts toward +Y).
    tilt_y: rotation about Y axis (positive tilts toward +X).
    """
    tx = math.radians(tilt_x_deg)
    ty = math.radians(tilt_y_deg)
    # Starting from (0,0,-1), apply Rx then Ry.
    # Rx: rotates -Z -> (0, sin(tx), -cos(tx))
    # Then Ry: rotates that (x,y,z) -> (z*sin(ty)+x*cos(ty), y, z*cos(ty)-x*sin(ty))
    x0, y0, z0 = 0.0, math.sin(tx), -math.cos(tx)
    x1 = z0 * math.sin(ty) + x0 * math.cos(ty)
    y1 = y0
    z1 = z0 * math.cos(ty) - x0 * math.sin(ty)
    return (magnitude * x1, magnitude * y1, magnitude * z1)


def render_template(template_text: str, mapping: dict) -> str:
    """Replace {{var}} placeholders with str(mapping[var]). Missing -> KeyError."""
    out = template_text
    for key, val in mapping.items():
        placeholder = "{{" + key + "}}"
        out = out.replace(placeholder, str(val))
    # Sanity check: any leftover {{...}} -> raise
    if "{{" in out:
        unfilled = []
        i = 0
        while True:
            i = out.find("{{", i)
            if i == -1:
                break
            j = out.find("}}", i)
            unfilled.append(out[i + 2:j])
            i = j + 2
        raise KeyError(f"Unfilled placeholders: {unfilled}")
    return out


def build_mapping(params: dict, stl_path: Path, dp: float,
                  timemax: float, timeout: float) -> dict:
    """Compose the placeholder mapping from user params + defaults."""
    nozzle_x = float(params["nozzle_x"])
    nozzle_y = float(params["nozzle_y"])
    nozzle_z = float(params.get("nozzle_z", DEFAULT_NOZZLE_Z))
    nozzle_d = float(params["nozzle_diameter"])
    flow_v = float(params["flow_velocity"])
    tilt_x = float(params.get("nozzle_angle_x", 0.0))
    tilt_y = float(params.get("nozzle_angle_y", 0.0))
    vx, vy, vz = angles_to_velocity_vector(flow_v, tilt_x, tilt_y)

    domain = DEFAULT_DOMAIN.copy()
    pond = DEFAULT_POND.copy()

    mapping = {
        "dp": dp,
        "timemax": timemax,
        "timeout": timeout,
        "stl_path": str(stl_path).replace("\\", "/"),
        "stl_scale": float(params["sculpture_size"]),
        "stl_rotate_z_deg": float(params["sculpture_angle"]),
        "stl_move_z": float(params["sculpture_height"]),
        "nozzle_x": nozzle_x,
        "nozzle_y": nozzle_y,
        "nozzle_z": nozzle_z,
        "nozzle_diameter": nozzle_d,
        "nozzle_thickness": NOZZLE_THICKNESS,
        "nozzle_x_minus_half": nozzle_x - nozzle_d / 2.0,
        "nozzle_y_minus_half": nozzle_y - nozzle_d / 2.0,
        "nozzle_angle_x_deg": tilt_x,
        "nozzle_angle_y_deg": tilt_y,
        "flow_velocity": flow_v,
        "vel_x": vx,
        "vel_y": vy,
        "vel_z": vz,
        "domain_xsize": domain["domain_xmax"] - domain["domain_xmin"],
        "domain_ysize": domain["domain_ymax"] - domain["domain_ymin"],
    }
    mapping.update(domain)
    mapping.update(pond)
    return mapping


# --- main evaluate ------------------------------------------------------------
def evaluate(params: dict,
             stl_path: str | os.PathLike,
             iter_id: str | None = None,
             dp: float = 0.015,
             timemax: float = 4.0,
             timeout: float = 0.05,
             use_gpu: bool = True,
             keep_outputs: bool = False) -> dict:
    """Run one DualSPHysics simulation and return fitness."""
    t0 = time.time()
    if iter_id is None:
        iter_id = time.strftime("%Y%m%d_%H%M%S")
    iter_dir = RUNS_DIR / f"iter_{iter_id}"
    out_dir = iter_dir / "Out"
    iter_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy STL into iter_dir for reproducibility
    stl_src = Path(stl_path)
    if not stl_src.exists():
        raise FileNotFoundError(f"STL not found: {stl_src}")
    stl_local = iter_dir / "sculpture.stl"
    shutil.copyfile(stl_src, stl_local)

    # 2. Render XML template
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    mapping = build_mapping(params, stl_local, dp, timemax, timeout)
    case_xml_text = render_template(template_text, mapping)
    # GenCase expects input WITHOUT .xml AND filename must end with _Def
    case_def_xml = iter_dir / "case_Def.xml"
    case_def_xml.write_text(case_xml_text, encoding="utf-8")
    case_def_stub = iter_dir / "case_Def"   # GenCase appends .xml
    case_stub = out_dir / "case"
    data_dir = out_dir / "data"

    log_lines = [f"[run_case] iter_id={iter_id}", f"[run_case] params={json.dumps(params)}"]

    # 3. GenCase  --  args: <case_def_stub> <out_stub> -save:all
    rc = _run([str(GENCASE), str(case_def_stub), str(case_stub), "-save:all"],
              cwd=iter_dir, log=log_lines, label="GenCase")
    if rc != 0:
        return _failed(iter_id, iter_dir, case_def_xml, log_lines, t0, "GenCase failed")

    # 4. DualSPHysics  --  args: -gpu/-cpu <case_stub> <out_dir>
    solver = DUALSPH_GPU if use_gpu else DUALSPH_CPU
    flag = "-gpu" if use_gpu else "-cpu"
    rc = _run([str(solver), flag, str(case_stub), str(out_dir)],
              cwd=iter_dir, log=log_lines, label="DualSPHysics", timeout_s=900)
    if rc != 0:
        return _failed(iter_id, iter_dir, case_def_xml, log_lines, t0, "DualSPHysics failed")

    # 5. PartVTKOut — out-of-domain particles (reads from <out>/data/)
    splash_csv_stub = iter_dir / "splash_out"
    _run([str(PARTVTKOUT), "-dirdata", str(data_dir),
          "-savecsv", str(splash_csv_stub),
          "-SaveResume", str(iter_dir / "_ResumeOut")],
         cwd=iter_dir, log=log_lines, label="PartVTKOut")

    # 6. PartVTK — fluid particles, all frames (also enables Rhino visualization)
    _run([str(PARTVTK), "-dirdata", str(data_dir),
          "-savevtk", str(out_dir / "PartFluid"),
          "-onlytype:-all,+fluid"],
         cwd=iter_dir, log=log_lines, label="PartVTK")

    # 7. Fitness — parse last fluid VTK + splash CSV
    from fitness import compute_fitness_from_vtk  # local module
    fluid_vtks = sorted(out_dir.glob("PartFluid_*.vtk"))
    last_fluid_vtk = fluid_vtks[-1] if fluid_vtks else None
    splash_csv = _find_particle_csv(iter_dir, "splash_out")
    fit = compute_fitness_from_vtk(
        last_fluid_vtk=last_fluid_vtk,
        splash_csv=splash_csv,
        pond_aabb=(
            DEFAULT_POND["pond_xmin"],
            DEFAULT_POND["pond_ymin"],
            DEFAULT_POND["pond_xmin"] + DEFAULT_POND["pond_xsize"],
            DEFAULT_POND["pond_ymin"] + DEFAULT_POND["pond_ysize"],
        ),
        pond_top_z=DEFAULT_POND["pond_thickness"] + 0.30,
    )

    elapsed = time.time() - t0
    result = {
        "iter_id": iter_id,
        "params": params,
        "splash_count": fit["splash_count"],
        "caught_count": fit["caught_count"],
        "total_fluid": fit["total_fluid"],
        "splash_ratio": fit["splash_ratio"],
        "elapsed_s": elapsed,
        "out_dir": str(out_dir),
        "case_xml": str(case_def_xml),
        "log": "\n".join(log_lines),
        "ok": True,
    }

    fitness_json = iter_dir / "fitness.json"
    fitness_json.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if not keep_outputs:
        # Keep VTK frames? For GA, optional. By default keep — visualization needs them.
        pass

    return result


def _find_particle_csv(iter_dir: Path, stem: str) -> Path | None:
    """Find the CSV with actual particle data (has Pos.x header), skipping stats CSVs."""
    candidates = sorted(iter_dir.glob(f"{stem}*.csv"))
    for c in candidates:
        try:
            with c.open("r", encoding="utf-8", errors="ignore") as f:
                head = f.read(2048)
            if "Pos.x" in head:
                return c
        except OSError:
            continue
    return candidates[0] if candidates else None


def _run(cmd: list[str], cwd: Path, log: list[str], label: str,
         timeout_s: int = 1800) -> int:
    log.append(f"[run_case] >>> {label}: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        log.append(f"[run_case] {label} TIMEOUT after {timeout_s}s")
        return 124
    if proc.stdout:
        log.append(proc.stdout.strip())
    if proc.stderr:
        log.append("[stderr] " + proc.stderr.strip())
    log.append(f"[run_case] <<< {label} exit={proc.returncode}")
    return proc.returncode


def _failed(iter_id, iter_dir, case_xml, log_lines, t0, reason):
    elapsed = time.time() - t0
    log_lines.append(f"[run_case] FAILED: {reason}")
    err_log = iter_dir / "fitness.json"
    result = {
        "iter_id": iter_id,
        "ok": False,
        "reason": reason,
        "splash_ratio": 999.0,  # GA penalty
        "elapsed_s": elapsed,
        "case_xml": str(case_xml),
        "log": "\n".join(log_lines),
    }
    err_log.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--params", required=True, help="JSON file with param dict")
    parser.add_argument("--stl", required=True, help="Path to sculpture STL")
    parser.add_argument("--iter-id", default=None)
    parser.add_argument("--dp", type=float, default=0.015)
    parser.add_argument("--timemax", type=float, default=4.0)
    parser.add_argument("--timeout", type=float, default=0.05)
    parser.add_argument("--cpu", action="store_true", help="Use CPU solver instead of GPU")
    args = parser.parse_args()

    params = json.loads(Path(args.params).read_text(encoding="utf-8"))
    result = evaluate(
        params=params,
        stl_path=args.stl,
        iter_id=args.iter_id,
        dp=args.dp,
        timemax=args.timemax,
        timeout=args.timeout,
        use_gpu=not args.cpu,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "log"}, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
