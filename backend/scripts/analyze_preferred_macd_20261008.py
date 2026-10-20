"""Read-only historical association study; no business application imports."""
import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PREFIX = ROOT / 'docs' / 'preferred-macd-20261008'
START = '20250401'
SEED = 20261008
FEATURES = {
    'dif_positive': 'DIF在零轴上',
    'dea_positive': 'DEA在零轴上',
    'both_positive': 'DIF、DEA都在零轴上',
    'both_negative': 'DIF、DEA都在零轴下',
    'red': '红柱（DIF>DEA）',
    'green': '绿柱（DIF<DEA）',
    'gold_today': '入选日金叉',
    'gold5': '近5日出现金叉',
    'dead5': '近5日出现死叉',
    'gold_above5': '近5日零轴上金叉',
    'gold_below5': '近5日零轴下金叉',
    'red_growing': '红柱比昨日放大',
    'red_shrinking': '红柱比昨日缩短',
    'green_shrinking': '绿柱比昨日缩短',
    'green_growing': '绿柱比昨日放大',
    'dif_rising': 'DIF比昨日上升',
    'dif_rising3': 'DIF连续3日上升',
    'dea_rising': 'DEA比昨日上升',
    'fresh_red': '金叉后5日内且仍为红柱',
    'positive_pullback': '双线零轴上且红柱缩短',
}


def open_db(path):
    conn = sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def valid_price(value):
    return value is not None and math.isfinite(value) and value > 0


def valid_bar(row):
    return (all(valid_price(row.get(k)) for k in ('open', 'high', 'low', 'close'))
            and row['high'] >= max(row['open'], row['close'], row['low']) - .001
            and row['low'] <= min(row['open'], row['close']) + .001)


def close_reference_ok(row, previous):
    change = row.get('change')
    return (valid_price(previous) and valid_price(row.get('close'))
            and change is not None and math.isfinite(change)
            and abs((row['close'] / previous - 1) * 100 - change) <= .15)


def indicators(history, calendar_pos, reset=True):
    """Past-only EMA; suspension gaps and unadjusted discontinuities are explicit."""
    output = {}
    ema12 = ema26 = dea = None
    previous = None
    last_pos = None
    segment = []
    total_valid = 0
    recent_break = -10000
    for row in history:
        day = row['trade_date']
        pos = calendar_pos.get(day)
        if pos is None:
            continue
        price = row.get('close')
        if not valid_bar(row):
            if reset:
                ema12 = ema26 = dea = None
                segment = []
            recent_break = total_valid
            previous = None
            continue
        broken = (previous is not None and not close_reference_ok(row, previous))
        gap = last_pos is not None and pos - last_pos > 10
        if broken or gap:
            recent_break = total_valid
            if reset:
                ema12 = ema26 = dea = None
                segment = []
        if ema12 is None:
            ema12 = ema26 = price
            dif = dea = 0.
        else:
            ema12 = price * (2 / 13) + ema12 * (11 / 13)
            ema26 = price * (2 / 27) + ema26 * (25 / 27)
            dif = ema12 - ema26
            dea = dif * .2 + dea * .8
        hist = 2 * (dif - dea)
        gold = bool(segment and dif > dea and segment[-1]['dif'] <= segment[-1]['dea'])
        dead = bool(segment and dif < dea and segment[-1]['dif'] >= segment[-1]['dea'])
        segment.append({'dif': dif, 'dea': dea, 'hist': hist, 'gold': gold, 'dead': dead})
        total_valid += 1
        prev_hist = segment[-2]['hist'] if len(segment) >= 2 else hist
        gold_window = segment[-5:]
        masks = {
            'dif_positive': dif > 0, 'dea_positive': dea > 0,
            'both_positive': dif > 0 and dea > 0,
            'both_negative': dif < 0 and dea < 0,
            'red': hist > 0, 'green': hist < 0,
            'gold_today': gold, 'gold5': any(x['gold'] for x in gold_window),
            'dead5': any(x['dead'] for x in gold_window),
            'gold_above5': any(x['gold'] and x['dif'] > 0 and x['dea'] > 0 for x in gold_window),
            'gold_below5': any(x['gold'] and x['dif'] < 0 and x['dea'] < 0 for x in gold_window),
            'red_growing': hist > 0 and prev_hist > 0 and hist > prev_hist,
            'red_shrinking': hist > 0 and prev_hist > 0 and hist < prev_hist,
            'green_shrinking': hist < 0 and prev_hist < 0 and hist > prev_hist,
            'green_growing': hist < 0 and prev_hist < 0 and hist < prev_hist,
            'dif_rising': len(segment) >= 2 and dif > segment[-2]['dif'],
            'dif_rising3': len(segment) >= 4 and all(segment[j]['dif'] > segment[j-1]['dif'] for j in (-1, -2, -3)),
            'dea_rising': len(segment) >= 2 and dea > segment[-2]['dea'],
        }
        masks['fresh_red'] = masks['red'] and masks['gold5']
        masks['positive_pullback'] = masks['both_positive'] and masks['red_shrinking']
        output[day] = dict(
            dif=dif, dea=dea, hist=hist,
            dif_pct=dif / price * 100, dea_pct=dea / price * 100,
            hist_pct=hist / price * 100,
            dif_slope_pct=(dif - segment[-2]['dif']) / price * 100 if len(segment) >= 2 else 0,
            hist_slope_pct=(hist - prev_hist) / price * 100,
            warmup=len(segment), since_break=total_valid - recent_break,
            flags={k: bool(v) for k, v in masks.items()})
        previous = price
        last_pos = pos
    return output


