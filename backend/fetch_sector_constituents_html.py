"""板块成分股 HTML 路径 (不走 push2 API, 走 data.eastmoney.com/bkzj 页面).

为啥这个存在:
  push2.eastmoney.com/api/qt/clist/get IP 级别限流, 本机被盯死
  改走 data.eastmoney.com/bkzj/{code}.html 真页面 + 翻页, IP 宽松

表头列 (11 列):
  0:序号 1:代码 2:名称 3:相关 4:最新价 5:今日涨跌幅
  6:今日主力净流入 7:今日超大单净流入 8:今日大单净流入
  9:今日中单净流入 10:今日小单净流入

⚠ 此版只拿 (code, name, close, change_pct), 其他列 (amount/turnover/pe) 留空
  (HTML 表格里没有这些字段, 要从行情 API 拿, 但 push2 被盯死)
"""
import argparse
import asyncio
import datetime as dt
import os
import random
import re
import sys
import sqlite3
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils_browser import BrowserSession


def log(msg):
    """用 sys.stdout.write + flush 替代 print, 避免 asyncio 事件循环里 print 走丢.
    (之前 print(flush=True) 在 nohup 重定向 + Playwright asyncio 场景下, 中间 OK 报告
     没刷盘, log 只显示 0 行 + 最终 0 行分支, 看起来全失败其实部分成功.)
    """
    sys.stdout.write(f'[{dt.datetime.now():%H:%M:%S}] {msg}\n')
    sys.stdout.flush()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
PAGE_URL_TPL = 'https://data.eastmoney.com/bkzj/{code}.html'
PAGE_SIZE = 50  # HTML 页面每页固定 50
MAX_PAGES = 30  # 兜底防死循环 (1500 成员上限, 实际没这么大板块)

DDL_ADD = """
CREATE TABLE IF NOT EXISTS sector_constituent (
    sector_code TEXT NOT NULL,
    sector_type TEXT NOT NULL,
    sector_name TEXT,
    stock_code  TEXT NOT NULL,
    stock_name  TEXT,
    sub_industry TEXT,
    market      TEXT,
    close       REAL,
    change_pct  REAL,
    amount      REAL,
    turnover_pct REAL,
    pe_ratio    REAL,
    in_date     TEXT DEFAULT (date('now')),
    PRIMARY KEY (sector_code, stock_code)
);
"""


def _to_float(s):
    if not s or s == '-':
        return None
    try:
        v = float(s.replace(',', '').replace('%', ''))
        return v if v == v else None
    except (ValueError, TypeError):
        return None


async def fetch_one_sector(page, code: str, name: str, stype: str) -> list:
    """点翻页拿全一个 sector 成分股, 返回 [{code, name, close, change_pct}, ...]."""
    url = PAGE_URL_TPL.format(code=code)
    try:
        resp = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    except Exception as e:
        return [], f'goto: {type(e).__name__}: {str(e)[:80]}'
    if not resp or resp.status != 200:
        return [], f'HTTP {resp.status if resp else "?"}'
    # 等 JS 渲染 + 表格出现
    try:
        await page.wait_for_selector('td', timeout=15000)
    except Exception:
        return [], 'no <td> rendered in 15s'
    await asyncio.sleep(1.5)

    all_rows = []
    for pn in range(1, MAX_PAGES + 1):
        rows = await page.evaluate('''
() => {
    const tds = Array.from(document.querySelectorAll('td')).filter(td => /^\\d{6}$/.test((td.textContent||'').trim()));
    const result = [];
    tds.forEach(td => {
        const tr = td.closest('tr');
        if (!tr) return;
        const cells = Array.from(tr.querySelectorAll('td')).map(c => (c.textContent||'').trim());
        result.push({
            code: cells[1] || '',
            name: cells[2] || '',
            close: cells[4] || '',
            change_pct: cells[5] || '',
        });
    });
    return result;
}
        ''')
        if not rows:
            break
        all_rows.extend(rows)
        # 不到 50 行 = 最后一页
        if len(rows) < 50:
            break
        # 点下一页
        clicked = await page.evaluate('''
() => {
    const a = Array.from(document.querySelectorAll('a')).find(x => (x.textContent||'').trim() === '下一页');
    if (a) { a.click(); return true; }
    return false;
}
        ''')
        if not clicked:
            break
        await asyncio.sleep(1.5 + random.uniform(0, 0.8))

    # 去重 (按 code)
    seen = set()
    uniq = []
    for r in all_rows:
        if r['code'] and r['code'] not in seen:
            seen.add(r['code'])
            uniq.append(r)
    return uniq, None


