"""
回填 fund_flow 表: 用 Tushare 拉最近 N 个交易日全市场主力 top 200 入库.
数据源: Tushare pro.moneyflow(trade_date=YYYYMMDD) 单次返 5,189 行全市场一天.

用法:
  python backfill_topn.py           # 默认 30 天, top 200
  python backfill_topn.py 60        # 60 天
  python backfill_topn.py 30 500    # 30 天, top 500
"""
import sys
import time
import sqlite3
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')

sys.path.insert(0, '.')
from app import _tushare_topn, _save_fund_flow_df, DB_PATH

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
TOPN = int(sys.argv[2]) if len(sys.argv) > 2 else 200


def get_recent_trade_dates(n):
    """取最近 n 个交易日的日期列表 (Tushare trade_cal)."""
    import tushare as ts
    pro = ts.pro_api()
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=int(n * 1.8))).strftime('%Y%m%d')
    cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end,
                        is_open='1', fields='cal_date')
    dates = sorted(cal['cal_date'].tolist(), reverse=True)
    return dates[:n]


def main():
    print(f'=== Tushare 回填 top {TOPN}, 最近 {DAYS} 个交易日 ===', flush=True)
    dates = get_recent_trade_dates(DAYS)
    print(f'交易日 ({len(dates)}): {dates[0]} ~ {dates[-1]}', flush=True)

    # 先清掉已有的 tushare-topn 旧数据 (避免重复)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM fund_flow WHERE source='tushare-topn'")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        print(f'清掉旧 tushare-topn 数据 {deleted} 条', flush=True)

    ok_days, fail_days = 0, 0
    for i, d in enumerate(dates, 1):
        t0 = time.time()
        try:
            df = _tushare_topn(d, n=TOPN)
            if df is None or df.empty:
                print(f'[{i}/{len(dates)}] {d} ❌ 拉取失败', flush=True)
                fail_days += 1
                time.sleep(1)
                continue
            _save_fund_flow_df(df, source='tushare-topn')
            top1 = df.iloc[0]
            print(f'[{i}/{len(dates)}] {d} ✅ {len(df):>3d} 条, top1={top1["ts_code"]} {top1["name"]} {top1["main_net_inflow"]/1e4:>+8.0f}万  ({time.time()-t0:.1f}s)', flush=True)
            ok_days += 1
        except Exception as e:
            print(f'[{i}/{len(dates)}] {d} ❌ {type(e).__name__}: {str(e)[:80]}', flush=True)
            fail_days += 1
        time.sleep(0.3)  # 限频

    # 统计
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date) FROM fund_flow WHERE source='tushare-topn'")
    total, dates_count, min_d, max_d = cur.fetchone()
    conn.close()
    print()
    print(f'=== 完成 ===')
    print(f'  成功 {ok_days} 天 / 失败 {fail_days} 天')
    print(f'  fund_flow tushare-topn: {total} 条, {dates_count} 个交易日, {min_d} ~ {max_d}')


if __name__ == '__main__':
    main()
