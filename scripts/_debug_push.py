"""Generate the actual STL import script and show what's sent to Rhino."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from experiment_runner import _ensure_layer_via_mcp, mcp_call

stl_path = Path("runs/iter_test_01/sculpture_viz.stl").resolve()
stl_esc = str(stl_path).replace("\\", "\\\\")

print("stl_esc raw repr:")
print(repr(stl_esc))
print()

argb = (255, 140, 90, 30)
lid = _ensure_layer_via_mcp("test_dbg2::collider", argb)
print(f"ensure lid={lid}")

ox, oy, oz = 0.0, 0.0, 0.0
code = f'''
import Rhino, System, scriptcontext as sc
doc = sc.doc
doc.Objects.UnselectAll()
lid = doc.Layers.FindByFullPath("test_dbg2::collider", -1)
print("LID=" + str(lid))
if lid < 0:
    raise Exception("layer missing")
for o in list(doc.Objects):
    if o.Attributes.LayerIndex == lid:
        doc.Objects.Delete(o, True)
doc.Objects.UnselectAll()

cmd = '_-Import "{stl_esc}" _Enter'
print("CMD=" + cmd)
ok = Rhino.RhinoApp.RunScript(cmd, False)
print("Import ok=" + str(ok))
sel = list(doc.Objects.GetSelectedObjects(False, False))
print("sel after=" + str(len(sel)))
'''
print("--- SCRIPT (first 800 chars) ---")
print(code[:800])
print()

r = mcp_call(code, timeout=60)
print("status:", r.get("status"))
print("output:", r.get("result", {}).get("output", "")[:1500])
print("msg:", r.get("result", {}).get("message", ""))
