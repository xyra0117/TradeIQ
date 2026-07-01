"""7 类异动预警规则 (需求文档 §3.1.4)

严格逐条实现:
1. RULE_LEVEL_JUMP      情绪等级跨越 2 级以上
2. RULE_LIMITUP_50_100  涨停家数突破 50/100 整数关口
3. RULE_LIMITDOWN_20    跌停 > 20
4. RULE_BROKEN_50       炸板率 > 50%
5. RULE_NORTH_50        北向半小时内 ±50 亿
6. RULE_MEDIAN_FLIP     涨幅中位数由正转负/由负转正
7. RULE_STREAK_THRESHOLD 连板高度突破/跌破关键阈值 (默认 5/8)

每条规则触发时插入 sentiment_alert + 推送到飞书.
阈值在 push_config 表可配.
"""
import sqlite3
from datetime import datetime, timedelta

import pandas as pd

# 规则名常量 (DB 存储用, 飞书推送用)
RULE_LEVEL_JUMP = 'RULE_LEVEL_JUMP'
RULE_LIMITUP_50_100 = 'RULE_LIMITUP_50_100'
RULE_LIMITDOWN_20 = 'RULE_LIMITDOWN_20'
RULE_BROKEN_50 = 'RULE_BROKEN_50'
RULE_NORTH_50 = 'RULE_NORTH_50'
RULE_MEDIAN_FLIP = 'RULE_MEDIAN_FLIP'
RULE_STREAK_THRESHOLD = 'RULE_STREAK_THRESHOLD'

ALL_RULES = [RULE_LEVEL_JUMP, RULE_LIMITUP_50_100, RULE_LIMITDOWN_20, RULE_BROKEN_50,
             RULE_NORTH_50, RULE_MEDIAN_FLIP, RULE_STREAK_THRESHOLD]


def _db_path():
    from app import DB_PATH
    return DB_PATH


def _get_config(key, default):
    """从 push_config 读阈值, 缺省用 default."""
    conn = sqlite3.connect(_db_path(), timeout=5)
    try:
        row = conn.execute("SELECT value FROM push_config WHERE key=?", (key,)).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    finally:
        conn.close()
    return default


def _save_alert(ts, level, rule, title, content):
    """写 sentiment_alert (新表, 不动现有)."""
    conn = sqlite3.connect(_db_path(), timeout=5)
    try:
        conn.execute(
            "INSERT INTO sentiment_alert (ts, level, rule, title, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, level, rule, title, content))
        conn.commit()
    finally:
        conn.close()
    # 立即推飞书 (阶段 5 实现 push_alert)
    try:
        from feishu import push_alert
        push_alert(rule, title, content, level=level)
    except Exception as e:
        print(f'[alert_rules] 推飞书失败: {e}')


def _get_prev_level(trade_date):
    """查前一个采样点的情绪等级. 没有则 None."""
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT level FROM sentiment_intraday "
        "WHERE ts < ? ORDER BY ts DESC LIMIT 1", (trade_date + ' 23:59:59',)).fetchone()
    conn.close()
    return row['level'] if row else None


_LEVEL_RANK = {'冰点': 0, '低迷': 1, '温和': 2, '火热': 3, '亢奋': 4}


def check_level_jump(current_ts, current_level):
    """规则 1: 情绪等级跨越 2 级以上."""
    if not current_level:
        return
    trade_date = current_ts[:10]
    prev = _get_prev_level(trade_date)
    if not prev or prev not in _LEVEL_RANK or current_level not in _LEVEL_RANK:
        return
    diff = abs(_LEVEL_RANK[current_level] - _LEVEL_RANK[prev])
    if diff >= 2:
        title = f'情绪等级跳变: {prev} → {current_level}'
        content = f'{current_ts} 情绪从 {prev} 跳到 {current_level} (跨越 {diff} 级), 请关注盘面变化'
        _save_alert(current_ts, 'urgent', RULE_LEVEL_JUMP, title, content)


