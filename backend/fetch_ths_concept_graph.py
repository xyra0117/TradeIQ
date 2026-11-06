"""抓同花顺概念板块详情页里的「概念图谱」(该概念下产业链一览) → ths_concept_graph 表。

页面结构 (2026-10-30 实测):
  <h2>概念图谱</h2>
  <div class="body diagram">
    <a class="label label-l">        概念本身: 名称 + 涨跌幅/涨跌额
    <div class="series"><span class="item">产业链1</span>   产业链分组 (1..N)
    <table class="series-table">
      <thead><tr><td><a class="label label-m">系统软件</a> ...   子板块 (列)
      <tbody><tr><td><a href=".../stockpage.../002711/" class="label label-s">欧浦退 +6.67% +0.32</a> ...
                                                          个股 (列, 与 thead 同列对应)

子板块属于第几条产业链: <a class="label label-m down"> = 下排 (产业链2), 无 down = 上排 (产业链1)。

不是每个概念都有图谱: 361 个概念里约四分之一有。

落库表 ths_concept_graph (单表三层, level 区分):
  level=1 概念本身   chain_index=0, chain_label=NULL, node_name=概念名
  level=2 产业链子板块 chain_index=1..N, chain_label=产业链N, node_name=子板块名
  level=3 个股       ts_code 有值, node_name=股票名

命令行:
  python3 fetch_ths_concept_graph.py --write
  python3 fetch_ths_concept_graph.py --boards 5        # 试跑
"""
import argparse
import os
import re
import sqlite3
import sys
import time

import fetch_ths_concepts as f

DB_PATH = f.DB_PATH

_RE_CHAIN = re.compile(r'<div class="series">\s*<span class="item">([^<]+)</span>')
_RE_L1 = re.compile(r'class="label label-l"[^>]*>\s*<p class="title">([^<]+)</p>\s*'
                    r'<p class="data[^"]*">([^<]*)</p>')
_RE_LABEL_M = re.compile(r'class="label label-m([^"]*)"[^>]*>\s*<p class="title">([^<]+)</p>')
_RE_STOCK = re.compile(r'href="http://stockpage\.10jqka\.com\.cn/(\d{6})/"[^>]*'
                       r'class="label label-s[^"]*"[^>]*>\s*<p class="title">([^<]*)</p>(.*?)</a>',
                       re.S)
_RE_DATA = re.compile(r'<p class="data[^"]*">([^<]*)</p>')


def _parse_data(text):
    """'-1.49%&nbsp;&nbsp;+-30.85' → (-1.49, -30.85); '-1.28%  6.92' → (-1.28, -6.92)."""
    raw = re.sub(r'&nbsp;|\s+', ' ', str(text or '')).strip()
    parts = raw.split()
    pct = None
    amt = None
    if parts:
        pct = _to_float(parts[0].replace('%', ''))
    if len(parts) > 1:
        token = parts[1]
        if token.startswith('+-'):
            token = token[1:]          # '+-33.44' → '-33.44'
        amt = _to_float(token)
    if pct is not None and amt is not None and pct < 0 and amt > 0:
        amt = -amt          # THS 跌的时候把涨跌额的负号吞了, 按涨跌幅补回来
    return pct, amt


def _to_float(text):
    try:
        return float(str(text).replace(',', '').replace('+', ''))
    except (TypeError, ValueError):
        return None


