"""抓取同花顺「概念板块」列表 + 每个概念的成分股 → ths_concept / ths_concept_stock 表。

为什么不用 ajax 翻页:
  q.10jqka.com.cn 的 ajax 端点 (…/ajax/1/…) 有 WAF (chameleon), 直接请求返回 401 + 跳转脚本.
  但普通的分页 URL (…/detail/code/{code}/page/{n}/) 直接 curl 就能拿到渲染好的表格, 不需要 hexin-v.

页面事实 (2026-10-29 实测):
  - GBK 编码
  - 列表页 https://q.10jqka.com.cn/gn/ 侧栏 cate_items 里是全部 361 个概念 (code + 名称)
  - 详情页 成分股表格: 序号/代码/名称/现价/涨跌幅/涨跌/涨速/换手/量比/振幅/成交额/流通股/流通市值/市盈率
  - 每页 27 行, 总页数在 <span class="page_info">1/18</span>
  - 金额列是 '7.27亿' / '5391.24万' 文本
  - 页面默认按涨跌幅降序, 翻页顺序一致

命令行:
  python3 fetch_ths_concepts.py --write                 # 全量抓 + 落库
  python3 fetch_ths_concepts.py --boards 3 --write      # 只抓 3 个板块试跑
  python3 fetch_ths_concepts.py --list-only             # 只打印板块清单
"""
import argparse
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')

# 同花顺四类板块: 概念 / 同花顺行业 / 证监会行业 / 地域
BOARD_TYPES = {
    'concept': {
        'label': '概念板块',
        'path': 'gn',
        'list': 'https://q.10jqka.com.cn/gn/',
        'detail': 'http://q.10jqka.com.cn/gn/detail/code/{code}/',
        'page': 'http://q.10jqka.com.cn/gn/detail/code/{code}/page/{page}/',
        'code_re': r'[0-9]+',
    },
    'thshy': {
        'label': '同花顺行业',
        'path': 'thshy',
        'list': 'https://q.10jqka.com.cn/thshy/',
        'detail': 'http://q.10jqka.com.cn/thshy/detail/code/{code}/',
        'page': 'http://q.10jqka.com.cn/thshy/detail/code/{code}/page/{page}/',
        'code_re': r'[0-9]+',
    },
    'zjhhy': {
        'label': '证监会行业',
        'path': 'zjhhy',
        'list': 'https://q.10jqka.com.cn/zjhhy/',
        'detail': 'http://q.10jqka.com.cn/zjhhy/detail/code/{code}/',
        'page': 'http://q.10jqka.com.cn/zjhhy/detail/code/{code}/page/{page}/',
        'code_re': r'[A-Z]',
    },
    'dy': {
        'label': '地域板块',
        'path': 'dy',
        'list': 'https://q.10jqka.com.cn/dy/',
        'detail': 'http://q.10jqka.com.cn/dy/detail/code/{code}/',
        'page': 'http://q.10jqka.com.cn/dy/detail/code/{code}/page/{page}/',
        'code_re': r'[0-9]+',
    },
}
LIST_URL = BOARD_TYPES['concept']['list']
DETAIL_URL = BOARD_TYPES['concept']['detail']
PAGE_URL = BOARD_TYPES['concept']['page']

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

# 同花顺会按 IP 封禁 (直连全线 403 "Nginx forbidden")。设了这个值就走代理换出口 IP。
# 命令行: --proxy http://127.0.0.1:7890
PROXY = None

def _board_re(board_type):
    conf = BOARD_TYPES[board_type]
    return re.compile(r'href="http://q\.10jqka\.com\.cn/%s/detail/code/(%s)/"[^>]*>([^<]+)</a>'
                      % (conf['path'], conf['code_re']))

_RE_PAGEINFO = re.compile(r'page_info">\d+/(\d+)<')
_RE_ROWS = re.compile(r'<tr>(.*?)</tr>', re.S)
_RE_TD = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
_RE_CODE = re.compile(r'stockpage\.10jqka\.com\.cn/(\d{6})')
_RE_NAME = re.compile(r'<a[^>]*>([^<]*)</a>')
_RE_TAG = re.compile(r'<[^>]+>')


def _http_get(url, referer=None, timeout=20, retries=3):
    """GET 一个 URL, 返回解码后的 GBK 文本. 401/5xx 自动重试.

    刻意绕开环境里的系统代理 (Clash): 走代理时同花顺会挂住, 直连才稳.
    """
    headers = {'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'}
    if referer:
        headers['Referer'] = referer
    proxy = PROXY
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode('gbk', errors='replace')
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403, 429, 500, 502, 503):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f'GET {url} 失败: {last}')


