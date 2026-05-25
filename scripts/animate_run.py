"""
Render an animated GIF/MP4 from a run's PartFluid_*.vtk frames.
3D scatter with pond AABB box; particles colored by caught/splash classification.

Run:
    python animate_run.py                     # uses latest run
    python animate_run.py <iter_id>
"""
from __future__ import annotations

import sys
import struct
import re
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter
from mpl_toolkits.mplot3d.art3d import Line3DCollection

PROJECT = Path(__file__).resolve().parent.parent
RUNS = PROJECT / "runs"

POND_AABB = (-0.4, -0.4, 0.4, 0.4)   # xmin, ymin, xmax, ymax
POND_TOP_Z = 0.32
DOMAIN_VIEW = ((-1.0, 1.0), (-1.0, 1.0), (0.0, 2.5))


def parse_vtk_points(vtk_path: Path) -> np.ndarray:
    raw = vtk_path.read_bytes()
    head = raw.find(b"POINTS")
    if head < 0:
        return np.empty((0, 3), dtype=np.float32)
    is_binary = b"\nBINARY\n" in raw[:head]
    line_end = raw.find(b"\n", head)
    header = raw[head:line_end].decode("ascii", errors="ignore")
    m = re.match(r"POINTS\s+(\d+)\s+(\w+)", header)
    if not m:
        return np.empty((0, 3), dtype=np.float32)
    n = int(m.group(1))
    if not n:
        return np.empty((0, 3), dtype=np.float32)
    dtype = m.group(2).lower()
    start = line_end + 1
    if is_binary:
        if dtype == "float":
            arr = np.frombuffer(raw[start:start + n * 12], dtype=">f4").reshape(n, 3).astype(np.float32)
        elif dtype == "double":
            arr = np.frombuffer(raw[start:start + n * 24], dtype=">f8").reshape(n, 3).astype(np.float32)
        else:
            return np.empty((0, 3), dtype=np.float32)
        return arr
    # ASCII fallback
    text = raw[start:].decode("ascii", errors="ignore")
    floats = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not re.match(r"^-?\d", line):
            break
        floats.extend(float(t) for t in line.split())
        if len(floats) >= n * 3:
            break
    return np.asarray(floats[:n * 3], dtype=np.float32).reshape(-1, 3)


def find_latest_iter() -> Path | None:
    iters = sorted([p for p in RUNS.iterdir() if p.is_dir() and p.name.startswith("iter_")])
    for p in reversed(iters):
        if any((p / "Out").glob("PartFluid_*.vtk")):
            return p
    return None


def box_edges(xmin, ymin, zmin, xmax, ymax, zmax):
    pts = np.array([
        (xmin, ymin, zmin), (xmax, ymin, zmin), (xmax, ymax, zmin), (xmin, ymax, zmin),
        (xmin, ymin, zmax), (xmax, ymin, zmax), (xmax, ymax, zmax), (xmin, ymax, zmax),
    ])
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    return [(pts[a], pts[b]) for a, b in edges]


def main():
    iter_dir: Path | None = None
    if len(sys.argv) > 1:
        cand = RUNS / f"iter_{sys.argv[1]}"
        if cand.is_dir():
            iter_dir = cand
    if iter_dir is None:
        iter_dir = find_latest_iter()
    if iter_dir is None:
        print("No iter_* directory with PartFluid VTKs found.")
        sys.exit(1)

    out_dir = iter_dir / "Out"
    vtks = sorted(out_dir.glob("PartFluid_*.vtk"))
    print(f"Animating {len(vtks)} frames from {iter_dir.name}")

    pond_x0, pond_y0, pond_x1, pond_y1 = POND_AABB

    fig = plt.figure(figsize=(8, 6), dpi=120)
    ax = fig.add_subplot(111, projection="3d")

    def classify(pts):
        in_pond = (
            (pts[:, 0] >= pond_x0) & (pts[:, 0] <= pond_x1) &
            (pts[:, 1] >= pond_y0) & (pts[:, 1] <= pond_y1) &
            (pts[:, 2] <= POND_TOP_Z)
        )
        return in_pond

    def draw_static():
        ax.clear()
        ax.set_xlim(*DOMAIN_VIEW[0])
        ax.set_ylim(*DOMAIN_VIEW[1])
        ax.set_zlim(*DOMAIN_VIEW[2])
        ax.set_xlabel("X (m)"); ax.set_ylabel("Y (m)"); ax.set_zlabel("Z (m)")
        ax.view_init(elev=22, azim=-55)
        # Pond AABB wireframe (blue)
        edges = box_edges(pond_x0, pond_y0, 0.0, pond_x1, pond_y1, POND_TOP_Z)
        lc = Line3DCollection(edges, colors=(0.05, 0.4, 0.9, 0.9), linewidths=1.4)
        ax.add_collection3d(lc)

    def update(fi):
        draw_static()
        pts = parse_vtk_points(vtks[fi])
        if pts.size:
            in_pond = classify(pts)
            caught = pts[in_pond]; splash = pts[~in_pond]
            if splash.size:
                ax.scatter(splash[:, 0], splash[:, 1], splash[:, 2],
                           s=2, c="#e25822", alpha=0.55, depthshade=False)
            if caught.size:
                ax.scatter(caught[:, 0], caught[:, 1], caught[:, 2],
                           s=3, c="#1fa336", alpha=0.85, depthshade=False)
            n_total = len(pts); n_caught = int(in_pond.sum())
            ratio = (n_total - n_caught) / n_total if n_total else 0.0
            title = (
                f"{iter_dir.name}  frame {fi+1}/{len(vtks)}  "
                f"particles={n_total}  caught={n_caught}  splash_ratio={ratio:.3f}"
            )
        else:
            title = f"{iter_dir.name}  frame {fi+1}/{len(vtks)}  (empty)"
        ax.set_title(title, fontsize=9)
        return []

    anim = FuncAnimation(fig, update, frames=len(vtks), interval=80, blit=False)
    gif_path = iter_dir / "animation.gif"
    print(f"Writing {gif_path}")
    anim.save(str(gif_path), writer=PillowWriter(fps=12), dpi=100)
    print("Done.")
    print(f"Frames: {len(vtks)}  GIF: {gif_path}  size: {gif_path.stat().st_size/1024:.1f} KiB")


if __name__ == "__main__":
    main()
