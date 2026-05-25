"""Query the running Rhino doc through rhinomcp port 1999 and summarize."""
import json, socket, sys

HOST, PORT = "127.0.0.1", 1999


def call(cmd_type: str, params: dict | None = None, timeout: float = 30.0):
    payload = json.dumps({"type": cmd_type, "params": params or {}}).encode("utf-8")
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(payload)
        buf = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode("utf-8", "ignore"))
            except json.JSONDecodeError:
                continue
    return {"raw": buf[:500].decode("utf-8", "ignore")}


# Full doc inspection via Python in Rhino
inspect_code = r"""
import scriptcontext as sc
import Rhino
import json

doc = sc.doc if isinstance(sc.doc, Rhino.RhinoDoc) else Rhino.RhinoDoc.ActiveDoc

# Units
unit_system = str(doc.ModelUnitSystem)
unit_scale_to_m = Rhino.RhinoMath.UnitScale(doc.ModelUnitSystem, Rhino.UnitSystem.Meters)

# Layer tree
layers = []
for li in range(doc.Layers.Count):
    L = doc.Layers[li]
    if L.IsDeleted:
        continue
    n_obj = 0
    objs = doc.Objects.FindByLayer(L)
    if objs:
        n_obj = len(list(objs))
    layers.append({
        "name": L.FullPath,
        "n_objects": n_obj,
        "visible": L.IsVisible,
        "color": (L.Color.R, L.Color.G, L.Color.B),
    })

# Object type summary
types = {}
total_objs = 0
bbox_all = None
for obj in doc.Objects:
    if obj is None:
        continue
    total_objs += 1
    t = obj.ObjectType.ToString()
    types[t] = types.get(t, 0) + 1
    bb = obj.Geometry.GetBoundingBox(True) if obj.Geometry else None
    if bb is not None and bb.IsValid:
        if bbox_all is None:
            bbox_all = bb
        else:
            bbox_all.Union(bb)

result = {
    "unit_system": unit_system,
    "unit_scale_to_m": unit_scale_to_m,
    "total_objects": total_objs,
    "type_counts": types,
    "n_layers": len(layers),
    "doc_bbox_min": [bbox_all.Min.X, bbox_all.Min.Y, bbox_all.Min.Z] if bbox_all else None,
    "doc_bbox_max": [bbox_all.Max.X, bbox_all.Max.Y, bbox_all.Max.Z] if bbox_all else None,
    "layers_top_15_by_obj_count": sorted(layers, key=lambda L: -L["n_objects"])[:15],
}
print(json.dumps(result, indent=2))
"""

r = call("execute_rhinoscript_python_code", {"code": inspect_code})
if r.get("status") == "success" and "result" in r:
    out = r["result"].get("output", "")
    # Strip duplicate prints
    text = out.split("\n}\n", 1)[0] + "\n}" if out.count("}\n") > 1 else out
    print(text)
else:
    print(json.dumps(r, indent=2))
