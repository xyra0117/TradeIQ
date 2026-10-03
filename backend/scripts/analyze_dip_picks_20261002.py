"""Read-only, reproducible dip-pick study. Dated artifacts stay in existing docs/.

Selection uses the actual current screen; outcomes and features never write SQLite.
Entry-date features use only information available on or before the selection date.
"""
import bisect
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
import dip_picks

STAMP = '20261002'
OUT = ROOT / 'docs'
NOW = datetime(2026, 10, 2, 16, tzinfo=ZoneInfo('Asia/Shanghai'))


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    return value


def normalize_day(value):
    return str(value).replace('-', '')[:8]


def ratio(value, base, scale=100):
    return value / base * scale if pd.notna(value) and pd.notna(base) and base != 0 else np.nan


def extract_features(frame, conn, calendar, histories):
    """No feature reads a date later than its row's selection date."""
    print('features: loading funds and market context', flush=True)
    flows = pd.read_sql_query("SELECT * FROM fund_flow WHERE source='eastmoney-push2'", conn)
    flow_by = {code: group.set_index('trade_date') for code, group in flows.groupby('ts_code')}
    ths = pd.read_sql_query('SELECT * FROM fund_flow_ths', conn).set_index(['ts_code', 'trade_date'])
    basic = pd.read_sql_query('SELECT ts_code,listing_date,last_share_sync,total_share,float_share FROM stock_basic', conn).set_index('ts_code')
    members = pd.read_sql_query("SELECT m.ts_code,m.in_date,m.out_date,i.industry_name FROM sw_industry_member m "
                               "JOIN sw_industry i ON i.index_code=m.index_code WHERE i.level='L1'", conn)
    industry_by = {code: group.sort_values('in_date') for code, group in members.groupby('ts_code')}

    def industry(code, day):
        group = industry_by.get(code)
        if group is None:
            return '未知'
        valid = group[(group.in_date.fillna('') <= day) &
                      (group.out_date.isna() | (group.out_date == '') | (group.out_date >= day))]
        return str(valid.iloc[-1].industry_name) if len(valid) else '未知'

    indices = pd.read_sql_query('SELECT * FROM index_daily', conn)
    indices['date'] = indices.date.map(normalize_day)
    index_by = {name: group.drop_duplicates('date', keep='last').set_index('date').sort_index()
                for name, group in indices.groupby('name')}
    overview = pd.read_sql_query('SELECT * FROM overview', conn)
    overview['date'] = overview.date.map(normalize_day)
    overview = overview.drop_duplicates('date', keep='last').set_index('date').sort_index()
    market_flow = pd.read_sql_query('SELECT * FROM market_fflow WHERE complete=1', conn).set_index('trade_date')
    limits = pd.read_sql_query('SELECT date,code,sector,keyword FROM limitup', conn)
    limits['date'] = limits.date.map(normalize_day)
    limits['code'] = limits.code.astype(str).str.split('.').str[0].str.zfill(6)
    limits_by = {code: group.sort_values('date') for code, group in limits.groupby('code')}
    sectors = pd.read_sql_query('SELECT * FROM sector_fund_flow', conn)
    sector_by = {day: group for day, group in sectors.groupby('trade_date')}
    # All local stocks on each entry day support contemporaneous industry breadth.
    entry_dates = sorted(frame.trade_date.unique())
    market_daily = pd.read_sql_query('SELECT ts_code,trade_date,change,amount FROM stock_daily WHERE trade_date IN ('
                                   + ','.join('?' for _ in entry_dates) + ')', conn, params=entry_dates)
    market_daily['industry'] = [industry(code, day) for code, day in zip(market_daily.ts_code, market_daily.trade_date)]
    industry_stats = market_daily.groupby(['trade_date', 'industry']).agg(
        day_return=('change', 'mean'), breadth=('change', lambda s: (s > 0).mean() * 100), count=('ts_code', 'size'))
    # Optional intraday data are used only when date, close and reference price match.
    minute_dates = {r[0] for r in conn.execute('SELECT DISTINCT trade_date FROM stock_minline_bars')}
    minute_by = {}
    for day in entry_dates:
        if day not in minute_dates:
            continue
        codes = frame.loc[frame.trade_date == day, 'ts_code'].tolist()
        minute = pd.read_sql_query('SELECT ts_code,m,p,avg_p,v,prev_close FROM stock_minline_bars '
                                  'WHERE trade_date=? AND ts_code IN (' + ','.join('?' for _ in codes) + ') ORDER BY m',
                                  conn, params=[day, *codes])
        for code, group in minute.groupby('ts_code'):
            minute_by[(code, day)] = group
    index_minutes = pd.read_sql_query('SELECT * FROM index_minline_bars', conn)
    index_minute_by = {(code, day): group.sort_values('m') for (code, day), group in index_minutes.groupby(['ts_code', 'trade_date'])}
    rows = []
    aliases = {'上证指数': 'sh', '深证成指': 'sz', '创业板指': 'cyb', '科创50': 'kc50'}
    index_codes = {'sh': '000001.SH', 'sz': '399001.SZ', 'cyb': '399006.SZ', 'kc50': '000688.SH'}
    for record in frame.to_dict('records'):
        day, code = record['trade_date'], record['ts_code']
        position = bisect.bisect_right(calendar, day)
        dates = calendar[position - 121:position]
        bars = histories[code].reindex(dates)
        close, volume, amount = bars.close, bars.volume, bars.amount
        today, previous = bars.iloc[-1], bars.iloc[-2]
        record.update(board='科创板' if code.startswith(('688', '689')) else '创业板' if code.startswith(('300', '301'))
                      else '北交所' if code.endswith('.BJ') else '主板', industry=industry(code, day), price=today['close'],
                      day_return=today['change'], amount_yi=today['amount'] / 100000,
                      amount5_daily_yi=amount.iloc[-5:].mean() / 100000,
                      opening_gap=(today['open'] / previous['close'] - 1) * 100,
                      volume_vs_prior=ratio(today['volume'], previous['volume'], 1),
                      volume_trend5_20=ratio(volume.iloc[-6:-1].mean(), volume.iloc[-21:-1].mean(), 1),
                      daily_amplitude=ratio(today['high'] - today['low'], previous['close']),
                      close_location=ratio(today['close'] - today['low'], today['high'] - today['low']),
                      lower_wick=ratio(min(today['open'], today['close']) - today['low'], today['high'] - today['low']),
                      upper_wick=ratio(today['high'] - max(today['open'], today['close']), today['high'] - today['low']),
                      drawdown20=(today['close'] / bars.high.iloc[-20:].max() - 1) * 100,
                      drawdown60=(today['close'] / bars.high.iloc[-60:].max() - 1) * 100,
                      volatility20=bars.change.iloc[-20:].std(), negative_days5=int((bars.change.iloc[-5:] < 0).sum()),
                      prior_ret5=(previous['close'] / close.iloc[-7] - 1) * 100)
        ma = {}
        for n in [3, 5, 10, 20, 30, 60, 120]:
            record[f'return{n}'] = (today['close'] / close.iloc[-n - 1] - 1) * 100
        for n in [5, 10, 20, 30, 60, 120]:
            ma[n] = close.iloc[-n:].mean()
            record[f'bias{n}'] = (today['close'] / ma[n] - 1) * 100
            if n <= 60:
                old_ma = close.iloc[-n - 5:-5].mean()
                record[f'ma{n}_slope5'] = (ma[n] / old_ma - 1) * 100
        record['ma5_above_ma20'] = float(ma[5] > ma[20])
        record['bull_stack'] = float(ma[5] > ma[10] > ma[20] > ma[60])
        recent_flows = flow_by[code].reindex(dates[-20:])
        for alias, col in [('main', 'main_net_inflow'), ('super', 'super_net'), ('big', 'big_net'), ('mid', 'mid_net'), ('small', 'small_net')]:
            for n in [1, 3, 5, 10, 20]:
                series = recent_flows[col].iloc[-n:]
                net = series.sum() if series.notna().all() else np.nan
                record[f'{alias}{n}_yi'] = net / 1e8
                record[f'{alias}{n}_strength'] = ratio(net, amount.iloc[-n:].sum() * 1000)
        record['main_positive_days5'] = float((recent_flows.main_net_inflow.iloc[-5:] > 0).sum())
        record['main_acceleration5_20'] = ratio(record['main5_yi'] / 5, record['main20_yi'] / 20, 1)
        record['main_recent3_share'] = ratio(record['main3_yi'], record['main20_yi'])
        record['main_pct_provider'] = recent_flows.iloc[-1].main_net_pct
        record['super_pct_provider'] = recent_flows.iloc[-1].super_pct
        record['small_pct_provider'] = recent_flows.iloc[-1].small_pct
        if (code, day) in ths.index:
            flow = ths.loc[(code, day)]
            record['ths_turnover'] = flow['turnover_pct']
            record['ths_net_yi'] = flow['net'] / 1e8
            record['ths_strength'] = ratio(flow['net'], flow['amount'])
        if code in basic.index:
            listing = normalize_day(basic.loc[code, 'listing_date'])
            try:
                record['listing_years'] = (datetime.strptime(day, '%Y%m%d') - datetime.strptime(listing, '%Y%m%d')).days / 365.25
            except ValueError:
                pass
            # Latest shares are not historical market cap; keep as annotated metadata only.
            record['share_snapshot_date'] = basic.loc[code, 'last_share_sync']
        history_limits = limits_by.get(code.split('.')[0])
        record['prior_limitup_count20'] = 0
        record['prior_limitup_count5'] = 0
        record['historical_themes'] = ''
        if history_limits is not None:
            known = history_limits[(history_limits.date >= dates[-20]) & (history_limits.date <= day)]
            record['prior_limitup_count20'] = known.date.nunique()
            record['prior_limitup_count5'] = known[known.date >= dates[-5]].date.nunique()
            record['historical_themes'] = ' | '.join(sorted(set(str(s) for s in known.sector.dropna() if str(s).strip())))
        record['prior_up9_days20'] = int((bars.change.iloc[-20:-1] > 9).sum())
        if (day, record['industry']) in industry_stats.index and record['industry'] != '未知':
            info = industry_stats.loc[(day, record['industry'])]
            record['industry_day_return'] = info.day_return
            record['industry_breadth'] = info.breadth
            record['relative_industry_return'] = today['change'] - info.day_return
        ov_dates = calendar[position - 21:position]
        ovs = overview.reindex(ov_dates)
        ov = ovs.iloc[-1]
        total_count = ov.upCount + ov.downCount + ov.flatCount
        for col in ['upCount', 'downCount', 'flatCount', 'limitUp', 'limitDown', 'down7', 'down5_7', 'down3_5', 'up7', 'totalVolume', 'volumeDiff']:
            record[f'market_{col}'] = ov[col]
        record['market_down_pct'] = ratio(ov.downCount, total_count)
        record['market_up_pct'] = ratio(ov.upCount, total_count)
        record['market_down5_pct'] = ratio(ov.down7 + ov.down5_7, total_count)
        record['market_up_pct_prev5'] = (ovs.upCount / (ovs.upCount + ovs.downCount + ovs.flatCount) * 100).iloc[-6:-1].mean()
        record['market_turnover_vs5'] = ratio(ov.totalVolume, ovs.totalVolume.iloc[-6:-1].mean(), 1)
        if day in market_flow.index:
            mf = market_flow.loc[day]
            try:
                last, pct = json.loads(mf['last']), json.loads(mf['pct'])
                for alias in ['main', 'super', 'big', 'mid', 'small']:
                    record[f'market_{alias}_net_yi'] = last.get(alias, np.nan) / 1e8
                    record[f'market_{alias}_pct'] = pct.get(alias, np.nan)
            except (TypeError, ValueError):
                pass
        for name, alias in aliases.items():
            data = index_by[name].reindex(calendar[position - 61:position])
            current = data.iloc[-1]
            record[f'index_{alias}_day_return'] = current['change']
            record[f'index_{alias}_bias20'] = (current['close'] / data.close.iloc[-20:].mean() - 1) * 100
            record[f'index_{alias}_turnover_vs5'] = ratio(current['volume'], data.volume.iloc[-6:-1].mean(), 1)
            for n in [5, 20]:
                record[f'index_{alias}_return{n}'] = (current['close'] / data.close.iloc[-n - 1] - 1) * 100
            future_days = calendar[position:position + 5]
            if record['complete'] and len(future_days) == 5 and all(d in index_by[name].index for d in future_days):
                record[f'post_index_{alias}_close5'] = (index_by[name].loc[future_days[-1], 'close'] / current['close'] - 1) * 100
            minute = index_minute_by.get((index_codes[alias], day))
            if minute is not None and len(minute) >= 200 and minute.iloc[-1]['m'].startswith('15:00'):
                quote_prev = minute.iloc[-1].prev_close
                if pd.notna(quote_prev) and abs((quote_prev / data.close.iloc[-2] - 1) * 100) <= .15 and abs((minute.iloc[-1].p / current['close'] - 1) * 100) <= .15:
                    record[f'index_{alias}_intraday_recovery'] = (minute.iloc[-1].p / minute.p.min() - 1) * 100
        if record.get('post_index_sh_close5') is not None:
            record['excess_sh_close5'] = record['close5'] - record['post_index_sh_close5']
        if record.get('post_index_cyb_close5') is not None and record['board'] == '创业板':
            record['excess_board_close5'] = record['close5'] - record['post_index_cyb_close5']
        elif record.get('post_index_kc50_close5') is not None and record['board'] == '科创板':
            record['excess_board_close5'] = record['close5'] - record['post_index_kc50_close5']
        else:
            record['excess_board_close5'] = record.get('excess_sh_close5')
        sd = sector_by.get(day)
        if sd is not None:
            record['sector_positive_flow_pct'] = (sd.main_net_inflow > 0).mean() * 100
            record['sector_positive_return_pct'] = (sd.change_pct > 0).mean() * 100
            for thematic, key in [('半导体', 'semiconductor'), ('PCB', 'pcb'), ('机器人', 'robot'), ('算力', 'computing')]:
                matches = sd[sd.name.str.contains(thematic, regex=False, na=False)]
                if len(matches):
                    record[f'sector_{key}_return'] = matches.iloc[0].change_pct
                    record[f'sector_{key}_main_pct'] = matches.iloc[0].main_net_pct
        minute = minute_by.get((code, day))
        record['minute_valid'] = False
        if minute is not None and len(minute) >= 200 and minute.iloc[-1]['m'].startswith('15:00'):
            pclose, ref = minute.iloc[-1].p, minute.iloc[-1].prev_close
            if (pd.notna(ref) and ref > 0 and abs((ref / previous['close'] - 1) * 100) <= .15
                    and abs((pclose / today['close'] - 1) * 100) <= .15 and (minute.p > 0).all()):
                record['minute_valid'] = True
                record['minute_recovery_from_low'] = (pclose / minute.p.min() - 1) * 100
                record['minute_time_below_prev_pct'] = (minute.p < ref).mean() * 100
                record['minute_vwap_bias'] = (pclose / minute.iloc[-1].avg_p - 1) * 100 if minute.iloc[-1].avg_p > 0 else np.nan
                for clock, key in [('11:30:00', 'afternoon_return'), ('14:30:00', 'last30_return')]:
                    first = minute[minute.m <= clock]
                    if len(first):
                        record[f'minute_{key}'] = (pclose / first.iloc[-1].p - 1) * 100
        rows.append(record)
    result = pd.DataFrame(rows)
    ordinal = {d: i for i, d in enumerate(calendar)}
    last = {}
    nonoverlap = []
    for row in result.itertuples():
        keep = ordinal[row.trade_date] > last.get(row.ts_code, -1000)
        nonoverlap.append(keep)
        if keep:
            last[row.ts_code] = ordinal[row.trade_date] + 5
    result['nonoverlap'] = nonoverlap
    result['first_stock_event'] = ~result.ts_code.duplicated()
    return result


