"""Close-based dip screen over existing local data; no writes or provider requests."""
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from itertools import combinations
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from surge_picks import (attach_sector_history, flow_source_for_range, _bar_valid, _closed,
                         _flow_where, _number, _reference_valid)
from intraday_picks import picks_connection, down_state


# Public rule keys are stable across the UI and read-only API.
DIP_CONDITIONS = ('market_down', 'market_index', 'decline', 'return120', 'funds_red',
                  'funds_threshold', 'volume', 'above_ma20', 'amplitude', 'bullish')
CHASE_CONDITIONS = ('market_down', 'market_up', 'gain', 'return120', 'funds_red',
                    'funds_threshold', 'volume', 'above_ma20', 'amplitude', 'not_limit', 'bullish')
_condition_scan_lock = Lock()


def _chase_disabled(value):
    keys = set(filter(None, (key.strip() for key in value.split(',')))) if isinstance(value, str) else set(value or ())
    if keys - set(CHASE_CONDITIONS):
        raise ValueError('未知追高条件，请使用页面提供的条件开关')
    return keys


def _dip_disabled(value):
    keys = set(filter(None, (key.strip() for key in value.split(',')))) if isinstance(value, str) else set(value or ())
    if keys - set(DIP_CONDITIONS):
        raise ValueError('未知低吸条件，请使用页面提供的条件开关')
    return keys


def _valid_window(data, dates, length):
    """Optional display metrics use the same continuity and reference checks as filters."""
    if len(dates) < length or any(day not in data for day in dates[-length:]):
        return None
    bars = [data[day] for day in dates[-length:]]
    if not all(_bar_valid(row) for row in bars):
        return None
    if any(not _reference_valid(bars[i], bars[i - 1]) for i in range(1, length)):
        return None
    return bars


def _strictly_below(value, threshold):
    return value < threshold and not math.isclose(value, threshold, abs_tol=1e-10, rel_tol=0)


DIP_PARAMETER_DEFAULTS = dict(return_days=120, return_op='lt', return_threshold=300,
                              change_op='lt', change_threshold=0, change_mode='single',
                              change_lower=-5, change_upper=0)


def _dip_parameters(**values):
    if set(values) - set(DIP_PARAMETER_DEFAULTS):
        raise ValueError('未知低吸自定义参数')
    settings = {**DIP_PARAMETER_DEFAULTS, **values}
    try:
        days = float(settings['return_days'])
        if not math.isfinite(days) or not days.is_integer() or not 1 <= days <= 250:
            raise ValueError()
        settings['return_days'] = int(days)
        for key in ('return_threshold', 'change_threshold', 'change_lower', 'change_upper'):
            settings[key] = float(settings[key])
            if not math.isfinite(settings[key]) or abs(settings[key]) > 1_000_000:
                raise ValueError()
        if settings['change_mode'] not in ('single', 'range'):
            raise ValueError()
        if any(settings[key] not in ('lt', 'le', 'gt', 'ge') for key in ('return_op', 'change_op')):
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('累计涨幅周期须为1～250个交易日，比较方向须为<、≤、>、≥，百分比须为有效数字') from None
    if settings['change_mode'] == 'range' and settings['change_lower'] >= settings['change_upper']:
        raise ValueError('当日涨跌幅区间的下限必须小于上限，例如大于−5%且小于0%')
    return settings


def _matches_parameter(value, op, threshold):
    if not _number(value):
        return False
    equal = math.isclose(value, threshold, abs_tol=1e-10, rel_tol=0)
    return {'lt': value < threshold and not equal, 'le': value < threshold or equal,
            'gt': value > threshold and not equal, 'ge': value > threshold or equal}[op]


def _matches_daily_change(value, parameters):
    if parameters['change_mode'] == 'range':
        return (_matches_parameter(value, 'gt', parameters['change_lower'])
                and _matches_parameter(value, 'lt', parameters['change_upper']))
    return _matches_parameter(value, parameters['change_op'], parameters['change_threshold'])


