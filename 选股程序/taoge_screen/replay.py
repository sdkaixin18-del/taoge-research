"""Predeclared consecutive-date replay with a single frozen rule file."""
from pathlib import Path
from .core import screen, aware
from .io import digest, seal, save
from .validation import evaluate
from .report import records,export_rows


def replay(universe,daily,minutes,rules,freeze,out):
    if freeze.get('rules_sha256') != digest(rules) or freeze.get('rules') != rules:
        raise ValueError('规则与采集前封存不一致')
    if freeze.get('universe_sha256') != digest(universe):
        raise ValueError('观察池与采集前封存不一致')
    if aware(freeze['frozen_at']) >= aware(minutes['retrieved_at']):
        raise ValueError('分钟采集早于封存，不能标为本协议先固定后采集')
    last_outcome = rules['validation']['outcome_dates'][-1]
    later = sorted(d for d in daily['calendar'] if d > last_outcome)
    if not later or later[0] != rules['validation']['overnight_last_date']:
        raise ValueError('日历未覆盖协议规定的最后一个T+1交易日')
    out=Path(out)
    screens=[]
    # Finish every daily screen before evaluating any outcome path.
    for asof in rules['validation']['screen_dates']:
        s=screen(universe,daily,rules,asof)
        save(out/asof/'筛选.json',s)
        if not s['valid'] or s['data_errors']:
            raise ValueError('连续回放筛选数据不完整:'+asof)
        screens.append(s)
    allrows=[];results=[]
    for s,day in zip(screens,rules['validation']['outcome_dates']):
        v=evaluate(s,daily,minutes,day)
        save(out/s['asof'][:10]/'验证.json',v)
        allrows.extend(records(s,v));results.append(v)
    export_rows(allrows,out/'导出','五个连续日期的强势回调与超跌反抽')
    summary=seal(dict(schema_version=2,rules_sha256=digest(rules),universe_sha256=digest(universe),
        freeze=freeze,screen_hashes=[s['content_sha256'] for s in screens],
        validation_hashes=[v['content_sha256'] for v in results],
        n_stock_dates=len(allrows),n_data_errors=sum(r['status']=='数据不足_不判成功失败' for v in results for r in v['rows']),
        n_price_basis_unresolved=sum(r['status']=='价格口径未统一_不判成功失败' for v in results for r in v['rows']),
        daily=[dict(asof=s['asof'][:10],outcome=v['date'],modes=s['modes'],summary=v['summary']) for s,v in zip(screens,results)]))
    save(out/'汇总.json',summary)
    return summary
