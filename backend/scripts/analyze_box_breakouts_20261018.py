"""Read-only, causal shape study. See docs/box-breakout-20261018-protocol.md."""
import argparse
import json
import pickle
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'box-breakout-20261018'
CACHE = Path('/tmp/tradeiq-box-breakout-20261018.pkl')
CASES = [
    ('中润光学', '688307.SH', '20260302'), ('广钢气体', '688548.SH', '20260622'),
    ('斯莱克', '300382.SZ', '20260622'), ('昆船智能', '301311.SZ', '20260922'),
    ('向日葵', '300111.SZ', '20261009'), ('晶雪节能', '301010.SZ', '20261008'),
    ('国力电子', '688103.SH', '20260630'), ('杰恩股份', '300668.SZ', '20260323'),
    ('善水科技', '301190.SZ', '20260928'), ('近岸蛋白', '688137.SH', '20260807'),
    ('澳弘电子', '605058.SH', '20260910'),
]
WINDOWS = (20, 30, 40, 60, 90, 120)
RULES = [
    ('此前20日高低范围不超过35%', '(MAX(high,20,1) / MIN(low,20,1) - 1) * 100 <= 35'),
    ('箱体末5日与初5日均价差不超过10%', 'ABS(AVG(close,5,1) / AVG(close,5,16) - 1) * 100 <= 10'),
    ('收盘超过此前20日最高收盘价至少1%', '(close / MAX(close,20,1) - 1) * 100 >= 1'),
    ('成交量至少为此前5日均量的1.2倍', 'volume / AVG(volume,5,1) >= 1.2'),
    ('当日上涨至少2%', 'change >= 2'),
    ('收盘处于当日振幅上方40%区域', 'high > low and (close - low) / (high - low) >= 0.6'),
]


def clean(v):
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, np.generic):
        return clean(v.item())
    if isinstance(v, float) and not np.isfinite(v):
        return None
    return v


def write_json(path, v):
    path.write_text(json.dumps(clean(v), ensure_ascii=False, indent=2), encoding='utf-8')


def open_db(path=None):
    path = Path(path or ROOT / 'backend/market_data.db').resolve()
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def shift(a, n=1):
    if n < 0:
        raise ValueError('Future shifts forbidden')
    if n == 0:
        return a.copy()
    out = np.full_like(a, np.nan, dtype=float)
    out[n:] = a[:-n]
    return out


def roll(a, n, how='mean'):
    return getattr(pd.DataFrame(a).rolling(n, min_periods=n), how)().to_numpy()


def valid_bars(a):
    finite = np.logical_and.reduce([np.isfinite(a[k]) for k in ('open', 'high', 'low', 'close', 'volume', 'amount', 'change')])
    return (finite & (a['open'] > 0) & (a['low'] > 0) & (a['close'] > 0)
            & (a['high'] >= np.maximum(a['open'], a['close']))
            & (a['low'] <= np.minimum(a['open'], a['close']))
            & (a['volume'] > 0) & (a['amount'] > 0)
            & np.isfinite(a['change']))


def reference_ok(a):
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.abs((a['close'] / shift(a['close']) - 1) * 100 - a['change']) <= .15


def valid_window(a, n):
    """N prior bars plus signal day, checking only internal price links."""
    return (roll(valid_bars(a).astype(float), n + 1, 'sum') == n + 1) & (roll(reference_ok(a).astype(float), n, 'sum') == n)


