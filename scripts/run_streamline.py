"""
Streamline visualization: brief burst emission + Idp polyline tracing.

Each nozzle emits for ~0.05s, gets ~10 particles. With 5 nozzles -> ~50
unique particle trajectories. PartVTK output includes Idp column;
group frame positions by Idp and connect with polylines.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import time
import re
import struct
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
from run_case import (
    GENCASE, DUALSPH_GPU, DUALSPH_CPU, PARTVTK, PARTVTKOUT,
    TEMPLATE_PATH, RUNS_DIR, build_mapping, render_template, _apply_xml_patches,
    angles_to_velocity_vector,
)

GEOM = PROJECT / "runs" / "_real_geom.json"
STL_PATH = PROJECT / "runs" / "_real_sculpture.stl"


def make_velocity_profile_file(target: Path, burst_v: float, burst_end: float):
    """Write a velocity profile CSV: time;velocity. v=burst_v from 0..burst_end, then 0."""
    lines = [
        "#Time;Velocity",
        "0;{0}".format(burst_v),
        "{0:.6f};{1}".format(burst_end, burst_v),
        "{0:.6f};0.0".format(burst_end + 0.001),
        "999;0.0",
    ]
    target.write_text("\n".join(lines), encoding="utf-8")


def build_burst_inlet_blocks(holes, nozzle_d, thickness, flow_v,
                              tilt_x, tilt_y, profile_filename: str):
    """Like _build_inlet_blocks in run_case but uses imposevelocity mode='1' (file)."""
    vx, vy, vz = angles_to_velocity_vector(flow_v, tilt_x, tilt_y)
    geom = []; ivel = []; iout = []
    for i, (hx, hy, hz) in enumerate(holes, start=1):
        x0 = hx - nozzle_d / 2.0
        y0 = hy - nozzle_d / 2.0
        geom.append(
            f'                    <setmkfluid mk="{i}" />\n'
            f'                    <drawbox>\n'
            f'                        <boxfill>bottom</boxfill>\n'
            f'                        <point x="{x0:.6f}" y="{y0:.6f}" z="{hz:.6f}" />\n'
            f'                        <size x="{nozzle_d:.6f}" y="{nozzle_d:.6f}" z="{thickness:.6f}" />\n'
            f'                    </drawbox>'
        )
        ivel.append(
            f'            <velocity mkfluid="{i}" x="{vx:.6f}" y="{vy:.6f}" z="{vz:.6f}" />'
        )
        # mode="1" uses an external file with (time, value) rows
        iout.append(
            f'                <inoutzone>\n'
            f'                    <refilling value="0" />\n'
            f'                    <inputtreatment value="2" />\n'
            f'                    <layers value="4" />\n'
            f'                    <zone3d>\n'
            f'                        <particles mkfluid="{i}" direction="bottom" />\n'
            f'                    </zone3d>\n'
            f'                    <imposevelocity mode="1">\n'
            f'                        <velocityfile file="{profile_filename}" />\n'
            f'                    </imposevelocity>\n'
            f'                    <imposerhop mode="0" />\n'
            f'                </inoutzone>'
        )
    return ("\n".join(geom), "\n".join(ivel), "\n".join(iout))


def parse_csv_with_idp(csv_path: Path):
    """Return list of (Idp, x, y, z) for a PartVTK -savecsv output."""
    rows = []
    if not csv_path.exists():
        return rows
    with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
        rd = csv.reader(f, delimiter=";")
        idx = {"Idp": None, "Pos.x": None, "Pos.y": None, "Pos.z": None}
        for row in rd:
            if not row:
                continue
            first = row[0].strip()
            if idx["Idp"] is None and (first.startswith("Pos.x") or first.startswith("Idp")):
                for i, c in enumerate(row):
                    c = c.strip()
                    if c.startswith("Idp"): idx["Idp"] = i
                    elif c.startswith("Pos.x"): idx["Pos.x"] = i
                    elif c.startswith("Pos.y"): idx["Pos.y"] = i
                    elif c.startswith("Pos.z"): idx["Pos.z"] = i
                continue
            if idx["Pos.x"] is None:
                continue
            try:
                idp_i = int(float(row[idx["Idp"]]))
                x = float(row[idx["Pos.x"]])
                y = float(row[idx["Pos.y"]])
                z = float(row[idx["Pos.z"]])
                rows.append((idp_i, x, y, z))
            except (ValueError, IndexError):
                continue
    return rows


def main():
    geom = json.loads(GEOM.read_text(encoding="utf-8"))
    holes = geom["nozzle_holes_m"]
    holes = [(h[0], h[1], h[2] - 0.05) for h in holes]

    # Domain
    coll_min = geom["collider_bbox_m"][0]; coll_max = geom["collider_bbox_m"][1]
    pond_min = geom["pond_bbox_m"][0];     pond_max = geom["pond_bbox_m"][1]
    nozzle_center = geom["nozzle_center_m"]
    domain_override = {
        "domain_xmin": min(coll_min[0], pond_min[0]) - 3.0,
        "domain_xmax": max(coll_max[0], pond_max[0]) + 3.0,
        "domain_ymin": min(coll_min[1], pond_min[1]) - 3.0,
        "domain_ymax": max(coll_max[1], pond_max[1]) + 3.0,
        "domain_zmin": pond_min[2] - 1.0,
        "domain_zmax": max(nozzle_center[2], coll_max[2]) + 2.0,
    }
    pond_override = {
        "pond_xmin": pond_min[0], "pond_ymin": pond_min[1],
        "pond_xsize": pond_max[0] - pond_min[0],
        "pond_ysize": pond_max[1] - pond_min[1],
        "pond_thickness": 0.5,
    }

    burst_v_override = float(sys.argv[1]) if len(sys.argv) > 1 else None
    iter_id = sys.argv[2] if len(sys.argv) > 2 else "streamline_v1"
    iter_dir = RUNS_DIR / f"iter_{iter_id}"
    if iter_dir.exists():
        shutil.rmtree(iter_dir)
    iter_dir.mkdir(parents=True)
    out_dir = iter_dir / "Out"; out_dir.mkdir()
    data_dir = out_dir / "data"

    # Velocity profile file: emit for 0.06s then stop
    vp_file = iter_dir / "velprof.dat"
    burst_v = burst_v_override if burst_v_override is not None else 10.0
    burst_end = 0.06
    make_velocity_profile_file(vp_file, burst_v, burst_end)
    print(f"[burst v_inlet = {burst_v} m/s]")

    # Copy STL
    stl_local = iter_dir / "sculpture.stl"
    shutil.copy(STL_PATH, stl_local)

    # Build inlet blocks (burst mode)
    nozzle_d = 0.15
    thickness = 0.05
    g_blk, iv_blk, io_blk = build_burst_inlet_blocks(
        holes, nozzle_d, thickness, burst_v, 0.0, 0.0, vp_file.name
    )

    # Render template
    params = {
        "sculpture_size": 1.0, "sculpture_angle": 0.0, "sculpture_height": 0.0,
        "nozzle_x": holes[0][0], "nozzle_y": holes[0][1], "nozzle_z": holes[0][2],
        "nozzle_diameter": nozzle_d, "nozzle_angle_x": 0.0, "nozzle_angle_y": 0.0,
        "flow_velocity": burst_v,
    }
    dp = 0.10
    timemax = 5.0
    timeout = 0.05
    mapping = build_mapping(params, stl_local, dp, timemax, timeout,
                             domain_override=domain_override, pond_override=pond_override)
    # Override the inlet blocks built by build_mapping (it generated mode=0 versions)
    mapping["INLET_GEOMETRY"] = g_blk
    mapping["INLET_INITIAL_VELOCITIES"] = iv_blk
    mapping["INLET_INOUTZONES"] = io_blk

    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    text = render_template(text, mapping)
    text = _apply_xml_patches(text, {
        "speedsound": 250, "cflnumber": 0.30, "DensityDT": 1, "Visco": 0.05,
    }, inlet_layers=4)

    case_def_xml = iter_dir / "case_Def.xml"
    case_def_xml.write_text(text, encoding="utf-8")
    case_def_stub = iter_dir / "case_Def"
    case_stub = out_dir / "case"

    def runproc(cmd, label, timeout_s=600):
        t0 = time.time()
        p = subprocess.run([str(c) for c in cmd], cwd=str(iter_dir),
                            capture_output=True, text=True, timeout=timeout_s)
        dt = time.time() - t0
        if p.returncode != 0:
            print(f"[{label}] FAIL ({dt:.1f}s)")
            print(p.stdout[-800:])
            print(p.stderr[-400:])
        else:
            print(f"[{label}] OK ({dt:.1f}s)")
        return p.returncode, dt

    rc, _ = runproc([GENCASE, case_def_stub, case_stub, "-save:all"], "GenCase")
    if rc != 0: return

    rc, t_sim = runproc([DUALSPH_CPU, "-cpu", case_stub, out_dir], "DualSPHysics CPU", timeout_s=600)
    if rc != 0: return
    print(f"sim wall: {t_sim:.1f}s")

    # PartVTK with CSV (Idp included)
    rc, _ = runproc([PARTVTK, "-dirdata", data_dir, "-savecsv",
                     out_dir / "PartFluid", "-onlytype:-all,+fluid"], "PartVTK CSV")

    # Parse Idp-tagged trajectories
    csvs = sorted(out_dir.glob("PartFluid_*.csv"))
    trails: dict[int, list[tuple[float, float, float]]] = {}
    for ci, csv_p in enumerate(csvs):
        rows = parse_csv_with_idp(csv_p)
        for idp, x, y, z in rows:
            trails.setdefault(idp, []).append((x, y, z))

    trails_json = iter_dir / "trails.json"
    trails_data = {str(k): v for k, v in trails.items()}
    trails_json.write_text(json.dumps(trails_data), encoding="utf-8")
    print(f"Unique trajectories: {len(trails)}")
    print(f"Trails JSON: {trails_json}")
    print(f"Sample lengths: {sorted([len(v) for v in trails.values()], reverse=True)[:10]}")


if __name__ == "__main__":
    main()
