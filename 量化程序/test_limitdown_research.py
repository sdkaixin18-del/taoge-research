"""Temporal and execution boundary tests for the offline study."""
import unittest
from copy import deepcopy
from decimal import Decimal

from limitdown_research import (entry_signal, execution_issue, exit_scenario,
                               limits, one_word, prior_up_run, quality)


def day(**kw):
    return {"date": "2024-03-05", "open": "9.00", "high": "10.00", "low": "9.00",
            "close": "9.50", "preclose": "10.00", "volume": "10000000",
            "amount": "100000000", "isST": "0", "tradestatus": "1", **kw}


def bar(t, o, c, low=None, high=None, **kw):
    return {"date": "2024-03-05", "time": "20240305" + t.replace(":", "") + "00000",
            "open": str(o), "close": str(c), "low": str(low if low is not None else min(o, c)),
            "high": str(high if high is not None else max(o, c)),
            "volume": "1000000", "amount": "10000000", **kw}


class BoundaryTests(unittest.TestCase):
    def test_exchange_half_up_and_strict_one_word(self):
        r = day(preclose="5.15", open="4.64", high="4.64", low="4.64", close="4.64")
        self.assertEqual(limits(r)[0], Decimal("4.64"))
        self.assertTrue(one_word(r))
        self.assertFalse(one_word({**r, "high": "4.65"}))
        self.assertFalse(one_word({**r, "isST": "1"}))

    def test_future_bars_cannot_change_first_confirmation(self):
        bars = [bar("09:35", 9, 9), bar("09:40", 9, 9.1), bar("09:45", 9.1, 9.2, low=9.01)]
        for branch, expected in (("A", 1), ("B", 2)):
            self.assertEqual(entry_signal(bars, Decimal("9"), branch), expected)
            changed = deepcopy(bars) + [bar("10:00", 1, 99)]
            self.assertEqual(entry_signal(changed, Decimal("9"), branch), expected)

    def test_confirmation_does_not_use_intrabar_high(self):
        self.assertIsNone(entry_signal([bar("09:35", 9, 9, high=10)], Decimal("9"), "A"))

    def test_lunch_is_not_two_adjacent_confirmation_bars(self):
        bars = [bar("11:30", 9, 9.1), bar("13:05", 9.1, 9.2, low=9.01)]
        self.assertIsNone(entry_signal(bars, Decimal("9"), "B"))

    def test_late_signal_is_not_used(self):
        self.assertIsNone(entry_signal([bar("14:35", 9, 9.2)], Decimal("9"), "A"))

    def test_signal_cannot_clear_execution_at_limit(self):
        self.assertEqual(execution_issue(bar("09:45", 9, 9.3), day(), buy=True), "at_down_limit")
        self.assertEqual(execution_issue(bar("09:45", 11, 10.9), day(), buy=True), "at_up_limit")

    def test_prior_classification_cannot_see_future_up_run(self):
        rows = [day(close="10.5"), day(close="9"), day(close="11"), day(close="11"), day(close="11")]
        self.assertEqual(prior_up_run(rows, 1), 0)

    def test_locked_next_day_remains_unresolved(self):
        bars = [bar("09:35", 9, 9), bar("09:40", 9, 9)]
        r = exit_scenario(bars, day(close="9"), "X1", 10)
        self.assertEqual(r["exit_status"], "next_day_exit_unconfirmed")
        self.assertIsNone(r["net20_pct"])
        self.assertAlmostEqual(r["mark_to_close_pct"], -10)

    def test_exit_does_not_fill_at_locked_auction_open(self):
        bars = [bar("09:35", 9, 9.1), bar("09:40", 9.1, 9.2)]
        r = exit_scenario(bars, day(), "X1", 10)
        self.assertEqual(r["exit_status"], "delayed_price_scenario")
        self.assertEqual(r["exit_time"], "09:35")
        self.assertAlmostEqual(r["net20_pct"], -9.2)

    def test_quality_keeps_minute_gaps_and_price_disputes(self):
        issues = quality(day(), [bar("09:35", 9, 9.1)])
        self.assertIn("minute_coverage", issues)
        self.assertIn("daily_minute_close", issues)


if __name__ == "__main__":
    unittest.main()
