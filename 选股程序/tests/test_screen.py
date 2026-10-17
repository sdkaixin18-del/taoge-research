import copy
from datetime import date,timedelta
from pathlib import Path
import tempfile
import unittest
from taoge_screen.core import screen
from taoge_screen.io import digest,read,save,seal,verify
from taoge_screen.validation import evaluate,expected_times,label_path,simulate
from taoge_screen.report import export
from taoge_screen.replay import replay

RULES=Path(__file__).parents[1]/'配置/反抽规则.json'
ASOF,DAY,NEXT='2026-10-08','2026-10-09','2026-10-12'


def fixture():
    rules=read(RULES)
    stocks=[dict(symbol=s,name=s,member_known_at='2026-08-01T00:00:00+08:00',peer_group='test',
        peer_group_known_at='2026-08-01T00:00:00+08:00',event_review='unknown',corporate_action_review='unknown',context_records=[])
        for s in ('sz000001','sz000002','sz000003')]
    u=dict(schema_version=1,stocks=stocks,references=[dict(symbol='sh515000',name='ETF',member_known_at='2020-01-01T00:00:00+08:00')])
    days=[(date(2026,8,31)+timedelta(days=i)).isoformat() for i in range(39) if (date(2026,8,31)+timedelta(days=i)).weekday()<5]
    d=dict(interval='1d',price_basis='unadjusted',amount_unit='CNY',volume_unit='shares',universe_sha256=digest(u),requested_start=days[0],calendar=days,stocks={})
    for j,sym in enumerate(('sz000001','sz000002','sz000003','sh515000')):
        rows=[];previous=None;peak=len(days)-6
        for i,day in enumerate(days):
            c=(100+2*min(i,peak)-max(0,i-peak)*2 if j==0 else 100-max(0,i-peak)*8 if j==1 else 100)
            rows.append(dict(date=day,open=c-.2,high=c+1,low=c-1,close=c,preclose=c if previous is None else previous,
                amount=[1.5e8,1.1e8,1e9,2e9][j],volume=1e6,trading=True,is_st=False));previous=c
        d['stocks'][sym]=rows
    return u,d,rules


def bars(day=DAY,ref=False):
    out=[]
    for i,t in enumerate(expected_times(day)):
        if ref:c=100+i;o,h,l=c-.2,c+1,c-1
        else:
            o,h,l,c=([(95,96,93,95),(95,97,94,96),(96,99,95,98),(98,104,97,100),(100,111,99,110)][i] if i<5 else (105,106,104,105))
        out.append(dict(date=day,end=t.isoformat(),open=o,high=h,low=l,close=c,volume=1000.,amount=c*1000.))
    return out


def sim_fixture():
    u,d,r=fixture();s=screen(u,d,r,ASOF);row=copy.deepcopy(s['rows'][0]);row['features'].update(close=110,atr=4)
    return row,s,bars(),bars(ref=True),bars(ref=True),{'a':bars(ref=True),'b':bars(ref=True)}


def eval_fixture():
    u,d,r=fixture();s=screen(u,d,r,ASOF)
    for row in s['rows']:row['features'].update(close=110,atr=4)
    s=seal({k:v for k,v in s.items() if k!='content_sha256'})
    common=dict(universe_sha256=digest(u),price_basis='unadjusted',amount_unit='CNY',volume_unit='shares')
    dd=dict(common,interval='1d',requested_start=ASOF,calendar=[ASOF,DAY,NEXT],stocks={})
    mm=dict(common,interval='5m',retrieved_at='2026-10-18T03:00:00+00:00',stocks={})
    for ins in u['stocks']+u['references']:
        sym=ins['symbol'];ms=bars(ref=sym=='sh515000');ns=bars(NEXT,ref=True);mm['stocks'][sym]=ms+ns
        dd['stocks'][sym]=[dict(date=DAY,open=ms[0]['open'],high=max(b['high'] for b in ms),low=min(b['low'] for b in ms),close=ms[-1]['close'],preclose=110,volume=48000,amount=5e6,trading=True),
            dict(date=NEXT,open=100,high=150,low=90,close=105,preclose=105,volume=48000,amount=5e6,trading=True)]
    return s,dd,mm


