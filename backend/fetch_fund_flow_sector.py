"""
拉取东方财富板块资金流 (行业 + 概念) 并入库 sector_fund_flow.

【方案 A - JSONP】在 Playwright 浏览器 context 里用 JSONP 加载 push2 接口
为什么 JSONP (跟 fetch_fund_flow_today.py 个股版完全一样的理由):
  - 板块接口本身也是 JSONP 格式 (cb=jQuery...({...}))
  - JSONP 通过 <script> 加载, 不受 CORS 限制 (fetch 被 CORS 拦)
  - 完全不需要 click 翻页, 0 风控风险
  - 行业 5 页 ≈ 5s, 概念 5 页 ≈ 5s, 总 ~10s 跑完

数据源: push2.eastmoney.com clist/get
排序:   fid=f62 (主力净流入金额降序)
每页:   pz=100 (东财上限)
行业:   fs=m:90+t:2  → 496 行
概念:   fs=m:90+t:3  → 504 行
入口:   https://data.eastmoney.com/bkzj/hy.html (行业)
        https://data.eastmoney.com/bkzj/gn.html (概念)

字段:
  f12  板块代码 (BK1036)
  f14  板块名称
  f2   最新价
  f3   涨跌幅 %
  f62  主力净流入金额 (元) - 主力 = 超大单+大单
  f184 主力净流入占比 %
  f66/f69 超大单净额 / 占比
  f72/f75 大单净额 / 占比
  f78/f81 中单净额 / 占比
  f84/f87 小单净额 / 占比
  f128 领涨股名称
  f140 领涨股代码 (6 位)
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

# 复用个股版的交易日历 (SQLite trading_dates_cache + akshare 兜底)
from fetch_fund_flow_today import last_trading_date, DB_PATH


# ---------- 配置 ----------
SECTOR_TYPES = {
    'industry': ('m:90+t:2', 'https://data.eastmoney.com/bkzj/hy.html', '行业'),
    'concept':  ('m:90+t:3', 'https://data.eastmoney.com/bkzj/gn.html', '概念'),
}

# 行业/概念共用同一套 URL 模板, 只换 fs 过滤和 PAGE_URL
PUSH2_URL_TEMPLATE = (
    "https://push2.eastmoney.com/api/qt/clist/get?"
    "fid=f62&po=1&pz=100&np=1&fltt=2&invt=2"
    "&ut=8dec03ba335b81bf4ebdf7b29ec27d15"
    "&fs={fs}"
    "&fields=f1%2Cf2%2Cf3%2Cf4%2Cf12%2Cf14%2Cf62%2Cf66%2Cf69%2Cf72%2Cf75%2Cf78%2Cf81%2Cf84%2Cf87%2Cf100%2Cf128%2Cf140%2Cf184%2Cf10000"
)

# JSONP 注入是模板化 URL, 但 fetch_page_jsonp 用 %s 注入, fs 提前格式化进去即可
WAIT_MS_PER_PAGE = 1200  # 板块量级小 (≤504 行), 比个股 1500ms 略快
PZ = 100  # 东财上限


def _build_url(sector_type: str) -> tuple:
    """返 (url_with_fs, page_url)"""
    fs, page_url, _ = SECTOR_TYPES[sector_type]
    return PUSH2_URL_TEMPLATE.format(fs=fs), page_url


def fetch_page_jsonp(page, pn: int, url_base: str) -> Optional[dict]:
    """在浏览器 context 里用 JSONP 加载第 pn 页数据. 返回 data dict 或 None.
    url_base 是已注入 fs 过滤的完整 URL 模板 (含 fields 和 ut).
    """
    js = """
    (pn) => {
        return new Promise((resolve, reject) => {
            const cb = 'jjsonp_s_' + Date.now() + '_' + pn;
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
    """ % url_base
    result = page.evaluate(js, pn)
    if result and result.get("ok"):
        return result.get("data")
    return None


def run(sector_type: str, trade_date: Optional[str] = None,
        headless: bool = True, verbose: bool = True, force: bool = False,
        skip_if_exists: bool = True, wait_for_user: bool = False,
        wait_timeout_sec: int = 600,
        progress_callback=None) -> dict:
    """JSONP 循环拉某个 sector_type 的全板块数据 (lib API, 给 Flask 后台调用).

    Args:
        sector_type: 'industry' | 'concept'
        trade_date: 不传 → 自动 last_trading_date(today); 传则用指定日期
        headless: Playwright 启动模式 (后台调用建议 True, 手动调试可 False)
        verbose: 是否 print 到 stderr (后台调用设 False)
        force: 跳过交易日判断, 强制抓取 (不推荐, 节日会错标)
        skip_if_exists: trade_date 在 sector_fund_flow 表里已有数据时早退 (幂等, 默认 True)
                        force=True 时此参数被忽略
        wait_for_user: 第 1 页拿不到数据时不立刻关闭浏览器, 每 5s 轮询一次 (反爬限流兜底)
        wait_timeout_sec: wait_for_user 模式最长等待秒数, 默认 600

    Returns:
        dict: {
            'rows': list[dict],          # detail.html 原始 f-code dict
            'actual_date': str,          # 数据真正归属交易日 (YYYYMMDD)
            'sector_type': str,          # industry / concept
            'total': int,                # 板块总数
            'done': int,                 # 成功拉到的行数
            'failed_pages': list[int],   # 失败页码列表
            'is_already_synced': bool,   # 幂等守卫是否命中
            'is_holiday': bool,          # 今天是否非交易日
            'waited_for_user': bool,     # 是否走过 wait_for_user 轮询
        }
    """
    if sector_type not in SECTOR_TYPES:
        raise ValueError(f'sector_type 必须是 {list(SECTOR_TYPES.keys())} 之一, 收到: {sector_type}')

    all_rows = []
    failed_pages = []
    total = 0
    waited_for_user = False

    def _emit(stage, **kw):
        if progress_callback:
            try:
                progress_callback(stage, sector_type=sector_type, **kw)
            except Exception:
                pass

    # 交易日归属判断
    today = datetime.now().strftime('%Y%m%d')
    if trade_date:
        actual_date = trade_date
    elif force:
        actual_date = today
    else:
        actual_date = last_trading_date(today)
        # 交易日开盘前 (00:00~09:30): push2 还是上一交易日收盘数据, 归今天会覆盖昨天的
        now = datetime.now()
        if actual_date == today and (now.hour, now.minute) < (9, 30):
            actual_date = last_trading_date((now - timedelta(days=1)).strftime('%Y%m%d'))

    is_holiday = (actual_date != today)
    if verbose and is_holiday:
        print(f"[{datetime.now():%H:%M:%S}] {today} 非交易日, 数据归属到 {actual_date}")

    # 幂等守卫: actual_date 已有数据就早退 (force 时跳过)
    is_already_synced = False
    if not force and skip_if_exists:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            row = conn.execute(
                "SELECT COUNT(*) FROM sector_fund_flow WHERE trade_date = ? AND sector_type = ?",
                (actual_date, sector_type),
            ).fetchone()
            conn.close()
            if row and row[0] > 0:
                is_already_synced = True
                if verbose:
                    print(f"[{datetime.now():%H:%M:%S}] {sector_type}/{actual_date} 已有数据 ({row[0]} 行), 跳过抓取")
                _emit("already_synced", actual_date=actual_date, existing_count=row[0])
                return {
                    'rows': [], 'actual_date': actual_date, 'sector_type': sector_type,
                    'total': 0, 'done': 0, 'failed_pages': [],
                    'is_already_synced': True, 'is_holiday': is_holiday,
                    'waited_for_user': False,
                }
        except Exception as e:
            if verbose:
                print(f"[skip_if_exists] 检查失败, 继续拉取: {e}")

    _emit("start", actual_date=actual_date, today=today)

    url_base, page_url = _build_url(sector_type)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="chrome")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        if verbose:
            print(f"[{datetime.now():%H:%M:%S}] [{sector_type}] 打开 {page_url} 建立 session...")
        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # 第 1 页: 看 total
        if verbose:
            print(f"[{sector_type}] 拉第 1 页...")
        data = fetch_page_jsonp(page, 1, url_base)
        if not data or not data.get("data"):
            # 第 1 页失败: wait_for_user 模式兜底 (反爬限流)
            if wait_for_user:
                import time as _time
                if verbose:
                    print(f"[{datetime.now():%H:%M:%S}] [{sector_type}] 第 1 页失败, 进入等待用户刷新模式 (最长 {wait_timeout_sec}s)...")
                waited_for_user = True
                _emit("waiting_for_user", elapsed_sec=0, remaining_sec=wait_timeout_sec)
                deadline = _time.time() + wait_timeout_sec
                poll_count = 0
                while _time.time() < deadline:
                    _time.sleep(5)
                    poll_count += 1
                    elapsed = int(wait_timeout_sec - (deadline - _time.time()))
                    remaining = int(deadline - _time.time())
                    if verbose and poll_count % 2 == 0:
                        print(f"[{datetime.now():%H:%M:%S}] [{sector_type}] 轮询第 {poll_count} 次 (已等 {elapsed}s, 剩 {remaining}s)...")
                    _emit("waiting_for_user", elapsed_sec=elapsed, remaining_sec=remaining)
                    data = fetch_page_jsonp(page, 1, url_base)
                    if data and data.get("data"):
                        if verbose:
                            print(f"[{datetime.now():%H:%M:%S}] ✅ [{sector_type}] 第 {poll_count} 次轮询拿到数据")
                        break
                else:
                    if verbose:
                        print(f"FAIL: [{sector_type}] 等待用户刷新 {wait_timeout_sec}s 超时", file=sys.stderr)
                    _emit("error", message=f"等待用户刷新 {wait_timeout_sec}s 超时")
                    browser.close()
                    return {
                        'rows': [], 'actual_date': actual_date, 'sector_type': sector_type,
                        'total': 0, 'done': 0, 'failed_pages': list(range(1, 6)),
                        'is_already_synced': False, 'is_holiday': is_holiday,
                        'waited_for_user': True,
                    }
            else:
                if verbose:
                    print(f"FAIL: [{sector_type}] 第 1 页拿不到数据", file=sys.stderr)
                _emit("error", message="第 1 页拿不到数据")
                browser.close()
                return {
                    'rows': [], 'actual_date': actual_date, 'sector_type': sector_type,
                    'total': 0, 'done': 0, 'failed_pages': list(range(1, 6)),
                    'is_already_synced': False, 'is_holiday': is_holiday,
                    'waited_for_user': False,
                }

        total = data["data"].get("total", 0)
        diff = data["data"].get("diff") or []
        all_rows.extend(diff)
        if verbose:
            print(f"  [{sector_type}] total={total}, 第 1 页 {len(diff)} 行")
        _emit("total", total=total)

        if total == 0:
            browser.close()
            _emit("done", rows=0, total=0, failed=list(range(2, 6)))
            return {
                'rows': [], 'actual_date': actual_date, 'sector_type': sector_type,
                'total': 0, 'done': 0, 'failed_pages': list(range(2, 6)),
                'is_already_synced': False, 'is_holiday': is_holiday,
                'waited_for_user': waited_for_user,
            }

        import math
        total_pages = math.ceil(total / PZ)
        if verbose:
            print(f"  [{sector_type}] 总页数: {total_pages}（{total} 个板块）")

        # 翻剩余页
        for pn in range(2, total_pages + 1):
            page.wait_for_timeout(WAIT_MS_PER_PAGE)
            data = fetch_page_jsonp(page, pn, url_base)
            if not data or not data.get("data") or not data["data"].get("diff"):
                failed_pages.append(pn)
                if verbose:
                    print(f"  [{sector_type}] pn={pn}: 无数据", file=sys.stderr)
                _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])
                continue
            all_rows.extend(data["data"]["diff"])
            if verbose and (pn % 5 == 0 or pn == total_pages):
                print(f"  [{sector_type}] pn={pn}/{total_pages}, 累计 {len(all_rows)} 行")
            _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])

        browser.close()

    if verbose:
        print(f"\n[{sector_type}] 抓取完成: {len(all_rows)} 行")
        if failed_pages:
            print(f"⚠️ [{sector_type}] 失败页: {failed_pages[:20]}{'...' if len(failed_pages) > 20 else ''}")
    _emit("done", rows=len(all_rows), total=total, failed=failed_pages[:])
    return {
        'rows': all_rows, 'actual_date': actual_date, 'sector_type': sector_type,
        'total': total, 'done': len(all_rows), 'failed_pages': failed_pages[:],
        'is_already_synced': False, 'is_holiday': is_holiday,
        'waited_for_user': waited_for_user,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=list(SECTOR_TYPES.keys()) + ['both'], default='both',
                    help="板块类型: industry(行业) / concept(概念) / both(都跑, 默认)")
    ap.add_argument("--date", type=str, default=None, help="指定 trade_date (YYYYMMDD), 不传则自动归属最近交易日")
    ap.add_argument("--headless", action="store_true", help="无头模式 (默认有头, 方便看浏览器)")
    ap.add_argument("--force", action="store_true", help="跳过交易日判断 + 跳过幂等守卫, 强制重抓")
    ap.add_argument("--wait-for-user", action="store_true", help="第 1 页失败时弹浏览器等用户手动刷新")
    args = ap.parse_args()

    types = list(SECTOR_TYPES.keys()) if args.type == 'both' else [args.type]
    for st in types:
        result = run(
            sector_type=st, trade_date=args.date,
            headless=args.headless, verbose=True, force=args.force,
            skip_if_exists=True, wait_for_user=args.wait_for_user,
        )
        if result['is_already_synced']:
            print(f"⏭  {st}/{result['actual_date']} 已有数据, 跳过")
            continue
        if not result['rows']:
            print(f"❌  {st}/{result['actual_date']} 抓取失败 (0 行)")
            sys.exit(1)
        print(f"✅  {st}/{result['actual_date']} 拉到 {result['done']}/{result['total']} 行 "
              f"({len(result['failed_pages'])} 页失败)")
    print("\n🎉 全部完成")


if __name__ == "__main__":
    main()
