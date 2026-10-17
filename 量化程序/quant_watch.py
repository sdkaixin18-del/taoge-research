#!/usr/bin/env python3
"""Read-only cross-instrument bounce research. Standard library; no account API."""
import argparse
import hashlib
import json
import math
import re
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
VERSION = '0.1-research'
PARAMS = dict(source_lookback=12, min_bars=6, source_drawdown=.02,
              source_rebound_atr=.5, support_buffer_atr=.1,
              min_net_reward_risk=1.5, min_relation_days=20)


def dt(value):
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise ValueError('时间必须带时区，例如 2026-10-09T11:30:00+08:00')
    return t.astimezone(TZ)


def regular(t):
    hm = t.strftime('%H:%M')
    return t.weekday() < 5 and ('09:30' < hm <= '11:30' or '13:00' < hm <= '15:00')


def validate(rows):
    seen = set()
    for r in rows:
        t = dt(r['end'])
        if t in seen:
            raise ValueError('重复K线完成时间')
        seen.add(t)
        o, h, l, c = (float(r[k]) for k in ('open', 'high', 'low', 'close'))
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            raise ValueError('价格必须为有限正数')
        if not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError('OHLC范围异常')
    return sorted(rows, key=lambda r: dt(r['end']))


def visible(rows, asof, today=False, a_session=False):
    return [r for r in rows if dt(r['end']) <= asof
            and (not today or dt(r['end']).date() == asof.date())
            and (not a_session or regular(dt(r['end'])))]


def atr(rows, n=14):
    tr = []
    for i, r in enumerate(rows):
        prev = rows[i-1]['close'] if i else r['open']
        tr.append(max(r['high']-r['low'], abs(r['high']-prev), abs(r['low']-prev)))
    return statistics.mean(tr[-n:]) if tr else 0


def pearson(x, y):
    if len(x) < 3:
        return None
    a, b = statistics.mean(x), statistics.mean(y)
    vx = sum((v-a)**2 for v in x)
    vy = sum((v-b)**2 for v in y)
    if vx == 0 or vy == 0:
        return None
    return sum((v-a)*(w-b) for v, w in zip(x, y)) / math.sqrt(vx*vy)


def interval_returns(rows, asof):
    v = visible(rows, asof, a_session=True)
    out = {}
    for a, b in zip(v, v[1:]):
        # Exclude lunch, overnight and gaps, rather than forward-fill stale stock quotes.
        if dt(b['end'])-dt(a['end']) == timedelta(minutes=15):
            out[dt(b['end'])] = math.log(b['close']/a['close'])
    return out


def relation(source, trade, asof):
    s, t = interval_returns(source, asof), interval_returns(trade, asof)
    keys = sorted(set(s) & set(t))
    lag = {}
    for n in (0, 1, 2):
        pairs = [(s[k], t[k+timedelta(minutes=15*n)]) for k in keys
                 if k+timedelta(minutes=15*n) in t
                 and all(k+timedelta(minutes=15*j) in t for j in range(n+1))]
        lag[str(n)] = dict(n=len(pairs), correlation=pearson(
            [v[0] for v in pairs], [v[1] for v in pairs]))
    days = len({k.date() for k in keys})
    return dict(status='未验证', days=days, intervals=len(keys), lag_15m=lag,
                first_interval=keys[0].isoformat() if keys else None,
                last_interval=keys[-1].isoformat() if keys else None,
                sufficient_history=days >= PARAMS['min_relation_days'],
                note='相关性仅作诊断；尚未完成独立样本的成本后策略检验，不能证明领先。')


def make_plan(trade, cost_bps):
    price, vol = trade[-1]['close'], atr(trade)
    stop = min(r['low'] for r in trade[-2:]) - PARAMS['support_buffer_atr']*vol
    if not 0 < stop < price:
        raise ValueError('风险边界无效，暂停价格方案计算')
    # Only a pivot with its right-hand confirmation bar already closed is available.
    pivots = [b['high'] for a, b, c in zip(trade, trade[1:], trade[2:])
              if b['high'] > a['high'] and b['high'] >= c['high']]
    levels = sorted({p for p in pivots+[trade[0]['open']] if p > price})
    result = dict(reference_price=price, reference_time=trade[-1]['end'], atr=vol,
                  invalidation=stop, targets=levels[:2], stop_basis='last_price',
                  gross_risk_pct=(1-stop/price)*100, cost_bps=cost_bps,
                  net_reward_risk=None, max_entry_price=None, passes=False)
    if levels:
        result['gross_target_pct'] = (levels[0]/price-1)*100
    if levels and cost_bps is not None:
        cost = cost_bps/10000
        reward = levels[0]/price-1-cost
        risk = 1-stop/price+cost
        rr = reward/risk if risk > 0 else None
        result.update(net_target_pct=reward*100, net_reward_risk=rr,
                      max_entry_price=(levels[0]+PARAMS['min_net_reward_risk']*stop)
                      / ((1+PARAMS['min_net_reward_risk'])*(1+cost)),
                      passes=rr is not None and rr >= PARAMS['min_net_reward_risk'])
    return result


