#!/usr/bin/env python3
"""用 TuShare 补齐 index_daily 缺失的 OHLC，并修正同期成交额。

默认只做检查；传入 --apply 才会备份目标行并在单个事务中更新数据库。
只处理 open/high/low 任一为空的既有记录，不新增日期，也不修改 close/change。
"""
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
import app

INDICES = app.INDEX_CHART_CODES
DB_PATH = app.DB_PATH


def load_targets(conn):
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(
        "SELECT id,date,name,open,high,low,close,change,volume FROM index_daily "
        "WHERE open IS NULL OR high IS NULL OR low IS NULL ORDER BY date,name"
    )]


def fetch_updates(pro, targets):
    """拉取并校验每个目标行；覆盖不完整时直接失败，禁止部分回填。"""
    if not targets:
        return []
    start_date = min(row['date'] for row in targets)
    end_date = max(row['date'] for row in targets)
    wanted = {(row['date'], row['name']): row for row in targets}
    fetched = {}
    for code, name in INDICES.items():
        frame = pro.index_daily(ts_code=code, start_date=start_date, end_date=end_date)
        for _, row in frame.iterrows():
            key = (str(row['trade_date']), name)
            if key not in wanted:
                continue
            values = {field: float(row[field]) for field in ('open', 'high', 'low', 'close', 'pct_chg', 'amount')}
            if not all(math.isfinite(value) for value in values.values()):
                raise RuntimeError(f'{key} 的 TuShare 数据包含无效数值')
            original = wanted[key]
            if abs(values['close'] - original['close']) > 0.0001:
                raise RuntimeError(f'{key} 收盘价不一致：本地 {original["close"]} / TuShare {values["close"]}')
            if abs(values['pct_chg'] - original['change']) > 0.0001:
                raise RuntimeError(f'{key} 涨跌幅不一致：本地 {original["change"]} / TuShare {values["pct_chg"]}')
            fetched[key] = {
                'id': original['id'], 'date': key[0], 'name': name,
                'open': values['open'], 'high': values['high'], 'low': values['low'],
                'volume': values['amount'] / 100000,  # TuShare 千元 -> 亿元
            }
    missing = sorted(set(wanted) - set(fetched))
    if missing:
        sample = ', '.join(f'{date}/{name}' for date, name in missing[:5])
        raise RuntimeError(f'TuShare 未覆盖全部目标：缺 {len(missing)} 行（{sample}）')
    return [fetched[key] for key in sorted(fetched)]


def apply_updates(conn, targets, updates, backup_path):
    with open(backup_path, 'x', encoding='utf-8') as handle:
        json.dump(targets, handle, ensure_ascii=False, indent=2)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.executemany(
            'UPDATE index_daily SET open=?,high=?,low=?,volume=? '
            'WHERE id=? AND (open IS NULL OR high IS NULL OR low IS NULL)',
            [(row['open'], row['high'], row['low'], row['volume'], row['id']) for row in updates]
        )
        remaining = conn.execute(
            'SELECT COUNT(*) FROM index_daily WHERE open IS NULL OR high IS NULL OR low IS NULL'
        ).fetchone()[0]
        if remaining:
            raise RuntimeError(f'事务内验证失败：仍有 {remaining} 行缺少 OHLC')
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='备份后写入数据库；默认只检查')
    args = parser.parse_args()
    conn = sqlite3.connect(DB_PATH)
    try:
        targets = load_targets(conn)
        if not targets:
            print('无需回填：index_daily 的 OHLC 已完整')
            return
        print(f'待回填 {len(targets)} 行：{targets[0]["date"]} ~ {targets[-1]["date"]}')
        updates = fetch_updates(app.get_pro(), targets)
        old_volume = sum(float(row['volume'] or 0) for row in targets)
        new_volume = sum(row['volume'] for row in updates)
        print(f'校验通过 {len(updates)} 行；同期成交额合计 {old_volume:.2f} -> {new_volume:.2f} 亿元')
        if not args.apply:
            print('DRY RUN：未写数据库；确认后使用 --apply')
            return
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = os.path.join(BACKEND_DIR, f'index_daily_backup_{stamp}.json')
        apply_updates(conn, targets, updates, backup)
        print(f'回填完成：{len(updates)} 行；备份：{backup}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
