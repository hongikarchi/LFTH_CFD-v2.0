"""Steel H-section profile loading and unit conversion."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
DEFAULT_PROFILE_CSV = MODULE_ROOT / "data" / "ks_jis_h_profiles.csv"


@dataclass(frozen=True)
class SteelMaterial:
    name: str = "Steel"
    E_Pa: float = 200.0e9
    nu: float = 0.3
    density_kgpm3: float = 7850.0
    Fy_Pa: float = 275.0e6

    @property
    def G_Pa(self) -> float:
        return self.E_Pa / (2.0 * (1.0 + self.nu))


@dataclass(frozen=True)
class SteelProfile:
    name: str
    h_mm: float
    b_mm: float
    tw_mm: float
    tf_mm: float
    A_cm2: float
    I_strong_cm4: float
    I_weak_cm4: float
    J_cm4: float
    S_strong_cm3: float
    S_weak_cm3: float
    kg_per_m: float

    @property
    def area_m2(self) -> float:
        return self.A_cm2 * 1.0e-4

    @property
    def i_strong_m4(self) -> float:
        return self.I_strong_cm4 * 1.0e-8

    @property
    def i_weak_m4(self) -> float:
        return self.I_weak_cm4 * 1.0e-8

    @property
    def j_m4(self) -> float:
        return max(self.J_cm4 * 1.0e-8, self.i_weak_m4 * 0.01)

    @property
    def s_strong_m3(self) -> float:
        return self.S_strong_cm3 * 1.0e-6

    @property
    def s_weak_m3(self) -> float:
        return self.S_weak_cm3 * 1.0e-6

    @property
    def r_min_m(self) -> float:
        if self.area_m2 <= 0.0:
            return 0.0
        return (self.i_weak_m4 / self.area_m2) ** 0.5

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "h_mm": self.h_mm,
            "b_mm": self.b_mm,
            "tw_mm": self.tw_mm,
            "tf_mm": self.tf_mm,
            "area_m2": self.area_m2,
            "i_strong_m4": self.i_strong_m4,
            "i_weak_m4": self.i_weak_m4,
            "j_m4": self.j_m4,
            "s_strong_m3": self.s_strong_m3,
            "s_weak_m3": self.s_weak_m3,
            "kg_per_m": self.kg_per_m,
        }


def _positive_float(row: dict, key: str) -> float:
    value = float(row[key])
    if value <= 0.0:
        raise ValueError(f"{key} must be positive for profile {row.get('name')}")
    return value


def load_profiles(path: Path | str = DEFAULT_PROFILE_CSV) -> list[SteelProfile]:
    p = Path(path)
    out: list[SteelProfile] = []
    with p.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row or not row.get("name"):
                continue
            out.append(
                SteelProfile(
                    name=row["name"].strip(),
                    h_mm=_positive_float(row, "h_mm"),
                    b_mm=_positive_float(row, "b_mm"),
                    tw_mm=_positive_float(row, "tw_mm"),
                    tf_mm=_positive_float(row, "tf_mm"),
                    A_cm2=_positive_float(row, "A_cm2"),
                    I_strong_cm4=_positive_float(row, "I_strong_cm4"),
                    I_weak_cm4=_positive_float(row, "I_weak_cm4"),
                    J_cm4=_positive_float(row, "J_cm4"),
                    S_strong_cm3=_positive_float(row, "S_strong_cm3"),
                    S_weak_cm3=_positive_float(row, "S_weak_cm3"),
                    kg_per_m=_positive_float(row, "kg_per_m"),
                )
            )
    if not out:
        raise ValueError(f"no profiles loaded from {p}")
    return sorted(out, key=lambda prof: prof.kg_per_m)


def profile_by_name(profiles: list[SteelProfile]) -> dict[str, SteelProfile]:
    return {p.name: p for p in profiles}

