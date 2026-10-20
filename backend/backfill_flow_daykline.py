"""
补齐个股主力净流入历史缺口 (2026-04-09 ~ 2026-06-15, 45 个交易日).

【为什么现有两条路都补不了】
  1. 每日任务走 push2.eastmoney.com clist/get —— 只能拿"当日排行", 历史区间拿不到.
  2. backfill_flow.py 走 "5 日累计反推" —— 硬限制在最近 5 个交易日窗口内, 滑出窗口就补不了.
  4 月的缺口对这两条路都是死路, 必须换接口.

【本脚本用的接口】
  push2his.eastmoney.com/api/qt/stock/fflow/daykline/get
  逐股请求, 一次返回该股最近 120 个交易日的历史资金流 (不是按天翻页).
  -> 5500 只股票 = 5500 次请求, 一次就把 45 天的缺口全包了.

⚠️ 有保质期: 服务端只保留最近 120 个交易日 (lmt=500 也突破不了), 且窗口每个交易日
   往前挪一天. 2026-04-09 在 2026-10-09 之后就从接口消失了. 整个 4 月约 2026-06 中前掉光.

【口径已验证 2026-10-02】
  003006 在 20260616 的 daykline 值, 与库里 clist 抓的 eastmoney-push2 逐字段完全一致:
    主力 -1653344 / 超大 0 / 大 -1653344 / 中 9573082 / 小 -7919738
  所以补出来的数据能直接跟现有 6/16 之后的数据拼在一起用, 不用做口径换算.

【字段映射】(逗号串, 位置对应 f51..f65)
  f51 日期        f52 主力净额   f53 小单净额   f54 中单净额   f55 大单净额   f56 超大单净额
  f57 主力占比%   f58 小单占比%  f59 中单占比%  f60 大单占比%  f61 超大单占比%
  f62 收盘价      f63 涨跌幅%     f64/f65 恒为 0
  恒等式 (入库前自检用): 超大+大+中+小 = 0, 且 主力 = 大+超大

【落库】
  fund_flow, source='eastmoney-daykline' (跟每日任务的 eastmoney-push2 分开标, 便于回滚对账)
  已存在的 (trade_date, ts_code) 一律不覆盖 —— fund_flow 上有 UNIQUE 约束, 用 INSERT OR IGNORE,
  只计数不动已有数据.

【顺带对账】
  同一批 kline 里也带着 6/16 之后的数据, 默认跟库里已有的 eastmoney-push2 逐字段比对,
  只报告不覆盖. 实测已发现: 20260731 只入库 1000 只 (应 ~5290)、20260817 缺 ~400 只,
  20260930 是在 14:49 (收盘前) 抓的盘中快照 —— 这几个都是既有问题, 本脚本只报不改.

【执行通道】
  Playwright 真实浏览器 (channel=chrome) 建 context, 用 ctx.request.get() 发请求:
  共享浏览器的 UA / cookie / TLS 指纹, 且不受 CORS 限制, 比 urllib 直连稳得多
  (实测 urllib 直连被东财限流得很惨, 大量 RemoteDisconnected).

【断点续跑】
  fflow_backfill_progress 表逐股记状态, 随时 Ctrl-C 随时续, 不重复请求已经 ok 的.

用法:
  python3 backfill_flow_daykline.py --status             # 只看进度
  python3 backfill_flow_daykline.py --limit 50 --dry-run # 先小批量测速, 不入库
  python3 backfill_flow_daykline.py --limit 50           # 50 只真入库, 看对账结果
  python3 backfill_flow_daykline.py                      # 全量 (断点续跑)
  python3 backfill_flow_daykline.py --workers 4 --headless
  python3 backfill_flow_daykline.py --retry-fail         # 重跑之前失败的
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

DEFAULT_START = '20260409'   # 缺口起点 (东财 120 交易日窗口的最左端)
DEFAULT_END = '20260615'     # 缺口终点 (含), 之后 eastmoney-push2 已有

SOURCE = 'eastmoney-daykline'
PAGE_URL = 'https://data.eastmoney.com/zjlx/detail.html'
FIELDS1 = 'f1,f2,f3,f7'
FIELDS2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'
DAYKLINE_HOST = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'

# kline 逗号串下标 → 含义
IDX = {
    'date': 0, 'main_net': 1, 'small_net': 2, 'mid_net': 3, 'big_net': 4, 'super_net': 5,
    'main_pct': 6, 'small_pct': 7, 'mid_pct': 8, 'big_pct': 9, 'super_pct': 10,
    'close': 11, 'chg': 12,
}

UNIVERSE_SQL = 'SELECT DISTINCT ts_code, name FROM stock_daily WHERE trade_date BETWEEN ? AND ?'

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS fflow_backfill_progress (
    ts_code    TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    rows       INTEGER DEFAULT 0,
    tried_dates TEXT,
    err        TEXT,
    tries      INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
)
"""