def parse_graph(html):
    """从详情页 HTML 里解析概念图谱. 返回 dict 或 None (该概念没有图谱)."""
    if '概念图谱' not in html:
        return None
    start = html.find('概念图谱')
    end = html.find('<h2>', start + 10)
    block = html[start:end] if end > 0 else html[start:start + 60000]

    l1 = _RE_L1.search(block)
    if not l1:
        return None
    concept_name, concept_data = l1.group(1).strip(), l1.group(2)
    concept_pct, concept_amt = _parse_data(concept_data)

    chains = [c.strip() for c in _RE_CHAIN.findall(block)]

    # thead: 每个 td 一个子板块 (列); tbody 同列 td 是该子板块的个股
    thead_m = re.search(r'<thead>(.*?)</thead>', block, re.S)
    tbody_m = re.search(r'<tbody>(.*?)</tbody>', block, re.S)
    head_cells = re.findall(r'<td>(.*?)</td>', thead_m.group(1), re.S) if thead_m else []
    body_cells = re.findall(r'<td>(.*?)</td>', tbody_m.group(1), re.S) if tbody_m else []

    subs = []          # [(子板块名, 产业链序号)]
    for cell in head_cells:
        m = _RE_LABEL_M.search(cell)
        if not m:
            continue
        cls, name = m.group(1), m.group(2).strip()
        # down = 下排 → 产业链2, 其余 = 上排 → 产业链1
        chain_index = 2 if 'down' in cls else 1
        subs.append((name, chain_index))

    stocks = []        # [(子板块名, 序号, 代码, 名称, pct, amt)]
    for idx, cell in enumerate(body_cells):
        sub_name = subs[idx][0] if idx < len(subs) else ''
        for seq, sm in enumerate(_RE_STOCK.finditer(cell)):
            code, name, tail = sm.group(1), sm.group(2).strip(), sm.group(3)
            dm = _RE_DATA.search(tail)
            pct, amt = _parse_data(dm.group(1) if dm else '')
            stocks.append((sub_name, seq, code, name, pct, amt))

    return {
        'concept_name': concept_name,
        'concept_pct': concept_pct,
        'concept_amt': concept_amt,
        'chains': chains,
        'subs': subs,
        'stocks': stocks,
    }


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ths_concept_graph (
        concept_code TEXT NOT NULL,
        level INTEGER NOT NULL,           -- 1=概念 2=产业链子板块 3=个股
        seq INTEGER NOT NULL,             -- 同层内的顺序
        chain_index INTEGER,              -- 0=概念本身, 1..N=产业链
        chain_label TEXT,                 -- '产业链1'
        node_name TEXT,
        parent_name TEXT,                 -- level=3: 所属子板块名
        ts_code TEXT,                     -- level=3 才有
        change_pct REAL,
        change_amt REAL,
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (concept_code, level, seq)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ths_graph_stock ON ths_concept_graph(ts_code)')
    conn.commit()


def write_graph(conn, code, graph):
    conn.execute('DELETE FROM ths_concept_graph WHERE concept_code=?', (code,))
    rows = [(code, 1, 0, 0, None, graph['concept_name'], None, None,
             graph['concept_pct'], graph['concept_amt'])]
    for i, (name, chain_index) in enumerate(graph['subs'], 1):
        label = graph['chains'][chain_index - 1] if chain_index - 1 < len(graph['chains']) else None
        rows.append((code, 2, i, chain_index, label, name, None, None, None, None))
    for i, (sub_name, seq, s_code, s_name, pct, amt) in enumerate(graph['stocks'], 1):
        rows.append((code, 3, i, None, None, s_name, sub_name, f"{s_code}.{f._market(s_code)}"
                     if f._market(s_code) else s_code, pct, amt))
    conn.executemany('''INSERT OR REPLACE INTO ths_concept_graph
        (concept_code, level, seq, chain_index, chain_label, node_name, parent_name, ts_code,
         change_pct, change_amt) VALUES (?,?,?,?,?,?,?,?,?,?)''', rows)
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--boards', type=int, default=None, help='只抓前 N 个板块')
    ap.add_argument('--codes', default=None, help='只抓指定板块代码, 逗号分隔')
    ap.add_argument('--delay', type=float, default=0.25)
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--db', default=DB_PATH)
    args = ap.parse_args()

    boards = f.fetch_concept_list()
    if args.codes:
        want = {c.strip() for c in args.codes.split(',') if c.strip()}
        boards = [b for b in boards if b[0] in want]
    elif args.boards:
        boards = boards[:args.boards]

    conn = sqlite3.connect(args.db, timeout=60)
    ensure_schema(conn)
    stats = {'with_graph': 0, 'without_graph': 0, 'subs': 0, 'stocks': 0, 'failed': []}
    for idx, (code, name) in enumerate(boards, 1):
        try:
            html = f._http_get(f.DETAIL_URL.format(code=code), referer=f.LIST_URL)
        except Exception as exc:  # noqa: BLE001
            stats['failed'].append((code, name, str(exc)))
            continue
        graph = parse_graph(html)
        if graph:
            stats['with_graph'] += 1
            stats['subs'] += len(graph['subs'])
            stats['stocks'] += len(graph['stocks'])
            if len(graph['chains']) != 2:
                stats.setdefault('odd_chains', []).append((name, graph['chains']))
            if args.write:
                write_graph(conn, code, graph)
        else:
            stats['without_graph'] += 1
        if idx % 20 == 0 or idx == len(boards):
            print(f'[{idx}/{len(boards)}] 有图谱 {stats["with_graph"]} / 无 {stats["without_graph"]} '
                  f'(子板块 {stats["subs"]}, 个股 {stats["stocks"]})', flush=True)
        time.sleep(args.delay)
    conn.close()
    print(f"\n完成: 有图谱 {stats['with_graph']} 个概念, 子板块 {stats['subs']}, 个股 {stats['stocks']}, "
          f"失败 {len(stats['failed'])}")
    for name, chains in stats.get('odd_chains', []):
        print(f'  产业链数不为 2: {name} → {chains}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