def outcome(daily_by_day, day, calendar, calendar_pos, entry_close, asof):
    pos = calendar_pos.get(day)
    if pos is None:
        return {'outcome_reason': 'missing_calendar'}
    days = calendar[pos + 1:pos + 6]
    if len(days) != 5 or days[-1] > asof:
        return {'outcome_reason': 'future_unavailable'}
    current = daily_by_day.get(day)
    if not current or not valid_bar(current):
        return {'outcome_reason': 'invalid_entry'}
    if not valid_price(entry_close) or abs(entry_close - current['close']) > .001:
        return {'outcome_reason': 'entry_price_mismatch'}
    previous = entry_close
    path = []
    for d in days:
        row = daily_by_day.get(d)
        if row is None:
            return {'outcome_reason': 'missing_future_daily'}
        if not valid_bar(row):
            return {'outcome_reason': 'invalid_future_daily'}
        if not close_reference_ok(row, previous):
            return {'outcome_reason': 'future_reference_mismatch'}
        path.append((row['high'] / entry_close - 1) * 100)
        previous = row['close']
    peak = max(path)
    return dict(outcome_reason='', peak_pct=peak, winner=bool(peak > 10 + 1e-10),
                close5_pct=(previous / entry_close - 1) * 100,
                peak_day=days[int(np.argmax(path))], end_date=days[-1], high_path=path)


def fisher_two_sided(a, b, c, d):
    n = a + b + c + d
    if not n:
        return 1.
    row = a + b
    success = a + c
    logchoose = lambda n, k: math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    def prob(k):
        return math.exp(logchoose(success, k) + logchoose(n - success, row - k) - logchoose(n, row))
    observed = prob(a)
    return min(1., sum(prob(k) for k in range(max(0, row - (n - success)), min(row, success) + 1)
                       if prob(k) <= observed * (1 + 1e-10)))


