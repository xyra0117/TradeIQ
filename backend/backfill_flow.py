"""通用补数模块: 反推某一交易日的主力净流入.

原理 (跟 backfill_5d_infer_*.py 一致, 但日期参数化):
  东财实时 5 日累计排行 = 最近 5 个交易日之和.
  target 当日值 = 5 日累计 - (窗口内其余 4 天的当日值, DB 已有).

硬约束: target_date 必须落在"当前最近 5 个交易日窗口"内, 且其余 4 天都已在库.
        更早的缺口滑出窗口后, 实时 5 日排行不再含它, 无法反推.

对外主入口:
  compute_gaps(anchor_date)           -> 检测可补/不可补缺口
  backfill_fund_flow(target_date, ..) -> 执行反推补数 + 健康检查
两者都自带窗口/邻居校验, 供 app.py 的接口层直接调用.
"""
import os
import sys
import math
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_data.db')
SOURCE = 'eastmoney-push2'
WINDOW = 5  # 东财 5 日排行


# ── 数值化 (对齐 app._save_em_fund_flow_rows) ──
def _f(v):
    if v is None or v == '-' or v == '':
        return None
    try:
        x = float(v)
        return 0.0 if math.isnan(x) else x
    except (ValueError, TypeError):
        return None


def _to_ts_code(code):
    code = str(code).strip()
    if not code or len(code) != 6:
        return f'{code}.SH' if code else ''
    return f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ'


# ── 交易日窗口 ──
def get_recent_window(conn, anchor_date, size=WINDOW):
    """返回 <= anchor_date 的最近 size 个交易日 (升序)."""
    rows = conn.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date <= ? ORDER BY cal_date DESC LIMIT ?',
        (anchor_date, size),
    ).fetchall()
    return sorted(r[0] for r in rows)


def _dates_with_data(conn, dates):
    """dates 里哪些在 fund_flow(push2) 已有数据, 返回 set."""
    if not dates:
        return set()
    ph = ','.join('?' * len(dates))
    rows = conn.execute(
        f'SELECT DISTINCT trade_date FROM fund_flow WHERE source=? AND trade_date IN ({ph})',
        (SOURCE, *dates),
    ).fetchall()
    return {r[0] for r in rows}


def check_feasible(conn, target_date, anchor_date):
    """判定 target_date 能否用 5 日反推法补.
    返回 (ok, info) — info 含 window/neighbors/reason.
    """
    window = get_recent_window(conn, anchor_date)
    info = {'target': target_date, 'window': window, 'neighbors': []}
    if target_date not in window:
        info['reason'] = 'out_of_window'
        info['reason_text'] = f'{target_date} 已滑出最近 {WINDOW} 个交易日窗口，实时 5 日排行不再含它，无法反推'
        return False, info
    neighbors = [d for d in window if d != target_date]
    info['neighbors'] = neighbors
    have = _dates_with_data(conn, neighbors)
    missing = [d for d in neighbors if d not in have]
    if missing:
        info['reason'] = 'neighbor_missing'
        info['missing'] = missing
        info['reason_text'] = f'窗口内邻居日缺数据: {",".join(missing)}，无法反推'
        return False, info
    return True, info


def compute_gaps(anchor_date, lookback=15):
    """检测最近 lookback 个交易日里 fund_flow(push2) 的缺口.
    返回 {fillable:[{date,neighbors}], unfillable:[{date,reason_text}], window, checked:[...]}.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        recent = [r[0] for r in conn.execute(
            'SELECT cal_date FROM trading_dates_cache WHERE cal_date <= ? ORDER BY cal_date DESC LIMIT ?',
            (anchor_date, lookback),
        ).fetchall()]
        have = _dates_with_data(conn, recent)
        window = get_recent_window(conn, anchor_date)
        # 当天收盘前 (15:00 前) 不报缺口: 数据还不存在, 收盘后自动同步会补
        from datetime import datetime as _dt
        _now = _dt.now()
        _skip_today = _now.strftime('%Y%m%d') if (_now.hour, _now.minute) < (15, 0) else None
        fillable, unfillable = [], []
        for d in sorted(recent, reverse=True):
            if d in have or d == _skip_today:
                continue
            ok, info = check_feasible(conn, d, anchor_date)
            if ok:
                fillable.append({'date': d, 'neighbors': info['neighbors']})
            else:
                unfillable.append({'date': d, 'reason': info.get('reason'),
                                   'reason_text': info.get('reason_text', '')})
        return {'fillable': fillable, 'unfillable': unfillable,
                'window': window, 'anchor': anchor_date}
    finally:
        conn.close()


# ── 抓取 + 反推 ──
def _fetch_5day_aggregate(target_date, progress_cb=None, headless=False):
    """调 fetch_5day_market, 返回 {ts_code: {...}} 5 日累计."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fetch_fund_flow_today import fetch_5day_market

    rows, total, failed, _ = fetch_5day_market(
        target_date=target_date, progress_callback=progress_cb,
        headless=headless, verbose=True, skip_if_exists=False,
    )
    agg = {}
    for r in rows:
        code = str(r.get('f12', '')).strip()
        if not code or len(code) != 6:
            continue
        agg[_to_ts_code(code)] = {
            'code': code,
            'name': r.get('f14', ''),
            'close': _f(r.get('f2')),
            'main_net_inflow': _f(r.get('f164')) or 0.0,
            'main_net_pct': _f(r.get('f165')),
            'super_net': _f(r.get('f166')), 'super_pct': _f(r.get('f167')),
            'big_net': _f(r.get('f168')), 'big_pct': _f(r.get('f169')),
            'mid_net': _f(r.get('f170')), 'mid_pct': _f(r.get('f171')),
            'small_net': _f(r.get('f172')), 'small_pct': _f(r.get('f173')),
        }
    return agg, total, failed