def load_universe(conn) -> list:
    """从已有 sector_constituent 表里拿所有 (type, code, name) — 复用 10-07 那批 universe."""
    return [(r[0], r[1], r[2]) for r in conn.execute(
        'SELECT DISTINCT sector_type, sector_code, sector_name FROM sector_constituent ORDER BY sector_type, sector_code'
    ).fetchall()]


async def run(limit=None):
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL_ADD)
    conn.commit()

    universe = load_universe(conn)
    if limit:
        universe = universe[:limit]
    total = len(universe)
    log(f'[init] universe: {total} 个板块')

    UPSERT = (
        'INSERT INTO sector_constituent '
        '(sector_code, sector_type, sector_name, stock_code, stock_name, '
        ' sub_industry, market, close, change_pct, amount, turnover_pct, pe_ratio, in_date) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?, date(\'now\')) '
        'ON CONFLICT(sector_code, stock_code) DO UPDATE SET '
        'sector_name=excluded.sector_name, '
        'sector_type=excluded.sector_type, '
        'stock_name=excluded.stock_name, '
        'close=excluded.close, '
        'change_pct=excluded.change_pct, '
        'in_date=excluded.in_date'
    )

    async with BrowserSession() as browser:
        page = browser._page
        t0 = time.time()
        ok_n = 0
        fail_n = 0
        ins_n = 0
        for i, (stype, code, name) in enumerate(universe, 1):
            rows, err = await fetch_one_sector(page, code, name, stype)
            if err:
                fail_n += 1
                log(f'[{i:4d}/{total}] {stype} {code} {name:<10} ✗ {err}')
                await asyncio.sleep(3 + random.uniform(0, 2))
                continue
            if not rows:
                fail_n += 1
                log(f'[{i:4d}/{total}] {stype} {code} {name:<10} — 0 行')
                await asyncio.sleep(2)
                continue
            # 删旧成员再插 (保证删除已退出的成分股)
            new_codes = set(r['code'] for r in rows)
            placeholders = ','.join('?' * len(new_codes))
            conn.execute(
                f'DELETE FROM sector_constituent WHERE sector_code=? '
                f'AND stock_code NOT IN ({placeholders})',
                [code] + list(new_codes)
            )
            for r in rows:
                conn.execute(UPSERT, (
                    code, stype, name, r['code'], r['name'],
                    None, None, _to_float(r['close']), _to_float(r['change_pct']),
                    None, None, None,
                ))
            conn.commit()
            ok_n += 1
            ins_n += len(rows)
            # 每板块必打 OK 行 (避免 i%20 门限漏掉中间成功)
            elapsed = time.time() - t0
            rate = elapsed / i
            eta = (total - i) * rate
            log(f'[{i:4d}/{total}] {stype} {code} {name:<10} ✓ {len(rows)} 行 | '
                f'ok={ok_n} fail={fail_n} ins={ins_n} rate={rate:.1f}s/板块 ETA {eta/60:.1f}min')
            await asyncio.sleep(2 + random.uniform(0, 1.5))

    conn.close()
    log(f'完成: ok={ok_n} fail={fail_n} 入库 {ins_n} 行, 耗时 {(time.time()-t0)/60:.1f}min')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, help='只跑前 N 个板块 (调试)')
    parser.add_argument('--code', help='指定单个 sector_code (如 BK1106) 验证翻页')
    args = parser.parse_args()
    if args.code:
        asyncio.run(run_one(args.code))
    else:
        asyncio.run(run(limit=args.limit))


async def run_one(code: str):
    """指定单个 sector 跑, 用于翻页验证."""
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')
    row = conn.execute(
        'SELECT DISTINCT sector_type, sector_name FROM sector_constituent WHERE sector_code=?',
        (code,)
    ).fetchone()
    if not row:
        print(f'ERROR: sector_code={code} 不在 sector_constituent 表里', flush=True)
        return
    stype, name = row
    print(f'[init] 验证 {code} {name} ({stype})', flush=True)

    async with BrowserSession() as browser:
        page = browser._page
        t0 = time.time()
        rows, err = await fetch_one_sector(page, code, name, stype)
        elapsed = time.time() - t0
        if err:
            print(f'✗ {err}', flush=True)
            return
        print(f'✓ 拿到 {len(rows)} 行, 耗时 {elapsed:.1f}s', flush=True)
        if rows:
            print(f'前 5: {rows[:5]}', flush=True)
            print(f'后 5: {rows[-5:]}', flush=True)
    conn.close()


if __name__ == '__main__':
    main()
