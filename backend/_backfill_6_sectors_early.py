"""一次性脚本:补 6 个近期热门 sector 的早期缺失日 (5/12 起 → DB first_seen -1).

为啥要补: BK0884/0952/1106/1128/1134/1184 这 6 个 sector 在东财 fflow/daykline 接口
5/12 起就有数据, 但 tradeiq 4/9 起的 backfill 跑时它们还在 tradeiq universe 之外
(theme_concept.em_code 还没把这些放进来, sector_fund_flow 也没这几行),
所以 5/12 到 DB first_seen 之间的 50+ 个交易日一直没入库.
用户查 4/9-9/14 那段时, 这些 sector 表格里就不见行.

验证: probe 4 个 sector 起点都是 2026-05-12.

用法:
    python3 _backfill_6_sectors_early.py
"""
import asyncio
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils_browser import BrowserSession

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
SOURCE = 'eastmoney-daykline'

# (sector_code, name, end_exclusive) - end_exclusive = DB first_seen
TARGETS = [
    ('BK0884', '光刻机',     '20260803'),
    ('BK1134', '算力概念',   '20260803'),
    ('BK1184', '人形机器人', '20260804'),
    ('BK1128', 'CPO概念',    '20260805'),
    ('BK1106', '创新药',     '20260806'),
    ('BK0952', '第三代半导体','20260827'),
]

IDX = {
    'date': 0, 'main_net': 1, 'small_net': 2, 'mid_net': 3, 'big_net': 4, 'super_net': 5,
    'main_pct': 6, 'small_pct': 7, 'mid_pct': 8, 'big_pct': 9, 'super_pct': 10,
    'close': 11, 'chg': 12,
}
FIELDS2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'
HOST = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
BEG = '20260512'


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


async def fetch_range(s, code: str, beg: str, end: str):
    """返回 [(yyyy, kline_line), ...] - 包含 beg..end-1 区间所有 kline"""
    url = (f'{HOST}?secid=90.{code}&fields1=f1,f2,f3,f7'
           f'&fields2={FIELDS2}&klt=101&fqt=0'
           f'&beg={beg}&end={end}')
    body, tries, err = await s.fetch_json(url)
    if err:
        return [], tries, err
    kl = (body.get('data') or {}).get('klines') or []
    return kl, tries, None


def get_existing_dates(conn, code: str) -> set:
    rows = conn.execute(
        'SELECT trade_date FROM sector_fund_flow WHERE sector_code=?',
        (code,),
    ).fetchall()
    return {r[0] for r in rows}


def get_trading_dates(conn, end_exclusive: str) -> list:
    """返回 5/12 到 end_exclusive (不含) 之间所有交易日 (含非交易日空档)"""
    rows = conn.execute(
        "SELECT cal_date FROM trading_dates_cache "
        "WHERE cal_date >= ? AND cal_date < ? ORDER BY cal_date",
        (BEG, end_exclusive),
    ).fetchall()
    return [r[0] for r in rows]


async def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')

    total_inserted = 0
    total_skipped = 0
    total_failed = 0

    async with BrowserSession() as s:
        for code, name, end_ex in TARGETS:
            t0 = time.time()
            existing = get_existing_dates(conn, code)
            trading_days = get_trading_dates(conn, end_ex)

            # 拉一次完整 daykline
            klines, tries, err = await fetch_range(s, code, BEG, end_ex)
            if err:
                print(f'{code} {name} ✗ 拉取失败: {err}')
                total_failed += len(trading_days)
                continue

            # kline 索引: ymd → line
            kline_map = {}
            for line in klines:
                p = line.split(',')
                kline_map[_ymd(p[0])] = p

            inserted = 0
            skipped = 0
            missing_days = []
            for ymd in trading_days:
                if ymd in existing:
                    skipped += 1
                    continue
                p = kline_map.get(ymd)
                if not p:
                    missing_days.append(ymd)
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
                inserted += 1
            conn.commit()

            print(f'{code} {name} 补 {inserted} 行, 跳过已存在 {skipped}, '
                  f'东财无数据 {len(missing_days)} ({",".join(missing_days[:5])}{"..." if len(missing_days) > 5 else ""}) '
                  f'({time.time()-t0:.1f}s)')
            total_inserted += inserted
            total_skipped += skipped
            total_failed += len(missing_days)
            await s.sleep_human()

    print()
    print(f'汇总: 新增 {total_inserted}, 跳过已存在 {total_skipped}, 东财无数据 {total_failed}')
    print()
    print('===== 验证 =====')
    for code, name, _ in TARGETS:
        rows = conn.execute(
            'SELECT COUNT(*) FROM sector_fund_flow WHERE sector_code=?',
            (code,),
        ).fetchone()[0]
        first = conn.execute(
            'SELECT MIN(trade_date) FROM sector_fund_flow WHERE sector_code=?',
            (code,),
        ).fetchone()[0]
        print(f'  {code} {name}: {rows} 行, 起点 {first}')

    conn.close()


if __name__ == '__main__':
    asyncio.run(main())