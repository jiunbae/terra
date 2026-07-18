from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app import gemini, repository
from app.image_jobs import ImageJob, ImageJobManager, ImageQueueFull
from app.images import (
    ImageGenerationError,
    ImageProviderStatus,
    generate_candidate_batch,
    image_subprocess_environment,
)
from app.rate_limit import RateLimiter
from app.schema import PlanetSpec


class _FakeGeminiClient:
    responses: list[httpx.Response] = []

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> _FakeGeminiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, *_: object, **__: object) -> httpx.Response:
        return self.responses.pop(0)


class GeminiReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def _success(self) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"answer": 42}'}]}, "finishReason": "STOP"}
                ]
            },
        )

    async def test_transient_errors_rotate_and_backoff_once_per_key_round(self) -> None:
        _FakeGeminiClient.responses = [
            httpx.Response(502, text="bad gateway"),
            httpx.Response(504, text="gateway timeout"),
            self._success(),
        ]
        sleep = AsyncMock()
        with (
            patch.dict("os.environ", {"GEMINI_API_KEYS": "key-a,key-b"}, clear=False),
            patch("app.gemini.httpx.AsyncClient", _FakeGeminiClient),
            patch("app.gemini._next_key", side_effect=["key-a", "key-b", "key-a"]),
            patch("app.gemini.asyncio.sleep", new=sleep),
        ):
            result = await gemini.generate_json("system", "user", {"type": "object"})

        self.assertEqual(result, {"answer": 42})
        sleep.assert_awaited_once()

    async def test_non_object_payload_has_explicit_gemini_error(self) -> None:
        _FakeGeminiClient.responses = [httpx.Response(200, json=[])]
        with (
            patch.dict("os.environ", {"GEMINI_API_KEYS": "key-a"}, clear=False),
            patch("app.gemini.httpx.AsyncClient", _FakeGeminiClient),
            patch("app.gemini._next_key", return_value="key-a"),
        ):
            with self.assertRaisesRegex(gemini.GeminiError, "finishReason=\\?"):
                await gemini.generate_json("system", "user", {"type": "object"})

    def test_duplicate_keys_are_removed(self) -> None:
        with patch.dict("os.environ", {"GEMINI_API_KEYS": "one, two, one"}, clear=False):
            self.assertEqual(gemini._load_keys(), ["one", "two"])

    def test_key_order_is_full_rotation_per_call(self) -> None:
        # 동시 요청이 서로의 draw를 가로채도, 각 호출은 모든 키를 정확히 한 번씩
        # 담은 순열을 받아야 한다(정상 키를 못 써보는 문제 방지). 시작 키는 분산된다.
        with patch.dict("os.environ", {"GEMINI_API_KEYS": "a,b,c"}, clear=False):
            gemini._key_cycle = None  # 이전 테스트의 cycle 상태 초기화
            orders = [gemini._key_order() for _ in range(3)]
        for order in orders:
            self.assertEqual(sorted(order), ["a", "b", "c"])
        # 세 번의 호출이 서로 다른 키에서 시작해 부하를 분산한다.
        self.assertEqual({order[0] for order in orders}, {"a", "b", "c"})


class _TimeoutProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        raise TimeoutError

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int:
        return self.returncode or 0


