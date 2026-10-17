"""Walk forward over completed bars; no parameter search or hindsight entries."""
from datetime import datetime, timedelta
from .core import TZ, aware, price_bar
from .io import digest, seal, verify


def expected_times(day):
    out = []
    for hour,minute in ((9,35),(13,5)):
        t = datetime.fromisoformat(day).replace(hour=hour,minute=minute,tzinfo=TZ)
        end = t.replace(hour=11,minute=30) if hour == 9 else t.replace(hour=15,minute=0)
        while t <= end:
            out.append(t)
            t += timedelta(minutes=5)
    return out


def minute_day(data, sym, day):
    rows = sorted((r for r in data.get('stocks',{}).get(sym,[]) if r['date']==day),key=lambda r:r['end'])
    if len(rows) != 48 or [aware(r['end']) for r in rows] != expected_times(day):
        raise ValueError(sym+':缺少完整48根5分钟线或时间错位')
    for b in rows: price_bar(b)
    return rows


def stable(bars, i):
    a,b,c = bars[i-2:i+1]
    return a['low'] <= b['low'] <= c['low'] and c['close'] > b['close']


def label_path(bars, upper, lower):
    for b in bars:
        if b['open'] >= upper: return '先目标',b['end'],b['open']
        if b['open'] <= lower: return '先失效',b['end'],b['open']
        up, down = b['high'] >= upper,b['low'] <= lower
        if up and down: return '同根双触_先后未知',b['end'],None
        if up: return '先目标',b['end'],upper
        if down: return '先失效',b['end'],lower
    return '收盘未触及',bars[-1]['end'],bars[-1]['close']


def net_return(entry, exit_price, cost):
    return (exit_price*(1-cost)/(entry*(1+cost))-1)*100


def simulate(row, snapshot, stockbars, marketbars, pressurebars, peerbars):
    e = snapshot['rules']['entry']
    prior, atr = row['features']['close'],row['features']['atr']
    cost = e['one_way_cost_bps']/10000
    out = dict(status='未触发',traces=[],setup_count=0,space_rejected=0,coverage_sufficient=False)
    cum_amount = cum_volume = 0
    running_low = stockbars[0]['low']
    for i,b in enumerate(stockbars):
        cum_amount += b['amount'];cum_volume += b['volume']
        running_low = min(running_low,b['low'])
        if i+1 < e['earliest_bar_number'] or i+1 > e['last_entry_bar_number']:
            continue
        if cum_volume <= 0:
            raise ValueError('截至触发时没有可用成交量')
        vwap = cum_amount/cum_volume
        visible_high = max(x['high'] for x in stockbars[:i+1])
        if vwap < running_low-e['stock_tick'] or vwap > visible_high+e['stock_tick']:
            raise ValueError('累计真实量额VWAP与价格区间不一致，不能猜测单位')
        lows = [x['low'] for x in stockbars[i-2:i+1]]
        panic = running_low <= prior-e['panic_atr']*atr
        reclaim = lows[0] <= lows[1] <= lows[2] and b['close'] > stockbars[i-1]['high'] and b['close'] > vwap
        market_ok,pressure_ok = stable(marketbars,i),stable(pressurebars,i)
        peer_fraction = (sum(v[i]['close'] >= v[i-1]['close'] for v in peerbars.values())/len(peerbars)
                         if peerbars else None)
        peers_ok = peer_fraction is None or peer_fraction >= e['peer_nonfalling_fraction']
        coverage = len(peerbars) >= e['minimum_peers_for_confirmation']
        trace = dict(bar_end=b['end'],panic=panic,reclaim=reclaim,market_relief=market_ok,
                     pressure_relief=pressure_ok,peer_fraction=peer_fraction,
                     coverage_sufficient=coverage,vwap=vwap,visible_low=running_low)
        out['traces'].append(trace)
        if not all((panic,reclaim,market_ok,pressure_ok,peers_ok)):
            continue
        out['setup_count'] += 1
        nextbar = stockbars[i+1]
        entry = nextbar['open']
        stop = round(min(lows)-e['stock_tick'],6)
        target = prior
        reward = target*(1-cost)-entry*(1+cost)
        risk = entry*(1+cost)-stop*(1-cost)
        rr = reward/risk if risk > 0 else None
        trace.update(next_open=entry,stop=stop,target=target,reward_risk=rr)
        if stop <= 0 or entry <= stop or reward <= 0 or rr is None or rr < e['min_reward_risk_after_cost']:
            out['space_rejected'] += 1
            trace['entry_decision'] = '跳空越过失效位或成本后空间不足'
            continue
        if nextbar['volume'] <= 0 or nextbar['high'] == nextbar['low']:
            out.update(status='入场成交未确认',signal_bar_end=b['end'],coverage_sufficient=coverage)
            trace['entry_decision'] = '无成交或只有一字价格_不假定成交'
            return out
        trace['entry_decision'] = '价格路径参与假设'
        path = stockbars[i+1:]
        label,when,exit_price = label_path(path,target,stop)
        out.update(status=label,signal_bar_end=b['end'],entry_bar_end=nextbar['end'],
            entry_time=(aware(nextbar['end'])-timedelta(minutes=5)).isoformat(),
            entry_price=entry,target=target,stop=stop,reward_risk=rr,
            path_bar_end=when,path_exit_price=exit_price,coverage_sufficient=coverage,
            price_path_net_pct=net_return(entry,exit_price,cost) if exit_price is not None else None,
            entry_to_close_net_pct=net_return(entry,stockbars[-1]['close'],cost),
            mfe_pct=(max(x['high'] for x in path)/entry-1)*100,
            mae_pct=(min(x['low'] for x in path)/entry-1)*100,
            full_context_confirmed=bool(row['ready'] and coverage),
            signal_kind='情境及价格条件齐备的研究假设' if row['ready'] and coverage else '仅价格代理触发_情境未确认')
        return out
    if out['setup_count']:
        out['status'] = '触发但空间不足'
    return out