def _fetch_neighbor_sums(conn, neighbor_dates):
    """邻居各天当日值按 ts_code 求和."""
    ph = ','.join('?' * len(neighbor_dates))
    sql = f"""
        SELECT ts_code,
               SUM(main_net_inflow) AS sum_main,
               SUM(super_net) AS sum_super, SUM(big_net) AS sum_big,
               SUM(mid_net) AS sum_mid,     SUM(small_net) AS sum_small
        FROM fund_flow
        WHERE trade_date IN ({ph}) AND source = ?
        GROUP BY ts_code
    """
    out = {}
    for r in conn.execute(sql, (*neighbor_dates, SOURCE)).fetchall():
        out[r[0]] = {'main': r[1] or 0.0, 'super': r[2] or 0.0, 'big': r[3] or 0.0,
                     'mid': r[4] or 0.0, 'small': r[5] or 0.0}
    return out


def _infer(agg_5d, neighbor_sums, target_date):
    out, skipped = [], 0
    for ts_code, a in agg_5d.items():
        ns = neighbor_sums.get(ts_code)
        if not ns:  # 邻居 4 天无数据 (跨交易日停牌/次新), 跳过
            skipped += 1
            continue
        out.append({
            'trade_date': target_date, 'ts_code': ts_code,
            'code': a['code'], 'name': a['name'], 'close': a['close'],
            'change_pct': None,  # 5 日排行无当日涨跌幅
            'main_net_inflow': a['main_net_inflow'] - ns['main'],
            'main_net_pct': a['main_net_pct'],  # 占比无可减语义, 留 5 日值
            'super_net': (a['super_net'] or 0) - ns['super'], 'super_pct': a['super_pct'],
            'big_net': (a['big_net'] or 0) - ns['big'], 'big_pct': a['big_pct'],
            'mid_net': (a['mid_net'] or 0) - ns['mid'], 'mid_pct': a['mid_pct'],
            'small_net': (a['small_net'] or 0) - ns['small'], 'small_pct': a['small_pct'],
        })
    return out, skipped


