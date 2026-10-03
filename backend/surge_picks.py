"""Closed-session surge watchlist; no provider requests, writes, or persistent caches."""
import math
import sqlite3
from contextlib import closing
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _number(value):
    return isinstance(value, (float, int)) and math.isfinite(value)


def _bar_valid(row):
    return (all(_number(row[k]) for k in ('open', 'high', 'low', 'close', 'volume'))
            and row['low'] > 0 and row['volume'] > 0
            and row['low'] <= min(row['open'], row['close'])
            and row['high'] >= max(row['open'], row['close']))


def _reference_valid(row, previous):
    return (_number(row['change']) and previous['close'] > 0
            and abs((row['close'] / previous['close'] - 1) * 100 - row['change']) <= .15)


def _closed(day, now):
    return day < now.strftime('%Y%m%d') or (day == now.strftime('%Y%m%d') and (now.hour, now.minute) >= (15, 5))


def _outcome(rows, future_dates, anchor, now):
    highs = []
    previous = anchor
    for day in future_dates:
        if not _closed(day, now):
            return {'status': 'pending', 'completed_days': len(highs)}
        row = rows.get(day)
        if row is None or not _bar_valid(row):
            return {'status': 'missing', 'completed_days': len(highs), 'missing_date': day}
        if not _reference_valid(row, previous):
            return {'status': 'reference_mismatch', 'completed_days': len(highs)}
        highs.append(row['high'])
        previous = row
    if len(highs) < 10:
        return {'status': 'pending', 'completed_days': len(highs)}
    return {'status': 'complete', 'completed_days': 10, 'end_date': future_dates[-1],
            'high_pct': (max(highs) / anchor['close'] - 1) * 100,
            'close_pct': (previous['close'] / anchor['close'] - 1) * 100}


