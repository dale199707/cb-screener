import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import cb_screener as screener


class ScreenerTests(unittest.TestCase):
    def test_infer_source_date_uses_most_common_create_date(self):
        df = pd.DataFrame({
            "create_date": [
                "2026-09-03T09:00:00", "2026-09-03T09:01:00",
                "2026-09-02T09:00:00",
            ]
        })
        self.assertEqual(screener.infer_source_date(df).isoformat(), "2026-09-03")

    def test_missing_required_columns_fail_validation(self):
        with self.assertRaisesRegex(ValueError, "缺少必要欄位"):
            screener.validate_cb_data(pd.DataFrame({"代號": ["12345"]}), {"min_records": 1})

    def test_same_snapshot_does_not_consume_new_tick(self):
        previous = {
            "date": "2026-09-03",
            "codes": ["12345"],
            "first_seen": {"12345": "2026-09-03"},
            "new_ticks_left": {"12345": 2},
        }
        tags, first_seen, ticks = screener.determine_new_tags(
            ["12345"], previous, "2026-09-03"
        )
        self.assertEqual(tags, {"12345": "2026-09-03"})
        self.assertEqual(first_seen, {"12345": "2026-09-03"})
        self.assertEqual(ticks, {"12345": 2})

    def test_next_snapshot_consumes_one_new_tick(self):
        previous = {
            "date": "2026-09-03",
            "codes": ["12345"],
            "first_seen": {"12345": "2026-09-03"},
            "new_ticks_left": {"12345": 2},
        }
        _, _, ticks = screener.determine_new_tags(
            ["12345"], previous, "2026-09-04"
        )
        self.assertEqual(ticks, {"12345": 1})

    def test_honor_price_range_is_strictly_above_103_and_below_135(self):
        df = pd.DataFrame([
            {"代號": "LOW_EDGE", "債券市價": 103.0},
            {"代號": "LOW_PASS", "債券市價": 103.01},
            {"代號": "HIGH_PASS", "債券市價": 134.99},
            {"代號": "HIGH_EDGE", "債券市價": 135.0},
        ])
        df["股價轉換價比"] = 0.0
        df["轉換價值"] = 100.0
        df["CB均量"] = 21.0

        result = screener.filter_cb_honor(df, {
            "params": {
                "cb_price_min_exclusive": 103,
                "cb_price_max_exclusive": 135,
                "stock_conversion_ratio_min": -0.20,
                "stock_conversion_ratio_max": 0.30,
                "min_avg_volume_5d_exclusive": 20,
            },
        })
        self.assertEqual(result["代號"].tolist(), ["LOW_PASS", "HIGH_PASS"])

    def test_empty_result_overwrites_daily_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_dir = os.path.join(temp_dir, "daily")
            os.makedirs(daily_dir)
            daily_path = os.path.join(daily_dir, "2026-09-04_測試.json")
            with open(daily_path, "w", encoding="utf-8") as f:
                json.dump({"count": 1, "data": [{"代號": "OLD"}]}, f)
            with patch.object(screener, "HISTORY_DIR", temp_dir):
                screener.save_current_results(
                    "測試", [], {}, {}, {}, pd.DataFrame(), "2026-09-04"
                )
            with open(daily_path, encoding="utf-8") as f:
                result = json.load(f)
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["data"], [])


if __name__ == "__main__":
    unittest.main()
