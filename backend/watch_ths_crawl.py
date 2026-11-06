"""实时看同花顺概念板块抓取进度。

用法:
  python3 backend/watch_ths_crawl.py            # 跟随 backend/ths_concepts.db
  python3 backend/watch_ths_crawl.py --db xxx.db # 指定库

按写入时间增量打印, 每个板块一行, 不影响抓取进程本身。
"""
import argparse
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, 'ths_concepts.db')
TOTAL_BOARDS = 361          # 同花顺概念板块总数 (2026-10-29 实测)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--interval', type=float, default=1.0)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cursor = ('', '')
    seen = 0
    print(f'盯梢中: {args.db}   (Ctrl+C 退出)\n', flush=True)
    try:
        while True:
            rows = conn.execute(
                'SELECT concept_code, name, stock_count, page_count, updated_at FROM ths_concept '
                'WHERE (updated_at > ?) OR (updated_at = ? AND concept_code > ?) '
                'ORDER BY updated_at, concept_code',
                (cursor[0], cursor[0], cursor[1])).fetchall()
            for code, name, count, pages, ts in rows:
                seen += 1
                print(f'[{seen:>3}/{TOTAL_BOARDS}] {name}({code})  {pages}页 {count}只   {ts}', flush=True)
                cursor = (ts, code)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n退出。')


if __name__ == '__main__':
    main()
