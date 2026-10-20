"""一次性:全量拉融资融券历史汇总 (RPTA_RZRQ_LSHJ) 入 DB.

数据源 datacenter-web.eastmoney.com (curl 直连无反爬), 不走 playwright.
汇总字段 = 沪深300 全市场 (不分 SH/SZ/BJ, 单条每日一行).
"""
import os
import sqlite3
import sys
import time
import urllib.request

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
HOST = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
REPORT = 'RPTA_RZRQ_LSHJ'  # 沪深 300 全市场汇总 (RZYE/RQYE/RZRQYE/LTSZ...)
PAGE_SIZE = 800            # 实测上限
SOURCE = 'eastmoney-rzrq-summary'

# 字段: 直接 ALL 拉, 入库用全部
DDL = """
CREATE TABLE IF NOT EXISTS rzrq_history_summary (
    dim_date TEXT PRIMARY KEY,            -- 2026-09-29
    -- 大盘
    new REAL,                             -- 上证指数点位
    zdf REAL,                             -- 上证涨跌幅
    ltsz REAL,                            -- 流通市值 (元)
    zdf3d REAL, zdf5d REAL, zdf10d REAL,
    -- 融资 (元)
    rzye REAL,                            -- 融资余额
    rzyezb REAL,                          -- 融资余额占流通市值比 (%)
    rzmre REAL,                           -- 融资买入额
    rzmre3d REAL, rzmre5d REAL, rzmre10d REAL,
    rzche REAL,                           -- 融资偿还额
    rzche3d REAL, rzche5d REAL, rzche10d REAL,
    rzjme REAL,                           -- 融资净买入
    rzjme3d REAL, rzjme5d REAL, rzjme10d REAL,
    -- 融券
    rqye REAL,                            -- 融券余额 (元)
    rqyl REAL,                            -- 融券余量 (股)
    rqchl REAL,                           -- 融券卖出量
    rqchl3d REAL, rqchl5d REAL, rqchl10d REAL,
    rqmcl REAL,                           -- 融券偿还量
    rqmcl3d REAL, rqmcl5d REAL, rqmcl10d REAL,
    rqjmg REAL,                           -- 融券净卖出
    rqjmg3d REAL, rqjmg5d REAL, rqjmg10d REAL,
    -- 合计
    rzrqye REAL,                          -- 融资融券余额
    rzrqyecz REAL,                        -- 融资融券余额差 (前一日)
    -- 元数据
    in_date TEXT DEFAULT (date('now')),
    source TEXT DEFAULT 'eastmoney-rzrq-summary'
);
"""

# 接口字段 → DB 列 (顺序对齐 INSERT)
INSERT_COLS = [
    'dim_date',
    'new', 'zdf', 'ltsz', 'zdf3d', 'zdf5d', 'zdf10d',
    'rzye', 'rzyezb',
    'rzmre', 'rzmre3d', 'rzmre5d', 'rzmre10d',
    'rzche', 'rzche3d', 'rzche5d', 'rzche10d',
    'rzjme', 'rzjme3d', 'rzjme5d', 'rzjme10d',
    'rqye', 'rqyl',
    'rqchl', 'rqchl3d', 'rqchl5d', 'rqchl10d',
    'rqmcl', 'rqmcl3d', 'rqmcl5d', 'rqmcl10d',
    'rqjmg', 'rqjmg3d', 'rqjmg5d', 'rqjmg10d',
    'rzrqye', 'rzrqyecz',
]

UPSERT = (
    'INSERT INTO rzrq_history_summary (' + ', '.join(INSERT_COLS) + ') '
    'VALUES (' + ','.join('?' * len(INSERT_COLS)) + ') '
    'ON CONFLICT(dim_date) DO UPDATE SET '
    + ', '.join(f'{c}=excluded.{c}' for c in INSERT_COLS if c != 'dim_date')
)


def fetch_page(page: int) -> tuple:
    url = (f'{HOST}?reportName={REPORT}&columns=ALL&source=WEB'
           f'&sortColumns=DIM_DATE&sortTypes=-1'
           f'&pageNumber={page}&pageSize={PAGE_SIZE}')
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36',
        'Referer': 'https://data.eastmoney.com/rzrq/total.html',
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        import json
        body = json.load(r)
    if not body.get('success'):
        return None, 0, body.get('message', 'unknown')
    rows = body['result']['data']
    total = body['result']['count']
    return rows, total, None


def _f(v):
    if v is None or v == '' or v == '-':
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (ValueError, TypeError):
        return None


def _row_to_vals(r: dict, dim_date: str) -> list:
    """API 字段 (大写) → DB 列 (小写). 跟 main 同样的清洗模式."""
    return [dim_date] + [_f(r.get(k.upper())) for k in INSERT_COLS[1:]]


