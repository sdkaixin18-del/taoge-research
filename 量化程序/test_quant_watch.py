import copy
import unittest
from pathlib import Path
from quant_watch import dt, evaluate, from_case, make_plan

CASE = Path(__file__).resolve().parents[1]/'课程视频'/'KSTR对照_20261017'


@unittest.skipUnless((CASE/'source_signal_alignment.json').is_file(),
                     '历史回放需要本地课程视频目录中的行情；行情不随Git分发')
class HistoricalIntegrity(unittest.TestCase):
    def setUp(self):
        self.data = from_case(CASE, 'sh688498', 'KSTRUSDT')

    def run_at(self, when='11:30', data=None, cost=15, position=None):
        return evaluate(data or self.data, dt('2026-10-09T'+when+':00+08:00'), cost, position)

    def test_1130_trial_not_confirmation(self):
        r = self.run_at()
        self.assertEqual(r['technical_state'], '试错候选')
        self.assertFalse(r['basket']['sh688256']['no_new_low'])
        self.assertTrue(r['research_only'])

    def test_1315_confirmation_is_not_automatic_entry(self):
        r = self.run_at('13:15')
        self.assertEqual(r['technical_state'], '确认候选')
        self.assertIn('放弃追入', r['decision'])
        self.assertLess(r['plan']['net_reward_risk'], 1.5)

    def test_prefix_invariance_at_every_bar(self):
        # Removing ALL future prices cannot change any past result.
        for r in self.data['trade']:
            asof = dt(r['end'])
            truncated = copy.deepcopy(self.data)
            for key in ('source_fast', 'source_slow', 'trade'):
                truncated[key] = [x for x in truncated[key] if dt(x['end']) <= asof]
            truncated['basket'] = {k:[x for x in v if dt(x['end']) <= asof]
                                   for k, v in truncated['basket'].items()}
            self.assertEqual(evaluate(self.data, asof, 15), evaluate(truncated, asof, 15))

    def test_future_price_mutation_cannot_change_past(self):
        changed = copy.deepcopy(self.data)
        for rows in [changed[k] for k in ('source_fast', 'source_slow', 'trade')]+list(changed['basket'].values()):
            for r in rows:
                if dt(r['end']) > dt('2026-10-09T11:30:00+08:00'):
                    for key in ('open','high','low','close'):
                        r[key] *= 100
        self.assertEqual(self.run_at(), self.run_at(data=changed))

    def test_lunch_not_fresh_signal(self):
        r = self.run_at('12:30')
        self.assertEqual(r['decision'], '暂停新增候选')
        self.assertTrue(any('午休' in x for x in r['reasons']))

    def test_missing_source_bar_blocks(self):
        self.data['source_fast'].pop(4)
        self.assertTrue(any('分钟缺口' in x for x in self.run_at()['reasons']))

    def test_missing_basket_bar_blocks(self):
        self.data['basket']['sh588000'].pop(4)
        self.assertTrue(any('有缺口' in x for x in self.run_at()['reasons']))

    def test_missing_trade_bar_blocks(self):
        self.data['trade'].pop(4)
        self.assertTrue(any('有缺口' in x for x in self.run_at()['reasons']))

    def test_cost_unknown_not_zero(self):
        r = self.run_at(cost=None)
        self.assertIsNone(r['plan']['net_reward_risk'])
        self.assertIn('成本未知', r['decision'])

    def test_higher_cost_reduces_entry_ceiling(self):
        a, b = self.run_at(cost=5)['plan'], self.run_at(cost=50)['plan']
        self.assertGreater(a['max_entry_price'], b['max_entry_price'])
        self.assertGreater(a['net_reward_risk'], b['net_reward_risk'])

    def test_entry_ceiling_matches_formula(self):
        plan = self.run_at()['plan']
        cap = plan['max_entry_price']
        rr = (plan['targets'][0]/cap-1-.0015)/(1-plan['invalidation']/cap+.0015)
        self.assertAlmostEqual(rr, 1.5)

    def test_day_one_does_not_validate_relation(self):
        r = self.run_at()['relation']
        self.assertEqual(r['days'], 1)
        self.assertFalse(r['sufficient_history'])
        self.assertEqual(r['status'], '未验证')

    def test_reversed_input_same_answer(self):
        changed = copy.deepcopy(self.data)
        for key in ('source_fast','source_slow','trade'):
            changed[key].reverse()
        self.assertEqual(self.run_at(), self.run_at(data=changed))

    def test_outside_source_session_blocks(self):
        self.assertEqual(self.run_at('15:00')['decision'], '暂停新增候选')

    def test_bad_identity_rejected(self):
        with self.assertRaises(ValueError):
            from_case(CASE, 'sh688256', 'KSTRUSDT')

    def test_stop_fixed_and_no_pre_entry_high(self):
        position = dict(entry_time='2026-10-09T13:20:00+08:00', entry_price=20.90,
                        stop=20.7, targets=[21.2], source_low=1158, entry_atr=.2)
        r = self.run_at('13:30', position=position)
        self.assertEqual(r['position']['signals'], [])
        position['entry_time'] = '2026-10-09T11:30:00+08:00'
        r = self.run_at('13:30', position=position)
        self.assertTrue(any('止损' in x for x in r['position']['signals']))

    def test_no_timezone_rejected(self):
        with self.assertRaises(ValueError):
            dt('2026-10-09T11:30:00')

    def test_mark_stop_cannot_use_trade_candles(self):
        with self.assertRaises(ValueError):
            self.run_at(position=dict(stop_basis='mark_price'))


if __name__ == '__main__':
    unittest.main()
