"""
Run one experiment defined by experiments/test_NN.json.

Pipeline (parametric module version):
  1. Read params (4 modules, 8 genes each: radius/move_z/rotation_x/rotation_z/
     offset_dist/tx/ty/tz).
  2. Build each module mesh in Python via module_geometry.build_module_mesh,
     placed at base_point_mm + (tx, ty, tz). Combine 4 meshes -> single STL in m.
  3. Render case_Def.xml with multi-nozzle inlet (one block per nozzle in
     runs/_real_geom.json) + velocity profile.
  4. GenCase + DualSPHysics CPU + PartVTK CSV.
  5. Build trails.json + compute fitness (catch_rate + touch metric) -> result.json.
  6. Push combined STL into Rhino layer test_NN/collider_NN (visual check).
  7. Push streamlines into Rhino layer test_NN/stream_nozzle_*.
  8. Regenerate experiments.html dashboard.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from run_case import (
    GENCASE, DUALSPH_CPU, DUALSPH_GPU, PARTVTK, PARTVTKOUT,
    TEMPLATE_PATH, RUNS_DIR, build_mapping, render_template, _apply_xml_patches,
    angles_to_velocity_vector,
)
from run_streamline import (
    make_velocity_profile_file, build_burst_inlet_blocks,
    parse_csv_with_idp,
)
from module_geometry import (build_modules_combined_stl,
                               build_module_fill_particles,
                               GENE_ORDER, DEFAULTS)


def build_initial_particles_block(holes, flow_v: float):
    """Option D: place ONE fluid particle at each hole position with initial
    downward velocity. NO inlet zone, NO refilling — these particles exist at
    t=0 and propagate ballistically (subject to gravity + boundary interaction).

    Each particle uses mkfluid="1". `<drawpoints>` lets us specify exact (x,y,z)
    positions free of dp-grid snap (unlike `<drawbox>`).
    Initial velocity is set via `<initials>` block in case_Def with vel mk="1".
    """
    from run_case import angles_to_velocity_vector
    vx, vy, vz = angles_to_velocity_vector(flow_v, 0.0, 0.0)
    geom_lines = ['                    <setmkfluid mk="1" />']
    geom_lines.append('                    <drawpoints>')
    for (hx, hy, hz) in holes:
        geom_lines.append(
            f'                        <point x="{hx:.6f}" y="{hy:.6f}" z="{hz:.6f}" />'
        )
    geom_lines.append('                    </drawpoints>')
    g_blk = "\n".join(geom_lines)
    iv_blk = f'            <velocity mkfluid="1" x="{vx:.6f}" y="{vy:.6f}" z="{vz:.6f}" />'
    # No inoutzone — particles are pure initial state. Return empty inoutzone block.
    io_blk = ""
    return g_blk, iv_blk, io_blk

HOST, PORT = "127.0.0.1", 1999
MODULES_JSON = PROJECT / "runs" / "_collider_modules.json"
GEOM_JSON = PROJECT / "runs" / "_real_geom.json"
EXPERIMENTS_DIR = PROJECT / "experiments"

# --- defaults -----------------------------------------------------------------
SIM_DEFAULTS = {
    "v_inlet": 0.0,
    "burst_end": 0.5,
    "dp": 0.20,                # 230 surfaces -> ~58 unique on dp-grid. Coarse but
                               # ~16 s/eval on GPU -> 128 evals fit in ~36 min.
    "timemax": 5.0,            # particles reach pond by ~3 s; 5 s leaves margin
    "timeout": 0.05,           # PART save interval = 20 fps so polylines look smooth
    "nozzle_diameter": 0.20,
    "nozzle_thickness": 0.20,
    "speedsound": 120,        # stiff fluid (250) caused trampoline rebound
                              # off prefill water surfaces. 120 is still
                              # safely > 10*v_max (~10 m/s terminal) and
                              # absorbs impact shocks instead of reflecting.
    "cflnumber": 0.30,
    "DensityDT": 1,
    "Visco": 0.50,
    "viscobound": 2.0,
    "use_gpu": True,
    "timepart": 0.10,
}


def mcp_call(code: str, timeout: float = 120.0, retries: int = 2) -> dict:
    payload = json.dumps({"type": "execute_rhinoscript_python_code",
                          "params": {"code": code}}).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            with socket.create_connection((HOST, PORT), timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall(payload)
                buf = b""
                while True:
                    try:
                        c = s.recv(65536)
                    except socket.timeout:
                        break
                    if not c:
                        break
                    buf += c
                    try:
                        return json.loads(buf.decode("utf-8", "ignore"))
                    except json.JSONDecodeError:
                        continue
            return {"error": "no json"}
        except (ConnectionResetError, ConnectionRefusedError,
                ConnectionAbortedError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 + attempt * 2)   # 2s, 4s backoff
                continue
            return {"error": f"mcp connection failed: {e}"}
    return {"error": f"mcp call failed: {last_err}"}


# --- step 2: build combined parametric mesh STL -------------------------------
def build_sculpture_stl(modules_info: list, genes_by_index: dict,
                        stl_out: Path) -> dict:
    """Returns dict with stl_path, total_verts, total_tris, per_module list,
    plus bbox_m of the combined mesh."""
    info = build_modules_combined_stl(modules_info, genes_by_index, stl_out)
    m = trimesh.load(stl_out)
    info["bbox_m"] = m.bounds.tolist()
    return info


# --- step 6: push STL into Rhino layer ----------------------------------------
def push_stl_to_rhino_layer(stl_path: Path, layer_full_path: str,
                            color_rgb: tuple,
                            offset_mm: tuple = (0.0, 0.0, 0.0),
                            obj_name: str = "") -> None:
    """Import STL (in METERS) into Rhino layer. Scales m->mm, translates by
    offset_mm. Layer is created via the dedicated helper first, then the
    import call looks it up by FullPath."""
    a = 255
    argb = (a, color_rgb[0], color_rgb[1], color_rgb[2])
    lid = _ensure_layer_via_mcp(layer_full_path, argb)
    if lid < 0:
        print(f"WARN: could not create layer {layer_full_path}")
        return

    stl_esc = str(stl_path).replace("\\", "\\\\")
    ox, oy, oz = offset_mm
    code = f'''
import Rhino, System, scriptcontext as sc
doc = sc.doc
doc.Objects.UnselectAll()
lid = doc.Layers.FindByFullPath("{layer_full_path}", -1)
if lid < 0:
    raise Exception("layer missing: {layer_full_path}")

# Purge prior objects on this layer matching this obj_name (so re-runs of
# the same cell replace, rather than stack).
obj_name = "{obj_name}"
purged = 0
if obj_name:
    for o in list(doc.Objects):
        if o.Attributes.LayerIndex == lid and o.Name == obj_name:
            doc.Objects.Delete(o, True); purged += 1
doc.Objects.UnselectAll()

prev_current = doc.Layers.CurrentLayerIndex
doc.Layers.SetCurrentLayerIndex(lid, True)
try:
    ok = Rhino.RhinoApp.RunScript('_-Import "{stl_esc}" _Enter', False)
finally:
    doc.Layers.SetCurrentLayerIndex(prev_current, True)

sel = list(doc.Objects.GetSelectedObjects(False, False))
scale = Rhino.Geometry.Transform.Scale(Rhino.Geometry.Point3d(0, 0, 0), 1000.0)
trans = Rhino.Geometry.Transform.Translation({ox}, {oy}, {oz})
xform = trans * scale
for o in sel:
    doc.Objects.Transform(o, xform, True)
    if obj_name:
        o.Attributes.Name = obj_name
        o.CommitChanges()
doc.Objects.UnselectAll()
doc.Views.Redraw()
print("imported " + obj_name + " sel=" + str(len(sel)) + " purged=" + str(purged) + " lid=" + str(lid))
'''
    r = mcp_call(code, timeout=120)
    if r.get("status") != "success":
        print(f"WARN: push STL {layer_full_path}:", json.dumps(r)[:200])
    else:
        out = r.get("result", {}).get("output", "")
        if "layer missing" in out:
            print(f"WARN: layer lost for {layer_full_path}")
        else:
            # Find "imported ... sel=N" line and confirm
            for line in out.splitlines():
                if line.startswith("imported"):
                    print(f"  STL: {line}")
                    break
            else:
                msg = r.get("result", {}).get("message", "")
                print(f"  STL push no confirmation. msg={msg!r} out_tail={out[-200:]!r}")


# --- step 7: push polylines into Rhino layer ----------------------------------
STREAM_COLOR_ARGB = (0xFF, 0xFF, 0x00, 0x00)   # opaque red #FF0000

# Grid layout (from Rhino "grid" layer; cell size = 25000 mm).
GRID_X0_MM = -9225.1
GRID_Y0_MM = -11472.0
GRID_CELL_MM = 25000.0


def grid_cell_center_mm(gen_idx: int, var_idx: int) -> tuple[float, float]:
    """gen_idx in 1..10 (X col), var_idx in 1..8 (Y row). Returns (cx, cy) mm."""
    cx = GRID_X0_MM + GRID_CELL_MM * (gen_idx - 0.5)
    cy = GRID_Y0_MM + GRID_CELL_MM * (var_idx - 0.5)
    return cx, cy


def _ensure_layer_via_mcp(full_path: str, argb: tuple) -> int:
    """Create the nested layer path in Rhino if missing. Returns its layer
    index, or -1 on failure. Done in ONE dedicated MCP call so subsequent
    chunks can rely on lookup by FullPath."""
    a, r, g, b = argb
    code = f'''
import Rhino, System, scriptcontext as sc
from System.Drawing import Color
doc = sc.doc
fp = "{full_path}"
idx = doc.Layers.FindByFullPath(fp, -1)
if idx < 0:
    parts = fp.split("::")
    parent_id = System.Guid.Empty
    cur = ""
    for k, part in enumerate(parts):
        cur = part if k == 0 else cur + "::" + part
        ix = doc.Layers.FindByFullPath(cur, -1)
        if ix < 0:
            nl = Rhino.DocObjects.Layer(); nl.Name = part
            if parent_id != System.Guid.Empty:
                nl.ParentLayerId = parent_id
            if k == len(parts) - 1:
                nl.Color = Color.FromArgb({a}, {r}, {g}, {b})
            ix = doc.Layers.Add(nl)
        parent_id = doc.Layers[ix].Id
    idx = doc.Layers.FindByFullPath(fp, -1)
print("LAYER_IDX " + str(idx))
'''
    res = mcp_call(code, timeout=60)
    out = res.get("result", {}).get("output", "") if isinstance(res, dict) else ""
    for line in out.splitlines():
        if line.startswith("LAYER_IDX"):
            try:
                return int(line.split()[1])
            except Exception:
                return -1
    return -1


def push_polylines(trails: dict, holes_m: list, layer_path: str,
                    offset_mm: tuple = (0.0, 0.0, 0.0),
                    max_polylines: int = 500,
                    obj_name: str = ""):
    """Send polylines to `layer_path` (the stream sub-layer of the batch).
    Each polyline gets its `Attributes.Name = obj_name` so we can identify
    the cell it belongs to without separate layers."""
    # Only push NOZZLE trails (z0 >= 28; prefill sits at z < 28). The prefill
    # water polylines crowd the view and visually overlap the cap walls,
    # creating the "stream through collider" illusion the user reported.
    all_polys = [pts for pts in trails.values()
                  if pts and len(pts) >= 2 and pts[0][2] >= 28.0]
    if len(all_polys) > max_polylines:
        step = len(all_polys) / max_polylines
        polys = [all_polys[int(i * step)] for i in range(max_polylines)]
    else:
        polys = all_polys
    ox, oy, oz = offset_mm

    lid_initial = _ensure_layer_via_mcp(layer_path, STREAM_COLOR_ARGB)
    if lid_initial < 0:
        print(f"WARN: could not create layer {layer_path}")
        return

    # Purge prior polylines with the SAME name (per-cell replace).
    if obj_name:
        purge_code = f'''
import Rhino, scriptcontext as sc
doc = sc.doc
lid = doc.Layers.FindByFullPath("{layer_path}", -1)
if lid >= 0:
    n = 0
    for o in list(doc.Objects):
        if o.Attributes.LayerIndex == lid and o.Name == "{obj_name}":
            doc.Objects.Delete(o, True); n += 1
    print("purged " + str(n))
'''
        mcp_call(purge_code, timeout=120)

    HEADER = [
        "import Rhino, System, scriptcontext as sc",
        "import Rhino.Geometry as rg",
        "doc = sc.doc",
        f"_layer_path = '{layer_path}'",
        f"_obj_name = '{obj_name}'",
        "lid = doc.Layers.FindByFullPath(_layer_path, -1)",
        "if lid < 0: raise Exception('layer missing: ' + _layer_path)",
        "att = Rhino.DocObjects.ObjectAttributes()",
        "att.LayerIndex = lid",
        "if _obj_name: att.Name = _obj_name",
    ]

    chunks = []
    cur = list(HEADER) + ["added = 0"]
    for pts in polys:
        mm = ",".join(
            f"rg.Point3d({p[0]*1000+ox:.2f},{p[1]*1000+oy:.2f},{p[2]*1000+oz:.2f})"
            for p in pts
        )
        cur.append(f"pl = rg.Polyline([{mm}])")
        cur.append("doc.Objects.AddPolyline(pl, att)")
        cur.append("added += 1")
        if sum(len(x) for x in cur) > 70000:
            cur.append("print('chunk lid=' + str(lid) + ' added=' + str(added))")
            chunks.append("\n".join(cur))
            cur = list(HEADER) + ["added = 0"]
    cur.append("doc.Views.Redraw()")
    cur.append("print('chunk lid=' + str(lid) + ' added=' + str(added))")
    chunks.append("\n".join(cur))

    for i, code in enumerate(chunks, 1):
        r = mcp_call(code, timeout=240)
        if r.get("status") != "success":
            print(f"chunk {i} FAIL:", json.dumps(r)[:300])
        else:
            out = r.get("result", {}).get("output", "")
            if "chunk lid=-1" in out or "layer missing" in out:
                print(f"chunk {i} LAYER LOST: {out[-200:]}")


# --- step 5: fitness ----------------------------------------------------------
def classify_trails(trails: dict, pond_min: list, pond_max: list,
                     pond_top_z: float) -> dict:
    """Bucket each trail's LAST position. Drop particles with z_drop < 1 m
    (inlet residue) — only count moved/caught/splash."""
    caught = 0; splash = 0; total = 0; moved = 0; stuck = 0
    for idp, pts in trails.items():
        if not pts: continue
        x0, y0, z0 = pts[0]
        x, y, z = pts[-1]
        total += 1
        if (z0 - z) < 1.0:
            stuck += 1
            continue
        moved += 1
        if (pond_min[0] <= x <= pond_max[0] and pond_min[1] <= y <= pond_max[1]
                and z <= pond_top_z):
            caught += 1
        else:
            splash += 1
    catch_rate = (caught / moved) if moved else 0.0
    return {
        "caught": caught, "splash": splash, "moved": moved, "stuck": stuck,
        "total": total,
        "splash_ratio": (splash / total) if total else 1.0,
        "catch_rate_moved": round(catch_rate, 4),
    }


def retention_metric(trails: dict, per_module_bboxes: list,
                      pond_min: list, pond_max: list, pond_top_z: float,
                      margin_m: float = 0.30) -> dict:
    """At TimeMax, classify every trail's endpoint.

    Splash = endpoint XY OUTSIDE both the pond bbox AND every module bbox.
    Otherwise the particle is still inside the sculpture's footprint (either
    sitting in a module, mid-air above the column, or in the pond).
    """
    total = 0
    splash = 0
    in_pond = 0
    in_column = 0          # in column but above pond
    settled_in_place = 0   # endpoint near its start (prefill that didn't move)
    on_module = {b["index"]: 0 for b in per_module_bboxes}

    bbs = []
    for b in per_module_bboxes:
        bb = b["bbox_m"]
        bbs.append((b["index"], bb[0], bb[1]))

    def in_any_module_xy(x: float, y: float) -> bool:
        for idx, lo, hi in bbs:
            if lo[0] - margin_m <= x <= hi[0] + margin_m and \
                    lo[1] - margin_m <= y <= hi[1] + margin_m:
                return True
        return False

    for idp, pts in trails.items():
        if not pts: continue
        total += 1
        x0, y0, z0 = pts[0]
        x, y, z = pts[-1]
        in_pond_xy = (pond_min[0] <= x <= pond_max[0]
                        and pond_min[1] <= y <= pond_max[1])
        in_mod_xy = in_any_module_xy(x, y)
        if not in_pond_xy and not in_mod_xy:
            splash += 1
            continue
        if in_pond_xy and z <= pond_top_z:
            in_pond += 1
        else:
            in_column += 1
            for idx, lo, hi in bbs:
                if (lo[0] - margin_m <= x <= hi[0] + margin_m and
                        lo[1] - margin_m <= y <= hi[1] + margin_m and
                        lo[2] - margin_m <= z <= hi[2] + margin_m):
                    on_module[idx] += 1
                    break
            if (x - x0) ** 2 + (y - y0) ** 2 + (z - z0) ** 2 < 1.0:
                settled_in_place += 1

    retained = in_pond + in_column
    return {
        "total": total,
        "splash": splash,
        "in_pond": in_pond,
        "in_column": in_column,
        "settled_in_place": settled_in_place,
        "on_module": on_module,
        "retained": retained,
        "retention_rate": (retained / total) if total else 0.0,
    }


def touch_metric(trails: dict, modules_info: list, half_slab_m: float = 3.0,
                  genes_by_index: dict | None = None) -> dict:
    """Compute fraction of trails that pass through ALL N module z-slabs
    (slab = [P.z - half, P.z + half] in METERS, with P.z based on the actual
    module placement = base_z + tz).
    """
    slabs = []   # list of (lo_z, hi_z) in meters
    for m in modules_info:
        base_mm = m["base_point_mm"]
        tz_mm = float(genes_by_index.get(m["index"], {}).get("tz", 0.0)) if genes_by_index else 0.0
        cz_m = (base_mm[2] + tz_mm) * 0.001
        slabs.append((cz_m - half_slab_m, cz_m + half_slab_m))

    per_slab_count = [0] * len(slabs)
    n_total = 0
    n_touch_all = 0
    for idp, pts in trails.items():
        if not pts: continue
        n_total += 1
        touched = [False] * len(slabs)
        for p in pts:
            z = p[2]
            for i, (lo, hi) in enumerate(slabs):
                if not touched[i] and lo <= z <= hi:
                    touched[i] = True
        for i, t in enumerate(touched):
            if t: per_slab_count[i] += 1
        if all(touched):
            n_touch_all += 1
    return {
        "n_total_trails": n_total,
        "per_slab_touch": per_slab_count,
        "n_touched_all": n_touch_all,
        "touch_all_ratio": (n_touch_all / n_total) if n_total else 0.0,
    }


# --- main ---------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print("Usage: experiment_runner.py experiments/test_NN.json")
        sys.exit(2)
    params_path = Path(sys.argv[1])
    params = json.loads(params_path.read_text(encoding="utf-8"))

    test_id = params["test_id"]
    iter_dir = RUNS_DIR / f"iter_{test_id}"
    if iter_dir.exists(): shutil.rmtree(iter_dir)
    iter_dir.mkdir(parents=True)
    out_dir = iter_dir / "Out"; out_dir.mkdir()
    data_dir = out_dir / "data"

    # ---- merge defaults ----
    glob = dict(SIM_DEFAULTS)
    glob.update(params.get("global", {}))

    modules_info = json.loads(MODULES_JSON.read_text(encoding="utf-8"))["modules"]
    geom = json.loads(GEOM_JSON.read_text(encoding="utf-8"))

    # ---- step 2: build combined parametric STL ----
    genes_by_index = {}
    for m in params.get("modules", []):
        g = {k: float(m.get(k, DEFAULTS.get(k, 0.0))) for k in GENE_ORDER}
        genes_by_index[int(m["index"])] = g
    stl_path = iter_dir / "sculpture.stl"
    stl_info = build_sculpture_stl(modules_info, genes_by_index, stl_path)
    n_tri = stl_info["total_tris"]
    bbox_m = stl_info["bbox_m"]
    print(f"[mesh] {n_tri} tris, bbox_m={bbox_m}")

    # ---- step 3: render XML ----
    # Option D + pre-fill: initial fluid particles =
    #   (a) 230 nozzle positions (drop from above)
    #   (b) PRE-FILLED water inside each module bowl up to its tilted rim's
    #       lowest point. SPH naturally handles overflow as nozzle water lands.
    vp_file = iter_dir / "velprof.dat"
    make_velocity_profile_file(vp_file, glob["v_inlet"], glob["burst_end"])

    nozzle_holes = list(geom["nozzle_holes_m"])
    fill_particles = []
    if params.get("prefill", True):
        for m in modules_info:
            g = genes_by_index.get(m["index"])
            if g is None:
                continue
            base = m["base_point_mm"]
            P_mm = (base[0] + g["tx"], base[1] + g["ty"], base[2] + g["tz"])
            pts = build_module_fill_particles(
                P_mm, g["radius"], g["move_z"], g["rotation_x"], g["rotation_z"],
                dp_m=glob["dp"],
            )
            fill_particles.extend(pts)
    initial_points = nozzle_holes + fill_particles
    print(f"[fluid] {len(nozzle_holes)} nozzle + {len(fill_particles)} prefill "
          f"= {len(initial_points)} initial particles")
    g_blk, iv_blk, io_blk = build_initial_particles_block(initial_points,
                                                            glob["v_inlet"])

    coll_min = bbox_m[0]; coll_max = bbox_m[1]
    pond_min = geom["pond_bbox_m"][0]; pond_max = geom["pond_bbox_m"][1]
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
        "pond_thickness": 0.15,   # 3 dp layers — enough for mDBC, far thinner than 0.5
    }
    nz0 = nozzle_holes[0]
    sim_params = {
        "sculpture_size": 1.0, "sculpture_angle": 0.0, "sculpture_height": 0.0,
        "nozzle_x": nz0[0], "nozzle_y": nz0[1], "nozzle_z": nz0[2],
        "nozzle_diameter": glob["nozzle_diameter"], "nozzle_angle_x": 0.0,
        "nozzle_angle_y": 0.0, "flow_velocity": glob["v_inlet"],
    }
    mapping = build_mapping(sim_params, stl_path, glob["dp"], glob["timemax"],
                            glob["timeout"], domain_override=domain_override,
                            pond_override=pond_override)
    mapping["INLET_GEOMETRY"] = g_blk
    mapping["INLET_INITIAL_VELOCITIES"] = iv_blk
    mapping["INLET_INOUTZONES"] = io_blk
    text = render_template(TEMPLATE_PATH.read_text(encoding="utf-8"), mapping)
    text = _apply_xml_patches(text, {
        "speedsound": glob["speedsound"], "cflnumber": glob["cflnumber"],
        "DensityDT": glob["DensityDT"], "Visco": glob["Visco"],
        "ViscoBoundFactor": glob["viscobound"],
    }, inlet_layers=4)

    # Option D: no inlet zones. Strip the entire <inout>...</inout> block so
    # DSPH does not try to initialise an empty inflow system.
    if not io_blk.strip():
        import re
        text = re.sub(r"\s*<inout>.*?</inout>\s*", "\n", text, count=1, flags=re.DOTALL)

    case_def_xml = iter_dir / "case_Def.xml"
    case_def_xml.write_text(text, encoding="utf-8")
    case_def_stub = iter_dir / "case_Def"
    case_stub = out_dir / "case"

    def run(cmd, label, to_s=900):
        t0 = time.time()
        p = subprocess.run([str(c) for c in cmd], cwd=str(iter_dir),
                            capture_output=True, text=True, timeout=to_s)
        dt = time.time() - t0
        print(f"[{label}] exit={p.returncode} ({dt:.1f}s)")
        if p.returncode != 0:
            print(p.stdout[-1000:]); print(p.stderr[-300:])
        return p.returncode, dt

    rc, _ = run([GENCASE, case_def_stub, case_stub, "-save:all"], "GenCase", 120)
    if rc != 0: sys.exit(1)
    if glob.get("use_gpu", True):
        rc, t_sim = run([DUALSPH_GPU, "-gpu", case_stub, out_dir],
                         "DualSPHysics GPU", 1800)
    else:
        rc, t_sim = run([DUALSPH_CPU, "-cpu", case_stub, out_dir],
                         "DualSPHysics CPU", 1800)
    if rc != 0: sys.exit(1)
    rc, _ = run([PARTVTK, "-dirdata", data_dir, "-savecsv",
                 out_dir / "PartFluid", "-onlytype:-all,+fluid"], "PartVTK CSV", 120)

    # ---- step 5: build trails + fitness ----
    csvs = sorted(out_dir.glob("PartFluid_*.csv"))
    trails: dict[int, list] = {}
    for cp in csvs:
        for idp, x, y, z in parse_csv_with_idp(cp):
            trails.setdefault(idp, []).append((x, y, z))

    (iter_dir / "trails.json").write_text(
        json.dumps({str(k): v for k, v in trails.items()}), encoding="utf-8")

    pond_top_z = pond_override["pond_thickness"] + 3 * glob["dp"]
    pond_min_xyz = [pond_override["pond_xmin"], pond_override["pond_ymin"], 0.0]
    pond_max_xyz = [pond_override["pond_xmin"] + pond_override["pond_xsize"],
                     pond_override["pond_ymin"] + pond_override["pond_ysize"], 0.0]
    classify = classify_trails(trails, pond_min_xyz, pond_max_xyz, pond_top_z)
    touch = touch_metric(trails, modules_info, half_slab_m=3.0,
                          genes_by_index=genes_by_index)
    retention = retention_metric(trails, stl_info["per_module"],
                                  pond_min_xyz, pond_max_xyz, pond_top_z,
                                  margin_m=0.30)

    result = {
        "test_id": test_id,
        **classify,
        "touch": touch,
        "retention": retention,
        "wall_time_s": round(t_sim, 1),
        "stl_triangles": n_tri,
        "stl_bbox_m": bbox_m,
        "per_module_bboxes_m": [{"index": b["index"], "bbox_m": b["bbox_m"]}
                                  for b in stl_info["per_module"]],
        "params": params,
        "globals_applied": glob,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (iter_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (iter_dir / "params.json").write_text(json.dumps(params, indent=2), encoding="utf-8")

    print()
    print(f"=== {test_id} RESULT ===")
    print(f"  caught={classify['caught']}  splash={classify['splash']}  "
          f"moved={classify['moved']}  stuck={classify['stuck']}  total={classify['total']}")
    print(f"  catch_rate_moved={classify['catch_rate_moved']}  "
          f"touch_all_ratio={touch['touch_all_ratio']:.3f}")
    print(f"  retention: in_pond={retention['in_pond']} "
          f"in_column={retention.get('in_column', 0)} "
          f"on_module={retention['on_module']} "
          f"splash={retention['splash']} -> retention_rate={retention['retention_rate']:.3f}")
    print(f"  wall_time={result['wall_time_s']}s   stl_triangles={n_tri}")

    # ---- step 6/7: push to Rhino ----
    # New scheme: ALL cells in a sweep go onto a single parent layer pair
    # (batch_layer::collider, batch_layer::stream). The per-cell identity is
    # carried via the object Name. batch_layer comes from params.batch_layer
    # (default "test_01"); a new sweep just bumps to test_02 etc., no cleanup.
    grid_cell = params.get("grid_cell")
    batch_layer = params.get("batch_layer", "test_01")
    if grid_cell:
        gen_idx = int(grid_cell["gen"])
        var_idx = int(grid_cell["var"])
        cx, cy = grid_cell_center_mm(gen_idx, var_idx)
        nozzle_ref = geom["nozzle_center_m"]
        ox = cx - nozzle_ref[0] * 1000.0
        oy = cy - nozzle_ref[1] * 1000.0
        oz = 0.0
        obj_name = f"gen_{gen_idx:02d}-var_{var_idx:02d}"
    else:
        ox = oy = oz = 0.0
        obj_name = test_id
    collider_layer = f"{batch_layer}::collider"
    stream_layer = f"{batch_layer}::stream"
    viz_path = stl_info.get("viz_stl_path")
    bake_stl = Path(viz_path) if viz_path and Path(viz_path).exists() else stl_path
    print(f"[Rhino] push collider STL -> {collider_layer} (name={obj_name}) "
          f"offset=({ox:.0f},{oy:.0f}) src={bake_stl.name}")
    push_stl_to_rhino_layer(bake_stl, collider_layer, (140, 90, 30),
                              offset_mm=(ox, oy, oz), obj_name=obj_name)
    print(f"[Rhino] push streamlines -> {stream_layer} (name={obj_name})")
    push_polylines({k: v for k, v in trails.items()},
                    geom["nozzle_holes_m"], stream_layer,
                    offset_mm=(ox, oy, oz), obj_name=obj_name)

    # ---- step 8: refresh dashboard ----
    try:
        from update_experiments_html import regenerate
        regenerate()
        print("[dashboard] experiments.html refreshed")
    except Exception as e:
        print("[dashboard] skip:", e)


if __name__ == "__main__":
    main()
