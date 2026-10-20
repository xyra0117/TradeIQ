"""两融账户信息 (RPTA_WEB_MARGIN_DAILYTRADE) 单日同步, 入 DB.

数据源 datacenter-web.eastmoney.com (subprocess 调 curl, urllib SSL 间歇 timeout).
来源: data.eastmoney.com/rzrq/zhtjday.html (zhtjday.js:1171 + 1423)
全表 count=3401, 区间 2012-09-27 ~ 至今, 每天 1 行.

⚠ 单位陷阱 (跟其他三张 rzrq 表不一样, 下游 SQL 计算 ratio 必须小心):
  - FIN_BALANCE / LOAN_BALANCE / MARGIN_BALANCE / FIN_BUY_AMT / LOAN_SELL_AMT
    / MARGIN_TRADE_AMT / FUND_MARKET_CAP / TOTAL_GUARANTEE / SUBSITUTION_CAP
    → 单位是「亿元」(非「元」, 跟 rzrq_history_summary / rzrq_stock_history 不同)
  - PERSONAL_INVESTOR_NUM → 单位是「万名」(东财前端 dw: "万名")
  - ORG_INVESTOR_NUM / INVESTOR_NUM / MARGINLIAB_INVESTOR_NUM / SECURITY_ORG_NUM / OPERATEDEPT_NUM → 单位是「个」
  - AVG_GUARANTEE_RATIO / BALANCE_RATIO / TRADE_AMT_RATIO / SCI_CHANGE_RATE → 单位是「%」

⚠ 日期列名 STATISTICS_DATE (不是 DATE 也不是 DIM_DATE)
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
REPORT = 'RPTA_WEB_MARGIN_DAILYTRADE'  # 两融账户每日统计 (zhtjday.js:1171)
PAGE_SIZE = 5000                        # 全表 3401 行, 一页拉完
SOURCE = 'eastmoney-rzrq-account'

# ⚠ 字段注释里必须写死单位, 否则下游 SQL ratio 算错
DDL = """
CREATE TABLE IF NOT EXISTS rzrq_account_history (
    statistics_date     TEXT PRIMARY KEY,            -- 2026-09-30 (格式 YYYY-MM-DD)
    date_mark           TEXT,                        -- "1" = 当日有效, 其它待确认
    -- 余额 (亿元) ⚠ 单位不是元
    fin_balance         REAL,                        -- 融资余额 (亿元)
    loan_balance        REAL,                        -- 融券余额 (亿元)
    margin_balance      REAL,                        -- 融资融券余额 (亿元)
    balance_ratio       REAL,                        -- 余额占流通比 (%)
    -- 交易额 (亿元)
    fin_buy_amt         REAL,                        -- 融资买入额 (亿元)
    loan_sell_amt       REAL,                        -- 融券卖出额 (亿元)
    margin_trade_amt    REAL,                        -- 两融交易额 (亿元)
    trade_amt_ratio     REAL,                        -- 两融交易额占成交比 (%)
    -- 机构数 (个)
    security_org_num    REAL,                        -- 证券公司家数 (个)
    operatedept_num     REAL,                        -- 营业部家数 (个)
    -- 投资者数
    personal_investor_num   REAL,                    -- 个人投资者 (万名) ⚠ 单位是万名
    org_investor_num        REAL,                    -- 机构投资者 (个)
    investor_num            REAL,                    -- 参与交易投资者 (个)
    marginliab_investor_num REAL,                    -- 有负债投资者 (个)
    -- 担保 (亿元)
    fund_market_cap         REAL,                    -- 客户资金余额 (亿元)
    total_guarantee         REAL,                    -- 担保物市值 (亿元)
    avg_guarantee_ratio     REAL,                    -- 平均担保比例 (%)
    subsitution_cap         REAL,                    -- 可充抵保证金证券市值 (亿元)
    -- 行情
    sci_close_price     REAL,                        -- 上证收盘点位
    sci_change_rate     REAL,                        -- 上证涨跌幅 (%)
    -- 元数据
    in_date TEXT DEFAULT (date('now')),
    source  TEXT DEFAULT 'eastmoney-rzrq-account'
);
CREATE INDEX IF NOT EXISTS idx_rzrq_account_date ON rzrq_account_history(statistics_date);
"""

# 字段顺序对齐 INSERT (注意 STATISTICS_DATE 是字符串 [:10] 截断)
INSERT_COLS = [
    'statistics_date', 'date_mark',
    'fin_balance', 'loan_balance', 'margin_balance', 'balance_ratio',
    'fin_buy_amt', 'loan_sell_amt', 'margin_trade_amt', 'trade_amt_ratio',
    'security_org_num', 'operatedept_num',
    'personal_investor_num', 'org_investor_num', 'investor_num', 'marginliab_investor_num',
    'fund_market_cap', 'total_guarantee', 'avg_guarantee_ratio', 'subsitution_cap',
    'sci_close_price', 'sci_change_rate',
]

UPSERT = (
    'INSERT INTO rzrq_account_history (' + ', '.join(INSERT_COLS) + ') '
    'VALUES (' + ','.join('?' * len(INSERT_COLS)) + ') '
    'ON CONFLICT(statistics_date) DO UPDATE SET '
    + ', '.join(f'{c}=excluded.{c}' for c in INSERT_COLS if c != 'statistics_date')
)


def _ensure_schema(conn):
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    conn.commit()


def fetch_page(date_str: str) -> tuple:
    """subprocess 调 curl 拉单日. 退避 5 次: 2/4/8/16/32s.
    date_str 格式 YYYY-MM-DD (API 接受短横)."""
    import json
    fltr = f"(STATISTICS_DATE='{date_str}')"
    url = (f'{HOST}?reportName={REPORT}&columns=ALL&source=WEB'
           f'&sortColumns=STATISTICS_DATE&sortTypes=-1'
           f'&pageNumber=1&pageSize={PAGE_SIZE}'
           f'&filter={urllib.parse.quote(fltr)}')
    last_err = None
    for attempt in range(1, 6):
        try:
            res = subprocess.run([
                'curl', '-s', '--noproxy', '*',
                '--max-time', '30',
                '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36',
                '-H', 'Referer: https://data.eastmoney.com/rzrq/zhtjday.html',
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
                print(f'    ⚠ 第 {attempt} 次失败: {last_err}, 等 {wait}s 重试', flush=True)
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
    """API 字段 (大写) → DB 列 (小写). 字符串列裸取, 数值列 _f()."""
    return [
        r.get('STATISTICS_DATE', '')[:10],
        r.get('DATE_MARK', ''),
        _f(r.get('FIN_BALANCE')), _f(r.get('LOAN_BALANCE')),
        _f(r.get('MARGIN_BALANCE')), _f(r.get('BALANCE_RATIO')),
        _f(r.get('FIN_BUY_AMT')), _f(r.get('LOAN_SELL_AMT')),
        _f(r.get('MARGIN_TRADE_AMT')), _f(r.get('TRADE_AMT_RATIO')),
        _f(r.get('SECURITY_ORG_NUM')), _f(r.get('OPERATEDEPT_NUM')),
        _f(r.get('PERSONAL_INVESTOR_NUM')), _f(r.get('ORG_INVESTOR_NUM')),
        _f(r.get('INVESTOR_NUM')), _f(r.get('MARGINLIAB_INVESTOR_NUM')),
        _f(r.get('FUND_MARKET_CAP')), _f(r.get('TOTAL_GUARANTEE')),
        _f(r.get('AVG_GUARANTEE_RATIO')), _f(r.get('SUBSITUTION_CAP')),
        _f(r.get('SCI_CLOSE_PRICE')), _f(r.get('SCI_CHANGE_RATE')),
    ]


def fetch_one_date(date_str: str, progress_callback=None) -> int:
    """单日同步入口 (供拉取目标面板调用).
    date_str 格式: YYYYMMDD (如 '20260930').
    返回: 写入行数 (UPSERT 计 1). 失败返回 0."""
    api_date = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    rows, total, err = fetch_page(api_date)
    if err:
        print(f'  ✗ account {api_date} 失败: {err}', flush=True)
        if progress_callback:
            progress_callback(0, 0, err)
        return 0
    if not rows:
        # 账户表历史数据有, 任意交易日 1 行, 不该空
        print(f'  ⚠ account {api_date} 返回 0 行 (可能非交易日或 filter 异常)', flush=True)
        if progress_callback:
            progress_callback(0, total, None)
        return 0

    conn = sqlite3.connect(DB_PATH, timeout=120)
    try:
        _ensure_schema(conn)
        for r in rows:
            conn.execute(UPSERT, _row_to_vals(r))
        conn.commit()
    finally:
        conn.close()

    inserted = len(rows)
    print(f'  ✅ account {api_date} → rzrq_account_history: {inserted} 行', flush=True)
    if progress_callback:
        progress_callback(inserted, total, None)
    return inserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default='', help='YYYYMMDD 格式, 默认空')
    args = parser.parse_args()

    if not args.date:
        today = time.strftime('%Y%m%d')
        print(f'未传 --date, 默认今天 {today}', flush=True)
        date_str = today
    else:
        date_str = args.date

    t0 = time.time()
    fetch_one_date(date_str)
    print(f'耗时 {(time.time()-t0):.1f}s', flush=True)


if __name__ == '__main__':
    main()
