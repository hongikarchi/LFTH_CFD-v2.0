"""Probe the rhinomcp TCP server on 127.0.0.1:1999 with a basic command."""
import json
import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 1999


def send_recv(payload: dict, timeout: float = 5.0) -> dict | None:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            data = json.dumps(payload).encode("utf-8")
            s.sendall(data)
            buf = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                # rhinomcp ends with \n or returns single JSON
                try:
                    return json.loads(buf.decode("utf-8", "ignore"))
                except json.JSONDecodeError:
                    continue
        if buf:
            return {"raw": buf[:500].decode("utf-8", "ignore")}
        return None
    except Exception as e:
        return {"error": str(e)}


# Try a few common command shapes
probes = [
    {"type": "get_document_info", "params": {}},
    {"type": "get_objects", "params": {}},
    {"type": "get_objects_with_metadata", "params": {}},
    {"type": "get_selected_objects", "params": {}},
    {"type": "execute_rhinoscript_python_code", "params": {"code": "import rhinoscriptsyntax as rs; print(rs.LayerCount())"}},
    {"type": "list", "params": {}},
    {"type": "help", "params": {}},
]

for p in probes:
    print(f"-> {p}")
    r = send_recv(p)
    print(f"<- {json.dumps(r, default=str)[:300]}\n")
