"""
Run one experiment defined by experiments/test_NN.json.

Steps:
  1. Read params.
  2. Pull each original collider Mesh via rhinomcp by GUID.
  3. Apply per-module transforms (rotation, translation, scale).
  4. Write a single STL of the combined transformed mesh (mm -> m).
  5. Render case_Def.xml with multi-hole + velocity profile.
  6. GenCase + DualSPHysics CPU + PartVTK CSV.
  7. Build trails.json + compute fitness (result.json).
  8. Push transformed collider mesh into Rhino layer
     test_NN/collider_NN  (mm units).
  9. Push polylines into  test_NN/stream_nozzle_X.
 10. Regenerate experiments.html dashboard.
"""
from __future__ import annotations

import csv
import json
import math
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from run_case import (
    GENCASE, DUALSPH_CPU, PARTVTK, PARTVTKOUT,
    TEMPLATE_PATH, RUNS_DIR, build_mapping, render_template, _apply_xml_patches,
    angles_to_velocity_vector,
)
from run_streamline import (
    make_velocity_profile_file, build_burst_inlet_blocks,
    parse_csv_with_idp,
)

HOST, PORT = "127.0.0.1", 1999
MODULES_JSON = PROJECT / "runs" / "_collider_modules.json"
GEOM_JSON = PROJECT / "runs" / "_real_geom.json"
EXPERIMENTS_DIR = PROJECT / "experiments"

# --- defaults -----------------------------------------------------------------
DEFAULTS = {
    "v_inlet": 2.0,
    "burst_end": 0.5,          # widened from 0.06 — particles must clear inlet zone
    "dp": 0.10,
    "timemax": 10.0,
    "timeout": 0.10,
    "nozzle_diameter": 0.15,
    "nozzle_thickness": 0.05,
    "speedsound": 250,
    "cflnumber": 0.30,
    "DensityDT": 1,
    "Visco": 0.05,
    "viscobound": 1.0,
}


def mcp_call(code: str, timeout: float = 120.0) -> dict:
    payload = json.dumps({"type": "execute_rhinoscript_python_code",
                          "params": {"code": code}}).encode()
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(payload)
        buf = b""
        while True:
            try: c = s.recv(65536)
            except socket.timeout: break
            if not c: break
            buf += c
            try: return json.loads(buf.decode("utf-8", "ignore"))
            except json.JSONDecodeError: continue
    return {"error": "no json"}


def extract_balanced_json(text: str) -> dict:
    s = text.find("{"); depth = 0; e = -1
    for i in range(s, len(text)):
        if text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0: e = i + 1; break
    return json.loads(text[s:e])


