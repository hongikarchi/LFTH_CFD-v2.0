"""Reparent existing stream_nozzle_* layers under a parent (e.g. test_01)."""
import json
import socket
import sys

HOST, PORT = "127.0.0.1", 1999
parent = sys.argv[1] if len(sys.argv) > 1 else "test_01"

code = f"""
import Rhino
import System
doc = Rhino.RhinoDoc.ActiveDoc
parent_name = "{parent}"
pidx = doc.Layers.FindByFullPath(parent_name, -1)
if pidx < 0:
    pi = doc.Layers.Add(); P = doc.Layers[pi]; P.Name = parent_name
    pidx = P.Index
parent_id = doc.Layers[pidx].Id

moved = 0
empty_id = System.Guid.Empty
for li in range(doc.Layers.Count):
    L = doc.Layers[li]
    if L.IsDeleted: continue
    if L.Name.startswith("stream_nozzle_") and L.ParentLayerId == empty_id:
        L.ParentLayerId = parent_id
        moved += 1
doc.Views.Redraw()
print("moved layers:", moved)
print("parent:", parent_name)
"""

s = socket.create_connection((HOST, PORT), timeout=30)
s.settimeout(30)
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
s.close()
