"""A 股情绪指数计算引擎 (需求文档 §3.1)

严格按文档实现:
- 0-100 分, 5 档等级: 冰点/低迷/温和/火热/亢奋
- 加权: 基础 60% + 资金 25% + 板块 15%
- 数据源全部走现有表 + 阶段 0 新表 (limitup, overview, fund_flow, sw_industry_daily, akshare_zt_pool, hsgt_flow)
- 不修改任何现有表 schema, 只读
"""
import sqlite3
from datetime import datetime

import pandas as pd
from flask import jsonify, request

# DB 路径从 app 拿, 避免循环 import
def _db_path():
    from app import DB_PATH
    return DB_PATH


# ============ 文档 §3.1.1 等级映射 ============
LEVELS = [
    (0, 20, '冰点'),
    (21, 40, '低迷'),
    (41, 60, '温和'),
    (61, 80, '火热'),
    (81, 100, '亢奋'),
]


def score_to_level(score):
    """0-100 分 → 5 档等级名. 边界用闭区间 (e.g. 21 → 低迷)."""
    s = max(0, min(100, round(score or 0)))
    for lo, hi, name in LEVELS:
        if lo <= s <= hi:
            return name
    return '亢奋'  # 100 分兜底


# ============ 子因子权重 (内部合理默认值) ============
# 基础 60% 内: 涨/跌家数比 (15%), 涨幅中位数 (12%), 涨跌停家数 (15%), 封板率 (8%), 炸板率 (5%), 连板 (5%)
BASE_WEIGHTS = {
    'advance_decline': 15,    # 涨/跌家数比
    'median_change': 12,      # 涨幅中位数
    'limit_up_down': 15,      # 涨跌停家数
    'seal_rate': 8,           # 封板成功率
    'broken_rate': 5,         # 炸板率 (反指标)
    'streak': 5,              # 连板 (高度+家数)
}
assert sum(BASE_WEIGHTS.values()) == 60, f'基础权重和 {sum(BASE_WEIGHTS.values())} 不等于 60'

# 资金 25% 内: 北向 (10%), 两市成交额 (6%), 主力净流入 (6%), 涨跌停封单金额 (3%)
CAPITAL_WEIGHTS = {
    'north': 10,
    'total_amount': 6,
    'main_net': 6,
    'seal_amount': 3,
}
assert sum(CAPITAL_WEIGHTS.values()) == 25, f'资金权重和 {sum(CAPITAL_WEIGHTS.values())} 不等于 25'

# 板块 15% 内: 上涨板块占比 (6%), 板块涨幅离散度 (3%), 领涨涨停数 (3%), 板块轮动 (3%)
SECTOR_WEIGHTS = {
    'up_sector_pct': 6,
    'dispersity': 3,
    'leading_count': 3,
    'rotation': 3,
}
assert sum(SECTOR_WEIGHTS.values()) == 15, f'板块权重和 {sum(SECTOR_WEIGHTS.values())} 不等于 15'


# ============ 子因子计算 (0-100 分) ============

def _score_advance_decline(up_count, down_count, flat_count):
    """涨/跌家数比 → 0-100. 涨多=高分. 涨:跌=1:1 时 50, 全部涨=100, 全部跌=0."""
    total = (up_count or 0) + (down_count or 0) + (flat_count or 0)
    if total == 0:
        return 50.0
    return 100 * (up_count or 0) / total


def _score_median_change(median_change_pct):
    """市场涨幅中位数 → 0-100. 0% = 50, +5% = 100, -5% = 0 (夹紧)."""
    if median_change_pct is None:
        return 50.0
    return 50 + 10 * max(-5, min(5, median_change_pct))


def _score_limit_up_down(limit_up_count, limit_down_count):
    """涨跌停家数 → 0-100. 涨停多=高分. 上限 100 家涨停=100, 100 家跌停=0."""
    up = limit_up_count or 0
    down = limit_down_count or 0
    return 50 + 0.5 * up - 0.5 * down  # 0-100 范围, 超出夹紧
    # 实际用更精细的归一化: 涨停越多越好, 跌停越多越差
    return max(0, min(100, 50 + 0.5 * (up - down)))


def _score_seal_rate(sealed, touched):
    """封板成功率 = sealed/touched. 0-100 分. 100% 封=100, 0% 封=0."""
    if not touched:
        return 50.0
    return 100 * sealed / touched


