"""接管用户本地 Chrome (CDP) 跑全量 sector 成分股.

为啥这个能过 EM 反爬:
  push2/push2delay/Playwright headless + 临时 profile + OpenSSL JA3 都被盯死.
  用户本地真 Chrome (BoringSSL, 真实 profile, 真实 cookie 池) 不被盯.
  Playwright connect_over_cdp 接管这个 Chrome, 走它的真 cookie 池发请求,
  EM 看不到"自动化"特征.

数据靠 evaluate 读 bkzj 页面的 td 表格 (11 列: 序号/代码/名称/相关/最新价/今日涨跌幅/...).
翻页靠点"下一页"链接. 不走 push2/push2delay API (避免跨域 CORS).
走纯 sync API — connect_over_cdp 是 sync 的, page.goto/evaluate/wait_for_selector 也都
有 sync 版本, 整段不需要 asyncio.
"""
import argparse
import datetime as dt
import os
import random
import sqlite3
import sys
import time

from playwright.sync_api import sync_playwright

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
CDP_URL = 'http://localhost:9222'
PAGE_URL_TPL = 'https://data.eastmoney.com/bkzj/{code}.html'
MAX_PAGES = 30  # 1500 成员上限兜底

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


def log(msg):
    sys.stdout.write(f'[{dt.datetime.now():%H:%M:%S}] {msg}\n')
    sys.stdout.flush()


def _to_float(s):
    if not s or s == '-':
        return None
    try:
        v = float(s.replace(',', '').replace('%', ''))
        return v if v == v else None
    except (ValueError, TypeError):
        return None


