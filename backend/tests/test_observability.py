from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.image_jobs import ImageJobManager, ImageQueueFull, ImageStorageFull
from app.maintenance import cleanup_generated_images
from app.observability import (
    RequestObservabilityMiddleware,
    TerraMetrics,
    correlation_fields,
    job_correlation,
    job_correlation_id,
    observe_readiness,
    prometheus_text,
)
from app.schema import PlanetSpec


class RequestObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = TerraMetrics(max_routes=4, max_request_series=32)
        app = FastAPI()

        @app.get("/items/{item_id}")
        async def item(item_id: str, request: Request) -> dict[str, str]:
            return {
                "item": item_id,
                "state_request_id": request.state.request_id,
                "context_request_id": correlation_fields()["request_id"],
            }

        @app.get("/broken")
        async def broken() -> None:
            raise RuntimeError("expected test failure")

        app.add_middleware(
            RequestObservabilityMiddleware,
            metrics_registry=self.registry,
            request_id_factory=lambda: "req_" + "a" * 24,
        )
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_server_request_id_and_route_template_do_not_retain_untrusted_data(self) -> None:
        secret_header = "edit-capability-must-not-be-trusted"
        response = self.client.get(
            "/items/private-story-title?prompt=secret-story-text",
            headers={"X-Request-ID": secret_header},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "req_" + "a" * 24)
        self.assertEqual(response.json()["state_request_id"], "req_" + "a" * 24)
        self.assertEqual(response.json()["context_request_id"], "req_" + "a" * 24)

        rendered = self.registry.prometheus_text()
        self.assertIn('route="/items/{item_id}"', rendered)
        self.assertIn('status="200"', rendered)
        self.assertNotIn("private-story-title", rendered)
        self.assertNotIn("secret-story-text", rendered)
        self.assertNotIn(secret_header, rendered)

    def test_unhandled_exception_is_counted_as_500_and_context_is_reset(self) -> None:
        response = self.client.get("/broken")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["X-Request-ID"], "req_" + "a" * 24)
        self.assertEqual(response.json(), {"detail": "서버 내부 오류가 발생했습니다."})
        self.assertIn(
            'terra_http_requests_total{method="GET",route="/broken",status="500"} 1',
            self.registry.prometheus_text(),
        )
        self.assertEqual(correlation_fields()["request_id"], "-")


class BoundedMetricsTests(unittest.TestCase):
    def test_request_series_and_routes_remain_bounded(self) -> None:
        registry = TerraMetrics(max_routes=2, max_request_series=16)
        for index in range(1000):
            registry.record_request(
                method=("GET", "POST", "PATCH", "TRACE")[index % 4],
                route=f"/configured/{index}",
                status_code=100 + (index % 500),
                duration_seconds=index / 1000,
            )

        self.assertLessEqual(len(registry._known_routes), 2)  # noqa: SLF001
        self.assertLessEqual(len(registry._request_counts), 17)  # noqa: SLF001
        self.assertIn('route="__overflow__"', registry.prometheus_text())

    def test_cleanup_readiness_and_storage_signals_drop_sensitive_details(self) -> None:
        registry = TerraMetrics()
        secret = "private/path/edit-token/GEMINI_API_KEY"
        registry.record_cleanup(
            SimpleNamespace(
                aborted=False,
                errors=(secret,),
                limits_satisfied=False,
                elapsed_seconds=0.75,
                reclaimed_bytes=4096,
                store_bytes_after=8192,
                estimated_free_bytes_after=16384,
            )
        )
        observe_readiness(
            {
                "database": True,
                "storage": False,
                secret: False,
            },
            free_disk_bytes=16384,
            registry=registry,
        )

        rendered = prometheus_text(registry)
        self.assertIn('terra_generated_cleanup_runs_total{outcome="degraded"} 1', rendered)
        self.assertIn('terra_readiness_check{check="database"} 1', rendered)
        self.assertIn('terra_readiness_check{check="storage"} 0', rendered)
        self.assertIn("terra_storage_free_bytes 16384", rendered)
        self.assertNotIn(secret, rendered)

    def test_job_correlation_is_safe_and_process_local(self) -> None:
        capability = "job-cancel-capability-that-must-not-be-logged"
        safe_value = job_correlation_id(capability)

        self.assertRegex(safe_value, r"^job_[a-f0-9]{20}$")
        self.assertNotIn(capability, safe_value)
        with job_correlation(capability):
            self.assertEqual(correlation_fields()["job_correlation_id"], safe_value)
        self.assertEqual(correlation_fields()["job_correlation_id"], "-")

    def test_cleanup_function_publishes_health_without_a_main_hook(self) -> None:
        registry = TerraMetrics()
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "not-created"
            with patch("app.maintenance.metrics", registry):
                result = cleanup_generated_images(generated_dir=missing)

        self.assertTrue(result.limits_satisfied)
        self.assertIn(
            'terra_generated_cleanup_runs_total{outcome="success"} 1',
            registry.prometheus_text(),
        )


class ImageJobObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.generated = Path(self.temp.name)
        self.registry = TerraMetrics()

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def _wait(self, manager: ImageJobManager) -> None:
        while manager._tasks:  # noqa: SLF001 - task completion is the tested lifecycle
            await asyncio.gather(*tuple(manager._tasks))

    async def test_completed_job_updates_queue_and_phase_outcomes(self) -> None:
        manager = ImageJobManager(telemetry=self.registry)

        async def generate(*_: object, **kwargs: object) -> list[tuple[str, int]]:
            on_started = kwargs.get("on_started")
            if on_started is not None:
                await on_started()
            output = self.generated / "completed.png"
            output.write_bytes(b"png")
            return [("/generated/completed.png", kwargs["seeds"][0])]

        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", side_effect=generate),
            patch.dict(os.environ, {"TERRA_MIN_FREE_DISK_MB": "128"}, clear=False),
        ):
            job = await manager.create(
                prompt="never-record-this-prompt",
                negative_prompt="never-record-this-negative-prompt",
                spec=PlanetSpec(),
                kind="planet",
                seed=7,
                quality="fast",
            )
            await self._wait(manager)

        self.assertEqual(job.status, "completed")
        rendered = self.registry.prometheus_text()
        self.assertIn('kind="planet",quality="fast",outcome="accepted"} 1', rendered)
        self.assertIn('kind="planet",quality="fast",outcome="completed"} 1', rendered)
        self.assertIn('phase="queued",outcome="succeeded"} 1', rendered)
        self.assertIn('phase="generating",outcome="succeeded"} 1', rendered)
        self.assertIn("terra_image_queue_active_jobs 0", rendered)
        self.assertNotIn("never-record-this", rendered)

    async def test_cancellation_and_queue_rejection_are_counted_once(self) -> None:
        manager = ImageJobManager(telemetry=self.registry)
        started = asyncio.Event()

        async def blocked(*_: object, **kwargs: object) -> list[tuple[str, int]]:
            on_started = kwargs.get("on_started")
            if on_started is not None:
                await on_started()
            started.set()
            await asyncio.Event().wait()
            return []

        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", new=AsyncMock(side_effect=blocked)),
            patch.dict(
                os.environ,
                {
                    "TERRA_IMAGE_MAX_ACTIVE_JOBS": "1",
                    "TERRA_IMAGE_MAX_WORK_UNITS": "4",
                    "TERRA_MIN_FREE_DISK_MB": "128",
                },
                clear=False,
            ),
        ):
            first = await manager.create(
                prompt="prompt",
                negative_prompt="",
                spec=PlanetSpec(),
                kind="surface",
                seed=1,
                quality="fast",
            )
            await started.wait()
            with self.assertRaises(ImageQueueFull):
                await manager.create(
                    prompt="prompt",
                    negative_prompt="",
                    spec=PlanetSpec(),
                    kind="surface",
                    seed=2,
                    quality="fast",
                )
            await manager.cancel(first.id)

        rendered = self.registry.prometheus_text()
        self.assertIn('outcome="rejected_queue"} 1', rendered)
        self.assertIn('kind="surface",quality="fast",outcome="cancelled"} 1', rendered)
        self.assertIn('phase="generating",outcome="cancelled"} 1', rendered)
        self.assertNotIn('kind="surface",quality="fast",outcome="cancelled"} 2', rendered)

    async def test_storage_rejection_is_counted(self) -> None:
        manager = ImageJobManager(telemetry=self.registry)
        disk_usage = SimpleNamespace(total=10, used=10, free=0)
        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.shutil.disk_usage", return_value=disk_usage),
        ):
            with self.assertRaises(ImageStorageFull):
                await manager.create(
                    prompt="prompt",
                    negative_prompt="",
                    spec=PlanetSpec(),
                    kind="inhabitant",
                    seed=3,
                    quality="quality",
                )

        self.assertIn(
            'kind="inhabitant",quality="quality",outcome="rejected_storage"} 1',
            self.registry.prometheus_text(),
        )


if __name__ == "__main__":
    unittest.main()
