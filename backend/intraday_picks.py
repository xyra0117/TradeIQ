"""Request-scoped live inputs over read-only history. Never writes the market DB."""
import math
import re
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
FLOW_FIELDS = ('main_net_inflow', 'super_net', 'big_net', 'mid_net', 'small_net')


def local_now(value=None):
    value = value or datetime.now(TZ)
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def anchor(now):
    t = now.time().replace(tzinfo=None)
    return now.replace(hour=11, minute=30, second=0, microsecond=0) if time(11, 30) <= t < time(13) else (
        now.replace(hour=15, minute=0, second=0, microsecond=0) if t >= time(15) else now)


def timestamp(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None


def fresh(value, now, seconds):
    stamp = timestamp(value)
    age = (anchor(now) - stamp).total_seconds() if stamp else None
    # A provider may timestamp its unchanged closing quote after 15:00.
    # The anchor limits staleness; future-clock rejection must use actual now.
    return stamp is not None and stamp.date() == now.date() and age <= seconds and stamp <= now + timedelta(seconds=60)


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


class LiveConnection(sqlite3.Connection):
    pass


def down_state(conn, threshold=2600, below=False):
    """Only claim a breadth threshold when even unknown symbols cannot change it."""
    breadth = getattr(conn, 'intraday_breadth', None)
    if breadth is None:
        return None
    lo, hi = breadth['down'], breadth['down'] + breadth['unknown']
    return (True if hi < threshold else False if lo >= threshold else None) if below else (
        True if lo > threshold else False if hi <= threshold else None)


class IntradayPicks:
    def __init__(self, quote_loader, clock=None, fund_loader=None):
        self.quote_loader = quote_loader
        self.fund_loader = fund_loader
        self.clock = clock or local_now
        self.lock = Lock()
        self.snapshots = {}

    def get(self, conn, day, snapshot_id='', refresh=False):
        now = local_now(self.clock())
        with self.lock:
            self.snapshots = {k: v for k, v in self.snapshots.items()
                              if v['day'] == day and (now - v['created']).total_seconds() < 900}
            if snapshot_id:
                if snapshot_id not in self.snapshots:
                    raise ValueError('这批盘中数据已过期，请点击刷新重新筛选')
                return self.snapshots[snapshot_id]
            if not refresh and self.snapshots:
                latest = max(self.snapshots.values(), key=lambda x: x['created'])
                if (now - latest['created']).total_seconds() < 30:
                    return latest
            codes = [r[0] for r in conn.execute("SELECT ts_code FROM stock_basic WHERE COALESCE(status,'')!='delisting'")
                     if re.fullmatch(r'(?:60\d{4}\.SH|68\d{4}\.SH|00\d{4}\.SZ|30\d{4}\.SZ|\d{6}\.BJ)', r[0])]
            quotes = {}
            batches = [codes[i:i+200] for i in range(0, len(codes), 200)] + [['000001.SH']]
            def load(batch):
                try:
                    return self.quote_loader(batch, cache_ttl=0) or {}
                except Exception:
                    return {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for result in pool.map(load, batches):
                    quotes.update(result)
            now = local_now(self.clock())
            bars, times = {}, []
            breadth = dict(up=0, down=0, flat=0, unknown=0, expected=len(codes))
            for code in codes:
                q = quotes.get(code) or {}
                timely = fresh(q.get('time'), now, 120)
                known = timely and finite(q.get('price')) and finite(q.get('prev_close')) and q['price'] > 0 and q['prev_close'] > 0
                if known:
                    breadth['up' if q['price'] > q['prev_close'] else 'down' if q['price'] < q['prev_close'] else 'flat'] += 1
                else:
                    breadth['unknown'] += 1
                fields = ('open', 'high', 'low', 'price', 'volume', 'amount')
                if not known or not all(finite(q.get(k)) and q[k] > 0 for k in fields):
                    continue
                if not q['low'] <= min(q['open'], q['price']) <= max(q['open'], q['price']) <= q['high']:
                    continue
                bars[code] = dict(ts_code=code, name=q.get('name') or code, trade_date=day,
                                  open=q['open'], high=q['high'], low=q['low'], close=q['price'],
                                  change=(q['price']/q['prev_close']-1)*100,
                                  volume=q['volume'], amount=q['amount']/1000)
                times.append(q['time'])
            index = quotes.get('000001.SH') or {}
            index_change = ((index['price']/index['prev_close']-1)*100
                            if fresh(index.get('time'), now, 120) and all(finite(index.get(k)) and index[k] > 0 for k in ('price', 'prev_close')) else None)
            conn.row_factory = sqlite3.Row
            columns = {r[1] for r in conn.execute('PRAGMA table_info(fund_flow)')}
            fields = ','.join(k if k in columns else 'NULL AS '+k for k in ('ts_code', 'trade_date', 'source', 'updated_at', *FLOW_FIELDS))
            flows, fund_times, seen_fund_times = {}, [], []
            for row in conn.execute(f"SELECT {fields} FROM fund_flow WHERE trade_date=? AND source IN ('eastmoney-push2','eastmoney-daykline')", (day,)):
                row = dict(row)
                if timestamp(row['updated_at']):
                    seen_fund_times.append(row['updated_at'])
                if fresh(row['updated_at'], now, 300):
                    if row['ts_code'] not in flows or row['source'] == 'eastmoney-push2':
                        flows[row['ts_code']] = row
                        fund_times.append(row['updated_at'])
            fund_error = ''
            if self.fund_loader and (refresh or len(flows) < len(codes) * .9):
                try:
                    current = self.fund_loader(now)
                    for code, row in current.items():
                        if code in codes and row.get('trade_date') == day and row.get('source') in ('eastmoney-push2','eastmoney-daykline') and fresh(row.get('updated_at'), now, 300):
                            flows[code] = row
                    fund_times = [row['updated_at'] for row in flows.values()]
                except Exception:
                    fund_error = ' 东方财富盘中资金读取失败，缺失项无法核验，可点击更新资金重试。'
            snapshot = dict(id=uuid.uuid4().hex, day=day, created=now, bars=bars, flows=flows,
                            breadth=breadth, index_change=index_change,
                            metadata=dict(quote_source='sina', fund_source='eastmoney',
                                          captured_at=now.strftime('%Y-%m-%d %H:%M:%S'),
                                          quote_from=min(times) if times else None, quote_to=max(times) if times else None,
                                          funds_from=min(fund_times) if fund_times else None,
                                          funds_to=max(fund_times) if fund_times else None,
                                          funds_latest_seen=max(seen_fund_times) if seen_fund_times else None,
                                          quoted=len(bars), universe=len(codes), fresh_funds=len(flows), breadth=breadth,
                                          note='当前满足，收盘可能变化；量为截至当前累计量。'
                                          + ('' if flows else ' 当天资金缺失或超过5分钟，请更新东方财富资金后刷新。') + fund_error))
            self.snapshots[snapshot['id']] = snapshot
            while len(self.snapshots) > 8:
                self.snapshots.pop(next(iter(self.snapshots)))
            return snapshot


def fetch_eastmoney_intraday(now):
    """Same push2 fields as the existing collector, without a browser or DB writes."""
    import requests
    from fetch_fund_flow_today import PUSH2_URL_BASE
    def page(n):
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(PUSH2_URL_BASE+'&pn='+str(n),
                headers={'Referer':'https://data.eastmoney.com/','User-Agent':'Mozilla/5.0'}, timeout=8)
            response.raise_for_status()
            data = response.json().get('data') or {}
            return data
        finally:
            session.close()
    first = page(1)
    total = int(first.get('total') or 0)
    if not 0 < total < 10000:
        raise ValueError('东方财富未返回有效市场数据')
    pages = [first]
    def later(n):
        try:
            return page(n)
        except Exception:
            return {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages.extend(pool.map(later, range(2, (total+99)//100+1)))
    fields = dict(main_net_inflow='f62', super_net='f66', big_net='f72', mid_net='f78', small_net='f84')
    rows = {}
    for data in pages:
        for item in data.get('diff') or []:
            try:
                code = str(item['f12'])
                stamp = datetime.fromtimestamp(float(item['f124']), TZ).strftime('%Y-%m-%d %H:%M:%S')
                if not re.fullmatch(r'\d{6}', code) or not fresh(stamp, now, 300):
                    continue
                suffix = 'SH' if item.get('f13') == 1 else 'BJ' if code.startswith(('4','8','9')) else 'SZ'
                full = code+'.'+suffix
                values = {key:float(item[field]) if finite(item.get(field)) else None for key,field in fields.items()}
                rows[full] = dict(ts_code=full, trade_date=now.strftime('%Y%m%d'), source='eastmoney-push2', updated_at=stamp, **values)
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
    if not rows:
        raise ValueError('东方财富没有当天新鲜资金记录')
    return rows


def overlay(db_path, snapshot):
    """SQLite's TEMP namespace adapts existing SQL without copying long history."""
    conn = sqlite3.connect(':memory:', uri=True, factory=LiveConnection)
    conn.execute('ATTACH DATABASE ? AS history', (Path(db_path).resolve().as_uri()+'?mode=ro',))
    day = snapshot['day']
    specs = {
        'stock_daily': ('ts_code,name,trade_date,open,high,low,close,change,volume,amount'.split(','), list(snapshot['bars'].values()), 'trade_date'),
        'fund_flow': (['ts_code', 'trade_date', 'source', *FLOW_FIELDS], list(snapshot['flows'].values()), 'trade_date'),
        'overview': (['date', 'upCount', 'downCount', 'flatCount'], [dict(date=day, **{
            field: snapshot['breadth'][key] if not snapshot['breadth']['unknown'] else None
            for field, key in [('upCount','up'),('downCount','down'),('flatCount','flat')]})], 'date'),
        'index_daily': (['date', 'name', 'change'], [dict(date=day, name='上证指数', change=snapshot['index_change'])], 'date'),
    }
    for name, (columns, rows, date_key) in specs.items():
        conn.execute(f'CREATE TEMP TABLE live_{name} (' + ','.join(columns) + ')')
        conn.executemany(f'INSERT INTO live_{name} VALUES ('+','.join('?' for _ in columns)+')',
                         [tuple(row.get(k) for k in columns) for row in rows])
        existing = {r[1] for r in conn.execute(f'PRAGMA history.table_info({name})')}
        selected = ','.join(k if k in existing else 'NULL AS '+k for k in columns)
        # Daily bars and funds use YYYYMMDD; retain their date indexes. Market
        # summaries also accept legacy ISO dates and contain only a few rows/day.
        cutoff = date_key if date_key == 'trade_date' else f"REPLACE({date_key},'-','')"
        conn.execute(f"CREATE TEMP VIEW {name} AS SELECT {selected} FROM history.{name} WHERE {cutoff}<'{day}' UNION ALL SELECT * FROM live_{name}")
    for name in ('trading_dates_cache', 'stock_sector_summary'):
        conn.execute(f'CREATE TEMP VIEW {name} AS SELECT * FROM history.{name}')
    conn.commit()
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    conn.intraday_breadth = snapshot['breadth']
    conn.intraday_context = dict(date=day, requested_date=day, daily_ready=False,
        selection_ready=bool(snapshot['bars']), total=len(snapshot['bars']), screen_mode='intraday',
        readiness_reason='intraday_live', intraday_estimate=True, snapshot_id=snapshot['id'],
        intraday=snapshot['metadata'], latest_closed_date=None,
        note=snapshot['metadata']['note'] if snapshot['bars'] else '没有当天新鲜有效行情，请刷新重试')
    return conn


@contextmanager
def picks_connection(db_path, provider=None, date='', intraday=False, snapshot_id='', refresh=False):
    if not isinstance(snapshot_id, str) or len(snapshot_id) > 64:
        raise ValueError('盘中批次标识无效，请刷新重试')
    base = sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True, timeout=10, factory=LiveConnection)
    base.execute('PRAGMA query_only=ON')
    base.execute('BEGIN')
    live = None
    try:
        if provider and intraday:
            from dip_picks import _daily_context
            now = local_now(provider.clock())
            day = date or now.strftime('%Y%m%d')
            context, _ = _daily_context(base, day, now)
            if day == now.strftime('%Y%m%d') and not context['daily_ready']:
                trading = base.execute('SELECT 1 FROM trading_dates_cache WHERE cal_date=?', (day,)).fetchone()
                if not trading or now.time().replace(tzinfo=None) < time(9, 30):
                    base.intraday_context = dict(context, selection_ready=False, screen_mode='intraday',
                        note='当前尚未开盘或不是本地交易日，暂不生成盘中名单')
                else:
                    snapshot = provider.get(base, day, snapshot_id, refresh)
                    live = overlay(db_path, snapshot)
                    live.intraday_context['requested_date'] = date
                    live.intraday_context['latest_closed_date'] = context['latest_closed_date']
                    if now.time().replace(tzinfo=None) >= time(15, 5):
                        live.intraday_context['awaiting_close'] = True
                        live.intraday_context['note'] += ' 已收市，等待完整日线入库后收盘确认。'
        yield live or base
    finally:
        if live is not None:
            live.close()
        base.close()
