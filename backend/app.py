"""
Flask + SQLite + TuShare 后端
数据存储在本地 SQLite，定时从 TuShare 拉取数据
"""

import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory

# 涨停简图 OCR 异步任务队列
_ocr_jobs = {}
from flask_cors import CORS
import tushare as ts

app = Flask(__name__)
CORS(app)

# 配置
DB_PATH = os.path.join(os.path.dirname(__file__), 'market_data.db')
TUSHARE_TOKEN = '1905eff139f559e9caa61c7363ac18e6217a33032e9e434812db1e34'
INTERVALS = [('5日', 5), ('10日', 10), ('20日', 20)]  # 涨幅榜区间 (名称, 天数)

# 初始化数据库
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 指数日线数据
    c.execute('''CREATE TABLE IF NOT EXISTS index_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        name TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        change REAL,
        volume REAL,
        UNIQUE(date, name)
    )''')

    # 涨幅榜单
    c.execute('''CREATE TABLE IF NOT EXISTS leaderboard (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        type TEXT NOT NULL,
        rank INTEGER,
        code TEXT,
        name TEXT,
        change REAL,
        volume REAL,
        reason TEXT,
        UNIQUE(date, type, rank)
    )''')

    # 盘面概览
    c.execute('''CREATE TABLE IF NOT EXISTS overview (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT UNIQUE NOT NULL,
        totalVolume REAL,
        volumeDiff REAL,
        netFlow REAL,
        upCount INTEGER,
        downCount INTEGER,
        flatCount INTEGER,
        limitUp INTEGER,
        limitDown INTEGER,
        up7 INTEGER, up5_7 INTEGER, up3_5 INTEGER, up0_3 INTEGER,
        down7 INTEGER, down5_7 INTEGER, down3_5 INTEGER, down0_3 INTEGER
    )''')

    # 涨停数据
    c.execute('''CREATE TABLE IF NOT EXISTS limitup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        code TEXT,
        name TEXT,
        marketCap REAL,
        time TEXT,
        sector TEXT,
        volume REAL,
        streak TEXT,
        keyword TEXT,
        UNIQUE(date, code, time)
    )''')

    # 股票日线数据
    c.execute('''CREATE TABLE IF NOT EXISTS stock_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        name TEXT,
        trade_date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        change REAL,
        volume REAL,
        amount REAL,
        UNIQUE(ts_code, trade_date)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_daily_date ON stock_daily(trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_daily_ts_code ON stock_daily(ts_code)')

    # 股票基础信息
    c.execute('''CREATE TABLE IF NOT EXISTS stock_basic (
        ts_code TEXT PRIMARY KEY,
        name TEXT,
        listing_date TEXT,
        market TEXT,
        status TEXT DEFAULT 'normal'
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_basic_listing ON stock_basic(listing_date)')

    # 交易日历缓存
    c.execute('''CREATE TABLE IF NOT EXISTS trading_dates_cache (
        cal_date TEXT PRIMARY KEY,
        exchange TEXT DEFAULT 'SSE'
    )''')

    # 持仓表
    c.execute('''CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        name TEXT,
        shares REAL NOT NULL,
        original_shares REAL,
        cost_price REAL NOT NULL,
        buy_date TEXT,
        note TEXT,
        closed_at TEXT,
        total_sell_amount REAL DEFAULT 0,
        realized_pnl REAL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(ts_code, cost_price, buy_date)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_positions_ts_code ON positions(ts_code)')

    # 兼容老库: 给已存在的 positions 表加新列
    existing_cols = {r[1] for r in c.execute('PRAGMA table_info(positions)').fetchall()}
    for col, decl in [('closed_at', 'TEXT'), ('total_sell_amount', 'REAL DEFAULT 0'),
                      ('realized_pnl', 'REAL DEFAULT 0'), ('original_shares', 'REAL')]:
        if col not in existing_cols:
            c.execute(f'ALTER TABLE positions ADD COLUMN {col} {decl}')
    c.execute('CREATE INDEX IF NOT EXISTS idx_positions_closed ON positions(closed_at)')

    # 成交明细表
    # 用 (trade_no, ts_code, trade_time) 组合去重, 防止源数据 trade_no 重复(如占位 2147483647)
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_no TEXT,
        ts_code TEXT NOT NULL,
        name TEXT,
        direction TEXT NOT NULL,
        price REAL NOT NULL,
        shares REAL NOT NULL,
        amount REAL,
        trade_date TEXT,
        trade_time TEXT,
        note TEXT,
        applied INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(trade_no, ts_code, trade_time)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_ts_code ON trades(ts_code)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_applied ON trades(applied)')

    conn.commit()
    conn.close()


def get_pro():
    """获取 TuShare Pro 接口"""
    if TUSHARE_TOKEN:
        ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


# ============ 股票基础数据 ============

@app.route('/api/stock/basic/sync', methods=['POST'])
def sync_stock_basic():
    """批量同步股票基础信息（上市日期、市场等）"""
    pro = get_pro()
    try:
        df = pro.stock_basic(exchange='', list_status='L')
        if len(df) == 0:
            return jsonify({'status': 'no_data', 'message': 'TuShare 返回空数据'})

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        count = 0
        for _, row in df.iterrows():
            try:
                name = row['name'] or ''
                status = 'normal'
                if '*ST' in name:
                    status = 'star'
                elif 'ST' in name:
                    status = 'st'
                elif '退' in name:
                    status = 'delisting'

                c.execute('''INSERT OR REPLACE INTO stock_basic
                    (ts_code, name, listing_date, market, status)
                    VALUES (?, ?, ?, ?, ?)''',
                    (row['ts_code'], name, row['list_date'],
                     row['market'] or '', status))
                count += 1
            except Exception as e:
                print(f'stock_basic 写入错误 {row.get("ts_code", "unknown")}: {e}')
                continue
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'count': count})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/stock/basic', methods=['GET'])
def get_stock_basic():
    """查询股票基础信息"""
    ts_code = request.args.get('ts_code')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if ts_code:
        row = c.execute('SELECT * FROM stock_basic WHERE ts_code=?', (ts_code,)).fetchone()
        conn.close()
        return jsonify(dict(row) if row else {})
    else:
        rows = c.execute('SELECT * FROM stock_basic LIMIT 100').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])


# ============ 指数数据 ============

@app.route('/api/index/daily/latest-date', methods=['GET'])
def get_index_daily_latest_date():
    """获取实际有数据的最新日期"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute('SELECT date FROM index_daily ORDER BY date DESC LIMIT 1').fetchone()
    conn.close()
    return jsonify({'latest_date': row[0] if row else None})

@app.route('/api/index/daily', methods=['GET'])
def get_index_daily():
    """获取指数日线数据"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    name = request.args.get('name', 'all')
    limit = request.args.get('limit', 100, type=int)

    if name == 'all':
        rows = c.execute(
            'SELECT * FROM index_daily ORDER BY date DESC LIMIT ?', (limit,)
        ).fetchall()
    else:
        rows = c.execute(
            'SELECT * FROM index_daily WHERE name=? ORDER BY date DESC LIMIT ?',
            (name, limit)
        ).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/index/sync', methods=['POST'])
def sync_index_data():
    """从 TuShare 同步指数数据"""
    pro = get_pro()
    indices = {
        '上证指数': '000001.SH',
        '深证成指': '399001.SZ',
        '创业板指': '399006.SZ',
        '科创50': '000688.SH'
    }

    results = []

    # 支持指定日期，否则获取最近有数据的交易日
    trade_date = request.args.get('date')
    if not trade_date:
        for i in range(7):
            test_date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
            df_test = pro.index_daily(ts_code='000001.SH', start_date=test_date, end_date=test_date)
            if len(df_test) > 0:
                trade_date = test_date
                break

    if not trade_date:
        return jsonify({'status': 'error', 'message': '无法获取交易日数据'})

    # TuShare index_daily 返回字段: ts_code, trade_date, open, high, low, close, change, pct_chg, vol, amount
    for name, code in indices.items():
        try:
            df = pro.index_daily(
                ts_code=code,
                start_date=trade_date,
                end_date=trade_date
            )

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()

            for _, row in df.iterrows():
                c.execute('''INSERT OR REPLACE INTO index_daily
                    (date, name, open, high, low, close, change, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (row['trade_date'], name, row['open'], row['high'], row['low'],
                     row['close'], row['pct_chg'], row['amount'] / 100000))  # tushare返回千元，转为亿

            conn.commit()
            results.append({'name': name, 'count': len(df)})
            conn.close()

        except Exception as e:
            results.append({'name': name, 'error': str(e)})

    return jsonify({'status': 'success', 'date': trade_date, 'results': results})


# ============ 涨幅榜单 ============


@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    """获取涨幅榜单

    参数:
        type: 5日|10日|20日|all (默认 all)
        date: YYYYMMDD 格式（可选，默认最新有数据交易日）
        limit: 返回条数（默认 10）
    """
    interval_map = dict(INTERVALS)
    lb_type = request.args.get('type', 'all')
    limit = request.args.get('limit', 10, type=int)
    date = request.args.get('date')

    # type == 'all' 时返回数据库中已有历史数据
    if lb_type == 'all':
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        query = 'SELECT * FROM leaderboard'
        params = []
        if date:
            query += ' WHERE date=?'
            params.append(date)
        query += ' ORDER BY date DESC, type, rank'
        rows = c.execute(query, params).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    if lb_type not in interval_map:
        return jsonify({'status': 'error', 'message': f'无效的 type: {lb_type}'}), 400

    days = interval_map[lb_type]

    # 先查 leaderboard 表是否有该日期的数据，有则直接返回
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        'SELECT * FROM leaderboard WHERE date=? AND type=? ORDER BY rank LIMIT ?',
        (date, lb_type, limit)
    ).fetchall()
    conn.close()
    if rows:
        return jsonify([dict(r) for r in rows])

    # 表里没有数据，返回 need_sync 让前端触发同步
    return jsonify({'need_sync': True, 'date': date, 'type': lb_type})


@app.route('/api/leaderboard/latest-date', methods=['GET'])
def get_leaderboard_latest_date():
    """返回 leaderboard 表中最新有数据的日期"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute('SELECT MAX(date) FROM leaderboard').fetchone()
    conn.close()
    return jsonify({'latest_date': row[0] if row and row[0] else ''})


@app.route('/api/leaderboard/top1', methods=['GET'])
def get_leaderboard_top1():
    """返回历史 TOP1 数据（每种类型的 rank=1 记录），用于走势图

    参数:
        date: YYYYMMDD 格式（可选，默认最新日期）
        limit: 每种类型返回条数（默认 30）
    """
    limit = request.args.get('limit', 30, type=int)
    date = request.args.get('date')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if date:
        rows = c.execute('''
            SELECT date, type, code, name, change FROM leaderboard
            WHERE rank=1 AND date <= ? ORDER BY date DESC LIMIT ?
        ''', (date, limit * 3)).fetchall()
    else:
        rows = c.execute('''
            SELECT date, type, code, name, change FROM leaderboard
            WHERE rank=1 ORDER BY date DESC LIMIT ?
        ''', (limit * 3,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/leaderboard/status', methods=['GET'])
def get_leaderboard_status():
    """查询指定日期的 leaderboard 数据状态"""
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': '缺少 date 参数'}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        'SELECT DISTINCT type FROM leaderboard WHERE date=? ORDER BY type',
        (date,)
    ).fetchall()
    conn.close()

    intervals = [r[0] for r in rows]
    return jsonify({
        'date': date,
        'has_data': len(intervals) > 0,
        'intervals': intervals
    })


@app.route('/api/leaderboard/sync', methods=['POST'])
def sync_leaderboard():
    """计算并保存涨幅榜单（5日/10日/20日）"""
    date = request.args.get('date')
    pro = get_pro()

    # 找最新有数据的交易日
    if not date:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        row = c.execute(
            'SELECT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 1'
        ).fetchone()
        conn.close()
        date = row[0] if row else None

    if not date:
        return jsonify({'status': 'error', 'message': '无交易日期数据'})

    # 检查 stock_daily 是否有足够历史数据覆盖最大区间（20日）
    # 数据不够的话主动从 TuShare 往前补
    max_interval = max(d for _, d in INTERVALS)  # 20
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    existing_dates = [r[0] for r in c.execute(
        'SELECT trade_date FROM stock_daily ORDER BY trade_date DESC'
    ).fetchall()]
    conn.close()

    if date in existing_dates:
        date_idx = existing_dates.index(date)
        # existing_dates 是降序排列（最新在前），date_idx 是 date 在其中的位置
        # 从 date 当天往回数，有 len(existing_dates) - date_idx 个交易日的数据
        available = len(existing_dates) - date_idx
        if available < max_interval + 1:
            need = max_interval + 1 - available
            # 从 date 之前按时间顺序补 (date 往前 need 天)
            sorted_dates = sorted(existing_dates)
            if date in sorted_dates:
                date_pos = sorted_dates.index(date)
                # 需要再往前 need 条交易日
                start_pos = max(0, date_pos - need)
                missing_dates = sorted_dates[start_pos:date_pos]
                for d in missing_dates:
                    sync_stock_daily(d)
                # 补完后再取最新的日期列表
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                existing_dates = [r[0] for r in c.execute(
                    'SELECT trade_date FROM stock_daily ORDER BY trade_date DESC'
                ).fetchall()]
                conn.close()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    results = []

    for lb_type, days in INTERVALS:
        stocks = get_top_stocks(days, date, 10)
        for i, s in enumerate(stocks):
            c.execute('''INSERT OR REPLACE INTO leaderboard
                (date, type, rank, code, name, change, volume, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (date, lb_type, i+1, s['code'], s['name'],
                 s['change'], s.get('amount', 0), s.get('reason', '')))
        results.append({'type': lb_type, 'count': len(stocks)})

    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'date': date, 'results': results})


# ============ 股票日线数据 ============

def sync_stock_daily(trade_date):
    """从 TuShare 同步单日所有股票数据"""
    pro = get_pro()
    try:
        df = pro.daily(trade_date=trade_date)
        if len(df) == 0:
            return 0

        # 批量获取股票名称
        codes = df['ts_code'].unique().tolist()
        name_map = {}
        batch_size = 100
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i+batch_size]
            try:
                df_basic = pro.stock_basic(ts_code=','.join(batch))
                for _, r in df_basic.iterrows():
                    name_map[r['ts_code']] = r['name']
            except Exception as e:
                # 批量失败，逐个获取
                for code in batch:
                    try:
                        basic = pro.stock_basic(ts_code=code)
                        if len(basic) > 0:
                            name_map[code] = basic.iloc[0]['name']
                    except:
                        name_map[code] = ''

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        count = 0
        for _, row in df.iterrows():
            try:
                name = name_map.get(row['ts_code'], '')
                c.execute('''INSERT OR IGNORE INTO stock_daily
                    (ts_code, name, trade_date, open, high, low, close, change, volume, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (row['ts_code'], name, trade_date,
                     row['open'], row['high'], row['low'], row['close'],
                     row['pct_chg'], row['vol'], row['amount']))
                count += 1
            except Exception as e:
                print(f'股票 {row["ts_code"]} 写入错误: {e}')
                continue
        conn.commit()
        conn.close()
        return count
    except Exception as e:
        print(f'sync_stock_daily error: {e}')
        return -1


def sync_stock_daily_batch(start_date, end_date, dry_run=False):
    """批量同步 start_date ~ end_date 范围内的缺失交易日线数据

    - 从 trade_cal 获取交易日历
    - 找出本地数据库缺失的日期
    - 逐日调用 sync_stock_daily 补数据
    - 速率限制: 每200天休息3秒
    """
    pro = get_pro()
    df_cal = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date)
    trading_dates = sorted(df_cal[df_cal['is_open'] == 1]['cal_date'].tolist())

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    existing = set(r[0] for r in c.execute('SELECT DISTINCT trade_date FROM stock_daily').fetchall())
    conn.close()

    to_sync = [d for d in trading_dates if d not in existing]
    if dry_run:
        return {'total': len(trading_dates), 'existing': len(existing), 'missing': len(to_sync), 'dates': to_sync}

    results = {'success': 0, 'failed': 0, 'errors': []}
    for i, date in enumerate(to_sync):
        ret = sync_stock_daily(date)
        if ret > 0:
            results['success'] += 1
        else:
            results['failed'] += 1
            if ret < 0:
                results['errors'].append(date)
        if (i + 1) % 200 == 0:
            time.sleep(3)
        else:
            time.sleep(0.1)
    return results


@app.route('/api/stock/sync/batch', methods=['POST'])
def sync_stock_daily_batch_api():
    """批量同步历史日线数据

    参数:
        start_date: 起始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)
        dry_run: 1 表示只返回缺失日期列表，不实际同步
    """
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    dry_run = request.args.get('dry_run', '0') == '1'

    if not start_date or not end_date:
        return jsonify({'status': 'error', 'message': '需要 start_date 和 end_date 参数'}), 400

    result = sync_stock_daily_batch(start_date, end_date, dry_run=dry_run)
    if dry_run:
        return jsonify({'status': 'ok', **result})
    return jsonify({'status': 'ok', **result})


@app.route('/api/stock/dates-known', methods=['GET'])
def get_known_stock_dates():
    """获取本地数据库已有股票日线数据的日期列表"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        'SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC'
    ).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])


