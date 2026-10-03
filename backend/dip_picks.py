"""Close-based dip screen over existing local data; no writes or provider requests."""
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from surge_picks import _bar_valid, _closed, _number, _reference_valid


def _strictly_below(value, threshold):
    return value < threshold and not math.isclose(value, threshold, abs_tol=1e-10, rel_tol=0)


def _market_gate(conn, day, dates):
    market = {'status': 'missing', 'passed': False, 'down_count': None,
              'index_change': None, 'previous_three': [], 'previous_three_up': None,
              'note': ''}
    iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
    overview = conn.execute('SELECT downCount FROM overview WHERE date IN (?,?) '
                            'ORDER BY date DESC LIMIT 1', (day, iso)).fetchone()
    index = conn.execute("SELECT change FROM index_daily WHERE name='上证指数' "
                         'AND date IN (?,?) ORDER BY date DESC LIMIT 1', (day, iso)).fetchone()
    market['down_count'] = overview[0] if overview else None
    market['index_change'] = index[0] if index else None
    down, change = market['down_count'], market['index_change']
    if not _number(down) or down < 0 or not _number(change):
        market['note'] = '同日下跌家数或上证涨跌幅缺失，暂不推荐'
        return market
    previous = dates[-4:-1]
    for date in previous:
        iso_date = f'{date[:4]}-{date[4:6]}-{date[6:]}'
        row = conn.execute("SELECT change FROM index_daily WHERE name='上证指数' "
                           'AND date IN (?,?) ORDER BY date DESC LIMIT 1', (date, iso_date)).fetchone()
        market['previous_three'].append({'date': date, 'change_pct': row[0] if row else None})
    if len(previous) == 3 and all(_number(r['change_pct']) for r in market['previous_three']):
        market['previous_three_up'] = all(r['change_pct'] > 0 for r in market['previous_three'])
    market['status'] = 'unmet'
    if down <= 2800:
        market['note'] = f'下跌家数 {int(down)} 家，未超过2800家，不推荐'
    elif change >= 0:
        market['note'] = '上证当日未下跌，不推荐'
    elif math.isclose(change, -2, abs_tol=1e-10, rel_tol=0):
        market['note'] = '上证恰好下跌−2%，未满足严格不等式，不推荐'
    elif change < -2:
        market.update(status='matched', passed=True, note='下跌家数>2800且上证下跌超过2%，大盘条件触发')
    elif market['previous_three_up'] is None:
        market.update(status='missing', note='上证此前三个交易日数据不足，暂不推荐')
    elif market['previous_three_up']:
        market['note'] = '上证跌幅不足2%，且此前三个交易日连续上涨，不推荐'
    else:
        market.update(status='matched', passed=True, note='下跌家数>2800，上证跌幅不足2%且此前三日非连涨，大盘条件触发')
    return market


