#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 positions_backup_*.json 一键还原 持仓 / 成交流水 / 对账日志

用法(先看看会还原什么, 不动数据库):
    python3 scripts/restore_from_backup.py backend/positions_backup_20261013_234624.json --dry-run

真的还原:
    python3 scripts/restore_from_backup.py backend/positions_backup_20261013_234624.json --yes

行为说明:
  - 还原 = DELETE 整张表 + 按备份原样插回 (保留原始 id / applied 标记)
  - 只处理备份里存在的表, 不碰 market_data.db 里的其他数据
  - 默认先自动备份当前状态, 避免"还原"本身也变成不可逆操作
"""

import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'market_data.db')
MANAGED_TABLES = ('positions', 'trades', 'position_reconcile_log')


def latest_backup():
    """没指定文件时, 自动找 backend/ 下最新的备份"""
    here = os.path.dirname(os.path.abspath(__file__))
    pattern = os.path.join(os.path.dirname(here), 'positions_backup_*.json')
    files = sorted(glob.glob(pattern))
    if not files:
        sys.exit('没找到备份文件, 先确认 backend/ 下有 positions_backup_*.json')
    return files[-1]


def dump_current(out_path):
    """把当前三张表的状态另存一份, 保证这一步也可逆"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    data = {}
    for t in MANAGED_TABLES:
        cur = c.execute('SELECT * FROM %s' % t)
        cols = [d[0] for d in cur.description]
        data[t] = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print('  当前状态已另存: %s' % out_path)


def restore(backup_path, dry_run=False):
    with open(backup_path, encoding='utf-8') as f:
        data = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    tables = [t for t in MANAGED_TABLES if t in data]
    if not tables:
        conn.close()
        sys.exit('备份文件里没有可还原的表, 期望包含: %s' % ', '.join(MANAGED_TABLES))

    print('还原来源: %s' % backup_path)
    for t in tables:
        now = c.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
        print('  %-24s 当前 %5d 行  ->  还原后 %5d 行' % (t, now, len(data[t])))

    if dry_run:
        conn.close()
        print('\n这是演练, 数据库没有改动。')
        return

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dump_current(os.path.join(os.path.dirname(DB_PATH), 'positions_backup_%s.json' % stamp))

    for t in tables:
        rows = data[t]
        c.execute('DELETE FROM %s' % t)
        if rows:
            cols = list(rows[0].keys())
            # 只保留表里真实存在的列, 防止备份来自更新/更旧的结构时插不进去
            valid = {r[1] for r in c.execute('PRAGMA table_info(%s)' % t)}
            cols = [x for x in cols if x in valid]
            sql = 'INSERT OR REPLACE INTO %s (%s) VALUES (%s)' % (
                t, ','.join(cols), ','.join('?' * len(cols)))
            c.executemany(sql, [[r.get(x) for x in cols] for r in rows])
        print('  %s 还原完成' % t)

    conn.commit()
    conn.close()
    print('\n全部还原完成。')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='从备份 JSON 还原持仓相关数据')
    ap.add_argument('backup', nargs='?', help='备份文件路径, 省略则取最新的')
    ap.add_argument('--dry-run', action='store_true', help='只看会还原什么, 不动数据库')
    ap.add_argument('--yes', action='store_true', help='确认执行还原')
    args = ap.parse_args()

    path = args.backup or latest_backup()
    if not os.path.exists(path):
        sys.exit('文件不存在: %s' % path)

    if not args.dry_run and not args.yes:
        print('这是会改动数据库的操作。加 --yes 确认执行, 或加 --dry-run 先看。')
        sys.exit(1)

    restore(path, dry_run=args.dry_run)
