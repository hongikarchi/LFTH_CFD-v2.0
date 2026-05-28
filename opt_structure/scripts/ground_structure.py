"""Ground-structure graph generation for steel support optimization."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_ROOT = SCRIPT_DIR.parent
REPO_ROOT = MODULE_ROOT.parent
DEFAULT_CONTEXT = MODULE_ROOT / "runs" / "_structure_context.json"
DEFAULT_CASE = MODULE_ROOT / "config" / "structure_case.json"

Vec3 = tuple[float, float, float]


@dataclass
class StructureNode:
    id: str
    xyz_mm: Vec3
    kind: str
    fixed: bool = False
    load_N: Vec3 = (0.0, 0.0, 0.0)
    module_index: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "xyz_mm": list(self.xyz_mm),
            "kind": self.kind,
            "fixed": self.fixed,
            "load_N": list(self.load_N),
            "module_index": self.module_index,
        }


@dataclass
class CandidateMember:
    id: str
    i: str
    j: str
    length_mm: float
    kind: str = "candidate"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "i": self.i,
            "j": self.j,
            "length_mm": self.length_mm,
            "kind": self.kind,
        }


@dataclass
class GroundStructure:
    nodes: list[StructureNode]
    members: list[CandidateMember]
    meta: dict

    def node_map(self) -> dict[str, StructureNode]:
        return {n.id: n for n in self.nodes}

    def member_map(self) -> dict[str, CandidateMember]:
        return {m.id: m for m in self.members}

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "members": [m.to_dict() for m in self.members],
            "meta": self.meta,
        }


def load_case(path: Path | str = DEFAULT_CASE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_context(path: Path | str = DEFAULT_CONTEXT) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dist(a: Vec3, b: Vec3) -> float:
    return math.sqrt(sum((a[k] - b[k]) ** 2 for k in range(3)))


def _mid(a: Vec3, b: Vec3, t: float) -> Vec3:
    return tuple(a[k] + (b[k] - a[k]) * t for k in range(3))  # type: ignore[return-value]


def _round_key(p: Vec3, tol_mm: float = 10.0) -> tuple[int, int, int]:
    return tuple(int(round(v / tol_mm)) for v in p)  # type: ignore[return-value]


class _NodeBuilder:
    def __init__(self) -> None:
        self.nodes: list[StructureNode] = []
        self.by_key: dict[tuple[int, int, int, str], str] = {}

    def add(self, xyz: Iterable[float], kind: str, *, fixed: bool = False,
            load_N: Vec3 = (0.0, 0.0, 0.0),
            module_index: int | None = None) -> str:
        p = tuple(float(v) for v in xyz)
        key = (*_round_key(p), kind)
        existing = self.by_key.get(key)
        if existing:
            node = self.nodes[int(existing.split("_")[-1])]
            node.fixed = node.fixed or fixed
            node.load_N = tuple(node.load_N[k] + load_N[k] for k in range(3))  # type: ignore[assignment]
            if node.module_index is None:
                node.module_index = module_index
            return existing
        node_id = f"N_{len(self.nodes)}"
        self.by_key[key] = node_id
        self.nodes.append(
            StructureNode(node_id, p, kind, fixed=fixed,
                          load_N=load_N, module_index=module_index)
        )
        return node_id


def _load_for_module(module_index: int, case: dict) -> float:
    loads = case.get("loads", {})
    dead_by_index = loads.get("module_dead_kg_by_index") or {}
    water_by_index = loads.get("module_water_kg_by_index") or {}
    dead_kg = float(dead_by_index.get(str(module_index), loads.get("module_dead_kg", 500.0)))
    water_kg = float(water_by_index.get(str(module_index), loads.get("module_water_kg", 300.0)))
    dyn = float(loads.get("water_dynamic_factor", 2.0))
    g = float(loads.get("gravity_mps2", 9.81))
    return (dead_kg + dyn * water_kg) * g


def _module_target_points(module: dict, case: dict) -> list[Vec3]:
    bbox = module.get("bbox_mm")
    if not bbox:
        center = tuple(float(v) for v in module.get("base_point_mm", (0.0, 0.0, 0.0)))
        return [center]  # type: ignore[list-item]
    lo = tuple(float(v) for v in bbox[0])
    hi = tuple(float(v) for v in bbox[1])
    scale = float(case.get("ground_structure", {}).get("target_corner_scale", 0.65))
    cx = 0.5 * (lo[0] + hi[0])
    cy = 0.5 * (lo[1] + hi[1])
    z = lo[2]
    hx = 0.5 * (hi[0] - lo[0]) * scale
    hy = 0.5 * (hi[1] - lo[1]) * scale
    return [
        (cx, cy, z),
        (cx - hx, cy - hy, z),
        (cx + hx, cy - hy, z),
        (cx + hx, cy + hy, z),
        (cx - hx, cy + hy, z),
    ]


def _add_member(members: list[CandidateMember], seen: set[tuple[str, str]],
                nodes: dict[str, StructureNode], i: str, j: str,
                min_len: float, max_len: float, kind: str = "candidate") -> None:
    if i == j:
        return
    key = tuple(sorted((i, j)))
    if key in seen:
        return
    length = _dist(nodes[i].xyz_mm, nodes[j].xyz_mm)
    if length < min_len or length > max_len:
        return
    seen.add(key)
    members.append(CandidateMember(f"M_{len(members)}", key[0], key[1], length, kind))


def generate_ground_structure(context: dict, case: dict) -> GroundStructure:
    gs_cfg = case.get("ground_structure", {})
    min_len = float(gs_cfg.get("min_member_length_mm", 300.0))
    max_len = float(gs_cfg.get("max_member_length_mm", 35000.0))
    nearest_supports = int(gs_cfg.get("nearest_supports_per_target", 4))
    target_to_target_max = float(gs_cfg.get("target_to_target_max_mm", 9000.0))

    nb = _NodeBuilder()
    support_ids: list[str] = []
    for curve in context.get("support_curves", []):
        for p in curve.get("points_mm", []):
            support_ids.append(nb.add(p, "support", fixed=True))

    for beam in context.get("existing_beams", []):
        for p in (beam.get("start_mm"), beam.get("end_mm")):
            if p:
                support_ids.append(nb.add(p, "existing_structure", fixed=True))

    if not support_ids:
        raise ValueError("no support nodes found; run extract_structure.py with env::structure_start curves")

    target_ids: list[str] = []
    module_targets: dict[int, list[str]] = {}
    for module in context.get("modules", []):
        idx = int(module.get("index", len(module_targets)))
        pts = _module_target_points(module, case)
        load_each = -_load_for_module(idx, case) / max(len(pts), 1)
        ids = [
            nb.add(p, "module_target", load_N=(0.0, 0.0, load_each),
                   module_index=idx)
            for p in pts
        ]
        target_ids.extend(ids)
        module_targets[idx] = ids

    if not target_ids:
        raise ValueError("no module targets found; expected modules in structure context")

    support_xyz = [nb.nodes[int(nid.split("_")[-1])].xyz_mm for nid in support_ids]
    support_centroid = tuple(sum(p[k] for p in support_xyz) / len(support_xyz) for k in range(3))
    intermediate_ids: list[str] = []
    levels = [float(v) for v in gs_cfg.get("intermediate_levels", [0.33, 0.66])]
    for idx, ids in module_targets.items():
        pts = [nb.nodes[int(nid.split("_")[-1])].xyz_mm for nid in ids]
        c = tuple(sum(p[k] for p in pts) / len(pts) for k in range(3))
        for level in levels:
            intermediate_ids.append(
                nb.add(_mid(support_centroid, c, level), "intermediate",
                       module_index=idx)
            )

    nodes = {n.id: n for n in nb.nodes}
    members: list[CandidateMember] = []
    seen: set[tuple[str, str]] = set()

    for tid in target_ids:
        tnode = nodes[tid]
        ranked = sorted(support_ids, key=lambda sid: _dist(nodes[sid].xyz_mm, tnode.xyz_mm))
        for sid in ranked[:max(nearest_supports, 1)]:
            _add_member(members, seen, nodes, sid, tid, min_len, max_len, "support_to_target")

    for mid in intermediate_ids:
        mnode = nodes[mid]
        ranked_supports = sorted(support_ids, key=lambda sid: _dist(nodes[sid].xyz_mm, mnode.xyz_mm))
        for sid in ranked_supports[:max(nearest_supports, 1)]:
            _add_member(members, seen, nodes, sid, mid, min_len, max_len, "support_to_intermediate")
        ranked_targets = sorted(target_ids, key=lambda tid: _dist(nodes[tid].xyz_mm, mnode.xyz_mm))
        for tid in ranked_targets[:6]:
            _add_member(members, seen, nodes, mid, tid, min_len, max_len, "intermediate_to_target")

    for ids in module_targets.values():
        for a_i, a in enumerate(ids):
            for b in ids[a_i + 1:]:
                _add_member(members, seen, nodes, a, b, min_len, target_to_target_max, "module_target_tie")

    for a_i, a in enumerate(target_ids):
        for b in target_ids[a_i + 1:]:
            if nodes[a].module_index != nodes[b].module_index:
                _add_member(members, seen, nodes, a, b, min_len, target_to_target_max, "inter_module_brace")

    return GroundStructure(
        list(nodes.values()),
        members,
        {
            "support_count": len(set(support_ids)),
            "target_count": len(target_ids),
            "intermediate_count": len(intermediate_ids),
            "module_count": len(module_targets),
        },
    )


def design_from_encoded(graph: GroundStructure, encoded: list[int],
                        profile_names: list[str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for value, member in zip(encoded, graph.members):
        idx = int(value)
        if idx <= 0:
            continue
        idx = min(idx, len(profile_names))
        selected[member.id] = profile_names[idx - 1]
    return selected


def seed_connected_design(graph: GroundStructure, profile_count: int,
                          *, supports_per_target: int = 3,
                          profile_index: int | None = None) -> list[int]:
    """Return a conservative encoded design connecting each target to supports."""
    if profile_index is None:
        profile_index = profile_count
    nodes = graph.node_map()
    encoded = [0 for _ in graph.members]
    target_ids = [n.id for n in graph.nodes if n.kind == "module_target"]
    for tid in target_ids:
        candidates = [
            (idx, m.length_mm)
            for idx, m in enumerate(graph.members)
            if m.kind == "support_to_target" and (m.i == tid or m.j == tid)
        ]
        for idx, _ in sorted(candidates, key=lambda item: item[1])[:supports_per_target]:
            encoded[idx] = profile_index
    # Tie target points belonging to the same module so loads can share paths.
    for idx, m in enumerate(graph.members):
        if m.kind == "module_target_tie":
            encoded[idx] = max(1, min(profile_index, profile_count))
    # Add a few intermediate braces only when they connect already selected islands.
    fixed_reachable = {n.id for n in graph.nodes if n.fixed}
    for idx, m in enumerate(graph.members):
        if encoded[idx] > 0:
            fixed_reachable.add(m.i)
            fixed_reachable.add(m.j)
    for idx, m in enumerate(graph.members):
        if m.kind in {"inter_module_brace", "intermediate_to_target"}:
            if nodes[m.i].module_index == nodes[m.j].module_index:
                encoded[idx] = max(1, min(profile_index, profile_count))
    return encoded


def write_graph(path: Path | str, graph: GroundStructure) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(graph.to_dict(), indent=2), encoding="utf-8")

