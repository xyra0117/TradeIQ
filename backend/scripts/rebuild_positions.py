#!/usr/bin/env python3
"""从 trades 表按 FIFO 算法重算 positions, 修复 cost_price / shares 偏差。

cost_price 口径: 含买入手续费 (跟 _apply_one_trade 算 trade_cost_per_share 一致)
  trade_cost_per_share = (price * shares + buy_fee) / shares
  其中 buy_fee = 佣金 + 过户费 (沪市) -- calc_buy_fees

用法:
  python3 rebuild_positions.py --dry-run    # 只列出差异, 不动 DB
  python3 rebuild_positions.py --apply     # 备份 + 重写 positions
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
# 复用后端 fee 计算, 保证口径一致
import importlib.util
_spec = importlib.util.spec_from_file_location('app_fees', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py'))
_mod = importlib.util.module_from_spec(_spec)
# 不跑整个 app.py, 只导入需要的常量/函数, 跳过 Flask app 初始化
import ast as _ast
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')) as _f:
    _tree = _ast.parse(_f.read())
# 提取 calc_buy_fees / _is_shanghai / _round_half_up 所需名字 (它们的依赖 FEE_CONFIG 也一起保留)
# 简单做法: exec app.py 但替换 app.run 那一行
_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')).read()
_src = _src.replace("    app.run(debug=True, host='0.0.0.0', port=5555)", "    pass")
_ns = {'__name__': 'fees_only', '__file__': os.path.join(os.path.dirname(__file__), 'app.py')}
exec(_src, _ns)
calc_buy_fees = _ns['calc_buy_fees']
calc_sell_fees = _ns['calc_sell_fees']
_is_shanghai = _ns['_is_shanghai']
_round_half_up = _ns['_round_half_up']

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'market_data.db')


def calc_trades_fifo(cur, ts_code):
    """按时间序遍历 trades, FIFO 算法, 返回 (net_shares, cost_price, realized_pnl, buy_n, sell_n)
    cost_price 口径: 含买入手续费 (跟 _apply_one_trade 算 trade_cost_per_share 一致)
    """
    trades = cur.execute('''
        SELECT id, trade_date, trade_time, direction, price, shares, name
        FROM trades
        WHERE ts_code=? AND applied=1
        ORDER BY COALESCE(NULLIF(trade_date,''),'99999999'), trade_time, id
    ''', (ts_code,)).fetchall()
    net = 0.0
    total_buy_amt = 0.0  # 含买入费的"含费总成本"
    total_buy_shares = 0.0
    total_sell_amt = 0.0
    realized_pnl = 0.0
    buy_n = sell_n = 0
    last_buy_date = None
    last_trade_date = None
    # 用于算 position_type: 记录"最后一笔的方向"和"此笔之前的 net"
    last_dir = None        # 'buy' / 'sell'
    prev_net_before_this = 0.0  # 此笔处理前的 net (买/卖都用)
    for _id, td, tti, d, p, sh, name in trades:
        amt = (p or 0) * (sh or 0)
        prev_net_before_this = net  # 记下"此笔前"的净持仓, 用于判断建仓 vs 加仓
        if d == 'buy':
            # 含费成本 = price*shares + 买入费
            buy_fee = calc_buy_fees(amt, ts_code)
            cost_per_share_with_fee = (amt + buy_fee) / sh if sh else 0
            net += sh
            total_buy_shares += sh
            total_buy_amt += cost_per_share_with_fee * sh  # 累加含费成本
            last_buy_date = td
            buy_n += 1
            last_dir = 'buy'
        else:
            sell_n += 1
            if net > 0:
                avg_cost = total_buy_amt / total_buy_shares if total_buy_shares else 0
                sold = min(sh, net)
                # 实现盈亏扣卖方费用
                sell_amt_inc = p * sold
                sell_fee_inc = calc_sell_fees(sell_amt_inc, ts_code)
                realized_pnl += (p - avg_cost) * sold - sell_fee_inc
                net -= sold
            total_sell_amt += amt
            last_dir = 'sell'
        # 任何 buy/sell 都更新 last_trade_date, 仓位应反映"最近一笔"动的时间
        if td:
            last_trade_date = td
    cost = total_buy_amt / total_buy_shares if total_buy_shares else 0
    # 算 position_type:
    #   最近 buy + 之前没底仓 (prev_net_before_this ≤ 0) → 建仓
    #   最近 buy + 之前有底仓                       → 加仓
    #   最近 sell + 之后还有底仓 (net > 0)           → 减仓
    #   最近 sell + 之后卖完 (net ≤ 0)              → 清仓 (但 net>0 才建仓, 不会到这里)
    if last_dir == 'buy':
        if prev_net_before_this <= 0.0001:
            position_type = '建仓'
        else:
            position_type = '加仓'
    elif last_dir == 'sell':
        position_type = '减仓'  # rebuild 只写 net>0 的行, net=0 那一笔就 close 到清仓历史
    else:
        position_type = '建仓'  # 没有任何 trade (兜底)
    return net, cost, realized_pnl, buy_n, sell_n, last_buy_date, last_trade_date, total_buy_amt, total_sell_amt, position_type


def backup_positions():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), f'positions_backup_{ts}.json')
    c = sqlite3.connect(DB_PATH)
    cur = c.cursor()
    cur.execute('SELECT * FROM positions')
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    data = [dict(zip(cols, r)) for r in rows]
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    c.close()
    print(f'备份: {backup_path} ({len(data)} 行)')
    return backup_path


def dry_run():
    c = sqlite3.connect(DB_PATH)
    cur = c.cursor()
    ts_codes = [r[0] for r in cur.execute('''
        SELECT DISTINCT ts_code FROM trades WHERE applied=1
    ''')]
    print(f'=== DRY RUN: {len(ts_codes)} 只股票参与 ===\n')

    plan = []
    for ts in ts_codes:
        net, cost, pnl, bn, sn, lbd, ltd, bamt, samt, ptype = calc_trades_fifo(cur, ts)
        cur_pos = cur.execute(
            'SELECT id, shares, cost_price, position_type, closed_at FROM positions WHERE ts_code=?',
            (ts,)).fetchall()
        plan.append({
            'ts_code': ts, 'net': net, 'cost': cost, 'pnl': pnl,
            'buy_n': bn, 'sell_n': sn, 'last_buy': lbd, 'last_trade': ltd,
            'cur': cur_pos,
        })

    # 找差异: shares 或 cost 跟现存不同, 或没有现存 position
    changed = []
    for p in plan:
        cur_open = [c for c in p['cur'] if c[4] is None]  # closed_at IS NULL
        cur_shares = sum(c[1] or 0 for c in cur_open)
        cur_cost = cur_open[0][2] if cur_open else None
        if cur_open and abs(cur_shares - p['net']) < 0.01 and cur_cost is not None and abs(cur_cost - p['cost']) / max(p['cost'], 0.01) < 0.005:
            continue  # 一致, 跳过
        changed.append(p)

    print(f'差异显著: {len(changed)} / {len(plan)} 只\n')
    print(f'{"ts_code":12s} {"净shares":>10s} {"成本价":>10s} {"实现盈亏":>10s} | 当前')
    print('-' * 80)
    for p in changed[:200]:
        cur_str = '; '.join(f'id={c[0]} {c[1]}股@{c[2]}({c[3]}, closed={c[4] is not None})' for c in p['cur']) or '(无)'
        print(f'  {p["ts_code"]:12s} {p["net"]:>10.0f} {p["cost"]:>10.4f} {p["pnl"]:>10.2f} | {cur_str}')
    if len(changed) > 200:
        print(f'  ...还有 {len(changed) - 200} 只')
    c.close()
    return changed


def apply():
    c = sqlite3.connect(DB_PATH)
    cur = c.cursor()
    backup_path = backup_positions()
    ts_codes = [r[0] for r in cur.execute('''
        SELECT DISTINCT ts_code FROM trades WHERE applied=1
    ''')]
    print(f'\n重算 {len(ts_codes)} 只股票...')
    new_rows = []
    skipped = []
    for ts in ts_codes:
        net, cost, pnl, bn, sn, lbd, ltd, bamt, samt, ptype = calc_trades_fifo(cur, ts)
        if net < -0.001:
            # 卖超: 不重建, 提示人工检查
            skipped.append((ts, '卖超', -net))
            continue
        if net < 0.01:
            # 净持仓 0, 不创建 position
            continue
        name = cur.execute('SELECT name FROM trades WHERE ts_code=? LIMIT 1', (ts,)).fetchone()
        name_str = name[0] if name else ''
        new_rows.append({
            'ts_code': ts, 'name': name_str,
            'shares': round(net, 2), 'cost_price': round(cost, 4),
            'buy_date': ltd or lbd or '',  # 最近一笔(buy/sell), 没 sell 就回退到最后 buy
            'last_trade_date': ltd or lbd or '',
            'original_shares': round(net, 2),
            'position_type': ptype,  # 从 trades 算: 建仓/加仓/减仓
            'total_sell_amount': round(samt, 2),
            'realized_pnl': round(pnl, 2),
        })
    print(f'将写入 {len(new_rows)} 条, 跳过 {len(skipped)} 条 (卖超)')
    if skipped:
        for ts, reason, sh in skipped:
            print(f'  ⚠️ {ts} {reason} 缺 {sh}股')
    # 清空重建
    cur.execute('DELETE FROM positions')
    for r in new_rows:
        cur.execute('''INSERT INTO positions
            (ts_code, name, shares, original_shares, cost_price, buy_date,
             last_trade_date, total_sell_amount, realized_pnl,
             position_type, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),datetime('now','localtime'))''',
            (r['ts_code'], r['name'], r['shares'], r['original_shares'],
             r['cost_price'], r['buy_date'], r['last_trade_date'],
             r['total_sell_amount'], r['realized_pnl'], r['position_type']))
    c.commit()
    c.close()
    print(f'\n完成! 备份: {backup_path}')
    return backup_path


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--yes', action='store_true', help='跳过 apply 前的交互确认')
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        args.dry_run = True
    if args.dry_run:
        dry_run()
    if args.apply:
        if not args.yes:
            print('\n⚠️  即将清空 positions 表并从 trades 重建!')
            ans = input('确认? 输 yes 继续: ').strip().lower()
            if ans != 'yes':
                print('取消')
                sys.exit(0)
        apply()