class Screening(unittest.TestCase):
    def setUp(self):self.u,self.d,self.r=fixture()
    def run_screen(self):return screen(self.u,self.d,self.r,ASOF)
    def sync(self):self.d['universe_sha256']=digest(self.u)
    def test_two_modes(self):
        s=self.run_screen();self.assertEqual([r['mode'] for r in s['rows']],['强势回调','超跌反抽',None]);self.assertEqual(len(s['selected_symbols']),2);verify(s)
    def test_liquidity_not_rank(self):
        before=self.run_screen()['selected_symbols']
        for b in self.d['stocks']['sz000003']:b['amount']*=100
        self.assertEqual(self.run_screen()['selected_symbols'],before)
    def test_absolute_capacity(self):
        for b in self.d['stocks']['sz000001']:b['amount']=1e6
        self.assertFalse(self.run_screen()['rows'][0]['selected'])
    def test_missing_member_kept_without_changing_benchmark(self):
        del self.d['stocks']['sz000003'];s=self.run_screen()
        self.assertTrue(s['valid']);self.assertEqual(s['selected_symbols'],['sz000001','sz000002']);self.assertEqual(s['data_errors'],['sz000003']);self.assertEqual(len(s['rows']),3)
    def test_missing_reference_blocks(self):
        del self.d['stocks']['sh515000'];self.assertFalse(self.run_screen()['valid'])
    def test_future_corrupt_bars_ignored(self):
        before=self.run_screen();self.d['calendar'].append(DAY)
        for x in self.d['stocks'].values():x.append(dict(date=DAY,open='bad'))
        self.assertEqual(self.run_screen(),before)
    def test_future_membership(self):
        self.u['stocks'][0]['member_known_at']='2026-10-09T00:00:00+08:00';self.sync();self.assertFalse(self.run_screen()['rows'][0]['selected'])
    def test_amount_not_estimated(self):
        del self.d['stocks']['sz000001'][-2]['amount'];self.assertIn('sz000001',self.run_screen()['data_errors'])
    def test_missing_preclose_not_assumed_no_action(self):
        del self.d['stocks']['sz000001'][-2]['preclose'];self.assertIn('sz000001',self.run_screen()['data_errors'])
    def test_historical_st(self):
        self.d['stocks']['sz000001'][-1]['is_st']=True;self.assertFalse(self.run_screen()['rows'][0]['selected'])
    def test_unknown_status(self):
        del self.d['stocks']['sz000001'][-1]['is_st'];self.assertIn('sz000001',self.run_screen()['data_errors'])
    def test_future_clear(self):
        self.u['stocks'][0].update(event_review='clear',event_review_at='2026-10-09T00:00:00+08:00');self.sync();self.assertEqual(self.run_screen()['rows'][0]['event_review'],'unknown')
    def test_unknown_context_never_ready(self):self.assertFalse(any(r['ready'] for r in self.run_screen()['rows']))
    def test_complete_current_context_can_be_ready(self):
        stock=self.u['stocks'][0]
        stock.update(event_review='clear',event_review_at='2026-10-08T14:00:00+08:00',event_review_source='https://example.com/event',
            corporate_action_review='clear',corporate_action_review_at='2026-10-08T14:00:00+08:00',corporate_action_review_source='https://example.com/action')
        stock['context_records']=[dict(known_at='2026-10-08T14:00:00+08:00',valid_through='2026-10-08T15:00:00+08:00',state='active',
            premise='test',link_basis='test',expectation_source='https://example.com/expectation',pressure_symbol='sz000002',invalidation='test',source_url='https://example.com/source')]
        self.sync();r=self.run_screen()['rows'][0];self.assertTrue(r['ready']);self.assertEqual(r['pressure_symbol'],'sz000002')
    def test_clear_without_source_stays_unknown(self):
        self.u['stocks'][0].update(event_review='clear',event_review_at='2026-10-08T14:00:00+08:00');self.sync()
        self.assertEqual(self.run_screen()['rows'][0]['event_review'],'unknown')
    def test_nonfinite_rule_rejected(self):
        self.r['entry']['panic_atr']=float('nan')
        with self.assertRaises(ValueError):self.run_screen()
    def test_future_context(self):
        self.u['stocks'][0]['context_records']=[dict(known_at='2026-10-08T20:00:00+08:00',valid_through='2026-10-09T15:00:00+08:00',state='active',pressure_symbol='sz000002')];self.sync();self.assertIsNone(self.run_screen()['rows'][0]['context'])
    def test_invalidated_context(self):
        self.u['stocks'][0]['context_records']=[dict(known_at='2026-10-08T14:00:00+08:00',valid_through='2026-10-08T15:00:00+08:00',state='invalidated')];self.sync();self.assertFalse(self.run_screen()['rows'][0]['selected'])
    def test_future_peer_group(self):
        self.u['stocks'][0]['peer_group_known_at']='2026-10-09T00:00:00+08:00';self.sync();self.assertEqual(self.run_screen()['rows'][0]['peers'],[])
    def test_unknown_rule(self):
        self.r['pick_yunnan']=True
        with self.assertRaises(ValueError):self.run_screen()
    def test_unknown_entry_rule(self):
        self.r['entry']['posthoc_stop']=True
        with self.assertRaises(ValueError):self.run_screen()
    def test_units(self):
        self.d['amount_unit']='万元'
        with self.assertRaises(ValueError):self.run_screen()
    def test_stale_reference(self):
        self.d['stocks']['sh515000'].pop();self.assertFalse(self.run_screen()['valid'])
    def test_duplicate_calendar(self):
        self.d['calendar'].append(ASOF)
        with self.assertRaises(ValueError):self.run_screen()
    def test_partial_daily(self):
        self.d['retrieved_at']='2026-10-08T14:59:00+08:00'
        with self.assertRaises(ValueError):self.run_screen()
    def test_price_basis_break_not_an_oversold_signal(self):
        self.d['stocks']['sz000001'][-2]['preclose']-=20
        r=self.run_screen()['rows'][0];self.assertFalse(r['selected']);self.assertTrue(r['price_basis_unresolved'])
    def test_no_name_specific_rule(self):
        before=self.run_screen()['selected_symbols'];self.u['stocks'][0]['name']='任意';self.sync();self.assertEqual(self.run_screen()['selected_symbols'],before)


