"""同花顺概念板块全量成分股 (需要登录一次)。

为什么要有这个脚本:
  同花顺 q.10jqka.com.cn 的概念成分股详情页匿名只能翻到第 5 页, 第 6 页起 302 跳
  upass 登录页 (真实 Chrome、无痕 session、curl 全都一样, 不是反爬误判)。
  所以要拿全量成分股, 得先在真浏览器里登录一次, 之后复用这个会话翻页。

跑法:
  1. python3 fetch_ths_concepts_full.py            # 会弹出 Chrome 窗口
  2. 在弹出的窗口里登录同花顺 (扫码), 脚本自动检测到会话可用就开始抓
  3. 想重跑不用再登录: 会话存在 PROFILE_DIR 里

会话复用方式: 登录后用 context.request.get() 发 HTTP 请求 (带 cookie, 不渲染页面),
比逐页 goto 快一个数量级; 万一 request 拿不到数据会退回 page.goto。

落库: 默认写 backend/ths_concepts.db 的 ths_concept / ths_concept_stock 两张表。
"""
import argparse
import os
import random
import re
import sqlite3
import sys
import time

from playwright.sync_api import sync_playwright

import fetch_ths_concepts as f

PROFILE_DIR = os.path.join('/tmp', 'tradeiq-ths-profile')
LAUNCH_ARGS = ['--window-position=60,60', '--window-size=1200,900',
               '--disable-blink-features=AutomationControlled', '--no-proxy-server']
# 走代理时不能用 --no-proxy-server (它会强制直连), 用下面这组
LAUNCH_ARGS_PROXY = ['--window-position=60,60', '--window-size=1200,900',
                     '--disable-blink-features=AutomationControlled']
LOGIN_CHECK_CODE = '300800'   # 安防, 18 页, 用来探测会话能不能翻到第 6 页
LOGIN_CHECK_PAGE = 6


def _find_deep_probe(ctx, boards, board_type='concept'):
    """找一个页数 > 5 的板块, 用来探测深页权限 (第 6 页起才需要登录/会话)."""
    conf = f.BOARD_TYPES[board_type]
    for code, _name in boards[:25]:
        try:
            resp = ctx.request.get(conf['detail'].format(code=code), timeout=20000)
            html = resp.body().decode('gbk', errors='replace')
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r'page_info">\d+/(\d+)<', html)
        if m and int(m.group(1)) > LOGIN_CHECK_PAGE:
            return code
    return None


def _session_ready(ctx, board_type='concept', probe_code=None):
    """用一次深页请求判断登录会话是否可用."""
    conf = f.BOARD_TYPES[board_type]
    code = probe_code or LOGIN_CHECK_CODE
    url = conf['page'].format(code=code, page=LOGIN_CHECK_PAGE)
    try:
        resp = ctx.request.get(url, headers={'Referer': conf['detail'].format(code=code)},
                               timeout=20000, max_redirects=0)
    except Exception:  # noqa: BLE001
        return 'waf'
    if resp.status in (301, 302, 303, 307):
        return 'need_login'      # 跳登录页 → 必须登录
    if resp.status in (401, 403):
        return 'waf'             # WAF 挑战 → 改用浏览器导航即可, 不需要登录
    body = resp.body()
    try:
        html = body.decode('gbk', errors='replace')
    except Exception:  # noqa: BLE001
        return 'waf'
    return 'ok' if 'stockpage.10jqka.com.cn' in html else 'waf'


def _fetch(ctx, page, code, referer, board_type='concept', browser=None):
    """抓一页详情页 HTML. 拿不到就返回 None.

    同花顺 WAF 的 hexin-v 校验会失效: ctx.request 直连久了会返回 401 (chameleon 挑战页),
    这时必须用真实浏览器导航 (browser.goto), 让页面自己跑 JS 生成校验串。
    """
    url = f.BOARD_TYPES[board_type]['page'].format(code=code, page=page)
    try:
        resp = ctx.request.get(url, headers={'Referer': referer}, timeout=20000)
        if resp.status == 200:
            html = resp.body().decode('gbk', errors='replace')
            if 'stockpage.10jqka.com.cn' in html or 'page_info' in html:
                return html
    except Exception:  # noqa: BLE001
        pass
    if browser is not None:
        try:
            browser.goto(url, wait_until='domcontentloaded', timeout=30000)
            return browser.content()
        except Exception:  # noqa: BLE001
            return None
    return None


