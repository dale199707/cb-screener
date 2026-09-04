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

    def test_price_history_overwrites_same_source_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            price_file = os.path.join(temp_dir, "cb_prices.json")
            df1 = pd.DataFrame([{"代號": "12345", "債券市價": 101.0, "CB成交量": 10}])
            df2 = pd.DataFrame([{"代號": "12345", "債券市價": 102.0, "CB成交量": 20}])
            with patch.object(screener, "HISTORY_DIR", temp_dir), patch.object(
                screener, "CB_PRICES_FILE", price_file
            ):
                screener.update_cb_price_history(df1, "2026-09-04")
                result = screener.update_cb_price_history(df2, "2026-09-04")
            self.assertEqual(result["12345"], [
                {"date": "2026-09-04", "close": 102.0, "vol": 20}
            ])

    def test_leader_requires_per_bond_history_and_stock_data(self):
        base = {
            "CB創20日高": True,
            "股價20日高": 100.0,
            "股價收盤": 95.0,
            "CB放量倍數": 1.5,
            "CB均量5日": 30.0,
            "CB歷史筆數": 5,
        }
        df = pd.DataFrame([
            {"代號": "PASS", **base},
            {"代號": "SHORT", **base, "CB歷史筆數": 4},
            {"代號": "NO_MA", **base, "股價20日高": None},
        ])
        result = screener.filter_leader(df, {
            "min_history_days": 5,
            "params": {
                "stock_below_high_ratio": 0.98,
                "min_volume_ratio": 1.3,
                "min_avg_volume_5d": 20,
            },
        })
        self.assertEqual(result["代號"].tolist(), ["PASS"])

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
