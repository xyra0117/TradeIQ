"""复权因子当日同步: TuShare pro.adj_factor → adj_factor 表.

设计 (跟 backfill_adj_factor.py 全量回填同口径):
  - 复权因子是客观事实 (不含任何人为选的口径), 一天拉一次, 重复拉用 INSERT OR REPLACE 覆盖
  - 前复权价不落库, 查询时现算: qfq_close = close * f_t / f_latest, 原始价永远保留
  - 只入库本地 stock_basic 里有的股票, 跟全量回填脚本一致
  - TuShare 免费接口有每分钟调用上限, 同一秒内多天要加间隔
"""
import os
import sqlite3
import sys
import time

import tushare as ts

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
RETRY = 3
GAP_SEC = float(os.environ.get('ADJ_FACTOR_GAP_SEC', '0.4'))


def resolve_token(token=None):
    """拿 TuShare token: 显式传入 > 环境变量 > 已加载的 app 模块.

    调度线程里没有 Flask app context, 也不能 import app (会重复加载整个模块),
    所以从 sys.modules 里取 —— app.py 跑成 __main__ 时就挂在 __main__ 上."""
    if token:
        return token
    env = os.environ.get('TUSHARE_API_KEY') or os.environ.get('TUSHARE_TOKEN')
    if env:
        return env
    for name in ('app', '__main__'):
        module = sys.modules.get(name)
        found = getattr(module, 'TUSHARE_TOKEN', None)
        if found:
            return found
    return None


def _valid_factor(value):
    """复权因子必须是正数; NaN / None / 0 都当没有."""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return None
    if factor != factor or factor <= 0:   # NaN 自比较不等
        return None
    return factor


def _pro(token=None):
    token = resolve_token(token)
    if token:
        ts.set_token(token)
    return ts.pro_api()


def fetch_one_date(date_str, token=None, db_path=None, pro=None):
    """拉某个交易日的复权因子入库, 返回 {date, rows, skipped, reason}.

    非交易日 / TuShare 当天还没入库 → skipped, 不算失败 (盘后 17:00 前很常见)."""
    if not date_str or len(date_str) != 8 or not str(date_str).isdigit():
        return {'date': date_str, 'rows': 0, 'skipped': True, 'reason': '日期格式应为 YYYYMMDD'}
    path = db_path or DB
    client = pro or _pro(token)
    df = None
    for attempt in range(RETRY):
        try:
            df = client.adj_factor(trade_date=date_str)
            break
        except Exception as error:
            if attempt == RETRY - 1:
                return {'date': date_str, 'rows': 0, 'skipped': True,
                        'reason': f'TuShare 拉取失败 (重试 {RETRY} 次): {error}'}
            time.sleep(2 * (attempt + 1))
    if df is None or len(df) == 0:
        return {'date': date_str, 'rows': 0, 'skipped': True,
                'reason': '当日没有复权因子数据 (非交易日, 或 TuShare 还没入库)'}
    conn = sqlite3.connect(path, timeout=30)
    try:
        local = {r[0] for r in conn.execute('SELECT ts_code FROM stock_basic')}
        rows = []
        for item in df.itertuples():
            if item.ts_code not in local:
                continue
            factor = _valid_factor(item.adj_factor)
            if factor is None:
                continue
            rows.append((item.ts_code, item.trade_date, factor))
        conn.executemany(
            'INSERT OR REPLACE INTO adj_factor(ts_code, trade_date, adj_factor) VALUES (?,?,?)', rows)
        conn.commit()
    finally:
        conn.close()
    return {'date': date_str, 'rows': len(rows), 'skipped': False, 'reason': ''}


def recent_trading_days(db_path=None, days=6):
    """最近 N 个交易日 (含今天, 按本地交易日历)."""
    path = db_path or DB
    conn = sqlite3.connect(path, timeout=10)
    try:
        rows = conn.execute(
            'SELECT cal_date FROM trading_dates_cache WHERE cal_date<=? ORDER BY cal_date DESC LIMIT ?',
            (_today(), days)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def missing_days(db_path=None, days=6):
    """最近 N 个交易日里, adj_factor 一行都没有的那些天 (补数据的对象)."""
    path = db_path or DB
    conn = sqlite3.connect(path, timeout=10)
    try:
        result = []
        for day in recent_trading_days(path, days):
            count = conn.execute('SELECT COUNT(*) FROM adj_factor WHERE trade_date=?', (day,)).fetchone()[0]
            if not count:
                result.append(day)
    finally:
        conn.close()
    return result


def sync_recent(days=6, token=None, db_path=None, pro=None):
    """补最近 N 个交易日的复权因子 (已有数据的跳过). 供盘后定时任务用."""
    path = db_path or DB
    targets = missing_days(path, days)
    if not targets:
        return {'targets': [], 'results': [], 'rows': 0}
    client = pro or _pro(token)
    results = []
    for index, day in enumerate(targets):
        results.append(fetch_one_date(day, db_path=path, pro=client))
        if index < len(targets) - 1:
            time.sleep(GAP_SEC)
    return {'targets': targets, 'results': results,
            'rows': sum(r['rows'] for r in results)}


def _today():
    from datetime import datetime
    return datetime.now().strftime('%Y%m%d')
