"""Keep partial MCP responses and media estimates out of trading inputs."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from limitdown_ifind_audit import (
    add_bar,
    bar_row,
    column_dates,
    envelope,
    inventory_change,
    number,
    run,
    seal_columns,
    tables,
)


def daily(**changes):
    return {
        "证券代码": "600001.SH",
        "证券简称": "合成股票",
        "日期": "20240304",
        "开盘价（单位：元）": "9",
        "最高价（单位：元）": "9.2",
        "最低价（单位：元）": "9",
        "收盘价": "9.1",
        "成交量": "12.3456万",
        **changes,
    }


class IfindAuditTests(unittest.TestCase):
    def test_chinese_scale_preserves_share_precision(self):
        self.assertEqual(number("12.3456万"), Decimal(123456))
        self.assertEqual(number("3.83亿"), Decimal(383000000))
        self.assertIsNone(number("\t"))
        for value in ("2手", "3亿元", "NaN", "inf", "1e9999999999999999999"):
            with (
                self.subTest(value=value),
                self.assertRaises((ValueError, ArithmeticError)),
            ):
                number(value)

    def test_holiday_carried_close_cannot_be_a_trading_bar(self):
        row = daily(日期="20240303", 收盘价="10", 成交量="\t")
        result, state = bar_row(row, {"2024-03-04"}, "股")
        self.assertIsNone(result)
        self.assertEqual(state, "not_a_trading_day")

    def test_latest_price_column_cannot_be_relabelled_historical(self):
        row = daily()
        row["收盘价:不复权[20261016]"] = row.pop("收盘价")
        result, state = bar_row(row, {"2024-03-04"}, "股")
        self.assertIsNone(result)
        self.assertEqual(state, "column_date_disagrees")
        self.assertEqual(column_dates(row), {"收盘价:不复权[20261016]": "20261016"})

    def test_bar_requires_all_prices_and_verified_volume_unit(self):
        for row, unit, expected in [
            (daily(成交量=""), "股", "incomplete_bar"),
            (daily(), "手", "volume_unit_unverified"),
            (daily(成交量="0"), "股", "no_traded_volume"),
        ]:
            result, state = bar_row(row, {"2024-03-04"}, unit)
            self.assertIsNone(result)
            self.assertEqual(state, expected)

    def test_impossible_ohlc_is_not_repaired(self):
        row = daily()
        row["最低价（单位：元）"] = "9.3"
        self.assertEqual(bar_row(row, {"2024-03-04"}, "股")[1], "invalid_ohlc")

    def test_current_st_name_does_not_set_historical_st_status(self):
        bar, state = bar_row(daily(证券简称="ST合成"), {"2024-03-04"}, "股")
        self.assertEqual(state, "complete_trading_bar")
        self.assertEqual(bar["volume"], Decimal(123456))
        self.assertNotIn("isST", bar)

    def test_conflicting_duplicate_bars_stop_the_audit(self):
        bar, _ = bar_row(daily(), {"2024-03-04"}, "股")
        index = {}
        add_bar(index, bar)
        add_bar(index, dict(bar))
        with self.assertRaises(ValueError):
            add_bar(index, {**bar, "close": Decimal("9.15")})

    def test_success_envelope_does_not_create_requested_seal_fields(self):
        headers = [
            "证券代码",
            "日期",
            "跌停价（单位：元）",
            "流通A股（单位：股）",
            "成交额（单位：元）",
        ]
        self.assertEqual(
            seal_columns(headers),
            {"sell_queue_shares_present": False, "sell_queue_amount_present": False},
        )

    def test_smaller_seal_amount_can_mean_similar_or_more_shares(self):
        result = inventory_change(
            Decimal(383000000),
            Decimal("17.53"),
            Decimal(347000000),
            Decimal("15.78"),
        )
        self.assertLess(result["amount_change_pct"], 0)
        self.assertGreater(result["implied_shares_change_pct"], 0)
        self.assertIsNone(result["qualified_Q_shares"])
        self.assertFalse(result["prediction_generated"])

    def test_reject_ragged_tables_and_non_success_envelopes(self):
        with self.assertRaises(ValueError):
            list(tables("|证券代码|日期|\n|---|---|\n|600001.SH|"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(
                json.dumps(
                    {
                        "result": {
                            "content": [
                                {"type": "text", "text": '{"code":0,"data":{}}'}
                            ]
                        }
                    }
                )
            )
            with self.assertRaises(ValueError):
                envelope(path)

    def test_frozen_protocol_hash_must_still_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "研究协议_封存.json").write_text("{}")
            (path / "研究协议_SHA256.txt").write_text("different")
            with self.assertRaisesRegex(ValueError, "Frozen protocol changed"):
                run(path, path, path / "out")
            self.assertFalse((path / "out").exists())


if __name__ == "__main__":
    unittest.main()
