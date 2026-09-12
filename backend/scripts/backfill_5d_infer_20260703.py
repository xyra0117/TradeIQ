"""补 7/3 当日主力净流入数据.

数据来源: 东方财富 detail.html 5 日排行 (fid=f184).
原理: 5 日累计 = 7/3 + 7/2 + 7/6 + 7/7 + 7/8 (最近 5 个交易日)
      → 7/3 当日值 = 5 日累计 - 7/2 - 7/6 - 7/7 - 7/8 的当日值 (DB 已有)

复用 fetch_fund_flow_today.fetch_5day_market 抓 5 日排行, 不重新发明轮子.
"""
import sqlite3
import os
import sys
import math
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')

# 5 日累计 = target_date + 之后 4 个交易日
TARGET_DATE = '20260703'
NEIGHBOR_DATES = ['20260702', '20260706', '20260707', '20260708']


def _f(v):
    """模拟 app.py _save_em_fund_flow_rows 的数值化."""
    if v is None or v == '-' or v == '':
        return None
    try:
        x = float(v)
        return 0.0 if math.isnan(x) else x
    except (ValueError, TypeError):
        return None


def _to_ts_code(code):
    code = str(code).strip()
    if not code or len(code) != 6:
        return f'{code}.SH' if code else ''
    return f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ'


def fetch_5day_aggregate():
    """调 fetch_5day_market, 返回 {ts_code: {main_net_inflow, super_net, ...}} 5 日累计."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fetch_fund_flow_today import fetch_5day_market

    rows, total, failed, actual_date = fetch_5day_market(
        target_date=TARGET_DATE, headless=False, verbose=True, skip_if_exists=False,
    )
    if failed:
        print(f'⚠️ 失败页: {failed[:10]}', file=sys.stderr)
    print(f'5 日排行抓取: {len(rows)}/{total} 行, 失败 {len(failed)} 页')

    agg = {}
    for r in rows:
        code = str(r.get('f12', '')).strip()
        if not code or len(code) != 6:
            continue
        ts_code = _to_ts_code(code)
        # 5 日排行字段 (fid=f164): f164/f165/f166/f167/f168/f169/f170/f171/f172/f173
        agg[ts_code] = {
            'code': code,
            'name': r.get('f14', ''),
            'close': _f(r.get('f2')),
            'change_pct': _f(r.get('f109')),  # 5 日涨跌幅, 当日涨跌幅是 f3
            'main_net_inflow': _f(r.get('f164')) or 0.0,
            'main_net_pct': _f(r.get('f165')),
            'super_net': _f(r.get('f166')),
            'super_pct': _f(r.get('f167')),
            'big_net': _f(r.get('f168')),
            'big_pct': _f(r.get('f169')),
            'mid_net': _f(r.get('f170')),
            'mid_pct': _f(r.get('f171')),
            'small_net': _f(r.get('f172')),
            'small_pct': _f(r.get('f173')),
        }
    return agg


def fetch_neighbor_sums():
    """从 DB 查 7/2+7/6+7/7+7/8 四天的当日主力净流入, 按 ts_code 求和."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    placeholders = ','.join('?' * len(NEIGHBOR_DATES))
    sql = f"""
        SELECT ts_code,
               SUM(main_net_inflow) AS sum_main,
               SUM(super_net)       AS sum_super,
               SUM(big_net)         AS sum_big,
               SUM(mid_net)         AS sum_mid,
               SUM(small_net)       AS sum_small
        FROM fund_flow
        WHERE trade_date IN ({placeholders})
          AND source = 'eastmoney-push2'
        GROUP BY ts_code
    """
    rows = conn.execute(sql, NEIGHBOR_DATES).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out[r[0]] = {
            'sum_main': r[1] or 0.0,
            'sum_super': r[2] or 0.0,
            'sum_big': r[3] or 0.0,
            'sum_mid': r[4] or 0.0,
            'sum_small': r[5] or 0.0,
        }
    return out