def freshness(rows, asof, minutes):
    return bool(rows) and asof-dt(rows[-1]['end']) <= timedelta(minutes=minutes)


def position_review(position, trade, source, asof):
    if not position:
        return None
    if position.get('stop_basis', 'last_price') != 'last_price':
        raise ValueError('当前持仓回顾仅支持成交价边界；标记价止损须另接标记价分钟数据')
    entry_time = dt(position['entry_time'])
    if entry_time > asof:
        raise ValueError('持仓入场时间晚于观察时间')
    # Never count highs/lows from the unobservable part before an intrabar entry.
    after = [r for r in trade if dt(r['end'])-timedelta(minutes=15) >= entry_time]
    signals = []
    stop = float(position['stop'])
    if any(r['low'] <= stop for r in after):
        signals.append('交易标的止损边界曾被触及，核查退出')
    targets = [float(t) for t in position.get('targets', [])]
    for t in targets:
        if any(r['high'] >= t for r in after):
            signals.append(f'目标 {t:g} 曾被触及，核查分批兑现')
    anchor = position.get('source_low')
    if anchor and any(r['low'] < anchor for r in source
                      if dt(r['end'])-timedelta(minutes=5) >= entry_time):
        signals.append('观察标的跌破入场时固定的压力低点，原判断失败')
    if len(after) >= 3:
        early = after[:3]
        initial_atr = float(position.get('entry_atr', 0))
        if initial_atr > 0 and max(r['close'] for r in early) < position['entry_price']+.5*initial_atr:
            signals.append('入场后3根15分钟收盘未修复0.5个入场ATR，时间退出条件触发')
    if asof.strftime('%H:%M') >= position.get('exit_by', '14:55'):
        signals.append('已到约定的当日退出时间')
    return dict(signals=signals, note='触及记录不等于成交；同根同时触及目标和止损时无法判定先后。'
                '止损与目标沿用入场记录，不按新低向下移动。')