def _upsert(conn, rows):
    cur = conn.cursor()
    for r in rows:
        cur.execute('DELETE FROM fund_flow WHERE ts_code=? AND trade_date=?',
                    (r['ts_code'], r['trade_date']))
        cur.execute('''
            INSERT INTO fund_flow
            (trade_date, ts_code, code, name, close, change_pct,
             main_net_inflow, main_net_pct, super_net, super_pct,
             big_net, big_pct, mid_net, mid_pct, small_net, small_pct, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (r['trade_date'], r['ts_code'], r['code'], r['name'], r['close'],
              r['change_pct'], r['main_net_inflow'], r['main_net_pct'],
              r['super_net'], r['super_pct'], r['big_net'], r['big_pct'],
              r['mid_net'], r['mid_pct'], r['small_net'], r['small_pct'], SOURCE))
    conn.commit()
    return len(rows)


# ── 健康检查 ──
def _day_stats(conn, date):
    r = conn.execute(
        '''SELECT COUNT(*), SUM(main_net_inflow), SUM(ABS(main_net_inflow)),
                  SUM(CASE WHEN ABS(main_net_inflow)>1e9 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN main_net_inflow IS NULL THEN 1 ELSE 0 END),
                  MAX(ABS(main_net_inflow))
           FROM fund_flow WHERE source=? AND trade_date=?''',
        (SOURCE, date)).fetchone()
    return {'rows': r[0] or 0, 'net': r[1] or 0.0, 'abs': r[2] or 0.0,
            'n10': r[3] or 0, 'nulls': r[4] or 0, 'max_abs': r[5] or 0.0}


def compute_health(conn, target_date, neighbor_dates):
    """补数后健康检查: target 各指标 vs 邻居区间. 返回给前端展示的结构."""
    t = _day_stats(conn, target_date)
    ns = [_day_stats(conn, d) for d in neighbor_dates]

    def rng(key):
        vals = [n[key] for n in ns]
        return (min(vals), max(vals)) if vals else (0, 0)

    def in_rng(v, lo, hi, pad=0.0):
        span = (hi - lo) or abs(hi) or 1
        return (lo - span * pad) <= v <= (hi + span * pad)

    net_lo, net_hi = rng('net')
    abs_lo, abs_hi = rng('abs')
    n10_lo, n10_hi = rng('n10')
    row_lo, row_hi = rng('rows')

    # 单只最大值 + 邻居各天单只峰值上限
    top = conn.execute(
        '''SELECT name, main_net_inflow FROM fund_flow
           WHERE source=? AND trade_date=?
           ORDER BY ABS(main_net_inflow) DESC LIMIT 1''',
        (SOURCE, target_date)).fetchone()
    neigh_peak = max((n['max_abs'] for n in ns), default=0.0)
    tail_ratio = (t['max_abs'] / neigh_peak) if neigh_peak else 0.0
    tail_warn = tail_ratio > 1.5

    checks = {
        'net': {'val': t['net'], 'lo': net_lo, 'hi': net_hi,
                'ok': in_rng(t['net'], net_lo, net_hi, 0.15)},
        'abs': {'val': t['abs'], 'lo': abs_lo, 'hi': abs_hi,
                'ok': in_rng(t['abs'], abs_lo, abs_hi, 0.15)},
        'n10': {'val': t['n10'], 'lo': n10_lo, 'hi': n10_hi,
                'ok': in_rng(t['n10'], n10_lo, n10_hi, 0.3)},
        'rows': {'val': t['rows'], 'lo': row_lo, 'hi': row_hi, 'nulls': t['nulls'],
                 'ok': t['nulls'] == 0 and t['rows'] >= row_lo * 0.98},
    }
    all_ok = all(c['ok'] for c in checks.values())
    return {
        'target': target_date, 'rows': t['rows'],
        'checks': checks,
        'tail': {'name': top[0] if top else '', 'max_abs': t['max_abs'],
                 'neigh_peak': neigh_peak, 'ratio': round(tail_ratio, 2), 'warn': tail_warn},
        'verdict': 'ok' if (all_ok and not tail_warn) else ('warn' if all_ok else 'suspect'),
    }


# ── 主入口 ──
def backfill_fund_flow(target_date, anchor_date=None, force=False, progress_cb=None, headless=False):
    """执行反推补数. 返回 dict:
      成功: {ok:True, inserted, skipped, failed_pages, health:{...}, neighbors:[...]}
      失败: {ok:False, reason, reason_text, ...}
    """
    anchor_date = anchor_date or target_date
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        # 1) 可行性 (窗口 + 邻居齐全)
        ok, info = check_feasible(conn, target_date, anchor_date)
        if not ok:
            return {'ok': False, **info}
        neighbors = info['neighbors']

        # 2) 覆盖守卫
        exist = conn.execute(
            'SELECT COUNT(*) FROM fund_flow WHERE source=? AND trade_date=?',
            (SOURCE, target_date)).fetchone()[0]
        if exist and not force:
            return {'ok': False, 'reason': 'already_exists', 'existing_count': exist,
                    'reason_text': f'{target_date} 已有 {exist} 行，默认不覆盖（force 可强制重补）',
                    'neighbors': neighbors}

        # 3) 抓 5 日排行
        if progress_cb:
            progress_cb('phase', phase='fetch')
        agg_5d, total, failed = _fetch_5day_aggregate(target_date, progress_cb, headless)
        if not agg_5d:
            return {'ok': False, 'reason': 'fetch_empty',
                    'reason_text': '5 日排行抓取为空', 'failed_pages': failed}

        # 4) 反推
        if progress_cb:
            progress_cb('phase', phase='infer')
        neighbor_sums = _fetch_neighbor_sums(conn, neighbors)
        rows, skipped = _infer(agg_5d, neighbor_sums, target_date)
        if not rows:
            return {'ok': False, 'reason': 'infer_empty', 'reason_text': '反推结果为空'}

        # 5) 写库
        if progress_cb:
            progress_cb('phase', phase='write')
        inserted = _upsert(conn, rows)

        # 6) 健康检查
        if progress_cb:
            progress_cb('phase', phase='validate')
        health = compute_health(conn, target_date, neighbors)

        return {'ok': True, 'inserted': inserted, 'skipped': skipped,
                'failed_pages': failed, 'neighbors': neighbors, 'health': health}
    finally:
        conn.close()


if __name__ == '__main__':
    # CLI: python backfill_flow.py YYYYMMDD [--force]
    if len(sys.argv) < 2:
        print('用法: python backfill_flow.py YYYYMMDD [--force]')
        sys.exit(1)
    td = sys.argv[1]
    force = '--force' in sys.argv
    def _cli_cb(stage, **kw):
        if stage == 'page' and kw.get('pn', 0) % 10 == 0:
            print(f"  pn={kw['pn']}/{kw.get('total_pages')} 累计 {kw.get('rows_count')} 行")
        elif stage == 'phase':
            print(f"[{kw.get('phase')}]")
    res = backfill_fund_flow(td, force=force, progress_cb=_cli_cb, headless=False)
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))
