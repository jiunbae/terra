from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.image_jobs import ImageJobManager
from app.image_verifier import CriterionScores, ImageVerificationResult
from app.images import ImageGenerationError
from app.observability import TerraMetrics
from app.schema import Inhabitant, PlanetSpec


def _verification(score: int, *, passed: bool = True) -> ImageVerificationResult:
    return ImageVerificationResult(
        total_score=score,
        criterion_scores=CriterionScores(
            specification_fidelity=score,
            distinctive_features=score,
            environment_fidelity=score,
            material_detail=score,
            technical_quality=score,
        ),
        passed=passed,
        missing_features=[] if passed else ["signature feature missing"],
        problems=[],
        refinement_prompt="Strengthen the signature feature while preserving composition.",
    )


class ImageJobPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.generated = Path(self.temp.name)
        self.spec = PlanetSpec()
        self.spec.planet.name = "파이프라인 테스트"

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def _wait(self, manager: ImageJobManager) -> None:
        while manager._tasks:  # noqa: SLF001 - explicit unit-test synchronization
            await __import__("asyncio").gather(*tuple(manager._tasks))

    def _batch(self) -> AsyncMock:
        async def generate(*_: object, **kwargs: object) -> list[tuple[str, int]]:
            seeds = kwargs["seeds"]
            assert isinstance(seeds, list)
            results: list[tuple[str, int]] = []
            for seed in seeds:
                name = f"candidate-{seed}.png"
                (self.generated / name).write_bytes(b"png")
                results.append((f"/generated/{name}", seed))
            return results

        return AsyncMock(side_effect=generate)

    async def test_fast_mode_skips_verifier(self) -> None:
        manager = ImageJobManager()
        batch = self._batch()
        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", batch),
            patch("app.image_jobs.verify_generated_image", new=AsyncMock()) as verifier,
        ):
            job = await manager.create(
                prompt="test",
                negative_prompt="",
                spec=self.spec,
                kind="planet",
                seed=10,
                quality="fast",
            )
            await self._wait(manager)

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.candidate_total, 1)
        self.assertIsNone(job.quality_score)
        verifier.assert_not_awaited()

    async def test_balanced_mode_selects_highest_verified_candidate(self) -> None:
        manager = ImageJobManager()
        batch = self._batch()
        verifier = AsyncMock(side_effect=[_verification(61, passed=False), _verification(88)])
        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", batch),
            patch("app.image_jobs.verify_generated_image", new=verifier),
        ):
            job = await manager.create(
                prompt="test",
                negative_prompt="",
                spec=self.spec,
                kind="surface",
                seed=20,
                quality="balanced",
            )
            await self._wait(manager)

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.candidate_total, 2)
        self.assertEqual(job.quality_score, 88)
        self.assertEqual(job.actual_seed, batch.await_args.kwargs["seeds"][1])
        self.assertTrue((self.generated / Path(job.url or "").name).is_file())
        self.assertEqual(len(list(self.generated.glob("*.png"))), 1)

    async def test_balanced_mode_survives_verifier_transport_failure(self) -> None:
        manager = ImageJobManager()
        batch = self._batch()
        verifier = AsyncMock(side_effect=RuntimeError("vision unavailable"))
        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", batch),
            patch("app.image_jobs.verify_generated_image", new=verifier),
        ):
            job = await manager.create(
                prompt="test",
                negative_prompt="",
                spec=self.spec,
                kind="surface",
                seed=21,
                quality="balanced",
            )
            await self._wait(manager)

        self.assertEqual(job.status, "completed")
        self.assertIsNone(job.quality_score)
        self.assertTrue(any("검수를 사용할 수 없어" in note for note in job.verification_notes))
        self.assertEqual(len(list(self.generated.glob("*.png"))), 1)

    async def test_quality_mode_refines_winner_at_final_dimensions(self) -> None:
        manager = ImageJobManager()
        self.spec.inhabitants = [Inhabitant(name="테스트 거주민")]
        batch = self._batch()
        verifier = AsyncMock(
            side_effect=[_verification(70), _verification(91), _verification(80), _verification(94)]
        )
        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", batch),
            patch("app.image_jobs.verify_generated_image", new=verifier),
            patch("app.image_jobs.build_upscaler_command", return_value=None),
        ):
            job = await manager.create(
                prompt="test",
                negative_prompt="",
                spec=self.spec,
                kind="inhabitant",
                seed=30,
                quality="quality",
                inhabitant_index=0,
            )
            await self._wait(manager)

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.candidate_total, 3)
        self.assertEqual(job.quality_score, 94)
        self.assertEqual(batch.await_count, 2)
        refinement = batch.await_args_list[1].kwargs
        self.assertEqual((refinement["width"], refinement["height"]), (1024, 1344))
        self.assertIsNotNone(refinement["init_image_path"])
        self.assertEqual(len(list(self.generated.glob("*.png"))), 1)

    async def test_quality_mode_keeps_verified_winner_when_refinement_regresses(self) -> None:
        manager = ImageJobManager()
        batch = self._batch()
        verifier = AsyncMock(
            side_effect=[_verification(70), _verification(91), _verification(80), _verification(60)]
        )
        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", batch),
            patch("app.image_jobs.verify_generated_image", new=verifier),
            patch("app.image_jobs.build_upscaler_command", return_value=None),
        ):
            job = await manager.create(
                prompt="test",
                negative_prompt="",
                spec=self.spec,
                kind="surface",
                seed=31,
                quality="quality",
            )
            await self._wait(manager)

        candidate_seeds = batch.await_args_list[0].kwargs["seeds"]
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.quality_score, 91)
        self.assertEqual(job.actual_seed, candidate_seeds[1])
        self.assertTrue(any("원본 후보" in note for note in job.verification_notes))
        self.assertEqual(len(list(self.generated.glob("*.png"))), 1)

    async def test_quality_mode_survives_optional_refinement_failure(self) -> None:
        telemetry = TerraMetrics()
        manager = ImageJobManager(telemetry=telemetry)
        first_batch = self._batch()
        calls = 0

        async def generate(*args: object, **kwargs: object) -> list[tuple[str, int]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return await first_batch(*args, **kwargs)
            raise ImageGenerationError("refine failed")

        batch = AsyncMock(side_effect=generate)
        verifier = AsyncMock(side_effect=[_verification(70), _verification(91), _verification(80)])
        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", batch),
            patch("app.image_jobs.verify_generated_image", new=verifier),
            patch("app.image_jobs.build_upscaler_command", return_value=None),
        ):
            job = await manager.create(
                prompt="test",
                negative_prompt="",
                spec=self.spec,
                kind="surface",
                seed=32,
                quality="quality",
            )
            await self._wait(manager)

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.quality_score, 91)
        self.assertTrue(any("보정에 실패" in note for note in job.verification_notes))
        rendered = telemetry.prometheus_text()
        self.assertIn('phase="refining",outcome="failed"} 1', rendered)
        self.assertNotIn('phase="refining",outcome="succeeded"} 1', rendered)

    async def test_quality_mode_records_optional_upscaler_failure_without_failing_job(self) -> None:
        telemetry = TerraMetrics()
        manager = ImageJobManager(telemetry=telemetry)
        batch = self._batch()
        verifier = AsyncMock(
            side_effect=[_verification(70), _verification(91), _verification(80), _verification(94)]
        )

        class FailedUpscaler:
            returncode = 1

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b"upscaler failed"

        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", batch),
            patch("app.image_jobs.verify_generated_image", new=verifier),
            patch("app.image_jobs.build_upscaler_command", return_value=["fake-upscaler"]),
            patch(
                "app.image_jobs.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=FailedUpscaler()),
            ),
        ):
            job = await manager.create(
                prompt="test",
                negative_prompt="",
                spec=self.spec,
                kind="surface",
                seed=33,
                quality="quality",
            )
            await self._wait(manager)

        self.assertEqual(job.status, "completed")
        self.assertTrue(any("업스케일에 실패" in note for note in job.verification_notes))
        rendered = telemetry.prometheus_text()
        self.assertIn('phase="upscaling",outcome="failed"} 1', rendered)
        self.assertNotIn('phase="upscaling",outcome="succeeded"} 1', rendered)


if __name__ == "__main__":
    unittest.main()
