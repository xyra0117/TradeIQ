"""一次性脚本:全量拉所有概念+行业板块的成分股,落 sector_constituent 表 (覆盖式).

数据源 (push2):
  https://push2.eastmoney.com/api/qt/clist/get?fs=b:BK1106 + fields=f12,f14,f1,f2,f3,f6,f8,f9,f20,f100
  所有数值字段都 ×100 整数化 (close×100=分, chg×100=整数%, amount×100=整数元, turn×100=整数%)

为啥用 BrowserSession + page.goto (不 curl 不用 fetch_text):
  v16 同款经验: push2his 反爬盯 curl/requests 的 TLS 指纹 (JA3), 真 chromium page.goto
  走 BoringSSL + HTTP/2 + 浏览器 cookie 池能过. fetch_text 的 JSON regex 抓 raw JSON 不稳,
  改用 page.goto + response.body() 直接读 API 响应体.

规模: 1000 sector × ~50-100 成分股 ≈ 6-10 万行,带翻页.
用法: python3 fetch_sector_constituents.py [--limit N]
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import random
import re
import sqlite3
import sys
import time

from playwright.async_api import async_playwright

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')

DDL = """
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
CREATE INDEX IF NOT EXISTS idx_sc_stock ON sector_constituent(stock_code);
CREATE INDEX IF NOT EXISTS idx_sc_type  ON sector_constituent(sector_type);
CREATE INDEX IF NOT EXISTS idx_sc_sub   ON sector_constituent(sub_industry);
"""

MKT_MAP = {0: 'SZ', 1: 'SH', 2: 'BJ'}

# push2 clist/get 参数: pz=100 是上限, fid=f12 按股票代码排序保证翻页连续
URL_TPL = (
    'https://push2.eastmoney.com/api/qt/clist/get?'
    'pn={pn}&pz=100&po=1&np=1&ut=8dec03ba335b81bf4ebdf7b29ec27d15'
    '&fltt=2&invt=2&fid=f12'
    '&fs=b:{code}'
    '&fields=f1,f2,f3,f6,f8,f9,f12,f14,f20,f100'
)
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# 预热页: 跟 v16 同款
WARM_PAGES = [
    'https://quote.eastmoney.com/center/gridlist.html#hs_a_board',
    'https://data.eastmoney.com/bkzj/BK0438.html',
]


def log(msg):
    print(f'[{dt.datetime.now():%H:%M:%S}] {msg}', flush=True)


def _f(v):
    if v is None or v == '' or v == '-':
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (ValueError, TypeError):
        return None


def load_universe(conn):
    """概念 + 行业板块 universe."""
    from_theme = {r[0]: r[1] for r in conn.execute(
        "SELECT em_code, em_name FROM theme_concept "
        "WHERE em_code IS NOT NULL AND em_code != ''").fetchall()}
    from_data = {r[0]: r[1] for r in conn.execute(
        "SELECT DISTINCT sector_code, name FROM sector_fund_flow "
        "WHERE sector_type='concept'").fetchall()}
    concept = {}
    for code in set(from_theme) | set(from_data):
        concept[code] = from_theme.get(code) or from_data.get(code) or ''
    concept_list = [(c, n) for c, n in sorted(concept.items())]

    industry = [(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT sector_code, name FROM sector_fund_flow "
        "WHERE sector_type='industry' AND name IS NOT NULL AND name != ''").fetchall()]
    industry_list = sorted(set(industry))

    log(f'universe: concept {len(concept_list)} 个, industry {len(industry_list)} 个')
    return [('concept', c, n) for c, n in concept_list] + \
           [('industry', c, n) for c, n in industry_list]


class BrowserClient:
    """轻量 BrowserSession 替代: 真 chromium + 预热 + page.goto + 拿 response body."""

    def __init__(self, verbose=True):
        self.verbose = verbose
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._consec_fail = 0

    def log(self, msg):
        if self.verbose:
            print(f'[{dt.datetime.now():%H:%M:%S}] {msg}', flush=True)

    async def start(self):
        self.log('启动 playwright 浏览器...')
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._ctx = await self._browser.new_context(
            user_agent=UA,
            locale='zh-CN',
            viewport={'width': 1440, 'height': 900},
            ignore_https_errors=True,
        )
        # stealth 屏蔽 headless 特征 (跟 utils_browser 同款)
        await self._ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
        """)
        # 屏蔽图片/字体加速
        async def _route(route, request):
            if request.resource_type in ('image', 'media', 'font'):
                await route.abort()
            else:
                await route.continue_()
        await self._ctx.route('**/*', _route)

        self._page = await self._ctx.new_page()
        self.log('预热: 访问真页面攒 cookie + 浏览历史...')
        for url in WARM_PAGES:
            try:
                t0 = time.time()
                await self._page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await asyncio.sleep(random.uniform(2.5, 4.0))
                await self._page.mouse.move(random.randint(200, 1000), random.randint(200, 600))
                self.log(f'  ✓ {url[:70]} ({time.time()-t0:.1f}s)')
            except Exception as e:
                self.log(f'  ⚠ {url[:70]}: {type(e).__name__}: {str(e)[:80]}')
        cookies = await self._ctx.cookies()
        self.log(f'预热完成: cookie {len(cookies)} 条')

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def fetch_page(self, code, pn):
        """page.goto + response.body() 拿原始 JSON body."""
        url = URL_TPL.format(code=code, pn=pn)
        last_err = None
        for attempt in range(1, 4):
            try:
                resp = await self._page.goto(
                    url, wait_until='domcontentloaded',
                    timeout=30000,
                )
                status = resp.status if resp else 0
                if status != 200:
                    raise RuntimeError(f'HTTP {status}')
                body = await resp.body()
                text = body.decode('utf-8', errors='ignore')
                j = json.loads(text)
                data = j.get('data') or {}
                diff = data.get('diff') or []
                total = data.get('total') or 0
                rows = []
                for r in diff:
                    market_id = r.get('f1')
                    rows.append({
                        'code': r.get('f12', ''),
                        'name': r.get('f14', ''),
                        'market': MKT_MAP.get(market_id, ''),
                        'close':       (_f(r.get('f2')) or 0) / 100,
                        'change_pct':  (_f(r.get('f3')) or 0) / 100,
                        'amount':      _f(r.get('f6')) or 0,
                        'turnover_pct':(_f(r.get('f8')) or 0) / 100,
                        'pe_ratio':    _f(r.get('f9')),
                        'sub_industry':r.get('f100', ''),
                    })
                self._consec_fail = 0
                return rows, total, None
            except Exception as e:
                last_err = f'{type(e).__name__}: {str(e)[:120]}'
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt + random.uniform(0, 1.0))
        self._consec_fail += 1
        return [], 0, last_err