def main():
    conn = sqlite3.connect((ROOT / 'backend/market_data.db').as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    calendar = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache ORDER BY cal_date')]
    counts = dict(conn.execute('SELECT trade_date,COUNT(*) FROM stock_daily WHERE close>0 GROUP BY trade_date'))
    dates = sorted(d for d in counts if d <= NOW.strftime('%Y%m%d'))
    latest = dates[-1]
    gates, events = [], []
    for number, day in enumerate(dates, 1):
        end = bisect.bisect_right(calendar, day)
        market = dip_picks._market_gate(conn, day, calendar[max(0, end - 121):end])
        gate = {'trade_date': day, 'daily_rows': counts[day], **market}
        if market['passed']:
            payload = dip_picks.screen_dip(conn, day, NOW)
            gate.update(stats=payload['stats'], count=payload['count'], note=payload['note'])
            events.extend(payload['results'])
            print('screen', day, payload['count'], flush=True)
        gates.append(gate)
    frame = pd.DataFrame(events)
    if frame.empty:
        raise RuntimeError('No historical dip candidates in the current database.')
    print('selected', len(frame), 'stocks', frame.ts_code.nunique(), 'dates', frame.trade_date.nunique(), flush=True)
    # Read all available history for every selected stock, including amount and post-entry bars.
    daily_parts = []
    codes = sorted(frame.ts_code.unique())
    for i in range(0, len(codes), 400):
        batch = codes[i:i + 400]
        daily_parts.append(pd.read_sql_query('SELECT ts_code,name,trade_date,open,high,low,close,change,volume,amount '
                          'FROM stock_daily WHERE ts_code IN (' + ','.join('?' for _ in batch) + ') ORDER BY trade_date', conn, params=batch))
    daily = pd.concat(daily_parts, ignore_index=True)
    histories = {code: g.set_index('trade_date').sort_index() for code, g in daily.groupby('ts_code')}
    rows = []
    for entry in events:
        row = dict(entry)
        code, day, anchor = row['ts_code'], row['trade_date'], row['close']
        position = bisect.bisect_right(calendar, day)
        future = calendar[position:position + 5]
        history = histories[code]
        previous = anchor
        blocked = None
        highs, lows = [], []
        for n in range(1, 6):
            next_day = future[n - 1] if len(future) >= n else None
            row[f't{n}_date'] = next_day
            row[f't{n}_high_pct'] = None
            row[f't{n}_close_pct'] = None
            if not blocked:
                if not next_day or next_day > latest:
                    blocked = 'pending'
                elif next_day not in history.index:
                    blocked = 'missing_daily'
                else:
                    bar = history.loc[next_day]
                    if (any(not pd.notna(bar[k]) or not np.isfinite(bar[k]) or bar[k] <= 0 for k in ('open', 'high', 'close'))
                            or bar['high'] < max(bar['open'], bar['close']) - .001):
                        blocked = 'invalid_daily'
                    elif not pd.notna(bar['change']) or abs((bar['close'] / previous - 1) * 100 - bar['change']) > .15:
                        blocked = 'reference_mismatch'
                    else:
                        row[f't{n}_high_pct'] = (bar['high'] / anchor - 1) * 100
                        row[f't{n}_close_pct'] = (bar['close'] / anchor - 1) * 100
                        highs.append(row[f't{n}_high_pct'])
                        lows.append((bar['low'] / anchor - 1) * 100 if pd.notna(bar['low']) and 0 < bar['low'] <= min(bar['open'], bar['close']) else np.nan)
                        previous = bar['close']
            row[f't{n}_reason'] = blocked
        row['complete'] = blocked is None
        row['completed_days'] = len(highs)
        row['outcome_status'] = blocked or 'complete'
        row['all_positive'] = row['complete'] and min(highs) > 1e-10
        row['touch10'] = row['complete'] and max(highs) > 10 + 1e-10
        row['both'] = row['all_positive'] and row['touch10']
        row['peak5'] = max(highs) if row['complete'] else None
        row['min_high5'] = min(highs) if row['complete'] else None
        row['close5'] = row['t5_close_pct']
        row['adverse5'] = min(lows) if row['complete'] and all(np.isfinite(lows)) else None
        row['first_touch10_day'] = next((i + 1 for i, gain in enumerate(highs) if gain > 10 + 1e-10), None)
        row['peak_day'] = int(np.argmax(highs)) + 1 if row['complete'] else None
        rows.append(row)
    frame = pd.DataFrame(rows)
    frame = extract_features(frame, conn, calendar, histories)
    frame.to_csv(OUT / f'dip-picks-events-{STAMP}.csv', index=False, encoding='utf-8-sig')
    report = {'asof': latest, 'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'rule_hash': hashlib.sha256((ROOT / 'backend/dip_picks.py').read_bytes()).hexdigest(),
              'date_count': len(dates), 'gate_dates': sum(g['passed'] for g in gates),
              'gates': gates, 'selection_count': len(frame), 'selection_stocks': frame.ts_code.nunique(),
              'selection_dates': frame.trade_date.nunique(), 'statuses': frame.outcome_status.value_counts().to_dict(),
              'query_only': conn.execute('PRAGMA query_only').fetchone()[0]}
    settled = frame[frame.complete]
    report['groups'] = {label: {'events': len(group), 'stocks': group.ts_code.nunique(), 'dates': group.trade_date.nunique(),
                          'close5_median': group.close5.median(), 'peak5_median': group.peak5.median(),
                          'close5_win_rate': (group.close5 > 0).mean() * 100,
                          'close5_loss10_rate': (group.close5 < -10).mean() * 100}
                       for label, group in [('all', settled), ('all_positive', settled[settled.all_positive]),
                                            ('touch10', settled[settled.touch10]), ('both', settled[settled.both])]}
    (OUT / f'dip-picks-analysis-{STAMP}.json').write_text(json.dumps(clean(report), ensure_ascii=False, indent=2))
    print(json.dumps(clean(report['groups']), ensure_ascii=False), flush=True)
    conn.close()


if __name__ == '__main__':
    main()
