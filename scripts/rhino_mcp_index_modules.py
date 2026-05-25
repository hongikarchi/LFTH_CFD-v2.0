"""
Enumerate the 6 (or N) collider Mesh objects in the Rhino doc.
Sort top -> bottom by bbox center Z. Persist GUID list as
runs/_collider_modules.json so later transforms can address them by
stable index 0..N-1.

Each module entry also stores its original bbox + centroid for reference.
"""
import json
import socket
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUT = PROJECT / "runs" / "_collider_modules.json"
HOST, PORT = "127.0.0.1", 1999

CODE = r"""
import Rhino, json
doc = Rhino.RhinoDoc.ActiveDoc
target = None
for li in range(doc.Layers.Count):
    L = doc.Layers[li]
    if L.IsDeleted: continue
    if L.FullPath == "collider":
        target = L; break
modules = []
if target is not None:
    objs = doc.Objects.FindByLayer(target)
    if objs is not None:
        for o in objs:
            g = o.Geometry
            if g is None: continue
            bb = g.GetBoundingBox(True)
            if not bb.IsValid: continue
            modules.append({
                "guid": str(o.Id),
                "name": o.Name or "",
                "type": g.GetType().Name,
                "bbox_mm": [[bb.Min.X, bb.Min.Y, bb.Min.Z], [bb.Max.X, bb.Max.Y, bb.Max.Z]],
                "center_mm": [bb.Center.X, bb.Center.Y, bb.Center.Z],
            })
# Sort by center Z descending (top to bottom)
modules.sort(key=lambda m: -m["center_mm"][2])
for i, m in enumerate(modules):
    m["index"] = i
print(json.dumps({"modules": modules}, indent=2))
"""


def call(code, timeout=30):
    payload = json.dumps({"type": "execute_rhinoscript_python_code", "params": {"code": code}}).encode()
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.settimeout(timeout); s.sendall(payload); buf = b""
        while True:
            try: c = s.recv(65536)
            except socket.timeout: break
            if not c: break
            buf += c
            try: return json.loads(buf.decode())
            except json.JSONDecodeError: continue
    return {"error": "no json"}


def main():
    r = call(CODE)
    if r.get("status") != "success":
        print("ERR:", json.dumps(r, indent=2)[:600]); return
    out = r["result"].get("output", "")
    # rhinomcp duplicates output; find first balanced JSON object
    s = out.find("{")
    depth = 0; e = -1
    for i in range(s, len(out)):
        if out[i] == "{": depth += 1
        elif out[i] == "}":
            depth -= 1
            if depth == 0:
                e = i + 1; break
    info = json.loads(out[s:e])
    print(f"Found {len(info['modules'])} collider modules.")
    for m in info["modules"]:
        cz = m["center_mm"][2] / 1000.0
        print(f"  [{m['index']}] guid={m['guid'][:8]}... center_z={cz:+.2f}m  type={m['type']}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
