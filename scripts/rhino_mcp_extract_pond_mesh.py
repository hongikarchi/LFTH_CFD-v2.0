"""
Extract the end_point mesh (real pond shape) and write as STL (m).
Also pick N nozzle hole positions distributed within the start_point
bbox and write to runs/_real_geom.json.
"""
import json
import math
import socket
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUT_END_STL = PROJECT / "runs" / "_real_endpoint.stl"
GEOM = PROJECT / "runs" / "_real_geom.json"

HOST, PORT = "127.0.0.1", 1999
N_HOLES = 5

CODE = r"""
import scriptcontext as sc
import Rhino
import json

doc = Rhino.RhinoDoc.ActiveDoc

def layer_objs(name):
    for li in range(doc.Layers.Count):
        x = doc.Layers[li]
        if not x.IsDeleted and x.FullPath == name:
            objs = doc.Objects.FindByLayer(x)
            return list(objs) if objs is not None else []
    return []

def merge_meshes(objs):
    m = Rhino.Geometry.Mesh()
    for o in objs:
        g = o.Geometry
        if isinstance(g, Rhino.Geometry.Mesh):
            m.Append(g)
        elif isinstance(g, Rhino.Geometry.Brep):
            for sub in Rhino.Geometry.Mesh.CreateFromBrep(g, Rhino.Geometry.MeshingParameters.Default):
                m.Append(sub)
    m.Vertices.CombineIdentical(True, True)
    m.Faces.ConvertQuadsToTriangles()
    m.FaceNormals.ComputeFaceNormals()
    return m

mm_to_m = 0.001
end_objs = layer_objs("end_point")
end_mesh = merge_meshes(end_objs) if end_objs else None
out = {}
if end_mesh is not None and end_mesh.Faces.Count > 0:
    lines = ["solid endpoint"]
    V = end_mesh.Vertices; F = end_mesh.Faces; FN = end_mesh.FaceNormals
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
    lines.append("endsolid endpoint")
    f = open(r"%END_STL%", "w")
    try: f.write("\n".join(lines))
    finally: f.close()
    bb = end_mesh.GetBoundingBox(True)
    out["end_stl_written"] = True
    out["end_triangles"] = F.Count
    out["end_bbox_m"] = [[bb.Min.X*mm_to_m, bb.Min.Y*mm_to_m, bb.Min.Z*mm_to_m],
                         [bb.Max.X*mm_to_m, bb.Max.Y*mm_to_m, bb.Max.Z*mm_to_m]]

# Sample N points across start_point area: pick a grid pattern within the bbox
start_objs = layer_objs("start_point")
if start_objs:
    sb = None
    for o in start_objs:
        b = o.Geometry.GetBoundingBox(True)
        if sb is None: sb = Rhino.Geometry.BoundingBox(b.Min, b.Max)
        else: sb.Union(b)
    N = %N%
    # arrange N holes in a roughly square grid that fits inside the bbox
    import math
    side = int(math.ceil(math.sqrt(N)))
    holes = []
    dx_full = sb.Max.X - sb.Min.X
    dy_full = sb.Max.Y - sb.Min.Y
    # Inset 20% so holes are not on the very edge
    inset = 0.20
    x0 = sb.Min.X + dx_full * inset
    x1 = sb.Max.X - dx_full * inset
    y0 = sb.Min.Y + dy_full * inset
    y1 = sb.Max.Y - dy_full * inset
    z = sb.Center.Z
    for i in range(N):
        if side == 1:
            hx, hy = (x0+x1)/2.0, (y0+y1)/2.0
        else:
            r = i // side
            c = i % side
            hx = x0 + (x1 - x0) * (c / float(side - 1)) if side > 1 else (x0+x1)/2.0
            hy = y0 + (y1 - y0) * (r / float(side - 1)) if side > 1 else (y0+y1)/2.0
        holes.append([hx*mm_to_m, hy*mm_to_m, z*mm_to_m])
    out["nozzle_holes_m"] = holes[:N]

print(json.dumps(out, indent=2))
""".replace("%END_STL%", str(OUT_END_STL).replace("\\", "\\\\")).replace("%N%", str(N_HOLES))


def call(code: str, timeout: float = 120.0):
    payload = json.dumps({"type": "execute_rhinoscript_python_code", "params": {"code": code}}).encode("utf-8")
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
    return {"error": "no json"}


def main():
    res = call(CODE)
    if res.get("status") != "success":
        print("ERROR:", json.dumps(res, indent=2)[:2000])
        return
    out_text = res["result"].get("output", "")
    start = out_text.find("{")
    if start < 0:
        print("No JSON")
        return
    depth = 0; end = -1
    for i, ch in enumerate(out_text[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1; break
    info = json.loads(out_text[start:end])
    print(json.dumps(info, indent=2))

    # Merge with existing _real_geom.json
    existing = json.loads(GEOM.read_text(encoding="utf-8")) if GEOM.exists() else {}
    existing.update(info)
    GEOM.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"\nMerged into {GEOM}")
    if info.get("end_stl_written"):
        print(f"endpoint STL: {OUT_END_STL}")


if __name__ == "__main__":
    main()
