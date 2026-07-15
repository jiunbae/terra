from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.images import provider_status


class ImageModelSupplyChainTests(unittest.TestCase):
    def test_exact_local_snapshot_is_used_and_exposed_as_a_revision_label(self) -> None:
        revision = "b3a8f31115a11f2f9e2fa0bfbc8d78dcc3e6568b"
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot = Path(temporary_directory) / revision
            snapshot.mkdir()
            with (
                patch.dict(
                    os.environ,
                    {
                        "TERRA_IMAGE_MODEL": "publisher/model",
                        "TERRA_IMAGE_MODEL_REVISION": revision,
                        "TERRA_IMAGE_MODEL_PATH": str(snapshot),
                    },
                    clear=True,
                ),
                patch("app.images.shutil.which", return_value="/usr/local/bin/mflux"),
            ):
                status = provider_status()

        self.assertTrue(status.available)
        self.assertEqual(status.model, f"publisher/model@{revision}")
        self.assertEqual(status.model_argument, str(snapshot.resolve()))

    def test_revision_mismatch_fails_closed(self) -> None:
        expected_revision = "a" * 40
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot = Path(temporary_directory) / "unexpected-revision"
            snapshot.mkdir()
            with (
                patch.dict(
                    os.environ,
                    {
                        "TERRA_IMAGE_MODEL": "publisher/model",
                        "TERRA_IMAGE_MODEL_REVISION": expected_revision,
                        "TERRA_IMAGE_MODEL_PATH": str(snapshot),
                    },
                    clear=True,
                ),
                patch("app.images.shutil.which", return_value="/usr/local/bin/mflux"),
            ):
                status = provider_status()

        self.assertFalse(status.available)
        self.assertIn("revision", status.message)

    def test_revision_without_an_exact_local_snapshot_fails_closed(self) -> None:
        revision = "a" * 40
        with (
            patch.dict(
                os.environ,
                {"TERRA_IMAGE_MODEL_REVISION": revision},
                clear=True,
            ),
            patch("app.images.shutil.which", return_value="/usr/local/bin/mflux"),
        ):
            status = provider_status()

        self.assertFalse(status.available)
        self.assertIn("로컬", status.message)

    def test_missing_pinned_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing-revision"
            with (
                patch.dict(
                    os.environ,
                    {
                        "TERRA_IMAGE_MODEL_REVISION": "a" * 40,
                        "TERRA_IMAGE_MODEL_PATH": str(missing),
                    },
                    clear=True,
                ),
                patch("app.images.shutil.which", return_value="/usr/local/bin/mflux"),
            ):
                status = provider_status()

        self.assertFalse(status.available)
        self.assertIn("찾을 수 없습니다", status.message)