# --- step 2/3/4: build transformed STL ---------------------------------------
def fetch_and_transform_mesh(modules_info: list, transforms_by_index: dict,
                              stl_out: Path) -> int:
    """Pull each collider module by GUID, apply per-module transforms server-side,
    merge into one mesh, write ASCII STL in METERS to stl_out.
    Also returns triangle count.
    """
    # Build server-side script
    transforms_json = json.dumps(transforms_by_index)
    modules_json = json.dumps(modules_info)
    stl_path_py = str(stl_out).replace("\\", "\\\\")

    code = r"""
import Rhino, json
doc = Rhino.RhinoDoc.ActiveDoc

modules = json.loads(r'''__MODULES__''')
xf_by_idx = json.loads(r'''__XF__''')
mm_to_m = 0.001

combined = Rhino.Geometry.Mesh()
total_tri = 0
for m in modules:
    idx_str = str(m["index"])
    xf = xf_by_idx.get(idx_str, None)
    rot = xf["rotation_deg"] if xf else [0, 0, 0]
    trans = xf["translation_m"] if xf else [0, 0, 0]
    scl = xf["scale"] if xf else 1.0
    obj = doc.Objects.FindId(System.Guid.Parse(m["guid"])) if False else None
    # IronPython needs explicit System import
    import System
    obj = doc.Objects.FindId(System.Guid.Parse(m["guid"]))
    if obj is None or obj.Geometry is None: continue
    src = obj.Geometry.DuplicateMesh() if isinstance(obj.Geometry, Rhino.Geometry.Mesh) else None
    if src is None: continue
    # Center of bbox (mm) before transform
    bb = src.GetBoundingBox(True)
    cx = bb.Center.X; cy = bb.Center.Y; cz = bb.Center.Z

    # Build affine: translate to origin -> scale -> rotate (XYZ) -> translate back -> apply translation (m -> mm)
    Tneg = Rhino.Geometry.Transform.Translation(-cx, -cy, -cz)
    S = Rhino.Geometry.Transform.Scale(Rhino.Geometry.Point3d(0, 0, 0), scl)
    Rx = Rhino.Geometry.Transform.Rotation(Rhino.RhinoMath.ToRadians(rot[0]), Rhino.Geometry.Vector3d(1, 0, 0), Rhino.Geometry.Point3d(0, 0, 0))
    Ry = Rhino.Geometry.Transform.Rotation(Rhino.RhinoMath.ToRadians(rot[1]), Rhino.Geometry.Vector3d(0, 1, 0), Rhino.Geometry.Point3d(0, 0, 0))
    Rz = Rhino.Geometry.Transform.Rotation(Rhino.RhinoMath.ToRadians(rot[2]), Rhino.Geometry.Vector3d(0, 0, 1), Rhino.Geometry.Point3d(0, 0, 0))
    Tpos = Rhino.Geometry.Transform.Translation(cx, cy, cz)
    Tuser = Rhino.Geometry.Transform.Translation(trans[0] * 1000.0, trans[1] * 1000.0, trans[2] * 1000.0)
    # Chain
    X = Tuser * Tpos * Rz * Ry * Rx * S * Tneg
    src.Transform(X)

    # Append to combined
    combined.Append(src)
    total_tri += src.Faces.Count

# Triangulate + recompute normals
combined.Faces.ConvertQuadsToTriangles()
combined.Normals.ComputeNormals()
combined.FaceNormals.ComputeFaceNormals()
V = combined.Vertices; F = combined.Faces; FN = combined.FaceNormals
lines = ["solid collider"]
for i in range(F.Count):
    f = F[i]; n = FN[i]
    a = V[f.A]; b = V[f.B]; c = V[f.C]
    lines.append("  facet normal {0:.6e} {1:.6e} {2:.6e}".format(n.X, n.Y, n.Z))
    lines.append("    outer loop")
    lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(a.X*mm_to_m, a.Y*mm_to_m, a.Z*mm_to_m))
    lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(b.X*mm_to_m, b.Y*mm_to_m, b.Z*mm_to_m))
    lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(c.X*mm_to_m, c.Y*mm_to_m, c.Z*mm_to_m))
    lines.append("    endloop")
    lines.append("  endfacet")
lines.append("endsolid collider")

f = open(r"__STL__", "w")
try: f.write("\n".join(lines))
finally: f.close()
print(json.dumps({"triangles": F.Count}))
"""
    code = (code
            .replace("__MODULES__", modules_json)
            .replace("__XF__", transforms_json)
            .replace("__STL__", stl_path_py))

    r = mcp_call(code, timeout=180)
    if r.get("status") != "success":
        raise RuntimeError(f"MCP transform failed: {json.dumps(r)[:500]}")
    info = extract_balanced_json(r["result"].get("output", ""))
    return int(info.get("triangles", 0))


