"""
Dry-run: 用东财 10 日排行反推 9月1日 缺失的主力净流入数据。

公式: daily_0901 = 10日累计(9/14 截止) - Σ(daily for 9/2..9/14)
输出: backend/previews/reverse_20260901_dryrun.csv
不入库.

数据源: push2.eastmoney.com clist/get (detail.html 10日排行 tab)
排序:  fid=f185 (10日主力净流入占比)
字段:  f174 10日主力净流入净额, f175 10日主力净流入净占比,
      f176-f183 10日超大/大/中/小单净额+占比
      f12 代码, f14 名称
"""
import csv
import math
import os
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------- 路径 ----------
HERE = Path(__file__).parent
DB_PATH = str(HERE / 'market_data.db')
PREVIEW_DIR = HERE / 'previews'
OUTPUT_CSV = PREVIEW_DIR / 'reverse_20260901_dryrun.csv'
CACHE_10D = PREVIEW_DIR / 'eastmoney_10day_ranking.json'  # 10日排行 raw rows 缓存, 避免反复 hit push2

# ---------- 10 日窗口 ----------
# 9/14 (今天) 是 10 日排行的截止日。回推 9 个交易日 = 9/2..9/14
END_DATE = '20260914'
MISSING_DATE = '20260901'
KNOWN_DATES = ['20260902', '20260903', '20260904', '20260907', '20260908',
               '20260909', '20260910', '20260911', '20260914']

# ---------- push2 URL (10日排行, fid=f185) ----------
# fields: f12/f14 代码/名称, f174 10日主力净流入净额, f175 10日占比,
#         f176/f177 10日超大单, f178/f179 10日大单,
#         f180/f181 10日中单, f182/f183 10日小单
PUSH2_URL_BASE_10D = (
    "https://push2.eastmoney.com/api/qt/clist/get?"
    "fid=f185&po=1&pz=100&np=1&fltt=2&invt=2"
    "&ut=8dec03ba335b81bf4ebdf7b29ec27d15"
    "&fs=m%3A0%2Bt%3A6%2Bf%3A%212%2Cm%3A0%2Bt%3A13%2Bf%3A%212%2Cm%3A0%2Bt%3A80%2Bf%3A%212%2Cm%3A1%2Bt%3A2%2Bf%3A%212%2Cm%3A1%2Bt%3A23%2Bf%3A%212%2Cm%3A0%2Bt%3A7%2Bf%3A%212%2Cm%3A1%2Bt%3A3%2Bf%3A%212"
    "&fields=f12%2Cf14%2Cf174%2Cf175%2Cf176%2Cf177%2Cf178%2Cf179%2Cf180%2Cf181%2Cf182%2Cf183"
)

PAGE_URL = "https://data.eastmoney.com/zjlx/detail.html"
WAIT_MS_PER_PAGE = 1500


def _f(v):
    """push2 返回 '-' 表缺失, 转 None; 否则转 float."""
    if v in ('-', '', None):
        return None
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (ValueError, TypeError):
        return None


def _to_ts_code(code: str) -> str:
    """6xxxxx/9xxxxx → .SH; 0/3/2xxxxx → .SZ. 跟 app.py _to_ts_code 一致."""
    code = str(code).strip()
    if not code or len(code) != 6:
        return f'{code}.SH' if code else ''
    return f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ'


def _jsonp_eval_js(url_base: str) -> str:
    """单次 JSONP 请求的 JS 模板."""
    return """
    (pn) => {
        return new Promise((resolve) => {
            const cb = 'jjsonp_10d_' + Date.now() + '_' + pn;
            window[cb] = (data) => {
                try { delete window[cb]; } catch(e) { window[cb] = undefined; }
                if (script.parentNode) script.parentNode.removeChild(script);
                resolve({ok: true, data: data});
            };
            const script = document.createElement('script');
            script.src = '%s' + '&pn=' + pn + '&cb=' + cb;
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
        });
    }
    """ % url_base


