"""
Wrapper: invoke load_vtk_particles.main() and write a status file so the
outer process can verify the visualization completed.

Run from Rhino:
    -_RunPythonScript ("C:\\...\\load_with_signal.py")
"""
import sys
import traceback
from pathlib import Path

SIG_PATH = Path(r"C:\Users\user\Documents\LFTH_CFD v2.0\runs\_rhino_status.txt")
SIG_PATH.parent.mkdir(parents=True, exist_ok=True)

try:
    sys.path.insert(0, r"C:\Users\user\Documents\LFTH_CFD v2.0\rhino_import")
    import load_vtk_particles as lvp
    lvp.main()
    SIG_PATH.write_text("OK\n", encoding="utf-8")
except Exception as e:
    SIG_PATH.write_text(f"ERROR: {type(e).__name__}: {e}\n\n{traceback.format_exc()}", encoding="utf-8")
