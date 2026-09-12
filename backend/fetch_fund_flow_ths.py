"""
拉取同花顺「个股资金流」全市场排行 (data.10jqka.com.cn/funds/ggzjl/) → fund_flow_ths 表。

为什么不用无头 / JSONP (东财那套):
  同花顺 WAF 检测 Playwright 的自动化标记 (navigator.webdriver + AutomationControlled),
  无头/带标记的请求一律 401/403, 连页面自己的翻页 ajax 都被拦 (2026-08-20 实测).
  唯一可行的组合: 真实 Chrome (channel='chrome') + 有界面 (窗口藏屏幕外) + 反检测标记.
  风险: 同花顺升级规则后可能再失效, 东财 push2 线保留为主力数据源。

页面事实 (2026-08-20 实测):
  - GBK 编码, 表格 .J-ajax-table tbody tr, 每页 50 行
  - 翻页点 a.changePage (「下一页」文本的那个), 共 ~105 页 ≈ 5250 只
  - 列: 序号/股票代码/股票简称/最新价/涨跌幅/换手率/流入资金/流出资金/净额/成交额
    (列映射按表头文本动态识别, 同花顺加减列不会错位)
  - 金额是 '6.90亿' / '5391.24万' 文本 → 换算成元
  - 「即时」board 只有当日数据, 无法补历史 — 历史从上线日起逐日积累
  - 同花顺口径「净额」= 流入-流出 (总资金净流入, 非主力), 落库到 net 列

给 Flask 后台调用: fetch_ths_market(progress_callback, force, skip_if_exists)
命令行手动测试: python3 fetch_fund_flow_ths.py --max-pages 2 --write
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

# 交易日历 / 归属逻辑跟东财版共用 (不复制, import)
from fetch_fund_flow_today import last_trading_date

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')

PAGE_URL = 'https://data.10jqka.com.cn/funds/ggzjl/'
ROWS_PER_PAGE = 50
# 同花顺 WAF 不认 Playwright 的自动化标记, 必须全部抹掉
_LAUNCH_ARGS = ['--window-position=32000,32000', '--window-size=1440,900',
                '--disable-blink-features=AutomationControlled']
_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


def _parse_amount(s):
    """'6.90亿' / '5391.24万' / '20.01%' / '-' / None → 浮点 (元 / % 数值). 失败 None."""
    if s is None:
        return None
    s = str(s).replace(',', '').strip()
    if s in ('', '-', '--'):
        return None
    try:
        if s.endswith('亿'):
            return float(s[:-1]) * 1e8
        if s.endswith('万'):
            return float(s[:-1]) * 1e4
        if s.endswith('%'):
            return float(s[:-1])
        return float(s)
    except ValueError:
        return None


def _ts_code_for(code):
    """6 位代码 → ts_code. '920'→BJ (北交所), '6'/'900'/'905'→SH, '4'/'8'→BJ, '0'/'2'/'3'→SZ."""
    if code.startswith('920'):
        return f'{code}.BJ'
    if code.startswith(('6', '900', '905')):
        return f'{code}.SH'
    if code.startswith(('4', '8')):
        return f'{code}.BJ'
    return f'{code}.SZ'


def _read_page_rows(page):
    """读当前页表格, 按表头文本动态映射列. 返回 list[dict].
    页面上有多个 .J-ajax-table (即时/3日/5日 board 各一个, 只有激活的那个有数据),
    必须挑 tbody 非空的那个, querySelector 拿第一个会拿到空表 (2026-08-21 踩过)."""
    data = page.evaluate("""() => {
        const tables = Array.from(document.querySelectorAll('.J-ajax-table'));
        const table = tables.find(t => t.querySelectorAll('tbody tr').length > 0);
        if (!table) return null;
        const heads = Array.from(table.querySelectorAll('thead th')).map(th => th.innerText.trim());
        const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr =>
            Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()));
        return {heads, rows};
    }""")
    if not data or not data['rows']:
        return []
    heads = data['heads']
    # 表头 → 列索引 (同花顺可能加减列, 按文本认列不按下标)
    col = {}
    for i, h in enumerate(heads):
        h = h.replace('(元)', '').strip()
        if h == '股票代码':
            col['code'] = i
        elif h == '股票简称':
            col['name'] = i
        elif h == '最新价':
            col['close'] = i
        elif h == '涨跌幅':
            col['change_pct'] = i
        elif h == '换手率':
            col['turnover_pct'] = i
        elif h == '流入资金':
            col['inflow'] = i
        elif h == '流出资金':
            col['outflow'] = i
        elif h == '净额':
            col['net'] = i
        elif h == '成交额':
            col['amount'] = i
        elif h == '大单流入':
            col['big_inflow'] = i
    if 'code' not in col:
        return []
    out = []
    for cells in data['rows']:
        def _get(key):
            i = col.get(key)
            return cells[i] if i is not None and i < len(cells) else None
        code = (_get('code') or '').strip()
        if not code.isdigit() or len(code) != 6:
            continue
        out.append({
            'code': code,
            'ts_code': _ts_code_for(code),
            'name': (_get('name') or '').strip(),
            'close': _parse_amount(_get('close')),
            'change_pct': _parse_amount(_get('change_pct')),
            'turnover_pct': _parse_amount(_get('turnover_pct')),
            'inflow': _parse_amount(_get('inflow')),
            'outflow': _parse_amount(_get('outflow')),
            'net': _parse_amount(_get('net')),
            'amount': _parse_amount(_get('amount')),
            'big_inflow': _parse_amount(_get('big_inflow')),
        })
    return out


def _first_row_no(page):
    """当前页首行序号 (str), 用来判断翻页 ajax 是否完成. 读不到返回 ''."""
    try:
        return page.evaluate("""() => {
            const tables = Array.from(document.querySelectorAll('.J-ajax-table'));
            const table = tables.find(t => t.querySelectorAll('tbody tr').length > 0);
            if (!table) return '';
            const td = table.querySelector('tbody tr td');
            return td ? td.innerText.trim() : '';
        }""")
    except Exception:
        return ''


def fetch_ths_market(progress_callback=None, verbose=True, force=False,
                     skip_if_exists=False, max_pages=None) -> tuple:
    """翻页抓全市场个股资金流 (lib API, 给 Flask 后台调用).

    Args:
        progress_callback: 可选回调 (stage, **kw), 协议跟东财版 fetch_today_market 对齐:
            start(actual_date, today) / total(total, total_pages)
            page(pn, total_pages, rows_count, failed)
            done(rows, total, failed)
            already_synced(actual_date, existing_count)
            error(message)
        force: 跳过交易日归属 (写到今天) + 忽略幂等守卫
        skip_if_exists: actual_date 在 fund_flow_ths 已有 ≥4000 行时早退 (幂等)
        max_pages: 只抓前 N 页 (调试用, None=全部)

    Returns:
        (rows, total, failed_pages, actual_date)
    """
    def _emit(stage, **kw):
        if progress_callback:
            try:
                progress_callback(stage, **kw)
            except Exception:
                pass

    # 交易日归属: 跟东财版同一套逻辑 — 休市日/开盘前抓到的是上一交易日数据
    today = datetime.now().strftime('%Y%m%d')
    if force:
        actual_date = today
    else:
        actual_date = last_trading_date(today)
        now = datetime.now()
        if actual_date == today and (now.hour, now.minute) < (9, 30):
            actual_date = last_trading_date((now - timedelta(days=1)).strftime('%Y%m%d'))
    if verbose and actual_date != today:
        print(f"[{datetime.now():%H:%M:%S}] {today} 非交易日(或未开盘), 数据将归属到 {actual_date}")

    # 幂等守卫
    if not force and skip_if_exists:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            row = conn.execute(
                'SELECT COUNT(*) FROM fund_flow_ths WHERE trade_date = ?',
                (actual_date,),
            ).fetchone()
            conn.close()
            if row and row[0] >= 4000:
                if verbose:
                    print(f"[{datetime.now():%H:%M:%S}] {actual_date} 已有数据 ({row[0]} 行), 跳过抓取")
                _emit('already_synced', actual_date=actual_date, existing_count=row[0])
                return [], 0, [], actual_date
        except Exception as e:
            if verbose:
                print(f'[skip_if_exists] 检查失败, 继续拉取: {e}')

    _emit('start', actual_date=actual_date, today=today)

    all_rows = []
    failed_pages = []
    total_pages = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel='chrome', args=_LAUNCH_ARGS)
        ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
        ctx.add_init_script(_INIT_SCRIPT)
        page = ctx.new_page()

        if verbose:
            print(f"[{datetime.now():%H:%M:%S}] 打开 {PAGE_URL} (真实 Chrome, 窗口在屏幕外)...")
        try:
            page.goto(PAGE_URL, wait_until='domcontentloaded', timeout=45000)
            # 首屏服务端先吐 25 行, 页面自己的 ajax 随后清空重画成 50 行 —
            # 中间有瞬间 tbody 是空的, 固定 sleep 会踩空 (2026-08-21 实测).
            # 等「某个表 >= 40 行」= ajax 重画完成
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('.J-ajax-table'))"
                ".some(t => t.querySelectorAll('tbody tr').length >= 40)",
                timeout=30000)
        except Exception as e:
            browser.close()
            _emit('error', message=f'页面打开失败: {e}')
            return [], 0, [], actual_date

        # 总页数: 分页器里最大的 page 属性 (「尾页」链接)
        try:
            total_pages = page.evaluate("""() => {
                const as = Array.from(document.querySelectorAll('.m-page a.changePage'));
                return Math.max(0, ...as.map(a => parseInt(a.getAttribute('page') || '0')));
            }""")
        except Exception:
            total_pages = 0
        if total_pages <= 0:
            # 分页器没渲染出来, 退化为只抓第 1 页
            total_pages = 1
        if max_pages:
            total_pages = min(total_pages, max_pages)
        if verbose:
            print(f'  总页数: {total_pages}')
        _emit('total', total=total_pages * ROWS_PER_PAGE, total_pages=total_pages)

        # 第 1 页 (读空可能是又撞上一次 ajax 重画, 再等一轮)
        rows = _read_page_rows(page)
        if not rows:
            try:
                page.wait_for_function(
                    "() => Array.from(document.querySelectorAll('.J-ajax-table'))"
                    ".some(t => t.querySelectorAll('tbody tr').length >= 40)",
                    timeout=10000)
                rows = _read_page_rows(page)
            except Exception:
                pass
        if not rows:
            browser.close()
            _emit('error', message='第 1 页读不到数据 (表格为空或列结构变了)')
            return [], 0, [1], actual_date
        all_rows.extend(rows)
        _emit('page', pn=1, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])
        if verbose:
            print(f'  pn=1/{total_pages}, {len(rows)} 行')

        # 翻页: 点「下一页」(分页器只显示附近页码, 任意 N 页按钮不一定存在)
        for pn in range(2, total_pages + 1):
            prev_no = _first_row_no(page)
            expected_no = str((pn - 1) * ROWS_PER_PAGE + 1)
            ok = False
            for attempt in range(2):  # 点不动就再点一次
                try:
                    page.locator('a.changePage:has-text("下一页")').first.click(timeout=8000)
                except Exception as e:
                    if verbose:
                        print(f'  pn={pn}: 点下一页失败 (第{attempt+1}次): {e}', file=sys.stderr)
                    continue
                # 等首行序号变成期望值 (ajax 完成), 最多 12s
                try:
                    page.wait_for_function(
                        "(exp) => { const ts = Array.from(document.querySelectorAll('.J-ajax-table'));"
                        " const t = ts.find(x => x.querySelectorAll('tbody tr').length > 0);"
                        " if (!t) return false;"
                        " const td = t.querySelector('tbody tr td');"
                        " return td && td.innerText.trim() === exp; }",
                        arg=expected_no, timeout=12000)
                    ok = True
                    break
                except Exception:
                    cur = _first_row_no(page)
                    if verbose:
                        print(f'  pn={pn}: 等翻页超时 (第{attempt+1}次, 首行={cur}, 期望={expected_no})',
                              file=sys.stderr)
            if not ok:
                failed_pages.append(pn)
                _emit('page', pn=pn, total_pages=total_pages, rows_count=len(all_rows),
                      failed=failed_pages[:])
                continue
            rows = _read_page_rows(page)
            if not rows:
                failed_pages.append(pn)
            else:
                all_rows.extend(rows)
            if verbose and (pn % 10 == 0 or pn == total_pages):
                print(f'  pn={pn}/{total_pages}, 累计 {len(all_rows)} 行')
            _emit('page', pn=pn, total_pages=total_pages, rows_count=len(all_rows),
                  failed=failed_pages[:])
            page.wait_for_timeout(300)  # 别太猛, 像人

        browser.close()

    if verbose:
        print(f'\n抓取完成: {len(all_rows)} 行')
        if failed_pages:
            print(f'⚠️ 失败页: {failed_pages[:20]}{"..." if len(failed_pages) > 20 else ""}')
    _emit('done', rows=len(all_rows), total=total_pages * ROWS_PER_PAGE, failed=failed_pages[:])
    return all_rows, total_pages * ROWS_PER_PAGE, failed_pages[:], actual_date


def write_rows_to_db(rows, trade_date):
    """rows → fund_flow_ths (DELETE+INSERT, 跟 _save_em_fund_flow_rows 同风格). 返回写入数."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cnt = 0
    for r in rows:
        try:
            cur.execute('DELETE FROM fund_flow_ths WHERE ts_code=? AND trade_date=?',
                        (r['ts_code'], trade_date))
            cur.execute('''INSERT INTO fund_flow_ths
                (trade_date, ts_code, code, name, close, change_pct, turnover_pct,
                 inflow, outflow, net, amount, big_inflow)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (trade_date, r['ts_code'], r['code'], r['name'], r['close'], r['change_pct'],
                 r['turnover_pct'], r['inflow'], r['outflow'], r['net'], r['amount'],
                 r['big_inflow']))
            cnt += 1
        except Exception as e:
            print(f'[write] {r.get("code")} 写入失败: {e}', file=sys.stderr)
    conn.commit()
    conn.close()
    return cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-pages', type=int, default=None, help='只抓前 N 页 (调试)')
    ap.add_argument('--write', action='store_true', help='写入 fund_flow_ths 表')
    ap.add_argument('--force', action='store_true', help='跳过交易日归属和幂等守卫')
    args = ap.parse_args()

    rows, total, failed, actual_date = fetch_ths_market(
        verbose=True, force=args.force, skip_if_exists=not args.force,
        max_pages=args.max_pages)
    print(f'\n结果: {len(rows)}/{total} 行, 归属 {actual_date}, 失败页 {failed}')
    if rows:
        print(f'样本: {rows[0]}')
        bj = [r for r in rows if r["ts_code"].endswith(".BJ")]
        print(f'北交所样本: {bj[0] if bj else "(本批无)"}')
    if args.write and rows:
        cnt = write_rows_to_db(rows, actual_date)
        print(f'已写入 fund_flow_ths: {cnt} 行 (trade_date={actual_date})')


if __name__ == '__main__':
    main()
