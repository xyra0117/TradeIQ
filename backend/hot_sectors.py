"""Independent, read-only sector heat and explainable limit-up leader candidates."""
import math
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from dip_picks import _limit_up_price_for
from surge_picks import _closed, _number, _reference_valid

HEAT_RULES = ('近期热门候选：近5日累计≥10次，至少3天出现。持续活跃：候选且未触发降温。'
              '已经降温：候选今天为0，或同时低于昨天与此前4日平均。'
              '近两日新热点：此前3日为0，近2日每天≥3只且合计≥8次。'
              '今日异动：此前4日为0，今天≥3只。缺数据不判状态；标签仅描述已收录涨停热度。')


def _heat_status(item):
    counts = [row['count'] for row in item['timeline'][-5:]]
    heat = {'key': 'missing', 'label': '数据不足', 'focus': False, 'in_board': True,
            'rank': None, 'reason': '近5个交易日拆解不完整，暂不判断近期热度'}
    if len(counts) != 5 or any(not _number(n) for n in counts):
        return heat
    total, days = sum(counts), sum(n > 0 for n in counts)
    focus = total >= 10 and days >= 3
    cooling = focus and (counts[-1] == 0 or (counts[-1] < counts[-2] and counts[-1] < sum(counts[:-1]) / 4))
    new = sum(counts[:3]) == 0 and min(counts[-2:]) >= 3 and sum(counts[-2:]) >= 8
    sudden = sum(counts[:-1]) == 0 and counts[-1] >= 3
    key, label = ('cooling', '已经降温') if cooling else ('active', '持续活跃') if focus else (
        ('new', '新近活跃') if new else ('sudden', '今日异动') if sudden else (
            ('ordinary', '一般活跃') if counts[-1] > 0 else ('quiet', '暂无热度')))
    reason = (f'近5日累计{total}次，{days}/5天出现；今天{counts[-1]}只，昨天{counts[-2]}只，'
              f'此前4日平均{sum(counts[:-1])/4:.2f}只')
    heat.update(key=key, label=label, focus=focus, in_board=focus or new or sudden, reason=reason)
    return heat


def _date(value):
    value = (value or '').strip()
    if value:
        try:
            if not re.fullmatch(r'\d{8}', value):
                raise ValueError()
            datetime.strptime(value, '%Y%m%d')
        except ValueError:
            raise ValueError('日期须为有效的 YYYYMMDD') from None
    return value


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def normalize_code(value):
    raw = str(value or '').strip().upper()
    match = re.fullmatch(r'(\d{6})\.?((?:SH|SZ|BJ))', raw)
    prefix = re.fullmatch(r'(SH|SZ|BJ)(\d{6})', raw)
    if prefix:
        code, market = prefix[2], prefix[1]
    elif match:
        code, market = match[1], match[2]
    elif re.fullmatch(r'\d{6}', raw):
        code, market = raw, None
    else:
        return None
    expected = ('SH' if code.startswith(('60', '68')) else
                'SZ' if code.startswith(('00', '30')) else
                'BJ' if code.startswith(('43', '83', '87', '92')) else None)
    if not expected or (market and market != expected):
        return None
    return code + '.' + expected


