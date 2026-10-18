"""Boundary tests for cross-source evidence and as-of seal eligibility."""
import unittest
from copy import deepcopy

from limitdown_data_audit import (checkpoints, minute_labels, point_map,
                                  point_first_confirmation, sampled_ticks_clock,
                                  seal_feature)
from decimal import Decimal


class DataAuditTests(unittest.TestCase):
    def test_no_lunch_point_or_missing_point_synthesis(self):
        labels = minute_labels()
        self.assertEqual(labels[119:122], ['11:30', '13:01', '13:02'])
        self.assertEqual(labels[-1], '15:00')
        with self.assertRaises(ValueError):
            point_map([{'price': 5, 'vol': 0}] * 239)

    def test_point_clock_prior_minute_and_closing_auction(self):
        points = [{'price': 5, 'vol': 0}] * 240
        ticks = [{'time': '09:30', 'price': 5}, {'time': '09:31', 'price': 6},
                 {'time': '14:59', 'price': 7}, {'time': '15:00', 'price': 5}]
        records = sampled_ticks_clock(points, ticks)
        self.assertEqual(records[0]['trade_minute'], '09:30')
        self.assertTrue(records[0]['same_cent'])
        self.assertFalse(records[1]['same_cent'])
        self.assertEqual(records[-1]['trade_minute'], '15:00')
        self.assertTrue(records[-1]['same_cent'])

    def test_price_point_not_high_low_and_no_future_confirmation(self):
        bars = [{'time': '20240305093500000', 'close': '5.00', 'high': '6.00'},
                {'time': '20240305094000000', 'close': '5.20', 'high': '6.00'}]
        points = {'09:35': {'price': 5.00}, '09:40': {'price': 5.20}}
        self.assertEqual(point_first_confirmation(bars, points, Decimal('5.00')), '09:40')
        changed = deepcopy(bars)
        changed[0]['high'] = '100.00'
        self.assertEqual(checkpoints(bars, points), checkpoints(changed, points))
        points['09:40']['price'] = 50
        self.assertIsNone(point_first_confirmation(bars[:1], points, Decimal('5.00')))

    def test_only_exact_available_sell_inventory_with_known_units(self):
        good = {'kind': 'remaining_sell_queue', 'relation': 'eq', 'quantity': 10,
                'unit': 'lots_100_shares', 'snapshot_time': '2024-03-01T14:57:00+08:00',
                'available_at': '2024-03-01T14:57:01+08:00',
                'prediction_cutoff': '2024-03-01T15:00:00+08:00'}
        self.assertEqual(seal_feature(good), 1000)
        for key, bad in [('kind', 'at_limit_turnover'), ('relation', 'gt'),
                         ('available_at', '2024-03-01T18:23:00+08:00'),
                         ('snapshot_time', None), ('unit', '元|个'),
                         ('snapshot_time', '2024-03-01T14:57:00'),
                         ('quantity', float('nan')), ('quantity', float('inf')),
                         ('snapshot_time', '2024-02-29T14:57:00+08:00')]:
            with self.subTest(key=key, value=bad):
                self.assertIsNone(seal_feature({**good, key: bad}))


if __name__ == '__main__':
    unittest.main()