async def fetch_constituents(client, code):
    """翻页拿全 sector 成分股.

    ⚠ 不依赖 total 字段 (EM 接口对部分板块 total 报少, 例如 BK1106 报 100 实际 214).
       终止条件: rows 为空, 或本页不满 pz, 或达硬上限.
       硬上限 MAX_PAGES 防止 API 抽风 (total=0) 死循环.
    """
    MAX_PAGES = 50  # 5000 成员 / 100 页容 = 50 页 = 任何板块都够, 死循环兜底
    all_rows = []
    pn = 1
    last_page_rows = 0
    while pn <= MAX_PAGES:
        rows, total, err = await client.fetch_page(code, pn)
        if err:
            return all_rows, err
        if not rows:
            break
        all_rows.extend(rows)
        last_page_rows = len(rows)
        # 不满页 = 翻到底了 (最可靠的终止信号)
        if last_page_rows < 100:
            break
        pn += 1
    return all_rows, None


async def run(limit=None):
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL)
    conn.commit()

    universe = load_universe(conn)
    if limit:
        universe = universe[:limit]
    total = len(universe)

    UPSERT = (
        'INSERT INTO sector_constituent '
        '(sector_code, sector_type, sector_name, stock_code, stock_name, '
        ' sub_industry, market, close, change_pct, amount, turnover_pct, pe_ratio, in_date) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?, date(\'now\')) '
        'ON CONFLICT(sector_code, stock_code) DO UPDATE SET '
        'sector_name=excluded.sector_name, '
        'sector_type=excluded.sector_type, '
        'stock_name=excluded.stock_name, '
        'sub_industry=excluded.sub_industry, '
        'market=excluded.market, '
        'close=excluded.close, '
        'change_pct=excluded.change_pct, '
        'amount=excluded.amount, '
        'turnover_pct=excluded.turnover_pct, '
        'pe_ratio=excluded.pe_ratio, '
        'in_date=excluded.in_date'
    )

    client = BrowserClient(verbose=True)
    await client.start()

    inserted = 0
    deleted_old = 0
    failed = []
    skipped_no_data = 0
    t0_total = time.time()
    consec_long_pause = 0

    try:
        for i, (stype, code, name) in enumerate(universe, 1):
            t0 = time.time()
            rows, err = await fetch_constituents(client, code)
            elapsed = time.time() - t0
            if err:
                failed.append((code, name, err))
                consec_long_pause += 1
                log(f'[{i:4d}/{total}] {stype} {code} {name:<10} ✗ {err} ({elapsed:.1f}s)')
                if consec_long_pause >= 8:
                    log(f'⚠ 连续 {consec_long_pause} 个失败, 长歇 5 分钟让反爬冷却...')
                    await asyncio.sleep(5 * 60 + random.uniform(0, 30))
                    consec_long_pause = 0
                    log('⚠ 歇完继续')
                await asyncio.sleep(2 + random.uniform(0, 1.5))
                continue
            consec_long_pause = 0
            if not rows:
                skipped_no_data += 1
                log(f'[{i:4d}/{total}] {stype} {code} {name:<10} — 0 行 ({elapsed:.1f}s)')
                await asyncio.sleep(2 + random.uniform(0, 1.5))
                continue

            n_before = conn.execute(
                'SELECT COUNT(*) FROM sector_constituent WHERE sector_code=?',
                (code,)).fetchone()[0]
            for r in rows:
                conn.execute(UPSERT, (
                    code, stype, name, r['code'], r['name'],
                    r['sub_industry'], r['market'], r['close'], r['change_pct'],
                    r['amount'], r['turnover_pct'], r['pe_ratio'],
                ))
            conn.commit()
            if n_before > 0:
                new_code_set = set(r['code'] for r in rows)
                placeholders = ','.join('?' * len(new_code_set))
                cur = conn.execute(
                    f'DELETE FROM sector_constituent WHERE sector_code=? '
                    f'AND stock_code NOT IN ({placeholders})',
                    [code] + list(new_code_set)
                )
                if cur.rowcount:
                    deleted_old += cur.rowcount
            inserted += len(rows)

            if i % 20 == 0 or i == total:
                rate = (time.time() - t0_total) / i
                eta = (total - i) * rate
                log(f'[{i:4d}/{total}] {stype} {code} {name:<10} ✓ {len(rows)} 行 '
                    f'({elapsed:.1f}s) | 累计 {inserted} 淘汰 {deleted_old} '
                    f'失败 {len(failed)} ETA {eta/60:.1f}min')
            await asyncio.sleep(2 + random.uniform(0, 1.5))
    finally:
        await client.close()

    print()
    log('=======')
    log(f'完成: universe {total} 个 sector, 插入 {inserted} 行, 淘汰旧 {deleted_old}, '
        f'失败 {len(failed)}, 无数据 {skipped_no_data}')
    print(f'耗时: {(time.time()-t0_total)/60:.1f} 分钟')
    if failed:
        log('失败列表 (前 10):')
        for code, name, err in failed[:10]:
            print(f'  {code} {name}: {err}')

    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, help='只跑前 N 个 sector (调试)')
    args = parser.parse_args()
    asyncio.run(run(limit=args.limit))


if __name__ == '__main__':
    main()
