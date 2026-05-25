"""Delete prior sim_* / stream_* viz layers and their objects via rhinomcp."""
import json
import socket

HOST, PORT = "127.0.0.1", 1999

code = r"""
import Rhino
doc = Rhino.RhinoDoc.ActiveDoc
prefixes = ("sim_", "stream_", "iter_", "frame_")
def is_viz(name):
    base = name.split("::")[-1]
    if name.endswith("_LAST") or name == "pond_AABB" or name == "sim_pond_AABB" or name == "sim_nozzle":
        return True
    for p in prefixes:
        if base.startswith(p):
            return True
    return False
n_obj = 0; n_lay = 0
viz_ids = []
for li in range(doc.Layers.Count):
    L = doc.Layers[li]
    if L.IsDeleted: continue
    if is_viz(L.FullPath):
        viz_ids.append(L.Id)
for lid in viz_ids:
    L = doc.Layers.FindId(lid)
    if L is None: continue
    objs = doc.Objects.FindByLayer(L)
    if objs is not None:
        for o in objs:
            doc.Objects.Delete(o, True)
            n_obj += 1
for lid in viz_ids:
    L = doc.Layers.FindId(lid)
    if L is None: continue
    if doc.Layers.Delete(L.LayerIndex, True):
        n_lay += 1
doc.Views.Redraw()
print("deleted objects:", n_obj)
print("deleted layers:", n_lay)
"""

s = socket.create_connection((HOST, PORT), timeout=60)
s.settimeout(60)
s.sendall(json.dumps({"type": "execute_rhinoscript_python_code", "params": {"code": code}}).encode())
buf = b""
while True:
    try: c = s.recv(65536)
    except socket.timeout: break
    if not c: break
    buf += c
    try:
        d = json.loads(buf.decode("utf-8", "ignore"))
        print(d.get("result", {}).get("output", d))
        break
    except json.JSONDecodeError: continue
s.close()
