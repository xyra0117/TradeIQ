"""
Flask + SQLite + TuShare 后端
数据存储在本地 SQLite，定时从 TuShare 拉取数据
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
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


if __name__ == '__main__':
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")
    print("启动 Flask 服务...")
    app.run(debug=True, host='0.0.0.0', port=5555)