"""Optional BaoStock adapter. No brokerage account, no order interface."""
from datetime import datetime, timezone
import importlib.metadata
import socket
from pathlib import Path
from .io import digest, save, read


FIELDS = 'date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST'
MINUTE_FIELDS = 'date,time,code,open,high,low,close,volume,amount,adjustflag'


def bs_symbol(symbol):
    import re
    if not re.fullmatch(r'(sh|sz)\d{6}', symbol):
        raise ValueError(f'不支持的代码格式：{symbol}')
    return symbol[:2]+'.'+symbol[2:]


def records(result):
    if result.error_code != '0':
        raise ValueError(f'行情接口错误 {result.error_code}: {result.error_msg}')
    out = []
    while result.next():
        out.append(dict(zip(result.fields, result.get_row_data())))
    if result.error_code != '0':
        raise ValueError(f'行情读取中断 {result.error_code}: {result.error_msg}')
    return out


def collect(universe, start, end, out, *, minutes=False):
    """One provider session; requests are sequential because its client is global."""
    if Path(out).exists():
        cached = read(out)
        if (cached.get('universe_sha256') != digest(universe)
            or cached.get('requested_start') != start or cached.get('requested_end') != end
            or cached.get('interval') != ('5m' if minutes else '1d')):
            raise ValueError('已有缓存的请求身份不同，请使用新路径')
        print('复用原始缓存：'+str(out),flush=True)
        return cached
    try:
        import baostock as bs
    except ImportError as exc:
        raise ValueError('联网读取需要 baostock==0.9.4；离线筛选和测试无需该依赖') from exc
    socket.setdefaulttimeout(25)
    login = bs.login()
    if login.error_code != '0':
        raise ValueError(f'公开行情连接失败：{login.error_msg}')
    data = dict(schema_version=1, provider='baostock',
                provider_version=importlib.metadata.version('baostock'),
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                universe_sha256=digest(universe), requested_start=start, requested_end=end,
                price_basis='unadjusted', interval='5m' if minutes else '1d',
                amount_unit='CNY', volume_unit='shares', stocks={}, errors={})
    try:
        cal = records(bs.query_trade_dates(start_date=start, end_date=end))
        data['calendar'] = [r['calendar_date'] for r in cal if r['is_trading_day']=='1']
        instruments = universe['stocks'] + universe.get('references', [])
        if len({s['symbol'] for s in instruments}) != len(instruments):
            raise ValueError('股票与参考工具的代码重复')
        for stock in instruments:
            sym = stock['symbol']
            try:
                query = bs.query_history_k_data_plus(bs_symbol(sym),
                    MINUTE_FIELDS if minutes else FIELDS, start_date=start, end_date=end,
                    frequency='5' if minutes else 'd', adjustflag='3')
                raw = records(query)
                save(str(out)+f'.raw/{sym}.json', raw)
                if not raw:
                    raise ValueError('请求成功但历史行情为空')
                rows = []
                for r in raw:
                    if r['code'] != bs_symbol(sym) or r['adjustflag'] != '3':
                        raise ValueError('返回代码或复权口径不一致')
                    bar = {k:float(r[k]) for k in ['open','high','low','close','volume','amount']}
                    bar['date'] = r['date']
                    if minutes:
                        t = datetime.strptime(r['time'][:14], '%Y%m%d%H%M%S')
                        bar['end'] = t.isoformat()+'+08:00'
                    else:
                        bar.update(preclose=float(r['preclose']),
                                   turnover=float(r['turn']) if r['turn'] else None,
                                   trading=r['tradestatus']=='1', is_st=r['isST']=='1')
                    rows.append(bar)
                data['stocks'][sym] = rows
                print(f'{sym}: {len(rows)} bars', flush=True)
            except (ValueError, OSError, KeyError) as exc:
                data['errors'][sym] = str(exc)
        data['complete'] = not data['errors']
        save(out, data)
    finally:
        bs.logout()
    return data