def evaluate(data, asof, cost_bps=None, position=None):
    if cost_bps is not None and (not math.isfinite(cost_bps) or cost_bps < 0):
        raise ValueError('往返成本基点必须为非负有限数')
    data = {**data, **{key: validate(data[key]) for key in ('source_fast', 'source_slow', 'trade')},
            'basket': {name: validate(rows) for name, rows in data['basket'].items()}}
    fast = visible(data['source_fast'], asof, today=True, a_session=True)
    tr = [r for r in visible(data['trade'], asof, today=True)
          if dt(r['end']).strftime('%H:%M') > '09:30']
    baskets = {s: visible(rows, asof, today=True, a_session=True)
               for s, rows in data['basket'].items()}
    result = dict(version=VERSION, research_only=True, observe=data['observe'],
                  trade=data['trade_symbol'], asof=asof.isoformat(), params=PARAMS,
                  relation=relation(data['source_slow'], data['trade'], asof),
                  basket_relation={name:relation(rows, data['trade'], asof)
                                   for name, rows in data['basket'].items()},
                  decision='继续观察', technical_state='数据不足', reasons=[],
                  position=position_review(position, tr, fast, asof))
    errors = []
    if len(fast) < PARAMS['min_bars'] or len(tr) < PARAMS['min_bars']:
        errors.append('当日完整K线不足6根')
    if not baskets or any(len(rows) < 2 for rows in baskets.values()):
        errors.append('篮子缺少至少2根完整15分钟K线')
    if not freshness(fast, asof, 5) or not freshness(tr, asof, 15):
        errors.append('观察或交易行情过期，不能把旧价当作当前价')
    if any(dt(b['end'])-dt(a['end']) != timedelta(minutes=15) for a, b in zip(tr, tr[1:])):
        errors.append('交易标的日内K线有缺口，不能重建风险边界')
    if any(not freshness(rows, asof, 15) for rows in baskets.values()):
        errors.append('篮子报价过期或午休冻结')
    for name, rows in baskets.items():
        expected = {dt(r['end']) for r in fast if dt(r['end']).minute % 15 == 0}
        if expected - {dt(r['end']) for r in rows}:
            errors.append(f'篮子 {name} 的15分钟K线有缺口')
    if not regular(asof) or asof.strftime('%H:%M') >= '14:55':
        errors.append('不在A股有效信号窗口，暂停新增候选')
    # Require the source session to start at 09:35. Gaps invalidate the pressure low.
    if fast:
        expected = []
        t = asof.replace(hour=9, minute=35, second=0, microsecond=0)
        while t <= asof:
            if regular(t):
                expected.append(t)
            t += timedelta(minutes=5)
        actual = {dt(r['end']) for r in fast}
        if any(t not in actual for t in expected):
            errors.append('观察标的当日分钟缺口，无法可靠重建压力区')
    if errors:
        result.update(decision='暂停新增候选', reasons=errors)
        return result
    window = fast[-PARAMS['source_lookback']:]
    floor = min(r['low'] for r in window)
    vol = atr(fast)
    high = max(r['high'] for r in fast)
    pressure = (high-floor)/high >= PARAMS['source_drawdown']
    stable = (fast[-1]['close']-floor >= PARAMS['source_rebound_atr']*vol
              and min(r['low'] for r in fast[-3:]) > floor+PARAMS['support_buffer_atr']*vol)
    bdetail = {}
    for name, rows in baskets.items():
        prior_low = min(r['low'] for r in rows[:-1])
        bdetail[name] = dict(up=rows[-1]['close'] > rows[-2]['close'],
                             no_new_low=rows[-1]['low'] >= prior_low,
                             close=rows[-1]['close'], end=rows[-1]['end'])
    count = sum(v['up'] and v['no_new_low'] for v in bdetail.values())
    follows = (tr[-1]['close'] > tr[-2]['close']
               and tr[-1]['low'] >= min(r['low'] for r in tr[-4:-1]))
    plan = make_plan(tr, cost_bps)
    result.update(source=dict(pressure=pressure, stable=stable, low=floor, atr=vol,
                              rebound_atr=(fast[-1]['close']-floor)/vol if vol else None),
                  basket=bdetail, basket_confirmed=count, execution_follows=follows, plan=plan)
    if not pressure:
        result['reasons'].append('未出现研究定义的明显压力，不能套用恐慌反抽')
    elif not stable:
        result['technical_state'] = '压力仍在'
    elif not follows or count == 0:
        result['technical_state'] = '观察标的缓和，篮子或交易工具未响应'
    else:
        result['technical_state'] = '确认候选' if count == len(bdetail) else '试错候选'
        result['decision'] = result['technical_state']+'，仅供研究'
        if not plan['targets']:
            result.update(decision='缺少事前目标，暂不参与')
        elif cost_bps is None:
            result.update(decision='成本未知，暂不形成入场候选')
        elif not plan['passes']:
            result.update(decision='第一目标的净收益风险比不足，放弃追入')
    result['reasons'].append('跨标的关系及成本后策略尚未独立验证，技术候选不等于可执行买点')
    return result


def from_case(path, observe, trade):
    if observe != 'sh688498' or trade != 'KSTRUSDT':
        raise ValueError('离线案例仅对应源杰科技 sh688498 → KSTRUSDT')
    x = json.loads((path/'source_signal_alignment.json').read_text())
    def convert(rows):
        return [dict(end=r['close_time_bj'], **{k:float(r[k]) for k in ('open','high','low','close')}) for r in rows]
    return dict(observe=observe, trade_symbol=trade,
                source_fast=convert(x['source_688498_5m']),
                source_slow=convert(x['source_688498_15m']),
                basket={'sh588000':convert(x['star50_etf_588000_15m']),
                        'sh688256':convert(x['cambricon_688256_15m'])},
                trade=convert(x['KSTRUSDT_15m']))