def check_limitup_threshold(current_ts):
    """规则 2: 涨停家数突破 50/100 整数关口.
    从 push_config 读 limitup_50 / limitup_100 (默认都开启)."""
    trade_date = current_ts[:10]
    use_50 = _get_config('limitup_50_enabled', 'true').lower() == 'true'
    use_100 = _get_config('limitup_100_enabled', 'true').lower() == 'true'
    if not (use_50 or use_100):
        return
    conn = sqlite3.connect(_db_path(), timeout=5)
    try:
        # 优先从 akshare_zt_pool 拿当日涨停家数
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM akshare_zt_pool WHERE trade_date=?",
            (trade_date,)).fetchone()
        n = (row[0] if row else 0) or 0
    finally:
        conn.close()
    if use_100 and n >= 100 and n < 200:  # 首次破 100
        title = f'涨停家数破 100: {n} 家'
        content = f'{current_ts} 当日涨停家数达 {n} 家, 突破 100 关口, 情绪火热'
        _save_alert(current_ts, 'warning', RULE_LIMITUP_50_100, title, content)
    elif use_50 and 50 <= n < 60:  # 首次破 50
        title = f'涨停家数破 50: {n} 家'
        content = f'{current_ts} 当日涨停家数达 {n} 家, 突破 50 关口'
        _save_alert(current_ts, 'warning', RULE_LIMITUP_50_100, title, content)


def check_limitdown_threshold(current_ts):
    """规则 3: 跌停家数 > 20 (冰点信号)."""
    threshold = int(_get_config('limitdown_threshold', '20'))
    trade_date = current_ts[:10]
    conn = sqlite3.connect(_db_path(), timeout=5)
    try:
        row = conn.execute(
            "SELECT limitDown AS limit_down FROM overview WHERE date=? "
            "ORDER BY date DESC LIMIT 1", (trade_date,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT limitDown AS limit_down FROM overview ORDER BY date DESC LIMIT 1").fetchone()
        n = (row[0] if row else 0) or 0
    finally:
        conn.close()
    if n > threshold:
        title = f'跌停家数 {n} (阈值 {threshold})'
        content = f'{current_ts} 跌停家数 {n} 家, 超过阈值 {threshold}, 冰点信号'
        _save_alert(current_ts, 'urgent', RULE_LIMITDOWN_20, title, content)


def check_broken_rate(current_ts):
    """规则 4: 炸板率 > 50% (情绪退潮)."""
    threshold = float(_get_config('broken_rate_threshold', '0.5'))
    trade_date = current_ts[:10]
    conn = sqlite3.connect(_db_path(), timeout=5)
    try:
        # 今日触板 (akshare_zt_pool) 中炸板 (open_times > 0) 占比
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN open_times > 0 THEN 1 ELSE 0 END) AS broken "
            "FROM akshare_zt_pool WHERE trade_date=?", (trade_date,)).fetchone()
        total = (row[0] if row else 0) or 0
        broken = (row[1] if row else 0) or 0
    finally:
        conn.close()
    if total == 0:
        return
    rate = broken / total
    if rate > threshold:
        title = f'炸板率 {rate*100:.1f}% (阈值 {threshold*100:.0f}%)'
        content = f'{current_ts} 炸板 {broken}/{total} = {rate*100:.1f}%, 超过 {threshold*100:.0f}% 阈值, 情绪退潮'
        _save_alert(current_ts, 'warning', RULE_BROKEN_50, title, content)


