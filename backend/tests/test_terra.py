from __future__ import annotations

import json
import unittest
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

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
from app.schema import Inference, Inhabitant, PlanetSpec


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


class SchemaBoundaryTests(unittest.TestCase):
    def test_invalid_color_and_oversized_collections_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            PlanetSpec.model_validate({"atmosphere": {"color_hex": "blue"}})
        with self.assertRaises(ValidationError):
            PlanetSpec.model_validate(
                {"climate": {"phenomena": [f"storm-{index}" for index in range(17)]}}
            )


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

    def test_liveness_and_readiness_have_distinct_dependency_semantics(self) -> None:
        self.assertEqual(self.client.get("/api/livez").status_code, 200)
        with (
            patch.dict(os.environ, {"GEMINI_API_KEYS": "test-key"}),
            patch("app.main.healthcheck_database", return_value=True),
            patch(
                "app.main.shutil.disk_usage",
                return_value=SimpleNamespace(total=10_000, used=1_000, free=9_000 * 1024 * 1024),
            ),
        ):
            response = self.client.get("/api/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

        with (
            patch.dict(os.environ, {"GEMINI_API_KEYS": "", "GEMINI_API_KEY": ""}),
            patch("app.main.healthcheck_database", return_value=True),
            patch(
                "app.main.shutil.disk_usage",
                return_value=SimpleNamespace(total=10_000, used=1_000, free=9_000 * 1024 * 1024),
            ),
        ):
            response = self.client.get("/api/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["analysis_provider"])

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

    def test_image_job_can_be_cancelled_by_capability_id(self) -> None:
        job = ImageJob(
            id="cancel-job",
            status="failed",
            created_at=1,
            updated_at=2,
            kind="surface",
            seed=42,
            error="이미지 생성 작업이 취소되었습니다.",
        )
        with patch("app.main.image_jobs.cancel", new=AsyncMock(return_value=job)):
            response = self.client.delete("/api/image/jobs/cancel-job")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertIn("취소", response.json()["error"])

    def test_save_uses_server_derived_physics_and_model(self) -> None:
        spec = PlanetSpec()
        with patch(
            "app.main.save_planet",
            return_value={"id": "saved", "spec": spec.model_dump(), "edit_token": "token"},
        ) as save:
            response = self.client.post(
                "/api/planets",
                json={
                    "spec": spec.model_dump(),
                    "physics": {"mass_earths": 999999},
                    "model": "untrusted-client-model",
                    "public": True,
                },
            )
        self.assertEqual(response.status_code, 201)
        kwargs = save.call_args.kwargs
        self.assertAlmostEqual(kwargs["physics"]["mass_earths"], 1.0, delta=0.01)
        self.assertNotEqual(kwargs["model"], "untrusted-client-model")

    def test_public_save_strips_evidence_quotes_without_changing_other_inference_fields(self) -> None:
        spec = PlanetSpec()
        spec.inferences = [
            Inference(
                topic="대기",
                claim="대기가 짙다",
                evidence_quote="출판 전 원문 문장",
                reasoning="빛의 산란 묘사에 근거",
            )
        ]
        with patch(
            "app.main.save_planet",
            return_value={"id": "saved", "spec": spec.model_dump(), "edit_token": "token"},
        ) as save:
            response = self.client.post(
                "/api/planets",
                json={
                    "spec": spec.model_dump(),
                    "physics": {},
                    "model": "client",
                    "public": True,
                },
            )

        self.assertEqual(response.status_code, 201)
        public_inference = save.call_args.kwargs["spec"].inferences[0]
        self.assertEqual(public_inference.evidence_quote, "")
        self.assertEqual(public_inference.claim, "대기가 짙다")
        self.assertEqual(public_inference.reasoning, "빛의 산란 묘사에 근거")
        self.assertEqual(spec.inferences[0].evidence_quote, "출판 전 원문 문장")

    def test_private_save_and_nested_generated_path_are_rejected(self) -> None:
        spec = PlanetSpec().model_dump()
        private = self.client.post(
            "/api/planets",
            json={"spec": spec, "physics": {}, "model": "client", "public": False},
        )
        self.assertEqual(private.status_code, 422)
        nested = self.client.post(
            "/api/planets",
            json={
                "spec": spec,
                "physics": {},
                "model": "client",
                "cover_image_url": "/generated/nested/image.png",
                "public": True,
            },
        )
        self.assertEqual(nested.status_code, 422)

    def test_non_public_saved_planet_is_not_shared(self) -> None:
        with patch("app.main.get_planet", return_value=None) as get:
            response = self.client.get("/api/planets/private-id")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(get.call_args.kwargs["public_only"])

    def test_report_intake_is_bounded_and_returns_no_internal_id(self) -> None:
        with (
            patch("app.main.report_limiter.check", new=AsyncMock()),
            patch("app.main.report_global_limiter.check", new=AsyncMock()),
            patch("app.main.create_moderation_report", return_value=True) as create_report,
        ):
            response = self.client.post(
                "/api/planets/public-id/reports",
                json={"reason": "copyright", "details": " 권리자 확인 요청 "},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "received"})
        create_report.assert_called_once_with("public-id", "copyright", "권리자 확인 요청")
        invalid = self.client.post(
            "/api/planets/public-id/reports",
            json={"reason": "free-form-unbounded", "details": "x"},
        )
        self.assertEqual(invalid.status_code, 422)
        oversized = self.client.post(
            "/api/planets/public-id/reports",
            json={"reason": "spam", "details": "x" * 501},
        )
        self.assertEqual(oversized.status_code, 422)

    def test_delete_requires_capability_and_returns_an_empty_response(self) -> None:
        token = "valid-edit-capability-token"
        with (
            patch("app.main.save_limiter.check", new=AsyncMock()),
            patch("app.main.delete_planet", return_value=True) as delete,
        ):
            response = self.client.request(
                "DELETE",
                "/api/planets/saved-id",
                json={"edit_token": token},
            )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        delete.assert_called_once_with("saved-id", token)


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
        spec.inferences = [
            Inference(topic="근거", claim="공개 주장", evidence_quote="비공개 원문")
        ]
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
        public_detail = repository.get_planet(saved["id"])
        self.assertEqual(public_detail["spec"]["planet"]["name"], "공유 행성")
        self.assertNotIn("edit_token", public_detail)
        self.assertEqual(saved["spec"]["inferences"][0]["evidence_quote"], "")
        self.assertEqual(spec.inferences[0].evidence_quote, "비공개 원문")
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

    def test_report_and_capability_delete_are_transactional_without_sync_file_removal(self) -> None:
        generated = Path(self.temp.name) / "preserved.png"
        generated.write_bytes(b"generated-asset")
        saved = repository.save_planet(
            spec=PlanetSpec(),
            physics={"mass_earths": 1.0},
            model="test-model",
            cover_image_url="/generated/preserved.png",
            is_public=True,
        )
        self.assertTrue(
            repository.create_moderation_report(saved["id"], "spam", "중복 저장")
        )

        with self.assertRaises(PermissionError):
            repository.delete_planet(saved["id"], "wrong-token")
        self.assertIsNotNone(repository.get_planet(saved["id"]))
        with repository._db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM moderation_reports WHERE planet_id = ?",
                    (saved["id"],),
                ).fetchone()[0],
                1,
            )

        self.assertTrue(repository.delete_planet(saved["id"], saved["edit_token"]))
        self.assertIsNone(repository.get_planet(saved["id"]))
        with repository._db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM moderation_reports WHERE planet_id = ?",
                    (saved["id"],),
                ).fetchone()[0],
                0,
            )
        self.assertTrue(generated.exists())

    def test_reports_accept_only_public_planets_and_store_no_requester_identity(self) -> None:
        saved = repository.save_planet(
            spec=PlanetSpec(),
            physics={"mass_earths": 1.0},
            model="test-model",
            cover_image_url=None,
            is_public=False,
        )
        self.assertFalse(repository.create_moderation_report(saved["id"], "spam"))
        with repository._db() as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(moderation_reports)")
            }
        self.assertEqual(
            columns,
            {"id", "planet_id", "reason", "details", "created_at"},
        )

    def test_report_table_migration_preserves_existing_planet_rows(self) -> None:
        with repository._db(write=True) as db:
            db.execute("DROP TABLE moderation_reports")
        saved = repository.save_planet(
            spec=PlanetSpec(),
            physics={"mass_earths": 1.0},
            model="legacy-model",
            cover_image_url="/generated/legacy.png",
            is_public=True,
        )

        repository.initialize()

        migrated = repository.get_planet(saved["id"])
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated["model"], "legacy-model")
        self.assertEqual(migrated["cover_image_url"], "/generated/legacy.png")
        self.assertTrue(repository.create_moderation_report(saved["id"], "copyright"))

    def test_migration_redacts_legacy_public_quotes_without_touching_private_data(self) -> None:
        public = repository.save_planet(
            spec=PlanetSpec(),
            physics={"mass_earths": 1.0},
            model="legacy-public-model",
            cover_image_url="/generated/legacy-public.png",
            is_public=True,
        )
        private = repository.save_planet(
            spec=PlanetSpec(),
            physics={"mass_earths": 1.0},
            model="legacy-private-model",
            cover_image_url=None,
            is_public=False,
        )
        with repository._db(write=True) as db:
            for planet_id in (public["id"], private["id"]):
                raw = json.loads(
                    db.execute(
                        "SELECT spec_json FROM planets WHERE id = ?", (planet_id,)
                    ).fetchone()[0]
                )
                raw["inferences"] = [
                    {
                        "topic": "legacy",
                        "claim": "preserved claim",
                        "confidence": "stated",
                        "evidence_quote": "direct source quotation",
                        "reasoning": "preserved reasoning",
                    }
                ]
                db.execute(
                    "UPDATE planets SET spec_json = ? WHERE id = ?",
                    (json.dumps(raw), planet_id),
                )
            db.execute(
                "DELETE FROM schema_migrations WHERE name = ?",
                (repository.PUBLIC_EVIDENCE_REDACTION_MIGRATION,),
            )

        repository.initialize()

        migrated_public = repository.get_planet(public["id"])
        migrated_private = repository.get_planet(private["id"])
        self.assertEqual(
            migrated_public["spec"]["inferences"][0],
            {
                "topic": "legacy",
                "claim": "preserved claim",
                "confidence": "stated",
                "evidence_quote": "",
                "reasoning": "preserved reasoning",
            },
        )
        self.assertEqual(
            migrated_private["spec"]["inferences"][0]["evidence_quote"],
            "direct source quotation",
        )
        self.assertEqual(migrated_public["model"], "legacy-public-model")
        self.assertEqual(migrated_public["cover_image_url"], "/generated/legacy-public.png")


if __name__ == "__main__":
    unittest.main()