def _init_conn():
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')  # 等 Flask 释放, 最多 60s
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    conn.commit()
    return conn


def fetch_one_date(date_str: str, progress_callback=None) -> int:
    """单日同步入口 (供拉取目标面板调用).
    date_str 格式: YYYYMMDD (如 '20260930'). 该报表全历史可查, 不像行业/概念只有当天.
    返回: 写入行数 (UPSERT 计 1). 失败返回 0.
    """
    # 汇总表里 date 列存的是 YYYY-MM-DD, API filter 也用 YYYY-MM-DD
    api_date = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    conn = _init_conn()
    try:
        # 先查该日是否已存在 (skip_if_exists 语义, 跟 fetch_fund_flow_today 同步)
        existing = conn.execute(
            'SELECT 1 FROM rzrq_history_summary WHERE dim_date = ? LIMIT 1', (api_date,)
        ).fetchone()
        if existing:
            print(f'  ⏭ summary {api_date} 已有数据, 跳过 (可 --force 覆盖)', flush=True)
            if progress_callback:
                progress_callback(0, 0, None)
            return 0

        # 该 reportName 不用 filter, 只能通过分页扫描 — 优化: 先翻第一页看 date 范围
        # 为简单起见, 单日同步就翻全部 (4009 行 / 800 = 6 页, 几秒)
        rows, total, err = fetch_page(1)
        if err:
            print(f'  ✗ summary {api_date} 第 1 页失败: {err}', flush=True)
            if progress_callback:
                progress_callback(0, 0, err)
            return 0
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        inserted = 0
        for pn in range(1, pages + 1):
            if pn > 1:
                rows, _, err = fetch_page(pn)
                if err:
                    print(f'    第 {pn} 页失败, 跳过: {err}', flush=True)
                    continue
            for r in rows:
                dim_date = r.get('DIM_DATE', '')[:10]
                if dim_date != api_date:
                    continue
                conn.execute(UPSERT, _row_to_vals(r, dim_date))
                inserted += 1
            if pn < pages:
                time.sleep(0.3)
        conn.commit()
        print(f'  ✅ summary {api_date} → rzrq_history_summary: {inserted} 行', flush=True)
        if progress_callback:
            progress_callback(inserted, total, None)
        return inserted
    finally:
        conn.close()


def main():
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')  # 等 Flask 释放, 最多 60s
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    conn.commit()

    # 先看一遍拿到 total,再决定翻多少页
    rows, total, err = fetch_page(1)
    if err:
        print(f'ERR 第 1 页失败: {err}')
        sys.exit(1)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f'总 {total} 条, 每页 {PAGE_SIZE}, 共 {pages} 页')

    inserted = 0
    for pn in range(1, pages + 1):
        if pn > 1:
            rows, _, err = fetch_page(pn)
            if err:
                print(f'ERR 第 {pn} 页失败: {err}')
                continue
        if not rows:
            break
        for r in rows:
            dim_date = r.get('DIM_DATE', '')[:10]
            vals = [dim_date] + [_f(r.get(k.upper())) for k in INSERT_COLS[1:]]
            conn.execute(UPSERT, vals)
            inserted += 1
        conn.commit()
        print(f'  第 {pn}/{pages} 页: {len(rows)} 行 (累计 {inserted})')
        time.sleep(0.5)

    print()
    # 验证
    first = conn.execute('SELECT MIN(dim_date), MAX(dim_date), COUNT(*) FROM rzrq_history_summary').fetchone()
    print(f'汇总历史: {first[2]} 行, 区间 {first[0]} → {first[1]}')

    # 最新一日数据预览
    latest = conn.execute(
        'SELECT dim_date, new, zdf, rzye, rzjme, rqye, rzrqye, rzyezb '
        'FROM rzrq_history_summary ORDER BY dim_date DESC LIMIT 5'
    ).fetchall()
    print('\n最新 5 日预览:')
    print(f'{"日期":<12} {"点位":<10} {"涨跌%":<8} {"融资余额":<18} {"融资净买入":<18} {"融券余额":<18} {"两融余额":<18} {"融资占流通%":<8}')
    for r in latest:
        d, new, zdf, rzye, rzjme, rqye, rzrqye, rzyezb = r
        print(f'{d:<12} {new:<10.2f} {zdf:<8.3f} {rzye/1e8:<15.2f}亿 {rzjme/1e8:<15.2f}亿 {rqye/1e8:<15.2f}亿 {rzrqye/1e8:<15.2f}亿 {rzyezb:<8.4f}')

    conn.close()


if __name__ == '__main__':
    main()