# --- step 8: push transformed mesh into Rhino test_NN/collider_NN ------------
def push_collider_mesh_to_layer(modules_info: list, transforms_by_index: dict,
                                 test_id: str):
    """Recreate the transformed mesh as a single Mesh object under test_NN/collider_NN."""
    transforms_json = json.dumps(transforms_by_index)
    modules_json = json.dumps(modules_info)

    code = r"""
import Rhino, System, json
from System.Drawing import Color
doc = Rhino.RhinoDoc.ActiveDoc

def ensure_layer(full_path, rgb):
    idx = doc.Layers.FindByFullPath(full_path, -1)
    if idx >= 0: return idx
    parts = full_path.split('::')
    parent_id = System.Guid.Empty
    cur = ''
    for k, part in enumerate(parts):
        cur = part if k == 0 else cur + '::' + part
        ix = doc.Layers.FindByFullPath(cur, -1)
        if ix < 0:
            nl = Rhino.DocObjects.Layer()
            nl.Name = part
            if parent_id != System.Guid.Empty:
                nl.ParentLayerId = parent_id
            if k == len(parts) - 1:
                nl.Color = Color.FromArgb(rgb[0], rgb[1], rgb[2])
            ix = doc.Layers.Add(nl)
        parent_id = doc.Layers[ix].Id
    return ix

modules = json.loads(r'''__MODULES__''')
xf_by_idx = json.loads(r'''__XF__''')
test_id = "__TID__"

# Target layer: test_NN::collider_NN
collider_layer = "{0}::collider_{1}".format(test_id, test_id.split('_')[-1])
li = ensure_layer(collider_layer, (140, 90, 30))
attr = Rhino.DocObjects.ObjectAttributes(); attr.LayerIndex = li

combined = Rhino.Geometry.Mesh()
for m in modules:
    idx_str = str(m["index"])
    xf = xf_by_idx.get(idx_str, None)
    rot = xf["rotation_deg"] if xf else [0, 0, 0]
    trans = xf["translation_m"] if xf else [0, 0, 0]
    scl = xf["scale"] if xf else 1.0
    obj = doc.Objects.FindId(System.Guid.Parse(m["guid"]))
    if obj is None: continue
    src = obj.Geometry.DuplicateMesh()
    bb = src.GetBoundingBox(True)
    cx = bb.Center.X; cy = bb.Center.Y; cz = bb.Center.Z

    Tneg = Rhino.Geometry.Transform.Translation(-cx, -cy, -cz)
    S = Rhino.Geometry.Transform.Scale(Rhino.Geometry.Point3d(0, 0, 0), scl)
    Rx = Rhino.Geometry.Transform.Rotation(Rhino.RhinoMath.ToRadians(rot[0]), Rhino.Geometry.Vector3d(1, 0, 0), Rhino.Geometry.Point3d(0, 0, 0))
    Ry = Rhino.Geometry.Transform.Rotation(Rhino.RhinoMath.ToRadians(rot[1]), Rhino.Geometry.Vector3d(0, 1, 0), Rhino.Geometry.Point3d(0, 0, 0))
    Rz = Rhino.Geometry.Transform.Rotation(Rhino.RhinoMath.ToRadians(rot[2]), Rhino.Geometry.Vector3d(0, 0, 1), Rhino.Geometry.Point3d(0, 0, 0))
    Tpos = Rhino.Geometry.Transform.Translation(cx, cy, cz)
    Tuser = Rhino.Geometry.Transform.Translation(trans[0] * 1000.0, trans[1] * 1000.0, trans[2] * 1000.0)
    X = Tuser * Tpos * Rz * Ry * Rx * S * Tneg
    src.Transform(X)
    combined.Append(src)

combined.Normals.ComputeNormals()
doc.Objects.AddMesh(combined, attr)
doc.Views.Redraw()
print("OK", combined.Faces.Count)
"""
    code = (code
            .replace("__MODULES__", modules_json)
            .replace("__XF__", transforms_json)
            .replace("__TID__", test_id))
    r = mcp_call(code, timeout=180)
    if r.get("status") != "success":
        print("WARN: collider mesh push failed:", json.dumps(r)[:300])


# --- step 9: push polylines (mm-scaled) into test_NN/stream_nozzle_X ---------
NOZZLE_COLORS = [(220, 60, 60), (60, 180, 75), (60, 100, 220),
                  (240, 160, 40), (180, 60, 220)]