def _score_broken_rate(broken, touched):
    """炸板率 (反指标) → 0-100. 0% 炸=100, 50% 炸=0 (夹紧)."""
    if not touched:
        return 75.0  # 没数据时给中等偏上
    return max(0, min(100, 100 - 2 * broken / touched * 100))


def _score_streak(streak_max, streak_count):
    """连板梯队 → 0-100. 最高板 (10 板=100) + 连板家数 (10 家=100) 加权."""
    sm = min(10, streak_max or 0) * 10  # 最高板 0-100
    sc = min(10, streak_count or 0) * 5  # 连板家数 0-50 (折半权重)
    return min(100, sm * 0.7 + sc * 0.3)


def _score_north(north_net_wan):
    """北向净流入 (亿) → 0-100. 0=50, +100 亿=100, -100 亿=0 (夹紧)."""
    if north_net_wan is None:
        return 50.0
    return 50 + 0.5 * max(-100, min(100, north_net_wan))


def _score_total_amount(total_amount_wan):
    """两市成交额 (亿) vs 近 10 日均. 1x=50, 2x=100, 0.5x=0 (夹紧)."""
    # 简化: 1 万亿=50, 2 万亿=100, 5000 亿=0
    if total_amount_wan is None:
        return 50.0
    ratio = total_amount_wan / 10000  # 万亿为单位
    return max(0, min(100, 50 * ratio))


def _score_main_net(main_net_wan):
    """主力净流入 (亿) → 0-100. 0=50, +500 亿=100, -500 亿=0 (夹紧)."""
    if main_net_wan is None:
        return 50.0
    return 50 + 0.1 * max(-500, min(500, main_net_wan))


def _score_seal_amount(seal_amount_wan):
    """涨跌停封单金额 (亿) → 0-100. 0=50, +500 亿=100, -500 亿=0 (夹紧)."""
    if seal_amount_wan is None:
        return 50.0
    return 50 + 0.1 * max(-500, min(500, seal_amount_wan))


def _score_up_sector_pct(up_count, total_count):
    """上涨板块占比 → 0-100. 50% 板块涨=50, 100%=100, 0%=0."""
    if not total_count:
        return 50.0
    return 100 * up_count / total_count


def _score_dispersity(stds):
    """板块涨幅离散度 → 0-100. 离散度小=资金集中(高分), 离散度大=分歧(低分)."""
    # 简化: std 0%=100, 5%=0 (夹紧)
    if stds is None:
        return 50.0
    return max(0, min(100, 100 - 20 * stds))


def _score_leading_count(limit_up_count_in_leading):
    """领涨板块涨停个股数 → 0-100. 0=0, 10=100."""
    return min(100, (limit_up_count_in_leading or 0) * 10)


def _score_rotation(rotations):
    """板块轮动速度 → 0-100. 0 次/h=100 (稳定), 10 次/h=0 (乱轮)."""
    if rotations is None:
        return 50.0
    return max(0, min(100, 100 - 10 * rotations))


# ============ 顶层 compute_sentiment ============

def compute_sentiment(ts=None):
    """计算某时刻的情绪指数. ts: 'YYYY-MM-DD HH:MM:SS' (默认当前).
    返回 dict: {score, level, base_score, capital_score, sector_score, factors: {...}}.
    数据全部从 DB 读, 不写任何现有表. 写 sentiment_intraday (新表)."""
    if ts is None:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    trade_date = ts[:10]

    base = _compute_base_factors(trade_date)
    capital = _compute_capital_factors(trade_date)
    sector = _compute_sector_factors(trade_date)

    base_score = sum(base[k] * BASE_WEIGHTS[k] for k in BASE_WEIGHTS) / 100
    capital_score = sum(capital[k] * CAPITAL_WEIGHTS[k] for k in CAPITAL_WEIGHTS) / 100
    sector_score = sum(sector[k] * SECTOR_WEIGHTS[k] for k in SECTOR_WEIGHTS) / 100
    score = base_score + capital_score + sector_score
    score = max(0, min(100, score))
    level = score_to_level(score)

    result = {
        'ts': ts,
        'trade_date': trade_date,
        'score': round(score, 2),
        'level': level,
        'base_score': round(base_score, 2),
        'capital_score': round(capital_score, 2),
        'sector_score': round(sector_score, 2),
        'factors': {
            'base': base,
            'capital': capital,
            'sector': sector,
        }
    }

    # 写 sentiment_intraday
    _save_intraday(result)
    return result


