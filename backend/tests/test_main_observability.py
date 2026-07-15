from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class MainObservabilityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_request_id_is_server_generated(self) -> None:
        response = self.client.get(
            "/api/health",
            headers={"X-Request-ID": "caller-controlled-value"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.headers["X-Request-ID"], r"^req_[0-9a-f]{24}$")
        self.assertNotEqual(response.headers["X-Request-ID"], "caller-controlled-value")

    def test_metrics_endpoint_is_hidden_without_configuration(self) -> None:
        with patch.dict(os.environ, {"TERRA_METRICS_TOKEN": ""}, clear=False):
            response = self.client.get("/api/admin/metrics")

        self.assertEqual(response.status_code, 404)

    def test_metrics_endpoint_requires_the_exact_bearer_token(self) -> None:
        token = "a" * 64
        with patch.dict(os.environ, {"TERRA_METRICS_TOKEN": token}, clear=False):
            unauthorized = self.client.get(
                "/api/admin/metrics",
                headers={"Authorization": "Bearer wrong"},
            )
            authorized = self.client.get(
                "/api/admin/metrics",
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(authorized.status_code, 200)
        self.assertIn("text/plain", authorized.headers["Content-Type"])
        self.assertEqual(authorized.headers["Cache-Control"], "no-store")
        self.assertIn("terra_http_requests_total", authorized.text)
        self.assertNotIn(token, authorized.text)


if __name__ == "__main__":
    unittest.main()