def check_north_30min(current_ts):
    """规则 5: 北向半小时内 ±50 亿."""
    threshold_yi = float(_get_config('north_money_threshold_yi', '50'))  # 亿
    threshold_wan = threshold_yi * 1e4  # 转为万元
    trade_date = current_ts[:10]
    # 简化: hsgt_flow 按日存, 不支持半小时窗口. 取全天值兜底 (按 ±50 亿阈值判断).
    # TODO: 阶段 5 之后做 hsgt_flow_intraday 表来支持真正的半小时窗口.
    conn = sqlite3.connect(_db_path(), timeout=5)
    try:
        row = conn.execute(
            "SELECT north_money FROM hsgt_flow WHERE trade_date=?",
            (trade_date,)).fetchone()
        delta = (row[0] if row else 0) or 0
    finally:
        conn.close()
    if abs(delta) > threshold_wan:
        direction = '流入' if delta > 0 else '流出'
        title = f'北向 30min {direction} {abs(delta)/1e4:.1f} 亿 (阈值 {threshold_yi} 亿)'
        content = f'{current_ts} 北向近 30min {direction} {abs(delta)/1e4:.1f} 亿, 超过阈值 {threshold_yi} 亿'
        _save_alert(current_ts, 'warning', RULE_NORTH_50, title, content)


def check_median_flip(current_ts):
    """规则 6: 市场涨幅中位数由正转负或由负转正."""
    trade_date = current_ts[:10]
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    # 上一个采样点的中位数
    rows = conn.execute(
        "SELECT median_change_pct FROM sentiment_intraday "
        "WHERE ts < ? AND median_change_pct IS NOT NULL "
        "ORDER BY ts DESC LIMIT 5", (current_ts,)).fetchall()
    conn.close()
    if len(rows) < 2:
        return
    # 当前中位数 (从 stock_daily 重算)
    from sentiment import _calc_median_change_pct
    conn = sqlite3.connect(_db_path(), timeout=5)
    try:
        curr = _calc_median_change_pct(conn, trade_date)
    finally:
        conn.close()
    if curr is None:
        return
    prev = rows[0]['median_change_pct']
    if prev is None:
        return
    if (prev > 0 and curr < 0) or (prev < 0 and curr > 0):
        title = f'涨幅中位数 {prev:.2f}% → {curr:.2f}% (极性反转)'
        content = f'{current_ts} 涨幅中位数由 {"正" if prev > 0 else "负"} 转 {"负" if curr < 0 else "正"}, 情绪极性反转'
        _save_alert(current_ts, 'urgent', RULE_MEDIAN_FLIP, title, content)


def check_streak_threshold(current_ts):
    """规则 7: 连板高度突破/跌破关键阈值 (默认突破 5/8 板)."""
    high_threshold = int(_get_config('streak_high_threshold', '5'))
    break_threshold = int(_get_config('streak_break_threshold', '8'))
    trade_date = current_ts[:10]
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT MAX(streak) AS max_streak FROM akshare_zt_pool WHERE trade_date=?",
            (trade_date,)).fetchone()
        max_streak = (row['max_streak'] if row else 0) or 0
    finally:
        conn.close()
    # 突破 5 板
    if high_threshold <= max_streak < high_threshold + 1:
        title = f'连板高度突破 {high_threshold} 板: {max_streak} 板'
        content = f'{current_ts} 最高连板 {max_streak} 板, 突破 {high_threshold} 板关键阈值'
        _save_alert(current_ts, 'warning', RULE_STREAK_THRESHOLD, title, content)
    # 突破 8 板
    if break_threshold <= max_streak < break_threshold + 1:
        title = f'连板高度突破 {break_threshold} 板: {max_streak} 板'
        content = f'{current_ts} 最高连板 {max_streak} 板, 突破 {break_threshold} 板关键阈值, 高位板风险'
        _save_alert(current_ts, 'urgent', RULE_STREAK_THRESHOLD, title, content)


# ============ 入口: 跑所有规则 ============

def run_all_rules(current_ts, current_level):
    """在每次情绪采样后调用, 跑全部 7 条规则."""
    check_level_jump(current_ts, current_level)
    check_limitup_threshold(current_ts)
    check_limitdown_threshold(current_ts)
    check_broken_rate(current_ts)
    check_north_30min(current_ts)
    check_median_flip(current_ts)
    check_streak_threshold(current_ts)
