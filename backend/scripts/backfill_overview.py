"""回填 overview + index_daily 缺失的历史盘面数据（近两年）。

用法:
    python scripts/backfill_overview.py [起始日期 YYYYMMDD]

默认起始日期 = 今天往前推两年。
只填缺失的日期，已有数据不动。
"""
import os
import sys
import time
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tushare as ts

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'market_data.db')
TUSHARE_TOKEN = '1905eff139f559e9caa61c7363ac18e6217a33032e9e434812db1e34'

INDICES = {
    '上证指数': '000001.SH',
    '深证成指': '399001.SZ',
    '创业板指': '399006.SZ',
    '科创50': '000688.SH',
}

SLEEP = 0.4  # TuShare 限流保护


def main():
    start_date = sys.argv[1] if len(sys.argv) > 1 else (
        datetime.now() - timedelta(days=730)).strftime('%Y%m%d')
    end_date = datetime.now().strftime('%Y%m%d')

    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 交易日历（库里的缓存）
    cal = [r[0] for r in c.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date >= ? AND cal_date <= ? ORDER BY cal_date',
        (start_date, end_date)).fetchall()]
    if not cal:
        print('trading_dates_cache 没有该范围的交易日，先同步交易日历')
        return

    existing = {r[0] for r in c.execute(
        'SELECT date FROM overview WHERE date >= ?', (start_date,)).fetchall()}
    missing = [d for d in cal if d not in existing]
    print(f'交易日 {len(cal)} 天，已有 {len(existing)} 天，缺 {len(missing)} 天')

    # ---- 1. 指数日线：按区间一次拉全，只插缺失 ----
    idx_existing = {r[0] for r in c.execute(
        'SELECT DISTINCT date FROM index_daily WHERE date >= ?', (start_date,)).fetchall()}
    idx_missing = [d for d in cal if d not in idx_existing]
    print(f'指数日线缺 {len(idx_missing)} 天，开始回填...')
    idx_inserted = 0
    for name, code in INDICES.items():
        try:
            df = pro.index_daily(ts_code=code, start_date=start_date, end_date=end_date)
            for _, row in df.iterrows():
                if row['trade_date'] in idx_existing:
                    continue
                c.execute('''INSERT OR REPLACE INTO index_daily
                    (date, name, open, high, low, close, change, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (row['trade_date'], name, row['open'], row['high'], row['low'],
                     row['close'], row['pct_chg'], row['amount'] / 100000))
                idx_inserted += 1
            conn.commit()
            time.sleep(SLEEP)
        except Exception as e:
            print(f'  指数 {name} 失败: {e}')
    print(f'指数日线补了 {idx_inserted} 条')

    # ---- 2. 盘面概览：逐日回填 ----
    for i, date in enumerate(missing):
        try:
            df = pro.daily(trade_date=date)
            if len(df) == 0:
                print(f'  {date} 无数据，跳过')
                continue

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
            down7 = len(df[(df['pct_chg'] < -7)])
            total_volume = df['amount'].sum() / 100000

            # 前一交易日成交额：直接查库里 overview（按日期升序回填，前一天已存在）
            prev = c.execute(
                'SELECT totalVolume FROM overview WHERE date < ? ORDER BY date DESC LIMIT 1',
                (date,)).fetchone()
            volume_diff = total_volume - prev[0] if prev else 0

            # 全市场资金净流入
            net_flow = 0
            try:
                df_mf = pro.moneyflow(trade_date=date)
                if len(df_mf) > 0:
                    net_flow = df_mf['net_mf_amount'].sum() / 10000
            except Exception as e:
                print(f'  {date} moneyflow 失败: {e}')

            c.execute('''INSERT OR REPLACE INTO overview
                (date, totalVolume, volumeDiff, netFlow, upCount, downCount,
                 flatCount, limitUp, limitDown, up7, up5_7, up3_5, up0_3,
                 down7, down5_7, down3_5, down0_3)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (date, total_volume, volume_diff, net_flow, up_count, down_count,
                 flat_count, limit_up, limit_down, up7, up5_7, up3_5, up0_3,
                 down7, down5_7, down3_5, down0_3))

            if (i + 1) % 20 == 0:
                conn.commit()
                print(f'  进度 {i + 1}/{len(missing)} ({date})')
            time.sleep(SLEEP)
        except Exception as e:
            print(f'  {date} 失败: {e}')
            time.sleep(2)

    conn.commit()
    conn.close()
    print('完成')


if __name__ == '__main__':
    main()