def push_polylines(trails: dict, holes_m: list, parent_layer: str):
    """Send polylines to Rhino. Color-group by nearest nozzle. mm conversion."""
    # Group by nearest nozzle
    groups = {i: [] for i in range(len(holes_m))}
    for idp, pts in trails.items():
        if not pts: continue
        sx, sy = pts[0][0], pts[0][1]
        best = 0; bd = 1e18
        for i, h in enumerate(holes_m):
            d = (sx - h[0]) ** 2 + (sy - h[1]) ** 2
            if d < bd: bd = d; best = i
        groups[best].append(pts)

    HEADER = [
        "import Rhino, System",
        "import Rhino.Geometry as rg",
        "from System.Drawing import Color",
        "doc = Rhino.RhinoDoc.ActiveDoc",
        "def ensure_layer(full_path, rgb):",
        "    idx = doc.Layers.FindByFullPath(full_path, -1)",
        "    if idx >= 0: return idx",
        "    parts = full_path.split('::')",
        "    parent_id = System.Guid.Empty",
        "    cur = ''",
        "    for k, part in enumerate(parts):",
        "        cur = part if k == 0 else cur + '::' + part",
        "        ix = doc.Layers.FindByFullPath(cur, -1)",
        "        if ix < 0:",
        "            nl = Rhino.DocObjects.Layer(); nl.Name = part",
        "            if parent_id != System.Guid.Empty: nl.ParentLayerId = parent_id",
        "            if k == len(parts) - 1: nl.Color = Color.FromArgb(rgb[0], rgb[1], rgb[2])",
        "            ix = doc.Layers.Add(nl)",
        "        parent_id = doc.Layers[ix].Id",
        "    return ix",
    ]

    chunks = []
    cur = list(HEADER); cur.append("added = 0")
    setup_done = set()
    for ni, group in groups.items():
        if not group: continue
        color = NOZZLE_COLORS[ni % len(NOZZLE_COLORS)]
        for pts in group:
            if len(pts) < 2: continue
            mm = ",".join(f"rg.Point3d({p[0]*1000:.2f},{p[1]*1000:.2f},{p[2]*1000:.2f})" for p in pts)
            if ni not in setup_done:
                path = f"{parent_layer}::stream_nozzle_{ni}"
                cur.append(f"lid_{ni} = ensure_layer({path!r}, ({color[0]},{color[1]},{color[2]}))")
                cur.append(f"att_{ni} = Rhino.DocObjects.ObjectAttributes(); att_{ni}.LayerIndex = lid_{ni}")
                setup_done.add(ni)
            cur.append(f"pl = rg.Polyline([{mm}])")
            cur.append(f"doc.Objects.AddPolyline(pl, att_{ni})")
            cur.append("added += 1")
            if sum(len(x) for x in cur) > 70000:
                cur.append("print('chunk added', added)")
                chunks.append("\n".join(cur))
                cur = list(HEADER); cur.append("added = 0")
                setup_done = set()
    cur.append("doc.Views.Redraw()")
    cur.append("print('chunk added', added)")
    chunks.append("\n".join(cur))

    for i, code in enumerate(chunks, 1):
        r = mcp_call(code, timeout=240)
        if r.get("status") != "success":
            print(f"chunk {i} FAIL:", json.dumps(r)[:300])


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

    # ---- merge defaults
    glob = dict(DEFAULTS)
    glob.update(params.get("global", {}))

    transforms = {str(m["index"]): m for m in params.get("modules", [])}
    modules_info = json.loads(MODULES_JSON.read_text(encoding="utf-8"))["modules"]
    geom = json.loads(GEOM_JSON.read_text(encoding="utf-8"))

    # ---- step 2-4: build STL ----
    stl_path = iter_dir / "sculpture.stl"
    n_tri = fetch_and_transform_mesh(modules_info, transforms, stl_path)
    print(f"[transform] {n_tri} triangles -> {stl_path.name}")

    # ---- step 5: render XML ----
    vp_file = iter_dir / "velprof.dat"
    make_velocity_profile_file(vp_file, glob["v_inlet"], glob["burst_end"])

    holes = geom["nozzle_holes_m"]
    holes = [(h[0], h[1], h[2] - 0.05) for h in holes]
    g_blk, iv_blk, io_blk = build_burst_inlet_blocks(
        holes, glob["nozzle_diameter"], glob["nozzle_thickness"], glob["v_inlet"],
        0.0, 0.0, vp_file.name)

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

    sim_params = {
        "sculpture_size": 1.0, "sculpture_angle": 0.0, "sculpture_height": 0.0,
        "nozzle_x": holes[0][0], "nozzle_y": holes[0][1], "nozzle_z": holes[0][2],
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
            print(p.stdout[-800:]); print(p.stderr[-300:])
        return p.returncode, dt

    rc, _ = run([GENCASE, case_def_stub, case_stub, "-save:all"], "GenCase", 120)
    if rc != 0: sys.exit(1)
    rc, t_sim = run([DUALSPH_CPU, "-cpu", case_stub, out_dir], "DualSPHysics CPU", 1800)
    if rc != 0: sys.exit(1)
    rc, _ = run([PARTVTK, "-dirdata", data_dir, "-savecsv",
                 out_dir / "PartFluid", "-onlytype:-all,+fluid"], "PartVTK CSV", 120)

    # ---- step 7: build trails + fitness ----
    csvs = sorted(out_dir.glob("PartFluid_*.csv"))
    trails: dict[int, list] = {}
    for cp in csvs:
        for idp, x, y, z in parse_csv_with_idp(cp):
            trails.setdefault(idp, []).append((x, y, z))

    iter_dir_trails = iter_dir / "trails.json"
    iter_dir_trails.write_text(json.dumps({str(k): v for k, v in trails.items()}),
                                encoding="utf-8")

    # Fitness — classify each trail's LAST position. Only count particles that
    # actually descended (>1m drop) so inlet residue does not inflate splash.
    px0, py0 = pond_override["pond_xmin"], pond_override["pond_ymin"]
    px1 = px0 + pond_override["pond_xsize"]
    py1 = py0 + pond_override["pond_ysize"]
    pond_top_z = pond_override["pond_thickness"] + 3 * glob["dp"]
    caught = 0; splash = 0; total = 0; moved = 0; stuck = 0
    for idp, pts in trails.items():
        if not pts: continue
        x0, y0, z0 = pts[0]
        x, y, z = pts[-1]
        total += 1
        z_drop = z0 - z
        if z_drop < 1.0:
            stuck += 1
            continue
        moved += 1
        in_pond = (px0 <= x <= px1 and py0 <= y <= py1 and z <= pond_top_z)
        if in_pond: caught += 1
        else: splash += 1

    catch_rate = (caught / moved) if moved else 0.0
    result = {
        "test_id": test_id,
        "n_modules_transformed": sum(1 for m in transforms.values()
                                     if any(m.get("rotation_deg", [0]*3)) or
                                        any(m.get("translation_m", [0]*3)) or
                                        m.get("scale", 1.0) != 1.0),
        "caught": caught, "splash": splash, "moved": moved, "stuck": stuck,
        "total": total,
        "splash_ratio": (splash / total) if total else 1.0,
        "catch_rate_moved": round(catch_rate, 4),
        "wall_time_s": round(t_sim, 1),
        "stl_triangles": n_tri,
        "params": params,
        "globals_applied": glob,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (iter_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (iter_dir / "params.json").write_text(json.dumps(params, indent=2), encoding="utf-8")

    print()
    print(f"=== {test_id} RESULT ===")
    print(f"  caught={caught}  splash={splash}  moved={moved}  stuck={stuck}  total={total}")
    print(f"  splash_ratio={result['splash_ratio']:.3f}   catch_rate(moved)={catch_rate:.3f}")
    print(f"  wall_time={result['wall_time_s']}s   stl_triangles={n_tri}")

    # ---- step 8/9: push to Rhino ----
    parent_layer = test_id  # e.g. "test_03"
    print(f"[Rhino] push collider mesh -> {parent_layer}/collider_{test_id.split('_')[-1]}")
    push_collider_mesh_to_layer(modules_info, transforms, test_id)
    print(f"[Rhino] push streamlines -> {parent_layer}/stream_nozzle_*")
    push_polylines({k: v for k, v in trails.items()}, holes, parent_layer)

    # ---- step 10: refresh dashboard ----
    from update_experiments_html import regenerate
    regenerate()
    print("[dashboard] experiments.html refreshed")


if __name__ == "__main__":
    main()
