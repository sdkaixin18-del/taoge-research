"""Full-cohort, escaped HTML/CSV export. No winner-only selection."""
import csv
from html import escape
import json
from pathlib import Path
from .io import encoded, verify


def records(snapshot, validation=None):
    verify(snapshot)
    if validation:
        verify(validation)
        if validation['screen_sha256'] != snapshot['content_sha256']:
            raise ValueError('报告的筛选及验证身份不匹配')
    outcomes = {r['symbol']:r for r in validation['rows']} if validation else {}
    rows=[]
    for r in snapshot['rows']:
        f=r.get('features',{});v=outcomes.get(r['symbol'],{})
        trend=str(snapshot['rules']['trend_sessions']);short=str(snapshot['rules']['pullback_sessions'])
        rows.append(dict(筛选日=snapshot['asof'][:10],验证日=validation['date'] if validation else '',
            股票=r['name'],代码=r['symbol'],类别=r['mode'] or '未归类',入选=r['selected'],情境合格=r['ready'],
            日均成交额亿元=f.get('avg_amount_20',0)/1e8,近5日收益=f.get('returns_pct',{}).get(short),
            近20日收益=f.get('returns_pct',{}).get(trend),相对科技ETF20日百分点=f.get('excess_pct_points',{}).get(trend),
            距20日高点ATR=f.get('drawdown_atr'),近5日下跌ATR=f.get('pullback_atr'),
            压力观察对象=r.get('pressure_symbol'),压力依据=r.get('pressure_kind'),同伴数=len(r['peers']),
            淘汰原因='；'.join(r['exclusion_reasons']),待核查='；'.join(r['review_flags']),
            筛选数据错误=r.get('data_error',''),路径=v.get('status','未验证'),
            信号K线结束=v.get('signal_bar_end'),参与时间假设=v.get('entry_time'),参与价格假设=v.get('entry_price'),
            目标=v.get('target'),失效价=v.get('stop'),成本后盈亏比=v.get('reward_risk'),
            日内路径含成本变化=v.get('price_path_net_pct'),入场至收盘含成本变化=v.get('entry_to_close_net_pct'),
            次日开盘状态=v.get('t1_status'),次日开盘含成本变化=v.get('t1_next_open_net_pct'),
            开盘至收盘变化=v.get('open_to_close_pct'),情境确认触发=v.get('full_context_confirmed',False),
            数据差异='；'.join(v.get('quality_flags',[])),验证数据错误=v.get('error','')))
    return rows


def export_rows(rows, folder, title):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    if not rows: raise ValueError('不能导出空报告')
    # CSVs are for viewing too; prevent formula injection through external text.
    safe=[]
    for row in rows:
        safe.append({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()})
    with (folder/'全部样本.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(safe)
    def val(v):
        return f'{v:.4f}' if isinstance(v,float) else '' if v is None else str(v)
    head=''.join('<th>'+escape(k)+'</th>' for k in rows[0])
    body=''.join('<tr>'+''.join('<td>'+escape(val(v))+'</td>' for v in row.values())+'</tr>' for row in rows)
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'''+escape(title)+'''</title>
<style>body{font:14px system-ui;margin:24px;color:#182332}h1{font-size:24px}p{max-width:1000px;line-height:1.7}input{padding:9px;min-width:350px}.table{overflow:auto;max-height:75vh}table{border-collapse:collapse;white-space:nowrap}td,th{border:1px solid #ddd;padding:8px}th{position:sticky;top:0;background:#e9eff5}tr:nth-child(even){background:#f7f9fc}</style>
<h1>'''+escape(title)+'''</h1><p>日终候选、情境核查和日内价格路径分列。日内目标/失效不等于普通A股新买仓位可当日退出；次日开盘列也只是假设价格对照。全部入选、未入选、未触发和数据不足均保留。</p><input id="filter" placeholder="按股票、日期、类别、路径等筛选"><p id="count"></p><div class="table"><table><thead><tr>'''+head+'</tr></thead><tbody>'+body+'''</tbody></table></div><script>
const rows=[...document.querySelectorAll('tbody tr')],input=document.querySelector('#filter');function filter(){let n=0;for(const r of rows){r.hidden=!r.textContent.includes(input.value);if(!r.hidden)n++}document.querySelector('#count').textContent=`显示 ${n} / ${rows.length} 条`};input.addEventListener('input',filter);filter();</script></html>'''
    (folder/'报告.html').write_text(html,encoding='utf-8')


def export(snapshot, validation, folder):
    export_rows(records(snapshot,validation),folder,'强势回调与超跌反抽全样本')
