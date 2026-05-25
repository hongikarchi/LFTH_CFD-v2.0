"""Estimate water velocity at first sculpture contact from streamline trails."""
import json
import math
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TRAILS = PROJECT / "runs" / "iter_streamline_v1" / "trails.json"
GEOM = PROJECT / "runs" / "_real_geom.json"

trails = json.loads(TRAILS.read_text())
geom = json.loads(GEOM.read_text())
sculpture_top_z = geom["collider_bbox_m"][1][2]   # ~24.72
nozzle_z = geom["nozzle_center_m"][2]              # ~28.92
free_fall_h = nozzle_z - sculpture_top_z

# Frame interval = timeout from XML. We used 0.05s.
DT = 0.05

# Theoretical (vacuum) impact velocity from initial v=10 m/s
v0 = 10.0
v_theo = math.sqrt(v0**2 + 2 * 9.81 * free_fall_h)

print(f"Sculpture top z: {sculpture_top_z:.2f} m")
print(f"Nozzle z:        {nozzle_z:.2f} m")
print(f"Free fall:       {free_fall_h:.2f} m")
print(f"Theoretical vacuum impact velocity (v0=10): {v_theo:.2f} m/s")
print()

impact_speeds = []
total_speeds_3D = []
for idp, pts in trails.items():
    if len(pts) < 3:
        continue
    # Find first frame index where z is at or below sculpture_top_z
    contact_i = None
    for i, p in enumerate(pts):
        if p[2] <= sculpture_top_z:
            contact_i = i
            break
    if contact_i is None or contact_i < 1 or contact_i >= len(pts) - 1:
        continue
    # Velocity by centered finite difference around contact
    a = pts[contact_i - 1]
    b = pts[contact_i + 1]
    vx = (b[0] - a[0]) / (2 * DT)
    vy = (b[1] - a[1]) / (2 * DT)
    vz = (b[2] - a[2]) / (2 * DT)
    speed = math.sqrt(vx*vx + vy*vy + vz*vz)
    impact_speeds.append(speed)
    total_speeds_3D.append((vx, vy, vz, speed))

if impact_speeds:
    n = len(impact_speeds)
    avg = sum(impact_speeds) / n
    impact_speeds.sort()
    p25 = impact_speeds[n // 4]
    p50 = impact_speeds[n // 2]
    p75 = impact_speeds[3 * n // 4]
    p_min = impact_speeds[0]
    p_max = impact_speeds[-1]
    print(f"Measured at first sculpture-top crossing (n={n} trails):")
    print(f"  mean:   {avg:6.2f} m/s")
    print(f"  median: {p50:6.2f} m/s")
    print(f"  p25:    {p25:6.2f} m/s")
    print(f"  p75:    {p75:6.2f} m/s")
    print(f"  min:    {p_min:6.2f} m/s")
    print(f"  max:    {p_max:6.2f} m/s")
else:
    print("No trails reached sculpture top")
