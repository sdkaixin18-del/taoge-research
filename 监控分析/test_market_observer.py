import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from cb_limitup_monitor.domain import Quote, SH
from market_observer import Store, advance, parse_benchmarks, valid_stock


def quote(at, price='11', **extra):
    q = Quote('sh600001','合成股票','stock','tencent',at,at,Decimal(price),Decimal('10'),
              Decimal('1000'),Decimal('11000'),Decimal(price),Decimal('100'),None,Decimal(0),
              Decimal('11'),'tencent:field47',status='normal',open=Decimal('10'),
              high=Decimal('11'),low=Decimal('10'))
    return dataclasses.replace(q, **extra)


class ObserverTest(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,10,16,10,0,tzinfo=SH)

    def test_initial_is_not_true_first_seal(self):
        s,k=advance(None,quote(self.at),self.at)
        self.assertEqual(k,'INITIAL_AT_LIMIT')
        self.assertEqual(s['first_observed_kind'],'INITIAL_AT_LIMIT')

    def test_observed_first_touch_after_below(self):
        s,_=advance(None,quote(self.at,'10.8'),self.at)
        t=self.at+timedelta(seconds=30)
        s,k=advance(s,quote(t),t)
        self.assertEqual(k,'FIRST_OBSERVED_TOUCH')

    def test_two_samples_for_break_and_retouch(self):
        s,_=advance(None,quote(self.at),self.at)
        kinds=[]
        for i,p in enumerate(['10.9','10.8','11','11'],1):
            t=self.at+timedelta(seconds=i*30);s,k=advance(s,quote(t,p),t);kinds.append(k)
        self.assertEqual(kinds,[None,'BREAK_OBSERVED',None,'RETOUCH_OBSERVED'])
        self.assertEqual(s['observed_breaks'],1)
        self.assertEqual(s['observed_retouches'],1)

    def test_duplicate_source_does_not_confirm_break(self):
        s,_=advance(None,quote(self.at),self.at)
        t=self.at+timedelta(seconds=30);q=quote(t,'10.8')
        s,_=advance(s,q,t);s,k=advance(s,q,t+timedelta(seconds=2))
        self.assertIsNone(k);self.assertEqual(s['samples'],1)

    def test_gap_does_not_invent_transition(self):
        s,_=advance(None,quote(self.at),self.at)
        t=self.at+timedelta(minutes=5)
        s,k=advance(s,quote(t,'10.8'),t)
        self.assertIsNone(k);self.assertEqual(s['gaps'],1);self.assertEqual(s['observed_breaks'],0)

    def test_day_reset(self):
        s,_=advance(None,quote(self.at),self.at)
        t=self.at+timedelta(days=3)
        s,k=advance(s,quote(t),t)
        self.assertEqual(k,'INITIAL_AT_LIMIT');self.assertEqual(s['day'],'2026-10-19')

    def test_day_low_before_seal_does_not_mean_new_break(self):
        s,_=advance(None,quote(self.at),self.at)
        t=self.at+timedelta(seconds=30)
        s,k=advance(s,quote(t,low=Decimal('9')),t)
        self.assertIsNone(k);self.assertEqual(s['observed_breaks'],0)

    def test_active_stale_quote_is_blocked(self):
        self.assertFalse(valid_stock(quote(self.at),self.at+timedelta(minutes=5),True,'2026-10-16'))

    def test_closed_snapshot_has_date(self):
        self.assertTrue(valid_stock(quote(self.at),self.at+timedelta(days=2),False,'2026-10-16'))
        self.assertFalse(valid_stock(quote(self.at),self.at+timedelta(days=2),False,'2026-10-18'))

    def test_no_guessed_limit(self):
        self.assertFalse(valid_stock(quote(self.at,upper_limit=None),self.at,True,'2026-10-16'))

    def test_no_auction_quote_in_continuous_state(self):
        t=self.at.replace(hour=9,minute=29,second=30)
        self.assertFalse(valid_stock(quote(t),t+timedelta(seconds=40),True,'2026-10-16'))

    def test_closed_ingest_has_no_fake_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));s.ingest({'sh600001':quote(self.at)},'stock',self.at,False)
            self.assertEqual(s.db.execute('select count(*) from events').fetchone()[0],0)
            self.assertEqual(s.db.execute('select count(*) from states').fetchone()[0],0)
            self.assertEqual(s.db.execute('select count(*) from quotes').fetchone()[0],1)
            s.close()

    def test_no_outbox_and_readonly_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));tables={r[0] for r in s.db.execute("select name from sqlite_master where type='table'")}
            self.assertNotIn('outbox',tables);s.close()
            ro=Store(Path(tmp),readonly=True)
            with self.assertRaises(Exception):ro.db.execute("insert into kv values('x','{}')")
            ro.close()

    def test_active_event_persists_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));q=quote(self.at)
            s.ingest({q.symbol:q},'stock',self.at,True);s.ingest({q.symbol:q},'stock',self.at,True)
            self.assertEqual(s.db.execute('select count(*) from events').fetchone()[0],1)
            event=json.loads(s.db.execute('select payload from events').fetchone()[0])
            self.assertEqual(event['notifications_sent'],0);s.close()

    def test_old_source_does_not_overwrite_new_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp));new=quote(self.at+timedelta(seconds=30))
            s.ingest({new.symbol:new},'stock',new.received_at,True)
            old=quote(self.at,'10.8');s.ingest({old.symbol:old},'stock',new.received_at,True)
            result=json.loads(s.db.execute('select payload from quotes').fetchone()[0])
            self.assertEqual(result['last'],'11');s.close()

    def test_index_price_adapter(self):
        f=['']*88
        for i,v in {0:'1',1:'上证指数',2:'000001',3:'3716.28',4:'3707.48',5:'3723.80',30:'20261016150000',33:'3750.77',34:'3681.28'}.items():f[i]=v
        result=parse_benchmarks('v_sh000001="'+'~'.join(f)+'";',self.at.replace(hour=16))
        self.assertEqual(result['sh000001']['kind'],'benchmark')
        self.assertNotIn('volume',result['sh000001'])
        f[2]='600001'
        self.assertEqual(parse_benchmarks('v_sh000001="'+'~'.join(f)+'";',self.at.replace(hour=16)),{})


if __name__=='__main__':unittest.main()
