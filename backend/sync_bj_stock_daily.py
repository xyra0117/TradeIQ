"""一次性脚本: 用 akshare 拉北交所 stock_daily 06-16~09-30 缺失数据.

北交所 (.BJ) 不在 TuShare pro.daily() 返回范围内, 用 akshare.stock_zh_a_hist 补.
"""
import os
import sys
import time
import warnings
warnings.filterwarnings('ignore')

# 必须在 import requests 之前清代理
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        os.environ[k] = ''

import requests
_orig_init = requests.Session.__init__
def _new_init(self, *a, **kw):
    _orig_init(self, *a, **kw)
    self.proxies = {'http': None, 'https': None, 'all': None}
    self.trust_env = False
requests.Session.__init__ = _new_init

import akshare as ak
import sqlite3

DB_PATH = '/Users/xyra/Desktop/claude code/TradeIQ/backend/market_data.db'
START = '20260616'
END = '20260930'
SLEEP_PER_STOCK = 0.3  # 节流, 避免被东财盯上


def already_loaded(conn, ts_code):
    cur = conn.execute(
        'SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM stock_daily WHERE ts_code=?',
        (ts_code,),
    )
    mn, mx, n = cur.fetchone()
    return mn, mx, n


def main():
    conn = sqlite3.connect(DB_PATH)
    # 1) 拿 stock_daily 里 06-16~09-30 的北交所代码清单
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT ts_code FROM stock_daily WHERE ts_code LIKE '%.BJ' "
        "AND trade_date BETWEEN ? AND ?", (START, END),
    ).fetchall()]
    print(f'北交所标的 (stock_daily 06-16~09-30): {len(codes)} 只')

    # 2) 实际需要拉的: stock_daily 04-09~09-30 全期间存在, 但 06-16~09-30 缺失的
    all_bj = [r[0] for r in conn.execute(
        "SELECT DISTINCT ts_code FROM stock_daily WHERE ts_code LIKE '%.BJ' "
        "AND trade_date BETWEEN '20260409' AND '20260930'"
    ).fetchall()]
    print(f'北交所标的 (stock_daily 04-09~09-30 全期间): {len(all_bj)} 只')

    targets = []
    for ts in all_bj:
        mn, mx, n = already_loaded(conn, ts)
        # 06-16~09-30 有数据就跳过
        cur2 = conn.execute(
            'SELECT COUNT(*) FROM stock_daily WHERE ts_code=? AND trade_date BETWEEN ? AND ?',
            (ts, START, END),
        ).fetchone()
        if cur2[0] == 0:
            targets.append(ts)
    print(f'需要补 06-16~09-30 的北交所股票: {len(targets)} 只')

    if not targets:
        print('无目标, 退出')
        return

    # 3) 循环拉
    total_rows = 0
    fail_streak = 0
    t0 = time.time()
    for i, ts in enumerate(targets, 1):
        code = ts.split('.')[0]  # 6 位
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period='daily',
                start_date='20260616', end_date='20260930', adjust='',
            )
        except Exception as e:
            err = str(e)[:120]
            print(f'  [{i}/{len(targets)}] {ts} ❌ {err}')
            fail_streak += 1
            if fail_streak >= 10:
                print(f'  ⚠️ 连续失败 {fail_streak} 只, 歇 60 秒')
                time.sleep(60)
                fail_streak = 0
            continue

        if df is None or len(df) == 0:
            print(f'  [{i}/{len(targets)}] {ts} (无数据)')
            fail_streak = 0
            continue

        # 字段映射 (日期 YYYY-MM-DD → YYYYMMDD)
        rows = []
        for _, r in df.iterrows():
            d = str(r['日期']).replace('-', '')
            rows.append((
                ts, r.get('股票代码', code), d,
                r.get('开盘'), r.get('收盘'), r.get('最高'), r.get('最低'),
                r.get('涨跌幅'), r.get('成交量'), r.get('成交额'),
            ))

        cur = conn.cursor()
        cur.executemany(
            'INSERT OR IGNORE INTO stock_daily '
            '(ts_code, name, trade_date, open, close, high, low, change, volume, amount) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            rows,
        )
        n = cur.rowcount
        conn.commit()
        total_rows += n
        fail_streak = 0
        print(f'  [{i}/{len(targets)}] {ts} ✅ 写入 {n} 行')

        time.sleep(SLEEP_PER_STOCK)

    conn.close()
    print(f'\n完成: 写入 {total_rows} 行, 用时 {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