def _market_gate(conn, day, dates, disabled=()):
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
    breadth = getattr(conn, 'intraday_breadth', None)
    down_passed = down_state(conn) if breadth is not None else (down > 2600 if _number(down) and down >= 0 else None)
    if breadth is not None:
        market.update(down_count=breadth['down'], down_count_max=breadth['down']+breadth['unknown'])
        down = breadth['down']
    check_down = 'market_down' not in disabled
    check_index = 'market_index' not in disabled
    if ((check_down and down_passed is None) or (check_index and not _number(change))):
        market['note'] = '同日下跌家数或上证涨跌幅缺失，暂不推荐'
        return market
    if not _number(down) or down < 0:
        market['down_count'] = None
    if not _number(change):
        market['index_change'] = None
    previous = dates[-4:-1]
    for date in previous:
        iso_date = f'{date[:4]}-{date[4:6]}-{date[6:]}'
        row = conn.execute("SELECT change FROM index_daily WHERE name='上证指数' "
                           'AND date IN (?,?) ORDER BY date DESC LIMIT 1', (date, iso_date)).fetchone()
        market['previous_three'].append({'date': date, 'change_pct': row[0] if row else None})
    if len(previous) == 3 and all(_number(r['change_pct']) for r in market['previous_three']):
        market['previous_three_up'] = all(r['change_pct'] > 0 for r in market['previous_three'])
    market['status'] = 'unmet'
    if check_down and down_passed is False:
        market['note'] = f'下跌家数 {int(down)} 家，未超过2600家，不推荐'
    elif not check_index:
        market.update(status='matched', passed=True, note='上证走势条件已关闭，大盘已开启条件通过' if check_down else '两项大盘条件均已关闭，按已开启的个股条件筛选')
    elif change >= 0:
        market['note'] = '上证当日未下跌，不推荐'
    elif math.isclose(change, -1.6, abs_tol=1e-10, rel_tol=0):
        market['note'] = '上证恰好下跌−1.6%，未满足严格不等式，不推荐'
    elif change < -1.6:
        market.update(status='matched', passed=True, note=('下跌家数>2600且' if check_down else '') + '上证下跌超过1.6%，大盘条件触发')
    elif market['previous_three_up'] is None:
        market.update(status='missing', note='上证此前三个交易日数据不足，暂不推荐')
    elif market['previous_three_up']:
        market['note'] = '上证跌幅不足1.6%，且此前三个交易日连续上涨，不推荐'
    else:
        market.update(status='matched', passed=True, note=('下跌家数>2600，' if check_down else '') + '上证跌幅不足1.6%且此前三日非连涨，大盘条件触发')
    return market


def _market_gate_v2(conn, day, dates, disabled=()):
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
    breadth = getattr(conn, 'intraday_breadth', None)
    down_passed = down_state(conn, below=True) if breadth is not None else (down < 2600 if _number(down) and down >= 0 else None)
    if breadth is not None:
        market.update(down_count=breadth['down'], down_count_max=breadth['down']+breadth['unknown'])
        down = breadth['down']
    check_down = 'market_down' not in disabled
    check_up = 'market_up' not in disabled
    if ((check_down and down_passed is None)
            or (check_up and not _number(change))):
        market['note'] = '已开启大盘条件所需的同日数据缺失，暂不推荐'
        return market
    # Never emit non-finite numbers for disabled, display-only fields.
    if not _number(down) or down < 0:
        market['down_count'] = None
    if not _number(change):
        market['index_change'] = None
    market['status'] = 'unmet'
    if check_down and down_passed is False:
        market['note'] = f'下跌家数 {int(down)} 家，未少于2600家，不推荐'
    elif check_up and change <= 0:
        market['note'] = '上证当日未上涨，不推荐'
    else:
        note = ('下跌家数<2600且上证当日上涨，大盘条件触发' if check_down and check_up
                else '下跌家数<2600，大盘已开启条件通过' if check_down
                else '上证当日上涨，大盘已开启条件通过' if check_up
                else '两项大盘条件均已关闭，按已开启的个股条件筛选')
        market.update(status='matched', passed=True, note=note)
    return market


def _limit_up_price_for(code, name, previous_close, day):
    """Historical board limits; use entry-day names instead of current ST metadata."""
    if not _number(previous_close) or previous_close <= 0:
        return None
    name = (name or '').strip().upper()
    st = name.startswith(('ST', '*ST', 'SST', 'S*ST'))
    if code.endswith('.BJ') and len(code) == 9 and code[:6].isdigit():
        pct = 30
    elif code.endswith('.SH') and code.startswith(('688', '689')):
        pct = 20
    elif code.endswith('.SZ') and code.startswith(('300', '301')):
        if day < '20200824' and (not name or name == code):
            return None
        pct = 20 if day >= '20200824' else 5 if st else 10
    elif (code.endswith('.SH') and code.startswith('60')) or (code.endswith('.SZ') and code.startswith('00')):
        if day < '20260706' and (not name or name == code):
            return None
        pct = 5 if st and day < '20260706' else 10
    else:
        return None
    price = Decimal(str(previous_close)) * (Decimal(1) + Decimal(pct) / 100)
    return float(price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)), pct


