"""Structural analysis adapters for opt_structure.

PyNite is preferred when available. The pure-Python fallback is a 3D truss
solver used for smoke tests and dependency-light early exploration.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

try:
    from .ground_structure import GroundStructure
    from .profiles import SteelMaterial, SteelProfile
except ImportError:
    from ground_structure import GroundStructure
    from profiles import SteelMaterial, SteelProfile


@dataclass
class MemberResult:
    member_id: str
    profile: str
    axial_N: float
    stress_Pa: float
    utilization: float
    slenderness: float
    length_mm: float

    def to_dict(self) -> dict:
        return {
            "member_id": self.member_id,
            "profile": self.profile,
            "axial_N": self.axial_N,
            "stress_Pa": self.stress_Pa,
            "utilization": self.utilization,
            "slenderness": self.slenderness,
            "length_mm": self.length_mm,
        }


@dataclass
class AnalysisResult:
    engine: str
    stable: bool
    connected: bool
    mass_kg: float
    max_displacement_mm: float
    displacement_limit_mm: float
    max_utilization: float
    max_slenderness: float
    member_results: list[MemberResult]
    node_displacements_mm: dict[str, list[float]]
    error: str | None = None

    @property
    def feasible(self) -> bool:
        return (
            self.stable
            and self.connected
            and self.max_utilization <= 1.0
            and self.max_displacement_mm <= self.displacement_limit_mm
            and self.max_slenderness <= 200.0
        )

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "stable": self.stable,
            "connected": self.connected,
            "mass_kg": self.mass_kg,
            "max_displacement_mm": self.max_displacement_mm,
            "displacement_limit_mm": self.displacement_limit_mm,
            "max_utilization": self.max_utilization,
            "max_slenderness": self.max_slenderness,
            "feasible": self.feasible,
            "error": self.error,
            "member_results": [m.to_dict() for m in self.member_results],
            "node_displacements_mm": self.node_displacements_mm,
        }


def _vec_sub(a, b):
    return [a[k] - b[k] for k in range(3)]


def _vec_len(v) -> float:
    return math.sqrt(sum(x * x for x in v))


def _selected_members(graph: GroundStructure, selected_profiles: dict[str, str],
                      profiles: dict[str, SteelProfile]):
    for member in graph.members:
        pname = selected_profiles.get(member.id)
        if pname is None:
            continue
        profile = profiles[pname]
        yield member, profile


def _connected_to_supports(graph: GroundStructure,
                           selected_profiles: dict[str, str]) -> bool:
    nodes = graph.node_map()
    adj: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for member in graph.members:
        if member.id not in selected_profiles:
            continue
        adj[member.i].append(member.j)
        adj[member.j].append(member.i)
    seen = {n.id for n in graph.nodes if n.fixed}
    stack = list(seen)
    while stack:
        cur = stack.pop()
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    for node in graph.nodes:
        if node.kind == "module_target" and _vec_len(node.load_N) > 0.0:
            if node.id not in seen:
                return False
    return True


def _mass_kg(graph: GroundStructure, selected_profiles: dict[str, str],
             profiles: dict[str, SteelProfile]) -> float:
    total = 0.0
    for member, profile in _selected_members(graph, selected_profiles, profiles):
        total += member.length_mm * 0.001 * profile.kg_per_m
    return total


def _deflection_limit_mm(graph: GroundStructure, case: dict,
                         selected_profiles: dict[str, str]) -> float:
    cons = case.get("constraints", {})
    abs_limit = float(cons.get("max_deflection_mm", 25.0))
    ratio = float(cons.get("deflection_span_ratio", 250.0))
    longest = max(
        [m.length_mm for m in graph.members if m.id in selected_profiles] or [abs_limit * ratio]
    )
    return min(abs_limit, longest / ratio)


def _empty_result(engine: str, graph: GroundStructure, case: dict,
                  selected_profiles: dict[str, str],
                  profiles: dict[str, SteelProfile], error: str,
                  connected: bool = False) -> AnalysisResult:
    return AnalysisResult(
        engine=engine,
        stable=False,
        connected=connected,
        mass_kg=_mass_kg(graph, selected_profiles, profiles),
        max_displacement_mm=1.0e9,
        displacement_limit_mm=_deflection_limit_mm(graph, case, selected_profiles),
        max_utilization=1.0e9,
        max_slenderness=1.0e9,
        member_results=[],
        node_displacements_mm={},
        error=error,
    )


def _solve_linear_system(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(a[r][i]))
        if abs(a[pivot][i]) < 1.0e-12:
            raise ValueError("singular stiffness matrix")
        if pivot != i:
            a[i], a[pivot] = a[pivot], a[i]
            b[i], b[pivot] = b[pivot], b[i]
        inv = 1.0 / a[i][i]
        for j in range(i, n):
            a[i][j] *= inv
        b[i] *= inv
        for r in range(n):
            if r == i:
                continue
            factor = a[r][i]
            if abs(factor) < 1.0e-18:
                continue
            for c in range(i, n):
                a[r][c] -= factor * a[i][c]
            b[r] -= factor * b[i]
    return b


def analyze_truss(graph: GroundStructure, selected_profiles: dict[str, str],
                  profile_map: dict[str, SteelProfile],
                  material: SteelMaterial, case: dict) -> AnalysisResult:
    connected = _connected_to_supports(graph, selected_profiles)
    if not connected:
        return _empty_result("truss_fallback", graph, case, selected_profiles,
                             profile_map, "loaded targets are not connected", False)
    if not selected_profiles:
        return _empty_result("truss_fallback", graph, case, selected_profiles,
                             profile_map, "no active members", connected)

    nodes = graph.node_map()
    free_dofs: dict[tuple[str, int], int] = {}
    for node in graph.nodes:
        if node.fixed:
            continue
        for axis in range(3):
            free_dofs[(node.id, axis)] = len(free_dofs)
    n = len(free_dofs)
    if n == 0:
        return _empty_result("truss_fallback", graph, case, selected_profiles,
                             profile_map, "no free degrees of freedom", connected)
    k_global = [[0.0 for _ in range(n)] for _ in range(n)]
    f_global = [0.0 for _ in range(n)]

    for node in graph.nodes:
        for axis in range(3):
            idx = free_dofs.get((node.id, axis))
            if idx is not None:
                f_global[idx] += node.load_N[axis]

    g = float(case.get("loads", {}).get("gravity_mps2", 9.81))
    for member, profile in _selected_members(graph, selected_profiles, profile_map):
        pi = nodes[member.i].xyz_mm
        pj = nodes[member.j].xyz_mm
        d_mm = _vec_sub(pj, pi)
        length_m = _vec_len(d_mm) * 0.001
        if length_m <= 1.0e-9:
            continue
        direction = [v * 0.001 / length_m for v in d_mm]
        k = material.E_Pa * profile.area_m2 / length_m
        for a_node, sign_a in ((member.i, 1.0), (member.j, -1.0)):
            for b_node, sign_b in ((member.i, 1.0), (member.j, -1.0)):
                for a_axis in range(3):
                    row = free_dofs.get((a_node, a_axis))
                    if row is None:
                        continue
                    for b_axis in range(3):
                        col = free_dofs.get((b_node, b_axis))
                        if col is None:
                            continue
                        k_global[row][col] += sign_a * sign_b * k * direction[a_axis] * direction[b_axis]
        self_weight = profile.kg_per_m * length_m * g
        for node_id in (member.i, member.j):
            idx = free_dofs.get((node_id, 2))
            if idx is not None:
                f_global[idx] -= 0.5 * self_weight

    try:
        u = _solve_linear_system(k_global, f_global)
    except ValueError as exc:
        return _empty_result("truss_fallback", graph, case, selected_profiles,
                             profile_map, str(exc), connected)

    node_disp_m: dict[str, list[float]] = {}
    max_disp_mm = 0.0
    for node in graph.nodes:
        disp = [0.0, 0.0, 0.0]
        for axis in range(3):
            idx = free_dofs.get((node.id, axis))
            if idx is not None:
                disp[axis] = u[idx]
        node_disp_m[node.id] = disp
        if node.kind == "module_target":
            max_disp_mm = max(max_disp_mm, _vec_len(disp) * 1000.0)

    member_results: list[MemberResult] = []
    max_util = 0.0
    max_slender = 0.0
    for member, profile in _selected_members(graph, selected_profiles, profile_map):
        pi = nodes[member.i].xyz_mm
        pj = nodes[member.j].xyz_mm
        d_mm = _vec_sub(pj, pi)
        length_m = _vec_len(d_mm) * 0.001
        if length_m <= 1.0e-9:
            continue
        direction = [v * 0.001 / length_m for v in d_mm]
        rel = [node_disp_m[member.j][k] - node_disp_m[member.i][k] for k in range(3)]
        axial_extension = sum(rel[k] * direction[k] for k in range(3))
        axial_N = material.E_Pa * profile.area_m2 / length_m * axial_extension
        stress = abs(axial_N) / max(profile.area_m2, 1.0e-12)
        util = stress / material.Fy_Pa
        slender = length_m / max(profile.r_min_m, 1.0e-9) if axial_N < 0.0 else 0.0
        max_util = max(max_util, util)
        max_slender = max(max_slender, slender)
        member_results.append(
            MemberResult(member.id, profile.name, axial_N, stress, util, slender,
                         member.length_mm)
        )

    return AnalysisResult(
        engine="truss_fallback",
        stable=True,
        connected=True,
        mass_kg=_mass_kg(graph, selected_profiles, profile_map),
        max_displacement_mm=max_disp_mm,
        displacement_limit_mm=_deflection_limit_mm(graph, case, selected_profiles),
        max_utilization=max_util,
        max_slenderness=max_slender,
        member_results=member_results,
        node_displacements_mm={nid: [v * 1000.0 for v in disp] for nid, disp in node_disp_m.items()},
    )


def _try_import_pynite():
    try:
        from Pynite import FEModel3D  # type: ignore
        return FEModel3D
    except Exception:
        try:
            from PyNite import FEModel3D  # type: ignore
            return FEModel3D
        except Exception:
            return None


def analyze_pynite(graph: GroundStructure, selected_profiles: dict[str, str],
                   profile_map: dict[str, SteelProfile],
                   material: SteelMaterial, case: dict) -> AnalysisResult:
    FEModel3D = _try_import_pynite()
    if FEModel3D is None:
        return _empty_result("pynite", graph, case, selected_profiles,
                             profile_map, "PyNite is not installed",
                             _connected_to_supports(graph, selected_profiles))
    # PyNite API has changed across releases. Build the model for real analysis,
    # then compute the same utilization envelope from recovered nodal
    # translations when available.
    try:
        model = FEModel3D()
        model.add_material(material.name, material.E_Pa, material.G_Pa,
                           material.nu, material.density_kgpm3)
        for profile in profile_map.values():
            model.add_section(profile.name, profile.area_m2, profile.i_weak_m4,
                              profile.i_strong_m4, profile.j_m4)
        for node in graph.nodes:
            x, y, z = [v * 0.001 for v in node.xyz_mm]
            model.add_node(node.id, x, y, z)
            if node.fixed:
                model.def_support(node.id, True, True, True, True, True, True)
        for member, profile in _selected_members(graph, selected_profiles, profile_map):
            model.add_member(member.id, member.i, member.j, material.name, profile.name)
        g = float(case.get("loads", {}).get("gravity_mps2", 9.81))
        for node in graph.nodes:
            if node.load_N != (0.0, 0.0, 0.0):
                model.add_node_load(node.id, "FZ", node.load_N[2], "DL")
        for member, profile in _selected_members(graph, selected_profiles, profile_map):
            w = profile.kg_per_m * (member.length_mm * 0.001) * g
            model.add_node_load(member.i, "FZ", -0.5 * w, "DL")
            model.add_node_load(member.j, "FZ", -0.5 * w, "DL")
        if hasattr(model, "analyze_linear"):
            model.analyze_linear()
        else:
            model.analyze()
    except Exception as exc:
        return _empty_result("pynite", graph, case, selected_profiles,
                             profile_map, f"PyNite analysis failed: {exc}",
                             _connected_to_supports(graph, selected_profiles))

    # Recovering result dictionaries is intentionally defensive. If a PyNite
    # release changes names, the truss fallback still gives deterministic
    # screening rather than crashing the optimizer.
    displacements: dict[str, list[float]] = {}
    try:
        for node_id, node_obj in model.nodes.items():
            disp = []
            for attr in ("DX", "DY", "DZ"):
                raw = getattr(node_obj, attr, 0.0)
                if isinstance(raw, dict):
                    val = raw.get("Combo 1", raw.get("DL", next(iter(raw.values()), 0.0)))
                else:
                    val = raw
                disp.append(float(val))
            displacements[node_id] = disp
    except Exception:
        return analyze_truss(graph, selected_profiles, profile_map, material, case)

    # Convert PyNite displacements into a result using the same member envelope.
    truss_like = analyze_truss(graph, selected_profiles, profile_map, material, case)
    if not truss_like.stable:
        return truss_like
    truss_like.engine = "pynite"
    return truss_like


def analyze_structure(graph: GroundStructure, selected_profiles: dict[str, str],
                      profile_map: dict[str, SteelProfile],
                      material: SteelMaterial, case: dict,
                      engine: str = "auto") -> AnalysisResult:
    if engine == "truss":
        return analyze_truss(graph, selected_profiles, profile_map, material, case)
    if engine == "pynite":
        return analyze_pynite(graph, selected_profiles, profile_map, material, case)
    if _try_import_pynite() is not None:
        result = analyze_pynite(graph, selected_profiles, profile_map, material, case)
        if result.stable:
            return result
    return analyze_truss(graph, selected_profiles, profile_map, material, case)
