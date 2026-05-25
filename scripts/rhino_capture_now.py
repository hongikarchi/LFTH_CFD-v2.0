import json, socket

code = r"""
import Rhino
doc = Rhino.RhinoDoc.ActiveDoc
view = doc.Views.ActiveView
view.ActiveViewport.ZoomExtents()
view.Redraw()
size = view.ActiveViewport.Size
bmp = view.CaptureToBitmap(size)
bmp.Save(r"C:\Users\user\Documents\LFTH_CFD v2.0\runs\_real_sim_capture.png")
print("saved")
"""

s = socket.create_connection(('127.0.0.1', 1999), timeout=60)
s.settimeout(60)
s.sendall(json.dumps({'type': 'execute_rhinoscript_python_code', 'params': {'code': code}}).encode())
buf = b''
while True:
    try: c = s.recv(65536)
    except socket.timeout: break
    if not c: break
    buf += c
    try:
        d = json.loads(buf.decode())
        print(d.get('result', {}).get('output', d))
        break
    except json.JSONDecodeError: continue