def screen_dip(conn, requested_date='', now=None):
    """The caller owns a consistent read transaction. Time is injectable for tests."""
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
    comparison = '<=' if _closed(today, now) else '<'
    counts = dict(conn.execute('SELECT trade_date,COUNT(*) FROM stock_daily WHERE trade_date' + comparison + '? '
                              'AND close>0 GROUP BY trade_date ORDER BY trade_date DESC LIMIT 2', (today,)).fetchall())
    available = sorted(counts)
    # Avoid treating a partial latest batch as a complete market-wide close.
    if len(available) > 1 and counts[available[-1]] < counts[available[-2]] * .9:
        available.pop()
    day = requested_date or (available[-1] if available else '')
    stats = dict(total=0, declining=0, eligible=0, missing_history=0, invalid_prices=0,
                 reference_mismatch=0, missing_funds=0, funds_not_red=0, funds_below_threshold=0,
                 return_too_high=0, volume_too_high=0, below_ma20=0, inactive=0)
    result = {'status': 'success', 'strategy': 'dip-picks-v1', 'date': day,
              'requested_date': requested_date, 'latest_closed_date': available[-1] if available else None,
              'daily_ready': False, 'market_passed': False, 'market': None,
              'count': 0, 'results': [], 'stats': stats, 'note': ''}
    if not day:
        result['note'] = '本地尚无可用收盘日线'
        return result
    if not _closed(day, now):
        result['note'] = '等待该日完整收盘数据；盘中不生成低吸名单'
        return result
    count = counts.get(day)
    if count is None:
        count = conn.execute('SELECT COUNT(*) FROM stock_daily WHERE trade_date=? AND close>0', (day,)).fetchone()[0]
    latest_incomplete = bool(counts and day == max(counts) and day not in available)
    if not count or latest_incomplete:
        result['note'] = '该日收盘数据尚未入库或仍不完整'
        return result
    dates = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache '
                                      'WHERE cal_date<=? ORDER BY cal_date DESC LIMIT 121', (day,))][::-1]
    if not dates or dates[-1] != day:
        result['note'] = '本地交易日历中没有该日期'
        return result
    result['daily_ready'] = True
    stats['total'] = count
    market = _market_gate(conn, day, dates)
    result.update(market=market, market_passed=market['passed'], note=market['note'])
    if not market['passed']:
        return result
    if len(dates) < 121:
        stats['missing_history'] = stats['total']
        result['note'] = '不足连续121个交易日，暂不能计算120日累计涨幅'
        return result

    conn.row_factory = sqlite3.Row
    candidates = {r['ts_code']: r for r in conn.execute(
        'SELECT ts_code,name,close,change FROM stock_daily WHERE trade_date=? AND change<0 AND close>0', (day,))}
    stats['declining'] = len(candidates)
    flows = defaultdict(dict)
    for row in conn.execute("SELECT ts_code,trade_date,main_net_inflow FROM fund_flow "
                            "WHERE source='eastmoney-push2' AND trade_date BETWEEN ? AND ?", (dates[-20], day)):
        if row['ts_code'] in candidates:
            flows[row['ts_code']][row['trade_date']] = row['main_net_inflow']
    funds = {}
    for code in sorted(candidates):
        values = [flows[code].get(d) for d in dates[-20:]]
        if not all(_number(v) for v in values):
            stats['missing_funds'] += 1
            continue
        sums = {key: math.fsum(values[-n:]) for key, n in (
            ('main_net_today', 1), ('main_net_3d', 3), ('main_net_5d', 5),
            ('main_net_10d', 10), ('main_net_20d', 20))}
        cumulative = [sums[key] for key in ('main_net_3d', 'main_net_5d', 'main_net_10d', 'main_net_20d')]
        if not all(v > 0 for v in cumulative):
            stats['funds_not_red'] += 1
        elif max(cumulative) <= 50_000_000:
            stats['funds_below_threshold'] += 1
        else:
            funds[code] = sums

    # Read long histories only for stocks passing the cheaper same-day and fund conditions.
    history = defaultdict(dict)
    codes = sorted(funds)
    for start in range(0, len(codes), 500):
        batch = codes[start:start + 500]
        query = ('SELECT ts_code,name,trade_date,open,high,low,close,change,volume '
                 'FROM stock_daily WHERE trade_date BETWEEN ? AND ? AND ts_code IN ('
                 + ','.join('?' for _ in batch) + ')')
        for row in conn.execute(query, (dates[0], day, *batch)):
            history[row['ts_code']][row['trade_date']] = row
    hits = []
    for code in codes:
        data = history[code]
        if any(d not in data for d in dates):
            stats['missing_history'] += 1
            continue
        bars = [data[d] for d in dates]
        if not all(_bar_valid(row) for row in bars):
            stats['invalid_prices'] += 1
            continue
        if any(not _reference_valid(bars[i], bars[i - 1]) for i in range(1, 121)):
            stats['reference_mismatch'] += 1
            continue
        stats['eligible'] += 1
        close = bars[-1]['close']
        ret120 = (close / bars[0]['close'] - 1) * 100
        volume_ratio = bars[-1]['volume'] / (math.fsum(r['volume'] for r in bars[-6:-1]) / 5)
        ma20 = math.fsum(r['close'] for r in bars[-20:]) / 20
        amplitude = math.fsum((bars[i]['high'] - bars[i]['low']) / bars[i - 1]['close'] * 100
                              for i in range(101, 121)) / 20
        if not _strictly_below(ret120, 300):
            stats['return_too_high'] += 1
        elif not _strictly_below(volume_ratio, 1.2):
            stats['volume_too_high'] += 1
        elif close <= ma20:
            stats['below_ma20'] += 1
        elif amplitude < 5 and not math.isclose(amplitude, 5, abs_tol=1e-10, rel_tol=0):
            stats['inactive'] += 1
        else:
            hits.append({'ts_code': code, 'name': bars[-1]['name'] or code, 'trade_date': day,
                         'close': close, 'change_pct': bars[-1]['change'], 'ret120': ret120,
                         'volume_ratio5': volume_ratio, 'ma20': ma20, 'amplitude20': amplitude,
                         **funds[code]})
    result.update(results=hits, count=len(hits), note='全部条件匹配才入选；按股票代码排列' if hits
                  else '该日没有同时满足全部个股条件的股票，不推荐；缺失或异常数据已排除')
    return result


