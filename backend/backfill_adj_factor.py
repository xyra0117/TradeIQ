#!/usr/bin/env python3
"""从 TuShare pro.adj_factor 回填复权因子到 adj_factor 表。

用法:
  python3 backfill_adj_factor.py                      # 全量
  python3 backfill_adj_factor.py 20260101 20261019    # 指定区间
  python3 backfill_adj_factor.py --skip-existing      # 已有数据的交易日跳过
  python3 backfill_adj_factor.py --probe 20261016     # 只探测单日覆盖情况

设计说明:
  - adj_factor 是客观事实(不含任何人为选的口径), 一次写入不再变化
  - 前复权价不落库, 查询时现算: qfq_close = close * f_t / f_latest
    这样原始价永远保留, 每年新分红只需重算不需重写
  - TuShare 免费接口有每分钟调用上限, 固定间隔限速
"""
import os
import sqlite3
import sys
import time

import tushare as ts

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
TOKEN = os.environ.get('TUSHARE_API_KEY')
GAP_SEC = float(os.environ.get('GAP_SEC', '0.35'))


def connect_pro():
    if not TOKEN:
        raise SystemExit('缺少 TUSHARE_API_KEY 环境变量')
    ts.set_token(TOKEN)
    return ts.pro_api()


def ensure_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS adj_factor (
            ts_code     TEXT NOT NULL,
            trade_date  TEXT NOT NULL,
            adj_factor  REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_adj_factor_date ON adj_factor(trade_date)')
    conn.commit()
    print('[schema] adj_factor 表就绪', flush=True)


def trading_dates(conn, start, end):
    return [r[0] for r in conn.execute('''
        SELECT DISTINCT trade_date FROM stock_daily
        WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
    ''', (start, end)).fetchall()]


def probe(pro, date, local_codes):
    df = pro.adj_factor(trade_date=date)
    got = set(df.ts_code)
    print(f'[probe] {date} 返回 {len(got)} 只, '
          f'本地覆盖 {len(got & local_codes)}/{len(local_codes)}', flush=True)
    return df


def backfill(pro, conn, dates, skip_existing=False):
    local_codes = {r[0] for r in conn.execute('SELECT ts_code FROM stock_basic')}
    total = 0
    failed = []
    t0 = time.time()

    for i, d in enumerate(dates, 1):
        if skip_existing and conn.execute(
                'SELECT 1 FROM adj_factor WHERE trade_date=? LIMIT 1', (d,)).fetchone():
            continue
        for attempt in range(3):
            try:
                df = pro.adj_factor(trade_date=d)
                break
            except Exception as e:
                if attempt == 2:
                    print(f'[fail] {d} 重试 3 次仍失败: {e}', flush=True)
                    failed.append(d)
                    df = None
                time.sleep(2 * (attempt + 1))
        if df is None:
            continue

        rows = [(r.ts_code, r.trade_date, r.adj_factor) for r in df.itertuples()
                if r.ts_code in local_codes and r.adj_factor]
        if rows:
            conn.executemany(
                'INSERT OR REPLACE INTO adj_factor(ts_code, trade_date, adj_factor) VALUES (?,?,?)',
                rows)
            conn.commit()
            total += len(rows)

        if i % 20 == 0:
            rate = total / max(time.time() - t0, 1)
            eta = (len(dates) - i) * len(rows or [1]) / max(rate, 1)
            print(f'[{i}/{len(dates)}] {d} 累计 {total} 行, {rate:.0f} 行/s, '
                  f'预计剩余 {eta/60:.1f} 分钟', flush=True)
        time.sleep(GAP_SEC)

    print(f'\n=== 完成: {total} 行, 失败 {len(failed)} 天 ===', flush=True)
    if failed:
        print('失败日期:', failed[:20], flush=True)
    return failed


def main():
    args = sys.argv[1:]
    skip = '--skip-existing' in args
    probe_only = '--probe' in args
    pos = [a for a in args if not a.startswith('--')]

    conn = sqlite3.connect(DB, timeout=60)
    ensure_table(conn)

    if probe_only:
        d = pos[0] if pos else '20261016'
        local = {r[0] for r in conn.execute('SELECT ts_code FROM stock_basic')}
        probe(connect_pro(), d, local)
        return

    start = pos[0] if len(pos) > 0 else '20200101'
    end = pos[1] if len(pos) > 1 else '20991231'
    dates = trading_dates(conn, start, end)
    print(f'=== backfill_adj_factor: {start} ~ {end}, {len(dates)} 个交易日 ===', flush=True)

    backfill(connect_pro(), conn, dates, skip_existing=skip)
    conn.close()


if __name__ == '__main__':
    main()
