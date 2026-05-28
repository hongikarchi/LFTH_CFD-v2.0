"""Extract env::positive / env::negative / env::nozzle / env::collider from Rhino.

Outputs:
  runs/_real_targets.json    — positive_bbox_m, negative_bbox_m, nozzles_m list, collider info
  runs/_real_collider.stl    — combined env::collider meshes as binary STL (meters)

Rhino doc is in millimeters; all outputs are in METERS (SI) for FluidX3D ingest.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import trimesh

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from rhino_mcp import mcp_call

RUNS = PROJECT / "runs"
OUT_JSON = RUNS / "_real_targets.json"
OUT_STL = RUNS / "_real_collider.stl"

POSITIVE_LAYER = "env::positive"
NEGATIVE_LAYER = "env::negative"
NOZZLE_LAYER = "env::nozzle"
COLLIDER_LAYER = "env::collider"


def _mcp_python(code: str, timeout: float = 30.0) -> str:
    r = mcp_call(code, timeout=timeout)
    if r.get("status") != "success":
        raise RuntimeError(f"MCP call failed: {r}")
    return r.get("result", {}).get("output", "")


def fetch_layer_bbox_mm(full_path: str) -> tuple[list[float], list[float]]:
    out = _mcp_python(f'''
import scriptcontext as sc
doc = sc.doc
lid = doc.Layers.FindByFullPath("{full_path}", -1)
bb_min = [1e30, 1e30, 1e30]
bb_max = [-1e30, -1e30, -1e30]
cnt = 0
for o in doc.Objects:
    if o.Attributes.LayerIndex == lid:
        b = o.Geometry.GetBoundingBox(True)
        if b.IsValid:
            for k, v in enumerate([b.Min.X, b.Min.Y, b.Min.Z]): bb_min[k] = min(bb_min[k], v)
            for k, v in enumerate([b.Max.X, b.Max.Y, b.Max.Z]): bb_max[k] = max(bb_max[k], v)
            cnt += 1
print("BBOX " + str(cnt) + " " + " ".join(str(v) for v in bb_min + bb_max))
''')
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("BBOX "):
            parts = line.split()
            cnt = int(parts[1])
            mn = [float(x) for x in parts[2:5]]
            mx = [float(x) for x in parts[5:8]]
            if cnt == 0:
                raise RuntimeError(f"layer {full_path} has 0 objects")
            return mn, mx
    # legacy tuple format fallback
    import re
    m = re.search(r"BBOX',\s*(\d+),\s*([-\d.eE+]+),\s*([-\d.eE+]+),\s*([-\d.eE+]+),\s*([-\d.eE+]+),\s*([-\d.eE+]+),\s*([-\d.eE+]+)", out)
    if m:
        cnt = int(m.group(1))
        nums = [float(m.group(i)) for i in range(2, 8)]
        return nums[0:3], nums[3:6]
    raise RuntimeError(f"no BBOX line in MCP output for {full_path}: {out}")


def fetch_nozzle_points_mm(layer: str) -> list[list[float]]:
    out = _mcp_python(f'''
import scriptcontext as sc, Rhino
doc = sc.doc
lid = doc.Layers.FindByFullPath("{layer}", -1)
pts = []
for o in doc.Objects:
    if o.Attributes.LayerIndex == lid:
        g = o.Geometry
        # PointObject.Geometry is a Rhino.Geometry.Point (wraps Point3d)
        if isinstance(g, Rhino.Geometry.Point):
            p = g.Location
            pts.append((p.X, p.Y, p.Z))
print("NPTS " + str(len(pts)))
for p in pts:
    print("PT " + str(p[0]) + " " + str(p[1]) + " " + str(p[2]))
''')
    # MCP echoes stdout twice — read NPTS announcement, take first N "PT" lines.
    n_expected = None
    pts = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("NPTS ") and n_expected is None:
            try:
                n_expected = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("PT "):
            parts = line.split()
            try:
                pts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            except (IndexError, ValueError):
                pass
        if n_expected is not None and len(pts) >= n_expected:
            break
    return pts


def fetch_collider_mesh_data(layer: str, only_names: list[str] | None = None) -> tuple[list[list[float]], list[list[int]], list[dict]]:
    """Returns combined (vertices_mm, faces) plus per-object info.

    Each Brep gets meshed via Mesh.CreateFromBrep; existing Meshes used as-is.
    If only_names is given, restrict to objects whose Attributes.Name matches.
    """
    name_filter = ""
    if only_names:
        name_list = ",".join(f'"{n}"' for n in only_names)
        name_filter = f"\n    if o.Attributes.Name not in [{name_list}]: continue"
    out = _mcp_python(f'''
import scriptcontext as sc, Rhino
import json as _json
doc = sc.doc
lid = doc.Layers.FindByFullPath("{layer}", -1)
all_meshes = []
infos = []
for o in doc.Objects:
    if o.Attributes.LayerIndex != lid: continue{name_filter}
    g = o.Geometry
    meshes_for_obj = []
    if isinstance(g, Rhino.Geometry.Mesh):
        meshes_for_obj = [g.DuplicateMesh()]
    elif isinstance(g, Rhino.Geometry.Brep):
        mp = Rhino.Geometry.MeshingParameters.Default
        meshes_for_obj = list(Rhino.Geometry.Mesh.CreateFromBrep(g, mp) or [])
    else:
        continue
    if not meshes_for_obj:
        continue
    obj_name = o.Attributes.Name or ""
    obj_id = str(o.Id)
    nverts_obj = 0; nfaces_obj = 0
    for m in meshes_for_obj:
        nverts_obj += m.Vertices.Count
        nfaces_obj += m.Faces.Count
    infos.append({{"name": obj_name, "id": obj_id, "verts": nverts_obj, "faces": nfaces_obj}})
    all_meshes.extend(meshes_for_obj)

# combine and dump
big = Rhino.Geometry.Mesh()
for m in all_meshes:
    big.Append(m)
print("VERTS_FACES " + str(big.Vertices.Count) + " " + str(big.Faces.Count))
for i in range(big.Vertices.Count):
    v = big.Vertices[i]
    print("V " + str(v.X) + " " + str(v.Y) + " " + str(v.Z))
for i in range(big.Faces.Count):
    f = big.Faces[i]
    # MeshFace may be triangle or quad; emit two triangles for quads
    print("F " + str(f.A) + " " + str(f.B) + " " + str(f.C) + " " + str(f.D) + " " + ("1" if f.IsQuad else "0"))
print("INFOS " + _json.dumps(infos))
''', timeout=60)
    # MCP echoes stdout twice — bound by VERTS_FACES counts to avoid duplicates.
    verts: list[list[float]] = []
    tris: list[list[int]] = []
    infos: list[dict] = []
    n_verts_expected = None
    n_faces_expected = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("VERTS_FACES "):
            if n_verts_expected is None:
                try:
                    parts = line.split()
                    n_verts_expected = int(parts[1])
                    n_faces_expected = int(parts[2])
                except (IndexError, ValueError):
                    pass
        elif line.startswith("V ") and (n_verts_expected is None or len(verts) < n_verts_expected):
            parts = line.split()
            try:
                verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            except (IndexError, ValueError):
                pass
        elif line.startswith("F ") and (n_faces_expected is None or len(tris) < n_faces_expected * 2):
            parts = line.split()
            try:
                a, b, c, d, is_quad = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
                tris.append([a, b, c])
                if is_quad:
                    tris.append([a, c, d])
            except (IndexError, ValueError):
                pass
        elif line.startswith("INFOS ") and not infos:
            try:
                infos = json.loads(line[len("INFOS "):])
            except json.JSONDecodeError:
                pass
    return verts, tris, infos


def main() -> int:
    print(f"reading {POSITIVE_LAYER} ...")
    pos_min, pos_max = fetch_layer_bbox_mm(POSITIVE_LAYER)
    print(f"  bbox_mm: {pos_min} -> {pos_max}")

    print(f"reading {NEGATIVE_LAYER} ...")
    neg_min, neg_max = fetch_layer_bbox_mm(NEGATIVE_LAYER)
    print(f"  bbox_mm: {neg_min} -> {neg_max}")

    print(f"reading {NOZZLE_LAYER} ...")
    nozzles_mm = fetch_nozzle_points_mm(NOZZLE_LAYER)
    print(f"  {len(nozzles_mm)} nozzle points")
    if not nozzles_mm:
        raise RuntimeError("no nozzles found")

    print(f"reading {COLLIDER_LAYER} (combine + mesh) ...")
    verts_mm, tris, infos = fetch_collider_mesh_data(COLLIDER_LAYER)
    print(f"  combined: {len(verts_mm)} verts, {len(tris)} tris (over {len(infos)} objects)")

    # to meters
    V_m = np.asarray(verts_mm, dtype=np.float64) / 1000.0
    F = np.asarray(tris, dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=V_m, faces=F, process=True)
    mesh.export(OUT_STL, file_type="stl")
    print(f"  wrote {OUT_STL}  ({len(mesh.faces)} faces after process)")

    def mm_to_m(p): return [v / 1000.0 for v in p]

    targets = {
        "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "rhino_doc": "test.3dm",
        "positive_layer": POSITIVE_LAYER,
        "positive_bbox_m": [mm_to_m(pos_min), mm_to_m(pos_max)],
        "negative_layer": NEGATIVE_LAYER,
        "negative_bbox_m": [mm_to_m(neg_min), mm_to_m(neg_max)],
        "nozzle_layer": NOZZLE_LAYER,
        "n_nozzles": len(nozzles_mm),
        "nozzles_m": [mm_to_m(p) for p in nozzles_mm],
        "collider_layer": COLLIDER_LAYER,
        "collider_stl": str(OUT_STL).replace("\\", "/"),
        "collider_bbox_m": [V_m.min(axis=0).tolist(), V_m.max(axis=0).tolist()],
        "collider_objects": infos,
    }
    OUT_JSON.write_text(json.dumps(targets, indent=2), encoding="utf-8")
    print(f"  wrote {OUT_JSON}")

    # summary
    print()
    print("=" * 50)
    print(f"positive bbox m: {targets['positive_bbox_m']}")
    print(f"negative bbox m: {targets['negative_bbox_m']}")
    print(f"collider bbox m: {targets['collider_bbox_m']}")
    print(f"nozzles: {len(nozzles_mm)} @ z avg={np.mean([p[2] for p in nozzles_mm])/1000.0:.3f} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
