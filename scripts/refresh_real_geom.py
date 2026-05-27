"""Refresh runs/_real_geom.json with the current Rhino setup:
  - 230 nozzle centers from layer 'start_point'
  - pond from layer 'end_point' (bbox + STL)
  - collider top-4 module bboxes (kept here for legacy compat; primary source
    of truth for modules is runs/_collider_modules.json)
"""
import json
import socket
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUT = PROJECT / "runs" / "_real_geom.json"
END_STL = PROJECT / "runs" / "_real_endpoint.stl"

HOST, PORT = "127.0.0.1", 1999


def py(code: str, timeout: float = 60) -> dict:
    payload = json.dumps({"type": "execute_rhinoscript_python_code",
                          "params": {"code": code}}).encode()
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
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


def extract_balanced_json(text: str) -> dict:
    s = text.find("{"); depth = 0; e = -1
    for i in range(s, len(text)):
        if text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0: e = i + 1; break
    return json.loads(text[s:e])


CODE = r"""
import json
import Rhino, scriptcontext as sc
import System

doc = sc.doc

# --- 1) Nozzles from start_point layer ---
sp_idx = -1
for L in doc.Layers:
    if L.Name == "start_point":
        sp_idx = L.Index; break

nozzles_mm = []
for o in doc.Objects:
    if o.Attributes.LayerIndex == sp_idx:
        bb = o.Geometry.GetBoundingBox(True)
        nozzles_mm.append([
            (bb.Min.X + bb.Max.X) / 2.0,
            (bb.Min.Y + bb.Max.Y) / 2.0,
            (bb.Min.Z + bb.Max.Z) / 2.0,
        ])

# --- 2) Pond from end_point layer ---
ep_idx = -1
for L in doc.Layers:
    if L.Name == "end_point":
        ep_idx = L.Index; break

pond_bb_min = [1e18, 1e18, 1e18]
pond_bb_max = [-1e18, -1e18, -1e18]
end_meshes = []
for o in doc.Objects:
    if o.Attributes.LayerIndex == ep_idx:
        bb = o.Geometry.GetBoundingBox(True)
        for i, v in enumerate([bb.Min.X, bb.Min.Y, bb.Min.Z]):
            pond_bb_min[i] = min(pond_bb_min[i], v)
        for i, v in enumerate([bb.Max.X, bb.Max.Y, bb.Max.Z]):
            pond_bb_max[i] = max(pond_bb_max[i], v)
        # Collect as mesh for STL export
        g = o.Geometry
        if isinstance(g, Rhino.Geometry.Mesh):
            end_meshes.append(g.DuplicateMesh())
        elif isinstance(g, Rhino.Geometry.Brep):
            mp = Rhino.Geometry.MeshingParameters.Default
            ms = Rhino.Geometry.Mesh.CreateFromBrep(g, mp)
            if ms:
                for m in ms:
                    if m is not None:
                        end_meshes.append(m)

# Combine end meshes -> single mesh in meters -> ASCII STL
import os
END_STL_PATH = r"__END_STL__"
end_tri = 0
if end_meshes:
    combined = Rhino.Geometry.Mesh()
    for m in end_meshes:
        combined.Append(m)
    combined.Faces.ConvertQuadsToTriangles()
    combined.Normals.ComputeNormals()
    combined.FaceNormals.ComputeFaceNormals()

    V = combined.Vertices; F = combined.Faces; FN = combined.FaceNormals
    lines = ["solid end_point"]
    for fi in range(F.Count):
        f = F[fi]
        n = FN[fi]
        a = V[f.A]; b = V[f.B]; c = V[f.C]
        ax, ay, az = a.X * 0.001, a.Y * 0.001, a.Z * 0.001
        bx, by, bz = b.X * 0.001, b.Y * 0.001, b.Z * 0.001
        cx, cy, cz = c.X * 0.001, c.Y * 0.001, c.Z * 0.001
        lines.append("  facet normal " + str(n.X) + " " + str(n.Y) + " " + str(n.Z))
        lines.append("    outer loop")
        lines.append("      vertex " + str(ax) + " " + str(ay) + " " + str(az))
        lines.append("      vertex " + str(bx) + " " + str(by) + " " + str(bz))
        lines.append("      vertex " + str(cx) + " " + str(cy) + " " + str(cz))
        lines.append("    endloop")
        lines.append("  endfacet")
        if f.IsQuad:
            d = V[f.D]
            dx, dy, dz = d.X * 0.001, d.Y * 0.001, d.Z * 0.001
            lines.append("  facet normal " + str(n.X) + " " + str(n.Y) + " " + str(n.Z))
            lines.append("    outer loop")
            lines.append("      vertex " + str(ax) + " " + str(ay) + " " + str(az))
            lines.append("      vertex " + str(cx) + " " + str(cy) + " " + str(cz))
            lines.append("      vertex " + str(dx) + " " + str(dy) + " " + str(dz))
            lines.append("    endloop")
            lines.append("  endfacet")
            end_tri += 1
        end_tri += 1
    lines.append("endsolid end_point")
    with open(END_STL_PATH, "w") as fh:
        fh.write("\n".join(lines))

# --- 3) Aggregate collider bbox (M0..M3 only) from _collider_modules.json (in meters) ---
# We don't read that JSON here; the runner re-reads it. Just leave a placeholder.

out = {
    "n_nozzles": len(nozzles_mm),
    "nozzles_mm": nozzles_mm,
    "pond_bbox_mm": [pond_bb_min, pond_bb_max],
    "end_triangles": end_tri,
    "end_stl_written": (end_tri > 0),
}
print(json.dumps(out))
"""


