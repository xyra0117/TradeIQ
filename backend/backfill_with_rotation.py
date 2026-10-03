#!/usr/bin/env python3
"""backfill_with_rotation.py
每 200 只切下一个 mihomo 节点, 单 worker 慢跑, 自动续跑 fail + pending.
走代理 + 切节点(用户意图), 跑 1 轮 (--limit 200) 评估代理路径下成功率.
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

MIHOMO_SOCK = '/tmp/mihomo-party-501-1038.sock'
MIHOMO_GROUP = 'GLOBAL'
BATCH_SIZE = 200
MAX_DELAY_MS = 500
BACKEND_DIR = '/Users/xyra/Desktop/claude code/TradeIQ/backend'
DB_PATH = f'{BACKEND_DIR}/market_data.db'


def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


def mihomo_get(path):
    r = subprocess.run(
        ['curl', '-s', '--max-time', '5', '--unix-socket', MIHOMO_SOCK,
         f'http://localhost/{path}'],
        capture_output=True, text=True,
    )
    return json.loads(r.stdout)


def mihomo_put(path, body):
    r = subprocess.run(
        ['curl', '-s', '--max-time', '5', '--unix-socket', MIHOMO_SOCK,
         '-X', 'PUT', f'http://localhost/{path}',
         '-H', 'Content-Type: application/json',
         '-d', json.dumps(body)],
        capture_output=True, text=True,
    )
    return r.stdout


def select_node(name):
    r = mihomo_put(f'proxies/{MIHOMO_GROUP}', {'name': name})
    log(f'切节点 → {name}  ({r.strip()[:60] or "ok"})')


def get_fast_nodes():
    proxies = mihomo_get('proxies')
    all_nodes = [n for n in proxies['proxies'][MIHOMO_GROUP]['all']
                 if n not in ('DIRECT', 'REJECT')]
    def delay_of(n):
        h = (proxies['proxies'].get(n, {})
             .get('extra', {})
             .get('https://www.gstatic.com/generate_204', {})
             .get('history', []))
        valid = [x['delay'] for x in h if x.get('delay', 0) > 0]
        return min(valid) if valid else 9999
    nodes_with_delay = [(delay_of(n), n) for n in all_nodes]
    nodes_with_delay.sort()
    return [n for d, n in nodes_with_delay if d < MAX_DELAY_MS]


def progress_stats():
    c = sqlite3.connect(DB_PATH)
    done = c.execute("SELECT COUNT(*) FROM fflow_backfill_progress WHERE status='done'").fetchone()[0]
    fail = c.execute("SELECT COUNT(*) FROM fflow_backfill_progress WHERE status='fail'").fetchone()[0]
    empty = c.execute("SELECT COUNT(*) FROM fflow_backfill_progress WHERE status='empty'").fetchone()[0]
    c.close()
    return done, fail, empty


def run_one_batch():
    cmd = [
        'python3', 'backfill_flow_daykline.py',
        '--retry-fail', '--workers', '1', '--gap-ms', '800',
        '--limit', str(BATCH_SIZE),
    ]
    log(f'跑一轮: {" ".join(cmd)}')
    p = subprocess.run(cmd, cwd=BACKEND_DIR)
    return p.returncode


def main():
    log('=== backfill_with_rotation 启动 ===')
    nodes = get_fast_nodes()
    log(f'低延迟(<{MAX_DELAY_MS}ms)节点 {len(nodes)} 个')
    if not nodes:
        log('无可用节点, 退出')
        sys.exit(1)

    idx = 0
    round_no = 0
    while True:
        round_no += 1
        done, fail, empty = progress_stats()
        total = done + fail + empty
        log(f'>>> 第 {round_no} 轮, done={done} fail={fail} empty={empty} 处理={total}')
        if done + fail + empty >= 5211 and fail == 0:
            log('全部完成')
            break

        select_node(nodes[idx])
        idx = (idx + 1) % len(nodes)

        run_one_batch()
        time.sleep(3)


if __name__ == '__main__':
    main()
