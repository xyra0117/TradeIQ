"""盘后批量抓 4 大指数分时入库.

用法:
  python3 backend/fetch_index_minline_daily.py [--date YYYYMMDD]

scheduler 在每个交易日 15:05 自动调用 run(); 也可 CLI 手动触发.

策略:
  - 4 只指数 (INDEX_CHART_CODES): 上证/深证/创业板/科创50, 共 ~960 行/天
  - 每只: 跳已有 (ts_code, trade_date) → 抓 sina minline → quote 前后核验 → INSERT OR REPLACE 入库
  - 失败: warn 日志 + 跳过该指数, 不阻塞其他指数
  - 整体进度: done=N/4, saved=本日新增, skipped=已存在, failed=抓取/核验失败
"""
import argparse
import os
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

# 让 from app import _fetch_sina_minline_rows / _fetch_index_chart_quote / INDEX_CHART_CODES / DB_PATH 能跑
import app as _app  # noqa: E402

DB_PATH = _app.DB_PATH
_fetch_sina_minline_rows = _app._fetch_sina_minline_rows
_fetch_index_chart_quote = _app._fetch_index_chart_quote
_sina_code_for_ts = _app._sina_code_for_ts
_persist_index_minline_bars = _app._persist_index_minline_bars
_infer_minline_trade_date = _app._infer_minline_trade_date
INDEX_CHART_CODES = _app.INDEX_CHART_CODES

import sqlite3  # noqa: E402


def _already_persisted(ts_code, trade_date):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        row = conn.execute(
            'SELECT COUNT(*) FROM index_minline_bars WHERE ts_code=? AND trade_date=?',
            (ts_code, trade_date)).fetchone()
        return row and row[0] > 0
    finally:
        conn.close()


def run(trade_date=None, skip_existing=True, verbose=True):
    """盘后跑一次. trade_date 不传 → _infer_minline_trade_date() (盘后就是 today).

    返回 dict {total, done, saved_rows, skipped, failed}.
    """
    if trade_date is None:
        trade_date = _infer_minline_trade_date()

    total = len(INDEX_CHART_CODES)
    if verbose:
        print(f'[fetch_idx_minline] trade_date={trade_date}  全量 {total} 只指数', flush=True)

    done = saved_rows = skipped = failed = 0
    t0 = time.time()

    for ts_code in INDEX_CHART_CODES:
        done += 1
        try:
            if skip_existing and _already_persisted(ts_code, trade_date):
                skipped += 1
                continue
            # 前后两次 quote 核验防跨日
            before = _fetch_index_chart_quote(ts_code)
            if before['date'] != trade_date or before['open'] <= 0 or before['time'] < '09:30:00':
                failed += 1
                if verbose:
                    print(f'[fetch_idx_minline] {ts_code} 行情未开盘/跨日 ({before}), 跳过', flush=True)
                continue
            rows = _fetch_sina_minline_rows(_sina_code_for_ts(ts_code))
            if not rows:
                failed += 1
                if verbose:
                    print(f'[fetch_idx_minline] {ts_code} 空数据', flush=True)
                continue
            after = _fetch_index_chart_quote(ts_code)
            if after['date'] != trade_date or rows[-1]['m'] > after['time']:
                failed += 1
                if verbose:
                    print(f'[fetch_idx_minline] {ts_code} 核验失败 (quote {after["date"]}/{after["time"]} 与 rows 末条 {rows[-1]["m"]} 不匹配)', flush=True)
                continue
            _persist_index_minline_bars(ts_code, trade_date, rows, after['prev_close'])
            saved_rows += len(rows)
        except Exception as e:
            failed += 1
            if verbose:
                print(f'[fetch_idx_minline] {ts_code} 抓取/落库异常: {e}', flush=True)
                import traceback; traceback.print_exc()

    elapsed = time.time() - t0
    summary = {
        'total': total, 'done': done, 'saved_rows': saved_rows,
        'skipped': skipped, 'failed': failed,
        'elapsed_sec': round(elapsed, 2),
        'trade_date': trade_date,
    }
    if verbose:
        print(f'[fetch_idx_minline] 完成: {summary}', flush=True)
    return summary


def main():
    p = argparse.ArgumentParser(description='盘后批量抓 4 大指数分时入库')
    p.add_argument('--date', help='交易日 YYYYMMDD, 缺省 = _infer_minline_trade_date()')
    p.add_argument('--force', action='store_true', help='不跳过已存在的 (ts_code, trade_date)')
    args = p.parse_args()
    summary = run(trade_date=args.date, skip_existing=not args.force)
    if summary['failed'] == summary['total']:
        print(f'[fetch_idx_minline] ⚠️ 全部失败 ({summary["total"]} 只)', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
