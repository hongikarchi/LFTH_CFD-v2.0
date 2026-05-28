"""Push FluidX3D test case sculpture STL into Rhino layer for visual review.

After FluidX3D produces water-flow PNG sequence + phi VTKs, this script:
1. Bakes the sculpture STL into Rhino layer `fluidx3d::test_22::sculpture`.
2. Drops a small marker box at the inflow center so the source position is
   visible alongside the geometry.
3. Prints the FluidX3D output paths for the user to open externally.

Run: python scripts/fx3d_visualize_in_rhino.py
"""

from __future__ import annotations
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent
sys.path.insert(0, str(SCRIPT_DIR))

from rhino_mcp import push_stl_to_rhino_layer, mcp_call

TEST_ID = "test_22"
ITER_DIR = MODULE_ROOT / "runs" / f"iter_{TEST_ID}"
STL_PATH = ITER_DIR / "sculpture.stl"
FRAMES_DIR = ITER_DIR / "fx3d_out" / "frames"
VTK_DIR = ITER_DIR / "fx3d_out" / "vtk"

LAYER_ROOT = "fluidx3d"
SCULPTURE_LAYER = f"{LAYER_ROOT}::{TEST_ID}::sculpture"
MARKER_LAYER = f"{LAYER_ROOT}::{TEST_ID}::inflow"

# Inflow geometry in setup.cpp (SI meters)
INFLOW_CX_M = 4.0
INFLOW_CY_M = -2.4
INFLOW_Z0_M = 24.0
INFLOW_Z1_M = 30.0
INFLOW_R_M  = 1.0


def push_inflow_marker_cylinder() -> None:
    # Rhino doc is in mm — convert SI meters to mm
    cx_mm = INFLOW_CX_M * 1000.0
    cy_mm = INFLOW_CY_M * 1000.0
    z0_mm = INFLOW_Z0_M * 1000.0
    z1_mm = INFLOW_Z1_M * 1000.0
    r_mm  = INFLOW_R_M  * 1000.0
    code = f"""
import Rhino, scriptcontext as sc, System
from Rhino.Geometry import Point3d, Plane, Vector3d, Cylinder, Circle, Brep

doc = sc.doc
lid = doc.Layers.FindByFullPath("{MARKER_LAYER}", -1)
if lid < 0:
    layer = Rhino.DocObjects.Layer()
    layer.Name = "inflow"
    parent = doc.Layers.FindByFullPath("{LAYER_ROOT}::{TEST_ID}", -1)
    if parent >= 0:
        layer.ParentLayerId = doc.Layers[parent].Id
    layer.Color = System.Drawing.Color.FromArgb(0, 120, 255)
    lid = doc.Layers.Add(layer)

# purge old objects on layer
to_del = []
for ro in doc.Objects.FindByLayer(doc.Layers[lid]):
    to_del.append(ro.Id)
for gid in to_del:
    doc.Objects.Delete(gid, True)

base = Point3d({cx_mm}, {cy_mm}, {z0_mm})
top  = Point3d({cx_mm}, {cy_mm}, {z1_mm})
plane = Plane(base, Vector3d.ZAxis)
circle = Circle(plane, {r_mm})
cyl = Cylinder(circle, {z1_mm - z0_mm})
brep = cyl.ToBrep(True, True)
attr = Rhino.DocObjects.ObjectAttributes()
attr.LayerIndex = lid
attr.Name = "inflow_column"
doc.Objects.AddBrep(brep, attr)
doc.Views.Redraw()
print("inflow marker added at z={z0_mm}..{z1_mm} mm")
"""
    resp = mcp_call(code, timeout=30.0)
    print("inflow marker:", resp.get("result") or resp)


def main():
    if not STL_PATH.exists():
        print(f"ERROR: {STL_PATH} missing"); return 1

    print(f"Pushing sculpture to Rhino layer {SCULPTURE_LAYER}...")
    push_stl_to_rhino_layer(STL_PATH, SCULPTURE_LAYER, (140, 90, 30),
                            offset_mm=(0.0, 0.0, 0.0),
                            obj_name=f"sculpture_{TEST_ID}")

    print(f"Pushing inflow marker cylinder to Rhino layer {MARKER_LAYER}...")
    push_inflow_marker_cylinder()

    # report PNG / VTK paths
    n_frames = len(list(FRAMES_DIR.glob("*.png"))) if FRAMES_DIR.exists() else 0
    n_vtk    = len(list(VTK_DIR.glob("phi-*.vtk"))) if VTK_DIR.exists() else 0
    print()
    print("=" * 60)
    print(f"FluidX3D outputs for {TEST_ID}:")
    print(f"  PNG frames  : {FRAMES_DIR}  ({n_frames} files)")
    print(f"  Phi VTK     : {VTK_DIR}     ({n_vtk} files)")
    print()
    print("To view water flow animation:")
    print(f"  open {FRAMES_DIR}/image-000000750.png  (early splash)")
    print(f"  open {FRAMES_DIR}/image-000002000.png  (steady cascade)")
    print()
    print("In Rhino, the sculpture is on layer 'fluidx3d::test_22::sculpture',")
    print("inflow cylinder marker on layer 'fluidx3d::test_22::inflow'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
