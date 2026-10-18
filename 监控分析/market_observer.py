"""Independent silent market data collector. Uses the installed monitor's adapters.

No news fetching, model API, outbox, webhook, or notification imports.
"""
import argparse
import asyncio
import dataclasses
import contextlib
import fcntl
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from cb_limitup_monitor.archive import RawArchive
from cb_limitup_monitor.calendar import TradingCalendar
from cb_limitup_monitor.domain import SH, Quote, dumps, now_utc
from cb_limitup_monitor.market import MarketClient
from cb_limitup_monitor.reference import ReferenceStore
from cb_limitup_monitor.stock_topics import choose_topics

BENCHMARKS = {'sh000001': '上证指数', 'sz399001': '深证成指',
              'sz399006': '创业板指', 'sh000832': '中证转债指数', 'sh511380': '转债ETF'}
VERSION = 'silent-market-0.1'
CALENDAR = TradingCalendar()


@contextlib.contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def parse_benchmarks(body, received):
    """Price-only index/fund adapter; their volume units are not assumed."""
    out = {}
    for code, payload in re.findall(r'v_([a-z]+\d+)="([^"\n]*)";', body):
        f = payload.split('~')
        if code not in BENCHMARKS or len(f) < 35 or f[2] != code[2:]:
            continue
        try:
            values = [Decimal(f[k]) for k in (3, 4, 5, 33, 34)]
            if not all(v.is_finite() and v > 0 for v in values):
                continue
            last, previous, opening, high, low = values
            if not low <= last <= high:
                continue
            source_at = datetime.strptime(f[30], '%Y%m%d%H%M%S').replace(tzinfo=SH)
            if source_at > received + timedelta(seconds=2):
                continue
            out[code] = dict(symbol=code, name=f[1], kind='benchmark', source='tencent',
                             last=str(last), previous_close=str(previous), open=str(opening),
                             high=str(high), low=str(low), source_at=source_at.isoformat(),
                             received_at=received.isoformat())
        except (ValueError, ArithmeticError):
            continue
    return out


def valid_stock(q, at, active, day):
    if not (q.kind == 'stock' and q.source == 'tencent' and q.last > 0 and
            q.previous_close > 0 and q.volume > 0 and q.upper_limit and
            q.limit_source == 'tencent:field47' and q.previous_close < q.upper_limit <=
            q.previous_close * Decimal('1.35') and q.last <= q.upper_limit and
            q.source_at.astimezone(SH).date().isoformat() == day and
            q.source_at <= at + timedelta(seconds=2)):
        return False
    return not active or (q.usable(at, 90) and CALENDAR.active(q.source_at))


def advance(old, q, at):
    """Only call for fresh continuous-session quotes. Counts observed transitions."""
    day = at.astimezone(SH).date().isoformat()
    s = dict(old) if old and old['day'] == day else None
    if s and q.source_at <= datetime.fromisoformat(s['last_source_at']):
        return s, None
    phase = 'at_limit' if q.last == q.upper_limit else 'below'
    gap = bool(s and ((at - datetime.fromisoformat(s['last_observed_at'])).total_seconds() > 180
                     or TradingCalendar.segment(at) != TradingCalendar.segment(
                         datetime.fromisoformat(s['last_observed_at']))))
    kind = None
    if s is None:
        s = dict(day=day, symbol=q.symbol, phase=phase, first_observed_at=None,
                 first_observed_kind=None, stable_since=None, observed_breaks=0,
                 observed_retouches=0, sequence=0, gaps=0, candidate=None, samples=0)
        if phase == 'at_limit':
            kind = 'INITIAL_AT_LIMIT'
    elif gap:
        s.update(phase=phase, stable_since=None, candidate=None, samples=0, gaps=s['gaps'] + 1)
        if phase == 'at_limit':
            kind = 'RESUME_AT_LIMIT'
    elif phase != s['phase']:
        s['samples'] = s['samples'] + 1 if s['candidate'] == phase else 1
        s['candidate'] = phase
        if phase == 'at_limit' and s['first_observed_at'] is None:
            kind = 'FIRST_OBSERVED_TOUCH'
        elif s['samples'] >= 2:
            kind = 'RETOUCH_OBSERVED' if phase == 'at_limit' else 'BREAK_OBSERVED'
        if kind:
            s.update(phase=phase, candidate=None, samples=0)
    else:
        s.update(candidate=None, samples=0)
    if kind and phase == 'at_limit':
        if s['first_observed_at'] is None:
            s.update(first_observed_at=at.isoformat(), first_observed_kind=kind)
        s['stable_since'] = at.isoformat()
    if kind == 'BREAK_OBSERVED':
        s['observed_breaks'] += 1
        s['stable_since'] = None
    if kind == 'RETOUCH_OBSERVED':
        s['observed_retouches'] += 1
    s.update(last_source_at=q.source_at.isoformat(), last_observed_at=at.isoformat())
    if kind:
        s['sequence'] += 1
    return s, kind