def main():
    code = CODE.replace("__END_STL__", str(END_STL).replace("\\", "\\\\"))
    r = py(code)
    if r.get("status") != "success":
        print("ERR:", json.dumps(r, indent=2)[:1500]); return
    data = extract_balanced_json(r["result"]["output"])

    nozzles_mm = data["nozzles_mm"]
    nozzles_m = [[c[0] / 1000.0, c[1] / 1000.0, c[2] / 1000.0] for c in nozzles_mm]
    pond_mm_min, pond_mm_max = data["pond_bbox_mm"]
    pond_m = [[v / 1000.0 for v in pond_mm_min], [v / 1000.0 for v in pond_mm_max]]
    pond_center_m = [(pond_m[0][i] + pond_m[1][i]) / 2.0 for i in range(3)]

    # Bbox of all nozzles
    xs = [c[0] for c in nozzles_m]; ys = [c[1] for c in nozzles_m]; zs = [c[2] for c in nozzles_m]
    start_bbox_m = [[min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]]
    nozzle_center_m = [sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs)]

    geom = {
        "n_nozzles": len(nozzles_m),
        "nozzle_holes_m": nozzles_m,
        "nozzle_center_m": nozzle_center_m,
        "start_bbox_m": start_bbox_m,
        "pond_bbox_m": pond_m,
        "pond_center_m": pond_center_m,
        "end_stl_written": data["end_stl_written"],
        "end_triangles": data["end_triangles"],
    }
    OUT.write_text(json.dumps(geom, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"  n_nozzles = {len(nozzles_m)}")
    print(f"  nozzle z = {start_bbox_m[0][2]:.3f}..{start_bbox_m[1][2]:.3f} m")
    print(f"  nozzle x range = [{start_bbox_m[0][0]:.3f}, {start_bbox_m[1][0]:.3f}] m ({(start_bbox_m[1][0]-start_bbox_m[0][0])*1000:.0f} mm)")
    print(f"  nozzle y range = [{start_bbox_m[0][1]:.3f}, {start_bbox_m[1][1]:.3f}] m ({(start_bbox_m[1][1]-start_bbox_m[0][1])*1000:.0f} mm)")
    print(f"  pond bbox = {pond_m}")
    print(f"  end_stl = {data['end_triangles']} tris -> {END_STL}")


if __name__ == "__main__":
    main()
