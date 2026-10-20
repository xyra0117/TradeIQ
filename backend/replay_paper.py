"""模拟盘: 历史行情回放 + 人工纸面交易。

回放的成不成立全靠一条不变量——**客户端永远看不到当前时钟之后的分时**。
所以时钟由本模块权威持有, 分时查询恒带 `m <= cur_min`, 订单时间戳由服务端盖。
任何客户端传入的 date/minute 一律忽略; 少了这条, 回放会静默退化成
「一个带交易界面的回测」: 界面完全正常, 只是用户开始赚钱。
"""
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from flask import jsonify, request

SESSION_OPEN = '09:25:00'
SESSION_CLOSE = '15:00:00'

# 日历门槛: 20261001 这天全市场只有 1 只票落库(239 根 bar), 走进它等于走进
# 一个空日子。日历必须从 stock_minline_bars 的实际覆盖率派生, 不能信
# trading_dates_cache —— 日历表只管"那天开没开市", 不管"行情抓到了没有"。
MIN_BARS_PER_DAY = 10000

STEP_OPS = ('tick', 'next_minute', 'skip_close', 'next_day')
MAX_TICK_STEPS = 240


class SimNotFound(LookupError):
    """账户 / 会话不存在。"""


class SimConflict(Exception):
    """需要调用方确认才放行的情形(时钟错位 / 价格离谱), 对应 HTTP 409。"""

    def __init__(self, message, **payload):
        super().__init__(message)
        self.payload = payload


# 金额一律以「分」做整数运算, 只在返回给前端时换回元。浮点累加会让 16 天后
# 总资产和 initial_cash + Σcash_delta 差出几分钱, 而这是用户会死盯的数字。
def _cents(amount):
    return int(round(amount * 100))


def _yuan(cents):
    return cents / 100.0

# 手滑把 11.35 输成 113.50 是最常见的下单事故, 超过这个幅度要二次确认
PRICE_SANITY_BAND = 0.20


# ── 缓存 ────────────────────────────────────────────────────
# 这两个查询都要扫表: replay_calendar 要过 2187 万条索引项(实测 ~20 秒),
# session_grid 要扫当日 ~128 万行。自动播放每秒 4 次 tick, 不缓存就是把本地库打死。
# 行情只由盘后采集追加, 短 TTL 足够; 盘后采集完可以调 clear_caches() 立刻生效。
_CACHE_TTL_SECONDS = 1800
_cache = {}


def clear_caches():
    """采集任务落完新数据后可主动失效; 测试夹具换库时也靠它隔离。"""
    _cache.clear()


def _cached(key, build):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    value = build()
    _cache[key] = (now, value)
    return value


# ── 建表 ────────────────────────────────────────────────────

