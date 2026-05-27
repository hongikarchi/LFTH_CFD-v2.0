"""Bake the active GH 'output' Param_Geometry into Rhino layer _module_gh_bake.

Lets us put the GH-produced geometry side-by-side with the Python build so the
shapes can be compared 1:1 instead of guessing from preview.
"""
import json
import socket

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


CODE = r"""
import json
import Rhino
import System
import scriptcontext as sc
import Grasshopper as gh

doc = sc.doc
canvas = gh.Instances.ActiveCanvas
ghdoc = canvas.Document

# Find the Param_Geometry output (nickname == 'output').
target = None
for obj in ghdoc.Objects:
    if obj.GetType().Name == "Param_Geometry" and obj.NickName == "output":
        target = obj
        break
if target is None:
    print(json.dumps({"error": "no Param_Geometry 'output' on canvas"}))
else:
    layer_name = "_module_gh_bake"
    idx = doc.Layers.FindByFullPath(layer_name, -1)
    if idx < 0:
        L = Rhino.DocObjects.Layer()
        L.Name = layer_name
        L.Color = System.Drawing.Color.FromArgb(255, 80, 255, 100)
        L.IsVisible = True
        idx = doc.Layers.Add(L)

    # Purge old bake
    purged = 0
    for o in list(doc.Objects):
        if o.Attributes.LayerIndex == idx:
            doc.Objects.Delete(o, True)
            purged += 1

    # Bake every geometry in VolatileData (current run state)
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = idx
    baked = 0
    types = []
    bboxes = []
    for item in target.VolatileData.AllData(True):
        if item is None: continue
        geom = item.Value
        if geom is None: continue
        types.append(type(geom).__name__)
        gid = doc.Objects.Add(geom, attr)
        if gid != System.Guid.Empty:
            baked += 1
            bb = geom.GetBoundingBox(True)
            bboxes.append({"min": [bb.Min.X, bb.Min.Y, bb.Min.Z],
                            "max": [bb.Max.X, bb.Max.Y, bb.Max.Z]})

    doc.Views.Redraw()
    print(json.dumps({
        "purged": purged,
        "baked": baked,
        "geometry_types": types,
        "bboxes": bboxes,
        "layer_idx": idx,
    }))
"""


def main():
    r = py(CODE)
    if r.get("status") != "success":
        print("ERR:", json.dumps(r, indent=2)[:1500])
        return
    txt = r["result"]["output"]
    s = txt.find("{"); depth = 0; e = -1
    for i in range(s, len(txt)):
        if txt[i] == "{": depth += 1
        elif txt[i] == "}":
            depth -= 1
            if depth == 0: e = i + 1; break
    data = json.loads(txt[s:e])
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