class Store:
    def __init__(self, root, readonly=False):
        path = root / 'market.sqlite3'
        if readonly:
            self.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        else:
            root.mkdir(parents=True, exist_ok=True)
            root.chmod(0o700)
            self.db = sqlite3.connect(path, timeout=5)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY,role TEXT,payload TEXT);
              CREATE TABLE IF NOT EXISTS states(day TEXT,symbol TEXT,payload TEXT,PRIMARY KEY(day,symbol));
              CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,day TEXT,symbol TEXT,observed_at TEXT,payload TEXT);
              CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,payload TEXT);
            ''')
            self.db.commit()
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, dumps(value)))

    def get(self, key, default=None):
        row = self.db.execute('SELECT payload FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def ingest(self, quotes, role, at, active):
        with self.db:
            for q in quotes.values():
                payload = q if isinstance(q, dict) else dataclasses.asdict(q)
                code = payload['symbol']
                previous = self.db.execute('SELECT payload FROM quotes WHERE symbol=?', (code,)).fetchone()
                if previous and datetime.fromisoformat(json.loads(previous[0])['source_at']) > (
                    datetime.fromisoformat(payload['source_at']) if isinstance(payload['source_at'], str)
                    else payload['source_at']):
                    continue
                self.db.execute('INSERT OR REPLACE INTO quotes VALUES (?,?,?)', (code, role, dumps(payload)))
                day = at.astimezone(SH).date().isoformat()
                if role != 'stock' or not active or not valid_stock(q, at, True, day):
                    continue
                row = self.db.execute('SELECT payload FROM states WHERE day=? AND symbol=?', (day, code)).fetchone()
                state, kind = advance(json.loads(row[0]) if row else None, q, at)
                self.db.execute('INSERT OR REPLACE INTO states VALUES (?,?,?)', (day, code, dumps(state)))
                if kind:
                    event = dict(symbol=code, kind=kind, state=state, quote=q,
                                 observed_at=at, source_at=q.source_at, notifications_sent=0)
                    self.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)',
                                    (f'{day}:{code}:{state["sequence"]}', day, code, at.isoformat(), dumps(event)))


def references(root):
    ref = ReferenceStore(root, read_only=True)
    try:
        stocks = ref.get('oneword_stocks', {})
        if len(stocks.get('stocks', {})) < 4000:
            raise ValueError('full_stock_reference_missing_or_incomplete')
        if datetime.fromisoformat(stocks['captured_at']) > now_utc() + timedelta(seconds=2):
            raise ValueError('future_reference')
        links = ref.load_universe(now_utc().astimezone(SH).date(), allow_stale=True)
        return stocks, links
    finally:
        ref.close()


def snapshot(store, reference_root):
    at = now_utc()
    calendar = TradingCalendar()
    stocks, links = references(reference_root)
    records = {r['symbol']: (r['role'], json.loads(r['payload']))
               for r in store.db.execute('SELECT * FROM quotes')}
    stock_quotes = {s: Quote.from_dict(q) for s, (role, q) in records.items() if role == 'stock'}
    day = max((q.source_at.astimezone(SH).date().isoformat() for q in stock_quotes.values()), default=None)
    active = calendar.active(at)
    valid = {s: q for s, q in stock_quotes.items() if valid_stock(q, at, active, day)}
    current = sorted(s for s, q in valid.items() if q.last == q.upper_limit)
    touched = sorted(s for s, q in valid.items() if q.high == q.upper_limit)
    selected = set(touched) | {x.stock for x in links} | set(current)
    states = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM states WHERE day=?', (day,))]
    events = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM events WHERE day=? ORDER BY observed_at DESC LIMIT 100', (day,))]
    ref = ReferenceStore(reference_root, read_only=True)
    try:
        topic_records = ref.topic_records()
    finally:
        ref.close()
    topics = {s: choose_topics(topic_records.get(s, {}), q.name, at) for s, q in valid.items() if s in selected}
    return dict(schema='cb-research-snapshot-v2', version=VERSION, generated_at=at,
                market_active=active, trading_day=calendar.trading_day(at.astimezone(SH).date()),
                quote_day=day, mode=('live_observation' if len(valid)>=len(stocks['stocks'])*.98 else 'active_data_incomplete') if active else 'last_session_snapshot',
                reference_captured_at=stocks['captured_at'],
                coverage=dict(expected=len(stocks['stocks']), returned=len(stock_quotes),
                              usable_limit_prices=len(valid), current_limit_count=len(current),
                              daily_high_touched_count=len(touched),
                              actual_first_seal_times_known=False),
                current_limits=current, daily_high_touched=touched,
                stock_quotes={s: q for s, q in stock_quotes.items() if s in selected},
                bond_quotes={s:q for s,(role,q) in records.items() if role == 'bond'},
                benchmarks={s:q for s,(role,q) in records.items() if role == 'benchmark'},
                bond_links=links, topics=topics, observed_states=[s for s in states if s['symbol'] in selected], observed_events=events, events_limit=100,
                collector=store.get('health', {}), notifications_sent=0, news_stored=False,
                precision='polling observations; daily high does not establish first seal or break counts')


async def run(args):
    root = args.data_dir.expanduser()
    store = Store(root)
    archive = RawArchive(root / 'raw')
    calendar = TradingCalendar()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=4)) as http:
            market = MarketClient(http, archive, 100)
            full_at, watch_at, last_cleanup = 0., 0., None
            watched = set()
            while True:
                at = now_utc()
                active = calendar.active(at)
                if not active and not (args.once and args.allow_closed):
                    with store.db:
                        store.put('health', dict(updated_at=at, session='closed', notifications_sent=0))
                    if args.once:
                        return
                    await asyncio.sleep(30)
                    continue
                try:
                    stocks, links = references(args.reference_root.expanduser())
                    full = args.once or time.monotonic() >= full_at
                    if full or time.monotonic() >= watch_at:
                        codes = set(stocks['stocks']) if full else watched | {x.stock for x in links}
                        async def on_batch(batch):
                            now = now_utc()
                            store.ingest(batch, 'stock', now, calendar.active(now))
                        quotes = await market.fetch({s:'stock' for s in codes}, 'tencent', on_batch=on_batch)
                        now = now_utc()
                        today = max((q.source_at.astimezone(SH).date().isoformat() for q in quotes.values()), default='')
                        valid = {s:q for s,q in quotes.items() if valid_stock(q, now, active, today)}
                        touched = {s for s,q in valid.items() if q.high == q.upper_limit or q.last == q.upper_limit}
                        near = {s for s,q in valid.items() if q.last >= q.upper_limit * Decimal('.98')}
                        if full:
                            watched = touched | near
                            full_at = time.monotonic() + args.full_seconds
                        else:
                            watched |= touched | near
                        watch_at = time.monotonic() + args.watch_seconds
                        if full:
                            bond_codes = {x.bond for x in links}
                        else:
                            bond_codes = {x.bond for x in links if x.stock in watched}
                        bonds = await market.fetch({s:'bond' for s in bond_codes}, 'tencent')
                        store.ingest(bonds, 'bond', now_utc(), active)
                        response = await http.get('https://qt.gtimg.cn/q=' + ','.join(BENCHMARKS))
                        received = now_utc()
                        ref = archive.save('tencent_benchmarks', response.content, received, symbols=list(BENCHMARKS), http_status=response.status_code)
                        response.raise_for_status()
                        benchmarks = parse_benchmarks(response.content.decode('gb18030'), received)
                        for q in benchmarks.values():q['raw_ref'] = ref
                        store.ingest(benchmarks, 'benchmark', received, active)
                        with store.db:
                            if full:
                                store.put('last_full_round', dict(at=received, expected=len(codes), returned=len(quotes),
                                                                 valid=len(valid), traffic=market.traffic_snapshot()))
                            store.put('health', dict(updated_at=received, session='active' if active else 'closed_snapshot',
                                current_round_full=full, watched=len(watched), benchmarks_returned=len(benchmarks),
                                full_round=store.get('last_full_round'), notifications_sent=0, error=None if len(benchmarks)==len(BENCHMARKS) else 'benchmark_coverage_incomplete'))
                        # Bound runtime storage; no writes to the existing monitor's data.
                        if last_cleanup != at.astimezone(SH).date():
                            cutoff = (at - timedelta(days=7)).astimezone(SH).date().isoformat()
                            with store.db:
                                store.db.execute('DELETE FROM states WHERE day<?', (cutoff,))
                                store.db.execute('DELETE FROM events WHERE day<?', (cutoff,))
                            for file in (root/'raw').glob('raw-*.jsonl.gz'):
                                if file.name[4:14] < cutoff:file.unlink()
                            last_cleanup = at.astimezone(SH).date()
                    if args.once:
                        print(dumps(snapshot(store, args.reference_root.expanduser())))
                        return
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    with store.db:
                        store.put('health', dict(updated_at=now_utc(), session='error', error=type(exc).__name__, notifications_sent=0))
                    if args.once:
                        raise
                    await asyncio.sleep(30)
                await asyncio.sleep(1)
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, default=Path('~/.local/share/cb-market-observer'))
    p.add_argument('--reference-root', type=Path, default=Path('~/.local/share/cb-limitup-monitor/metadata'))
    p.add_argument('--full-seconds', type=int, default=60)
    p.add_argument('--watch-seconds', type=int, default=30)
    p.add_argument('--once', action='store_true')
    p.add_argument('--allow-closed', action='store_true')
    p.add_argument('--snapshot', action='store_true')
    args = p.parse_args()
    if args.full_seconds < 60 or args.watch_seconds < 15:
        p.error('full scan >=60s; watch scan >=15s')
    if args.allow_closed and not args.once:
        p.error('--allow-closed requires --once')
    if args.snapshot:
        store = Store(args.data_dir.expanduser(), readonly=True)
        try:print(dumps(snapshot(store, args.reference_root.expanduser())))
        finally:store.close()
    else:
        with process_lock(args.data_dir.expanduser()/'process.lock'):
            asyncio.run(run(args))


if __name__ == '__main__':main()
