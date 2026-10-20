#!/usr/bin/env python3
"""补 stock_daily 的交易日缺口, 同时补对应日期的 adj_factor.

用法:
  python3 backfill_gap.py                      # 补所有缺失交易日
  python3 backfill_gap.py 20201224 20250331    # 指定区间
  python3 backfill_gap.py --list               # 只列缺失日期, 不拉数据

说明:
  - 已有日期自动跳过, 可重复运行 (stock_daily 有 UNIQUE(ts_code, trade_date))
  - 交易日历取自 TuShare trade_cal, 不用自然日猜 (A 股有调休)
  - pro.daily() 覆盖沪深北三个市场, 无需单独处理 BJ
  - stock_daily 的 volume 列存的是成交量(手), amount 存成交额(千元), 与 app.py 同步逻辑一致
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


def trading_calendar(pro, start, end):
    df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end,
                       is_open='1', fields='cal_date')
    return sorted(df.cal_date.tolist())


def missing_dates(conn, cal):
    have = {r[0] for r in conn.execute('SELECT DISTINCT trade_date FROM stock_daily')}
    return [d for d in cal if d not in have]


def fetch_with_retry(fn, label, retries=3):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt == retries - 1:
                print(f'[fail] {label} 重试 {retries} 次仍失败: {e}', flush=True)
                return None
            time.sleep(2 * (attempt + 1))


def backfill_day(conn, pro, date, local_codes, name_map):
    df = fetch_with_retry(lambda: pro.daily(trade_date=date), f'daily {date}')
    if df is None or len(df) == 0:
        return 0, 0, 'no_data'

    price_rows = []
    for r in df.itertuples():
        name = name_map.get(r.ts_code, '')
        if r.ts_code not in local_codes:
            continue
        price_rows.append((r.ts_code, name, date, r.open, r.high, r.low, r.close,
                           r.pct_chg, r.vol, r.amount))
    if price_rows:
        conn.executemany('''
            INSERT OR IGNORE INTO stock_daily
            (ts_code, name, trade_date, open, high, low, close, change, volume, amount)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', price_rows)

    fa = fetch_with_retry(lambda: pro.adj_factor(trade_date=date), f'adj_factor {date}')
    factor_rows = []
    if fa is not None:
        factor_rows = [(r.ts_code, r.trade_date, r.adj_factor) for r in fa.itertuples()
                       if r.ts_code in local_codes and r.adj_factor]
        if factor_rows:
            conn.executemany('''
                INSERT OR REPLACE INTO adj_factor(ts_code, trade_date, adj_factor)
                VALUES (?,?,?)
            ''', factor_rows)

    conn.commit()
    return len(price_rows), len(factor_rows), 'ok'


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    list_only = '--list' in sys.argv

    start = args[0] if len(args) > 0 else '20200101'
    end = args[1] if len(args) > 1 else '20991231'

    conn = sqlite3.connect(DB, timeout=120)
    pro = connect_pro()

    cal = trading_calendar(pro, start, end)
    missing = missing_dates(conn, cal)
    print(f'=== backfill_gap: {start} ~ {end} ===', flush=True)
    print(f'区间内交易日 {len(cal)} 天, 本库缺失 {len(missing)} 天', flush=True)

    if list_only:
        for d in missing:
            print(' ', d)
        conn.close()
        return

    if not missing:
        print('无缺口, 无需补', flush=True)
        conn.close()
        return

    local_codes = {r[0] for r in conn.execute('SELECT ts_code FROM stock_basic')}
    name_map = {r[0]: r[1] for r in conn.execute(
        'SELECT ts_code, name FROM stock_basic WHERE name IS NOT NULL')}
    print(f'本地 stock_basic {len(local_codes)} 只 (其中 {len(name_map)} 只有名称), '
          f'开始补 (限速 {GAP_SEC}s/请求)', flush=True)

    total_p = total_f = 0
    no_data = []
    failed = []
    t0 = time.time()

    for i, d in enumerate(missing, 1):
        np_, nf, status = backfill_day(conn, pro, d, local_codes, name_map)
        if status == 'no_data':
            no_data.append(d)
            continue
        total_p += np_
        total_f += nf

        if i % 20 == 0:
            el = time.time() - t0
            rate = i / max(el, 1)
            print(f'[{i}/{len(missing)}] {d} 累计日线 {total_p} 行 / 因子 {total_f} 行, '
                  f'{rate*60:.1f} 天/min, 预计剩余 {(len(missing)-i)/max(rate,1)/60:.1f} 分钟',
                  flush=True)
        time.sleep(GAP_SEC)

    print(f'\n=== 完成 ===', flush=True)
    print(f'日线 {total_p} 行, 因子 {total_f} 行', flush=True)
    print(f'无数据(非交易日/接口空) {len(no_data)} 天: {no_data[:10]}', flush=True)
    print(f'总耗时 {(time.time()-t0)/60:.1f} 分钟', flush=True)
    conn.close()


if __name__ == '__main__':
    main()
