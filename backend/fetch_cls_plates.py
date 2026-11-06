"""抓财联社 (cls.cn) 板块 + 成分股 → cls_plate / cls_plate_stock 表。

接口 (2026-10-30 实测, 不需要签名, 不限 IP):
  板块列表  https://x-quote.cls.cn/web_quote/plate/plate_list?type=concept&way=change&page=1&rever=1
      type: industry(84) / concept(3128) / area(62)
      每页 30 个, is_all=1 表示到底了
  板块详情  /web_quote/plate/info?secu_code=cls80481
      涨跌幅/周月季年涨幅/主力资金/涨停跌停家数/涨跌平家数/板块简介
  成分股    /web_quote/plate/stocks?secu_code=cls80481&way=change&page=1&rever=1
      一次返回全部成分股 (page 参数无效, 忽略), 数量 = up_num+down_num+flat_num
  相关板块  /web_quote/plate/industry?secu_code=...  (多数板块为空)

页面: https://www.cls.cn/hotplate (板块列表), https://www.cls.cn/plate?code=clsXXXXX (成分股)

命令行:
  python3 fetch_cls_plates.py --write --type concept
  python3 fetch_cls_plates.py --write --type industry --type area
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
API_BASE = 'https://x-quote.cls.cn'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
PLATE_TYPES = ('industry', 'concept', 'area')

_RE_SUCODE = re.compile(r'^(sh|sz|bj)(\d{6})$')


def _http_json(path, params, retries=3, timeout=25):
    """调财联社接口, 返回 data 段. 出错抛异常."""
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    url = f'{API_BASE}{path}?{query}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': UA, 'Referer': 'https://www.cls.cn/hotplate'})
            with opener.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8', errors='replace'))
            if payload.get('code') != 200:
                raise RuntimeError(f"接口返回 code={payload.get('code')} msg={payload.get('msg')}")
            return payload.get('data')
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f'GET {path}?{query} 失败: {last}')


def fetch_plate_list(plate_type, way='change'):
    """拉全部板块 (翻页到 is_all)。返回 [plate_dict, ...], 已按板块代码去重.

    注意: 列表是按涨跌幅排序的, 翻页过程中盘面一变排序就变, 同一个板块会在多页里
    重复出现 (概念板块 3128 行其实只有 398 个唯一板块, 每个平均出现 14 次)。必须去重。
    """
    seen = set()
    out = []
    page = 1
    while page < 200:
        data = _http_json('/web_quote/plate/plate_list',
                          {'type': plate_type, 'way': way, 'page': page, 'rever': 1})
        rows = data.get('plate_data') or []
        for row in rows:
            code = row.get('secu_code')
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(row)
        if data.get('is_all') == 1 or not rows:
            break
        page += 1
        time.sleep(0.15)
    return out


def fetch_plate_info(plate_code):
    return _http_json('/web_quote/plate/info', {'secu_code': plate_code})


def fetch_plate_stocks(plate_code):
    data = _http_json('/web_quote/plate/stocks',
                      {'secu_code': plate_code, 'way': 'change', 'page': 1, 'rever': 1})
    return data.get('stocks') or []


def _ts_code(secu_code):
    """'sh688073' → '688073.SH'"""
    m = _RE_SUCODE.match(str(secu_code or '').lower())
    return f'{m.group(2)}.{m.group(1).upper()}' if m else str(secu_code or '')


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS cls_plate (
        plate_code TEXT PRIMARY KEY,        -- clsXXXXX
        name TEXT NOT NULL,
        plate_type TEXT NOT NULL,           -- industry / concept / area
        description TEXT,
        change REAL, week_change REAL, month_change REAL,
        quarter_change REAL, year_change REAL,
        fundflow REAL,
        limit_up_num INTEGER, limit_down_num INTEGER,
        up_num INTEGER, down_num INTEGER, flat_num INTEGER,
        stock_count INTEGER DEFAULT 0,
        has_core INTEGER, has_etf INTEGER, has_industry INTEGER,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS cls_plate_stock (
        plate_code TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        name TEXT,
        change REAL,                       -- 涨跌幅 (小数, 0.0234 = 2.34%)
        last_px REAL,
        cmc REAL,                          -- 流通市值 (元)
        fundflow REAL,                     -- 主力资金 (元)
        head_num INTEGER,                  -- 领涨次数
        weight INTEGER,
        is_core INTEGER,
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (plate_code, ts_code)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cls_plate_stock_code ON cls_plate_stock(ts_code)')
    conn.commit()


def write_plate(conn, plate_type, info, stocks):
    code = info.get('secu_code')
    conn.execute('DELETE FROM cls_plate_stock WHERE plate_code=?', (code,))
    conn.executemany('''INSERT OR REPLACE INTO cls_plate_stock
        (plate_code, ts_code, name, change, last_px, cmc, fundflow, head_num, weight, is_core)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        [(code, _ts_code(s.get('secu_code')), s.get('secu_name'), s.get('change'),
          s.get('last_px'), s.get('cmc'), s.get('fundflow'), s.get('head_num'),
          s.get('weight'), s.get('is_core')) for s in stocks])
    conn.execute('''INSERT OR REPLACE INTO cls_plate
        (plate_code, name, plate_type, description, change, week_change, month_change,
         quarter_change, year_change, fundflow, limit_up_num, limit_down_num,
         up_num, down_num, flat_num, stock_count, has_core, has_etf, has_industry, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))''',
        (code, info.get('secu_name'), plate_type, info.get('desc'), info.get('change'),
         info.get('week_change'), info.get('month_change'), info.get('quarter_change'),
         info.get('year_change'), info.get('fundflow'), info.get('limit_up_num'),
         info.get('limit_down_num'), info.get('up_num'), info.get('down_num'),
         info.get('flat_num'), len(stocks), info.get('has_core'), info.get('has_etf'),
         info.get('has_industry')))
    conn.commit()


def crawl(plate_types=('concept',), delay=0.2, write=False, db_path=DB_PATH, limit=None):
    conn = sqlite3.connect(db_path, timeout=60)
    ensure_schema(conn)
    stats = {'plates': 0, 'stocks': 0, 'failed': []}
    for plate_type in plate_types:
        boards = fetch_plate_list(plate_type)
        if limit:
            boards = boards[:limit]
        print(f'[{plate_type}] 共 {len(boards)} 个板块', flush=True)
        for idx, board in enumerate(boards, 1):
            code = board.get('secu_code')
            try:
                info = fetch_plate_info(code)
                stocks = fetch_plate_stocks(code)
            except Exception as exc:  # noqa: BLE001
                stats['failed'].append((code, board.get('secu_name'), str(exc)))
                print(f'  [{idx}/{len(boards)}] {board.get("secu_name")}({code}) 失败: {exc}', flush=True)
                continue
            if write:
                write_plate(conn, plate_type, info, stocks)
            stats['plates'] += 1
            stats['stocks'] += len(stocks)
            if idx % 50 == 0 or idx == len(boards):
                print(f'  [{idx}/{len(boards)}] {info.get("secu_name")}({code}) {len(stocks)}只 '
                      f'(累计 {stats["plates"]} 板块 / {stats["stocks"]} 只)', flush=True)
            time.sleep(delay)
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--type', action='append', choices=PLATE_TYPES, default=None,
                    help='板块类型, 可多次指定 (默认 concept)')
    ap.add_argument('--limit', type=int, default=None, help='每种类型只抓前 N 个')
    ap.add_argument('--delay', type=float, default=0.2)
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--db', default=DB_PATH)
    ap.add_argument('--list-only', action='store_true')
    args = ap.parse_args()

    plate_types = args.type or ['concept']
    if args.list_only:
        for t in plate_types:
            for b in fetch_plate_list(t):
                print(f'{t}\t{b.get("secu_code")}\t{b.get("secu_name")}')
        return 0

    stats = crawl(plate_types=plate_types, delay=args.delay, write=args.write,
                  db_path=args.db, limit=args.limit)
    print(f"\n完成: {stats['plates']} 个板块, {stats['stocks']} 条成分股, 失败 {len(stats['failed'])}")
    for code, name, err in stats['failed'][:20]:
        print(f'  失败 {name}({code}): {err}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
