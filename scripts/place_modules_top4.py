"""Place 4 parametric Python modules at the top-centers of the top 4
existing collider module bboxes, then push each to its own Rhino layer.

Uses the current GH slider values for shape params (radius, move_z, rotation_x,
rotation_z, offset_dist) so the Python modules match the live GH preview's
proportions. The Point is overridden per-module based on
runs/_collider_modules.json.
"""
import json
import socket
import sys
import tempfile
from pathlib import Path

import trimesh

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
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


def read_gh_sliders() -> dict:
    code = r"""
import json, Grasshopper as gh
doc = gh.Instances.ActiveCanvas.Document
port_to_recv = {}
for obj in doc.Objects:
    if hasattr(obj, "Params"):
        for p in obj.Params.Input:
            port_to_recv[str(p.InstanceGuid)] = (obj.NickName or obj.Name, p.Name)
out = {}
for obj in doc.Objects:
    if obj.GetType().Name == "GH_NumberSlider":
        nick = obj.NickName or ""
        try: val = float(obj.CurrentValue)
        except: val = None
        if (not nick or nick == "Number Slider") and hasattr(obj, "Recipients"):
            for rec in obj.Recipients:
                info = port_to_recv.get(str(rec.InstanceGuid))
                if info:
                    comp, in_name = info
                    if "Offset" in comp and in_name == "Distance":
                        nick = "offset_dist"; break
        out[nick or "unnamed"] = val
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


def push_mesh(mesh: trimesh.Trimesh, layer: str, color_rgb: tuple) -> None:
    stl = Path(tempfile.gettempdir()) / f"{layer}.stl"
    mesh.export(stl)
    stl_esc = str(stl).replace("\\", "\\\\")
    r, g, b = color_rgb
    code = f'''
import Rhino, System, scriptcontext as sc
doc = sc.doc
doc.Objects.UnselectAll()
idx = doc.Layers.FindByFullPath("{layer}", -1)
if idx < 0:
    L = Rhino.DocObjects.Layer()
    L.Name = "{layer}"
    L.Color = System.Drawing.Color.FromArgb(255, {r}, {g}, {b})
    L.IsVisible = True
    idx = doc.Layers.Add(L)
else:
    L = doc.Layers[idx]; L.IsVisible = True; doc.Layers.Modify(L, idx, True)
purged = 0
for o in list(doc.Objects):
    if o.Attributes.LayerIndex == idx:
        doc.Objects.Delete(o, True); purged += 1
doc.Objects.UnselectAll()
ok = Rhino.RhinoApp.RunScript('_-Import "{stl_esc}" _Enter', False)
sel = list(doc.Objects.GetSelectedObjects(False, False))
moved = 0
for o in sel:
    o.Attributes.LayerIndex = idx; o.CommitChanges(); moved += 1
doc.Objects.UnselectAll()
doc.Views.Redraw()
print("{layer}: ok=" + str(ok) + " purged=" + str(purged) + " moved=" + str(moved))
'''
    out = py(code)
    print(out.get("result", {}).get("output", "").strip())


def main():
    print("[1/3] reading GH slider values")
    sliders = read_gh_sliders()
    radius = sliders.get("radius", 4000.0)
    move_z = sliders.get("move_z", 3368.0)
    rx = sliders.get("rotation_x", 23.0)
    rz = sliders.get("rotation_z", 0.0)
    off = sliders.get("offset_dist", 50.0)
    print(f"  radius={radius} move_z={move_z} rx={rx} rz={rz} off={off}")

    print("[2/3] loading collider module top-centers")
    modules = json.loads((PROJECT / "runs" / "_collider_modules.json").read_text(encoding="utf-8"))["modules"]
    top4 = [m for m in modules if m["index"] in (0, 1, 2, 3)]

    PALETTE = [
        (255, 120, 120),   # M0 red
        (255, 200, 80),    # M1 orange
        (120, 220, 120),   # M2 green
        (120, 180, 255),   # M3 blue
    ]

    print("[3/3] building + pushing each module")
    for m, color in zip(top4, PALETTE):
        bb_min, bb_max = m["bbox_mm"]
        # P = top-center of the collider bbox
        P = (
            (bb_min[0] + bb_max[0]) / 2.0,
            (bb_min[1] + bb_max[1]) / 2.0,
            bb_max[2],
        )
        mesh = build_module_mesh(
            P, radius=radius, move_z=move_z,
            rotation_x_deg=rx, rotation_z_deg=rz, offset_dist=off,
        )
        layer = f"_module_py_M{m['index']:02d}"
        print(f"  M{m['index']}: P={tuple(round(c, 1) for c in P)}  "
              f"verts={len(mesh.vertices)} tris={len(mesh.faces)} "
              f"wt={mesh.is_watertight} vol={mesh.volume:.0f}")
        push_mesh(mesh, layer, color)


if __name__ == "__main__":
    main()