def window_features(a, n):
    c, h, l, v = (a[k] for k in ('close', 'high', 'low', 'volume'))
    top = shift(roll(h, n, 'max'))
    bottom = shift(roll(l, n, 'min'))
    close_top = shift(roll(c, n, 'max'))
    close_bottom = shift(roll(c, n, 'min'))
    with np.errstate(invalid='ignore', divide='ignore'):
        return {
            'valid': valid_window(a, n),
            'valid_first': valid_window(a, n + 1),
            'top': top, 'bottom': bottom, 'close_top': close_top,
            'width': (top / bottom - 1) * 100,
            'close_width': (close_top / close_bottom - 1) * 100,
            'break_high': (c / top - 1) * 100,
            'break_close': (c / close_top - 1) * 100,
            'intraday_break': (h / top - 1) * 100,
            'prior_break': (shift(c) / shift(top) - 1) * 100,
            'prior_close_break': (shift(c) / shift(close_top) - 1) * 100,
            'net_return': (shift(c) / shift(c, n) - 1) * 100,
            'drift': (shift(roll(c, 5)) / shift(roll(c, 5), n - 4) - 1) * 100,
            'volume_contraction': shift(roll(v, 5)) / shift(roll(v, 5), n - 4),
        }


def prepare():
    end = max(x[2] for x in CASES)
    with open_db() as conn:
        dates = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date', ('20250801', end))]
        daily = pd.read_sql_query('SELECT ts_code,trade_date,name,open,high,low,close,change,volume,amount FROM stock_daily WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date,ts_code', conn, params=(dates[0], end))
        if daily.duplicated(['trade_date', 'ts_code']).any():
            raise ValueError('Duplicate daily bars')
        codes = sorted(daily.ts_code.unique())
        a = {k: daily.pivot(index='trade_date', columns='ts_code', values=k).reindex(index=dates, columns=codes).to_numpy(float)
             for k in ('open', 'high', 'low', 'close', 'change', 'volume', 'amount')}
        names = daily.pivot(index='trade_date', columns='ts_code', values='name').reindex(index=dates, columns=codes).fillna('')
        case_charts = []
        for name, code, day in CASES:
            bars = daily[(daily.ts_code == code) & (daily.trade_date <= day)].tail(141)
            case_charts.append({'name': name, 'code': code, 'date': day, 'bars': bars.to_dict('records')})
        del daily
        day_indices = [dates.index(d) for d in sorted({x[2] for x in CASES})]
        basic = {'day_change': a['change'], 'amount_yi': a['amount'] / 100000,
                 'close': a['close'], 'close_position': (a['close'] - a['low']) / np.maximum(a['high'] - a['low'], .000001)}
        for n in (5, 20):
            basic['volume_ratio' + str(n)] = a['volume'] / shift(roll(a['volume'], n))
        ma = {n: roll(a['close'], n) for n in (5, 10, 20, 60)}
        basic['bullish'] = (a['close'] > ma[5]) & (ma[5] > ma[10]) & (ma[10] > ma[20]) & (ma[20] > ma[60])
        basic['bullish_valid'] = valid_window(a, 59)
        basic['above_ma20'] = a['close'] > ma[20]
        basic['bias20'] = (a['close'] / ma[20] - 1) * 100
        basic['prior_ma5_ma20_gap'] = (shift(ma[5]) / shift(ma[20]) - 1) * 100
        basic['prior_ma20_ma60_gap'] = (shift(ma[20]) / shift(ma[60]) - 1) * 100
        basic['valid20'] = valid_window(a, 20)
        basic['valid60'] = valid_window(a, 60)
        frames = []
        for i in day_indices:
            frame = pd.DataFrame({k: v[i] for k, v in basic.items()})
            frame['date'] = dates[i]
            frame['code'] = codes
            frame['name'] = names.iloc[i].to_list()
            frames.append(frame)
        data = pd.concat(frames, ignore_index=True)
        for n in WINDOWS:
            features = window_features(a, n)
            for key, value in features.items():
                data[f'{key}{n}'] = np.concatenate([value[i] for i in day_indices])
            del features
        case_data = []
        for name, code, day in CASES:
            row = data[(data.code == code) & (data.date == day)].iloc[0].to_dict()
            row['name'] = name
            case_data.append(row)
        overview = pd.read_sql_query('SELECT * FROM overview WHERE REPLACE(date,\'-\',\'\') BETWEEN ? AND ?', conn, params=(min(x[2] for x in CASES), end))
        index = pd.read_sql_query("SELECT date,name,close,change FROM index_daily WHERE name='上证指数' AND REPLACE(date,'-','') BETWEEN ? AND ?", conn, params=(min(x[2] for x in CASES), end))
        overview['date'] = overview.date.str.replace('-', '')
        index['date'] = index.date.str.replace('-', '')
        market = overview.merge(index, on='date', how='outer', suffixes=('', '_index'))
        market = market[market.date.isin([x[2] for x in CASES])].to_dict('records')
        # Funding is a supplementary case feature, never required for price shape.
        flows = []
        for name, code, day in CASES:
            i = dates.index(day)
            rows = conn.execute('SELECT trade_date,main_net_inflow,source FROM fund_flow WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date', (code, dates[i - 19], day)).fetchall()
            byday = {r[0]: r for r in rows if r[2] in ('eastmoney-push2', 'eastmoney-daykline')}
            vals = {}
            for n in (1, 3, 5, 10, 20):
                f = [byday.get(d) for d in dates[i - n + 1:i + 1]]
                # Both labels are Eastmoney interfaces; never mix with THS.
                ok = all(r is not None and r[1] is not None and np.isfinite(r[1]) for r in f)
                vals[f'main{n}'] = sum(r[1] for r in f) / 1e8 if ok else None
                vals[f'main{n}_sources'] = sorted({r[2] for r in f if r})
                vals[f'main{n}_missing_dates'] = [d for d, r in zip(dates[i - n + 1:i + 1], f) if r is None or r[1] is None or not np.isfinite(r[1])]
            flows.append({'name': name, 'code': code, 'date': day, **vals})
        payload = {'frame': data, 'cases': case_data, 'charts': case_charts, 'market': market, 'flows': flows,
                   'dates': dates, 'rows_read': int(np.isfinite(a['close']).sum())}
    with CACHE.open('wb') as out:
        pickle.dump(payload, out, protocol=pickle.HIGHEST_PROTOCOL)
    return payload