def screen_dip_v2(conn, requested_date='', now=None):
    """Filter the original close-based list using entry-date research criteria only."""
    result = screen_dip(conn, requested_date, now)
    result['strategy'] = 'dip-picks-v2'
    stats = dict(base_count=result['count'], too_far_ma20=0, above_ma60=0,
                 invalid_amount=0, weak_main5=0, matched=0)
    result['v2_stats'] = stats
    if not result['results']:
        return result
    day = result['date']
    dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT cal_date FROM trading_dates_cache WHERE cal_date<=? '
        'ORDER BY cal_date DESC LIMIT 60', (day,))][::-1]
    hits = []
    for row in result['results']:
        bias20 = (row['close'] / row['ma20'] - 1) * 100
        if bias20 > 5 and not math.isclose(bias20, 5, abs_tol=1e-10, rel_tol=0):
            stats['too_far_ma20'] += 1
            continue
        # Original selection already verified every one of these 60 daily bars.
        bars = conn.execute('SELECT trade_date,close,amount FROM stock_daily '
                            'WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date',
                            (row['ts_code'], dates[0], day)).fetchall()
        ma60 = math.fsum(bar['close'] for bar in bars) / 60
        if row['close'] > ma60 and not math.isclose(row['close'], ma60, abs_tol=1e-10, rel_tol=0):
            stats['above_ma60'] += 1
            continue
        amounts = [bar['amount'] for bar in bars[-5:]]
        if not all(_number(amount) and amount > 0 for amount in amounts):
            stats['invalid_amount'] += 1
            continue
        strength5 = row['main_net_5d'] / (math.fsum(amounts) * 1000) * 100
        if strength5 < 5 and not math.isclose(strength5, 5, abs_tol=1e-10, rel_tol=0):
            stats['weak_main5'] += 1
            continue
        hits.append({**row, 'ma20_bias_pct': bias20, 'ma60': ma60, 'main_strength5_pct': strength5})
    stats['matched'] = len(hits)
    result.update(results=hits, count=len(hits),
                  note='原低吸条件及三个新增条件全部匹配；按股票代码排列' if hits
                  else '原低吸名单中没有同时满足三个新增条件的股票，不推荐')
    return result


def register_dip_picks(app, db_path, outcome_loader=None):
    from flask import jsonify, request

    def respond(screen):
        try:
            with closing(sqlite3.connect(Path(db_path()).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                payload = screen(conn, request.args.get('date', ''))
                # Enrich only after selecting the close-based list; future bars cannot change membership.
                payload.update(next_dates=[], live_next_index=0, intraday_used=False)
                if outcome_loader is not None and payload['daily_ready'] and payload['market_passed']:
                    days, live_index = outcome_loader(payload['results'], payload['date'], conn, key='dip_outcomes')
                    payload.update(next_dates=days, live_next_index=live_index,
                                   intraday_used=any(o.get('reason') == 'intraday'
                                                     for row in payload['results'] for o in row.get('dip_outcomes', [])))
            response = jsonify(payload)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ValueError as error:
            return jsonify(status='error', message=str(error)), 400
        except sqlite3.Error:
            return jsonify(status='error', message='本地行情暂时无法读取，请稍后重试'), 503

    @app.get('/api/picks/dip')
    def api_dip_picks():
        return respond(screen_dip)

    @app.get('/api/picks/dip-v2')
    def api_dip_picks_v2():
        return respond(screen_dip_v2)
