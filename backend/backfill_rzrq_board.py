"""行业 / 概念 融资融券 (RPTA_WEB_BKJYMXN) 单日同步, 入 DB.

数据源 datacenter-web.eastmoney.com (subprocess 调 curl, urllib SSL 间歇 timeout).
行业 = BOARD_TYPE_CODE='005' (财富通行业, 127 行)
概念 = BOARD_TYPE_CODE='006' (财富通概念, 502 行)
两个共用一个 reportName, 只差 filter.

⚠ 该报表物理上只有最近一个交易日一行/板块 — 没有历史. 历史日期 filter 返回 9201.
   所以只支持"当日同步", 不做全量回填. 拉取目标面板里也是补 last_trading_date.
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
REPORT = 'RPTA_WEB_BKJYMXN'         # 行业 / 概念 融资融券明细 (bklist.js:1887 + bkdetail.js:2056)
PAGE_SIZE = 500                     # 该 reportName 实测上限 (行业 127 / 概念 502 都单页够)

# BOARD_TYPE_CODE 取值 (bklist.js:2033-2050): 005=行业 006=概念 004=地域
TYPE_INDUSTRY = '005'
TYPE_CONCEPT = '006'
TYPE_NAME = {'005': 'industry', '006': 'concept'}

DDL = """
CREATE TABLE IF NOT EXISTS rzrq_industry_history (
    trade_date      TEXT NOT NULL,         -- 2026-09-30
    board_code      TEXT NOT NULL,         -- 1018 / BK0438
    board_name      TEXT,                  -- 半导体 / 元件
    board_type      TEXT,                  -- 财富通行业
    board_type_code TEXT,                  -- 005
    -- 余额 (元)
    fin_balance        REAL,               -- 融资余额
    loan_balance       REAL,               -- 融券余额
    margin_balance     REAL,               -- 两融余额
    fin_balance_diff   REAL,               -- 余额差 (前一日)
    fin_balance_ratio  REAL,               -- 融资余额占流通比 (%)
    -- 融资 (元)
    fin_buy_amt     REAL, fin_repay_amt  REAL, fin_netbuy_amt  REAL, fin_netsell_amt REAL,
    -- 融券 (量/股)
    loan_balance_vol REAL, loan_sell_vol REAL, loan_sell_amt REAL, loan_repay_vol REAL, loan_netsell_amt REAL,
    -- 衍生
    notlimited_marketcap_a REAL,           -- 流通市值 (元)
    -- 元数据
    in_date TEXT DEFAULT (date('now')),
    source  TEXT DEFAULT 'eastmoney-rzrq-industry',
    PRIMARY KEY (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_rzrq_industry_date  ON rzrq_industry_history(trade_date);
CREATE INDEX IF NOT EXISTS idx_rzrq_industry_code  ON rzrq_industry_history(board_code);

CREATE TABLE IF NOT EXISTS rzrq_concept_history (
    trade_date      TEXT NOT NULL,
    board_code      TEXT NOT NULL,
    board_name      TEXT,
    board_type      TEXT,
    board_type_code TEXT,
    fin_balance        REAL, loan_balance REAL, margin_balance REAL, fin_balance_diff REAL, fin_balance_ratio REAL,
    fin_buy_amt     REAL, fin_repay_amt  REAL, fin_netbuy_amt  REAL, fin_netsell_amt REAL,
    loan_balance_vol REAL, loan_sell_vol REAL, loan_sell_amt REAL, loan_repay_vol REAL, loan_netsell_amt REAL,
    notlimited_marketcap_a REAL,
    in_date TEXT DEFAULT (date('now')),
    source  TEXT DEFAULT 'eastmoney-rzrq-concept',
    PRIMARY KEY (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_rzrq_concept_date  ON rzrq_concept_history(trade_date);
CREATE INDEX IF NOT EXISTS idx_rzrq_concept_code  ON rzrq_concept_history(board_code);
"""

# 字段顺序对齐 INSERT, 跟 stock 脚本同样的 _f() 清洗模式
INSERT_COLS = [
    'trade_date', 'board_code', 'board_name', 'board_type', 'board_type_code',
    'fin_balance', 'loan_balance', 'margin_balance', 'fin_balance_diff', 'fin_balance_ratio',
    'fin_buy_amt', 'fin_repay_amt', 'fin_netbuy_amt', 'fin_netsell_amt',
    'loan_balance_vol', 'loan_sell_vol', 'loan_sell_amt', 'loan_repay_vol', 'loan_netsell_amt',
    'notlimited_marketcap_a',
]

UPSERT_TMPL = (
    'INSERT INTO {table} (' + ', '.join(INSERT_COLS) + ') '
    'VALUES (' + ','.join('?' * len(INSERT_COLS)) + ') '
    'ON CONFLICT(trade_date, board_code) DO UPDATE SET '
    + ', '.join(f'{c}=excluded.{c}' for c in INSERT_COLS if c not in ('trade_date', 'board_code'))
)
UPSERT_INDUSTRY = UPSERT_TMPL.format(table='rzrq_industry_history')
UPSERT_CONCEPT  = UPSERT_TMPL.format(table='rzrq_concept_history')

REFERER = {
    TYPE_INDUSTRY: 'https://data.eastmoney.com/rzrq/hy.html',
    TYPE_CONCEPT:  'https://data.eastmoney.com/rzrq/gn.html',
}


def _ensure_schema(conn):
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    conn.commit()


def fetch_page() -> tuple:
    """subprocess 调 curl 拉全量 (无 filter, 因为多 filter 用 and 会被东财后端忽略).
    pageSize=1000 已经能装下 9-30 的 3 类合计 ~661 行, 一页够.
    指数退避重试 5 次: 2/4/8/16/32s.
    返回: (rows 全量, total, err_msg)
    """
    import json
    url = (f'{HOST}?reportName={REPORT}&columns=ALL&source=WEB'
           f'&sortColumns=FIN_BALANCE&sortTypes=-1'
           f'&pageNumber=1&pageSize=1000')
    last_err = None
    for attempt in range(1, 6):
        try:
            res = subprocess.run([
                'curl', '-s', '--noproxy', '*',
                '--max-time', '30',
                '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36',
                '-H', 'Referer: https://data.eastmoney.com/rzrq/hy.html',
                url,
            ], capture_output=True, text=True, timeout=40)
            if res.returncode != 0:
                raise RuntimeError(f'curl exit {res.returncode}: {res.stderr[:100]}')
            body = json.loads(res.stdout)
            if not body.get('success'):
                return None, 0, body.get('message', 'unknown')
            rows = body['result']['data'] or []
            total = body['result']['count'] or 0
            return rows, total, None
        except Exception as e:
            last_err = f'{type(e).__name__}: {str(e)[:120]}'
            if attempt < 5:
                wait = 2 ** attempt
                print(f'    ⚠ fetch_page 第 {attempt} 次失败: {last_err}, 等 {wait}s 重试', flush=True)
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


def _row_to_vals(r: dict, trade_date: str) -> list:
    """API 字段 (大写) → DB 列 (小写). 跟 stock 脚本一致的 _f() 清洗 + 字符串列裸取."""
    return [
        trade_date,
        r.get('BOARD_CODE', ''),
        r.get('BOARD_NAME', ''),
        r.get('BOARD_TYPE', ''),
        r.get('BOARD_TYPE_CODE', ''),
        _f(r.get('FIN_BALANCE')), _f(r.get('LOAN_BALANCE')), _f(r.get('MARGIN_BALANCE')),
        _f(r.get('FIN_BALANCE_DIFF')), _f(r.get('FIN_BALANCE_RATIO')),
        _f(r.get('FIN_BUY_AMT')), _f(r.get('FIN_REPAY_AMT')),
        _f(r.get('FIN_NETBUY_AMT')), _f(r.get('FIN_NETSELL_AMT')),
        _f(r.get('LOAN_BALANCE_VOL')), _f(r.get('LOAN_SELL_VOL')),
        _f(r.get('LOAN_SELL_AMT')), _f(r.get('LOAN_REPAY_VOL')),
        _f(r.get('LOAN_NETSELL_AMT')),
        _f(r.get('NOTLIMITED_MARKETCAP_A')),
    ]


def fetch_one_date(date_str: str, board_type_code: str, progress_callback=None) -> int:
    """单日单类型同步入口 (供拉取目标面板调用).
    date_str 格式: YYYYMMDD (如 '20260930').
    board_type_code: '005' 行业 / '006' 概念.
    返回: 写入行数 (UPSERT 全部计 1). 失败返回 0.

    ⚠ 该 reportName 物理上只存最近一天数据, 没有历史. 拉到的 TRADE_DATE 必须等于
       传入的 api_date, 否则说明报表已被下一天覆盖, 此时不写入 (返回 0 + 警告).
    """
    api_date = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    rows, total, err = fetch_page()
    if err:
        print(f'  ✗ {TYPE_NAME[board_type_code]} {api_date} 失败: {err}', flush=True)
        if progress_callback:
            progress_callback(0, 0, err)
        return 0
    if not rows:
        print(f'  ⚠ {TYPE_NAME[board_type_code]} {api_date} 返回 0 行 (报表无数据)', flush=True)
        if progress_callback:
            progress_callback(0, total, None)
        return 0

    # Python 端按 BOARD_TYPE_CODE 过滤 + 校验 TRADE_DATE 一致
    actual_date = rows[0].get('TRADE_DATE', '')[:10]
    if actual_date != api_date:
        # 报表已是更新的日期 (如 10-15 调用却返回 10-15 而非 9-30), 不写入
        print(f'  ⚠ {TYPE_NAME[board_type_code]} 想拉 {api_date}, 但报表已是 {actual_date} '
              f'(该 reportName 只存最近一天), 跳过', flush=True)
        if progress_callback:
            progress_callback(0, total, f'report_date={actual_date} != want={api_date}')
        return 0

    filtered = [r for r in rows if r.get('BOARD_TYPE_CODE') == board_type_code]
    if not filtered:
        print(f'  ⚠ {TYPE_NAME[board_type_code]} {api_date} 过滤后 0 行 (类型 {board_type_code} 无数据)', flush=True)
        if progress_callback:
            progress_callback(0, total, None)
        return 0

    table = 'rzrq_industry_history' if board_type_code == TYPE_INDUSTRY else 'rzrq_concept_history'
    upsert = UPSERT_INDUSTRY if board_type_code == TYPE_INDUSTRY else UPSERT_CONCEPT

    conn = sqlite3.connect(DB_PATH, timeout=120)
    try:
        _ensure_schema(conn)
        for r in filtered:
            conn.execute(upsert, _row_to_vals(r, api_date))
        conn.commit()
    finally:
        conn.close()

    inserted = len(filtered)
    print(f'  ✅ {TYPE_NAME[board_type_code]} {api_date} → {table}: {inserted} 行 (总 {total} 含其他类型)', flush=True)
    if progress_callback:
        progress_callback(inserted, total, None)
    return inserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--type', default='both', choices=['industry', 'concept', 'both'],
                        help='industry / concept / both (默认 both)')
    parser.add_argument('--date', default='', help='YYYYMMDD 格式, 默认空 (留待调用方传)')
    args = parser.parse_args()

    if not args.date:
        # CLI 不传 --date 时, 默认拉今天. 实际面板走 fetch_one_date(), 会显式传 ref_date
        today = time.strftime('%Y%m%d')
        print(f'未传 --date, 默认今天 {today} (注意: 报表只有最近一个交易日, 节假日会拿不到)', flush=True)
        date_str = today
    else:
        date_str = args.date

    types = [TYPE_INDUSTRY, TYPE_CONCEPT] if args.type == 'both' else (
        [TYPE_INDUSTRY] if args.type == 'industry' else [TYPE_CONCEPT]
    )
    t0 = time.time()
    for code in types:
        fetch_one_date(date_str, code)
    print(f'耗时 {(time.time()-t0):.1f}s', flush=True)


if __name__ == '__main__':
    main()