def _daily_context(conn, requested_date, now=None, history_days=121):
    """Shared date/readiness checks for selection and independent condition counts."""
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
    live = getattr(conn, 'intraday_context', None)
    if live is not None and (not requested_date or requested_date == live['date']):
        dates = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache WHERE cal_date<=? ORDER BY cal_date DESC LIMIT ?', (live['date'], history_days))][::-1]
        return dict(live, requested_date=requested_date), dates
    comparison = '<=' if _closed(today, now) else '<'
    counts = dict(conn.execute('SELECT trade_date,COUNT(*) FROM stock_daily WHERE trade_date' + comparison + '? '
                              'AND close>0 GROUP BY trade_date ORDER BY trade_date DESC LIMIT 2', (today,)).fetchall())
    available = sorted(counts)
    if len(available) > 1 and counts[available[-1]] < counts[available[-2]] * .9:
        available.pop()
    day = requested_date or (available[-1] if available else '')
    context = dict(date=day, requested_date=requested_date,
                   latest_closed_date=available[-1] if available else None,
                   daily_ready=False, total=0, note='')
    if not day:
        context['note'] = '本地尚无可用收盘日线'
        return context, []
    if not _closed(day, now):
        context['note'] = '等待该日完整收盘数据；盘中不生成低吸名单'
        return context, []
    count = counts.get(day)
    if count is None:
        count = conn.execute('SELECT COUNT(*) FROM stock_daily WHERE trade_date=? AND close>0', (day,)).fetchone()[0]
    if not count or (counts and day == max(counts) and day not in available):
        context['note'] = '该日收盘数据尚未入库或仍不完整'
        return context, []
    dates = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache '
                                      'WHERE cal_date<=? ORDER BY cal_date DESC LIMIT ?', (day, history_days))][::-1]
    if not dates or dates[-1] != day:
        context['note'] = '本地交易日历中没有该日期'
        return context, []
    context.update(daily_ready=True, total=count)
    return context, dates


def _daily_metrics(data, dates, code, day, return_days=120):
    # Validate the longest contiguous suffix once. Each metric uses its own window;
    # a gap or reference error in an older window cannot hide valid shorter metrics.
    recent = []
    for date in reversed(dates):
        row = data.get(date)
        if row is None or not _bar_valid(row):
            break
        if recent and not _reference_valid(recent[-1], row):
            break
        recent.append(row)
    bars = recent[::-1]
    close = data[day]['close']
    metrics = {'change_pct': data[day]['change'] if len(bars) >= 2 else None}
    metrics['ret120'] = (close / bars[-121]['close'] - 1) * 100 if len(bars) >= 121 else None
    metrics['ret_custom'] = (close / bars[-return_days - 1]['close'] - 1) * 100 if len(bars) > return_days else None
    metrics['volume_ratio5'] = bars[-1]['volume'] / (math.fsum(r['volume'] for r in bars[-6:-1]) / 5) if len(bars) >= 6 else None
    for n in (5, 10, 20, 60):
        metrics[f'ma{n}'] = math.fsum(r['close'] for r in bars[-n:]) / n if len(bars) >= n else None
    window = bars[-21:]
    metrics['amplitude20'] = math.fsum((window[i]['high'] - window[i]['low']) / window[i - 1]['close'] * 100
                                      for i in range(1, 21)) / 20 if len(bars) >= 21 else None
    limit = _limit_up_price_for(code, data[day]['name'], bars[-2]['close'], day) if len(bars) >= 2 else None
    metrics['limit_up_price'], metrics['limit_pct'] = limit or (None, None)
    return metrics


