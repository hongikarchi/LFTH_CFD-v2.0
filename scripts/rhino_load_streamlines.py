"""
Push streamline polylines into the running Rhino doc through rhinomcp.
Each trajectory becomes one Polyline; color groups by nozzle origin.
Coords converted m -> mm.
"""
import json
import socket
from pathlib import Path

import sys
PROJECT = Path(__file__).resolve().parent.parent
GEOM = PROJECT / "runs" / "_real_geom.json"
HOST, PORT = "127.0.0.1", 1999
# CLI: rhino_load_streamlines.py [iter_id] [parent_layer]
ITER_ID = sys.argv[1] if len(sys.argv) > 1 else "streamline_v1"
PARENT_LAYER = sys.argv[2] if len(sys.argv) > 2 else ""
TRAILS = PROJECT / "runs" / f"iter_{ITER_ID}" / "trails.json"

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

    # Build chunked code; each chunk self-contained (own layer + attr setup).
    HEADER = [
        "import Rhino",
        "import Rhino.Geometry as rg",
        "import System",
        "from System.Drawing import Color",
        "doc = Rhino.RhinoDoc.ActiveDoc",
        "def ensure_layer(full_path, rgb):",
        "    idx = doc.Layers.FindByFullPath(full_path, -1)",
        "    if idx >= 0: return idx",
        "    parts = full_path.split('::')",
        "    parent_id = System.Guid.Empty",
        "    cur_path = ''",
        "    for k, part in enumerate(parts):",
        "        cur_path = part if k == 0 else cur_path + '::' + part",
        "        ix = doc.Layers.FindByFullPath(cur_path, -1)",
        "        if ix < 0:",
        "            nl = Rhino.DocObjects.Layer()",
        "            nl.Name = part",
        "            if parent_id != System.Guid.Empty:",
        "                nl.ParentLayerId = parent_id",
        "            if k == len(parts) - 1:",
        "                nl.Color = Color.FromArgb(rgb[0], rgb[1], rgb[2])",
        "            ix = doc.Layers.Add(nl)",
        "        parent_id = doc.Layers[ix].Id",
        "    return ix",
    ]
    chunks = []
    cur_lines = list(HEADER)
    cur_lines.append("added = 0")
    cur_setup_layers = set()  # which nozzle layers are set up in current chunk

    layer_full_path = (lambda n: f"{PARENT_LAYER}::stream_nozzle_{n}" if PARENT_LAYER else f"stream_nozzle_{n}")

    def setup_layer(nozzle_idx, color):
        path = layer_full_path(nozzle_idx)
        cur_lines.append(
            f"lid_{nozzle_idx} = ensure_layer({path!r}, "
            f"({color[0]},{color[1]},{color[2]}))"
        )
        cur_lines.append(
            f"att_{nozzle_idx} = Rhino.DocObjects.ObjectAttributes(); att_{nozzle_idx}.LayerIndex = lid_{nozzle_idx}"
        )

    for ni, group in trail_groups.items():
        if not group:
            continue
        color = NOZZLE_COLORS[ni % len(NOZZLE_COLORS)]
        for idp, pts in group:
            if len(pts) < 2:
                continue
            mm_pts = [(p[0]*1000, p[1]*1000, p[2]*1000) for p in pts]
            pts_args = ",".join(f"rg.Point3d({x:.2f},{y:.2f},{z:.2f})" for x, y, z in mm_pts)
            # Ensure layer/attr defined in current chunk
            if ni not in cur_setup_layers:
                setup_layer(ni, color)
                cur_setup_layers.add(ni)
            cur_lines.append(f"pl = rg.Polyline([{pts_args}])")
            cur_lines.append(f"doc.Objects.AddPolyline(pl, att_{ni})")
            cur_lines.append("added += 1")
            # Roll over chunk if too big
            if sum(len(x) for x in cur_lines) > 70_000:
                cur_lines.append("print('chunk added:', added)")
                chunks.append("\n".join(cur_lines))
                cur_lines = list(HEADER)
                cur_lines.append("added = 0")
                cur_setup_layers = set()
    cur_lines.append("doc.Views.Redraw()")
    cur_lines.append("print('chunk added:', added)")
    chunks.append("\n".join(cur_lines))

    for ci, code in enumerate(chunks, 1):
        print(f"sending chunk {ci}/{len(chunks)} ({len(code)} bytes)...")
        r = call(code)
        if r.get("status") != "success":
            print("ERR:", json.dumps(r, indent=2)[:600])
            break
        print("  ->", r["result"].get("output", "").strip()[-200:])


if __name__ == "__main__":
    main()
