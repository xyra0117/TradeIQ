"""盘后批量抓全市场个股分时入库.

用法:
  python3 backend/fetch_stock_minline_daily.py [--date YYYYMMDD] [--batch-size 50] [--sleep 0.05]

scheduler 在每个交易日 15:05 自动调用 run(); 也可 CLI 手动触发.

策略:
  - 取 stock_basic 全量 ~5500 只
  - 分批 (默认 50/批) 抓 sina minline + INSERT OR REPLACE 入库
  - 跳过本地已有 (ts_code, trade_date) 的: 幂等, 盘后重跑不会爆
  - 失败: warn 日志 + 跳过该票, 不阻塞全市场
  - 整体进度: done=N/total, saved=本日新增, skipped=已存在, failed=抓取失败
"""
import argparse
import os
import sys
import time
import sqlite3

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

# 让 from app import _fetch_sina_minline_rows / fetch_sina_quotes / _infer_minline_trade_date / DB_PATH 能跑
import app as _app  # noqa: E402

DB_PATH = _app.DB_PATH
_fetch_sina_minline_rows = _app._fetch_sina_minline_rows
_sina_code_for_ts = _app._sina_code_for_ts
fetch_sina_quotes = _app.fetch_sina_quotes
_infer_minline_trade_date = _app._infer_minline_trade_date


def _list_all_ts_codes():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = conn.execute('SELECT ts_code, name FROM stock_basic').fetchall()
    finally:
        conn.close()
    return rows


def _already_persisted(ts_code, trade_date):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        row = conn.execute(
            'SELECT COUNT(*) FROM stock_minline_bars WHERE ts_code=? AND trade_date=?',
            (ts_code, trade_date)).fetchone()
        return row and row[0] > 0
    finally:
        conn.close()


def _persist_one(ts_code, trade_date, rows, name, prev_close):
    if not rows:
        return 0
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        c = conn.cursor()
        payload = [(ts_code, trade_date, r.get('m'), r.get('p'), r.get('avg_p'),
                    r.get('v') or 0, name, prev_close) for r in rows]
        c.executemany(
            '''INSERT OR REPLACE INTO stock_minline_bars
               (ts_code, trade_date, m, p, avg_p, v, name, prev_close)
               VALUES (?,?,?,?,?,?,?,?)''',
            payload)
        conn.commit()
        return len(payload)
    finally:
        conn.close()


def run(trade_date=None, batch_size=50, sleep_between=0.05, skip_existing=True, verbose=True):
    """盘后跑一次. trade_date 不传 → 用 _infer_minline_trade_date() (盘后跑就是 today).

    返回 dict {total, done, saved_rows, skipped, failed}.
    """
    if trade_date is None:
        trade_date = _infer_minline_trade_date()

    all_codes = _list_all_ts_codes()
    total = len(all_codes)
    if verbose:
        print(f'[fetch_minline] trade_date={trade_date}  全市场 {total} 只, batch={batch_size}, sleep={sleep_between}s', flush=True)

    done = saved_rows = skipped = failed = 0
    t0 = time.time()
    # 缓存 name/pre_close, 50 只一批才走一次 fetch_sina_quotes (复用 fetch_sina_quotes 30s 全局缓存)
    pending_batch = []  # [(ts_code, sina_code, name), ...]
    pending_meta = {}  # ts_code -> (name, prev_close)

    def _flush_batch():
        nonlocal saved_rows, skipped, failed, done
        if not pending_batch:
            return
        ts_codes = [t[0] for t in pending_batch]
        # 一次拿 50 只的 quote (name/prev_close), 复用 fetch_sina_quotes 全局 30s 缓存
        try:
            quotes = fetch_sina_quotes(ts_codes) or {}
        except Exception as e:
            print(f'[fetch_minline] fetch_sina_quotes 批量失败 ({len(ts_codes)} 只): {e}', flush=True)
            quotes = {}
        for ts_code, sina_code, db_name in pending_batch:
            done += 1
            try:
                if skip_existing and _already_persisted(ts_code, trade_date):
                    skipped += 1
                    continue
                rows = _fetch_sina_minline_rows(sina_code)
                if not rows:
                    failed += 1
                    if verbose and failed <= 5:
                        print(f'[fetch_minline] {ts_code} 空数据', flush=True)
                    continue
                q = quotes.get(ts_code) or {}
                # name 优先用 stock_basic, 再用 quote; prev_close 用 quote
                name = db_name or q.get('name') or ''
                prev_close = q.get('prev_close')
                cnt = _persist_one(ts_code, trade_date, rows, name, prev_close)
                saved_rows += cnt
            except Exception as e:
                failed += 1
                if verbose and failed <= 5:
                    print(f'[fetch_minline] {ts_code} 抓取失败: {e}', flush=True)
        pending_batch.clear()
        if verbose and done % 500 == 0:
            elapsed = time.time() - t0
            speed = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / speed if speed > 0 else 0
            print(f'[fetch_minline] 进度 {done}/{total}  saved_rows={saved_rows}  skipped={skipped}  failed={failed}  '
                  f'速度 {speed:.1f}只/s  预计剩余 {eta/60:.1f} 分钟', flush=True)

    for ts_code, db_name in all_codes:
        sina_code = _sina_code_for_ts(ts_code)
        if not sina_code:
            failed += 1
            done += 1
            continue
        pending_batch.append((ts_code, sina_code, db_name))
        if len(pending_batch) >= batch_size:
            _flush_batch()
            if sleep_between > 0:
                time.sleep(sleep_between)

    # 收尾
    _flush_batch()

    elapsed = time.time() - t0
    summary = {
        'total': total, 'done': done, 'saved_rows': saved_rows,
        'skipped': skipped, 'failed': failed,
        'elapsed_sec': round(elapsed, 1),
        'trade_date': trade_date,
    }
    if verbose:
        print(f'[fetch_minline] 完成: {summary}', flush=True)
    return summary


def main():
    p = argparse.ArgumentParser(description='盘后批量抓全市场个股分时入库')
    p.add_argument('--date', help='交易日 YYYYMMDD, 缺省 = _infer_minline_trade_date()')
    p.add_argument('--batch-size', type=int, default=50, help='每批几只 (默认 50)')
    p.add_argument('--sleep', type=float, default=0.05, help='批间间隔秒 (默认 0.05s)')
    p.add_argument('--force', action='store_true', help='不跳过已存在的 (ts_code, trade_date)')
    args = p.parse_args()
    summary = run(trade_date=args.date, batch_size=args.batch_size,
                  sleep_between=args.sleep, skip_existing=not args.force)
    if summary['failed'] > summary['total'] * 0.1:
        print(f'[fetch_minline] ⚠️ 失败率 {summary["failed"]/summary["total"]*100:.1f}% (>10%)', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())