RECONCILE_FIELDS = ('main_net_inflow', 'super_net', 'big_net', 'mid_net', 'small_net')
RECON_SQL = f"SELECT {','.join(RECONCILE_FIELDS)}, source FROM fund_flow WHERE trade_date=? AND ts_code=?"

INSERT_SQL = (
    'INSERT OR IGNORE INTO fund_flow '
    '(trade_date, ts_code, code, name, main_net_inflow, main_net_pct, '
    ' super_net, super_pct, big_net, big_pct, mid_net, mid_pct, small_net, small_pct, source) '
    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
)

PROGRESS_UPSERT = (
    'INSERT INTO fflow_backfill_progress (ts_code, status, rows, tried_dates, err, tries) '
    'VALUES (?,?,?,?,?,?) ON CONFLICT(ts_code) DO UPDATE SET '
    'status=excluded.status, rows=excluded.rows, tried_dates=excluded.tried_dates, '
    'err=excluded.err, tries=excluded.tries, updated_at=datetime(\'now\',\'localtime\')'
)


def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


# ---------- 数值化 (对齐 app._save_em_fund_flow_rows) ----------
def _f(v):
    if v is None or v == '-' or v == '':
        return None
    try:
        x = float(v)
        return None if x != x else x  # NaN 检查
    except (ValueError, TypeError):
        return None


def to_secid(ts_code: str) -> str:
    """ts_code (003006.SZ / 600000.SH / 920000.BJ) → push2his 的 secid.

    按市场后缀判断, 不要按代码前缀猜:
      SH → 1.*, SZ 和 BJ 都是 0.*
    (920xxx.BJ 是北交所, 按前缀 '9' 猜会误判成沪市)
    """
    code, _, mkt = ts_code.partition('.')
    return f"{'1' if mkt.upper() == 'SH' else '0'}.{code}"


def build_url(secid: str, lmt: int = 0) -> str:
    return (f'{DAYKLINE_HOST}?lmt={lmt}&klt=101&secid={secid}'
            f'&fields1={FIELDS1}&fields2={FIELDS2}')


# ---------- 解析 / 自检 ----------
def parse_klines(klines, start, end, name):
    """klines → 落在 [start, end] 内的行. 日期统一成 YYYYMMDD (跟库里 trade_date 一致)."""
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
            'main_net_inflow': _f(p[IDX['main_net']]),
            'small_net': _f(p[IDX['small_net']]),
            'mid_net': _f(p[IDX['mid_net']]),
            'big_net': _f(p[IDX['big_net']]),
            'super_net': _f(p[IDX['super_net']]),
            'main_net_pct': _f(p[IDX['main_pct']]),
            'small_pct': _f(p[IDX['small_pct']]),
            'mid_pct': _f(p[IDX['mid_pct']]),
            'big_pct': _f(p[IDX['big_pct']]),
            'super_pct': _f(p[IDX['super_pct']]),
        })
    return out


def check_row(r):
    """口径异常排查用, 当前不在写入路径上调用 (app._save_em_fund_flow_rows 也不卡阈值).
    保留函数以便后续对账时排查四档不归零的行.
    """
    four = sum((r[k] or 0.0) for k in ('super_net', 'big_net', 'mid_net', 'small_net'))
    if abs(four) > 1.0:
        return f'四档和≠0 ({four:,.0f})'
    main = (r['big_net'] or 0.0) + (r['super_net'] or 0.0)
    if abs(main - (r['main_net_inflow'] or 0.0)) > 1.0:
        return f'主力≠大+超大 ({r["main_net_inflow"]:,.0f} vs {main:,.0f})'
    return None


# ---------- DB ----------
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(PROGRESS_DDL)
    conn.commit()
    return conn


