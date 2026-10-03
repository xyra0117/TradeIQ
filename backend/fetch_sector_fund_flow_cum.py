"""板块资金流向 · 3/5/10/20 日累计抓取 (东方财富)

数据源: push2his.eastmoney.com/api/qt/stock/fflow/daykline/get
  - push2 域被本机 IP 限流, push2his 不限 (经验)
  - 一次返回单板块 N 天的 5 档净额 + 占比 + 价格 (f51~f60 + 价)
  - 不需要 JSONP (直接 JSON, 简单 requests.get 即可)

跟个股资金流累计 (fund_flow SUM) 口径一致: 按交易日 SUM 5 档, 不重新去 push2 拿每日截面.
trade_date 用所选日期 (默认 last_trading_date(today)); 窗口取 [trade_date-N+1, trade_date].
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# push2 系列 API 走 curl / Python urllib / requests 都空响应 (本机 IP + 代理 IP 都被限);
# 但浏览器内 fetch() 能拿到 (跟现有 fetch_fund_flow_today.py 同款 wait_for_user 套路).
# 这里直接用 playwright 跑 1000 个 sector 的 daykline fetch.

DB_PATH = Path(__file__).resolve().parent / 'market_data.db'
HIS_URL = ('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
           '?secid=90.{secid}&fields1=f1,f2,f3,f7'
           '&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60'
           '&klt=101&lmt={lmt}&cb=cb')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
           'Referer': 'https://data.eastmoney.com/bkzj/hy.html'}


def fetch_one_sector_in_browser(page, secid: str, days: int = 25, max_retries: int = 5) -> list:
    """在浏览器里用 JSONP 加载 push2his daykline 接口 (跟现有 fetch_fund_flow_sector.py 同款套路,
    <script> 注入绕过 CORS + 反爬白名单生效). 返 [(date, main, super, big, mid, small)] 降序."""
    url_base = HIS_URL.format(secid=secid, lmt=days + 5) + '&pn=1'
    js = """
    (url_base) => new Promise((resolve) => {
        const cb = 'jsc_' + Date.now() + '_' + Math.floor(Math.random()*1000);
        window[cb] = (data) => {
            try { delete window[cb]; } catch(e) { window[cb] = undefined; }
            if (script.parentNode) script.parentNode.removeChild(script);
            resolve({ok: true, data});
        };
        const script = document.createElement('script');
        script.src = url_base + '&cb=' + cb;
        script.onerror = () => {
            try { delete window[cb]; } catch(e) {}
            if (script.parentNode) script.parentNode.removeChild(script);
            resolve({ok: false, err: 'script load failed'});
        };
        document.head.appendChild(script);
        setTimeout(() => {
            if (window[cb] !== undefined) {
                try { delete window[cb]; } catch(e) {}
                if (script.parentNode) script.parentNode.removeChild(script);
                resolve({ok: false, err: 'timeout 15s'});
            }
        }, 15000);
    })
    """
    for attempt in range(max_retries):
        try:
            result = page.evaluate(js, url_base)
            if not result or not result.get('ok'):
                raise RuntimeError(result.get('err') if result else 'no result')
            data = result.get('data') or {}
            if not data.get('klines'):
                raise RuntimeError('no klines')
            rows = []
            for k in data.get('klines') or []:
                parts = k.split(',')
                if len(parts) < 10:
                    continue
                date = parts[0].replace('-', '')
                try:
                    rows.append((
                        date,
                        float(parts[1]),  # f51 主力
                        float(parts[3]),  # f53 超大
                        float(parts[5]),  # f55 大单
                        float(parts[7]),  # f57 中单
                        float(parts[9]),  # f59 小单
                    ))
                except (ValueError, IndexError):
                    continue
            return rows
        except Exception as e:
            if attempt == max_retries - 1:
                print(f'  [fail] {secid}: {e}', file=sys.stderr)
                return []
            time.sleep(1 + attempt * 2)
    return []


# 保留旧名作为 alias, 给单元测试用
def fetch_one_sector(secid: str, days: int = 25) -> list:
    """直连 fallback (curl 子进程, 反爬限流时也会失败, 仅作代码完整保留)."""
    url = HIS_URL.format(secid=secid, lmt=days + 5)
    try:
        result = subprocess.run(
            ['curl', '-s', '--noproxy', '*', '--max-time', '15',
             '-A', HEADERS['User-Agent'], '-H', f'Referer: {HEADERS["Referer"]}', url],
            capture_output=True, text=True, timeout=20)
        text = result.stdout.strip()
    except Exception:
        return []
    if not text or not text.startswith('cb('):
        return []
    try:
        d = json.loads(text[3:text.rfind(')')])
    except Exception:
        return []
    rows = []
    for k in (d.get('data') or {}).get('klines') or []:
        parts = k.split(',')
        if len(parts) < 10:
            continue
        try:
            rows.append((parts[0].replace('-', ''), float(parts[1]), float(parts[3]),
                         float(parts[5]), float(parts[7]), float(parts[9])))
        except (ValueError, IndexError):
            continue
    return rows


def run(trade_date: str, periods=(5, 10), headless: bool = True, headless_log: bool = True):
    """抓所有板块在 trade_date 当天的 N 日累计, 写入 sector_fund_flow_cumulative.
    走浏览器内 fetch (跟现有 fetch_fund_flow_today.py 同款反爬应对)."""
    from playwright.sync_api import sync_playwright

    conn = sqlite3.connect(DB_PATH)
    sectors = list(conn.execute(
        "SELECT DISTINCT sector_type, sector_code, name FROM sector_fund_flow "
        "WHERE trade_date=? ORDER BY sector_type, sector_code", (trade_date,)).fetchall())
    conn.close()
    print(f'[{datetime.now():%H:%M:%S}] sectors: {len(sectors)} (trade_date={trade_date})')
    if not sectors:
        print('⚠️ 该 trade_date sector_fund_flow 还没数据, 没有板块列表可拉')
        return

    window_days = max(periods) + 5
    aggregates = {p: [] for p in periods}
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel='chrome')
        ctx = browser.new_context(user_agent=HEADERS['User-Agent'])
        page = ctx.new_page()
        # 第一次 goto 用来建 push2 session + 拿反爬白名单 cookie
        page.goto('https://data.eastmoney.com/bkzj/hy.html', wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(2000)
        print(f'[{datetime.now():%H:%M:%S}] 浏览器 session 建好, 开始拉 {len(sectors)} 个 sector')

        for i, (stype, secid, name) in enumerate(sectors, 1):
            rows = fetch_one_sector_in_browser(page, secid, days=window_days)
            if not rows:
                failed.append((stype, secid, name))
                if headless_log and i % 20 == 0:
                    print(f'  [{i}/{len(sectors)}] {stype}/{secid} 空')
                continue
            by_date = {r[0]: r for r in rows}
            if trade_date not in by_date:
                failed.append((stype, secid, name))
                continue
            for p_n in periods:
                window = [by_date[d] for d in sorted(by_date.keys(), reverse=True)[:p_n]
                          if d in by_date and d <= trade_date]
                if not window:
                    continue
                sorted_dates = sorted(by_date.keys(), reverse=True)
                start_d = sorted_dates[len(window)-1] if len(window) > 0 else ''
                aggregates[p_n].append((stype, secid, name, p_n,
                                        sum(r[1] for r in window),
                                        sum(r[2] for r in window),
                                        sum(r[3] for r in window),
                                        sum(r[4] for r in window),
                                        sum(r[5] for r in window),
                                        len(window), start_d, window[0][0]))
            if headless_log and i % 50 == 0:
                print(f'  [{i}/{len(sectors)}] {stype}/{secid} {name}')

        browser.close()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS sector_fund_flow_cumulative (
        trade_date TEXT NOT NULL,
        period INTEGER NOT NULL,
        sector_type TEXT NOT NULL,
        sector_code TEXT NOT NULL,
        name TEXT,
        sum_main_net REAL, sum_super_net REAL, sum_big_net REAL,
        sum_mid_net REAL, sum_small_net REAL,
        flow_days INTEGER,
        start_date TEXT,
        end_date TEXT,
        saved_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (trade_date, period, sector_type, sector_code)
    )''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sffc_main ON sector_fund_flow_cumulative(period, trade_date, sum_main_net DESC)')
    for p_n, rows in aggregates.items():
        cur.execute('DELETE FROM sector_fund_flow_cumulative WHERE trade_date=? AND period=?', (trade_date, p_n))
        for row in rows:
            cur.execute('''INSERT OR REPLACE INTO sector_fund_flow_cumulative
                (trade_date, period, sector_type, sector_code, name,
                 sum_main_net, sum_super_net, sum_big_net, sum_mid_net, sum_small_net,
                 flow_days, start_date, end_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (trade_date, row[3], row[0], row[1], row[2],
                 row[4], row[5], row[6], row[7], row[8],
                 row[9], row[10], row[11]))
    conn.commit()
    conn.close()

    for p_n, rows in aggregates.items():
        print(f'  period={p_n}d: {len(rows)} 板块写入')
    if failed:
        print(f'⚠️ {len(failed)} 板块拉取失败 (skip): {failed[:5]}...')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True, help='trade_date YYYYMMDD')
    ap.add_argument('--periods', default='3,5,10,20',
                    help='逗号分隔周期天数, 默认 3,5,10,20')
    args = ap.parse_args()
    periods = [int(p) for p in args.periods.split(',')]
    run(args.date, periods=tuple(periods))


if __name__ == '__main__':
    main()