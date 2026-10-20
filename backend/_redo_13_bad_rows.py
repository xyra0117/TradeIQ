"""一次性脚本:重做 13 行 sector_fund_flow,接口 stock/fflow/daykline/get (修正自 stock/kline/get).

之前 13 行 (source='eastmoney-daykline-topk') 用错了 kline 接口 (OHLCV),
main_net_inflow 实际是收盘价. 现删后重做,用 fflow/daykline 接口.

用法:
    python3 _redo_13_bad_rows.py
"""
import asyncio
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils_browser import BrowserSession

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
SOURCE = 'eastmoney-daykline'  # 正确 source 名(去掉 -topk)

# 13 行: (sector_code, name, trade_date)
TARGETS = [
    ('BK0884', '光刻机',     '20260803'),
    ('BK1134', '算力概念',   '20260803'),
    ('BK1184', '人形机器人', '20260804'),
    ('BK1128', 'CPO概念',    '20260805'),
    ('BK1106', '创新药',     '20260806'),
    ('BK0884', '光刻机',     '20260812'),
    ('BK0952', '第三代半导体','20260827'),
    ('BK0884', '光刻机',     '20260916'),
    ('BK0952', '第三代半导体','20260916'),
    ('BK1106', '创新药',     '20260916'),
    ('BK1128', 'CPO概念',    '20260916'),
    ('BK1134', '算力概念',   '20260916'),
    ('BK1184', '人形机器人', '20260916'),
]

# push2his fflow/daykline 字段映射 (与 backfill_sector_flow_daykline.py 一致)
IDX = {
    'date': 0, 'main_net': 1, 'small_net': 2, 'mid_net': 3, 'big_net': 4, 'super_net': 5,
    'main_pct': 6, 'small_pct': 7, 'mid_pct': 8, 'big_pct': 9, 'super_pct': 10,
    'close': 11, 'chg': 12,
}
FIELDS2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'
HOST = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'


def _f(v):
    if v is None or v == '' or v == '-':
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (ValueError, TypeError):
        return None


def _ymd(dashed: str) -> str:
    return dashed.replace('-', '')


async def fetch_one(s, code: str, ymd: str):
    url = (f'{HOST}?secid=90.{code}&fields1=f1,f2,f3,f7'
           f'&fields2={FIELDS2}&klt=101&fqt=0'
           f'&beg={ymd}&end={ymd}')
    body, tries, err = await s.fetch_json(url)
    if err:
        return None, tries, err
    kl = (body.get('data') or {}).get('klines') or []
    if not kl:
        return None, tries, 'klines 空 (停牌?)'
    for line in kl:
        p = line.split(',')
        if _ymd(p[IDX['date']]) == ymd:
            return p, tries, None
    return None, tries, f'{ymd} 不在 klines ({len(kl)} 行)'


async def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')

    inserted = 0
    failed = []

    async with BrowserSession() as s:
        for i, (code, name, ymd) in enumerate(TARGETS, 1):
            t0 = time.time()
            p, tries, err = await fetch_one(s, code, ymd)
            if err:
                print(f'[{i:2d}/{len(TARGETS)}] {ymd} {code} {name:<8} ✗ {err} ({time.time()-t0:.1f}s)')
                failed.append((code, name, ymd, err))
                await s.sleep_human()
                continue

            record = (
                ymd, 'concept', code, name,
                _f(p[IDX['close']]),
                _f(p[IDX['chg']]),
                _f(p[IDX['main_net']]),
                _f(p[IDX['main_pct']]),
                _f(p[IDX['super_net']]),
                _f(p[IDX['super_pct']]),
                _f(p[IDX['big_net']]),
                _f(p[IDX['big_pct']]),
                _f(p[IDX['mid_net']]),
                _f(p[IDX['mid_pct']]),
                _f(p[IDX['small_net']]),
                _f(p[IDX['small_pct']]),
                SOURCE,
            )
            try:
                conn.execute(
                    'INSERT OR IGNORE INTO sector_fund_flow '
                    '(trade_date, sector_type, sector_code, name, close, change_pct, '
                    ' main_net_inflow, main_net_pct, '
                    ' super_net, super_pct, big_net, big_pct, '
                    ' mid_net, mid_pct, small_net, small_pct, '
                    ' source) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    record,
                )
                conn.commit()
                inserted += 1
                main_m = _f(p[IDX['main_net']]) or 0
                print(f'[{i:2d}/{len(TARGETS)}] {ymd} {code} {name:<8} ✓ main={main_m:>16,.0f}元 ({main_m/1e8:+.2f}亿) tries={tries} ({time.time()-t0:.1f}s)')
            except Exception as e:
                print(f'[{i:2d}/{len(TARGETS)}] {ymd} {code} {name:<8} ✗ insert 异常: {e}')
                failed.append((code, name, ymd, str(e)))

            await s.sleep_human()

    print()
    print(f'完成: 插入 {inserted}/{len(TARGETS)} 行')
    if failed:
        print(f'失败 {len(failed)} 行:')
        for code, name, ymd, err in failed:
            print(f'  {ymd} {code} {name}: {err}')

    # 校验: 9/16 BK1106 应该是 +2.94 亿(我刚 ground truth)
    print()
    print('校验 9/16 BK1106 创新药:')
    row = conn.execute(
        "SELECT trade_date, name, main_net_inflow, source FROM sector_fund_flow "
        "WHERE sector_code='BK1106' AND trade_date='20260916'"
    ).fetchone()
    if row:
        print(f'  DB: {row[0]} {row[2]:,.0f} 元 ({row[2]/1e8:+.2f} 亿) source={row[3]}')


if __name__ == '__main__':
    asyncio.run(main())