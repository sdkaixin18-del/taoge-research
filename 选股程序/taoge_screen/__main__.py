"""Command line entry point; exit 2 means missing data or a blocked run."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys
from .core import TZ, cutoff_date, screen
from .io import digest, read, save
from .report import export
from .validation import evaluate


def main(argv=None):
    p = argparse.ArgumentParser(description='桃哥课程延伸的反抽研究选股；只读取行情、输出候选。')
    sub = p.add_subparsers(dest='cmd',required=True)
    f = sub.add_parser('fetch',help='采集公开历史行情；原始返回与缓存只写指定数据目录')
    f.add_argument('--universe',required=True,type=Path)
    f.add_argument('--start',required=True);f.add_argument('--end',required=True)
    f.add_argument('--interval',choices=['day','5m'],default='day')
    f.add_argument('--out',required=True,type=Path)
    s = sub.add_parser('screen',help='仅使用收盘时点及以前数据筛选')
    for arg in ['universe','daily','rules','out']:
        s.add_argument('--'+arg,required=True,type=Path)
    s.add_argument('--asof',required=True)
    v = sub.add_parser('validate',help='读取已封存的筛选快照，独立评估后续路径')
    for arg in ['snapshot','daily','minutes','out']:
        v.add_argument('--'+arg,required=True,type=Path)
    v.add_argument('--date',required=True)
    e = sub.add_parser('report',help='输出全部样本的CSV与HTML表格')
    e.add_argument('--snapshot',required=True,type=Path)
    e.add_argument('--validation',type=Path)
    e.add_argument('--out',required=True,type=Path)
    run = sub.add_parser('run',help='收盘后采集、筛选并导出；不读取下一日行情')
    run.add_argument('--date',required=True)
    for arg in ['universe','rules','data-dir']:
        run.add_argument('--'+arg,required=True,type=Path)
    rp = sub.add_parser('replay',help='按采集前封存协议回放全部连续日期与未入选对照')
    for arg in ['universe','daily','minutes','rules','freeze','out']:
        rp.add_argument('--'+arg,required=True,type=Path)
    a = p.parse_args(argv)
    try:
        if a.cmd=='fetch':
            from .providers import collect
            d = collect(read(a.universe),a.start,a.end,a.out,minutes=a.interval=='5m')
            return 0 if d['complete'] else 2
        if a.cmd=='screen':
            now = datetime.now(TZ)
            cutoff = cutoff_date(a.asof)
            if cutoff > now:
                raise ValueError('指定的收盘时点尚未发生')
            s = screen(read(a.universe),read(a.daily),read(a.rules),a.asof)
            save(a.out,s)
            print('候选：'+','.join(s.get('selected_symbols',[])))
            print('快照：'+s['content_sha256'])
            return 0 if s['valid'] and not s['data_errors'] else 2
        if a.cmd=='validate':
            o = evaluate(read(a.snapshot),read(a.daily),read(a.minutes),a.date)
            save(a.out,o)
            print(o['summary'])
            return 0 if all(r['status']!='数据不足_不判成功失败' for r in o['rows']) else 2
        if a.cmd=='replay':
            from .replay import replay
            o=replay(read(a.universe),read(a.daily),read(a.minutes),read(a.rules),read(a.freeze),a.out)
            print('全部股票日期：'+str(o['n_stock_dates']))
            print('验证数据错误：'+str(o['n_data_errors']))
            print('结果：'+str(a.out))
            return 0 if o['n_data_errors']==0 else 2
        if a.cmd=='report':
            export(read(a.snapshot),read(a.validation) if a.validation else None,a.out)
            print('报告目录：'+str(a.out))
            return 0
        if a.cmd=='run':
            from .providers import collect
            cutoff = cutoff_date(a.date)
            if datetime.now(TZ) < cutoff+timedelta(minutes=10):
                raise ValueError('日终运行须在指定交易日15:10以后，避免未完成日K')
            u,r = read(a.universe),read(a.rules)
            folder = a.data_dir/a.date/(digest(u)[:10]+'_'+digest(r)[:10])
            start = (cutoff.date()-timedelta(days=100)).isoformat()
            d = collect(u,start,a.date,folder/'日线.json')
            s = screen(u,d,r,a.date)
            save(folder/'筛选快照.json',s)
            export(s,None,folder/'报告')
            print('结果：'+str(folder))
            return 0 if s['valid'] and not s['data_errors'] else 2
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print('任务失败：'+str(exc),file=sys.stderr)
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
