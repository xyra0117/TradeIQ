"""一次性:全量拉个股融资融券历史 (RPTA_WEB_RZRQ_GGMX), 入 DB.

数据源 datacenter-web.eastmoney.com (subprocess 调 curl, urllib SSL 间歇 timeout).
范围: 默认最近 30 天 (命令行可改 BEG_DATE).
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
HOST = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
REPORT = 'RPTA_WEB_RZRQ_GGMX'  # 个股融资融券
PAGE_SIZE = 500                 # 该 reportName 实测上限
SOURCE = 'eastmoney-rzrq-stock'

# 字段: 直接 ALL 拉, 入库用全部
DDL = """
CREATE TABLE IF NOT EXISTS rzrq_stock_history (
    date          TEXT NOT NULL,           -- 2026-09-30
    stock_code    TEXT NOT NULL,           -- 688399
    stock_name    TEXT,                    -- 硕世生物
    secucode      TEXT,                    -- 688399.SH
    market        TEXT,                    -- 融资融券_沪证
    trade_market  TEXT,                    -- 上交所科创板
    trade_market_code TEXT,
    kcb           INTEGER,                 -- 科创板标志 (0/1)
    -- 余额 (元)
    rzye   REAL, rqye REAL, rzrqye REAL, rzrqyecz REAL, rzyezb REAL,
    -- 融资 (元)
    rzmre  REAL, rzche REAL, rzjme REAL,
    rzmre3d REAL, rzmre5d REAL, rzmre10d REAL,
    rzche3d REAL, rzche5d REAL, rzche10d REAL,
    rzjme3d REAL, rzjme5d REAL, rzjme10d REAL,
    -- 融券 (量/股)
    rqyl  REAL, rqmcl REAL, rqchl REAL, rqjmg REAL,
    rqyl3d REAL, rqyl5d REAL, rqyl10d REAL,
    rqmcl3d REAL, rqmcl5d REAL, rqmcl10d REAL,
    rqchl3d REAL, rqchl5d REAL, rqchl10d REAL,
    rqjmg3d REAL, rqjmg5d REAL, rqjmg10d REAL,
    -- 行情 + 衍生
    spj REAL, zdf REAL, sz REAL,
    rchange3dcp REAL, rchange5dcp REAL, rchange10dcp REAL,
    fin_balance_gr REAL,
    -- 元数据
    in_date TEXT DEFAULT (date('now')),
    source  TEXT DEFAULT 'eastmoney-rzrq-stock',
    PRIMARY KEY (date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_rzrq_stock_code ON rzrq_stock_history(stock_code);
CREATE INDEX IF NOT EXISTS idx_rzrq_stock_date ON rzrq_stock_history(date);
"""

# API 字段 → DB 列 (顺序对齐 INSERT)
INSERT_COLS = [
    'date', 'stock_code', 'stock_name', 'secucode', 'market', 'trade_market', 'trade_market_code', 'kcb',
    'rzye', 'rqye', 'rzrqye', 'rzrqyecz', 'rzyezb',
    'rzmre', 'rzche', 'rzjme',
    'rzmre3d', 'rzmre5d', 'rzmre10d',
    'rzche3d', 'rzche5d', 'rzche10d',
    'rzjme3d', 'rzjme5d', 'rzjme10d',
    'rqyl', 'rqmcl', 'rqchl', 'rqjmg',
    'rqyl3d', 'rqyl5d', 'rqyl10d',
    'rqmcl3d', 'rqmcl5d', 'rqmcl10d',
    'rqchl3d', 'rqchl5d', 'rqchl10d',
    'rqjmg3d', 'rqjmg5d', 'rqjmg10d',
    'spj', 'zdf', 'sz',
    'rchange3dcp', 'rchange5dcp', 'rchange10dcp',
    'fin_balance_gr',
]

UPSERT = (
    'INSERT INTO rzrq_stock_history (' + ', '.join(INSERT_COLS) + ') '
    'VALUES (' + ','.join('?' * len(INSERT_COLS)) + ') '
    'ON CONFLICT(date, stock_code) DO UPDATE SET '
    + ', '.join(f'{c}=excluded.{c}' for c in INSERT_COLS if c not in ('date', 'stock_code'))
)


def fetch_page(page: int, beg_date: str) -> tuple:
    """subprocess 调 curl (urllib SSL handshake 间歇 timeout 不可靠).
    指数退避重试 5 次: 2/4/8/16/32s.
    """
    import json  # 局部 import, 让 main 顶部不依赖 json
    fltr = f"(DATE>='{beg_date}')"
    url = (f'{HOST}?reportName={REPORT}&columns=ALL&source=WEB'
           f'&sortColumns=DATE&sortTypes=-1'
           f'&pageNumber={page}&pageSize={PAGE_SIZE}'
           f'&filter={urllib.parse.quote(fltr)}')
    last_err = None
    for attempt in range(1, 6):
        try:
            res = subprocess.run([
                'curl', '-s', '--noproxy', '*',
                '--max-time', '30',
                '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36',
                '-H', 'Referer: https://data.eastmoney.com/rzrq/total.html',
                url,
            ], capture_output=True, text=True, timeout=40)
            if res.returncode != 0:
                raise RuntimeError(f'curl exit {res.returncode}: {res.stderr[:100]}')
            body = json.loads(res.stdout)
            if not body.get('success'):
                return None, 0, body.get('message', 'unknown')
            rows = body['result']['data']
            total = body['result']['count']
            return rows, total, None
        except Exception as e:
            last_err = f'{type(e).__name__}: {str(e)[:120]}'
            if attempt < 5:
                wait = 2 ** attempt
                print(f'    ⚠ 第 {page} 页第 {attempt} 次失败: {last_err}, 等 {wait}s 重试')
                time.sleep(wait)
    return None, 0, last_err


def _f(v):
    if v is None or v == '' or v == '-':
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (ValueError, TypeError):
        return None


def _row_to_vals(r: dict) -> list:
    """API 字段 (大写) → DB 列 (小写). 跟 main 同样的清洗模式."""
    return [
        r.get('DATE', '')[:10],
        r.get('SCODE', ''),
        r.get('SECNAME', ''),
        r.get('SECUCODE', ''),
        r.get('MARKET', ''),
        r.get('TRADE_MARKET', ''),
        r.get('TRADE_MARKET_CODE', ''),
        _f(r.get('KCB')),
        _f(r.get('RZYE')), _f(r.get('RQYE')), _f(r.get('RZRQYE')), _f(r.get('RZRQYECZ')), _f(r.get('RZYEZB')),
        _f(r.get('RZMRE')), _f(r.get('RZCHE')), _f(r.get('RZJME')),
        _f(r.get('RZMRE3D')), _f(r.get('RZMRE5D')), _f(r.get('RZMRE10D')),
        _f(r.get('RZCHE3D')), _f(r.get('RZCHE5D')), _f(r.get('RZCHE10D')),
        _f(r.get('RZJME3D')), _f(r.get('RZJME5D')), _f(r.get('RZJME10D')),
        _f(r.get('RQYL')), _f(r.get('RQMCL')), _f(r.get('RQCHL')), _f(r.get('RQJMG')),
        _f(r.get('RQYL3D')), _f(r.get('RQYL5D')), _f(r.get('RQYL10D')),
        _f(r.get('RQMCL3D')), _f(r.get('RQMCL5D')), _f(r.get('RQMCL10D')),
        _f(r.get('RQCHL3D')), _f(r.get('RQCHL5D')), _f(r.get('RQCHL10D')),
        _f(r.get('RQJMG3D')), _f(r.get('RQJMG5D')), _f(r.get('RQJMG10D')),
        _f(r.get('SPJ')), _f(r.get('ZDF')), _f(r.get('SZ')),
        _f(r.get('RCHANGE3DCP')), _f(r.get('RCHANGE5DCP')), _f(r.get('RCHANGE10DCP')),
        _f(r.get('FIN_BALANCE_GR')),
    ]


def _init_conn():
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    conn.commit()
    return conn


def fetch_one_date(date_str: str, progress_callback=None) -> int:
    """单日同步入口 (供拉取目标面板调用).
    date_str 格式: YYYYMMDD (如 '20260930'). 该报表全历史可查.
    返回: 写入行数 (UPSERT 计 1). 失败返回 0.
    """
    # API filter 用 YYYY-MM-DD, DB 里 date 列也是 YYYY-MM-DD
    api_date = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    conn = _init_conn()
    try:
        # skip_if_exists 守卫: 该日已有数据就不重抓 (跟 summary 一致)
        existing = conn.execute(
            'SELECT 1 FROM rzrq_stock_history WHERE date = ? LIMIT 1', (api_date,)
        ).fetchone()
        if existing:
            print(f'  ⏭ stock {api_date} 已有数据, 跳过', flush=True)
            if progress_callback:
                progress_callback(0, 0, None)
            return 0

        rows, total, err = fetch_page(1, api_date)
        if err:
            print(f'  ✗ stock {api_date} 第 1 页失败: {err}', flush=True)
            if progress_callback:
                progress_callback(0, 0, err)
            return 0
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        inserted = 0
        for pn in range(1, pages + 1):
            if pn > 1:
                rows, _, err = fetch_page(pn, api_date)
                if err:
                    print(f'    第 {pn} 页失败, 跳过: {err}', flush=True)
                    continue
            if not rows:
                break
            for r in rows:
                conn.execute(UPSERT, _row_to_vals(r))
                inserted += 1
            conn.commit()
            if pn < pages:
                time.sleep(0.6)
        print(f'  ✅ stock {api_date} → rzrq_stock_history: {inserted} 行', flush=True)
        if progress_callback:
            progress_callback(inserted, total, None)
        return inserted
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--beg', default='2026-09-01', help='开始日期 (含), 默认 2026-09-01 = 最近 30 天')
    args = parser.parse_args()
    beg = args.beg

    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    conn.commit()

    rows, total, err = fetch_page(1, beg)
    if err:
        print(f'ERR 第 1 页失败: {err}')
        sys.exit(1)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f'beg={beg} 起 总 {total} 条, 每页 {PAGE_SIZE}, 共 {pages} 页')

    inserted = 0
    t0 = time.time()
    for pn in range(1, pages + 1):
        if pn > 1:
            rows, _, err = fetch_page(pn, beg)
            if err:
                print(f'  ✗ 第 {pn}/{pages} 页失败: {err}')
                time.sleep(2)
                continue
        if not rows:
            break
        page_inserted = 0
        for r in rows:
            vals = [
                r.get('DATE', '')[:10],
                r.get('SCODE', ''),
                r.get('SECNAME', ''),
                r.get('SECUCODE', ''),
                r.get('MARKET', ''),
                r.get('TRADE_MARKET', ''),
                r.get('TRADE_MARKET_CODE', ''),
                _f(r.get('KCB')),
                _f(r.get('RZYE')), _f(r.get('RQYE')), _f(r.get('RZRQYE')), _f(r.get('RZRQYECZ')), _f(r.get('RZYEZB')),
                _f(r.get('RZMRE')), _f(r.get('RZCHE')), _f(r.get('RZJME')),
                _f(r.get('RZMRE3D')), _f(r.get('RZMRE5D')), _f(r.get('RZMRE10D')),
                _f(r.get('RZCHE3D')), _f(r.get('RZCHE5D')), _f(r.get('RZCHE10D')),
                _f(r.get('RZJME3D')), _f(r.get('RZJME5D')), _f(r.get('RZJME10D')),
                _f(r.get('RQYL')), _f(r.get('RQMCL')), _f(r.get('RQCHL')), _f(r.get('RQJMG')),
                _f(r.get('RQYL3D')), _f(r.get('RQYL5D')), _f(r.get('RQYL10D')),
                _f(r.get('RQMCL3D')), _f(r.get('RQMCL5D')), _f(r.get('RQMCL10D')),
                _f(r.get('RQCHL3D')), _f(r.get('RQCHL5D')), _f(r.get('RQCHL10D')),
                _f(r.get('RQJMG3D')), _f(r.get('RQJMG5D')), _f(r.get('RQJMG10D')),
                _f(r.get('SPJ')), _f(r.get('ZDF')), _f(r.get('SZ')),
                _f(r.get('RCHANGE3DCP')), _f(r.get('RCHANGE5DCP')), _f(r.get('RCHANGE10DCP')),
                _f(r.get('FIN_BALANCE_GR')),
            ]
            conn.execute(UPSERT, vals)
            page_inserted += 1
            inserted += 1
        conn.commit()
        if pn % 10 == 0 or pn == pages:
            rate = (time.time() - t0) / pn
            eta = (pages - pn) * rate
            print(f'  [{pn:4d}/{pages}] {len(rows)} 行 (累计 {inserted}) | ETA {eta/60:.1f}min')
        time.sleep(0.6)

    # 验证
    conn.commit()
    cur = conn.execute('SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT stock_code) FROM rzrq_stock_history').fetchone()
    print(f'\n完成: {cur[2]} 行, {cur[3]} 只个股, 区间 {cur[0]} → {cur[1]}, 耗时 {(time.time()-t0)/60:.1f}min')

    # 预览
    sample = conn.execute(
        'SELECT date, stock_code, stock_name, rzye, rzjme, rqye, rzrqye, zdf '
        'FROM rzrq_stock_history ORDER BY rzye DESC LIMIT 5'
    ).fetchall()
    print('\n融资余额 top 5:')
    print(f'{"日期":<12} {"代码":<8} {"名称":<12} {"融资余额":<14} {"融资净买入":<14} {"融券余额":<14} {"两融余额":<14} {"涨跌%":<8}')
    for r in sample:
        d, code, name, rzye, rzjme, rqye, rzrqye, zdf = r
        print(f'{d:<12} {code:<8} {name:<12} {rzye/1e8:<10.2f}亿 {rzjme/1e8:<10.2f}亿 {rqye/1e8:<10.2f}亿 {rzrqye/1e8:<10.2f}亿 {zdf:<8.2f}')

    conn.close()


if __name__ == '__main__':
    main()
