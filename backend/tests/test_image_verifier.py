from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.image_verifier import MAX_IMAGE_BYTES, verify_image
from app.schema import Inhabitant, PlanetSpec


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse, calls: list[dict], **_: object) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


class ImageVerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temp.name) / "candidate.png"
        self.image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall-test-image")

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_multimodal_request_uses_header_and_returns_normalized_score(self) -> None:
        model_result = {
            "total_score": 99,
            "criterion_scores": {
                "specification_fidelity": 80,
                "distinctive_features": 70,
                "environment_fidelity": 90,
                "material_detail": 60,
                "technical_quality": 100,
            },
            "passed": True,
            "missing_features": [],
            "problems": ["Terrain is slightly smooth."],
            "refinement_prompt": "Add fine geological relief while preserving the silhouette.",
        }
        api_payload = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(model_result)}]}}]
        }
        response = _FakeResponse(200, api_payload)
        calls: list[dict] = []

        with (
            patch("app.image_verifier.gemini._load_keys", return_value=["secret-key"]),
            patch("app.image_verifier.gemini._next_key", return_value="secret-key"),
            patch(
                "app.image_verifier.httpx.AsyncClient",
                side_effect=lambda **kwargs: _FakeAsyncClient(response, calls, **kwargs),
            ),
        ):
            result = await verify_image(PlanetSpec(), "planet", self.image_path)

        self.assertTrue(result.verified)
        self.assertTrue(result.passed)
        # Weighted server-side score: the model's self-reported 99 is not trusted.
        self.assertEqual(result.total_score, 78)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret-key", calls[0]["url"])
        self.assertEqual(calls[0]["headers"], {"x-goog-api-key": "secret-key"})
        part = calls[0]["json"]["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(part["mimeType"], "image/png")
        self.assertLess(len(part["data"]), MAX_IMAGE_BYTES * 2)

    async def test_missing_feature_forces_failure_and_code_fence_is_parsed(self) -> None:
        raw = {
            "total_score": 100,
            "criterion_scores": {
                "specification_fidelity": 95,
                "distinctive_features": 95,
                "environment_fidelity": 95,
                "material_detail": 95,
                "technical_quality": 95,
            },
            "passed": True,
            "missing_features": ["Exactly two antennae are not visible."],
            "problems": [],
            "refinement_prompt": "Show exactly two separated antennae.",
        }
        fenced = "```json\n" + json.dumps(raw) + "\n```"
        response = _FakeResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": fenced}]}}]},
        )
        calls: list[dict] = []
        inhabitant = Inhabitant(name="케른", appearance="분홍 더듬이 두 개")

        with (
            patch("app.image_verifier.gemini._load_keys", return_value=["key"]),
            patch("app.image_verifier.gemini._next_key", return_value="key"),
            patch(
                "app.image_verifier.httpx.AsyncClient",
                side_effect=lambda **kwargs: _FakeAsyncClient(response, calls, **kwargs),
            ),
        ):
            result = await verify_image(
                PlanetSpec(), "inhabitant", self.image_path, inhabitant
            )

        self.assertTrue(result.verified)
        self.assertFalse(result.passed)
        prompt = calls[0]["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("분홍 더듬이 두 개", prompt)

    async def test_oversized_file_returns_explicit_fallback_without_http(self) -> None:
        self.image_path.write_bytes(b"0" * (MAX_IMAGE_BYTES + 1))
        with patch("app.image_verifier.httpx.AsyncClient") as client:
            result = await verify_image(PlanetSpec(), "surface", self.image_path)

        self.assertFalse(result.verified)
        self.assertFalse(result.passed)
        self.assertEqual(result.total_score, 0)
        self.assertIn("exceeds", result.error or "")
        client.assert_not_called()

    async def test_malformed_response_returns_explicit_fallback(self) -> None:
        response = _FakeResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]},
        )
        with (
            patch("app.image_verifier.gemini._load_keys", return_value=["key"]),
            patch("app.image_verifier.gemini._next_key", return_value="key"),
            patch(
                "app.image_verifier.httpx.AsyncClient",
                return_value=_FakeAsyncClient(response, []),
            ),
        ):
            result = await verify_image(PlanetSpec(), "planet", self.image_path)

        self.assertFalse(result.verified)
        self.assertIn("parsing", result.error or "")


if __name__ == "__main__":
    unittest.main()