@app.route('/api/stock/sync', methods=['POST'])
def sync_stock_daily_api():
    """同步股票日线数据"""
    date = request.args.get('date')
    if not date:
        for i in range(7):
            test_date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
            try:
                df = get_pro().daily(trade_date=test_date)
                if len(df) > 0:
                    date = test_date
                    break
            except:
                continue

    if not date:
        return jsonify({'status': 'error', 'message': '无法获取有效日期'})

    count = sync_stock_daily(date)
    if count < 0:
        return jsonify({'status': 'error', 'message': '同步失败'})
    return jsonify({'status': 'success', 'date': date, 'count': count})


# ============ 股票过滤与榜单计算 ============

def load_stock_basic_map(conn):
    """加载股票基础信息到内存字典"""
    c = conn.cursor()
    rows = c.execute('SELECT ts_code, name, listing_date, market, status FROM stock_basic').fetchall()
    return {row['ts_code']: dict(row) for row in rows}


def is_new_stock(ts_code, end_date, stock_basic_map, conn, threshold=20):
    """上市未满 threshold 个交易日返回 True"""
    sb = stock_basic_map.get(ts_code)
    if not sb or not sb.get('listing_date'):
        return True
    listing_date = sb['listing_date']
    if listing_date > end_date:
        return True
    count = conn.execute(
        '''SELECT COUNT(DISTINCT trade_date) FROM stock_daily
           WHERE ts_code=? AND trade_date > ? AND trade_date <= ?''',
        (ts_code, listing_date, end_date)
    ).fetchone()[0]
    return count < threshold


def is_st_or_delisting(name):
    """名称中含 ST/*ST/退市相关字样返回 True"""
    if not name:
        return True
    name_upper = name.upper()
    return 'ST' in name_upper or '*ST' in name_upper or '退' in name


def is_suspended(ts_code, start_date, end_date, conn):
    """区间内无任何交易数据返回 True"""
    count = conn.execute(
        'SELECT COUNT(*) FROM stock_daily WHERE ts_code=? AND trade_date BETWEEN ? AND ?',
        (ts_code, start_date, end_date)
    ).fetchone()[0]
    return count == 0


def should_exclude(ts_code, name, start_date, end_date, stock_basic_map, conn):
    """判断股票是否应被排除"""
    if is_new_stock(ts_code, end_date, stock_basic_map, conn):
        return True
    if is_st_or_delisting(name):
        return True
    if is_suspended(ts_code, start_date, end_date, conn):
        return True
    return False


