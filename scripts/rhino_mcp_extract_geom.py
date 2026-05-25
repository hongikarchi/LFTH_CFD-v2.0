"""
Pull collider/start_point/end_point from the running Rhino doc through
the rhinomcp socket. Write an ASCII STL of the collider meshes
(converted mm -> m), and emit a JSON file with nozzle position + pond
AABB derived from start_point and end_point centroids/bboxes.
"""
import json
import socket
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUT_STL = PROJECT / "runs" / "_real_sculpture.stl"
OUT_JSON = PROJECT / "runs" / "_real_geom.json"

HOST, PORT = "127.0.0.1", 1999

RHINO_CODE = r"""
import scriptcontext as sc
import Rhino
import json

doc = Rhino.RhinoDoc.ActiveDoc

def layer_objs(name):
    L = None
    for li in range(doc.Layers.Count):
        x = doc.Layers[li]
        if not x.IsDeleted and x.FullPath == name:
            L = x; break
    if L is None: return []
    objs = doc.Objects.FindByLayer(L)
    return list(objs) if objs is not None else []

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
    m.Normals.ComputeNormals()
    m.FaceNormals.ComputeFaceNormals()
    return m

def bbox(objs):
    bb = None
    for o in objs:
        b = o.Geometry.GetBoundingBox(True)
        if not b.IsValid: continue
        if bb is None: bb = Rhino.Geometry.BoundingBox(b.Min, b.Max)
        else: bb.Union(b)
    return bb

collider = layer_objs("collider")
start_pt = layer_objs("start_point")
end_pt   = layer_objs("end_point")

mm_to_m = 0.001
out = {
    "n_collider": len(collider),
    "n_start": len(start_pt),
    "n_end": len(end_pt),
}

# Merge collider into single mesh
col_mesh = merge_meshes(collider) if collider else None
if col_mesh is not None and col_mesh.Faces.Count > 0:
    # Move to origin via doc transform? No - keep absolute coords, just scale mm->m
    lines = []
    lines.append("solid collider")
    V = col_mesh.Vertices
    F = col_mesh.Faces
    FN = col_mesh.FaceNormals
    if FN.Count != F.Count:
        col_mesh.FaceNormals.ComputeFaceNormals()
        FN = col_mesh.FaceNormals
    for i in range(F.Count):
        f = F[i]
        n = FN[i]
        a = V[f.A]; b = V[f.B]; c = V[f.C]
        lines.append("  facet normal {0:.6e} {1:.6e} {2:.6e}".format(n.X, n.Y, n.Z))
        lines.append("    outer loop")
        lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(a.X*mm_to_m, a.Y*mm_to_m, a.Z*mm_to_m))
        lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(b.X*mm_to_m, b.Y*mm_to_m, b.Z*mm_to_m))
        lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(c.X*mm_to_m, c.Y*mm_to_m, c.Z*mm_to_m))
        lines.append("    endloop")
        lines.append("  endfacet")
        if f.IsQuad:
            lines.append("  facet normal {0:.6e} {1:.6e} {2:.6e}".format(n.X, n.Y, n.Z))
            lines.append("    outer loop")
            lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(a.X*mm_to_m, a.Y*mm_to_m, a.Z*mm_to_m))
            d = V[f.D] if f.IsQuad else V[f.A]
            lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(c.X*mm_to_m, c.Y*mm_to_m, c.Z*mm_to_m))
            lines.append("      vertex {0:.6e} {1:.6e} {2:.6e}".format(d.X*mm_to_m, d.Y*mm_to_m, d.Z*mm_to_m))
            lines.append("    endloop")
            lines.append("  endfacet")
    lines.append("endsolid collider")
    stl_text = "\n".join(lines)
    f = open(r"%STL_PATH%", "w")
    try: f.write(stl_text)
    finally: f.close()
    out["stl_written"] = True
    out["stl_triangles"] = F.Count
    bb = col_mesh.GetBoundingBox(True)
    out["collider_bbox_m"] = [[bb.Min.X*mm_to_m, bb.Min.Y*mm_to_m, bb.Min.Z*mm_to_m],
                              [bb.Max.X*mm_to_m, bb.Max.Y*mm_to_m, bb.Max.Z*mm_to_m]]

# start_point: take centroid as nozzle position
sb = bbox(start_pt)
if sb is not None and sb.IsValid:
    c = sb.Center
    out["nozzle_center_m"] = [c.X*mm_to_m, c.Y*mm_to_m, c.Z*mm_to_m]
    out["start_bbox_m"] = [[sb.Min.X*mm_to_m, sb.Min.Y*mm_to_m, sb.Min.Z*mm_to_m],
                            [sb.Max.X*mm_to_m, sb.Max.Y*mm_to_m, sb.Max.Z*mm_to_m]]

# end_point: take AABB as pond
eb = bbox(end_pt)
if eb is not None and eb.IsValid:
    out["pond_bbox_m"] = [[eb.Min.X*mm_to_m, eb.Min.Y*mm_to_m, eb.Min.Z*mm_to_m],
                          [eb.Max.X*mm_to_m, eb.Max.Y*mm_to_m, eb.Max.Z*mm_to_m]]
    c = eb.Center
    out["pond_center_m"] = [c.X*mm_to_m, c.Y*mm_to_m, c.Z*mm_to_m]

print(json.dumps(out, indent=2))
""".replace("%STL_PATH%", str(OUT_STL).replace("\\", "\\\\"))


def call(code: str, timeout: float = 90.0) -> dict:
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
    OUT_STL.parent.mkdir(parents=True, exist_ok=True)
    res = call(RHINO_CODE)
    if res.get("status") != "success":
        print("ERROR:", json.dumps(res, indent=2)[:2000])
        return
    output = res["result"].get("output", "")
    # The script printed JSON; rhinomcp may double-print. Take first JSON object.
    txt = output.strip()
    # Find first {...}
    start = txt.find("{")
    if start < 0:
        print("No JSON in output:", txt[:500])
        return
    depth = 0; end = -1
    for i, ch in enumerate(txt[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        print("Unbalanced JSON:", txt[:500])
        return
    info = json.loads(txt[start:end])
    print(json.dumps(info, indent=2))

    OUT_JSON.write_text(json.dumps(info, indent=2), encoding="utf-8")
    print("\nSaved geom info to:", OUT_JSON)
    if info.get("stl_written"):
        print("Saved STL to:", OUT_STL, f"({info.get('stl_triangles')} triangles)")


if __name__ == "__main__":
    main()
