#!/usr/bin/env python3
"""从指定日期范围回填 stock_ma 表。

用法:
  python3 backfill_ma.py                    # 默认 20250506 ~ 今天
  python3 backfill_ma.py 20250506 20260611
"""
import os
import sqlite3
import sys
import time

DB = os.path.join(os.path.dirname(__file__), 'market_data.db')


def backfill_ma(start_date='20250506', end_date='20260611'):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    dates = [r[0] for r in cur.execute('''
        SELECT DISTINCT trade_date FROM stock_daily
        WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
    ''', (start_date, end_date)).fetchall()]
    print(f'=== backfill_ma: {start_date} ~ {end_date}, {len(dates)} 个交易日 ===', flush=True)

    all_codes = [r[0] for r in cur.execute(
        'SELECT DISTINCT ts_code FROM stock_daily WHERE close IS NOT NULL').fetchall()]
    print(f'全市场 {len(all_codes)} 只股票', flush=True)

    total_writes = 0
    t0 = time.time()
    for i, date in enumerate(dates):
        day_writes = 0
        for code in all_codes:
            # 拉 date 当天及之前 最近 30 天 close (按时间正序: 旧→新)
            rows = cur.execute('''
                SELECT close FROM stock_daily
                WHERE ts_code=? AND close IS NOT NULL AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT 30
            ''', (code, date)).fetchall()
            if not rows:
                continue
            closes = list(reversed([r[0] for r in rows]))
            n = len(closes)
            ma5  = sum(closes[-5:])  / 5  if n >= 5  else None
            ma10 = sum(closes[-10:]) / 10 if n >= 10 else None
            ma20 = sum(closes[-20:]) / 20 if n >= 20 else None
            ma30 = sum(closes[-30:]) / 30 if n >= 30 else None
            cur.execute('''INSERT OR REPLACE INTO stock_ma
                (ts_code, trade_date, ma5, ma10, ma20, ma30, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))''',
                (code, date, ma5, ma10, ma20, ma30))
            day_writes += 1
            total_writes += 1
        c.commit()
        elapsed = time.time() - t0
        rate = (i + 1) / elapsed if elapsed > 0 else 0
        eta_min = (len(dates) - i - 1) / rate / 60 if rate > 0 else 0
        if (i + 1) % 5 == 0 or i == 0 or i == len(dates) - 1:
            print(f'  [{i+1}/{len(dates)}] {date} 今日 {day_writes} 累计 {total_writes} | {elapsed:.0f}s 已耗, 速率 {rate:.2f} 天/s, 预计还需 {eta_min:.0f} 分', flush=True)
    c.close()
    print(f'\n完成: 累计 {total_writes} 行 MA 写入, 总耗时 {(time.time()-t0)/60:.1f} 分', flush=True)


if __name__ == '__main__':
    start = sys.argv[1] if len(sys.argv) > 1 else '20250506'
    end = sys.argv[2] if len(sys.argv) > 2 else '20260611'
    backfill_ma(start, end)
