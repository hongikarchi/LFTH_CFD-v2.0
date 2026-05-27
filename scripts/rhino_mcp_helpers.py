"""Rhino MCP socket helpers extracted from the (now obsolete) DSPH runner.

Reusable by any pipeline (FluidX3D, GA driver, ad-hoc tools) that needs to
push STLs / run RhinoScript Python code into a running Rhino MCP server.

Server defaults to 127.0.0.1:1999.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

HOST, PORT = "127.0.0.1", 1999


def mcp_call(code: str, timeout: float = 120.0, retries: int = 2) -> dict:
    """Send RhinoScriptPython code via MCP. Returns parsed JSON response or {error}."""
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
                time.sleep(2 + attempt * 2)
                continue
            return {"error": f"mcp connection failed: {e}"}
    return {"error": f"mcp call failed: {last_err}"}


def ensure_layer(full_path: str, argb: tuple = (255, 128, 128, 128)) -> int:
    """Create nested layer path. Returns layer index, or -1 on failure."""
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


def push_stl_to_rhino_layer(stl_path: Path, layer_full_path: str,
                            color_rgb: tuple,
                            offset_mm: tuple = (0.0, 0.0, 0.0),
                            obj_name: str = "") -> None:
    """Import STL (in METERS) into Rhino layer. Scales m->mm, translates by
    offset_mm. Replaces any prior object on the layer with the same obj_name."""
    a = 255
    argb = (a, color_rgb[0], color_rgb[1], color_rgb[2])
    lid = ensure_layer(layer_full_path, argb)
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
    Rhino.RhinoApp.RunScript('_-Import "{stl_esc}" _Enter', False)
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
        for line in out.splitlines():
            if line.startswith("imported"):
                print(f"  STL: {line}")
                return
        msg = r.get("result", {}).get("message", "")
        print(f"  STL push no confirmation. msg={msg!r} out_tail={out[-200:]!r}")