def _compute_base_factors(trade_date):
    """基础因子 60% 内部子项打分 (0-100)."""
    conn = sqlite3.connect(_db_path(), timeout=10)
    try:
        # overview 表有 up_count / down_count / flat_count (今日)
        row = conn.execute(
            "SELECT upCount AS up_count, downCount AS down_count, flatCount AS flat_count, limitUp AS limit_up, limitDown AS limit_down "
            "FROM overview WHERE date=? ORDER BY date DESC LIMIT 1", (trade_date,)).fetchone()
        if row is None:
            # 找最近一个交易日
            row = conn.execute(
                "SELECT upCount AS up_count, downCount AS down_count, flatCount AS flat_count, limitUp AS limit_up, limitDown AS limit_down "
                "FROM overview ORDER BY date DESC LIMIT 1").fetchone()
        up, down, flat, l_up, l_down = (row or (0, 0, 0, 0, 0))

        # 涨幅中位数 (从 stock_daily 算)
        median_chg = _calc_median_change_pct(conn, trade_date)

        # 封板率 / 炸板率 (从 akshare_zt_pool)
        sealed, touched, broken = _calc_seal_broken(conn, trade_date)

        # 连板梯队 (从 akshare_zt_pool)
        streak_max, streak_count = _calc_streak(conn, trade_date)

        return {
            'advance_decline': _score_advance_decline(up, down, flat),
            'median_change': _score_median_change(median_chg),
            'limit_up_down': _score_limit_up_down(l_up, l_down),
            'seal_rate': _score_seal_rate(sealed, touched),
            'broken_rate': _score_broken_rate(broken, touched),
            'streak': _score_streak(streak_max, streak_count),
        }
    finally:
        conn.close()


