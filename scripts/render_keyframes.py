"""
Render 4 keyframes (early/quarter/mid/late) as a single contact sheet PNG.
"""
from __future__ import annotations
import sys, re
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Line3DCollection
from animate_run import parse_vtk_points, find_latest_iter, box_edges, POND_AABB, POND_TOP_Z, DOMAIN_VIEW


def main():
    iter_dir = find_latest_iter()
    vtks = sorted((iter_dir / "Out").glob("PartFluid_*.vtk"))
    if not vtks:
        print("No frames")
        return

    # Pick 4 keyframes
    n = len(vtks)
    idxs = [int(n * 0.15), int(n * 0.4), int(n * 0.7), n - 1]
    px0, py0, px1, py1 = POND_AABB

    fig = plt.figure(figsize=(14, 4), dpi=120)
    for k, fi in enumerate(idxs):
        ax = fig.add_subplot(1, 4, k + 1, projection="3d")
        ax.set_xlim(*DOMAIN_VIEW[0]); ax.set_ylim(*DOMAIN_VIEW[1]); ax.set_zlim(*DOMAIN_VIEW[2])
        ax.view_init(elev=22, azim=-55)
        ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")
        edges = box_edges(px0, py0, 0.0, px1, py1, POND_TOP_Z)
        ax.add_collection3d(Line3DCollection(edges, colors=(0.05, 0.4, 0.9, 0.9), linewidths=1.2))
        pts = parse_vtk_points(vtks[fi])
        if pts.size:
            in_pond = ((pts[:, 0] >= px0) & (pts[:, 0] <= px1) &
                       (pts[:, 1] >= py0) & (pts[:, 1] <= py1) &
                       (pts[:, 2] <= POND_TOP_Z))
            cau = pts[in_pond]; spl = pts[~in_pond]
            if spl.size:
                ax.scatter(spl[:, 0], spl[:, 1], spl[:, 2], s=1.2, c="#e25822", alpha=0.55, depthshade=False)
            if cau.size:
                ax.scatter(cau[:, 0], cau[:, 1], cau[:, 2], s=2.0, c="#1fa336", alpha=0.9, depthshade=False)
            ratio = (len(pts) - int(in_pond.sum())) / len(pts)
            ax.set_title(f"t={fi*0.05:.2f}s  np={len(pts)}  splash={ratio:.2f}", fontsize=9)
        else:
            ax.set_title(f"t={fi*0.05:.2f}s  (empty)", fontsize=9)

    out = iter_dir / "keyframes.png"
    fig.tight_layout()
    fig.savefig(str(out), dpi=140, bbox_inches="tight")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
