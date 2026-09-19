"""Saved TradeIQ data only. No upstream clients, scheduler, or database bootstrap."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

INDEX_CODES = {'000001.SH': '上证指数', '399001.SZ': '深证成指',
               '399006.SZ': '创业板指', '000688.SH': '科创50'}
TABLES = {
    'index': ('index_daily', 'date'), 'leaderboard': ('leaderboard', 'date'),
    'limitup': ('limitup', 'date'), 'lz': ('limitup_collection', 'trade_date'),
    'flow': ('fund_flow', 'trade_date'), 'picks': ('stock_picks', 'trade_date'),
    'sectors': ('limitup', 'date'), 'positions': ('stock_daily', 'trade_date'),
    'flow-sector': ('sector_fund_flow', 'trade_date'),
}
PICKS = {
    'system': 'stock_picks', 'ths': 'stock_picks_ths',
    'conditional': 'conditional_picks_snapshot', 'combined': 'combined_picks_snapshot',
    'conditional-ths': 'conditional_picks_snapshot_ths',
    'combined-ths': 'combined_picks_snapshot_ths',
    'conditional-new': 'conditional_picks_new_snapshot',
    'conditional-preferred': 'conditional_picks_preferred_snapshot',
}


def date_arg(value):
    if not value:
        return None
    value = value.replace('-', '')
    if not re.fullmatch(r'\d{8}', value):
        raise ValueError('日期须为 YYYYMMDD')
    datetime.strptime(value, '%Y%m%d')
    return value


def stock_code(value):
    value = (value or '').upper()
    if re.fullmatch(r'\d{6}', value):
        value += '.BJ' if value[0] in '48' or value.startswith('920') else '.SH' if value[0] in '569' else '.SZ'
    if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', value):
        raise ValueError('股票代码格式错误')
    return value


@contextmanager
def connect_readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        # An explicit read transaction keeps dates, rows and summaries consistent.
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.close()


class SavedData:
    def __init__(self, conn):
        self.conn = conn

    def exists(self, table):
        return bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def table_for(self, module, kind='system', source='eastmoney-push2'):
        if module not in TABLES:
            raise ValueError('未知模块')
        if module == 'picks':
            if kind == 'self':
                return 'stock_daily', 'trade_date'
            if kind not in PICKS:
                raise ValueError('未知选股类型')
            return PICKS[kind], 'trade_date'
        if module == 'flow':
            if source not in ('eastmoney-push2', 'all', 'ths'):
                raise ValueError('未知资金来源')
            if source == 'ths':
                return 'fund_flow_ths', 'trade_date'
        return TABLES[module]

    def dates(self, module, kind='system', source='eastmoney-push2'):
        table, col = self.table_for(module, kind, source)
        if not self.exists(table):
            return []
        where, args = '', []
        if module == 'flow' and source == 'eastmoney-push2':
            where, args = ' WHERE source=?', [source]
        return [r['d'] for r in self.rows(
            f"SELECT DISTINCT REPLACE({col}, '-', '') AS d FROM {table}{where} ORDER BY d DESC", args)
            if r['d'] and re.fullmatch(r'\d{8}', r['d'])]

    def day_rows(self, table, col, day, extra='', args=()):
        if not day or not self.exists(table):
            return []
        # Both historical ISO dates and compact dates occur in existing tables.
        iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
        return self.rows(f'SELECT * FROM {table} WHERE {col} IN (?, ?)' + extra,
                         (day, iso, *args))

    def daily(self, code, day, exact=False):
        if not self.exists('stock_daily') or not day:
            return None
        op = '=' if exact else '<='
        rows = self.rows(f"SELECT *, REPLACE(trade_date,'-','') AS price_date FROM stock_daily "
                         f"WHERE ts_code=? AND REPLACE(trade_date,'-','') {op} ? ORDER BY REPLACE(trade_date,'-','') DESC LIMIT 1", (code, day))
        return rows[0] if rows else None

    def enrich(self, items, day):
        if not day or not self.exists('stock_daily'):
            return items
        iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
        # 一次取整天价格 (走 idx_stock_daily_date 索引); 逐股 REPLACE 查询会全表扫描, 资金流 5000+ 行时超时
        prices = {r['ts_code']: r for r in self.rows(
            "SELECT ts_code, close, change, trade_date, name FROM stock_daily WHERE trade_date IN (?, ?)",
            (day, iso))}
        for r in items:
            raw = r.get('ts_code') or r.get('code')
            if not raw:
                continue
            try:
                code = stock_code(raw)
            except ValueError:
                continue
            r['ts_code'] = code
            daily = prices.get(code)
            if daily:
                r['close'] = daily['close']
                r['change_pct'] = daily['change']
                r['price_date'] = daily['trade_date']
                r['name'] = r.get('name') or daily.get('name')
        return items

    def module(self, module, requested=None, kind='system', source='eastmoney-push2', period='5日', threshold_yi=3, limit=None):
        available = self.dates(module, kind, source)
        day = requested or (available[0] if available else None)
        table, col = self.table_for(module, kind, source)
        extra = {}
        if module == 'positions':
            items = self.positions(day)
            extra['note'] = '当前持仓数量 × 截至所选日期的已保存收盘价；不是历史持仓快照。'
            extra['summary'] = {'count': len(items), 'total_cost': round(sum(r['cost_value'] for r in items), 2),
                                'priced_count': sum(r['current_price'] is not None for r in items)}
        elif module == 'sectors':
            items = self.sectors(day)
            extra['note'] = '截至所选日期近 7 个自然日涨停次数；点击板块查看股票。'
        elif module == 'picks' and kind == 'self':
            items = self.rows('SELECT * FROM user_picks ORDER BY added_at DESC') if self.exists('user_picks') else []
            self.enrich(items, day)
            extra['note'] = '当前自选名单，价格为所选日期已保存日线。'
        else:
            items = self.day_rows(table, col, day)
            if module == 'index':
                reverse = {name: code for code, name in INDEX_CODES.items()}
                items = [dict(r, ts_code=reverse[r['name']], change_pct=r['change']) for r in items if r['name'] in reverse]
                extra['overview'] = next(iter(self.day_rows('overview', 'date', day)), None)
                extra['market_flow'] = self.market_flow(day)
                extra['overview_history'] = self.overview_history()
            elif module == 'leaderboard':
                if period not in ('5日', '10日', '20日'):
                    raise ValueError('未知涨幅周期')
                items = sorted((r for r in items if r['type'] == period), key=lambda r: r.get('rank') or 0)
                self.enrich(items, day)
                extra['frequency'] = self.leaderboard_frequency(day, period)
            elif module in ('limitup', 'lz'):
                self.enrich(items, day)
            elif module == 'flow':
                # 大盘资金流向分时图 (所选日期, 跟核心指数 tab 同口径)
                extra['market_flow'] = self.market_flow(day)
                if period in ('3d', '5d', '10d', '20d'):
                    n = {'3d': 3, '5d': 5, '10d': 10, '20d': 20}[period]
                    items = self.flow_cumulative(source, day, n)
                    extra['note'] = (f'截至所选日期的近 {n} 个交易日累计'
                                     + ('（同花顺总净额口径）。' if source == 'ths' else '（东方财富主力净流入口径）。'))
                else:
                    if source == 'eastmoney-push2':
                        items = [r for r in items if r.get('source') == source]
                    # Desktop uses stock_daily as authoritative price/change source.
                    for r in items:
                        r['close'] = r['change_pct'] = None
                    self.enrich(items, day)
                    key = 'net' if source == 'ths' else 'main_net_inflow'
                    items.sort(key=lambda r: r.get(key) or 0, reverse=True)
                    extra['note'] = '同花顺总净流入（元），与东方财富主力净流入不同口径。' if source == 'ths' else '东方财富主力净流入（超大单＋大单，元）。'
            elif module == 'flow-sector':
                sector_type = source if source in ('industry', 'concept') else 'industry'
                items = [r for r in items if r.get('sector_type') == sector_type]
                items.sort(key=lambda r: r.get('main_net_inflow') or 0, reverse=True)
                extra['sector_type'] = sector_type
                extra['note'] = '东方财富板块主力净流入（行业/概念，元）；当日口径，不做周期累计。'
            elif module == 'picks':
                if 'snapshot' in table:
                    snapshot = items[0] if items else {}
                    try:
                        items = json.loads(snapshot.get('results_json') or '[]')
                    except (TypeError, json.JSONDecodeError) as error:
                        raise sqlite3.DatabaseError('Invalid saved snapshot') from error
                    if not isinstance(items, list) or any(not isinstance(r, dict) for r in items):
                        raise sqlite3.DatabaseError('Invalid saved snapshot')
                    # conditional_new / conditional_preferred 快照保存的就是已过滤的最终命中,
                    # 不再用 sum_Nd 阈值重筛 (它们用 main_net/super_net/small_net/close_vs_ma10 等口径).
                    if kind in ('conditional-new', 'conditional-preferred'):
                        if kind == 'conditional-preferred':
                            # 优选按 score (preferred 排序用) 倒序
                            items.sort(key=lambda r: (r.get('score') is not None, r.get('score') or 0), reverse=True)
                    else:
                        windows = (3, 5, 10) if kind.endswith('-ths') else (3, 5, 10, 20)
                        threshold = threshold_yi * 1e8
                        items = [r for r in items if max(r.get(f'sum_{n}d') or 0 for n in windows) >= threshold]
                        if kind.startswith('conditional'):
                            items.sort(key=lambda r: max(r.get(f'sum_{n}d') or 0 for n in windows), reverse=True)
                    extra['saved_at'] = snapshot.get('saved_at')
                    extra['threshold_yi'] = threshold_yi
                    extra['note'] = f'已保存候选快照按至少一项累计 ≥ {threshold_yi:g} 亿元筛选；不重新采集或补齐。' \
                        if kind not in ('conditional-new', 'conditional-preferred') else \
                        '已保存候选快照；不再按 sum_Nd 重筛。'
                else:
                    items.sort(key=lambda r: r.get('score') or 0, reverse=True)
        total = len(items)
        truncated = bool(limit) and total > limit
        if truncated:
            items = items[:limit]
        return dict(module=module, date=day, latest_date=available[0] if available else None,
                    items=items, count=total, truncated=truncated,
                    empty=not items, readonly=True,
                    message='' if items else '该日期没有已保存数据，请在电脑端更新。', **extra)

    def positions(self, day):
        if not self.exists('positions'):
            return []
        items = self.rows('SELECT * FROM positions WHERE shares>0 AND closed_at IS NULL ORDER BY created_at DESC')
        for r in items:
            daily = self.daily(r['ts_code'], day)
            cost, shares = r['cost_price'], r['shares']
            price = daily['close'] if daily else None
            r.update(cost_value=round(cost * shares, 2), current_price=price,
                     price_date=daily['trade_date'] if daily else None,
                     change_pct=daily['change'] if daily else None, profit=None, profit_pct=None, market_value=None)
            if price is not None:
                mv = round(price * shares, 2)
                # Same broker-cost convention as app.get_positions (not a new fee policy).
                commission = round(max(mv * .0001, 5), 2)
                stamp = round(mv * .0005, 2)
                transfer = round(mv * .00001, 2) if r['ts_code'].endswith(('.SH', '.BJ')) else 0
                r.update(market_value=mv, profit=round((price-cost)*shares-commission-stamp-transfer, 2),
                         profit_pct=round((price-cost)/cost*100, 3) if cost else None)
        return items

    def sectors(self, day):
        if not day or not self.exists('limitup'):
            return []
        start = (datetime.strptime(day, '%Y%m%d') - timedelta(days=6)).strftime('%Y%m%d')
        return self.rows("""SELECT sector, COUNT(*) AS hot_count, COUNT(DISTINCT code) AS stock_count,
            MAX(REPLACE(date,'-','')) AS latest_date FROM limitup
            WHERE REPLACE(date,'-','') BETWEEN ? AND ? AND sector IS NOT NULL
            AND sector NOT IN ('', '其他', '公告') GROUP BY sector ORDER BY hot_count DESC, stock_count DESC""", (start, day))

    def sector_detail(self, name, day):
        if not self.exists('limitup'):
            return []
        rows = self.rows("""SELECT * FROM (SELECT *, ROW_NUMBER() OVER
            (PARTITION BY code ORDER BY date DESC) AS rn FROM limitup
            WHERE sector=? AND REPLACE(date,'-','')<=?) WHERE rn=1 ORDER BY date DESC""", (name, day))
        return self.enrich(rows, day)

    def user_sectors(self):
        if not self.exists('user_sectors'):
            return []
        items = self.rows('SELECT id,name,note,pinned FROM user_sectors ORDER BY pinned DESC,name')
        for r in items:
            r['stocks'] = self.rows('SELECT ts_code,name,role,rank FROM user_sector_stocks WHERE sector_id=? ORDER BY role,rank', (r['id'],)) if self.exists('user_sector_stocks') else []
        return items

    def market_flow(self, day):
        rows = self.day_rows('market_fflow', 'trade_date', day)
        if not rows:
            return None
        r = rows[0]
        return dict(date=r['trade_date'], times=json.loads(r['times']), series=json.loads(r['series']),
                    last=json.loads(r['last']), pct=json.loads(r['pct']), complete=bool(r['complete']))

    def flow_cumulative(self, source, day, n):
        """近 N 个交易日资金流累计 (跟桌面 /api/flow/market?period=Nd 同口径).
        窗口: 截至 day(含) 往前数第 N 个交易日, 用 trading_dates_cache 取窗口起点.
        close/change_pct 取该股截至 day 的已存日线."""
        table = 'fund_flow_ths' if source == 'ths' else 'fund_flow'
        if not day or not self.exists(table):
            return []
        if self.exists('trading_dates_cache'):
            win = self.rows("""SELECT cal_date FROM trading_dates_cache
                WHERE cal_date <= ? ORDER BY cal_date DESC LIMIT 1 OFFSET ?""", (day, n - 1))
        else:
            win = []
        if not win:
            return []
        start = win[0]['cal_date']
        if table == 'fund_flow_ths':
            items = self.rows("""SELECT ts_code, code, name,
                    SUM(net) AS sum_net, COUNT(*) AS flow_days,
                    MIN(trade_date) AS start_date, MAX(trade_date) AS end_date
                FROM fund_flow_ths
                WHERE REPLACE(trade_date,'-','') BETWEEN ? AND ?
                GROUP BY ts_code ORDER BY sum_net DESC""", (start, day))
        else:
            where = '' if source == 'all' else 'AND source=?'
            args = (start, day) if source == 'all' else (start, day, source)
            items = self.rows(f"""SELECT ts_code, code, name,
                    SUM(main_net_inflow) AS sum_main_net, SUM(super_net) AS sum_super_net,
                    SUM(big_net) AS sum_big_net, SUM(mid_net) AS sum_mid_net,
                    SUM(small_net) AS sum_small_net, COUNT(*) AS flow_days,
                    MIN(trade_date) AS start_date, MAX(trade_date) AS end_date
                FROM fund_flow
                WHERE REPLACE(trade_date,'-','') BETWEEN ? AND ? {where}
                GROUP BY ts_code ORDER BY sum_main_net DESC""", args)
        self.enrich(items, day)
        return items

    def stock_flow_history(self, code, day=None, days=60):
        """个股 5 档资金流向逐日走势 (跟桌面 /api/stock/<code>/fflow/daily 同口径).
        day: 截至该日期(含), 缺省取最近. days: 窗口天数."""
        if not self.exists('fund_flow'):
            return None
        try:
            code = stock_code(code)
        except ValueError:
            raise
        days = max(7, min(int(days), 250))
        where_day = 'AND REPLACE(f.trade_date,\'-\',\'\') <= ?' if day else ''
        args = (code, day, days) if day else (code, days)
        rows = self.rows(f"""SELECT f.trade_date, f.name, f.code,
                d.close, d.change, d.amount,
                f.main_net_inflow AS main, f.main_net_pct AS main_pct,
                f.super_net AS super, f.super_pct AS super_pct,
                f.big_net AS big, f.big_pct AS big_pct,
                f.mid_net AS mid, f.mid_pct AS mid_pct,
                f.small_net AS small, f.small_pct AS small_pct
            FROM fund_flow f
            LEFT JOIN stock_daily d ON d.ts_code = f.ts_code AND d.trade_date = f.trade_date
            WHERE f.ts_code = ? {where_day}
            ORDER BY f.trade_date DESC LIMIT ?""", args)
        if not rows:
            return None
        rows.reverse()
        keys = ['main', 'super', 'big', 'mid', 'small']
        series, pct_map, amounts, closes, changes = {}, {}, [], [], []
        dates = []
        for r in rows:
            dates.append(r['trade_date'])
            amounts.append(r['amount'])
            closes.append(r['close'])
            if r['change'] is not None and r['close'] is not None:
                pre = r['close'] - r['change']
                changes.append(round(r['change'] / pre * 100, 2) if pre else None)
            else:
                changes.append(None)
            for k in keys:
                series.setdefault(k, []).append(r[k])
                pct_map.setdefault(k, []).append(r[k + '_pct'])
        return dict(ts_code=code, name=rows[0]['name'], dates=dates,
                    amount=amounts, close=closes, change_pct=changes,
                    series=series, pct=pct_map)

    def leaderboard_frequency(self, day, period):
        """上榜频次统计 (跟桌面 /api/frequency 同口径):
        在所选日期往前 1/6/12 个月内, 该股出现在该周期榜单的次数."""
        if not day or not self.exists('leaderboard'):
            return None
        ranges = {}
        end_dt = datetime.strptime(day, '%Y%m%d')
        for months, label in ((1, '1个月'), (6, '6个月'), (12, '12个月')):
            ranges[label] = (end_dt - timedelta(days=months * 30)).strftime('%Y%m%d')
        start = ranges['12个月']
        rows = self.rows("""SELECT code, name, REPLACE(date,'-','') AS d FROM leaderboard
            WHERE type=? AND REPLACE(date,'-','') BETWEEN ? AND ?""", (period, start, day))
        stats = {}
        for r in rows:
            s = stats.setdefault(r['code'], {'code': r['code'], 'name': r['name'],
                                             '1个月': 0, '6个月': 0, '12个月': 0})
            for label, st in ranges.items():
                if st <= r['d']:
                    s[label] += 1
        return {'tabs': ['1个月', '6个月', '12个月'], 'ranges': ranges,
                'items': sorted(stats.values(), key=lambda s: -s['12个月'])}

    def overview_history(self, limit=60):
        # Same sources as desktop /api/overview: overview + market_fflow + 上证指数收盘.
        if not self.exists('overview'):
            return []
        rows = self.rows("""SELECT o.date, o.totalVolume, o.netFlow, o.upCount, o.downCount,
                o.limitUp, o.limitDown, m.last AS em_last, i.close AS sh_close, i.change AS sh_change
            FROM overview o
            LEFT JOIN market_fflow m ON REPLACE(m.trade_date,'-','') = REPLACE(o.date,'-','')
            LEFT JOIN index_daily i ON i.name = '上证指数' AND REPLACE(i.date,'-','') = REPLACE(o.date,'-','')
            WHERE o.upCount > 0 ORDER BY REPLACE(o.date,'-','') DESC LIMIT ?""", (limit,))
        for r in rows:
            em_last = r.pop('em_last', None)
            r['emMain'] = r['emSuper'] = None
            if em_last:
                try:
                    em = json.loads(em_last)
                    r['emMain'] = em.get('main', 0) / 1e8
                    r['emSuper'] = em.get('super', 0) / 1e8
                except (ValueError, AttributeError):
                    pass
        return rows

    def chart(self, code, kind, day, start=None):
        if kind not in ('index', 'stock'):
            raise ValueError('未知图表类型')
        if kind == 'index':
            if code not in INDEX_CODES:
                raise ValueError('不支持的指数代码')
            rows = self.rows('SELECT date AS trade_date,open,high,low,close,change,volume FROM index_daily WHERE name=? ORDER BY date', (INDEX_CODES[code],)) if self.exists('index_daily') else []
            name, unit, label = INDEX_CODES[code], '亿元', '成交额'
        else:
            code = stock_code(code)
            rows = self.rows('SELECT trade_date,name,open,high,low,close,change,volume,amount FROM stock_daily WHERE ts_code=? ORDER BY trade_date', (code,)) if self.exists('stock_daily') else []
            name, unit, label = (rows[-1].get('name') if rows else code), '亿元', '成交额'
            for r in rows:
                r['volume'] = r['amount']/100000 if r['amount'] is not None else None
        rows = [r for r in rows if (not day or r['trade_date'].replace('-', '') <= day)
                and (not start or r['trade_date'].replace('-', '') >= start)]
        return dict(ts_code=code, name=name, rows=rows, count=len(rows), volume_unit=unit,
                    volume_label=label, intraday=False, readonly=True,
                    missing_ohlc=sum(any(r[k] is None for k in ('open','high','low','close')) for r in rows),
                    message='' if rows else '没有已保存日线，请在电脑端更新。',
                    minline_message='分时未持久保存，手机只读版不发起行情采集。')
