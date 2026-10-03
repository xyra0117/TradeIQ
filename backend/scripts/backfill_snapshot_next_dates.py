"""按真实交易日历回填快照表里已落库的 next_date_1..5.

背景: 这 5 列是在写入快照时算好存下的, 旧实现按"纯周末顺延"数日子, 遇法定节假日
(中秋/国庆/春节) 整体错位 —— 比如 20260924 的快照把 T+1 标成 20260925 (中秋休市),
真实的首个交易日是 20260928。app.py 的 _next_workdays 已改为查 trading_dates_cache,
本脚本负责把历史行里已经写死的旧值按新口径重算一遍。

只改日期列, 不动 count / results_json / 阈值等任何其他字段。
可重复执行 (幂等); 默认 dry-run, 加 --write 才落库。
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import date as _date, timedelta as _timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'market_data.db')
NEXT_DAYS = 5
TABLES = (
    'conditional_picks_snapshot',
    'conditional_picks_new_snapshot',
    'conditional_picks_preferred_snapshot',
    'conditional_picks_snapshot_ths',
)
COLS = ('next_date', 'next_date_2', 'next_date_3', 'next_date_4', 'next_date_5')


def trading_days_after(ymd, n, conn):
    """ymd 之后的前 n 个真实交易日; 历表覆盖不足时返回不足 n 项的列表."""
    base = _date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    horizon = (base + _timedelta(days=n * 3 + 20)).strftime('%Y%m%d')
    return [r[0] for r in conn.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date > ? AND cal_date <= ? '
        'ORDER BY cal_date LIMIT ?', (ymd, horizon, n)).fetchall()]


def main():
    parser = argparse.ArgumentParser(description='回填快照表 next_date_1..5 (交易日历口径)')
    parser.add_argument('--write', action='store_true', help='真正写库 (默认只打印差异)')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    conn.row_factory = sqlite3.Row
    cal_max = conn.execute('SELECT MAX(cal_date) FROM trading_dates_cache').fetchone()[0]
    print(f'交易日历覆盖到: {cal_max}')
    if not cal_max:
        print('交易日历为空, 终止 (先补 trading_dates_cache)')
        return 1

    total_changed = 0
    for table in TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            print(f'  {table}: 表不存在, 跳过')
            continue
        rows = conn.execute(
            f'SELECT trade_date, {", ".join(COLS)} FROM {table} ORDER BY trade_date').fetchall()
        updates, changes = [], 0
        for row in rows:
            want = trading_days_after(row['trade_date'], NEXT_DAYS, conn)
            have = [row[c] for c in COLS]
            if want and have != want[:len(have)]:
                # executemany 要的是「每行一组平铺参数」: 5 个日期 + 主键
                updates.append(tuple(want) + (row['trade_date'],))
                changes += 1
        if not changes:
            print(f'  {table}: {len(rows)} 行, 无需修正')
            continue
        total_changed += changes
        print(f'  {table}: {len(rows)} 行, 需修正 {changes} 行')
        if args.write:
            set_sql = ', '.join(f'{c} = ?' for c in COLS)
            # 后端 scheduler 在同一份库上持续写入, 写锁竞争激烈 → 退避重试
            for attempt in range(8):
                try:
                    conn.executemany(
                        f'UPDATE {table} SET {set_sql} WHERE trade_date = ?', updates)
                    conn.commit()
                    print(f'    已写库 ({len(updates)} 行)')
                    break
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc) and 'busy' not in str(exc):
                        raise
                    wait = 3 * (attempt + 1)
                    print(f'    第 {attempt + 1} 次遇写锁 ({exc}), {wait}s 后重试', flush=True)
                    time.sleep(wait)
            else:
                print(f'    {table}: 重试 8 次仍被锁, 已跳过 (可稍后重跑, 幂等)')
                continue
        else:
            for params in updates[:3]:
                print(f'    {params[-1]}: {list(params[:-1])}')
            if len(updates) > 3:
                print(f'    ... 其余 {len(updates) - 3} 行省略')
    conn.close()
    print(f'合计需修正: {total_changed} 行' + ('' if args.write else '  (dry-run, 未写库)'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
