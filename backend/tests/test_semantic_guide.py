from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.schema import PlanetSpec
from app.semantic_guide import SEMANTIC_CLASS_LEGEND, create_semantic_guide


def _read_ppm(path: Path) -> tuple[int, int, bytes]:
    raw = path.read_bytes()
    magic, dimensions, maximum, pixels = raw.split(b"\n", 3)
    if magic != b"P6" or maximum != b"255":
        raise AssertionError("invalid test PPM")
    width, height = (int(value) for value in dimensions.split())
    return width, height, pixels


class SemanticGuideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_guide_is_deterministic_and_returns_independent_legend(self) -> None:
        spec = PlanetSpec()
        first = Path(self.temp.name) / "first.ppm"
        second = Path(self.temp.name) / "second.ppm"
        result = create_semantic_guide(spec, first, seed=73, width=96, height=64)
        create_semantic_guide(spec, second, seed=73, width=96, height=64)

        self.assertEqual(result.path, first)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(result.legend, SEMANTIC_CLASS_LEGEND)
        self.assertIsNot(result.legend, SEMANTIC_CLASS_LEGEND)
        width, height, pixels = _read_ppm(first)
        self.assertEqual((width, height), (96, 64))
        self.assertEqual(len(pixels), width * height * 3)

    def test_requested_semantic_classes_are_present_as_exact_rgb_values(self) -> None:
        spec = PlanetSpec()
        spec.planet.name = "의미 지도 테스트"
        spec.surface.ocean_coverage = 0.42
        spec.surface.ice_coverage = 0.18
        spec.surface.vegetation_coverage = 0.55
        spec.surface.feature_type = "crystalline"
        spec.surface.material_type = "crystal"
        spec.surface.feature_scale = 0.8
        spec.surface.city_lights = 0.7
        spec.surface.landmarks = [
            "cave_openings",
            "crystal_fields",
            "artificial_structures",
        ]
        spec.climate.phenomena = ["고체 광물성 강수"]
        spec.clouds.coverage = 0.65
        spec.clouds.storminess = 0.8
        path = Path(self.temp.name) / "all-classes.ppm"

        create_semantic_guide(spec, path, seed=91, width=192, height=128)
        _, _, pixels = _read_ppm(path)
        colors = {tuple(pixels[index : index + 3]) for index in range(0, len(pixels), 3)}

        expected = {
            "ocean",
            "land_low",
            "land_mid",
            "land_high",
            "cave",
            "crystal_field",
            "artificial_structure",
            "ice",
            "vegetation",
            "weather",
        }
        for class_name in expected:
            self.assertIn(
                SEMANTIC_CLASS_LEGEND[class_name],
                colors,
                f"missing class {class_name}",
            )
        self.assertEqual(len(set(SEMANTIC_CLASS_LEGEND.values())), len(SEMANTIC_CLASS_LEGEND))

    def test_unrequested_signature_features_are_not_invented(self) -> None:
        spec = PlanetSpec()
        spec.surface.feature_type = "continents"
        spec.surface.material_type = "rock"
        spec.surface.city_lights = 0
        spec.surface.landmarks = []
        spec.climate.phenomena = []
        spec.clouds.storminess = 0.1
        path = Path(self.temp.name) / "plain.ppm"

        create_semantic_guide(spec, path, seed=2, width=128, height=96)
        _, _, pixels = _read_ppm(path)
        colors = {tuple(pixels[index : index + 3]) for index in range(0, len(pixels), 3)}

        for class_name in ("cave", "crystal_field", "artificial_structure", "weather"):
            self.assertNotIn(SEMANTIC_CLASS_LEGEND[class_name], colors)

    def test_invalid_dimensions_are_rejected_before_writing(self) -> None:
        path = Path(self.temp.name) / "invalid.ppm"
        with self.assertRaises(ValueError):
            create_semantic_guide(PlanetSpec(), path, seed=1, width=16, height=64)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
