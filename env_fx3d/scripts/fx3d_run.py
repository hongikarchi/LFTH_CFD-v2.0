"""Unified FluidX3D experiment driver.

Reads canonical config (config/case.json) on each invocation. Each call:
  1. Decides STL (case.json collider_stl_path override, then runs/_real_collider_thickened.stl / raw)
  2. Generates iter_dir/case.txt + nozzles.txt + case.json (snapshot)
  3. Runs FluidX3D.exe with cwd=iter_dir
  4. Postprocesses -> result.json
  5. Appends to runs/_settings_log.jsonl
  6. (optional) Pushes STL into Rhino
  7. Refreshes settings_compare.html

Callable as:
  CLI:  python scripts/fx3d_run.py [--test-id ID] [--stl PATH] [--config PATH]
  Lib:  from fx3d_run import run_experiment
        run_experiment(test_id, stl_path=..., config_overrides={...})
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent
sys.path.insert(0, str(SCRIPT_DIR))

from fx3d_postprocess import postprocess
from rhino_mcp import push_stl_to_rhino_layer

FLUIDX3D_DIR = MODULE_ROOT / "external" / "FluidX3D"
FLUIDX3D_EXE = FLUIDX3D_DIR / "bin" / "FluidX3D.exe"
FLUIDX3D_EXE_INTERACTIVE = FLUIDX3D_DIR / "bin" / "FluidX3D_interactive.exe"

CONFIG_DEFAULT = MODULE_ROOT / "config" / "case.json"
BUILD_CONFIG_DEFAULT = MODULE_ROOT / "config" / "build.json"
RUNS = MODULE_ROOT / "runs"
TARGETS_JSON = RUNS / "_real_targets.json"
SETTINGS_LOG = MODULE_ROOT / "_settings_log.jsonl"

DEFAULT_COLLIDER_THICK = RUNS / "_real_collider_thickened.stl"
DEFAULT_COLLIDER_RAW = RUNS / "_real_collider.stl"


# --- helpers -----------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"missing canonical config {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # strip leading underscore (comment) keys
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def load_build_config(path: Path = BUILD_CONFIG_DEFAULT) -> dict:
    if not path.exists():
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def load_targets() -> dict:
    if not TARGETS_JSON.exists():
        raise FileNotFoundError(f"{TARGETS_JSON} missing. Run extract_targets.py first.")
    return json.loads(TARGETS_JSON.read_text(encoding="utf-8"))


def load_module_bboxes(path: Path | None = None) -> list:
    modules_path = path or (RUNS / "_collider_modules.json")
    if not modules_path.exists():
        return []
    data = json.loads(modules_path.read_text(encoding="utf-8"))
    out = []
    for module in data.get("modules", []):
        bbox_m = module.get("bbox_m")
        if bbox_m:
            out.append(bbox_m)
            continue
        bbox_mm = module.get("bbox_mm")
        if not bbox_mm:
            continue
        out.append([[float(v) / 1000.0 for v in bbox_mm[0]],
                    [float(v) / 1000.0 for v in bbox_mm[1]]])
    return out


def select_nozzles(nozzles_m: list, cfg: dict) -> list:
    """Reduce Rhino nozzle samples to the actual inlet model used by FluidX3D.

    Rhino's env::nozzle layer can contain many sampled points describing one
    faucet footprint. Treating all samples as independent faucets creates a
    large fixed source blanket that fills the top bowl instead of cascading.
    """
    mode = str(cfg.get("nozzle_mode", "centroid")).lower()
    if mode == "all" or len(nozzles_m) <= 1:
        return nozzles_m
    if mode == "index":
        idx = int(cfg.get("nozzle_index", 0) or 0) % len(nozzles_m)
        return [nozzles_m[idx]]
    if mode == "centroid":
        n = float(len(nozzles_m))
        return [[
            sum(float(p[0]) for p in nozzles_m) / n,
            sum(float(p[1]) for p in nozzles_m) / n,
            max(float(p[2]) for p in nozzles_m),
        ]]
    if mode == "downstream_edge":
        modules_path = Path(cfg["module_bboxes_path"]) if cfg.get("module_bboxes_path") else None
        modules = load_module_bboxes(modules_path)
        if len(modules) < 2:
            return select_nozzles(nozzles_m, {**cfg, "nozzle_mode": "centroid"})
        n = float(len(nozzles_m))
        nozzle_c = (
            sum(float(p[0]) for p in nozzles_m) / n,
            sum(float(p[1]) for p in nozzles_m) / n,
            max(float(p[2]) for p in nozzles_m),
        )
        order = sorted(range(len(modules)), key=lambda i: modules[i][1][2], reverse=True)
        top = modules[order[0]]
        nxt = modules[order[1]]
        top_c = ((top[0][0] + top[1][0]) * 0.5, (top[0][1] + top[1][1]) * 0.5)
        nxt_c = ((nxt[0][0] + nxt[1][0]) * 0.5, (nxt[0][1] + nxt[1][1]) * 0.5)
        dx = nxt_c[0] - top_c[0]
        dy = nxt_c[1] - top_c[1]
        mag = max((dx * dx + dy * dy) ** 0.5, 1.0e-9)
        ux, uy = dx / mag, dy / mag
        edge = max(nozzles_m, key=lambda p: (float(p[0]) - nozzle_c[0]) * ux
                                      + (float(p[1]) - nozzle_c[1]) * uy)
        blend = max(0.0, min(float(cfg.get("nozzle_downstream_blend", 0.65) or 0.65), 1.0))
        return [[
            nozzle_c[0] + (float(edge[0]) - nozzle_c[0]) * blend,
            nozzle_c[1] + (float(edge[1]) - nozzle_c[1]) * blend,
            nozzle_c[2],
        ]]
    if mode == "stride":
        max_points = max(int(cfg.get("nozzle_max_points", 8) or 8), 1)
        if len(nozzles_m) <= max_points:
            return nozzles_m
        step = max(len(nozzles_m) / float(max_points), 1.0)
        return [nozzles_m[min(int(round(i * step)), len(nozzles_m) - 1)]
                for i in range(max_points)]
    raise ValueError(f"unknown nozzle_mode={mode!r}; expected all, centroid, downstream_edge, index, or stride")


def pick_default_stl() -> Path:
    cfg_path = CONFIG_DEFAULT
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            override = cfg.get("collider_stl_path")
            if override:
                p = Path(override)
                if not p.is_absolute():
                    p = REPO_ROOT / p
                if p.exists() and p.stat().st_size >= 100:
                    return p
        except Exception:
            pass
    if DEFAULT_COLLIDER_THICK.exists():
        return DEFAULT_COLLIDER_THICK
    if DEFAULT_COLLIDER_RAW.exists():
        return DEFAULT_COLLIDER_RAW
    raise FileNotFoundError("no collider STL - run extract_targets.py (+ thicken_collider.py)")


def read_stl_bbox(path: Path) -> list[list[float]]:
    """Return [[x0,y0,z0], [x1,y1,z1]] for binary or ASCII STL."""
    raw = path.read_bytes()
    points: list[tuple[float, float, float]] = []
    if len(raw) >= 84:
        n_tri = struct.unpack("<I", raw[80:84])[0]
        expected = 84 + n_tri * 50
        if expected <= len(raw):
            for i in range(n_tri):
                base = 84 + i * 50 + 12
                for j in range(3):
                    points.append(struct.unpack("<3f", raw[base + j * 12:base + (j + 1) * 12]))
    if not points:
        for line in raw.decode("utf-8", errors="ignore").splitlines():
            parts = line.strip().split()
            if len(parts) == 4 and parts[0].lower() == "vertex":
                try:
                    points.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    pass
    if not points:
        raise RuntimeError(f"STL {path} did not contain readable vertices")
    xs, ys, zs = zip(*points)
    return [[min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]]


def nozzle_vz_from_lpm(lpm: float, dp_m: float, velocity_floor_mps: float = 1.0) -> float:
    """Per-nozzle initial downward velocity from volumetric flow.
    v = Q/A, floored at -1 m/s (LBM stability - very low LPM still produces
    non-zero motion). Returns negative (downward)."""
    q_m3s = lpm * 1.0e-3 / 60.0
    area = dp_m * dp_m
    return -max(q_m3s / area, velocity_floor_mps)


def nozzle_vz_from_config(cfg: dict) -> float:
    """Return downward nozzle velocity in m/s.

    `nozzle_velocity_mps` is the preferred control for this project because the
    Rhino nozzle layer is a sampled inlet surface, not 230 independent faucets.
    `nozzle_LPM` remains as a backwards-compatible fallback.
    """
    direct = float(cfg.get("nozzle_velocity_mps", 0) or 0)
    if direct > 0:
        return -direct
    floor = float(cfg.get("nozzle_velocity_floor_mps", 1.0) or 1.0)
    return nozzle_vz_from_lpm(float(cfg["nozzle_LPM"]), float(cfg["dp_m"]), floor)


def nozzle_horizontal_velocity(cfg: dict) -> tuple[float, float]:
    speed = float(cfg.get("nozzle_horizontal_mps", 0.0) or 0.0)
    if speed <= 0.0:
        return 0.0, 0.0
    mode = str(cfg.get("nozzle_horizontal_mode", "toward_next_module")).lower()
    if mode == "none":
        return 0.0, 0.0
    modules_path = Path(cfg["module_bboxes_path"]) if cfg.get("module_bboxes_path") else None
    modules = load_module_bboxes(modules_path)
    if mode == "toward_next_module" and len(modules) >= 2:
        order = sorted(range(len(modules)), key=lambda i: modules[i][1][2], reverse=True)
        top = modules[order[0]]
        nxt = modules[order[1]]
        top_c = ((top[0][0] + top[1][0]) * 0.5, (top[0][1] + top[1][1]) * 0.5)
        nxt_c = ((nxt[0][0] + nxt[1][0]) * 0.5, (nxt[0][1] + nxt[1][1]) * 0.5)
        dx = nxt_c[0] - top_c[0]
        dy = nxt_c[1] - top_c[1]
    else:
        dx = float(cfg.get("nozzle_horizontal_x", 0.0) or 0.0)
        dy = float(cfg.get("nozzle_horizontal_y", 0.0) or 0.0)
    mag = max((dx * dx + dy * dy) ** 0.5, 1.0e-9)
    return speed * dx / mag, speed * dy / mag


def write_nozzles_txt(path: Path, nozzles_m: list, vx: float, vy: float, vz: float) -> None:
    lines = ["# x_m y_m z_m vx_mps vy_mps vz_mps  (auto-generated from _real_targets.json)"]
    for nz in nozzles_m:
        lines.append(f"{nz[0]:.6f} {nz[1]:.6f} {nz[2]:.6f} {vx:.6f} {vy:.6f} {vz:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_case_txt(path: Path, payload: dict) -> None:
    lines = []
    for k, v in payload.items():
        if isinstance(v, (list, tuple)):
            lines.append(k + " " + " ".join(f"{x:.6f}" for x in v))
        elif isinstance(v, bool):
            lines.append(f"{k} {1 if v else 0}")
        else:
            lines.append(f"{k} {v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compute_domain(stl_bbox: list, pad_m: float, nozzles_m: list) -> tuple[list, list]:
    (x0, y0, z0), (x1, y1, z1) = stl_bbox
    nozzle_z_top = max(p[2] for p in nozzles_m) if nozzles_m else z1
    lo = [x0 - pad_m, y0 - pad_m, 0.0]            # floor at z=0
    hi = [x1 + pad_m, y1 + pad_m, max(z1, nozzle_z_top + 1.0)]
    return lo, hi


def append_settings_log(test_id: str, case: dict, result: dict,
                         iter_dir: Path, wall_s: float,
                         collider_stl: Path, cfg: dict) -> None:
    r = result.get("retention", {}) or {}
    arrival = result.get("arrival", {}) or {}
    diagnostics = result.get("diagnostics", {}) or {}
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "test_id": test_id,
        "engine": result.get("engine", "fluidx3d"),
        "solver_collision": cfg.get("_solver_collision"),
        # case.txt group
        "dp_m": case.get("dp_m"),
        "timemax_s": case.get("timemax_s"),
        "dt_out_s": case.get("dt_out_s"),
        "nozzle_refill_dt_s": case.get("nozzle_refill_dt_s"),
        "nozzle_refill_col_h": case.get("nozzle_refill_col_h"),
        "nozzle_emit_col_h": case.get("nozzle_emit_col_h"),
        "surface_tension_Npm": case.get("surface_tension_Npm"),
        "viscosity_m2ps": case.get("viscosity_m2ps"),
        "density_kgpm3": case.get("density_kgpm3"),
        "gravity_mps2": case.get("gravity_mps2"),
        "side_walls": case.get("side_walls"),
        "seed_col_h": case.get("seed_col_h"),
        "domain_bbox_m": case.get("domain_bbox_m"),
        # nozzles group
        "n_nozzles": cfg.get("_n_nozzles"),
        "raw_n_nozzles": cfg.get("_raw_n_nozzles"),
        "nozzle_mode": cfg.get("nozzle_mode"),
        "nozzle_max_points": cfg.get("nozzle_max_points"),
        "nozzle_LPM": cfg.get("nozzle_LPM"),
        "nozzle_velocity_mps": cfg.get("nozzle_velocity_mps"),
        "nozzle_velocity_floor_mps": cfg.get("nozzle_velocity_floor_mps"),
        "nozzle_horizontal_mps": cfg.get("nozzle_horizontal_mps"),
        "nozzle_horizontal_mode": cfg.get("nozzle_horizontal_mode"),
        "nozzle_vx_mps": cfg.get("_nozzle_vx"),
        "nozzle_vy_mps": cfg.get("_nozzle_vy"),
        "nozzle_vz_mps": cfg.get("_nozzle_vz"),
        "total_Q_m3ps": cfg.get("_total_Q_m3ps"),
        # preprocess
        "thicken_thickness_m": cfg.get("thicken_thickness_m"),
        "collider_stl": str(collider_stl).replace("\\", "/"),
        # results
        "score": result.get("score"),
        "arrival_score": arrival.get("score", result.get("arrival_score")),
        "floor_hit_band_m": diagnostics.get("floor_hit_band_m"),
        "in_positive": r.get("in_positive"),
        "in_negative": r.get("in_negative"),
        "in_column": r.get("in_column"),
        "splash": r.get("splash"),
        "total": r.get("total"),
        "retention_rate": r.get("retention_rate"),
        "reached_floor": diagnostics.get("reached_floor"),
        "final_reached_floor": diagnostics.get("final_reached_floor"),
        "arrival_floor_total": arrival.get("floor_total"),
        "arrival_target_total": arrival.get("target_total"),
        "arrival_frames_with_floor_contact": arrival.get("frames_with_floor_contact"),
        "frames_with_floor_contact": arrival.get("frames_with_floor_contact"),
        "max_floor_cells_per_frame": arrival.get("max_floor_cells_per_frame"),
        "strong_floor_arrival": arrival.get("strong_floor_arrival"),
        "max_module_counts": arrival.get("max_module_counts"),
        "modules_with_fluid": arrival.get("modules_with_fluid"),
        "source_tail_late_frames": diagnostics.get("source_tail_late_frames"),
        "max_source_tail_cells": diagnostics.get("max_source_tail_cells"),
        "continuous_source": diagnostics.get("continuous_source"),
        "top_module_final_cells": diagnostics.get("top_module_final_cells"),
        "top_module_final_ratio": diagnostics.get("top_module_final_ratio"),
        "top_retention_failure": diagnostics.get("top_retention_failure"),
        "final_score": result.get("final_score"),
        "lowest_fluid_z_m": diagnostics.get("lowest_fluid_z_m"),
        "floor_hit_count": diagnostics.get("floor_hit_count"),
        # cost / paths
        "wall_s": round(wall_s, 2),
        "iter_dir": str(iter_dir).replace("\\", "/"),
        "frames_dir": str(iter_dir / "fx3d_out" / "frames").replace("\\", "/"),
        # post-run annotations (filled later by dashboard or analyst - start null)
        "issue": result.get("issue"),
        "notes": result.get("notes"),
    }
    SETTINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --- main API ----------------------------------------------------------------

def run_experiment(test_id: str,
                   *,
                   stl_path: Path | None = None,
                   config_overrides: dict | None = None,
                   config_path: Path = CONFIG_DEFAULT,
                   push_rhino: bool | None = None,
                   interactive: bool = False,
                   timeout_s: float = 7200.0,
                   runs_root: Path | None = None) -> dict:
    cfg = load_config(config_path)
    build_cfg = load_build_config()
    solver_collision = str(build_cfg.get("collision", "SRT")).upper()
    if config_overrides:
        cfg.update(config_overrides)
    if stl_path is None:
        stl_path = pick_default_stl()
    if push_rhino is None:
        push_rhino = bool(cfg.get("push_to_rhino", True))

    iter_dir = (runs_root or RUNS) / f"iter_{test_id}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "fx3d_out" / "frames").mkdir(parents=True, exist_ok=True)
    (iter_dir / "fx3d_out" / "vtk").mkdir(parents=True, exist_ok=True)

    # STL copy (snapshot in iter dir, plus FluidX3D reads from absolute path)
    local_stl = iter_dir / "sculpture.stl"
    shutil.copy(stl_path, local_stl)
    if local_stl.stat().st_size < 100:
        raise RuntimeError(
            f"STL {stl_path} is empty/corrupt ({local_stl.stat().st_size} bytes). "
            "Regenerate via extract_targets.py + thicken_collider.py, or "
            "build parametric STL via pymoo_gen_module.")
    stl_bbox = read_stl_bbox(local_stl)

    # Targets (positive/negative + nozzles)
    targets = load_targets()
    raw_nozzles_m = targets["nozzles_m"]
    nozzles_m = select_nozzles(raw_nozzles_m, cfg)
    vx, vy = nozzle_horizontal_velocity(cfg)
    vz = nozzle_vz_from_config(cfg)
    write_nozzles_txt(iter_dir / "nozzles.txt", nozzles_m, vx, vy, vz)

    # Domain auto-compute (sculpture + nozzle + pad)
    domain_lo, domain_hi = compute_domain(stl_bbox, cfg["domain_pad_m"], nozzles_m)

    # case.txt for FluidX3D
    case = {
        "stl_path": str(local_stl).replace("\\", "/"),
        "out_dir": str(iter_dir / "fx3d_out").replace("\\", "/") + "/",
        "nozzles_file": str(iter_dir / "nozzles.txt").replace("\\", "/"),
        "domain_bbox_m": domain_lo + domain_hi,
        "dp_m": cfg["dp_m"],
        "timemax_s": cfg["timemax_s"],
        "dt_out_s": cfg["dt_out_s"],
        "nozzle_refill_dt_s": cfg.get("nozzle_refill_dt_s", cfg["dt_out_s"]),
        "nozzle_refill_col_h": cfg.get("nozzle_refill_col_h", 3),
        "nozzle_emit_col_h": cfg.get("nozzle_emit_col_h", cfg.get("nozzle_refill_col_h", 3)),
        "seed_col_h": cfg["seed_col_h"],
        "lbm_u_ref": cfg.get("lbm_u_ref", 0.05),
        "nozzle_rho_inflow": cfg.get("nozzle_rho_inflow", 1.0),
        "nozzle_area_cells": cfg.get("nozzle_area_cells", 1),
        "pond_prefill_z_m": cfg.get("pond_prefill_z_m", 0),
        "pond_prefill_z_bot_m": cfg.get("pond_prefill_z_bot_m", 0),
        "pond_prefill_xy_bbox_m": cfg.get("pond_prefill_xy_bbox_m", [0, 0, 0, 0]),
        "side_walls": cfg["side_walls"],
        "floor_type": cfg.get("floor_type", "S"),
        "visualization_modes": cfg.get("visualization_modes", "PHI_RAYTRACE,FLAG_SURFACE"),
        "surface_tension_Npm": cfg["surface_tension_Npm"],
        "viscosity_m2ps": cfg["viscosity_m2ps"],
        "density_kgpm3": cfg["density_kgpm3"],
        "gravity_mps2": cfg["gravity_mps2"],
        "camera": cfg["camera"],
    }
    write_case_txt(iter_dir / "case.txt", case)

    # case.json (richer snapshot for postprocess + history)
    (iter_dir / "case.json").write_text(json.dumps({
        **case,
        "test_id": test_id,
        "solver_collision": solver_collision,
        "positive_bbox_m": targets["positive_bbox_m"],
        "negative_bbox_m": targets["negative_bbox_m"],
        "module_bboxes_m": load_module_bboxes(Path(cfg["module_bboxes_path"]) if cfg.get("module_bboxes_path") else None),
        "module_bboxes_path": cfg.get("module_bboxes_path"),
        "score_slab_thickness_m": cfg["score_slab_thickness_m"],
        "floor_hit_band_m": cfg.get("floor_hit_band_m", cfg["score_slab_thickness_m"]),
        "arrival_min_floor_cells_per_frame": cfg.get("arrival_min_floor_cells_per_frame", 10),
        "arrival_min_floor_contact_frames": cfg.get("arrival_min_floor_contact_frames", 5),
        "arrival_min_floor_total": cfg.get("arrival_min_floor_total", 100),
        "top_lock_min_drop_m": cfg.get("top_lock_min_drop_m", 10.0),
        "top_retention_max_final_ratio": cfg.get("top_retention_max_final_ratio", 0.35),
        "top_retention_max_final_cells": cfg.get("top_retention_max_final_cells", 1000),
        "cascade_min_module_touch_cells": cfg.get("cascade_min_module_touch_cells", 25),
        "cascade_min_modules_with_fluid": cfg.get("cascade_min_modules_with_fluid", 3),
        "source_tail_min_cells": cfg.get("source_tail_min_cells"),
        "source_tail_min_late_frames": cfg.get("source_tail_min_late_frames"),
        "fluid_threshold": cfg.get("fluid_threshold", 0.5),
        "n_nozzles": len(nozzles_m),
        "raw_n_nozzles": len(raw_nozzles_m),
        "nozzle_mode": cfg.get("nozzle_mode", "centroid"),
        "nozzle_max_points": cfg.get("nozzle_max_points"),
        "nozzle_LPM": cfg["nozzle_LPM"],
        "nozzle_velocity_mps": cfg.get("nozzle_velocity_mps", 0),
        "nozzle_velocity_floor_mps": cfg.get("nozzle_velocity_floor_mps", 1.0),
        "nozzle_refill_dt_s": cfg.get("nozzle_refill_dt_s", cfg["dt_out_s"]),
        "nozzle_refill_col_h": cfg.get("nozzle_refill_col_h", 3),
        "nozzle_emit_col_h": cfg.get("nozzle_emit_col_h", cfg.get("nozzle_refill_col_h", 3)),
        "nozzle_vz_mps": vz,
        "nozzle_vx_mps": vx,
        "nozzle_vy_mps": vy,
        "total_Q_m3ps": len(nozzles_m) * cfg["nozzle_LPM"] * 1e-3 / 60.0,
        "thicken_thickness_m": cfg.get("thicken_thickness_m"),
        "collider_stl_source": str(stl_path).replace("\\", "/"),
    }, indent=2), encoding="utf-8")

    # Run FluidX3D (interactive uses the GUI variant - viz only, no PNG/VTK)
    exe = FLUIDX3D_EXE_INTERACTIVE if interactive else FLUIDX3D_EXE
    if not exe.exists():
        raise FileNotFoundError(f"{exe} missing. Run scripts/build_fluidx3d.py to build it.")
    mode_tag = "INTERACTIVE" if interactive else "PNG"
    print(f"[{test_id}] FluidX3D launching ({mode_tag} mode, solver={solver_collision}, dp={cfg['dp_m']} timemax={cfg['timemax_s']} side_walls={cfg['side_walls']})")
    # PNG mode: suppress console window (fully background); INTERACTIVE: keep window
    creation_flags = 0
    startup_info = None
    if not interactive and sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = 0  # SW_HIDE
    t0 = time.time()
    proc = subprocess.run([str(exe)],
                          cwd=str(iter_dir),
                          capture_output=True, text=True, timeout=timeout_s,
                          creationflags=creation_flags, startupinfo=startup_info)
    wall = time.time() - t0
    (iter_dir / "fx3d_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (iter_dir / "fx3d_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    print(f"[{test_id}] wall={wall:.1f}s rc={proc.returncode}")
    if proc.returncode != 0:
        print("  stdout tail:\n  " + "\n  ".join(proc.stdout.splitlines()[-10:]))

    # Interactive mode: no PNG/VTK output, so skip postprocess + DB + Rhino push.
    if interactive:
        print("  (interactive run - DB skipped)")
        return {"engine": "fluidx3d-interactive", "wall_time_s": round(wall, 1),
                 "score": None, "retention": {}}

    # Postprocess
    result = postprocess(iter_dir)
    result["wall_time_s"] = round(wall, 1)
    (iter_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    r = result["retention"]
    arrival = result.get("arrival", {}) or {}
    print(f"  score={result['score']:.4f}  in_pos={r['in_positive']}  in_neg={r['in_negative']}  "
          f"in_col={r['in_column']}  splash={r['splash']}  total={r['total']}")
    print(f"  arrival_floor={arrival.get('floor_total', 0)}  arrival_target={arrival.get('target_total', 0)}  "
          f"frames_floor={arrival.get('frames_with_floor_contact', 0)}  final_score={result.get('final_score', 0):.4f}")
    diagnostics = result.get("diagnostics", {}) or {}
    print(f"  arrival_score={arrival.get('score', 0):.4f}  issue={result.get('issue') or 'none'}  "
          f"top_final={diagnostics.get('top_module_final_cells', 0)}  "
          f"top_ratio={diagnostics.get('top_module_final_ratio', 0):.3f}  "
          f"modules={diagnostics.get('modules_with_fluid', 0)}  "
          f"source_late={diagnostics.get('source_tail_late_frames', 0)}")

    # Settings DB
    cfg_for_log = {**cfg,
                   "_n_nozzles": len(nozzles_m),
                   "_raw_n_nozzles": len(raw_nozzles_m),
                   "_nozzle_vx": vx,
                   "_nozzle_vy": vy,
                   "_nozzle_vz": vz,
                   "_total_Q_m3ps": len(nozzles_m) * cfg["nozzle_LPM"] * 1e-3 / 60.0,
                   "_solver_collision": solver_collision}
    append_settings_log(test_id, case, result, iter_dir, wall, stl_path, cfg_for_log)

    # Dashboard (Flask) serves DB live -- no static refresh needed.

    # Rhino push
    if push_rhino:
        try:
            push_stl_to_rhino_layer(local_stl, f"fluidx3d::{test_id}::sculpture",
                                    (140, 90, 30), obj_name=f"sculpture_{test_id}")
        except Exception as e:
            print(f"  (Rhino push skipped: {e})")

    return result


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test-id", default="real", help="iter_<test_id>/ directory under runs/")
    ap.add_argument("--stl", default=None, help="path to collider STL (default: _real_collider_thickened.stl)")
    ap.add_argument("--config", default=str(CONFIG_DEFAULT), help="canonical config json")
    ap.add_argument("--no-push", action="store_true", help="skip Rhino push")
    ap.add_argument("--interactive", action="store_true", help="use FluidX3D_interactive.exe (GUI window, no DB)")
    args = ap.parse_args(argv[1:])

    stl_path = Path(args.stl).resolve() if args.stl else None
    config_path = Path(args.config).resolve()
    run_experiment(args.test_id,
                    stl_path=stl_path,
                    config_path=config_path,
                    push_rhino=False if args.no_push else None,
                    interactive=args.interactive)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