def _screen_dip(conn, requested_date, now, market_gate, rising=False, disabled=(), parameters=None):
    """The caller owns a consistent read transaction. Time is injectable for tests."""
    def on(key):
        return key not in disabled
    parameters = parameters or DIP_PARAMETER_DEFAULTS
    windows = [('return120', parameters['return_days'] + 1), ('amplitude', 21), ('above_ma20', 20), ('volume', 6), ('bullish', 60)]
    windows += [('not_limit', 2), ('gain', 2)] if rising else [('decline', 2)]
    required_days = max([1] + [n for key, n in windows if on(key)])
    context, dates = _daily_context(conn, requested_date, now, max(121, parameters['return_days'] + 1))
    day = context['date']
    stats = dict(total=context['total'], declining=0, eligible=0, missing_history=0, invalid_prices=0,
                 reference_mismatch=0, missing_funds=0, funds_display_missing=0, funds_not_red=0, funds_below_threshold=0,
                 return_too_high=0, volume_too_high=0, below_ma20=0, inactive=0, not_bullish=0)
    result = {'status': 'success', 'strategy': 'dip-picks-v1',
              **{key: value for key, value in context.items() if key != 'total'},
              'market_passed': False, 'market': None, 'count': 0, 'results': [], 'stats': stats}
    result['conditions'] = {key: on(key) for key in (CHASE_CONDITIONS if rising else DIP_CONDITIONS)}
    result['required_history_days'] = required_days
    if not rising:
        result['parameters'] = dict(parameters)
    if rising:
        result['strategy'] = 'dip-picks-v2'
        result['conditions'] = {key: on(key) for key in CHASE_CONDITIONS}
        result['required_history_days'] = required_days
        stats.update(candidate_count=0, funds_display_missing=0, strong_rising=0)
        result['v2_stats'] = dict(base_count=0, missing_limit=0, limit_up=0, not_bullish=0, matched=0)
    if not context.get('selection_ready', context['daily_ready']):
        return result
    market = market_gate(conn, day, dates)
    result.update(market=market, market_passed=market['passed'], note=market['note'])
    if not market['passed']:
        return result
    if len(dates) < required_days or ((on('funds_red') or on('funds_threshold')) and len(dates) < 20):
        stats['missing_history'] = stats['total']
        result['note'] = f'不足连续{required_days}个交易日，暂不能计算{parameters["return_days"]}日累计涨幅' if on('return120') and required_days == parameters['return_days'] + 1 else '本地交易日历不足已开启条件所需窗口'
        return result

    conn.row_factory = sqlite3.Row
    change_filter = ('change>5' if on('gain') else '1=1') if rising else '1=1'
    candidates = {r['ts_code']: r for r in conn.execute(
        'SELECT ts_code,name,close,change FROM stock_daily WHERE trade_date=? AND ' + change_filter + ' AND close>0', (day,))}
    if not rising and on('decline'):
        candidates = {code: row for code, row in candidates.items()
                      if _matches_daily_change(row['change'], parameters)}
    stats['strong_rising' if rising else 'declining'] = sum(_number(r['change']) and r['change'] < 0 for r in candidates.values()) if not rising else len(candidates)
    stats['candidate_count'] = len(candidates)
    if rising:
        stats['candidate_count'] = len(candidates)
        stats['strong_rising'] = sum(_number(r['change']) and r['change'] > 5 for r in candidates.values())
    flows = defaultdict(dict)
    _fs = flow_source_for_range(conn, dates[-20] if len(dates) >= 20 else dates[0], day)
    _fw, _fp = _flow_where(_fs)
    for row in conn.execute("SELECT ts_code,trade_date,main_net_inflow FROM fund_flow "
                            f"WHERE {_fw} AND trade_date BETWEEN ? AND ?",
                            (*_fp, dates[-20] if len(dates) >= 20 else dates[0], day)):
        if row['ts_code'] in candidates:
            flows[row['ts_code']][row['trade_date']] = row['main_net_inflow']
    funds = {}
    for code in sorted(candidates):
        values = [flows[code].get(d) for d in dates[-20:]]
        complete = len(values) == 20 and all(_number(v) for v in values)
        if not complete:
            if on('funds_red') or on('funds_threshold'):
                stats['missing_funds'] += 1
                continue
            stats['funds_display_missing'] += 1
        sums = {key: math.fsum(values[-n:]) if len(values) >= n and all(_number(v) for v in values[-n:]) else None
                for key, n in (('main_net_today', 1), ('main_net_3d', 3), ('main_net_5d', 5),
                               ('main_net_10d', 10), ('main_net_20d', 20))}
        cumulative = [sums[key] for key in ('main_net_3d', 'main_net_5d', 'main_net_10d', 'main_net_20d')]
        if on('funds_red') and not all(v > 0 for v in cumulative):
            stats['funds_not_red'] += 1
        elif on('funds_threshold') and max(cumulative) <= 50_000_000:
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
        required_dates = dates[-required_days:]
        if any(d not in data for d in required_dates):
            stats['missing_history'] += 1
            continue
        bars = [data[d] for d in required_dates]
        if not all(_bar_valid(row) for row in bars):
            stats['invalid_prices'] += 1
            continue
        if any(not _reference_valid(bars[i], bars[i - 1]) for i in range(1, len(bars))):
            stats['reference_mismatch'] += 1
            continue
        stats['eligible'] += 1
        close = bars[-1]['close']
        metrics = _daily_metrics(data, dates, code, day, parameters['return_days'])
        ret120, volume_ratio, ma20, amplitude = (metrics[key] for key in
            ('ret120', 'volume_ratio5', 'ma20', 'amplitude20'))
        if on('return120') and not _matches_parameter(metrics['ret_custom'], parameters['return_op'], parameters['return_threshold']):
            stats['return_too_high'] += 1
        elif on('volume') and not _strictly_below(volume_ratio, 1.2):
            stats['volume_too_high'] += 1
        elif on('above_ma20') and close <= ma20:
            stats['below_ma20'] += 1
        elif on('amplitude') and amplitude < 5 and not math.isclose(amplitude, 5, abs_tol=1e-10, rel_tol=0):
            stats['inactive'] += 1
        else:
            extra = {}
            if rising:
                v2_stats = result['v2_stats']
                v2_stats['base_count'] += 1
                limit_price, limit_pct = metrics['limit_up_price'], metrics['limit_pct']
                if on('not_limit') and limit_price is None:
                    v2_stats['missing_limit'] += 1
                    continue
                if on('not_limit') and not _strictly_below(close, limit_price):
                    v2_stats['limit_up'] += 1
                    continue
                ma5, ma10, ma60 = (metrics[key] for key in ('ma5', 'ma10', 'ma60'))
                if on('bullish') and not all(_strictly_below(low, high) for low, high in
                           ((ma5, close), (ma10, ma5), (ma20, ma10), (ma60, ma20))):
                    v2_stats['not_bullish'] += 1
                    continue
                extra = dict(ma5=ma5, ma10=ma10, ma60=ma60,
                             limit_up_price=limit_price, limit_pct=limit_pct)
            if not rising:
                extra = {key: metrics[key] for key in ('ma5', 'ma10', 'ma60', 'ret_custom')}
                if on('bullish') and not all(_strictly_below(low, high) for low, high in
                        ((metrics['ma5'], close), (metrics['ma10'], metrics['ma5']), (ma20, metrics['ma10']), (metrics['ma60'], ma20))):
                    stats['not_bullish'] += 1
                    continue
            hits.append({'ts_code': code, 'name': bars[-1]['name'] or code, 'trade_date': day,
                         'close': close, 'change_pct': bars[-1]['change'] if _number(bars[-1]['change']) else None, 'ret120': ret120,
                         'volume_ratio5': volume_ratio, 'ma20': ma20, 'amplitude20': amplitude,
                         **funds[code], **extra})
    if rising:
        result['v2_stats']['matched'] = len(hits)
    note = ('全部条件匹配才入选；按股票代码排列' if hits else
            '该日没有同时满足全部个股条件的股票，不推荐；缺失或异常数据已排除')
    if disabled:
        note = ('已开启条件匹配才入选；按股票代码排列' if hits else
                '该日没有同时满足已开启条件的股票，不推荐；必要数据缺失或异常已排除')
    result.update(results=hits, count=len(hits), note=note)
    return result


