"""
补齐板块(行业/概念)主力净流入历史缺口 (2026-04-09 ~ 2026-09-14).

【为什么走 push2his daykline】
  每日任务走 push2 clist/get, 只拿当日排行, 历史区间拿不到.
  push2his.eastmoney.com/api/qt/stock/fflow/daykline/get 一次返回某 secid
  最近 ~120 交易日的日 K 线, 带主力 / 超大 / 大 / 中 / 小 净额和占比.
  板块 secid 形态: 90.BKxxxx (90 = 东财板块市场固定前缀).

【字段映射 (跟个股 backfill_flow_daykline 一致, 标号对得齐)】
  kline 逗号串顺序: 日期, 主力净额, 小单净额, 中单净额, 大单净额, 超大单净额,
                   主力占比, 小单占比, 中单占比, 大单占比, 超大单占比,
                   收盘, 涨跌幅, lead_stock_name(0.00), lead_stock_code(0.00)
  后两列在 daykline 接口恒为 0.00, 本脚本不入 lead_stock_* (与现有数据保持一致).

【落库】
  sector_fund_flow, source='eastmoney-daykline' (沿用每日任务的 eastmoney-push2 同一表, 便于回滚).
  已有 (trade_date, sector_type, sector_code) 一律不覆盖: UNIQUE 约束 + INSERT OR IGNORE.

【口径验证 2026-10-04】
  BK1064 在 2026-09-15 的 daykline 数据 vs 现存 eastmoney-push2 行: 字段一致, 数值差距 < 0.51.

【执行通道 v16: playwright 真浏览器 + 主页预热 + 长间隔 + page.goto API】
  v14/v15 走 curl 直连, 反爬在第二层判定 "请求里穿 request 时空响应关连接",
  因为 curl 是 libcurl (TLS 指纹 + HTTP/1.1) 而非真浏览器 (BoringSSL + HTTP/2).
  v16 切到 playwright Chromium:
    - 启动 headless chromium, 启一个 context + page
    - 预热: page.goto 2 个真板块页, 攒 cookie + 浏览历史
    - 每个板块: page.goto API URL (浏览器真 navigate), 走真浏览器网络栈
      - 不能用 context.request: playwright APIRequestContext 是 raw TCP client, 不是真浏览器
      - 不能用 page.evaluate fetch: CORS 跨源拦截
      - page.goto 是唯一真浏览器网络栈 (BoringSSL TLS + HTTP/2 + 浏览器频率)
    - 间隔人味化: 2.0-4.0 秒随机, 每 8 个歇 15 秒, 失败退避指数级
  v13 当时同路径 (context.request) 挂了, v16 用 page.goto + 预热 + 人味间隔, 唯一能绕 push2his 反爬的路.

【断点续跑】
  sector_fflow_progress 表逐板块记状态, Ctrl-C 不丢, 重跑自动续.

用法:
  python3 backfill_sector_flow_daykline.py --status          # 只看进度
  python3 backfill_sector_flow_daykline.py --limit 5         # 5 个试
  python3 backfill_sector_flow_daykline.py                   # 全量 (断点续跑)
  python3 backfill_sector_flow_daykline.py --retry-fail      # 重跑失败
  python3 backfill_sector_flow_daykline.py --date 20260916 --skip-existing  # 单日补
"""
import argparse
import asyncio
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime

from playwright.async_api import async_playwright


# ---------- 常量 ----------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')

DEFAULT_START = '20260409'   # 缺口起点
DEFAULT_END   = '20260914'   # 缺口终点 (含), 之后 eastmoney-push2 已有

SOURCE = 'eastmoney-daykline'
FIELDS1 = 'f1,f2,f3,f7'
FIELDS2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'
DAYKLINE_HOST = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'

# 真浏览器 UA + Referer
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
REFERER = 'https://quote.eastmoney.com/'
ACCEPT_JSON = 'application/json,text/plain,*/*;q=0.9'
ACCEPT_LANG = 'zh-CN,zh;q=0.9,en;q=0.8'

# v16 预热页面: 主页 + 一个真板块页 (让 push2his/data 看到这浏览器来过 + 设 cookie)
WARM_PAGES = [
    'https://quote.eastmoney.com/center/gridlist.html#hs_a_board',
    'https://data.eastmoney.com/bkzj/BK0438.html',
]

