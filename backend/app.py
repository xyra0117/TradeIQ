"""
Flask + SQLite + TuShare 后端
数据存储在本地 SQLite，定时从 TuShare 拉取数据
"""

import os
import re
import sqlite3
import subprocess
import time
import json
import json as _json  # 历史别名，_save_result / _read_ocr_status / 路由内多处使用
import asyncio
import uuid as _uuid
from collections import defaultdict
from datetime import datetime, timedelta, date
from flask import Flask, jsonify, request, send_from_directory
import requests as _requests

# 涨停简图 OCR 异步任务队列
_ocr_jobs = {}
from flask_cors import CORS
import tushare as ts
import akshare as ak
# 模块加载时清掉所有代理环境变量, 避免 AKShare/requests 走系统代理
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)
# 强制 no_proxy=*, 即便系统配置了代理也绕过
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
del _k

# Monkey-patch requests 默认 Session 的 trust_env, 避免 AKShare/requests 走代理
try:
    import requests as _req_mod
    _orig_session_init = _req_mod.Session.__init__
    def _patched_session_init(self, *a, **kw):
        _orig_session_init(self, *a, **kw)
        self.trust_env = False
        self.proxies = {}
    _req_mod.Session.__init__ = _patched_session_init
except Exception:
    pass

app = Flask(__name__)
CORS(app)

# 配置
DB_PATH = os.path.join(os.path.dirname(__file__), 'market_data.db')
TUSHARE_TOKEN = '1905eff139f559e9caa61c7363ac18e6217a33032e9e434812db1e34'
INTERVALS = [('5日', 5), ('10日', 10), ('20日', 20)]  # 涨幅榜区间 (名称, 天数)

# 主力净流入数据源
AKSHARE_TIMEOUT = 15                                  # 单次 AKShare 调用超时
PUSH2_BASE = 'https://push2.eastmoney.com/api/qt/clist/get'  # 东方财富 push2 全市场接口

# 中信证券 A股 手续费 (用户 2026-06-04 确认: 万1.0 低佣)
FEE_CONFIG = {
    'commission_rate': 0.0001,   # 券商佣金 万1.0 (双边)
    'min_commission': 5.0,       # 最低 5元/笔
    'transfer_rate': 0.00001,    # 过户费 0.001% (仅沪市双边, 深市 2022-06-03 起打包在佣金里)
    'stamp_rate': 0.0005,        # 印花税 0.05% (卖出单边)
}


def _round_half_up(x, n=2):
    """标准四舍五入 (half up) — 跟券商交割单一致. Python 内置 round 是银行家舍入, 6.335 会舍成 6.33.
    用 Decimal 实现: 保留 2 位小数, ROUND_HALF_UP.
    """
    from decimal import Decimal, ROUND_HALF_UP
    if x is None: return 0.0
    return float(Decimal(str(x)).quantize(Decimal('0.' + '0'*n), rounding=ROUND_HALF_UP))


def _is_shanghai(ts_code: str) -> bool:
    """是否沪市 (含北市): 过户费规则按沪市走"""
    if not ts_code: return False
    if ts_code.endswith('.SH'): return True
    if ts_code.endswith('.BJ'): return True
    if ts_code.endswith('.SZ'): return False
    return ts_code.startswith(('60', '68', '90', '11', '13', '8', '43', '92'))


def calc_buy_fees(cost_value, ts_code=''):
    """买入时手续费: 佣金 + 过户费 (仅沪市/北市, 深市过户费打包在佣金里)
    单边, 沉没成本. 跟交割单对齐: 各分项四舍五入 2 位后求和.
    """
    if not cost_value or cost_value <= 0:
        return 0.0
    commission = _round_half_up(max(cost_value * FEE_CONFIG['commission_rate'], FEE_CONFIG['min_commission']))
    transfer = _round_half_up(cost_value * FEE_CONFIG['transfer_rate']) if _is_shanghai(ts_code) else 0.0
    return commission + transfer


def calc_sell_fees(market_value, ts_code=''):
    """卖出时手续费: 佣金 + 过户费 (仅沪市/北市) + 印花税 — 单边, 未来才发生
    跟交割单对齐: 各分项四舍五入 2 位后求和.
    """
    if not market_value or market_value <= 0:
        return 0.0
    commission = _round_half_up(max(market_value * FEE_CONFIG['commission_rate'], FEE_CONFIG['min_commission']))
    transfer = _round_half_up(market_value * FEE_CONFIG['transfer_rate']) if _is_shanghai(ts_code) else 0.0
    stamp = _round_half_up(market_value * FEE_CONFIG['stamp_rate'])
    return commission + transfer + stamp

# 初始化数据库
def _migrate_user_picks_to_id_pk(c):
    """user_picks 老 schema: ts_code PRIMARY KEY (去重); 新 schema: id 自增主键, ts_code 可重复.
    检测到 schema 不匹配时自动重建表 (把已有数据搬运过去, 老主键约束去掉).
    """
    cur = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_picks'")
    row = cur.fetchone()
    if not row:
        return
    schema_sql = row[0]
    # 新 schema 不需要 PRIMARY KEY 在 ts_code 上
    if 'PRIMARY KEY' in schema_sql and 'ts_code' in schema_sql.split('PRIMARY KEY')[1].split(')')[0]:
        print('[init_db] 迁移 user_picks: ts_code 主键 -> id 自增主键 (允许重复入库)')
        c.execute('''CREATE TABLE IF NOT EXISTS user_picks_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_code TEXT NOT NULL,
            added_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            note TEXT
        )''')
        # 搬运数据
        try:
            c.execute('INSERT INTO user_picks_new (ts_code, added_at, note) SELECT ts_code, added_at, note FROM user_picks')
        except Exception as e:
            print(f'[init_db] 搬运失败: {e}')
        c.execute('DROP TABLE user_picks')
        c.execute('ALTER TABLE user_picks_new RENAME TO user_picks')


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 指数日线数据
    c.execute('''CREATE TABLE IF NOT EXISTS index_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        name TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        change REAL,
        volume REAL,
        UNIQUE(date, name)
    )''')

    # 涨幅榜单
    c.execute('''CREATE TABLE IF NOT EXISTS leaderboard (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        type TEXT NOT NULL,
        rank INTEGER,
        code TEXT,
        name TEXT,
        change REAL,
        volume REAL,
        reason TEXT,
        UNIQUE(date, type, rank)
    )''')

    # 盘面概览
    c.execute('''CREATE TABLE IF NOT EXISTS overview (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT UNIQUE NOT NULL,
        totalVolume REAL,
        volumeDiff REAL,
        netFlow REAL,
        upCount INTEGER,
        downCount INTEGER,
        flatCount INTEGER,
        limitUp INTEGER,
        limitDown INTEGER,
        up7 INTEGER, up5_7 INTEGER, up3_5 INTEGER, up0_3 INTEGER,
        down7 INTEGER, down5_7 INTEGER, down3_5 INTEGER, down0_3 INTEGER
    )''')

    # 涨停数据
    c.execute('''CREATE TABLE IF NOT EXISTS limitup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        code TEXT,
        name TEXT,
        marketCap REAL,
        time TEXT,
        sector TEXT,
        volume REAL,
        streak TEXT,
        keyword TEXT,
        UNIQUE(date, code, time)
    )''')

    # 板块历史汇总 (按 ts_code 聚合 limitup.sector, 给 4 个 API 用)
    c.execute('''CREATE TABLE IF NOT EXISTS stock_sector_summary (
        ts_code TEXT PRIMARY KEY,
        sectors_json TEXT NOT NULL,
        total_limitups INTEGER NOT NULL,
        last_updated TEXT NOT NULL
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sector_summary_updated ON stock_sector_summary(last_updated)')

    # 股票日线数据
    c.execute('''CREATE TABLE IF NOT EXISTS stock_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        name TEXT,
        trade_date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        change REAL,
        volume REAL,
        amount REAL,
        UNIQUE(ts_code, trade_date)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_daily_date ON stock_daily(trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_daily_ts_code ON stock_daily(ts_code)')

    # 股票基础信息
    c.execute('''CREATE TABLE IF NOT EXISTS stock_basic (
        ts_code TEXT PRIMARY KEY,
        name TEXT,
        listing_date TEXT,
        market TEXT,
        status TEXT DEFAULT 'normal',
        total_share REAL,
        float_share REAL,
        last_share_sync TEXT
    )''')
    # 兜底: 老库没这两列, 现场加 (CREATE IF NOT EXISTS 不会补列)
    try:
        c.execute('ALTER TABLE stock_basic ADD COLUMN total_share REAL')
    except Exception: pass
    try:
        c.execute('ALTER TABLE stock_basic ADD COLUMN float_share REAL')
    except Exception: pass
    try:
        c.execute('ALTER TABLE stock_basic ADD COLUMN last_share_sync TEXT')
    except Exception: pass
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_basic_listing ON stock_basic(listing_date)')

    # 交易日历缓存
    c.execute('''CREATE TABLE IF NOT EXISTS trading_dates_cache (
        cal_date TEXT PRIMARY KEY,
        exchange TEXT DEFAULT 'SSE'
    )''')

    # 持仓表
    c.execute('''CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        name TEXT,
        shares REAL NOT NULL,
        original_shares REAL,
        cost_price REAL NOT NULL,
        buy_date TEXT,
        note TEXT,
        closed_at TEXT,
        total_sell_amount REAL DEFAULT 0,
        realized_pnl REAL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(ts_code, cost_price, buy_date)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_positions_ts_code ON positions(ts_code)')

    # 兼容老库: 给已存在的 positions 表加新列
    existing_cols = {r[1] for r in c.execute('PRAGMA table_info(positions)').fetchall()}
    for col, decl in [('closed_at', 'TEXT'), ('total_sell_amount', 'REAL DEFAULT 0'),
                      ('realized_pnl', 'REAL DEFAULT 0'), ('original_shares', 'REAL'),
                      ('position_type', 'TEXT'), ('last_trade_date', 'TEXT')]:
        if col not in existing_cols:
            c.execute(f'ALTER TABLE positions ADD COLUMN {col} {decl}')
    # last_trade_date 建索引 (dashboard 排序用)
    c.execute('CREATE INDEX IF NOT EXISTS idx_positions_last_trade ON positions(last_trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_positions_closed ON positions(closed_at)')

    # maintenance: trades.trade_date 统一为 'YYYY-MM-DD' (8 位 → 10 位带横线)
    c.execute("""UPDATE trades SET trade_date =
        substr(trade_date,1,4) || '-' || substr(trade_date,5,2) || '-' || substr(trade_date,7,2)
        WHERE length(trade_date) = 8 AND trade_date NOT LIKE '%-%'""")
    # maintenance: positions 的日期列也统一为 'YYYY-MM-DD'
    c.execute("""UPDATE positions SET
        buy_date = CASE WHEN length(buy_date)=8 AND buy_date NOT LIKE '%-%'
                        THEN substr(buy_date,1,4) || '-' || substr(buy_date,5,2) || '-' || substr(buy_date,7,2) ELSE buy_date END,
        last_trade_date = CASE WHEN length(last_trade_date)=8 AND last_trade_date NOT LIKE '%-%'
                        THEN substr(last_trade_date,1,4) || '-' || substr(last_trade_date,5,2) || '-' || substr(last_trade_date,7,2) ELSE last_trade_date END,
        last_add_date = CASE WHEN last_add_date IS NOT NULL AND length(last_add_date)=8 AND last_add_date NOT LIKE '%-%'
                        THEN substr(last_add_date,1,4) || '-' || substr(last_add_date,5,2) || '-' || substr(last_add_date,7,2) ELSE last_add_date END,
        closed_at = CASE WHEN closed_at IS NOT NULL AND length(closed_at)=8 AND closed_at NOT LIKE '%-%'
                        THEN substr(closed_at,1,4) || '-' || substr(closed_at,5,2) || '-' || substr(closed_at,7,2) ELSE closed_at END""")

    # 成交明细表
    # 用 (trade_no, ts_code, trade_time) 组合去重, 防止源数据 trade_no 重复(如占位 2147483647)
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_no TEXT,
        ts_code TEXT NOT NULL,
        name TEXT,
        direction TEXT NOT NULL,
        price REAL NOT NULL,
        shares REAL NOT NULL,
        amount REAL,
        trade_date TEXT,
        trade_time TEXT,
        note TEXT,
        applied INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(trade_no, ts_code, trade_time)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_ts_code ON trades(ts_code)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_applied ON trades(applied)')

    # ── 对账日志: positions 写入后 self-heal 事件记录 ──
    c.execute('''CREATE TABLE IF NOT EXISTS position_reconcile_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        name TEXT,
        position_id INTEGER,
        field TEXT NOT NULL,
        old_value REAL,
        new_value REAL,
        diff REAL,
        triggered_by_trade_id INTEGER,
        triggered_by_trade_date TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_reconcile_log_created ON position_reconcile_log(id DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_reconcile_log_tscode ON position_reconcile_log(ts_code)')

    # 主力净流入 (fund_flow) - 数据源: akshare / 东方财富 push2
    c.execute('''CREATE TABLE IF NOT EXISTS fund_flow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        close REAL,
        change_pct REAL,
        main_net_inflow REAL NOT NULL,
        main_net_pct REAL,
        super_net REAL, super_pct REAL,
        big_net REAL, big_pct REAL,
        mid_net REAL, mid_pct REAL,
        small_net REAL, small_pct REAL,
        source TEXT DEFAULT 'akshare',
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(trade_date, ts_code)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_fund_flow_date        ON fund_flow(trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_fund_flow_date_inflow ON fund_flow(trade_date, main_net_inflow DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_fund_flow_ts_code     ON fund_flow(ts_code, trade_date DESC)')
    # 兼容早期版本: 同花顺全市场同步曾把万元字段按亿元放大。
    c.execute("""UPDATE fund_flow
        SET main_net_inflow = main_net_inflow / 10000.0,
            main_net_pct = CASE
                WHEN main_net_pct IS NOT NULL AND ABS(main_net_pct) > 1000
                THEN main_net_pct / 10000.0
                ELSE main_net_pct
            END
        WHERE source='akshare-10jqka'
          AND (ABS(main_net_inflow) > 100000000000 OR ABS(COALESCE(main_net_pct, 0)) > 1000)
    """)

    # 选股推荐缓存表 (按 均线+资金流.md 文档重新设计)
    c.execute('''CREATE TABLE IF NOT EXISTS stock_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        name TEXT,
        close REAL,
        change_pct REAL,
        -- 均线 (4 条)
        ma5 REAL, ma10 REAL, ma20 REAL, ma30 REAL,
        ma5_slope REAL, ma10_slope REAL, ma20_slope REAL, ma30_slope REAL,
        -- 主力资金流 (5 个时间维度)
        main_net_today REAL,  -- 当日
        main_net_3d REAL,     -- 3 日累计 (5 日线对应)
        main_net_5d REAL,     -- 5 日累计 (10 日线对应)
        main_net_10d REAL,    -- 10 日累计 (20 日线对应)
        main_net_20d REAL,    -- 20 日累计 (30 日线对应)
        main_net_pct_today REAL,  -- 当日主力净流入占比 %
        -- 5 档资金流 (当日)
        super_net REAL, big_net REAL, mid_net REAL, small_net REAL,
        super_pct REAL, big_pct REAL, mid_pct REAL, small_pct REAL,
        -- 评分
        score REAL,
        signal_type TEXT,        -- 'buy' / 'sell' / 'hold'
        flow_days_available INTEGER,  -- 当前 fund_flow 表可用天数
        reasons_json TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(trade_date, ts_code)
    )''')
    # 兼容旧表: 给已有表加列 (如果不存在)
    _add_col_if_missing(c, 'stock_picks', 'ma20', 'REAL')
    for col in ['ma5_slope', 'ma10_slope', 'ma20_slope', 'ma30_slope']:
        _add_col_if_missing(c, 'stock_picks', col, 'REAL')
    for col in ['main_net_today', 'main_net_3d', 'main_net_5d', 'main_net_10d', 'main_net_20d', 'main_net_pct_today']:
        _add_col_if_missing(c, 'stock_picks', col, 'REAL')
    for col in ['super_net', 'big_net', 'mid_net', 'small_net', 'super_pct', 'big_pct', 'mid_pct', 'small_pct']:
        _add_col_if_missing(c, 'stock_picks', col, 'REAL')
    _add_col_if_missing(c, 'stock_picks', 'signal_type', 'TEXT')
    _add_col_if_missing(c, 'stock_picks', 'flow_days_available', 'INTEGER')
    _add_col_if_missing(c, 'stock_picks', 'reasons_json', 'TEXT')  # 旧表可能叫 reasons, 加这个保险
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_picks_date ON stock_picks(trade_date, score DESC)')

    # 自选股 (单一分组, user 手动维护, 复用 _compute_picks 算法但不入 stock_picks 表)
    c.execute('''CREATE TABLE IF NOT EXISTS user_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        added_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        note TEXT
    )''')
    # 兼容老库: 如果 user_picks 还在用 ts_code 做主键, 迁移到新 schema
    _migrate_user_picks_to_id_pk(c)

    # 自选板块: 表是用户之前手工建好的 (id, name, pinned, created_at, updated_at)
    # 沿用现有 schema, 不重建. 仅补 note 列 (旧表可能没有)
    # CREATE TABLE IF NOT EXISTS 兜底, 防止新库没表
    c.execute('''CREATE TABLE IF NOT EXISTS user_sectors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(name COLLATE NOCASE)
    )''')
    _add_col_if_missing(c, 'user_sectors', 'note', 'TEXT')

    # 自选板块关联的 6 只股票 (核心三杰 + 同领域优质企业)
    # 沿用用户之前手工建的 schema (sector_id + ts_code + added_at, ON DELETE CASCADE)
    # CREATE TABLE IF NOT EXISTS 兜底; 再补 name/role/rank 列 (旧数据为 NULL, 读时回查 stock_basic)
    c.execute('''CREATE TABLE IF NOT EXISTS user_sector_stocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sector_id INTEGER NOT NULL,
        ts_code TEXT NOT NULL,
        added_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(sector_id, ts_code),
        FOREIGN KEY (sector_id) REFERENCES user_sectors(id) ON DELETE CASCADE
    )''')
    _add_col_if_missing(c, 'user_sector_stocks', 'name', 'TEXT')
    _add_col_if_missing(c, 'user_sector_stocks', 'role', 'TEXT')     # 'core' 核心三杰 | 'peer' 同领域优质企业
    _add_col_if_missing(c, 'user_sector_stocks', 'rank', 'INTEGER')  # 1/2/3 (在 role 内的排序)
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_sector_stocks_sector ON user_sector_stocks(sector_id)')

    # ============ 阶段 0: 申万行业 / 涨停 Tushare 兜底 / 北向资金 新表 (情绪分析系统前置) ============

    # 申万行业分类 (L1/L2/L3)
    c.execute('''CREATE TABLE IF NOT EXISTS sw_industry (
        index_code TEXT PRIMARY KEY,    -- e.g. '801010.SI'
        industry_name TEXT NOT NULL,
        level TEXT NOT NULL,             -- 'L1' / 'L2' / 'L3'
        src TEXT DEFAULT 'SW',           -- 'SW' / 'CSI' / 'CITIC' 等
        parent_code TEXT,                -- 上级行业代码
        updated_at TEXT
    )''')

    # 申万行业成分股
    c.execute('''CREATE TABLE IF NOT EXISTS sw_industry_member (
        index_code TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        in_date TEXT,
        out_date TEXT,                   -- 剔除日期 (NULL=在)
        is_new TEXT DEFAULT 'N',
        PRIMARY KEY (index_code, ts_code, in_date)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sw_industry_member_ts ON sw_industry_member(ts_code)')

    # 申万行业日线
    c.execute('''CREATE TABLE IF NOT EXISTS sw_industry_daily (
        index_code TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        close REAL, open REAL, high REAL, low REAL,
        change_pct REAL, vol REAL, amount REAL,
        PRIMARY KEY (index_code, trade_date)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sw_industry_daily_date ON sw_industry_daily(trade_date)')

    # 涨停池 (akshare stock_zt_pool_em 拉, 独立补充非研 OCR, 供情绪指数计算封板率/炸板率/连板梯队; 不替换现有 limitup 表)
    # 注: Tushare limit_list_d 免费版无权限, 改用 akshare. 表名 akshare_zt_pool 反映数据源.
    c.execute('''CREATE TABLE IF NOT EXISTS akshare_zt_pool (
        trade_date TEXT NOT NULL,
        ts_code TEXT NOT NULL,            -- e.g. '301520' (akshare 用 6 位代码, 无后缀)
        name TEXT,
        industry TEXT,                    -- akshare '所属行业' 字段
        close REAL,                       -- 最新价
        pct_chg REAL,                     -- 涨跌幅
        amount REAL,                       -- 成交额
        circulate_mv REAL,                -- 流通市值
        turnover_pct REAL,                -- 换手率
        seal_amount REAL,                 -- 封板资金
        first_seal_time TEXT,             -- 首次封板时间
        last_seal_time TEXT,              -- 最后封板时间
        open_times INTEGER,               -- 炸板次数 (0=没炸过)
        limit_stats TEXT,                  -- 涨停统计 (e.g. '1/1', '2/3')
        streak INTEGER,                   -- 连板数
        PRIMARY KEY (trade_date, ts_code)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_akshare_zt_pool_date ON akshare_zt_pool(trade_date)')

    # ============ 阶段 1: 情绪分析 (sentiment_intraday + sentiment_alert) ============

    # 情绪分时采样 (盘中 15 秒一采样)
    c.execute('''CREATE TABLE IF NOT EXISTS sentiment_intraday (
        ts TEXT PRIMARY KEY,            -- 'YYYY-MM-DD HH:MM:SS' 15 秒一采样
        score REAL NOT NULL,            -- 0-100
        level TEXT NOT NULL,            -- 冰点/低迷/温和/火热/亢奋 (5 档, 严格用文档名)
        base_score REAL,                -- 基础 60%
        capital_score REAL,             -- 资金 25%
        sector_score REAL,              -- 板块 15%
        -- 基础因子 (60%)
        up_count INTEGER, down_count INTEGER, flat_count INTEGER,
        limit_up_count INTEGER, limit_down_count INTEGER,
        median_change_pct REAL,
        sealed_count INTEGER, touched_count INTEGER,
        broken_count INTEGER,
        streak_max INTEGER, streak_count INTEGER,
        -- 资金因子 (25%)
        north_net REAL, total_amount REAL, main_net REAL,
        seal_amount REAL,
        -- 板块因子 (15%)
        up_sector_count INTEGER, total_sector_count INTEGER,
        leading_sector_change_pct REAL, sector_rotation INTEGER
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sentiment_intraday_ts ON sentiment_intraday(ts)')

    # 异动预警
    c.execute('''CREATE TABLE IF NOT EXISTS sentiment_alert (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        level TEXT NOT NULL,            -- 'warning' / 'urgent'
        rule TEXT NOT NULL,             -- 触发的规则名
        title TEXT, content TEXT,
        pushed INTEGER DEFAULT 0        -- 是否已推送 (0/1)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sentiment_alert_ts ON sentiment_alert(ts)')

    # 推送配置 (alert_rules 和 feishu 读阈值用)
    c.execute('''CREATE TABLE IF NOT EXISTS push_config (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )''')
    # 默认配置
    _init_default_push_config(conn)

    # 北向资金 (Tushare moneyflow_hsgt)
    c.execute('''CREATE TABLE IF NOT EXISTS hsgt_flow (
        trade_date TEXT PRIMARY KEY,
        hgt REAL, sgt REAL, north_money REAL, south_money REAL
    )''')

    conn.commit()

    # 启用 FK 约束 (sqlite3 默认关闭, 否则 user_sector_stocks.FOREIGN KEY ... ON DELETE CASCADE 不生效)
    conn.execute('PRAGMA foreign_keys = ON')
    # 清理孤儿 rows (FK 之前没启用, 板块被删后 user_sector_stocks 关联行残留)
    cur = conn.execute('''
        DELETE FROM user_sector_stocks
        WHERE sector_id NOT IN (SELECT id FROM user_sectors)
    ''')
    if cur.rowcount:
        print(f'[init_db] 清理 {cur.rowcount} 条孤儿 user_sector_stocks')
    conn.commit()
    conn.close()

    # 启动时全量重建 sector summary (几百 ms, 1929 只股)
    try:
        n = rebuild_sector_summary()
        print(f'[init_db] stock_sector_summary 重建完成: {n} 只股')
    except Exception as e:
        print(f'[init_db] sector summary 重建失败 (非致命): {e}')


def get_pro():
    """获取 TuShare Pro 接口"""
    if TUSHARE_TOKEN:
        ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


# ============ 股票基础数据 ============

@app.route('/api/stock/basic/sync', methods=['POST'])
def sync_stock_basic():
    """批量同步股票基础信息（上市日期、市场、股本等）
    股本 (total_share / float_share) 从 daily_basic 拿, 用于板块涨幅实时市值加权
    流通股本变化慢, 同步一次可用很久; 建议每月跑一次, 或重启用
    """
    pro = get_pro()
    try:
        # 1) stock_basic: 基础信息 (TuShare 这接口不返回 total_share/float_share)
        df = pro.stock_basic(exchange='', list_status='L',
                             fields='ts_code,name,list_date,market')
        if len(df) == 0:
            return jsonify({'status': 'no_data', 'message': 'TuShare 返回空数据'})

        # 2) daily_basic: 拿最近一个交易日的股本和市值
        # 优先今天, 今天是交易日就今天; 非交易日 fallback 到 leaderboard 最新交易日
        shares_map = {}  # ts_code -> (total_share, float_share)
        try:
            trade_date = datetime.now().strftime('%Y%m%d')
            df_db = pro.daily_basic(trade_date=trade_date,
                                     fields='ts_code,total_share,float_share,total_mv,circ_mv')
            if df_db is None or len(df_db) == 0:
                # 非交易日, 退到 leaderboard 最新日期
                lb_conn = sqlite3.connect(DB_PATH, timeout=10)
                lb_row = lb_conn.execute("SELECT MAX(date) FROM leaderboard").fetchone()
                lb_conn.close()
                if lb_row and lb_row[0]:
                    df_db = pro.daily_basic(trade_date=lb_row[0],
                                             fields='ts_code,total_share,float_share,total_mv,circ_mv')
            if df_db is not None:
                for _, r in df_db.iterrows():
                    shares_map[r['ts_code']] = (r.get('total_share'), r.get('float_share'))
        except Exception as e:
            print(f'[sync_stock_basic] daily_basic 拉取失败 (非致命, 后续会用 limitup.marketCap 兜底): {e}')

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        count = 0
        share_filled = 0
        today = datetime.now().strftime('%Y-%m-%d')
        for _, row in df.iterrows():
            try:
                name = row['name'] or ''
                status = 'normal'
                if '*ST' in name:
                    status = 'star'
                elif 'ST' in name:
                    status = 'st'
                elif '退' in name:
                    status = 'delisting'

                ts_code = row['ts_code']
                ts, fs = shares_map.get(ts_code, (None, None))
                has_share = fs is not None and fs > 0
                c.execute('''INSERT OR REPLACE INTO stock_basic
                    (ts_code, name, listing_date, market, status,
                     total_share, float_share, last_share_sync)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (ts_code, name, row['list_date'],
                     row['market'] or '', status,
                     ts, fs,
                     today if has_share else None))
                count += 1
                if has_share:
                    share_filled += 1
            except Exception as e:
                print(f'stock_basic 写入错误 {row.get("ts_code", "unknown")}: {e}')
                continue
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'count': count, 'shares_filled': share_filled})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/stock/basic', methods=['GET'])
def get_stock_basic():
    """查询股票基础信息"""
    ts_code = request.args.get('ts_code')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if ts_code:
        row = c.execute('SELECT * FROM stock_basic WHERE ts_code=?', (ts_code,)).fetchone()
        conn.close()
        return jsonify(dict(row) if row else {})
    else:
        rows = c.execute('SELECT * FROM stock_basic LIMIT 100').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])


@app.route('/api/stock-basic/<path:code>', methods=['GET'])
def lookup_stock_basic(code):
    """轻量查股票名: 接受 6 位 / sh600519 / 600519.SH 等格式, 返回 {ts_code, name}.
    用于前端失焦回查, 不返回其它字段.
    """
    norm = _normalize_code(code)
    if not norm:
        return jsonify({'error': f'代码格式无效: {code!r}'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT ts_code, name FROM stock_basic WHERE ts_code = ?', (norm,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': f'股票不存在: {norm}'}), 404
    return jsonify({'ts_code': row['ts_code'], 'name': row['name']})


# ============ 指数数据 ============

@app.route('/api/index/daily/latest-date', methods=['GET'])
def get_index_daily_latest_date():
    """获取实际有数据的最新日期"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute('SELECT date FROM index_daily ORDER BY date DESC LIMIT 1').fetchone()
    conn.close()
    return jsonify({'latest_date': row[0] if row else None})

@app.route('/api/index/daily', methods=['GET'])
def get_index_daily():
    """获取指数日线数据"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    name = request.args.get('name', 'all')
    limit = request.args.get('limit', 100, type=int)

    if name == 'all':
        rows = c.execute(
            'SELECT * FROM index_daily ORDER BY date DESC LIMIT ?', (limit,)
        ).fetchall()
    else:
        rows = c.execute(
            'SELECT * FROM index_daily WHERE name=? ORDER BY date DESC LIMIT ?',
            (name, limit)
        ).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/index/sync', methods=['POST'])
def sync_index_data():
    """从 TuShare 同步指数数据"""
    pro = get_pro()
    indices = {
        '上证指数': '000001.SH',
        '深证成指': '399001.SZ',
        '创业板指': '399006.SZ',
        '科创50': '000688.SH'
    }

    results = []

    # 支持指定日期，否则获取最近有数据的交易日
    trade_date = request.args.get('date')
    if not trade_date:
        for i in range(7):
            test_date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
            df_test = pro.index_daily(ts_code='000001.SH', start_date=test_date, end_date=test_date)
            if len(df_test) > 0:
                trade_date = test_date
                break

    if not trade_date:
        return jsonify({'status': 'error', 'message': '无法获取交易日数据'})

    # TuShare index_daily 返回字段: ts_code, trade_date, open, high, low, close, change, pct_chg, vol, amount
    for name, code in indices.items():
        try:
            df = pro.index_daily(
                ts_code=code,
                start_date=trade_date,
                end_date=trade_date
            )

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()

            for _, row in df.iterrows():
                c.execute('''INSERT OR REPLACE INTO index_daily
                    (date, name, open, high, low, close, change, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (row['trade_date'], name, row['open'], row['high'], row['low'],
                     row['close'], row['pct_chg'], row['amount'] / 100000))  # tushare返回千元，转为亿

            conn.commit()
            results.append({'name': name, 'count': len(df)})
            conn.close()

        except Exception as e:
            results.append({'name': name, 'error': str(e)})

    return jsonify({'status': 'success', 'date': trade_date, 'results': results})


# ============ 板块历史汇总 (sector_history) ============


def _attach_sector(rows, key='ts_code'):
    """给 rows 列表每行加 sector_history 字段 (原地修改).
    排序规则: is_latest 排第一, 其余按 count 降序.
    rows 形如 [{ts_code|code: ..., ...}] — 4 个 API 共用."""
    if not rows:
        return rows
    codes = list({r.get(key) for r in rows if r.get(key)})
    if not codes:
        return rows
    placeholders = ','.join('?' * len(codes))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sector_rows = conn.execute(
        f'SELECT ts_code, sectors_json FROM stock_sector_summary WHERE ts_code IN ({placeholders})',
        codes
    ).fetchall()
    conn.close()
    sector_map = {r['ts_code']: json.loads(r['sectors_json']) for r in sector_rows}
    for r in rows:
        code = r.get(key)
        sectors = sector_map.get(code, [])
        # 排序: is_latest=True 排最前, 其余 count 降序
        sectors_sorted = sorted(sectors, key=lambda s: (not s.get('is_latest', False), -s.get('count', 0)))
        r['sector_history'] = sectors_sorted
    return rows


def refresh_sector_summary_for_codes(ts_codes):
    """增量刷新指定 ts_code 列表的 sector 汇总.
    逻辑: GROUP BY (code, sector) → 标记 is_latest (跨 sector 取 MAX(date) 的 sector) → UPSERT.
    对已无 limitup 记录的 ts_code, 删掉 summary 行."""
    if not ts_codes:
        return 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    placeholders = ','.join('?' * len(ts_codes))
    rows = conn.execute(
        f"""SELECT code AS ts_code, sector, MAX(date) AS latest_date, COUNT(*) AS cnt
            FROM limitup
            WHERE code IN ({placeholders}) AND sector IS NOT NULL AND sector != ''
            GROUP BY code, sector""",
        ts_codes
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r['ts_code'], []).append({
            'sector': r['sector'],
            'count': r['cnt'],
            'latest_date': r['latest_date'],
        })
    now_iso = datetime.now().isoformat(timespec='seconds')
    for code, sectors in grouped.items():
        max_date = max(s['latest_date'] for s in sectors)
        for s in sectors:
            s['is_latest'] = (s['latest_date'] == max_date)
        conn.execute(
            'INSERT OR REPLACE INTO stock_sector_summary (ts_code, sectors_json, total_limitups, last_updated) VALUES (?, ?, ?, ?)',
            (code, json.dumps(sectors, ensure_ascii=False), sum(s['count'] for s in sectors), now_iso)
        )
    # 已无涨停记录的股 → 删 summary 行 (避免显示过期数据)
    to_delete = [c for c in ts_codes if c not in grouped]
    if to_delete:
        del_placeholders = ','.join('?' * len(to_delete))
        conn.execute(f'DELETE FROM stock_sector_summary WHERE ts_code IN ({del_placeholders})', to_delete)
    conn.commit()
    conn.close()
    return len(grouped)


def _init_default_push_config(conn):
    """默认推送配置 (alert_rules / feishu 读阈值用). 已存在的不覆盖."""
    defaults = {
        # 开关
        'enabled': 'true',
        # 7 类预警阈值
        'limitup_50_enabled': 'true',
        'limitup_100_enabled': 'true',
        'limitdown_threshold': '20',
        'broken_rate_threshold': '0.5',
        'north_money_threshold_yi': '50',
        'streak_high_threshold': '5',
        'streak_break_threshold': '8',
        # 免打扰
        'do_not_disturb_start': '12:00',
        'do_not_disturb_end': '13:00',
        # 关注题材
        'watched_sectors': '[]',
        # 飞书 Webhook
        'feishu_webhook_url': '',
        'feishu_webhook_secret': '',
    }
    now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for k, v in defaults.items():
        existing = conn.execute("SELECT 1 FROM push_config WHERE key=?", (k,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO push_config (key, value, updated_at) VALUES (?, ?, ?)",
                (k, v, now_ts))
    conn.commit()


def rebuild_sector_summary():
    """全量重建 stock_sector_summary (供应用启动时调用)."""
    conn = sqlite3.connect(DB_PATH)
    codes = [r[0] for r in conn.execute(
        'SELECT DISTINCT code FROM limitup WHERE code IS NOT NULL'
    ).fetchall()]
    conn.close()
    return refresh_sector_summary_for_codes(codes)


# ============ 涨幅榜单 ============


def _attach_close(rows):
    """给 leaderboard rows 补 close (现价) 字段 — 从 stock_daily 最新一天取.
    rows 形如 [{date, type, rank, code, name, change, volume, reason, ...}]
    """
    if not rows:
        return jsonify([])
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # 取 rows 中所有 code, JOIN stock_daily 取最新一天的 close
    codes = list({r.get('code') for r in rows if r.get('code')})
    if not codes:
        conn.close()
        _attach_sector(rows, key='code')  # 顺便补 sector_history (空 data 时也补, 给前端统一字段)
        return jsonify(rows)
    placeholders = ','.join('?' for _ in codes)
    # 每只股票取 trade_date 最大的那行
    c.execute(
        f"""SELECT ts_code, close, trade_date FROM stock_daily
            WHERE ts_code IN ({placeholders})
              AND trade_date = (SELECT MAX(trade_date) FROM stock_daily
                                WHERE ts_code = stock_daily.ts_code)""",
        codes)
    close_map = {r['ts_code']: (r['close'], r['trade_date']) for r in c.fetchall()}
    conn.close()
    for r in rows:
        code = r.get('code')
        if code in close_map:
            r['close'], r['close_date'] = close_map[code]
        else:
            r['close'] = None
            r['close_date'] = None
    _attach_sector(rows, key='code')
    return jsonify(rows)


@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    """获取涨幅榜单

    参数:
        type: 5日|10日|20日|all (默认 all)
        date: YYYYMMDD 格式（可选，默认最新有数据交易日）
        limit: 返回条数（默认 10）
    """
    interval_map = dict(INTERVALS)
    lb_type = request.args.get('type', 'all')
    limit = request.args.get('limit', 10, type=int)
    date = request.args.get('date')

    # type == 'all' 时返回数据库中已有历史数据
    if lb_type == 'all':
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        query = 'SELECT * FROM leaderboard'
        params = []
        if date:
            query += ' WHERE date=?'
            params.append(date)
        query += ' ORDER BY date DESC, type, rank'
        rows = c.execute(query, params).fetchall()
        conn.close()
        return _attach_close([dict(r) for r in rows])

    if lb_type not in interval_map:
        return jsonify({'status': 'error', 'message': f'无效的 type: {lb_type}'}), 400

    days = interval_map[lb_type]

    # 先查 leaderboard 表是否有该日期的数据，有则直接返回
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        'SELECT * FROM leaderboard WHERE date=? AND type=? ORDER BY rank LIMIT ?',
        (date, lb_type, limit)
    ).fetchall()
    conn.close()
    if rows:
        return _attach_close([dict(r) for r in rows])

    # 表里没有数据，返回 need_sync 让前端触发同步
    return jsonify({'need_sync': True, 'date': date, 'type': lb_type})


@app.route('/api/leaderboard/latest-date', methods=['GET'])
def get_leaderboard_latest_date():
    """返回 leaderboard 表中最新有数据的日期"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute('SELECT MAX(date) FROM leaderboard').fetchone()
    conn.close()
    return jsonify({'latest_date': row[0] if row and row[0] else ''})


@app.route('/api/leaderboard/top1', methods=['GET'])
def get_leaderboard_top1():
    """返回历史 TOP1 数据（每种类型的 rank=1 记录），用于走势图

    参数:
        date: YYYYMMDD 格式（可选，默认最新日期）
        limit: 每种类型返回条数（默认 30）
    """
    limit = request.args.get('limit', 30, type=int)
    date = request.args.get('date')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if date:
        rows = c.execute('''
            SELECT date, type, code, name, change FROM leaderboard
            WHERE rank=1 AND date <= ? ORDER BY date DESC LIMIT ?
        ''', (date, limit * 3)).fetchall()
    else:
        rows = c.execute('''
            SELECT date, type, code, name, change FROM leaderboard
            WHERE rank=1 ORDER BY date DESC LIMIT ?
        ''', (limit * 3,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/leaderboard/status', methods=['GET'])
def get_leaderboard_status():
    """查询指定日期的 leaderboard 数据状态"""
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': '缺少 date 参数'}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        'SELECT DISTINCT type FROM leaderboard WHERE date=? ORDER BY type',
        (date,)
    ).fetchall()
    conn.close()

    intervals = [r[0] for r in rows]
    return jsonify({
        'date': date,
        'has_data': len(intervals) > 0,
        'intervals': intervals
    })


@app.route('/api/leaderboard/sync', methods=['POST'])
def sync_leaderboard():
    """计算并保存涨幅榜单（5日/10日/20日）"""
    date = request.args.get('date')
    pro = get_pro()

    # 找最新有数据的交易日
    if not date:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        row = c.execute(
            'SELECT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 1'
        ).fetchone()
        conn.close()
        date = row[0] if row else None

    if not date:
        return jsonify({'status': 'error', 'message': '无交易日期数据'})

    # 检查 stock_daily 是否有足够历史数据覆盖最大区间（20日）
    # 数据不够的话主动从 TuShare 往前补
    max_interval = max(d for _, d in INTERVALS)  # 20
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    existing_dates = [r[0] for r in c.execute(
        'SELECT trade_date FROM stock_daily ORDER BY trade_date DESC'
    ).fetchall()]
    conn.close()

    if date in existing_dates:
        date_idx = existing_dates.index(date)
        # existing_dates 是降序排列（最新在前），date_idx 是 date 在其中的位置
        # 从 date 当天往回数，有 len(existing_dates) - date_idx 个交易日的数据
        available = len(existing_dates) - date_idx
        if available < max_interval + 1:
            need = max_interval + 1 - available
            # 从 date 之前按时间顺序补 (date 往前 need 天)
            sorted_dates = sorted(existing_dates)
            if date in sorted_dates:
                date_pos = sorted_dates.index(date)
                # 需要再往前 need 条交易日
                start_pos = max(0, date_pos - need)
                missing_dates = sorted_dates[start_pos:date_pos]
                for d in missing_dates:
                    sync_stock_daily(d)
                # 补完后再取最新的日期列表
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                existing_dates = [r[0] for r in c.execute(
                    'SELECT trade_date FROM stock_daily ORDER BY trade_date DESC'
                ).fetchall()]
                conn.close()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    results = []

    for lb_type, days in INTERVALS:
        stocks = get_top_stocks(days, date, 10)
        for i, s in enumerate(stocks):
            c.execute('''INSERT OR REPLACE INTO leaderboard
                (date, type, rank, code, name, change, volume, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (date, lb_type, i+1, s['code'], s['name'],
                 s['change'], s.get('amount', 0), s.get('reason', '')))
        results.append({'type': lb_type, 'count': len(stocks)})

    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'date': date, 'results': results})


# ============ 股票日线数据 ============

def sync_stock_daily(trade_date):
    """从 TuShare 同步单日所有股票数据"""
    pro = get_pro()
    try:
        df = pro.daily(trade_date=trade_date)
        if len(df) == 0:
            return 0

        # 批量获取股票名称
        codes = df['ts_code'].unique().tolist()
        name_map = {}
        batch_size = 100
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i+batch_size]
            try:
                df_basic = pro.stock_basic(ts_code=','.join(batch))
                for _, r in df_basic.iterrows():
                    name_map[r['ts_code']] = r['name']
            except Exception as e:
                # 批量失败，逐个获取
                for code in batch:
                    try:
                        basic = pro.stock_basic(ts_code=code)
                        if len(basic) > 0:
                            name_map[code] = basic.iloc[0]['name']
                    except:
                        name_map[code] = ''

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        count = 0
        for _, row in df.iterrows():
            try:
                name = name_map.get(row['ts_code'], '')
                c.execute('''INSERT OR IGNORE INTO stock_daily
                    (ts_code, name, trade_date, open, high, low, close, change, volume, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (row['ts_code'], name, trade_date,
                     row['open'], row['high'], row['low'], row['close'],
                     row['pct_chg'], row['vol'], row['amount']))
                count += 1
            except Exception as e:
                print(f'股票 {row["ts_code"]} 写入错误: {e}')
                continue
        conn.commit()
        conn.close()
        return count
    except Exception as e:
        print(f'sync_stock_daily error: {e}')
        return -1


def sync_stock_daily_batch(start_date, end_date, dry_run=False):
    """批量同步 start_date ~ end_date 范围内的缺失交易日线数据

    - 从 trade_cal 获取交易日历
    - 找出本地数据库缺失的日期
    - 逐日调用 sync_stock_daily 补数据
    - 速率限制: 每200天休息3秒
    """
    pro = get_pro()
    df_cal = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date)
    trading_dates = sorted(df_cal[df_cal['is_open'] == 1]['cal_date'].tolist())

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    existing = set(r[0] for r in c.execute('SELECT DISTINCT trade_date FROM stock_daily').fetchall())
    conn.close()

    to_sync = [d for d in trading_dates if d not in existing]
    if dry_run:
        return {'total': len(trading_dates), 'existing': len(existing), 'missing': len(to_sync), 'dates': to_sync}

    results = {'success': 0, 'failed': 0, 'errors': []}
    for i, date in enumerate(to_sync):
        ret = sync_stock_daily(date)
        if ret > 0:
            results['success'] += 1
        else:
            results['failed'] += 1
            if ret < 0:
                results['errors'].append(date)
        if (i + 1) % 200 == 0:
            time.sleep(3)
        else:
            time.sleep(0.1)
    return results


@app.route('/api/stock/sync/batch', methods=['POST'])
def sync_stock_daily_batch_api():
    """批量同步历史日线数据

    参数:
        start_date: 起始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)
        dry_run: 1 表示只返回缺失日期列表，不实际同步
    """
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    dry_run = request.args.get('dry_run', '0') == '1'

    if not start_date or not end_date:
        return jsonify({'status': 'error', 'message': '需要 start_date 和 end_date 参数'}), 400

    result = sync_stock_daily_batch(start_date, end_date, dry_run=dry_run)
    if dry_run:
        return jsonify({'status': 'ok', **result})
    return jsonify({'status': 'ok', **result})


@app.route('/api/stock/dates-known', methods=['GET'])
def get_known_stock_dates():
    """获取本地数据库已有股票日线数据的日期列表"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute(
        'SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC'
    ).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])


@app.route('/api/stock/sync', methods=['POST'])
def sync_stock_daily_api():
    """同步股票日线数据"""
    date = request.args.get('date')
    if not date:
        for i in range(7):
            test_date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
            try:
                df = get_pro().daily(trade_date=test_date)
                if len(df) > 0:
                    date = test_date
                    break
            except:
                continue

    if not date:
        return jsonify({'status': 'error', 'message': '无法获取有效日期'})

    count = sync_stock_daily(date)
    if count < 0:
        return jsonify({'status': 'error', 'message': '同步失败'})
    return jsonify({'status': 'success', 'date': date, 'count': count})


# ============ 股票过滤与榜单计算 ============

def load_stock_basic_map(conn):
    """加载股票基础信息到内存字典"""
    c = conn.cursor()
    rows = c.execute('SELECT ts_code, name, listing_date, market, status FROM stock_basic').fetchall()
    return {row['ts_code']: dict(row) for row in rows}


def is_new_stock(ts_code, end_date, stock_basic_map, conn, threshold=20):
    """上市未满 threshold 个交易日返回 True"""
    sb = stock_basic_map.get(ts_code)
    if not sb or not sb.get('listing_date'):
        return True
    listing_date = sb['listing_date']
    if listing_date > end_date:
        return True
    count = conn.execute(
        '''SELECT COUNT(DISTINCT trade_date) FROM stock_daily
           WHERE ts_code=? AND trade_date > ? AND trade_date <= ?''',
        (ts_code, listing_date, end_date)
    ).fetchone()[0]
    return count < threshold


def is_st_or_delisting(name):
    """名称中含 ST/*ST/退市相关字样返回 True"""
    if not name:
        return True
    name_upper = name.upper()
    return 'ST' in name_upper or '*ST' in name_upper or '退' in name


def is_suspended(ts_code, start_date, end_date, conn):
    """区间内无任何交易数据返回 True"""
    count = conn.execute(
        'SELECT COUNT(*) FROM stock_daily WHERE ts_code=? AND trade_date BETWEEN ? AND ?',
        (ts_code, start_date, end_date)
    ).fetchone()[0]
    return count == 0


def should_exclude(ts_code, name, start_date, end_date, stock_basic_map, conn):
    """判断股票是否应被排除"""
    if is_new_stock(ts_code, end_date, stock_basic_map, conn):
        return True
    if is_st_or_delisting(name):
        return True
    if is_suspended(ts_code, start_date, end_date, conn):
        return True
    return False


def get_top_stocks(interval_days, end_date, limit=10):
    """获取区间涨幅前N名股票（含过滤逻辑）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 加载股票基础信息
    stock_basic_map = load_stock_basic_map(conn)

    # 获取 end_date 前 interval_days 个交易日
    dates = c.execute(
        'SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC'
    ).fetchall()
    dates = [d['trade_date'] for d in dates]

    if end_date not in dates:
        conn.close()
        return []
    end_idx = dates.index(end_date)
    if len(dates) - end_idx < interval_days + 1:
        conn.close()
        return []
    start_date = dates[end_idx + interval_days]

    # 获取 end_date 有数据的股票
    stocks = c.execute(
        'SELECT DISTINCT ts_code FROM stock_daily WHERE trade_date=?', (end_date,)
    ).fetchall()

    results = []
    for (ts_code,) in stocks:
        # 获取股票名称：优先 stock_daily， fallback 到 stock_basic
        name_row = c.execute(
            'SELECT name FROM stock_daily WHERE ts_code=? AND trade_date=?',
            (ts_code, end_date)
        ).fetchone()
        name = name_row['name'] if name_row else ''
        if not name:
            basic_row = c.execute('SELECT name FROM stock_basic WHERE ts_code=?', (ts_code,)).fetchone()
            name = basic_row['name'] if basic_row else ''
        if not name:
            continue

        # 过滤
        if should_exclude(ts_code, name, start_date, end_date, stock_basic_map, conn):
            continue

        # 过滤
        if should_exclude(ts_code, name, start_date, end_date, stock_basic_map, conn):
            continue

        # 获取起止价格
        start_row = c.execute(
            'SELECT close FROM stock_daily WHERE ts_code=? AND trade_date=?',
            (ts_code, start_date)
        ).fetchone()
        end_row = c.execute(
            'SELECT close FROM stock_daily WHERE ts_code=? AND trade_date=?',
            (ts_code, end_date)
        ).fetchone()
        if not (start_row and end_row and start_row['close'] > 0):
            continue

        # 计算区间涨跌幅
        change = (end_row['close'] / start_row['close'] - 1) * 100

        # 计算区间总成交额（千元转亿）
        amount_row = c.execute(
            '''SELECT SUM(amount) FROM stock_daily
               WHERE ts_code=? AND trade_date BETWEEN ? AND ?''',
            (ts_code, start_date, end_date)
        ).fetchone()
        amount = (amount_row[0] or 0) / 100000

        results.append({
            'code': ts_code,
            'name': name,
            'change': round(change, 2),
            'close': end_row['close'],
            'amount': round(amount, 2)
        })

    conn.close()

    # 排序：涨幅降序，成交额降序
    results.sort(key=lambda x: (x['change'], x['amount']), reverse=True)
    return results[:limit]


# ============ 盘面概览 ============

@app.route('/api/overview', methods=['GET'])
def get_overview():
    """获取盘面概览"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    limit = request.args.get('limit', 100, type=int)
    rows = c.execute(
        'SELECT * FROM overview WHERE upCount > 0 ORDER BY date DESC LIMIT ?', (limit,)
    ).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/overview/sync', methods=['POST'])
def sync_overview():
    """从 TuShare 同步盘面数据（同时同步指数数据）"""
    pro = get_pro()
    indices = {
        '上证指数': '000001.SH',
        '深证成指': '399001.SZ',
        '创业板指': '399006.SZ',
        '科创50': '000688.SH'
    }

    try:
        # 支持指定日期，否则找最近有数据的交易日
        trade_date = request.args.get('date')
        if not trade_date:
            for i in range(7):
                test_date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
                df_test = pro.daily(trade_date=test_date)
                if len(df_test) > 0:
                    trade_date = test_date
                break

        if not trade_date:
            return jsonify({'status': 'no_data', 'message': '找不到有效交易日'})

        # 同步指数数据
        for name, code in indices.items():
            try:
                df_idx = pro.index_daily(ts_code=code, start_date=trade_date, end_date=trade_date)
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                for _, row in df_idx.iterrows():
                    c.execute('''INSERT OR REPLACE INTO index_daily
                        (date, name, open, high, low, close, change, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (row['trade_date'], name, row['open'], row['high'], row['low'],
                         row['close'], row['pct_chg'], row['amount'] / 100000))  # tushare返回千元，转为亿
                conn.commit()
                conn.close()
            except Exception as e:
                print(f'指数同步错误 {name}: {e}')

        # 同步盘面数据
        df = pro.daily(trade_date=trade_date)
        if len(df) == 0:
            return jsonify({'status': 'no_data', 'message': f'{trade_date} 非交易日或无数据'})

        up_count = len(df[df['pct_chg'] > 0])
        down_count = len(df[df['pct_chg'] < 0])
        flat_count = len(df[df['pct_chg'] == 0])
        limit_up = len(df[df['pct_chg'] >= 9.9])
        limit_down = len(df[df['pct_chg'] <= -9.9])
        up7 = len(df[df['pct_chg'] > 7])
        up5_7 = len(df[(df['pct_chg'] > 5) & (df['pct_chg'] <= 7)])
        up3_5 = len(df[(df['pct_chg'] > 3) & (df['pct_chg'] <= 5)])
        up0_3 = len(df[(df['pct_chg'] > 0) & (df['pct_chg'] <= 3)])
        down0_3 = len(df[(df['pct_chg'] >= -3) & (df['pct_chg'] < 0)])
        down3_5 = len(df[(df['pct_chg'] >= -5) & (df['pct_chg'] < -3)])
        down5_7 = len(df[(df['pct_chg'] >= -7) & (df['pct_chg'] < -5)])
        down7 = len(df[df['pct_chg'] < -7])

        # limitup 表的写入已移除，只通过韭研 OCR 通道填充

        # 全市场总成交额 = sum(daily.amount) / 100000 (千元转亿)
        total_volume = df['amount'].sum() / 100000

        # 获取前一个交易日，计算较前日差额
        prev_date = None
        for i in range(1, 30):
            test_date = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=i)).strftime('%Y%m%d')
            df_prev = pro.daily(trade_date=test_date)
            if len(df_prev) > 0:
                prev_date = test_date
                break

        volume_diff = 0
        if prev_date:
            df_prev = pro.daily(trade_date=prev_date)
            prev_volume = df_prev['amount'].sum() / 100000 if len(df_prev) > 0 else 0
            volume_diff = total_volume - prev_volume

        # 获取全市场资金净流入 (moneyflow.net_mf_amount 总和，单位元，转为亿)
        net_flow = 0
        for i in range(7):
            test_date = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=i)).strftime('%Y%m%d')
            try:
                df_mf = pro.moneyflow(trade_date=test_date)
                if len(df_mf) > 0:
                    net_flow = df_mf['net_mf_amount'].sum() / 10000  # 元转亿
                    break
            except Exception as e:
                print(f'资金净流入获取错误: {e}')
                continue

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO overview
            (date, totalVolume, volumeDiff, netFlow, upCount, downCount,
             flatCount, limitUp, limitDown, up7, up5_7, up3_5, up0_3,
             down7, down5_7, down3_5, down0_3)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (trade_date, total_volume, volume_diff, net_flow, up_count, down_count,
             flat_count, limit_up, limit_down, up7, up5_7, up3_5, up0_3,
             down7, down5_7, down3_5, down0_3))
        conn.commit()
        conn.close()

        return jsonify({'status': 'success', 'date': trade_date, 'up': up_count, 'down': down_count, 'volumeDiff': volume_diff, 'netFlow': net_flow})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


# ============ 涨停数据 ============

@app.route('/api/limitup/latest-date', methods=['GET'])
def get_limitup_latest_date():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT MAX(date) FROM limitup')
    row = c.fetchone()
    conn.close()
    return jsonify({'latest_date': row[0] or ''})


@app.route('/api/limitup', methods=['GET'])
def get_limitup():
    """获取涨停数据"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    date = request.args.get('date')
    sector = request.args.get('sector', 'all')
    limit = request.args.get('limit', 5000, type=int)
    if limit > 50000:
        limit = 50000

    # LEFT JOIN stock_daily 取当日涨跌幅.
    # limitup.code 是 6 位数字 (无后缀), stock_daily.ts_code 是 000012.SZ (带后缀),
    # 用 CASE 按首位判断市场拼接后缀匹配. 6/5/9 → sh, 0/2/3 → sz, 4/8 → bj.
    query = '''SELECT l.*, sd.change AS change_pct
               FROM limitup l
               LEFT JOIN stock_daily sd ON sd.ts_code = (
                   l.code || CASE
                       WHEN l.code LIKE '6%' OR l.code LIKE '5%' OR l.code LIKE '9%' THEN '.SH'
                       WHEN l.code LIKE '4%' OR l.code LIKE '8%' THEN '.BJ'
                       ELSE '.SZ'
                   END
               ) AND sd.trade_date = l.date
               WHERE 1=1'''
    params = []

    if date:
        query += ' AND l.date=?'
        params.append(date)
    if sector != 'all':
        query += ' AND l.sector=?'
        params.append(sector)

    query += ' ORDER BY l.date DESC LIMIT ?'
    params.append(limit)

    rows = c.execute(query, params).fetchall()
    conn.close()
    items = [dict(r) for r in rows]

    # with_quote=1 时, 调 sina 实时行情覆盖 change_pct (盘中最新价)
    # 仅在数据量 <= 200 时启用, 避免 sina 单次请求过大
    if request.args.get('with_quote') == '1' and 0 < len(items) <= 200:
        codes = list({r.get('code') for r in items if r.get('code')})
        if codes:
            try:
                quotes = fetch_sina_quotes(codes)
                for item in items:
                    q = quotes.get(item.get('code'))
                    if q:
                        item['change_pct'] = q['change_pct']  # 用盘中实时价覆盖
                        item['quote_time'] = q.get('time', '')
            except Exception as e:
                print(f'[limitup] sina 实时拉取失败 (降级 stock_daily): {e}')

    return jsonify(items)


@app.route('/api/limitup', methods=['DELETE'])
def delete_limitup():
    """删除涨停数据"""
    date = request.args.get('date')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 删前先抓受影响的 ts_code, 删完用于刷新 sector summary
    if date:
        affected = [r[0] for r in c.execute(
            'SELECT DISTINCT code FROM limitup WHERE date = ? AND code IS NOT NULL', (date,)
        ).fetchall()]
        c.execute('DELETE FROM limitup WHERE date=?', (date,))
    else:
        affected = [r[0] for r in c.execute(
            'SELECT DISTINCT code FROM limitup WHERE code IS NOT NULL'
        ).fetchall()]
        c.execute('DELETE FROM limitup')
    conn.commit()
    cnt = c.rowcount
    conn.close()
    # 刷新 sector summary (可能清掉已无涨停记录的股的 summary)
    if affected:
        try:
            refresh_sector_summary_for_codes(affected)
        except Exception as _e:
            print(f'[delete_limitup] sector summary 刷新失败 (非致命): {_e}')
    return jsonify({'status': 'ok', 'deleted': cnt})


def run_ocr_job(filepath, job_id, trade_date):
    """模块级 OCR 任务：跑 mmx → 解析 → 写库 → 保留原图。供 /parse-image 和 /fetch 复用。
    每个 job 单独写到 uploads/_ocr_<job_id>.json, 父进程 poll 读它, 绕开线程 pipe 死锁.
    """
    import re, json as _json
    import threading as _th
    job_status_path = os.path.join(os.path.dirname(__file__), 'uploads', f'_ocr_{job_id}.json')

    def _save(stage, **extra):
        try:
            payload = {'status': 'processing', 'stage': stage}
            payload.update(extra)
            with open(job_status_path, 'w', encoding='utf-8') as _f:
                _json.dump(payload, _f, ensure_ascii=False)
        except Exception:
            pass
    # 切图策略：原图 2318×8602(3.9MB) 整张送 mmx 会触发上游 vision API "system error (HTTP 200)"，
    # 之前的缩图方案(JPEG 1200 宽)能识别 4 字段但 7 字段输出太长会再次触发。
    # 改成保留原分辨率，按高度切成多块(每块 ~1900px, 250px overlap)，每块独立调 mmx，最后合并去重。
    # 字清晰 → 7 字段都能识别；单块 <1MB → 不会撞 mmx 服务端限制；overlap 防止板块/股票被切到丢失。
    # 7 字段 prompt：原图阶段和切图阶段都共用。
    # 原图可能触发上游 token 上限（实测 system error 或 hang），所以用 5min × 2 次重试 + 切图兜底
    prompt = (
        '识别图中所有涨停股票。严格按 JSON 数组输出，不加任何解释、不带 markdown 代码块标记：\n'
        '[{"sector":"板块名","stocks":[{"code":"代码","name":"名称","days":"连板数","time":"封板时间","market_cap":"流通市值","turnover":"成交额","keywords":"关键词"}]}]\n'
        '要求：保留所有板块和股票不遗漏；板块名去掉 *N 后缀；缺失字段填空串；'
        '若某组股票顶部看不到板块标题（可能被裁剪），sector 留空字符串 ""，不要猜测；'
        '输出从 [ 开始 ] 结束。'
    )

    def _parse_mmx_content(stdout_text):
        """从 mmx stdout 解析出 [{sector, stocks:[...]}, ...] 数组，失败返回 ([], 错误描述)"""
        try:
            outer = _json.loads(stdout_text.strip())
            inner = outer.get('content', '')
            inner = re.sub(r'^```json\s*', '', inner).strip()
            inner = re.sub(r'```\s*$', '', inner).strip()
            if inner.startswith('```'):
                inner = re.sub(r'^```[a-z]*\s*', '', inner).strip()
                inner = re.sub(r'```\s*$', '', inner).strip()
            if inner.startswith('['):
                return _json.loads(inner), ''
            if inner.startswith('"'):
                decoded = _json.loads(inner)
                if isinstance(decoded, str) and decoded.startswith('['):
                    return _json.loads(decoded), ''
            # 兜底：正则匹配第一个 JSON 数组
            m = re.search(r'\[\s*\{.*\}\s*\]', stdout_text.strip(), re.DOTALL)
            if m:
                return _json.loads(m.group()), ''
        except Exception as _pe:
            return [], f'{type(_pe).__name__}: {_pe}'
        return [], 'no array found'

    def _flatten_short_data(data):
        """把 4 字段 prompt 输出 [{sector, stocks:[{code,name,days}]}] 扁平化成
        [{sector, code, name, days}, ...]，sector 沿用原值，sector="" 仍为 ""(后续会被规范化)。
        """
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            sec = (item.get('sector') or item.get('sector_name') or '').strip()
            sec = re.sub(r'\*\d+$', '', sec)
            sec_final = sec if sec else '其他'
            for stock in item.get('stocks', []) or []:
                if not isinstance(stock, dict):
                    continue
                s = dict(stock)
                s['sector'] = sec_final
                out.append(s)
        return out

    def _apply_fields3(stocks, fields3_data):
        """按 code 把 3 字段 (time/market_cap/turnover/keywords) 填进 stocks。
        fields3_data 格式: [{code, time, market_cap, turnover, keywords}, ...]
        不在 fields3_data 里的股票保持原值(空字符串)。"""
        if not fields3_data:
            return stocks
        by_code = {}
        for item in fields3_data:
            if not isinstance(item, dict):
                continue
            code = (item.get('code') or '').strip()
            if code:
                by_code[code] = item
        for s in stocks:
            code = (s.get('code') or '').strip()
            if not code or code not in by_code:
                continue
            f3 = by_code[code]
            for k in ('time', 'market_cap', 'turnover', 'keywords'):
                v = (f3.get(k) or '').strip()
                if v:
                    s[k] = v
        return stocks

    def _try_ocr_image(sp, prompt, max_attempts=2, timeout=240, attempt_label=''):
        """单图调 mmx 最多 max_attempts 次（瞬时错误重试）。返回 (data, err, last_stdout, last_stderr, attempts_used)。
        attempt_label 用来给落盘的 stdout 文件名加前缀，避免重名覆盖。
        timeout 默认 240s（切片用）；原图阶段可传 60s，因为原图如果 hang 住多半会一直 hang。"""
        data, err = [], ''
        last_out, last_err = '', ''
        attempts_used = 0
        for attempt in range(max_attempts):
            try:
                r = subprocess.run(
                    ['/usr/local/bin/mmx', 'vision', 'describe', '--image', sp,
                     '--output', 'json', '--prompt', prompt],
                    capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                err = f'timeout {timeout}s ({attempt_label}attempt {attempt})'
                time.sleep(2)
                continue
            except Exception as _e:
                err = f'{type(_e).__name__}: {_e} ({attempt_label}attempt {attempt})'
                time.sleep(2)
                continue
            try:
                debug_path = sp + f'.{attempt_label}attempt{attempt}.txt'
                with open(debug_path, 'w', encoding='utf-8') as _df:
                    _df.write(f'rc={r.returncode}\nstderr:\n{r.stderr}\nstdout:\n{r.stdout}\n')
            except Exception:
                pass
            last_out, last_err = r.stdout, r.stderr
            if r.returncode == 0 and r.stdout.strip():
                data, err = _parse_mmx_content(r.stdout)
                attempts_used = attempt + 1
                if data:
                    return data, '', last_out, last_err, attempts_used
            err = (r.stderr or 'empty stdout')[:200]
            attempts_used = attempt + 1
            time.sleep(2)
        return [], err, last_out, last_err, attempts_used

    def _slice_image_into(filepath):
        """按高度切成长图切片, 返回 (slice_paths, meta_dict). 图小时只切 1 块.
        切块规则 (user 2026-06-22 拍板):
          H ≤ 6000           → 1 块 (整张直送)
          6000 <  H <  7000  → 2 块
          7000 ≤ H <  9000  → 3 块
          9000 ≤ H < 12000  → 4 块
          12000 ≤ H < 16000 → 5 块
          H ≥ 16000         → 5 块 cap (防止长图越切越多)
        """
        from PIL import Image as _Image
        img = _Image.open(filepath)
        W, H = img.size
        OVERLAP = 400  # 大 overlap 让切口附近股票尽量同时出现在两片, 配合跨切片传播双保险
        if H <= 6000:
            N = 1
        elif H < 7000:
            N = 2
        elif H < 9000:
            N = 3
        elif H < 12000:
            N = 4
        else:  # 12000 ≤ H < 16000 或 H ≥ 16000
            N = 5
        if N == 1:
            slice_h = H
        else:
            slice_h = (H + (N - 1) * OVERLAP) // N
        slice_paths = []
        for i in range(N):
            y0 = max(0, i * (slice_h - OVERLAP)) if N > 1 else 0
            y1 = min(H, y0 + slice_h)
            if i == N - 1:
                y1 = H
            if N == 1:
                sp = filepath.rsplit('.', 1)[0] + '_ocr.jpg'
                img.convert('RGB').save(sp, 'JPEG', quality=80)
            else:
                sp = filepath.rsplit('.', 1)[0] + f'_slice{i}.jpg'
                img.crop((0, y0, W, y1)).convert('RGB').save(sp, 'JPEG', quality=75)
            slice_paths.append(sp)
        return slice_paths, {'slice_count': N, 'slice_height': slice_h, 'image_size': f'{W}x{H}'}

    def _merge_parsed_results(results):
        """results: [(idx, [{sector, stocks:[...]}, ...]), ...] 按 idx 升序。
        做两件事：
        1. 跨切片 sector="" 传播：本切片首项 sector="" 用上一切片最后具体板块填充
        2. 按 code 去重，merge 字段（同 code 取信息更全的版本）
        返回 (parsed_data_list, errors_list, last_stdout, last_stderr)
        """
        all_parsed = []
        last_out, last_err = '', ''
        errors = []
        for idx, data in results:
            for sec_idx, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                raw_sector = (item.get('sector') or item.get('sector_name') or '').strip()
                raw_sector = re.sub(r'\*\d+$', '', raw_sector)
                # 跨切片传播：本切片首项 sector="" 且非首条目 → 用上一切片最后具体板块
                if not raw_sector and sec_idx == 0 and idx > 0:
                    prev_data = None
                    for prev_idx, prev_d in results:
                        if prev_idx == idx - 1:
                            prev_data = prev_d
                            break
                    if prev_data:
                        for prev_item in reversed(prev_data):
                            if not isinstance(prev_item, dict):
                                continue
                            prev_sec = (prev_item.get('sector') or prev_item.get('sector_name') or '').strip()
                            prev_sec = re.sub(r'\*\d+$', '', prev_sec)
                            if prev_sec and prev_sec != '其他':
                                raw_sector = prev_sec
                                break
                sector_final = raw_sector if raw_sector else '其他'
                for stock in item.get('stocks', []) or []:
                    if not isinstance(stock, dict):
                        continue
                    s = dict(stock)
                    s['sector'] = sector_final
                    s['_slice'] = idx
                    all_parsed.append(s)

        def _better(a, b):
            a = (a or '').strip(); b = (b or '').strip()
            if not a: return b
            if not b: return a
            return a if len(a) >= len(b) else b

        merged = {}
        for s in all_parsed:
            code = (s.get('code') or '').strip()
            if not code:
                continue
            if code not in merged:
                merged[code] = s
                continue
            cur = merged[code]
            for k in ('name', 'days', 'time', 'market_cap', 'turnover', 'keywords'):
                cur[k] = _better(cur.get(k), s.get(k))
            # sector: 首次出现的板块通常更准（板块标题先于股票出现），但若首次是"其他"就接受新的
            if (cur.get('sector') in ('其他', '', None)) and s.get('sector') not in ('其他', '', None):
                cur['sector'] = s.get('sector')

        return list(merged.values()), errors, last_out, last_err

    try:
        _save('ocr_starting')

        # ---- 阶段 A: 原图直接 7 字段 mmx, 5min × 2 次重试 ----
        # 原图可能触发上游 token 上限 (system error 或 hang)，所以给足 5min timeout
        # 2 次都失败才走切图兜底（切图每片 1.3MB 必然能拿 7 字段全数据）
        _save('ocr_full', phase='A', max_attempts=2, timeout=300)
        full_data, full_err, full_out, full_errstr, full_attempts = _try_ocr_image(
            filepath, prompt, max_attempts=2, timeout=300, attempt_label='full.')
        results_for_merge = []
        date_image = filepath  # 默认日期识别用原图
        parsed_data = None
        if full_data:
            _save('ocr_full_ok')
            results_for_merge = [(-1, full_data)]
        else:
            # ---- 阶段 B: 切图 + 并发 + 合并（7 字段 prompt, 跟之前一样）----
            _save('ocr_full_failed', err=full_err, fallback='slice')
            slice_paths, slice_meta = _slice_image_into(filepath)
            _save('sliced', **slice_meta)
            N = len(slice_paths)

            # 并发 + 每片最多 2 次重试
            from concurrent.futures import ThreadPoolExecutor, as_completed
            results_by_idx = {}
            progress = {'done': 0}
            progress_lock = _th.Lock()

            def _ocr_slice(idx, sp):
                with progress_lock:
                    _save('ocr_slicing', done=progress['done'], total=N, current=idx)
                d, e, o, estr, slice_attempts = _try_ocr_image(sp, prompt, max_attempts=2,
                                                attempt_label=f'slice{idx}.')
                with progress_lock:
                    progress['done'] += 1
                    _save('ocr_slicing', done=progress['done'], total=N, current=idx)
                return idx, d, e, o, estr

            # max_workers=3 避免打挂 mmx 上游（实测并发 5 会触发限速，单片排队到 >180s 超时）
            MAX_WORKERS = min(3, N)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futs = [ex.submit(_ocr_slice, i, sp) for i, sp in enumerate(slice_paths)]
                for f in as_completed(futs):
                    try:
                        idx, data, err, out, errstr = f.result()
                    except Exception as _fe:
                        continue  # 内部已 try，正常不会到这里
                    results_by_idx[idx] = (data, err)
                    if out: full_out = out
                    if errstr: full_errstr = errstr

            # 按 idx 升序喂给合并函数
            for idx in range(N):
                data, err = results_by_idx.get(idx, ([], 'missing'))
                if data:
                    results_for_merge.append((idx, data))
                else:
                    full_err = (full_err or '') + f' slice{idx}:{err}'
            # 日期识别改用第一块切片（标题在顶部）
            date_image = slice_paths[0]

        if parsed_data is None and not results_for_merge:
            err_msg = '原图和切图都识别失败: ' + (full_err or 'unknown')
            _save_result({'status': 'error', 'stage': 'error', 'error': err_msg,
                          'mmx_stderr': (full_errstr or '')[:500],
                          'raw_stdout': (full_out or '')[:500],
                          'job_id': job_id})
            return

        if parsed_data is None:
            # 切图路径: 走合并逻辑
            _save('merging', sources=[idx for idx, _ in results_for_merge],
                  total_raw=sum(len(d) for _, d in results_for_merge))
            parsed_data, _merge_errs, _, _ = _merge_parsed_results(results_for_merge)
        else:
            _save('merging_short_path', count=len(parsed_data))

        # ---- 日期识别：用 date_image（原图成功用原图, 否则用第一块切片）----
        # 加 2 次重试 + 120s timeout。原图日期可能跟 OCR 一样触发上游 token 限制,
        # 用第一块切片更稳（也跟 OCR 一致，标题通常在最顶部）
        date_prompt = (
            'What date is shown on this image? '
            'Output ONLY the date text visible in the title or header area, '
            'e.g. "05.15" or "5月15日". '
            'Do not add any explanation or additional text.'
        )
        date_r = None
        for _d_attempt in range(2):
            try:
                date_r = subprocess.run(
                    ['/usr/local/bin/mmx', 'vision', 'describe', '--image', date_image,
                     '--output', 'json', '--prompt', date_prompt],
                    capture_output=True, text=True, timeout=120
                )
                if date_r.returncode == 0 and date_r.stdout.strip():
                    break
            except subprocess.TimeoutExpired:
                time.sleep(2)
                continue
        parsed_date = trade_date
        if date_r is not None and date_r.returncode == 0:
            try:
                date_outer = _json.loads(date_r.stdout.strip())
                date_str = date_outer.get('content', '').strip()
                if date_str:
                    m = re.match(r'(\d{1,2})\.(\d{1,2})', date_str)
                    if m:
                        month, day = int(m.group(1)), int(m.group(2))
                        from datetime import datetime as _dt
                        year = _dt.now().year
                        parsed_date = f'{year}{month:02d}{day:02d}'
                    else:
                        m2 = re.match(r'(\d{1,2})月(\d{1,2})日', date_str)
                        if m2:
                            month, day = int(m2.group(1)), int(m2.group(2))
                            from datetime import datetime as _dt
                            year = _dt.now().year
                            parsed_date = f'{year}{month:02d}{day:02d}'
            except Exception:
                pass
        if not parsed_date:
            parsed_date = datetime.now().strftime('%Y%m%d')

        # 兼容下游：原代码会用 parse_error/raw_stdout/mmx_stderr 做诊断
        parse_error = full_err or ''
        raw_stdout = (full_out or '')[:800]
        mmx_stderr = (full_errstr or '')[:500] if (full_err or full_errstr) else ''

        _save('parsing', parse_error=parse_error, raw_stdout=raw_stdout,
              mmx_stderr=mmx_stderr, merged_count=len(parsed_data))
        boards_map = {}
        streak_stocks = []

        if parsed_data and any('stocks' in item for item in parsed_data):
            flat = []
            for item in parsed_data:
                if isinstance(item, dict) and 'stocks' in item:
                    sector_base = (item.get('sector') or item.get('sector_name') or '其他').strip()
                    sector_base = re.sub(r'\*\d+$', '', sector_base)
                    for stock in item.get('stocks', []):
                        stock_copy = dict(stock)
                        stock_copy['sector'] = sector_base
                        flat.append(stock_copy)
                elif isinstance(item, dict):
                    flat.append(item)
            parsed_data = flat

        for s in parsed_data:
            sector = (s.get('sector') or '其他').strip()
            sector = re.sub(r'\*\d+$', '', sector)
            if sector not in boards_map:
                boards_map[sector] = []
            boards_map[sector].append(s)
            days = s.get('days', '1') or '1'
            if days not in ('1', '首板', '1天'):
                streak_stocks.append(s)

        if not parsed_date:
            parsed_date = datetime.now().strftime('%Y%m%d')

        def to_float(v):
            try:
                if not v: return 0.0
                s = str(v).replace(',', '').replace('亿', '').replace('万', '')
                return float(s)
            except Exception:
                return 0.0

        def norm(v):
            if not v or v in ('1', '首板', '1天'):
                return '首板'
            return str(v)

        _save('writing_db')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM limitup WHERE date=?', (parsed_date,))
        cnt = 0
        for s in streak_stocks:
            try:
                c.execute('''INSERT INTO limitup (date,code,name,marketCap,time,sector,volume,streak,keyword)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (parsed_date, s.get('code',''), s.get('name',''),
                     to_float(s.get('market_cap')),
                     s.get('time',''), s.get('sector',''),
                     to_float(s.get('turnover')), norm(s.get('days','')), s.get('keywords', '')))
                cnt += 1
            except Exception:
                pass
        for board_sector, stocks in boards_map.items():
            for s in stocks:
                try:
                    c.execute('''INSERT INTO limitup (date,code,name,marketCap,time,sector,volume,streak,keyword)
                        VALUES (?,?,?,?,?,?,?,?,?)''',
                        (parsed_date, s.get('code',''), s.get('name',''),
                         to_float(s.get('market_cap')),
                         s.get('time',''), board_sector,
                         to_float(s.get('turnover')), norm(s.get('days', '1')), s.get('keywords', '')))
                    cnt += 1
                except Exception:
                    pass
        conn.commit()
        conn.close()
        # 刷新涉及的 ts_code 的 sector summary (用本日期所有 code, 包括刚被 DELETE 替换的)
        try:
            _sum_conn = sqlite3.connect(DB_PATH)
            _sum_codes = [r[0] for r in _sum_conn.execute(
                'SELECT DISTINCT code FROM limitup WHERE date = ? AND code IS NOT NULL',
                (parsed_date,)
            ).fetchall()]
            _sum_conn.close()
            if _sum_codes:
                refresh_sector_summary_for_codes(_sum_codes)
        except Exception as _e:
            print(f'[run_ocr_job] sector summary 刷新失败 (非致命): {_e}')
        # 计算图片尺寸 + 切片信息 (前端进度条显示 "1920x1800 整张直送 1 次成功" / "4 块每块 ~1900px")
        from PIL import Image as _PILImage
        _w, _h = _PILImage.open(filepath).size
        result = {
            'status': 'done', 'stage': 'done', 'count': cnt, 'date': parsed_date,
            'boards': list(boards_map.keys()), 'streak_count': len(streak_stocks),
            'image_path': filepath, 'job_id': job_id,
            'image_size': f'{_w}x{_h}',
            'slices': slice_paths,
            'attempts': full_attempts if parsed_data is not None else len(results_by_idx),
            'mode': 'full' if parsed_data is not None else f'sliced_{N}',
        }
        if cnt == 0:
            result['parse_error'] = parse_error
            result['raw_stdout'] = raw_stdout
            result['mmx_stderr'] = mmx_stderr
        _save_result(result)
        # OCR 成功, 5 分钟后删临时图片 (让前端有时间加载预览, status=error 不删保留重试)
        _schedule_ocr_image_cleanup(filepath, delay_sec=0)
    except Exception as e:
        _save_result({'status': 'error', 'stage': 'error', 'error': str(e), 'job_id': job_id})


def _ocr_status_path(job_id):
    return os.path.join(os.path.dirname(__file__), 'uploads', f'_ocr_{job_id}.json')


def _schedule_ocr_image_cleanup(filepath, delay_sec=0):
    """OCR 成功后立即删临时图片 (delay_sec=0). 前端 OCR modal 只显示识别结果不显示原图, 没并发读风险.
    status=error 时不删 (保留供重试). 启动一个 daemon 线程等 N 秒后 os.remove.
    """
    if not filepath or not os.path.exists(filepath):
        return
    import threading as _th
    def _del():
        import time as _t
        if delay_sec > 0:
            _t.sleep(delay_sec)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f'[ocr-cleanup] 已删 {os.path.basename(filepath)} (job 跑完 +{delay_sec}s)', flush=True)
        except Exception as e:
            print(f'[ocr-cleanup] 删 {filepath} 失败: {e}', flush=True)
    _th.Thread(target=_del, daemon=True).start()


def _save_result(result):
    try:
        with open(_ocr_status_path(result.get('job_id', 'unknown')), 'w', encoding='utf-8') as _f:
            _json.dump(result, _f, ensure_ascii=False)
    except Exception:
        pass


def _read_ocr_status(job_id):
    try:
        with open(_ocr_status_path(job_id), 'r', encoding='utf-8') as _f:
            return _json.load(_f)
    except Exception:
        return None


def _load_jiuye_creds():
    """读 .jiuye_login.json，返回 {'phone':..., 'password':...} 或 None（文件不存在/无效）。
    凭据文件不应进 git（已在 .gitignore），权限建议 chmod 600。
    """
    path = os.path.join(os.path.dirname(__file__), '.jiuye_login.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get('phone') and d.get('password'):
            return {'phone': str(d['phone']).strip(), 'password': str(d['password'])}
    except Exception:
        pass
    return None


async def _jiuye_auto_login(context, page, creds):
    """用账号密码自动登录韭研。成功返回 True，失败返回 False。
    页面 DOM 实测（2026-06）：登录页有"手机快捷登录"和"账号密码登录"两个 tab，
    默认"手机快捷登录"激活；需点切到"账号密码登录"才会出现密码 input。
    """
    try:
        await page.goto('https://www.jiuyangongshe.com/login',
                        wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(1200)
        # 切到"账号密码登录" tab
        try:
            await page.locator('text=账号密码登录').first.click(timeout=5000)
            await page.wait_for_timeout(500)
        except Exception:
            return False
        # 填手机号和密码（密码 input type=password 唯一）
        try:
            phone_inputs = await page.locator('input[placeholder="请输入手机号"]').all()
            if not phone_inputs:
                return False
            # 切到密码 tab 后，可能两个 tab 的手机号输入都存在；用最后一个（密码 tab 的）
            await phone_inputs[-1].fill(creds['phone'])
            await page.locator('input[type="password"]').first.fill(creds['password'])
        except Exception:
            return False
        # 点登录按钮（密码 tab 下的"登录"按钮；多个时取最后一个，密码 tab 通常在后）
        try:
            login_btns = await page.locator('button:has-text("登录")').all()
            if not login_btns:
                return False
            await login_btns[-1].click(timeout=5000)
        except Exception:
            return False
        # 等 SESSION cookie 出现 或 URL 跳走（最多 15s）
        for _ in range(30):
            cookies = await context.cookies('https://www.jiuyangongshe.com')
            if any(c.get('name') == 'SESSION' and c.get('value') for c in cookies):
                # 再等一点让其它登录态 cookie 也落下来
                await page.wait_for_timeout(500)
                return True
            await page.wait_for_timeout(500)
        return False
    except Exception:
        return False


def _persist_jiuye_cookies(cookies_path, fresh_cookies):
    """把 Playwright context.cookies() 返回的最新 cookies 合并写回文件。
    Playwright 字段 (name/value/domain/path/expires/httpOnly/secure/sameSite) 与文件 schema 一致。
    以 (name, domain, path) 为唯一键覆盖；原文件中独有的 cookie 保留（防止刷掉之前手动放的特殊 cookie）。
    原子写：tmp + rename，避免半写文件让下次启动直接挂掉。
    """
    try:
        with open(cookies_path, 'r', encoding='utf-8') as _f:
            old = json.load(_f)
        if not isinstance(old, list):
            old = []
    except Exception:
        old = []

    def _key(c):
        return (c.get('name', ''), c.get('domain', ''), c.get('path', '/'))

    merged = {_key(c): c for c in old}
    for c in fresh_cookies:
        if not isinstance(c, dict) or not c.get('name'):
            continue
        # Playwright 用 -1 表示 session cookie；归一成 0（与原文件 1970 时间戳风格一致）
        if c.get('expires') in (None, -1):
            c = dict(c); c['expires'] = 0
        merged[_key(c)] = c

    tmp_path = cookies_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as _f:
        json.dump(list(merged.values()), _f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, cookies_path)


def fetch_jiuye_diagram(date_str, job_id=None):
    """从韭研公社拉取某日涨停简图，存到 uploads/，返回 (filepath, job_id, ymd) 或 raise。date_str 格式 YYYY-MM-DD。
    job_id 可选传入；不传则内部生成。传入是为了让 /api/limitup/fetch 异步流程能预先把 job_id 返回给前端轮询。
    """
    cookies_path = os.path.join(os.path.dirname(__file__), '.jiuye_cookies.json')
    creds = _load_jiuye_creds()
    if not os.path.exists(cookies_path):
        if not creds:
            raise RuntimeError(f'cookie 文件不存在: {cookies_path}，且 .jiuye_login.json 也没配，无法自动登录')
        cookie_list = []  # 空 cookie 启动，靠 _jiuye_auto_login 建立会话
    else:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            cookie_list = json.load(f)
        if not isinstance(cookie_list, list):
            raise RuntimeError('cookie 文件格式错误：应为数组')

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    job_id = job_id or _uuid.uuid4().hex[:8]
    ymd = date_str.replace('-', '')

    async def _fetch():
        from playwright.async_api import async_playwright
        img_url_holder = {'url': None, 'err': None}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
                viewport={'width': 1440, 'height': 900},
            )
            await context.add_cookies(cookie_list)
            page = await context.new_page()
            # A. 拉图前先访问首页让阿里 WAF 派发新鲜的 acw_tc / cdn_sec_tc（这俩 24h 过期，
            #    是 cookie 失效的主因）。访问任意 jiuyangongshe.com 路径都会刷新它们。
            try:
                await page.goto('https://www.jiuyangongshe.com/', wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(800)
            except Exception:
                pass  # 失败不阻断主流程，下面 goto 实际目标页时还会再 ping 一次

            # D'. 拉图前直接走自动登录刷新 SESSION，不靠 DOM 判定登录态。
            #     原因：cookie 半失效时（SESSION 过期但其它 cookie 还在），SPA 顶部不显示
            #     "登录注册" 按钮，DOM 误判"已登录"；但跳 action 页时会弹登录对话框挡操作。
            #     每天一次频率，每次多 5s 登录开销可接受。
            if creds:
                ok = await _jiuye_auto_login(context, page, creds)
                if not ok:
                    # 登录失败不立即抛错，让后面的拉图尝试一次，错误统一在那里报
                    pass

            async def on_resp(r):
                if 'diagram-url' in r.url and r.request.method == 'POST':
                    try:
                        body = await r.json()
                    except Exception:
                        body = await r.text()
                    err = body.get('errCode') if isinstance(body, dict) else None
                    if err in ('0', 0):
                        img_url_holder['url'] = body.get('data')
                    else:
                        img_url_holder['err'] = body
            page.on('response', on_resp)
            try:
                await page.goto(f'https://www.jiuyangongshe.com/action/{date_str}',
                                wait_until='domcontentloaded', timeout=45000)
            except Exception as e:
                await browser.close()
                raise RuntimeError(f'打开页面失败: {e}')
            await page.wait_for_timeout(2000)
            # diagram-url 仅在"涨停简图" tab 激活时前端 JS 才会调，需点击该 tab
            # 2026-06 起韭研 SPA 重渲染后单凭 text= 容易点不中，加多策略 + 失败必喊，
            # 否则 fail silent 12s 后误报"无数据/cookie 失效"误导排查
            tab_clicked = False
            # 策略1: aria-controls 包含 diagram（element-plus 标准属性，最稳）
            try:
                await page.locator('[role="tab"][aria-controls*="diagram"]').first.click(timeout=5000)
                tab_clicked = True
                print(f'[jiuye] tab 点中 (策略1: aria-controls=diagram)', flush=True)
            except Exception as e:
                print(f'[jiuye] tab 策略1 失败: {type(e).__name__}: {e}', flush=True)
            # 策略2: 文字匹配（fallback，兼容老版本 DOM）
            if not tab_clicked:
                try:
                    await page.locator('text=涨停简图').first.click(timeout=5000)
                    tab_clicked = True
                    print(f'[jiuye] tab 点中 (策略2: text=涨停简图)', flush=True)
                except Exception as e:
                    print(f'[jiuye] tab 策略2 失败: {type(e).__name__}: {e}', flush=True)
            # 策略3: 尝试 Vue store 强切（Vue 2 兼容，Vue 3 大概率 no vue，但留着不亏）
            if not tab_clicked:
                try:
                    vue_result = await page.evaluate("""() => {
                        const app = document.querySelector('#app');
                        if (!app || !app.__vue__) return 'no vue';
                        const walk = (n) => {
                            if (n.activeName !== undefined) { n.activeName = 'diagram'; return true; }
                            if (n.$children) for (const c of n.$children) if (walk(c)) return true;
                            return false;
                        };
                        return walk(app.__vue__) ? 'toggled' : 'no activeName';
                    }""")
                    print(f'[jiuye] tab 策略3 (Vue store) 返回: {vue_result}', flush=True)
                except Exception as e:
                    print(f'[jiuye] tab 策略3 失败: {type(e).__name__}: {e}', flush=True)
            # 全部失败时：把页面所有 tab 元素 dump 出来，下次失败能直接看到 DOM 长啥样
            if not tab_clicked:
                try:
                    tabs_dump = await page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('[role="tab"], .el-tabs__item')).map(t => ({
                            tag: t.tagName,
                            text: (t.textContent || '').trim().slice(0, 30),
                            aria: t.getAttribute('aria-controls') || '',
                            cls: t.className || ''
                        }));
                    }""")
                    print(f'[jiuye] tab 三策略全失败, 页面所有 tab 元素: {tabs_dump}', flush=True)
                except Exception as e:
                    print(f'[jiuye] tab dump 也失败: {type(e).__name__}: {e}', flush=True)
            # 等 diagram-url 响应（最久 25 秒，比原来 12s 翻倍，给 SPA 懒加载时间）
            for _ in range(50):
                if img_url_holder['url'] or img_url_holder['err']:
                    break
                await page.wait_for_timeout(500)
            # 兜底：25 秒还没响应, 重试点一次 tab 再等 8 秒（应对首次点击未生效）
            if not img_url_holder['url'] and not img_url_holder['err']:
                print('[jiuye] 25s 内未捕获响应, 重试点击 tab...', flush=True)
                try:
                    await page.locator('[role="tab"][aria-controls*="diagram"]').first.click(timeout=3000)
                except Exception:
                    try:
                        await page.locator('text=涨停简图').first.click(timeout=3000)
                    except Exception as e:
                        print(f'[jiuye] tab 重试点击也失败: {type(e).__name__}: {e}', flush=True)
                for _ in range(16):
                    if img_url_holder['url'] or img_url_holder['err']:
                        break
                    await page.wait_for_timeout(500)
            # B. 拉完后把 context 当前的 cookies 全部回写文件，覆盖文件里旧的同名 cookie。
            #    这样下次运行时 acw_tc/cdn_sec_tc 和（若有）刷新过的 SESSION 都用新的。
            try:
                fresh_cookies = await context.cookies('https://www.jiuyangongshe.com')
                if fresh_cookies:
                    _persist_jiuye_cookies(cookies_path, fresh_cookies)
            except Exception:
                pass
            await browser.close()
            if img_url_holder['err']:
                raise RuntimeError(f'韭研 API 错误: {img_url_holder["err"].get("msg")} (errCode={img_url_holder["err"].get("errCode")})')
            if not img_url_holder['url']:
                raise RuntimeError('未捕获到 diagram-url 响应（该日可能无涨停数据或 cookie 已失效）')
            return img_url_holder['url']

    img_url = asyncio.run(_fetch())
    img_resp = _requests.get(img_url, timeout=60)
    img_resp.raise_for_status()
    ext = '.png'
    base = img_url.split('?')[0].split('/')[-1]
    if '.' in base:
        e = '.' + base.rsplit('.', 1)[-1].lower()
        if 1 < len(e) <= 5:
            ext = e
    filename = f'jiuye_{ymd}_{job_id}{ext}'
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(img_resp.content)
    return filepath, job_id, ymd


def run_fetch_and_ocr(date_str, job_id, trade_date):
    """子进程入口：先拉韭研公社图（Playwright 启动 15-30s），再调 run_ocr_job 跑切片 OCR。
    任何阶段失败都写到 _ocr_<job_id>.json 让前端轮询能拿到，绝不让异常逃逸到父进程。
    """
    import json as _json2
    status_path = _ocr_status_path(job_id)
    def _save_err(stage, msg):
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json2.dump({'status': 'error', 'stage': stage,
                             'error': msg, 'job_id': job_id}, _f, ensure_ascii=False)
        except Exception:
            pass
    try:
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json2.dump({'status': 'processing', 'stage': 'pulling_image'},
                            _f, ensure_ascii=False)
        except Exception:
            pass
        try:
            filepath, _, _ = fetch_jiuye_diagram(date_str, job_id=job_id)
        except Exception as e:
            _save_err('pulling_image', f'拉取韭研图失败: {e}')
            return
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json2.dump({'status': 'processing', 'stage': 'fetched'},
                            _f, ensure_ascii=False)
        except Exception:
            pass
        run_ocr_job(filepath, job_id, trade_date)
    except Exception as e:
        _save_err('unexpected', f'子进程未处理异常: {type(e).__name__}: {e}')


@app.route('/api/limitup/fetch', methods=['POST'])
def fetch_limitup_from_jiuye():
    """从韭研公社拉取指定日期的涨停简图并自动 OCR 入库。date 格式 YYYY-MM-DD。
    异步：立即生成 job_id 返回，子进程里串行跑「拉图 + OCR」全流程，前端用 /parse-status 轮询。
    （之前是同步等 Playwright 拉图 15-30s，系统代理 timeout 直接 502。）
    """
    data = request.get_json(silent=True) or request.form
    date_str = (data.get('date') or '').strip()
    if not date_str or len(date_str) != 10 or date_str[4] != '-':
        return jsonify({'status': 'error', 'message': 'date 必填，格式 YYYY-MM-DD'}), 400
    trade_date = date_str.replace('-', '')
    job_id = _uuid.uuid4().hex[:8]
    try:
        with open(_ocr_status_path(job_id), 'w', encoding='utf-8') as _f:
            _json.dump({'status': 'processing', 'stage': 'pulling_image'}, _f, ensure_ascii=False)
    except Exception:
        pass
    import multiprocessing
    p = multiprocessing.Process(target=run_fetch_and_ocr,
                                args=(date_str, job_id, trade_date), daemon=True)
    p.start()
    return jsonify({'status': 'processing', 'job_id': job_id, 'date': trade_date})


@app.route('/api/limitup/parse-image', methods=['POST'])
def parse_limitup_image():
    """异步解析涨停简图：立即返回 job_id，后台处理 mmx OCR（约3-4分钟）"""
    if 'image' not in request.files:
        return jsonify({'status': 'error', 'message': '请上传图片文件'}), 400
    image_file = request.files['image']
    if not image_file.filename:
        return jsonify({'status': 'error', 'message': '请上传图片文件'}), 400

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    import uuid
    job_id = str(uuid.uuid4())[:8]
    filename = f'limitup_{datetime.now().strftime("%Y%m%d%H%M%S")}_{job_id}_{image_file.filename}'
    filepath = os.path.join(upload_dir, filename)
    image_file.save(filepath)

    trade_date = request.form.get('date', '')

    try:
        with open(_ocr_status_path(job_id), 'w', encoding='utf-8') as _f:
            _json.dump({'status': 'processing', 'stage': 'saving_image'}, _f, ensure_ascii=False)
    except Exception:
        pass

    import multiprocessing
    p = multiprocessing.Process(target=run_ocr_job, args=(filepath, job_id, trade_date), daemon=True)
    p.start()

    return jsonify({'status': 'processing', 'job_id': job_id})


@app.route('/api/limitup/parse-status/<job_id>', methods=['GET'])
def get_parse_status(job_id):
    # 优先读文件 (multiprocess 模式), 兼容老的 in-memory _ocr_jobs
    job = _read_ocr_status(job_id) or _ocr_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': '任务不存在'}), 404
    if job['status'] == 'done':
        return jsonify({'status': 'done', **job})
    elif job['status'] == 'error':
        return jsonify({'status': 'error', 'message': job['error']}), 500
    return jsonify({'status': 'processing'})


# ============ 静态文件 ============

@app.route('/')
def index():
    resp = send_from_directory(os.path.join(os.path.dirname(__file__), '..'), 'dashboard/index.html')
    # 强制浏览器每次重拉, 避免开发时缓存老 HTML 导致列错位/字段缺失
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/dashboard/<path:filename>')
def dashboard_static(filename):
    resp = send_from_directory(os.path.join(os.path.dirname(__file__), '..', 'dashboard'), filename)
    if filename.endswith('.html'):
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    return resp

# ============ 健康检查 ============

@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    exists = os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    conn.close()

    return jsonify({
        'status': 'ok',
        'database': DB_PATH,
        'exists': exists,
        'tables': [t[0] for t in tables]
    })


# ============ 数据统计 ============

@app.route('/api/trading-dates', methods=['GET'])
def get_trading_dates():
    """获取A股所有交易日（优先从本地缓存读取）"""
    start_date = request.args.get('start_date', '20200101')
    end_date = request.args.get('end_date', datetime.now().strftime('%Y%m%d'))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 先查本地缓存
    rows = c.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date',
        (start_date, end_date)
    ).fetchall()
    local_dates = [r[0] for r in rows]
    conn.close()

    # 如果本地最新日期比今天早，自动从 TuShare 补最新数据
    today = datetime.now().strftime('%Y%m%d')
    if local_dates and local_dates[-1] < today:
        pro = get_pro()
        try:
            df = pro.trade_cal(exchange='SSE', start_date=local_dates[-1], end_date=today)
            new_dates = df[df['is_open'] == 1]['cal_date'].tolist()
            if new_dates:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                for cal_date in new_dates:
                    c.execute('INSERT OR IGNORE INTO trading_dates_cache (cal_date) VALUES (?)', (cal_date,))
                conn.commit()
                conn.close()
                # 重新查询
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                rows = c.execute(
                    'SELECT cal_date FROM trading_dates_cache WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date',
                    (start_date, end_date)
                ).fetchall()
                local_dates = [r[0] for r in rows]
                conn.close()
        except Exception as e:
            print(f'补全交易日历失败: {e}')

    if local_dates:
        return jsonify(local_dates)

    # 缓存为空，拉取 TuShare 并永久存储
    pro = get_pro()
    try:
        df = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date)
        trading_dates = df[df['is_open'] == 1]['cal_date'].tolist()

        if not trading_dates:
            return jsonify({'status': 'error', 'message': 'TuShare 返回空数据'}), 500

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for cal_date in trading_dates:
            c.execute('INSERT OR IGNORE INTO trading_dates_cache (cal_date) VALUES (?)', (cal_date,))
        conn.commit()
        conn.close()

        return jsonify(trading_dates)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/is-trading-date', methods=['GET'])
def is_trading_date():
    """检查指定日期是否为交易日"""
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': '缺少 date 参数'}), 400

    pro = get_pro()
    try:
        df = pro.trade_cal(exchange='SSE', start_date=date, end_date=date)
        if len(df) > 0:
            is_open = df.iloc[0]['is_open'] == 1
            return jsonify({'date': date, 'is_trading_date': is_open})
        return jsonify({'date': date, 'is_trading_date': False})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/stats', methods=['GET'])
def stats():
    """获取各表数据量统计"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    stats = {}
    for table in ['index_daily', 'leaderboard', 'overview', 'limitup', 'trading_dates_cache']:
        try:
            count = c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            last_date = c.execute(
                f'SELECT date FROM {table} ORDER BY date DESC LIMIT 1'
            ).fetchone()
            stats[table] = {'count': count, 'last_date': last_date[0] if last_date else None}
        except:
            stats[table] = {'count': 0, 'last_date': None}

    conn.close()
    return jsonify(stats)


@app.route('/api/stock/dates-check', methods=['GET'])
def check_stock_dates():
    """检查给定日期列表中哪些已有股票日线数据"""
    dates_str = request.args.get('dates', '')
    if not dates_str:
        return jsonify([])
    dates = [d.strip() for d in dates_str.split(',')]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    placeholders = ','.join(['?' for _ in dates])
    rows = c.execute(
        f'SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date IN ({placeholders})',
        dates
    ).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])


def get_date_range(base_date, months):
    """计算基准日期往前N个月的日期范围（含首尾）"""
    dt = datetime.strptime(base_date, '%Y%m%d')
    start_dt = dt - timedelta(days=months * 30)
    return start_dt.strftime('%Y%m%d'), base_date


@app.route('/api/frequency', methods=['GET'])
def get_frequency():
    """上榜频次统计

    参数:
        date: YYYYMMDD 格式（必选，当前选中日期）

    返回:
        {
          "date": "20260520",
          "tabs": ["1个月", "6个月", "12个月"],  # 前端Tab切换选项
          "ranges": {
            "1个月": {"start": "20260420", "end": "20260520"},
            "6个月": {"start": "20251121", "end": "20260520"},
            "12个月": {"start": "20250525", "end": "20260520"}
          },
          "frequency": {
            "5日": [
              {"code": "000001", "name": "平安银行", "1个月": 2, "6个月": 8, "12个月": 15},
              ...
            ],
            "10日": [...],
            "20日": [...]
          }
        }
    """
    date = request.args.get('date')
    if not date:
        return jsonify({'status': 'error', 'message': '缺少 date 参数'}), 400

    # 计算三个时间区间
    range_labels = ["1个月", "6个月", "12个月"]
    ranges = {}
    for label in range_labels:
        months = int(label.split("个月")[0])
        dt = datetime.strptime(date, '%Y%m%d')
        start_dt = dt - timedelta(days=months * 30)
        ranges[label] = {"start": start_dt.strftime('%Y%m%d'), "end": date}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    result = {
        "date": date,
        "tabs": range_labels,
        "ranges": ranges,
        "frequency": {}
    }

    # 遍历 5日、10日、20日 三个榜单
    for lb_type in ["5日", "10日", "20日"]:
        # 用12个月的起始日期作为查询起点（覆盖所有区间）
        rows = c.execute('''
            SELECT code, name, date
            FROM leaderboard
            WHERE type=? AND date >= ? AND date <= ?
            ORDER BY date DESC, rank
        ''', (lb_type, ranges["12个月"]["start"], date)).fetchall()

        # 统计每只股票在三个时间区间的出现次数
        stats = {}
        for row in rows:
            code = row['code']
            if code not in stats:
                stats[code] = {'code': code, 'name': row['name'], '1个月': 0, '6个月': 0, '12个月': 0}

            trade_date = row['date']
            for label in range_labels:
                r = ranges[label]
                if r['start'] <= trade_date <= r['end']:
                    stats[code][label] += 1

        result["frequency"][lb_type] = [
            {"code": s['code'], "name": s['name'],
             "1个月": s['1个月'], "6个月": s['6个月'], "12个月": s['12个月']}
            for s in stats.values()
        ]

    conn.close()
    return jsonify(result)


# ============ 持仓管理 ============

import pandas as pd
import requests as _requests

# 列名别名映射: 不同券商导出格式不一样,做宽容识别
COLUMN_ALIASES = {
    'ts_code': ['证券代码', '股票代码', '代码', 'code', '证券编码', 'stock_code'],
    'name': ['证券名称', '股票名称', '名称', 'name', 'stock_name'],
    'shares': ['证券数量', '持仓数量', '当前持仓', '股份余额', '余额', '数量', '持股数', 'shares', '持仓', '参考持股数', '参考持股', '当前持股'],
    'cost_price': ['成本价', '买入均价', '成本', '摊薄成本价', 'cost', 'cost_price', '均价', '参考成本价'],
    'buy_date': ['买入日期', '建仓日期', '名称日期', '日期', 'date', 'buy_date'],
    'note': ['备注', 'note', 'memo'],
}


def _normalize_code(raw, name=None):
    """把各种格式的代码标准化成 ts_code 格式 (e.g. 600519 -> 600519.SH)

    - 6位数字: 6/9/0/2/3 开头归深市, 其他归沪市
    - 带前缀: sh600519 / sz000001 归一化
    - 已带 .SH/.SZ: 直接返回
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if s.endswith('.SH') or s.endswith('.SZ') or s.endswith('.BJ'):
        return s
    s = s.replace('SH', '').replace('SZ', '').replace('BJ', '')
    s = s.lstrip('shzSZ')
    if not s.isdigit():
        return None
    if len(s) == 6:
        if s.startswith(('6', '9', '5')):
            return f'{s}.SH'
        elif s.startswith(('0', '2', '3')):
            return f'{s}.SZ'
        elif s.startswith(('4', '8')):
            return f'{s}.BJ'
    return s


def _read_uploaded_table(filepath, ext):
    """读取上传文件为 DataFrame, 支持 csv/xlsx/xls"""
    if ext in ('.xlsx', '.xls'):
        engine = 'openpyxl' if ext == '.xlsx' else 'xlrd'
        df = pd.read_excel(filepath, engine=engine, dtype=str)
    else:
        # 尝试多种编码
        for enc in ('utf-8', 'gbk', 'gb18030', 'utf-8-sig'):
            try:
                df = pd.read_csv(filepath, encoding=enc, dtype=str)
                break
            except (UnicodeDecodeError, Exception):
                continue
        else:
            raise ValueError('无法识别文件编码, 请使用 UTF-8 或 GBK 编码的 CSV')
    # 清理列名: 去掉 BOM (﻿ / ￾) 和空白
    cleaned = []
    for c in df.columns:
        s = str(c)
        # 各种 BOM 前缀
        for bom in ('﻿', '￾', '﻿'):
            if s.startswith(bom):
                s = s[1:]
        s = s.strip()
        cleaned.append(s)
    df.columns = cleaned
    return df


def _auto_map_columns(df):
    """自动识别 DataFrame 列名, 返回 {canonical_name: actual_column}"""
    # 处理 .1 / .2 这类 pandas 自动加的重复列名后缀 — 只保留第一个
    cols = []
    seen = {}
    for c in df.columns:
        base = c
        if '.' in c and c.split('.')[-1].isdigit():
            base = c.rsplit('.', 1)[0]
        if base in seen:
            continue
        seen[base] = True
        cols.append(c)

    mapping = {}
    unmapped_cols = []
    used = set()
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            # 优先精确匹配, 再尝试 contains
            match = None
            if alias in cols and alias not in used:
                match = alias
            else:
                for c in cols:
                    if c in used:
                        continue
                    if alias and alias in c:
                        match = c
                        break
            if match:
                mapping[canonical] = match
                used.add(match)
                break
    for col in cols:
        if col not in used:
            unmapped_cols.append(col)
    return mapping, unmapped_cols


def _clean_cell(v):
    """清洗单元格: 去 BOM / 全角空白 / 普通空白"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    s = str(v)
    for bom in ('﻿', '￾', '﻿'):
        if s.startswith(bom):
            s = s[1:]
    # 全角空格 -> 半角
    s = s.replace('　', ' ').strip()
    return s


def _norm_date_str(s):
    """归一化日期字符串到 'YYYY-MM-DD'. 输入 8位 / 10位 / 带斜线 都接受. 空 → ''."""
    if not s: return ''
    s = str(s).replace('-', '').replace('/', '').strip()
    if len(s) == 8 and s.isdigit():
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
    return str(s).strip()  # 兜底: 不认识就原样返回 (前端会再处理)


def _upsert_position(c, r):
    """写入单条持仓。如果是加仓（同一 ts_code 已存在未清仓记录），自动合并：shares 累加，cost_price 加权平均，buy_date 取最早。返回 ('inserted' | 'added' | 'updated' | 'skipped', id)。"""
    # 归一化 buy_date 到 'YYYY-MM-DD' (前端可能传 8 位 / 10 位 / 都不传, 一律转成 10 位)
    r['buy_date'] = _norm_date_str(r.get('buy_date'))
    # 1) 查同 ts_code 的未清仓持仓（按买入时间最早的优先合并；如果有 original_shares 表示是还原回来的也合并）
    existing = c.execute(
        '''SELECT id, shares, cost_price, buy_date, original_shares FROM positions
           WHERE ts_code=? AND closed_at IS NULL
           ORDER BY (buy_date IS NULL), buy_date, id
           LIMIT 1''',
        (r['ts_code'],)
    ).fetchone()
    if existing:
        old_id, old_shares, old_cost, old_buy_date, old_original = existing
        old_shares = old_shares or 0
        old_cost = old_cost or 0
        new_shares = old_shares + r['shares']
        if new_shares <= 0:
            return 'skipped', old_id
        # 加权平均成本价
        new_cost = (old_shares * old_cost + r['shares'] * r['cost_price']) / new_shares
        # 交易日期取最新 (加仓时显示最近一次加仓的日期)
        candidates = [d for d in (old_buy_date, r.get('buy_date') or '') if d]
        new_buy_date = max(candidates) if candidates else date.today().isoformat()
        new_original = (old_original or old_shares) + r['shares']
        # last_trade_date 取最新 (用户手动加仓时 buy_date 就是交易日)
        new_last_trade = new_buy_date if r.get('buy_date') else None
        c.execute('''UPDATE positions SET
            shares=?, cost_price=?, buy_date=?, last_trade_date=?, original_shares=?,
            position_type='加仓',
            name=COALESCE(NULLIF(?, ''), positions.name),
            note=COALESCE(NULLIF(?, ''), positions.note),
            updated_at=datetime('now', 'localtime')
            WHERE id=?''',
            (new_shares, new_cost, new_buy_date, new_last_trade, new_original,
             r.get('name') or '', r.get('note') or '', old_id))
        return 'added', old_id
    # 2) 没有现存未清仓持仓 → 新增
    # 资金股份 导入: buy_date 留空 (用户 2026-06-04 反馈)
    init_buy = r.get('buy_date') or ''
    cur = c.execute('''INSERT INTO positions
        (ts_code, name, shares, original_shares, cost_price, buy_date, last_trade_date, note, position_type, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '建仓', datetime('now', 'localtime'))''',
        (r['ts_code'], r.get('name') or '', r['shares'], r['shares'],
         r['cost_price'], init_buy, init_buy or None, r.get('note') or ''))
    return 'inserted', cur.lastrowid


def _parse_positions_from_df(df, column_map):
    """根据列名映射解析出持仓记录列表"""
    records = []
    for _, row in df.iterrows():
        raw_code = _clean_cell(row.get(column_map.get('ts_code')))
        if not raw_code:
            continue
        ts_code = _normalize_code(raw_code)
        if not ts_code:
            continue

        name = _clean_cell(row.get(column_map.get('name', ''), ''))

        try:
            shares = float(_clean_cell(row[column_map['shares']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if shares <= 0:
            continue

        try:
            cost_price = float(_clean_cell(row[column_map['cost_price']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if cost_price <= 0:
            continue

        buy_date = ''
        if 'buy_date' in column_map:
            buy_date = _norm_date_str(_clean_cell(row[column_map['buy_date']]))

        note = ''
        if 'note' in column_map:
            note = _clean_cell(row[column_map['note']])

        records.append({
            'ts_code': ts_code,
            'name': name,
            'shares': shares,
            'cost_price': cost_price,
            'buy_date': buy_date,
            'note': note,
        })
    return records


@app.route('/api/positions/preview', methods=['POST'])
def preview_positions_csv():
    """解析上传文件, 返回识别结果预览 (不入库)"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.csv', '.xlsx', '.xls'):
        return jsonify({'status': 'error', 'message': f'不支持的文件格式: {ext}, 请使用 csv/xlsx/xls'}), 400

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    import uuid
    tmp_id = str(uuid.uuid4())[:8]
    safe_name = f'positions_{tmp_id}{ext}'
    filepath = os.path.join(upload_dir, safe_name)
    f.save(filepath)

    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        try: os.remove(filepath)
        except: pass
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, unmapped = _auto_map_columns(df)
    missing_required = [k for k in ('ts_code', 'shares', 'cost_price') if k not in mapping]

    preview = []
    if not missing_required:
        records = _parse_positions_from_df(df, mapping)
        # 只展示前 20 条
        for r in records[:20]:
            preview.append(r)
    else:
        records = []

    # 保留文件, 用户确认后用同文件再次调用 import 接口
    return jsonify({
        'status': 'success',
        'tmp_id': tmp_id,
        'filename': safe_name,
        'total_rows': len(df),
        'columns': list(df.columns),
        'column_map': mapping,
        'unmapped_columns': unmapped,
        'missing_required': missing_required,
        'preview': preview,
        'parsed_count': len(records),
    })


@app.route('/api/positions/import', methods=['POST'])
def import_positions():
    """根据上传文件 (或 column_map override) 真正入库"""
    data = request.get_json() or {}
    filename = data.get('filename')
    column_map_override = data.get('column_map')  # 可选, 让用户手动指定列
    file_date = data.get('file_date')  # 文件名里的日期, buy_date 兜底 (用户 2026-06-05 反馈)

    if not filename:
        return jsonify({'status': 'error', 'message': '缺少 filename'}), 400

    filepath = os.path.join(os.path.dirname(__file__), 'uploads', filename)
    if not os.path.exists(filepath):
        return jsonify({'status': 'error', 'message': '上传文件已失效, 请重新上传'}), 400

    ext = os.path.splitext(filename)[1].lower()
    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, unmapped = _auto_map_columns(df)
    if column_map_override:
        mapping.update(column_map_override)

    missing_required = [k for k in ('ts_code', 'shares', 'cost_price') if k not in mapping]
    if missing_required:
        return jsonify({
            'status': 'error',
            'message': f'缺少必需列: {missing_required}',
            'columns': list(df.columns),
            'column_map': mapping,
        }), 400

    records = _parse_positions_from_df(df, mapping)
    if not records:
        return jsonify({'status': 'error', 'message': '未解析出任何有效记录'}), 400

    # 文件名日期兜底: CSV 里没 buy_date 的行, 用 file_date
    if file_date:
        for r in records:
            if not (r.get('buy_date') or '').strip():
                r['buy_date'] = file_date

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    inserted = 0
    added = 0
    skipped = 0
    for r in records:
        try:
            action, _ = _upsert_position(c, r)
            if action == 'inserted':
                inserted += 1
            elif action == 'added':
                added += 1
            else:
                skipped += 1
        except Exception as e:
            print(f'positions 写入错误 {r["ts_code"]}: {e}')
            skipped += 1
    conn.commit()
    conn.close()

    # 清理上传文件
    try: os.remove(filepath)
    except: pass

    return jsonify({
        'status': 'success',
        'inserted': inserted,
        'added': added,
        'skipped': skipped,
        'total': len(records),
    })


@app.route('/api/positions', methods=['GET'])
def get_positions():
    """获取持仓列表 (现持仓: shares>0 且未清仓), 可选 ?with_quote=1 拉实时价并计算盈亏"""
    with_quote = request.args.get('with_quote', '1') == '1'
    include_closed = request.args.get('include_closed', '0') == '1'
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if include_closed:
        rows = c.execute('SELECT * FROM positions ORDER BY created_at DESC').fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM positions WHERE shares > 0 AND closed_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    conn.close()

    positions = [dict(r) for r in rows]

    # 成本市值不依赖行情, 始终先算出来
    for p in positions:
        cost = p.get('cost_price')
        sh = p.get('shares')
        p['cost_value'] = round(cost * sh, 2) if cost and sh else None
        p['market_value'] = None
        p['current_price'] = None
        p['change_pct'] = None
        p['quote_time'] = None
        p['quote_name'] = None
        p['profit'] = None
        p['profit_pct'] = None

    if with_quote and positions:
        codes = list(set(p['ts_code'] for p in positions))
        quote_map = fetch_sina_quotes(codes)
        for p in positions:
            q = quote_map.get(p['ts_code'], {})
            p['current_price'] = q.get('price')
            p['prev_close'] = q.get('prev_close')
            p['change_pct'] = q.get('change_pct')
            p['quote_time'] = q.get('time')
            p['quote_name'] = q.get('name') or p['name']
            cur = p['current_price']
            prev = p['prev_close']
            if p.get('cost_value') is not None and cur is not None:
                p['market_value'] = round(cur * p['shares'], 2)
                # 浮动盈亏: 跟中信证券公式保持一致
                # 公式: (现价 - 持仓成本价) × 数量 - 卖出佣金(万1, min5) - 印花税(万5) - 卖出过户费(万0.1)
                # 持仓成本价 = cost_price (含买入费, broker 摊薄成本价口径)
                # 买入费 不进入 profit, 只在 tooltip 提示
                p['buy_fee'] = round(calc_buy_fees(p['cost_value'], p['ts_code']), 2)
                p['cost_with_fees'] = round(p['cost_value'] + p['buy_fee'], 2)
                p['sell_commission'] = round(max(p['market_value'] * FEE_CONFIG['commission_rate'],
                                                  FEE_CONFIG['min_commission']), 2)
                p['stamp_tax'] = round(p['market_value'] * FEE_CONFIG['stamp_rate'], 2)
                # 沪市 (.SH/.BJ) 含卖出过户费, 深市 (.SZ) 不含 (过户费打包在佣金里)
                is_shanghai = _is_shanghai(p['ts_code'])
                p['sell_transfer_fee'] = round(p['market_value'] * FEE_CONFIG['transfer_rate'], 2) if is_shanghai else 0
                # 浮动盈亏 = (现价 - 持仓成本) × 数量 - 卖佣 - 印花税 - 卖出过户费(沪市)
                p['profit'] = round((p['current_price'] - p['cost_price']) * p['shares']
                                    - p['sell_commission'] - p['stamp_tax'] - p['sell_transfer_fee'], 2)
                # 盈亏比 = (现价 - 持仓成本价) / 持仓成本价 × 100 (broker 每股毛百分比)
                p['profit_pct'] = round((p['current_price'] - p['cost_price']) / p['cost_price'] * 100, 3) if p['cost_price'] else None
                # 预估卖出费: 若按现价全部卖出, 还要花多少 (含过户费, 用于 tooltip)
                p['sell_cost_estimate'] = round(calc_sell_fees(p['market_value'], p['ts_code']), 2)
            # 单日盈亏: (现价 - 昨收) × 持仓 (不扣费, 反映日内价差)
            if cur is not None and prev is not None:
                p['daily_profit'] = round((cur - prev) * p['shares'], 2)
            else:
                p['daily_profit'] = None
            # 仓位类型标签: 减仓优先判定, 否则用 DB hint / fallback
            p['position_type'] = _classify_position_type(p)

    # 主力净流入聚合 (近 5/20 个交易日, 一次查询避免 N+1)
    if positions:
        ts_codes = list(set(p['ts_code'] for p in positions))
        placeholders = ','.join('?' * len(ts_codes))
        conn_flow = sqlite3.connect(DB_PATH, timeout=30)
        flow_rows = conn_flow.execute(f"""
            SELECT ts_code, trade_date, main_net_inflow
            FROM fund_flow
            WHERE ts_code IN ({placeholders})
              AND trade_date >= date('now', '-' || '20 days', 'localtime')
            ORDER BY ts_code, trade_date DESC
        """, ts_codes).fetchall()
        conn_flow.close()
        flow_by_code = {}
        for r in flow_rows:
            flow_by_code.setdefault(r[0], []).append(r[2] or 0)
        for p in positions:
            vals = flow_by_code.get(p['ts_code'], [])
            p['main_net_5d'] = round(sum(vals[:5]), 2) if vals else None
            p['main_net_20d'] = round(sum(vals[:20]), 2) if vals else None

    # 板块历史 (每只股的历史涨停板块)
    _attach_sector(positions, key='ts_code')

    total_mv = sum((p.get('market_value') or 0) for p in positions)
    total_cost = sum((p.get('cost_value') or 0) for p in positions)
    total_cost_with_fees = sum((p.get('cost_with_fees') or 0) for p in positions)
    total_buy_fee = sum((p.get('buy_fee') or 0) for p in positions)
    total_sell_estimate = sum((p.get('sell_cost_estimate') or 0) for p in positions)
    total_sell_commission = sum((p.get('sell_commission') or 0) for p in positions)
    total_stamp_tax = sum((p.get('stamp_tax') or 0) for p in positions)
    total_profit = sum((p.get('profit') or 0) for p in positions)
    total_daily = sum((p.get('daily_profit') or 0) for p in positions)
    summary = {
        'count': len(positions),
        'total_market_value': round(total_mv, 2),
        'total_cost': round(total_cost, 2),
        'total_cost_with_fees': round(total_cost_with_fees, 2),
        'total_buy_fee': round(total_buy_fee, 2),
        'total_sell_estimate': round(total_sell_estimate, 2),
        'total_sell_commission': round(total_sell_commission, 2),
        'total_stamp_tax': round(total_stamp_tax, 2),
        'total_profit': round(total_profit, 2),
        'total_profit_pct': round(total_profit / total_cost * 100, 2) if total_cost else None,
        'total_daily_profit': round(total_daily, 2),
        'fee_config': FEE_CONFIG,
    }

    return jsonify({'positions': positions, 'summary': summary})


def _classify_position_type(p):
    """position_type 反映"最近一笔 buy/sell 触发的状态":
      - 最近 buy + 之前已清仓 (从 trades 算出来) → 建仓
      - 最近 buy + 之前有底仓                       → 加仓
      - 最近 sell + 之后还有底仓                    → 减仓
      - 最近 sell + 之后卖完 (net=0)               → 清仓
    实现: 从 trades 查最近一笔的方向 + 之前的净持仓. DB 的 position_type 字段
    在整合脚本里已按此规则写入, 这里只是兜底. shares=0 → 清仓.
    """
    try:
        shares = p.get('shares') or 0
        if shares <= 0:
            return '清仓'
        # 优先用 DB hint (整合脚本算过的)
        db_type = p.get('position_type')
        if db_type in ('建仓', '加仓', '减仓'):
            return db_type
        # fallback: 用 original_shares 简单判定
        original = p.get('original_shares') or 0
        if original > 0 and shares < original - 0.0001:
            return '减仓'
        return '建仓'
    except Exception:
        return '建仓'


def _classify_closed_type(p):
    """卖出 tab 标签: 反映"当前是否还有剩余持仓"
      - 已清仓 (shares=0) → 清仓
      - 还持有部分 (shares>0, < original) → 减仓
    注意: 这跟 DB 里 position_type (按"最近一笔 buy/sell 触发") 是两套独立规则.
    卖出 tab 用这套 (按状态); 现持仓 tab 用 DB hint (按最近一笔).
    """
    try:
        shares = p.get('shares') or 0
        original = p.get('original_shares') or 0
        if shares < 0.5:
            return '清仓'
        if original > 0 and shares >= original - 0.5:
            return '清仓'
        return '减仓'
    except Exception:
        return '清仓'


@app.route('/api/positions', methods=['POST'])
def add_position():
    """手动添加一条持仓"""
    data = request.get_json() or {}
    ts_code = _normalize_code(data.get('ts_code'))
    if not ts_code:
        return jsonify({'status': 'error', 'message': '股票代码无效'}), 400
    try:
        shares = float(data.get('shares', 0))
        cost_price = float(data.get('cost_price', 0))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '数量/成本价必须是数字'}), 400
    if shares <= 0 or cost_price <= 0:
        return jsonify({'status': 'error', 'message': '数量/成本价必须大于 0'}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    action, new_id = _upsert_position(c, {
        'ts_code': ts_code,
        'name': (data.get('name') or '').strip(),
        'shares': shares,
        'cost_price': cost_price,
        'buy_date': (data.get('buy_date') or '').strip(),
        'note': (data.get('note') or '').strip(),
    })
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'id': new_id,
        'ts_code': ts_code,
        'action': action,  # 'inserted' 新建 / 'added' 加仓合并
    })


@app.route('/api/positions/<int:pid>', methods=['PUT'])
def update_position(pid):
    """更新单条持仓"""
    data = request.get_json() or {}
    fields = []
    values = []
    for k in ('name', 'shares', 'cost_price', 'buy_date', 'note'):
        if k in data:
            fields.append(f'{k}=?')
            values.append(data[k])
    if not fields:
        return jsonify({'status': 'error', 'message': '无更新字段'}), 400
    fields.append("updated_at=datetime('now', 'localtime')")
    values.append(pid)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f'UPDATE positions SET {", ".join(fields)} WHERE id=?', values)
    if c.rowcount == 0:
        conn.close()
        return jsonify({'status': 'error', 'message': '记录不存在'}), 404
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/positions/<int:pid>', methods=['DELETE'])
def delete_position(pid):
    """删除单条持仓"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM positions WHERE id=?', (pid,))
    if c.rowcount == 0:
        conn.close()
        return jsonify({'status': 'error', 'message': '记录不存在'}), 404
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/positions/clear', methods=['POST'])
def clear_positions():
    """清空所有持仓"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM positions')
    n = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'deleted': n})


@app.route('/api/positions/sell', methods=['GET'])
def get_sell_positions():
    """获取卖出列表: 只列"最近一笔交易是 sell"的 position
      - shares=0 (已清仓) → 清仓
      - shares>0 + 最近一笔是 sell → 减仓 (如 09992 港股)
    那些"曾经卖过但最近又买回来"的股票不出现, 因为用户视角它们现在不是在卖.
    标签用 DB 的 position_type 字段 (整合脚本按"最近一笔 buy/sell 触发"算的).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        """SELECT p.* FROM positions p
           WHERE p.position_type IN ('减仓', '清仓')
           ORDER BY
             CASE WHEN p.closed_at IS NOT NULL THEN p.closed_at
                  ELSE COALESCE(p.last_trade_date, p.last_add_date, p.buy_date, '')
             END DESC"""
    ).fetchall()
    conn.close()
    closed = []
    # 拉所有持仓的实时价 (用于"清仓后涨幅")
    ts_codes = [dict(r)['ts_code'] for r in rows]
    quote_map = fetch_sina_quotes(ts_codes) if ts_codes else {}
    # 按每只股票, 从 trades 表拉 sell 流水, 按笔算 sell_fees 再求和
    # (不能用 sell_amount 一次性算, 因为每笔都触发最低 5 元佣金)
    sell_fee_by_code = {}
    if ts_codes:
        conn2 = sqlite3.connect(DB_PATH)
        placeholders = ','.join('?' for _ in ts_codes)
        for r in conn2.execute(
            f"SELECT ts_code, amount FROM trades WHERE direction='sell' AND applied!=-1 AND ts_code IN ({placeholders})",
            ts_codes
        ).fetchall():
            code, amt = r[0], (r[1] or 0)
            fee = calc_sell_fees(amt, code)
            sell_fee_by_code[code] = sell_fee_by_code.get(code, 0) + fee
        conn2.close()

    for r in rows:
        p = dict(r)
        # 已实现盈亏/卖出额从 DB 字段读
        p['profit'] = p.get('realized_pnl') or 0
        p['sell_amount'] = p.get('total_sell_amount') or 0
        p['original_shares'] = p.get('original_shares') or 0
        p['cost_value'] = round(p['cost_price'] * p['original_shares'], 2)
        if p['cost_value']:
            p['profit_pct'] = round(p['profit'] / p['cost_value'] * 100, 3)
            p['avg_sell_price'] = round(p['sell_amount'] / p['original_shares'], 4) if p['original_shares'] else None
        else:
            p['profit_pct'] = None
            p['avg_sell_price'] = None
        # 卖出时的交易税费: 按每笔 sell trade 算 (每笔都触发最低 5 元佣金) 再求和
        p['sell_fees'] = round(sell_fee_by_code.get(p['ts_code'], 0), 2)
        p['includes_transfer'] = _is_shanghai(p['ts_code'])
        # 清仓后涨幅 = (现价 - 卖出均价) / 卖出均价 × 100
        cur_q = quote_map.get(p['ts_code'], {})
        cur_price = cur_q.get('price')
        if cur_price and p['avg_sell_price']:
            p['current_price'] = cur_price
            p['post_close_change_pct'] = round((cur_price - p['avg_sell_price']) / p['avg_sell_price'] * 100, 3)
        else:
            p['current_price'] = None
            p['post_close_change_pct'] = None
        # 仓位类型标签: 卖出 tab 内部一致 — shares<original → 减仓, shares=0 → 清仓
        # 这跟现持仓 tab 的"最近一笔 buy/sell 触发"是两套独立语义, 各管各的
        p['position_type'] = _classify_closed_type(p)
        closed.append(p)

    total_profit = sum(p['profit'] for p in closed)
    total_cost = sum(p['cost_value'] for p in closed)
    total_sell_fees = sum(p.get('sell_fees', 0) for p in closed)
    summary = {
        'count': len(closed),
        'total_realized_pnl': round(total_profit, 2),
        'total_cost': round(total_cost, 2),
        'total_sell_fees': round(total_sell_fees, 2),
        'total_pnl_pct': round(total_profit / total_cost * 100, 2) if total_cost else None,
    }
    return jsonify({'closed': closed, 'summary': summary})


@app.route('/api/positions/restore-closed/<int:pid>', methods=['POST'])
def restore_closed_position(pid):
    """把已清仓的记录恢复成持仓 (shares 用 original_shares 备份的值, 需手动核对)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute('SELECT shares, closed_at, total_sell_amount, realized_pnl FROM positions WHERE id=?', (pid,)).fetchone()
    if not row or not row[1]:
        conn.close()
        return jsonify({'status': 'error', 'message': '记录不是已清仓状态'}), 400
    c.execute('''UPDATE positions SET closed_at=NULL, shares=?, total_sell_amount=0, realized_pnl=0,
        updated_at=datetime('now','localtime') WHERE id=?''', (row[0] or 0, pid))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/positions/backfill-closed', methods=['POST'])
def backfill_closed_positions():
    """一次性回填: 从给定的资金股份 xls + 当日成交 xls 重建已清仓记录
    仅回填当前 DB 中不存在的 (ts_code, cost_price, buy_date) 组合
    """
    data = request.get_json() or {}
    capital_path = data.get('capital_path')
    trade_path = data.get('trade_path')
    if not capital_path or not trade_path:
        return jsonify({'status': 'error', 'message': '需要 capital_path 和 trade_path'}), 400
    if not os.path.exists(capital_path):
        return jsonify({'status': 'error', 'message': f'资金股份文件不存在: {capital_path}'}), 400
    if not os.path.exists(trade_path):
        return jsonify({'status': 'error', 'message': f'成交文件不存在: {trade_path}'}), 400

    cap_ext = os.path.splitext(capital_path)[1].lower()
    trd_ext = os.path.splitext(trade_path)[1].lower()

    try:
        cap_df = _read_uploaded_table(capital_path, cap_ext)
        trd_df = _read_uploaded_table(trade_path, trd_ext)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    cap_map, _ = _auto_map_columns(cap_df)
    trd_map, _ = _auto_map_trade_columns(trd_df)
    if not all(k in cap_map for k in ('ts_code', 'shares', 'cost_price')):
        return jsonify({'status': 'error', 'message': '资金股份缺少必需列'}), 400
    if not all(k in trd_map for k in ('ts_code', 'direction', 'price', 'shares')):
        return jsonify({'status': 'error', 'message': '成交表缺少必需列'}), 400

    cap_records = _parse_positions_from_df(cap_df, cap_map)
    trd_records = _parse_trades_from_df(trd_df, trd_map)

    # 按 ts_code 累计卖出 (用于计算 realized_pnl)
    sold_by_code = {}
    for t in trd_records:
        if t['direction'] == 'sell':
            sold_by_code.setdefault(t['ts_code'], {'shares': 0, 'amount': 0})
            sold_by_code[t['ts_code']]['shares'] += t['shares']
            sold_by_code[t['ts_code']]['amount'] += t['amount'] or (t['price'] * t['shares'])

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    inserted_open = 0
    inserted_closed = 0
    skipped_existing = 0
    today = datetime.now().strftime('%Y-%m-%d')
    for p in cap_records:
        existing = c.execute(
            'SELECT id FROM positions WHERE ts_code=? AND cost_price=? AND buy_date=?',
            (p['ts_code'], p['cost_price'], p['buy_date'])
        ).fetchone()
        if existing:
            skipped_existing += 1
            continue

        sold = sold_by_code.get(p['ts_code'])
        sell_shares = sold['shares'] if sold else 0

        if sell_shares >= p['shares'] - 0.0001:
            # 全部清仓
            avg_sell = (sold['amount'] / sold['shares']) if sold['shares'] > 0 else p['cost_price']
            realized_pnl = round((avg_sell - p['cost_price']) * p['shares'], 2)
            c.execute('''INSERT INTO positions
                (ts_code, name, shares, original_shares, cost_price, buy_date, note,
                 closed_at, total_sell_amount, realized_pnl)
                VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)''',
                (p['ts_code'], p['name'], p['shares'], p['cost_price'], p['buy_date'],
                 f'[回填] 成本{p["cost_price"]}×{p["shares"]}股, 均价{avg_sell:.2f}清仓',
                 today, round(sold['amount'], 2), realized_pnl))
            inserted_closed += 1
        else:
            # 现持仓 (部分减仓也算现持仓, 减仓数量记录在 note)
            remain = p['shares'] - sell_shares
            note = ''
            if sell_shares > 0:
                avg_sell = (sold['amount'] / sold['shares']) if sold['shares'] > 0 else 0
                note = f'[回填] 今日部分减仓 {int(sell_shares)}股 @均价{avg_sell:.2f}, 剩余{remain}股'
            c.execute('''INSERT INTO positions
                (ts_code, name, shares, original_shares, cost_price, buy_date, note,
                 total_sell_amount, realized_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (p['ts_code'], p['name'], remain, p['shares'], p['cost_price'], p['buy_date'],
                 note, round(sold['amount'], 2) if sold else 0,
                 round((sold['amount'] / sold['shares'] - p['cost_price']) * sell_shares, 2) if sold and sold['shares'] > 0 else 0))
            inserted_open += 1

    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'inserted_open': inserted_open,
        'inserted_closed': inserted_closed,
        'skipped_existing': skipped_existing,
    })


# ============ 成交明细管理 (中信「当日成交」导入) ============

TRADE_COLUMN_ALIASES = {
    'ts_code': ['证券代码', '代码', 'code'],
    'name': ['证券名称', '名称', 'name'],
    'direction': ['买卖标志', '买卖方向', '委托方向', '方向', 'direction'],
    'price': ['成交价格', '价格', '成交均价', 'price'],
    'shares': ['成交数量', '数量', '股数', 'shares'],
    'amount': ['成交金额', '发生金额', '金额', 'amount'],
    'trade_time': ['成交时间', '时间', 'time'],
    'trade_date': ['成交日期', '日期', 'date'],
    'trade_no': ['成交编号', '合同号', 'trade_no', 'order_no'],
    'note': ['备注', 'note', 'memo'],
}


def _normalize_direction(raw):
    s = _clean_cell(raw)
    if not s:
        return None
    if any(k in s for k in ('买', 'B', 'b', 'Buy', 'BUY')):
        return 'buy'
    if any(k in s for k in ('卖', 'S', 's', 'Sell', 'SELL')):
        return 'sell'
    return None


def _auto_map_trade_columns(df):
    """成交表的列名识别"""
    cols = list(df.columns)
    mapping = {}
    used = set()
    for canonical, aliases in TRADE_COLUMN_ALIASES.items():
        for alias in aliases:
            match = None
            if alias in cols and alias not in used:
                match = alias
            else:
                for c in cols:
                    if c in used:
                        continue
                    if alias and alias in c:
                        match = c
                        break
            if match:
                mapping[canonical] = match
                used.add(match)
                break
    unmapped = [c for c in cols if c not in used]
    return mapping, unmapped


def _parse_trades_from_df(df, column_map):
    records = []
    for _, row in df.iterrows():
        raw_code = _clean_cell(row.get(column_map.get('ts_code')))
        if not raw_code:
            continue
        ts_code = _normalize_code(raw_code)
        if not ts_code:
            continue

        direction = _normalize_direction(row.get(column_map.get('direction')))
        if not direction:
            continue

        try:
            price = float(_clean_cell(row[column_map['price']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if price <= 0:
            continue

        try:
            shares = float(_clean_cell(row[column_map['shares']]).replace(',', ''))
        except (KeyError, ValueError, TypeError):
            continue
        if shares <= 0:
            continue

        try:
            amount = float(_clean_cell(row[column_map['amount']]).replace(',', ''))
        except (KeyError, ValueError, TypeError, KeyError):
            amount = round(price * shares, 2)

        name = _clean_cell(row.get(column_map.get('name', ''), ''))
        trade_time = _clean_cell(row.get(column_map.get('trade_time', ''), ''))
        trade_date = _clean_cell(row.get(column_map.get('trade_date', ''), ''))
        # 归一化: 20260608 → 2026-06-08 (加横线), 跟 DB 现有格式保持一致
        trade_date = trade_date.replace('-', '').replace('/', '')
        if len(trade_date) == 8:
            trade_date = f'{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}'
        trade_no = _clean_cell(row.get(column_map.get('trade_no', ''), ''))
        note = _clean_cell(row.get(column_map.get('note', ''), ''))

        records.append({
            'trade_no': trade_no,
            'ts_code': ts_code,
            'name': name,
            'direction': direction,
            'price': price,
            'shares': shares,
            'amount': amount,
            'trade_date': trade_date,
            'trade_time': trade_time,
            'note': note,
        })
    return records


@app.route('/api/trades/preview', methods=['POST'])
def preview_trades_file():
    """解析成交表, 返回识别结果 + 去重检查"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'status': 'error', 'message': '请上传文件'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.csv', '.xlsx', '.xls'):
        return jsonify({'status': 'error', 'message': f'不支持的文件格式: {ext}'}), 400

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    import uuid
    tmp_id = str(uuid.uuid4())[:8]
    safe_name = f'trades_{tmp_id}{ext}'
    filepath = os.path.join(upload_dir, safe_name)
    f.save(filepath)

    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        try: os.remove(filepath)
        except: pass
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, unmapped = _auto_map_trade_columns(df)
    missing = [k for k in ('ts_code', 'direction', 'price', 'shares') if k not in mapping]

    records = []
    if not missing:
        records = _parse_trades_from_df(df, mapping)

    # 查重 + 持仓一致性: 检测已 applied 的记录是否在持仓的 note 中还留有痕迹
    # 真重复按 UNIQUE(trade_no, ts_code, trade_time) 三元组判定, 避免分笔成交被误判
    conn = sqlite3.connect(DB_PATH)
    existing_map = {}  # (trade_no, ts_code, trade_time) -> {id, applied}
    if records:
        trade_nos = sorted({r['trade_no'] for r in records if r['trade_no']})
        if trade_nos:
            q = ','.join('?' * len(trade_nos))
            for r in conn.execute(
                f'SELECT id, trade_no, ts_code, trade_time, applied FROM trades WHERE trade_no IN ({q})',
                trade_nos).fetchall():
                key = (r[1] or '', r[2] or '', r[3] or '')
                existing_map[key] = {'id': r[0], 'ts_code': r[2], 'applied': r[4]}
        # 抓取这批 ts_code 涉及的所有持仓 note, 用于判断 trade_no 是否在 note 中
        ts_codes = sorted({r['ts_code'] for r in records})
        if ts_codes:
            q2 = ','.join('?' * len(ts_codes))
            all_notes = '\n'.join(r[0] or '' for r in conn.execute(
                f'SELECT note FROM positions WHERE ts_code IN ({q2})', ts_codes).fetchall())
        else:
            all_notes = ''
    else:
        all_notes = ''
    conn.close()

    duplicate_count = 0
    stale_count = 0
    for r in records:
        key = (r['trade_no'] or '', r['ts_code'] or '', r.get('trade_time') or '')
        info = existing_map.get(key)
        if info:
            duplicate_count += 1
            r['existing_id'] = info['id']
            r['already_applied'] = info['applied'] == 1
            # stale: 已 applied, 但对应持仓的 note 中找不到该 trade_no (持仓被删/被改)
            r['stale'] = info['applied'] == 1 and r['trade_no'] and f"#{r['trade_no']}" not in all_notes
            if r['stale']:
                stale_count += 1
        else:
            r['existing_id'] = None
            r['already_applied'] = False
            r['stale'] = False
    new_count = len(records) - duplicate_count
    buy_count = sum(1 for r in records if r['direction'] == 'buy')
    sell_count = sum(1 for r in records if r['direction'] == 'sell')

    return jsonify({
        'status': 'success',
        'filename': safe_name,
        'total_rows': len(df),
        'columns': list(df.columns),
        'column_map': mapping,
        'unmapped_columns': unmapped,
        'missing_required': missing,
        'parsed_count': len(records),
        'new_count': new_count,
        'duplicate_count': duplicate_count,
        'stale_count': stale_count,
        'buy_count': buy_count,
        'sell_count': sell_count,
        'preview': records[:20],
    })


@app.route('/api/trades/import', methods=['POST'])
def import_trades():
    data = request.get_json() or {}
    filename = data.get('filename')
    column_map_override = data.get('column_map')
    if not filename:
        return jsonify({'status': 'error', 'message': '缺少 filename'}), 400
    filepath = os.path.join(os.path.dirname(__file__), 'uploads', filename)
    if not os.path.exists(filepath):
        return jsonify({'status': 'error', 'message': '文件已失效, 请重新上传'}), 400

    ext = os.path.splitext(filename)[1].lower()
    try:
        df = _read_uploaded_table(filepath, ext)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'文件解析失败: {e}'}), 400

    mapping, _ = _auto_map_trade_columns(df)
    if column_map_override:
        mapping.update(column_map_override)

    missing = [k for k in ('ts_code', 'direction', 'price', 'shares') if k not in mapping]
    if missing:
        return jsonify({'status': 'error', 'message': f'缺少必需列: {missing}'}), 400

    records = _parse_trades_from_df(df, mapping)
    if not records:
        return jsonify({'status': 'error', 'message': '未解析出任何有效成交'}), 400

    # 合并分笔成交: 同 (trade_no, ts_code, trade_time, direction, price) 视为同一次委托的分次成交, 累加 shares/amount
    merged = {}
    for r in records:
        key = (r['trade_no'], r['ts_code'], r.get('trade_time', ''), r['direction'], r['price'])
        if key in merged:
            merged[key]['shares'] = (merged[key].get('shares') or 0) + (r.get('shares') or 0)
            merged[key]['amount'] = (merged[key].get('amount') or 0) + (r.get('amount') or 0)
        else:
            merged[key] = dict(r)
    merged_records = list(merged.values())
    merge_count = len(records) - len(merged_records)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    inserted = 0
    duplicate = 0
    skipped = 0
    for r in merged_records:
        try:
            cur = c.execute('''INSERT INTO trades
                (trade_no, ts_code, name, direction, price, shares, amount, trade_date, trade_time, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (r['trade_no'], r['ts_code'], r['name'], r['direction'],
                 r['price'], r['shares'], r['amount'], r['trade_date'],
                 r['trade_time'], r['note']))
            if cur.rowcount == 1:
                inserted += 1
        except sqlite3.IntegrityError:
            duplicate += 1
        except Exception as e:
            print(f'trades 写入错误: {e}')
            skipped += 1
    conn.commit()
    conn.close()

    try: os.remove(filepath)
    except: pass

    return jsonify({
        'status': 'success',
        'inserted': inserted,
        'duplicate': duplicate,
        'skipped': skipped,
        'total': len(merged_records),
        'merged_from': len(records),
    })


@app.route('/api/trades', methods=['GET'])
def list_trades():
    """交易记录查询
    Query:
      ts_code     股票代码 (允许 600519 / 600519.SH, 自动归一化)
      direction   'buy' / 'sell' / 空=全部
      applied     0/1/-1 / 空=全部 (-1 软删除默认排除)
      include_deleted 1 时把 applied=-1 也返回
    返回: {trades, count, summary?}
      summary 仅在指定 ts_code 时计算:
        hold_days        持仓天数 (首笔 buy → 今天/最后清仓)
        total_buy_amount 累计买入金额
        total_sell_amount 累计卖出金额
        total_buy_fees   累计买入费
        total_sell_fees  累计卖出费
        total_fees       累计税费合计
        realized_pnl     已实现盈亏 (FIFO 简化, 扣买卖费)
        buy_count / sell_count 笔数
    """
    from datetime import date as _date
    args = request.args
    where, params = [], []

    if args.get('direction') in ('buy', 'sell'):
        where.append('direction = ?'); params.append(args['direction'])
    include_deleted = args.get('include_deleted') == '1'
    if args.get('applied') in ('0', '1', '-1'):
        where.append('applied = ?'); params.append(int(args['applied']))
    elif not include_deleted:
        where.append('applied != -1')

    ts_code_query = None
    if args.get('ts_code'):
        raw = args['ts_code'].strip().upper()
        if '.' not in raw and raw.isdigit() and len(raw) == 6:
            # 600519 → 600519.SH / .SZ / .BJ 都匹配
            where.append('(ts_code = ? OR ts_code LIKE ?)')
            params.extend([raw + '.%', raw + '.%'])
        else:
            where.append('ts_code = ?'); params.append(raw)
        # 归一化成完整代码, 给 summary 用 (取 DB 里第一条出现的形式)
        ts_code_query = raw if '.' in raw else (raw + '.SH')

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        f'SELECT * FROM trades{where_sql} ORDER BY trade_date DESC, trade_time DESC, id DESC',
        params
    ).fetchall()
    trades = [dict(r) for r in rows]
    conn.close()

    summary = None
    if ts_code_query and trades:
        # 重新打开查归一化代码 (用户传 600519 时归一化成 DB 里的形式)
        conn2 = sqlite3.connect(DB_PATH)
        conn2.row_factory = sqlite3.Row
        code_row = conn2.execute(
            "SELECT ts_code FROM trades WHERE ts_code LIKE ? OR ts_code = ? LIMIT 1",
            (ts_code_query.split('.')[0] + '.%', ts_code_query)
        ).fetchone()
        actual_code = code_row['ts_code'] if code_row else ts_code_query
        conn2.close()

        buys  = [t for t in trades if t['direction'] == 'buy'  and t.get('applied', 0) != -1]
        sells = [t for t in trades if t['direction'] == 'sell' and t.get('applied', 0) != -1]

        total_buy  = sum((b.get('amount') or b['price'] * b['shares']) for b in buys)
        total_sell = sum((s.get('amount') or s['price'] * s['shares']) for s in sells)
        total_buy_fees  = sum(calc_buy_fees(b.get('amount')  or b['price'] * b['shares'], b.get('ts_code','')) for b in buys)
        total_sell_fees = sum(calc_sell_fees(s.get('amount') or s['price'] * s['shares'], s.get('ts_code','')) for s in sells)

        # 持仓天数: 首笔 buy → 今天 (未清仓) 或最后清仓 sell
        hold_days = 0
        if buys:
            first_buy = min(b['trade_date'] for b in buys if b.get('trade_date'))
            if not first_buy:
                first_buy = min(b['id'] for b in buys)  # 兜底
            last_sell = max((s['trade_date'] for s in sells if s.get('trade_date')), default=None)
            def _parse(d):
                if not d or len(d) < 8: return None
                try: return _date(int(d[:4]), int(d[4:6]), int(d[6:8]))
                except Exception: return None
            fb = _parse(first_buy)
            if fb:
                end_d = _parse(last_sell) if last_sell else _date.today()
                if end_d and end_d >= fb:
                    hold_days = (end_d - fb).days

        # 已实现盈亏: FIFO 简化
        # 队列每项: [cost_per_share_incl_buy_fee, remaining_shares]
        buy_queue = []
        # 按 id 升序 (时间) 累加
        for b in sorted(buys, key=lambda x: (x.get('trade_date') or '', x.get('trade_time') or '', x['id'])):
            amt = b.get('amount') or b['price'] * b['shares']
            fee = calc_buy_fees(amt, b.get('ts_code',''))
            cps = (amt + fee) / b['shares'] if b['shares'] else b['price']  # 每股含买入费
            buy_queue.append([cps, b['shares']])
        realized = 0.0
        for s in sorted(sells, key=lambda x: (x.get('trade_date') or '', x.get('trade_time') or '', x['id'])):
            amt = s.get('amount') or s['price'] * s['shares']
            fee = calc_sell_fees(amt, s.get('ts_code',''))
            sps = (amt - fee) / s['shares'] if s['shares'] else s['price']  # 卖出每股净额
            remaining = s['shares']
            while remaining > 0 and buy_queue:
                head = buy_queue[0]
                take = min(head[1], remaining)
                realized += (sps - head[0]) * take
                head[1] -= take
                remaining -= take
                if head[1] <= 1e-9:
                    buy_queue.pop(0)

        summary = {
            'ts_code': actual_code,
            'hold_days': hold_days,
            'buy_count': len(buys),
            'sell_count': len(sells),
            'total_buy_amount': round(total_buy, 2),
            'total_sell_amount': round(total_sell, 2),
            'total_buy_fees': round(total_buy_fees, 2),
            'total_sell_fees': round(total_sell_fees, 2),
            'total_fees': round(total_buy_fees + total_sell_fees, 2),
            'realized_pnl': round(realized, 2),
        }

    return jsonify({
        'trades': trades,
        'count': len(trades),
        'summary': summary,
    })


@app.route('/api/trades/all_summary', methods=['GET'])
def list_all_trades_summary():
    """批量返回所有 ts_code 的 {trades, summary, summary_pos}.
    前端切到 buys/sells/positions tab 时预拉一次, 展开子表零延迟.
    用未 round 中间值的 FIFO 严格算法 (跟中信证券对账).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # 拉所有 trades (按 ts_code 索引)
    all_rows = c.execute(
        """SELECT id, ts_code, direction, price, shares, amount, trade_date, trade_time, applied
           FROM trades
           WHERE direction IN ('buy','sell','bonus','transfer_out','dividend','tax') AND applied != -1
           ORDER BY ts_code, trade_date, trade_time, trade_no, id"""
    ).fetchall()
    # 拉所有 positions (summary_pos) — 在 conn close 之前
    pos_rows = c.execute("SELECT ts_code, shares, cost_price, position_type, closed_at, total_sell_amount, realized_pnl FROM positions").fetchall()
    pos_map = {r['ts_code']: dict(r) for r in pos_rows}
    conn.close()

    trades_by_code = defaultdict(list)
    for r in all_rows:
        trades_by_code[r['ts_code']].append(dict(r))

    result = []
    for code, ts_list in trades_by_code.items():
        ts_list.sort(key=lambda x: (x['trade_date'], x['trade_time'], x.get('id', 0)))
        # FIFO 队列: (raw_cps, remaining, buy_date) — 跟踪每项的买入日期, 判定是否享受 dividend
        buy_queue = []
        pending_net_div = []  # [(amount, apply_date), ...] — 正数=减成本, 负数=加回成本 (保留兼容, 但实际用 ts_list 扫)
        net_shares = 0.0
        total_buy_amount = 0.0
        total_sell_amount = 0.0
        total_buy_fees = 0.0
        total_sell_fees = 0.0
        realized_pnl = 0.0
        first_buy_date = None
        last_sell_date = None
        for t in ts_list:
            amt = t.get('amount') or 0
            sh = t.get('shares') or 0
            if t['direction'] == 'buy':
                bf = calc_buy_fees(amt, code)
                raw_cps = (amt + bf) / sh if sh else t['price']
                buy_queue.append([raw_cps, sh, t['trade_date']])
                total_buy_amount += amt
                total_buy_fees += bf
                net_shares += sh
                if not first_buy_date: first_buy_date = t['trade_date']
            elif t['direction'] == 'sell':
                sf = calc_sell_fees(amt, code)
                raw_sps = (amt - sf) / sh if sh else t['price']
                total_sell_amount += amt
                total_sell_fees += sf
                net_shares -= sh
                last_sell_date = t['trade_date']
                # 同日合并: 扫 ts_list 里 ≤ sell_date 的所有 div/tax (按 date 聚合, 不看 time)
                net_div_total = 0
                max_div_date = None  # 严格按 div_date (不含 tax) 判定
                for prior in ts_list:
                    if prior['trade_date'] > t['trade_date']:
                        break
                    if prior['direction'] == 'dividend':
                        net_div_total += prior.get('amount') or 0
                        if max_div_date is None or prior['trade_date'] > max_div_date:
                            max_div_date = prior['trade_date']
                    elif prior['direction'] == 'tax':
                        net_div_total -= prior.get('amount') or 0
                        # tax 不参与 max_div_date (避免把 tax_date 算作 div_date)
                pending_net_div = [x for x in pending_net_div if x[1] > t['trade_date']]
                if net_div_total != 0 and max_div_date is not None:
                    eligible = sum(q[1] for q in buy_queue if q[2] <= max_div_date and q[1] > 0)
                    if eligible > 0:
                        per_share = net_div_total / eligible
                        for q in buy_queue:
                            if q[2] <= max_div_date and q[1] > 0:
                                q[0] = q[0] - per_share
                remaining = sh
                while remaining > 0 and buy_queue:
                    head = buy_queue[0]
                    take = min(head[1], remaining)
                    seg = _round_half_up((raw_sps - head[0]) * take)
                    realized_pnl = _round_half_up(realized_pnl + seg)
                    head[1] -= take
                    remaining -= take
                    if head[1] < 1e-9: buy_queue.pop(0)
            elif t['direction'] == 'bonus':
                net_shares += sh
                buy_queue.append([0.0, sh, t['trade_date']])
            elif t['direction'] == 'transfer_out':
                net_shares -= sh
                remaining = sh
                while remaining > 0 and buy_queue:
                    head = buy_queue[0]
                    take = min(head[1], remaining)
                    head[1] -= take
                    remaining -= take
                    if head[1] < 1e-9: buy_queue.pop(0)
        # 持仓天数 (dividend/tax 已由 sell 块按"同日合并"处理, 这里不再摊销)
        hold_days = 0
        if first_buy_date:
            from datetime import datetime, date
            try:
                fb = datetime.strptime(first_buy_date, '%Y%m%d').date()
                end_d = datetime.strptime(last_sell_date, '%Y%m%d').date() if last_sell_date else date.today()
                hold_days = max(0, (end_d - fb).days)
            except Exception:
                pass
        summary = {
            'buy_count': sum(1 for t in ts_list if t['direction'] == 'buy'),
            'sell_count': sum(1 for t in ts_list if t['direction'] == 'sell'),
            'total_buy_amount': round(total_buy_amount, 2),
            'total_sell_amount': round(total_sell_amount, 2),
            'total_buy_fees': round(total_buy_fees, 2),
            'total_sell_fees': round(total_sell_fees, 2),
            'total_fees': round(total_buy_fees + total_sell_fees, 2),
            'realized_pnl': round(realized_pnl, 2),
            'hold_days': hold_days,
        }
        # summary_pos 关联现持仓 (扣分红摊到 cost 的精确 cost)
        summary_pos = pos_map.get(code) or None
        # ts_list 给前端 (按时间倒序, 用于子表)
        trades_sorted = sorted(ts_list, key=lambda x: (x['trade_date'], x.get('trade_time', '')), reverse=True)
        result.append({
            'ts_code': code,
            'trades': trades_sorted,
            'summary': summary,
            'summary_pos': summary_pos,
        })
    return jsonify(result)


@app.route('/api/trades/buys', methods=['GET'])
def list_buy_trades():
    """获取所有买入成交明细 (trades 表 direction='buy'), 给持仓 Tab 的「买入」用
    每笔 buy 的 position_status 跟现持仓 tab 保持一致: 直接读对应 position 的 position_type
    (建仓 / 加仓 / 减仓). 仓位已清仓时也用其最终 type.
    按 trade_date DESC 展示, 最新的在前
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # LEFT JOIN positions on ts_code, 只取 active (未清仓) 的那条
    rows = conn.execute(
        """SELECT t.*, p.position_type AS pos_type, p.shares AS pos_shares,
                  p.original_shares AS pos_original_shares, p.closed_at AS pos_closed_at
           FROM trades t
           LEFT JOIN positions p
             ON p.ts_code = t.ts_code AND p.closed_at IS NULL
           WHERE t.direction = 'buy'
           ORDER BY t.trade_date DESC, t.trade_time DESC, t.id DESC"""
    ).fetchall()
    conn.close()
    buys = [dict(r) for r in rows]

    # 现价: 优先 fetch_sina_quotes 实时拉 (反映当下涨跌), fallback 到 stock_daily 最新 close
    quote_map = {}
    if buys:
        # 1. fetch_sina_quotes 实时 (去重 + 分批 50, 避免 URL 太长)
        unique_codes = list({b['ts_code'] for b in buys})
        BATCH = 50
        for i in range(0, len(unique_codes), BATCH):
            batch = unique_codes[i:i+BATCH]
            try:
                quote_map.update(fetch_sina_quotes(batch))
            except Exception:
                pass
        # 2. fallback: stock_daily 最新 close (给 fetch_sina 拿不到的补底)
        missing = [c for c in unique_codes if c not in quote_map]
        if missing:
            conn2 = sqlite3.connect(DB_PATH)
            conn2.row_factory = sqlite3.Row
            placeholders = ','.join('?' for _ in missing)
            for r in conn2.execute(
                f"""SELECT ts_code, close FROM stock_daily
                    WHERE ts_code IN ({placeholders})
                      AND trade_date = (SELECT MAX(trade_date) FROM stock_daily
                                        WHERE ts_code = stock_daily.ts_code)""",
                missing).fetchall():
                quote_map[r['ts_code']] = {'price': r['close']}
            conn2.close()

    # 用现持仓同样的 _classify_position_type 逻辑, 跟 dashboard 现持仓 tab 完全一致
    for b in buys:
        # 构造跟 get_positions 一样的 p 字典, 让 _classify_position_type 能直接处理
        p = {
            'shares': b.get('pos_shares'),
            'original_shares': b.get('pos_original_shares'),
            'position_type': b.get('pos_type'),
            'note': b.get('note'),
        }
        # 仓位不存在 (LEFT JOIN 没匹配) → 默认建仓
        if p.get('shares') is None:
            b['position_status'] = '建仓'
        else:
            b['position_status'] = _classify_position_type(p)
        # 买入交易税费 (佣金 max(额×万1, 5) + 过户费 额×万0.1, 跟现持仓 tooltip 同公式)
        buy_amt = b.get('amount') or (b.get('price', 0) * b.get('shares', 0))
        b['buy_fees'] = round(calc_buy_fees(buy_amt, b.get('ts_code','')), 2) if buy_amt > 0 else 0.0
        # 买入后涨幅 = (现价 - 买入价) / 买入价 × 100
        cur_price = quote_map.get(b['ts_code'], {}).get('price')
        if cur_price and b.get('price'):
            b['current_price'] = cur_price
            b['post_buy_change_pct'] = round((cur_price - b['price']) / b['price'] * 100, 3)
        else:
            b['current_price'] = None
            b['post_buy_change_pct'] = None

    total_amount = sum((b.get('amount') or (b.get('price', 0) * b.get('shares', 0))) for b in buys)
    total_shares = sum(b.get('shares') or 0 for b in buys)
    total_buy_fees = sum(b.get('buy_fees', 0) for b in buys)
    summary = {
        'count': len(buys),
        'total_amount': round(total_amount, 2),
        'total_buy_fees': round(total_buy_fees, 2),
        'total_shares': round(total_shares, 0),
        'unique_codes': len(set(b['ts_code'] for b in buys)),
    }
    return jsonify({'buys': buys, 'summary': summary})


@app.route('/api/trades/sells', methods=['GET'])
def list_sell_trades():
    """获取所有卖出成交明细 (trades 表 direction='sell'), 给持仓 Tab 的「卖出」用
    跟 buys 对应: 一行 = 一笔 sell.
    单笔 realized_pnl 用 FIFO 精确算: 维护每只股票的 buy 队列, 按时间 sell 段扣.
    跟中信证券交割单对账: cps/sps 不预先 round, 段用未 round 中间值算, 段结果 round(2).
    拉行情算"卖出后涨幅" (现价 vs 卖价)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 拉所有 trades (按 ts_code 分组排序, 用于 FIFO 计算) — 含 dividend/tax 用来摊销
    all_trades = conn.execute(
        """SELECT id, ts_code, direction, price, shares, amount, trade_date, trade_time
           FROM trades
           WHERE direction IN ('buy','sell','bonus','transfer_out','dividend','tax') AND applied != -1
           ORDER BY ts_code, trade_date, trade_time, trade_no, id"""
    ).fetchall()
    # 同时拉所有 sell (返回行)
    sell_rows = conn.execute(
        """SELECT t.*, p.position_type AS pos_type, p.shares AS pos_shares,
                  p.cost_price AS pos_cost, p.original_shares AS pos_original_shares,
                  p.closed_at AS pos_closed_at
           FROM trades t
           LEFT JOIN positions p
             ON p.ts_code = t.ts_code
                AND p.id = (SELECT id FROM positions WHERE ts_code = t.ts_code
                            ORDER BY (closed_at IS NULL) DESC, closed_at DESC LIMIT 1)
           WHERE t.direction = 'sell' AND t.applied != -1
           ORDER BY t.trade_date DESC, t.trade_time DESC, t.id DESC"""
    ).fetchall()
    conn.close()
    sells = [dict(r) for r in sell_rows]
    # 去重 + 分批: 新浪对单次 URL 长度有限, 超 50 个容易失败
    unique_codes = list({s['ts_code'] for s in sells})
    quote_map = {}
    BATCH = 50
    for i in range(0, len(unique_codes), BATCH):
        batch = unique_codes[i:i+BATCH]
        quote_map.update(fetch_sina_quotes(batch))

    # 按 FIFO 算每只股票的每笔 sell 的 realized_pnl
    # 维护每只股票的 buy_queue: [[raw_cps, remaining_shares], ...]
    # 按时间顺序遍历该股票所有 buy/sell/bonus/transfer, 同时给匹配上的 sell 算段
    fifo_queues = {}  # ts_code -> [(raw_cps, remaining), ...]
    # 按 ts_code 索引所有 sell (按时间正序, 方便 FIFO 处理)
    all_sells_by_code = defaultdict(list)
    for s in sells:
        all_sells_by_code[s['ts_code']].append(s)
    for code in all_sells_by_code:
        all_sells_by_code[code].sort(key=lambda x: (x['trade_date'], x['trade_time'], x.get('trade_no',''), x['id']))

    # 遍历所有 trades, 维护 FIFO 队列
    trade_cols = ['id','ts_code','direction','price','shares','amount','trade_date','trade_time']
    # 按 ts_code 索引所有 trades (含 dividend/tax 也用来摊销)
    all_trades_full = conn2.execute(
        """SELECT id, ts_code, direction, price, shares, amount, trade_date, trade_time
           FROM trades
           WHERE direction IN ('buy','sell','bonus','transfer_out','dividend','tax') AND applied != -1
           ORDER BY ts_code, trade_date, trade_time, trade_no, id"""
    ).fetchall() if False else all_trades  # 复用上面的
    trades_by_code = defaultdict(list)
    for r in all_trades_full:
        trades_by_code[r[1]].append(dict(zip(trade_cols, r)))
    for code, ts_list in trades_by_code.items():
        ts_list.sort(key=lambda x: (x['trade_date'], x['trade_time'], x['id']))

    for code, ts_list in trades_by_code.items():
        queue = []  # [(raw_cps, remaining, buy_date)]
        # 维护"待摊销净分红"：dividend amount 加, tax amount 减
        # 避免分开算两次舍入误差
        pending_net_div = []  # [(amount, apply_date), ...] — 正数=减成本, 负数=加回成本
        for t in ts_list:
            amt = t['amount'] or 0
            sh = t['shares'] or 0
            if t['direction'] == 'buy':
                bf = calc_buy_fees(amt, code)
                raw_cps = (amt + bf) / sh if sh else t['price']
                queue.append([raw_cps, sh, t['trade_date']])
            elif t['direction'] == 'sell':
                sf = calc_sell_fees(amt, code)
                raw_sps = (amt - sf) / sh if sh else t['price']
                # 同日合并: 直接扫 trades 列表里 ≤ sell_date 的所有 div/tax (按 date 聚合, 不看 time)
                # 因为 trades 列表按 (date, time) 排, sell 可能在同日 div/tax 之前
                net_div_total = 0
                max_div_date = None
                for prior in ts_list:  # ts_list 是当前股票的完整排序列表
                    if prior['trade_date'] > t['trade_date']:
                        break
                    if prior['direction'] == 'dividend':
                        net_div_total += prior.get('amount') or 0
                        if max_div_date is None or prior['trade_date'] > max_div_date:
                            max_div_date = prior['trade_date']
                    elif prior['direction'] == 'tax':
                        net_div_total -= prior.get('amount') or 0
                        if max_div_date is None or prior['trade_date'] > max_div_date:
                            max_div_date = prior['trade_date']
                # 清掉 pending (因为 sell 处理完已经包含所有)
                pending_net_div = [x for x in pending_net_div if x[1] > t['trade_date']]
                if net_div_total != 0 and max_div_date is not None:
                    eligible = sum(q[1] for q in queue if q[2] <= max_div_date and q[1] > 0)
                    if eligible > 0:
                        per_share = net_div_total / eligible
                        for q in queue:
                            if q[2] <= max_div_date and q[1] > 0:
                                q[0] = q[0] - per_share
                seg_total = 0.0
                remaining = sh
                while remaining > 0 and queue:
                    head = queue[0]
                    take = min(head[1], remaining)
                    seg = _round_half_up((raw_sps - head[0]) * take)
                    seg_total = _round_half_up(seg_total + seg)
                    head[1] -= take
                    remaining -= take
                    if head[1] < 1e-9: queue.pop(0)
                for s in all_sells_by_code.get(code, []):
                    if s['trade_date'] == t['trade_date'] and (s.get('trade_time') or '')[:8] == (t['trade_time'] or '')[:8] and abs(s['shares'] - sh) < 0.01 and s.get('realized_pnl') is None:
                        s['realized_pnl'] = seg_total
                        break
            elif t['direction'] == 'bonus':
                queue.append([0.0, sh, t['trade_date']])
            elif t['direction'] == 'transfer_out':
                remaining = sh
                while remaining > 0 and queue:
                    head = queue[0]
                    take = min(head[1], remaining)
                    head[1] -= take
                    remaining -= take
                    if head[1] < 1e-9: queue.pop(0)
            elif t['direction'] == 'dividend':
                # 现金分红: 净派息 (amount - 后续 tax)
                if amt > 0:
                    pending_net_div.append((amt, t['trade_date']))
            elif t['direction'] == 'tax':
                # 股息红利税补缴: 减回去 (负 net)
                if amt > 0:
                    pending_net_div.append((-amt, t['trade_date']))

    # 填其他字段 (sell_fees, post_sell_change_pct, is_closed)
    for s in sells:
        if s.get('realized_pnl') is None:
            s['realized_pnl'] = None  # 真算不出 (没匹配上或无 buy)
        sell_amt = s.get('amount') or (s.get('price', 0) * s.get('shares', 0))
        s['sell_fees'] = calc_sell_fees(sell_amt, s.get('ts_code','')) if sell_amt > 0 else 0.0
        cur = quote_map.get(s['ts_code'], {}).get('price')
        if cur and s.get('price'):
            s['current_price'] = cur
            s['post_sell_change_pct'] = round((cur - s['price']) / s['price'] * 100, 3)
        else:
            s['current_price'] = None
            s['post_sell_change_pct'] = None
        s['is_closed'] = bool(s.get('pos_closed_at'))

    total_sell_amount = sum((s.get('amount') or (s.get('price', 0) * s.get('shares', 0))) for s in sells)
    total_sell_shares = sum(s.get('shares') or 0 for s in sells)
    total_sell_fees = sum(s.get('sell_fees', 0) for s in sells)
    # 累计已实现盈亏: 只统计 FIFO 算出来有值的 (没匹配上或无 buy 的卖单 None 不计)
    total_realized_pnl = sum((s.get('realized_pnl') or 0) for s in sells)
    summary = {
        'count': len(sells),
        'total_sell_amount': round(total_sell_amount, 2),
        'total_sell_fees': round(total_sell_fees, 2),
        'total_sell_shares': round(total_sell_shares, 0),
        'total_realized_pnl': round(total_realized_pnl, 2),
        'unique_codes': len(set(s['ts_code'] for s in sells)),
    }
    return jsonify({'sells': sells, 'summary': summary})


def _apply_one_trade(c, trade, closed_at_expr="datetime('now','localtime')"):
    """把单条成交应用到 positions, 返回 (status, msg)
    status: 'ok' | 'oversell' | 'missing'
    closed_at_expr: SQL 表达式, 关闭仓位时写入 closed_at"""
    # 归一化 trade_date 到 'YYYY-MM-DD' (写入 positions 的 buy_date / last_trade_date)
    if trade.get('trade_date'):
        trade['trade_date'] = _norm_date_str(trade['trade_date'])
    if trade['direction'] == 'buy':
        # 找同 ts_code 未清仓的仓位 (按买入时间最早优先合并, 与 _upsert_position 一致)
        row = c.execute(
            '''SELECT id, shares, cost_price, buy_date FROM positions
               WHERE ts_code=? AND closed_at IS NULL
               ORDER BY (buy_date IS NULL), buy_date, id
               LIMIT 1''',
            (trade['ts_code'],)).fetchone()
        ts = trade.get('trade_date') or trade.get('trade_time') or ''
        new_note = f"[{ts} 买入 {trade['shares']}股 @{trade['price']} #{trade['trade_no']}]"
        # trade['price'] 是成交价 (excl fees). 持仓成本价 = 成交价 + 买入费/股
        trade_amount = trade['price'] * trade['shares']
        trade_buy_fee = calc_buy_fees(trade_amount, trade.get('ts_code',''))
        trade_cost_per_share = round((trade_amount + trade_buy_fee) / trade['shares'], 4)
        if row:
            old_id, old_shares, old_cost, old_buy_date = row
            old_shares = old_shares or 0
            old_cost = old_cost or 0
            new_shares = old_shares + trade['shares']
            # 加权平均成本价 (持仓成本价口径, 已含买入费)
            new_cost = (old_shares * old_cost + trade['shares'] * trade_cost_per_share) / new_shares
            # 交易日期取最新 (加仓仓位显示最近一次加仓的日期, 方便追溯)
            candidates = [d for d in (old_buy_date, trade.get('trade_date') or '') if d]
            new_buy_date = max(candidates) if candidates else date.today().isoformat()
            # last_trade_date: 最近一笔 buy/sell (本笔就是 buy, 一定 max)
            c.execute('''UPDATE positions SET shares=?, cost_price=?, buy_date=?,
                last_trade_date=?, original_shares=COALESCE(original_shares, shares)+?,
                position_type='加仓',
                name=COALESCE(NULLIF(?, ''), positions.name),
                note=COALESCE(note,'')||?, updated_at=datetime('now','localtime') WHERE id=?''',
                (new_shares, new_cost, new_buy_date, trade.get('trade_date') or '',
                 trade['shares'], trade.get('name') or '', new_note, old_id))
        else:
            # 新建仓: trade 没带 trade_date 时, 默认用今天
            buy_date = trade.get('trade_date') or date.today().isoformat()
            c.execute('''INSERT INTO positions
                (ts_code, name, shares, original_shares, cost_price, buy_date, last_trade_date,
                 note, position_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '建仓')''',
                (trade['ts_code'], trade['name'], trade['shares'], trade['shares'],
                 trade_cost_per_share, buy_date, trade.get('trade_date') or '', new_note))
        return 'ok', None

    elif trade['direction'] == 'sell':
        # FIFO: 找 ts_code 相同, 按 created_at ASC 依次扣
        positions = c.execute(
            'SELECT id, shares, cost_price, note, total_sell_amount, realized_pnl FROM positions WHERE ts_code=? AND shares>0 ORDER BY datetime(created_at) ASC, id ASC',
            (trade['ts_code'],)).fetchall()
        if not positions:
            return 'missing', f'无可扣减仓位: {trade["ts_code"]} {trade["name"]}'

        remaining = trade['shares']
        sell_price = trade['price']
        ts = trade.get('trade_date') or trade.get('trade_time') or ''
        sell_note = f"[{ts} 卖出 {trade['shares']}股 @{trade['price']} #{trade['trade_no']}]"
        is_shanghai = trade['ts_code'].endswith('.SH')  # 沪市含过户费, 深市不含
        for pid, pshares, pcost, pnote, prev_sell_amt, prev_pnl in positions:
            if remaining <= 0:
                break
            take = min(remaining, pshares)
            new_shares = pshares - take
            new_note = (pnote or '') + sell_note
            # 累计卖出额 + 已实现盈亏 (按交易所规则扣 sell 侧费用)
            # 沪市: 卖佣 + 印花税 + 过户费; 深市: 卖佣 + 印花税 (过户费打包在佣金里)
            sell_amt_inc = sell_price * take
            sell_commission = max(sell_amt_inc * FEE_CONFIG['commission_rate'], FEE_CONFIG['min_commission'])
            transfer_inc = sell_amt_inc * FEE_CONFIG['transfer_rate'] if is_shanghai else 0
            sell_fees_inc = sell_commission + sell_amt_inc * FEE_CONFIG['stamp_rate'] + transfer_inc
            new_sell_amt = (prev_sell_amt or 0) + sell_amt_inc
            new_pnl = (prev_pnl or 0) + (sell_price - pcost) * take - sell_fees_inc
            if new_shares <= 0.0001:
                # 软删除: shares=0 + closed_at
                c.execute(f'''UPDATE positions SET shares=0, note=?, closed_at={closed_at_expr},
                    total_sell_amount=?, realized_pnl=?, last_trade_date=?,
                    updated_at=datetime('now','localtime') WHERE id=?''',
                    (new_note, new_sell_amt, new_pnl, trade.get('trade_date') or '', pid))
            else:
                c.execute('''UPDATE positions SET shares=?, note=?, total_sell_amount=?,
                    realized_pnl=?, last_trade_date=?,
                    updated_at=datetime('now','localtime') WHERE id=?''',
                    (new_shares, new_note, new_sell_amt, new_pnl,
                     trade.get('trade_date') or '', pid))
            remaining -= take
        if remaining > 0.0001:
            return 'oversell', f'{trade["ts_code"]} 卖超 {remaining}股, 仓位不足'
        return 'ok', None
    return 'unknown', '未知方向'


# ── 对账 self-heal: 从 trades 表 FIFO 重算 position 状态, drift > 0.01 元则修正 + 写 log ──
DRIFT_THRESHOLD = 0.01  # 数值字段差异 > 此值才算 drift, 避免浮点误报


def _reconcile_position(c, ts_code, triggered_by_trade_id=None, triggered_by_trade_date=None):
    """对单只 ts_code 跑严格 FIFO 重算, 跟 positions 表对比, 漂移则修正 + 写 log.
    返回本次修正的 drift 数量. 主动复用 caller's cursor (保证同事务).
    范围: 只算"当前活跃窗口"内的 trades (latest closed_at 之后), 避免历史已关仓位污染.
    """
    if not ts_code:
        return 0

    # 0) 找当前活跃窗口起点
    # 优先: 该 ts_code 最新 closed_at (来自已关仓位) → 用 > 排除 closing trade
    # 回退: trades 表里最早的 buy 日期 → 用 >= 包含第一笔 buy
    # 注意: 不能用 positions.buy_date, 因为 _apply_one_trade 把它存成"最新"日期,
    #       MIN(buy_date) 等于 MAX(buy_date), cutoff 会丢失前面的 trade.
    latest_close = c.execute(
        '''SELECT MAX(closed_at) FROM positions WHERE ts_code=? AND closed_at IS NOT NULL''',
        (ts_code,)).fetchone()
    cutoff_date = None
    use_inclusive = False
    if latest_close and latest_close[0]:
        cutoff_date = latest_close[0][:10]
        use_inclusive = False  # 严格 >, 排除 closing trade
    else:
        first_trade = c.execute(
            '''SELECT MIN(trade_date) FROM trades
               WHERE ts_code=? AND applied=1 AND direction='buy' ''',
            (ts_code,)).fetchone()
        if first_trade and first_trade[0]:
            cutoff_date = first_trade[0]
            use_inclusive = True  # >= 包含第一笔 buy

    # 1) 拉 cutoff 之后所有 applied=1 的 trades (按时间序). 排除 dividend/tax/bonus/transfer
    if cutoff_date:
        cmp = '>=' if use_inclusive else '>'
        trades = c.execute(
            f'''SELECT id, trade_no, trade_date, trade_time, direction, price, shares, amount, ts_code, name
               FROM trades WHERE ts_code=? AND applied=1 AND direction IN ('buy','sell')
                 AND trade_date {cmp} ?
               ORDER BY trade_date, trade_time, id''',
            (ts_code, cutoff_date)).fetchall()
    else:
        trades = c.execute(
            '''SELECT id, trade_no, trade_date, trade_time, direction, price, shares, amount, ts_code, name
               FROM trades WHERE ts_code=? AND applied=1 AND direction IN ('buy','sell')
               ORDER BY trade_date, trade_time, id''', (ts_code,)).fetchall()
    cols = [d[0] for d in c.description]
    trade_list = [dict(zip(cols, r)) for r in trades]

    # 2) 严格 FIFO 重算 (per-lot cost, 严格按时间序)
    buy_queue = []           # [[shares_remaining, cost_per_share, buy_trade_id, buy_date], ...]
    realized_pnl_total = 0.0
    total_sell_amount_total = 0.0
    first_buy_date = None
    last_trade_date = None
    last_trade_id = None
    last_direction = None
    original_shares_total = 0.0
    first_buy_amount_total = 0.0
    add_count = 0
    is_shanghai = _is_shanghai(ts_code)

    for t in trade_list:
        if t['direction'] == 'buy':
            amt = t['price'] * t['shares']
            buy_fee = calc_buy_fees(amt, ts_code)
            cost_per_share = (amt + buy_fee) / t['shares']
            buy_queue.append([float(t['shares']), float(cost_per_share), t['id'], t.get('trade_date')])
            original_shares_total += t['shares']
            first_buy_amount_total += amt
            if first_buy_date is None:
                first_buy_date = t.get('trade_date')
            else:
                add_count += 1
            last_trade_date = t.get('trade_date') or last_trade_date
            last_trade_id = t['id']
            last_direction = 'buy'
        elif t['direction'] == 'sell':
            remaining = t['shares']
            sell_price = t['price']
            sell_amt_inc = 0.0
            while remaining > 0.0001 and buy_queue:
                sh, cps, _, _ = buy_queue[0]
                take = min(remaining, sh)
                sell_amt_inc += sell_price * take
                sell_commission = max(sell_price * take * FEE_CONFIG['commission_rate'], FEE_CONFIG['min_commission'])
                transfer = sell_price * take * FEE_CONFIG['transfer_rate'] if is_shanghai else 0
                stamp = sell_price * take * FEE_CONFIG['stamp_rate']
                realized_pnl_total += (sell_price - cps) * take - sell_commission - stamp - transfer
                buy_queue[0][0] -= take
                if buy_queue[0][0] <= 0.0001:
                    buy_queue.pop(0)
                remaining -= take
            total_sell_amount_total += sell_amt_inc
            last_trade_date = t.get('trade_date') or last_trade_date
            last_trade_id = t['id']
            last_direction = 'sell'

    # 3) 算当前 active 仓位状态
    current_shares = sum(b[0] for b in buy_queue)
    current_cost = None
    if current_shares > 0.0001:
        current_cost = sum(b[0] * b[1] for b in buy_queue) / current_shares

    drifts_fixed = 0

    def _log_drift(pos_id, field, old, new, name):
        nonlocal drifts_fixed
        try:
            o = float(old) if old is not None else None
            n = float(new) if new is not None else None
        except (TypeError, ValueError):
            return
        if o is None and n is None:
            return
        if o is not None and n is not None and abs(n - o) <= DRIFT_THRESHOLD:
            return
        diff = (n - o) if (n is not None and o is not None) else (n if n is not None else -o)
        try:
            app.logger.warning(
                'RECONCILE DRIFT %s %s: %s → %s (diff=%.4f, position_id=%s, trade=#%s)',
                ts_code, field, o, n, diff, pos_id, triggered_by_trade_id)
        except Exception:
            pass
        c.execute('''INSERT INTO position_reconcile_log
            (ts_code, name, position_id, field, old_value, new_value, diff,
             triggered_by_trade_id, triggered_by_trade_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (ts_code, name, pos_id, field, o, n, diff,
             triggered_by_trade_id, triggered_by_trade_date))
        drifts_fixed += 1

    # 4) 处理 active positions (closed_at IS NULL). 用 index 访问 (caller 用默认 tuple factory)
    active_rows = c.execute(
        '''SELECT id, shares, cost_price, original_shares, first_buy_amount, add_count,
                  buy_date, last_trade_date, name
           FROM positions WHERE ts_code=? AND closed_at IS NULL ORDER BY id''',
        (ts_code,)).fetchall()
    pos_name = active_rows[0][8] if active_rows else (trade_list[0]['name'] if trade_list else None)

    if not active_rows and current_shares > 0.0001:
        # 异常: trades 算出有 active 仓位, 但 DB 没有 → 插入新行
        app.logger.warning('RECONCILE MISSING ACTIVE ROW for %s (current_shares=%.2f), inserting', ts_code, current_shares)
        c.execute('''INSERT INTO positions
            (ts_code, name, shares, original_shares, cost_price, buy_date, last_trade_date,
             position_type, add_count, first_buy_amount, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))''',
            (ts_code, pos_name, current_shares, original_shares_total, round(current_cost, 4) if current_cost else None,
             first_buy_date, last_trade_date,
             '建仓' if add_count == 0 else '加仓', add_count, first_buy_amount_total))
        drifts_fixed += 1
    elif not active_rows and current_shares <= 0.0001:
        pass  # 都关了, 正常

    if len(active_rows) == 1:
        r = active_rows[0]
        pid, db_shares, db_cost, db_orig, db_first_amt, db_add_cnt, db_buy_date, db_last_trade, rname = r[:9]
        if current_shares <= 0.0001:
            app.logger.warning('RECONCILE STALE ACTIVE ROW for %s id=%d, closing it', ts_code, pid)
            _log_drift(pid, 'shares', db_shares, 0, rname)
            c.execute('''UPDATE positions SET shares=0, closed_at=COALESCE(closed_at, datetime('now','localtime')),
                last_trade_date=?, position_type='清仓', updated_at=datetime('now','localtime') WHERE id=?''',
                (last_trade_date, pid))
            drifts_fixed += 1
        else:
            _log_drift(pid, 'shares', db_shares, current_shares, rname)
            _log_drift(pid, 'cost_price', db_cost, round(current_cost, 4) if current_cost else None, rname)
            _log_drift(pid, 'original_shares', db_orig, original_shares_total, rname)
            _log_drift(pid, 'first_buy_amount', db_first_amt, first_buy_amount_total, rname)
            _log_drift(pid, 'add_count', db_add_cnt, add_count, rname)
            new_cost_rounded = round(current_cost, 4) if current_cost else None
            c.execute('''UPDATE positions SET
                shares=?, cost_price=?, original_shares=?, first_buy_amount=?, add_count=?,
                buy_date=COALESCE(NULLIF(?, ''), buy_date),
                last_trade_date=COALESCE(NULLIF(?, ''), last_trade_date),
                updated_at=datetime('now','localtime')
                WHERE id=?''',
                (current_shares, new_cost_rounded, original_shares_total, first_buy_amount_total, add_count,
                 first_buy_date, last_trade_date, pid))
    elif len(active_rows) > 1:
        # 多 active: 合并到最早的, 其他的 closed_at 软删
        keep_id = active_rows[0][0]
        app.logger.warning('RECONCILE MULTIPLE ACTIVE ROWS for %s (%d rows), merging into id=%d',
                           ts_code, len(active_rows), keep_id)
        for r in active_rows[1:]:
            _log_drift(r[0], 'merged_into', r[1], 0, r[8])
            c.execute('UPDATE positions SET closed_at=datetime(\'now\',\'localtime\'), shares=0, position_type=\'清仓\', updated_at=datetime(\'now\',\'localtime\') WHERE id=?', (r[0],))
            drifts_fixed += 1
        # 重新刷 keep_id
        c.execute('''UPDATE positions SET
            shares=?, cost_price=?, original_shares=?, first_buy_amount=?, add_count=?,
            buy_date=COALESCE(NULLIF(?, ''), buy_date),
            last_trade_date=COALESCE(NULLIF(?, ''), last_trade_date),
            updated_at=datetime('now','localtime')
            WHERE id=? AND shares>0''',
            (current_shares, round(current_cost, 4) if current_cost else None, original_shares_total,
             first_buy_amount_total, add_count, first_buy_date, last_trade_date, keep_id))

    return drifts_fixed


@app.route('/api/trades/apply', methods=['POST'])
def apply_trades():
    """把已入库未 applied 的成交, 按顺序应用到 positions"""
    data = request.get_json() or {}
    trade_ids = data.get('trade_ids')  # 可选: 只 apply 指定 ID
    force_trade_ids = data.get('force_trade_ids') or []  # 强制重跑已 applied 的指定 ID
    closed_at_date = data.get('closed_at_date')  # 可选: 文件名里的日期, 用于 closed_at
    file_date = data.get('file_date')  # 文件名里的日期, trade_date 空时用这个
    if closed_at_date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', closed_at_date):
        return jsonify({'status': 'error', 'message': 'closed_at_date 格式必须为 YYYY-MM-DD'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if trade_ids:
        placeholders = ','.join('?' * len(trade_ids))
        rows = c.execute(
            # trade_date 是 'YYYYMMDD' 字符串, SQLite 的 datetime() 不认, 用字典序排序 (等价于时间序)
            f'SELECT * FROM trades WHERE id IN ({placeholders}) AND applied=0 ORDER BY trade_date, trade_time, id',
            trade_ids).fetchall()
    elif force_trade_ids:
        placeholders = ','.join('?' * len(force_trade_ids))
        rows = c.execute(
            f'SELECT * FROM trades WHERE id IN ({placeholders}) ORDER BY trade_date, trade_time, id',
            force_trade_ids).fetchall()
    else:
        # 空 trade_date 排到末尾 (COALESCE 替换成 '99999999' 字典序最大)
        rows = c.execute(
            "SELECT * FROM trades WHERE applied=0 ORDER BY COALESCE(NULLIF(trade_date, ''), '99999999'), trade_time, id").fetchall()

    cols = [d[0] for d in c.description] if c.description else []
    trades = [dict(zip(cols, r)) for r in rows]

    applied = []
    errors = []
    warnings = []
    closed_at_value = f"'{data.get('closed_at_date')} 15:00:00'" if data.get('closed_at_date') else "datetime('now','localtime')"
    for t in trades:
        # 文件名日期兜底: trade 自己没 trade_date 时用 file_date (用户 2026-06-05 反馈)
        if file_date and not (t.get('trade_date') or '').strip():
            t['trade_date'] = file_date
            # 同时把 trade 自身 DB 字段也补上, 后续查询也能看到日期
            c.execute('UPDATE trades SET trade_date=? WHERE id=?', (file_date, t['id']))
        status, msg = _apply_one_trade(c, t, closed_at_expr=closed_at_value)
        if status in ('ok', 'oversell'):
            # 仓位确实被扣减了 (即使卖超) → 标记为已应用, 避免卡在 limbo
            c.execute('UPDATE trades SET applied=1 WHERE id=?', (t['id'],))
            applied.append({'id': t['id'], 'ts_code': t['ts_code'], 'name': t['name'],
                            'direction': t['direction'], 'shares': t['shares'], 'price': t['price']})
            if status == 'oversell':
                warnings.append({'id': t['id'], 'ts_code': t['ts_code'], 'name': t['name'],
                                 'message': msg})
        else:
            errors.append({'id': t['id'], 'ts_code': t['ts_code'], 'name': t['name'],
                           'status': status, 'message': msg})

    # ── self-heal: 对所有 active 仓位都跑一次 FIFO 对账 (覆盖外部 drift, 不只是本次 apply 涉及的) ──
    active_codes_rows = c.execute(
        "SELECT DISTINCT ts_code FROM positions WHERE closed_at IS NULL").fetchall()
    active_codes = {r[0] for r in active_codes_rows}
    # 加上本次 apply 涉及的 (即使后来被关了也跑一次, 防止 closed 仓位 realized_pnl 漂移)
    apply_codes = {t['ts_code'] for t in trades if t.get('ts_code')}
    all_codes = sorted(active_codes | apply_codes)
    reconcile_summary = {'checked': len(all_codes), 'drifts_fixed': 0, 'ts_codes': []}
    for ts in all_codes:
        # 触发 trade: 优先取本次 apply 涉及 ts 的, 否则取该 ts 最近的 applied=1 trade
        trigger = next((t for t in reversed(trades) if t.get('ts_code') == ts and t.get('id')), None)
        if not trigger:
            last_trade = c.execute(
                "SELECT id, trade_date FROM trades WHERE ts_code=? AND applied=1 ORDER BY trade_date DESC, trade_time DESC, id DESC LIMIT 1",
                (ts,)).fetchone()
            trigger = {'id': last_trade[0], 'trade_date': last_trade[1]} if last_trade else None
        try:
            drifts = _reconcile_position(
                c, ts,
                triggered_by_trade_id=trigger.get('id') if trigger else None,
                triggered_by_trade_date=trigger.get('trade_date') if trigger else None,
            )
        except Exception as e:
            app.logger.exception('RECONCILE failed for %s: %s', ts, e)
            drifts = 0
        if drifts:
            reconcile_summary['drifts_fixed'] += drifts
            reconcile_summary['ts_codes'].append({'ts_code': ts, 'drifts': drifts})

    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success' if not errors else 'partial',
        'applied': applied,
        'errors': errors,
        'warnings': warnings,
        'applied_count': len(applied),
        'error_count': len(errors),
        'warning_count': len(warnings),
        'reconcile': reconcile_summary,
    })


@app.route('/api/positions/reconcile-log', methods=['GET'])
def get_reconcile_log():
    """对账日志: 列出 position_reconcile_log, 可按 ts_code 过滤"""
    limit = min(int(request.args.get('limit', 100)), 500)
    ts_code = request.args.get('ts_code')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if ts_code:
        rows = c.execute(
            'SELECT * FROM position_reconcile_log WHERE ts_code=? ORDER BY id DESC LIMIT ?',
            (ts_code, limit)).fetchall()
    else:
        rows = c.execute(
            'SELECT * FROM position_reconcile_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    log = [dict(r) for r in rows]
    # summary
    codes = sorted({r['ts_code'] for r in log})
    summary = {
        'count': len(log),
        'codes_count': len(codes),
        'latest': log[0]['created_at'] if log else None,
    }
    conn.close()
    return jsonify({'log': log, 'summary': summary})


@app.route('/api/positions/reconcile-log', methods=['DELETE'])
def clear_reconcile_log():
    """清空对账日志"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM position_reconcile_log')
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'deleted': deleted})


@app.route('/api/positions/reconcile-all', methods=['POST'])
def reconcile_all_positions():
    """对所有有 trade 的 ts_code 跑一次 reconcile, 修复 drift (cost_price / shares / original_shares 等)
    用于一次性修复历史数据, 比如 position.buy_date cutoff bug 导致的 shares 丢失."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    ts_codes = [r[0] for r in c.execute(
        "SELECT DISTINCT ts_code FROM trades WHERE applied=1 AND direction IN ('buy','sell')"
    ).fetchall()]
    total_drift = 0
    fixed_ts = []
    for ts in ts_codes:
        try:
            n = _reconcile_position(c, ts)
            if n > 0:
                total_drift += n
                fixed_ts.append(ts)
        except Exception as e:
            print(f'[reconcile-all] {ts} 失败: {e}')
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'total': len(ts_codes),
        'fixed': len(fixed_ts),
        'drift_count': total_drift,
        'fixed_ts_codes': fixed_ts,
    })


@app.route('/api/trades/rollback', methods=['POST'])
def rollback_trades():
    """撤销 apply: 把指定 trades 的 applied 改回 0 (position 状态不会自动还原!)"""
    data = request.get_json() or {}
    trade_ids = data.get('trade_ids') or []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if trade_ids:
        placeholders = ','.join('?' * len(trade_ids))
        c.execute(f'UPDATE trades SET applied=0 WHERE id IN ({placeholders})', trade_ids)
    else:
        c.execute('UPDATE trades SET applied=0 WHERE applied=1')
    n = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'rolled_back': n,
                    'warning': '持仓数据未自动还原, 如需修正请手动调整 positions 表'})


@app.route('/api/trades/clear', methods=['POST'])
def clear_trades():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM trades')
    n = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'deleted': n})


# ============ 新浪实时行情转发 ============

def _sina_code_for_ts(ts_code):
    """600519.SH -> sh600519, 000001.SZ -> sz000001, 605069 (无后缀) -> 自动判断 (6/5/9 → sh, 0/2/3 → sz, 4/8 → bj)"""
    if not ts_code:
        return None
    s = str(ts_code).strip().lower()
    if '.' in s:
        # 已带后缀, 按 .SH/.SZ/.BJ 拆
        code, market = s.split('.', 1)
        prefix = {'sh': 'sh', 'sz': 'sz', 'bj': 'bj'}.get(market, '')
        if not prefix:
            return None
        return f'{prefix}{code}'
    # 无后缀, 按 6 位数字首位判断市场
    if not s.isdigit() or len(s) != 6:
        return None
    if s.startswith(('6', '5', '9')):
        return f'sh{s}'  # 6/5/9 开头 → 沪
    if s.startswith(('0', '2', '3')):
        return f'sz{s}'  # 0/2/3 开头 → 深
    if s.startswith(('4', '8')):
        return f'bj{s}'  # 4/8 开头 → 北交所
    return None


def _ts_code_for_sina(sina_code):
    """sh600519 -> 600519.SH"""
    if not sina_code:
        return None
    s = str(sina_code).lower()
    if s.startswith('sh'):
        return f'{s[2:]}.SH'
    if s.startswith('sz'):
        return f'{s[2:]}.SZ'
    if s.startswith('bj'):
        return f'{s[2:]}.BJ'
    return None


_QUOTE_CACHE = {}  # ts_code -> {'price', 'prev_close', 'change_pct', 'name', 'time', '_ts': fetch_time}
_QUOTE_CACHE_TTL = 3  # 默认 3s TTL, 跟 posAutoInterval 联动时按前端 ?interval= 算 (max 2, min 30)
_QUOTE_CACHE_HITS = 0
_QUOTE_CACHE_MISSES = 0


def fetch_sina_quotes(ts_codes, cache_ttl=None):
    """通过新浪财经 hq.sinajs.cn 拉一批代码的实时行情

    返回 {ts_code: {price, prev_close, change_pct, name, time}}
    全局缓存: 同一只股 cache_ttl 秒内多次请求复用, 避免持仓/选股/板块/涨停拆解等 tab 重复拉 sina.
    cache_ttl: 调用方传 (通常从 ?interval= 算), None 用 _QUOTE_CACHE_TTL 默认值.
    """
    import time as _t
    now = _t.time()
    ttl = cache_ttl if cache_ttl is not None else _QUOTE_CACHE_TTL

    # 1) 拆 sina_code + 查缓存 (按 ts_code 复用)
    sina_codes = []
    code_map = {}  # sina_code -> ts_code
    cached = {}    # 直接从缓存返
    for tc in ts_codes:
        sc = _sina_code_for_ts(tc)
        if not sc:
            continue
        # 缓存命中?
        entry = _QUOTE_CACHE.get(tc)
        if entry and now - entry['_ts'] < ttl:
            global _QUOTE_CACHE_HITS
            _QUOTE_CACHE_HITS += 1
            cached[tc] = {k: v for k, v in entry.items() if k != '_ts'}  # 不返 _ts
        else:
            sina_codes.append(sc)
            code_map[sc] = tc
            global _QUOTE_CACHE_MISSES
            _QUOTE_CACHE_MISSES += 1

    if not sina_codes:
        return cached

    # 2) 缓存 miss 的才发 sina (分批 50 避免 URL 字符超限)
    result = {}
    BATCH = 50
    for i in range(0, len(sina_codes), BATCH):
        batch = sina_codes[i:i + BATCH]
        url = f'https://hq.sinajs.cn/list={",".join(batch)}'
        headers = {
            'Referer': 'https://finance.sina.com.cn',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        }
        try:
            r = _requests.get(url, headers=headers, timeout=5)
            if r.status_code != 200:
                print(f'sina 返回 {r.status_code}')
                continue
            for line in r.text.strip().split('\n'):
                # 格式: var hq_str_sh600519="贵州茅台,1800.00,1780.00,1810.00,1820.00,1790.00,1805.00,...";
                if '=' not in line or '"' not in line:
                    continue
                var_part, val_part = line.split('=', 1)
                sina_code = var_part.strip().replace('var hq_str_', '')
                val = val_part.strip().strip(';').strip('"')
                if not val or val == 'NULL':
                    continue
                fields = val.split(',')
                if len(fields) < 10:
                    continue
                # 字段含义: 0=名称 1=今开 2=昨收 3=当前 4=日高 5=日低 ...
                name = fields[0]
                try:
                    current = float(fields[3])
                    prev_close = float(fields[2])
                except (ValueError, IndexError):
                    continue
                if prev_close <= 0:
                    continue
                change_pct = (current - prev_close) / prev_close * 100
                # 时间: index 30 起始 HHMMSS (不同市场略有差异)
                quote_time = fields[30] if len(fields) > 30 else ''
                ts_code = _ts_code_for_sina(sina_code)
                if ts_code:
                    entry = {
                        'price': current,
                        'prev_close': prev_close,
                        'change_pct': round(change_pct, 2),
                        'name': name,
                        'time': quote_time,
                    }
                    _QUOTE_CACHE[ts_code] = {**entry, '_ts': now}
                    result[ts_code] = entry
        except Exception as e:
            print(f'fetch_sina_quotes 错误: {e}')

    # 3) 合并: 缓存 + 新拉
    return {**cached, **result}


def _quote_cache_ttl_from_request():
    """从 request ?interval= 算 cache_ttl = max(2, min(30, int(interval))).
    联动 posAutoInterval: 3s 周期 -> 3s 缓存 (真实 3s 刷新), 30s 周期 -> 30s 缓存.
    """
    try:
        n = int(request.args.get('interval', 3))
    except (TypeError, ValueError):
        n = 3
    return max(2, min(30, n))


@app.route('/api/positions/quote', methods=['GET'])
def get_positions_quote():
    """独立 quote 端点, 给前端手动刷新 / 调试用"""
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if c.strip()]
    if not codes:
        return jsonify({})
    return jsonify(fetch_sina_quotes(codes, cache_ttl=_quote_cache_ttl_from_request()))


@app.route('/api/limitup/quote', methods=['GET'])
def get_limitup_quote():
    """涨停拆解明细专用 quote 端点: 输入一批 ts_code, 返回 sina 实时价 dict.
    给前端补"所有"涨停股的盘中实时涨幅 (不限 date).
    sina 单次 URL 长度有限, 内部按 200 一组串行分批拉."""
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if c.strip()]
    if not codes:
        return jsonify({})
    all_quotes = {}
    BATCH = 200
    ttl = _quote_cache_ttl_from_request()
    for i in range(0, len(codes), BATCH):
        batch = codes[i:i + BATCH]
        all_quotes.update(fetch_sina_quotes(batch, cache_ttl=ttl))
    return jsonify(all_quotes)


@app.route('/api/leaderboard/quote', methods=['GET'])
def get_leaderboard_quote():
    """阶段涨幅榜单专用 quote 端点: 给榜单每只股票补"当下实时涨跌幅" (跟 5日/10日/20日 涨幅并列显示)
    sina 全局 30s 缓存, 跨 tab 复用. 复用 /api/limitup/quote 同样的批量逻辑.
    """
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if c.strip()]
    if not codes:
        return jsonify({})
    all_quotes = {}
    BATCH = 200
    ttl = _quote_cache_ttl_from_request()
    for i in range(0, len(codes), BATCH):
        batch = codes[i:i + BATCH]
        all_quotes.update(fetch_sina_quotes(batch, cache_ttl=ttl))
    return jsonify(all_quotes)


@app.route('/api/picks/quote', methods=['GET'])
def api_picks_quote():
    """选股推荐专用 quote 端点: 返回实时价 + 用实时价替换昨日 close 重算的 MA5/10/20/30.

    输入: ?codes=000001.SZ,600000.SH (逗号分隔, 最多 200 个)
    输出: {ts_code: {price, prev_close, change_pct, ma5, ma10, ma20, ma30}}
    """
    import statistics
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if c.strip()][:200]
    if not codes:
        return jsonify({})

    # 1. 拉新浪实时价
    quotes = fetch_sina_quotes(codes, cache_ttl=_quote_cache_ttl_from_request())
    if not quotes:
        return jsonify({})

    # 2. 对每只股票: 从 stock_daily 拉最近 30 日 close, 替换最后一天为实时价, 重算 MA
    conn = sqlite3.connect(DB_PATH, timeout=30)
    result = {}
    for ts_code, q in quotes.items():
        if not q or q.get('price') is None:
            continue
        rows = conn.execute("""
            SELECT trade_date, close FROM stock_daily
            WHERE ts_code = ? ORDER BY trade_date DESC LIMIT 31
        """, (ts_code,)).fetchall()
        # 取最近 30 日 close 序列 (旧→新)
        closes = [r[1] for r in rows[1:31] if r[1] is not None]  # 跳过最新那条 (因为要替换)
        closes = list(reversed(closes))[-30:]  # 保留最近 30 个
        if not closes:
            continue
        # 用今日实时价替换最后一位
        closes.append(float(q['price']))
        # 算 MA
        def _ma(arr, n):
            return sum(arr[-n:]) / n if len(arr) >= n else None
        result[ts_code] = {
            'price': float(q['price']),
            'prev_close': float(q['prev_close']) if q.get('prev_close') else None,
            'change_pct': float(q['change_pct']) if q.get('change_pct') is not None else None,
            'ma5': _ma(closes, 5),
            'ma10': _ma(closes, 10),
            'ma20': _ma(closes, 20),
            'ma30': _ma(closes, 30),
            'name': q.get('name', ''),
            'time': q.get('time', ''),
        }
    conn.close()
    return jsonify(result)


# ============ 主力净流入 (capital-flow, AKShare + 东方财富 push2) ============


def _ak_call(fn, *args, retries=2, **kwargs):
    """AKShare 调用包装: 失败重试, 指数退避. 代理在模块加载时已清, 此处不再处理."""
    last = None
    for i in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            print(f'[akshare] retry {i+1}/{retries}: {type(e).__name__}: {e}', flush=True)
            time.sleep(min(2 ** i, 5))
    raise last


def _ak_fund_flow_renamed(df):
    """AKShare 中文列名 → 英文 DB 列名."""
    rename = {
        '日期': 'trade_date', '收盘价': 'close', '涨跌幅': 'change_pct',
        '主力净流入-净额': 'main_net_inflow', '主力净流入-净占比': 'main_net_pct',
        '超大单净流入-净额': 'super_net', '超大单净流入-净占比': 'super_pct',
        '大单净流入-净额': 'big_net', '大单净流入-净占比': 'big_pct',
        '中单净流入-净额': 'mid_net', '中单净流入-净占比': 'mid_pct',
        '小单净流入-净额': 'small_net', '小单净流入-净占比': 'small_pct',
    }
    return df.rename(columns=rename)


def _save_fund_flow_df(df, source='akshare'):
    """DataFrame → fund_flow 表 DELETE+INSERT upsert. 返回写入条数. 不自动补 code/ts_code (由调用方补)."""
    import math
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    count = 0
    for _, r in df.iterrows():
        td = str(r.get('trade_date', '')).replace('-', '')[:8]
        if len(td) != 8:
            continue
        code = str(r.get('code', '')).strip() or str(r.get('ts_code', '')).split('.')[0]
        if not code or len(code) != 6:
            continue
        ts_code = r.get('ts_code') if r.get('ts_code') and '.' in str(r.get('ts_code')) else (
            f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ'
        )
        def _f(v):
            try:
                x = float(v)
                return 0.0 if math.isnan(x) else x
            except (ValueError, TypeError):
                return None
        cur.execute('DELETE FROM fund_flow WHERE ts_code=? AND trade_date=?', (ts_code, td))
        cur.execute('''INSERT INTO fund_flow
            (trade_date, ts_code, code, name, close, change_pct,
             main_net_inflow, main_net_pct,
             super_net, super_pct, big_net, big_pct,
             mid_net, mid_pct, small_net, small_pct, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (td, ts_code, code, r.get('name', ''),
             _f(r.get('close')), _f(r.get('change_pct')),
             _f(r.get('main_net_inflow')) or 0.0, _f(r.get('main_net_pct')),
             _f(r.get('super_net')), _f(r.get('super_pct')),
             _f(r.get('big_net')), _f(r.get('big_pct')),
             _f(r.get('mid_net')), _f(r.get('mid_pct')),
             _f(r.get('small_net')), _f(r.get('small_pct')),
             source))
        count += 1
    conn.commit()
    conn.close()
    return count


def _eastmoney_fflow(code, days=120):
    """东方财富个股资金流日线. code 6 位数字. 返回 DataFrame(列名与 fund_flow 表一致) 或 None.
    无 6 个月窗口限制, 历史通常 1+ 年. 失败返回 None, 调用方继续走 AKShare 兜底.
    已知问题: 本机 IP 会被东财短时限流 (HTTP 200 + Empty reply), 间歇性 200/失败, AKShare 同源也受影响.
    """
    if not code or len(code) != 6 or not code.isdigit():
        return None
    secid = ('1.' if code.startswith(('6', '9')) else '0.') + code
    fields2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f63,f64'
    url = ('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
           f'?secid={secid}&fields1=f1,f2,f3,f4&fields2={fields2}'
           '&klt=101&fqt=0&beg=0&end=20500101')
    # 短时连发会被东财反爬掐, 指数退避重试 3 次
    klines, name = [], ''
    for attempt in range(3):
        try:
            r = _requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Referer': 'https://quote.eastmoney.com/',
                'Accept': '*/*',
            }, timeout=8)
            d = r.json()
            klines = (d.get('data') or {}).get('klines') or []
            name = (d.get('data') or {}).get('name') or ''
            if klines:
                break
        except Exception as e:
            print(f'[eastmoney fflow] {code} attempt {attempt+1}/3 failed: {e}', flush=True)
        time.sleep(min(2 ** attempt, 4))  # 1s, 2s, 4s
    if not klines:
        return None
    rows = []
    for line in klines[-days:]:  # 取最近 N 天
        parts = line.split(',')
        if len(parts) < 14:
            continue
        try:
            rows.append({
                'trade_date': parts[0].replace('-', ''),  # YYYYMMDD
                'code': code,
                'ts_code': f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ',
                'name': name,
                'close': float(parts[13]) if parts[13] else None,    # f64
                'change_pct': float(parts[12]) if parts[12] else None,  # f63
                'main_net_inflow': float(parts[1]),  # f52
                'main_net_pct': float(parts[6]),     # f57
                'super_net': float(parts[5]),        # f56
                'super_pct': float(parts[10]),       # f61
                'big_net': float(parts[4]),          # f55
                'big_pct': float(parts[9]),          # f60
                'mid_net': float(parts[3]),          # f54
                'mid_pct': float(parts[8]),          # f59
                'small_net': float(parts[2]),        # f53
                'small_pct': float(parts[7]),        # f58
            })
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows) if rows else None




def _akshare_topn(date, n=200):
    """AKShare 拉指定日期全市场主力净额 top n. 返回 DataFrame 或 None.
    注意: AKShare stock_individual_fund_flow_rank 只能拉"今天", 不能指定历史日期.
    历史日期会返回 None, 调用方走东财兜底.
    """
    from datetime import datetime as _dt
    today = _dt.now().strftime('%Y%m%d')
    if str(date) != today:
        return None  # AKShare 不支持历史日期
    try:
        df = _ak_call(ak.stock_individual_fund_flow_rank, indicator='今日')
    except Exception as e:
        print(f'[akshare topn] {date} failed: {e}', flush=True)
        return None
    if df is None or len(df) == 0:
        return None
    # AKShare 列名 → fund_flow 表列名 (复用 _ak_fund_flow_renamed 但只取今天)
    df = _ak_fund_flow_renamed(df)
    if 'name' not in df.columns:
        df['name'] = ''
    if 'ts_code' not in df.columns:
        df['ts_code'] = df.get('code', '').apply(
            lambda c: f'{c}.SH' if str(c).startswith(('6','9')) else f'{c}.SZ')
    if 'code' not in df.columns:
        df['code'] = df['ts_code'].str.split('.').str[0]
    # 按主力净额排序取 top n
    df = df.sort_values('main_net_inflow', ascending=False).head(n).reset_index(drop=True)
    df['trade_date'] = str(date)
    return df






@app.route('/api/flow/latest-date', methods=['GET'])
def api_flow_latest_date():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MAX(trade_date) AS d, COUNT(*) AS c, COUNT(DISTINCT ts_code) AS stocks FROM fund_flow").fetchone()
    conn.close()
    return jsonify({
        'latest_date': row[0] or '',
        'rows': row[1] or 0,
        'stocks': row[2] or 0,
    })


@app.route('/api/flow/market', methods=['GET'])
def api_flow_market():
    """全市场: 取某日 (或最近 N 天) 每只股票的主力气净额. 支持 ?date=YYYYMMDD 过滤.
    默认 source=eastmoney-push2 (Skill 抓取的数据). 传 source=all 看全部来源.
    """
    days = min(max(int(request.args.get('days', 1)), 1), 200)
    sort = request.args.get('sort', 'main_net_inflow')
    order = 'desc' if request.args.get('order', 'desc').lower() == 'desc' else 'asc'
    market = request.args.get('market', 'all').lower()
    search = request.args.get('search', '').strip()
    limit = min(int(request.args.get('limit', 200)), 10000)
    date = request.args.get('date', '').strip()  # 指定日期 (YYYYMMDD)
    source = request.args.get('source', 'eastmoney-push2').strip()  # 默认只显示 Skill 抓的数据

    sort_whitelist = {'main_net_inflow', 'main_net_pct', 'change_pct', 'close'}
    if sort not in sort_whitelist:
        sort = 'main_net_inflow'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if date:
        # 指定日期: 取该日所有 ts_code
        if source == 'all':
            rows = conn.execute("""
                SELECT trade_date, ts_code, code, name, close, change_pct,
                       main_net_inflow, main_net_pct,
                       super_net, big_net, mid_net, small_net, source
                FROM fund_flow
                WHERE trade_date = ?
            """, (date,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT trade_date, ts_code, code, name, close, change_pct,
                       main_net_inflow, main_net_pct,
                       super_net, big_net, mid_net, small_net, source
                FROM fund_flow
                WHERE trade_date = ? AND source = ?
            """, (date, source)).fetchall()
    else:
        # 无日期: 取最近 N 天每个 ts_code 的最新一行
        if source == 'all':
            rows = conn.execute("""
                SELECT f.trade_date, f.ts_code, f.code, f.name, f.close, f.change_pct,
                       f.main_net_inflow, f.main_net_pct,
                       f.super_net, f.big_net, f.mid_net, f.small_net, f.source
                FROM fund_flow f
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) AS max_date
                    FROM fund_flow
                    WHERE trade_date >= date('now', '-' || ? || ' days', 'localtime')
                    GROUP BY ts_code
                ) m ON f.ts_code = m.ts_code AND f.trade_date = m.max_date
            """, (days,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT f.trade_date, f.ts_code, f.code, f.name, f.close, f.change_pct,
                       f.main_net_inflow, f.main_net_pct,
                       f.super_net, f.big_net, f.mid_net, f.small_net, f.source
                FROM fund_flow f
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) AS max_date
                    FROM fund_flow
                    WHERE trade_date >= date('now', '-' || ? || ' days', 'localtime')
                      AND source = ?
                    GROUP BY ts_code
                ) m ON f.ts_code = m.ts_code AND f.trade_date = m.max_date
            """, (days, source)).fetchall()
    conn.close()

    # market 过滤 (按 code 前缀)
    if market == 'sh':
        rows = [r for r in rows if r['code'].startswith(('6', '9'))]
    elif market == 'sz':
        rows = [r for r in rows if r['code'].startswith(('0', '2', '3'))]
    elif market == 'cy':
        rows = [r for r in rows if r['code'].startswith('3')]
    elif market == 'kcb':
        rows = [r for r in rows if r['code'].startswith('688')]

    # search 模糊
    if search:
        s = search.lower()
        rows = [r for r in rows if s in r['name'].lower() or s in r['code']]

    # 排序
    rows.sort(key=lambda r: (r[sort] or 0) if sort in r.keys() else 0, reverse=(order == 'desc'))

    out = [dict(r) for r in rows[:limit]]
    _attach_sector(out, key='ts_code')  # 补 sector_history
    return jsonify({
        'count': len(out),
        'total_matched': len(rows),
        'days': days,
        'date': date,
        'sort': sort,
        'order': order,
        'rows': out,
    })


@app.route('/api/flow/holdings', methods=['GET'])
def api_flow_holdings():
    """当前持仓的主力净: 跨 positions 表 JOIN fund_flow."""
    days = min(max(int(request.args.get('days', 60)), 1), 200)
    sort = request.args.get('sort', 'main_net_inflow')
    order = 'desc' if request.args.get('order', 'desc').lower() == 'desc' else 'asc'
    sort_whitelist = {'main_net_inflow', 'main_net_pct', 'sum_main_net', 'inflow_days', 'consecutive_inflow', 'change_pct'}
    if sort not in sort_whitelist:
        sort = 'main_net_inflow'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    open_rows = conn.execute(
        "SELECT ts_code FROM positions WHERE closed_at IS NULL"
    ).fetchall()
    if not open_rows:
        conn.close()
        return jsonify({'count': 0, 'days': days, 'sort': sort, 'order': order, 'holdings': []})

    open_codes = [r['ts_code'] for r in open_rows]
    placeholders = ','.join('?' * len(open_codes))
    rows = conn.execute(f"""
        SELECT trade_date, ts_code, code, name, close, change_pct,
               main_net_inflow, main_net_pct,
               super_net, big_net, mid_net, small_net
        FROM fund_flow
        WHERE ts_code IN ({placeholders})
          AND trade_date >= date('now', '-' || ? || ' days', 'localtime')
        ORDER BY ts_code, trade_date DESC
    """, (*open_codes, days)).fetchall()
    conn.close()

    # 聚合: 每个 ts_code 取最新一日 + 算 sum/inflow_days/consecutive
    by_code = {}
    for r in rows:
        ts_code = r['ts_code']
        if ts_code not in by_code:
            by_code[ts_code] = {
                'ts_code': ts_code,
                'code': r['code'],
                'name': r['name'],
                'close': r['close'],
                'change_pct': r['change_pct'],
                'main_net_inflow': r['main_net_inflow'],
                'main_net_pct': r['main_net_pct'],
                'sum_main_net': 0.0,
                'inflow_days': 0,
                'consecutive_inflow': 0,
                'super_net': r['super_net'],
                'big_net': r['big_net'],
                'mid_net': r['mid_net'],
                'small_net': r['small_net'],
                '_daily': [],  # 内部: 用于算连续
            }
        d = by_code[ts_code]
        d['sum_main_net'] += r['main_net_inflow'] or 0
        if r['main_net_inflow'] and r['main_net_inflow'] > 0:
            d['inflow_days'] += 1
        d['_daily'].append((r['trade_date'], r['main_net_inflow'] or 0))

    # 算连续净流入 (按日期降序, 最先遇到正数累加, 遇到 0 或负数停)
    for d in by_code.values():
        consec = 0
        for td, v in d['_daily']:
            if v > 0:
                consec += 1
            else:
                break
        d['consecutive_inflow'] = consec
        del d['_daily']

    out = list(by_code.values())
    out.sort(key=lambda x: x.get(sort) or 0, reverse=(order == 'desc'))
    return jsonify({'count': len(out), 'days': days, 'sort': sort, 'order': order, 'holdings': out})


@app.route('/api/flow/single', methods=['GET'])
def api_flow_single():
    """个股详情: 从 DB 读, 不够则用 AKShare 补, 返回 daily + periods + summary."""
    ts_code = request.args.get('ts_code', '').strip()
    if not ts_code or '.' not in ts_code:
        return jsonify({'status': 'error', 'message': 'invalid ts_code (格式: 600519.SH)'}), 400
    days = min(max(int(request.args.get('days', 60)), 1), 200)
    code = ts_code.split('.')[0]

    # 1) 读 DB
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT trade_date, close, change_pct, main_net_inflow, main_net_pct,
               super_net, super_pct, big_net, big_pct,
               mid_net, mid_pct, small_net, small_pct, name
        FROM fund_flow
        WHERE ts_code=? AND trade_date >= date('now', '-' || ? || ' days', 'localtime')
        ORDER BY trade_date DESC
    """, (ts_code, days)).fetchall()
    conn.close()

    # 2) DB 数据少于请求天数一半 → Tushare 优先 → AKShare → 东财兜底
    if len(rows) < days // 2 or len(rows) == 0:
        from datetime import datetime as _dt, timedelta as _td
        end_d = _dt.now()
        start_d = end_d - _td(days=int(days * 1.6))  # 多取一些以防非交易日
        start_str = start_d.strftime('%Y%m%d')
        end_str = end_d.strftime('%Y%m%d')
        filled = False
        # Tushare 优先 (稳定 + 历史 1+ 年 + 单次 1 API)
        try:
            import tushare as _ts
            pro = _ts.pro_api()
            df = pro.moneyflow(ts_code=ts_code, start_date=start_str, end_date=end_str)
            if df is not None and not df.empty:
                # 补 name 字段
                try:
                    basic = pro.stock_basic(list_status='L', fields='ts_code,name')
                    name_map = dict(zip(basic['ts_code'], basic['name']))
                except Exception:
                    name_map = {}
                df['name'] = df['ts_code'].map(name_map).fillna('')
                # Tushare 千元 → 元, 转成 fund_flow 列
                df['main_net_inflow'] = df['net_mf_amount'] * 1000
                df['super_net'] = (df['buy_elg_amount'] - df['sell_elg_amount']) * 1000
                df['big_net']   = (df['buy_lg_amount']  - df['sell_lg_amount'])  * 1000
                df['mid_net']   = (df['buy_md_amount']  - df['sell_md_amount'])  * 1000
                df['small_net'] = (df['buy_sm_amount']  - df['sell_sm_amount'])  * 1000
                df['code'] = code
                df['ts_code'] = ts_code
                df['trade_date'] = df['trade_date'].astype(str)
                df_out = df[['trade_date','ts_code','code','name','main_net_inflow','super_net','big_net','mid_net','small_net']].copy()
                df_out['close'] = None
                df_out['change_pct'] = None
                df_out['main_net_pct'] = None
                df_out['super_pct'] = None
                df_out['big_pct'] = None
                df_out['mid_pct'] = None
                df_out['small_pct'] = None
                _save_fund_flow_df(df_out, source='tushare')
                filled = True
                print(f'[flow/single] {ts_code} Tushare 补 {len(df_out)} 条', flush=True)
        except Exception as e:
            print(f'[flow/single] {ts_code} Tushare 失败: {e}', flush=True)
        # Tushare 失败 → AKShare 兜底
        if not filled:
            market = 'sh' if ts_code.endswith('.SH') else 'sz'
            try:
                df = _ak_call(ak.stock_individual_fund_flow, stock=code, market=market)
                df = _ak_fund_flow_renamed(df)
                if 'ts_code' not in df.columns:
                    df['ts_code'] = ts_code
                if 'code' not in df.columns:
                    df['code'] = code
                _save_fund_flow_df(df, source='akshare')
                filled = True
                print(f'[flow/single] {ts_code} AKShare 兜底 {len(df)} 条', flush=True)
            except Exception as e:
                print(f'[flow/single] {ts_code} AKShare 兜底失败: {e}', flush=True)
        # AKShare 失败 → 东方财富兜底
        if not filled:
            df = _eastmoney_fflow(code, days=days)
            if df is not None and not df.empty:
                _save_fund_flow_df(df, source='eastmoney')
                filled = True
                print(f'[flow/single] {ts_code} 东方财富补 {len(df)} 条', flush=True)
            else:
                return jsonify({'status': 'error', 'message': f'Tushare+AKShare+东财 全部失败', 'ts_code': ts_code}), 500
        # 重读
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT trade_date, close, change_pct, main_net_inflow, main_net_pct,
                   super_net, super_pct, big_net, big_pct,
                   mid_net, mid_pct, small_net, small_pct, name
            FROM fund_flow
            WHERE ts_code=? AND trade_date >= date('now', '-' || ? || ' days', 'localtime')
            ORDER BY trade_date DESC
        """, (ts_code, days)).fetchall()
        conn.close()

    if not rows:
        return jsonify({'status': 'error', 'message': 'no data', 'ts_code': ts_code}), 404

    # 3) 算 periods (3/5/10/20/30/60/120 日累计, rows 是 DESC)
    daily_list = [{'trade_date': r['trade_date'], 'close': r['close'], 'change_pct': r['change_pct'],
                   'main_net_inflow': r['main_net_inflow'] or 0, 'main_net_pct': r['main_net_pct'],
                   'super_net': r['super_net'] or 0, 'big_net': r['big_net'] or 0,
                   'mid_net': r['mid_net'] or 0, 'small_net': r['small_net'] or 0} for r in rows]
    periods = {}
    for p in [3, 5, 10, 20, 30, 60, 120]:
        v = sum(d['main_net_inflow'] for d in daily_list[:p])
        periods[p] = v

    # 4) summary (4 张卡) - 取最新一日
    last = daily_list[0]
    sum_main = sum(d['main_net_inflow'] for d in daily_list)
    days_pos = sum(1 for d in daily_list if d['main_net_inflow'] > 0)
    summary = {
        'latest_main_net': last['main_net_inflow'],
        'latest_main_pct': last['main_net_pct'],
        'latest_date': last['trade_date'],
        'latest_change_pct': last['change_pct'],
        'sum_main_net': sum_main,
        'days_pos': days_pos,
        'days_total': len(daily_list),
    }

    # 4.5) 查 stock_basic / positions 拿 name (AKShare 不返回 name 字段)
    name = ''
    conn2 = sqlite3.connect(DB_PATH)
    row = conn2.execute("SELECT name FROM stock_basic WHERE ts_code=? LIMIT 1", (ts_code,)).fetchone()
    if row and row[0]:
        name = row[0]
    else:
        row = conn2.execute("SELECT name FROM positions WHERE ts_code=? LIMIT 1", (ts_code,)).fetchone()
        if row and row[0]:
            name = row[0]
    conn2.close()

    # 5) 历史窗口不足提示 (东财历史通常 1+ 年, 缺失一般是新股/长期停牌)
    note = None
    if len(daily_list) < days:
        note = f'历史数据不足, 实际返回 {len(daily_list)} 个交易日 (请求 {days})'

    return jsonify({
        'status': 'success',
        'ts_code': ts_code,
        'code': code,
        'name': name,
        'summary': summary,
        'daily': daily_list,
        'periods': periods,
        'window_limit_note': note,
    })


# 主力净流入同步状态 (今日 JSONP + 历史 Tushare, 一次 ~5289 行)
_AKSYNC_STATE = {
    'running': False, 'started_at': None, 'finished_at': None,
    'total': 0, 'done': 0, 'success': 0, 'failed': 0,
    'errors': [], 'current_code': '', 'elapsed_sec': 0,
    'source': 'eastmoney-push2-jsonp', 'target_date': '',
}


def _parse_amount(s, numeric_unit='yuan'):
    """'5.38亿' / '9948.48万' / '20.01%' / 139.53 / 139.53 → 元
    numeric_unit 用于 AKShare/read_html 已经把单位文本剥掉的数字:
    - yuan: 数字本身就是元
    - wan: 数字本身是万元
    - yi: 数字本身是亿元
    - 涨跌幅/换手率带 '%' → 返回 % 数值
    """
    if s is None or s == '' or (isinstance(s, float) and (s != s)):
        return None
    if isinstance(s, (int, float)):
        multipliers = {'yuan': 1, 'wan': 1e4, 'yi': 1e8}
        return float(s) * multipliers.get(numeric_unit, 1)
    s = str(s).replace(',', '').strip()
    if s.endswith('亿'):
        try: return float(s[:-1]) * 1e8
        except: return None
    if s.endswith('万'):
        try: return float(s[:-1]) * 1e4
        except: return None
    if s.endswith('%'):
        try: return float(s[:-1])
        except: return None
    try: return float(s)
    except: return None


def _parse_pct(s):
    """'5.65%' / '5.65' / None → 5.65 (浮点). 无单位转换, % 数值本身."""
    if s is None or s == '' or (isinstance(s, float) and (s != s)):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(',', '').strip()
    if s.endswith('%'):
        s = s[:-1]
    try: return float(s)
    except: return None


def _ak_individual_to_db_rows(df, today):
    """ak.stock_fund_flow_individual() DataFrame → (rows_for_fund_flow, list of dicts)
    5189 行全市场, 字段: 序号/股票代码/股票简称/最新价/涨跌幅/换手率/流入资金/流出资金/净额/成交额
    注: 同花顺口径"净额" = 流入 - 流出 (总资金净流入, 非主力), 我们存为 main_net_inflow.
    单位: stock_fund_flow_individual 的金额字段是字符串 "5.38亿" / "9948.48万", _parse_amount 自动换算成元.
    """
    out = []
    for _, r in df.iterrows():
        try:
            code_int = int(r['股票代码'])
        except (ValueError, TypeError, KeyError):
            continue
        code = f'{code_int:06d}'
        ts_code = f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ'

        close = r.get('最新价')
        change_pct = _parse_pct(r.get('涨跌幅'))
        turnover = _parse_pct(r.get('换手率'))
        inflow = _parse_amount(r.get('流入资金'), numeric_unit='wan') or 0
        outflow = _parse_amount(r.get('流出资金'), numeric_unit='wan') or 0
        net = _parse_amount(r.get('净额'), numeric_unit='wan') or 0
        turnover_amt = _parse_amount(r.get('成交额'), numeric_unit='yi') or 0

        main_pct = (net / turnover_amt * 100) if turnover_amt else None
        out.append({
            'trade_date': today,
            'ts_code': ts_code,
            'code': code,
            'name': r.get('股票简称', ''),
            'close': close,
            'change_pct': change_pct,
            'main_net_inflow': net,
            'main_net_pct': main_pct,
            'super_net': None, 'super_pct': None,
            'big_net': None, 'big_pct': None,
            'mid_net': None, 'mid_pct': None,
            'small_net': None, 'small_pct': None,
            'source': 'akshare-10jqka',
            '_turnover_pct': turnover,
            '_turnover_amt': turnover_amt,
            '_inflow': inflow,
            '_outflow': outflow,
        })
    return out


def _save_individual_rows(rows):
    """rows → fund_flow 表 (DELETE+INSERT). 返回成功/失败数."""
    import math
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    succ = 0
    for r in rows:
        try:
            cur.execute('DELETE FROM fund_flow WHERE ts_code=? AND trade_date=?',
                         (r['ts_code'], r['trade_date']))
            def _f(v):
                if v is None: return None
                try:
                    x = float(v)
                    return None if math.isnan(x) else x
                except (ValueError, TypeError):
                    return None
            cur.execute('''INSERT INTO fund_flow
                (trade_date, ts_code, code, name, close, change_pct,
                 main_net_inflow, main_net_pct,
                 super_net, super_pct, big_net, big_pct,
                 mid_net, mid_pct, small_net, small_pct, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (r['trade_date'], r['ts_code'], r['code'], r['name'],
                 _f(r['close']), _f(r['change_pct']),
                 _f(r['main_net_inflow']) or 0.0, _f(r['main_net_pct']),
                 _f(r['super_net']), _f(r['super_pct']),
                 _f(r['big_net']), _f(r['big_pct']),
                 _f(r['mid_net']), _f(r['mid_pct']),
                 _f(r['small_net']), _f(r['small_pct']),
                 r['source']))
            succ += 1
        except Exception as e:
            print(f'[sync] insert {r.get("ts_code")} fail: {e}', flush=True)
    conn.commit()
    conn.close()
    return succ


def _save_em_fund_flow_rows(rows, today, source='eastmoney-push2'):
    """把 detail.html JSONP 拿到的 rows (raw f-code dict) 写入 fund_flow 表.
    detail.html 字段 → fund_flow 表列名映射:
      f12 code, f14 name, f2 close, f3 change_pct,
      f62 main_net_inflow, f184 main_net_pct,
      f66 super_net, f69 super_pct,
      f72 big_net, f75 big_pct,
      f78 mid_net, f81 mid_pct,
      f84 small_net, f87 small_pct
    Returns: 写入条数.
    """
    import math
    def _f(v):
        if v is None or v == '-' or v == '':
            return None
        try:
            x = float(v)
            return 0.0 if math.isnan(x) else x
        except (ValueError, TypeError):
            return None

    def _to_ts_code(code: str) -> str:
        code = str(code).strip()
        if not code or len(code) != 6:
            return f'{code}.SH' if code else ''
        return f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ'

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    count = 0
    for r in rows:
        code = str(r.get('f12', '')).strip()
        if not code or len(code) != 6:
            continue
        ts_code = _to_ts_code(code)
        name = r.get('f14', '')
        close = _f(r.get('f2'))
        change_pct = _f(r.get('f3'))
        main_net_inflow = _f(r.get('f62')) or 0.0
        main_net_pct = _f(r.get('f184'))
        super_net = _f(r.get('f66'))
        super_pct = _f(r.get('f69'))
        big_net = _f(r.get('f72'))
        big_pct = _f(r.get('f75'))
        mid_net = _f(r.get('f78'))
        mid_pct = _f(r.get('f81'))
        small_net = _f(r.get('f84'))
        small_pct = _f(r.get('f87'))

        cur.execute('DELETE FROM fund_flow WHERE ts_code=? AND trade_date=?', (ts_code, today))
        cur.execute('''INSERT INTO fund_flow
            (trade_date, ts_code, code, name, close, change_pct,
             main_net_inflow, main_net_pct,
             super_net, super_pct, big_net, big_pct,
             mid_net, mid_pct, small_net, small_pct, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (today, ts_code, code, name, close, change_pct,
             main_net_inflow, main_net_pct,
             super_net, super_pct, big_net, big_pct,
             mid_net, mid_pct, small_net, small_pct, source))
        count += 1
    conn.commit()
    conn.close()
    return count


def _em_sync_market_bg():
    """后台线程: 抓取今日主力净流入 (JSONP + 东财 detail.html).
    数据写入 fund_flow 表, trade_date = 今天. 每天点一次同步即积累一天历史.
    历史日期查询通过前端日历切换 (只是查 DB, 不重新抓).
    """
    import time as _t
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(__file__))
    from fetch_fund_flow_today import fetch_today_market

    t0 = _t.time()
    today = datetime.now().strftime('%Y%m%d')

    _AKSYNC_STATE.update({
        'running': True, 'started_at': datetime.now().isoformat(),
        'finished_at': None, 'total': 0, 'done': 0,
        'success': 0, 'failed': 0, 'errors': [],
        'current_code': f'准备同步 {today}...',
        'elapsed_sec': 0, 'source': 'eastmoney-push2',
        'target_date': today,
    })
    print(f'[em-sync] 开始: 同步 {today}', flush=True)

    def _cb(stage, **kw):
        if stage == 'start':
            # kw: actual_date, today
            ad = kw.get('actual_date', today)
            if ad != today:
                _AKSYNC_STATE['is_holiday'] = True
                _AKSYNC_STATE['actual_date'] = ad
                _AKSYNC_STATE['current_code'] = f'⏸ {today} 非交易日, 数据将归属到 {ad}'
            else:
                _AKSYNC_STATE['is_holiday'] = False
                _AKSYNC_STATE['actual_date'] = ad
        elif stage == 'total':
            _AKSYNC_STATE['total'] = kw.get('total', 0)
            _AKSYNC_STATE['current_code'] = f'总 {kw["total"]} 只, 开始翻页...'
        elif stage == 'page':
            pn = kw.get('pn', 0)
            total_pages = kw.get('total_pages', 0)
            rows_count = kw.get('rows_count', 0)
            _AKSYNC_STATE['done'] = rows_count
            _AKSYNC_STATE['failed'] = len(kw.get('failed', []))
            _AKSYNC_STATE['current_code'] = f'pn={pn}/{total_pages} 累计 {rows_count} 行'
        elif stage == 'done':
            _AKSYNC_STATE['done'] = kw.get('rows', 0)
            _AKSYNC_STATE['failed'] = len(kw.get('failed', []))
            ad = kw.get('actual_date', _AKSYNC_STATE.get('actual_date', today))
            _AKSYNC_STATE['current_code'] = (
                f'抓取完成 {kw["rows"]}/{kw["total"]} 行 (写入 {ad})'
                + (f' (失败 {len(kw["failed"])} 页)' if kw.get('failed') else '')
            )
        elif stage == 'already_synced':
            # 幂等守卫命中, 跳过抓取
            ad = kw.get('actual_date', today)
            cnt = kw.get('existing_count', 0)
            _AKSYNC_STATE['is_already_synced'] = True
            _AKSYNC_STATE['actual_date'] = ad
            _AKSYNC_STATE['current_code'] = f'✅ {ad} 已有数据 ({cnt} 行), 跳过抓取'
        elif stage == 'error':
            _AKSYNC_STATE['errors'].append(('eastmoney-push2', kw.get('message', '')))

    _AKSYNC_STATE['is_holiday'] = False
    _AKSYNC_STATE['is_already_synced'] = False
    _AKSYNC_STATE['actual_date'] = today
    try:
        rows, total, failed, actual_date = fetch_today_market(
            progress_callback=_cb, headless=False, verbose=True, skip_if_exists=True)
    except Exception as e:
        err_msg = f'{type(e).__name__}: {str(e)[:200]}'
        _AKSYNC_STATE['errors'].append(('eastmoney-push2', err_msg))
        _AKSYNC_STATE['failed'] = 1
        _AKSYNC_STATE['running'] = False
        _AKSYNC_STATE['finished_at'] = datetime.now().isoformat()
        _AKSYNC_STATE['elapsed_sec'] = round(_t.time() - t0, 1)
        print(f'[em-sync] 异常: {err_msg}', flush=True)
        return

    # 幂等守卫命中: 早退, 算成功不算错误
    if not rows and _AKSYNC_STATE.get('is_already_synced'):
        _AKSYNC_STATE['running'] = False
        _AKSYNC_STATE['finished_at'] = datetime.now().isoformat()
        _AKSYNC_STATE['elapsed_sec'] = round(_t.time() - t0, 1)
        print(f'[em-sync] ⏭ {actual_date} 已有数据, 跳过 ({_AKSYNC_STATE["elapsed_sec"]}s)', flush=True)
        return

    if not rows:
        _AKSYNC_STATE['failed'] = 1
        if not _AKSYNC_STATE['errors']:
            _AKSYNC_STATE['errors'].append(('eastmoney-push2', '抓取结果为空'))
    else:
        try:
            _AKSYNC_STATE['current_code'] = f'写入 DB {len(rows)} 行 ({actual_date})...'
            inserted = _save_em_fund_flow_rows(rows, actual_date, source='eastmoney-push2')
            _AKSYNC_STATE['success'] = inserted
            _AKSYNC_STATE['current_code'] = f'✅ {actual_date} 已写入 {inserted} 行'
            print(f'[em-sync] ✅ {actual_date} 写入 {inserted} 行', flush=True)
        except Exception as e:
            err_msg = f'写入 DB: {type(e).__name__}: {str(e)[:200]}'
            _AKSYNC_STATE['errors'].append(('db.write', err_msg))
            _AKSYNC_STATE['failed'] = 1
            print(f'[em-sync] 写 DB 失败: {err_msg}', flush=True)

    _AKSYNC_STATE['elapsed_sec'] = round(_t.time() - t0, 1)
    _AKSYNC_STATE['running'] = False
    _AKSYNC_STATE['finished_at'] = datetime.now().isoformat()
    print(f'[em-sync] 完成: {today} success={_AKSYNC_STATE["success"]} elapsed={_AKSYNC_STATE["elapsed_sec"]}s', flush=True)




@app.route('/api/flow/dates', methods=['GET'])
def api_flow_dates():
    """返回 fund_flow 表里有数据的 distinct trade_date 列表 (倒序).
    默认 source=eastmoney-push2. 用于前端日历控件的上一日/下一日导航.
    """
    source = request.args.get('source', 'eastmoney-push2').strip()
    conn = sqlite3.connect(DB_PATH)
    if source == 'all':
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM fund_flow ORDER BY trade_date DESC"
        ).fetchall()]
    else:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM fund_flow WHERE source=? ORDER BY trade_date DESC",
            (source,)
        ).fetchall()]
    conn.close()
    return jsonify({'dates': dates, 'count': len(dates), 'source': source})


@app.route('/api/flow/sync-market', methods=['POST'])
def api_flow_sync_market():
    """启动后台抓取今日主力净流入 (JSONP). 立即返回 'started'.
    每天点一次同步, trade_date 自动标记今天, 写入 fund_flow 表.
    历史日期切换查询只读 DB, 不需要再抓.
    """
    if _AKSYNC_STATE['running']:
        return jsonify({'status': 'already_running', 'state': _AKSYNC_STATE})
    import threading as _th
    t = _th.Thread(target=_em_sync_market_bg, daemon=True)
    t.start()
    return jsonify({'status': 'started', 'state': _AKSYNC_STATE})


@app.route('/api/flow/sync-status', methods=['GET'])
def api_flow_sync_status():
    """查询 AKShare 全市场拉取状态. 前端 setInterval 轮询."""
    return jsonify(_AKSYNC_STATE)


@app.route('/api/flow/industry', methods=['GET'])
def api_flow_industry():
    """行业资金流 (ak.stock_fund_flow_industry). 90 个行业. 缓存 5 分钟."""
    symbol = request.args.get('symbol', '即时')
    try:
        df = _ak_call(ak.stock_fund_flow_industry, symbol=symbol)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                'name': r.get('行业', ''),
                'index': r.get('行业指数'),
                'change_pct': r.get('行业-涨跌幅'),
                'inflow': _parse_amount(r.get('流入资金'), numeric_unit='yi') or 0,
                'outflow': _parse_amount(r.get('流出资金'), numeric_unit='yi') or 0,
                'net': _parse_amount(r.get('净额'), numeric_unit='yi') or 0,
                'company_count': r.get('公司家数'),
                'leading_stock': r.get('领涨股', ''),
                'leading_change_pct': r.get('领涨股-涨跌幅'),
                'leading_price': r.get('当前价'),
            })
        rows.sort(key=lambda x: (x.get('net') or 0), reverse=True)
        return jsonify({
            'status': 'success',
            'count': len(rows),
            'symbol': symbol,
            'rows': rows,
            'source': 'akshare-10jqka / 东方财富',
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:200]}), 500


@app.route('/api/flow/concept', methods=['GET'])
def api_flow_concept():
    """概念资金流 (ak.stock_fund_flow_concept). 385 个概念."""
    symbol = request.args.get('symbol', '即时')
    try:
        df = _ak_call(ak.stock_fund_flow_concept, symbol=symbol)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                'name': r.get('行业', ''),
                'index': r.get('行业指数'),
                'change_pct': r.get('行业-涨跌幅'),
                'inflow': _parse_amount(r.get('流入资金'), numeric_unit='yi') or 0,
                'outflow': _parse_amount(r.get('流出资金'), numeric_unit='yi') or 0,
                'net': _parse_amount(r.get('净额'), numeric_unit='yi') or 0,
                'company_count': r.get('公司家数'),
                'leading_stock': r.get('领涨股', ''),
                'leading_change_pct': r.get('领涨股-涨跌幅'),
                'leading_price': r.get('当前价'),
            })
        rows.sort(key=lambda x: (x.get('net') or 0), reverse=True)
        return jsonify({
            'status': 'success',
            'count': len(rows),
            'symbol': symbol,
            'rows': rows,
            'source': 'akshare-10jqka / 东方财富',
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:200]}), 500


@app.route('/api/flow/big-deal', methods=['GET'])
def api_flow_big_deal():
    """大宗交易 (ak.stock_fund_flow_big_deal). 5000 条."""
    try:
        df = _ak_call(ak.stock_fund_flow_big_deal)
        rows = []
        for _, r in df.iterrows():
            row = {}
            for c in df.columns:
                v = r.get(c)
                if isinstance(v, str) and any(unit in v for unit in ['亿', '万']):
                    row[c] = _parse_amount(v, numeric_unit='yi')  # 元
                elif isinstance(v, str) and '%' in v:
                    row[c] = _parse_pct(v)
                else:
                    row[c] = v
            rows.append(row)
        return jsonify({
            'status': 'success',
            'count': len(rows),
            'rows': rows,
            'source': 'akshare-10jqka / 东方财富',
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:200]}), 500


@app.route('/api/flow/sync-holdings', methods=['POST'])
def api_flow_sync_holdings():
    """对每个 open 持仓调 AKShare 拉历史, 写入 fund_flow."""
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    days = min(int(body.get('days', 120)), 200)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    holdings = conn.execute(
        "SELECT ts_code, SUBSTR(ts_code, 1, 6) AS code, name FROM positions WHERE closed_at IS NULL"
    ).fetchall()
    conn.close()

    if not holdings:
        return jsonify({'status': 'no_holdings', 'stocks': [], 'elapsed_sec': 0})

    t0 = time.time()
    results = []
    for h in holdings:
        market = 'sh' if h['ts_code'].endswith('.SH') else 'sz'
        try:
            df = _ak_call(ak.stock_individual_fund_flow, stock=h['code'], market=market)
            df = _ak_fund_flow_renamed(df)
            if 'ts_code' not in df.columns:
                df['ts_code'] = h['ts_code']
            if 'code' not in df.columns:
                df['code'] = h['code']
            cnt = _save_fund_flow_df(df, source='akshare')
            results.append({'code': h['code'], 'name': h['name'], 'fetched': cnt, 'status': 'success'})
        except Exception as e:
            results.append({'code': h['code'], 'name': h['name'], 'fetched': 0, 'status': str(e)})
        time.sleep(0.3)  # 防限速

    elapsed = round(time.time() - t0, 1)
    return jsonify({'status': 'success', 'stocks': results, 'elapsed_sec': elapsed})


# ============ 选股推荐 ============

def _add_col_if_missing(c, table, col, ctype):
    """如果表里没有这个列就加上 (兼容旧版表)"""
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")


def _calc_ma(closes, period):
    """计算移动平均线"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _calc_ma_slope(closes, period):
    """计算均线斜率 (近 period 天的斜率, 度数). > 0 上升, < 0 下降"""
    if len(closes) < period + 1:
        return None
    # 用首尾两点求斜率 (deg) = atan2(dy, dx) * 180/pi
    import math
    y1 = sum(closes[:period]) / period
    y2 = sum(closes[-period:]) / period
    slope_rad = math.atan2(y2 - y1, period - 1)
    return slope_rad * 180 / math.pi


def _fund_flow_window(ts_code, end_date, window):
    """拉 ts_code 在 end_date 之前 window 个交易日的主资金净额列表 (新→旧).
    返回 main_net_inflow 列表; 长度 < window 表示数据不足.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute("""
        SELECT main_net_inflow FROM fund_flow
        WHERE ts_code = ? AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT ?
    """, (ts_code, end_date, window)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _flow_consecutive_sign(ts_code, end_date, n, want_positive=True):
    """最近 n 日全部主力净流入(>0) 或 净流出(<0). 数据不足返回 False."""
    flows = _fund_flow_window(ts_code, end_date, n)
    if len(flows) < n:
        return False
    return all(v > 0 for v in flows) if want_positive else all(v < 0 for v in flows)


def _flow_turn(ts_code, end_date, n, want_positive):
    """最近 n 日由反向转正向 (want_positive=True) 或 反之.
    例如 n=3: 前 2 日 <= 0, 最近 1 日 > 0 (want_positive=True).
    """
    flows = _fund_flow_window(ts_code, end_date, n)
    if len(flows) < n:
        return False
    opposite = (lambda v: v <= 0) if want_positive else (lambda v: v >= 0)
    return all(opposite(v) for v in flows[:-1]) and (flows[-1] > 0 if want_positive else flows[-1] < 0)


def _is_new_high(closes, lookback=60):
    """创 N 日新高 (收盘价 >= 过去 lookback 日内的最高收盘价)"""
    if len(closes) < lookback + 1:
        return False
    return closes[-1] >= max(closes[-lookback:-1])


def _is_breakout_confirmed(closes, lookback=20):
    """真突破: 最近 3 日收盘都站上 N 日高点 + 当前 > 高点"""
    if len(closes) < lookback + 4:
        return False, None
    recent_high = max(closes[-lookback:-3])  # 突破前的高点
    if closes[-1] <= recent_high:
        return False, None
    # 最近 3 日收盘都在高点之上
    if all(c > recent_high for c in closes[-3:]):
        return True, recent_high
    return False, None


def _volume_ratio(closes_or_vols, idx=-1, lookback=5):
    """当前量/过去 N 日均量的比值"""
    if len(closes_or_vols) < abs(idx) + lookback:
        return None
    recent = closes_or_vols[idx]
    past = closes_or_vols[idx - lookback: idx]
    avg = sum(past) / len(past) if past else 0
    return recent / avg if avg > 0 else None


def _flow_days_available(end_date):
    """fund_flow 表在 end_date 及之前, eastmoney-push2 源有多少"有效"资金流数据日。

    口径跟主力净流入 tab 默认源一致 (source=eastmoney-push2),
    main_net_inflow != 0 过滤 ifind 那种全 0 的空壳日 (tushare 已清, 不会再出现).
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    row = conn.execute("""
        SELECT COUNT(DISTINCT trade_date) FROM fund_flow
        WHERE trade_date <= ?
          AND source = 'eastmoney-push2'
          AND main_net_inflow IS NOT NULL
          AND main_net_inflow != 0
    """, (end_date,)).fetchone()
    conn.close()
    return row[0] if row else 0


def _compute_picks(trade_date=None, ts_codes=None):
    """按《均线 + 资金流》文档计算选股推荐.

    双维度评分:
      1) 均线: MA5/10/20/30 + 斜率 + 多头排列 + 金叉死叉
      2) 资金流: 当日主力净流入 + 3/5/10/20 日累计主力净流入 + 5 档占比

    数据不足时降级: 缺几天的累计就跳过哪个维度, 不强行估算.

    Args:
        trade_date: 计算日期 (YYYYMMDD), None = 最新
        ts_codes: 限定只算这些 ts_code (None = 全市场). 用于自选股 sub-tab 只算用户加的代码.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 确定计算日期
    if not trade_date:
        row = conn.execute('SELECT MAX(trade_date) as d FROM stock_daily').fetchone()
        if not row or not row['d']:
            conn.close()
            return []
        trade_date = row['d']

    # 拉最近 35 天日线数据 (够算 MA30 + 斜率 + 各种窗口)
    dates_30 = [r[0] for r in conn.execute("""
        SELECT DISTINCT trade_date FROM stock_daily
        WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 35
    """, (trade_date,)).fetchall()]
    if trade_date not in dates_30:
        conn.close()
        return []

    # fund_flow 可用天数 (决定哪些累计资金流维度能算)
    # 口径 B: 只数默认源 eastmoney-push2, 跟主力净流入 tab 一致
    # (ifind 全 0 是空壳, tushare 已被清掉, 都不再参与累计算法)
    flow_dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM fund_flow '
        'WHERE trade_date <= ? '
        '  AND source = \'eastmoney-push2\' '
        '  AND main_net_inflow IS NOT NULL AND main_net_inflow != 0 '
        'ORDER BY trade_date DESC',
        (trade_date,)
    ).fetchall()]
    flow_days = len(flow_dates)
    # 各个窗口能不能算
    can_3d = flow_days >= 3
    can_5d = flow_days >= 5
    can_10d = flow_days >= 10
    can_20d = flow_days >= 20

    # 拉股票日线 (ts_codes 限定时只算给定代码, 否则全市场)
    placeholders = ','.join(['?' for _ in dates_30])
    sql_args = list(dates_30)
    if ts_codes:
        codes_ph = ','.join(['?' for _ in ts_codes])
        rows = conn.execute(f"""
            SELECT ts_code, name, trade_date, close, change, volume
            FROM stock_daily
            WHERE trade_date IN ({placeholders}) AND ts_code IN ({codes_ph})
            ORDER BY ts_code, trade_date DESC
        """, sql_args + list(ts_codes)).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT ts_code, name, trade_date, close, change, volume
            FROM stock_daily
            WHERE trade_date IN ({placeholders})
            ORDER BY ts_code, trade_date DESC
        """, sql_args).fetchall()

    # 按股票分组
    stock_data = defaultdict(list)
    for r in rows:
        stock_data[r['ts_code']].append({
            'date': r['trade_date'],
            'close': r['close'],
            'change': r['change'],
            'volume': r['volume'],
            'name': r['name']
        })

    picks = []
    for ts_code, data_list in stock_data.items():
        if len(data_list) < 30:
            continue
        data_list.sort(key=lambda x: x['date'], reverse=True)

        latest = data_list[0]
        if not latest['close'] or latest['close'] <= 0:
            continue
        if not latest['volume'] or latest['volume'] == 0:
            continue
        if latest['name'] and ('*ST' in latest['name'] or 'ST' in latest['name']):
            continue

        # ===== 均线 =====
        closes = [d['close'] for d in reversed(data_list)]
        ma5 = _calc_ma(closes, 5)
        ma10 = _calc_ma(closes, 10)
        ma20 = _calc_ma(closes, 20)
        ma30 = _calc_ma(closes, 30)
        if not all([ma5, ma10, ma20, ma30]):
            continue

        ma5_slope = _calc_ma_slope(closes, 5)
        ma10_slope = _calc_ma_slope(closes, 10)
        ma20_slope = _calc_ma_slope(closes, 20)
        ma30_slope = _calc_ma_slope(closes, 30)

        # ===== 资金流 (多窗口累计) =====
        # 取各窗口的主资金净额列表 (新→旧), 然后求和 (注意: 只用 <= trade_date 的数据)
        flows = {}
        if can_3d:
            flows['3d'] = sum(_fund_flow_window(ts_code, trade_date, 3))
        if can_5d:
            flows['5d'] = sum(_fund_flow_window(ts_code, trade_date, 5))
        if can_10d:
            flows['10d'] = sum(_fund_flow_window(ts_code, trade_date, 10))
        if can_20d:
            flows['20d'] = sum(_fund_flow_window(ts_code, trade_date, 20))
        # 当日资金流详情
        today_flow = _fund_flow_window(ts_code, trade_date, 1)
        main_net_today = today_flow[0] if today_flow else None
        # 当日 5 档 + 占比 (来自 fund_flow 当日行)
        flow_today_row = conn.execute("""
            SELECT main_net_pct, super_net, super_pct, big_net, big_pct,
                   mid_net, mid_pct, small_net, small_pct
            FROM fund_flow WHERE ts_code = ? AND trade_date = ?
        """, (ts_code, trade_date)).fetchone()
        main_net_pct_today = flow_today_row['main_net_pct'] if flow_today_row else None
        super_net = flow_today_row['super_net'] if flow_today_row else None
        super_pct = flow_today_row['super_pct'] if flow_today_row else None
        big_net = flow_today_row['big_net'] if flow_today_row else None
        big_pct = flow_today_row['big_pct'] if flow_today_row else None
        mid_net = flow_today_row['mid_pct'] if flow_today_row else None
        mid_pct = flow_today_row['mid_pct'] if flow_today_row else None
        small_net = flow_today_row['small_net'] if flow_today_row else None
        small_pct = flow_today_row['small_pct'] if flow_today_row else None

        # 涨跌幅
        change_pct = 0
        if len(data_list) >= 2:
            prev_close = data_list[1]['close']
            if prev_close and prev_close > 0:
                change_pct = (latest['close'] - prev_close) / prev_close * 100

        # ===== 评分 (按《均线+资金流.md》文档) =====
        score = 0
        reasons = []
        buy_signals = 0
        sell_signals = 0

        # ---------- 章节三: 均线信号 ----------
        # 多头排列 (MA5 > MA10 > MA20 > MA30) +30
        if ma5 > ma10 > ma20 > ma30:
            score += 30
            reasons.append('多头排列 MA5>MA10>MA20>MA30')
        elif ma5 > ma10 > ma20:
            score += 18
            reasons.append('短中期多头排列 (MA5>MA10>MA20)')
        # 空头排列
        if ma5 < ma10 < ma20 < ma30:
            score -= 20
            reasons.append('空头排列 MA5<MA10<MA20<MA30')
            sell_signals += 1

        # 价格在均线之上
        if latest['close'] > ma5: score += 5; reasons.append('站上MA5')
        if latest['close'] > ma10: score += 5; reasons.append('站上MA10')
        if latest['close'] > ma20: score += 5; reasons.append('站上MA20')
        if latest['close'] > ma30: score += 5; reasons.append('站上MA30')

        # MA5 陡峭向上 (斜率 > 45° 文档要求) +8
        if ma5_slope is not None and ma5_slope > 45:
            score += 8
            reasons.append(f'MA5陡峭向上 ({ma5_slope:.1f}°>45°)')
        elif ma5_slope is not None and ma5_slope > 30:
            score += 3
            reasons.append(f'MA5向上 ({ma5_slope:.1f}°)')

        # ---------- 章节三: 金叉死叉 (文档表) ----------
        prev_ma5 = sum(closes[-6:-1]) / 5
        prev_ma10 = sum(closes[-11:-1]) / 10
        prev_ma30 = sum(closes[-31:-1]) / 30

        # 5日上穿10日 +15 (验证: 当日主力净流入占比>3%)
        if prev_ma5 <= prev_ma10 and ma5 > ma10:
            if main_net_pct_today is not None and main_net_pct_today > 3:
                score += 15; reasons.append('5日金叉10日 ★ 主力净流入占比>3%'); buy_signals += 1
            else:
                score += 8; reasons.append('5日金叉10日 (资金流未验证)')

        # 10日上穿30日 +20 (验证: 30日拐头 + 10日累计>5000万)
        if prev_ma10 <= prev_ma30 and ma10 > ma30:
            if ma30_slope is not None and ma30_slope > 0 and can_10d and flows.get('10d', 0) > 50000000:
                score += 20; reasons.append('10日金叉30日 ★★★ (30日拐头+10日资金>5000万)'); buy_signals += 1
            elif can_10d:
                score += 10; reasons.append('10日金叉30日 (资金验证待 N≥10天数据)')

        # 10日下穿30日 (死叉) -30 (验证: 30日累计主力净流入由正转负)
        if prev_ma10 >= prev_ma30 and ma10 < ma30:
            if can_20d and flows.get('20d', 0) < 0:
                score -= 30; reasons.append('20日死叉30日 ★★★ (20日资金<0,无条件清仓)'); sell_signals += 1
            elif can_20d:
                score -= 18; reasons.append('20日死叉30日 (资金验证待 N≥20天数据)')
            else:
                score -= 10; reasons.append('20日死叉30日 (资金数据待积累)')

        # ---------- 章节七: 黄金组合 (4 个) ----------
        # 黄金 1: 股价突破 20 日线 + 20 日线拐头向上 + 连续 3 日主力净流入
        if (latest['close'] > ma20 and ma20_slope is not None and ma20_slope > 0
            and can_3d and _flow_consecutive_sign(ts_code, trade_date, 3, want_positive=True)):
            score += 25
            reasons.append('黄金1: 突破20日线+连续3日资金流入 ★★★')
            buy_signals += 1
        elif latest['close'] > ma20 and ma20_slope is not None and ma20_slope > 0:
            reasons.append('黄金1部分: 突破20日线+拐头向上 (需 N≥3天连续资金数据)')

        # 黄金 2: 强势股回踩 10 日线 + 缩量止跌 + 当日主力资金由流出转流入
        # 判定: close 在 ma10 附近 (回踩), 当日由负转正
        near_ma10 = abs(latest['close'] - ma10) / ma10 < 0.02  # ±2%
        if near_ma10 and can_3d and _flow_turn(ts_code, trade_date, 3, want_positive=True):
            score += 20
            reasons.append('黄金2: 回踩MA10+资金流出转流入 ★★')
            buy_signals += 1
        elif near_ma10:
            reasons.append('黄金2部分: 回踩MA10 (资金由流出转流入需 N≥3天数据)')

        # 黄金 3: 10 日上穿 30 日线 + 30 日线拐头向上 + 10 日主力净流入为正
        # (跟前面 10日金叉30日 + 资金验证 重复, 这里升级为 ★★★ 信号)
        if (prev_ma10 <= prev_ma30 and ma10 > ma30
            and ma30_slope is not None and ma30_slope > 0
            and can_10d and flows.get('10d', 0) > 0):
            # 已计入上面的金叉信号, 这里不重复加分
            reasons.append('黄金3: 10日金叉30日+30日拐头+10日资金>0 ★★★')

        # 黄金 4: 股价跌破 5 日线但 10 日线支撑有效 + 主力资金逆势流入
        # 判定: close 跌破 ma5 但仍在 ma10 上方 + 当日资金 > 0
        if (latest['close'] < ma5 and latest['close'] > ma10
            and main_net_today is not None and main_net_today > 0):
            score += 15
            reasons.append('黄金4: 跌破MA5但MA10支撑+主力逆势流入 ★★ (洗盘结束)')
            buy_signals += 1

        # ---------- 章节七: 死亡组合 (4 个) ----------
        # 死亡 1: 跌破 10 日线 + 5 日资金由正转负
        if latest['close'] < ma10 and can_5d and _flow_turn(ts_code, trade_date, 3, want_positive=False):
            score -= 25
            reasons.append('死亡1: 跌破MA10+5日资金由正转负 ★★★ (无条件离场)')
            sell_signals += 1
        elif latest['close'] < ma10:
            reasons.append('死亡1部分: 跌破MA10 (资金由正转负需 N≥5天数据)')

        # 死亡 2: 20 日下穿 30 日线 + 20 日资金由正转负 (跟 10日下穿30日死叉合并处理)
        # 单独标记
        if (prev_ma10 >= prev_ma30 and ma10 < ma30
            and can_20d and flows.get('20d', 0) < 0):
            reasons.append('死亡2: 20日死叉30日+20日资金<0 ★★★')

        # 死亡 3: 股价创新高但主力资金连续 3 日净流出
        if (change_pct > 0 and _is_new_high(closes, 60)
            and can_3d and _flow_consecutive_sign(ts_code, trade_date, 3, want_positive=False)):
            score -= 20
            reasons.append('死亡3: 创新高+连续3日主力净流出 ★★★ (顶背离)')
            sell_signals += 1
        elif _is_new_high(closes, 60):
            reasons.append('死亡3部分: 创新高 (连续3日流出需 N≥3天数据)')

        # 死亡 4: 多头排列但 20 日/30 日资金开始流出
        if (ma5 > ma10 > ma20 > ma30  # 多头排列
            and can_10d and can_20d
            and flows.get('10d', 0) < 0 and flows.get('20d', 0) < 0):
            score -= 15
            reasons.append('死亡4: 多头排列但10日/20日资金均流出 ★★ (趋势即将反转)')
            sell_signals += 1
        elif ma5 > ma10 > ma20 > ma30:
            if not can_20d:
                reasons.append('死亡4待验证: 多头排列 (20日资金验证需 N≥20天数据)')

        # ---------- 章节六: 真假突破识别 ----------
        # 真突破: 站稳 3 日 + 量 > 50% + 主力净流入占比 > 15%
        is_breakout, brk_level = _is_breakout_confirmed(closes, 20)
        if is_breakout and main_net_pct_today is not None and main_net_pct_today > 15:
            score += 20
            reasons.append(f'真突破确认: 站稳3日+主力净流入占比{main_net_pct_today:.1f}% (>15%) ★★★')
            buy_signals += 1
        elif is_breakout:
            reasons.append(f'突破中: 站稳3日但主力净流入占比{main_net_pct_today or "?"}% (未达15%阈值,可能是诱多)')

        # ---------- 章节六: 真假洗盘识别 ----------
        # 真洗盘: 5 日累计仍为正 (回调时主力暗中承接)
        if latest['close'] < ma10 and can_5d and flows.get('5d', 0) > 0:
            score += 8
            reasons.append('真洗盘: 跌破MA10但5日累计资金仍>0 (主力承接)')

        # ---------- 章节五: 葛兰碧八大法则 (8 个) ----------
        bias = (latest['close'] - ma5) / ma5 * 100 if ma5 else 0
        bias10 = (latest['close'] - ma10) / ma10 * 100 if ma10 else 0
        bias20 = (latest['close'] - ma20) / ma20 * 100 if ma20 else 0
        bias30 = (latest['close'] - ma30) / ma30 * 100 if ma30 else 0

        # 1. 突破买入 (中期趋势启动): 突破 MA20/MA30 + 量放大 + 资金>1亿
        volumes = [d['volume'] for d in reversed(data_list)]
        vol_ratio = _volume_ratio(volumes) if len(volumes) >= 6 else None
        if (latest['close'] > ma20 and prev_ma5 <= prev_ma10 and ma5 > ma10
            and vol_ratio is not None and vol_ratio > 1.5
            and main_net_today is not None and main_net_today > 1e8):
            score += 18
            reasons.append(f'葛兰碧突破买入: 突破MA20+量>50%+主力>1亿 ★★')
            buy_signals += 1

        # 2. 回踩不破买入: 回踩 MA10/MA20 不破 + 资金流入
        if ((abs(bias10) < 3 and latest['close'] > ma10)
            and main_net_today is not None and main_net_today > 0):
            score += 12
            reasons.append(f'葛兰碧回踩不破: 回踩MA10不破+资金流入 ★')
            buy_signals += 1

        # 3. 假跌破买入 (主力洗盘): 跌破但快速收回 + 缩量 + 实际资金流入
        # 判定: 盘中破 MA5/MA10 但当前 close 在均线上方 + 当日资金>0
        if (latest['close'] < ma5 * 1.005 and latest['close'] > ma5 * 0.99
            and main_net_today is not None and main_net_today > 0):
            reasons.append('葛兰碧假跌破: 触及MA5后收回+资金流入 (洗盘结束信号) ★')

        # 4. 超跌反弹: 偏离 MA5 15% 以上 + 当日资金开始流入
        if bias < -15 and main_net_today is not None and main_net_today > 0:
            score += 10
            reasons.append(f'葛兰碧超跌反弹: 偏离MA5 {bias:.1f}%+资金流入 ★')
            buy_signals += 1

        # 5. 跌破卖出: 跌破 MA20/MA30 + 资金流出
        if ((latest['close'] < ma20 or latest['close'] < ma30)
            and main_net_today is not None and main_net_today < 0):
            score -= 15
            reasons.append(f'葛兰碧跌破卖出: 跌破MA20/MA30+资金流出 ★★')
            sell_signals += 1

        # 6. 反弹不过卖出: 反弹至 MA10/MA20 受阻 + 资金流出
        # 判定: 距离 MA10/MA20 较近 (反弹到位) + 资金流出
        if ((abs(bias10) < 2 or abs(bias20) < 2)
            and latest['close'] < ma10
            and main_net_today is not None and main_net_today < 0):
            score -= 10
            reasons.append(f'葛兰碧反弹不过: 反弹至MA10/MA20受阻+资金流出 ★')
            sell_signals += 1

        # 7. 假突破卖出 (诱多): 突破 MA 但主力净流入占比 < 5% (散户主导)
        if is_breakout and main_net_pct_today is not None and main_net_pct_today < 5:
            score -= 15
            reasons.append(f'葛兰碧假突破: 突破但主力净流入占比仅{main_net_pct_today:.1f}% (诱多嫌疑) ★★')
            sell_signals += 1

        # 8. 超涨卖出: 偏离 MA5 15% 以上 + 当日资金开始流出
        if bias > 15 and main_net_today is not None and main_net_today < 0:
            score -= 10
            reasons.append(f'葛兰碧超涨: 偏离MA5 {bias:.1f}%+资金流出 ★')
            sell_signals += 1

        # ---------- 章节六: 真假突破 - 假突破识别 ----------
        # 假突破特征: 突破时放量但随后快速萎缩 + 主力净流入占比低
        # 简化判定: 突破但当日主力占比 < 5% → 标记假突破嫌疑
        if is_breakout and main_net_pct_today is not None and main_net_pct_today < 5:
            # 已在 葛兰碧假突破 里加过分, 这里补充"次日确认"逻辑位
            reasons.append(f'真假突破: 突破但资金占比{main_net_pct_today:.1f}% < 5% (散户主导,警惕诱多)')

        # ---------- 章节六: 真假洗盘 - 真出货识别 ----------
        # 真出货特征: 跌破均线 + 5 日累计资金流出 + 上涨时缩量
        if (latest['close'] < ma10 and can_5d and flows.get('5d', 0) < 0
            and vol_ratio is not None and vol_ratio < 1):
            score -= 15
            reasons.append('真出货: 跌破MA10+5日累计资金流出+上涨缩量 ★★')
            sell_signals += 1

        # ---------- 章节二/三: 资金流累计信号 ----------
        # 当日主力净流入占比>3% (短线启动)
        if main_net_pct_today is not None and main_net_pct_today > 3:
            score += 15
            reasons.append(f'当日主力净流入占比 {main_net_pct_today:.1f}% (>3% 启动信号)')
            buy_signals += 1

        # 当日主力净流出占比>2% (短线走弱)
        if main_net_pct_today is not None and main_net_pct_today < -2:
            score -= 15
            reasons.append(f'当日主力净流出占比 {main_net_pct_today:.1f}% (短线走弱)')
            sell_signals += 1

        # 5 日累计主力净流入 (10 日线对应)
        if can_5d:
            v = flows['5d']
            if v > 0:
                score += 10
                reasons.append(f'5日累计主力净流入 {v/1e8:.2f}亿 (波段健康)')
            else:
                score -= 10
                reasons.append(f'5日累计主力净流出 {v/1e8:.2f}亿 (波段走坏)')
                sell_signals += 1
        else:
            reasons.append(f'5日累计资金: 待 N≥5天数据 (当前 {flow_days}天)')

        # 10 日累计主力净流入 (20 日线对应)
        if can_10d:
            v = flows['10d']
            if v > 0:
                score += 12
                reasons.append(f'10日累计主力净流入 {v/1e8:.2f}亿 (中期强势)')
            else:
                score -= 12
                reasons.append(f'10日累计主力净流出 {v/1e8:.2f}亿 (中期转弱)')
                sell_signals += 1
        else:
            reasons.append(f'10日累计资金: 待 N≥10天数据 (当前 {flow_days}天)')

        # 20 日累计主力净流入 (30 日线对应)
        if can_20d:
            v = flows['20d']
            if v > 0:
                score += 15
                reasons.append(f'20日累计主力净流入 {v/1e8:.2f}亿 (机构加仓)')
            else:
                score -= 15
                reasons.append(f'20日累计主力净流出 {v/1e8:.2f}亿 (机构离场)')
                sell_signals += 1
        else:
            reasons.append(f'20日累计资金: 待 N≥20天数据 (当前 {flow_days}天)')

        # ---------- 量价信号 ----------
        if 0 < change_pct <= 5:
            score += 5; reasons.append(f'温和上涨 {change_pct:.2f}%')
        elif 5 < change_pct <= 9.5:
            score += 3; reasons.append(f'强势上涨 {change_pct:.2f}%')

        # ---------- 信号类型 ----------
        if sell_signals > buy_signals:
            signal_type = 'sell'
        elif buy_signals > 0 and score > 0:
            signal_type = 'buy'
        else:
            signal_type = 'hold'

        # ---------- 评分<0 也保留 (含死亡组合) ----------
        picks.append({
            'ts_code': ts_code,
            'name': latest['name'] or ts_code.split('.')[0],
            'trade_date': trade_date,  # 入选日, 入选后涨幅的参考基准
            'close': latest['close'],
            'change_pct': round(change_pct, 2),
            'ma5': round(ma5, 2), 'ma10': round(ma10, 2),
            'ma20': round(ma20, 2), 'ma30': round(ma30, 2),
            'ma5_slope': round(ma5_slope, 1) if ma5_slope is not None else None,
            'ma10_slope': round(ma10_slope, 1) if ma10_slope is not None else None,
            'ma20_slope': round(ma20_slope, 1) if ma20_slope is not None else None,
            'ma30_slope': round(ma30_slope, 1) if ma30_slope is not None else None,
            'main_net_today': main_net_today,
            'main_net_pct_today': main_net_pct_today,
            'main_net_3d': flows.get('3d'),
            'main_net_5d': flows.get('5d'),
            'main_net_10d': flows.get('10d'),
            'main_net_20d': flows.get('20d'),
            'super_net': super_net, 'super_pct': super_pct,
            'big_net': big_net, 'big_pct': big_pct,
            'mid_net': mid_net, 'mid_pct': mid_pct,
            'small_net': small_net, 'small_pct': small_pct,
            'score': round(score, 1),
            'signal_type': signal_type,
            'flow_days_available': flow_days,
            'reasons': reasons,
        })

    conn.close()

    # 按评分绝对值降序 (推荐排序按 score 降序, 卖出信号靠后)
    picks.sort(key=lambda x: x['score'], reverse=True)
    return picks[:100]  # 返回 top 100, buy/sell 都包含 (前端 limit=100)


@app.route('/api/picks/sync', methods=['POST'])
def api_picks_sync():
    """触发选股推荐计算。支持 ?date=YYYYMMDD 指定日期。"""
    try:
        req_date = request.json.get('date', '') if request.is_json else ''
        picks = _compute_picks(req_date or None)

        if not picks:
            return jsonify({'status': 'success', 'message': '无推荐股票', 'count': 0})

        # 保存到数据库
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        if req_date:
            trade_date = req_date
        else:
            latest_row = c.execute('SELECT MAX(trade_date) FROM stock_daily').fetchone()
            trade_date = latest_row[0] if latest_row and latest_row[0] else datetime.now().strftime('%Y%m%d')

        # 清除旧数据
        c.execute('DELETE FROM stock_picks WHERE trade_date = ?', (trade_date,))

        # 插入新数据 (按文档字段集)
        for p in picks:
            c.execute('''INSERT OR REPLACE INTO stock_picks
                (trade_date, ts_code, name, close, change_pct,
                 ma5, ma10, ma20, ma30,
                 ma5_slope, ma10_slope, ma20_slope, ma30_slope,
                 main_net_today, main_net_pct_today,
                 main_net_3d, main_net_5d, main_net_10d, main_net_20d,
                 super_net, super_pct, big_net, big_pct,
                 mid_net, mid_pct, small_net, small_pct,
                 score, signal_type, flow_days_available, reasons_json)
                VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?)''',
                (trade_date, p['ts_code'], p['name'], p['close'], p['change_pct'],
                 p['ma5'], p['ma10'], p['ma20'], p['ma30'],
                 p['ma5_slope'], p['ma10_slope'], p['ma20_slope'], p['ma30_slope'],
                 p['main_net_today'], p['main_net_pct_today'],
                 p['main_net_3d'], p['main_net_5d'], p['main_net_10d'], p['main_net_20d'],
                 p['super_net'], p['super_pct'], p['big_net'], p['big_pct'],
                 p['mid_net'], p['mid_pct'], p['small_net'], p['small_pct'],
                 p['score'], p['signal_type'], p['flow_days_available'],
                 json.dumps(p['reasons'], ensure_ascii=False)))

        conn.commit()
        conn.close()

        return jsonify({
            'status': 'success',
            'count': len(picks),
            'date': trade_date
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/picks', methods=['GET'])
def api_picks():
    """获取选股推荐列表"""
    limit = min(int(request.args.get('limit', 30)), 100)
    date = request.args.get('date', '').strip()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if date:
        rows = conn.execute(
            'SELECT * FROM stock_picks WHERE trade_date = ? ORDER BY score DESC LIMIT ?',
            (date, limit)
        ).fetchall()
    else:
        # 获取最新日期
        latest = conn.execute('SELECT MAX(trade_date) as d FROM stock_picks').fetchone()
        if latest and latest['d']:
            rows = conn.execute(
                'SELECT * FROM stock_picks WHERE trade_date = ? ORDER BY score DESC LIMIT ?',
                (latest['d'], limit)
            ).fetchall()
        else:
            rows = []

    conn.close()

    picks = []
    for r in rows:
        # 兼容 reasons / reasons_json 列名
        raw_reasons = r['reasons'] if 'reasons' in r.keys() else (r['reasons_json'] if 'reasons_json' in r.keys() else '')
        try:
            reasons = json.loads(raw_reasons) if raw_reasons else []
        except Exception:
            reasons = []

        def _f(v):
            return v if v is not None else None

        picks.append({
            'ts_code': r['ts_code'],
            'name': r['name'],
            'trade_date': r['trade_date'],  # 入选日, 给"入选后涨幅"做 tooltip
            'close': r['close'],            # 入选当日收盘价
            'change_pct': r['change_pct'],
            # 均线 (保留数据, 前端可隐藏列)
            'ma5': _f(r['ma5']) if 'ma5' in r.keys() else None,
            'ma10': _f(r['ma10']) if 'ma10' in r.keys() else None,
            'ma20': _f(r['ma20']) if 'ma20' in r.keys() else None,
            'ma30': _f(r['ma30']) if 'ma30' in r.keys() else None,
            'ma5_slope': _f(r['ma5_slope']) if 'ma5_slope' in r.keys() else None,
            'ma10_slope': _f(r['ma10_slope']) if 'ma10_slope' in r.keys() else None,
            'ma20_slope': _f(r['ma20_slope']) if 'ma20_slope' in r.keys() else None,
            'ma30_slope': _f(r['ma30_slope']) if 'ma30_slope' in r.keys() else None,
            # 资金流
            'main_net_today': _f(r['main_net_today']) if 'main_net_today' in r.keys() else None,
            'main_net_pct_today': _f(r['main_net_pct_today']) if 'main_net_pct_today' in r.keys() else None,
            'main_net_3d': _f(r['main_net_3d']) if 'main_net_3d' in r.keys() else None,
            'main_net_5d': _f(r['main_net_5d']) if 'main_net_5d' in r.keys() else None,
            'main_net_10d': _f(r['main_net_10d']) if 'main_net_10d' in r.keys() else None,
            'main_net_20d': _f(r['main_net_20d']) if 'main_net_20d' in r.keys() else None,
            'super_net': _f(r['super_net']) if 'super_net' in r.keys() else None,
            'super_pct': _f(r['super_pct']) if 'super_pct' in r.keys() else None,
            'big_net': _f(r['big_net']) if 'big_net' in r.keys() else None,
            'big_pct': _f(r['big_pct']) if 'big_pct' in r.keys() else None,
            'mid_net': _f(r['mid_net']) if 'mid_net' in r.keys() else None,
            'mid_pct': _f(r['mid_pct']) if 'mid_pct' in r.keys() else None,
            'small_net': _f(r['small_net']) if 'small_net' in r.keys() else None,
            'small_pct': _f(r['small_pct']) if 'small_pct' in r.keys() else None,
            'score': r['score'],
            'signal_type': r['signal_type'] if 'signal_type' in r.keys() else 'hold',
            'flow_days_available': r['flow_days_available'] if 'flow_days_available' in r.keys() else 0,
            'reasons': reasons,
        })

    # 板块历史
    _attach_sector(picks, key='ts_code')

    # 入选后涨幅: 调 sina 拿当前实时价, 对比入选当日 close
    if picks:
        codes = [p['ts_code'] for p in picks if p.get('ts_code')]
        if codes:
            try:
                quotes = fetch_sina_quotes(codes)
                for p in picks:
                    q = quotes.get(p['ts_code'])
                    if q and p.get('close') and q.get('price'):
                        cur = q['price']
                        ref = p['close']
                        p['current_price'] = cur
                        p['quote_time'] = q.get('time', '')
                        p['post_pick_change_pct'] = round((cur - ref) / ref * 100, 2)
            except Exception as e:
                print(f'[api_picks] sina 拉取失败 (不影响主流程): {e}')

    return jsonify({
        'picks': picks,
        'date': rows[0]['trade_date'] if rows else '',
        'count': len(picks)
    })


# ============ 我的选股 (user_picks, 复用 _compute_picks 算法) ============


@app.route('/api/user-picks', methods=['GET'])
def api_user_picks_list():
    """从 user_picks 拉 ts_code 列表, 复用 _compute_picks 全量算法 + 板块 + 实时价.
    返回结构跟 /api/picks 一致, 多一个 group 字段.
    """
    date = request.args.get('date', '').strip() or None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # date 过滤: 只返回那天入库的股票; 无 date 返回全部
    # date 参数可能是 YYYYMMDD (前端 flatpickr) 或 YYYY-MM-DD, 归一成 YYYYMMDD (内部 _compute_picks 用)
    date_yyyymmdd = None
    if date:
        if len(date) == 8 and date.isdigit():
            date_yyyymmdd = date
        elif len(date) == 10 and date[4] == '-':
            date_yyyymmdd = date.replace('-', '')
    if date_yyyymmdd:
        # added_at 是 'YYYY-MM-DD HH:MM:SS' 格式, 用 substr 比对前 10 位
        date_dash = f'{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}'
        sql = "SELECT ts_code, added_at FROM user_picks WHERE substr(added_at, 1, 10) = ? ORDER BY added_at DESC"
        user_rows = conn.execute(sql, (date_dash,)).fetchall()
    else:
        user_rows = conn.execute('SELECT ts_code, added_at FROM user_picks ORDER BY added_at DESC').fetchall()
    conn.close()
    codes = [r['ts_code'] for r in user_rows]
    # 入库时间映射 (ts_code -> 'YYYY-MM-DD HH:MM'), 前端表格显示用
    added_at_map = {r['ts_code']: r['added_at'] for r in user_rows if r['added_at']}
    if not codes:
        return jsonify({'picks': [], 'date': '', 'count': 0, 'group': 'user', 'added_at_map': {}})

    # 选股计算用 stock_daily 最新有数据的日期 (入库日可能是当天, stock_daily 还没同步)
    # 这样 6/25 入库的股能用 6/24 的最新行情算 MA / 资金流
    conn2 = sqlite3.connect(DB_PATH, timeout=10)
    latest_sd_row = conn2.execute('SELECT MAX(trade_date) FROM stock_daily').fetchone()
    conn2.close()
    calc_trade_date = latest_sd_row[0] if latest_sd_row and latest_sd_row[0] else None
    picks = _compute_picks(trade_date=calc_trade_date, ts_codes=codes)
    if not picks:
        return jsonify({'picks': [], 'date': date or '', 'count': 0, 'group': 'user', 'added_at_map': added_at_map})

    # 板块历史 (复用)
    _attach_sector(picks, key='ts_code')

    # 入选后涨幅 (复用, 用 picks 中第一只的 trade_date 当入选日)
    if picks:
        all_codes = [p['ts_code'] for p in picks if p.get('ts_code')]
        if all_codes:
            try:
                quotes = fetch_sina_quotes(all_codes)
                for p in picks:
                    q = quotes.get(p['ts_code'])
                    if q and p.get('close') and q.get('price'):
                        p['current_price'] = q['price']
                        p['quote_time'] = q.get('time', '')
                        p['post_pick_change_pct'] = round((q['price'] - p['close']) / p['close'] * 100, 2)
            except Exception as e:
                print(f'[api_user_picks] sina 拉取失败 (不影响主流程): {e}')

    return jsonify({
        'picks': picks,
        'date': picks[0]['trade_date'] if picks else '',
        'count': len(picks),
        'group': 'user',
        'added_count': len(codes),
        'added_at_map': added_at_map,
    })


@app.route('/api/user-picks', methods=['POST'])
def api_user_picks_add():
    """添加一只到 user_picks. body: {ts_code, note?, date?}
    _normalize_code 归一化 + stock_basic 校验
    date: 可选 YYYYMMDD 或 YYYY-MM-DD; 传入则 added_at 用该日期中午 12:00 (避免跨日), 缺省用当下时间
    """
    data = request.get_json(silent=True) or request.form
    raw = data.get('ts_code')
    note = (data.get('note') or '').strip() or None
    target_date = (data.get('date') or '').strip()
    ts_code = _normalize_code(raw)
    if not ts_code:
        return jsonify({'status': 'error', 'message': f'代码格式无效: {raw!r} (接受 6 位数字 / sh600519 / 600519.SH)'}), 400

    # 校验 stock_basic 存在
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT name FROM stock_basic WHERE ts_code = ?', (ts_code,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': f'股票不存在: {ts_code} (stock_basic 里没找到)'}), 400
    name = row['name']

    # 解析目标日期: YYYYMMDD 或 YYYY-MM-DD -> 写入 added_at 用该日期中午 12:00
    added_at_override = None
    if target_date:
        date_norm = None
        if len(target_date) == 8 and target_date.isdigit():
            date_norm = target_date
        elif len(target_date) == 10 and target_date[4] == '-':
            date_norm = target_date.replace('-', '')
        if date_norm:
            added_at_override = f'{date_norm[:4]}-{date_norm[4:6]}-{date_norm[6:8]} 12:00:00'

    # 直接 INSERT: 每次添加都新建一条记录, 不去重 (用户每天的选择完整保留, 重复的也记)
    if added_at_override:
        cur = conn.execute('INSERT INTO user_picks (ts_code, added_at, note) VALUES (?, ?, ?)',
                          (ts_code, added_at_override, note))
    else:
        cur = conn.execute('INSERT INTO user_picks (ts_code, note) VALUES (?, ?)', (ts_code, note))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({
        'status': 'success',
        'ts_code': ts_code,
        'name': name,
        'action': 'added',
        'id': new_id,
    })


@app.route('/api/user-picks/<ts_code>', methods=['DELETE'])
def api_user_picks_delete(ts_code):
    """从 user_picks 删除. 路径参数 ts_code 接受任意格式 (_normalize_code 归一化)."""
    norm = _normalize_code(ts_code)
    if not norm:
        return jsonify({'status': 'error', 'message': f'代码格式无效: {ts_code!r}'}), 400
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute('DELETE FROM user_picks WHERE ts_code = ?', (norm,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        return jsonify({'status': 'error', 'message': f'自选股不存在: {norm}'}), 404
    return jsonify({
        'status': 'success',
        'ts_code': norm,
        'action': 'deleted',
    })


# ============ 自选股: 上传图片 OCR 识别股票 ============

_USER_PICKS_OCR_PROMPT = (
    '识别图中所有股票。每只股票输出 code (6位数字) 和 name (中文名称)。\n'
    '严格按 JSON 数组输出，不加任何解释、不带 markdown 代码块标记：\n'
    '[{"code":"600519","name":"贵州茅台"}, {"code":"000001","name":"平安银行"}]\n'
    '如果只看到名称没看到代码, code 填空字符串 ""。\n'
    '如果只看到代码没看到名称, name 填空字符串 ""。\n'
    '只输出图中实际出现的股票, 不要推测或编造。'
)


def _parse_mmx_picks_user(stdout_text):
    """从 mmx stdout 解析出 [{code, name}, ...] 数组. 失败返回 ([], 错误描述)."""
    try:
        outer = _json.loads(stdout_text.strip())
        inner = outer.get('content', '')
        inner = re.sub(r'^```json\s*', '', inner).strip()
        inner = re.sub(r'```\s*$', '', inner).strip()
        if inner.startswith('```'):
            inner = re.sub(r'^```[a-z]*\s*', '', inner).strip()
            inner = re.sub(r'```\s*$', '', inner).strip()
        if inner.startswith('['):
            return _json.loads(inner), ''
        if inner.startswith('"'):
            decoded = _json.loads(inner)
            if isinstance(decoded, str) and decoded.startswith('['):
                return _json.loads(decoded), ''
        m = re.search(r'\[\s*\{.*?\}\s*\]', stdout_text.strip(), re.DOTALL)
        if m:
            return _json.loads(m.group()), ''
    except Exception as _pe:
        return [], f'{type(_pe).__name__}: {_pe}'
    return [], 'no array found'


def _enrich_user_picks_with_stock_basic(items):
    """JOIN stock_basic: 按 code 精确匹配 (前缀 .SH/.SZ 试两边), 失败按 name 等值匹配.
    返回 [{code, name, ts_code, matched, candidates: [{ts_code, name}]}]
    matched=True 才会被前端选中添加.
    """
    if not items:
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out = []
    for it in items:
        code = (it.get('code') or '').strip()
        name = (it.get('name') or '').strip()
        ts_code = None
        # 1) code 精确: try 6位 + .SH/.SZ
        if code and len(code) == 6 and code.isdigit():
            for suffix in ('.SH', '.SZ', '.BJ'):
                row = conn.execute('SELECT ts_code, name FROM stock_basic WHERE ts_code = ?', (code + suffix,)).fetchone()
                if row:
                    ts_code = row['ts_code']
                    if not name:
                        name = row['name']
                    break
        # 2) name 等值匹配
        if not ts_code and name:
            row = conn.execute('SELECT ts_code, name FROM stock_basic WHERE name = ? LIMIT 1', (name,)).fetchone()
            if row:
                ts_code = row['ts_code']
        # 3) name 模糊 (LIKE) — 给候选
        candidates = []
        if not ts_code and name and len(name) >= 2:
            for r in conn.execute(
                "SELECT ts_code, name FROM stock_basic WHERE name LIKE ? LIMIT 5",
                (f'%{name}%',),
            ).fetchall():
                candidates.append({'ts_code': r['ts_code'], 'name': r['name']})
        out.append({
            'code': code,
            'name': name,
            'ts_code': ts_code,
            'matched': ts_code is not None,
            'candidates': candidates,
        })
    conn.close()
    return out


def _run_user_picks_ocr_job(filepath, job_id):
    """子进程: 跑 mmx vision 识别股票 → JOIN stock_basic → 写 status 文件.
    复用 _ocr_status_path / 格式. 自选股图片一般较小不切块, 单次 mmx 调用.
    """
    import re as _re
    status_path = _ocr_status_path(job_id)

    def _save(stage, **extra):
        try:
            payload = {'status': 'processing', 'stage': stage}
            payload.update(extra)
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json.dump(payload, _f, ensure_ascii=False)
        except Exception:
            pass

    def _save_done(items, raw_text=''):
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json.dump({
                    'status': 'done', 'stage': 'done',
                    'items': items, 'raw_text': raw_text[:2000],
                }, _f, ensure_ascii=False)
        except Exception:
            pass

    def _save_err(msg):
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json.dump({'status': 'error', 'stage': 'error', 'message': msg}, _f, ensure_ascii=False)
        except Exception:
            pass

    try:
        _save('ocring')
        r = subprocess.run(
            ['/usr/local/bin/mmx', 'vision', 'describe', '--image', filepath,
             '--output', 'json', '--prompt', _USER_PICKS_OCR_PROMPT],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0 or not r.stdout.strip():
            _save_err(f'mmx 失败: {(r.stderr or "empty stdout")[:200]}')
            return
        items, parse_err = _parse_mmx_picks_user(r.stdout)
        if parse_err:
            _save_err(f'OCR 解析失败: {parse_err}')
            return
        enriched = _enrich_user_picks_with_stock_basic(items)
        matched = sum(1 for x in enriched if x['matched'])
        _save_done(enriched, raw_text=r.stdout)
        # OCR 成功, 5 分钟后删临时图片
        _schedule_ocr_image_cleanup(filepath, delay_sec=0)
        print(f'[user-picks-ocr] job {job_id} 识别 {len(enriched)} 只, 匹配 {matched}', flush=True)
    except subprocess.TimeoutExpired:
        _save_err('mmx 180s 超时, 图片可能太大或太复杂')
    except Exception as e:
        _save_err(f'{type(e).__name__}: {str(e)[:200]}')


@app.route('/api/user-picks/ocr-image', methods=['POST'])
def ocr_user_picks_image():
    """异步 OCR 任意股票截图, 立即返回 job_id. 多进程跑 mmx, 父进程轮询 status."""
    if 'image' not in request.files:
        return jsonify({'status': 'error', 'message': '请上传图片文件'}), 400
    image_file = request.files['image']
    if not image_file.filename:
        return jsonify({'status': 'error', 'message': '请上传图片文件'}), 400

    upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    import uuid
    job_id = str(uuid.uuid4())[:8]
    filename = f'user_picks_{datetime.now().strftime("%Y%m%d%H%M%S")}_{job_id}_{image_file.filename}'
    filepath = os.path.join(upload_dir, filename)
    image_file.save(filepath)

    try:
        with open(_ocr_status_path(job_id), 'w', encoding='utf-8') as _f:
            _json.dump({'status': 'processing', 'stage': 'saving_image'}, _f, ensure_ascii=False)
    except Exception:
        pass

    import multiprocessing
    p = multiprocessing.Process(target=_run_user_picks_ocr_job, args=(filepath, job_id), daemon=True)
    p.start()
    return jsonify({'status': 'processing', 'job_id': job_id})


@app.route('/api/user-picks/ocr-status/<job_id>', methods=['GET'])
def get_user_picks_ocr_status(job_id):
    """轮询 OCR 状态. done 时返回 items (含 ts_code 匹配) + raw_text (mmx 原文)."""
    job = _read_ocr_status(job_id) or _ocr_jobs.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': '任务不存在'}), 404
    if job.get('status') == 'done':
        return jsonify({
            'status': 'done',
            'items': job.get('items', []),
            'raw_text': job.get('raw_text', ''),
        })
    if job.get('status') == 'error':
        return jsonify({'status': 'error', 'message': job.get('message', 'OCR 失败')}), 500
    return jsonify({'status': 'processing', 'stage': job.get('stage', '')})


# ============ 板块合集 (基于 limitup 表聚合) ============


@app.route('/api/sector-collections', methods=['GET'])
def api_sector_collections():
    """板块热度榜: 按近 N 天涨停次数排, 涉及股票数广度, 时间窗口可调.
    query: days (默认 7, 0=全部), search (板块名模糊), sort (hot/breadth/latest, 默认 hot)
    """
    days = max(0, int(request.args.get('days', 7)))
    search = (request.args.get('search') or '').strip()
    sort = request.args.get('sort', 'hot')

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    # 排除"占位"板块 (limitup 表里 sector 字段为"其他"/"公告" 的不是真板块, 是韭研/OCR fallback)
    excluded_sectors = ('其他', '公告')

    # 1) 基础: 每个 sector 累计统计 (全时间)
    base_sql = """
        SELECT sector,
               COUNT(DISTINCT code) AS stock_count,
               COUNT(DISTINCT date) AS days_count,
               MAX(date) AS latest_date
        FROM limitup
        WHERE sector IS NOT NULL AND sector != ''
          AND sector NOT IN ('其他', '公告')
    """
    params = []
    if search:
        base_sql += " AND sector LIKE ?"
        params.append(f'%{search}%')
    base_sql += " GROUP BY sector"

    base_rows = conn.execute(base_sql, params).fetchall()
    if not base_rows:
        conn.close()
        return jsonify({'sectors': [], 'days': days, 'total': 0})

    # 2) 近期热度: 近 N 天每个 sector 的涨停次数
    if days > 0:
        # 用相对今天减 N 天的 SQL date
        recent_sql = """
            SELECT sector, COUNT(*) AS hot_count,
                   GROUP_CONCAT(DISTINCT date) AS recent_dates
            FROM limitup
            WHERE sector IS NOT NULL AND sector != ''
              AND sector NOT IN ('其他', '公告')
              AND date >= date('now', ?)
        """
        recent_params = [f'-{days} days']
        if search:
            recent_sql += " AND sector LIKE ?"
            recent_params.append(f'%{search}%')
        recent_sql += " GROUP BY sector"
        recent_rows = conn.execute(recent_sql, recent_params).fetchall()
        recent_map = {r['sector']: dict(r) for r in recent_rows}
    else:
        # 全部时间 = 累计 hot_count
        recent_map = {r['sector']: {'hot_count': 0, 'recent_dates': ''} for r in base_rows}

    # 3) 最新一次涨停的代表股票 (top 3)
    latest_date = conn.execute("""
        SELECT MAX(date) AS d FROM limitup
    """).fetchone()['d']

    rep_map = {}
    if latest_date:
        rep_rows = conn.execute("""
            SELECT sector, code, name, streak, marketCap
            FROM limitup
            WHERE date = ? AND sector IS NOT NULL AND sector != ''
              AND sector NOT IN ('其他', '公告')
            ORDER BY marketCap DESC NULLS LAST
        """, (latest_date,)).fetchall()
        for r in rep_rows:
            rep_map.setdefault(r['sector'], []).append({
                'ts_code': r['code'],
                'name': r['name'],
                'streak': r['streak'],
                'market_cap': r['marketCap'],
            })

    # 4) 合并 + 排序
    out = []
    for r in base_rows:
        sector = r['sector']
        rec = recent_map.get(sector, {'hot_count': 0, 'recent_dates': ''})
        reps = rep_map.get(sector, [])[:3]
        recent_dates = (rec.get('recent_dates') or '').split(',') if rec.get('recent_dates') else []
        out.append({
            'sector': sector,
            'stock_count': r['stock_count'],
            'days_count': r['days_count'],
            'latest_date': r['latest_date'],
            'hot_count': rec['hot_count'],
            'recent_dates': recent_dates,
            'top_stocks': reps,
        })

    if sort == 'breadth':
        out.sort(key=lambda x: (x['stock_count'], x['hot_count']), reverse=True)
    elif sort == 'latest':
        out.sort(key=lambda x: (x['latest_date'] or '', x['hot_count']), reverse=True)
    else:  # hot
        out.sort(key=lambda x: (x['hot_count'], x['stock_count']), reverse=True)

    conn.close()
    return jsonify({
        'sectors': out,
        'days': days,
        'total': len(out),
        'sort': sort,
    })


@app.route('/api/sector-collections/<path:sector>', methods=['GET'])
def api_sector_collection_detail(sector):
    """板块详情: 该板块所有历史股票 + 走势.
    query: days (默认 30, 0=全部)
    """
    days = max(0, int(request.args.get('days', 30)))
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    # 板块摘要
    summary_row = conn.execute("""
        SELECT COUNT(DISTINCT code) AS stock_count,
               COUNT(DISTINCT date) AS days_count,
               MAX(date) AS latest_date
        FROM limitup WHERE sector = ?
    """, (sector,)).fetchone()

    # 历史股票 (按日期倒序, 应用时间窗口) — 单只股票只显示最近上榜的那一次
    sql = """
        SELECT * FROM (
            SELECT date, code, name, marketCap, time, sector, volume, streak, keyword,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC, marketCap DESC NULLS LAST) AS rn
            FROM limitup
            WHERE sector = ?
    """
    params = [sector]
    if days > 0:
        sql += " AND date >= date('now', ?)"
        params.append(f'-{days} days')
    sql += """
        ) WHERE rn = 1
        ORDER BY date DESC, marketCap DESC NULLS LAST
    """
    stocks = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # 走势: 每日的涨停次数 + 当日平均涨幅 (stock_daily.change 当作近似)
    timeline_sql = """
        SELECT l.date,
               COUNT(*) AS hot_count,
               GROUP_CONCAT(l.code) AS codes
        FROM limitup l
        WHERE l.sector = ?
    """
    tl_params = [sector]
    if days > 0:
        timeline_sql += " AND l.date >= date('now', ?)"
        tl_params.append(f'-{days} days')
    timeline_sql += " GROUP BY l.date ORDER BY l.date ASC"
    timeline = [dict(r) for r in conn.execute(timeline_sql, tl_params).fetchall()]

    # 涉及股票去重列表 (近 N 天)
    seen = set()
    involved = []
    for s in stocks:
        if s['code'] not in seen:
            seen.add(s['code'])
            involved.append({'ts_code': s['code'], 'name': s['name'], 'first_date': s['date']})

    # 给每只股票补今日涨幅 (调 sina 实时, 30s 全局缓存, 跨 tab 复用)
    codes = list({s['code'] for s in stocks if s.get('code')})
    quote_map = fetch_sina_quotes(codes) if codes else {}
    for s in stocks:
        q = quote_map.get(s['code'])
        if q:
            s['change_pct'] = q['change_pct']
            s['price'] = q['price']

    conn.close()
    return jsonify({
        'sector': sector,
        'days': days,
        'summary': dict(summary_row) if summary_row else {},
        'stocks': stocks,
        'timeline': timeline,
        'involved_count': len(involved),
    })


_SECTORS_QUOTE_CACHE = {}  # key: (days, sort, search, top_n, per_n) -> (timestamp, result)
_SECTORS_QUOTE_TTL = 30  # 30s 缓存 (避免高频拉 sina)


@app.route('/api/sector-collections/quote', methods=['GET'])
def api_sector_collections_quote():
    """板块涨幅:
    - 不传 date (实时): 拉 sina 实时价, 算 current 流通市值加权平均 (30s 缓存)
    - 传 date (YYYYMMDD): 用 stock_daily.change + stock_daily.close × float_share 算历史某日板块涨幅 (缓存 1h)
    query: days (默认 7), sort (默认 hot), search, top_n (默认 138 全板块), names (自选板块名 逗号分隔, 跟 top_n 互斥)
    """
    import time as _t
    days = max(0, int(request.args.get('days', 7)))
    sort = request.args.get('sort', 'hot')
    search = (request.args.get('search') or '').strip()
    top_n = min(200, max(1, int(request.args.get('top_n', 138))))
    date = (request.args.get('date') or '').strip()  # YYYYMMDD, 空 = 实时
    # 自选板块名列表 (逗号分隔); 传了 names 就只算这几个, 忽略 top_n
    names_param = (request.args.get('names') or '').strip()
    name_list = [n for n in names_param.split(',') if n.strip()] if names_param else []

    cache_key = (days, sort, search, top_n, date, tuple(name_list))
    ttl = 3600 if date else _SECTORS_QUOTE_TTL  # 历史日期缓存 1h, 实时 30s
    now = _t.time()
    if cache_key in _SECTORS_QUOTE_CACHE:
        ts, cached = _SECTORS_QUOTE_CACHE[cache_key]
        if now - ts < ttl:
            return jsonify({**cached, 'cached': True, 'cache_age': round(now - ts, 1)})
    return _compute_sectors_quote(days, sort, search, top_n, cache_key, now, date, name_list)


def _compute_sectors_quote(days, sort, search, top_n, cache_key, now, date='', name_list=None):
    """实际计算板块涨幅 (无缓存命中时调)
    date 空: 实时模式, sina 拉实时价 + 实时流通市值加权
    date 有值: 历史模式, 用 stock_daily.change + close × float_share 算当日板块涨幅
    name_list: 自选板块名列表; 传了则只算这几个 (否则走 top N)
    """
    import time as _t
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    # 1) 复用一级逻辑取 top N 板块 (或自选板块)
    base_sql = """
        SELECT sector, COUNT(DISTINCT code) AS stock_count, MAX(date) AS latest_date
        FROM limitup
        WHERE sector IS NOT NULL AND sector != ''
          AND sector NOT IN ('其他', '公告')
    """
    params = []
    if name_list:
        # 自选模式: 用 IN 限定
        placeholders = ','.join('?' * len(name_list))
        base_sql += f" AND sector IN ({placeholders})"
        params.extend(name_list)
    elif search:
        base_sql += " AND sector LIKE ?"
        params.append(f'%{search}%')
    base_sql += " GROUP BY sector"
    base_rows = conn.execute(base_sql, params).fetchall()
    if not base_rows:
        conn.close()
        return jsonify({'quotes': {}, 'count': 0, 'days': days, 'sort': sort, 'cached': False, 'cache_age': 0})

    # 2) 计算 hot_count (排信用)
    if days > 0:
        recent_sql = """
            SELECT sector, COUNT(*) AS hot_count
            FROM limitup
            WHERE sector IS NOT NULL AND sector != ''
              AND sector NOT IN ('其他', '公告')
              AND date >= date('now', ?)
        """
        recent_params = [f'-{days} days']
        if name_list:
            placeholders = ','.join('?' * len(name_list))
            recent_sql += f" AND sector IN ({placeholders})"
            recent_params.extend(name_list)
        elif search:
            recent_sql += " AND sector LIKE ?"
            recent_params.append(f'%{search}%')
        recent_sql += " GROUP BY sector"
        recent_map = {r['sector']: r['hot_count'] for r in conn.execute(recent_sql, recent_params).fetchall()}
    else:
        recent_map = {r['sector']: 0 for r in base_rows}

    sectors = []
    for r in base_rows:
        sectors.append({
            'sector': r['sector'],
            'hot_count': recent_map.get(r['sector'], 0),
            'stock_count': r['stock_count'],
        })
    if sort == 'breadth':
        sectors.sort(key=lambda x: (x['stock_count'], x['hot_count']), reverse=True)
    elif sort == 'latest':
        sectors.sort(key=lambda x: x['hot_count'], reverse=True)
    else:
        sectors.sort(key=lambda x: (x['hot_count'], x['stock_count']), reverse=True)
    # 自选模式: name_list 已限定, 不再截断 top_n
    if not name_list:
        sectors = sectors[:top_n]

    # 3) 每板块所有历史个股 (去重, 不限日期, 不限数量)
    # 一个股票多次涨停只算一次, 板块涨幅反映"概念股池"今日整体表现
    sector_codes = {}
    for s in sectors:
        all_rows = conn.execute("""
            SELECT DISTINCT code FROM limitup WHERE sector = ?
        """, (s['sector'],)).fetchall()
        sector_codes[s['sector']] = [r['code'] for r in all_rows]
    conn.close()

    # 4) 拉数据
    all_codes = []
    for codes in sector_codes.values():
        all_codes.extend(codes)
    all_codes = list(set(all_codes))  # 去重

    quotes = {}      # 实时模式: ts_code -> {price, change_pct, ...}
    daily_map = {}   # 历史模式: ts_code -> {close, change_pct}

    if date:
        # 历史模式: 从 stock_daily 查当日的 close + change
        try:
            daily_conn = sqlite3.connect(DB_PATH, timeout=10)
            qmarks = ','.join('?' * len(all_codes))
            daily_rows = daily_conn.execute(
                f'SELECT ts_code, close, change FROM stock_daily WHERE trade_date = ? AND ts_code IN ({qmarks})',
                [date, *all_codes]
            ).fetchall()
            daily_conn.close()
            for r in daily_rows:
                daily_map[r[0]] = {'close': r[1], 'change_pct': r[2]}
        except Exception as e:
            print(f'[sector-quote] daily lookup error: {e}')
    else:
        # 实时模式: sina 一次最多 ~80 个 code, 50 一批更安全
        if all_codes:
            BATCH = 50
            try:
                for i in range(0, len(all_codes), BATCH):
                    batch = all_codes[i:i + BATCH]
                    batch_quotes = fetch_sina_quotes(batch)
                    quotes.update(batch_quotes)
            except Exception as e:
                print(f'[sector-quote] fetch error: {e}')

    # 4.5) 拉流通股本 (stock_basic.float_share, 单位万股), 用于按市值加权
    share_map = {}  # ts_code -> float_share (万股)
    if all_codes:
        try:
            share_conn = sqlite3.connect(DB_PATH, timeout=10)
            qmarks = ','.join('?' * len(all_codes))
            share_rows = share_conn.execute(
                f'SELECT ts_code, float_share FROM stock_basic WHERE ts_code IN ({qmarks}) AND float_share IS NOT NULL AND float_share > 0',
                all_codes
            ).fetchall()
            share_conn.close()
            for r in share_rows:
                share_map[r[0]] = r[1]
        except Exception as e:
            print(f'[sector-quote] share lookup error: {e}')

    # 5) 按板块算加权 change_pct
    # 实时流通市值 = current_price × float_share × 10000
    # 当日流通市值 = close × float_share × 10000 (历史模式用)
    # 加权公式: Σ(change_pct × 流通市值) / Σ(流通市值)
    # 缺 float_share 的股票按 0 权重跳过 (但仍计入 sample_count 显示)
    result = {}
    for sec, codes in sector_codes.items():
        weighted_sum = 0.0
        weight_total = 0.0
        valid_count = 0
        weighted_count = 0
        for c in codes:
            if date:
                d = daily_map.get(c)
                if not d or d.get('change_pct') is None:
                    continue
                valid_count += 1
                price = d.get('close') or 0
                cp = d['change_pct']
            else:
                q = quotes.get(c)
                if not q or q.get('change_pct') is None:
                    continue
                valid_count += 1
                price = q.get('price') or 0
                cp = q['change_pct']
            fs = share_map.get(c)
            if price > 0 and fs:
                mcap = price * fs * 10000  # 元
                weighted_sum += cp * mcap
                weight_total += mcap
                weighted_count += 1
        if weight_total > 0:
            avg = round(weighted_sum / weight_total, 2)
            result[sec] = {
                'change_pct': avg,
                'sample_count': valid_count,
                'weighted_count': weighted_count,
                'total_stocks': len(codes),
            }
        elif valid_count > 0:
            # 兜底: 有数据但全无 float_share, 退化成算术平均
            if date:
                cps = [daily_map.get(c, {}).get('change_pct') for c in codes
                       if daily_map.get(c, {}).get('change_pct') is not None]
            else:
                cps = [quotes.get(c, {}).get('change_pct') for c in codes
                       if quotes.get(c, {}).get('change_pct') is not None]
            avg = round(sum(cps) / len(cps), 2)
            result[sec] = {
                'change_pct': avg,
                'sample_count': valid_count,
                'weighted_count': 0,
                'total_stocks': len(codes),
            }
        else:
            result[sec] = {'change_pct': None, 'sample_count': 0, 'weighted_count': 0, 'total_stocks': len(codes)}

    payload = {
        'quotes': result,
        'count': len(result),
        'days': days,
        'sort': sort,
        'date': date or None,
        'mode': 'historical' if date else 'realtime',
        'total_codes': len(all_codes),
        'cached': False,
        'cache_age': 0,
    }
    _SECTORS_QUOTE_CACHE[cache_key] = (now, payload)
    return jsonify(payload)


# ============ 申万行业 (sw_industry / sw_industry_member / sw_industry_daily, 给情绪分析用) ============
# 注: Tushare 免费版拿不到 src='SW' (新申万, 需高级积分). 用老版申万 2014 (不带 src 参数, ~28 个 L1 行业).
# 同时把 pro.stock_basic 的 industry 字段 (中信一级) 同步到 stock_basic 表, 给个股→行业映射用.

def sync_sw_industry_classify(level='L1'):
    """同步申万行业分类到老表 sw_industry.
    level: 'L1' / 'L2' / 'L3', 默认 L1.
    注意: src 不能传 'SW' (新申万要积分), 不传时返回老版申万 2014 (~28 L1 / ~100 L2)."""
    pro = get_pro()
    df = pro.index_classify(level=level)  # 不传 src, 走老版 SW2014
    if df is None or len(df) == 0:
        return 0
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        n = 0
        now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for _, row in df.iterrows():
            conn.execute('''INSERT OR REPLACE INTO sw_industry
                (index_code, industry_name, level, src, parent_code, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (row['index_code'], row['industry_name'], level, row.get('src') or 'SW2014',
                 row.get('parent_code') or '', now_ts))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def sync_sw_industry_members(level='L1'):
    """同步申万行业成分股到 sw_industry_member."""
    pro = get_pro()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        # 拉所有 L1 行业的 index_code
        codes = [r[0] for r in conn.execute(
            "SELECT index_code FROM sw_industry WHERE level=?", (level,)).fetchall()]
        n = 0
        for code in codes:
            try:
                df = pro.index_member(index_code=code)
            except Exception as e:
                print(f'[sync_sw_members] {code} 失败: {e}')
                continue
            if df is None or len(df) == 0:
                continue
            for _, row in df.iterrows():
                conn.execute('''INSERT OR REPLACE INTO sw_industry_member
                    (index_code, ts_code, in_date, out_date, is_new)
                    VALUES (?, ?, ?, ?, ?)''',
                    (code, row['con_code'], row.get('in_date') or '', row.get('out_date') or '',
                     row.get('is_new') or 'N'))
                n += 1
            time.sleep(0.2)  # 限速
        conn.commit()
        return n
    finally:
        conn.close()


def sync_sw_industry_daily(index_code, days=365):
    """同步单个申万行业日线到 sw_industry_daily."""
    pro = get_pro()
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
    df = pro.sw_index_daily(index_code=index_code, start_date=start_date, end_date=end_date)
    if df is None or len(df) == 0:
        return 0
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        n = 0
        for _, row in df.iterrows():
            conn.execute('''INSERT OR REPLACE INTO sw_industry_daily
                (index_code, trade_date, close, open, high, low, change_pct, vol, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (index_code, row['trade_date'],
                 row.get('close'), row.get('open'), row.get('high'), row.get('low'),
                 row.get('change_pct'), row.get('vol'), row.get('amount')))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


@app.route('/api/industry/sync', methods=['POST'])
def api_industry_sync():
    """同步申万分类 + 成分股 + 行业日线.
    body: {"levels": ["L1","L2"], "with_members": true, "with_daily": true, "daily_days": 30}
    申万日线较慢, 默认只同步 L1 分类 + 成分股; daily 可选."""
    body = request.get_json(silent=True) or {}
    levels = body.get('levels') or ['L1']
    with_members = body.get('with_members', True)
    with_daily = body.get('with_daily', False)
    daily_days = int(body.get('daily_days', 30))
    result = {'levels': {}, 'members': 0, 'daily_rows': 0}
    for lvl in levels:
        n = sync_sw_industry_classify(level=lvl)
        result['levels'][lvl] = n
    if with_members:
        for lvl in levels:
            result['members'] += sync_sw_industry_members(level=lvl)
    if with_daily:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        codes = [r[0] for r in conn.execute(
            "SELECT index_code FROM sw_industry WHERE level='L1'").fetchall()]
        conn.close()
        for code in codes:
            result['daily_rows'] += sync_sw_industry_daily(code, days=daily_days)
            time.sleep(0.1)
    return jsonify({'status': 'success', **result})


@app.route('/api/industry/list', methods=['GET'])
def api_industry_list():
    """列申万行业. query: level=L1 (默认) / L2 / L3"""
    level = request.args.get('level', 'L1')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT index_code, industry_name, level, src, parent_code, updated_at "
        "FROM sw_industry WHERE level=? ORDER BY index_code", (level,)).fetchall()
    conn.close()
    return jsonify({
        'level': level,
        'industries': [dict(r) for r in rows],
        'total': len(rows),
    })


@app.route('/api/industry/members', methods=['GET'])
def api_industry_members():
    """查某申万行业的成分股. query: index_code=801010.SI (必填)"""
    code = request.args.get('index_code', '').strip()
    if not code:
        return jsonify({'error': 'index_code 必填'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT index_code, ts_code, in_date, out_date, is_new "
        "FROM sw_industry_member WHERE index_code=? AND (out_date IS NULL OR out_date='') "
        "ORDER BY ts_code", (code,)).fetchall()
    conn.close()
    return jsonify({
        'index_code': code,
        'members': [dict(r) for r in rows],
        'total': len(rows),
    })


@app.route('/api/industry/daily', methods=['GET'])
def api_industry_daily():
    """查申万行业日线. query: index_code=801010.SI&days=30 (默认 30)"""
    code = request.args.get('index_code', '').strip()
    days = int(request.args.get('days', 30))
    if not code:
        return jsonify({'error': 'index_code 必填'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT index_code, trade_date, close, open, high, low, change_pct, vol, amount "
        "FROM sw_industry_daily WHERE index_code=? "
        "ORDER BY trade_date DESC LIMIT ?", (code, days)).fetchall()
    conn.close()
    return jsonify({
        'index_code': code,
        'daily': [dict(r) for r in rows],
        'total': len(rows),
    })


@app.route('/api/industry/ts-to-sw', methods=['GET'])
def api_industry_ts_to_sw():
    """个股→申万行业映射. query: ts_code=600519.SH"""
    ts_code = request.args.get('ts_code', '').strip()
    if not ts_code:
        return jsonify({'error': 'ts_code 必填'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 该个股所属的所有申万行业 (L1+L2)
    rows = conn.execute(
        "SELECT m.index_code, i.industry_name, i.level, i.src "
        "FROM sw_industry_member m JOIN sw_industry i ON m.index_code = i.index_code "
        "WHERE m.ts_code=? AND (m.out_date IS NULL OR m.out_date='') "
        "ORDER BY i.level, m.index_code", (ts_code,)).fetchall()
    conn.close()
    return jsonify({
        'ts_code': ts_code,
        'industries': [dict(r) for r in rows],
        'total': len(rows),
    })


# ============ 涨停池 (akshare stock_zt_pool_em, 给情绪指数计算封板率/炸板率) ============
# 注: Tushare limit_list_d 免费版无权限. 改用 akshare. 数据完全独立于现有 limitup (非研) 表, 不替换.

def sync_akshare_zt_pool(date_str):
    """同步单日涨停池. date_str: 'YYYY-MM-DD'.
    写入 akshare_zt_pool 表, ts_code 用 6 位代码 (无后缀)."""
    import akshare as ak
    # akshare 内部用 YYYYMMDD, 我们统一存 YYYY-MM-DD
    date_yyyymmdd = date_str.replace('-', '')
    df = None
    for attempt in range(2):
        try:
            df = ak.stock_zt_pool_em(date=date_yyyymmdd)
            break
        except RecursionError:
            print(f'[sync_akshare_zt_pool] {date_str} 递归深度超限, 重试中 ({attempt+1}/2)')
            import sys as _s
            _s.setrecursionlimit(50000)
        except Exception as e:
            print(f'[sync_akshare_zt_pool] {date_str} 失败: {e}')
            return 0
    if df is None:
        return 0
    if df is None or len(df) == 0:
        return 0
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        n = 0
        for _, row in df.iterrows():
            conn.execute('''INSERT OR REPLACE INTO akshare_zt_pool
                (trade_date, ts_code, name, industry, close, pct_chg, amount, circulate_mv, turnover_pct,
                 seal_amount, first_seal_time, last_seal_time, open_times, limit_stats, streak)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (date_str,
                 str(row.get('代码') or '').zfill(6),
                 row.get('名称'),
                 row.get('所属行业'),
                 _to_float(row.get('最新价')),
                 _to_float(row.get('涨跌幅')),
                 _to_float(row.get('成交额')),
                 _to_float(row.get('流通市值')),
                 _to_float(row.get('换手率')),
                 _to_float(row.get('封板资金')),
                 row.get('首次封板时间'),
                 row.get('最后封板时间'),
                 _to_int(row.get('炸板次数')),
                 row.get('涨停统计'),
                 _to_int(row.get('连板数'))))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def _to_float(v):
    """安全转 float, 处理 None/空字符串/非数字"""
    try:
        if v is None or v == '' or (isinstance(v, str) and v.strip() == ''):
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    """安全转 int"""
    try:
        if v is None or v == '' or (isinstance(v, str) and v.strip() == ''):
            return 0
        return int(float(v))
    except (ValueError, TypeError):
        return 0


@app.route('/api/limitup/ak/sync', methods=['POST'])
def api_limitup_ak_sync():
    """同步单日涨停池 (akshare). body: {"date": "YYYY-MM-DD"} (默认今天)"""
    body = request.get_json(silent=True) or {}
    date_str = body.get('date') or datetime.now().strftime('%Y-%m-%d')
    # 校验日期格式
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'status': 'error', 'message': f'日期格式错误: {date_str}, 需 YYYY-MM-DD'}), 400
    n = sync_akshare_zt_pool(date_str)
    return jsonify({'status': 'success', 'date': date_str, 'rows': n})


@app.route('/api/limitup/ak', methods=['GET'])
def api_limitup_ak_list():
    """查涨停池. query: date=YYYY-MM-DD (默认今天) & min_streak=1 (默认全部, 设 N 表示 ≥N 板)
    返回: 当日涨停池, 含连板/封板资金/炸板次数/封板时间等"""
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    min_streak = int(request.args.get('min_streak', 0))
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM akshare_zt_pool WHERE trade_date=?"
    params = [date_str]
    if min_streak > 0:
        sql += " AND streak >= ?"
        params.append(min_streak)
    sql += " ORDER BY streak DESC, seal_amount DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify({
        'date': date_str,
        'min_streak': min_streak,
        'rows': [dict(r) for r in rows],
        'total': len(rows),
    })

# ============ 北向资金 (Tushare moneyflow_hsgt, 给情绪指数资金因子) ============
# 注: 现有 /api/flow/* 用的是东方财富 push2 + akshare, 本表独立, 不替换.

def sync_hsgt_flow(date_str):
    """同步单日北向资金. date_str: 'YYYY-MM-DD'."""
    pro = get_pro()
    date_yyyymmdd = date_str.replace('-', '')
    df = None
    for attempt in range(2):
        try:
            df = pro.moneyflow_hsgt(trade_date=date_yyyymmdd)
            break
        except RecursionError:
            print(f'[sync_hsgt_flow] {date_str} 递归超限, 重试 ({attempt+1}/2)')
            import sys as _s
            _s.setrecursionlimit(50000)
        except Exception as e:
            print(f'[sync_hsgt_flow] {date_str} 失败: {e}')
            return 0
    if df is None:
        return 0
    if df is None or len(df) == 0:
        return 0
    row = df.iloc[0]
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute('''INSERT OR REPLACE INTO hsgt_flow
            (trade_date, hgt, sgt, north_money, south_money)
            VALUES (?, ?, ?, ?, ?)''',
            (date_str,
             _to_float(row.get('hgt')),
             _to_float(row.get('sgt')),
             _to_float(row.get('north_money')),
             _to_float(row.get('south_money'))))
        conn.commit()
        return 1
    finally:
        conn.close()


@app.route('/api/hsgt/sync', methods=['POST'])
def api_hsgt_sync():
    """同步单日北向资金. body: {"date": "YYYY-MM-DD"} (默认今天)"""
    body = request.get_json(silent=True) or {}
    date_str = body.get('date') or datetime.now().strftime('%Y-%m-%d')
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'status': 'error', 'message': f'日期格式错误: {date_str}'}), 400
    n = sync_hsgt_flow(date_str)
    return jsonify({'status': 'success', 'date': date_str, 'rows': n})


@app.route('/api/hsgt', methods=['GET'])
def api_hsgt_list():
    """查北向资金. query: date=YYYY-MM-DD (单日) 或 start_date+end_date (区间)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    date = request.args.get('date')
    start = request.args.get('start_date')
    end = request.args.get('end_date')
    if date:
        rows = conn.execute("SELECT * FROM hsgt_flow WHERE trade_date=?", (date,)).fetchall()
    elif start and end:
        rows = conn.execute(
            "SELECT * FROM hsgt_flow WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date DESC",
            (start, end)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM hsgt_flow ORDER BY trade_date DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify({
        'rows': [dict(r) for r in rows],
        'total': len(rows),
    })

# ============ 阶段 1: 情绪分析 (sentiment_intraday + 6 路由 + 7 类规则触发) ============

@app.route('/api/sentiment/current', methods=['GET'])
def api_sentiment_current():
    """GET 情绪最新分. 调 sentiment.api_sentiment_current()."""
    from sentiment import api_sentiment_current as _h
    return _h()


@app.route('/api/sentiment/compute', methods=['POST'])
def api_sentiment_compute():
    """POST 立即算一次情绪. body: {} (用当前时间)"""
    from sentiment import compute_sentiment
    from alert_rules import run_all_rules
    try:
        result = compute_sentiment()
        run_all_rules(result['ts'], result['level'])
        return jsonify({'status': 'success', **result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/sentiment/intraday', methods=['GET'])
def api_sentiment_intraday_route():
    """GET 情绪分时曲线. query: date=YYYY-MM-DD (默认今天)"""
    from sentiment import api_sentiment_intraday
    date_str = request.args.get('date')
    return api_sentiment_intraday(date_str)


@app.route('/api/sentiment/factors', methods=['GET'])
def api_sentiment_factors_route():
    """GET 某时刻因子分项. query: ts=YYYY-MM-DD HH:MM:SS (默认当前)"""
    from sentiment import api_sentiment_factors
    ts = request.args.get('ts')
    return api_sentiment_factors(ts)


@app.route('/api/sentiment/level', methods=['GET'])
def api_sentiment_level_route():
    """GET 当日 5 档等级. query: date=YYYY-MM-DD (默认今天)"""
    from sentiment import api_sentiment_level
    date_str = request.args.get('date')
    return api_sentiment_level(date_str)


@app.route('/api/sentiment/alerts', methods=['GET'])
def api_sentiment_alerts_route():
    """GET 当日预警列表. query: date=YYYY-MM-DD&limit=100"""
    from sentiment import api_sentiment_alerts
    date_str = request.args.get('date')
    limit = int(request.args.get('limit', 100))
    return api_sentiment_alerts(date_str, limit)


@app.route('/api/sentiment/daily-history', methods=['GET'])
def api_sentiment_daily_history_route():
    """GET 近 N 日情绪等级对比. query: days=10"""
    from sentiment import api_sentiment_daily_history
    days = int(request.args.get('days', 10))
    return api_sentiment_daily_history(days)

# ============ 阶段 2: 板块情绪热力图 + 连板梯队 (需求文档 §3.2 + §3.3) ============

@app.route('/api/sector/sw-heatmap', methods=['GET'])
def api_sector_sw_heatmap():
    """申万行业涨幅热力图. query: date=YYYY-MM-DD (默认今天) & level=L1 (默认) / L2
    返回: 申万行业 × {涨跌幅, 涨停数, 领涨龙头, 资金净流入, 持续性评分}.
    文档 §3.2: 按涨跌幅排序, 标注板块内涨停个股数量/领涨龙头/资金净流入.
    文档 §3.2 末: 识别主线/支线/轮动板块, 展示板块持续性评分 (近 3 日涨幅连续性)."""
    from datetime import timedelta as _td
    level = request.args.get('level', 'L1')
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # 1) 当日申万行业涨幅
        rows = conn.execute(
            "SELECT i.index_code, i.industry_name, d.change_pct, d.close, d.amount "
            "FROM sw_industry_daily d JOIN sw_industry i ON d.index_code = i.index_code "
            "WHERE d.trade_date=? AND i.level=? "
            "ORDER BY d.change_pct DESC", (date_str, level)).fetchall()
        if not rows:
            # 找最近一个交易日
            row = conn.execute(
                "SELECT MAX(trade_date) FROM sw_industry_daily").fetchone()
            last_date = row[0] if row else None
            if not last_date:
                return jsonify({'date': date_str, 'level': level, 'industries': [], 'total': 0})
            rows = conn.execute(
                "SELECT i.index_code, i.industry_name, d.change_pct, d.close, d.amount "
                "FROM sw_industry_daily d JOIN sw_industry i ON d.index_code = i.index_code "
                "WHERE d.trade_date=? AND i.level=? "
                "ORDER BY d.change_pct DESC", (last_date, level)).fetchall()
            date_str = last_date

        # 2) 板块内涨停个股数 (从 akshare_zt_pool 关联 sw_industry_member)
        # 3) 领涨龙头 (板块内涨幅最大的 1 只涨停股)
        # 4) 资金净流入 (从 fund_flow 关联成分股, 简化: 用板块涨幅近似; 精确: 计算板块成分股资金流)
        # 5) 持续性评分 (近 3 日涨幅连续性)
        industries = []
        for r in rows:
            idx = r['index_code']
            # 涨停数
            zt_row = conn.execute(
                "SELECT COUNT(*) AS n FROM akshare_zt_pool azp "
                "JOIN sw_industry_member sim ON azp.ts_code = sim.ts_code "
                "WHERE sim.index_code=? AND azp.trade_date=?",
                (idx, date_str)).fetchone()
            zt_count = (zt_row['n'] if zt_row else 0) or 0
            # 领涨龙头 (板块内涨幅最大的 1 只涨停股)
            lead_row = conn.execute(
                "SELECT azp.name, azp.pct_chg FROM akshare_zt_pool azp "
                "JOIN sw_industry_member sim ON azp.ts_code = sim.ts_code "
                "WHERE sim.index_code=? AND azp.trade_date=? "
                "ORDER BY azp.pct_chg DESC LIMIT 1", (idx, date_str)).fetchone()
            leader = {'name': lead_row['name'], 'pct_chg': lead_row['pct_chg']} if lead_row else None
            # 持续性: 近 3 日申万涨幅, 正数天数 >= 2 = 持续强
            last3 = conn.execute(
                "SELECT change_pct FROM sw_industry_daily WHERE index_code=? "
                "ORDER BY trade_date DESC LIMIT 3", (idx,)).fetchall()
            last3_vals = [r2['change_pct'] for r2 in last3 if r2['change_pct'] is not None]
            up_days = sum(1 for v in last3_vals if v > 0)
            avg3 = (sum(last3_vals) / len(last3_vals)) if last3_vals else 0
            persistent_score = min(100, max(0, 50 + up_days * 17 + avg3 * 5))
            # 资金净流入 (近 1 日, 从 fund_flow 关联成分股)
            fund_row = conn.execute(
                "SELECT SUM(f.main_net_inflow) AS total "
                "FROM fund_flow f JOIN sw_industry_member sim ON f.ts_code = sim.ts_code "
                "WHERE sim.index_code=? AND f.trade_date=?",
                (idx, date_str)).fetchone()
            main_net = (fund_row['total'] if fund_row and fund_row['total'] else 0) or 0
            # 主线/支线/轮动判断
            tag = '轮动'
            if r['change_pct'] is not None and r['change_pct'] >= 3 and zt_count >= 5:
                tag = '主线'
            elif r['change_pct'] is not None and r['change_pct'] >= 1 and zt_count >= 2:
                tag = '支线'

            industries.append({
                'index_code': idx,
                'industry_name': r['industry_name'],
                'level': level,
                'change_pct': r['change_pct'],
                'close': r['close'],
                'amount': r['amount'],
                'limit_up_count': zt_count,
                'leader': leader,
                'main_net': main_net,
                'persistent_score': round(persistent_score, 1),
                'tag': tag,
            })
        return jsonify({
            'date': date_str,
            'level': level,
            'industries': industries,
            'total': len(industries),
        })
    finally:
        conn.close()


@app.route('/api/limitup/hierarchy', methods=['GET'])
def api_limitup_hierarchy():
    """连板梯队 (需求文档 §3.3). query: date=YYYY-MM-DD (默认今天) & min_streak=1
    返回: 按连板数分组的梯队列表, 含开板次数/封单金额/封板时间/行业.
    1板 = streak=1, 2板 = streak=2, 3板+ = streak>=3, 高位板 = streak>=5 (可配)."""
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    min_streak = int(request.args.get('min_streak', 1))
    high_threshold = int(_get_config('streak_high_threshold', '5'))
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ts_code, name, industry, streak, open_times, seal_amount, "
            "first_seal_time, last_seal_time, pct_chg, close "
            "FROM akshare_zt_pool WHERE trade_date=? AND streak >= ? "
            "ORDER BY streak DESC, seal_amount DESC",
            (date_str, min_streak)).fetchall()
        # 分组
        groups = {
            '1板': [],
            '2板': [],
            '3板+': [],
            f'{high_threshold}板+ (高位)': [],
        }
        for r in rows:
            s = r['streak'] or 0
            item = {
                'ts_code': r['ts_code'], 'name': r['name'],
                'industry': r['industry'], 'streak': s,
                'open_times': r['open_times'] or 0,
                'seal_amount': r['seal_amount'],
                'first_seal_time': r['first_seal_time'],
                'last_seal_time': r['last_seal_time'],
                'pct_chg': r['pct_chg'], 'close': r['close'],
                # 状态: '涨停'/'炸板'/'回封'
                'status': '涨停' if (r['open_times'] or 0) == 0 else '炸板',
            }
            if s >= high_threshold:
                groups[f'{high_threshold}板+ (高位)'].append(item)
            elif s >= 3:
                groups['3板+'].append(item)
            elif s == 2:
                groups['2板'].append(item)
            else:
                groups['1板'].append(item)
        # 移除空组
        result = {k: v for k, v in groups.items() if v}
        return jsonify({
            'date': date_str,
            'min_streak': min_streak,
            'high_threshold': high_threshold,
            'groups': result,
            'total': len(rows),
        })
    finally:
        conn.close()


@app.route('/api/limitup/promotion-rate', methods=['GET'])
def api_limitup_promotion_rate():
    """连板晋级率 (需求文档 §3.3 末). 文档原话: 计算连板晋级率, 反映情绪承接力度.
    定义: 当日 streak>=N 的股数 / 昨日 streak>=N-1 且今日存在 (= 今日晋级 N 板的比例).
    简化实现: 用 akshare_zt_pool 当日 streak, 与昨日 limitup 行业 streak (如果有) 对比.
    文档没有严格定义晋级率公式, 此处用 '今日 N 板及以上家数 / 今日涨停总数' 作为简化指标.
    """
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN streak >= 1 THEN 1 ELSE 0 END) AS s1, "
            "SUM(CASE WHEN streak >= 2 THEN 1 ELSE 0 END) AS s2, "
            "SUM(CASE WHEN streak >= 3 THEN 1 ELSE 0 END) AS s3, "
            "SUM(CASE WHEN streak >= 5 THEN 1 ELSE 0 END) AS s5 "
            "FROM akshare_zt_pool WHERE trade_date=?",
            (date_str,)).fetchone()
        total = (row['total'] if row else 0) or 0
        return jsonify({
            'date': date_str,
            'total': total,
            'rates': {
                '1板率': (row['s1'] or 0) / total * 100 if total else 0,
                '2板率': (row['s2'] or 0) / total * 100 if total else 0,
                '3板率': (row['s3'] or 0) / total * 100 if total else 0,
                '5板率': (row['s5'] or 0) / total * 100 if total else 0,
            },
        })
    finally:
        conn.close()

# ============ 自选板块 (user_sectors, 板块名列表, 复用 limitup 聚合) ============

def _enrich_user_sectors_with_stats(user_rows, days, search, sort):
    """对 user_sectors 行数组, 从 limitup 表拉统计 (hot_count/stock_count/days_count/latest_date/top_stocks).
    user_rows: [{'id', 'name', 'note', 'created_at', 'updated_at', 'pinned'}, ...]
    返回: 同样的数组, 加上 hot_count/stock_count/days_count/latest_date/top_stocks 字段
    规则: 不在 limitup 出现过的板块 (limitup 表里没数据) 也要返回, 统计字段填 0 / None, 便于用户保存"预备关注"的板块
    """
    if not user_rows:
        return []
    names = [r['name'] for r in user_rows]
    placeholders = ','.join('?' * len(names))
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # 1) 全时间统计: stock_count / days_count / latest_date
    base_sql = f"""
        SELECT sector,
               COUNT(DISTINCT code) AS stock_count,
               COUNT(DISTINCT date) AS days_count,
               MAX(date) AS latest_date
        FROM limitup
        WHERE sector IN ({placeholders})
          AND sector NOT IN ('其他', '公告')
        GROUP BY sector
    """
    base_rows = conn.execute(base_sql, names).fetchall()
    base_map = {r['sector']: dict(r) for r in base_rows}

    # 2) 近 N 天 hot_count
    if days > 0:
        hot_sql = f"""
            SELECT sector, COUNT(*) AS hot_count
            FROM limitup
            WHERE sector IN ({placeholders})
              AND sector NOT IN ('其他', '公告')
              AND date >= date('now', ?)
            GROUP BY sector
        """
        hot_rows = conn.execute(hot_sql, names + [f'-{days} days']).fetchall()
        hot_map = {r['sector']: r['hot_count'] for r in hot_rows}
    else:
        # 全部时间 = 累计 hot_count
        hot_map = {n: base_map.get(n, {}).get('stock_count', 0) for n in names}

    # 3) 最新一日的代表股 top 3 (按流通市值)
    latest_date = conn.execute("SELECT MAX(date) AS d FROM limitup").fetchone()['d']
    rep_map = {}
    if latest_date:
        rep_sql = f"""
            SELECT sector, code, name, streak, marketCap
            FROM limitup
            WHERE date = ? AND sector IN ({placeholders})
              AND sector NOT IN ('其他', '公告')
            ORDER BY marketCap DESC NULLS LAST
        """
        rep_rows = conn.execute(rep_sql, [latest_date] + names).fetchall()
        for r in rep_rows:
            rep_map.setdefault(r['sector'], []).append({
                'ts_code': r['code'],
                'name': r['name'],
                'streak': r['streak'],
                'market_cap': r['marketCap'],
            })

    conn.close()

    # 4) 合并 + 搜索过滤
    out = []
    for r in user_rows:
        name = r['name']
        base = base_map.get(name, {})
        reps = rep_map.get(name, [])[:3]
        # 搜索过滤: 板块名 LIKE 模式
        if search and search not in name:
            continue
        out.append({
            'id': r['id'],
            'sector': name,
            'note': r.get('note'),
            'pinned': r.get('pinned', 0),
            'created_at': r.get('created_at', ''),
            'updated_at': r.get('updated_at', ''),
            'hot_count': hot_map.get(name, 0),
            'stock_count': base.get('stock_count', 0) or 0,
            'days_count': base.get('days_count', 0) or 0,
            'latest_date': base.get('latest_date') or '—',
            'top_stocks': reps,
        })

    # 排序
    if sort == 'breadth':
        out.sort(key=lambda x: (x['stock_count'], x['hot_count']), reverse=True)
    elif sort == 'latest':
        out.sort(key=lambda x: (x['latest_date'] or '', x['hot_count']), reverse=True)
    elif sort == 'created':
        out.sort(key=lambda x: x['created_at'] or '', reverse=True)
    else:  # hot
        out.sort(key=lambda x: (x['hot_count'], x['stock_count']), reverse=True)
    return out


# ============ 自选板块关联股票: 辅助函数 ============

def _connect_user_sectors_db():
    """开 sqlite 连接, 启用 FK 约束 (默认关闭) + row_factory.
    user_sector_stocks 有 FOREIGN KEY ... ON DELETE CASCADE, 不启用 FK 会留孤儿行.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    return conn

def _lookup_stock_names(codes):
    """批量查 stock_basic 拿 name; 返回 {ts_code: name}. 找不到返回 '股票名未知'."""
    if not codes:
        return {}
    placeholders = ','.join('?' * len(codes))
    rows = c.execute(f'SELECT ts_code, name FROM stock_basic WHERE ts_code IN ({placeholders})', codes).fetchall()
    return {r['ts_code']: r['name'] for r in rows}


def _load_user_sector_stocks(conn, sector_ids):
    """批量拉 sector_ids 对应的所有股票 (按 role/rank 排序). 返回 {sector_id: [{ts_code, name, role, rank}, ...]}"""
    if not sector_ids:
        return {}
    placeholders = ','.join('?' * len(sector_ids))
    rows = conn.execute(f'''
        SELECT sector_id, ts_code, name, role, rank
        FROM user_sector_stocks
        WHERE sector_id IN ({placeholders})
        ORDER BY sector_id, role, rank
    ''', sector_ids).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['sector_id'], []).append({
            'ts_code': r['ts_code'],
            'name': r['name'] or '',
            'role': r['role'] or 'legacy',
            'rank': r['rank'] if r['rank'] is not None else 99,
        })
    # 对每个 sector_id 的股票: 旧数据没有 name, 批量回查 stock_basic
    for sid, stocks in out.items():
        missing = [s for s in stocks if not s['name']]
        if missing:
            name_map = _lookup_stock_names(conn, [s['ts_code'] for s in missing])
            for s in stocks:
                if not s['name']:
                    s['name'] = name_map.get(s['ts_code'], '股票名未知')
    return out


def _insert_user_sector_stock(conn, sector_id, ts_code, role, rank, name=None):
    """插入一只股票到 user_sector_stocks. 重复则跳过 (ON CONFLICT DO NOTHING)."""
    if name is None:
        # 查 stock_basic 拿 name
        row = conn.execute('SELECT name FROM stock_basic WHERE ts_code = ?', (ts_code,)).fetchone()
        name = row['name'] if row else '股票名未知'
    try:
        conn.execute('''
            INSERT INTO user_sector_stocks (sector_id, ts_code, name, role, rank)
            VALUES (?, ?, ?, ?, ?)
        ''', (sector_id, ts_code, name, role, rank))
        return True, name
    except sqlite3.IntegrityError:
        # UNIQUE(sector_id, ts_code) 冲突 → 跳过
        return False, name


@app.route('/api/user-sectors', methods=['GET'])
def api_user_sectors_list():
    """自选板块列表 (user_sectors), 复用 limitup 聚合统计.
    query: days (默认 7), search (板块名包含), sort (hot/breadth/latest/created, 默认 hot)
    response 中每个 sector 多一个 stocks 字段: [{ts_code, name, role, rank}, ...]
    """
    days = max(0, int(request.args.get('days', 7)))
    search = (request.args.get('search') or '').strip()
    sort = request.args.get('sort', 'hot')
    conn = _connect_user_sectors_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT id, name, pinned, created_at, updated_at, note FROM user_sectors ORDER BY created_at DESC').fetchall()
    # 批量拉所有 stocks (一次查询, 避免 N+1)
    stocks_by_sector = _load_user_sector_stocks(conn, [r['id'] for r in rows])
    conn.close()
    sectors = _enrich_user_sectors_with_stats([dict(r) for r in rows], days, search, sort)
    # 把 stocks 挂到对应 sector 上
    for s in sectors:
        s['stocks'] = stocks_by_sector.get(s['id'], [])
        s['stock_total'] = len(s['stocks'])
    return jsonify({
        'sectors': sectors,
        'total': len(sectors),
        'days': days,
        'sort': sort,
    })


@app.route('/api/user-sectors', methods=['POST'])
def api_user_sectors_add():
    """添加一个自选板块. body: {name, note?, stocks?: [{code, role, rank}, ...]}
    name: 必填, 板块名
    stocks: 可选, 同时插入的股票列表; role ∈ {'core','peer'}, rank ∈ {1,2,3}
    重复添加返回 200 + action=already_exists (stocks 不会重复插入)
    """
    data = request.get_json(silent=True) or request.form
    name = (data.get('name') or '').strip()
    note = (data.get('note') or '').strip() or None
    if not name:
        return jsonify({'status': 'error', 'message': '板块名不能为空'}), 400
    if len(name) > 50:
        return jsonify({'status': 'error', 'message': '板块名过长 (限 50 字符)'}), 400
    conn = _connect_user_sectors_db()
    conn.row_factory = sqlite3.Row
    existing = conn.execute('SELECT id, name, created_at FROM user_sectors WHERE name = ? COLLATE NOCASE', (name,)).fetchone()
    if existing:
        sector_id = existing['id']
        action = 'already_exists'
    else:
        cur = conn.execute('INSERT INTO user_sectors (name, note) VALUES (?, ?)', (name, note))
        sector_id = cur.lastrowid
        action = 'added'
    # 解析 stocks 列表 [{code, role, rank}]
    raw_stocks = data.get('stocks') or []
    inserted = []
    skipped = []
    invalid = []
    if isinstance(raw_stocks, list):
        for item in raw_stocks:
            if not isinstance(item, dict):
                continue
            raw_code = (item.get('code') or '').strip()
            if not raw_code:
                continue
            ts_code = _normalize_code(raw_code)
            if not ts_code:
                invalid.append({'raw': raw_code, 'reason': '代码格式无效'})
                continue
            role = (item.get('role') or '').strip()
            if role not in ('core', 'peer'):
                invalid.append({'raw': raw_code, 'reason': f'role 必须是 core/peer, 收到 {role!r}'})
                continue
            try:
                rank = int(item.get('rank', 0))
            except (TypeError, ValueError):
                rank = 0
            if rank not in (1, 2, 3):
                invalid.append({'raw': raw_code, 'reason': f'rank 必须是 1/2/3, 收到 {rank}'})
                continue
            ok, name_found = _insert_user_sector_stock(conn, sector_id, ts_code, role, rank)
            if ok:
                inserted.append({'ts_code': ts_code, 'name': name_found, 'role': role, 'rank': rank})
            else:
                skipped.append(ts_code)
    conn.commit()
    # 拿回 created_at
    row = conn.execute('SELECT created_at FROM user_sectors WHERE id = ?', (sector_id,)).fetchone()
    conn.close()
    resp = {
        'status': 'success',
        'action': action,
        'id': sector_id,
        'name': name,
        'created_at': row['created_at'] if row else None,
        'stocks_inserted': len(inserted),
        'stocks_skipped': skipped,
        'stocks_invalid': invalid,
    }
    if action == 'already_exists':
        resp['message'] = f'"{name}" 已在自选板块中'
    return jsonify(resp)


@app.route('/api/user-sectors/<path:name>', methods=['DELETE'])
def api_user_sectors_delete(name):
    """删除自选板块. 路径参数 name 接受任意字符 (URL 解码后做大小写不敏感匹配).
    CASCADE 自动删 user_sector_stocks 关联股票 (FOREIGN KEY ON DELETE CASCADE)
    """
    from urllib.parse import unquote
    name = unquote(name).strip()
    if not name:
        return jsonify({'status': 'error', 'message': '板块名不能为空'}), 400
    conn = _connect_user_sectors_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT id, name FROM user_sectors WHERE name = ? COLLATE NOCASE', (name,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': f'自选板块不存在: {name}'}), 404
    # 先数一下关联股票, 用于返回信息
    stock_count = conn.execute('SELECT COUNT(*) AS c FROM user_sector_stocks WHERE sector_id = ?', (row['id'],)).fetchone()['c']
    conn.execute('DELETE FROM user_sectors WHERE id = ?', (row['id'],))
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'action': 'deleted',
        'id': row['id'],
        'name': row['name'],
        'stocks_deleted': stock_count,
    })


# ---- 单只股票: 增 / 删 / 详情 ----

@app.route('/api/user-sectors/<path:name>/stocks', methods=['POST'])
def api_user_sector_add_stock(name):
    """给已有板块加一只股票. body: {code, role, rank}
    role: 'core' | 'peer'; rank: 1|2|3
    """
    from urllib.parse import unquote
    name = unquote(name).strip()
    data = request.get_json(silent=True) or request.form
    raw_code = (data.get('code') or '').strip()
    role = (data.get('role') or '').strip()
    try:
        rank = int(data.get('rank', 0))
    except (TypeError, ValueError):
        rank = 0
    if not name:
        return jsonify({'status': 'error', 'message': '板块名缺失'}), 400
    if not raw_code:
        return jsonify({'status': 'error', 'message': '代码缺失'}), 400
    if role not in ('core', 'peer'):
        return jsonify({'status': 'error', 'message': f'role 必须是 core/peer, 收到 {role!r}'}), 400
    if rank not in (1, 2, 3):
        return jsonify({'status': 'error', 'message': f'rank 必须是 1/2/3, 收到 {rank}'}), 400
    ts_code = _normalize_code(raw_code)
    if not ts_code:
        return jsonify({'status': 'error', 'message': f'代码格式无效: {raw_code!r}'}), 400
    conn = _connect_user_sectors_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT id FROM user_sectors WHERE name = ? COLLATE NOCASE', (name,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': f'自选板块不存在: {name}'}), 404
    sector_id = row['id']
    # 同一 (role, rank) 已存在: 替换 (update); 同一 ts_code 已存在: 跳过
    existing_by_code = conn.execute('SELECT id, role, rank FROM user_sector_stocks WHERE sector_id = ? AND ts_code = ?', (sector_id, ts_code)).fetchone()
    if existing_by_code:
        conn.close()
        return jsonify({
            'status': 'success',
            'action': 'already_exists',
            'ts_code': ts_code,
            'message': f'{ts_code} 已在该板块 ({existing_by_code["role"]}-{existing_by_code["rank"]})',
        })
    # 同 (role, rank) 占位: 删旧的 (让新股票占位)
    conn.execute('DELETE FROM user_sector_stocks WHERE sector_id = ? AND role = ? AND rank = ?', (sector_id, role, rank))
    ok, name_found = _insert_user_sector_stock(conn, sector_id, ts_code, role, rank)
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'action': 'added' if ok else 'replaced',
        'ts_code': ts_code,
        'name': name_found,
        'role': role,
        'rank': rank,
    })


@app.route('/api/user-sectors/<path:name>/stocks/<path:ts_code>', methods=['DELETE'])
def api_user_sector_delete_stock(name, ts_code):
    """从板块删一只股票."""
    from urllib.parse import unquote
    name = unquote(name).strip()
    ts_code = unquote(ts_code).strip()
    norm = _normalize_code(ts_code)
    if not name or not norm:
        return jsonify({'status': 'error', 'message': '板块名或代码缺失'}), 400
    conn = _connect_user_sectors_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT id FROM user_sectors WHERE name = ? COLLATE NOCASE', (name,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': f'自选板块不存在: {name}'}), 404
    cur = conn.execute('DELETE FROM user_sector_stocks WHERE sector_id = ? AND ts_code = ?', (row['id'], norm))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        return jsonify({'status': 'error', 'message': f'{norm} 不在该板块中'}), 404
    return jsonify({
        'status': 'success',
        'action': 'deleted',
        'ts_code': norm,
    })


@app.route('/api/user-sectors/<path:name>/detail', methods=['GET'])
def api_user_sector_detail(name):
    """自选板块详情 (二级页): 板块信息 + 6 只股票 + 实时行情.
    返回结构跟 /api/sector-collections/<sector> 部分相似, 但 stocks 字段是用户选的 6 只, 不是全市场历史.
    """
    from urllib.parse import unquote
    name = unquote(name).strip()
    conn = _connect_user_sectors_db()
    conn.row_factory = sqlite3.Row
    sec = conn.execute('SELECT id, name, note, created_at, updated_at, pinned FROM user_sectors WHERE name = ? COLLATE NOCASE', (name,)).fetchone()
    if not sec:
        conn.close()
        return jsonify({'status': 'error', 'message': f'自选板块不存在: {name}'}), 404
    # 拉 6 只股票
    stocks = _load_user_sector_stocks(conn, [sec['id']]).get(sec['id'], [])
    conn.close()
    # 拉实时行情 (sina) - 用全量 fetch_sina_quotes, 跟选股推荐 / 持仓同一份
    codes = [s['ts_code'] for s in stocks]
    quotes = {}
    if codes:
        try:
            quotes = fetch_sina_quotes(codes)
        except Exception as e:
            print(f'[user_sector_detail] sina 拉取失败: {e}', flush=True)
    # 拼装每只股票的实时信息
    stocks_out = []
    for s in stocks:
        q = quotes.get(s['ts_code']) or {}
        price = q.get('price')
        change_pct = q.get('change_pct')
        prev_close = q.get('prev_close')
        stocks_out.append({
            'ts_code': s['ts_code'],
            'name': s['name'],
            'role': s['role'],
            'rank': s['rank'],
            'price': price,
            'change_pct': change_pct,
            'prev_close': prev_close,
            'quote_time': q.get('time', ''),
        })
    return jsonify({
        'sector': sec['name'],
        'id': sec['id'],
        'note': sec['note'],
        'created_at': sec['created_at'],
        'pinned': sec['pinned'],
        'stocks': stocks_out,
        'core_count': sum(1 for s in stocks_out if s['role'] == 'core'),
        'peer_count': sum(1 for s in stocks_out if s['role'] == 'peer'),
    })


if __name__ == '__main__':
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")
    # 启动 APScheduler 调度 (阶段 0.4 框架, 各阶段任务在各阶段代码里注册)
    try:
        from scheduler import start_scheduler, register_sentiment_sampling, register_daily_review, register_push_jobs
        register_sentiment_sampling()   # 阶段 1
        register_daily_review()          # 阶段 3
        register_push_jobs()             # 阶段 5
        start_scheduler()
    except Exception as e:
        print(f'[scheduler] 启动失败 (非致命, Flask 继续): {e}')
    print("启动 Flask 服务...")
    app.run(debug=True, host='0.0.0.0', port=5555)