def screen_dip(conn, requested_date='', now=None, disabled=None, **parameters):
    disabled = _dip_disabled(disabled)
    parameters = _dip_parameters(**parameters)
    return _screen_dip(conn, requested_date, now,
                       lambda c, d, dates: _market_gate(c, d, dates, disabled), disabled=disabled, parameters=parameters)


def screen_dip_v2(conn, requested_date='', now=None, disabled=None):
    """Rising stocks below their closing limit with strictly ordered moving averages."""
    disabled = _chase_disabled(disabled)
    return _screen_dip(conn, requested_date, now,
                       lambda c, d, dates: _market_gate_v2(c, d, dates, disabled),
                       rising=True, disabled=disabled)


def _condition_snapshot(conn, requested_date='', now=None, rising=True, parameters=None):
    # Count tags and diagnostics scan the same large history. Concurrent Python
    # row iteration causes severe contention in the threaded local service.
    with _condition_scan_lock:
        return _build_condition_snapshot(conn, requested_date, now, rising, parameters) if parameters is not None else _build_condition_snapshot(conn, requested_date, now, rising)


def _build_condition_snapshot(conn, requested_date='', now=None, rising=True, parameters=None):
    """Whole-market independent predicates. No outcomes, switches or future data."""
    parameters = parameters or DIP_PARAMETER_DEFAULTS
    context, dates = _daily_context(conn, requested_date, now, max(121, parameters['return_days'] + 1))
    total = context['total']
    counts = {key: dict(matched=0, evaluated=0, missing=total, total=total) for key in (CHASE_CONDITIONS if rising else DIP_CONDITIONS)}
    result = dict(status='success', **context, counts=counts)
    if not rising:
        result['parameters'] = dict(parameters)
    records = []
    if not context.get('selection_ready', context['daily_ready']):
        return result, records
    day = context['date']
    market = _market_gate_v2(conn, day, dates, disabled=('market_down', 'market_up'))
    result['market'] = {key: market[key] for key in ('down_count', 'index_change')}
    down = market['down_count']
    breadth = getattr(conn, 'intraday_breadth', None)
    down_passed = down_state(conn, below=rising) if breadth is not None else ((down < 2600 if rising else down > 2600) if _number(down) else None)
    if down_passed is not None:
        counts['market_down'].update(matched=total if down_passed else 0,
                                     evaluated=total, missing=0)
    if rising:
        change = market['index_change']
        if _number(change):
            counts['market_up'].update(matched=total if change > 0 else 0, evaluated=total, missing=0)
    else:
        index_gate = _market_gate(conn, day, dates, disabled=('market_down',))
        if index_gate['status'] != 'missing':
            counts['market_index'].update(matched=total if index_gate['passed'] else 0, evaluated=total, missing=0)
        result['market'].update(previous_three=index_gate['previous_three'], previous_three_up=index_gate['previous_three_up'], index_note=index_gate['note'])
    conn.row_factory = sqlite3.Row
    history = defaultdict(dict)
    # One date-range scan shared by counts and all diagnostic configurations.
    for row in conn.execute('SELECT ts_code,name,trade_date,open,high,low,close,change,volume '
                            'FROM stock_daily WHERE trade_date BETWEEN ? AND ?', (dates[0], day)):
        history[row['ts_code']][row['trade_date']] = row
    flows = defaultdict(dict)
    _fs = flow_source_for_range(conn, dates[-20] if len(dates) >= 20 else dates[0], day)
    _fw, _fp = _flow_where(_fs)
    for row in conn.execute("SELECT ts_code,trade_date,main_net_inflow FROM fund_flow "
                            f"WHERE {_fw} AND trade_date BETWEEN ? AND ?",
                            (*_fp, dates[-20] if len(dates) >= 20 else dates[0], day)):
        flows[row['ts_code']][row['trade_date']] = row['main_net_inflow']
    for code, data in history.items():
        entry = data.get(day)
        if entry is None or not _number(entry['close']) or entry['close'] <= 0 or not _bar_valid(entry):
            continue
        m = _daily_metrics(data, dates, code, day, parameters['return_days'])
        close = entry['close']
        def below(key, threshold):
            return _strictly_below(m[key], threshold) if _number(m[key]) else None
        predicates = {
            'gain': m['change_pct'] > 5 if _number(m['change_pct']) else None,
            'return120': _matches_parameter(m['ret_custom'], parameters['return_op'], parameters['return_threshold']) if _number(m['ret_custom']) else None,
            'volume': below('volume_ratio5', 1.2),
            'above_ma20': close > m['ma20'] if _number(m['ma20']) else None,
            'amplitude': (m['amplitude20'] > 5 or math.isclose(m['amplitude20'], 5, abs_tol=1e-10, rel_tol=0))
                         if _number(m['amplitude20']) else None,
            'not_limit': _strictly_below(close, m['limit_up_price']) if _number(m['limit_up_price']) else None,
            'bullish': all(_strictly_below(low, high) for low, high in
                           ((m['ma5'], close), (m['ma10'], m['ma5']), (m['ma20'], m['ma10']), (m['ma60'], m['ma20'])))
                       if all(_number(m[key]) for key in ('ma5', 'ma10', 'ma20', 'ma60')) else None,
        }
        if not rising:
            predicates = {key: value for key, value in predicates.items() if key in DIP_CONDITIONS}
            predicates['decline'] = _matches_daily_change(m['change_pct'], parameters) if _number(m['change_pct']) else None
        values = [flows[code].get(d) for d in dates[-20:]]
        if len(values) == 20 and all(_number(v) for v in values):
            cumulative = [math.fsum(values[-n:]) for n in (3, 5, 10, 20)]
            predicates.update(funds_red=all(v > 0 for v in cumulative),
                              funds_threshold=max(cumulative) > 50_000_000)
        sums = {key: math.fsum(values[-n:]) if len(values) >= n and all(_number(v) for v in values[-n:]) else None
                for key, n in (('main_net_today', 1), ('main_net_3d', 3), ('main_net_5d', 5),
                               ('main_net_10d', 10), ('main_net_20d', 20))}
        records.append(dict(row=dict(ts_code=code, name=entry['name'] or code, trade_date=day, close=close,
                                     **m, **sums), predicates=predicates))
        for key, passes in predicates.items():
            if passes is not None:
                counts[key]['evaluated'] += 1
                counts[key]['matched'] += int(passes)
    for count in counts.values():
        count['missing'] = total - count['evaluated']
    result['note'] = '按全市场单项独立统计；只核验对应条件所需窗口，缺失不算符合'
    records.sort(key=lambda record: record['row']['ts_code'])
    return result, records


