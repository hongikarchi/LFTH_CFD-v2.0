"""
Read the entire active Grasshopper document via the existing rhinomcp
socket (port 1999), bypassing the limited grasshopper-mcp (port 8080).
"""
import json
import socket

HOST, PORT = "127.0.0.1", 1999

CODE = r"""
import json
import Grasshopper as gh

canvas = gh.Instances.ActiveCanvas
if canvas is None:
    print(json.dumps({"error": "No active GH canvas. Open Grasshopper first."}))
else:
    doc = canvas.Document
    if doc is None:
        print(json.dumps({"error": "No GH document loaded."}))
    else:
        # Build port-GUID -> component-GUID map
        port_to_comp = {}
        for obj in doc.Objects:
            cid = str(obj.InstanceGuid)
            # The obj itself is a port for Param_* objects
            port_to_comp[cid] = cid
            if hasattr(obj, "Params"):
                for p in obj.Params.Input:
                    port_to_comp[str(p.InstanceGuid)] = cid
                for p in obj.Params.Output:
                    port_to_comp[str(p.InstanceGuid)] = cid

        objects = []
        for obj in doc.Objects:
            info = {
                "id": str(obj.InstanceGuid),
                "name": obj.Name,
                "nickname": obj.NickName if hasattr(obj, "NickName") else "",
                "type_short": obj.GetType().Name,
                "category": obj.Category if hasattr(obj, "Category") else "",
                "subcategory": obj.SubCategory if hasattr(obj, "SubCategory") else "",
                "pivot_x": obj.Attributes.Pivot.X,
                "pivot_y": obj.Attributes.Pivot.Y,
            }
            if obj.GetType().Name == "GH_NumberSlider":
                try:
                    info["slider_value"] = float(obj.CurrentValue)
                    info["slider_min"] = float(obj.Slider.Minimum)
                    info["slider_max"] = float(obj.Slider.Maximum)
                except Exception as e:
                    info["slider_err"] = str(e)
            elif obj.GetType().Name == "GH_Panel":
                try:
                    info["panel_text"] = obj.UserText
                except:
                    pass
            if hasattr(obj, "Params"):
                ins = []
                for p in obj.Params.Input:
                    src_comps = [port_to_comp.get(str(s.InstanceGuid), str(s.InstanceGuid))
                                 for s in p.Sources]
                    ins.append({
                        "name": p.Name,
                        "nickname": p.NickName,
                        "src_comps": src_comps,
                    })
                outs = []
                for p in obj.Params.Output:
                    rec_comps = []
                    if hasattr(p, "Recipients"):
                        rec_comps = [port_to_comp.get(str(r.InstanceGuid), str(r.InstanceGuid))
                                     for r in p.Recipients]
                    outs.append({
                        "name": p.Name,
                        "nickname": p.NickName,
                        "rec_comps": rec_comps,
                    })
                info["inputs"] = ins
                info["outputs"] = outs
            objects.append(info)
        print(json.dumps({"objects": objects, "count": len(objects)}, default=str))
"""


def call(code: str, timeout: float = 30) -> dict:
    payload = json.dumps({"type": "execute_rhinoscript_python_code",
                           "params": {"code": code}}).encode()
    with socket.create_connection((HOST, PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(payload)
        buf = b""
        while True:
            try: c = s.recv(65536)
            except socket.timeout: break
            if not c: break
            buf += c
            try: return json.loads(buf.decode("utf-8", "ignore"))
            except json.JSONDecodeError: continue
    return {"error": "no json"}


def main():
    r = call(CODE)
    if r.get("status") != "success":
        print("ERR:", json.dumps(r, indent=2)[:1000])
        return
    out = r["result"].get("output", "")
    # Strip leading non-JSON chars + find first balanced {...}
    s = out.find("{")
    depth = 0; e = -1
    for i in range(s, len(out)):
        if out[i] == "{": depth += 1
        elif out[i] == "}":
            depth -= 1
            if depth == 0: e = i + 1; break
    data = json.loads(out[s:e])
    import sys
    from pathlib import Path
    out_path = Path(__file__).resolve().parent.parent / "runs" / "_gh_doc.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    sys.stdout.write(f"Wrote {out_path}  count={data.get('count')}\n")


if __name__ == "__main__":
    main()
