"""Offline cross-source audit. TDX minute points are never promoted to OHLC bars.

Keep the original replay and protocol unchanged; output evidence and price paths,
not executable fills or a new strategy return series. Uses Python stdlib only.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path

from limitdown_research import (entry_signal, hhmm, limits, price, quality,
                                read_raw, write_csv)


def minute_labels():
    return [(datetime(2000, 1, 1, h, m) + timedelta(minutes=i)).strftime('%H:%M')
            for h, m in ((9, 31), (13, 1)) for i in range(120)]


def point_map(rows):
    if len(rows) != 240:
        raise ValueError('Expected 240 ordered minute points; no synthetic gap fills')
    return dict(zip(minute_labels(), rows))


def sampled_ticks_clock(rows, ticks):
    """Check inferred point labels against last sampled trade of prior minute.

    Closing-auction 15:00 belongs to the final point, not the 14:59 trade minute.
    Missing tick minutes are not backfilled or counted as price agreements.
    """
    grouped = defaultdict(list)
    for row in ticks:
        grouped[row['time']].append(row)
    records = []
    for label, point in point_map(rows).items():
        key = ('15:00' if label == '15:00' else
               (datetime.strptime(label, '%H:%M') - timedelta(minutes=1)).strftime('%H:%M'))
        trades = grouped[key]
        if trades:
            records.append({'point_end': label, 'trade_minute': key,
                            'point': point['price'], 'last_sample': trades[-1]['price'],
                            'same_cent': price(point['price']) == price(trades[-1]['price'])})
    return records


def checkpoints(bars, points):
    """Compare like-for-like closes, not sampled maxima to true interval highs."""
    return [{'time': hhmm(b), 'bs_close': float(b['close']),
             'tdx_point': float(points[hhmm(b)]['price']),
             'delta_cents': int((price(points[hhmm(b)]['price']) - price(b['close'])) * 100)}
            for b in bars]


def point_first_confirmation(bars, points, floor):
    # Same 5m end grid and deadline as branch A. No intraminute look-ahead.
    return next((hhmm(b) for b in bars if hhmm(b) <= '14:30'
                 and price(points[hhmm(b)]['price']) > floor), None)


def seal_feature(record):
    """Only exact, time-stamped remaining sell-queue inventory is numerical Q.

    Press lower bounds and at-limit traded amounts stay in a separate evidence
    ledger. Shares/lots are explicit. Reject observations unavailable as of D-1.
    """
    if (record.get('kind') != 'remaining_sell_queue' or record.get('relation') != 'eq'
            or not record.get('snapshot_time') or not record.get('available_at')
            or not record.get('prediction_cutoff')):
        return None
    try:
        snapshot = datetime.fromisoformat(record['snapshot_time'])
        available = datetime.fromisoformat(record['available_at'])
        cutoff = datetime.fromisoformat(record['prediction_cutoff'])
        if any(t.tzinfo is None for t in (snapshot, available, cutoff)):
            return None
        if snapshot > cutoff or available > cutoff or snapshot.date() != cutoff.date():
            return None
        value = float(record['quantity'])
    except (ValueError, TypeError, KeyError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    unit = record.get('unit')
    return value * {'shares': 1, 'lots_100_shares': 100}[unit] if unit in ('shares', 'lots_100_shares') else None


def run(study, check, out):
    frozen = (study / '研究协议_封存.json').read_bytes()
    digest = hashlib.sha256(frozen).hexdigest()
    if digest != (study / '研究协议_SHA256.txt').read_text().strip():
        raise ValueError('Original frozen protocol changed')
    original = json.loads((study / '回放结果/回放.json').read_text())
    protocol = json.loads(frozen)
    out.mkdir(parents=True, exist_ok=True)
    daily, minute = {}, {}
    for sym in protocol['universe']:
        daily[sym] = {r['date']: r for r in read_raw(study, sym, '日线')}
        minute[sym] = defaultdict(list)
        for r in read_raw(study, sym, '5分钟'):
            minute[sym][r['date']].append(r)
    audits, comparisons, clocks, point_data = [], [], [], {}
    for file in sorted((check / '通达信历史分时').glob('*.json')):
        raw = json.loads(file.read_text())
        sym, day, rows = raw['symbol'], raw['date'], raw['rows']
        points = point_map(rows)
        point_data[sym, day] = points
        d, bars = daily[sym][day], minute[sym][day]
        cp = checkpoints(bars, points)
        comparisons.extend({'symbol': sym, 'date': day, **r} for r in cp)
        total = sum(int(x['vol']) for x in rows) * 100
        audits.append({'symbol': sym, 'name': protocol['universe'][sym], 'date': day,
                       'tdx_points': len(rows), 'daily_close': float(d['close']),
                       'tdx_last_price': rows[-1]['price'],
                       'tdx_close_same_cent': price(rows[-1]['price']) == price(d['close']),
                       'daily_volume_shares': int(d['volume']), 'tdx_volume_shares_rounded': total,
                       'tdx_volume_delta_shares': total - int(d['volume']),
                       'bs_volume_delta_shares': round(sum(float(x['volume']) for x in bars) - int(d['volume'])),
                       'bs_tdx_different_cents': sum(r['delta_cents'] != 0 for r in cp),
                       'bs_tdx_over_one_cent': sum(abs(r['delta_cents']) > 1 for r in cp),
                       'bs_daily_issues': quality(d, bars)})
        ticks_file = check / '通达信历史分笔' / file.name
        if ticks_file.exists():
            ticks = json.loads(ticks_file.read_text())['rows']
            clocks.extend({'symbol': sym, 'date': day, **r}
                          for r in sampled_ticks_clock(rows, ticks))
    paths, branch_audits = [], []
    for episode in original['episodes']:
        sym, day, following = episode['symbol'], episode['opening_date'], episode['next_date']
        bars, points = minute[sym][day], point_data[sym, day]
        floor, _ = limits(daily[sym][day])
        native = point_first_confirmation(bars, points, floor)
        idx = entry_signal(bars, floor, 'A')
        bs = hhmm(bars[idx]) if idx is not None else None
        for branch in ('A', 'B'):
            idx = entry_signal(bars, floor, branch)
            t = hhmm(bars[idx]) if idx is not None else None
            rec = {'episode_id': episode['episode_id'], 'name': episode['name'],
                   'branch': branch, 'bs_confirm': t, 'tdx_A_confirm': native,
                   'bs_confirm_close': float(bars[idx]['close']) if idx is not None else None,
                   'tdx_confirm_point': points[t]['price'] if t else None,
                   'full_OHLC_verified': False, 'execution_verified': False}
            if branch == 'A':
                rec['first_confirmation_time_same'] = bs == native
            elif idx is not None and idx > 0:
                prev = hhmm(bars[idx-1])
                rec['tdx_two_close_conditions_hold'] = (
                    price(points[prev]['price']) > floor
                    and price(points[t]['price']) > price(points[prev]['price']))
                rec['low_condition_status'] = 'not_verifiable_from_minute_points'
            branch_audits.append(rec)
        if native:
            confirm = float(points[native]['price'])
            next_open = float(daily[sym][following]['open'])
            next_floor, _ = limits(daily[sym][following])
            paths.append({'episode_id': episode['episode_id'], 'symbol': sym,
                          'name': episode['name'], 'opening_date': day,
                          'eligible_price_proxy': episode['eligible_price_proxy'],
                          'confirm_time': native, 'confirm_point': confirm,
                          'opening_close': float(daily[sym][day]['close']),
                          'next_date': following, 'next_open': next_open,
                          'next_open_vs_confirm_pct': (next_open / confirm - 1) * 100,
                          'next_open_at_floor': price(next_open) <= next_floor,
                          'entry_or_exit_fill_assumed': False,
                          'interpretation': '确认点至次日开盘的价格路径；没有买入执行价，不是策略收益'})
    ledger_path = check / '历史封单证据.json'
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
    qualified = [r for r in ledger if seal_feature(r) is not None]
    summary = {'original_protocol_sha256': digest, 'stock_dates': len(audits),
               'tdx_daily_close_same_cent': sum(r['tdx_close_same_cent'] for r in audits),
               'tdx_max_abs_volume_delta_shares': max(abs(r['tdx_volume_delta_shares']) for r in audits),
               'checkpoint_pairs': len(comparisons),
               'checkpoint_different_cents': sum(r['delta_cents'] != 0 for r in comparisons),
               'checkpoint_over_one_cent': sum(abs(r['delta_cents']) > 1 for r in comparisons),
               'dates_with_over_one_cent': sum(r['bs_tdx_over_one_cent'] > 0 for r in audits),
               'clock_pairs': len(clocks), 'clock_exact_cent_matches': sum(r['same_cent'] for r in clocks),
               'A_confirmation_same_time': sum(r.get('first_confirmation_time_same', False) for r in branch_audits),
               'B_close_conditions_hold': sum(r.get('tdx_two_close_conditions_hold', False) for r in branch_audits),
               'B_full_low_conditions_verified': 0, 'verified_strategy_returns': 0,
               'seal_evidence_records': len(ledger), 'qualified_exact_seal_features': len(qualified),
               'note': 'Minute-end prices and sampled trades do not establish full OHLC, sell queues, or fills.'}
    for name, records in (('40日跨源核查', audits), ('逐检查点价格', comparisons),
                          ('分时时标验证', clocks), ('原A_B规则证据', branch_audits),
                          ('确认点至次日价格路径', paths), ('历史封单证据', ledger)):
        write_csv(out / f'{name}.csv', records)
    inputs = list((check / '通达信历史分时').glob('*.json')) + list((check / '通达信历史分笔').glob('*.json'))
    inputs += list((study / '原始行情').glob('*.json')) + [study / '回放结果/回放.json']
    if ledger_path.exists():
        inputs.append(ledger_path)
    payload = {'summary': summary, 'audits': audits, 'branch_audits': branch_audits, 'price_paths': paths,
               'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'inputs_sha256': {str(p.relative_to(study)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(inputs)}}
    (out / '复核.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return payload


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study-dir', type=Path, required=True)
    parser.add_argument('--check-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    run(args.study_dir, args.check_dir, args.out)