class Intraday(unittest.TestCase):
    def setUp(self):self.args=sim_fixture()
    def sim(self):return simulate(*self.args)
    def test_next_open_after_completed_signal(self):
        r=self.sim();self.assertEqual(r['status'],'先目标');self.assertEqual(r['entry_time'],'2026-10-09T09:45:00+08:00');self.assertEqual(r['entry_price'],98);self.assertFalse(r['full_context_confirmed'])
    def test_same_bar_unknown(self):self.assertEqual(label_path([dict(open=100,high=112,low=90,end='x')],110,93)[0],'同根双触_先后未知')
    def test_gap_uses_open(self):self.assertEqual(label_path([dict(open=80,high=100,low=80,end='x')],110,93),('先失效','x',80))
    def test_panic_required(self):
        self.args[0]['features']['close']=90;self.assertEqual(self.sim()['status'],'未触发')
    def test_market_confirmation(self):
        for b in self.args[3]:b.update(open=100,high=101,low=99,close=100,amount=100000)
        self.assertEqual(self.sim()['status'],'未触发')
    def test_pressure_confirmation(self):
        for b in self.args[4]:b.update(open=100,high=101,low=99,close=100,amount=100000)
        self.assertEqual(self.sim()['status'],'未触发')
    def test_gap_changes_risk_reward(self):
        self.args[2][3].update(open=109,high=110,low=108,close=109,amount=109000);r=self.sim()
        self.assertGreaterEqual(r['space_rejected'],1);self.assertNotEqual(r.get('entry_price'),109)
        self.assertIn('空间不足',r['traces'][0]['entry_decision'])
    def test_one_price_not_assumed_filled(self):
        self.args[2][3].update(open=98,high=98,low=98,close=98,amount=98000);self.assertEqual(self.sim()['status'],'入场成交未确认')
    def test_future_outcome_cannot_change_entry(self):
        a=self.sim();self.args[2][4].update(open=100,high=101,low=90,close=94,amount=94000);b=self.sim();self.assertEqual(b['status'],'先失效');self.assertEqual((a['entry_price'],a['signal_bar_end']),(b['entry_price'],b['signal_bar_end']))
    def test_no_peer_not_full_confirmation(self):
        self.args[5].clear();r=self.sim();self.assertIn('entry_price',r);self.assertFalse(r['coverage_sufficient'])
    def test_ambiguity_has_no_point_profit(self):
        self.args[2][3].update(high=111,low=90);r=self.sim();self.assertEqual(r['status'],'同根双触_先后未知');self.assertIsNone(r['price_path_net_pct'])
    def test_vwap_unit_mismatch(self):
        self.args[2][0]['amount']*=100
        with self.assertRaises(ValueError):self.sim()