def get_json(url, cache):
    req = urllib.request.Request(url, headers={'User-Agent':'taoge-research/0.1'})
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read()
    value = json.loads(raw)
    name = hashlib.sha256(url.encode()).hexdigest()[:16]+'.json'
    cache.mkdir(parents=True, exist_ok=True)
    (cache/name).write_bytes(raw)
    record = dict(url=url, sha256=hashlib.sha256(raw).hexdigest(), file=name,
                  fetched_at=datetime.now(TZ).isoformat())
    with (cache/'sources.jsonl').open('a') as f:
        f.write(json.dumps(record, ensure_ascii=False)+'\n')
    return value


def fetch_qq(symbol, interval, cache):
    if not re.fullmatch(r'(sh|sz|bj)\d{6}', symbol):
        raise ValueError('A股代码使用 sh688498 或 sz002428 格式')
    params = urllib.parse.urlencode({'param':f'{symbol},{interval},,320'})
    x = get_json('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline?'+params, cache)
    rows = x.get('data', {}).get(symbol, {}).get(interval, [])
    if not rows:
        raise ValueError(f'{symbol} {interval} 分钟数据缺失')
    return validate([dict(end=datetime.strptime(r[0], '%Y%m%d%H%M').replace(tzinfo=TZ).isoformat(),
                          open=float(r[1]), close=float(r[2]), high=float(r[3]), low=float(r[4])) for r in rows])


def fetch_binance(symbol, asof, cache):
    if not re.fullmatch(r'[A-Z0-9]{4,25}', symbol):
        raise ValueError('交易标的使用 Binance USDT 永续代码，例如 KSTRUSDT')
    rows, end = [], int(asof.timestamp()*1000)-1
    for _ in range(3):
        query = urllib.parse.urlencode(dict(symbol=symbol, interval='15m', endTime=end, limit=1500))
        x = get_json('https://fapi.binance.com/fapi/v1/klines?'+query, cache)
        if not isinstance(x, list) or not x:
            raise ValueError('Binance返回的K线为空或格式异常')
        rows = x+rows
        end = int(x[0][0])-1
        if len(x) < 1500:
            break
    return validate([dict(end=datetime.fromtimestamp((int(r[6])+1)/1000, TZ).isoformat(),
                          open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4])) for r in rows])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--observe', required=True)
    p.add_argument('--trade', required=True)
    p.add_argument('--as-of', help='带时区ISO时间；默认现在。历史时点需有当时数据。')
    p.add_argument('--basket', nargs='+')
    p.add_argument('--cost-bps', type=float, help='往返总成本情景；1基点=0.01%%，没有默认实际费率')
    p.add_argument('--offline-case', type=Path)
    p.add_argument('--data', type=Path, help='标准格式JSON，适配其他市场时使用')
    p.add_argument('--position', type=Path, help='已持仓JSON，固定入场时的止损与目标')
    p.add_argument('--out', type=Path)
    args = p.parse_args()
    asof = dt(args.as_of) if args.as_of else datetime.now(TZ)
    try:
        if args.data:
            data = json.loads(args.data.read_text())
            if data['observe'] != args.observe or data['trade_symbol'] != args.trade:
                raise ValueError('输入代码与数据身份不一致')
        elif args.offline_case:
            data = from_case(args.offline_case, args.observe, args.trade)
        else:
            basket = args.basket or (['sh588000', 'sh688256'] if args.trade == 'KSTRUSDT' else [])
            if not basket:
                raise ValueError('新配对需指定 --basket；不能因同属科技就自动认定联动')
            cache = (Path(__file__).resolve().parents[1]/'课程视频'/'跨标的反抽工具'
                     /'行情缓存'/datetime.now(TZ).strftime('%Y%m%d_%H%M%S'))
            data = dict(observe=args.observe, trade_symbol=args.trade,
                        source_fast=fetch_qq(args.observe, 'm5', cache),
                        source_slow=fetch_qq(args.observe, 'm15', cache),
                        basket={s:fetch_qq(s, 'm15', cache) for s in basket},
                        trade=fetch_binance(args.trade, asof, cache))
        position = json.loads(args.position.read_text()) if args.position else None
        result = evaluate(data, asof, args.cost_bps, position)
    except (ValueError, KeyError, OSError, TypeError) as e:
        result = dict(version=VERSION, research_only=True, decision='数据异常，暂停评估', error=str(e))
    body = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(body+'\n')
    print(body)


if __name__ == '__main__':
    main()