def fetch_concept_list(board_type='concept'):
    """返回 [(板块代码, 名称), ...], 顺序按页面 A~Z 分组."""
    conf = BOARD_TYPES[board_type]
    html = _http_get(conf['list'])
    seen, out = set(), []
    for code, name in _board_re(board_type).findall(html):
        name = name.strip()
        if code in seen or not name:
            continue
        seen.add(code)
        out.append((code, name))
    return out


def _num(text):
    """'4.95' / '--' / '' / '7.27亿' → float / None. 金额单位 '亿'/'万' 自动换算成元."""
    s = _RE_TAG.sub('', str(text or '')).replace(',', '').strip()
    if s in ('', '-', '--', 'None'):
        return None
    mult = 1.0
    if s.endswith('亿'):
        mult, s = 1e8, s[:-1]
    elif s.endswith('万'):
        mult, s = 1e4, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _market(code):
    if code.startswith(('60', '68')):
        return 'SH'
    if code.startswith(('00', '30')):
        return 'SZ'
    if code.startswith(('43', '83', '87', '92')):
        return 'BJ'
    return None


def parse_concept_page(html):
    """从详情页 HTML 里解析成分股行. 返回 (page_count, rows).

    rows 每项: dict(ts_code, code, name, close, change_pct, change_amt, speed, turnover_pct,
                    volume_ratio, amplitude, amount, float_share, float_mktcap, pe)
    """
    page_count = 1
    m = _RE_PAGEINFO.search(html)
    if m:
        try:
            page_count = max(1, int(m.group(1)))
        except ValueError:
            page_count = 1

    start = html.find('成分股涨跌排行榜')
    body = html[start:] if start >= 0 else html
    end = body.find('</table>')
    if end > 0:
        body = body[:end]

    rows = []
    for tr in _RE_ROWS.findall(body):
        tds = _RE_TD.findall(tr)
        if len(tds) < 3:
            continue
        code_m = _RE_CODE.search(tds[1])
        if not code_m:
            continue
        code = code_m.group(1)
        name_m = _RE_NAME.search(tds[2])
        name = name_m.group(1).strip() if name_m else ''
        if not name:
            continue
        vals = [_num(td) for td in tds[3:]]
        # 列顺序: 现价/涨跌幅/涨跌/涨速/换手/量比/振幅/成交额/流通股/流通市值/市盈率
        def at(i):
            return vals[i] if i < len(vals) else None
        market = _market(code)
        rows.append({
            'ts_code': f'{code}.{market}' if market else code,
            'code': code,
            'name': name,
            'close': at(0),
            'change_pct': at(1),
            'change_amt': at(2),
            'speed': at(3),
            'turnover_pct': at(4),
            'volume_ratio': at(5),
            'amplitude': at(6),
            'amount': at(7),
            'float_share': at(8),
            'float_mktcap': at(9),
            'pe': at(10),
        })
    return page_count, rows