def screen_surge(conn, requested_date='', now=None):
    """Caller owns the read transaction; injectable time and connection for regression tests."""
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    requested_date = requested_date.strip()
    if requested_date:
        try:
            if len(requested_date) != 8 or not requested_date.isdigit():
                raise ValueError()
            datetime.strptime(requested_date, '%Y%m%d')
        except ValueError:
            raise ValueError('日期格式应为 YYYYMMDD，且必须为有效日期') from None
    today = now.strftime('%Y%m%d')
    counts = dict(conn.execute('SELECT trade_date,COUNT(*) FROM stock_daily WHERE trade_date<=? '
                              'AND close>0 GROUP BY trade_date', (today,)).fetchall())
    available = sorted(d for d in counts if _closed(d, now))
    # An incomplete current-day batch must not be advertised as the latest closing screen.
    if available and available[-1] == today and len(available) > 1:
        if counts[today] < counts[available[-2]] * .9:
            available.pop()
    day = requested_date or (available[-1] if available else '')
    stats = dict(total=0, eligible=0, missing_history=0, invalid_prices=0, reference_mismatch=0,
                 below_amplitude=0, below_ma20=0, no_momentum=0, missing_funds=0)
    result = {'status': 'success', 'strategy': 'surge-watch-v1', 'date': day,
              'requested_date': requested_date, 'daily_ready': False, 'count': 0, 'results': [],
              'stats': stats, 'note': '', 'latest_closed_date': available[-1] if available else None}
    if not day:
        result['note'] = '本地尚无可用收盘日线'
        return result
    if not _closed(day, now):
        result['note'] = '等待该日完整收盘数据；盘中不生成观察名单'
        return result
    if day not in available:
        result['note'] = '该日收盘数据尚未入库或仍不完整'
        return result
    dates = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache '
                                      'WHERE cal_date<=? ORDER BY cal_date DESC LIMIT 21', (day,))][::-1]
    if not dates or dates[-1] != day:
        result['note'] = '本地交易日历中没有该日期'
        return result
    stats['total'] = counts[day]
    result['daily_ready'] = True
    if len(dates) < 21:
        stats['missing_history'] = stats['total']
        result['note'] = '不足连续21个交易日，暂不能计算完整筛选指标'
        return result
    conn.row_factory = sqlite3.Row
    history = defaultdict(dict)
    for row in conn.execute('SELECT ts_code,name,trade_date,open,high,low,close,change,volume,amount '
                            'FROM stock_daily WHERE trade_date BETWEEN ? AND ?', (dates[0], day)):
        history[row['ts_code']][row['trade_date']] = row
    flows = defaultdict(dict)
    for row in conn.execute("SELECT ts_code,trade_date,main_net_inflow FROM fund_flow "
                            "WHERE source='eastmoney-push2' AND trade_date BETWEEN ? AND ?",
                            (dates[-5], day)):
        flows[row['ts_code']][row['trade_date']] = row['main_net_inflow']
    hits = []
    for code in sorted(history):
        data = history[code]
        if day not in data or not _number(data[day]['close']) or data[day]['close'] <= 0:
            continue
        if any(d not in data for d in dates):
            stats['missing_history'] += 1
            continue
        bars = [data[d] for d in dates]
        if not all(_bar_valid(row) for row in bars):
            stats['invalid_prices'] += 1
            continue
        if any(not _reference_valid(bars[i], bars[i-1]) for i in range(1, 21)):
            stats['reference_mismatch'] += 1
            continue
        stats['eligible'] += 1
        amplitude = math.fsum((bars[i]['high'] - bars[i]['low']) / bars[i-1]['close'] * 100
                              for i in range(1, 21)) / 20
        ma20 = math.fsum(row['close'] for row in bars[1:]) / 20
        close = bars[-1]['close']
        ret20 = (close / bars[0]['close'] - 1) * 100
        previous_high = max(row['high'] for row in bars[:-1])
        momentum = ret20 > 15 and not math.isclose(ret20, 15, abs_tol=1e-10, rel_tol=0)
        breakout = close > previous_high
        if amplitude < 5 and not math.isclose(amplitude, 5, abs_tol=1e-10, rel_tol=0):
            stats['below_amplitude'] += 1
            continue
        if close <= ma20:
            stats['below_ma20'] += 1
            continue
        if not (momentum or breakout):
            stats['no_momentum'] += 1
            continue
        values = [flows[code].get(d) for d in dates[-5:]]
        amounts = [data[d]['amount'] for d in dates[-5:]]
        strength = None
        if all(_number(v) for v in values) and all(_number(v) and v > 0 for v in amounts):
            strength = math.fsum(values) / (math.fsum(amounts) * 1000) * 100
        else:
            stats['missing_funds'] += 1
        hits.append({'ts_code': code, 'name': bars[-1]['name'] or code, 'trade_date': day,
                     'close': close, 'change_pct': bars[-1]['change'], 'amplitude20': amplitude,
                     'ma20': ma20, 'ret20': ret20, 'previous_high20': previous_high,
                     'momentum': momentum, 'breakout': breakout, 'main_strength5': strength,
                     'fund_tag': strength is not None and (strength >= 5 or math.isclose(strength, 5, abs_tol=1e-10, rel_tol=0))})
    future_dates = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache '
                                             'WHERE cal_date>? ORDER BY cal_date LIMIT 10', (day,))]
    future = defaultdict(dict)
    if hits and future_dates:
        codes = {r['ts_code'] for r in hits}
        for row in conn.execute('SELECT ts_code,trade_date,open,high,low,close,change,volume '
                                'FROM stock_daily WHERE trade_date>? AND trade_date<=?',
                                (day, min(future_dates[-1], available[-1]))):
            if row['ts_code'] in codes:
                future[row['ts_code']][row['trade_date']] = row
    for row in hits:
        row['outcome10'] = _outcome(future[row['ts_code']], future_dates, history[row['ts_code']][day], now)
        # A past session beyond the latest fully loaded close is pending, not a proven data gap.
        outcome = row['outcome10']
        if outcome['status'] == 'missing' and outcome['missing_date'] > available[-1]:
            outcome['status'] = 'pending'
    result.update(count=len(hits), results=hits, note='按股票代码排列；资金仅作辅助标签，观察名单不是买入信号')
    return result


def register_surge_picks(app, db_path):
    from flask import jsonify, request

    @app.get('/api/picks/surge')
    def api_surge_picks():
        try:
            with closing(sqlite3.connect(Path(db_path()).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                payload = screen_surge(conn, request.args.get('date', ''))
            response = jsonify(payload)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ValueError as error:
            return jsonify(status='error', message=str(error)), 400
        except sqlite3.Error:
            return jsonify(status='error', message='本地行情暂时无法读取，请稍后重试'), 503
