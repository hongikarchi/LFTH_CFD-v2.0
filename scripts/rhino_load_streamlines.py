"""
Push streamline polylines into the running Rhino doc through rhinomcp.
Each trajectory becomes one Polyline; color groups by nozzle origin.
Coords converted m -> mm.
"""
import json
import socket
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TRAILS = PROJECT / "runs" / "iter_streamline_v1" / "trails.json"
GEOM = PROJECT / "runs" / "_real_geom.json"
HOST, PORT = "127.0.0.1", 1999

# Distinct colors per nozzle (5 nozzles)
NOZZLE_COLORS = [
    (220, 60, 60),    # red
    (60, 180, 75),    # green
    (60, 100, 220),   # blue
    (240, 160, 40),   # orange
    (180, 60, 220),   # purple
]


def call(code, timeout=240.0):
    payload = json.dumps({"type": "execute_rhinoscript_python_code", "params": {"code": code}}).encode("utf-8")
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.settimeout(timeout); s.sendall(payload); buf = b""
        while True:
            try: c = s.recv(65536)
            except socket.timeout: break
            if not c: break
            buf += c
            try: return json.loads(buf.decode("utf-8", "ignore"))
            except json.JSONDecodeError: continue
    return {"error": "no json"}


def main():
    trails = json.loads(TRAILS.read_text())
    geom = json.loads(GEOM.read_text())
    holes = geom["nozzle_holes_m"]
    print(f"{len(trails)} trails")

    # Assign each trail to nearest nozzle based on starting position
    trail_groups = {i: [] for i in range(len(holes))}
    for idp, pts in trails.items():
        if not pts:
            continue
        start = pts[0]
        # Find nearest nozzle by XY distance
        d_best = 1e18; n_best = 0
        for i, h in enumerate(holes):
            d = (start[0] - h[0]) ** 2 + (start[1] - h[1]) ** 2
            if d < d_best:
                d_best = d; n_best = i
        trail_groups[n_best].append((idp, pts))

    # Build Rhino code — chunked to keep payload reasonable
    chunks = []
    chunk_lines = [
        "import Rhino",
        "import Rhino.Geometry as rg",
        "from System.Drawing import Color",
        "doc = Rhino.RhinoDoc.ActiveDoc",
        "",
        "def ensure_layer(name, rgb):",
        "    idx = doc.Layers.FindByFullPath(name, -1)",
        "    if idx >= 0: return idx",
        "    i = doc.Layers.Add(); L = doc.Layers[i]",
        "    L.Name = name; L.Color = Color.FromArgb(rgb[0], rgb[1], rgb[2])",
        "    return L.Index",
        "",
        "total = 0",
    ]
    for ni, group in trail_groups.items():
        if not group:
            continue
        color = NOZZLE_COLORS[ni % len(NOZZLE_COLORS)]
        layer_name = f"stream_nozzle_{ni}"
        chunk_lines.append(f"lid_{ni} = ensure_layer('{layer_name}', ({color[0]},{color[1]},{color[2]}))")
        chunk_lines.append(f"att_{ni} = Rhino.DocObjects.ObjectAttributes(); att_{ni}.LayerIndex = lid_{ni}")
        for idp, pts in group:
            # mm conversion
            mm_pts = [(p[0]*1000, p[1]*1000, p[2]*1000) for p in pts]
            # Skip overly short tracks (need >=2 points for polyline)
            if len(mm_pts) < 2:
                continue
            # Compose Polyline literal
            pts_args = ",".join(f"rg.Point3d({x:.2f},{y:.2f},{z:.2f})" for x, y, z in mm_pts)
            chunk_lines.append(f"pl = rg.Polyline([{pts_args}])")
            chunk_lines.append(f"doc.Objects.AddPolyline(pl, att_{ni})")
            chunk_lines.append("total += 1")
        # Send chunk if getting too big
        approx = sum(len(x) for x in chunk_lines)
        if approx > 80_000:
            chunks.append("\n".join(chunk_lines))
            chunk_lines = ["import Rhino", "import Rhino.Geometry as rg",
                           "from System.Drawing import Color",
                           "doc = Rhino.RhinoDoc.ActiveDoc",
                           "total = 0"]
    chunk_lines.append("doc.Views.Redraw()")
    chunk_lines.append("print('chunk total polylines:', total)")
    chunks.append("\n".join(chunk_lines))

    for ci, code in enumerate(chunks, 1):
        print(f"sending chunk {ci}/{len(chunks)} ({len(code)} bytes)...")
        r = call(code)
        if r.get("status") != "success":
            print("ERR:", json.dumps(r, indent=2)[:600])
            break
        print("  ->", r["result"].get("output", "").strip()[-200:])


if __name__ == "__main__":
    main()
