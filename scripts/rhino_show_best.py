import json, socket, sys

target = sys.argv[1] if len(sys.argv) > 1 else "test_22"

code = r"""
import Rhino
doc = Rhino.RhinoDoc.ActiveDoc
target = "__T__"
for li in range(doc.Layers.Count):
    L = doc.Layers[li]
    if L.IsDeleted: continue
    if L.FullPath.startswith("test_"):
        if L.FullPath == target or L.FullPath.startswith(target + "::"):
            L.IsVisible = True
        else:
            L.IsVisible = False
view = doc.Views.ActiveView
view.ActiveViewport.ZoomExtents()
view.Redraw()
size = view.ActiveViewport.Size
bmp = view.CaptureToBitmap(size)
out_path = r"C:\Users\user\Documents\LFTH_CFD v2.0\runs\_best_" + target + ".png"
bmp.Save(out_path)
print("saved", out_path)
""".replace("__T__", target)

s = socket.create_connection(("127.0.0.1", 1999), timeout=60)
s.settimeout(60)
s.sendall(json.dumps({"type": "execute_rhinoscript_python_code", "params": {"code": code}}).encode())
buf = b""
while True:
    try: c = s.recv(65536)
    except socket.timeout: break
    if not c: break
    buf += c
    try:
        d = json.loads(buf.decode())
        print(d.get("result", {}).get("output", d))
        break
    except json.JSONDecodeError: continue
