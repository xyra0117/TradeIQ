"""回填 market_fflow 大盘主力净流入历史（东财 push2his 日K）。

数据源: https://data.eastmoney.com/zjlx/dpzjlx.html 对应的 daykline 接口,
覆盖 2026-03-19 至今。历史没有分钟曲线, 只补每日收盘定格值:
times=['15:00'], series 单点, complete=1。已有数据不动。

用法: python3 scripts/backfill_market_fflow.py
"""
import os
import sys
import json
import time
import sqlite3

import requests

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'market_data.db')

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Referer': 'https://data.eastmoney.com/',
}
_URL = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
_PARAMS = dict(lmt=0, klt=101, fields1='f1,f2,f3,f7',
               fields2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63')


def fetch_daykline(secid):
    """返回 {date: [main, small, mid, big, super, main_pct, ...]} (date='YYYY-MM-DD')."""
    s = requests.Session()
    s.trust_env = False  # 本机 shell 代理变量会坏事
    for attempt in range(5):
        try:
            d = s.get(_URL, params=dict(_PARAMS, secid=secid),
                      headers=_HEADERS, timeout=10).json()
            kl = (d.get('data') or {}).get('klines') or []
            out = {}
            for line in kl:
                p = line.split(',')
                if len(p) < 11:
                    continue
                out[p[0]] = [float(x) for x in p[1:11]]
            return out
        except Exception as e:
            print(f'  {secid} attempt {attempt + 1}/5 failed: {type(e).__name__}', flush=True)
            time.sleep(2 + attempt * 2)
    return {}


def main():
    sh = fetch_daykline('1.000001')   # 上证
    sz = fetch_daykline('0.399001')   # 深证
    if not sh or not sz:
        print('拉取失败，退出')
        return
    print(f'上证 {len(sh)} 天, 深证 {len(sz)} 天, 范围 {min(sh)} ~ {max(sh)}')

    conn = sqlite3.connect(DB_PATH)
    existing = {r[0] for r in conn.execute('SELECT trade_date FROM market_fflow').fetchall()}

    keys = ['main', 'small', 'mid', 'big', 'super']
    inserted = skipped = 0
    for date in sorted(set(sh) & set(sz)):
        td = date.replace('-', '')
        if td in existing:
            skipped += 1
            continue
        v_sh, v_sz = sh[date], sz[date]
        # 金额直接相加; amount 由 主力/主力占比 反推 (与 pct 同口径), pct 合并后重算
        vals = [v_sh[i] + v_sz[i] for i in range(5)]
        amt_sh = v_sh[0] / (v_sh[5] / 100) if v_sh[5] else 0
        amt_sz = v_sz[0] / (v_sz[5] / 100) if v_sz[5] else 0
        amount = amt_sh + amt_sz
        pcts = [(vals[i] / amount * 100 if amount else None) for i in range(5)]

        last = dict(zip(keys, vals))
        pct = dict(zip(keys, pcts))
        series = {k: [last[k]] for k in keys}
        conn.execute('''INSERT OR REPLACE INTO market_fflow
            (trade_date, times, series, amount, last, pct, complete, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now','localtime'))''',
            (td, json.dumps(['15:00']), json.dumps(series), amount,
             json.dumps(last), json.dumps(pct)))
        inserted += 1

    conn.commit()
    conn.close()
    print(f'补了 {inserted} 天, 已有跳过 {skipped} 天')


if __name__ == '__main__':
    main()