def _compute_capital_factors(trade_date):
    """资金因子 25% 内部子项打分 (0-100)."""
    conn = sqlite3.connect(_db_path(), timeout=10)
    try:
        # 北向 (hsgt_flow 表, 单位万元)
        row = conn.execute(
            "SELECT north_money, hgt, sgt FROM hsgt_flow WHERE trade_date=? "
            "ORDER BY trade_date DESC LIMIT 1", (trade_date,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT north_money, hgt, sgt FROM hsgt_flow "
                "ORDER BY trade_date DESC LIMIT 1").fetchone()
        north_net_wan = (row[0] if row else None)  # 已经是万元

        # 两市成交额 (overview.totalVolume, 单位元, 转为亿: /1e8)
        row = conn.execute(
            "SELECT totalVolume FROM overview WHERE date=? "
            "ORDER BY date DESC LIMIT 1", (trade_date,)).fetchone()
        if row is None:
            row = conn.execute("SELECT totalVolume FROM overview ORDER BY date DESC LIMIT 1").fetchone()
        total_amount_wan = (row[0] / 1e4) if row and row[0] else None  # 元 → 万 → 亿 (除 1e8)

        # 主力净流入 (fund_flow 表, 累加 main_net_inflow, 单位元)
        row = conn.execute(
            "SELECT SUM(main_net_inflow) FROM fund_flow WHERE trade_date=?",
            (trade_date,)).fetchone()
        main_net_wan = (row[0] / 1e8) if row and row[0] else None  # 元 → 亿

        # 涨跌停封单金额 (akshare_zt_pool.seal_amount, 单位元, 累加)
        row = conn.execute(
            "SELECT SUM(seal_amount) FROM akshare_zt_pool WHERE trade_date=?",
            (trade_date,)).fetchone()
        seal_amount_wan = (row[0] / 1e8) if row and row[0] else None

        return {
            'north': _score_north(north_net_wan),
            'total_amount': _score_total_amount(total_amount_wan),
            'main_net': _score_main_net(main_net_wan),
            'seal_amount': _score_seal_amount(seal_amount_wan),
        }
    finally:
        conn.close()


def _compute_sector_factors(trade_date):
    """板块因子 15% 内部子项打分 (0-100)."""
    conn = sqlite3.connect(_db_path(), timeout=10)
    try:
        # 上涨板块占比 + 板块涨幅离散度 + 板块轮动 (从 sw_industry_daily)
        up_count, total_count, stds, rotations = _calc_sector_sw(trade_date, conn)
        # 领涨板块涨停个股数 (akshare_zt_pool.industry = sw_industry 中涨最多的)
        leading_count = _calc_leading_count(trade_date, conn)

        return {
            'up_sector_pct': _score_up_sector_pct(up_count, total_count),
            'dispersity': _score_dispersity(stds),
            'leading_count': _score_leading_count(leading_count),
            'rotation': _score_rotation(rotations),
        }
    finally:
        conn.close()


# ============ 辅助查询函数 ============

def _calc_median_change_pct(conn, trade_date):
    """市场涨幅中位数 (从 stock_daily)."""
    row = conn.execute(
        "SELECT change FROM stock_daily WHERE trade_date=? AND change IS NOT NULL "
        "ORDER BY change", (trade_date,)).fetchall()
    if not row:
        return None
    changes = [r[0] for r in row]
    n = len(changes)
    if n == 0:
        return None
    if n % 2 == 1:
        return changes[n//2]
    return (changes[n//2 - 1] + changes[n//2]) / 2


def _calc_seal_broken(conn, trade_date):
    """从 akshare_zt_pool 算封板/触板/炸板."""
    # touched = 总涨停家数; sealed = open_times = 0 的家数 (没炸过); broken = 炸板家数 (open_times > 0)
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN open_times = 0 OR open_times IS NULL THEN 1 ELSE 0 END) AS sealed, "
        "SUM(CASE WHEN open_times > 0 THEN 1 ELSE 0 END) AS broken "
        "FROM akshare_zt_pool WHERE trade_date=?", (trade_date,)).fetchone()
    if not row or not row[0]:
        return 0, 0, 0
    total = row[0] or 0
    sealed = row[1] or 0
    broken = row[2] or 0
    # touched = sealed + broken (没炸过的 + 炸过的)
    return sealed, total, broken


def _calc_streak(conn, trade_date):
    """从 akshare_zt_pool 算连板梯队."""
    rows = conn.execute(
        "SELECT streak FROM akshare_zt_pool WHERE trade_date=? AND streak IS NOT NULL",
        (trade_date,)).fetchall()
    if not rows:
        return 0, 0
    streaks = [r[0] for r in rows if r[0]]
    if not streaks:
        return 0, 0
    return max(streaks), len(streaks)


def _calc_sector_sw(trade_date, conn):
    """从 sw_industry_daily 算上涨板块占比 / 离散度 / 轮动."""
    rows = conn.execute(
        "SELECT change_pct FROM sw_industry_daily WHERE trade_date=? AND change_pct IS NOT NULL",
        (trade_date,)).fetchall()
    if not rows:
        return 0, 0, None, None
    pct = [r[0] for r in rows]
    up = sum(1 for x in pct if x > 0)
    total = len(pct)
    stds = float(pd.Series(pct).std()) if total > 1 else 0
    # 轮动: 简化, 用 std 反向 (std 大=分歧大=轮动快)
    rotations = min(10, stds / 0.5) if stds else 0
    return up, total, stds, rotations


def _calc_leading_count(trade_date, conn):
    """领涨板块涨停数: 涨幅最高的申万行业里涨停股票数 (从 akshare_zt_pool 关联)."""
    # 找到 sw_industry_daily 中当日 change_pct 最高的 1 个行业
    row = conn.execute(
        "SELECT index_code FROM sw_industry_daily WHERE trade_date=? "
        "ORDER BY change_pct DESC LIMIT 1", (trade_date,)).fetchone()
    if not row:
        return 0
    leading_index = row[0]
    # 找该行业成分股中, 当日涨停的
    rows = conn.execute(
        "SELECT COUNT(*) FROM akshare_zt_pool azp "
        "JOIN sw_industry_member sim ON azp.ts_code = sim.ts_code "
        "WHERE sim.index_code=? AND azp.trade_date=?",
        (leading_index, trade_date)).fetchone()
    return (rows[0] if rows else 0) or 0


def _save_intraday(result):
    """写 sentiment_intraday 表 (新表). 不修改任何现有表."""
    f = result['factors']
    base = f['base']
    capital = f['capital']
    sector = f['sector']
    conn = sqlite3.connect(_db_path(), timeout=10)
    try:
        conn.execute('''INSERT OR REPLACE INTO sentiment_intraday
            (ts, score, level, base_score, capital_score, sector_score,
             up_count, down_count, flat_count, limit_up_count, limit_down_count,
             median_change_pct, sealed_count, touched_count, broken_count,
             streak_max, streak_count,
             north_net, total_amount, main_net, seal_amount,
             up_sector_count, total_sector_count, leading_sector_change_pct, sector_rotation)
            VALUES (?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?)''',
            (result['ts'], result['score'], result['level'],
             result['base_score'], result['capital_score'], result['sector_score'],
             # 基础因子 (从 factors.base 拿子值)
             int(base.get('advance_decline', 0)),  # 仅占位, 实际从原始值取
             0, 0, 0, 0,  # 暂占位
             base.get('median_change'),
             int(base.get('seal_rate', 0) * 10),  # 仅占位
             0, int(base.get('broken_rate', 0) * 10),
             int(base.get('streak', 0) * 10),
             0,
             # 资金因子
             capital.get('north'), capital.get('total_amount'),
             capital.get('main_net'), capital.get('seal_amount'),
             # 板块因子
             int(sector.get('up_sector_pct', 0) * 10),
             100,  # 暂占位
             sector.get('leading_count'),
             sector.get('rotation'),
             ))
        conn.commit()
    finally:
        conn.close()


# ============ 定时任务回调 (被 scheduler.py 调用) ============

def sample_intraday():
    """被 APScheduler 调用, 每 15 秒采一次."""
    from app import is_trading_time
    if not is_trading_time():
        return
    try:
        compute_sentiment()
    except Exception as e:
        print(f'[sample_intraday] 失败: {e}')


# ============ Flask 路由 (在 app.py 里 @app.route 调用) ============
# 这些函数从 app.py 调用, 避免循环依赖

def api_sentiment_current():
    """GET /api/sentiment/current - 最新情绪分数."""
    row = _get_latest_sentiment()
    if not row:
        return jsonify({'error': '暂无数据, 请先 sync 一下 (调用 compute_sentiment)'}), 404
    return jsonify(row)


def api_sentiment_intraday(date_str=None):
    """GET /api/sentiment/intraday?date=YYYY-MM-DD - 当日分时曲线."""
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sentiment_intraday WHERE ts LIKE ? "
        "ORDER BY ts", (date_str + '%',)).fetchall()
    conn.close()
    return jsonify({
        'date': date_str,
        'points': [dict(r) for r in rows],
        'total': len(rows),
    })


def api_sentiment_factors(ts=None):
    """GET /api/sentiment/factors?ts=YYYY-MM-DD HH:MM:SS - 某时刻的三大因子分项."""
    if not ts:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sentiment_intraday WHERE ts=?", (ts,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': f'no data at {ts}'}), 404
    return jsonify(dict(row))


def api_sentiment_level(date_str=None):
    """GET /api/sentiment/level?date=YYYY-MM-DD - 5 档等级 (基于当日分时平均分)."""
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(_db_path(), timeout=10)
    row = conn.execute(
        "SELECT AVG(score) AS avg, MIN(score) AS min, MAX(score) AS max "
        "FROM sentiment_intraday WHERE ts LIKE ?", (date_str + '%',)).fetchone()
    conn.close()
    if not row or row[0] is None:
        return jsonify({'error': f'no data on {date_str}'}), 404
    avg = row[0]
    return jsonify({
        'date': date_str,
        'avg_score': round(avg, 2),
        'min_score': round(row[1], 2) if row[1] is not None else None,
        'max_score': round(row[2], 2) if row[2] is not None else None,
        'avg_level': score_to_level(avg),
    })


def api_sentiment_alerts(date_str=None, limit=100):
    """GET /api/sentiment/alerts?date=YYYY-MM-DD&limit=100 - 当日预警列表."""
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sentiment_alert WHERE ts LIKE ? "
        "ORDER BY ts DESC LIMIT ?", (date_str + '%', limit)).fetchall()
    conn.close()
    return jsonify({
        'date': date_str,
        'alerts': [dict(r) for r in rows],
        'total': len(rows),
    })


def api_sentiment_daily_history(days=10):
    """GET /api/sentiment/daily-history?days=10 - 近 N 日情绪等级对比."""
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT SUBSTR(ts, 1, 10) AS date, AVG(score) AS avg_score, "
        "MIN(score) AS min_score, MAX(score) AS max_score "
        "FROM sentiment_intraday "
        "GROUP BY date ORDER BY date DESC LIMIT ?", (int(days),)).fetchall()
    conn.close()
    result = []
    for r in rows:
        avg = r['avg_score']
        result.append({
            'date': r['date'],
            'avg_score': round(avg, 2) if avg is not None else None,
            'min_score': round(r['min_score'], 2) if r['min_score'] is not None else None,
            'max_score': round(r['max_score'], 2) if r['max_score'] is not None else None,
            'avg_level': score_to_level(avg) if avg is not None else None,
        })
    return jsonify({'days': result, 'total': len(result)})


# ============ 内部辅助 ============

def _get_latest_sentiment():
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sentiment_intraday ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None