def fetch_10day_ranking(headless: bool = False, wait_for_user: bool = True,
                        wait_timeout_sec: int = 600):
    """JSONP 拉 detail.html 10日排行所有页.

    headless=False + wait_for_user=True: 弹出浏览器, 第 1 页失败时轮询等用户 Cmd+R
    手动刷新触发 push2 响应（绕本机 IP 限流, 跟 fetch_today_market 同款模式).
    """
    import time as _time
    from datetime import datetime

    all_rows = []
    failed = []
    js_template = _jsonp_eval_js(PUSH2_URL_BASE_10D)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="chrome")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        print("[10d] 打开 detail.html 建立 session...")
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        print("[10d] 拉第 1 页...")
        r = page.evaluate(js_template, 1)
        data = r.get("data") if r and r.get("ok") else None

        # 第 1 页失败 → 等用户手动刷新
        if (not data or not data.get("data")) and wait_for_user:
            print(f"[{datetime.now():%H:%M:%S}] 第 1 页失败, 进入等待用户刷新模式 (最长 {wait_timeout_sec}s)...")
            print("  → 浏览器已弹出 detail.html, 在浏览器里按 Cmd+R 刷新触发 push2 响应")
            deadline = _time.time() + wait_timeout_sec
            poll_count = 0
            while _time.time() < deadline:
                _time.sleep(5)
                poll_count += 1
                if poll_count % 2 == 0:
                    remaining = int(deadline - _time.time())
                    print(f"[{datetime.now():%H:%M:%S}] 轮询第 {poll_count} 次 (剩 {remaining}s)...")
                r = page.evaluate(js_template, 1)
                data = r.get("data") if r and r.get("ok") else None
                if data and data.get("data"):
                    print(f"[{datetime.now():%H:%M:%S}] ✅ 第 {poll_count} 次轮询拿到数据")
                    break
            else:
                print(f"FAIL: 等待用户刷新 {wait_timeout_sec}s 超时", file=sys.stderr)
                browser.close()
                return [], list(range(1, 54))

        if not data or not data.get("data"):
            print("[10d] FAIL: 第 1 页拿不到数据", file=sys.stderr)
            browser.close()
            return [], list(range(1, 54))

        total = data["data"].get("total", 0)
        diff = data["data"].get("diff") or []
        all_rows.extend(diff)
        print(f"  total={total}, 第 1 页 {len(diff)} 行")

        if total == 0:
            browser.close()
            return [], list(range(2, 54))

        total_pages = math.ceil(total / 100)
        print(f"  总页数: {total_pages}（{total} 只）")

        for pn in range(2, total_pages + 1):
            page.wait_for_timeout(WAIT_MS_PER_PAGE)
            r = page.evaluate(js_template, pn)
            data = r.get("data") if r and r.get("ok") else None
            if not data or not data.get("data") or not data["data"].get("diff"):
                failed.append(pn)
                continue
            all_rows.extend(data["data"]["diff"])
            if pn % 10 == 0 or pn == total_pages:
                print(f"  pn={pn}/{total_pages}, 累计 {len(all_rows)} 行")

        browser.close()

    print(f"\n[10d] 抓取完成: {len(all_rows)} 行, 失败页: {len(failed)}")
    return all_rows, failed


def load_known_daily():
    """从 DB 拉 9/2..9/14 每只股每日 main / super / big / mid / small 净额.

    返回: { ts_code: { 'main_sum', 'super_sum', 'big_sum', 'mid_sum', 'small_sum',
                       'days', 'has_zero' } }
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    placeholders = ','.join('?' * len(KNOWN_DATES))
    rows = conn.execute(f"""
        SELECT ts_code, trade_date, main_net_inflow, super_net, big_net, mid_net, small_net
        FROM fund_flow
        WHERE trade_date IN ({placeholders})
          AND source = 'eastmoney-push2'
    """, KNOWN_DATES).fetchall()
    conn.close()

    agg = {}
    for ts_code, td, main, sup, big, mid, small in rows:
        if not ts_code:
            continue
        rec = agg.setdefault(ts_code, {
            'main_sum': 0.0, 'super_sum': 0.0, 'big_sum': 0.0,
            'mid_sum': 0.0, 'small_sum': 0.0,
            'days': 0, 'has_zero': False,
        })
        if main is not None:
            rec['main_sum'] += main
            rec['days'] += 1
            if main == 0:
                rec['has_zero'] = True
        if sup is not None:
            rec['super_sum'] += sup
        if big is not None:
            rec['big_sum'] += big
        if mid is not None:
            rec['mid_sum'] += mid
        if small is not None:
            rec['small_sum'] += small

    return agg


def load_daily_quotes(trade_date: str) -> dict:
    """从 stock_daily 拉某日收盘价/涨跌额, 算 change_pct (stock_daily 没有 pct 列).

    返回: { ts_code: {'close': x, 'change_pct': y} }
    change_pct = change / (close - change) * 100  (即 change / prev_close * 100)
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT ts_code, close, change FROM stock_daily WHERE trade_date = ?",
        (trade_date,),
    ).fetchall()
    conn.close()
    out = {}
    for ts, close, change in rows:
        pct = None
        if close is not None and change is not None:
            prev = close - change
            if prev and prev != 0:
                pct = change / prev * 100
        out[ts] = {'close': close, 'change_pct': pct}
    return out