def crawl(ctx, boards, max_pages=None, delay=0.15, write=True, db_path=f.DB_PATH, resume=False,
          board_type='concept', browser=None):
    conn = sqlite3.connect(db_path, timeout=60)
    f.ensure_schema(conn)
    stats = {'boards': 0, 'stocks': 0, 'truncated': [], 'failed': [], 'skipped': 0}
    for idx, (code, name) in enumerate(boards, 1):
        if resume:
            done = conn.execute('SELECT stock_count, page_count FROM ths_concept '
                                'WHERE concept_code=?', (code,)).fetchone()
            # 只有看着抓全了 (行数 >= 页数*5) 才跳过, 截断的板块要重抓
            if done and done[0] and ((done[1] or 1) <= 1 or done[0] >= (done[1] or 1) * 5):
                stats['skipped'] += 1
                continue
        referer = f.BOARD_TYPES[board_type]['detail'].format(code=code)
        html = None
        try:
            resp = ctx.request.get(referer, timeout=20000)
            if resp.status == 200:
                cand = resp.body().decode('gbk', errors='replace')
                if '成分股涨跌排行榜' in cand:
                    html = cand
        except Exception:  # noqa: BLE001
            pass
        if html is None and browser is not None:
            try:
                browser.goto(referer, wait_until='domcontentloaded', timeout=30000)
                html = browser.content()
            except Exception as exc:  # noqa: BLE001
                stats['failed'].append((code, name, str(exc)))
                print(f'[{idx}/{len(boards)}] {name}({code}) 首页失败: {exc}', flush=True)
                continue
        if html is None:
            stats['failed'].append((code, name, '首页没拿到成分股表'))
            continue
        page_count, rows = f.parse_concept_page(html)
        total = page_count if max_pages is None else min(page_count, max_pages)
        for page in range(2, total + 1):
            time.sleep(delay + random.uniform(0, 0.1))
            page_html = _fetch(ctx, page, code, referer, board_type, browser)
            if page_html is None:
                stats['truncated'].append((code, name, page_count, len(rows)))
                break
            _, page_rows = f.parse_concept_page(page_html)
            if not page_rows:
                stats['truncated'].append((code, name, page_count, len(rows)))
                break
            rows.extend(page_rows)
        if write:
            f.write_concept(conn, code, name, page_count, rows, board_type)
        stats['boards'] += 1
        stats['stocks'] += len(rows)
        if idx % 10 == 0 or idx == len(boards):
            print(f'[{idx}/{len(boards)}] {name}({code}) {page_count}页 {len(rows)}只 '
                  f'(累计 {stats["stocks"]})', flush=True)
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--boards', type=int, default=None, help='只抓前 N 个板块')
    ap.add_argument('--max-pages', type=int, default=None, help='每个板块最多几页')
    ap.add_argument('--delay', type=float, default=0.15)
    ap.add_argument('--db', default=f.DB_PATH)
    ap.add_argument('--no-write', action='store_true')
    ap.add_argument('--resume', action='store_true', help='跳过库里已有的板块')
    ap.add_argument('--codes', default=None, help='只抓指定板块代码, 逗号分隔 (如 307954,300800)')
    ap.add_argument('--type', default='concept', choices=sorted(f.BOARD_TYPES),
                    help='板块类型: concept=概念 thshy=同花顺行业 zjhhy=证监会行业 dy=地域')
    ap.add_argument('--proxy', default=None, help='HTTP 代理 (如 http://127.0.0.1:7890), 直连被封时用')
    args = ap.parse_args()
    if args.proxy:
        f.PROXY = args.proxy

    boards = f.fetch_concept_list(args.type)
    if args.codes:
        want = {c.strip() for c in args.codes.split(',') if c.strip()}
        boards = [b for b in boards if b[0] in want]
    elif args.boards:
        boards = boards[:args.boards]
    print(f'共 {len(boards)} 个概念板块, 待抓取', flush=True)

    with sync_playwright() as p:
        launch_kwargs = {'proxy': {'server': args.proxy}} if args.proxy else {}
        launch_args = LAUNCH_ARGS_PROXY if args.proxy else LAUNCH_ARGS
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, channel='chrome', headless=False, args=launch_args, **launch_kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f.BOARD_TYPES[args.type]['list'], wait_until='domcontentloaded', timeout=60000)
        print('\n弹出的 Chrome 窗口里请登录同花顺（扫码即可）。登录后脚本会自动继续。\n', flush=True)

        probe = _find_deep_probe(ctx, boards, args.type)
        deadline = time.time() + 600
        state = 'need_login' if probe else 'ok'
        while time.time() < deadline:
            state = _session_ready(ctx, args.type, probe) if probe else 'ok'
            if state in ('ok', 'waf'):
                break
            page.wait_for_timeout(3000)
        if state == 'need_login':
            print('10 分钟内没检测到可用登录会话, 退出。', flush=True)
            ctx.close()
            return 1
        if state == 'waf':
            print('WAF 拦了直连请求, 改用浏览器导航抓取 (慢一些, 但不需要登录)。\n', flush=True)
        else:
            print('会话就绪, 开始抓取。\n', flush=True)

        stats = crawl(ctx, boards, max_pages=args.max_pages, delay=args.delay,
                      write=not args.no_write, db_path=args.db, resume=args.resume,
                      board_type=args.type, browser=page)
        ctx.close()

    print(f'\n完成: {stats["boards"]} 个板块, {stats["stocks"]} 条成分股, '
          f'未抓全 {len(stats["truncated"])} 个, 失败 {len(stats["failed"])}')
    for code, name, pages, got in stats['truncated'][:20]:
        print(f'  未抓全 {name}({code}): 共 {pages} 页, 只拿到 {got} 只')
    return 0


if __name__ == '__main__':
    sys.exit(main())