def infer_target_day(agg_5d, neighbor_sums):
    """5 日累计 - 邻居 4 天 = 7/3 当日值."""
    out = []
    missing_neighbor = 0
    for ts_code, agg in agg_5d.items():
        ns = neighbor_sums.get(ts_code)
        if not ns:
            # 邻居 4 天没数据 (极少见, 跨交易日停牌股), 跳过
            missing_neighbor += 1
            continue
        out.append({
            'ts_code': ts_code,
            'code': agg['code'],
            'name': agg['name'],
            'close': agg['close'],  # 5 日排行里 f2 = 当日最新价, OK
            'change_pct': None,  # 5 日排行没当日涨跌幅 (f109 是 5 日涨跌幅), 留 None
            # 当日值 = 5 日累计 - 邻居 4 天
            'main_net_inflow': agg['main_net_inflow'] - ns['sum_main'],
            'main_net_pct': agg['main_net_pct'],  # 占比没有可减的语义, 留原值 (5日占比)
            'super_net': (agg['super_net'] or 0) - ns['sum_super'],
            'super_pct': agg['super_pct'],
            'big_net': (agg['big_net'] or 0) - ns['sum_big'],
            'big_pct': agg['big_pct'],
            'mid_net': (agg['mid_net'] or 0) - ns['sum_mid'],
            'mid_pct': agg['mid_pct'],
            'small_net': (agg['small_net'] or 0) - ns['sum_small'],
            'small_pct': agg['small_pct'],
        })
    return out, missing_neighbor


def upsert_fund_flow(rows_703, target_date):
    """写入 fund_flow 表, trade_date = target_date. DELETE + INSERT 覆盖同 ts_code."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    inserted = 0
    for r in rows_703:
        cur.execute('DELETE FROM fund_flow WHERE ts_code=? AND trade_date=?',
                    (r['ts_code'], target_date))
        cur.execute('''
            INSERT INTO fund_flow
            (trade_date, ts_code, code, name, close, change_pct,
             main_net_inflow, main_net_pct,
             super_net, super_pct, big_net, big_pct,
             mid_net, mid_pct, small_net, small_pct, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (target_date, r['ts_code'], r['code'], r['name'],
              r['close'], r['change_pct'],
              r['main_net_inflow'], r['main_net_pct'],
              r['super_net'], r['super_pct'],
              r['big_net'], r['big_pct'],
              r['mid_net'], r['mid_pct'],
              r['small_net'], r['small_pct'],
              'eastmoney-push2'))
        inserted += 1
    conn.commit()
    conn.close()
    return inserted


def main():
    t0 = time.time()
    print(f'\n=== 补 {TARGET_DATE} 当日主力净流入 ===\n')

    print('1) 抓东财 5 日排行 (fid=f184)...')
    agg_5d = fetch_5day_aggregate()
    print(f'   5 日排行: {len(agg_5d)} 只股')

    print('\n2) 从 DB 查邻居 4 天 ({} + {} + {} + {}) 的当日值...'
          .format(*NEIGHBOR_DATES))
    neighbor = fetch_neighbor_sums()
    print(f'   邻居有数据的: {len(neighbor)} 只股')

    print(f'\n3) 反推 {TARGET_DATE} 当日值 (5 日累计 - 邻居 4 天)...')
    rows_703, missing = infer_target_day(agg_5d, neighbor)
    print(f'   反推成功: {len(rows_703)} 只, 邻居无数据跳过 {missing} 只')

    if not rows_703:
        print('❌ 没有可写入的行')
        return

    # 检查 top5 反推值是否合理 (主力净流入应该在 ±10 亿范围内)
    rows_703_sorted = sorted(rows_703, key=lambda x: abs(x['main_net_inflow']), reverse=True)[:5]
    print('\n4) 反推结果样例 (按 |main_net_inflow| 降序 top5):')
    for r in rows_703_sorted:
        print(f'   {r["code"]} {r["name"]}: '
              f'main={r["main_net_inflow"]/1e6:+.1f}M, '
              f'super={r["super_net"]/1e6:+.1f}M, '
              f'big={r["big_net"]/1e6:+.1f}M')

    print(f'\n5) 写入 fund_flow 表 trade_date={TARGET_DATE}...')
    n = upsert_fund_flow(rows_703, TARGET_DATE)
    print(f'   ✅ 写入 {n} 行 ({time.time()-t0:.1f}s)')


if __name__ == '__main__':
    main()