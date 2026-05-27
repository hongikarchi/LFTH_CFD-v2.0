"""Build FluidX3D twice (PNG and Interactive variants) from config/build.json.

Reads canonical compile-time options from config/build.json, patches
external/FluidX3D/src/defines.hpp via regex, runs msbuild twice, produces:
  external/FluidX3D/bin/FluidX3D.exe              (GRAPHICS only)
  external/FluidX3D/bin/FluidX3D_interactive.exe  (INTERACTIVE_GRAPHICS + GRAPHICS)

Defines.hpp is reverted to its baseline at the end so the file in repo stays
clean -- the user's config/build.json is the source of truth.

Run:  python scripts/build_fluidx3d.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
FLUIDX3D_DIR = PROJECT / "external" / "FluidX3D"
DEFINES_HPP = FLUIDX3D_DIR / "src" / "defines.hpp"
VCXPROJ = FLUIDX3D_DIR / "FluidX3D.vcxproj"
BIN_DIR = FLUIDX3D_DIR / "bin"
CONFIG_PATH = PROJECT / "config" / "build.json"

MSBUILD = Path("C:/Program Files (x86)/Microsoft Visual Studio/18/BuildTools/MSBuild/Current/Bin/MSBuild.exe")

PRECISION_OPTIONS = {"FP16S", "FP16C"}     # if neither: pure FP32
VELOCITY_OPTIONS = {"D2Q9", "D3Q15", "D3Q19", "D3Q27"}


def patch_macro_bool(content: str, name: str, enable: bool) -> str:
    """Toggle `#define <name>` between active and commented (//#define name)."""
    pat = re.compile(r"^[ \t]*(?://)?\s*#define\s+" + re.escape(name) + r"\b.*$", re.MULTILINE)
    repl = f"#define {name}" if enable else f"//#define {name}"
    if not pat.search(content):
        return content + "\n" + repl + "\n"
    return pat.sub(repl, content)


def patch_macro_num(content: str, name: str, value: str) -> str:
    """Set `#define <name> <value>` (replaces the whole line including any trailing comment)."""
    pat = re.compile(
        r"^([ \t]*)(?://)?\s*#define\s+" + re.escape(name) + r"\b[^\n]*$",
        re.MULTILINE,
    )
    def _sub(m):
        indent = m.group(1) or ""
        return f"{indent}#define {name} {value}"
    if not pat.search(content):
        return content + f"\n#define {name} {value}\n"
    return pat.sub(_sub, content)


def patch_defines(cfg: dict, interactive: bool) -> str:
    """Build the modified defines.hpp content (does not write)."""
    src = DEFINES_HPP.read_text(encoding="utf-8")

    # precision
    prec = cfg.get("precision", "FP16S")
    for p in PRECISION_OPTIONS:
        src = patch_macro_bool(src, p, p == prec)

    # velocity set
    vs = cfg.get("velocity_set", "D3Q19")
    for v in VELOCITY_OPTIONS:
        src = patch_macro_bool(src, v, v == vs)

    # SUBGRID
    src = patch_macro_bool(src, "SUBGRID", bool(cfg.get("subgrid", False)))

    # GRAPHICS family
    src = patch_macro_bool(src, "GRAPHICS", True)
    src = patch_macro_bool(src, "INTERACTIVE_GRAPHICS", interactive)
    src = patch_macro_bool(src, "INTERACTIVE_GRAPHICS_ASCII", False)
    src = patch_macro_bool(src, "BENCHMARK", False)

    # Required physics extensions
    src = patch_macro_bool(src, "VOLUME_FORCE", True)
    src = patch_macro_bool(src, "EQUILIBRIUM_BOUNDARIES", True)
    src = patch_macro_bool(src, "SURFACE", True)

    # Graphics constants
    src = patch_macro_num(src, "GRAPHICS_U_MAX", f"{cfg.get('graphics_u_max', 0.18):.4f}f")
    src = patch_macro_num(src, "GRAPHICS_RHO_DELTA", f"{cfg.get('graphics_rho_delta', 0.001):.5f}f")
    src = patch_macro_num(src, "GRAPHICS_RAYTRACING_TRANSMITTANCE", f"{cfg.get('graphics_raytracing_transmittance', 0.25):.4f}f")
    src = patch_macro_num(src, "GRAPHICS_RAYTRACING_COLOR", str(cfg.get("graphics_raytracing_color", "0x005F7F")))
    src = patch_macro_num(src, "GRAPHICS_FRAME_WIDTH", str(int(cfg.get("graphics_frame_width", 1920))))
    src = patch_macro_num(src, "GRAPHICS_FRAME_HEIGHT", str(int(cfg.get("graphics_frame_height", 1080))))
    return src


def msbuild_once(out_name: str) -> Path:
    """Build and rename the output to <out_name>. Always renames so subsequent
    builds (also producing FluidX3D.exe) cannot overwrite this one."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    # Wipe object cache so defines.hpp changes actually trigger recompile.
    temp_dir = FLUIDX3D_DIR / "temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    src_exe = BIN_DIR / "FluidX3D.exe"
    if src_exe.exists():
        src_exe.unlink()
    dst_exe = BIN_DIR / out_name
    if dst_exe.exists():
        dst_exe.unlink()
    print(f"  msbuild ({out_name}) [clean rebuild]...")
    proc = subprocess.run(
        [str(MSBUILD), str(VCXPROJ), "/p:Configuration=Release", "/p:Platform=x64",
         "/t:Rebuild", "/m", "/verbosity:minimal"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(FLUIDX3D_DIR),
    )
    if proc.returncode != 0:
        last = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        raise RuntimeError(f"msbuild FAILED:\n{last}")
    if not src_exe.exists():
        raise RuntimeError(f"build said success but {src_exe} missing")
    # MOVE (not copy) so the next build can't clobber this artifact via its own
    # FluidX3D.exe output.
    if out_name == "FluidX3D.exe":
        # Build target same as output -> leave in place.
        pass
    else:
        shutil.move(str(src_exe), str(dst_exe))
    return dst_exe


def main() -> int:
    if not DEFINES_HPP.exists():
        print(f"ERROR: {DEFINES_HPP} missing"); return 1
    if not VCXPROJ.exists():
        print(f"ERROR: {VCXPROJ} missing"); return 1
    if not MSBUILD.exists():
        print(f"ERROR: msbuild not at {MSBUILD}"); return 1
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}

    baseline = DEFINES_HPP.read_text(encoding="utf-8")
    try:
        # 1) Interactive build first so its FluidX3D.exe can be renamed before
        #    the PNG build produces its own FluidX3D.exe.
        print("[1/2] INTERACTIVE build (GRAPHICS + INTERACTIVE_GRAPHICS)")
        DEFINES_HPP.write_text(patch_defines(cfg, interactive=True), encoding="utf-8")
        int_exe = msbuild_once("FluidX3D_interactive.exe")
        # 2) PNG build keeps the canonical FluidX3D.exe name.
        print("[2/2] PNG build (GRAPHICS only)")
        DEFINES_HPP.write_text(patch_defines(cfg, interactive=False), encoding="utf-8")
        png_exe = msbuild_once("FluidX3D.exe")
    finally:
        # Restore baseline so the file in repo stays canonical
        DEFINES_HPP.write_text(baseline, encoding="utf-8")
    print(f"DONE:\n  {png_exe}\n  {int_exe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
