"""Bake optimized steel support geometry back into Rhino."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "env_fx3d" / "scripts"))

from rhino_mcp import mcp_call

DEFAULT_SOLUTION = MODULE_ROOT / "runs" / "structure_solution.json"


def bake_solution(solution: dict, *, purge: bool = True) -> str:
    payload = json.dumps(solution)
    code = f'''
import json, math
import Rhino, System, scriptcontext as sc
from System.Drawing import Color

doc = sc.doc
solution = json.loads({json.dumps(payload)})
purge = {str(bool(purge))}

LAYERS = {{
    "centerlines": ("structure_opt::centerlines", Color.FromArgb(255, 50, 120, 220)),
    "profiles": ("structure_opt::profiles", Color.FromArgb(255, 20, 20, 20)),
    "solid": ("structure_opt::solid_preview", Color.FromArgb(255, 170, 110, 50)),
    "loads": ("structure_opt::loads", Color.FromArgb(255, 220, 40, 40)),
}}

def ensure_layer(fp, color):
    idx = doc.Layers.FindByFullPath(fp, -1)
    if idx >= 0:
        doc.Layers[idx].Color = color
        return idx
    parts = fp.split("::")
    parent_id = System.Guid.Empty
    cur = ""
    for k, part in enumerate(parts):
        cur = part if k == 0 else cur + "::" + part
        ix = doc.Layers.FindByFullPath(cur, -1)
        if ix < 0:
            layer = Rhino.DocObjects.Layer()
            layer.Name = part
            if parent_id != System.Guid.Empty:
                layer.ParentLayerId = parent_id
            if k == len(parts) - 1:
                layer.Color = color
            ix = doc.Layers.Add(layer)
        parent_id = doc.Layers[ix].Id
    return doc.Layers.FindByFullPath(fp, -1)

def purge_layer(layer_idx):
    if layer_idx < 0:
        return 0
    count = 0
    for obj in list(doc.Objects):
        if obj.Attributes.LayerIndex == layer_idx:
            doc.Objects.Delete(obj, True)
            count += 1
    return count

layer_ids = {{name: ensure_layer(fp, color) for name, (fp, color) in LAYERS.items()}}
if purge:
    for lid in layer_ids.values():
        purge_layer(lid)

def pt(raw):
    return Rhino.Geometry.Point3d(float(raw[0]), float(raw[1]), float(raw[2]))

def set_user_strings(attr, data):
    for key, value in data.items():
        if value is None:
            continue
        attr.SetUserString(str(key), str(value))

def h_outline(profile):
    h = float(profile["h_mm"])
    b = float(profile["b_mm"])
    tw = float(profile["tw_mm"])
    tf = float(profile["tf_mm"])
    return [
        (-b/2, -h/2), ( b/2, -h/2), ( b/2, -h/2 + tf),
        ( tw/2, -h/2 + tf), ( tw/2, h/2 - tf), ( b/2, h/2 - tf),
        ( b/2, h/2), (-b/2, h/2), (-b/2, h/2 - tf),
        (-tw/2, h/2 - tf), (-tw/2, -h/2 + tf), (-b/2, -h/2 + tf),
    ]

def make_basis(a, b):
    axis = b - a
    if axis.Length <= 1e-9:
        return None
    axis.Unitize()
    up = Rhino.Geometry.Vector3d.ZAxis
    if abs(axis * up) > 0.95:
        up = Rhino.Geometry.Vector3d.XAxis
    u = Rhino.Geometry.Vector3d.CrossProduct(up, axis)
    if u.Length <= 1e-9:
        u = Rhino.Geometry.Vector3d.YAxis
    u.Unitize()
    v = Rhino.Geometry.Vector3d.CrossProduct(axis, u)
    v.Unitize()
    return axis, u, v

def add_h_mesh(member):
    a = pt(member["start_mm"])
    b = pt(member["end_mm"])
    basis = make_basis(a, b)
    if basis is None:
        return None
    _, u, v = basis
    coords = h_outline(member["profile"])
    mesh = Rhino.Geometry.Mesh()
    for base in (a, b):
        for x, y in coords:
            p = base + u * x + v * y
            mesh.Vertices.Add(p)
    n = len(coords)
    for i in range(n):
        j = (i + 1) % n
        mesh.Faces.AddFace(i, j, j + n, i + n)
    c0 = mesh.Vertices.Add(a)
    c1 = mesh.Vertices.Add(b)
    for i in range(n):
        j = (i + 1) % n
        mesh.Faces.AddFace(c0, j, i)
        mesh.Faces.AddFace(c1, i + n, j + n)
    mesh.Normals.ComputeNormals()
    mesh.Compact()
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = layer_ids["solid"]
    attr.Name = "structure_opt_solid_" + member["id"]
    set_user_strings(attr, {{
        "member_id": member["id"],
        "profile": member["profile"]["name"],
        "kind": member.get("kind"),
        "utilization": (member.get("analysis") or {{}}).get("utilization"),
        "length_mm": member.get("length_mm"),
    }})
    return doc.Objects.AddMesh(mesh, attr)

line_count = 0
mesh_count = 0
label_count = 0
for member in solution.get("selected_members", []):
    a = pt(member["start_mm"])
    b = pt(member["end_mm"])
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = layer_ids["centerlines"]
    attr.Name = "structure_opt_member_" + member["id"]
    set_user_strings(attr, {{
        "member_id": member["id"],
        "profile": member["profile"]["name"],
        "kind": member.get("kind"),
        "mass_kg_total": solution.get("result", {{}}).get("mass_kg"),
        "utilization": (member.get("analysis") or {{}}).get("utilization"),
        "axial_N": (member.get("analysis") or {{}}).get("axial_N"),
    }})
    doc.Objects.AddLine(Rhino.Geometry.Line(a, b), attr)
    line_count += 1

    mid = Rhino.Geometry.Point3d((a.X + b.X) * 0.5, (a.Y + b.Y) * 0.5, (a.Z + b.Z) * 0.5)
    dot = Rhino.Geometry.TextDot(member["profile"]["name"], mid)
    dattr = Rhino.DocObjects.ObjectAttributes()
    dattr.LayerIndex = layer_ids["profiles"]
    dattr.Name = "structure_opt_profile_" + member["id"]
    set_user_strings(dattr, {{"member_id": member["id"], "profile": member["profile"]["name"]}})
    doc.Objects.AddTextDot(dot, dattr)
    label_count += 1

    if add_h_mesh(member) is not None:
        mesh_count += 1

load_count = 0
for node in solution.get("nodes", []):
    load = node.get("load_N") or [0, 0, 0]
    if abs(load[0]) + abs(load[1]) + abs(load[2]) <= 1e-6:
        continue
    p = pt(node["xyz_mm"])
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = layer_ids["loads"]
    attr.Name = "structure_opt_load_" + node["id"]
    set_user_strings(attr, {{
        "node_id": node["id"],
        "module_index": node.get("module_index"),
        "load_N": load,
    }})
    doc.Objects.AddPoint(p, attr)
    dot = Rhino.Geometry.TextDot(str(round(load[2] / 1000.0, 2)) + " kN", p)
    doc.Objects.AddTextDot(dot, attr)
    load_count += 1

doc.Views.Redraw()
print("BAKED lines={{}} meshes={{}} labels={{}} loads={{}}".format(line_count, mesh_count, label_count, load_count))
'''
    r = mcp_call(code, timeout=120)
    if r.get("status") != "success":
        raise RuntimeError(f"Rhino bake failed: {r}")
    return r.get("result", {}).get("output", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solution", default=str(DEFAULT_SOLUTION))
    parser.add_argument("--no-purge", action="store_true")
    args = parser.parse_args(argv)

    solution_path = Path(args.solution)
    solution = json.loads(solution_path.read_text(encoding="utf-8"))
    out = bake_solution(solution, purge=not args.no_purge)
    print(out.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