def bh_adjust(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [1.] * len(values)
    last = 1.
    for rank in range(len(values) - 1, -1, -1):
        index = order[rank]
        last = min(last, values[index] * len(values) / (rank + 1))
        result[index] = min(1., last)
    return result


def describe(rows):
    n = len(rows)
    wins = sum(r['winner'] for r in rows)
    return dict(n=n, winners=wins, hit_rate=wins / n * 100 if n else None,
                stocks=len({r['ts_code'] for r in rows}), dates=len({r['trade_date'] for r in rows}),
                mean_peak=float(np.mean([r['peak_pct'] for r in rows])) if n else None,
                mean_close5=float(np.mean([r['close5_pct'] for r in rows])) if n else None)


def associations(rows):
    n = len(rows)
    wins = sum(r['winner'] for r in rows)
    output = []
    for feature, label in FEATURES.items():
        matched = [r for r in rows if r['flags'][feature]]
        a = sum(r['winner'] for r in matched)
        b = len(matched) - a
        c = wins - a
        d = n - len(matched) - c
        rate = a / (a + b) * 100 if a + b else None
        other = c / (c + d) * 100 if c + d else None
        output.append(dict(feature=feature, label=label, n=a+b, winners=a,
                           rate=rate, other_n=c+d, other_winners=c, other_rate=other,
                           lift_pp=rate-other if rate is not None and other is not None else None,
                           winner_coverage=a / wins * 100 if wins else None,
                           nonwinner_coverage=b / (n-wins) * 100 if n-wins else None,
                           fisher_p=fisher_two_sided(a,b,c,d), small_sample=a+b < 20))
    q = bh_adjust([r['fisher_p'] for r in output])
    for r, v in zip(output, q):
        r['fisher_q'] = v
    return output


def nonoverlap(rows, calendar_pos):
    last = {}
    kept = []
    for row in sorted(rows, key=lambda r: (r['trade_date'], r['ts_code'])):
        p = calendar_pos[row['trade_date']]
        if row['ts_code'] not in last or p - last[row['ts_code']] >= 5:
            kept.append(row)
            last[row['ts_code']] = p
    return kept


def board(code):
    if code.startswith(('300','301')):
        return '创业板'
    if code.startswith(('688','689')):
        return '科创板'
    if code.endswith('.BJ'):
        return '北交所'
    return '主板'


def clustered_ci(rows, feature, clusters, draws=4000):
    matched = [r for r in rows if r['flags'][feature]]
    other = [r for r in rows if not r['flags'][feature]]
    # A cluster bootstrap cannot manufacture missing rare successes/failures.
    # Suppress misleading degenerate intervals for tiny/zero-cell comparisons.
    if any(len(group) < 20 or sum(r['winner'] for r in group) < 5
           or sum(not r['winner'] for r in group) < 5 for group in (matched, other)):
        return [None,None]
    grouped = defaultdict(list)
    for r in rows:
        grouped[r[clusters]].append(r)
    vectors = []
    for group in grouped.values():
        a = sum(r['winner'] and r['flags'][feature] for r in group)
        n1 = sum(r['flags'][feature] for r in group)
        c = sum(r['winner'] and not r['flags'][feature] for r in group)
        vectors.append([a,n1,c,len(group)-n1])
    if len(vectors) < 2:
        return [None,None]
    arr = np.array(vectors, dtype=float)
    rng = np.random.default_rng(SEED)
    totals = arr[rng.integers(0,len(arr),size=(draws,len(arr)))].sum(axis=1)
    ok = (totals[:,1]>0) & (totals[:,3]>0)
    diff = (totals[ok,0]/totals[ok,1]-totals[ok,2]/totals[ok,3])*100
    return np.percentile(diff,[2.5,97.5]).tolist() if len(diff) else [None,None]


def stratified_permutation(rows, feature, draws=4000):
    """Within-date/parameter/board shuffle, reported with informative-stratum coverage."""
    grouped = defaultdict(list)
    for r in rows:
        grouped[(r['trade_date'],r['snapshot_volume'],r['board'])].append(r)
    groups = []
    for group in grouped.values():
        x = np.array([r['flags'][feature] for r in group],dtype=bool)
        y = np.array([r['winner'] for r in group],dtype=float)
        if x.any() and not x.all() and y.any() and not y.all():
            groups.append((x,y))
    if not groups:
        return dict(p=None,n=0,groups=0,statistic=0)
    statistic = lambda x,y: float(((x-x.mean())*(y-y.mean())).sum())
    observed = sum(statistic(x,y) for x,y in groups)
    rng = np.random.default_rng(SEED)
    extreme = 0
    for _ in range(draws):
        perm = sum(statistic(x,rng.permutation(y)) for x,y in groups)
        extreme += abs(perm) >= abs(observed) - 1e-12
    return dict(p=(extreme+1)/(draws+1),n=sum(len(y) for x,y in groups),groups=len(groups),statistic=observed)


def split_tables(rows):
    groupings = {
        'period': lambda r: '4–6月' if r['trade_date'] < '20260701' else '7–9月' if r['trade_date'] < '20261001' else '10月',
        'month': lambda r:r['trade_date'][:6],
        'snapshot_volume': lambda r:str(r['snapshot_volume']),
        'board': lambda r:r['board'],
    }
    out = {}
    for key, getkey in groupings.items():
        grouped = defaultdict(list)
        for r in rows:
            grouped[getkey(r)].append(r)
        out[key] = {k:dict(summary=describe(v),features=associations(v)) for k,v in sorted(grouped.items())}
    return out


def load_and_analyze(db_path, asof=None):
    conn = open_db(db_path)
    try:
        calendar = [r[0] for r in conn.execute('SELECT cal_date FROM trading_dates_cache ORDER BY cal_date')]
        pos = {d:i for i,d in enumerate(calendar)}
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        today = now.strftime('%Y%m%d')
        complete_days = [d for d in calendar if d < today or (d == today and (now.hour,now.minute) >= (15,0))]
        complete_before = complete_days[-1]
        latest = conn.execute('SELECT max(trade_date) FROM stock_daily').fetchone()[0]
        asof = asof or min(latest,complete_before)
        snapshots = [dict(r) for r in conn.execute('SELECT trade_date,count,results_json,threshold_yi,volume_ratio_pct '
                                                  'FROM conditional_picks_preferred_snapshot ORDER BY trade_date')]
        picks = []
        snapshot_issues = []
        for sn in snapshots:
            items = json.loads(sn['results_json'] or '[]')
            if len(items) != sn['count']:
                snapshot_issues.append({'trade_date':sn['trade_date'],'stored_count':sn['count'],'actual':len(items)})
            seen = set()
            for item in items:
                code = item.get('ts_code')
                if code in seen:
                    snapshot_issues.append({'trade_date':sn['trade_date'],'duplicate_code':code})
                    continue
                seen.add(code)
                picks.append(dict(trade_date=sn['trade_date'],ts_code=code,name=item.get('name'),
                                  entry_close=item.get('close'),snapshot_threshold=sn['threshold_yi'],
                                  snapshot_volume=sn['volume_ratio_pct'],
                                  actual_volume_ratio=item.get('volume_ratio_pct'),
                                  strength5=item.get('preferred_strength5'),board=board(code or '')))
        codes = sorted({r['ts_code'] for r in picks if r['ts_code']})
        daily = {}
        for code in codes:
            daily[code] = [dict(r) for r in conn.execute(
                'SELECT trade_date,open,high,low,close,change,volume,amount FROM stock_daily '
                'WHERE ts_code=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date',(code,START,asof))]
    finally:
        conn.rollback()
        conn.close()
    strict = {code:indicators(hist,pos,True) for code,hist in daily.items()}
    compat = {code:indicators(hist,pos,False) for code,hist in daily.items()}
    daily_maps = {code:{r['trade_date']:r for r in hist} for code,hist in daily.items()}
    ledger = []
    for pick in picks:
        r = dict(pick)
        r.update(outcome(daily_maps.get(r['ts_code'],{}),r['trade_date'],calendar,pos,r['entry_close'],asof))
        signal = strict.get(r['ts_code'],{}).get(r['trade_date'])
        fallback = compat.get(r['ts_code'],{}).get(r['trade_date'])
        r['macd_reason'] = 'missing_indicator' if not signal else 'warmup_under120' if signal['warmup'] < 120 else ''
        if signal:
            r.update(signal)
        r['compat'] = fallback
        r['included'] = not r['outcome_reason'] and not r['macd_reason']
        ledger.append(r)
    main = [r for r in ledger if r['included']]
    compat_rows = [dict(r,**r['compat']) for r in ledger if not r['outcome_reason'] and r['compat'] and r['compat']['warmup']>=120]
    subsets = {
        'primary':main,
        'warmup60':[r for r in ledger if not r['outcome_reason'] and r.get('warmup',0)>=60],
        'warmup150':[r for r in ledger if not r['outcome_reason'] and r.get('warmup',0)>=150],
        'page_compatible120':compat_rows,
        'nonoverlap':nonoverlap(main,pos),
        'actual_volume_le80':[r for r in main if r['actual_volume_ratio'] is not None and r['actual_volume_ratio']<=80],
    }
    tables = {k:dict(summary=describe(v),features=associations(v)) for k,v in subsets.items()}
    continuous = {}
    for key in ('dif_pct','dea_pct','hist_pct','dif_slope_pct','hist_slope_pct'):
        continuous[key] = {
            'winner_median':float(np.median([r[key] for r in main if r['winner']])) if any(r['winner'] for r in main) else None,
            'other_median':float(np.median([r[key] for r in main if not r['winner']])) if any(not r['winner'] for r in main) else None,
        }
    inference = {}
    for feature in FEATURES:
        inference[feature] = dict(date_ci=clustered_ci(main,feature,'trade_date'),
                                  stock_ci=clustered_ci(main,feature,'ts_code'),
                                  permutation=stratified_permutation(main,feature))
    qperm = bh_adjust([inference[k]['permutation']['p'] if inference[k]['permutation']['p'] is not None else 1. for k in FEATURES])
    for key,q in zip(FEATURES,qperm):
        inference[key]['permutation']['q'] = q
    data_audit = dict(asof=asof,snapshot_dates=len(snapshots),snapshot_nonzero_dates=sum(s['count']>0 for s in snapshots),
                      first_snapshot=snapshots[0]['trade_date'] if snapshots else None,last_snapshot=snapshots[-1]['trade_date'] if snapshots else None,
                      saved_events=len(picks),saved_stocks=len(codes),daily_rows=sum(len(x) for x in daily.values()),
                      outcome_exclusions=dict(Counter(r['outcome_reason'] for r in ledger if r['outcome_reason'])),
                      complete_outcomes=sum(not r['outcome_reason'] for r in ledger),
                      macd_exclusions_among_complete=dict(Counter(r['macd_reason'] for r in ledger if not r['outcome_reason'] and r['macd_reason'])),
                      snapshot_issues=snapshot_issues,
                      stored_parameters=[dict(threshold=t,volume=v,n=sum(r['snapshot_threshold']==t and r['snapshot_volume']==v for r in ledger))
                                         for t,v in sorted({(r['snapshot_threshold'],r['snapshot_volume']) for r in ledger})])
    return dict(protocol='preferred-macd-20261008-protocol.md',audit=data_audit,tables=tables,
                splits=split_tables(main),continuous=continuous,inference=inference),ledger


def export(result,ledger,prefix):
    Path(str(prefix)+'-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    columns = ['trade_date','ts_code','name','snapshot_threshold','snapshot_volume','actual_volume_ratio','strength5',
               'entry_close','board','included','outcome_reason','macd_reason','peak_pct','winner','close5_pct','peak_day','end_date',
               'warmup','since_break','dif','dea','hist','dif_pct','dea_pct','hist_pct','dif_slope_pct','hist_slope_pct'] + list(FEATURES)
    with Path(str(prefix)+'-events.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
        writer.writeheader()
        for r in ledger:
            writer.writerow(dict(r,**r.get('flags',{})))
    with Path(str(prefix)+'-associations.csv').open('w',newline='',encoding='utf-8-sig') as f:
        cols = list(result['tables']['primary']['features'][0])
        writer = csv.DictWriter(f,fieldnames=['subset']+cols)
        writer.writeheader()
        for subset,table in result['tables'].items():
            for row in table['features']:
                writer.writerow(dict(subset=subset,**row))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,default=ROOT/'backend/market_data.db')
    parser.add_argument('--asof',default=None)
    parser.add_argument('--output-prefix',type=Path,default=PREFIX)
    args = parser.parse_args()
    result,ledger = load_and_analyze(args.db,args.asof)
    export(result,ledger,args.output_prefix)
    print(json.dumps(dict(audit=result['audit'],primary=result['tables']['primary']['summary']),ensure_ascii=False))
    for row in sorted(result['tables']['primary']['features'],key=lambda r:r['fisher_p']):
        print(json.dumps(row,ensure_ascii=False))


if __name__ == '__main__':
    main()
