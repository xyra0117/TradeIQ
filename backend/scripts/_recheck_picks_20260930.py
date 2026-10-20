"""串行重算 4/9~9/30 各选股策略, 排查空快照短路问题.

串行是必须的: 并发请求会互相写快照, 读到刚写进去的空快照, 结果不可信.
每种策略串行跑完再跑下一种.
"""
import json
import sqlite3
import time
import urllib.request
from datetime import datetime

BASE = 'http://127.0.0.1:5555/api'
DB = 'market_data.db'
START, END = '20260409', '20260930'


def get(path, timeout=90):
    with urllib.request.urlopen(f'{BASE}/{path}', timeout=timeout) as r:
        return json.loads(r.read().decode())


def trading_days():
    conn = sqlite3.connect(DB)
    days = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM fund_flow WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date',
        (START, END))]
    conn.close()
    return days


def snap_table(api_prefix, days):
    """读快照表里各天的 count."""
    table = {
        'conditional-picks': 'conditional_picks_snapshot',
        'conditional-picks/new': 'conditional_picks_new_snapshot',
        'conditional-picks/preferred': 'conditional_picks_preferred_snapshot',
        'combined-picks': 'combined_picks_snapshot',
    }[api_prefix]
    conn = sqlite3.connect(DB)
    out = {}
    for d in days:
        row = conn.execute(f'SELECT count FROM {table} WHERE trade_date=?', (d,)).fetchone()
        out[d] = row[0] if row else None
    conn.close()
    return out


def run(api_prefix, days, label):
    print(f'\n===== {label} ({api_prefix}) =====', flush=True)
    print(f'{"日期":<10}{"count":>7}{"base":>7}  {"note":<28}', flush=True)
    zero = []
    rows_out = []
    for d in days:
        try:
            p = get(f'{api_prefix}?date={d}')
        except Exception as exc:
            print(f'{d:<10}{"ERR":>7}  {exc}', flush=True)
            continue
        cnt = p.get('count')
        ex = p.get('exclusions') or p.get('preferred_exclusions') or {}
        base = ex.get('base_count')
        note = (p.get('note') or '')[:26]
        print(f'{d:<10}{str(cnt):>7}{str(base):>7}  {note:<28}', flush=True)
        rows_out.append((d, cnt, base, note))
        if cnt == 0:
            zero.append(d)
    return rows_out, zero


def main():
    days = trading_days()
    print(f'交易日 {len(days)} 天: {days[0]} ~ {days[-1]}', flush=True)
    results = {}
    for prefix, label in [('conditional-picks', '条件选股'),
                          ('conditional-picks/new', '条件选股新'),
                          ('conditional-picks/preferred', '条件优选')]:
        rows, zero = run(prefix, days, label)
        results[label] = (rows, zero)
        print(f'-- {label}: 0 命中共 {len(zero)} 天', flush=True)

    out = {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'days': len(days), 'results': {k: {'rows': v[0], 'zero_days': v[1]}
                                          for k, v in results.items()}}
    with open('logs/picks_recheck_20261006.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('\n已写入 logs/picks_recheck_20261006.json', flush=True)


if __name__ == '__main__':
    t0 = time.time()
    main()
    print(f'耗时 {time.time()-t0:.0f}s', flush=True)