# kline 逗号串下标 → 含义 (与个股 backfill 完全一致)
IDX = {
    'date': 0, 'main_net': 1, 'small_net': 2, 'mid_net': 3, 'big_net': 4, 'super_net': 5,
    'main_pct': 6, 'main_net_pct': 6, 'small_pct': 7, 'mid_pct': 8, 'big_pct': 9, 'super_pct': 10,
    'close': 11, 'chg': 12,
}

# 板块 secid 市场前缀 (东财固定: 90 = 板块)
SECTOR_MKT = '90'

# 进度表: 单独一张, 不跟个股的 fflow_backfill_progress 混
PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS sector_fflow_progress (
    sector_code TEXT NOT NULL,
    sector_type TEXT NOT NULL,
    status      TEXT NOT NULL,
    rows        INTEGER DEFAULT 0,
    err         TEXT,
    tries       INTEGER DEFAULT 0,
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (sector_type, sector_code)
)
"""

INSERT_SQL = (
    'INSERT OR IGNORE INTO sector_fund_flow '
    '(trade_date, sector_type, sector_code, name, close, change_pct, '
    ' main_net_inflow, main_net_pct, '
    ' super_net, super_pct, big_net, big_pct, '
    ' mid_net, mid_pct, small_net, small_pct, '
    ' source) '
    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
)

PROGRESS_UPSERT = (
    'INSERT INTO sector_fflow_progress (sector_type, sector_code, status, rows, err, tries) '
    'VALUES (?,?,?,?,?,?) '
    'ON CONFLICT(sector_type, sector_code) DO UPDATE SET '
    'status=excluded.status, rows=excluded.rows, err=excluded.err, '
    'tries=excluded.tries, updated_at=datetime(\'now\',\'localtime\')'
)


def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


# ---------- 数值化 ----------
def _f(v):
    if v is None or v == '-' or v == '':
        return None
    try:
        x = float(v)
        return None if x != x else x  # NaN
    except (ValueError, TypeError):
        return None


# ---------- DB ----------
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(PROGRESS_DDL)
    conn.commit()
    return conn


def load_universe(conn):
    """标的池: concept + industry.

    concept = theme_concept.em_code 并集 sector_fund_flow 已入库的 distinct concept_code.
      原因: tradeiq 拉到的 BK code 落库后才在 theme_concept 里; 反之亦然.
      合并两边避免概念池僵在某个 snapshot (9/16 那次 v16 漏的 6 个就是单边依赖 theme_concept 漏的).
    industry: sector_fund_flow 里已有的 industry 段代码 + 名称 (04-09 ~ 09-14 期间行业列表相对稳定).
    """
    # concept: theme_concept.em_code
    from_theme = {(r[0], r[1]) for r in conn.execute(
        "SELECT em_code, em_name FROM theme_concept "
        "WHERE em_code IS NOT NULL AND em_code != ''").fetchall()}
    # concept: tradeiq 已入库的
    from_data = {(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT sector_code, name FROM sector_fund_flow "
        "WHERE sector_type='concept'").fetchall()}
    # name 取两边都有的优先,任一边有就用那边的
    concept_map = {}
    for code, name in from_theme | from_data:
        if code in concept_map:
            cur = concept_map[code]
            if not cur and name:
                concept_map[code] = name
        else:
            concept_map[code] = name
    concept = sorted(concept_map.items())

    # industry: sector_fund_flow 里已有的 industry 段代码 + 名称
    industry = [(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT sector_code, name FROM sector_fund_flow "
        "WHERE sector_type='industry' AND name IS NOT NULL").fetchall()]
    return [('concept', c, n) for c, n in concept] + [('industry', c, n) for c, n in industry]


def get_progress(conn):
    return {(r[0], r[1]): r[2] for r in conn.execute(
        'SELECT sector_type, sector_code, status FROM sector_fflow_progress')}


def get_progress_full(conn):
    """完整 progress: status + tries (供 pick_pending 用失败上限判断)."""
    return {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
        'SELECT sector_type, sector_code, status, tries FROM sector_fflow_progress')}


def pick_pending(conn, universe, retry_fail, limit, max_fail_tries=3):
    """挑选待跑.

    - 没记录 → 进 pending (新标的)
    - done → 跳过 (永久完成)
    - empty → 跳过 (API 返空 = 当时没数据, 不会突然有)
    - fail → 默认进 pending 重试, 但连续失败次数 >= max_fail_tries 的跳过 (避免死循环)
            --retry-fail 强制全 retry (兼容旧 CLI 习惯, 不再因 tries 跳过)

    历史坑: 9/16 v16 跑后缺 6 个 BK code (BK0884/BK0952/BK1106/BK1128/BK1134/BK1184),
    因为那天 theme_concept 还没收录这 6 个新 code; load_universe 已合并 tradeiq 入库的 sector_code,
    这里再默认重试 fail, 定时任务就既能兜底"概念池漏的"也能兜底"上次网络抖失败".
    """
    prog = get_progress_full(conn)
    pend = []
    for st, code, name in universe:
        if (st, code) not in prog:
            pend.append((st, code, name))
            continue
        status, tries = prog[(st, code)]
        if status in ('done', 'empty'):
            continue
        # status == 'fail'
        if retry_fail:
            pend.append((st, code, name))
        elif (tries or 0) < max_fail_tries:
            pend.append((st, code, name))
    return pend[:limit] if limit and limit > 0 else pend


def pick_pending_for_date(conn, universe, date_str):
    """单日补: 排除 sector_fund_flow 已存在的 (trade_date, sector_type, sector_code)."""
    rows = conn.execute(
        'SELECT sector_type, sector_code FROM sector_fund_flow WHERE trade_date=?',
        (date_str,),
    ).fetchall()
    have = set((r[0], r[1]) for r in rows)
    return [(st, code, name) for st, code, name in universe if (st, code) not in have]


def show_status(conn, universe):
    prog = get_progress(conn)
    by_type = {'concept': [0, 0, 0], 'industry': [0, 0, 0]}  # done, empty, fail
    rows_total = 0
    for st, code, _ in universe:
        s = prog.get((st, code))
        if s == 'done':
            by_type[st][0] += 1
        elif s == 'empty':
            by_type[st][1] += 1
        elif s == 'fail':
            by_type[st][2] += 1
    rows_total = conn.execute(
        "SELECT COALESCE(SUM(rows),0) FROM sector_fflow_progress WHERE status='done'").fetchone()[0]
    days = conn.execute(
        "SELECT COUNT(DISTINCT trade_date) FROM sector_fund_flow WHERE source=? "
        "AND trade_date BETWEEN ? AND ?", (SOURCE, DEFAULT_START, DEFAULT_END)).fetchone()[0]
    print(f'concept {by_type["concept"]}  industry {by_type["industry"]}')
    print(f'累计入库 {int(rows_total):,} 行, 覆盖交易日 {days}')
    for st in ('concept', 'industry'):
        d, e, f = by_type[st]
        total = d + e + f
        print(f'  {st:8s} 池子 {total:>4}  done {d:>4}  empty {e:>3}  fail {f:>3}')


# ---------- 抓取 (v16: playwright 真浏览器通道) ----------
def build_url(secid):
    return (f'{DAYKLINE_HOST}?lmt=0&klt=101&secid={secid}'
            f'&fields1={FIELDS1}&fields2={FIELDS2}')


async def launch_browser():
    """启 playwright chromium + context + page, 预热访问真页面.

    返回 (playwright, browser, context, page).
    page 和 context 给 fetch_klines 用, 预热后 cookie / 浏览历史 / TLS 指纹都是真浏览器的.
    """
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=UA,
        locale='zh-CN',
        viewport={'width': 1440, 'height': 900},
        ignore_https_errors=True,
        extra_http_headers={
            'Accept-Language': ACCEPT_LANG,
        },
    )
    # 路由: 屏蔽图片/媒体加速, 但 XHR/fetch (API 调用) 放行
    async def _route_handler(route, request):
        rt = request.resource_type
        if rt in ('image', 'media', 'font'):
            await route.abort()
        else:
            await route.continue_()
    await context.route('**/*', _route_handler)

    page = await context.new_page()

    log('playwright 预热: 访问真页面攒 cookie + 浏览历史...')
    for url in WARM_PAGES:
        try:
            t0 = time.time()
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(random.uniform(2.5, 4.0))
            # 模拟鼠标小范围移动, 让站点判定 "人在看"
            try:
                await page.mouse.move(random.randint(200, 1000), random.randint(200, 600))
                await asyncio.sleep(random.uniform(0.3, 0.8))
            except Exception:
                pass
            log(f'  ✓ {url[:80]} ({time.time()-t0:.1f}s)')
        except Exception as e:
            log(f'  ⚠ {url[:80]}: {e}')

    cookies = await context.cookies()
    log(f'预热完成: cookie {len(cookies)} 条, 准备开始拉 API')
    return pw, browser, context, page


async def fetch_klines(secid, cfg, page):
    """用 page.goto 真实 navigate 到 API URL, 走真浏览器网络栈.

    为什么不用 context.request: playwright APIRequestContext 底层是 raw TCP client,
    TLS 指纹 / HTTP/2 SETTINGS 跟真浏览器不一致, push2his 一律 RST.
    page.goto 走 Chromium 网络栈: TLS 是真浏览器 BoringSSL, HTTP/2 帧是 Chromium 的,
    cookie 共享 context, 频率是浏览器渲染级别 — 反爬判不到.

    浏览器把 JSON 渲染成 raw text, page.content() 提取 JSON 字符串再 parse.
    err=None           → 成功 (klines 可能空数组)
    err='empty: ...'   → 接口正常返回但 data 为空
    err='<异常>'       → 真失败 (被限流 / 超时), 可 --retry-fail 重来
    """
    url = build_url(secid)
    last = None
    null_streak = 0
    for attempt in range(1, cfg.max_tries + 1):
        try:
            response = await page.goto(
                url,
                wait_until='domcontentloaded',
                timeout=max(1, cfg.timeout_ms // 1000) * 1000,
            )
            status = response.status if response else 0
            if status != 200:
                raise RuntimeError(f'HTTP {status}')
            content = await page.content()
            # 浏览器把 JSON 渲染到 <html><body><pre>{...}</pre></body></html>
            import re
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if not m:
                raise RuntimeError(f'页面无 JSON: {content[:80]}')
            body = json.loads(m.group(0))
            data = body.get('data')
            if not data:
                null_streak += 1
                if null_streak >= 2:
                    return None, attempt, 'empty: 接口返回 data 为空'
                await asyncio.sleep(cfg.backoff_base + random.uniform(0, 0.8))
                continue
            return (data.get('klines') or []), attempt, None
        except Exception as e:
            last = e
            null_streak = 0
            if attempt < cfg.max_tries:
                delay = min(cfg.backoff_max, cfg.backoff_base * (2 ** (attempt - 1)))
                await asyncio.sleep(delay + random.uniform(0, 0.8))
    return None, cfg.max_tries, f'{type(last).__name__}: {str(last)[:120]}'


# ---------- 解析 ----------
def parse_klines(klines, start, end, name):
    out = []
    for line in klines or []:
        p = line.split(',')
        if len(p) < 13:
            continue
        d = p[IDX['date']].replace('-', '')
        if len(d) != 8 or not (start <= d <= end):
            continue
        out.append({
            'trade_date': d,
            'name': name,
            'close': _f(p[IDX['close']]),
            'change_pct': _f(p[IDX['chg']]),
            'main_net_inflow': _f(p[IDX['main_net']]) or 0.0,
            'main_net_pct': _f(p[IDX['main_net_pct']]),
            'super_net': _f(p[IDX['super_net']]) or 0.0,
            'super_pct': _f(p[IDX['super_pct']]),
            'big_net': _f(p[IDX['big_net']]) or 0.0,
            'big_pct': _f(p[IDX['big_pct']]),
            'mid_net': _f(p[IDX['mid_net']]) or 0.0,
            'mid_pct': _f(p[IDX['mid_pct']]),
            'small_net': _f(p[IDX['small_net']]) or 0.0,
            'small_pct': _f(p[IDX['small_pct']]),
        })
    return out


def write_rows(conn, cur, sector_type, sector_code, rows):
    inserted = skipped = 0
    for r in rows:
        cur.execute(INSERT_SQL, (
            r['trade_date'], sector_type, sector_code, r['name'],
            r['close'], r['change_pct'],
            r['main_net_inflow'], r['main_net_pct'],
            r['super_net'], r['super_pct'], r['big_net'], r['big_pct'],
            r['mid_net'], r['mid_pct'], r['small_net'], r['small_pct'],
            SOURCE,
        ))
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped


# ---------- 主流程 ----------
async def worker(wid, pending, cfg, conn, cur, state, page):
    n = len(pending)
    fail_streak = 0
    for i, (stype, code, name) in enumerate(pending, 1):
        secid = f'{SECTOR_MKT}.{code}'
        klines, tries, err = await fetch_klines(secid, cfg, page)
        if err:
            tag = 'empty' if err.startswith('empty:') else 'fail'
            state['empty' if tag == 'empty' else 'failed'] += 1
            if not cfg.dry_run:
                cur.execute(PROGRESS_UPSERT, (stype, code, tag, 0, err, tries))
                conn.commit()
            log(f'  [{wid}] {i}/{n} {stype} {code} {name or "":<8s} ⚠️ {err}')
            fail_streak += 1 if tag == 'fail' else 0
            if fail_streak >= 10:
                log(f'  [{wid}] ⚠️ 连续失败 {fail_streak} 个, 歇 5 分钟')
                await asyncio.sleep(300)
                fail_streak = 0
            continue
        fail_streak = 0

        rows = parse_klines(klines, cfg.start, cfg.end, name or '')
        state['fetched'] += 1
        state['rows'] += len(rows)

        if cfg.dry_run:
            log(f'  [{wid}] {i}/{n} {stype} {code} {name or "":<8s} tries={tries} 缺口 {len(rows)} 行')
        else:
            ins, skp = write_rows(conn, cur, stype, code, rows)
            state['inserted'] += ins
            state['skipped'] += skp
            cur.execute(PROGRESS_UPSERT, (stype, code, 'done', ins, None, tries))
            conn.commit()
            log(f'  [{wid}] {i}/{n} {stype} {code} {name or "":<8s} tries={tries} 入库 {ins} 跳 {skp}')

        # 人味间隔: gap_ms 基础上加 0-2.5 秒随机
        if cfg.gap_ms:
            base = cfg.gap_ms / 1000.0
            await asyncio.sleep(base + random.uniform(0, 2.5))
        if cfg.burst and i % cfg.burst == 0 and i < n:
            log(f'  [{wid}] 歇 {cfg.burst_pause_ms/1000:.0f}s (已跑 {i}/{n})')
            await asyncio.sleep(cfg.burst_pause_ms / 1000.0)


async def run(cfg):
    conn = connect()
    universe = load_universe(conn)
    n_concept = sum(1 for st, _, _ in universe if st == 'concept')
    n_industry = sum(1 for st, _, _ in universe if st == 'industry')
    log(f'概念 {n_concept} 个  行业 {n_industry} 个  合计 {len(universe)} 个')

    if cfg.status:
        show_status(conn, universe)
        conn.close()
        return

    if cfg.reset:
        conn.execute('DELETE FROM sector_fflow_progress')
        conn.commit()
        log('进度表已清空')

    pending = pick_pending(conn, universe, cfg.retry_fail, cfg.limit, cfg.max_fail_tries)
    # 单日补: 不看 progress (那是区间跑的, 单日是另一回事), 全部跑
    if cfg.date:
        if cfg.skip_existing:
            pending = pick_pending_for_date(conn, universe, cfg.date)
            log(f'单日补模式 ({cfg.date}): 跳过已入库, 剩余 {len(pending)} 个')
        else:
            pending = universe
            log(f'单日补模式 ({cfg.date}): 忽略 progress, 全量跑 {len(pending)} 个')
        # 单日补模式下 --limit 也生效 (验证用, 不然小测会跑全量 912 个)
        if cfg.limit and cfg.limit > 0:
            pending = pending[:cfg.limit]
            log(f'  --limit {cfg.limit}: 本轮只跑前 {len(pending)} 个')
    else:
        log(f'本轮待跑 {len(pending)} 个' + ('  [dry-run]' if cfg.dry_run else ''))
    if not pending:
        show_status(conn, universe)
        conn.close()
        return

    workers = max(1, min(cfg.workers, len(pending)))
    shards = [pending[i::workers] for i in range(workers)]
    state = {'fetched': 0, 'failed': 0, 'empty': 0, 'rows': 0,
             'inserted': 0, 'skipped': 0}
    cur = conn.cursor()
    t0 = time.time()

    # v16: 启动 playwright + 主页预热
    log('启动 playwright 浏览器...')
    pw, browser, context, page = await launch_browser()

    log(f'{workers} 个 worker (page.goto 真浏览器网络栈) 就绪, 开始抓...')
    log('提示: Ctrl-C 可随时中断, 已抓的都已落盘, 重跑自动续')

    async def reporter():
        while True:
            await asyncio.sleep(30)
            el = time.time() - t0
            rate = state['fetched'] / el if el else 0
            eta = (len(pending) - state['fetched']) / rate if rate > 0 else 0
            log(f'进度 {state["fetched"]}/{len(pending)}  缺口行 {state["rows"]:,}  '
                f'已入库 {state["inserted"]:,}  跳 {state["skipped"]:,}  '
                f'无数据 {state["empty"]}  失败 {state["failed"]}  '
                f'{rate:.2f} 个/s  已用 {el/60:.1f}min  预计还需 {eta/60:.0f}min')
    rep = asyncio.create_task(reporter())

    try:
        await asyncio.gather(*[
            worker(i + 1, shards[i], cfg, conn, cur, state, page)
            for i in range(workers)
        ])
    except KeyboardInterrupt:
        log('已中断. 进度已保存, 重跑自动续.')
    finally:
        rep.cancel()
        try:
            await page.close()
            await context.close()
            await browser.close()
            await pw.stop()
        except Exception as e:
            log(f'清理 playwright 资源失败: {e}')

    el = (time.time() - t0) / 60
    log(f'抓取结束 用时 {el:.1f}min  成功 {state["fetched"]}  无数据 {state["empty"]}  '
        f'失败 {state["failed"]}  缺口行 {state["rows"]:,}')
    if cfg.dry_run:
        log(f'dry-run: 没写库, 累计 {state["rows"]:,} 行')
    else:
        log(f'入库完成: 新增 {state["inserted"]:,} 行, 跳过 {state["skipped"]:,}')
    show_status(conn, universe)
    conn.close()


def main():
    ap = argparse.ArgumentParser(description='补齐板块(行业/概念)主力净流入历史缺口')
    ap.add_argument('--start', default=DEFAULT_START)
    ap.add_argument('--end', default=DEFAULT_END)
    ap.add_argument('--workers', type=int, default=1, help='v16 强烈建议 1, 多 worker 容易被盯防')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--max-tries', type=int, default=4)
    ap.add_argument('--backoff-base', type=float, default=2.0)
    ap.add_argument('--backoff-max', type=float, default=45.0)
    ap.add_argument('--gap-ms', type=int, default=2500, help='v16 人味化: 基础间隔 + 0-2.5s 随机')
    ap.add_argument('--burst', type=int, default=8)
    ap.add_argument('--burst-pause-ms', type=int, default=15000)
    ap.add_argument('--timeout-ms', type=int, default=25000)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--retry-fail', action='store_true', help='强制重试 fail(忽略 tries 上限), 用于临时排查')
    ap.add_argument('--max-fail-tries', type=int, default=3, help='fail 状态连续失败 N 次后跳过 (避免死循环; --retry-fail 会绕过此上限)')
    ap.add_argument('--reset', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--skip-existing', action='store_true', help='单日补时跳过 sector_fund_flow 已存在的 (sector_type, sector_code)')
    # 单日补: --date 20260916 -> start=end=20260916, 覆盖区间参数
    ap.add_argument('--date', help='只补某一天 (YYYYMMDD 或 YYYY-MM-DD)')
    cfg = ap.parse_args()

    if cfg.date:
        d = cfg.date.replace('-', '')
        cfg.start = d
        cfg.end = d

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log('已中断.')


if __name__ == '__main__':
    main()