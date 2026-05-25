# -*- coding: utf-8 -*-
"""
Rhino IronPython 2.7 / CPython3 - load particles + zoom + capture viewport PNG.
Writes a status file so an external watcher can verify completion.

Driven by Rhino.exe /runscript on launch.
"""
import os
import sys
import traceback

PROJECT = r"C:\Users\user\Documents\LFTH_CFD v2.0"
SIG_PATH = os.path.join(PROJECT, "runs", "_rhino_status.txt")
PNG_PATH = os.path.join(PROJECT, "runs", "_rhino_capture.png")

# Ensure runs dir exists
runs_dir = os.path.join(PROJECT, "runs")
if not os.path.isdir(runs_dir):
    os.makedirs(runs_dir)

# Clear prior status/capture
for p in (SIG_PATH, PNG_PATH):
    try:
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass

try:
    sys.path.insert(0, os.path.join(PROJECT, "rhino_import"))
    import load_vtk_particles as lvp

    import Rhino

    lvp.add_pond_box()
    out = lvp.find_latest_out_dir()
    info = {"frames": 0, "out_dir": None}
    if out is not None:
        info = lvp.load_run(out)

    doc = Rhino.RhinoDoc.ActiveDoc
    view = doc.Views.ActiveView
    if view is not None:
        # Set perspective view + zoom extents
        view.ActiveViewport.ZoomExtents()
        view.Redraw()
        # Capture viewport bitmap
        size = view.ActiveViewport.Size
        bmp = view.CaptureToBitmap(size)
        if bmp is not None:
            bmp.Save(PNG_PATH)

    fh = open(SIG_PATH, "w")
    try:
        fh.write("OK\n")
        fh.write("frames=" + str(info.get("frames", 0)) + "\n")
        fh.write("out_dir=" + str(info.get("out_dir")) + "\n")
        fh.write("png=" + PNG_PATH + "\n")
    finally:
        fh.close()
except Exception:
    err = traceback.format_exc()
    fh = open(SIG_PATH, "w")
    try:
        fh.write("ERROR\n")
        fh.write(err)
    finally:
        fh.close()
