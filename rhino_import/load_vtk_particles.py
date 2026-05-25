# -*- coding: utf-8 -*-
"""
Rhino IronPython 2.7 / CPython3 - load DualSPHysics VTK output as Rhino PointClouds.

Run from Rhino:
    -_RunPythonScript "C:\\path\\to\\load_vtk_particles.py"

Or from a wrapper that points to a specific run's Out/ directory.

Each frame -> one PointCloud on a layer named "<run>__frame_<n>".
Last frame additionally split into caught (green) / splash (red) PointClouds.

Pure RhinoCommon + stdlib (struct, os, re). No numpy, no f-strings.
"""

import os
import re
import struct
import sys

PROJECT_ROOT = r"C:\Users\user\Documents\LFTH_CFD v2.0"
RUNS_DIR = os.path.join(PROJECT_ROOT, "runs")

# Pond AABB used to classify caught vs splash (match templates/case_sculpture_template.xml)
POND_AABB = (-0.4, -0.4, 0.4, 0.4)   # xmin, ymin, xmax, ymax
POND_TOP_Z = 0.32                     # 0.02 thickness + 0.30 above


def find_latest_out_dir():
    if not os.path.isdir(RUNS_DIR):
        return None
    iters = []
    for name in os.listdir(RUNS_DIR):
        full = os.path.join(RUNS_DIR, name)
        if os.path.isdir(full) and name.startswith("iter_"):
            iters.append(full)
    iters.sort()
    for p in reversed(iters):
        out = os.path.join(p, "Out")
        if os.path.isdir(out):
            for n in os.listdir(out):
                if n.lower().endswith(".vtk") and ("partfluid" in n.lower() or "particles" in n.lower()):
                    return out
    return None


def parse_vtk_points(vtk_path):
    """Parse POINTS section of legacy VTK polydata (ASCII or BINARY). Returns list of (x,y,z)."""
    if not os.path.isfile(vtk_path):
        return []
    f = open(vtk_path, "rb")
    try:
        raw = f.read()
    finally:
        f.close()
    head_end = raw.find(b"POINTS")
    if head_end < 0:
        return []
    is_binary = b"\nBINARY\n" in raw[:head_end]
    line_end = raw.find(b"\n", head_end)
    if line_end < 0:
        return []
    header = raw[head_end:line_end].decode("ascii", "ignore")
    m = re.match(r"POINTS\s+(\d+)\s+(\w+)", header)
    if not m:
        return []
    n_pts = int(m.group(1))
    dtype = m.group(2).lower()
    data_start = line_end + 1

    if is_binary:
        if dtype == "float":
            fmt = ">" + str(n_pts * 3) + "f"
            itemsize = 4
        elif dtype == "double":
            fmt = ">" + str(n_pts * 3) + "d"
            itemsize = 8
        else:
            return []
        need = n_pts * 3 * itemsize
        if len(raw) < data_start + need:
            return []
        vals = struct.unpack(fmt, raw[data_start:data_start + need])
        return [(vals[i], vals[i + 1], vals[i + 2]) for i in range(0, n_pts * 3, 3)]

    text = raw[data_start:].decode("ascii", "ignore")
    pts = []
    buf = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^[A-Z_]+(\s+\d+)?\s*$", line) and not re.match(r"^-?\d", line):
            break
        try:
            for t in line.split():
                buf.append(float(t))
        except ValueError:
            break
        while len(buf) >= 3 and len(pts) < n_pts:
            pts.append((buf[0], buf[1], buf[2]))
            buf = buf[3:]
        if len(pts) >= n_pts:
            break
    return pts


def _frame_num(vtk_path):
    m = re.search(r"_(\d+)\.vtk$", os.path.basename(vtk_path))
    return int(m.group(1)) if m else -1


def _ensure_layer(doc, name, color_rgb=None):
    """Create or return layer index by path. color_rgb is (r, g, b) tuple."""
    idx = doc.Layers.FindByFullPath(name, -1)
    if idx >= 0:
        return idx
    layer_id = doc.Layers.Add()
    layer = doc.Layers[layer_id]
    layer.Name = name
    if color_rgb is not None:
        from System.Drawing import Color
        layer.Color = Color.FromArgb(color_rgb[0], color_rgb[1], color_rgb[2])
    return layer.Index


def add_pond_box():
    import Rhino
    import Rhino.Geometry as rg

    doc = Rhino.RhinoDoc.ActiveDoc
    box = rg.Box(
        rg.Plane.WorldXY,
        rg.Interval(POND_AABB[0], POND_AABB[2]),
        rg.Interval(POND_AABB[1], POND_AABB[3]),
        rg.Interval(0.0, POND_TOP_Z),
    )
    li = _ensure_layer(doc, "pond_AABB", (0, 100, 200))
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.LayerIndex = li
    doc.Objects.AddBrep(box.ToBrep(), attr)
    doc.Views.Redraw()


def load_run(out_dir, label=None):
    import Rhino
    import Rhino.Geometry as rg
    from System.Drawing import Color

    doc = Rhino.RhinoDoc.ActiveDoc
    if label is None:
        # use parent dir name (the iter_<id>)
        label = os.path.basename(os.path.dirname(os.path.normpath(out_dir)))

    # Collect fluid VTKs
    vtks = []
    for n in os.listdir(out_dir):
        if not n.lower().endswith(".vtk"):
            continue
        nl = n.lower()
        if ("partfluid" in nl) or ("particles" in nl) or nl.startswith("part"):
            vtks.append(os.path.join(out_dir, n))
    vtks.sort(key=_frame_num)
    if not vtks:
        return {"frames": 0, "out_dir": out_dir}

    pond_xmin, pond_ymin, pond_xmax, pond_ymax = POND_AABB
    last_idx = len(vtks) - 1
    print("Loading {0} frames from {1}".format(len(vtks), out_dir))

    for fi, vtk in enumerate(vtks):
        pts = parse_vtk_points(vtk)
        if not pts:
            continue

        if fi == last_idx:
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
            cl = _ensure_layer(doc, label + "__caught_LAST", (0, 200, 0))
            sl = _ensure_layer(doc, label + "__splash_LAST", (220, 80, 0))
            ac = Rhino.DocObjects.ObjectAttributes(); ac.LayerIndex = cl
            asp = Rhino.DocObjects.ObjectAttributes(); asp.LayerIndex = sl
            if caught_pc.Count:
                doc.Objects.AddPointCloud(caught_pc, ac)
            if splash_pc.Count:
                doc.Objects.AddPointCloud(splash_pc, asp)
        else:
            pc = rg.PointCloud()
            for x, y, z in pts:
                pc.Add(rg.Point3d(x, y, z))
            lname = label + "__frame_" + ("%04d" % fi)
            lid = _ensure_layer(doc, lname, (100, 150, 220))
            a = Rhino.DocObjects.ObjectAttributes(); a.LayerIndex = lid
            doc.Objects.AddPointCloud(pc, a)
            # hide intermediate frames for performance
            doc.Layers[lid].IsVisible = False

    doc.Views.Redraw()
    print("Loaded {0} frames. Last frame split caught(green)/splash(red).".format(len(vtks)))
    return {"frames": len(vtks), "out_dir": out_dir, "label": label}


def main():
    out = find_latest_out_dir()
    if not out:
        print("No run directory with VTKs found under " + RUNS_DIR)
        return
    print("Using run: " + out)
    add_pond_box()
    return load_run(out)


if __name__ == "__main__":
    main()
