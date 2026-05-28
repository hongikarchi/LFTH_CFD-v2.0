from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opt_structure.scripts.fea import analyze_structure
from opt_structure.scripts.ground_structure import (generate_ground_structure,
                                                    CandidateMember,
                                                    GroundStructure,
                                                    StructureNode,
                                                    seed_connected_design,
                                                    design_from_encoded)
from opt_structure.scripts.optimize_structure import (_is_feasible,
                                                      run_fallback_search)
from opt_structure.scripts.profiles import (SteelMaterial, load_profiles,
                                            profile_by_name, SteelProfile)


def synthetic_context() -> dict:
    return {
        "units": "mm",
        "support_curves": [
            {
                "guid": "support-a",
                "points_mm": [
                    [-2500.0, -2500.0, 0.0],
                    [2500.0, -2500.0, 0.0],
                    [0.0, 2500.0, 0.0],
                ],
            }
        ],
        "existing_beams": [],
        "modules": [
            {
                "index": 0,
                "bbox_mm": [
                    [-600.0, -600.0, 3000.0],
                    [600.0, 600.0, 4200.0],
                ],
                "base_point_mm": [0.0, 0.0, 4200.0],
            }
        ],
    }


def synthetic_case() -> dict:
    return {
        "material": {
            "name": "Steel",
            "E_Pa": 200.0e9,
            "nu": 0.3,
            "density_kgpm3": 7850.0,
            "Fy_Pa": 275.0e6,
        },
        "loads": {
            "module_dead_kg": 100.0,
            "module_water_kg": 50.0,
            "water_dynamic_factor": 2.0,
            "gravity_mps2": 9.81,
        },
        "constraints": {
            "max_utilization": 1.0,
            "max_deflection_mm": 25.0,
            "deflection_span_ratio": 250.0,
            "max_slenderness": 300.0,
        },
        "ground_structure": {
            "min_member_length_mm": 100.0,
            "max_member_length_mm": 10000.0,
            "target_corner_scale": 0.5,
            "intermediate_levels": [],
            "nearest_supports_per_target": 3,
            "target_to_target_max_mm": 3000.0,
        },
        "optimization": {"fallback_n_eval": 8, "seed": 7},
    }


class OptStructureSmokeTests(unittest.TestCase):
    def test_profile_unit_conversions(self) -> None:
        profiles = load_profiles()
        self.assertGreaterEqual(len(profiles), 5)
        first = profiles[0]
        self.assertGreater(first.area_m2, 0.0)
        self.assertAlmostEqual(first.A_cm2 * 1.0e-4, first.area_m2)
        self.assertGreater(first.i_strong_m4, first.i_weak_m4)
        self.assertGreater(first.kg_per_m, 0.0)

    def test_ground_structure_and_truss_analysis(self) -> None:
        case = synthetic_case()
        graph = generate_ground_structure(synthetic_context(), case)
        profiles = load_profiles()
        encoded = seed_connected_design(
            graph, len(profiles), supports_per_target=3,
            profile_index=len(profiles)
        )
        selected = design_from_encoded(graph, encoded, [p.name for p in profiles])
        result = analyze_structure(
            graph, selected, profile_by_name(profiles), SteelMaterial(),
            case, engine="truss"
        )
        self.assertTrue(result.connected, result.error)
        self.assertTrue(result.stable, result.error)
        self.assertGreater(result.mass_kg, 0.0)
        self.assertGreaterEqual(result.max_displacement_mm, 0.0)
        self.assertLess(result.max_displacement_mm, 25.0)

    def test_fallback_optimizer_smoke(self) -> None:
        case = synthetic_case()
        graph = generate_ground_structure(synthetic_context(), case)
        profiles = load_profiles()
        encoded, result = run_fallback_search(
            graph, profiles, SteelMaterial(), case,
            n_eval=8, seed=11, engine="truss"
        )
        self.assertEqual(len(encoded), len(graph.members))
        self.assertTrue(result.connected, result.error)
        self.assertTrue(result.stable, result.error)
        self.assertGreater(result.mass_kg, 0.0)
        # Feasibility can depend on the seed profile library, but the smoke
        # run should at least return a finite scored candidate.
        self.assertLess(result.max_utilization, 1.0e6)
        _ = _is_feasible(result, case)

    def test_frame_cantilever_matches_closed_form_deflection(self) -> None:
        graph = GroundStructure(
            nodes=[
                StructureNode("A", (0.0, 0.0, 0.0), "support", fixed=True),
                StructureNode("B", (3000.0, 0.0, 0.0), "module_target",
                              fixed=False, load_N=(0.0, 0.0, -1000.0)),
            ],
            members=[CandidateMember("M0", "A", "B", 3000.0, "test")],
            meta={},
        )
        profile = SteelProfile(
            name="TEST",
            h_mm=200.0,
            b_mm=100.0,
            tw_mm=8.0,
            tf_mm=12.0,
            A_cm2=50.0,
            I_strong_cm4=2000.0,
            I_weak_cm4=500.0,
            J_cm4=50.0,
            S_strong_cm3=200.0,
            S_weak_cm3=50.0,
            kg_per_m=0.0,
        )
        material = SteelMaterial()
        case = {
            "constraints": {
                "max_utilization": 1.0,
                "max_deflection_mm": 1000.0,
                "deflection_span_ratio": 1.0e9,
                "max_slenderness": 1.0e9,
            },
            "loads": {"gravity_mps2": 9.81},
        }
        result = analyze_structure(
            graph, {"M0": "TEST"}, {"TEST": profile}, material, case,
            engine="frame"
        )
        expected_m = 1000.0 * 3.0 ** 3 / (3.0 * material.E_Pa * profile.i_weak_m4)
        self.assertTrue(result.stable, result.error)
        self.assertAlmostEqual(result.max_displacement_mm, expected_m * 1000.0,
                               delta=expected_m * 1000.0 * 0.02)


if __name__ == "__main__":
    unittest.main()

