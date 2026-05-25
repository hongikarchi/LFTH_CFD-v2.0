"""
Render a 2x4 grid comparing precise mode (iter_timing_planned) vs
fast mode (iter_mode_fast_verify) at matching physical times.

Top row: precise (dp=0.015, GPU, 138s for 4s sim, ~32k particles)
Bottom row: fast (dp=0.045, CPU, 9.7s for 10s sim, ~2.7k particles)
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Line3DCollection

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
from animate_run import parse_vtk_points, box_edges, POND_AABB, POND_TOP_Z, DOMAIN_VIEW

PRECISE_DIR = PROJECT / "runs" / "iter_timing_planned" / "Out"
FAST_DIR    = PROJECT / "runs" / "iter_mode_fast_verify" / "Out"

# Match physical time across modes. timeout=0.05s for precise, 0.10s for fast.
TIMES = [1.0, 2.0, 3.0, 4.0]
PRECISE_TIMEOUT = 0.05
FAST_TIMEOUT    = 0.10


def frame_path(out_dir: Path, frame_idx: int) -> Path:
    return out_dir / f"PartFluid_{frame_idx:04d}.vtk"


def plot_scene(ax, vtk_path: Path, title: str):
    px0, py0, px1, py1 = POND_AABB
    ax.set_xlim(*DOMAIN_VIEW[0]); ax.set_ylim(*DOMAIN_VIEW[1]); ax.set_zlim(*DOMAIN_VIEW[2])
    ax.view_init(elev=22, azim=-55)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_zticks([])
    ax.set_xlabel(""); ax.set_ylabel(""); ax.set_zlabel("")
    edges = box_edges(px0, py0, 0.0, px1, py1, POND_TOP_Z)
    ax.add_collection3d(Line3DCollection(edges, colors=(0.05, 0.4, 0.9, 0.9), linewidths=1.0))
    pts = parse_vtk_points(vtk_path)
    if pts is not None and len(pts):
        pts = np.asarray(pts)
        in_pond = ((pts[:, 0] >= px0) & (pts[:, 0] <= px1) &
                   (pts[:, 1] >= py0) & (pts[:, 1] <= py1) &
                   (pts[:, 2] <= POND_TOP_Z))
        cau = pts[in_pond]; spl = pts[~in_pond]
        if spl.size:
            ax.scatter(spl[:, 0], spl[:, 1], spl[:, 2], s=1.0, c="#e25822", alpha=0.55, depthshade=False)
        if cau.size:
            ax.scatter(cau[:, 0], cau[:, 1], cau[:, 2], s=1.6, c="#1fa336", alpha=0.9, depthshade=False)
        ratio = (len(pts) - int(in_pond.sum())) / len(pts)
        sub = f"N={len(pts)}  splash={ratio:.3f}"
    else:
        sub = "empty"
    ax.set_title(f"{title}\n{sub}", fontsize=9)


def main():
    fig = plt.figure(figsize=(16, 7), dpi=120)
    for col, t in enumerate(TIMES):
        # Precise row
        f_p = int(round(t / PRECISE_TIMEOUT))
        ax_p = fig.add_subplot(2, 4, col + 1, projection="3d")
        plot_scene(ax_p, frame_path(PRECISE_DIR, f_p), f"PRECISE  t={t:.1f}s")
        # Fast row
        f_f = int(round(t / FAST_TIMEOUT))
        ax_f = fig.add_subplot(2, 4, col + 5, projection="3d")
        plot_scene(ax_f, frame_path(FAST_DIR, f_f), f"FAST  t={t:.1f}s")

    # Top-row label
    fig.text(0.02, 0.74, "PRECISE\ndp=0.015\nGPU\n~32k particles\n138s wall (for 4s sim)",
             fontsize=10, color="#1d6ddc", weight="bold", va="center")
    fig.text(0.02, 0.28, "FAST\ndp=0.045\nCPU\n~2.7k particles\n9.7s wall (for 10s sim)",
             fontsize=10, color="#c46900", weight="bold", va="center")

    fig.suptitle("PRECISE vs FAST mode — same physical time, same view", fontsize=13)
    fig.subplots_adjust(left=0.10, right=0.99, top=0.92, bottom=0.04, wspace=0.05, hspace=0.20)
    out = PROJECT / "runs" / "compare_precise_vs_fast.png"
    fig.savefig(str(out), dpi=140, bbox_inches="tight")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