class _BlockingProcess(_TimeoutProcess):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.terminated = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        await asyncio.Event().wait()
        return b"", b""

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class ImageProcessReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.generated = Path(self.temp.name)
        self.status = ImageProviderStatus(True, "mflux", "model", "fake-mflux", "ready")

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_timeout_kills_process_and_removes_partial_outputs(self) -> None:
        process = _TimeoutProcess()

        async def create_process(*args: object, **_: object) -> _TimeoutProcess:
            output_template = Path(str(args[args.index("--output") + 1]))
            Path(str(output_template).replace("{seed}", "7")).write_bytes(b"partial")
            return process

        with (
            patch("app.images.GENERATED_DIR", self.generated),
            patch("app.images.provider_status", return_value=self.status),
            patch("app.images.asyncio.create_subprocess_exec", side_effect=create_process),
        ):
            with self.assertRaisesRegex(ImageGenerationError, "시간"):
                await generate_candidate_batch("prompt", kind="surface", seeds=[7])

        self.assertTrue(process.killed)
        self.assertEqual(list(self.generated.glob("*.png")), [])

    async def test_manager_cancel_reaps_background_task(self) -> None:
        manager = ImageJobManager()
        started = asyncio.Event()

        async def blocked_batch(*_: object, **__: object) -> list[tuple[str, int]]:
            started.set()
            await asyncio.Event().wait()
            return []

        with patch("app.image_jobs.generate_candidate_batch", side_effect=blocked_batch):
            job = await manager.create(
                prompt="prompt",
                negative_prompt="",
                spec=PlanetSpec(),
                kind="surface",
                seed=3,
                quality="fast",
            )
            await started.wait()
            cancelled = await manager.cancel(job.id)

        self.assertIs(cancelled, job)
        self.assertEqual(job.status, "failed")
        self.assertIn("취소", job.error or "")
        self.assertFalse(manager._tasks)  # noqa: SLF001 - task reaping is the behavior under test

    async def test_generation_cancellation_terminates_child_process(self) -> None:
        process = _BlockingProcess()
        with (
            patch("app.images.GENERATED_DIR", self.generated),
            patch("app.images.provider_status", return_value=self.status),
            patch("app.images.asyncio.create_subprocess_exec", return_value=process),
        ):
            task = asyncio.create_task(
                generate_candidate_batch("prompt", kind="surface", seeds=[9])
            )
            await process.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.terminated)
        self.assertEqual(list(self.generated.glob("*.png")), [])

    def test_image_subprocess_environment_excludes_server_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "HF_HOME": "/tmp/hf",
                "GEMINI_API_KEYS": "secret-gemini",
                "BW_SESSION": "secret-vault",
                "TUNNEL_TOKEN": "secret-tunnel",
            },
            clear=True,
        ):
            child_env = image_subprocess_environment()

        self.assertEqual(child_env["PATH"], "/usr/bin")
        self.assertEqual(child_env["HF_HOME"], "/tmp/hf")
        self.assertNotIn("GEMINI_API_KEYS", child_env)
        self.assertNotIn("BW_SESSION", child_env)
        self.assertNotIn("TUNNEL_TOKEN", child_env)

    async def test_queue_admission_rejects_more_than_configured_capacity(self) -> None:
        manager = ImageJobManager()
        started = asyncio.Event()

        async def blocked_batch(*_: object, **__: object) -> list[tuple[str, int]]:
            started.set()
            await asyncio.Event().wait()
            return []

        with (
            patch("app.image_jobs.GENERATED_DIR", self.generated),
            patch("app.image_jobs.generate_candidate_batch", side_effect=blocked_batch),
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

    async def test_job_journal_survives_restart_and_marks_inflight_failed(self) -> None:
        state_path = self.generated / "image_jobs.json"
        now = time.time()
        manager = ImageJobManager(state_path=state_path)
        manager._jobs = {  # noqa: SLF001 - seed restart state under test
            "completed": ImageJob(
                id="completed",
                status="completed",
                created_at=now,
                updated_at=now,
                kind="planet",
                seed=11,
                url="/generated/completed.png",
                actual_seed=11,
            ),
            "running": ImageJob(
                id="running",
                status="generating",
                created_at=now,
                updated_at=now,
                kind="surface",
                seed=12,
            ),
        }
        manager._persist_state()  # noqa: SLF001

        restored = ImageJobManager(state_path=state_path)
        completed = await restored.get("completed")
        interrupted = await restored.get("running")

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.url, "/generated/completed.png")
        self.assertEqual(interrupted.status, "failed")
        self.assertIn("재시작", interrupted.error or "")
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)


def _request(peer: str, forwarded: str | None = None) -> SimpleNamespace:
    headers = {"cf-connecting-ip": forwarded} if forwarded is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


class RateLimiterReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_spoofed_forwarded_ip_is_ignored_for_untrusted_peer(self) -> None:
        limiter = RateLimiter(
            limit=1,
            window_seconds=60,
            trusted_proxy_ips={"127.0.0.1"},
        )
        await limiter.check(_request("203.0.113.8", "198.51.100.1"))
        with self.assertRaises(HTTPException) as raised:
            await limiter.check(_request("203.0.113.8", "198.51.100.2"))
        self.assertEqual(raised.exception.status_code, 429)

    async def test_trusted_proxy_ip_is_used_and_bucket_count_is_bounded(self) -> None:
        limiter = RateLimiter(
            limit=2,
            window_seconds=60,
            max_buckets=2,
            trusted_proxy_ips={"127.0.0.1"},
        )
        await limiter.check(_request("127.0.0.1", "198.51.100.1"))
        await limiter.check(_request("127.0.0.1", "198.51.100.2"))
        await limiter.check(_request("127.0.0.1", "198.51.100.3"))
        await limiter.check(_request("127.0.0.1", "198.51.100.4"))
        with self.assertRaises(HTTPException):
            await limiter.check(_request("127.0.0.1", "198.51.100.5"))
        self.assertEqual(len(limiter._hits), 2)  # noqa: SLF001 - memory bound under test
        self.assertIn("__overflow__", limiter._hits)  # noqa: SLF001


class RepositoryConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = repository.DB_PATH
        repository.DB_PATH = Path(self.temp.name) / "nested" / "test.sqlite3"
        repository.initialize()

    def tearDown(self) -> None:
        repository.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_concurrent_asset_updates_do_not_lose_each_other(self) -> None:
        saved = repository.save_planet(
            spec=PlanetSpec(),
            physics={},
            model="test",
            cover_image_url=None,
            is_public=True,
        )

        def update(index: int) -> None:
            repository.update_image_asset(
                saved["id"],
                f"inhabitant:{index}",
                {"url": f"/generated/{index}.png", "seed": index},
                saved["edit_token"],
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(update, range(4)))

        assets = repository.get_planet(saved["id"])["image_assets"]
        self.assertEqual(set(assets), {f"inhabitant:{index}" for index in range(4)})


if __name__ == "__main__":
    unittest.main()
