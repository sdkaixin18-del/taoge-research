"""Point-in-time screening. Outcome candles never enter this module."""
from datetime import date, datetime, time, timedelta, timezone
import math
import statistics
from . import __version__
from .io import digest, seal

TZ = timezone(timedelta(hours=8))
MODES = ('强势回调', '超跌反抽')


def aware(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return value.astimezone(TZ)


def cutoff_date(value):
    return datetime.combine(date.fromisoformat(value), time(15), TZ)


def mean(values):
    return statistics.mean(values)


def price_bar(bar):
    for key in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        x = bar.get(key)
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError('缺失或无效字段:' + key)
        if x < 0 or (key not in ('volume', 'amount') and x == 0):
            raise ValueError('价格必须为正，量额不能为负')
    o, h, l, c = (bar[k] for k in ('open', 'high', 'low', 'close'))
    if not l <= min(o, c) <= max(o, c) <= h:
        raise ValueError('OHLC区间错误')
    if bar['volume'] > 0 and bar['amount'] <= 0:
        raise ValueError('有成交量但真实成交额为零')
    if 'preclose' in bar and (isinstance(bar['preclose'],bool) or not isinstance(bar['preclose'],(int,float))
            or not math.isfinite(bar['preclose']) or bar['preclose'] <= 0):
        raise ValueError('前收价格无效')


def history(rows, asof, sessions, count, *, reference=False):
    # Discard future rows before even validating them.
    rows = sorted((r for r in rows if r['date'] <= asof), key=lambda r:r['date'])
    dates = [r['date'] for r in rows]
    if len(dates) != len(set(dates)):
        raise ValueError('存在重复交易日')
    expected = sessions[-count:]
    bydate = {r['date']:r for r in rows}
    if len(expected) < count or not expected or expected[-1] != asof or any(d not in bydate for d in expected):
        raise ValueError(f'缺少当日行情或不足{count}个完整交易日')
    recent = [bydate[d] for d in expected]
    for b in recent:
        price_bar(b)
        if 'preclose' not in b:
            raise ValueError('日线缺少前收，不能假设没有公司行动')
        if not reference and (type(b.get('is_st')) is not bool or type(b.get('trading')) is not bool):
            raise ValueError('缺少历史ST或交易状态')
    if reference and recent[-1]['volume'] <= 0:
        raise ValueError('市场参照当日没有正常成交')
    return recent


def reviewed(stock, prefix, cutoff):
    state = stock.get(prefix + '_review', 'unknown')
    if state not in ('clear', 'adverse', 'unknown'):
        raise ValueError('核查状态必须为clear/adverse/unknown')
    when = stock.get(prefix + '_review_at')
    if state != 'unknown' and (not when or aware(when) > cutoff or not stock.get(prefix+'_review_source')):
        return 'unknown'
    return state


def check_rules(r):
    def finite(value):
        if isinstance(value,dict):
            for x in value.values(): finite(x)
        elif isinstance(value,list):
            for x in value: finite(x)
        elif isinstance(value,(float,int)) and not math.isfinite(value):
            raise ValueError('规则数字必须有限')
    finite(r)
    keys = {'schema_version','rule_id','description','history_sessions','activity_sessions',
            'trend_sessions','pullback_sessions','atr_sessions','benchmark_symbol',
            'hypothetical_position_yuan','max_daily_participation','strong_pullback',
            'oversold_bounce','entry','context_policy','price_basis','missing_data_policy','validation'}
    if set(r) != keys or r['schema_version'] != 2:
        raise ValueError('未知或缺失规则字段；只支持当前双模式规则')
    fixed = {'price_basis':'unadjusted','context_policy':'timestamped_evidence_required_for_ready',
             'missing_data_policy':'block_screen_if_reference_missing_otherwise_keep_stock_error'}
    if any(r[k] != v for k,v in fixed.items()):
        raise ValueError('未实现的价格或核查口径')
    windows = ('history_sessions','activity_sessions','trend_sessions','pullback_sessions','atr_sessions')
    if any(type(r[k]) is not int or r[k] <= 0 for k in windows):
        raise ValueError('窗口必须为正整数')
    if r['history_sessions'] < max(r['activity_sessions'],r['trend_sessions']+1,r['pullback_sessions']+1,r['atr_sessions']+1,11):
        raise ValueError('历史长度不足以计算配置窗口')
    if not (r['hypothetical_position_yuan'] > 0 and 0 < r['max_daily_participation'] <= 1):
        raise ValueError('成交规模参数错误')
    if r['strong_pullback'] != dict(require_positive_trend_return=True,
            require_nonnegative_trend_excess=True,require_negative_pullback_return=True):
        raise ValueError('不支持的强势回调定义')
    o = r['oversold_bounce']
    if set(o) != {'min_drawdown_atr','min_pullback_atr','exclude_strong_pullback'} or o['exclude_strong_pullback'] is not True:
        raise ValueError('不支持的超跌反抽定义')
    if not 0 < o['min_drawdown_atr'] < 20 or not 0 < o['min_pullback_atr'] < 20:
        raise ValueError('超跌参数错误')
    e = r['entry']
    expected = {'interval_minutes','earliest_bar_number','panic_atr','require_above_vwap',
        'require_break_prior_bar_high','higher_low_bars','peer_nonfalling_fraction',
        'minimum_peers_for_confirmation','min_reward_risk_after_cost','one_way_cost_bps',
        'stock_tick','last_entry_bar_number','max_entries_per_stock_day','target','stop',
        'execution','same_bar_policy'}
    if set(e) != expected:
        raise ValueError('未知或缺失盘中字段')
    constants = dict(interval_minutes=5,require_above_vwap=True,require_break_prior_bar_high=True,
        higher_low_bars=3,max_entries_per_stock_day=1,target='asof_close',
        stop='last_three_completed_bars_low_minus_tick',execution='next_bar_open',same_bar_policy='ambiguous')
    if any(e[k] != v for k,v in constants.items()):
        raise ValueError('未实现的盘中规则')
    if (type(e['earliest_bar_number']) is not int or type(e['last_entry_bar_number']) is not int
        or not 3 <= e['earliest_bar_number'] <= e['last_entry_bar_number'] < 48
        or not 0 < e['panic_atr'] < 10 or not 0 <= e['peer_nonfalling_fraction'] <= 1
        or type(e['minimum_peers_for_confirmation']) is not int or e['minimum_peers_for_confirmation'] < 1
        or not 0 < e['min_reward_risk_after_cost'] < 20 or not 0 <= e['one_way_cost_bps'] < 1000
        or not 0 < e['stock_tick'] < 1):
        raise ValueError('盘中参数范围错误')
    v = r['validation']
    if set(v) != {'screen_dates','outcome_dates','overnight_last_date','purpose'}:
        raise ValueError('未知验证计划字段')
    if len(v['screen_dates']) != len(v['outcome_dates']) or not v['screen_dates']:
        raise ValueError('验证日期计划错误')
    for a,b in zip(v['screen_dates'],v['outcome_dates']):
        if date.fromisoformat(a) >= date.fromisoformat(b):
            raise ValueError('验证日必须晚于筛选日')


def context_at(stock, cutoff):
    records = []
    for c in stock.get('context_records', []):
        # Later-published evidence cannot clear earlier candidates.
        if aware(c['known_at']) <= cutoff and aware(c['valid_through']) >= cutoff:
            if c.get('state') not in ('active','invalidated','unknown'):
                raise ValueError('情境状态错误')
            records.append(c)
    if not records:
        return None
    return max(records, key=lambda c:aware(c['known_at']))


def features(rows, benchmark, r):
    c = rows[-1]['close']
    windows = sorted({1,r['pullback_sessions'],r['trend_sessions']})
    ret = {str(n):(c/rows[-n-1]['close']-1)*100 for n in windows}
    bret = {str(n):(benchmark[-1]['close']/benchmark[-n-1]['close']-1)*100 for n in windows}
    trs = [max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(rows,rows[1:])]
    atr = mean(trs[-r['atr_sessions']:])
    if atr <= 0:
        raise ValueError('ATR为零，不能计算位置与风险')
    repairs = []
    for i in range(len(rows)-r['pullback_sessions'],len(rows)):
        b,a = rows[i],rows[i-1]
        br,ba = benchmark[i],benchmark[i-1]
        if b['close'] > b['open'] and b['close'] > a['close'] and b['close']/a['close'] >= br['close']/ba['close']:
            repairs.append(b['date'])
    flags = []
    if c < mean(x['close'] for x in rows[-5:]): flags.append('低于MA5')
    if c < mean(x['close'] for x in rows[-10:]): flags.append('低于MA10')
    if rows[-1]['low'] < min(x['low'] for x in rows[:-1]): flags.append('跌破此前20日低点')
    if any(abs(b.get('preclose',a['close'])-a['close']) > .011 for a,b in zip(rows,rows[1:])):
        flags.append('前收差异_复核公司行动或源差异')
    return dict(close=c,returns_pct=ret,benchmark_returns_pct=bret,
        excess_pct_points={n:ret[n]-bret[n] for n in ret},atr=atr,
        avg_amount_20=mean(x['amount'] for x in rows[-r['activity_sessions']:]),
        drawdown_atr=(max(x['high'] for x in rows[-r['trend_sessions']:])-c)/atr,
        pullback_atr=(rows[-r['pullback_sessions']-1]['close']-c)/atr,
        recent_relative_repair_days=repairs,
        intraday_high_vs_preclose_pct=(rows[-1]['high']/rows[-2]['close']-1)*100,
        close_off_high_atr=(rows[-1]['high']-c)/atr,risk_flags=flags)


def screen(universe, data, rules, asof):
    check_rules(rules)
    cutoff = cutoff_date(asof)
    if data.get('retrieved_at') and aware(data['retrieved_at']) < cutoff:
        raise ValueError('收盘前获取的数据不能作为完整日K')
    if data.get('interval') != '1d' or data.get('price_basis') != 'unadjusted':
        raise ValueError('需要不复权日线')
    if data.get('amount_unit') != 'CNY' or data.get('volume_unit') != 'shares':
        raise ValueError('需要人民币元及股的明确单位')
    if data.get('universe_sha256') != digest(universe):
        raise ValueError('行情股票池身份不一致')
    instruments = universe['stocks'] + universe.get('references', [])
    symbols = [s['symbol'] for s in instruments]
    if len(set(symbols)) != len(symbols) or not universe['stocks']:
        raise ValueError('代码重复或空股票池')
    sessions = sorted(d for d in data['calendar'] if d <= asof)
    if len(sessions) != len(set(sessions)):
        raise ValueError('交易日历重复')
    for d in sessions: date.fromisoformat(d)
    out = dict(schema_version=2,engine_version=__version__,asof=cutoff.isoformat(),
        universe=universe,universe_sha256=digest(universe),rules=rules,rules_sha256=digest(rules),
        valid=True,block_reasons=[],research_only=True,rows=[])
    refs, prefix = {}, {}
    try:
        b = rules['benchmark_symbol']
        instrument = next(s for s in instruments if s['symbol'] == b)
        if aware(instrument['member_known_at']) > cutoff:
            raise ValueError('参照工具当时尚不可见')
        refs[b] = history(data['stocks'].get(b,[]),asof,sessions,rules['history_sessions'],reference=True)
        prefix[b] = refs[b]
    except (ValueError,KeyError,StopIteration,TypeError) as exc:
        out['valid'] = False
        out['block_reasons'].append('市场参照缺失或错误:' + str(exc))
    valid_stocks = {}
    for s in universe['stocks']:
        row = dict(symbol=s['symbol'],name=s['name'],selected=False,ready=False,mode=None,
                   status='数据待核查',exclusion_reasons=[],review_flags=[],peers=[])
        out['rows'].append(row)
        if not out['valid']:
            row['status'] = '市场参照缺失_暂停筛选'
            continue
        try:
            if aware(s['member_known_at']) > cutoff:
                raise ValueError('成员资格在当时不可见')
            rows = history(data['stocks'].get(s['symbol'],[]),asof,sessions,rules['history_sessions'])
            prefix[s['symbol']] = rows
            valid_stocks[s['symbol']] = (s,rows,row)
            f = features(rows,refs[b],rules)
            row['features'] = f
            trend,short = str(rules['trend_sessions']),str(rules['pullback_sessions'])
            strong = f['returns_pct'][trend] > 0 and f['excess_pct_points'][trend] >= 0 and f['returns_pct'][short] < 0
            oversold = (not strong and f['pullback_atr'] >= rules['oversold_bounce']['min_pullback_atr']
                        and f['drawdown_atr'] >= rules['oversold_bounce']['min_drawdown_atr'])
            row['conditions'] = dict(positive_trend=f['returns_pct'][trend]>0,
                nonnegative_trend_excess=f['excess_pct_points'][trend]>=0,
                recent_pullback=f['returns_pct'][short]<0,
                oversold_drawdown=f['drawdown_atr']>=rules['oversold_bounce']['min_drawdown_atr'],
                oversold_recent_decline=f['pullback_atr']>=rules['oversold_bounce']['min_pullback_atr'])
            row['mode'] = '强势回调' if strong else '超跌反抽' if oversold else None
            minimum = rules['hypothetical_position_yuan']/rules['max_daily_participation']
            row['liquidity_minimum_yuan'] = minimum
            if f['avg_amount_20'] < minimum: row['exclusion_reasons'].append('成交额不足示例规模的容量门槛')
            if rows[-1]['is_st']: row['exclusion_reasons'].append('历史ST')
            if not rows[-1]['trading'] or rows[-1]['volume'] <= 0: row['exclusion_reasons'].append('当日无正常交易')
            if row['mode'] is None: row['exclusion_reasons'].append('两类价格结构均不满足')
            for k in ('event','corporate_action'):
                state = reviewed(s,k,cutoff)
                row[k+'_review'] = state
                if state == 'adverse': row['exclusion_reasons'].append(k+'存在已知不利事项')
                if state == 'unknown': row['review_flags'].append(k+'事前核查未知')
            ctx = context_at(s,cutoff)
            row['context'] = ctx
            required = ('premise','link_basis','expectation_source','pressure_symbol','invalidation','source_url')
            if ctx and ctx['state'] == 'invalidated': row['exclusion_reasons'].append('已知情境失效')
            if not ctx or ctx['state'] != 'active' or not all(ctx.get(k) for k in required):
                row['review_flags'].append('当前预期与压力关系缺少完整事前证据')
            if f['risk_flags'] and any('复核' in x for x in f['risk_flags']):
                row['review_flags'].append('公司行动或价格差异待复核')
                row['price_basis_unresolved'] = True
                row['exclusion_reasons'].append('未复权窗口存在前收断点_不能同口径筛选')
            row['selected'] = bool(row['mode']) and not row['exclusion_reasons']
        except (ValueError,KeyError,TypeError) as exc:
            valid_stocks.pop(s['symbol'],None)
            row.update(status='数据错误_保留样本',data_error=str(exc))
    for sym,(s,rows,row) in valid_stocks.items():
        group = s.get('peer_group')
        group_visible = bool(group and s.get('peer_group_known_at') and aware(s['peer_group_known_at']) <= cutoff)
        expected = [x['symbol'] for x in universe['stocks'] if x['symbol'] != sym and group_visible and x.get('peer_group') == group
                    and x.get('peer_group_known_at') and aware(x['peer_group_known_at']) <= cutoff and aware(x['member_known_at']) <= cutoff]
        row['peers'] = sorted(expected)
        missing = [p for p in expected if p not in valid_stocks]
        if missing: row['review_flags'].append('同伴历史缺失:'+','.join(missing))
        if len(expected) < rules['entry']['minimum_peers_for_confirmation']:
            row['review_flags'].append('同主题样本覆盖不足')
        usable = [p for p in expected if p in valid_stocks and not valid_stocks[p][2].get('price_basis_unresolved')]
        ctx = row.get('context')
        if ctx and ctx.get('pressure_symbol') and ctx.get('state') == 'active':
            pressure = ctx['pressure_symbol']
            try:
                if pressure == sym: raise ValueError('不能用交易标的自身代替外部压力确认')
                ins = next(x for x in instruments if x['symbol'] == pressure)
                if aware(ins['member_known_at']) > cutoff: raise ValueError('压力工具当时不可见')
                pr = history(data['stocks'].get(pressure,[]),asof,sessions,rules['history_sessions'],reference=True)
                prefix[pressure] = pr
                row.update(pressure_symbol=pressure,pressure_kind='事前情境指定')
            except (ValueError,KeyError,StopIteration) as exc:
                row['review_flags'].append('指定压力源不可用:'+str(exc))
                row.update(pressure_symbol=b,pressure_kind='市场价格代理_弱证据')
        elif usable:
            pressure = min(usable,key=lambda p:(valid_stocks[p][2]['features']['returns_pct']['1'],p))
            row.update(pressure_symbol=pressure,pressure_kind='同组当日最弱价格代理_非事件归因')
        else:
            row.update(pressure_symbol=b,pressure_kind='市场价格代理_弱证据')
            row['review_flags'].append('没有可用同主题压力代理')
        row['ready'] = row['selected'] and not row['review_flags']
        row['status'] = '情境合格候选' if row['ready'] else '技术候选_待情境核查' if row['selected'] else '未入选'
    out['input_prefix_sha256'] = digest(dict(calendar=sessions[-rules['history_sessions']:],stocks=prefix))
    out['selected_symbols'] = [r['symbol'] for r in out['rows'] if r['selected']]
    out['modes'] = {m:[r['symbol'] for r in out['rows'] if r['selected'] and r['mode']==m] for m in MODES}
    out['data_errors'] = [r['symbol'] for r in out['rows'] if 'data_error' in r]
    return seal(out)
