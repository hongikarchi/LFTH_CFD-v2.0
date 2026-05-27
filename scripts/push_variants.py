"""Push 3 module variants to Rhino, spaced apart on X for visual comparison."""
import json
import socket
import sys
import tempfile
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_geometry import build_module_mesh  # noqa: E402

HOST, PORT = "127.0.0.1", 1999


def py(code: str) -> dict:
    payload = json.dumps({"type": "execute_rhinoscript_python_code",
                          "params": {"code": code}}).encode()
    with socket.create_connection((HOST, PORT), timeout=30) as s:
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


def push(name: str, mesh: trimesh.Trimesh, layer: str, color_rgb: tuple) -> None:
    stl = Path(tempfile.gettempdir()) / f"module_{name}.stl"
    mesh.export(stl)
    stl_esc = str(stl).replace("\\", "\\\\")
    r, g, b = color_rgb
    code = f'''
import Rhino, System, scriptcontext as sc
doc = sc.doc
# Deselect everything before importing so we only catch the new objects.
doc.Objects.UnselectAll()

idx = doc.Layers.FindByFullPath("{layer}", -1)
if idx < 0:
    L = Rhino.DocObjects.Layer()
    L.Name = "{layer}"
    L.Color = System.Drawing.Color.FromArgb(255, {r}, {g}, {b})
    L.IsVisible = True
    idx = doc.Layers.Add(L)
else:
    # Force the layer visible in case it was toggled off.
    L = doc.Layers[idx]
    L.IsVisible = True
    doc.Layers.Modify(L, idx, True)

purged = 0
for o in list(doc.Objects):
    if o.Attributes.LayerIndex == idx:
        doc.Objects.Delete(o, True)
        purged += 1
doc.Objects.UnselectAll()

ok = Rhino.RhinoApp.RunScript('_-Import "{stl_esc}" _Enter', False)
sel = list(doc.Objects.GetSelectedObjects(False, False))
moved = 0
for o in sel:
    o.Attributes.LayerIndex = idx
    o.CommitChanges()
    moved += 1
doc.Objects.UnselectAll()
doc.Views.Redraw()
print("{layer}: ok=" + str(ok) + " purged=" + str(purged) + " moved=" + str(moved) + " visible=" + str(doc.Layers[idx].IsVisible))
'''
    out = py(code)
    print(out.get("result", {}).get("output", "").strip())


def read_inputs():
    code = r"""
import json, Grasshopper as gh
doc = gh.Instances.ActiveCanvas.Document
port_to_recv = {}
for obj in doc.Objects:
    if hasattr(obj, "Params"):
        for p in obj.Params.Input:
            port_to_recv[str(p.InstanceGuid)] = (obj.NickName or obj.Name, p.Name)
out = {"sliders": {}, "point": None}
for obj in doc.Objects:
    n = obj.GetType().Name
    if n == "GH_NumberSlider":
        nick = obj.NickName or ""
        try: val = float(obj.CurrentValue)
        except: val = None
        if (not nick or nick == "Number Slider") and hasattr(obj, "Recipients"):
            for rec in obj.Recipients:
                info = port_to_recv.get(str(rec.InstanceGuid))
                if info:
                    comp, in_name = info
                    if "Offset" in comp and in_name == "Distance":
                        nick = "offset_dist"
                        break
        out["sliders"][nick or "unnamed"] = val
    elif n == "Param_Point" and obj.NickName == "Point":
        try:
            d = list(obj.PersistentData.AllData(True))
            if d:
                p = d[0].Value
                out["point"] = [p.X, p.Y, p.Z]
        except: pass
        if out["point"] is None:
            try:
                vd = list(obj.VolatileData.AllData(True))
                if vd:
                    p = vd[0].Value; out["point"] = [p.X, p.Y, p.Z]
            except: pass
print(json.dumps(out))
"""
    r = py(code)
    txt = r["result"]["output"]
    s = txt.find("{"); depth = 0; e = -1
    for i in range(s, len(txt)):
        if txt[i] == "{": depth += 1
        elif txt[i] == "}":
            depth -= 1
            if depth == 0: e = i + 1; break
    return json.loads(txt[s:e])


def build_open_cap(P, radius, move_z, rx_deg, rz_deg):
    sphere = trimesh.creation.icosphere(subdivisions=4, radius=radius)
    sphere.apply_translation((P[0], P[1], P[2] + move_z))
    mask = sphere.vertices[:, 2] <= P[2]
    keep = mask[sphere.faces].all(axis=1)
    kept = sphere.faces[keep]
    used = np.unique(kept)
    remap = -np.ones(len(sphere.vertices), dtype=np.int64)
    remap[used] = np.arange(len(used))
    cap = trimesh.Trimesh(vertices=sphere.vertices[used], faces=remap[kept], process=True)
    Pn = np.array(P, dtype=float)
    cap.apply_transform(trimesh.transformations.rotation_matrix(np.radians(rx_deg), [1, 0, 0], Pn))
    cap.apply_transform(trimesh.transformations.rotation_matrix(np.radians(rz_deg), [0, 0, 1], Pn))
    return cap


def main():
    print("[read GH inputs]")
    vals = read_inputs()
    s = vals.get("sliders", {})
    P = vals.get("point") or [0, 0, 0]
    radius = s.get("radius", 4000)
    move_z = s.get("move_z", 3368)
    rx = s.get("rotation_x", 23)
    rz = s.get("rotation_z", 0)
    off = s.get("offset_dist", 50)
    print(f"  P={P}  radius={radius} move_z={move_z} rx={rx} rz={rz} off={off}")

    # C: full closed thickened shell (current build) at ORIGINAL Point position.
    # This is the topological match for the GH OffsetSurface output.
    C = build_module_mesh(tuple(P), radius=radius, move_z=move_z,
                          rotation_x_deg=rx, rotation_z_deg=rz, offset_dist=off)
    print(f"C_solid_shell: verts={len(C.vertices)} tris={len(C.faces)} "
          f"wt={C.is_watertight} vol={C.volume:.0f} bbox={C.bounds.tolist()}")
    push("C_solid_shell", C, "_module_py_C_solid", (255, 80, 200))

    # Clean up A/B layers so they no longer clutter the comparison.
    code = '''
import scriptcontext as sc
doc = sc.doc
for nm in ["_module_py_A_open", "_module_py_B_offset"]:
    idx = doc.Layers.FindByFullPath(nm, -1)
    if idx >= 0:
        for o in list(doc.Objects):
            if o.Attributes.LayerIndex == idx:
                doc.Objects.Delete(o, True)
doc.Views.Redraw()
print("cleaned A/B layers")
'''
    out = py(code)
    print(out.get("result", {}).get("output", "").strip())


if __name__ == "__main__":
    main()