class Outcomes(unittest.TestCase):
    def setUp(self):self.s,self.d,self.m=eval_fixture()
    def ev(self):return evaluate(self.s,self.d,self.m,DAY)
    def test_all_samples_t1_and_snapshot_unchanged(self):
        before=copy.deepcopy(self.s);r=self.ev();self.assertEqual(len(r['rows']),3);self.assertEqual(self.s,before);self.assertEqual(r['summary']['未入选对照']['n'],1);self.assertIn('t1_next_open_net_pct',r['rows'][0])
    def test_missing_minute_not_failure(self):
        self.m['stocks']['sz000001'].pop(4);self.assertEqual(self.ev()['rows'][0]['status'],'数据不足_不判成功失败')
    def test_cannot_skip_session(self):
        with self.assertRaises(ValueError):evaluate(self.s,self.d,self.m,NEXT)
    def test_snapshot_tamper(self):
        self.s['rows'][0]['selected']=False
        with self.assertRaises(ValueError):self.ev()
    def test_far_future_ignored(self):
        before=self.ev();self.m['stocks']['sz000001'].append(dict(date='2026-10-20',open='bad'));self.d['stocks']['sz000001'].append(dict(date='2026-10-20',open='bad'));self.assertEqual(self.ev(),before)
    def test_source_difference_kept(self):
        self.d['stocks']['sz000001'][0]['close']=106;self.assertTrue(self.ev()['rows'][0]['quality_flags'])
    def test_outcome_basis_break_not_success(self):
        self.d['stocks']['sz000001'][0]['preclose']=80
        r=self.ev()['rows'][0];self.assertEqual(r['status'],'价格口径未统一_不判成功失败');self.assertNotIn('entry_price',r)
    def test_t1_basis_break_not_false_loss(self):
        self.d['stocks']['sz000001'][1]['preclose']=70
        r=self.ev()['rows'][0];self.assertNotIn('t1_next_open_net_pct',r);self.assertIn('断点',r['t1_status'])
    def test_t1_one_price_unconfirmed(self):
        self.m['stocks']['sz000001'][48].update(open=100,high=100,low=100,close=100);r=self.ev()['rows'][0];self.assertNotIn('t1_next_open_net_pct',r);self.assertIn('未确认',r['t1_status'])
    def test_changed_frozen_rule_rejected(self):
        f=dict(rules=self.s['rules'],rules_sha256='wrong',universe_sha256=self.s['universe_sha256'],frozen_at='2026-10-18T01:00:00+00:00')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):replay(self.s['universe'],self.d,self.m,self.s['rules'],f,d)
    def test_late_freeze_rejected(self):
        f=dict(rules=self.s['rules'],rules_sha256=self.s['rules_sha256'],universe_sha256=self.s['universe_sha256'],frozen_at='2026-10-18T04:00:00+00:00')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):replay(self.s['universe'],self.d,self.m,self.s['rules'],f,d)


class Artifacts(unittest.TestCase):
    def test_immutable_atomic_save(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.json';save(p,{'a':1});save(p,{'a':1})
            with self.assertRaises(ValueError):save(p,{'a':2})
            self.assertEqual(read(p),{'a':1})
    def test_report_keeps_exclusions_and_escapes_html(self):
        s,d,m=eval_fixture();s['rows'][0]['name']='<script>alert(1)</script>';s=seal({k:v for k,v in s.items() if k!='content_sha256'});v=evaluate(s,d,m,DAY)
        with tempfile.TemporaryDirectory() as f:
            export(s,v,f);html=(Path(f)/'报告.html').read_text();self.assertIn('&lt;script&gt;',html);self.assertIn('未归类',html);self.assertEqual(len((Path(f)/'全部样本.csv').read_text(encoding='utf-8-sig').splitlines()),4)

if __name__=='__main__':unittest.main()
