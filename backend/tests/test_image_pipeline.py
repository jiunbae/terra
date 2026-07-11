from __future__ import annotations

import unittest
from pathlib import Path

from app.image_pipeline import (
    QualityMode,
    ScoredCandidate,
    build_candidate_plans,
    build_upscaler_command,
    compose_pipeline_prompt,
    derive_candidate_seed,
    get_pipeline_config,
    select_winner,
)
from app.schema import Inhabitant, PlanetSpec
from app.world_bible import build_world_bible


def _distinctive_spec() -> PlanetSpec:
    spec = PlanetSpec()
    spec.planet.name = "칼리페른"
    spec.planet.shape = "oblate"
    spec.planet.oblateness = 0.12
    spec.star.count = 2
    spec.star.colors_hex = ["#aa3311", "#eeeeff"]
    spec.surface.feature_type = "crystalline"
    spec.surface.material_type = "crystal"
    spec.surface.landmarks = ["crystal_fields", "cave_openings"]
    spec.surface.visual_prompt = "Vast violet crystal continents beneath amber storms."
    spec.surface.palette.lowland = "#443366"
    spec.atmosphere.color_hex = "#c080ff"
    spec.climate.phenomena = ["amber mineral hail"]
    spec.rings.present = True
    spec.inhabitants = [
        Inhabitant(
            name="케른",
            category="지성체",
            appearance="translucent indigo exoskeleton and exactly two pink antennae",
            physiology="silicate circulatory structures",
            culture="woven silver tunics",
            gravity_adaptation="short dense legs",
            portrait_prompt="full-body field portrait with both antennae visible",
        )
    ]
    return spec


class WorldBibleTests(unittest.TestCase):
    def test_bible_is_deterministic_and_json_compatible(self) -> None:
        spec = _distinctive_spec()
        first = build_world_bible(spec)
        second = build_world_bible(spec.model_copy(deep=True))

        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(len(first.world_id), 16)
        self.assertEqual(first.palette.lowland, "#443366")

        changed = spec.model_copy(deep=True)
        changed.surface.palette.lowland = "#112233"
        self.assertNotEqual(first.world_id, build_world_bible(changed).world_id)

    def test_each_kind_inherits_world_and_species_identity(self) -> None:
        bible = build_world_bible(_distinctive_spec())
        orbital = bible.identity_prompt("planet")
        surface = bible.identity_prompt("surface")
        inhabitant = bible.identity_prompt("inhabitant", inhabitant_index=0)

        for prompt in (orbital, surface, inhabitant):
            self.assertIn("#443366", prompt)
            self.assertIn("amber mineral hail", prompt)
            self.assertIn("prismatic mineral fields", prompt)
        self.assertIn("translucent indigo exoskeleton", inhabitant)
        self.assertIn("woven silver tunics", inhabitant)
        with self.assertRaises(ValueError):
            bible.identity_prompt("inhabitant", inhabitant_index=-1)


class PipelineConfigTests(unittest.TestCase):
    def test_modes_have_honest_distinct_policies(self) -> None:
        fast = get_pipeline_config("fast")
        balanced = get_pipeline_config(QualityMode.BALANCED)
        quality = get_pipeline_config("quality")

        self.assertEqual(fast.candidate_count, 1)
        self.assertFalse(fast.verify_candidates)
        self.assertEqual(balanced.candidate_count, 2)
        self.assertTrue(balanced.verify_candidates)
        self.assertFalse(balanced.refine_winner)
        self.assertEqual(quality.candidate_count, 3)
        self.assertTrue(quality.refine_winner)
        self.assertTrue(quality.upscale_winner)
        self.assertEqual(quality.steps, fast.steps)
        self.assertEqual(quality.final_dimensions_for("inhabitant").width, 1024)
        self.assertEqual(quality.candidate_dimensions_for("planet").width, 1152)
        self.assertEqual(quality.preview_scale, 0.75)

    def test_candidate_plans_are_repeatable_and_unique(self) -> None:
        first = build_candidate_plans(42, quality="quality", kind="planet", world_id="world-a")
        second = build_candidate_plans(42, quality="quality", kind="planet", world_id="world-a")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertEqual(len({item.seed for item in first}), 3)
        self.assertEqual(first[0].width, 1152)
        self.assertTrue(all(item.width % 32 == item.height % 32 == 0 for item in first))
        self.assertNotEqual(
            derive_candidate_seed(42, 0, world_id="world-a", kind="planet"),
            derive_candidate_seed(42, 0, world_id="world-a", kind="surface"),
        )

    def test_prompt_composition_keeps_bible_before_corrections(self) -> None:
        bible = build_world_bible(_distinctive_spec())
        prompt = compose_pipeline_prompt(
            "A ground-level expedition photograph.",
            bible,
            kind="surface",
            refinement_prompt="Make the crystal strata sharper.",
        )
        self.assertLess(prompt.index("LOCKED WORLD VISUAL BIBLE"), prompt.index("SHOT-SPECIFIC BRIEF"))
        self.assertLess(prompt.index("SHOT-SPECIFIC BRIEF"), prompt.index("VERIFIER-GUIDED CORRECTIONS"))
        self.assertIn("preserve composition and world identity", prompt)


class CandidateSelectionTests(unittest.TestCase):
    def test_highest_score_wins_and_ties_are_stable(self) -> None:
        candidates = (
            ScoredCandidate(2, Path("c.png"), 3, 91, False),
            ScoredCandidate(1, Path("b.png"), 2, 91, True),
            ScoredCandidate(0, Path("a.png"), 1, 89, True),
        )
        self.assertEqual(select_winner(candidates).index, 1)

        tied = (
            ScoredCandidate(2, Path("c.png"), 3, 80, True),
            ScoredCandidate(0, Path("a.png"), 1, 80, True),
        )
        self.assertEqual(select_winner(tied).index, 0)


class UpscalerCommandTests(unittest.TestCase):
    def test_builds_argv_without_shell_parsing(self) -> None:
        command = build_upscaler_command(
            "/tmp/input image.png",
            "/tmp/output image.png",
            executable="realesrgan-ncnn-vulkan",
            scale=4,
            model="realesrgan-x4plus",
        )
        self.assertEqual(
            command,
            (
                "realesrgan-ncnn-vulkan",
                "-i",
                "/tmp/input image.png",
                "-o",
                "/tmp/output image.png",
                "-s",
                "4",
                "-n",
                "realesrgan-x4plus",
            ),
        )

    def test_disables_unconfigured_upscaler_and_rejects_shell_syntax(self) -> None:
        self.assertIsNone(build_upscaler_command("in.png", "out.png", executable=""))
        with self.assertRaises(ValueError):
            build_upscaler_command("in.png", "out.png", executable="tool; rm", scale=2)
        with self.assertRaises(ValueError):
            build_upscaler_command("in.png", "out.png", executable="tool", model="--help")
        with self.assertRaises(ValueError):
            build_upscaler_command("", "out.png", executable="tool")


if __name__ == "__main__":
    unittest.main()