def get_top_stocks(interval_days, end_date, limit=10):
    """获取区间涨幅前N名股票（含过滤逻辑）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 加载股票基础信息
    stock_basic_map = load_stock_basic_map(conn)

    # 获取 end_date 前 interval_days 个交易日
    dates = c.execute(
        'SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC'
    ).fetchall()
    dates = [d['trade_date'] for d in dates]

    if end_date not in dates:
        conn.close()
        return []
    end_idx = dates.index(end_date)
    if len(dates) - end_idx < interval_days + 1:
        conn.close()
        return []
    start_date = dates[end_idx + interval_days]

    # 获取 end_date 有数据的股票
    stocks = c.execute(
        'SELECT DISTINCT ts_code FROM stock_daily WHERE trade_date=?', (end_date,)
    ).fetchall()

    results = []
    for (ts_code,) in stocks:
        # 获取股票名称：优先 stock_daily， fallback 到 stock_basic
        name_row = c.execute(
            'SELECT name FROM stock_daily WHERE ts_code=? AND trade_date=?',
            (ts_code, end_date)
        ).fetchone()
        name = name_row['name'] if name_row else ''
        if not name:
            basic_row = c.execute('SELECT name FROM stock_basic WHERE ts_code=?', (ts_code,)).fetchone()
            name = basic_row['name'] if basic_row else ''
        if not name:
            continue

        # 过滤
        if should_exclude(ts_code, name, start_date, end_date, stock_basic_map, conn):
            continue

        # 过滤
        if should_exclude(ts_code, name, start_date, end_date, stock_basic_map, conn):
            continue

        # 获取起止价格
        start_row = c.execute(
            'SELECT close FROM stock_daily WHERE ts_code=? AND trade_date=?',
            (ts_code, start_date)
        ).fetchone()
        end_row = c.execute(
            'SELECT close FROM stock_daily WHERE ts_code=? AND trade_date=?',
            (ts_code, end_date)
        ).fetchone()
        if not (start_row and end_row and start_row['close'] > 0):
            continue

        # 计算区间涨跌幅
        change = (end_row['close'] / start_row['close'] - 1) * 100

        # 计算区间总成交额（千元转亿）
        amount_row = c.execute(
            '''SELECT SUM(amount) FROM stock_daily
               WHERE ts_code=? AND trade_date BETWEEN ? AND ?''',
            (ts_code, start_date, end_date)
        ).fetchone()
        amount = (amount_row[0] or 0) / 100000

        results.append({
            'code': ts_code,
            'name': name,
            'change': round(change, 2),
            'close': end_row['close'],
            'amount': round(amount, 2)
        })

    conn.close()

    # 排序：涨幅降序，成交额降序
    results.sort(key=lambda x: (x['change'], x['amount']), reverse=True)
    return results[:limit]


# ============ 盘面概览 ============

@app.route('/api/overview', methods=['GET'])
def get_overview():
    """获取盘面概览"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    limit = request.args.get('limit', 100, type=int)
    rows = c.execute(
        'SELECT * FROM overview WHERE upCount > 0 ORDER BY date DESC LIMIT ?', (limit,)
    ).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/overview/sync', methods=['POST'])
def sync_overview():
    """从 TuShare 同步盘面数据（同时同步指数数据）"""
    pro = get_pro()
    indices = {
        '上证指数': '000001.SH',
        '深证成指': '399001.SZ',
        '创业板指': '399006.SZ',
        '科创50': '000688.SH'
    }

    try:
        # 支持指定日期，否则找最近有数据的交易日
        trade_date = request.args.get('date')
        if not trade_date:
            for i in range(7):
                test_date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
                df_test = pro.daily(trade_date=test_date)
                if len(df_test) > 0:
                    trade_date = test_date
                break

        if not trade_date:
            return jsonify({'status': 'no_data', 'message': '找不到有效交易日'})

        # 同步指数数据
        for name, code in indices.items():
            try:
                df_idx = pro.index_daily(ts_code=code, start_date=trade_date, end_date=trade_date)
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                for _, row in df_idx.iterrows():
                    c.execute('''INSERT OR REPLACE INTO index_daily
                        (date, name, open, high, low, close, change, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (row['trade_date'], name, row['open'], row['high'], row['low'],
                         row['close'], row['pct_chg'], row['amount'] / 100000))  # tushare返回千元，转为亿
                conn.commit()
                conn.close()
            except Exception as e:
                print(f'指数同步错误 {name}: {e}')

        # 同步盘面数据
        df = pro.daily(trade_date=trade_date)
        if len(df) == 0:
            return jsonify({'status': 'no_data', 'message': f'{trade_date} 非交易日或无数据'})

        up_count = len(df[df['pct_chg'] > 0])
        down_count = len(df[df['pct_chg'] < 0])
        flat_count = len(df[df['pct_chg'] == 0])
        limit_up = len(df[df['pct_chg'] >= 9.9])
        limit_down = len(df[df['pct_chg'] <= -9.9])
        up7 = len(df[df['pct_chg'] > 7])
        up5_7 = len(df[(df['pct_chg'] > 5) & (df['pct_chg'] <= 7)])
        up3_5 = len(df[(df['pct_chg'] > 3) & (df['pct_chg'] <= 5)])
        up0_3 = len(df[(df['pct_chg'] > 0) & (df['pct_chg'] <= 3)])
        down0_3 = len(df[(df['pct_chg'] >= -3) & (df['pct_chg'] < 0)])
        down3_5 = len(df[(df['pct_chg'] >= -5) & (df['pct_chg'] < -3)])
        down5_7 = len(df[(df['pct_chg'] >= -7) & (df['pct_chg'] < -5)])
        down7 = len(df[df['pct_chg'] < -7])

        # 同时把涨停股写入 limitup 表（从日线数据直接筛选，不依赖 limit_list_d）
        # 需要获取股票名称
        codes = df[df['pct_chg'] >= 9.9]['ts_code'].tolist()
        name_map = {}
        for i in range(0, len(codes), 100):
            batch = codes[i:i+100]
            try:
                df_basic = pro.stock_basic(ts_code=','.join(batch))
                for _, r in df_basic.iterrows():
                    name_map[r['ts_code']] = r['name']
            except:
                pass

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for _, row in df[df['pct_chg'] >= 9.9].iterrows():
            ts_code = row['ts_code']
            c.execute('''INSERT OR REPLACE INTO limitup
                (date, code, name, marketCap, time, sector, volume, streak, keyword)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (trade_date, ts_code, name_map.get(ts_code, ''),
                 row.get('amount', 0) or 0, '', '',
                 row.get('vol', 0) or 0, '首板', ''))
        conn.commit()

        # 全市场总成交额 = sum(daily.amount) / 100000 (千元转亿)
        total_volume = df['amount'].sum() / 100000

        # 获取前一个交易日，计算较前日差额
        prev_date = None
        for i in range(1, 30):
            test_date = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=i)).strftime('%Y%m%d')
            df_prev = pro.daily(trade_date=test_date)
            if len(df_prev) > 0:
                prev_date = test_date
                break

        volume_diff = 0
        if prev_date:
            df_prev = pro.daily(trade_date=prev_date)
            prev_volume = df_prev['amount'].sum() / 100000 if len(df_prev) > 0 else 0
            volume_diff = total_volume - prev_volume

        # 获取全市场资金净流入 (moneyflow.net_mf_amount 总和，单位元，转为亿)
        net_flow = 0
        for i in range(7):
            test_date = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=i)).strftime('%Y%m%d')
            try:
                df_mf = pro.moneyflow(trade_date=test_date)
                if len(df_mf) > 0:
                    net_flow = df_mf['net_mf_amount'].sum() / 10000  # 元转亿
                    break
            except Exception as e:
                print(f'资金净流入获取错误: {e}')
                continue

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO overview
            (date, totalVolume, volumeDiff, netFlow, upCount, downCount,
             flatCount, limitUp, limitDown, up7, up5_7, up3_5, up0_3,
             down7, down5_7, down3_5, down0_3)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (trade_date, total_volume, volume_diff, net_flow, up_count, down_count,
             flat_count, limit_up, limit_down, up7, up5_7, up3_5, up0_3,
             down7, down5_7, down3_5, down0_3))
        conn.commit()
        conn.close()

        return jsonify({'status': 'success', 'date': trade_date, 'up': up_count, 'down': down_count, 'volumeDiff': volume_diff, 'netFlow': net_flow})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


# ============ 涨停数据 ============

# 简单限流：记录上次同步时间，间隔内拒绝重复请求
_last_sync_time = {}  # date -> timestamp

