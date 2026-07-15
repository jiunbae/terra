from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.maintenance import (
    GeneratedCleanupPolicy,
    cleanup_generated_images,
    load_referenced_generated_pngs,
)


class GeneratedMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.database = self.root / "terra.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE planets (cover_image_url TEXT, image_assets_json TEXT NOT NULL)"
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _insert(self, cover: str | None, assets: object) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO planets (cover_image_url, image_assets_json) VALUES (?, ?)",
                (cover, json.dumps(assets)),
            )

    def _file(self, relative: str, *, size: int, modified_at: float) -> Path:
        path = self.generated / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        os.utime(path, (modified_at, modified_at))
        return path

    @staticmethod
    def _policy(
        *,
        ttl_hours: float = 168,
        min_free_bytes: int = 0,
        max_store_bytes: int = 0,
        grace_minutes: float = 0,
    ) -> GeneratedCleanupPolicy:
        return GeneratedCleanupPolicy(
            ttl_hours=ttl_hours,
            min_free_bytes=min_free_bytes,
            max_store_bytes=max_store_bytes,
            grace_minutes=grace_minutes,
        )

    def test_ttl_deletes_only_unreferenced_top_level_pngs(self) -> None:
        now = 2_000_000.0
        old = now - 8 * 24 * 3600
        recent = now - 2 * 3600
        cover = self._file("cover.png", size=10, modified_at=old)
        inhabitant = self._file("inhabitant.png", size=11, modified_at=old)
        expired = self._file("expired.png", size=12, modified_at=old)
        fresh = self._file("fresh.png", size=13, modified_at=recent)
        non_png = self._file("notes.jpg", size=14, modified_at=old)
        guide = self._file(".guides/guide.png", size=15, modified_at=old)
        self._insert(
            "/generated/cover.png",
            {"inhabitant:0": {"url": "/generated/inhabitant.png"}},
        )

        result = cleanup_generated_images(
            generated_dir=self.generated,
            db_path=self.database,
            policy=self._policy(),
            now=now,
        )

        self.assertFalse(result.aborted)
        self.assertEqual(result.scanned_pngs, 4)
        self.assertEqual(result.referenced_pngs, 2)
        self.assertEqual(result.deleted_ttl, ("expired.png",))
        self.assertEqual(result.reclaimed_bytes, 12)
        self.assertFalse(expired.exists())
        for path in (cover, inhabitant, fresh, non_png, guide):
            self.assertTrue(path.exists())

    def test_capacity_pressure_deletes_oldest_unreferenced_first(self) -> None:
        now = 2_000_000.0
        referenced = self._file("kept.png", size=10, modified_at=now - 3000)
        oldest = self._file("oldest.png", size=10, modified_at=now - 2000)
        newer = self._file("newer.png", size=10, modified_at=now - 1000)
        self._insert("/generated/kept.png", {})

        result = cleanup_generated_images(
            generated_dir=self.generated,
            db_path=self.database,
            policy=self._policy(ttl_hours=9999, max_store_bytes=20),
            now=now,
        )

        self.assertTrue(result.pressure_was_active)
        self.assertEqual(result.deleted_pressure, ("oldest.png",))
        self.assertEqual(result.store_bytes_after, 20)
        self.assertTrue(result.limits_satisfied)
        self.assertTrue(referenced.exists())
        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())

    def test_low_disk_pressure_uses_estimated_reclaimed_space(self) -> None:
        now = 2_000_000.0
        first = self._file("first.png", size=12, modified_at=now - 2000)
        second = self._file("second.png", size=15, modified_at=now - 1000)
        self._insert(None, {})

        with patch(
            "app.maintenance.shutil.disk_usage",
            return_value=SimpleNamespace(total=100, used=50, free=50),
        ):
            result = cleanup_generated_images(
                generated_dir=self.generated,
                db_path=self.database,
                policy=self._policy(ttl_hours=9999, min_free_bytes=70),
                now=now,
            )

        self.assertEqual(result.deleted_pressure, ("first.png", "second.png"))
        self.assertEqual(result.estimated_free_bytes_after, 77)
        self.assertTrue(result.limits_satisfied)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_pressure_respects_recent_file_grace_and_reports_unsatisfied_limit(self) -> None:
        now = 2_000_000.0
        recent = self._file("active.png", size=20, modified_at=now - 30)
        self._insert(None, {})

        result = cleanup_generated_images(
            generated_dir=self.generated,
            db_path=self.database,
            policy=self._policy(
                ttl_hours=9999,
                max_store_bytes=1,
                grace_minutes=60,
            ),
            now=now,
        )

        self.assertEqual(result.deleted_count, 0)
        self.assertEqual(result.protected_recent_pngs, 1)
        self.assertFalse(result.limits_satisfied)
        self.assertTrue(recent.exists())

    def test_reference_scan_failure_aborts_without_deleting(self) -> None:
        now = 2_000_000.0
        expired = self._file("unknown.png", size=10, modified_at=now - 9 * 24 * 3600)

        result = cleanup_generated_images(
            generated_dir=self.generated,
            db_path=self.root / "missing.sqlite3",
            policy=self._policy(),
            now=now,
        )

        self.assertTrue(result.aborted)
        self.assertFalse(result.limits_satisfied)
        self.assertTrue(expired.exists())
        self.assertTrue(result.errors)

    def test_malformed_asset_json_is_fail_closed(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO planets (cover_image_url, image_assets_json) VALUES (?, ?)",
                (None, "{not-json"),
            )
        expired = self._file("unknown.png", size=10, modified_at=1)

        result = cleanup_generated_images(
            generated_dir=self.generated,
            db_path=self.database,
            policy=self._policy(ttl_hours=0),
            now=2_000_000,
        )

        self.assertTrue(result.aborted)
        self.assertTrue(expired.exists())

    def test_reference_loader_ignores_external_and_non_png_urls(self) -> None:
        self._insert(
            "/generated/cover.png",
            {
                "surface": {"url": "/generated/surface.png"},
                "external": {"url": "https://example.com/external.png"},
                "not_png": {"url": "/generated/readme.txt"},
            },
        )
        self.assertEqual(
            load_referenced_generated_pngs(self.database),
            frozenset({"cover.png", "surface.png"}),
        )


if __name__ == "__main__":
    unittest.main()
