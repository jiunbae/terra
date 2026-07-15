from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ProductionCommandTests(unittest.TestCase):
    def test_single_worker_production_disables_capability_bearing_access_logs(self) -> None:
        start_script = (ROOT / "start.sh").read_text(encoding="utf-8")

        self.assertIn("--workers 1", start_script)
        self.assertIn("--no-access-log", start_script)
        self.assertIn("--no-server-header", start_script)


if __name__ == "__main__":
    unittest.main()
