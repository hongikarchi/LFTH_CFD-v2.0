"""Create a cascade-aligned collider STL without external mesh dependencies.

The extracted Rhino geometry can be spatially mismatched with the simulated
falling stream: water leaves the top module around one XY line while lower
modules sit away from that line. This utility keeps the top component fixed and
translates lower connected STL components so their XY centers sit below the
observed/top stream line.

Input:
  env_fx3d/runs/_real_collider_thickened.stl

Outputs:
  env_fx3d/runs/_cascade_aligned_collider.stl
  env_fx3d/runs/_cascade_aligned_modules.json
"""
from __future__ import annotations

import argparse
import json
import struct
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
RUNS = MODULE_ROOT / "runs"


def find(parent: list[int], a: int) -> int:
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a


def union(parent: list[int], a: int, b: int) -> None:
    ra, rb = find(parent, a), find(parent, b)
    if ra != rb:
        parent[rb] = ra


def read_binary_stl(path: Path) -> tuple[bytes, list[tuple[tuple[float, float, float], list[list[float]]]]]:
    raw = path.read_bytes()
    if len(raw) < 84:
        raise ValueError(f"{path} is too small to be a binary STL")
    n_tri = struct.unpack("<I", raw[80:84])[0]
    if 84 + n_tri * 50 > len(raw):
        raise ValueError(f"{path} is not a supported binary STL")
    header = raw[:80]
    tris = []
    for i in range(n_tri):
        base = 84 + i * 50
        normal = struct.unpack("<3f", raw[base:base + 12])
        verts = []
        for j in range(3):
            verts.append(list(struct.unpack("<3f", raw[base + 12 + j * 12:base + 24 + j * 12])))
        tris.append((normal, verts))
    return header, tris


def split_components(tris: list) -> tuple[list[int], list[dict]]:
    verts = []
    vid = {}
    parent: list[int] = []
    tri_vids = []

    def get_vertex_id(v: list[float]) -> int:
        key = (round(v[0], 5), round(v[1], 5), round(v[2], 5))
        if key not in vid:
            vid[key] = len(verts)
            verts.append(tuple(v))
            parent.append(len(parent))
        return vid[key]

    for _, tri_verts in tris:
        ids = [get_vertex_id(v) for v in tri_verts]
        tri_vids.append(ids)
        union(parent, ids[0], ids[1])
        union(parent, ids[1], ids[2])

    root_to_comp = {}
    tri_comp = []
    comps = []
    for ids in tri_vids:
        root = find(parent, ids[0])
        if root not in root_to_comp:
            root_to_comp[root] = len(comps)
            comps.append({
                "triangles": 0,
                "bbox": [float("inf"), float("inf"), float("inf"),
                         float("-inf"), float("-inf"), float("-inf")],
            })
        ci = root_to_comp[root]
        tri_comp.append(ci)
        comps[ci]["triangles"] += 1
        bbox = comps[ci]["bbox"]
        for vi in ids:
            x, y, z = verts[vi]
            bbox[0] = min(bbox[0], x)
            bbox[1] = min(bbox[1], y)
            bbox[2] = min(bbox[2], z)
            bbox[3] = max(bbox[3], x)
            bbox[4] = max(bbox[4], y)
            bbox[5] = max(bbox[5], z)
    return tri_comp, comps


def write_binary_stl(path: Path, header: bytes, tris: list) -> None:
    with path.open("wb") as f:
        f.write(header[:80].ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for normal, verts in tris:
            f.write(struct.pack("<3f", *normal))
            for v in verts:
                f.write(struct.pack("<3f", *v))
            f.write(struct.pack("<H", 0))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=str(RUNS / "_real_collider_thickened.stl"))
    ap.add_argument("--output", default=str(RUNS / "_cascade_aligned_collider.stl"))
    ap.add_argument("--modules-out", default=str(RUNS / "_cascade_aligned_modules.json"))
    ap.add_argument("--stream-x", type=float, default=None)
    ap.add_argument("--stream-y", type=float, default=None)
    ap.add_argument("--stream-x-step", type=float, default=0.0,
                    help="per-lower-module x shift; rank1 uses 0, rank2 uses one step")
    ap.add_argument("--stream-y-step", type=float, default=0.0,
                    help="per-lower-module y shift; rank1 uses 0, rank2 uses one step")
    args = ap.parse_args(argv)

    header, tris = read_binary_stl(Path(args.input))
    tri_comp, comps = split_components(tris)
    order = sorted(range(len(comps)), key=lambda i: comps[i]["bbox"][5], reverse=True)
    if len(order) < 2:
        raise RuntimeError("expected at least two connected components")

    top_bbox = comps[order[0]]["bbox"]
    stream_x = args.stream_x
    stream_y = args.stream_y
    if stream_x is None:
        stream_x = 0.5 * (top_bbox[0] + top_bbox[3])
    if stream_y is None:
        # The observed discharge path is near the lower-Y rim of the top bowl.
        stream_y = top_bbox[1] + 0.20

    translations = {order[0]: (0.0, 0.0, 0.0)}
    modules = []
    for rank, ci in enumerate(order):
        bbox = comps[ci]["bbox"]
        cx = 0.5 * (bbox[0] + bbox[3])
        cy = 0.5 * (bbox[1] + bbox[4])
        if rank == 0:
            tx = ty = 0.0
            target_x = stream_x
            target_y = stream_y
        else:
            target_x = stream_x + max(rank - 1, 0) * args.stream_x_step
            target_y = stream_y + max(rank - 1, 0) * args.stream_y_step
            tx = target_x - cx
            ty = target_y - cy
        translations[ci] = (tx, ty, 0.0)
        moved_bbox = [[bbox[0] + tx, bbox[1] + ty, bbox[2]],
                      [bbox[3] + tx, bbox[4] + ty, bbox[5]]]
        modules.append({
            "index": rank,
            "source_component": ci,
            "translation_m": [tx, ty, 0.0],
            "target_center_xy_m": [target_x, target_y],
            "bbox_m": moved_bbox,
            "center_m": [
                0.5 * (moved_bbox[0][0] + moved_bbox[1][0]),
                0.5 * (moved_bbox[0][1] + moved_bbox[1][1]),
                0.5 * (moved_bbox[0][2] + moved_bbox[1][2]),
            ],
        })

    moved_tris = []
    for tri_idx, (normal, verts) in enumerate(tris):
        tx, ty, tz = translations[tri_comp[tri_idx]]
        moved = [[v[0] + tx, v[1] + ty, v[2] + tz] for v in verts]
        moved_tris.append((normal, moved))

    out_path = Path(args.output)
    write_binary_stl(out_path, header, moved_tris)
    modules_path = Path(args.modules_out)
    modules_path.write_text(json.dumps({
        "source": str(Path(args.input).resolve()),
        "stream_xy_m": [stream_x, stream_y],
        "modules": modules,
    }, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"wrote {modules_path}")
    print(json.dumps({"stream_xy_m": [stream_x, stream_y], "modules": modules}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
