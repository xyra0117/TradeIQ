"""板块成分股 (走 push2delay.eastmoney.com, 反爬宽松).

跟 html 版区别:
  - html 版用 Playwright evaluate DOM, IP 被盯全 0 行
  - 改走 push2delay.eastmoney.com/api/qt/clist/get, 镜像 EM 自己的延迟 API,
    返回数据跟 push2 一致 (BK0566 都 21 行, BK1106 都 214 行)
  - subprocess 调 curl (跟 backfill_rzrq_stock 同款, EM 对 curl 比 Python urllib 宽松)
  - 每个 sector sleep 2-3s

为啥 push2delay 存在: EM 把 push2.eastmoney.com 限流后, 页面 JS 自动跳到
push2delay.eastmoney.com (响应头 302 Location), 是 EM 自家的延迟镜像, 反爬宽松.
"""
import argparse
import datetime as dt
import json
import os
import random
import sqlite3
import subprocess
import sys
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')

HOST = 'https://push2delay.eastmoney.com/api/qt/clist/get'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'

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

MKT_MAP = {0: 'SZ', 1: 'SH', 2: 'BJ'}


def log(msg):
    sys.stdout.write(f'[{dt.datetime.now():%H:%M:%S}] {msg}\n')
    sys.stdout.flush()


def _f(v):
    if v is None or v == '' or v == '-':
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (ValueError, TypeError):
        return None


def fetch_page(code: str, pn: int) -> tuple:
    """单页 (pn) — subprocess 调 curl. 3 次重试 2/4/8s.
    走 push2delay (push2.eastmoney.com 备用镜像, 反爬宽松).
    """
    url = (f'{HOST}?pn={pn}&pz=100&po=1&np=1&ut=8dec03ba335b81bf4ebdf7b29ec27d15'
           f'&fltt=2&invt=2&fid=f12&fs=b:{code}'
           f'&fields=f1,f2,f3,f6,f8,f9,f12,f14,f20,f100')
    last_err = None
    for attempt in range(1, 4):
        try:
            res = subprocess.run([
                'curl', '-s', '--noproxy', '*',
                '--max-time', '20',
                '-A', UA,
                '-H', f'Referer: https://data.eastmoney.com/bkzj/{code}.html',
                url,
            ], capture_output=True, text=True, timeout=25)
            if res.returncode != 0:
                raise RuntimeError(f'curl exit {res.returncode}: {res.stderr[:80]}')
            j = json.loads(res.stdout)
            data = j.get('data') or {}
            diff = data.get('diff') or []
            total = data.get('total') or 0
            rows = []
            for r in diff:
                rows.append({
                    'code': r.get('f12', ''),
                    'name': r.get('f14', ''),
                    'market': MKT_MAP.get(r.get('f1'), ''),
                    'close': (_f(r.get('f2')) or 0) / 100,
                    'change_pct': (_f(r.get('f3')) or 0) / 100,
                    'amount': _f(r.get('f6')) or 0,
                    'turnover_pct': (_f(r.get('f8')) or 0) / 100,
                    'pe_ratio': _f(r.get('f9')),
                    'sub_industry': r.get('f100', ''),
                })
            return rows, total, None
        except Exception as e:
            last_err = f'{type(e).__name__}: {str(e)[:120]}'
            if attempt < 3:
                time.sleep(2 ** attempt + random.uniform(0, 0.5))
    return [], 0, last_err


def fetch_one_sector(code: str) -> tuple:
    """翻页拿全 sector 成分股. 终止: rows 为空 / 本页不满 100 / MAX_PAGES."""
    MAX_PAGES = 50
    all_rows = []
    for pn in range(1, MAX_PAGES + 1):
        rows, total, err = fetch_page(code, pn)
        if err:
            return all_rows, err
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 100:
            break
    return all_rows, None


def load_universe(conn) -> list:
    return [(r[0], r[1], r[2]) for r in conn.execute(
        'SELECT DISTINCT sector_type, sector_code, sector_name FROM sector_constituent ORDER BY sector_type, sector_code'
    ).fetchall()]


def run(limit=None, skip_existing=False):
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.execute('PRAGMA busy_timeout = 60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(DDL_ADD)
    conn.commit()

    universe = load_universe(conn)
    if skip_existing:
        today_done = {r[0] for r in conn.execute(
            "SELECT DISTINCT sector_code FROM sector_constituent WHERE in_date = date('now')"
        ).fetchall()}
        before = len(universe)
        universe = [s for s in universe if s[1] not in today_done]
        log(f'[skip-existing] 跳过 {before - len(universe)} 个今天已写入的, 剩 {len(universe)} 个待跑')
    if limit:
        universe = universe[:limit]
    total = len(universe)
    log(f'[init] universe: {total} 个板块, host=push2delay.eastmoney.com (subprocess curl)')

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

    ok_n = 0
    fail_n = 0
    ins_n = 0
    fail_list = []
    consec_fail = 0
    t0 = time.time()

    for i, (stype, code, name) in enumerate(universe, 1):
        rows, err = fetch_one_sector(code)
        if err:
            fail_n += 1
            fail_list.append((code, name, err))
            consec_fail += 1
            log(f'[{i:4d}/{total}] {stype} {code} {name:<10} ✗ {err}')
            if consec_fail >= 6:
                log(f'⚠ 连续 {consec_fail} 失败, 歇 60s 让反爬冷却...')
                time.sleep(60)
                consec_fail = 0
            time.sleep(3 + random.uniform(0, 1.5))
            continue
        consec_fail = 0
        if not rows:
            fail_n += 1
            log(f'[{i:4d}/{total}] {stype} {code} {name:<10} — 0 行')
            time.sleep(3)
            continue

        n_before = conn.execute(
            'SELECT COUNT(*) FROM sector_constituent WHERE sector_code=?', (code,)
        ).fetchone()[0]
        new_codes = set(r['code'] for r in rows)
        placeholders = ','.join('?' * len(new_codes))
        for r in rows:
            conn.execute(UPSERT, (
                code, stype, name, r['code'], r['name'],
                r['sub_industry'], r['market'], r['close'], r['change_pct'],
                r['amount'], r['turnover_pct'], r['pe_ratio'],
            ))
        conn.commit()
        if n_before > 0:
            cur = conn.execute(
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
        time.sleep(2 + random.uniform(0, 1.0))

    conn.close()
    log(f'完成: ok={ok_n} fail={fail_n} ins={ins_n} 耗时 {(time.time()-t0)/60:.1f}min')
    if fail_list:
        log(f'失败列表 (前 30):')
        for code, name, err in fail_list[:30]:
            log(f'  {code} {name}: {err}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, help='只跑前 N 个板块 (调试)')
    parser.add_argument('--skip-existing', action='store_true',
                        help='跳过今天 (in_date=today) DB 里已写入的 sector (断点续跑)')
    args = parser.parse_args()
    run(limit=args.limit, skip_existing=args.skip_existing)


if __name__ == '__main__':
    main()