def init_sim_db(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sim_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '模拟账户',
            initial_cash REAL NOT NULL DEFAULT 100000,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS sim_session (
            account_id INTEGER PRIMARY KEY,
            cur_date TEXT NOT NULL,
            cur_min  TEXT NOT NULL,
            rev      INTEGER NOT NULL DEFAULT 0,
            god_mode INTEGER NOT NULL DEFAULT 0,
            playing  INTEGER NOT NULL DEFAULT 0,
            speed    INTEGER NOT NULL DEFAULT 5,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS sim_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            ts_code TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            shares REAL NOT NULL,
            amount REAL NOT NULL,
            commission REAL NOT NULL DEFAULT 0,
            transfer_fee REAL NOT NULL DEFAULT 0,
            stamp_tax REAL NOT NULL DEFAULT 0,
            total_fee REAL NOT NULL DEFAULT 0,
            cash_delta REAL NOT NULL DEFAULT 0,
            trade_date TEXT,
            minute TEXT,
            ref_price REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_sim_orders_acct
            ON sim_orders(account_id, id DESC);
        CREATE TABLE IF NOT EXISTS sim_daily_nav (
            account_id INTEGER NOT NULL,
            trade_date TEXT NOT NULL,
            cash REAL NOT NULL,
            market_value REAL NOT NULL,
            total_asset REAL NOT NULL,
            profit REAL NOT NULL,
            ret_pct REAL NOT NULL,
            PRIMARY KEY (account_id, trade_date)
        );
    ''')


# ── 回放日历与时钟脊柱 ────────────────────────────────────────

def replay_calendar(conn, db_key=''):
    """可回放交易日。按分时覆盖股票数过滤掉脏日期。结果按 db_key 缓存。"""
    def build():
        # 用 COUNT(*) 而不是 COUNT(DISTINCT ts_code): 前者能走 covering index
        # 只扫索引不读表, 实测 2187 万行 97s -> 22s。两者筛出的日期完全一致,
        # 因为正常日 ~134 万行而脏日(20261001)只有 239 行, 差三个数量级。
        return [row[0] for row in conn.execute(
            'SELECT trade_date FROM (SELECT trade_date, COUNT(*) AS bars '
            'FROM stock_minline_bars GROUP BY trade_date) '
            'WHERE bars >= ? ORDER BY trade_date',
            (MIN_BARS_PER_DAY,))]
    return _cached(('calendar', db_key), build)


def session_grid(conn, trade_date, db_key=''):
    """当日全市场分钟并集, 即回放时钟的脊柱。结果按 db_key + 日期缓存。

    绝不能硬编码 09:25 + 09:30..11:30 + 13:00..15:00: 尾盘集合竞价时段
    (14:58 / 14:59) 数据源根本不推送, 而停牌票当天的点数从 80 到 239 不等。
    """
    def build():
        return [row[0] for row in conn.execute(
            'SELECT DISTINCT m FROM stock_minline_bars WHERE trade_date = ? ORDER BY m',
            (trade_date,))]
    return _cached(('grid', db_key, trade_date), build)


def _grid_index(grid, minute):
    """时钟可能指向一个网格里不存在的分钟(脏数据), 退到当日最后一个点。"""
    try:
        return grid.index(minute)
    except ValueError:
        return len(grid) - 1


def advance(conn, session, op, steps=1, db_key=''):
    """推进回放时钟, 返回 (cur_date, cur_min)。不写库, 由调用方落盘。"""
    if op not in STEP_OPS:
        raise ValueError('未知的推进方式: ' + str(op))
    grid = session_grid(conn, session['cur_date'], db_key)
    if not grid:
        raise ValueError(f'{session["cur_date"]} 没有分时数据')

    if op == 'next_day':
        return _roll_to_next_day(conn, session, db_key)

    index = _grid_index(grid, session['cur_min'])
    if op == 'skip_close':
        return session['cur_date'], grid[-1]
    if op == 'tick':
        target = min(index + max(1, min(steps, MAX_TICK_STEPS)), len(grid) - 1)
        if target > index:
            return session['cur_date'], grid[target]
        # 本日已收盘, 自动顺延到下一交易日 09:25
        return _roll_to_next_day(conn, session, db_key)
    # next_minute
    if index >= len(grid) - 1:
        raise ValueError('本日已收盘，请用「下一交易日」')
    return session['cur_date'], grid[index + 1]


def _roll_to_next_day(conn, session, db_key):
    calendar = replay_calendar(conn, db_key)
    if not calendar:
        raise ValueError('本地还没有可回放的分时数据')
    if session['cur_date'] not in calendar:
        raise ValueError(f'{session["cur_date"]} 不在可回放日历内')
    position = calendar.index(session['cur_date'])
    if position >= len(calendar) - 1:
        raise ValueError(f'已到回放窗口末尾（{calendar[-1]}）')
    # 跨日落到 09:25 集合竞价, 隔夜跳空在这一刻自然体现
    return calendar[position + 1], SESSION_OPEN


# ── 分时切片: 防未来函数的唯一出口 ────────────────────────────

def minline_slice(conn, ts_code, trade_date, cur_min):
    """取该票截至 cur_min 的分时。

    `m <= cur_min` 是闭区间: 走到哪根就能看到哪根, 再往后的字节根本不出服务端。
    个股用自己实际存在的点序列(停牌票当天可能只有 80 根), 不与会话网格对齐——
    那会让图表 x 轴和服务端下标错位。
    """
    rows = [{'m': m, 'p': p, 'avg_p': a, 'v': v}
            for m, p, a, v in conn.execute(
                'SELECT m, p, avg_p, v FROM stock_minline_bars '
                'WHERE ts_code = ? AND trade_date = ? AND m <= ? ORDER BY m',
                (ts_code, trade_date, cur_min))]
    head = conn.execute(
        'SELECT name, prev_close FROM stock_minline_bars '
        'WHERE ts_code = ? AND trade_date = ? ORDER BY m LIMIT 1',
        (ts_code, trade_date)).fetchone()
    prev_close = head[1] if head else None
    if prev_close is None:
        # 约 24 只票每天 prev_close 全为 NULL, 回落日线前收, 否则开盘跳空基准丢失
        fallback = conn.execute(
            'SELECT close FROM stock_daily WHERE ts_code = ? AND trade_date < ? '
            'ORDER BY trade_date DESC LIMIT 1', (ts_code, trade_date)).fetchone()
        prev_close = fallback[0] if fallback else None
    return {
        'ts_code': ts_code,
        'name': head[0] if head else None,
        'trade_date': trade_date,
        'cur_min': cur_min,
        'prev_close': prev_close,
        'count': len(rows),
        'rows': rows,
        'has_bar': bool(rows),
        'last_price': rows[-1]['p'] if rows else None,
        'last_min': rows[-1]['m'] if rows else None,
    }


# ── 会话读写 ────────────────────────────────────────────────

def price_at(conn, ts_code, trade_date, cur_min):
    """该票在当前时钟的 carry-forward 价: m <= cur_min 的最后一根。

    停牌票当天只有 80 根 bar, 取不到就返回 None——那是"没动", 不是"没有"。
    """
    row = conn.execute(
        'SELECT m, p FROM stock_minline_bars WHERE ts_code = ? AND trade_date = ? '
        'AND m <= ? ORDER BY m DESC LIMIT 1', (ts_code, trade_date, cur_min)).fetchone()
    return {'minute': row[0], 'price': row[1]} if row else None


def derive_positions(conn, account_id, cur_date):
    """从订单流派生持仓。orders 是唯一事实, 这里不落任何状态。

    三条边界必须显式处理:
      - 清仓(total_cost 归零)后再买, 成本基准要重置, 不能跟已归零的旧仓混算;
      - 部分卖出不改成本价(标准摊薄口径), 只减股数;
      - 可卖数量独立算, 当日买入的当天不可卖(A 股 T+1)。
    """
    lots = {}
    for ts_code, side, shares, price, amount, fee, trade_date in conn.execute(
            'SELECT ts_code, side, shares, price, amount, total_fee, trade_date '
            'FROM sim_orders WHERE account_id = ? ORDER BY id', (account_id,)):
        item = lots.setdefault(ts_code, {'shares': 0.0, 'sellable': 0.0,
                                         'cost_cents': 0, 'name': None})
        if side == 'buy':
            if item['shares'] <= 0:          # 清仓后重买, 重置成本基准
                item['cost_cents'] = 0
            item['shares'] += shares
            item['cost_cents'] += _cents(price * shares) + _cents(fee)
            if trade_date < cur_date:       # T+1: 隔夜才算可卖
                item['sellable'] += shares
        else:
            # 部分卖出按比例缩成本总额, 而不是让每股成本被稀释——标准摊薄口径下
            # cost_price 必须保持不变, 否则卖一半会把成本价抬高一倍。
            # 注意这里只能 round 不能 _cents(): cost_cents 单位已经是分,
            # 再过一遍 _cents 会把它当成元, 成本凭空放大 100 倍。
            remaining = item['shares'] - shares
            item['cost_cents'] = (int(round(item['cost_cents'] * remaining / item['shares']))
                                  if item['shares'] > 0 else 0)
            item['shares'] = remaining
            item['sellable'] -= shares
            if item['shares'] <= 0:          # 清仓, 成本基准一并清零
                item['shares'] = 0.0
                item['sellable'] = 0.0
                item['cost_cents'] = 0
    return {code: item for code, item in lots.items() if item['shares'] > 0}


def build_positions(conn, account_id, session, buy_fee, sell_fee):
    """派生持仓 + 中信口径浮盈。买入费不扣, cost_price 已是含费的摊薄成本。"""
    result = []
    for code, item in sorted(derive_positions(conn, account_id, session['cur_date']).items()):
        quote = price_at(conn, code, session['cur_date'], session['cur_min'])
        shares = item['shares']
        cost_price = _yuan(item['cost_cents']) / shares if shares else None
        entry = {'ts_code': code, 'name': item['name'], 'shares': shares,
                 'sellable': item['sellable'], 'cost_price': cost_price,
                 'price': None, 'market_value': 0.0, 'profit': 0.0,
                 'profit_pct': 0.0, 'stale': quote is None, 'last_bar_m': None}
        if quote is None:
            # 停牌/时钟之前还没出现: 浮盈不可知, 保持 None 而不是假装 0
            entry['profit'] = None
            entry['profit_pct'] = None
            result.append(entry)
            continue
        price = quote['price']
        market_value = _yuan(_cents(price * shares))
        profit = ((price - cost_price) * shares - sell_fee(market_value, code)
                  if cost_price is not None else 0.0)
        entry.update(price=price, market_value=market_value, last_bar_m=quote['minute'],
                     profit=round(profit, 2),
                     profit_pct=round((price - cost_price) / cost_price * 100, 3)
                     if cost_price else None)
        result.append(entry)
    return result


def build_account_summary(conn, account_id, positions):
    """现金 = 初始资金 + Σcash_delta, 不存列——存了必然和持仓对不上漂移。"""
    initial_cash, = conn.execute('SELECT initial_cash FROM sim_accounts WHERE id = ?',
                                 (account_id,)).fetchone()
    deltas = [row[0] for row in conn.execute(
        'SELECT cash_delta FROM sim_orders WHERE account_id = ?', (account_id,))]
    fees = [row[0] for row in conn.execute(
        'SELECT total_fee FROM sim_orders WHERE account_id = ?', (account_id,))]
    cash = _yuan(_cents(initial_cash) + sum(_cents(d) for d in deltas))
    market_value = _yuan(sum(_cents(p['market_value']) for p in positions))
    total_asset = _yuan(_cents(cash) + _cents(market_value))
    return {'initial_cash': initial_cash, 'cash': cash, 'market_value': market_value,
            'total_asset': total_asset, 'total_profit': round(total_asset - initial_cash, 2),
            'total_profit_pct': round((total_asset - initial_cash) / initial_cash * 100, 3)
            if initial_cash else None,
            'total_fee': _yuan(sum(_cents(f) for f in fees))}


def persist_nav(conn, account_id, date):
    """把某个已走完的交易日收盘时的净值落库。next_day 时调用。"""
    lots = derive_positions(conn, account_id, date)
    cash_cents = _cents(conn.execute('SELECT initial_cash FROM sim_accounts WHERE id = ?',
                                     (account_id,)).fetchone()[0])
    for delta in conn.execute('SELECT cash_delta FROM sim_orders WHERE account_id = ? '
                              'AND trade_date <= ?', (account_id, date)):
        cash_cents += _cents(delta[0])
    value_cents = 0
    for code, item in lots.items():
        quote = price_at(conn, code, date, SESSION_CLOSE)
        if quote and quote['price']:
            value_cents += _cents(quote['price'] * item['shares'])
    cash = _yuan(cash_cents)
    total = _yuan(cash_cents + value_cents)
    initial, = conn.execute('SELECT initial_cash FROM sim_accounts WHERE id = ?',
                             (account_id,)).fetchone()
    prev, = conn.execute('SELECT total_asset FROM sim_daily_nav WHERE account_id = ? '
                         'AND trade_date < ? ORDER BY trade_date DESC LIMIT 1',
                         (account_id, date)).fetchone() or (initial,)
    conn.execute(
        'INSERT OR REPLACE INTO sim_daily_nav (account_id, trade_date, cash, market_value, '
        'total_asset, profit, ret_pct) VALUES (?,?,?,?,?,?,?)',
        (account_id, date, cash, _yuan(value_cents), total, round(total - initial, 2),
         round((total - prev) / prev * 100, 3) if prev else 0.0))


def build_nav(conn, account_id, session, account_summary):
    """已收盘各日快照 + 正在进行中的当天(用当前总资产), 曲线因此是活的。"""
    points = [{'trade_date': row[0], 'cash': row[1], 'market_value': row[2],
               'total_asset': row[3], 'profit': row[4], 'ret_pct': row[5],
               'partial': False}
              for row in conn.execute(
                  'SELECT trade_date, cash, market_value, total_asset, profit, ret_pct '
                  'FROM sim_daily_nav WHERE account_id = ? AND trade_date < ? '
                  'ORDER BY trade_date', (account_id, session['cur_date']))]
    points.append({'trade_date': session['cur_date'], 'cash': account_summary['cash'],
                   'market_value': account_summary['market_value'],
                   'total_asset': account_summary['total_asset'],
                   'profit': account_summary['total_profit'],
                   'ret_pct': account_summary['total_profit_pct'], 'partial': True})
    return points


def _account(conn, account_id):
    row = conn.execute('SELECT id, name, initial_cash FROM sim_accounts WHERE id = ?',
                       (account_id,)).fetchone()
    if row is None:
        raise SimNotFound('模拟账户不存在')
    return {'id': row[0], 'name': row[1], 'initial_cash': row[2]}


def load_session(conn, account_id):
    row = conn.execute(
        'SELECT cur_date, cur_min, rev, god_mode, playing, speed '
        'FROM sim_session WHERE account_id = ?', (account_id,)).fetchone()
    if row is None:
        raise SimNotFound('回放会话不存在，请先创建')
    return {'cur_date': row[0], 'cur_min': row[1], 'rev': row[2],
            'god_mode': row[3], 'playing': row[4], 'speed': row[5]}


def write_session(conn, account_id, session):
    conn.execute(
        'UPDATE sim_session SET cur_date = ?, cur_min = ?, rev = rev + 1, '
        'god_mode = ?, playing = ?, speed = ?, '
        "updated_at = datetime('now','localtime') WHERE account_id = ?",
        (session['cur_date'], session['cur_min'], session['god_mode'],
         session['playing'], session['speed'], account_id))


def _int_arg(name, default=None):
    raw = request.args.get(name)
    if raw in (None, ''):
        if default is None:
            raise ValueError(f'缺少参数 {name}')
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'参数 {name} 必须是整数')


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ── 路由注册 ────────────────────────────────────────────────

def register_replay_paper(app, db_path, buy_fee=None, sell_fee=None):
    """注册模拟盘路由。buy_fee / sell_fee 由调用方注入, 避免与 app.py 循环依赖。"""

    db_key = str(Path(db_path()).resolve())

    def connect():
        return sqlite3.connect(Path(db_path()).resolve().as_uri() + '?mode=rw', uri=True, timeout=10)

    def respond(func):
        """统一错误口径: 参数错 400, 账户不存在 404, 库读不了 503。"""
        def wrapper(*args, **kwargs):
            try:
                result = func()
            except ValueError as error:
                return jsonify(status='error', message=str(error)), 400
            except SimConflict as conflict:
                return jsonify(status='error', message=str(conflict), **conflict.payload), 409
            except SimNotFound as error:
                return jsonify(status='error', message=str(error)), 404
            except sqlite3.Error:
                return jsonify(status='error', message='本地行情暂时无法读取，请稍后重试'), 503
            response = jsonify(result)
            response.headers['Cache-Control'] = 'no-store'
            return response
        wrapper.__name__ = getattr(func, '__name__', 'sim_endpoint')
        return wrapper

    @contextmanager
    def session_block(account_id):
        """账户 + 会话 + 时钟上下文, 连接在整个 with 块内有效。

        注意不能在这里 closing(): 端点还要在 with 块内查分时, 提前关连接会
        拿到一个 ProgrammingError——而测试夹具的 KeptConnection 把 close() 变成
        空操作, 会把这个 bug 藏起来, 只在真实库上炸。
        """
        conn = connect()
        try:
            init_sim_db(conn)
            conn.commit()
            account = _account(conn, account_id)
            session = load_session(conn, account_id)
            calendar = replay_calendar(conn, db_key)
            index = calendar.index(session['cur_date']) if session['cur_date'] in calendar else -1
            yield conn, account, session, calendar, index
        finally:
            conn.close()

    @app.get('/api/sim/replay/calendar')
    @respond
    def api_sim_calendar():
        with closing(connect()) as conn:
            return {'status': 'success', 'dates': replay_calendar(conn, db_key),
                    'session_open': SESSION_OPEN, 'session_close': SESSION_CLOSE}

    @app.get('/api/sim/state')
    @respond
    def api_sim_state():
        account_id = _int_arg('account_id')
        with session_block(account_id) as (conn, account, session, calendar, index):
            positions = build_positions(conn, account_id, session, buy_fee, sell_fee)
            summary = build_account_summary(conn, account_id, positions)
            summary.update(id=account['id'], name=account['name'])
            return {'status': 'success',
                    'session': dict(session, day_index=index, day_total=len(calendar),
                                    prev_date=calendar[index - 1] if index > 0 else None,
                                    next_date=calendar[index + 1] if 0 <= index < len(calendar) - 1 else None),
                    'account': summary,
                    'positions': positions, 'nav': build_nav(conn, account_id, session, summary)}

    @app.get('/api/sim/replay/minline')
    @respond
    def api_sim_minline():
        account_id = _int_arg('account_id')
        ts_code = (request.args.get('ts_code') or '').strip()
        if not ts_code:
            raise ValueError('缺少参数 ts_code')
        with session_block(account_id) as (conn, _, session, calendar, index):
            # 客户端可以指定 date 回看**已经走过**的交易日, 但只能往前:
            # 认不出来的日期、或指向回放时钟之后的, 一律静默夹回时钟当天 ——
            # 不是报错。报错等于告诉客户端"这里有条缝可以顶", 而这条接口的
            # 不变量本来就是"客户端传什么都不影响服务端认定的当前时刻"。
            want = (request.args.get('date') or session['cur_date']).strip()
            if want not in calendar or (index >= 0 and calendar.index(want) > index):
                want = session['cur_date']
            # 上帝视角放开到整天, 但只给绘图字段; 交易路径另外锁死。
            # 回看历史日时也放开到整天 —— 那天早就走完了, 遮着反而没法复盘。
            reviewing = want != session['cur_date']
            cur_min = SESSION_CLOSE if (reviewing or request.args.get('god') == '1') \
                else session['cur_min']
            # grid 是当日全市场分钟并集, 即整根固定的时间轴。前端用它把已走过的 bar
            # 钉在正确的位置上, 于是曲线是"往右长"而不是把整根轴撑满 —— 跟券商
            # 桌面端一致。个股停牌时自己的 bar 会少几个, 但轴不动。
            return dict({'status': 'success',
                         'view_date': want, 'clock_date': session['cur_date'],
                         'grid': session_grid(conn, want, db_key)},
                        **minline_slice(conn, ts_code, want, cur_min))

    @app.post('/api/sim/account')
    @respond
    def api_sim_account():
        body = _body()
        try:
            initial_cash = float(body.get('initial_cash'))
        except (TypeError, ValueError):
            raise ValueError('初始资金必须是数字')
        if initial_cash <= 0:
            raise ValueError('初始资金必须大于 0')
        name = (body.get('name') or '模拟账户').strip()[:40] or '模拟账户'
        with closing(connect()) as conn:
            init_sim_db(conn)
            cursor = conn.execute('INSERT INTO sim_accounts (name, initial_cash) VALUES (?, ?)',
                                  (name, initial_cash))
            conn.commit()
            account_id = cursor.lastrowid
        return {'status': 'success',
                'account': {'id': account_id, 'name': name, 'initial_cash': initial_cash}}

    @app.post('/api/sim/replay/init')
    @respond
    def api_sim_init():
        body = _body()
        try:
            account_id = int(body.get('account_id'))
        except (TypeError, ValueError):
            raise ValueError('缺少参数 account_id')
        start_date = str(body.get('start_date') or '')
        reset = bool(body.get('reset'))
        with closing(connect()) as conn:
            init_sim_db(conn)
            _account(conn, account_id)
            calendar = replay_calendar(conn, db_key)
            if start_date not in calendar:
                raise ValueError(f'{start_date or "该日期"} 没有可回放的分时数据')
            if reset:
                conn.execute('DELETE FROM sim_orders WHERE account_id = ?', (account_id,))
            conn.execute(
                'INSERT INTO sim_session (account_id, cur_date, cur_min, rev, god_mode, playing, speed) '
                "VALUES (?, ?, ?, 0, 0, 0, ?) ON CONFLICT(account_id) DO UPDATE SET "
                'cur_date = excluded.cur_date, cur_min = excluded.cur_min, '
                'rev = sim_session.rev + 1, playing = 0, '
                "updated_at = datetime('now','localtime')",
                (account_id, start_date, SESSION_OPEN, int(body.get('speed') or 5)))
            conn.commit()
            session = load_session(conn, account_id)
        return {'status': 'success', 'session': session}

    @app.get('/api/sim/candidates')
    @respond
    def api_sim_candidates():
        keyword = (request.args.get('q') or '').strip()
        try:
            limit = max(1, min(int(request.args.get('limit', 20)), 100))
        except (TypeError, ValueError):
            raise ValueError('limit 必须是整数')
        with closing(connect()) as conn:
            if keyword:
                # ts_code 是 '000048.SZ' 形态, 用户只输 6 位代码也能 LIKE 到
                rows = conn.execute(
                    'SELECT ts_code, name FROM stock_basic '
                    'WHERE ts_code LIKE ? OR name LIKE ? ORDER BY ts_code LIMIT ?',
                    ('%' + keyword + '%', '%' + keyword + '%', limit)).fetchall()
            else:
                # 无关键词时给回放当日的涨幅榜; 用 stock_daily 而非分时, 有索引且便宜
                account_id = _int_arg('account_id')
                session = load_session(conn, account_id)
                rows = conn.execute(
                    'SELECT ts_code, name, close, change FROM stock_daily '
                    'WHERE trade_date = ? AND change IS NOT NULL '
                    'ORDER BY change DESC LIMIT ?', (session['cur_date'], limit)).fetchall()
        return {'status': 'success',
                'results': [dict(zip(('ts_code', 'name'), row[:2])) for row in rows]}

    @app.post('/api/sim/order')
    @respond
    def api_sim_order():
        body = _body()
        try:
            account_id = int(body.get('account_id'))
        except (TypeError, ValueError):
            raise ValueError('缺少参数 account_id')
        ts_code = str(body.get('ts_code') or '').strip()
        side = body.get('side')
        if not ts_code:
            raise ValueError('缺少参数 ts_code')
        if side not in ('buy', 'sell'):
            raise ValueError('side 只能是 buy 或 sell')
        try:
            price = float(body.get('price'))
            shares = float(body.get('shares'))
        except (TypeError, ValueError):
            raise ValueError('价格和数量必须是数字')
        if price <= 0:
            raise ValueError('价格必须大于 0')
        if shares <= 0:
            raise ValueError('数量必须大于 0')
        confirmed = bool(body.get('confirm'))

        with session_block(account_id) as (conn, _, session, _, _):
            # 时钟错位: 另一个 tab 把时钟推走了, 而这块屏幕还停在旧时刻。
            # 不拦的话订单会被记成新时刻成交, 人却以为自己操作的是画面里的那一根。
            expected_min = body.get('expected_min')
            if expected_min and expected_min != session['cur_min']:
                raise SimConflict(
                    f'回放时钟已移动到 {session["cur_min"]}（你看到的是 {expected_min}），'
                    '请刷新后重试', code='stale_clock',
                    server_min=session['cur_min'], client_min=expected_min)
            expected_rev = body.get('expected_rev')
            if expected_rev not in (None, '') and int(expected_rev) != session['rev']:
                raise SimConflict(
                    '回放状态已被其他标签页改动，请刷新后重试', code='stale_rev',
                    server_rev=session['rev'], client_rev=int(expected_rev))
            # 后端同时看 session 标志和请求参数: 前端 disable 按钮只是第一道,
            # 直接打 API 也得被挡住。
            if session['god_mode'] or body.get('god'):
                raise SimConflict('上帝视角下不可下单，仅供复盘', code='god_mode')

            quote = price_at(conn, ts_code, session['cur_date'], session['cur_min'])
            if quote is None:
                raise ValueError(f'{ts_code} 在 {session["cur_date"]} 当前时刻还没有行情（停牌或尚未出现）')
            ref_price = quote['price']
            if not confirmed and ref_price and \
                    abs(price - ref_price) / ref_price > PRICE_SANITY_BAND:
                raise SimConflict(
                    f'成交价 {price} 偏离当前价 {ref_price} 超过 '
                    f'{int(PRICE_SANITY_BAND * 100)}%，确认要按此价成交吗？',
                    code='price_sanity', needs_confirm=True,
                    ref_price=ref_price, client_price=price,
                    minute=quote['minute'])

            amount = _yuan(_cents(price * shares))
            positions = derive_positions(conn, account_id, session['cur_date'])
            held = positions.get(ts_code)
            cash = _yuan(_cents(conn.execute(
                'SELECT initial_cash FROM sim_accounts WHERE id = ?', (account_id,)
            ).fetchone()[0]) + sum(
                _cents(row[0]) for row in conn.execute(
                    'SELECT cash_delta FROM sim_orders WHERE account_id = ?', (account_id,))))

            if side == 'buy':
                if shares % 100:
                    raise ValueError('买入必须是 100 股的整数倍')
                fee = _yuan(_cents(buy_fee(amount, ts_code)))
                if _cents(cash) < _cents(amount) + _cents(fee):
                    raise ValueError(f'可用资金不足：需要 {amount + fee:.2f} 元，可用 {cash:.2f} 元')
                cash_delta = -_yuan(_cents(amount) + _cents(fee))
            else:
                if not held or held['sellable'] < shares:
                    available = held['sellable'] if held else 0.0
                    raise ValueError(
                        f'可卖数量不足：可卖 {available:g} 股'
                        + ('（A 股 T+1，当天买入当天不能卖）' if held else ''))
                fee = _yuan(_cents(sell_fee(amount, ts_code)))
                cash_delta = _yuan(_cents(amount) - _cents(fee))

            name_row = conn.execute('SELECT name FROM stock_minline_bars WHERE ts_code = ? '
                                    'AND trade_date = ? ORDER BY m LIMIT 1',
                                    (ts_code, session['cur_date'])).fetchone()
            cursor = conn.execute(
                'INSERT INTO sim_orders (account_id, ts_code, name, side, price, shares, '
                'amount, commission, transfer_fee, stamp_tax, total_fee, cash_delta, '
                'trade_date, minute, ref_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (account_id, ts_code, name_row[0] if name_row else None, side, price,
                 shares, amount, fee, 0, 0, fee, cash_delta,
                 session['cur_date'], session['cur_min'], ref_price))
            conn.commit()
            order_id = cursor.lastrowid

            positions = build_positions(conn, account_id, session, buy_fee, sell_fee)
            summary = build_account_summary(conn, account_id, positions)
            return {'status': 'success',
                    'order': {'id': order_id, 'ts_code': ts_code, 'side': side,
                              'price': price, 'shares': shares, 'amount': amount,
                              'total_fee': fee, 'cash_delta': cash_delta,
                              'trade_date': session['cur_date'],
                              'minute': session['cur_min'], 'ref_price': ref_price},
                    'positions': positions, 'account': summary}

    @app.get('/api/sim/orders')
    @respond
    def api_sim_orders():
        account_id = _int_arg('account_id')
        try:
            limit = max(1, min(int(request.args.get('limit', 200)), 1000))
        except (TypeError, ValueError):
            raise ValueError('limit 必须是整数')
        with session_block(account_id) as (conn, _, _, _, _):
            rows = conn.execute(
                'SELECT id, ts_code, name, side, price, shares, amount, total_fee, '
                'cash_delta, trade_date, minute, ref_price '
                'FROM sim_orders WHERE account_id = ? ORDER BY id DESC LIMIT ?',
                (account_id, limit)).fetchall()
            orders = [dict(zip(('id', 'ts_code', 'name', 'side', 'price', 'shares', 'amount',
                                'total_fee', 'cash_delta', 'trade_date', 'minute',
                                'ref_price'), row)) for row in rows]
            totals = conn.execute(
                "SELECT SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END), SUM(total_fee) "
                'FROM sim_orders WHERE account_id = ?', (account_id,)).fetchone()
        return {'status': 'success', 'count': len(orders), 'orders': orders,
                'summary': {'buy_cnt': totals[0] or 0, 'sell_cnt': totals[1] or 0,
                            'fee_total': _yuan(_cents(totals[2] or 0))}}

    @app.post('/api/sim/replay/step')
    @respond
    def api_sim_step():
        body = _body()
        account_id = int(body.get('account_id') or 0)
        op = body.get('op')
        steps = body.get('steps', 1)
        try:
            steps = int(steps)
        except (TypeError, ValueError):
            raise ValueError('steps 必须是整数')
        if op not in STEP_OPS:
            raise ValueError('未知的推进方式: ' + str(op))
        with session_block(account_id) as (conn, _, session, _, _):
            previous_date = session['cur_date']
            session['cur_date'], session['cur_min'] = advance(conn, session, op, steps, db_key)
            if session['cur_date'] != previous_date:
                # 跨日: 先把刚走完的那天按收盘价结一次账, T+1 解锁随之生效
                persist_nav(conn, account_id, previous_date)
            write_session(conn, account_id, session)
            conn.commit()
            session['rev'] += 1
        return {'status': 'success', 'session': session}
