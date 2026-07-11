from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.derive import derive_physics
from app.images import (
    build_inhabitant_prompt,
    build_negative_prompt,
    build_planet_prompt,
    build_surface_prompt,
)
from app.main import app
from app.image_jobs import ImageJob
from app.planet_guide import create_planet_guide
from app import repository
from app.schema import Inhabitant, PlanetSpec


class PhysicsTests(unittest.TestCase):
    def test_earth_like_defaults_are_close_to_earth(self) -> None:
        physics = derive_physics(PlanetSpec())
        self.assertAlmostEqual(physics["mass_earths"], 1.0, delta=0.01)
        self.assertAlmostEqual(physics["surface_gravity_ms2"], 9.81, delta=0.01)
        self.assertGreater(physics["synchronous_orbit_altitude_km"], 30_000)

    def test_fast_rotation_reduces_equatorial_gravity(self) -> None:
        spec = PlanetSpec()
        spec.planet.rotation_hours = 6
        physics = derive_physics(spec)
        self.assertLess(physics["effective_equator_gravity_g"], spec.planet.gravity_g)
        self.assertGreater(physics["centrifugal_acceleration_ms2"], 0)


class PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = PlanetSpec()
        self.spec.planet.name = "칼리페른"
        self.spec.star.count = 2
        self.spec.star.colors_hex = ["#aa3311", "#eeeeff"]
        self.spec.surface.feature_type = "crystalline"
        self.spec.surface.feature_scale = 0.8
        self.spec.surface.material_type = "crystal"
        self.spec.surface.landmarks = ["crystal_fields"]
        self.spec.surface.visual_prompt = "Vast violet crystal continents under amber storms."
        self.spec.inhabitants = [
            Inhabitant(
                name="케른",
                appearance="잿빛 청색 피부와 가로로 긴 동공",
                gravity_adaptation="굵은 다리와 고밀도 골격",
            )
        ]

    def test_planet_prompt_contains_visual_constraints(self) -> None:
        prompt = build_planet_prompt(self.spec)
        self.assertIn("칼리페른", prompt)
        self.assertIn("#aa3311", prompt)
        self.assertIn("crystalline ridges", prompt)
        self.assertIn("Vast violet crystal continents", prompt)
        self.assertIn("reflective crystal fields", prompt)
        self.assertNotIn("Earth", prompt)
        self.assertIn("recognizable Earth continents", build_negative_prompt("planet", self.spec))

    def test_inhabitant_prompt_contains_environment_and_gravity(self) -> None:
        prompt = build_inhabitant_prompt(self.spec, self.spec.inhabitants[0])
        self.assertIn("케른", prompt)
        self.assertIn("1.00 g", prompt)
        self.assertIn("굵은 다리", prompt)

    def test_surface_prompt_emphasizes_scale_and_material(self) -> None:
        prompt = build_surface_prompt(self.spec)
        self.assertIn("foreground, midground and distant horizon", prompt)
        self.assertIn("Surface material: crystal", prompt)
        self.assertIn("reflective crystal fields", prompt)

    def test_inhabitant_prompt_locks_counted_traits(self) -> None:
        inhabitant = self.spec.inhabitants[0]
        inhabitant.appearance = "반투명 피부와 분홍 더듬이"
        inhabitant.portrait_prompt = "A translucent alien with pink antennae."
        prompt = build_inhabitant_prompt(self.spec, inhabitant)
        self.assertIn("Exactly two clearly separated", prompt)
        self.assertIn("both fully visible", prompt)


class PlanetGuideTests(unittest.TestCase):
    def test_guide_is_deterministic_and_uses_ppm_format(self) -> None:
        spec = PlanetSpec()
        spec.planet.name = "비지구형 테스트"
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.ppm"
            second = Path(directory) / "second.ppm"
            create_planet_guide(spec, first, seed=42, width=192, height=128)
            create_planet_guide(spec, second, seed=42, width=192, height=128)
            self.assertTrue(first.read_bytes().startswith(b"P6\n192 128\n255\n"))
            self.assertEqual(first.read_bytes(), second.read_bytes())


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_image_status_is_safe_without_generator(self) -> None:
        response = self.client.get("/api/image/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("available", response.json())

    def test_invalid_inhabitant_index_is_rejected_before_generation(self) -> None:
        response = self.client.post(
            "/api/image/generate",
            json={"spec": PlanetSpec().model_dump(), "kind": "inhabitant", "inhabitant_index": 0},
        )
        self.assertEqual(response.status_code, 400)

    def test_image_generation_returns_job_without_waiting(self) -> None:
        job = ImageJob(
            id="test-job",
            status="queued",
            created_at=1,
            updated_at=1,
            kind="planet",
            seed=42,
        )
        with patch("app.main.image_jobs.create", new=AsyncMock(return_value=job)):
            response = self.client.post(
                "/api/image/generate",
                json={"spec": PlanetSpec().model_dump(), "kind": "planet", "seed": 42},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["id"], "test-job")
        self.assertEqual(response.json()["status"], "queued")


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = repository.DB_PATH
        repository.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        repository.initialize()

    def tearDown(self) -> None:
        repository.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_save_list_share_and_authorized_cover_update(self) -> None:
        spec = PlanetSpec()
        spec.planet.name = "공유 행성"
        saved = repository.save_planet(
            spec=spec,
            physics={"mass_earths": 1.0},
            model="test-model",
            cover_image_url=None,
            is_public=True,
            image_assets={
                "inhabitant:0": {
                    "url": "/generated/inhabitant.png",
                    "seed": 17,
                    "provider": "mflux",
                    "model": "test-image-model",
                }
            },
        )
        self.assertEqual(repository.list_public_planets()[0]["name"], "공유 행성")
        self.assertEqual(repository.get_planet(saved["id"])["spec"]["planet"]["name"], "공유 행성")
        self.assertEqual(
            repository.get_planet(saved["id"])["image_assets"]["inhabitant:0"]["seed"],
            17,
        )

        with self.assertRaises(PermissionError):
            repository.update_cover(saved["id"], "/generated/wrong.png", "wrong-token")
        updated = repository.update_cover(
            saved["id"],
            "/generated/cover.png",
            saved["edit_token"],
        )
        self.assertEqual(updated["cover_image_url"], "/generated/cover.png")

        updated = repository.update_image_asset(
            saved["id"],
            "inhabitant:0",
            {
                "url": "/generated/new-inhabitant.png",
                "seed": 23,
                "provider": "mflux",
                "model": "test-image-model",
            },
            saved["edit_token"],
        )
        self.assertEqual(updated["image_assets"]["inhabitant:0"]["seed"], 23)


if __name__ == "__main__":
    unittest.main()