def _condition_counts(conn, requested_date='', now=None, rising=True):
    return _condition_snapshot(conn, requested_date, now, rising)[0]


def chase_condition_counts(conn, requested_date='', now=None):
    return _condition_counts(conn, requested_date, now, rising=True)


def dip_condition_counts(conn, requested_date='', now=None, **parameters):
    return _condition_snapshot(conn, requested_date, now, rising=False, parameters=_dip_parameters(**parameters))[0]


def _pick_diagnostics(conn, requested_date='', now=None, disabled=None, remove=None,
                      offset=0, limit=50, rising=False, **parameters):
    rules = CHASE_CONDITIONS if rising else DIP_CONDITIONS
    parse = _chase_disabled if rising else _dip_disabled
    disabled = parse(disabled)
    removed = parse(remove)
    if len(removed) > 2 or removed & disabled:
        raise ValueError('只能模拟关闭当前开启的一项或两项条件')
    try:
        offset, limit = int(offset), int(limit)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('分页参数无效，offset须非负，limit须为1到100') from None
    snapshot, records = _condition_snapshot(conn, requested_date, now, rising, None if rising else _dip_parameters(**parameters))
    enabled = [key for key in rules if key not in disabled]
    result = {key: value for key, value in snapshot.items() if key != 'note'}
    result.update(disabled=[key for key in rules if key in disabled], current_count=0,
                  singles=[], pairs=[], pairs_checked=False, pair_total=0,
                  market_blockers=[], data_summary=None, preview=None,
                  kind='waiting', message=snapshot['note'])
    if not snapshot.get('selection_ready', snapshot['daily_ready']):
        return result
    full = (1 << len(records)) - 1
    masks, states = {}, {}
    market = snapshot['market']
    for key in enabled:
        if key.startswith('market_'):
            c = snapshot['counts'][key]
            passes = None if not c['evaluated'] else c['matched'] == c['total']
            states[key] = passes
            masks[key] = full if passes else 0
            if passes is not True:
                note = ('同日下跌家数缺失' if passes is None else
                        f"下跌家数 {market['down_count']:g} 家，需{'少于' if rising else '超过'}2600家") if key == 'market_down' else (
                        ('同日上证涨跌幅缺失' if passes is None else f"上证涨跌幅 {market['index_change']:+.2f}%，需上涨")
                        if rising else market['index_note'])
                result['market_blockers'].append(dict(key=key, state='missing' if passes is None else 'unmet', note=note))
        else:
            masks[key] = sum(1 << i for i, record in enumerate(records) if record['predicates'].get(key) is True)
    def matching(removing=()):
        mask = full
        for key in enabled:
            if key not in removing:
                mask &= masks[key]
        return mask
    def count(mask):
        return bin(mask).count('1')  # Compatible with the project's Python 3.9.
    result['current_count'] = count(matching())
    result['singles'] = [dict(remove=[key], count=count(matching((key,)))) for key in enabled]
    if result['current_count'] == 0 and not any(item['count'] for item in result['singles']):
        result['pairs_checked'] = True
        result['pairs'] = [dict(remove=list(pair), count=count(matching(pair))) for pair in combinations(enabled, 2)]
        result['pair_total'] = len(result['pairs'])
    stock_keys = [key for key in enabled if not key.startswith('market_')]
    missing_count = only_missing = 0
    for record in records:
        values = [record['predicates'].get(key) for key in stock_keys]
        missing = any(value is None for value in values)
        missing_count += int(missing)
        only_missing += int(missing and not any(value is False for value in values))
    result['data_summary'] = dict(
        missing_stock_count=missing_count, only_missing_count=only_missing,
        invalid_daily_count=snapshot['total'] - len(records),
        by_condition=[dict(key=key, count=snapshot['counts'][key]['missing']) for key in stock_keys
                      if snapshot['counts'][key]['missing']])
    if result['current_count']:
        result.update(kind='selected', message='当前已有符合股票，可查看每项条件对名单数量的影响。')
    elif result['market_blockers']:
        result.update(kind='market', message='；'.join(item['note'] for item in result['market_blockers']) + '。当前大盘条件未通过。')
    elif any(item['count'] for item in result['singles']):
        result.update(kind='single', message='没有股票同时满足当前开启条件；单独关闭下列部分条件后，可以得到候选名单。')
    elif any(item['count'] for item in result['pairs']):
        result.update(kind='pair', message='只关闭任意一项仍为0只；部分两项组合可以得到候选名单。')
    elif only_missing:
        result.update(kind='missing', message='只关闭一项或两项仍为0只；部分股票未发现数值条件不符，但缺少必要数据，无法完整判断。')
    else:
        result.update(kind='combined', message='只关闭一项或两项仍为0只；其余条件没有共同匹配，或必要数据不足。这里只检查了单项和两项组合。')
    if removed:
        mask = matching(removed)
        indices = [i for i in range(len(records)) if mask & (1 << i)]
        rows = []
        for i in indices[offset:offset + limit]:
            record = records[i]
            removed_states = {key: ('missing' if (states.get(key) if key.startswith('market_') else record['predicates'].get(key)) is None
                                    else 'matched' if (states.get(key) if key.startswith('market_') else record['predicates'].get(key))
                                    else 'unmet') for key in rules if key in removed}
            rows.append(dict(**record['row'], removed_states=removed_states))
        result['preview'] = dict(remove=[key for key in rules if key in removed], count=len(indices),
                                 offset=offset, limit=limit, rows=rows)
    return result


