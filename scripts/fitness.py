"""
Compute fitness (splash ratio) from DualSPHysics outputs.

Caught = fluid particle whose final XY is inside pond AABB AND Z is below
        pond_top_z threshold (settled or in pond).
Splash = everything else (outside pond XY range, airborne above pond top,
        or particles already evicted from the simulation domain).

Pure stdlib — works inside Rhino's GHPython3 without numpy.
"""
from __future__ import annotations

import csv
import re
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
            # Skip header / comment rows. PartVTKOut header looks like
            # "Pos.x [m];Pos.y [m];Pos.z [m];PartOut;..." — match the *prefix*.
            first = row[0].strip()
            if header_idx["Pos.x"] is None and (
                first.startswith("Pos.x") or first.startswith("#") or first.startswith("Time")
            ):
                for i, c in enumerate(row):
                    c = c.strip()
                    if c.startswith("Pos.x"):
                        header_idx["Pos.x"] = i
                    elif c.startswith("Pos.y"):
                        header_idx["Pos.y"] = i
                    elif c.startswith("Pos.z"):
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


def parse_vtk_points(vtk_path: Path) -> list[tuple[float, float, float]]:
    """Parse POINTS section of legacy VTK polydata (ASCII or BINARY). Returns (x,y,z) list."""
    if not vtk_path or not Path(vtk_path).exists():
        return []
    raw = Path(vtk_path).read_bytes()
    # Find encoding line and POINTS header in the ASCII portion
    head_end = raw.find(b"POINTS")
    if head_end < 0:
        return []
    is_binary = b"\nBINARY\n" in raw[:head_end]
    # Header line: "POINTS <N> <type>\n"
    line_end = raw.find(b"\n", head_end)
    if line_end < 0:
        return []
    header = raw[head_end:line_end].decode("ascii", errors="ignore")
    m = re.match(r"POINTS\s+(\d+)\s+(\w+)", header)
    if not m:
        return []
    n_pts = int(m.group(1))
    dtype = m.group(2).lower()
    data_start = line_end + 1

    if is_binary:
        import struct
        # DualSPHysics writes big-endian float32 (legacy VTK convention)
        if dtype == "float":
            fmt = f">{n_pts * 3}f"; itemsize = 4
        elif dtype == "double":
            fmt = f">{n_pts * 3}d"; itemsize = 8
        else:
            return []
        need = n_pts * 3 * itemsize
        if len(raw) < data_start + need:
            return []
        vals = struct.unpack(fmt, raw[data_start:data_start + need])
        return [(vals[i], vals[i + 1], vals[i + 2]) for i in range(0, n_pts * 3, 3)]
    else:
        # ASCII path
        text = raw[data_start:].decode("ascii", errors="ignore")
        pts: list[tuple[float, float, float]] = []
        buf: list[float] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.match(r"^[A-Z_]+(\s+\d+)?\s*$", line) and not re.match(r"^-?\d", line):
                break
            try:
                buf.extend(float(t) for t in line.split())
            except ValueError:
                break
            while len(buf) >= 3 and len(pts) < n_pts:
                pts.append((buf[0], buf[1], buf[2]))
                buf = buf[3:]
            if len(pts) >= n_pts:
                break
        return pts


def compute_fitness_from_vtk(last_fluid_vtk: Path | None,
                             splash_csv: Path | None,
                             pond_aabb: tuple[float, float, float, float],
                             pond_top_z: float) -> dict:
    """VTK-based fitness: last fluid frame (VTK) + out-of-domain particles (CSV)."""
    pond_xmin, pond_ymin, pond_xmax, pond_ymax = pond_aabb
    fluid_pts = parse_vtk_points(Path(last_fluid_vtk)) if last_fluid_vtk else []
    out_pts = _parse_csv(Path(splash_csv)) if splash_csv else []

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
        return {"splash_count": 0, "caught_count": 0, "total_fluid": 0, "splash_ratio": 1.0}
    return {
        "splash_count": int(splash_count),
        "caught_count": int(caught),
        "total_fluid": int(total),
        "splash_ratio": float(splash_count) / float(total),
    }


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
