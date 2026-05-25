"""
Compute fitness (splash ratio) from DualSPHysics CSV exports.

Caught = fluid particle whose final XY is inside pond AABB AND Z is below
        pond_top_z threshold (settled or in pond).
Splash = everything else (out of pond XY range, or still airborne above pond top).

Pure stdlib — works inside Rhino's GHPython3 without numpy.
"""
from __future__ import annotations

import csv
from pathlib import Path


def _parse_csv(csv_path: Path) -> list[tuple[float, float, float]]:
    """
    DualSPHysics CSV from PartVTK is `;` delimited.
    Columns: Pos.x;Pos.y;Pos.z;...  (header on row 1, possibly row 2)
    Returns list of (x, y, z).
    """
    if not csv_path.exists():
        return []
    points = []
    with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f, delimiter=";")
        header_idx = {"Pos.x": None, "Pos.y": None, "Pos.z": None}
        for row in reader:
            if not row:
                continue
            # Skip header / comment rows
            if any(k in row[0] for k in ("#", "Part", "Time")) and header_idx["Pos.x"] is None:
                # Try to parse header
                for i, c in enumerate(row):
                    c = c.strip()
                    if c == "Pos.x":
                        header_idx["Pos.x"] = i
                    elif c == "Pos.y":
                        header_idx["Pos.y"] = i
                    elif c == "Pos.z":
                        header_idx["Pos.z"] = i
                continue
            # Data row
            try:
                if header_idx["Pos.x"] is not None:
                    x = float(row[header_idx["Pos.x"]])
                    y = float(row[header_idx["Pos.y"]])
                    z = float(row[header_idx["Pos.z"]])
                else:
                    # Fallback: assume first 3 columns are x, y, z
                    x, y, z = float(row[0]), float(row[1]), float(row[2])
                points.append((x, y, z))
            except (ValueError, IndexError):
                continue
    return points


def compute_fitness(fluid_csv: Path,
                    splash_csv: Path,
                    pond_aabb: tuple[float, float, float, float],
                    pond_top_z: float) -> dict:
    """
    Args:
        fluid_csv: PartVTK CSV of fluid particles at last frame.
        splash_csv: PartVTKOut CSV of particles that exited domain.
        pond_aabb: (xmin, ymin, xmax, ymax) of pond.
        pond_top_z: particles below this Z (and inside XY pond_aabb) count as "caught".

    Returns:
        {
            "splash_count": int,
            "caught_count": int,
            "total_fluid": int,
            "splash_ratio": float in [0, 1],  # lower = better
        }
    """
    pond_xmin, pond_ymin, pond_xmax, pond_ymax = pond_aabb

    fluid_pts = _parse_csv(Path(fluid_csv))
    out_pts = _parse_csv(Path(splash_csv))

    caught = 0
    splash_from_fluid = 0
    for x, y, z in fluid_pts:
        in_pond_xy = (pond_xmin <= x <= pond_xmax) and (pond_ymin <= y <= pond_ymax)
        if in_pond_xy and z <= pond_top_z:
            caught += 1
        else:
            splash_from_fluid += 1

    splash_count = splash_from_fluid + len(out_pts)
    total = caught + splash_count

    if total == 0:
        return {
            "splash_count": 0, "caught_count": 0,
            "total_fluid": 0, "splash_ratio": 1.0,
        }

    return {
        "splash_count": int(splash_count),
        "caught_count": int(caught),
        "total_fluid": int(total),
        "splash_ratio": float(splash_count) / float(total),
    }
