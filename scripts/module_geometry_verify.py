"""
Verify Python module_geometry against the live GH definition.

Steps:
  1. Read current slider values + Point input from the active GH canvas
     (via gh_read_via_rhinomcp.py path).
  2. Build the equivalent mesh in Python.
  3. Push the mesh into Rhino as a new layer "_module_py_verify" so it
     can be visually compared against the GH preview.

Run:
    python scripts/module_geometry_verify.py
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path

import numpy as np

from module_geometry import build_module_mesh

HOST, PORT = "127.0.0.1", 1999
DOC = Path(__file__).resolve().parent.parent / "runs" / "_gh_doc.json"


def call(cmd_type: str, params: dict, timeout: float = 30) -> dict:
    payload = json.dumps({"type": cmd_type, "params": params}).encode()
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


def py(code: str) -> dict:
    return call("execute_rhinoscript_python_code", {"code": code})


def read_slider_values() -> dict:
    """Read sliders and the Point input value from the GH canvas.

    Sliders without nicknames are tagged by their downstream component name so
    we can map e.g. an unnamed slider feeding 'Offset Surface.Distance' to
    `offset_dist`.
    """
    code = r"""
import json
import Grasshopper as gh
import Rhino

canvas = gh.Instances.ActiveCanvas
doc = canvas.Document

# Build input-port-GUID -> (component_name, input_name) map.
# Keyed by the receiver port (p.InstanceGuid) because GH_NumberSlider.Recipients
# returns receiver-side port GUIDs of downstream components.
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
        val = None
        try: val = float(obj.CurrentValue)
        except: pass
        # Resolve unnamed sliders by their downstream port. Sliders are
        # IGH_Param objects (no .Params); use .Recipients directly.
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
        # Persistent data first; fall back to volatile
        try:
            data = list(obj.PersistentData.AllData(True))
            if data:
                p = data[0].Value
                out["point"] = [p.X, p.Y, p.Z]
        except: pass
        if out["point"] is None:
            try:
                vd = list(obj.VolatileData.AllData(True))
                if vd:
                    p = vd[0].Value
                    out["point"] = [p.X, p.Y, p.Z]
            except: pass
print(json.dumps(out))
"""
    r = py(code)
    if r.get("status") != "success":
        raise RuntimeError(f"slider read failed: {r}")
    txt = r["result"].get("output", "")
    # Extract first balanced {...}
    s = txt.find("{")
    depth = 0; e = -1
    for i in range(s, len(txt)):
        if txt[i] == "{": depth += 1
        elif txt[i] == "}":
            depth -= 1
            if depth == 0:
                e = i + 1
                break
    return json.loads(txt[s:e])


def import_stl_to_rhino(stl_path: str, layer: str) -> dict:
    code = f'''
import Rhino
import System
import scriptcontext as sc

doc = sc.doc
idx = doc.Layers.FindByFullPath("{layer}", -1)
if idx < 0:
    L = Rhino.DocObjects.Layer()
    L.Name = "{layer}"
    L.Color = System.Drawing.Color.FromArgb(255, 80, 200, 255)
    idx = doc.Layers.Add(L)

# Purge old objects on this layer (so re-runs replace, not stack)
purged = 0
for o in list(doc.Objects):
    if o.Attributes.LayerIndex == idx:
        doc.Objects.Delete(o, True)
        purged += 1

ok = Rhino.RhinoApp.RunScript('_-Import "{stl_path}" _Enter', False)

sel = list(doc.Objects.GetSelectedObjects(False, False))
moved = 0
for o in sel:
    o.Attributes.LayerIndex = idx
    o.CommitChanges()
    moved += 1

doc.Views.Redraw()
print("imported ok=" + str(ok) + " purged=" + str(purged) + " moved=" + str(moved) + " layer_idx=" + str(idx))
'''
    return py(code)


def main():
    print("[1/3] reading slider + Point from GH canvas...")
    vals = read_slider_values()
    sliders = vals.get("sliders", {})
    point = vals.get("point") or [0.0, 0.0, 0.0]
    print(f"  point={point}")
    print(f"  sliders={sliders}")

    radius = sliders.get("radius", 4000.0)
    move_z = sliders.get("move_z", 3368.0)
    rx = sliders.get("rotation_x", 23.0)
    rz = sliders.get("rotation_z", 0.0)
    off = sliders.get("offset_dist", 50.0)

    print("[2/3] building Python mesh...")
    m = build_module_mesh(
        tuple(point), radius=radius, move_z=move_z,
        rotation_x_deg=rx, rotation_z_deg=rz, offset_dist=off,
    )
    print(f"  verts={len(m.vertices)} tris={len(m.faces)}")
    print(f"  bbox_min={m.bounds[0].tolist()}")
    print(f"  bbox_max={m.bounds[1].tolist()}")

    stl = Path(tempfile.gettempdir()) / "module_py_verify.stl"
    m.export(stl)
    print(f"  wrote {stl}")

    print("[3/3] pushing into Rhino on layer '_module_py_verify'...")
    # Rhino's _-Import expects native Windows path with backslashes; escape for Python string
    win_path = str(stl).replace("\\", "\\\\")
    r = import_stl_to_rhino(win_path, "_module_py_verify")
    print(json.dumps(r, indent=2)[:1200])


if __name__ == "__main__":
    main()