def apply_to_db(results: list, miss_date: str = MISSING_DATE):
    """DELETE 9/1 已有行 (任何 source) → INSERT 反推行 (source='eastmoney-push2').

    数据本质是东财 10 日排行推出来的, 跟其他日的 eastmoney-push2 同源同口径,
    共用同一个 source 名, 避免前端/API 走 fallback 兜底.

    反推只有净额 (main/super/big/mid/small), _pct 系列留 NULL (没单日成交额做分母算不出).
    close/change_pct 从 stock_daily 补.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()

    # 备份 + 删
    before = cur.execute(
        "SELECT ts_code, source, main_net_inflow FROM fund_flow WHERE trade_date = ?",
        (miss_date,),
    ).fetchall()
    before_by_source = {}
    for ts, src, m in before:
        before_by_source.setdefault(src, []).append((ts, m))

    print(f"\n[APPLY] 9/1 现有行 (by source):")
    for src, lst in before_by_source.items():
        print(f"  {src}: {len(lst)} 行")

    quotes = load_daily_quotes(miss_date)
    print(f"[APPLY] stock_daily 9/1 收盘价覆盖: {len(quotes)} 只")

    # 准备 INSERT 数据 (只插 high 置信度)
    insertable = [r for r in results if r['confidence'] == 'high']
    print(f"[APPLY] 反推行数 (high 置信度): {len(insertable)}")

    # 事务
    cur.execute("BEGIN")
    try:
        cur.execute("DELETE FROM fund_flow WHERE trade_date = ?", (miss_date,))
        deleted = cur.rowcount

        # 兼容 schema: 列名靠 PRAGMA 拿 (避免硬编码漏列)
        cols_info = cur.execute("PRAGMA table_info(fund_flow)").fetchall()
        col_names = {c[1] for c in cols_info}

        rows_to_insert = []
        skipped_no_close = 0
        for r in insertable:
            q = quotes.get(r['ts_code'], {})
            row = (
                miss_date,
                r['ts_code'],
                r['code'],
                r['name'],
                q.get('close'),
                q.get('change_pct'),
                r['reverse_daily_main'] or 0.0,
                None,  # main_net_pct — 反推拿不到 (没单日成交额)
                r['reverse_daily_super'],
                None,  # super_pct
                r['reverse_daily_big'],
                None,  # big_pct
                r['reverse_daily_mid'],
                None,  # mid_pct
                r['reverse_daily_small'],
                None,  # small_pct
                'eastmoney-push2',  # 跟其他日同源, 不走 reverse-from-10d 标识
            )
            # 实际写入只取 col_names 里有的列 (兼容老 schema)
            full = {
                'trade_date': row[0], 'ts_code': row[1], 'code': row[2],
                'name': row[3], 'close': row[4], 'change_pct': row[5],
                'main_net_inflow': row[6], 'main_net_pct': row[7],
                'super_net': row[8], 'super_pct': row[9],
                'big_net': row[10], 'big_pct': row[11],
                'mid_net': row[12], 'mid_pct': row[13],
                'small_net': row[14], 'small_pct': row[15],
                'source': row[16],
            }
            ordered_cols = [c for c in full.keys() if c in col_names]
            rows_to_insert.append(tuple(full[c] for c in ordered_cols))
            if q.get('close') is None:
                skipped_no_close += 1

        # 动态拼 INSERT (按实际 schema 列)
        placeholders_sql = ','.join('?' * len(ordered_cols))
        cols_sql = ','.join(ordered_cols)
        cur.executemany(
            f"INSERT INTO fund_flow ({cols_sql}) VALUES ({placeholders_sql})",
            rows_to_insert,
        )
        inserted = cur.rowcount
        conn.commit()

        print(f"\n[APPLY] ✅ 写入完成")
        print(f"  删除: {deleted} 行")
        print(f"  插入: {inserted} 行 (source=eastmoney-push2, 跟其他日同源)")
        print(f"  无收盘价 (close=NULL): {skipped_no_close} 行")
        return {'deleted': deleted, 'inserted': inserted, 'skipped_no_close': skipped_no_close}
    except Exception as e:
        conn.rollback()
        print(f"\n[APPLY] ❌ 失败, 回滚: {e}", file=sys.stderr)
        raise


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='真写 DB: DELETE 9/1 已有 → INSERT 反推行 (默认 dry-run)')
    args = ap.parse_args()

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 抓 10 日排行 (弹出浏览器, push2 限流时等用户 Cmd+R 刷新触发响应)
    import json
    from datetime import date
    today_str = date.today().isoformat()
    raw_rows = []
    if CACHE_10D.exists():
        try:
            cache = json.loads(CACHE_10D.read_text(encoding='utf-8'))
            if cache.get('fetched_at', '').startswith(today_str):
                raw_rows = cache.get('rows', [])
                print(f"[CACHE] 用今日缓存: {CACHE_10D.name} ({len(raw_rows)} 行)")
        except Exception as e:
            print(f"[CACHE] 读缓存失败, 重抓: {e}")

    if not raw_rows:
        raw_rows, failed = fetch_10day_ranking(headless=False, wait_for_user=True)
        if failed and not raw_rows:
            print("ABORT: 抓取全部失败, 请检查 eastmoney 接口", file=sys.stderr)
            sys.exit(1)
        # 落缓存
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_10D.write_text(
            json.dumps({'fetched_at': today_str, 'rows': raw_rows}, ensure_ascii=False),
            encoding='utf-8',
        )
        print(f"[CACHE] 写入: {CACHE_10D.name}")

    # 2) DB 里 9/2..9/14 已知每日
    known = load_known_daily()
    print(f"\n[DB] 9/2..9/14 已知股数: {len(known)}")

    # 3) 对每只股反推 9/1 当日值
    results = []
    skipped_no_known = 0
    skipped_negative = 0
    matched_existing = 0

    for r in raw_rows:
        code = str(r.get('f12', '')).strip()
        if not code or len(code) != 6:
            continue
        ts_code = _to_ts_code(code)
        name = r.get('f14', '')

        cum_main = _f(r.get('f174'))
        cum_super = _f(r.get('f176'))
        cum_big = _f(r.get('f178'))
        cum_mid = _f(r.get('f180'))
        cum_small = _f(r.get('f182'))
        cum_pct = _f(r.get('f175'))

        if cum_main is None:
            continue

        k = known.get(ts_code)
        if not k or k['days'] < len(KNOWN_DATES) - 1:
            # 9/2..9/14 缺一天以上, 反推不准 (差一天就误差一天的钱)
            skipped_no_known += 1
            daily_main = None
            daily_super = None
            daily_big = None
            daily_mid = None
            daily_small = None
            confidence = 'low'
        else:
            daily_main = cum_main - k['main_sum']
            daily_super = cum_super - k['super_sum'] if cum_super is not None else None
            daily_big = cum_big - k['big_sum'] if cum_big is not None else None
            daily_mid = cum_mid - k['mid_sum'] if cum_mid is not None else None
            daily_small = cum_small - k['small_sum'] if cum_small is not None else None
            confidence = 'high' if k['days'] == len(KNOWN_DATES) else 'medium'

            # 校验: daily_main = daily_super + daily_big
            if daily_super is not None and daily_big is not None:
                expected = daily_super + daily_big
                if abs(daily_main - expected) > 1:  # 容差 1 元 (四舍五入)
                    confidence = 'mismatch'

            if daily_main < 0:
                skipped_negative += 1

        results.append({
            'ts_code': ts_code,
            'code': code,
            'name': name,
            'cum_main_10d': cum_main,
            'cum_super_10d': cum_super,
            'cum_big_10d': cum_big,
            'cum_pct_10d': cum_pct,
            'db_main_sum_9_2_to_9_14': k['main_sum'] if k else None,
            'db_days': k['days'] if k else 0,
            'reverse_daily_main': daily_main,
            'reverse_daily_super': daily_super,
            'reverse_daily_big': daily_big,
            'reverse_daily_mid': daily_mid,
            'reverse_daily_small': daily_small,
            'reverse_check_super_big_sum': (
                daily_super + daily_big
                if (daily_super is not None and daily_big is not None) else None
            ),
            'reverse_check_diff': (
                daily_main - (daily_super + daily_big)
                if (daily_main is not None and daily_super is not None and daily_big is not None)
                else None
            ),
            'confidence': confidence,
        })

    # 4) 输出 CSV
    fieldnames = [
        'ts_code', 'code', 'name',
        'cum_main_10d', 'cum_super_10d', 'cum_big_10d', 'cum_pct_10d',
        'db_main_sum_9_2_to_9_14', 'db_days',
        'reverse_daily_main', 'reverse_daily_super', 'reverse_daily_big',
        'reverse_daily_mid', 'reverse_daily_small',
        'reverse_check_super_big_sum', 'reverse_check_diff',
        'confidence',
    ]
    # 按 reverse_daily_main 降序, None 排最后
    results.sort(
        key=lambda x: (x['reverse_daily_main'] is None, -(x['reverse_daily_main'] or 0))
    )
    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow(r)

    print(f"\n{'='*60}")
    print(f"[dry-run] 输出: {OUTPUT_CSV}")
    print(f"  10 日排行总股数: {len(results)}")
    print(f"  高置信度 (9 天齐全): {sum(1 for r in results if r['confidence']=='high')}")
    print(f"  中置信度 (缺 1 天): {sum(1 for r in results if r['confidence']=='medium')}")
    print(f"  低置信度 (缺 ≥2 天): {sum(1 for r in results if r['confidence']=='low')}")
    print(f"  主超+大对账不符: {sum(1 for r in results if r['confidence']=='mismatch')}")
    print(f"  反推为负值: {skipped_negative}")
    print(f"\n[对比 DB 9/1 已有 500 行]")
    conn = sqlite3.connect(DB_PATH, timeout=5)
    existing = conn.execute(
        "SELECT ts_code, main_net_inflow, super_net, big_net FROM fund_flow WHERE trade_date = ?",
        (MISSING_DATE,)
    ).fetchall()
    conn.close()
    existing_map = {r[0]: r for r in existing}
    matched = 0
    big_diff = 0
    diff_list = []
    for r in results:
        if r['reverse_daily_main'] is None:
            continue
        ex = existing_map.get(r['ts_code'])
        if not ex:
            continue
        matched += 1
        delta = r['reverse_daily_main'] - (ex[1] or 0)
        if abs(delta) > 1e8:  # 1 亿差异
            big_diff += 1
            diff_list.append((r['ts_code'], r['name'], ex[1], r['reverse_daily_main'], delta))
    print(f"  已有行匹配: {matched}/{len(existing)}")
    print(f"  差异 > 1 亿: {big_diff}")
    if diff_list:
        print(f"\n  差异最大 Top 10:")
        diff_list.sort(key=lambda x: abs(x[4]), reverse=True)
        for ts, n, old, new, d in diff_list[:10]:
            sign = '+' if d > 0 else ''
            print(f"    {ts} {n}:  DB 9/1={old/1e8:.3f}亿  →  反推={new/1e8:.3f}亿  ({sign}{d/1e8:.3f}亿)")

    if args.apply:
        print(f"\n{'='*60}")
        print(f"[APPLY] 用户传 --apply, 开始写库")
        apply_to_db(results)
    else:
        print(f"\n[DRY-RUN] 未传 --apply, 不写库")


if __name__ == '__main__':
    main()