@app.route('/api/limitup', methods=['GET'])
def get_limitup():
    """获取涨停数据"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    date = request.args.get('date')
    sector = request.args.get('sector', 'all')
    limit = request.args.get('limit', 200, type=int)

    query = 'SELECT * FROM limitup WHERE 1=1'
    params = []

    if date:
        query += ' AND date=?'
        params.append(date)
    if sector != 'all':
        query += ' AND sector=?'
        params.append(sector)

    query += ' ORDER BY date DESC LIMIT ?'
    params.append(limit)

    rows = c.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/limitup', methods=['DELETE'])
def delete_limitup():
    """删除涨停数据"""
    date = request.args.get('date')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if date:
        c.execute('DELETE FROM limitup WHERE date=?', (date,))
    else:
        c.execute('DELETE FROM limitup')
    conn.commit()
    cnt = c.rowcount
    conn.close()
    return jsonify({'status': 'ok', 'deleted': cnt})


@app.route('/api/limitup/sync', methods=['POST'])
def sync_limitup():
    """从 TuShare 同步涨停数据（受限于1次/分钟）"""
    global _last_sync_time
    import time

    pro = get_pro()
    today = datetime.now().strftime('%Y%m%d')

    try:
        # 简单限流：距上次同步不足60秒则拒绝
        key = 'limitup'
        now = time.time()
        last = _last_sync_time.get(key, 0)
        if now - last < 60:
            # 返回已有数据，不重复请求
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM limitup WHERE date=?', (today,))
            count = c.fetchone()[0]
            conn.close()
            return jsonify({
                'status': 'rate_limited',
                'message': f'限流中，请{(60 - int(now - last))}秒后重试',
                'cached_count': count
            })

        df = pro.limit_list_d(start_date=today, end_date=today)
        _last_sync_time[key] = now

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        count = 0
        for _, row in df.iterrows():
            try:
                c.execute('''INSERT OR REPLACE INTO limitup
                    (date, code, name, marketCap, time, sector, volume, streak, keyword)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (today, row['ts_code'], row['name'], row['total_mv'] or 0,
                     row['first_time'] or '', row['industry'] or '',
                     row['amount'] or 0, '首板', ''))
                count += 1
            except:
                continue

        conn.commit()
        conn.close()

        return jsonify({'status': 'success', 'date': today, 'count': count})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/limitup/parse-image', methods=['POST'])
def parse_limitup_image():
    """异步解析涨停简图：立即返回 job_id，后台处理 mmx OCR（约3-4分钟）"""
    if 'image' not in request.files:
        return jsonify({'status': 'error', 'message': '请上传图片文件'}), 400
    image_file = request.files['image']
    if not image_file.filename:
        return jsonify({'status': 'error', 'message': '请上传图片文件'}), 400

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    import uuid
    job_id = str(uuid.uuid4())[:8]
    filename = f'limitup_{datetime.now().strftime("%Y%m%d%H%M%S")}_{job_id}_{image_file.filename}'
    filepath = os.path.join(upload_dir, filename)
    image_file.save(filepath)

    trade_date = request.form.get('date', '')

    _ocr_jobs[job_id] = {'status': 'processing', 'stage': 'saving_image'}

    def do_ocr():
        import re, json as _json
        prompt = (
            'Output ONLY valid JSON array with the EXACT board/sector structure visible in the image. '
            'Format: [{"sector_name": "板块名*股票数", "stocks": [{...stock objects...}]}] '
            'Each stock object: {"code": "代码", "name": "名称", "days": "连板数", "time": "封板时间", "market_cap": "市值", "turnover": "成交额", "keywords": "关键词"}. '
            'Preserve ALL boards/sectors shown. Include every single stock visible. '
            '板块名用中文，如 "机器人*15"、"氟化工*9" 其中*后的数字表示该板块涨停股数量.'
        )
        try:
            _ocr_jobs[job_id]['stage'] = 'ocr_starting'
            r = subprocess.run(
                ['/usr/local/bin/mmx', 'vision', 'describe', '--image', filepath,
                 '--output', 'json', '--prompt', prompt],
                capture_output=True, text=True, timeout=600
            )
            _ocr_jobs[job_id]['stage'] = 'ocr_done'
            # 不在这里删除文件，留到所有OCR完成后再删
            date_prompt = (
                'What date is shown on this image? '
                'Output ONLY the date text visible in the title or header area, '
                'e.g. "05.15" or "5月15日". '
                'Do not add any explanation or additional text.'
            )
            date_r = subprocess.run(
                ['/usr/local/bin/mmx', 'vision', 'describe', '--image', filepath,
                 '--output', 'json', '--prompt', date_prompt],
                capture_output=True, text=True, timeout=30
            )
            parsed_date = trade_date
            if date_r.returncode == 0:
                try:
                    date_outer = _json.loads(date_r.stdout.strip())
                    date_str = date_outer.get('content', '').strip()
                    if date_str:
                        # 解析 "05.15" -> "20260515"
                        m = re.match(r'(\d{1,2})\.(\d{1,2})', date_str)
                        if m:
                            month, day = int(m.group(1)), int(m.group(2))
                            from datetime import datetime as _dt
                            year = _dt.now().year
                            parsed_date = f'{year}{month:02d}{day:02d}'
                        else:
                            m2 = re.match(r'(\d{1,2})月(\d{1,2})日', date_str)
                            if m2:
                                month, day = int(m2.group(1)), int(m2.group(2))
                                from datetime import datetime as _dt
                                year = _dt.now().year
                                parsed_date = f'{year}{month:02d}{day:02d}'
                except:
                    pass
            if not parsed_date:
                parsed_date = datetime.now().strftime('%Y%m%d')
            parsed_data = []
            try:
                outer = _json.loads(r.stdout.strip())
                inner = outer.get('content', '')
                # 去除 markdown 代码块标记 ```json ... ```
                inner = re.sub(r'^```json\s*', '', inner).strip()
                inner = re.sub(r'```\s*$', '', inner).strip()
                if inner.startswith('```'):
                    inner = re.sub(r'^```[a-z]*\s*', '', inner).strip()
                    inner = re.sub(r'```\s*$', '', inner).strip()
                if inner.startswith('['):
                    parsed_data = _json.loads(inner)
                elif inner.startswith('"'):
                    decoded = _json.loads(inner)
                    if isinstance(decoded, str) and decoded.startswith('['):
                        parsed_data = _json.loads(decoded)
            except:
                pass
            if not parsed_data:
                m = re.search(r'\[\s*\{.*?\}\s*\]', r.stdout.strip(), re.DOTALL)
                if m:
                    try:
                        parsed_data = _json.loads(m.group())
                    except:
                        pass

            _ocr_jobs[job_id]['stage'] = 'parsing'
            boards_map = {}
            streak_stocks = []

            # 兼容两种格式：扁平格式 [{sector, code, ...}] 和 结构化格式 [{sector_name, stocks}]
            def flatten_structured(data):
                """将结构化格式展平为扁平格式"""
                result = []
                for item in data:
                    if isinstance(item, dict) and 'stocks' in item:
                        # 结构化格式：提取板块名和所有股票
                        sector_base = (item.get('sector_name') or '其他').strip()
                        sector_base = re.sub(r'\*\d+$', '', sector_base)
                        for stock in item.get('stocks', []):
                            stock_copy = dict(stock)
                            stock_copy['sector'] = sector_base
                            result.append(stock_copy)
                    elif isinstance(item, dict):
                        result.append(item)
                return result

            if parsed_data and any('stocks' in item for item in parsed_data):
                # 检测到结构化格式，先展平
                parsed_data = flatten_structured(parsed_data)

            for s in parsed_data:
                sector = (s.get('sector') or '其他').strip()
                sector = re.sub(r'\*\d+$', '', sector)
                if sector not in boards_map:
                    boards_map[sector] = []
                boards_map[sector].append(s)
                days = s.get('days', '1') or '1'
                if days not in ('1', '首板', '1天'):
                    streak_stocks.append(s)

            if not parsed_date:
                parsed_date = datetime.now().strftime('%Y%m%d')

            def norm(v):
                if not v or v in ('1', '首板', '1天'):
                    return '首板'
                return str(v)

            _ocr_jobs[job_id]['stage'] = 'writing_db'
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('DELETE FROM limitup WHERE date=?', (parsed_date,))
            cnt = 0
            for s in streak_stocks:
                try:
                    c.execute('''INSERT INTO limitup (date,code,name,marketCap,time,sector,volume,streak,keyword)
                        VALUES (?,?,?,?,?,?,?,?,?)''',
                        (parsed_date, s.get('code',''), s.get('name',''),
                         float(s.get('market_cap') or 0),
                         s.get('time',''), s.get('sector',''),
                         float(s.get('turnover') or 0), norm(s.get('days','')), s.get('keywords', '')))
                    cnt += 1
                except:
                    pass
            for board_sector, stocks in boards_map.items():
                for s in stocks:
                    try:
                        c.execute('''INSERT INTO limitup (date,code,name,marketCap,time,sector,volume,streak,keyword)
                            VALUES (?,?,?,?,?,?,?,?,?)''',
                            (parsed_date, s.get('code',''), s.get('name',''),
                             float(s.get('market_cap') or 0),
                             s.get('time',''), board_sector,
                             float(s.get('turnover') or 0), norm(s.get('days', '1')), s.get('keywords', '')))
                        cnt += 1
                    except:
                        pass
            conn.commit()
            conn.close()
            # 删除临时文件
            try:
                os.remove(filepath)
            except Exception:
                pass
            _ocr_jobs[job_id] = {'status': 'done', 'stage': 'done', 'count': cnt, 'date': parsed_date,
                                  'boards': list(boards_map.keys()), 'streak_count': len(streak_stocks)}
        except Exception as e:
            _ocr_jobs[job_id] = {'status': 'error', 'stage': 'error', 'error': str(e)}

    import threading
    t = threading.Thread(target=do_ocr)
    t.daemon = True
    t.start()

    return jsonify({'status': 'processing', 'job_id': job_id})


@app.route('/api/limitup/parse-status/<job_id>', methods=['GET'])
def get_parse_status(job_id):
    job = _ocr_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': '任务不存在'}), 404
    if job['status'] == 'done':
        return jsonify({'status': 'done', **job})
    elif job['status'] == 'error':
        return jsonify({'status': 'error', 'message': job['error']}), 500
    return jsonify({'status': 'processing'})


# ============ 静态文件 ============

@app.route('/')
def index():
    return send_from_directory(os.path.join(os.path.dirname(__file__), '..'), 'dashboard/index.html')

@app.route('/dashboard/<path:filename>')
def dashboard_static(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), '..', 'dashboard'), filename)

# ============ 健康检查 ============

@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    exists = os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    conn.close()

    return jsonify({
        'status': 'ok',
        'database': DB_PATH,
        'exists': exists,
        'tables': [t[0] for t in tables]
    })


# ============ 数据统计 ============

@app.route('/api/trading-dates', methods=['GET'])
def get_trading_dates():
    """获取A股所有交易日（优先从本地缓存读取）"""
    start_date = request.args.get('start_date', '20200101')
    end_date = request.args.get('end_date', datetime.now().strftime('%Y%m%d'))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 先查本地缓存
    rows = c.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date',
        (start_date, end_date)
    ).fetchall()
    local_dates = [r[0] for r in rows]
    conn.close()

    # 如果本地最新日期比今天早，自动从 TuShare 补最新数据
    today = datetime.now().strftime('%Y%m%d')
    if local_dates and local_dates[-1] < today:
        pro = get_pro()
        try:
            df = pro.trade_cal(exchange='SSE', start_date=local_dates[-1], end_date=today)
            new_dates = df[df['is_open'] == 1]['cal_date'].tolist()
            if new_dates:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                for cal_date in new_dates:
                    c.execute('INSERT OR IGNORE INTO trading_dates_cache (cal_date) VALUES (?)', (cal_date,))
                conn.commit()
                conn.close()
                # 重新查询
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                rows = c.execute(
                    'SELECT cal_date FROM trading_dates_cache WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date',
                    (start_date, end_date)
                ).fetchall()
                local_dates = [r[0] for r in rows]
                conn.close()
        except Exception as e:
            print(f'补全交易日历失败: {e}')

    if local_dates:
        return jsonify(local_dates)

    # 缓存为空，拉取 TuShare 并永久存储
    pro = get_pro()
    try:
        df = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date)
        trading_dates = df[df['is_open'] == 1]['cal_date'].tolist()

        if not trading_dates:
            return jsonify({'status': 'error', 'message': 'TuShare 返回空数据'}), 500

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for cal_date in trading_dates:
            c.execute('INSERT OR IGNORE INTO trading_dates_cache (cal_date) VALUES (?)', (cal_date,))
        conn.commit()
        conn.close()

        return jsonify(trading_dates)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/is-trading-date', methods=['GET'])
def is_trading_date():
    """检查指定日期是否为交易日"""
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': '缺少 date 参数'}), 400

    pro = get_pro()
    try:
        df = pro.trade_cal(exchange='SSE', start_date=date, end_date=date)
        if len(df) > 0:
            is_open = df.iloc[0]['is_open'] == 1
            return jsonify({'date': date, 'is_trading_date': is_open})
        return jsonify({'date': date, 'is_trading_date': False})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/stats', methods=['GET'])
def stats():
    """获取各表数据量统计"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    stats = {}
    for table in ['index_daily', 'leaderboard', 'overview', 'limitup', 'trading_dates_cache']:
        try:
            count = c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            last_date = c.execute(
                f'SELECT date FROM {table} ORDER BY date DESC LIMIT 1'
            ).fetchone()
            stats[table] = {'count': count, 'last_date': last_date[0] if last_date else None}
        except:
            stats[table] = {'count': 0, 'last_date': None}

    conn.close()
    return jsonify(stats)