def evaluate(snapshot, daily, minutes, day):
    verify(snapshot)
    if not snapshot['valid']:
        raise ValueError('筛选快照无效')
    asof = snapshot['asof'][:10]
    later = sorted(d for d in daily.get('calendar',[]) if d > asof)
    if daily.get('requested_start','9999') > asof or not later or later[0] != day:
        raise ValueError('验证日必须是筛选后的第一个实际交易日')
    for data,interval in ((daily,'1d'),(minutes,'5m')):
        if data.get('universe_sha256') != snapshot['universe_sha256']:
            raise ValueError('验证股票池身份不同')
        if data.get('price_basis') != 'unadjusted' or data.get('interval') != interval:
            raise ValueError('验证周期或价格口径不一致')
        if data.get('amount_unit') != 'CNY' or data.get('volume_unit') != 'shares':
            raise ValueError('验证量额单位错误')
    out = dict(schema_version=2,screen_sha256=snapshot['content_sha256'],rules_sha256=snapshot['rules_sha256'],
        date=day,research_only=True,performance_kind='conditional_price_paths_not_account_pnl',rows=[])
    prefix = {}
    bars_cache = {}
    def get_bars(sym):
        if sym not in bars_cache:
            bars_cache[sym] = minute_day(minutes,sym,day)
        return bars_cache[sym]
    market = snapshot['rules']['benchmark_symbol']
    next_days = sorted(d for d in daily['calendar'] if d > day)
    overnight = next_days[0] if next_days else None
    for sr in snapshot['rows']:
        sym = sr['symbol']
        row = dict(symbol=sym,name=sr['name'],mode=sr['mode'],selected=sr['selected'],
            ready_at_screen=sr['ready'],screen_status=sr['status'],quality_flags=[],pressure_symbol=sr.get('pressure_symbol'))
        out['rows'].append(row)
        try:
            if 'features' not in sr or 'data_error' in sr:
                raise ValueError('筛选时该股数据不合格，不能补造事前特征')
            ds = [r for r in daily['stocks'].get(sym,[]) if r['date']==day]
            prefix[sym+'_daily'] = ds
            if len(ds) != 1: raise ValueError('验证日日线缺失或重复')
            d = ds[0];price_bar(d)
            if 'preclose' not in d: raise ValueError('验证日日线缺少前收，不能排除价格口径断点')
            if not d.get('trading') or d['volume'] <= 0: raise ValueError('验证日无正常交易')
            ms = get_bars(sym)
            actual = dict(open=ms[0]['open'],close=ms[-1]['close'],high=max(x['high'] for x in ms),low=min(x['low'] for x in ms))
            row.update(daily_ohlc={k:d[k] for k in ('open','high','low','close')},
                open_to_close_pct=(d['close']/d['open']-1)*100)
            for k,value in actual.items():
                if abs(value-d[k]) > .011:
                    row['quality_flags'].append(f'日线与分钟{k}不一致:{d[k]} vs {value}')
            if sr.get('price_basis_unresolved') or abs(d.get('preclose',sr['features']['close'])-sr['features']['close']) > .011:
                row.update(status='价格口径未统一_不判成功失败',
                    error='窗口或验证日前收存在断点；需除权因子及公司行动核查，不能按旧价计算目标与收益')
                continue
            peers = {p:get_bars(p) for p in sr['peers']}
            pressure = get_bars(sr['pressure_symbol'])
            row.update(simulate(sr,snapshot,ms,get_bars(market),pressure,peers))
            row.update(daily_ohlc={k:d[k] for k in ('open','high','low','close')},
                open_to_close_pct=(d['close']/d['open']-1)*100,
                close_vs_asof_pct=(d['close']/sr['features']['close']-1)*100)
            if 'entry_price' in row:
                cost = snapshot['rules']['entry']['one_way_cost_bps']/10000
                nd = [x for x in daily['stocks'].get(sym,[]) if x['date']==overnight]
                if len(nd) != 1:
                    row['t1_status'] = '次日数据缺失_不计算'
                else:
                    n = nd[0];price_bar(n)
                    first = [x for x in minutes['stocks'].get(sym,[]) if x['date']==overnight and aware(x['end'])==expected_times(overnight)[0]]
                    if 'preclose' not in n:
                        row['t1_status'] = '次日前收缺失_不计算'
                    elif abs(n['preclose']-d['close']) > .011:
                        row['t1_status'] = '隔夜前收口径断点_不计算未调整收益'
                    elif not n.get('trading') or n['volume']<=0 or len(first)!=1 or first[0]['high']==first[0]['low']:
                        row['t1_status'] = '次日开盘成交未确认_不计算'
                    else:
                        row.update(t1_status='次日开盘价格假设_非成交核实',t1_date=overnight,t1_open=n['open'],
                            t1_next_open_net_pct=net_return(row['entry_price'],n['open'],cost),
                            overnight_gap_pct=(n['open']/d['close']-1)*100)
                    prefix[sym+'_overnight'] = dict(daily=n,first_minute=first)
        except (ValueError,KeyError,TypeError) as exc:
            row.update(status='数据不足_不判成功失败',error=str(exc))
        prefix[sym+'_daily'] = [r for r in daily.get('stocks',{}).get(sym,[]) if r['date']==day]
    prefix['minutes_used'] = bars_cache
    out['validation_prefix_sha256'] = digest(prefix)
    groups = {}
    group_rows = [('强势回调',[r for r in out['rows'] if r['selected'] and r['mode']=='强势回调']),
                  ('超跌反抽',[r for r in out['rows'] if r['selected'] and r['mode']=='超跌反抽']),
                  ('未入选对照',[r for r in out['rows'] if not r['selected']])]
    for name,rows in group_rows:
        labels = {}
        for r in rows: labels[r['status']] = labels.get(r['status'],0)+1
        groups[name] = dict(n=len(rows),labels=labels,
            entries=sum('entry_price' in r for r in rows),
            context_confirmed_entries=sum(r.get('full_context_confirmed',False) for r in rows),
            quality_flagged=sum(bool(r['quality_flags']) for r in rows))
    out['summary'] = groups
    return seal(out)