def load_universe(conn, start, end):
    """标的池 + 名称表. 用 stock_daily 而不是 fund_flow, 能覆盖区间内的新股."""
    names, codes = {}, set()
    for ts_code, name in conn.execute(UNIVERSE_SQL, (start, end)):
        codes.add(ts_code)
        if name:
            names.setdefault(ts_code, name)
    return sorted(codes), names


def get_progress(conn):
    return {r[0]: r[1] for r in conn.execute('SELECT ts_code, status FROM fflow_backfill_progress')}


def pick_pending(conn, universe, retry_fail, limit):
    prog = get_progress(conn)
    pending = [t for t in universe
               if t not in prog or (retry_fail and prog[t] == 'fail')]
    return pending[:limit] if limit and limit > 0 else pending


def show_status(conn, universe):
    prog = get_progress(conn)
    done = sum(1 for t in universe if prog.get(t) == 'done')
    fail = sum(1 for t in universe if prog.get(t) == 'fail')
    empty = sum(1 for t in universe if prog.get(t) == 'empty')
    rows = conn.execute("SELECT COALESCE(SUM(rows),0) FROM fflow_backfill_progress "
                        "WHERE status='done'").fetchone()[0]
    days = conn.execute("SELECT COUNT(DISTINCT trade_date) FROM fund_flow WHERE source=? "
                        "AND trade_date BETWEEN ? AND ?", (SOURCE, DEFAULT_START, DEFAULT_END)).fetchone()[0]
    print(f'标的池 {len(universe)}  已完成 {done}  无数据 {empty}  失败 {fail}  累计入库 {int(rows):,} 行  '
          f'已覆盖交易日 {days}/{45}')
    return done, fail


# ---------- 抓取 + 对账 ----------
async def fetch_one(req, secid, cfg):
    """带退避重试地拉一只股票. 返回 (klines, tries, err).

    err=None           → 成功 (klines 可能为空数组, 代表这只确实没数据)
    err='empty: ...'   → 接口正常返回但 data 为空, 连着 2 次都这样 = 这只没有资金流数据
                          (北交所就是这种, 不该当成错误反复重试)
    err='<异常类型>'    → 真失败 (被限流 / 超时), 可以 --retry-fail 重来
    """
    url = build_url(secid, cfg.lmt)
    last = None
    null_streak = 0
    for attempt in range(1, cfg.max_tries + 1):
        try:
            resp = await req.get(url, timeout=cfg.timeout_ms)
            if resp.status != 200:
                raise RuntimeError(f'HTTP {resp.status}')
            body = await resp.json()
            data = body.get('data')
            if not data:
                # 正常响应但没数据. 连续两次都空才认定"这只没有", 避免把一次限流误判成没数据
                null_streak += 1
                if null_streak >= 2:
                    return None, attempt, 'empty: 接口返回 data 为空'
                await asyncio.sleep(cfg.backoff_base + random.uniform(0, 0.8))
                continue
            return (data.get('klines') or []), attempt, None
        except Exception as e:      # 被限流时典型是 RemoteDisconnected / TimeoutError
            last = e
            null_streak = 0
            if attempt < cfg.max_tries:
                # 指数退避 + 抖动, 避免多 worker 同时重试撞一起
                delay = min(cfg.backoff_max, cfg.backoff_base * (2 ** (attempt - 1)))
                await asyncio.sleep(delay + random.uniform(0, 0.8))
    return None, cfg.max_tries, f'{type(last).__name__}: {str(last)[:120]}'