@app.route('/api/stock/dates-check', methods=['GET'])
def check_stock_dates():
    """检查给定日期列表中哪些已有股票日线数据"""
    dates_str = request.args.get('dates', '')
    if not dates_str:
        return jsonify([])
    dates = [d.strip() for d in dates_str.split(',')]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    placeholders = ','.join(['?' for _ in dates])
    rows = c.execute(
        f'SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date IN ({placeholders})',
        dates
    ).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])


def get_date_range(base_date, months):
    """计算基准日期往前N个月的日期范围（含首尾）"""
    dt = datetime.strptime(base_date, '%Y%m%d')
    start_dt = dt - timedelta(days=months * 30)
    return start_dt.strftime('%Y%m%d'), base_date


@app.route('/api/frequency', methods=['GET'])
def get_frequency():
    """上榜频次统计

    参数:
        date: YYYYMMDD 格式（必选，当前选中日期）

    返回:
        {
          "date": "20260520",
          "tabs": ["1个月", "6个月", "12个月"],  # 前端Tab切换选项
          "ranges": {
            "1个月": {"start": "20260420", "end": "20260520"},
            "6个月": {"start": "20251121", "end": "20260520"},
            "12个月": {"start": "20250525", "end": "20260520"}
          },
          "frequency": {
            "5日": [
              {"code": "000001", "name": "平安银行", "1个月": 2, "6个月": 8, "12个月": 15},
              ...
            ],
            "10日": [...],
            "20日": [...]
          }
        }
    """
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': '缺少 date 参数'}), 400

    # 计算三个时间区间
    range_labels = ["1个月", "6个月", "12个月"]
    ranges = {}
    for label in range_labels:
        months = int(label.split("个月")[0])
        dt = datetime.strptime(date, '%Y%m%d')
        start_dt = dt - timedelta(days=months * 30)
        ranges[label] = {"start": start_dt.strftime('%Y%m%d'), "end": date}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    result = {
        "date": date,
        "tabs": range_labels,
        "ranges": ranges,
        "frequency": {}
    }

    # 遍历 5日、10日、20日 三个榜单
    for lb_type in ["5日", "10日", "20日"]:
        # 用12个月的起始日期作为查询起点（覆盖所有区间）
        rows = c.execute('''
            SELECT code, name, date
            FROM leaderboard
            WHERE type=? AND date >= ? AND date <= ?
            ORDER BY date DESC, rank
        ''', (lb_type, ranges["12个月"]["start"], date)).fetchall()

        # 统计每只股票在三个时间区间的出现次数
        stats = {}
        for row in rows:
            code = row['code']
            if code not in stats:
                stats[code] = {'code': code, 'name': row['name'], '1个月': 0, '6个月': 0, '12个月': 0}

            trade_date = row['date']
            for label in range_labels:
                r = ranges[label]
                if r['start'] <= trade_date <= r['end']:
                    stats[code][label] += 1

        result["frequency"][lb_type] = [
            {"code": s['code'], "name": s['name'],
             "1个月": s['1个月'], "6个月": s['6个月'], "12个月": s['12个月']}
            for s in stats.values()
        ]

    conn.close()
    return jsonify(result)


# ============ 持仓管理 ============

import pandas as pd
import requests as _requests

# 列名别名映射: 不同券商导出格式不一样,做宽容识别
COLUMN_ALIASES = {
    'ts_code': ['证券代码', '股票代码', '代码', 'code', '证券编码', 'stock_code'],
    'name': ['证券名称', '股票名称', '名称', 'name', 'stock_name'],
    'shares': ['证券数量', '持仓数量', '当前持仓', '股份余额', '余额', '数量', '持股数', 'shares', '持仓', '参考持股数', '参考持股', '当前持股'],
    'cost_price': ['成本价', '买入均价', '成本', '摊薄成本价', 'cost', 'cost_price', '均价', '参考成本价'],
    'buy_date': ['买入日期', '建仓日期', '日期', 'date', 'buy_date'],
    'note': ['备注', 'note', 'memo'],
}


def _normalize_code(raw, name=None):
    """把各种格式的代码标准化成 ts_code 格式 (e.g. 600519 -> 600519.SH)

    - 6位数字: 6/9/0/2/3 开头归深市, 其他归沪市
    - 带前缀: sh600519 / sz000001 归一化
    - 已带 .SH/.SZ: 直接返回
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if s.endswith('.SH') or s.endswith('.SZ') or s.endswith('.BJ'):
        return s
    s = s.replace('SH', '').replace('SZ', '').replace('BJ', '')
    s = s.lstrip('shzSZ')
    if not s.isdigit():
        return None
    if len(s) == 6:
        if s.startswith(('6', '9', '5')):
            return f'{s}.SH'
        elif s.startswith(('0', '2', '3')):
            return f'{s}.SZ'
        elif s.startswith(('4', '8')):
            return f'{s}.BJ'
    return s


def _read_uploaded_table(filepath, ext):
    """读取上传文件为 DataFrame, 支持 csv/xlsx/xls"""
    if ext in ('.xlsx', '.xls'):
        engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
        df = pd.read_excel(filepath, engine=engine, dtype=str)
    else:
        # 尝试多种编码
        for enc in ('utf-8', 'gbk', 'gb18030', 'utf-8-sig'):
            try:
                df = pd.read_csv(filepath, encoding=enc, dtype=str)
                break
            except (UnicodeDecodeError, Exception):
                continue
        else:
            raise ValueError('无法识别文件编码, 请使用 UTF-8 或 GBK 编码的 CSV')
    # 清理列名: 去掉 BOM (﻿ / ￾) 和空白
    cleaned = []
    for c in df.columns:
        s = str(c)
        # 各种 BOM 前缀
        for bom in ('﻿', '￾', '﻿'):
            if s.startswith(bom):
                s = s[1:]
        s = s.strip()
        cleaned.append(s)
    df.columns = cleaned
    return df


def _auto_map_columns(df):
    """自动识别 DataFrame 列名, 返回 {canonical_name: actual_column}"""
    # 处理 .1 / .2 这类 pandas 自动加的重复列名后缀 — 只保留第一个
    cols = []
    seen = {}
    for c in df.columns:
        base = c
        if '.' in c and c.split('.')[-1].isdigit():
            base = c.rsplit('.', 1)[0]
        if base in seen:
            continue
        seen[base] = True
        cols.append(c)

    mapping = {}
    unmapped_cols = []
    used = set()
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            # 优先精确匹配, 再尝试 contains
            match = None
            if alias in cols and alias not in used:
                match = alias
            else:
                for c in cols:
                    if c in used:
                        continue
                    if alias and alias in c:
                        match = c
                        break
            if match:
                mapping[canonical] = match
                used.add(match)
                break
    for col in cols:
        if col not in used:
            unmapped_cols.append(col)
    return mapping, unmapped_cols


def _clean_cell(v):
    """清洗单元格: 去 BOM / 全角空白 / 普通空白"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    s = str(v)
    for bom in ('﻿', '￾', '﻿'):
        if s.startswith(bom):
            s = s[1:]
    # 全角空格 -> 半角
    s = s.replace('　', ' ').strip()
    return s


