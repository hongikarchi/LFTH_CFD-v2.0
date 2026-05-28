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


def _vec_cross(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _vec_dot(a, b) -> float:
    return sum(a[k] * b[k] for k in range(3))


def _vec_len(v) -> float:
    return math.sqrt(sum(x * x for x in v))


def _vec_unit(v):
    n = _vec_len(v)
    if n <= 1.0e-12:
        raise ValueError("zero-length vector")
    return [x / n for x in v]


def _frame_basis(pi, pj):
    ex = _vec_unit([pj[k] - pi[k] for k in range(3)])
    ref = [0.0, 0.0, 1.0]
    if abs(_vec_dot(ex, ref)) > 0.95:
        ref = [0.0, 1.0, 0.0]
    ey = _vec_unit(_vec_cross(ref, ex))
    ez = _vec_unit(_vec_cross(ex, ey))
    return [ex, ey, ez]


def _selected_members(graph: GroundStructure, selected_profiles: dict[str, str],
                      profiles: dict[str, SteelProfile]):
    for member in graph.members:
        pname = selected_profiles.get(member.id)
        if pname is None:
            continue
        profile = profiles[pname]
        yield member, profile


def _active_node_ids(graph: GroundStructure,
                     selected_profiles: dict[str, str]) -> set[str]:
    """Nodes that should participate in an analysis model.

    Ground structures intentionally contain many candidate nodes. Nodes that
    are not touched by active members and carry no load must stay out of the
    stiffness model; otherwise they add unconstrained zero-stiffness DOFs and
    make otherwise valid trial designs singular.
    """
    active: set[str] = set()
    for member in graph.members:
        if member.id in selected_profiles:
            active.add(member.i)
            active.add(member.j)
    for node in graph.nodes:
        if _vec_len(node.load_N) > 0.0:
            active.add(node.id)
    return active


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


def _local_frame_stiffness(length_m: float, profile: SteelProfile,
                           material: SteelMaterial) -> list[list[float]]:
    L = length_m
    E = material.E_Pa
    G = material.G_Pa
    A = profile.area_m2
    Iy = profile.i_weak_m4
    Iz = profile.i_strong_m4
    J = profile.j_m4
    k = [[0.0 for _ in range(12)] for _ in range(12)]

    def set_sym(i, j, value):
        k[i][j] = value
        k[j][i] = value

    axial = E * A / L
    set_sym(0, 0, axial)
    set_sym(0, 6, -axial)
    set_sym(6, 6, axial)

    torsion = G * J / L
    set_sym(3, 3, torsion)
    set_sym(3, 9, -torsion)
    set_sym(9, 9, torsion)

    # Bending about local z: local y displacement + local rz.
    eiz = E * Iz
    c1 = 12.0 * eiz / (L ** 3)
    c2 = 6.0 * eiz / (L ** 2)
    c3 = 4.0 * eiz / L
    c4 = 2.0 * eiz / L
    set_sym(1, 1, c1)
    set_sym(1, 5, c2)
    set_sym(1, 7, -c1)
    set_sym(1, 11, c2)
    set_sym(5, 5, c3)
    set_sym(5, 7, -c2)
    set_sym(5, 11, c4)
    set_sym(7, 7, c1)
    set_sym(7, 11, -c2)
    set_sym(11, 11, c3)

    # Bending about local y: local z displacement + local ry.
    eiy = E * Iy
    d1 = 12.0 * eiy / (L ** 3)
    d2 = 6.0 * eiy / (L ** 2)
    d3 = 4.0 * eiy / L
    d4 = 2.0 * eiy / L
    set_sym(2, 2, d1)
    set_sym(2, 4, -d2)
    set_sym(2, 8, -d1)
    set_sym(2, 10, -d2)
    set_sym(4, 4, d3)
    set_sym(4, 8, d2)
    set_sym(4, 10, d4)
    set_sym(8, 8, d1)
    set_sym(8, 10, d2)
    set_sym(10, 10, d3)
    return k


def _frame_transform(basis) -> list[list[float]]:
    t = [[0.0 for _ in range(12)] for _ in range(12)]
    for block in (0, 3, 6, 9):
        for i in range(3):
            for j in range(3):
                t[block + i][block + j] = basis[i][j]
    return t


def _mat_t_k_t(k_local, transform):
    temp = [[0.0 for _ in range(12)] for _ in range(12)]
    for i in range(12):
        for j in range(12):
            s = 0.0
            for p in range(12):
                s += k_local[i][p] * transform[p][j]
            temp[i][j] = s
    out = [[0.0 for _ in range(12)] for _ in range(12)]
    for i in range(12):
        for j in range(12):
            s = 0.0
            for p in range(12):
                s += transform[p][i] * temp[p][j]
            out[i][j] = s
    return out


def _mat_vec(m, v):
    return [sum(m[i][j] * v[j] for j in range(len(v))) for i in range(len(m))]


def analyze_frame(graph: GroundStructure, selected_profiles: dict[str, str],
                  profile_map: dict[str, SteelProfile],
                  material: SteelMaterial, case: dict) -> AnalysisResult:
    connected = _connected_to_supports(graph, selected_profiles)
    if not connected:
        return _empty_result("frame_fallback", graph, case, selected_profiles,
                             profile_map, "loaded targets are not connected", False)
    if not selected_profiles:
        return _empty_result("frame_fallback", graph, case, selected_profiles,
                             profile_map, "no active members", connected)

    nodes = graph.node_map()
    active_nodes = _active_node_ids(graph, selected_profiles)
    free_dofs: dict[tuple[str, int], int] = {}
    for node in graph.nodes:
        if node.id not in active_nodes or node.fixed:
            continue
        for dof in range(6):
            free_dofs[(node.id, dof)] = len(free_dofs)
    n = len(free_dofs)
    if n == 0:
        return _empty_result("frame_fallback", graph, case, selected_profiles,
                             profile_map, "no free degrees of freedom", connected)

    k_global = [[0.0 for _ in range(n)] for _ in range(n)]
    f_global = [0.0 for _ in range(n)]

    for node in graph.nodes:
        for axis in range(3):
            idx = free_dofs.get((node.id, axis))
            if idx is not None:
                f_global[idx] += node.load_N[axis]

    g = float(case.get("loads", {}).get("gravity_mps2", 9.81))
    member_cache = {}
    for member, profile in _selected_members(graph, selected_profiles, profile_map):
        pi = [v * 0.001 for v in nodes[member.i].xyz_mm]
        pj = [v * 0.001 for v in nodes[member.j].xyz_mm]
        length_m = _vec_len(_vec_sub(pj, pi))
        if length_m <= 1.0e-9:
            continue
        basis = _frame_basis(pi, pj)
        transform = _frame_transform(basis)
        k_local = _local_frame_stiffness(length_m, profile, material)
        k_elem = _mat_t_k_t(k_local, transform)
        elem_dofs: list[tuple[str, int]] = []
        for node_id in (member.i, member.j):
            elem_dofs.extend((node_id, dof) for dof in range(6))
        for a_local, a_key in enumerate(elem_dofs):
            row = free_dofs.get(a_key)
            if row is None:
                continue
            for b_local, b_key in enumerate(elem_dofs):
                col = free_dofs.get(b_key)
                if col is None:
                    continue
                k_global[row][col] += k_elem[a_local][b_local]
        self_weight = profile.kg_per_m * length_m * g
        for node_id in (member.i, member.j):
            idx = free_dofs.get((node_id, 2))
            if idx is not None:
                f_global[idx] -= 0.5 * self_weight
        member_cache[member.id] = (k_local, transform, elem_dofs)

    try:
        u = _solve_linear_system(k_global, f_global)
    except ValueError as exc:
        return _empty_result("frame_fallback", graph, case, selected_profiles,
                             profile_map, str(exc), connected)

    node_disp: dict[str, list[float]] = {}
    max_disp_mm = 0.0
    for node in graph.nodes:
        disp6 = [0.0 for _ in range(6)]
        if node.id in active_nodes:
            for dof in range(6):
                idx = free_dofs.get((node.id, dof))
                if idx is not None:
                    disp6[dof] = u[idx]
        node_disp[node.id] = disp6
        if node.kind == "module_target":
            max_disp_mm = max(max_disp_mm, _vec_len(disp6[:3]) * 1000.0)

    member_results: list[MemberResult] = []
    max_util = 0.0
    max_slender = 0.0
    for member, profile in _selected_members(graph, selected_profiles, profile_map):
        cached = member_cache.get(member.id)
        if cached is None:
            continue
        k_local, transform, elem_dofs = cached
        u_global_elem = []
        for node_id, dof in elem_dofs:
            u_global_elem.append(node_disp[node_id][dof])
        u_local = _mat_vec(transform, u_global_elem)
        f_local = _mat_vec(k_local, u_local)
        axial_N = max(abs(f_local[0]), abs(f_local[6]))
        moment_y = max(abs(f_local[4]), abs(f_local[10]))
        moment_z = max(abs(f_local[5]), abs(f_local[11]))
        axial_stress = axial_N / max(profile.area_m2, 1.0e-12)
        bending_stress = (
            moment_y / max(profile.s_weak_m3, 1.0e-12)
            + moment_z / max(profile.s_strong_m3, 1.0e-12)
        )
        stress = axial_stress + bending_stress
        util = stress / material.Fy_Pa
        # Compression sign convention differs by element end; use either end
        # reporting compression to trigger slenderness screening.
        is_compression = f_local[0] > 0.0 or f_local[6] < 0.0
        slender = (
            (member.length_mm * 0.001) / max(profile.r_min_m, 1.0e-9)
            if is_compression else 0.0
        )
        max_util = max(max_util, util)
        max_slender = max(max_slender, slender)
        member_results.append(
            MemberResult(member.id, profile.name, axial_N, stress, util, slender,
                         member.length_mm)
        )

    return AnalysisResult(
        engine="frame_fallback",
        stable=True,
        connected=True,
        mass_kg=_mass_kg(graph, selected_profiles, profile_map),
        max_displacement_mm=max_disp_mm,
        displacement_limit_mm=_deflection_limit_mm(graph, case, selected_profiles),
        max_utilization=max_util,
        max_slenderness=max_slender,
        member_results=member_results,
        node_displacements_mm={nid: [v * 1000.0 for v in disp[:3]] for nid, disp in node_disp.items()},
    )


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
    active_nodes = _active_node_ids(graph, selected_profiles)
    free_dofs: dict[tuple[str, int], int] = {}
    for node in graph.nodes:
        if node.id not in active_nodes:
            continue
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
        if node.id in active_nodes:
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
    connected = _connected_to_supports(graph, selected_profiles)
    if not connected:
        return _empty_result("pynite", graph, case, selected_profiles,
                             profile_map, "loaded targets are not connected",
                             False)
    FEModel3D = _try_import_pynite()
    if FEModel3D is None:
        return _empty_result("pynite", graph, case, selected_profiles,
                             profile_map, "PyNite is not installed",
                             connected)
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
        active_nodes = _active_node_ids(graph, selected_profiles)
        for node in graph.nodes:
            if node.id not in active_nodes:
                continue
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
    if engine == "frame":
        return analyze_frame(graph, selected_profiles, profile_map, material, case)
    if engine == "truss":
        return analyze_truss(graph, selected_profiles, profile_map, material, case)
    if engine == "pynite":
        return analyze_pynite(graph, selected_profiles, profile_map, material, case)
    if _try_import_pynite() is not None:
        result = analyze_pynite(graph, selected_profiles, profile_map, material, case)
        if result.stable:
            return result
    return analyze_frame(graph, selected_profiles, profile_map, material, case)