def summarize_mask(frame, mask):
    cases = []
    for name, code, date in CASES:
        idx = (frame.code == code) & (frame.date == date)
        if bool(mask[idx].iloc[0]):
            cases.append(name)
    counts = frame.loc[mask].groupby('date').size().reindex(sorted({c[2] for c in CASES}), fill_value=0).astype(int).to_dict()
    return {'hits': len(cases), 'names': cases, 'counts': counts, 'total_candidates': int(mask.sum()), 'mean_daily': float(sum(counts.values()) / len(counts))}


def base_mask(d, n=20):
    return (d[f'valid{n}'] & (d[f'width{n}'] <= 35) & (d[f'drift{n}'].abs() <= 10)
            & (d[f'break_close{n}'] >= 1) & (d.volume_ratio5 >= 1.2)
            & (d.day_change >= 2) & (d.close_position >= .6))


def recommended(payload):
    d = payload['frame']
    base = base_mask(d)
    variants = {
        '观察池': base,
        '仅首次突破收盘箱顶': base & d.valid_first20 & (d.prior_close_break20 <= 0),
        '收盘同时超过此前最高价': base & (d.break_high20 > 0),
        '仅首次突破最高价': base & d.valid_first20 & (d.break_high20 > 0) & (d.prior_break20 <= 0),
        '放量至少1.5倍': base & (d.volume_ratio5 >= 1.5),
        '放量至少2倍': base & (d.volume_ratio5 >= 2),
        '当日涨幅超过5%': base & (d.day_change > 5),
        '箱体宽度最多25%': base & (d.width20 <= 25),
        '均线严格多头且60日数据有效': base & d.bullish & d.bullish_valid,
        '首次突破最高价且放量1.5倍': base & d.valid_first20 & (d.break_high20 > 0) & (d.prior_break20 <= 0) & (d.volume_ratio5 >= 1.5),
    }
    columns = ['date', 'code', 'name', 'close', 'day_change', 'volume_ratio5', 'close_position',
               'width20', 'drift20', 'break_close20', 'break_high20', 'prior_close_break20', 'prior_break20']
    candidates = d.loc[base, columns].copy()
    candidates.to_csv(ROOT / 'docs' / (PREFIX + '-candidates.csv'), index=False, encoding='utf-8-sig')
    funnel = []
    mask = d.close.notna()
    for label, condition in [
        ('21根连续有效日线及内部价格参考有效', d.valid20),
        ('此前20日高低范围<=35%', d.width20 <= 35),
        ('前后5日均价差绝对值<=10%', d.drift20.abs() <= 10),
        ('突破最高收盘价>=1%', d.break_close20 >= 1),
        ('量/前5日均量>=1.2', d.volume_ratio5 >= 1.2),
        ('当日涨幅>=2%', d.day_change >= 2),
        ('收盘位置>=60%', d.close_position >= .6),
    ]:
        mask &= condition
        funnel.append({'condition': label, **summarize_mask(d, mask)})
    return {'rules': RULES, 'variants': {k: summarize_mask(d, m) for k, m in variants.items()},
            'period_sensitivity': {n: summarize_mask(d, base_mask(d, n)) for n in WINDOWS},
            'distinct_candidates': int(candidates.code.nunique()), 'funnel': funnel,
            'candidate_dates': sorted(candidates.date.unique().tolist()),
            'caveat': '11个人工指定样本及10个指定日期全市场比较，仅作形态开发；未核验未来收益，不是独立样本外。'}