async def worker(wid, page, ctx, codes, cfg, names, conn, cur, state, recon, dry):
    """一个 worker: 顺序跑一批标的, 每抓完一只立刻解析 + 入库 + 记进度.

    方案 C (page-per-stock): 每只先 page.goto 该股票的具体页面 (data.eastmoney.com/zjlx/{code}.html),
    等页面 JS 真实调 push2.kline 建立 session, 然后 ctx.request.get push2his.daykline 拉日级数据.
    实测: 只在 setup 阶段 goto detail.html (主页) 时, ctx.request.get push2his 大量被拒;
    每只都 goto 具体股票页面, 让 page 真实触发 push2 调用, session 完整后 ctx.request.get
    就能拿到日 kline. 东财看到的是"频繁切股票的真人在浏览", 不容易盯.

    为什么在 worker 里就落盘 (而不是跑完统一写):
      这活要跑几小时, 之前"跑完才写"意味着中途 Ctrl-C 全白费.
      整个脚本是单线程 asyncio (Playwright 同步 API 不能跨线程), 所以这里直接写
      主连接不会产生并发写冲突 —— await 只在网络请求处让出控制权, sqlite 写入是同步的.

    dry=True 时只抓 + 记进度, 不往 fund_flow 写.
    """
    req = ctx.request
    n = len(codes)
    fail_streak = 0          # 连续失败计数, 触发自动歇
    for i, ts_code in enumerate(codes, 1):
        secid = to_secid(ts_code)
        code_only = secid.split('.')[1]
        stock_url = f'https://data.eastmoney.com/zjlx/{code_only}.html'
        # 1. page.goto 该股票 (让页面 JS 真实调 push2.kline, session 完整)
        try:
            await page.goto(stock_url, wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass  # goto 失败也继续, ctx.request 可能还能通
        # 2. 等页面渲染完成 + JS 调完 push2.kline
        await page.wait_for_timeout(2500)
        # 3. ctx.request.get push2his.daykline (日级)
        klines, tries, err = await fetch_one(req, secid, cfg)
        if err:
            st = 'empty' if err.startswith('empty:') else 'fail'
            state['empty' if st == 'empty' else 'failed'] += 1
            if not dry:
                cur.execute(PROGRESS_UPSERT, (ts_code, st, 0, None, err, tries))
                conn.commit()

            # 监控: 连续 10 只失败 → 歇 10 分钟 (东财盯上后让秒级 QPS 完全归零)
            fail_streak += 1
            if fail_streak >= 10:
                log(f'  [{wid}] ⚠️ 连续失败 {fail_streak} 只, 歇 10 分钟')
                await asyncio.sleep(600)
                fail_streak = 0          # 歇完重新计数
            continue

        # 成功: 重置连续失败状态
        fail_streak = 0

        name = names.get(ts_code, '')
        gap_rows = parse_klines(klines, cfg.start, cfg.end, name)
        state['fetched'] += 1
        state['rows'] += len(gap_rows)

        # 已有区间: 只对账, 顺便统计"库里没有但 kline 有"的漏日
        if cfg.reconcile:
            for r in parse_klines(klines, cfg.reconcile_start, cfg.end, name):
                d = r['trade_date']
                if d <= cfg.end:
                    continue
                old = conn.execute(RECON_SQL, (d, ts_code)).fetchone()
                if old is None:
                    e = recon['only_new'].setdefault(d, {'n': 0, 'samples': []})
                    e['n'] += 1
                    if len(e['samples']) < 3:
                        e['samples'].append(ts_code)
                    continue
                recon['checked'] += 1
                for j, f in enumerate(RECONCILE_FIELDS):
                    a, b = old[j], r[f]
                    if a is None or b is None:
                        continue
                    if abs(a - b) > 0.51:
                        # 按 (日期, 字段) 聚合: 差异是零星几只还是整天全错, 一眼能看出来
                        e = recon['diffs'].setdefault(
                            (d, f), {'n': 0, 'samples': [], 'src': old[5]})
                        e['n'] += 1
                        if len(e['samples']) < 3:
                            e['samples'].append((ts_code, a, b))

        if dry:
            log(f'  [{wid}] {i}/{n} {ts_code} tries={tries} 缺口内 {len(gap_rows)} 行')
        else:
            ins, skp = write_rows(conn, cur, ts_code, gap_rows)
            state['inserted'] += ins
            state['skipped'] += skp
            tried_dates = ','.join(sorted({r['trade_date'] for r in gap_rows}))[:400]
            cur.execute(PROGRESS_UPSERT, (
                ts_code, 'done', ins, tried_dates,
                None, tries))
            conn.commit()          # ← 每只都提交, 保证中断不丢

        if cfg.gap_ms:
            await asyncio.sleep(cfg.gap_ms / 1000.0)
        # burst 节奏: 每 N 只歇一次, 让秒级 QPS 归零
        if cfg.burst and i % cfg.burst == 0 and i < n:
            log(f'  [{wid}] 歇 {cfg.burst_pause_ms/1000:.0f}s (已跑 {i}/{n})')
            await asyncio.sleep(cfg.burst_pause_ms / 1000.0)


# ---------- 入库 ----------
def write_rows(conn, cur, ts_code, rows):
    """写缺口区间的行. 返回 (inserted, skipped). 与每日 _save_em_fund_flow_rows 同口径, 不做阈值自检."""
    code = ts_code.split('.')[0]
    inserted = skipped = 0
    for r in rows:
        cur.execute(INSERT_SQL, (
            r['trade_date'], ts_code, code, r['name'],
            r['main_net_inflow'] or 0.0, r['main_net_pct'],
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
def _report_recon(recon):
    """打印对账结果. 差异按 (日期, 字段) 聚合 —— 零星几只和整天全错要能一眼分开.

    只报告, 不改库里已有数据 (那些是每日任务抓的, 要不要修由人决定).
    """
    n_diff = sum(e['n'] for e in recon['diffs'].values())
    n_missing = sum(e['n'] for e in recon['only_new'].values())
    log(f'对账: 比对已有记录 {recon["checked"]:,} 条, 差异 {n_diff:,} 处, '
        f'库缺(仅kline有) {n_missing:,} 条')
    if recon['diffs']:
        log('  按 (日期,字段) 聚合的差异 (前12):')
        for (d, f), e in sorted(recon['diffs'].items(), key=lambda kv: -kv[1]['n'])[:12]:
            log(f'    {d} {f:<16} 差异 {e["n"]:>5} 只  源={e["src"]}  '
                f'样例={e["samples"][0]}')
    if recon['only_new']:
        log('  库里缺行 (按日期, 前12):')
        for d, e in sorted(recon['only_new'].items(), key=lambda kv: -kv[1]['n'])[:12]:
            log(f'    {d}  缺 {e["n"]:>5} 只  样例={e["samples"]}')


async def run(cfg):
    conn = connect()
    universe, names = load_universe(conn, cfg.start, cfg.end)
    n_bj = sum(1 for t in universe if t.endswith('.BJ'))
    if not cfg.include_bj:
        # 库里 fund_flow 从来没有北交所数据 (每日 clist 任务没覆盖 m:0+t:81).
        # 东财 daykline 是有北交所的 (实测 0.920000 返回 120 条), 但如果只在缺口区间补 BJ,
        # 就会出现"4-6 月有北交所 / 6-9 月没有"的口径断层, 所以默认排除, 保持跟已有数据同口径.
        universe = [t for t in universe if not t.endswith('.BJ')]
        log(f'已排除北交所 {n_bj} 只 (--include-bj 可带上)')
    else:
        log(f'包含北交所 {n_bj} 只 ⚠️ 6/16 之后库里没有北交所, 补完会造成口径断层')
    log(f'标的池 {len(universe)} 只 (来自 stock_daily {cfg.start}~{cfg.end})')

    if cfg.status:
        show_status(conn, universe)
        conn.close()
        return

    if cfg.reset:
        conn.execute('DELETE FROM fflow_backfill_progress')
        conn.commit()
        log('进度表已清空')

    pending = pick_pending(conn, universe, cfg.retry_fail, cfg.limit)
    log(f'本轮待跑 {len(pending)} 只' + ('  [dry-run 不入库]' if cfg.dry_run else ''))
    if not pending:
        show_status(conn, universe)
        conn.close()
        return

    if cfg.dry_run and cfg.limit <= 0:
        pending = pending[:5]
        log(f'dry-run 抽 {len(pending)} 只试抓, 不入库')

    workers = max(1, min(cfg.workers, len(pending)))
    shards = [pending[i::workers] for i in range(workers)]
    state = {'fetched': 0, 'failed': 0, 'empty': 0, 'rows': 0,
             'inserted': 0, 'skipped': 0}
    recon = {'diffs': {}, 'only_new': {}, 'checked': 0}
    cur = conn.cursor()

    t0 = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless, channel='chrome')
        contexts = []
        pages = []
        for i in range(workers):
            ctx = await browser.new_context(viewport={'width': 1600, 'height': 900})
            page = await ctx.new_page()
            # setup: 打开一只具体股票页面 (非 detail.html 主页), 让 session 完整到 push2his 能通.
            # 实测: detail.html 主页 JS 只调 push2 (push2his 没建连), ctx.request.get push2his 会被拒;
            # 用具体股票页 (000001.html) 让页面 JS 调 push2.kline, session 完整, push2his 也能通.
            try:
                await page.goto('https://data.eastmoney.com/zjlx/000001.html',
                                wait_until='domcontentloaded', timeout=45000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                log(f'  [setup w{i+1}] 启动 goto 异常: {type(e).__name__}: {str(e)[:80]}')
            contexts.append(ctx)
            pages.append(page)
        log(f'{workers} 个 worker context 就绪, 开始抓...')
        log('提示: Ctrl-C 可随时中断, 已抓的都已落盘, 重跑自动续')

        async def reporter():
            while True:
                await asyncio.sleep(20)
                el = time.time() - t0
                rate = state['fetched'] / el if el else 0
                eta = (len(pending) - state['fetched']) / rate if rate > 0 else 0
                extra = (f'已入库 {state["inserted"]:,}' if not cfg.dry_run
                         else f'抓 {state["fetched"]} 只')
                log(f'进度 {state["fetched"]}/{len(pending)}  缺口行 {state["rows"]:,}  '
                    f'{extra}  无数据 {state["empty"]}  失败 {state["failed"]}  '
                    f'{rate:.2f} 只/s  已用 {el/60:.1f}min  预计还需 {eta/60:.0f}min')
        rep = asyncio.create_task(reporter())

        try:
            await asyncio.gather(*[
                worker(i + 1, pages[i], contexts[i], shards[i], cfg, names,
                       conn, cur, state, recon, cfg.dry_run)
                for i in range(workers)
            ])
        finally:
            rep.cancel()
            await browser.close()

    el = (time.time() - t0) / 60
    log(f'抓取结束 用时 {el:.1f}min  成功 {state["fetched"]} 只  无数据 {state["empty"]} 只  '
        f'失败 {state["failed"]} 只  缺口行 {state["rows"]:,}')
    if cfg.dry_run:
        log(f'dry-run: 没往 fund_flow 写任何数据, 累计 {state["rows"]:,} 行')
    else:
        log(f'入库完成: 新增 {state["inserted"]:,} 行, 跳过(已存在) {state["skipped"]:,}')
    if cfg.reconcile and recon['checked']:
        _report_recon(recon)
    show_status(conn, universe)
    conn.close()


def main():
    ap = argparse.ArgumentParser(description='补齐个股主力净流入历史缺口 (push2his daykline)')
    ap.add_argument('--start', default=DEFAULT_START)
    ap.add_argument('--end', default=DEFAULT_END)
    ap.add_argument('--workers', type=int, default=2, help='并发 context 数')
    ap.add_argument('--limit', type=int, default=0, help='本轮最多跑多少只 (0=不限)')
    ap.add_argument('--max-tries', type=int, default=4, help='单只最大重试次数')
    ap.add_argument('--backoff-base', type=float, default=2.0, help='退避基数(秒)')
    ap.add_argument('--backoff-max', type=float, default=45.0, help='退避上限(秒)')
    ap.add_argument('--gap-ms', type=int, default=250, help='同 worker 两次请求间隔(毫秒)')
    ap.add_argument('--burst', type=int, default=0, help='每 N 只歇一次 (0=不歇), 让秒级 QPS 归零')
    ap.add_argument('--burst-pause-ms', type=int, default=30000, help='歇的毫秒数 (默认 30000=30秒)')
    ap.add_argument('--timeout-ms', type=int, default=25000)
    ap.add_argument('--lmt', type=int, default=0)
    ap.add_argument('--include-bj', action='store_true',
                    help='连北交所一起补 (默认不带: 库里 6/16 之后没有北交所, 带了会造成口径断层)')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='只抓不写库')
    ap.add_argument('--retry-fail', action='store_true', help='把之前失败的也重排进来')
    ap.add_argument('--reset', action='store_true', help='清空进度表')
    ap.add_argument('--status', action='store_true', help='只看进度不出发')
    ap.add_argument('--reconcile', dest='reconcile', action='store_true', default=True)
    ap.add_argument('--no-reconcile', dest='reconcile', action='store_false')
    ap.add_argument('--reconcile-start', default='20260616', help='从这个日期起跟库里已有数据对账')
    cfg = ap.parse_args()

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log('已中断. 进度已保存, 重跑本脚本会自动续.')


if __name__ == '__main__':
    main()
