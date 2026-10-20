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
# 条件优选历史涨幅分布的 8 档固定区间 (key, label, low 含, high 不含; None 表示无界)
PREFERRED_HIST_BUCKETS = (
    ('drop', '跌 (<0%)', None, 0.0),
    ('b0_3', '0~3%', 0.0, 3.0),
    ('b3_5', '3~5%', 3.0, 5.0),
    ('b5_10', '5~10%', 5.0, 10.0),
    ('b10_15', '10~15%', 10.0, 15.0),
    ('b15_20', '15~20%', 15.0, 20.0),
    ('b20_30', '20~30%', 20.0, 30.0),
    ('b30', '≥30%', 30.0, None),
)


def bucket_preferred_high_gain(gain_pct):
    """把累计最高涨幅落进 8 档之一, 返回 (key, label); 超出所有区间返回 (None, None)."""
    for key, label, low, high in PREFERRED_HIST_BUCKETS:
        if (low is None or gain_pct >= low) and (high is None or gain_pct < high):
            return key, label
    return None, None


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
        if module == 'barometer':
            # 走专属分支 (LEFT JOIN overview + index_daily + market_fflow ...), 不在 TABLES 里
            return 'overview', 'date'
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
        if module == 'sectors':
            from hot_sectors import hot_sectors
            result = hot_sectors(self.conn, requested or '')
            return {**result, 'items': result['sectors'], 'count': len(result['sectors']), 'module': module}
        available = self.dates(module, kind, source)
        day = requested or (available[0] if available else None)
        table, col = self.table_for(module, kind, source)
        extra = {}
        if module == 'positions':
            items = self.positions(day)
            extra['note'] = '当前持仓数量 × 截至所选日期的已保存收盘价；不是历史持仓快照。'
            extra['summary'] = {'count': len(items), 'total_cost': round(sum(r['cost_value'] for r in items), 2),
                                'priced_count': sum(r['current_price'] is not None for r in items)}
        elif module == 'picks' and kind == 'self':
            items = self.rows('SELECT * FROM user_picks ORDER BY added_at DESC') if self.exists('user_picks') else []
            self.enrich(items, day)
            extra['note'] = '当前自选名单，价格为所选日期已保存日线。'
        else:
            items = self.day_rows(table, col, day)
            if module == 'barometer':
                # 盘面晴雨表: 一次性 LEFT JOIN 原始表 (overview/index_daily/market_fflow/sector_fund_flow/leaderboard)
                # 输出行字段直接对应 xlsx 同款列; 字段未计算留 None
                items = self.barometer_history(limit)
            elif module == 'index':
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

    def preferred_history(self, window=5):
        """条件优选历史涨幅分布 (跟桌面 /api/conditional-picks/preferred/historical-distribution 同口径).
        扫全部已保存优选快照, 每条入选记录取入选后 T+1..T+window 任一天的最高价 / 入选日收盘价 − 1
        作为累计最高涨幅, 落进 8 档固定区间. 每次入选算 1 个独立样本 (同股多次入选计多次);
        窗口内任一天缺数据的记录整条排除并计入 pending."""
        if window not in (1, 3, 5):
            raise ValueError('统计窗口须为 1、3 或 5')
        if not self.exists('conditional_picks_preferred_snapshot'):
            return dict(window=window, buckets=[b[:2] + [b[2], b[3], 0, 0.0] for b in PREFERRED_HIST_BUCKETS],
                        samples=[], dates=[], settled=0, pending=0, total=0, date_count=0,
                        date_from=None, date_to=None, unique_stocks=0, windows=[1, 3, 5],
                        metric_label=f'入选后 T+1~T+{window} 累计最高涨幅')
        counts = {key: 0 for key, _, _, _ in PREFERRED_HIST_BUCKETS}
        samples, dates = [], []
        settled = pending = 0
        rows = self.rows('SELECT trade_date, results_json, threshold_yi, volume_ratio_pct '
                         'FROM conditional_picks_preferred_snapshot ORDER BY trade_date')
        for row in rows:
            try:
                results = json.loads(row['results_json'] or '[]')
            except (TypeError, json.JSONDecodeError) as error:
                raise sqlite3.DatabaseError('Invalid saved snapshot') from error
            if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
                raise sqlite3.DatabaseError('Invalid saved snapshot')
            day_settled = 0
            for item in results:
                outcomes = (item.get('preferred_outcomes') or [])[:window]
                values = [o.get('high_sum_pct') for o in outcomes]
                if len(values) < window or any(v is None for v in values):
                    pending += 1
                    continue
                gain = max(values)
                key, label = bucket_preferred_high_gain(gain)
                if key is None:
                    pending += 1
                    continue
                counts[key] += 1
                settled += 1
                day_settled += 1
                samples.append({
                    'trade_date': row['trade_date'], 'ts_code': item.get('ts_code'),
                    'name': item.get('name'), 'close': item.get('close'),
                    'gain_pct': round(gain, 4), 'bucket': key, 'bucket_label': label,
                    'path': [round(v, 4) for v in values],
                })
            if results:
                dates.append({'trade_date': row['trade_date'], 'count': len(results),
                              'settled': day_settled, 'threshold_yi': row['threshold_yi'],
                              'volume_ratio_pct': row['volume_ratio_pct']})
        samples.sort(key=lambda s: (-s['gain_pct'], s['trade_date']))
        buckets = [{'key': key, 'label': label, 'low': low, 'high': high, 'count': counts[key],
                    'pct': round(counts[key] / settled * 100, 2) if settled else 0.0}
                   for key, label, low, high in PREFERRED_HIST_BUCKETS]
        return dict(window=window, buckets=buckets, samples=samples, dates=dates,
                    settled=settled, pending=pending, total=settled + pending,
                    date_count=len(dates), date_from=dates[0]['trade_date'] if dates else None,
                    date_to=dates[-1]['trade_date'] if dates else None,
                    unique_stocks=len({s['ts_code'] for s in samples}), windows=[1, 3, 5],
                    metric_label=f'入选后 T+1~T+{window} 累计最高涨幅')

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

    def barometer_history(self, limit=1000, date=None):
        """盘面晴雨表 (手机端): 一次 LEFT JOIN overview/index_daily/market_fflow/sector_fund_flow/leaderboard,
        输出行字段直接对应前端 renderBarometer 期望的 xlsx 同款列.
        取近 limit 个有 overview 数据的交易日; 概念板块 TOP1/2/3 从 sector_fund_flow (change_pct DESC) 取; 涨幅榜从 leaderboard 取.
        默认 1000 (≈ 4 年交易日), 覆盖所有历史; mobile_app 上限 1000.
        date: YYYYMMDD 8 位字符串 (None = 不过滤). 过滤时只返回那一天的单行.
        """
        if not self.exists('overview'):
            return []
        # 主框架: overview + index_daily × 2 (上证/科创50) + market_fflow
        # 概念板块 TOP1/2/3: CTE 按 trade_date 分组, change_pct DESC + name ASC 排序, 取前 3
        # 性能: 各表的日期列 (overview.date / index_daily.date / sector_fund_flow.trade_date /
        # market_fflow.trade_date) 存的都是 YYYYMMDD **无横线**, 所以关联条件直接写 `=`,
        # 不要再套 REPLACE(x,'-','') —— 那会让索引失效走全表扫, 实测 60 行要 10.6 秒,
        # 去掉后 0.22 秒 (快约 48 倍). 下游拿到的 date 格式不变, 口径一致.
        if date:
            sql = """
                WITH top AS (
                  SELECT trade_date, name, change_pct, main_net_inflow, lead_stock_name,
                         ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY change_pct DESC, name ASC) AS rn
                  FROM sector_fund_flow
                  WHERE sector_type = 'concept'
                )
                SELECT
                    o.date AS date,
                    o.totalVolume AS turnover,
                    idx_sh.close AS shanghai_index, idx_sh.change AS change_rate,
                    idx_kc.close AS kechuang50_index, idx_kc.change AS kechuang50_rate,
                    idx_cyb.close AS chinext_index, idx_cyb.change AS chinext_rate,
                    o.upCount AS advancers, o.downCount AS decliners,
                    m.last AS em_last,
                    t1.name AS top1_name, t1.change_pct AS top1_rate, t1.main_net_inflow AS top1_money, t1.lead_stock_name AS top1_stock,
                    t2.name AS top2_name, t2.change_pct AS top2_rate, t2.main_net_inflow AS top2_money, t2.lead_stock_name AS top2_stock,
                    t3.name AS top3_name, t3.change_pct AS top3_rate, t3.main_net_inflow AS top3_money, t3.lead_stock_name AS top3_stock
                FROM overview o
                LEFT JOIN index_daily idx_sh ON idx_sh.name='上证指数' AND idx_sh.date = o.date
                LEFT JOIN index_daily idx_kc ON idx_kc.name='科创50' AND idx_kc.date = o.date
                LEFT JOIN index_daily idx_cyb ON idx_cyb.name='创业板指' AND idx_cyb.date = o.date
                LEFT JOIN market_fflow m ON m.trade_date = o.date
                LEFT JOIN top t1 ON t1.trade_date = o.date AND t1.rn = 1
                LEFT JOIN top t2 ON t2.trade_date = o.date AND t2.rn = 2
                LEFT JOIN top t3 ON t3.trade_date = o.date AND t3.rn = 3
                WHERE o.upCount > 0 AND o.date = ?
                ORDER BY o.date DESC
            """
            rows = self.rows(sql, (date,))
        else:
            sql = """
                WITH top AS (
                  SELECT trade_date, name, change_pct, main_net_inflow, lead_stock_name,
                         ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY change_pct DESC, name ASC) AS rn
                  FROM sector_fund_flow
                  WHERE sector_type = 'concept'
                )
                SELECT
                    o.date AS date,
                    o.totalVolume AS turnover,
                    idx_sh.close AS shanghai_index, idx_sh.change AS change_rate,
                    idx_kc.close AS kechuang50_index, idx_kc.change AS kechuang50_rate,
                    idx_cyb.close AS chinext_index, idx_cyb.change AS chinext_rate,
                    o.upCount AS advancers, o.downCount AS decliners,
                    m.last AS em_last,
                    t1.name AS top1_name, t1.change_pct AS top1_rate, t1.main_net_inflow AS top1_money, t1.lead_stock_name AS top1_stock,
                    t2.name AS top2_name, t2.change_pct AS top2_rate, t2.main_net_inflow AS top2_money, t2.lead_stock_name AS top2_stock,
                    t3.name AS top3_name, t3.change_pct AS top3_rate, t3.main_net_inflow AS top3_money, t3.lead_stock_name AS top3_stock
                FROM overview o
                LEFT JOIN index_daily idx_sh ON idx_sh.name='上证指数' AND idx_sh.date = o.date
                LEFT JOIN index_daily idx_kc ON idx_kc.name='科创50' AND idx_kc.date = o.date
                LEFT JOIN index_daily idx_cyb ON idx_cyb.name='创业板指' AND idx_cyb.date = o.date
                LEFT JOIN market_fflow m ON m.trade_date = o.date
                LEFT JOIN top t1 ON t1.trade_date = o.date AND t1.rn = 1
                LEFT JOIN top t2 ON t2.trade_date = o.date AND t2.rn = 2
                LEFT JOIN top t3 ON t3.trade_date = o.date AND t3.rn = 3
                WHERE o.upCount > 0
                ORDER BY o.date DESC
                LIMIT ?
            """
            rows = self.rows(sql, (limit,))
        # 解析 market_fflow.last JSON → main/super/big/small 资金 (单位 亿)
        for r in rows:
            em_last = r.pop('em_last', None)
            r['main_money'] = r['super_money'] = r['big_money'] = r['small_money'] = None
            if em_last:
                try:
                    em = json.loads(em_last)
                    # 东财 push2 last JSON 字段: main / super / big / mid / small (单位元)
                    r['main_money'] = em.get('main', 0) / 1e8
                    r['super_money'] = em.get('super', 0) / 1e8
                    r['big_money'] = em.get('big', 0) / 1e8
                    r['small_money'] = em.get('small', 0) / 1e8
                except (ValueError, AttributeError):
                    pass
            # 概念板块 TOP1/2/3: 字段重命名 + 资金转亿
            for i in (1, 2, 3):
                name = r.pop(f'top{i}_name', None)
                rate = r.pop(f'top{i}_rate', None)
                money = r.pop(f'top{i}_money', None)
                stock = r.pop(f'top{i}_stock', None)
                r[f'today_top{i}_concept'] = name
                r[f'today_top{i}_concept_rate'] = rate
                r[f'today_top{i}_concept_money'] = (money / 1e8) if money is not None else None
                r[f'today_top{i}_concept_stock'] = stock
            # 其它占位字段 (涨幅榜后填, 注释/信号/星期暂留空)
            for k in ('hot5_sector','hot5_stock','hot10_sector','hot10_stock','hot20_sector','hot20_stock',
                     'comments','signal','weekdays'):
                r.setdefault(k, None)
        # 涨幅榜: hot{N}_stock = leaderboard(type=N日, rank=1).name; hot{N}_sector = 该股在 barometer_date 之前最近一次非「其他」涨停的板块
        hot_top1 = self._barometer_hot_top1([r['date'] for r in rows])
        # 板块一次批量查完 (原来每行一次, 60 行 = 60 次查询, 实测整个接口 18 秒)
        pairs = []
        for r in rows:
            for n in (5, 10, 20):
                cn = hot_top1.get((r['date'], f'{n}日'))
                if cn:
                    pairs.append((cn[0], r['date']))
        hot_sector = self._barometer_hot_sector_bulk(pairs)
        for r in rows:
            for n in (5, 10, 20):
                code_name = hot_top1.get((r['date'], f'{n}日'))
                if not code_name:
                    r[f'hot{n}_stock'] = None
                    r[f'hot{n}_sector'] = None
                    continue
                r[f'hot{n}_stock'] = code_name[1]
                r[f'hot{n}_sector'] = hot_sector.get((code_name[0], r['date']))
        return rows

    def _barometer_hot_top1(self, dates):
        """返回 {(YYYYMMDD, '5日'|'10日'|'20日'): (code, name)} 仅 rank=1 行"""
        if not dates or not self.exists('leaderboard'):
            return {}
        rows = self.rows(
            "SELECT date, type, code, name FROM leaderboard WHERE rank=1 AND type IN ('5日','10日','20日') AND date IN ({})".format(
                ','.join('?' * len(dates))),
            tuple(dates))
        return {(r['date'], r['type']): (r['code'], r['name']) for r in rows}

    def _barometer_hot_sector(self, code, max_date):
        """返回 code 在 max_date 及之前最近一次非「其他」涨停的 sector; 找不到返回 None"""
        if not code or not self.exists('limitup'):
            return None
        rows = self.rows(
            "SELECT sector FROM limitup WHERE code=? AND date<=? AND sector!='其他' ORDER BY date DESC, time DESC LIMIT 1",
            (code, max_date))
        return rows[0]['sector'] if rows else None

    def _barometer_hot_sector_bulk(self, code_dates):
        """_barometer_hot_sector 的批量版: 一次查完, 避免 N+1.

        code_dates: [((code, max_date), ...)] 或 [(code, max_date), ...]
        返回 {(code, max_date): sector}.

        原实现对每一行单独查一次 limitup, 60 行 = 60 次查询, 实测整个接口要 18 秒;
        改成"把所有 (code, date) 排好序一次取回, 再在 Python 里挑每个 code 的最新一条",
        单次查询解决, 口径完全一致 (同 code 按 date DESC, time DESC 取第一条非「其他」).
        """
        if not code_dates or not self.exists('limitup'):
            return {}
        pairs = list(code_dates)
        # 同一个 code 可能配多个 max_date, 按 code 去重后一次取该 code 的全部候选
        codes = sorted({c for c, _ in pairs})
        max_date_of = {}
        for c, d in pairs:
            if c not in max_date_of or d > max_date_of[c]:
                max_date_of[c] = d
        # 一次取回: 每个 code 的所有涨停记录 (date/time 倒序), 板块非「其他」
        rows = self.rows(
            "SELECT code, date, time, sector FROM limitup "
            "WHERE code IN ({}) AND sector!='其他' ORDER BY code, date DESC, time DESC".format(
                ','.join('?' * len(codes))),
            tuple(codes))
        # 按 code 归组 (已按 date/time 倒序, 第一条就是最新)
        by_code = {}
        for r in rows:
            by_code.setdefault(r['code'], []).append((r['date'], r['sector']))
        out = {}
        for c, max_d in max_date_of.items():
            for d, sector in by_code.get(c, []):
                if d <= max_d:      # 跳过晚于该行基准日的数据
                    out[(c, max_d)] = sector
                    break
        return out

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
