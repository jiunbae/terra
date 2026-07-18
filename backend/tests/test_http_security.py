from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.images import GENERATED_DIR
from app.main import app


class HttpSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_api_has_security_headers_and_no_store(self) -> None:
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        # 엄격한 API CSP는 script를 self로만 제한한다.
        self.assertIn("script-src 'self';", response.headers["content-security-policy"])
        self.assertIn(
            "includeSubDomains", response.headers["strict-transport-security"]
        )
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_docs_csp_is_relaxed_for_swagger(self) -> None:
        # docs는 개발 환경에서만 활성. 해당 경로에서만 CDN 스크립트를 허용한다.
        response = self.client.get("/docs")
        if response.status_code == 404:
            self.skipTest("docs disabled in this environment")
        self.assertIn(
            "script-src 'self' 'unsafe-inline' https:",
            response.headers["content-security-policy"],
        )

    def test_readyz_hides_infra_details_without_admin_token(self) -> None:
        body = self.client.get("/api/readyz").json()
        self.assertIn("status", body)
        self.assertIn("checks", body)
        self.assertNotIn("free_disk_mb", body)
        self.assertNotIn("queue", body)

    def test_readyz_exposes_infra_details_to_admin(self) -> None:
        token = "x" * 40
        with patch.dict(os.environ, {"TERRA_METRICS_TOKEN": token}):
            body = self.client.get(
                "/api/readyz", headers={"authorization": f"Bearer {token}"}
            ).json()
        self.assertIn("free_disk_mb", body)
        self.assertIn("queue", body)

    def test_untrusted_host_is_rejected(self) -> None:
        response = self.client.get("/api/health", headers={"host": "attacker.invalid"})

        self.assertEqual(response.status_code, 400)

    def test_oversized_request_is_rejected_before_parsing(self) -> None:
        response = self.client.post(
            "/api/analyze",
            content=b"x" * (1024 * 1024 + 1),
            headers={"content-type": "application/json"},
        )

        self.assertEqual(response.status_code, 413)

    def test_oversized_delete_capability_body_is_rejected_before_parsing(self) -> None:
        response = self.client.request(
            "DELETE",
            "/api/planets/example",
            content=b"x" * (1024 * 1024 + 1),
            headers={"content-type": "application/json"},
        )

        self.assertEqual(response.status_code, 413)

    def test_cors_preflight_also_has_security_headers(self) -> None:
        response = self.client.options(
            "/api/analyze",
            headers={
                "origin": "http://localhost:5173",
                "access-control-request-method": "POST",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_chunked_body_is_limited_without_content_length(self) -> None:
        async def receive_chunks():
            yield {"type": "http.request", "body": b"x" * 700_000, "more_body": True}
            yield {"type": "http.request", "body": b"x" * 400_000, "more_body": False}

        messages = receive_chunks()
        sent: list[dict] = []

        async def receive():
            return await anext(messages)

        async def send(message):
            sent.append(message)

        # Exercise the limiter directly so TestClient cannot add Content-Length.
        from app.http_security import ContentLengthLimitMiddleware

        limited = ContentLengthLimitMiddleware(app, max_bytes=1024 * 1024)
        import asyncio

        asyncio.run(
            limited(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/api/analyze",
                    "raw_path": b"/api/analyze",
                    "query_string": b"",
                    "root_path": "",
                    "headers": [(b"host", b"testserver")],
                    "client": ("127.0.0.1", 1234),
                    "server": ("testserver", 80),
                },
                receive,
                send,
            )
        )

        self.assertEqual(sent[0]["status"], 413)

    def test_generated_assets_are_immutable_cached(self) -> None:
        path = GENERATED_DIR / "http-security-test.txt"
        path.write_text("ok", encoding="utf-8")
        try:
            response = self.client.get(f"/generated/{path.name}")
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )


if __name__ == "__main__":
    unittest.main()
