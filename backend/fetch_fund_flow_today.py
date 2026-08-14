"""
拉取今日全市场个股主力净流入数据并导出 Excel。

【方案 A - JSONP】在 Playwright 浏览器 context 里用 JSONP 加载 push2 接口
为什么 JSONP:
  - 东财 push2 接口本身就是 JSONP 格式（cb=jQuery...({...})）
  - JSONP 通过 <script> 加载，不受 CORS 限制（fetch 被 CORS 拦）
  - 完全不需要 click 翻页，0 风控风险
  - 53 页 ≈ 2 分钟跑完

数据源: push2.eastmoney.com clist/get
排序:  fid=f62 (主力净流入金额降序)
每页:  pz=100 (东财上限)

字段:
  f62  主力净流入金额（元）- 主力 = 超大单+大单
  f267 主力净流入（另一口径，东财主力净额）
  f268 主力净流入占比 %
  f269/f270 超大单净流入金额/占比
  f271/f272 大单净流入金额/占比
  f273/f274 中单净流入金额/占比
  f275/f276 小单净流入金额/占比
  f12/f14 代码/名称, f2 最新价, f3 涨跌幅, f100 板块, f124 时间戳
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from playwright.sync_api import sync_playwright


# ---------- 交易日历 ----------
# 复用后端 SQLite trading_dates_cache 表（TuShare 同步的数据），
# 缓存 miss 时用 akshare.tool_trade_date_hist_sina() 兜底。
#
# 非交易日同步的语义:
#   东财 push2 在周末/法定节假日仍会返回「上一交易日」的全市场数据 (amount 字段已更新),
#   所以非交易日也允许拉取, 但 trade_date 必须填「今天之前的最近一个交易日」,
#   不能填今天 (今天休市, 没数据, 写了就污染 fund_flow)。
#   例: 6/19 端午 sync → 实际数据是 6/18 收盘的 → trade_date='20260618'
#       6/21 周日 sync → 中间 6/19 端午不开 → trade_date 仍为 '20260618'
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
_AK_TRADE_DATES_CACHE: Optional[set] = None  # 模块级缓存, 一次进程内只拉一次
_AK_TRADE_DATES_SORTED: Optional[list] = None  # 升序 list, 给 last_trading_date 用


def _load_ak_trade_dates() -> bool:
    """从 akshare 拉历史交易日, 填两个缓存。返回是否成功。"""
    global _AK_TRADE_DATES_CACHE, _AK_TRADE_DATES_SORTED
    if _AK_TRADE_DATES_CACHE is not None:
        return True
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = df['trade_date'].astype(str).str.replace('-', '').tolist()
        _AK_TRADE_DATES_CACHE = set(dates)
        _AK_TRADE_DATES_SORTED = sorted(dates)
        return True
    except Exception as e:
        print(f'[akshare] 拉交易日历失败: {e}', file=sys.stderr)
        return False


def is_trading_date(date_str: str) -> bool:
    """判断 date_str (YYYYMMDD) 是否为 A 股交易日。

    优先级: SQLite trading_dates_cache > akshare.tool_trade_date_hist_sina() > 简单周末判断。
    缓存/网络都失败时, 兜底返回 True (按交易日处理, 避免误判休市) — 节假日错抓一两行
    总比节假日大年三十全市场报错强。
    """
    if not date_str or len(date_str) != 8 or not date_str.isdigit():
        return True  # 异常日期不挡, 让后续逻辑自己处理

    # 1) 本地 SQLite 缓存 (TuShare 同步的, 准确)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        row = conn.execute(
            'SELECT 1 FROM trading_dates_cache WHERE cal_date = ? LIMIT 1',
            (date_str,),
        ).fetchone()
        conn.close()
        if row is not None:
            return True
        # 缓存里没有这日期, 但缓存覆盖范围可能只到 2025 年, 6/19 端午节实际不在缓存,
        # 走第 2 步 akshare 二次确认
    except Exception:
        pass

    # 2) akshare 兜底 (全历史交易日)
    if not _load_ak_trade_dates():
        return True  # 兜底, 不挡
    return date_str in _AK_TRADE_DATES_CACHE


def last_trading_date(date_str: str) -> str:
    """返回 <= date_str 的最近一个交易日 (YYYYMMDD)。如果 date_str 本身是交易日, 返回自身。

    节日同步时, 东财 push2 返回的「今日」数据实际属于上一个交易日,
    调用方用这个函数算出真正的 trade_date, 避免把节日数据写到今天。
    """
    if not date_str or len(date_str) != 8 or not date_str.isdigit():
        return date_str
    if is_trading_date(date_str):
        return date_str

    # 向前找最近一个交易日
    # 1) 优先本地缓存
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        row = conn.execute(
            'SELECT MAX(cal_date) FROM trading_dates_cache WHERE cal_date <= ?',
            (date_str,),
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass

    # 2) akshare 兜底 (升序 list, bisect 找左侧最近)
    if not _load_ak_trade_dates():
        return date_str  # 兜底: 用今天 (上层会有数据错位风险, 但比报错强)
    import bisect
    idx = bisect.bisect_right(_AK_TRADE_DATES_SORTED, date_str) - 1
    if idx >= 0:
        return _AK_TRADE_DATES_SORTED[idx]
    return date_str


# ---------- 配置 ----------
# detail.html 默认 URL（fid=f62 按主力净流入金额排序，含"净额"口径分档）
PUSH2_URL_BASE = (
    "https://push2.eastmoney.com/api/qt/clist/get?"
    "fid=f62&po=1&pz=100&np=1&fltt=2&invt=2"
    "&ut=8dec03ba335b81bf4ebdf7b29ec27d15"
    "&fs=m%3A0%2Bt%3A6%2Bf%3A%212%2Cm%3A0%2Bt%3A13%2Bf%3A%212%2Cm%3A0%2Bt%3A80%2Bf%3A%212%2Cm%3A1%2Bt%3A2%2Bf%3A%212%2Cm%3A1%2Bt%3A23%2Bf%3A%212%2Cm%3A0%2Bt%3A7%2Bf%3A%212%2Cm%3A1%2Bt%3A3%2Bf%3A%212"
    "&fields=f12%2Cf14%2Cf2%2Cf3%2Cf62%2Cf184%2Cf66%2Cf69%2Cf72%2Cf75%2Cf78%2Cf81%2Cf84%2Cf87%2Cf204%2Cf205%2Cf124%2Cf1%2Cf13"
)

FIELD_MAP = [
    ("f12", "代码"),
    ("f14", "名称"),
    ("f2", "最新价"),
    ("f3", "涨跌幅"),
    ("f62", "主力净流入"),
    ("f184", "主力净流入占比"),
    ("f66", "超大单净额"),
    ("f69", "超大单净额占比"),
    ("f72", "大单净额"),
    ("f75", "大单净额占比"),
    ("f78", "中单净额"),
    ("f81", "中单净额占比"),
    ("f84", "小单净额"),
    ("f87", "小单净额占比"),
    ("f204", "主力净流入(口径2)"),
    ("f205", "主力净流入占比(口径2)"),
    ("f1", "市场"),
    ("f13", "市场代码"),
]

WAIT_MS_PER_PAGE = 1500  # 每页间隔（JSONP 加载完一般 <500ms）
PAGE_URL = "https://data.eastmoney.com/zjlx/detail.html"  # 正确页面：按金额排序+净额口径
OUTPUT_DIR = Path(__file__).parent.parent


def fetch_page_jsonp(page, pn: int) -> Optional[dict]:
    """在浏览器 context 里用 JSONP 加载第 pn 页数据。返回 data dict 或 None。"""
    js = """
    (pn) => {
        return new Promise((resolve, reject) => {
            const cb = 'jjsonp_' + Date.now() + '_' + pn;
            window[cb] = (data) => {
                try { delete window[cb]; } catch(e) { window[cb] = undefined; }
                if (script.parentNode) script.parentNode.removeChild(script);
                resolve({ok: true, data: data});
            };
            const script = document.createElement('script');
            const url = '%s' + '&pn=' + pn + '&cb=' + cb;
            script.src = url;
            script.onerror = (e) => {
                try { delete window[cb]; } catch(e2) {}
                if (script.parentNode) script.parentNode.removeChild(script);
                resolve({ok: false, err: 'script load failed'});
            };
            document.head.appendChild(script);
            setTimeout(() => {
                if (window[cb] !== undefined) {
                    try { delete window[cb]; } catch(e) {}
                    if (script.parentNode) script.parentNode.removeChild(script);
                    resolve({ok: false, err: 'timeout 15s'});
                }
            }, 15000);
        });
    }
    """ % PUSH2_URL_BASE
    result = page.evaluate(js, pn)
    if result and result.get("ok"):
        return result.get("data")
    return None


def fetch_today_market(progress_callback=None, headless: bool = False, verbose: bool = True, force: bool = False, skip_if_exists: bool = False) -> tuple:
    """JSONP 循环拉全市场数据 (lib API, 给 Flask 后台调用).

    Args:
        progress_callback: 可选回调, 签名 (stage: str, **kwargs).
            - stage="total", total=N
            - stage="page", pn=N, total_pages=N, rows_count=M, failed=[...]
            - stage="done", rows=M, total=N, failed=[...], actual_date=YYYYMMDD
            - stage="already_synced", actual_date=YYYYMMDD  (skip_if_exists 命中, 早退)
        headless: Playwright 启动模式
        verbose: 是否 print 到 stderr (后台调用设 False)
        force: 跳过交易日判断 (节日也强抓, 默认 False)
        skip_if_exists: actual_date 在 fund_flow 表里已有数据时早退 (幂等, 默认 False)
                        force=True 时此参数被忽略

    Returns:
        (rows: list[dict], total: int, failed_pages: list[int], actual_date: str)
        rows 是 detail.html 原始 f-code dict (未做中文/英文列名转换)
        actual_date 是数据真正归属的交易日:
          - 今天本身就是交易日 → datetime.now() 当天
          - 今天非交易日 → today 之前的最近一个交易日 (端午/周日点 sync 会拿到这个)
          - force=True → datetime.now() 当天 (不管是否交易日)
        skip_if_exists 命中时 rows/total/failed 都为空/0, actual_date 仍返回 (供 state 展示)
    """
    all_rows = []
    failed_pages = []
    total = 0
    total_pages = 0

    def _emit(stage, **kw):
        if progress_callback:
            try:
                progress_callback(stage, **kw)
            except Exception:
                pass

    # 节日也允许拉 (东财 push2 在休市日仍返回上一交易日数据, amount 已更新),
    # 但 trade_date 必须用 last_trading_date(today), 不能用 today
    today = datetime.now().strftime('%Y%m%d')
    if force:
        actual_date = today
    else:
        actual_date = last_trading_date(today)
        # 交易日开盘前 (00:00~09:30): 东财「今日」排行还是上一交易日的收盘数据,
        # 归到今天会把昨天的数据盖上今天的日期戳 (2026-08-04 凌晨事故)
        now = datetime.now()
        if actual_date == today and (now.hour, now.minute) < (9, 30):
            actual_date = last_trading_date((now - timedelta(days=1)).strftime('%Y%m%d'))
    if verbose and actual_date != today:
        print(f"[{datetime.now():%H:%M:%S}] {today} 非交易日(或未开盘), 数据将归属到 {actual_date} (最近交易日)")

    # 幂等守卫: actual_date 已有数据就早退 (force 时跳过)
    if not force and skip_if_exists:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            row = conn.execute(
                "SELECT COUNT(*) FROM fund_flow WHERE trade_date = ? AND source = 'eastmoney-push2'",
                (actual_date,),
            ).fetchone()
            conn.close()
            if row and row[0] > 0:
                if verbose:
                    print(f"[{datetime.now():%H:%M:%S}] {actual_date} 已有数据 ({row[0]} 行), 跳过抓取")
                _emit("already_synced", actual_date=actual_date, existing_count=row[0])
                return [], 0, [], actual_date
        except Exception as e:
            if verbose:
                print(f"[skip_if_exists] 检查失败, 继续拉取: {e}")

    _emit("start", actual_date=actual_date, today=today)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="chrome")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        if verbose:
            print(f"[{datetime.now():%H:%M:%S}] 打开页面建立 session...")
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # 先拿第 1 页看 total
        if verbose:
            print("拉第 1 页...")
        data = fetch_page_jsonp(page, 1)
        if not data or not data.get("data"):
            if verbose:
                print("FAIL: 第 1 页拿不到数据", file=sys.stderr)
            _emit("error", message="第 1 页拿不到数据")
            browser.close()
            return [], 0, list(range(1, 54)), actual_date

        total = data["data"].get("total", 0)
        diff = data["data"].get("diff") or []
        all_rows.extend(diff)
        if verbose:
            print(f"  total={total}, 第 1 页 {len(diff)} 行")
        _emit("total", total=total)

        if total == 0:
            browser.close()
            _emit("done", rows=0, total=0, failed=list(range(2, 54)))
            return [], 0, list(range(2, 54)), actual_date

        # 计算总页数
        import math
        total_pages = math.ceil(total / 100)
        if verbose:
            print(f"  总页数: {total_pages}（{total} 只）")

        # 循环拉剩余页
        for pn in range(2, total_pages + 1):
            page.wait_for_timeout(WAIT_MS_PER_PAGE)
            data = fetch_page_jsonp(page, pn)
            if not data or not data.get("data") or not data["data"].get("diff"):
                failed_pages.append(pn)
                if verbose:
                    print(f"  pn={pn}: 无数据", file=sys.stderr)
                _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])
                continue
            all_rows.extend(data["data"]["diff"])
            if verbose and (pn % 5 == 0 or pn == total_pages):
                print(f"  pn={pn}/{total_pages}, 累计 {len(all_rows)} 行")
            _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])

        browser.close()

    if verbose:
        print(f"\n抓取完成: {len(all_rows)} 行")
        if failed_pages:
            print(f"⚠️ 失败页: {failed_pages[:20]}{'...' if len(failed_pages) > 20 else ''}")
    _emit("done", rows=len(all_rows), total=total, failed=failed_pages[:])
    return all_rows, total, failed_pages[:], actual_date


# ===== 5 日排行: fid=f164 拿"近 5 个交易日"累计 =====
# 字段映射 (跟 akshare stock_fund_em.stock_individual_fund_flow_rank(indicator='5日') 一致):
#   f164 5日主力净流入净额, f165 5日主力净流入净占比,
#   f166 5日超大单净流入净额, f167 5日超大单净流入净占比,
#   f168 5日大单净流入净额,   f169 5日大单净流入净占比,
#   f170 5日中单净流入净额,   f171 5日中单净流入净占比,
#   f172 5日小单净流入净额,   f173 5日小单净流入净占比
PUSH2_URL_BASE_5D = (
    "https://push2.eastmoney.com/api/qt/clist/get?"
    "fid=f164&po=1&pz=100&np=1&fltt=2&invt=2"
    "&ut=8dec03ba335b81bf4ebdf7b29ec27d15"
    "&fs=m%3A0%2Bt%3A6%2Bf%3A%212%2Cm%3A0%2Bt%3A13%2Bf%3A%212%2Cm%3A0%2Bt%3A80%2Bf%3A%212%2Cm%3A1%2Bt%3A2%2Bf%3A%212%2Cm%3A1%2Bt%3A23%2Bf%3A%212%2Cm%3A0%2Bt%3A7%2Bf%3A%212%2Cm%3A1%2Bt%3A3%2Bf%3A%212"
    "&fields=f12%2Cf14%2Cf2%2Cf109%2Cf164%2Cf165%2Cf166%2Cf167%2Cf168%2Cf169%2Cf170%2Cf171%2Cf172%2Cf173%2Cf257%2Cf258%2Cf124"
)


def fetch_5day_market(target_date: str, progress_callback=None, headless: bool = False, verbose: bool = True, skip_if_exists: bool = False) -> tuple:
    """复用 fetch_today_market 的 playwright 流程, 但切到 detail.html 的「5日排行」tab.
    fid=f184 → 排序字段 = 5 日主力净流入占比; 返回 fields 含义都变成"5 日累计".
    用于补某一天的数据: 5日累计 = target_date 那天 + 之后 4 个交易日 (DB 里已有).

    Args:
        target_date: YYYYMMDD, 写入 fund_flow 用的 trade_date
        progress_callback / headless / verbose / skip_if_exists: 同 fetch_today_market
    Returns: 同 fetch_today_market, actual_date 固定为 target_date (5日排行无交易日判断)
    """
    all_rows = []
    failed_pages = []
    total = 0
    total_pages = 0

    def _emit(stage, **kw):
        if progress_callback:
            try:
                progress_callback(stage, **kw)
            except Exception:
                pass

    actual_date = target_date

    # 幂等守卫: target_date 已有数据就早退
    if skip_if_exists:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            row = conn.execute(
                "SELECT COUNT(*) FROM fund_flow WHERE trade_date = ? AND source = 'eastmoney-push2'",
                (actual_date,),
            ).fetchone()
            conn.close()
            if row and row[0] > 0:
                if verbose:
                    print(f"[5d] {actual_date} 已有数据 ({row[0]} 行), 跳过抓取")
                _emit("already_synced", actual_date=actual_date, existing_count=row[0])
                return [], 0, [], actual_date
        except Exception as e:
            if verbose:
                print(f"[5d skip_if_exists] 检查失败, 继续拉取: {e}")

    _emit("start", actual_date=actual_date, today=actual_date)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="chrome")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        if verbose:
            print(f"[5d] 打开页面建立 session...")
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # 注入 URL_BASE_5D, 复用 fetch_page_jsonp 的写法
        js = """
        (pn) => {
            return new Promise((resolve, reject) => {
                const cb = 'jjsonp_5d_' + Date.now() + '_' + pn;
                window[cb] = (data) => {
                    try { delete window[cb]; } catch(e) { window[cb] = undefined; }
                    if (script.parentNode) script.parentNode.removeChild(script);
                    resolve({ok: true, data: data});
                };
                const script = document.createElement('script');
                const url = '%s' + '&pn=' + pn + '&cb=' + cb;
                script.src = url;
                script.onerror = (e) => {
                    try { delete window[cb]; } catch(e2) {}
                    if (script.parentNode) script.parentNode.removeChild(script);
                    resolve({ok: false, err: 'script load failed'});
                };
                document.head.appendChild(script);
                setTimeout(() => {
                    if (window[cb] !== undefined) {
                        try { delete window[cb]; } catch(e) {}
                        if (script.parentNode) script.parentNode.removeChild(script);
                        resolve({ok: false, err: 'timeout 15s'});
                    }
                }, 15000);
            });
        }
        """ % PUSH2_URL_BASE_5D

        if verbose:
            print("[5d] 拉第 1 页...")
        result = page.evaluate(js, 1)
        data = result.get("data") if result and result.get("ok") else None
        if not data or not data.get("data"):
            if verbose:
                print("[5d] FAIL: 第 1 页拿不到数据", file=sys.stderr)
            browser.close()
            _emit("error", message="第 1 页拿不到数据")
            return [], 0, list(range(1, 54)), actual_date

        total = data["data"].get("total", 0)
        diff = data["data"].get("diff") or []
        all_rows.extend(diff)
        if verbose:
            print(f"  total={total}, 第 1 页 {len(diff)} 行")
        _emit("total", total=total)

        if total == 0:
            browser.close()
            _emit("done", rows=0, total=0, failed=list(range(2, 54)))
            return [], 0, list(range(2, 54)), actual_date

        import math
        total_pages = math.ceil(total / 100)
        if verbose:
            print(f"  总页数: {total_pages}（{total} 只）")

        for pn in range(2, total_pages + 1):
            page.wait_for_timeout(WAIT_MS_PER_PAGE)
            result = page.evaluate(js, pn)
            data = result.get("data") if result and result.get("ok") else None
            if not data or not data.get("data") or not data["data"].get("diff"):
                failed_pages.append(pn)
                if verbose:
                    print(f"  pn={pn}: 无数据", file=sys.stderr)
                _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])
                continue
            all_rows.extend(data["data"]["diff"])
            if verbose and (pn % 5 == 0 or pn == total_pages):
                print(f"  pn={pn}/{total_pages}, 累计 {len(all_rows)} 行")
            _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])

        browser.close()

    if verbose:
        print(f"\n[5d] 抓取完成: {len(all_rows)} 行")
        if failed_pages:
            print(f"⚠️ 失败页: {failed_pages[:20]}{'...' if len(failed_pages) > 20 else ''}")
    _emit("done", rows=len(all_rows), total=total, failed=failed_pages[:])
    return all_rows, total, failed_pages[:], actual_date


# ===== 兼容旧名: fetch_all 仍然可用, 但走新函数 =====
def fetch_all(headless: bool = False, force: bool = False) -> pd.DataFrame:
    """旧 CLI 入口, 内部用 fetch_today_market. 保留向后兼容.

    force=True 跳过交易日守卫; skip_if_exists 默认开 (actual_date 已有数据就早退).
    """
    rows, _, _, _ = fetch_today_market(
        progress_callback=None, headless=headless, verbose=True,
        force=force, skip_if_exists=True,
    )
    return rows_to_df(rows)


def rows_to_df(rows: list) -> pd.DataFrame:
    decoded = []
    for r in rows:
        rec = {}
        for k, zh in FIELD_MAP:
            v = r.get(k, "-")
            if v == "-" or v == "":
                v = None
            rec[zh] = v
        decoded.append(rec)
    df = pd.DataFrame(decoded)
    # 数值化
    numeric_cols = [
        "最新价", "涨跌幅", "主力净流入", "主力净流入占比",
        "主力净流入(口径2)", "主力净流入占比(口径2)",
        "超大单净额", "超大单净额占比",
        "大单净额", "大单净额占比",
        "中单净额", "中单净额占比",
        "小单净额", "小单净额占比",
    ]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 主力净流入降序
    df = df.sort_values("主力净流入", ascending=False, na_position="last").reset_index(drop=True)
    df.insert(0, "排名", range(1, len(df) + 1))
    return df


def export_excel(df: pd.DataFrame, out_path: Path, top: Optional[int] = None):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_to_write = df.head(top) if top else df

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df_to_write.to_excel(writer, sheet_name="主力净流入", index=False)
        notes = pd.DataFrame({
            "项": ["数据源", "接口", "排序字段", "抓取时间", "口径", "金额字段", "完整度"],
            "值": [
                "东方财富 push2.eastmoney.com",
                "clist/get (fid=f62 按主力净流入金额降序, pz=100)",
                "f62 = 主力净流入金额（主力=超大单+大单 净流入）",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "正数=主力净流入，负数=主力净流出；东财口径",
                "f62 主力净额、f267 主力净额(口径2)、f269 超大单、f271 大单、f273 中单、f275 小单（单位均为元）",
                f"本次抓到 {len(df)} 行（A 股共约 5300 只）",
            ],
        })
        notes.to_excel(writer, sheet_name="说明", index=False)

        ws = writer.sheets["主力净流入"]
        ws.freeze_panes = "A2"
        widths = {
            "排名": 6, "代码": 10, "名称": 12, "最新价": 10, "涨跌幅": 10,
            "主力净流入": 18, "主力净流入占比": 12,
            "主力净流入(口径2)": 18, "主力净流入占比(口径2)": 12,
            "超大单净额": 18, "超大单净额占比": 12,
            "大单净额": 18, "大单净额占比": 12,
            "中单净额": 18, "中单净额占比": 12,
            "小单净额": 18, "小单净额占比": 12,
            "市场": 8, "市场代码": 10,
        }
        for col_idx, header in enumerate(df_to_write.columns, start=1):
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = widths.get(header, 12)
        # 数值格式
        for col_idx, header in enumerate(df_to_write.columns, start=1):
            for row_idx in range(2, len(df_to_write) + 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                if header in ["主力净流入", "主力净流入(口径2)", "超大单净额", "大单净额", "中单净额", "小单净额"]:
                    cell.number_format = '#,##0.00,,"万"'
                elif header in ["最新价", "主力净流入占比", "主力净流入占比(口径2)", "涨跌幅",
                                 "超大单净额占比", "大单净额占比", "中单净额占比", "小单净额占比"]:
                    cell.number_format = "0.00"
        # 涨跌幅/主力净流入 条件格式（红涨绿跌）
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import PatternFill, Font
        red = PatternFill(start_color="FFE5E5", end_color="FFE5E5", fill_type="solid")
        green = PatternFill(start_color="E5F5E5", end_color="E5F5E5", fill_type="solid")
        for h in ["涨跌幅", "主力净流入", "超大单净额", "大单净额"]:
            if h in df_to_write.columns:
                col_idx = list(df_to_write.columns).index(h) + 1
                col_letter = ws.cell(row=1, column=col_idx).column_letter
                last_row = len(df_to_write) + 1
                ws.conditional_formatting.add(f"{col_letter}2:{col_letter}{last_row}",
                                               CellIsRule(operator="greaterThan", formula=["0"], fill=red))
                ws.conditional_formatting.add(f"{col_letter}2:{col_letter}{last_row}",
                                               CellIsRule(operator="lessThan", formula=["0"], fill=green))
        # 表头加粗
        for col_idx in range(1, len(df_to_write.columns) + 1):
            ws.cell(row=1, column=col_idx).font = Font(bold=True)
        # 说明 sheet 格式
        notes_ws = writer.sheets["说明"]
        notes_ws.column_dimensions["A"].width = 16
        notes_ws.column_dimensions["B"].width = 95
        for col_idx in range(1, 3):
            notes_ws.cell(row=1, column=col_idx).font = Font(bold=True)
        for row_idx in range(2, len(notes) + 2):
            notes_ws.cell(row=row_idx, column=1).font = Font(bold=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=None, help="只导出前 N 名")
    ap.add_argument("--out", type=str, default=None, help="输出 xlsx 路径")
    ap.add_argument("--headless", action="store_true", help="无头模式（默认有头）")
    ap.add_argument("--force", action="store_true", help="跳过交易日判断（节假日强抓, 写到 today）; 同时跳过幂等守卫, 强制重抓")
    args = ap.parse_args()

    today_yyyymmdd = datetime.now().strftime('%Y%m%d')
    actual = today_yyyymmdd if args.force else last_trading_date(today_yyyymmdd)
    if actual != today_yyyymmdd:
        print(f"⏸  {today_yyyymmdd} 是非交易日, 数据将归属到 {actual} (最近交易日)")

    # 幂等提示: 已有数据时不重抓 (--force 跳过)
    if not args.force:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            row = conn.execute(
                "SELECT COUNT(*) FROM fund_flow WHERE trade_date = ? AND source = 'eastmoney-push2'",
                (actual,),
            ).fetchone()
            conn.close()
            if row and row[0] > 0:
                print(f"✅ {actual} 已有数据 ({row[0]} 行), 跳过抓取。如需强制重抓, 加 --force。")
                sys.exit(0)
        except Exception as e:
            print(f"[main] 幂等检查失败, 继续拉取: {e}")

    today = datetime.now().strftime("%Y%m%d_%H%M")
    default_out = OUTPUT_DIR / f"主力净流入_{today}.xlsx"
    out_path = Path(args.out) if args.out else default_out

    print(f"[{datetime.now():%H:%M:%S}] 开始拉取（Playwright + JSONP, fid=f62 按金额排序）...")
    df = fetch_all(headless=args.headless, force=args.force)
    if df.empty:
        print("FAIL: 数据为空", file=sys.stderr)
        sys.exit(1)

    export_excel(df, out_path, top=args.top)
    print(f"\n✓ 已导出: {out_path}")
    print(f"  行数: {len(df)}, 字段: {list(df.columns)}")
    print("\n主力净流入 Top 5:")
    print(df[["排名", "代码", "名称", "最新价", "涨跌幅", "主力净流入", "主力净流入占比", "超大单净额", "大单净额"]].head(5).to_string(index=False))
    print("\n主力净流入 Bottom 5（资金流出最大）:")
    print(df[["排名", "代码", "名称", "最新价", "涨跌幅", "主力净流入", "主力净流入占比", "超大单净额", "大单净额"]].tail(5).to_string(index=False))


if __name__ == "__main__":
    main()