def _time(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', str(value or '').strip())
    if not match:
        return None
    h, m, s = int(match[1]), int(match[2]), int(match[3] or 0)
    if not (0 <= m < 60 and 0 <= s < 60 and 9 * 3600 + 25 * 60 <= h * 3600 + m * 60 + s <= 15 * 3600):
        return None
    return h * 3600 + m * 60 + s


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _sealed(current, previous, day):
    if (not current or not previous or not _number(current['close']) or current['close'] <= 0
            or not _number(previous['close']) or previous['close'] <= 0
            or not _number(current['volume']) or current['volume'] <= 0
            or not _reference_valid(current, previous)):
        return None
    limit = _limit_up_price_for(current['ts_code'], current['name'], previous['close'], day)
    if not limit:
        return None
    # BSE quotes may not exceed the unrounded 30% boundary; choose its highest valid cent.
    price = (float((Decimal(str(previous['close'])) * Decimal('1.3')).quantize(Decimal('.01'), rounding=ROUND_FLOOR))
             if current['ts_code'].endswith('.BJ') else limit[0])
    if current['close'] > price + .005:
        return None
    return math.isclose(current['close'], price, abs_tol=.005, rel_tol=0)


def _streak(code, day, calendar, bars):
    dates = calendar[:calendar.index(day) + 1]
    count = 0
    for i in range(len(dates) - 1, 0, -1):
        date, previous = dates[i], dates[i - 1]
        sealed = _sealed(bars.get((code, date)), bars.get((code, previous)), date)
        if sealed is None:
            return None, None if count == 0 else True
        if not sealed:
            return count, count > 0
        count += 1
    return None, True if count else None


def _market(conn, day):
    iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
    overview = _rows(conn, 'SELECT * FROM overview WHERE date IN (?,?) ORDER BY date DESC LIMIT 1', (day, iso)) if _exists(conn, 'overview') else []
    indices = _rows(conn, 'SELECT name,change,close FROM index_daily WHERE date IN (?,?)', (day, iso)) if _exists(conn, 'index_daily') else []
    return {'overview': overview[0] if overview else None,
            'indices': [{**row, 'change': row['change'] if _number(row['change']) else None,
                         'close': row['close'] if _number(row['close']) else None} for row in indices]}


def hot_sectors(conn, requested_date='', sector='', now=None):
    """Caller owns connection and transaction. No network, writes, or saved rankings."""
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    requested = _date(requested_date)
    sector = (sector or '').strip()
    if len(sector) > 200:
        raise ValueError('板块名称过长')
    available = [row[0] for row in conn.execute(
        "SELECT DISTINCT REPLACE(date,'-','') AS day FROM limitup ORDER BY day DESC")
        if re.fullmatch(r'\d{8}', row[0] or '') and _closed(row[0], now)] if _exists(conn, 'limitup') else []
    day = requested or (available[0] if available else '')
    result = {'status': 'missing', 'date': day, 'latest_date': available[0] if available else None,
              'available_dates': available, 'readonly': True, 'sectors': [], 'detail': None,
              'market': None, 'summary': {}, 'message': '该日没有已保存的涨停拆解数据',
              'note': '按已入库涨停拆解统计；龙头为自动判断的涨停型候选。'}
    if not day:
        return result
    if not _closed(day, now):
        result.update(status='pending', message='该日尚未收盘，收盘后才能确认龙头排名')
        return result
    if day not in available:
        return result

    # The saved exchange calendar is primary; observed market dates fill existing gaps.
    calendar = set()
    for table, col in (('trading_dates_cache', 'cal_date'), ('stock_daily', 'trade_date'), ('index_daily', 'date')):
        if _exists(conn, table):
            calendar.update(str(row[0]).replace('-', '') for row in conn.execute(
                f'SELECT DISTINCT {col} FROM {table} WHERE {col}<=?', (day,)))
    calendar = sorted(date for date in calendar if re.fullmatch(r'\d{8}', date or '') and date <= day)
    if day not in calendar:
        result.update(message='缺少该日交易日历或行情记录，无法确认统计窗口')
        return result
    # 61 sessions allow a 60-board chain plus its reference close; never infer missing history.
    calendar = calendar[-62:]
    previous_day = calendar[-2] if len(calendar) >= 2 else None
    start = calendar[0]
    raw = _rows(conn, "SELECT * FROM limitup WHERE REPLACE(date,'-','') BETWEEN ? AND ? ORDER BY date,code,time", (start, day))
    by_sector = defaultdict(dict)
    covered = set()
    raw_today, today_codes, excluded_codes = [], set(), set()
    for row in raw:
        date = str(row['date']).replace('-', '')
        if date not in calendar:
            continue
        covered.add(date)
        code = normalize_code(row['code'])
        key = code or str(row['code'])
        if date == day:
            raw_today.append(row)
            today_codes.add(key)
        name = (row.get('sector') or '').strip()
        if name in ('', '其他', '公告'):
            if date == day:
                excluded_codes.add(key)
            continue
        row = {**row, 'date': date, 'ts_code': code, 'original_name': row['name'],
               'seconds': _time(row['time'])}
        dedup = (date, key)
        old = by_sector[name].get(dedup)
        if old is None or (row['seconds'] is not None and (old['seconds'] is None or row['seconds'] < old['seconds'])):
            by_sector[name][dedup] = row

    codes = sorted({row['ts_code'] for rows in by_sector.values() for row in rows.values() if row['ts_code']})
    bars = {}
    if _exists(conn, 'stock_daily'):
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            placeholders = ','.join('?' * len(chunk))
            iso_start, iso_day = f'{start[:4]}-{start[4:6]}-{start[6:]}', f'{day[:4]}-{day[4:6]}-{day[6:]}'
            for row in _rows(conn, f"SELECT ts_code,name,REPLACE(trade_date,'-','') AS trade_date,close,change,volume,amount FROM stock_daily WHERE ts_code IN ({placeholders}) AND (trade_date BETWEEN ? AND ? OR trade_date BETWEEN ? AND ?)", (*chunk, start, day, iso_start, iso_day)):
                bars[(row['ts_code'], row['trade_date'])] = row

    def window(anchor, size):
        return calendar[:calendar.index(anchor) + 1][-size:]

    def complete(dates, size):
        return len(dates) == size and all(date in covered for date in dates)

    def evaluate(row, anchor, history):
        row = {**row}
        code = row['ts_code']
        bar = bars.get((code, anchor))
        row.update(name=bar['name'] if bar and bar.get('name') else row['name'],
                   close=bar['close'] if bar and _number(bar['close']) else None,
                   change_pct=bar['change'] if bar and _number(bar['change']) else None,
                   amount_yi=bar['amount'] / 100000 if bar and _number(bar['amount']) and bar['amount'] > 0 else None,
                   consecutive=None, sealed=None, rank=None, status='pending', reason='待确认',
                   recent5_count=None, rank_change='昨日排名待确认')
        if not code:
            row['reason'] = '代码无法确认'
            return row
        streak, sealed = _streak(code, anchor, calendar, bars)
        row.update(consecutive=streak, sealed=sealed)
        if sealed is False:
            row.update(status='not_sealed', reason='日线核验：收盘未涨停')
            return row
        if streak is None:
            row['reason'] = '日线缺失或价格参考异常，连板待确认'
            return row
        dates = window(anchor, 5)
        if not complete(dates, 5):
            row['reason'] = '近5交易日拆解数据不足'
            return row
        row['recent5_count'] = sum((date, code) in history for date in dates)
        if row['seconds'] is None:
            row['reason'] = '涨停时间缺失或异常'
            return row
        if row['amount_yi'] is None:
            row['reason'] = '当日成交额缺失或异常'
            return row
        row.update(status='ranked', reason=f"{streak}连板 · 近5日同板块涨停{row['recent5_count']}次 · {row['time']} · 成交额{row['amount_yi']:.2f}亿")
        return row

    def ranked(history, anchor):
        rows = [evaluate(row, anchor, history) for (date, _), row in history.items() if date == anchor]
        good = sorted((row for row in rows if row['status'] == 'ranked'),
                      key=lambda row: (-row['consecutive'], -row['recent5_count'], row['seconds'], -row['amount_yi'], row['ts_code']))
        for rank, row in enumerate(good, 1):
            row['rank'] = rank
        return rows, good

    five = window(day, 5)
    ten = window(day, 10)
    sectors = []
    for name, history in by_sector.items():
        recent = [row for (date, _), row in history.items() if date in five]
        if not recent or (sector and name != sector):
            continue
        rows, leaders = ranked(history, day)
        previous_rows, previous_leaders = ranked(history, previous_day) if previous_day else ([], [])
        previous_ready = (previous_day in covered and complete(window(previous_day, 5), 5)
                          and not any(row['status'] == 'pending' for row in previous_rows)) if previous_day else False
        previous_ranks = {row['ts_code']: row['rank'] for row in previous_leaders[:3]}
        for leader in leaders[:3]:
            old_rank = previous_ranks.get(leader['ts_code'])
            leader['rank_change'] = (f"昨日龙{old_rank} → 今日龙{leader['rank']}" if old_rank else '新进入前三') if previous_ready else '昨日排名待确认'
        tracking = None
        if previous_leaders:
            old = previous_leaders[0]
            current = bars.get((old['ts_code'], day))
            previous = bars.get((old['ts_code'], previous_day))
            sealed = _sealed(current, previous, day)
            tracking = {'ts_code': old['ts_code'], 'name': current['name'] if current and current.get('name') else old['name'],
                        'previous_date': previous_day, 'previous_rank_provisional': not previous_ready,
                        'status': 'missing' if sealed is None else 'sealed' if sealed else 'broken',
                        'status_label': '数据不足' if sealed is None else '继续涨停' if sealed else '断板',
                        'change_pct': current['change'] if current and sealed is not None else None,
                        'current_rank': next((row['rank'] for row in leaders if row['ts_code'] == old['ts_code']), None),
                        'in_sector_today': any(row['ts_code'] == old['ts_code'] for row in rows)}
        count = len(rows)
        prev_count = len(previous_rows) if previous_day in covered else None
        pending = sum(row['status'] == 'pending' for row in rows)
        item = {'sector': name, 'today_count': count, 'previous_count': prev_count,
                'delta': count - prev_count if prev_count is not None else None,
                'recent5_count': len(recent) if complete(five, 5) else None,
                'appearance5': len({row['date'] for row in recent}) if complete(five, 5) else None,
                'unique5': len({row['ts_code'] or row['code'] for row in recent}) if complete(five, 5) else None,
                'leaders': leaders[:3], 'pending_count': pending,
                'not_sealed_count': sum(row['status'] == 'not_sealed' for row in rows),
                'ranking_provisional': pending > 0, 'previous_ready': previous_ready,
                'yesterday_leader': tracking,
                'timeline': [{'date': date, 'count': sum(d == date for d, _ in history) if date in covered else None} for date in ten]}
        item['heat'] = _heat_status(item)
        if sector:
            item['stocks'] = sorted(rows, key=lambda row: (row['rank'] is None, row['rank'] or 0, row['ts_code'] or row['code']))
            item['history'] = [evaluate(row, row['date'], history) for (date, _), row in sorted(history.items(), reverse=True) if date in ten]
            result['detail'] = item
        sectors.append(item)
    sectors.sort(key=lambda row: (row['recent5_count'] is None, -(row['recent5_count'] or 0),
                                 -(row['appearance5'] or 0), -row['today_count'], row['sector']))
    board_rank = 0
    for item in sectors:
        if item['heat']['in_board'] and item['heat']['key'] != 'missing':
            board_rank += 1
            item['heat']['rank'] = board_rank
    result.update(status='ready', sectors=sectors, market=_market(conn, day), message='', previous_date=previous_day,
                  summary={'raw_records': len(raw_today), 'stock_count': len(today_codes),
                           'sector_count': len(sectors), 'excluded_stock_count': len(excluded_codes - {row['ts_code'] or row['code'] for rows in by_sector.values() for (date, _), row in rows.items() if date == day}),
                           'recent5_complete': complete(five, 5), 'recent5_dates': five,
                           'heat_rules': HEAT_RULES,
                           'heat_board_count': sum(row['heat']['in_board'] for row in sectors),
                           'heat_groups': {key: [row['sector'] for row in sectors if row['heat']['key'] == key]
                                           for key in ('active', 'new', 'cooling')},
                           'missing5_dates': [date for date in five if date not in covered],
                           'missing10_dates': [date for date in ten if date not in covered]})
    if sector and result['detail'] is None:
        result.update(status='empty', message='该板块近5个交易日没有涨停拆解记录')
    return result


def register_hot_sectors(app, db_path):
    from flask import jsonify, request

    def respond(detail=False):
        try:
            sector = request.args.get('sector', '').strip() if detail else ''
            if detail and not sector:
                raise ValueError('请选择板块')
            with closing(sqlite3.connect(Path(db_path()).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                payload = hot_sectors(conn, request.args.get('date', ''), sector)
            response = jsonify(payload)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ValueError as error:
            return jsonify(status='error', message=str(error)), 400
        except sqlite3.Error:
            return jsonify(status='error', message='本地板块数据暂时无法读取'), 503

    @app.get('/api/hot-sectors')
    def api_hot_sectors():
        return respond()

    @app.get('/api/hot-sectors/detail')
    def api_hot_sector_detail():
        return respond(True)
