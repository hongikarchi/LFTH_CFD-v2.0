"""
Load the real_sculpture_v1 simulation VTKs back into the Rhino doc
through rhinomcp. Converts SPH coordinates (m) -> Rhino doc (mm).
Also draws pond AABB + nozzle marker.

Run on the SPH-side python; talks to rhinomcp on 127.0.0.1:1999.
"""
import json
import socket
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
ITER_DIR = PROJECT / "runs" / "iter_real_sculpture_v1"
OUT_DIR = ITER_DIR / "Out"
GEOM = PROJECT / "runs" / "_real_geom.json"

HOST, PORT = "127.0.0.1", 1999


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


def parse_vtk_points(vtk_path: Path):
    """Local VTK parser for legacy binary POLYDATA POINTS."""
    import re, struct
    raw = vtk_path.read_bytes()
    head = raw.find(b"POINTS")
    if head < 0: return []
    is_binary = b"\nBINARY\n" in raw[:head]
    le = raw.find(b"\n", head)
    header = raw[head:le].decode("ascii", "ignore")
    m = re.match(r"POINTS\s+(\d+)\s+(\w+)", header)
    if not m: return []
    n = int(m.group(1))
    dtype = m.group(2).lower()
    start = le + 1
    if is_binary:
        fmt = ">" + str(n * 3) + ("f" if dtype == "float" else "d")
        item = 4 if dtype == "float" else 8
        vals = struct.unpack(fmt, raw[start:start + n * 3 * item])
        return [(vals[i], vals[i+1], vals[i+2]) for i in range(0, n*3, 3)]
    return []


def main():
    geom = json.loads(GEOM.read_text())
    pond_min = geom["pond_bbox_m"][0]
    pond_max = geom["pond_bbox_m"][1]
    nozzle = geom["nozzle_center_m"]

    # Collect frame points (only sample every Nth frame for speed)
    vtks = sorted(OUT_DIR.glob("PartFluid_*.vtk"))
    sample_idx = list(range(0, len(vtks), 5)) + [len(vtks) - 1]
    sample_idx = sorted(set(sample_idx))
    print(f"loaded {len(vtks)} frames, drawing {len(sample_idx)} samples")

    frames = []
    for i in sample_idx:
        pts = parse_vtk_points(vtks[i])
        frames.append((i, pts))

    # Build Rhino-side code that creates pond, nozzle marker, and PointClouds
    # Coords: SI m -> Rhino mm (x1000)
    code_parts = [
        "import Rhino",
        "import Rhino.Geometry as rg",
        "from System.Drawing import Color",
        "doc = Rhino.RhinoDoc.ActiveDoc",
        "",
        "def ensure_layer(name, rgb):",
        "    idx = doc.Layers.FindByFullPath(name, -1)",
        "    if idx >= 0: return idx",
        "    i = doc.Layers.Add()",
        "    L = doc.Layers[i]",
        "    L.Name = name",
        "    L.Color = Color.FromArgb(rgb[0], rgb[1], rgb[2])",
        "    return L.Index",
        "",
        "# Pond AABB (mm)",
        f"pond_xmin={pond_min[0]*1000};pond_ymin={pond_min[1]*1000};pond_zmin=0",
        f"pond_xmax={pond_max[0]*1000};pond_ymax={pond_max[1]*1000};pond_zmax=500",
        "pli = ensure_layer('sim_pond_AABB', (0, 100, 200))",
        "pbox = rg.Box(rg.Plane.WorldXY, rg.Interval(pond_xmin, pond_xmax), rg.Interval(pond_ymin, pond_ymax), rg.Interval(pond_zmin, pond_zmax))",
        "pa = Rhino.DocObjects.ObjectAttributes(); pa.LayerIndex = pli",
        "doc.Objects.AddBrep(pbox.ToBrep(), pa)",
        "",
        "# Nozzle marker (point)",
        f"nx={nozzle[0]*1000}; ny={nozzle[1]*1000}; nz={nozzle[2]*1000}",
        "nli = ensure_layer('sim_nozzle', (255, 180, 0))",
        "na = Rhino.DocObjects.ObjectAttributes(); na.LayerIndex = nli",
        "doc.Objects.AddPoint(rg.Point3d(nx, ny, nz), na)",
        "",
    ]

    for i, pts in frames:
        if not pts:
            continue
        last = (i == sample_idx[-1])
        if last:
            # split caught/splash on last frame
            cau, spl = [], []
            for x, y, z in pts:
                in_pond = (pond_min[0] <= x <= pond_max[0] and
                           pond_min[1] <= y <= pond_max[1] and
                           z <= 0.5 + 0.5)
                (cau if in_pond else spl).append((x * 1000, y * 1000, z * 1000))
            code_parts.append("# Last frame (split)")
            code_parts.append(f"clp = rg.PointCloud(); cli = ensure_layer('sim_caught_LAST', (0, 200, 0))")
            code_parts.append(f"slp = rg.PointCloud(); sli = ensure_layer('sim_splash_LAST', (220, 80, 0))")
            for x, y, z in cau:
                code_parts.append(f"clp.Add(rg.Point3d({x:.2f},{y:.2f},{z:.2f}))")
            for x, y, z in spl:
                code_parts.append(f"slp.Add(rg.Point3d({x:.2f},{y:.2f},{z:.2f}))")
            code_parts.append("ca = Rhino.DocObjects.ObjectAttributes(); ca.LayerIndex = cli")
            code_parts.append("sa = Rhino.DocObjects.ObjectAttributes(); sa.LayerIndex = sli")
            code_parts.append("if clp.Count: doc.Objects.AddPointCloud(clp, ca)")
            code_parts.append("if slp.Count: doc.Objects.AddPointCloud(slp, sa)")
        else:
            code_parts.append(f"# frame {i}")
            code_parts.append(f"pc{i} = rg.PointCloud()")
            for x, y, z in pts:
                code_parts.append(f"pc{i}.Add(rg.Point3d({x*1000:.2f},{y*1000:.2f},{z*1000:.2f}))")
            code_parts.append(f"li{i} = ensure_layer('sim_frame_{i:04d}', (100,150,220))")
            code_parts.append(f"a{i} = Rhino.DocObjects.ObjectAttributes(); a{i}.LayerIndex = li{i}")
            code_parts.append(f"doc.Objects.AddPointCloud(pc{i}, a{i})")
            code_parts.append(f"doc.Layers[li{i}].IsVisible = False")

    code_parts.append("doc.Views.Redraw()")
    code_parts.append(f"print('loaded {len(frames)} frames')")
    code = "\n".join(code_parts)

    # rhinomcp may have payload limit. Stream in chunks if too big.
    print(f"Sending {len(code)} bytes of Rhino code...")
    r = call(code, timeout=240.0)
    if r.get("status") == "success":
        print("OK:", r.get("result", {}).get("output", "")[:500])
    else:
        print("ERR:", json.dumps(r, indent=2)[:1500])


if __name__ == "__main__":
    main()