def fetch_concept(code, max_pages=None, delay=0.2, board_type='concept'):
    """抓一个板块的全部成分股. 返回 (page_count, rows)."""
    conf = BOARD_TYPES[board_type]
    url = conf['detail'].format(code=code)
    html = _http_get(url, referer=conf['list'])
    page_count, rows = parse_concept_page(html)
    total_pages = page_count if max_pages is None else min(page_count, max_pages)
    for page in range(2, total_pages + 1):
        time.sleep(delay + random.uniform(0, 0.15))
        html = _http_get(conf['page'].format(code=code, page=page), referer=url)
        _, page_rows = parse_concept_page(html)
        if not page_rows:
            break
        rows.extend(page_rows)
    return page_count, rows


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ths_concept (
        concept_code TEXT PRIMARY KEY,     -- 同花顺板块代码 (如 300800)
        name TEXT NOT NULL,
        board_type TEXT DEFAULT 'concept', -- concept/thshy/zjhhy/dy
        stock_count INTEGER DEFAULT 0,
        page_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS ths_concept_stock (
        concept_code TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        name TEXT,
        board_type TEXT DEFAULT 'concept',
        close REAL, change_pct REAL, change_amt REAL, speed REAL,
        turnover_pct REAL, volume_ratio REAL, amplitude REAL,
        amount REAL, float_share REAL, float_mktcap REAL, pe REAL,
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (concept_code, ts_code)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ths_concept_stock_code ON ths_concept_stock(ts_code)')
    # 老库补列
    for table in ('ths_concept', 'ths_concept_stock'):
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if 'board_type' not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN board_type TEXT DEFAULT 'concept'")
    conn.commit()


def write_concept(conn, code, name, page_count, rows, board_type='concept'):
    """覆盖式写入: 删掉这个板块旧成分股再写新的.

    保护: 抓到 0 行 (被 WAF 挡了) 时不写, 否则会把库里已有的好数据抹掉.
    """
    if not rows:
        return False
    old = conn.execute('SELECT stock_count FROM ths_concept WHERE concept_code=?',
                       (code,)).fetchone()
    if old and old[0] and len(rows) < old[0] * 0.5:
        return False        # 新数据明显比旧数据少 → 多半是抓断了, 保住旧数据
    conn.execute('DELETE FROM ths_concept_stock WHERE concept_code=?', (code,))
    conn.executemany('''INSERT OR REPLACE INTO ths_concept_stock
        (concept_code, ts_code, name, board_type, close, change_pct, change_amt, speed, turnover_pct,
         volume_ratio, amplitude, amount, float_share, float_mktcap, pe)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [(code, r['ts_code'], r['name'], board_type, r['close'], r['change_pct'], r['change_amt'],
          r['speed'], r['turnover_pct'], r['volume_ratio'], r['amplitude'], r['amount'],
          r['float_share'], r['float_mktcap'], r['pe']) for r in rows])
    conn.execute('''INSERT OR REPLACE INTO ths_concept
        (concept_code, name, board_type, stock_count, page_count, updated_at)
        VALUES (?,?,?,?,?,datetime('now','localtime'))''',
        (code, name, board_type, len(rows), page_count))
    conn.commit()
    return True


def sync_concepts(boards=None, max_pages=None, delay=0.2, write=False,
                  progress_callback=None, db_path=DB_PATH, resume=False, board_type='concept'):
    """抓取全部 (或指定数量的) 同花顺板块成分股.

    boards: None=全部, 整数=只抓前 N 个, 列表=[(code,name)]=指定板块
    resume: True=跳过库里已有的板块 (断点续抓)
    """
    all_boards = fetch_concept_list(board_type)
    if boards is None:
        boards = all_boards
    elif isinstance(boards, int):
        boards = all_boards[:boards]
    conn = sqlite3.connect(db_path, timeout=60)
    ensure_schema(conn)
    stats = {'boards': 0, 'stocks': 0, 'skipped': 0, 'failed': []}
    for idx, (code, name) in enumerate(boards, 1):
        if resume:
            done = conn.execute('SELECT stock_count, page_count FROM ths_concept '
                                'WHERE concept_code=?', (code,)).fetchone()
            # 只有看着抓全了 (行数 >= 页数*5) 才跳过, 截断的板块要重抓
            if done and done[0] and ((done[1] or 1) <= 1 or done[0] >= (done[1] or 1) * 5):
                stats['skipped'] += 1
                continue
        try:
            page_count, rows = fetch_concept(code, max_pages=max_pages, delay=delay,
                                             board_type=board_type)
        except Exception as exc:  # noqa: BLE001 - 单板块失败不中断整体
            stats['failed'].append((code, name, str(exc)))
            print(f'[{idx}/{len(boards)}] {name}({code}) 失败: {exc}', flush=True)
            continue
        if write:
            write_concept(conn, code, name, page_count, rows, board_type)
        stats['boards'] += 1
        stats['stocks'] += len(rows)
        print(f'[{idx}/{len(boards)}] {name}({code}) {page_count}页 {len(rows)}只', flush=True)
        if progress_callback:
            progress_callback(idx, len(boards), code, name, len(rows))
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--boards', type=int, default=None, help='只抓前 N 个板块 (默认全量 361)')
    ap.add_argument('--max-pages', type=int, default=None, help='每个板块最多抓几页')
    ap.add_argument('--delay', type=float, default=0.2, help='翻页间隔秒')
    ap.add_argument('--write', action='store_true', help='写入数据库')
    ap.add_argument('--db', default=DB_PATH, help='目标数据库 (默认 market_data.db)')
    ap.add_argument('--resume', action='store_true', help='跳过库里已有的板块')
    ap.add_argument('--list-only', action='store_true', help='只打印板块清单')
    ap.add_argument('--type', default='concept', choices=sorted(BOARD_TYPES),
                    help='板块类型: concept=概念 thshy=同花顺行业 zjhhy=证监会行业 dy=地域')
    ap.add_argument('--proxy', default=None, help='HTTP 代理 (如 http://127.0.0.1:7890), 直连被封时用')
    args = ap.parse_args()
    if args.proxy:
        globals()['PROXY'] = args.proxy

    if args.list_only:
        for code, name in fetch_concept_list(args.type):
            print(f'{code}\t{name}')
        return 0

    stats = sync_concepts(boards=args.boards, max_pages=args.max_pages,
                          delay=args.delay, write=args.write, db_path=args.db,
                          resume=args.resume, board_type=args.type)
    print(f"\n完成: {stats['boards']} 个板块, {stats['stocks']} 条成分股, 失败 {len(stats['failed'])}")
    for code, name, err in stats['failed']:
        print(f'  失败 {name}({code}): {err}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