def _parse_positions_from_df(df, column_map):
    """根据列名映射解析出持仓记录列表"""
    records = []
    for _, row in df.iterrows():
        raw_code = _clean_cell(row.get(column_map.get('ts_code')))
        if not raw_code:
            continue
        ts_code = _normalize_code(raw_code)
        if not ts_code:
            continue

        name = _clean_cell(row.get(column_map.get('name', ''), ''))

        try:
            shares = float(_clean_cell(row[column_map['shares']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if shares <= 0:
            continue

        try:
            cost_price = float(_clean_cell(row[column_map['cost_price']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if cost_price <= 0:
            continue

        buy_date = ''
        if 'buy_date' in column_map:
            buy_date = _clean_cell(row[column_map['buy_date']])

        note = ''
        if 'note' in column_map:
            note = _clean_cell(row[column_map['note']])

        records.append({
            'ts_code': ts_code,
            'name': name,
            'shares': shares,
            'cost_price': cost_price,
            'buy_date': buy_date,
            'note': note,
        })
    return records


@app.route('/api/positions/preview', methods=['POST'])
def preview_positions_csv():
    """解析上传文件, 返回识别结果预览 (不入库)"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.csv', '.xlsx', '.xls'):
        return jsonify({'status': 'error', 'message': f'不支持的文件格式: {ext}, 请使用 csv/xlsx/xls'}), 400

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    import uuid
    tmp_id = str(uuid.uuid4())[:8]
    safe_name = f'positions_{tmp_id}{ext}'
    filepath = os.path.join(upload_dir, safe_name)
    f.save(filepath)

    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        try: os.remove(filepath)
        except: pass
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, unmapped = _auto_map_columns(df)
    missing_required = [k for k in ('ts_code', 'shares', 'cost_price') if k not in mapping]

    preview = []
    if not missing_required:
        records = _parse_positions_from_df(df, mapping)
        # 只展示前 20 条
        for r in records[:20]:
            preview.append(r)
    else:
        records = []

    # 保留文件, 用户确认后用同文件再次调用 import 接口
    return jsonify({
        'status': 'success',
        'tmp_id': tmp_id,
        'filename': safe_name,
        'total_rows': len(df),
        'columns': list(df.columns),
        'column_map': mapping,
        'unmapped_columns': unmapped,
        'missing_required': missing_required,
        'preview': preview,
        'parsed_count': len(records),
    })


@app.route('/api/positions/import', methods=['POST'])
def import_positions():
    """根据上传文件 (或 column_map override) 真正入库"""
    data = request.get_json() or {}
    filename = data.get('filename')
    column_map_override = data.get('column_map')  # 可选, 让用户手动指定列

    if not filename:
        return jsonify({'status': 'error', 'message': '缺少 filename'}), 400

    filepath = os.path.join(os.path.dirname(__file__), 'uploads', filename)
    if not os.path.exists(filepath):
        return jsonify({'status': 'error', 'message': '上传文件已失效, 请重新上传'}), 400

    ext = os.path.splitext(filename)[1].lower()
    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, unmapped = _auto_map_columns(df)
    if column_map_override:
        mapping.update(column_map_override)

    missing_required = [k for k in ('ts_code', 'shares', 'cost_price') if k not in mapping]
    if missing_required:
        return jsonify({
            'status': 'error',
            'message': f'缺少必需列: {missing_required}',
            'columns': list(df.columns),
            'column_map': mapping,
        }), 400

    records = _parse_positions_from_df(df, mapping)
    if not records:
        return jsonify({'status': 'error', 'message': '未解析出任何有效记录'}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    inserted = 0
    updated = 0
    skipped = 0
    for r in records:
        try:
            cur = c.execute('''INSERT INTO positions
                (ts_code, name, shares, cost_price, buy_date, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(ts_code, cost_price, buy_date) DO UPDATE SET
                shares=excluded.shares,
                name=COALESCE(NULLIF(excluded.name, ''), positions.name),
                note=COALESCE(NULLIF(excluded.note, ''), positions.note),
                updated_at=datetime('now', 'localtime')''',
                (r['ts_code'], r['name'], r['shares'], r['cost_price'],
                 r['buy_date'], r['note']))
            if cur.rowcount == 1:
                inserted += 1
            elif cur.rowcount > 0:
                updated += 1
        except Exception as e:
            print(f'positions 写入错误 {r["ts_code"]}: {e}')
            skipped += 1
    conn.commit()
    conn.close()

    # 清理上传文件
    try: os.remove(filepath)
    except: pass

    return jsonify({
        'status': 'success',
        'inserted': inserted,
        'updated': updated,
        'skipped': skipped,
        'total': len(records),
    })


@app.route('/api/positions', methods=['GET'])
def get_positions():
    """获取持仓列表 (现持仓: shares>0 且未清仓), 可选 ?with_quote=1 拉实时价并计算盈亏"""
    with_quote = request.args.get('with_quote', '1') == '1'
    include_closed = request.args.get('include_closed', '0') == '1'
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if include_closed:
        rows = c.execute('SELECT * FROM positions ORDER BY created_at DESC').fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM positions WHERE shares > 0 AND closed_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    conn.close()

    positions = [dict(r) for r in rows]

    # 成本市值不依赖行情, 始终先算出来
    for p in positions:
        cost = p.get('cost_price')
        sh = p.get('shares')
        p['cost_value'] = round(cost * sh, 2) if cost and sh else None
        p['market_value'] = None
        p['current_price'] = None
        p['change_pct'] = None
        p['quote_time'] = None
        p['quote_name'] = None
        p['profit'] = None
        p['profit_pct'] = None

    if with_quote and positions:
        codes = list(set(p['ts_code'] for p in positions))
        quote_map = fetch_sina_quotes(codes)
        for p in positions:
            q = quote_map.get(p['ts_code'], {})
            p['current_price'] = q.get('price')
            p['prev_close'] = q.get('prev_close')
            p['change_pct'] = q.get('change_pct')
            p['quote_time'] = q.get('time')
            p['quote_name'] = q.get('name') or p['name']
            cur = p['current_price']
            prev = p['prev_close']
            if p.get('cost_value') is not None and cur is not None:
                p['market_value'] = round(cur * p['shares'], 2)
                p['profit'] = round(p['market_value'] - p['cost_value'], 2)
                p['profit_pct'] = round(p['profit'] / p['cost_value'] * 100, 2) if p['cost_value'] else None
            # 单日盈亏: (现价 - 昨收) × 持仓
            if cur is not None and prev is not None:
                p['daily_profit'] = round((cur - prev) * p['shares'], 2)
            else:
                p['daily_profit'] = None

    total_mv = sum((p.get('market_value') or 0) for p in positions)
    total_cost = sum((p.get('cost_value') or 0) for p in positions)
    total_profit = sum((p.get('profit') or 0) for p in positions)
    total_daily = sum((p.get('daily_profit') or 0) for p in positions)
    summary = {
        'count': len(positions),
        'total_market_value': round(total_mv, 2),
        'total_cost': round(total_cost, 2),
        'total_profit': round(total_profit, 2),
        'total_profit_pct': round(total_profit / total_cost * 100, 2) if total_cost else None,
        'total_daily_profit': round(total_daily, 2),
    }

    return jsonify({'positions': positions, 'summary': summary})


@app.route('/api/positions', methods=['POST'])
def add_position():
    """手动添加一条持仓"""
    data = request.get_json() or {}
    ts_code = _normalize_code(data.get('ts_code'))
    if not ts_code:
        return jsonify({'status': 'error', 'message': '股票代码无效'}), 400
    try:
        shares = float(data.get('shares', 0))
        cost_price = float(data.get('cost_price', 0))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '数量/成本价必须是数字'}), 400
    if shares <= 0 or cost_price <= 0:
        return jsonify({'status': 'error', 'message': '数量/成本价必须大于 0'}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO positions
        (ts_code, name, shares, cost_price, buy_date, note)
        VALUES (?, ?, ?, ?, ?, ?)''',
        (ts_code, (data.get('name') or '').strip(), shares, cost_price,
         (data.get('buy_date') or '').strip(), (data.get('note') or '').strip()))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'id': new_id, 'ts_code': ts_code})


@app.route('/api/positions/<int:pid>', methods=['PUT'])
def update_position(pid):
    """更新单条持仓"""
    data = request.get_json() or {}
    fields = []
    values = []
    for k in ('name', 'shares', 'cost_price', 'buy_date', 'note'):
        if k in data:
            fields.append(f'{k}=?')
            values.append(data[k])
    if not fields:
        return jsonify({'status': 'error', 'message': '无更新字段'}), 400
    fields.append("updated_at=datetime('now', 'localtime')")
    values.append(pid)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f'UPDATE positions SET {", ".join(fields)} WHERE id=?', values)
    if c.rowcount == 0:
        conn.close()
        return jsonify({'status': 'error', 'message': '记录不存在'}), 404
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/positions/<int:pid>', methods=['DELETE'])
def delete_position(pid):
    """删除单条持仓"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM positions WHERE id=?', (pid,))
    if c.rowcount == 0:
        conn.close()
        return jsonify({'status': 'error', 'message': '记录不存在'}), 404
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/positions/clear', methods=['POST'])
def clear_positions():
    """清空所有持仓"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM positions')
    n = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'deleted': n})


@app.route('/api/positions/closed', methods=['GET'])
def get_closed_positions():
    """获取已清仓列表: shares=0 且 closed_at IS NOT NULL
    可选 ?with_quote=1 拉现价(用来看浮盈, 通常清仓后用不上)"""
    with_quote = request.args.get('with_quote', '0') == '1'
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        "SELECT * FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC"
    ).fetchall()
    conn.close()
    closed = []
    for r in rows:
        p = dict(r)
        # 已实现盈亏/卖出额从 DB 字段读
        p['profit'] = p.get('realized_pnl') or 0
        p['sell_amount'] = p.get('total_sell_amount') or 0
        p['original_shares'] = p.get('original_shares') or 0
        p['cost_value'] = round(p['cost_price'] * p['original_shares'], 2)
        if p['cost_value']:
            p['profit_pct'] = round(p['profit'] / p['cost_value'] * 100, 2)
            p['avg_sell_price'] = round(p['sell_amount'] / p['original_shares'], 4) if p['original_shares'] else None
        else:
            p['profit_pct'] = None
            p['avg_sell_price'] = None
        closed.append(p)

    total_profit = sum(p['profit'] for p in closed)
    total_cost = sum(p['cost_value'] for p in closed)
    summary = {
        'count': len(closed),
        'total_realized_pnl': round(total_profit, 2),
        'total_cost': round(total_cost, 2),
        'total_pnl_pct': round(total_profit / total_cost * 100, 2) if total_cost else None,
    }
    return jsonify({'closed': closed, 'summary': summary})


@app.route('/api/positions/restore-closed/<int:pid>', methods=['POST'])
def restore_closed_position(pid):
    """把已清仓的记录恢复成持仓 (shares 用 original_shares 备份的值, 需手动核对)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute('SELECT shares, closed_at, total_sell_amount, realized_pnl FROM positions WHERE id=?', (pid,)).fetchone()
    if not row or not row[1]:
        conn.close()
        return jsonify({'status': 'error', 'message': '记录不是已清仓状态'}), 400
    c.execute('''UPDATE positions SET closed_at=NULL, shares=?, total_sell_amount=0, realized_pnl=0,
        updated_at=datetime('now','localtime') WHERE id=?''', (row[0] or 0, pid))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/positions/backfill-closed', methods=['POST'])
def backfill_closed_positions():
    """一次性回填: 从给定的资金股份 xls + 当日成交 xls 重建已清仓记录
    仅回填当前 DB 中不存在的 (ts_code, cost_price, buy_date) 组合
    """
    data = request.get_json() or {}
    capital_path = data.get('capital_path')
    trade_path = data.get('trade_path')
    if not capital_path or not trade_path:
        return jsonify({'status': 'error', 'message': '需要 capital_path 和 trade_path'}), 400
    if not os.path.exists(capital_path):
        return jsonify({'status': 'error', 'message': f'资金股份文件不存在: {capital_path}'}), 400
    if not os.path.exists(trade_path):
        return jsonify({'status': 'error', 'message': f'成交文件不存在: {trade_path}'}), 400

    cap_ext = os.path.splitext(capital_path)[1].lower()
    trd_ext = os.path.splitext(trade_path)[1].lower()

    try:
        cap_df = _read_uploaded_table(capital_path, cap_ext)
        trd_df = _read_uploaded_table(trade_path, trd_ext)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    cap_map, _ = _auto_map_columns(cap_df)
    trd_map, _ = _auto_map_trade_columns(trd_df)
    if not all(k in cap_map for k in ('ts_code', 'shares', 'cost_price')):
        return jsonify({'status': 'error', 'message': '资金股份缺少必需列'}), 400
    if not all(k in trd_map for k in ('ts_code', 'direction', 'price', 'shares')):
        return jsonify({'status': 'error', 'message': '成交表缺少必需列'}), 400

    cap_records = _parse_positions_from_df(cap_df, cap_map)
    trd_records = _parse_trades_from_df(trd_df, trd_map)

    # 按 ts_code 累计卖出 (用于计算 realized_pnl)
    sold_by_code = {}
    for t in trd_records:
        if t['direction'] == 'sell':
            sold_by_code.setdefault(t['ts_code'], {'shares': 0, 'amount': 0})
            sold_by_code[t['ts_code']]['shares'] += t['shares']
            sold_by_code[t['ts_code']]['amount'] += t['amount'] or (t['price'] * t['shares'])

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    inserted_open = 0
    inserted_closed = 0
    skipped_existing = 0
    today = datetime.now().strftime('%Y-%m-%d')
    for p in cap_records:
        existing = c.execute(
            'SELECT id FROM positions WHERE ts_code=? AND cost_price=? AND buy_date=?',
            (p['ts_code'], p['cost_price'], p['buy_date'])
        ).fetchone()
        if existing:
            skipped_existing += 1
            continue

        sold = sold_by_code.get(p['ts_code'])
        sell_shares = sold['shares'] if sold else 0

        if sell_shares >= p['shares'] - 0.0001:
            # 全部清仓
            avg_sell = (sold['amount'] / sold['shares']) if sold['shares'] > 0 else p['cost_price']
            realized_pnl = round((avg_sell - p['cost_price']) * p['shares'], 2)
            c.execute('''INSERT INTO positions
                (ts_code, name, shares, original_shares, cost_price, buy_date, note,
                 closed_at, total_sell_amount, realized_pnl)
                VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)''',
                (p['ts_code'], p['name'], p['shares'], p['cost_price'], p['buy_date'],
                 f'[回填] 成本{p["cost_price"]}×{p["shares"]}股, 均价{avg_sell:.2f}清仓',
                 today, round(sold['amount'], 2), realized_pnl))
            inserted_closed += 1
        else:
            # 现持仓 (部分减仓也算现持仓, 减仓数量记录在 note)
            remain = p['shares'] - sell_shares
            note = ''
            if sell_shares > 0:
                avg_sell = (sold['amount'] / sold['shares']) if sold['shares'] > 0 else 0
                note = f'[回填] 今日部分减仓 {int(sell_shares)}股 @均价{avg_sell:.2f}, 剩余{remain}股'
            c.execute('''INSERT INTO positions
                (ts_code, name, shares, original_shares, cost_price, buy_date, note,
                 total_sell_amount, realized_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (p['ts_code'], p['name'], remain, p['shares'], p['cost_price'], p['buy_date'],
                 note, round(sold['amount'], 2) if sold else 0,
                 round((sold['amount'] / sold['shares'] - p['cost_price']) * sell_shares, 2) if sold and sold['shares'] > 0 else 0))
            inserted_open += 1

    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'inserted_open': inserted_open,
        'inserted_closed': inserted_closed,
        'skipped_existing': skipped_existing,
    })


# ============ 成交明细管理 (中信「当日成交」导入) ============

TRADE_COLUMN_ALIASES = {
    'ts_code': ['证券代码', '代码', 'code'],
    'name': ['证券名称', '名称', 'name'],
    'direction': ['买卖标志', '买卖方向', '委托方向', '方向', 'direction'],
    'price': ['成交价格', '价格', '成交均价', 'price'],
    'shares': ['成交数量', '数量', '股数', 'shares'],
    'amount': ['成交金额', '发生金额', '金额', 'amount'],
    'trade_time': ['成交时间', '时间', 'time'],
    'trade_date': ['成交日期', '日期', 'date'],
    'trade_no': ['成交编号', '合同号', 'trade_no', 'order_no'],
    'note': ['备注', 'note', 'memo'],
}


def _normalize_direction(raw):
    s = _clean_cell(raw)
    if not s:
        return None
    if any(k in s for k in ('买', 'B', 'b', 'Buy', 'BUY')):
        return 'buy'
    if any(k in s for k in ('卖', 'S', 's', 'Sell', 'SELL')):
        return 'sell'
    return None


def _auto_map_trade_columns(df):
    """成交表的列名识别"""
    cols = list(df.columns)
    mapping = {}
    used = set()
    for canonical, aliases in TRADE_COLUMN_ALIASES.items():
        for alias in aliases:
            match = None
            if alias in cols and alias not in used:
                match = alias
            else:
                for c in cols:
                    if c in used:
                        continue
                    if alias and alias in c:
                        match = c
                        break
            if match:
                mapping[canonical] = match
                used.add(match)
                break
    unmapped = [c for c in cols if c not in used]
    return mapping, unmapped


def _parse_trades_from_df(df, column_map):
    records = []
    for _, row in df.iterrows():
        raw_code = _clean_cell(row.get(column_map.get('ts_code')))
        if not raw_code:
            continue
        ts_code = _normalize_code(raw_code)
        if not ts_code:
            continue

        direction = _normalize_direction(row.get(column_map.get('direction')))
        if not direction:
            continue

        try:
            price = float(_clean_cell(row[column_map['price']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if price <= 0:
            continue

        try:
            shares = float(_clean_cell(row[column_map['shares']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if shares <= 0:
            continue

        try:
            amount = float(_clean_cell(row[column_map['amount']]).replace(',', ''))
        except (KeyError, ValueError, TypeError, KeyError):
            amount = round(price * shares, 2)

        name = _clean_cell(row.get(column_map.get('name', ''), ''))
        trade_time = _clean_cell(row.get(column_map.get('trade_time', ''), ''))
        trade_date = _clean_cell(row.get(column_map.get('trade_date', ''), ''))
        trade_no = _clean_cell(row.get(column_map.get('trade_no', ''), ''))
        note = _clean_cell(row.get(column_map.get('note', ''), ''))

        records.append({
            'trade_no': trade_no,
            'ts_code': ts_code,
            'name': name,
            'direction': direction,
            'price': price,
            'shares': shares,
            'amount': amount,
            'trade_date': trade_date,
            'trade_time': trade_time,
            'note': note,
        })
    return records


@app.route('/api/trades/preview', methods=['POST'])
def preview_trades_file():
    """解析成交表, 返回识别结果 + 去重检查"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.csv', '.xlsx', '.xls'):
        return jsonify({'status': 'error', 'message': f'不支持的文件格式: {ext}'}), 400

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    import uuid
    tmp_id = str(uuid.uuid4())[:8]
    safe_name = f'trades_{tmp_id}{ext}'
    filepath = os.path.join(upload_dir, safe_name)
    f.save(filepath)

    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        try: os.remove(filepath)
        except: pass
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, unmapped = _auto_map_trade_columns(df)
    missing = [k for k in ('ts_code', 'direction', 'price', 'shares') if k not in mapping]

    records = []
    if not missing:
        records = _parse_trades_from_df(df, mapping)

    # 查重: 列出本次解析中已存在的 trade_no
    conn = sqlite3.connect(DB_PATH)
    existing_nos = set()
    if records:
        trade_nos = [r['trade_no'] for r in records if r['trade_no']]
        if trade_nos:
            q = ','.join('?' * len(trade_nos))
            existing_nos = set(r[0] for r in conn.execute(
                f'SELECT trade_no FROM trades WHERE trade_no IN ({q})', trade_nos).fetchall())
    conn.close()

    duplicate_count = sum(1 for r in records if r['trade_no'] in existing_nos)
    new_count = len(records) - duplicate_count
    buy_count = sum(1 for r in records if r['direction'] == 'buy')
    sell_count = sum(1 for r in records if r['direction'] == 'sell')

    return jsonify({
        'status': 'success',
        'filename': safe_name,
        'total_rows': len(df),
        'columns': list(df.columns),
        'column_map': mapping,
        'unmapped_columns': unmapped,
        'missing_required': missing,
        'parsed_count': len(records),
        'new_count': new_count,
        'duplicate_count': duplicate_count,
        'buy_count': buy_count,
        'sell_count': sell_count,
        'preview': records[:20],
    })


@app.route('/api/trades/import', methods=['POST'])
def import_trades():
    data = request.get_json() or {}
    filename = data.get('filename')
    column_map_override = data.get('column_map')
    if not filename:
        return jsonify({'status': 'error', 'message': '缺少 filename'}), 400
    filepath = os.path.join(os.path.dirname(__file__), 'uploads', filename)
    if not os.path.exists(filepath):
        return jsonify({'status': 'error', 'message': '文件已失效, 请重新上传'}), 400

    ext = os.path.splitext(filename)[1].lower()
    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, _ = _auto_map_trade_columns(df)
    if column_map_override:
        mapping.update(column_map_override)

    missing = [k for k in ('ts_code', 'direction', 'price', 'shares') if k not in mapping]
    if missing:
        return jsonify({'status': 'error', 'message': f'缺少必需列: {missing}'}), 400

    records = _parse_trades_from_df(df, mapping)
    if not records:
        return jsonify({'status': 'error', 'message': '未解析出任何有效成交'}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    inserted = 0
    duplicate = 0
    skipped = 0
    for r in records:
        try:
            cur = c.execute('''INSERT INTO trades
                (trade_no, ts_code, name, direction, price, shares, amount, trade_date, trade_time, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (r['trade_no'], r['ts_code'], r['name'], r['direction'],
                 r['price'], r['shares'], r['amount'], r['trade_date'],
                 r['trade_time'], r['note']))
            if cur.rowcount == 1:
                inserted += 1
        except sqlite3.IntegrityError:
            duplicate += 1
        except Exception as e:
            print(f'trades 写入错误: {e}')
            skipped += 1
    conn.commit()
    conn.close()

    try: os.remove(filepath)
    except: pass

    return jsonify({
        'status': 'success',
        'inserted': inserted,
        'duplicate': duplicate,
        'skipped': skipped,
        'total': len(records),
    })


@app.route('/api/trades', methods=['GET'])
def list_trades():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM trades ORDER BY trade_date DESC, trade_time DESC, id DESC').fetchall()
    conn.close()
    return jsonify({
        'trades': [dict(r) for r in rows],
        'count': len(rows),
    })


def _apply_one_trade(c, trade):
    """把单条成交应用到 positions, 返回 (status, msg)
    status: 'ok' | 'oversell' | 'missing' """
    if trade['direction'] == 'buy':
        # 找 ts_code + cost_price 一致的仓位
        row = c.execute(
            'SELECT id, shares, note FROM positions WHERE ts_code=? AND cost_price=? ORDER BY id LIMIT 1',
            (trade['ts_code'], trade['price'])).fetchone()
        ts = trade.get('trade_date') or trade.get('trade_time') or ''
        new_note = f"[{ts} 买入 {trade['shares']}股 @{trade['price']} #{trade['trade_no']}]"
        if row:
            c.execute('UPDATE positions SET shares=shares+?, note=COALESCE(note,\"\")||?, updated_at=datetime(\'now\', \'localtime\') WHERE id=?',
                      (trade['shares'], new_note, row[0]))
        else:
            c.execute('''INSERT INTO positions
                (ts_code, name, shares, cost_price, buy_date, note)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (trade['ts_code'], trade['name'], trade['shares'], trade['price'],
                 trade['trade_date'], new_note))
        return 'ok', None

    elif trade['direction'] == 'sell':
        # FIFO: 找 ts_code 相同, 按 created_at ASC 依次扣
        positions = c.execute(
            'SELECT id, shares, cost_price, note, total_sell_amount, realized_pnl FROM positions WHERE ts_code=? AND shares>0 ORDER BY datetime(created_at) ASC, id ASC',
            (trade['ts_code'],)).fetchall()
        if not positions:
            return 'missing', f'无可扣减仓位: {trade["ts_code"]} {trade["name"]}'

        remaining = trade['shares']
        sell_price = trade['price']
        ts = trade.get('trade_date') or trade.get('trade_time') or ''
        sell_note = f"[{ts} 卖出 {trade['shares']}股 @{trade['price']} #{trade['trade_no']}]"
        for pid, pshares, pcost, pnote, prev_sell_amt, prev_pnl in positions:
            if remaining <= 0:
                break
            take = min(remaining, pshares)
            new_shares = pshares - take
            new_note = (pnote or '') + sell_note
            # 累计卖出额 + 已实现盈亏
            new_sell_amt = (prev_sell_amt or 0) + sell_price * take
            new_pnl = (prev_pnl or 0) + (sell_price - pcost) * take
            if new_shares <= 0.0001:
                # 软删除: shares=0 + closed_at
                c.execute('''UPDATE positions SET shares=0, note=?, closed_at=datetime('now','localtime'),
                    total_sell_amount=?, realized_pnl=?, updated_at=datetime('now','localtime') WHERE id=?''',
                    (new_note, new_sell_amt, new_pnl, pid))
            else:
                c.execute('''UPDATE positions SET shares=?, note=?, total_sell_amount=?,
                    realized_pnl=?, updated_at=datetime('now','localtime') WHERE id=?''',
                    (new_shares, new_note, new_sell_amt, new_pnl, pid))
            remaining -= take
        if remaining > 0.0001:
            return 'oversell', f'{trade["ts_code"]} 卖超 {remaining}股, 仓位不足'
        return 'ok', None
    return 'unknown', '未知方向'


@app.route('/api/trades/apply', methods=['POST'])
def apply_trades():
    """把已入库未 applied 的成交, 按顺序应用到 positions"""
    data = request.get_json() or {}
    trade_ids = data.get('trade_ids')  # 可选: 只 apply 指定 ID
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if trade_ids:
        placeholders = ','.join('?' * len(trade_ids))
        rows = c.execute(
            f'SELECT * FROM trades WHERE id IN ({placeholders}) AND applied=0 ORDER BY datetime(trade_date), datetime(trade_time), id',
            trade_ids).fetchall()
    else:
        rows = c.execute(
            'SELECT * FROM trades WHERE applied=0 ORDER BY datetime(COALESCE(NULLIF(trade_date, \'\'), \'9999-12-31\')), datetime(trade_time), id').fetchall()

    cols = [d[0] for d in c.description] if c.description else []
    trades = [dict(zip(cols, r)) for r in rows]

    applied = []
    errors = []
    for t in trades:
        status, msg = _apply_one_trade(c, t)
        if status == 'ok':
            c.execute('UPDATE trades SET applied=1 WHERE id=?', (t['id'],))
            applied.append({'id': t['id'], 'ts_code': t['ts_code'], 'name': t['name'],
                            'direction': t['direction'], 'shares': t['shares'], 'price': t['price']})
        else:
            errors.append({'id': t['id'], 'ts_code': t['ts_code'], 'name': t['name'],
                           'status': status, 'message': msg})

    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success' if not errors else 'partial',
        'applied': applied,
        'errors': errors,
        'applied_count': len(applied),
        'error_count': len(errors),
    })


@app.route('/api/trades/rollback', methods=['POST'])
def rollback_trades():
    """撤销 apply: 把指定 trades 的 applied 改回 0 (position 状态不会自动还原!)"""
    data = request.get_json() or {}
    trade_ids = data.get('trade_ids') or []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if trade_ids:
        placeholders = ','.join('?' * len(trade_ids))
        c.execute(f'UPDATE trades SET applied=0 WHERE id IN ({placeholders})', trade_ids)
    else:
        c.execute('UPDATE trades SET applied=0 WHERE applied=1')
    n = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'rolled_back': n,
                    'warning': '持仓数据未自动还原, 如需修正请手动调整 positions 表'})


@app.route('/api/trades/clear', methods=['POST'])
def clear_trades():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM trades')
    n = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'deleted': n})


# ============ 新浪实时行情转发 ============

def _sina_code_for_ts(ts_code):
    """600519.SH -> sh600519, 000001.SZ -> sz000001"""
    if not ts_code or '.' not in ts_code:
        return None
    code, market = ts_code.lower().split('.')
    prefix = {'sh': 'sh', 'sz': 'sz', 'bj': 'bj'}.get(market, '')
    return f'{prefix}{code}'


def _ts_code_for_sina(sina_code):
    """sh600519 -> 600519.SH"""
    if not sina_code:
        return None
    s = str(sina_code).lower()
    if s.startswith('sh'):
        return f'{s[2:]}.SH'
    if s.startswith('sz'):
        return f'{s[2:]}.SZ'
    if s.startswith('bj'):
        return f'{s[2:]}.BJ'
    return None


def fetch_sina_quotes(ts_codes):
    """通过新浪财经 hq.sinajs.cn 拉一批代码的实时行情

    返回 {ts_code: {price, prev_close, change_pct, name, time}}
    """
    sina_codes = []
    code_map = {}
    for tc in ts_codes:
        sc = _sina_code_for_ts(tc)
        if sc:
            sina_codes.append(sc)
            code_map[sc] = tc
    if not sina_codes:
        return {}

    url = f'https://hq.sinajs.cn/list={",".join(sina_codes)}'
    headers = {
        'Referer': 'https://finance.sina.com.cn',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    }
    result = {}
    try:
        r = _requests.get(url, headers=headers, timeout=5)
        if r.status_code != 200:
            print(f'sina 返回 {r.status_code}')
            return result
        for line in r.text.strip().split('\n'):
            # 格式: var hq_str_sh600519="贵州茅台,1800.00,1780.00,1810.00,1820.00,1790.00,1805.00,...";
            if '=' not in line or '"' not in line:
                continue
            var_part, val_part = line.split('=', 1)
            sina_code = var_part.strip().replace('var hq_str_', '')
            val = val_part.strip().strip(';').strip('"')
            if not val or val == 'NULL':
                continue
            fields = val.split(',')
            if len(fields) < 10:
                continue
            # 字段含义: 0=名称 1=今开 2=昨收 3=当前 4=日高 5=日低 ...
            name = fields[0]
            try:
                current = float(fields[3])
                prev_close = float(fields[2])
            except (ValueError, IndexError):
                continue
            if prev_close <= 0:
                continue
            change_pct = (current - prev_close) / prev_close * 100
            # 时间: index 30 起始 HHMMSS (不同市场略有差异)
            quote_time = fields[30] if len(fields) > 30 else ''
            ts_code = _ts_code_for_sina(sina_code)
            if ts_code:
                result[ts_code] = {
                    'price': current,
                    'prev_close': prev_close,
                    'change_pct': round(change_pct, 2),
                    'name': name,
                    'time': quote_time,
                }
    except Exception as e:
        print(f'fetch_sina_quotes 错误: {e}')
    return result


@app.route('/api/positions/quote', methods=['GET'])
def get_positions_quote():
    """独立 quote 端点, 给前端手动刷新 / 调试用"""
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if c.strip()]
    if not codes:
        return jsonify({})
    return jsonify(fetch_sina_quotes(codes))


if __name__ == '__main__':
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")
    print("启动 Flask 服务...")
    app.run(debug=True, host='0.0.0.0', port=5555)