def dip_diagnostics(conn, requested_date='', now=None, **options):
    return _pick_diagnostics(conn, requested_date, now, rising=False, **options)


def chase_diagnostics(conn, requested_date='', now=None, **options):
    return _pick_diagnostics(conn, requested_date, now, rising=True, **options)


def register_dip_picks(app, db_path, outcome_loader=None, intraday_provider=None):
    from flask import jsonify, request

    def respond(screen, enrich=True):
        try:
            with picks_connection(db_path(), intraday_provider, request.args.get('date', ''),
                                  request.args.get('intraday') in ('1','true','True'),
                                  request.args.get('snapshot_id', ''), request.args.get('refresh') == '1') as conn:
                options = {'disabled': request.args.get('disabled', '')} if screen in (screen_dip, screen_dip_v2) else {}
                if screen in (dip_diagnostics, chase_diagnostics):
                    options = {key: request.args.get(key, default) for key, default in
                               (('disabled', ''), ('remove', ''), ('offset', '0'), ('limit', '50'))}
                if screen in (screen_dip, dip_condition_counts, dip_diagnostics):
                    options.update({key: request.args[key] for key in DIP_PARAMETER_DEFAULTS if key in request.args})
                payload = screen(conn, request.args.get('date', ''), **options)
                if enrich:
                    attach_sector_history(conn, payload.get('results') or [])
                # Enrich only after selecting the close-based list; future bars cannot change membership.
                if enrich:
                    next_dates = [row[0] for row in conn.execute(
                        'SELECT DISTINCT cal_date FROM trading_dates_cache WHERE cal_date>? ORDER BY cal_date LIMIT 5',
                        (payload['date'],))] if payload['daily_ready'] else []
                    payload.update(next_dates=next_dates, live_next_index=0, intraday_used=False)
                if enrich and outcome_loader is not None and payload['daily_ready'] and payload['market_passed']:
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

    @app.get('/api/picks/dip-v2/condition-counts')
    def api_chase_condition_counts():
        return respond(chase_condition_counts, enrich=False)

    @app.get('/api/picks/dip/condition-counts')
    def api_dip_condition_counts():
        return respond(dip_condition_counts, enrich=False)

    @app.get('/api/picks/dip/diagnostics')
    def api_dip_diagnostics():
        return respond(dip_diagnostics, enrich=False)

    @app.get('/api/picks/dip-v2/diagnostics')
    def api_chase_diagnostics():
        return respond(chase_diagnostics, enrich=False)
