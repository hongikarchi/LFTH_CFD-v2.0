"""
Rhino 8 Python script — load DualSPHysics VTK output as Rhino PointClouds.

Usage:
    1. In Rhino, run `_RunPythonScript` (or `_EditPythonScript`)
    2. Edit OUT_DIR below to the run's output dir
    3. Run.

Or from the Rhino Python prompt:
    >>> import sys; sys.path.append(r"C:\\Users\\user\\Documents\\LFTH_CFD v2.0\\rhino_import")
    >>> import load_vtk_particles as lvp
    >>> lvp.load_run(r"C:\\Users\\user\\Documents\\LFTH_CFD v2.0\\runs\\iter_<id>\\Out")

Each frame -> one PointCloud on a layer named "iter_<id>__frame_<n>".
Caught particles (inside pond AABB) get green, splash particles get red — only on
the last frame, since fitness is computed at simulation end.

Pure stdlib + RhinoCommon. No numpy required.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# === EDIT THIS to point at a run's Out/ directory ============================
OUT_DIR = r"C:\Users\user\Documents\LFTH_CFD v2.0\runs\iter_LATEST\Out"
# Or use the latest iter automatically:
USE_LATEST = True

# Pond AABB used to classify caught vs splash (must match templates/case_sculpture_template.xml)
POND_AABB = (-0.4, -0.4, 0.4, 0.4)   # xmin, ymin, xmax, ymax
POND_TOP_Z = 0.32                     # 0.02 thickness + 0.30 above
# ============================================================================


def _find_latest_out_dir() -> Path | None:
    runs = Path(r"C:\Users\user\Documents\LFTH_CFD v2.0\runs")
    if not runs.exists():
        return None
    iters = sorted([p for p in runs.iterdir() if p.is_dir() and p.name.startswith("iter_")])
    for p in reversed(iters):
        out = p / "Out"
        if out.exists() and any(out.glob("*.vtk")):
            return out
    return None


def parse_vtk_points(vtk_path: Path) -> list[tuple[float, float, float]]:
    """Parse POINTS section of legacy VTK polydata (ASCII or BINARY). Returns (x,y,z) list."""
    if not vtk_path.exists():
        return []
    raw = vtk_path.read_bytes()
    head_end = raw.find(b"POINTS")
    if head_end < 0:
        return []
    is_binary = b"\nBINARY\n" in raw[:head_end]
    line_end = raw.find(b"\n", head_end)
    if line_end < 0:
        return []
    header = raw[head_end:line_end].decode("ascii", errors="ignore")
    m = re.match(r"POINTS\s+(\d+)\s+(\w+)", header)
    if not m:
        return []
    n_pts = int(m.group(1))
    dtype = m.group(2).lower()
    data_start = line_end + 1
    if is_binary:
        import struct
        if dtype == "float":
            fmt = f">{n_pts * 3}f"; itemsize = 4
        elif dtype == "double":
            fmt = f">{n_pts * 3}d"; itemsize = 8
        else:
            return []
        need = n_pts * 3 * itemsize
        if len(raw) < data_start + need:
            return []
        vals = struct.unpack(fmt, raw[data_start:data_start + need])
        return [(vals[i], vals[i + 1], vals[i + 2]) for i in range(0, n_pts * 3, 3)]
    text = raw[data_start:].decode("ascii", errors="ignore")
    pts: list[tuple[float, float, float]] = []
    buf: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^[A-Z_]+(\s+\d+)?\s*$", line) and not re.match(r"^-?\d", line):
            break
        try:
            buf.extend(float(t) for t in line.split())
        except ValueError:
            break
        while len(buf) >= 3 and len(pts) < n_pts:
            pts.append((buf[0], buf[1], buf[2]))
            buf = buf[3:]
        if len(pts) >= n_pts:
            break
    return pts


def _frame_num(vtk_path: Path) -> int:
    """Extract frame integer from filenames like PartFluid_0000.vtk or Particles_0123.vtk."""
    m = re.search(r"_(\d+)\.vtk$", vtk_path.name)
    return int(m.group(1)) if m else -1


def _ensure_layer(doc, name: str, color=None) -> int:
    """Get or create layer; returns layer index."""
    import Rhino                                  # noqa: F401
    layer_table = doc.Layers
    idx = layer_table.FindByFullPath(name, -1)
    if idx >= 0:
        return idx
    layer = layer_table[layer_table.Add()]
    layer.Name = name
    if color is not None:
        from System.Drawing import Color
        layer.Color = Color.FromArgb(*color)
    return layer.Index


def load_run(out_dir: str | os.PathLike, iter_label: str = "") -> dict:
    """Load all fluid VTK frames in out_dir into Rhino as PointClouds, one layer per frame."""
    import Rhino
    import Rhino.Geometry as rg
    import scriptcontext as sc
    from System.Drawing import Color

    out_dir = Path(out_dir)
    doc = sc.doc
    if not isinstance(doc, Rhino.RhinoDoc):
        doc = Rhino.RhinoDoc.ActiveDoc

    # Find fluid VTK files (PartVTK with fluid filter produces PartFluid_*.vtk or similar)
    vtk_files = sorted(
        [p for p in out_dir.glob("*.vtk")
         if "fluid" in p.name.lower() or "particles" in p.name.lower()
         or p.name.startswith("Part")],
        key=_frame_num,
    )
    if not vtk_files:
        # Fallback: any .vtk
        vtk_files = sorted(out_dir.glob("*.vtk"), key=_frame_num)
    if not vtk_files:
        print(f"No VTK files in {out_dir}")
        return {"frames": 0}

    label = iter_label or out_dir.parent.name
    print(f"Loading {len(vtk_files)} frames from {out_dir}")

    pond_xmin, pond_ymin, pond_xmax, pond_ymax = POND_AABB

    last_frame_idx = len(vtk_files) - 1
    for fi, vtk in enumerate(vtk_files):
        pts = parse_vtk_points(vtk)
        if not pts:
            continue

        is_last = (fi == last_frame_idx)

        if is_last:
            # Split into caught vs splash for last frame
            caught_pc = rg.PointCloud()
            splash_pc = rg.PointCloud()
            for x, y, z in pts:
                in_pond = (pond_xmin <= x <= pond_xmax and
                           pond_ymin <= y <= pond_ymax and
                           z <= POND_TOP_Z)
                p = rg.Point3d(x, y, z)
                if in_pond:
                    caught_pc.Add(p, Color.LimeGreen)
                else:
                    splash_pc.Add(p, Color.OrangeRed)
            cl = _ensure_layer(doc, f"{label}__caught_LAST", (0, 200, 0))
            sl = _ensure_layer(doc, f"{label}__splash_LAST", (220, 80, 0))
            attr_c = Rhino.DocObjects.ObjectAttributes(); attr_c.LayerIndex = cl
            attr_s = Rhino.DocObjects.ObjectAttributes(); attr_s.LayerIndex = sl
            if caught_pc.Count:
                doc.Objects.AddPointCloud(caught_pc, attr_c)
            if splash_pc.Count:
                doc.Objects.AddPointCloud(splash_pc, attr_s)
        else:
            pc = rg.PointCloud()
            for x, y, z in pts:
                pc.Add(rg.Point3d(x, y, z))
            lid = _ensure_layer(doc, f"{label}__frame_{fi:04d}", (100, 150, 220))
            attr = Rhino.DocObjects.ObjectAttributes(); attr.LayerIndex = lid
            doc.Objects.AddPointCloud(pc, attr)

        # Hide non-last frames by default for performance
        if not is_last:
            li = doc.Layers.FindByFullPath(f"{label}__frame_{fi:04d}", -1)
            if li >= 0:
                doc.Layers[li].IsVisible = False

    doc.Views.Redraw()
    print(f"Loaded {len(vtk_files)} frames. Last frame split into caught (green) / splash (red).")
    return {"frames": len(vtk_files), "layers_prefix": label}


def add_pond_box() -> None:
    """Add a visual representation of the pond AABB to the doc."""
    import Rhino
    import Rhino.Geometry as rg
    import scriptcontext as sc
    from System.Drawing import Color

    doc = Rhino.RhinoDoc.ActiveDoc
    box = rg.Box(
        rg.Plane.WorldXY,
        rg.Interval(POND_AABB[0], POND_AABB[2]),
        rg.Interval(POND_AABB[1], POND_AABB[3]),
        rg.Interval(0.0, POND_TOP_Z),
    )
    li = _ensure_layer(doc, "pond_AABB", (0, 100, 200))
    attr = Rhino.DocObjects.ObjectAttributes(); attr.LayerIndex = li
    doc.Objects.AddBrep(box.ToBrep(), attr)
    doc.Views.Redraw()


def main():
    out = OUT_DIR
    if USE_LATEST:
        latest = _find_latest_out_dir()
        if latest is not None:
            out = str(latest)
            print(f"Using latest run: {out}")
    add_pond_box()
    load_run(out)


if __name__ == "__main__":
    main()