def fetch_one_sector(page, code: str, name: str, stype: str) -> tuple:
    """走真 Chrome 渲染 + 翻页读 DOM. 返回 (rows, err)."""
    url = PAGE_URL_TPL.format(code=code)
    try:
        resp = page.goto(url, wait_until='domcontentloaded', timeout=30000)
    except Exception as e:
        return [], f'goto: {type(e).__name__}: {str(e)[:80]}'
    if not resp or resp.status != 200:
        return [], f'HTTP {resp.status if resp else "?"}'

    # 等数据 td 出现 (排除 footer/header 的 td, 必须 6 位数字)
    try:
        page.wait_for_function('''
() => {
    const tds = document.querySelectorAll('td');
    for (const td of tds) {
        if (/^\\d{6}$/.test((td.textContent||'').trim())) return true;
    }
    return false;
}
        ''', timeout=20000)
    except Exception:
        # goto 后首次可能 cookie 没带, 试一次 reload
        try:
            page.reload(wait_until='domcontentloaded', timeout=30000)
            page.wait_for_function('''
() => {
    const tds = document.querySelectorAll('td');
    for (const td of tds) {
        if (/^\\d{6}$/.test((td.textContent||'').trim())) return true;
    }
    return false;
}
            ''', timeout=15000)
        except Exception:
            return [], 'no data <td> even after reload'
    time.sleep(1.5)

    all_rows = []
    for pn in range(1, MAX_PAGES + 1):
        rows = page.evaluate('''
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
            # 翻页后可能没刷新出数据, 试一次 reload
            if pn > 1:
                try:
                    page.reload(wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_function('''
() => {
    const tds = document.querySelectorAll('td');
    for (const td of tds) {
        if (/^\\d{6}$/.test((td.textContent||'').trim())) return true;
    }
    return false;
}
                    ''', timeout=12000)
                    time.sleep(1.0)
                    rows = page.evaluate('''
() => {
    const tds = Array.from(document.querySelectorAll('td')).filter(td => /^\\d{6}$/.test((td.textContent||'').trim()));
    const result = [];
    tds.forEach(td => {
        const tr = td.closest('tr');
        if (!tr) return;
        const cells = Array.from(tr.querySelectorAll('td')).map(c => (c.textContent||'').trim());
        result.push({code: cells[1] || '', name: cells[2] || '', close: cells[4] || '', change_pct: cells[5] || ''});
    });
    return result;
}
                    ''')
                except Exception:
                    pass
            if not rows:
                break
        all_rows.extend(rows)
        if len(rows) < 50:
            break
        clicked = page.evaluate('''
() => {
    const a = Array.from(document.querySelectorAll('a')).find(x => (x.textContent||'').trim() === '下一页');
    if (a) { a.click(); return true; }
    return false;
}
        ''')
        if not clicked:
            break
        time.sleep(2.0 + random.uniform(0, 0.8))

    seen = set()
    uniq = []
    for r in all_rows:
        if r['code'] and r['code'] not in seen:
            seen.add(r['code'])
            uniq.append(r)
    return uniq, None


def load_universe(conn) -> list:
    return [(r[0], r[1], r[2]) for r in conn.execute(
        'SELECT DISTINCT sector_type, sector_code, sector_name FROM sector_constituent ORDER BY sector_type, sector_code'
    ).fetchall()]


def load_today_done(conn) -> set:
    """昨天 + 今天 (in_date >= today-1) 已写入的 sector_code — 断点续跑跳过.

    为啥含昨天: 跨天跑时, 昨天成功的不需要重写 (UPSERT 模式无副作用, 但纯浪费时间).
    """
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT sector_code FROM sector_constituent "
        "WHERE in_date >= date('now', '-1 day')"
    ).fetchall()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, help='只跑前 N 个板块 (调试)')
    parser.add_argument('--skip-existing', action='store_true',
                        help='跳过今天 (in_date=today) DB 里已写入的 sector (断点续跑)')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL_ADD)
    conn.commit()

    universe = load_universe(conn)
    if args.skip_existing:
        today_done = load_today_done(conn)
        before = len(universe)
        universe = [s for s in universe if s[1] not in today_done]
        log(f'[skip-existing] 跳过 {before - len(universe)} 个今天已写入的, 剩 {len(universe)} 个待跑')
    if args.limit:
        universe = universe[:args.limit]
    total = len(universe)
    log(f'[init] universe: {total} 个板块, mode=CDP 接管用户 Chrome (sync API)')

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

    with sync_playwright() as pw:
        log('连接用户 Chrome (CDP localhost:9222)...')
        browsers = [pw.chromium.connect_over_cdp(CDP_URL)]

        FEISHU_KEYWORDS = ('feishu.cn', 'larksuite.com', 'docs.qq.com', 'notion.so',
                           'dingtalk.com', 'wecom.work', 'bytedance.com')

        def pick_page():
            """接管一个**非飞书**的 page (bkzj 优先 — 已有 cookie 池).
            用户飞书/办公 tab 永远不动, 接管的是其他 tab (browsing/工作闲 tab).
            接管后第一个 sector 的 goto 会改 URL — 但不是飞书, 用户不介意.
            """
            bkzj_page = None
            other_page = None
            for ctx in browsers[0].contexts:
                for p in ctx.pages:
                    url = p.url
                    if any(kw in url for kw in FEISHU_KEYWORDS):
                        continue  # 永远跳过飞书/办公
                    if 'bkzj' in url:
                        bkzj_page = p  # 优先 bkzj (有 cookie 池)
                    elif other_page is None:
                        other_page = p  # 退路: 任意非飞书
            if bkzj_page:
                return bkzj_page
            if other_page:
                return other_page
            # 最后才新建隔离 page (用户 Chrome 上只有飞书时)
            return browsers[0].contexts[0].new_page()

        pages = [pick_page()]
        log(f'已接管 page: {pages[0].url[:80]} (用户飞书 tab 跳过)')

        ok_n = 0
        fail_n = 0
        ins_n = 0
        fail_list = []
        consec_fail = 0
        t0 = time.time()

        def reset_session(reason, sleep_s):
            """彻底断 CDP 重连 + 歇 N 秒, 等同于清掉 EM 对该会话的盯防标记.
            重连后用 pick_page() 接管非飞书的 page."""
            log(f'  ↻ 断 CDP 重连 ({reason})')
            try:
                browsers[0].close()
            except Exception:
                pass
            time.sleep(sleep_s)
            browsers[0] = pw.chromium.connect_over_cdp(CDP_URL)
            pages[0] = pick_page()
            log(f'  ↻ 重连后接管 {pages[0].url[:80]}')

        for i, (stype, code, name) in enumerate(universe, 1):
            rows, err = fetch_one_sector(pages[0], code, name, stype)
            if err:
                fail_n += 1
                fail_list.append((code, name, err))
                consec_fail += 1
                log(f'[{i:4d}/{total}] {stype} {code} {name:<10} ✗ {err}')
                if consec_fail >= 2:
                    log(f'⚠ 连续 {consec_fail} 失败, 断 CDP 重连 + 歇 60s...')
                    reset_session(f'consec_fail={consec_fail}', 60)
                    consec_fail = 0
                else:
                    reset_session('单次失败', 10)
                time.sleep(5 + random.uniform(0, 2))
                continue
            consec_fail = 0
            if not rows:
                fail_n += 1
                log(f'[{i:4d}/{total}] {stype} {code} {name:<10} — 0 行')
                reset_session('0 行', 5)
                time.sleep(5)
                continue

            n_before = conn.execute(
                'SELECT COUNT(*) FROM sector_constituent WHERE sector_code=?', (code,)
            ).fetchone()[0]
            new_codes = set(r['code'] for r in rows)
            placeholders = ','.join('?' * len(new_codes))
            for r in rows:
                conn.execute(UPSERT, (
                    code, stype, name, r['code'], r['name'],
                    None, None, _to_float(r['close']), _to_float(r['change_pct']),
                    None, None, None,
                ))
            conn.commit()
            if n_before > 0:
                conn.execute(
                    f'DELETE FROM sector_constituent WHERE sector_code=? '
                    f'AND stock_code NOT IN ({placeholders})',
                    [code] + list(new_codes)
                )
            ok_n += 1
            ins_n += len(rows)
            elapsed = time.time() - t0
            rate = elapsed / i
            eta = (total - i) * rate
            log(f'[{i:4d}/{total}] {stype} {code} {name:<10} ✓ {len(rows)} 行 | '
                f'ok={ok_n} fail={fail_n} ins={ins_n} rate={rate:.1f}s/板块 ETA {eta/60:.1f}min')
            time.sleep(1.0 + random.uniform(0, 1.0))

    conn.close()
    log(f'完成: ok={ok_n} fail={fail_n} ins={ins_n} 耗时 {(time.time()-t0)/60:.1f}min')
    if fail_list:
        log(f'失败列表 (前 20):')
        for code, name, err in fail_list[:20]:
            log(f'  {code} {name}: {err}')


if __name__ == '__main__':
    main()
