"""Regression coverage for monitoring run scope and platform-weighted rates."""

from __future__ import annotations

import inspect
import pathlib
import re
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class MonitoringRunScopeAndWeightsTests(unittest.TestCase):
    def test_platform_weight_aliases_use_saved_canonical_weights(self) -> None:
        from db import monitoring_db

        original = dict(monitoring_db.PLATFORM_WEIGHTS)
        try:
            monitoring_db.PLATFORM_WEIGHTS.clear()
            monitoring_db.PLATFORM_WEIGHTS.update(
                {
                    "doubao": 0.55,
                    "dashscope": 0.20,
                    "deepseek": 0.15,
                    "kimi": 0.10,
                }
            )

            cases = {
                "doubao": 0.55,
                "豆包": 0.55,
                "DOUBAO": 0.55,
                "dashscope": 0.20,
                "qwen": 0.20,
                "通义千问": 0.20,
                "DeepSeek": 0.15,
                "kimi": 0.10,
                "moonshot": 0.10,
            }
            for platform, expected in cases.items():
                with self.subTest(platform=platform):
                    self.assertAlmostEqual(monitoring_db.get_platform_weight(platform), expected)
        finally:
            monitoring_db.PLATFORM_WEIGHTS.clear()
            monitoring_db.PLATFORM_WEIGHTS.update(original)

    def test_weighted_rate_sql_joins_on_normalized_platform_key(self) -> None:
        source = read_repo_file("db/monitoring_db.py")

        self.assertIn("platform_key", source)
        self.assertIn("LEFT JOIN platform_weight pw ON lpp.platform_key = pw.platform", source)
        self.assertIn("_platform_key_sql_expr", source)

    def test_sync_task_trends_uses_same_platform_weight_normalizer(self) -> None:
        from db import monitoring_db

        source = inspect.getsource(monitoring_db.sync_task_trends)
        self.assertIn("get_platform_weight", source)
        self.assertNotIn('PLATFORM_WEIGHTS.get(r["platform"], 0.25)', source)

    def test_run_monitoring_uses_brand_quote_scope_once(self) -> None:
        api_source = read_repo_file("api/monitoring_api.py")
        server_source = read_repo_file("server.py")

        self.assertIn("quote_ids=quote_ids", api_source)
        self.assertIn("quote_ids=quote_ids", server_source)

    def test_api_exposes_weighted_rate_as_primary_display_rate(self) -> None:
        source = read_repo_file("api/monitoring_api.py")

        self.assertIn("raw_detection_rate", source)
        self.assertRegex(source, re.compile(r"detection_rate[\"']?\]\s*=\s*weighted_rate"))

    def test_weight_save_refreshes_keyword_rates_in_frontend(self) -> None:
        source = read_repo_file("frontend/src/pages/Monitoring/index.tsx")
        match = re.search(r"const handleSaveWeights = async \(\) => \{(?P<body>.*?)\n\s*\};", source, re.S)
        self.assertIsNotNone(match)
        self.assertIn("fetchKeywords(parseInt(selectedClient", match.group("body"))


if __name__ == "__main__":
    unittest.main()