def explore(payload):
    data = payload['frame']
    comparisons = []
    # A declared sensitivity grid, not a profit optimiser or held-out evaluation.
    for n in WINDOWS:
        for ceiling in ('high', 'close'):
            for width in (20, 25, 30, 40, 50):
                for drift in (10, 15, 20, 100):
                    for vr in (1., 1.5, 2.):
                        mask = (data[f'valid{n}'] & (data[f'break_{ceiling}{n}'] > 0)
                                & (data[f'width{n}'] <= width) & (data[f'drift{n}'].abs() <= drift)
                                & (data.volume_ratio5 >= vr) & (data.day_change >= 5))
                        result = summarize_mask(data, mask)
                        comparisons.append({'n': n, 'ceiling': ceiling, 'width': width, 'drift': drift, 'vr5': vr, **result})
    return comparisons


def export(payload):
    docs = ROOT / 'docs'
    pd.DataFrame(payload['cases']).to_csv(docs / (PREFIX + '-cases.csv'), index=False, encoding='utf-8-sig')
    comparisons = explore(payload)
    result = {k: v for k, v in payload.items() if k != 'frame'}
    result['comparisons'] = comparisons
    result['recommendation'] = recommended(payload)
    result['started_on'] = '2026-10-18'
    result['completed_on'] = '2026-10-19'
    result['universe_counts'] = payload['frame'].groupby('date').agg(present=('close', 'count'), valid20=('valid20', 'sum'), valid60=('valid60', 'sum')).to_dict('index')
    write_json(docs / (PREFIX + '-results.json'), result)
    print(json.dumps(clean({'cases': [{k: r[k] for k in ['name', 'date', 'day_change', 'volume_ratio5', 'volume_ratio20', 'close_position', 'bullish', 'prior_ma5_ma20_gap'] + [f'{k}{n}' for n in WINDOWS for k in ('valid', 'width', 'break_high', 'break_close', 'drift', 'prior_break')]} for r in payload['cases']], 'flows': payload['flows'], 'market': payload['market']}), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--reuse', action='store_true', help='Reuse this research run\'s temporary feature file')
    args = parser.parse_args()
    if args.reuse:
        with CACHE.open('rb') as handle:
            payload = pickle.load(handle)
    else:
        payload = prepare()
    export(payload)
