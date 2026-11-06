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
from sync_jobs import (
    _ocr_status_path, _save_result, _read_ocr_status, _schedule_ocr_image_cleanup,
    run_ocr_job, run_fetch_and_ocr,
    fetch_jiuye_diagram, _load_jiuye_creds,
    _save_em_fund_flow_rows,
    refresh_sector_summary_for_codes, rebuild_sector_summary,
)

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

# 飞书表格同步 (盘面晴雨表 → 飞书)
FEISHU_BAROMETER_SPREADSHEET_TOKEN = 'Pbhssp0pshZXPktMddpcaHJlnue'  # "盘面晴雨表" spreadsheet
LARK_CLI_BIN = '/Users/xyra/.local/bin/lark-cli'                     # 本机 lark-cli 绝对路径 (PATH 不一定含 .local/bin)
LARK_CMD_TIMEOUT = 60                                                # 单次 lark-cli subprocess 超时 (秒)
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)                                   # lark 失败时落日志的目录 (幂等)

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

    # 清理遗留的情绪分析表 (2026-07 删除 sentiment/alert_rules 模块后)
    c.execute('DROP TABLE IF EXISTS sentiment_intraday')
    c.execute('DROP TABLE IF EXISTS sentiment_alert')

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

    # 个股分时 (新浪分钟线) 落库: 盘后批量 + lazy 兜底都用这张表.
    # PRAGMA 主键 (ts_code, trade_date, m) 保证 INSERT OR REPLACE 幂等.
    c.execute('''CREATE TABLE IF NOT EXISTS stock_minline_bars (
        ts_code    TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        m          TEXT NOT NULL,
        p          REAL,
        avg_p      REAL,
        v          REAL NOT NULL DEFAULT 0,
        name       TEXT,
        prev_close REAL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (ts_code, trade_date, m)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_minline_ts_date ON stock_minline_bars(ts_code, trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_minline_date ON stock_minline_bars(trade_date)')

    # 指数分时 (4 只指数 × 240 行/天, 跟个股结构对称但独立表)
    c.execute('''CREATE TABLE IF NOT EXISTS index_minline_bars (
        ts_code    TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        m          TEXT NOT NULL,
        p          REAL,
        avg_p      REAL,
        v          REAL NOT NULL DEFAULT 0,
        prev_close REAL,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (ts_code, trade_date, m)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_idx_minline_ts_date ON index_minline_bars(ts_code, trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_idx_minline_date ON index_minline_bars(trade_date)')

    # 切换到 WAL 模式: 全市场批量写 + lazy 并发写不互锁
    try:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass

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

    # ── fund_flow_ranked 物化表: 预排每只股的 rn + close_anchor + stock_daily 字段
    # 主 SELECT 读这张表避免 ROW_NUMBER sort + LEFT JOIN stock_daily (提速 35 倍)
    c.execute('''CREATE TABLE IF NOT EXISTS fund_flow_ranked (
        ts_code TEXT, trade_date TEXT, source TEXT,
        rn INTEGER,
        code TEXT, name TEXT,
        close REAL, change_pct REAL, high REAL,
        main_net_inflow REAL, super_net REAL, mid_net REAL, small_net REAL,
        close_anchor REAL,
        PRIMARY KEY (ts_code, trade_date, source)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ffr_date ON fund_flow_ranked(trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ffr_rn   ON fund_flow_ranked(source, rn)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ffr_ts   ON fund_flow_ranked(ts_code, trade_date DESC)')

    # ── 大盘实时资金流向 (沪深合计, 东财 push2 分钟K线) ──
    # scheduler 盘后 15:35 拉一次落表; endpoint 直接查这里, 不做盘中实时.
    # curves / last / pct 走 JSON 避免 5 档数组拆 5 列.
    c.execute('''CREATE TABLE IF NOT EXISTS market_fflow (
        trade_date TEXT PRIMARY KEY,            -- YYYYMMDD
        times TEXT NOT NULL,                    -- JSON ['09:31', ...]
        series TEXT NOT NULL,                   -- JSON {main, super, big, mid, small: [..]}
        amount REAL,                            -- 两市成交额 (元)
        last TEXT NOT NULL,                     -- JSON {main, super, big, mid, small: float}
        pct TEXT NOT NULL,                      -- JSON {main, super, big, mid, small: float%}
        complete INTEGER DEFAULT 0,             -- 1=曲线到 15:00 定格
        fetched_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 同花顺个股资金流 (data.10jqka.com.cn/funds/ggzjl, fetch_fund_flow_ths.py 抓取) ──
    # 独立建表不复用 fund_flow: ① fund_flow UNIQUE(trade_date, ts_code) 会让两个源互相覆盖
    # ② 同花顺特有字段 (流入/流出/换手率/成交额) fund_flow 没有对应列
    # 注意口径: net = 流入-流出, 是总资金净流入, 不是东财的「主力净流入」(超大单+大单)
    c.execute('''CREATE TABLE IF NOT EXISTS fund_flow_ths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        close REAL, change_pct REAL,
        turnover_pct REAL,                      -- 换手率 %
        inflow REAL,                            -- 流入资金 (元)
        outflow REAL,                           -- 流出资金 (元)
        net REAL,                               -- 净额 (元) = 流入-流出
        amount REAL,                            -- 成交额 (元)
        big_inflow REAL,                        -- 大单流入 (元), 页面多数时候没有这列 → NULL
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(trade_date, ts_code)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ffths_date     ON fund_flow_ths(trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ffths_date_net ON fund_flow_ths(trade_date, net DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ffths_ts_code  ON fund_flow_ths(ts_code, trade_date DESC)')

    # ── 东方财富板块资金流 (行业 + 概念, push2 clist/get) ──
    # 独立表不复用 fund_flow: ① 板块没有 ts_code, 用 sector_code (BK1036 字符串)
    # ② UNIQUE 要带 sector_type 区分行业/概念 ③ 多了 lead_stock_* 字段
    c.execute('''CREATE TABLE IF NOT EXISTS sector_fund_flow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,           -- YYYYMMDD
        sector_type TEXT NOT NULL,          -- 'industry' / 'concept'
        sector_code TEXT NOT NULL,          -- BK1036 (原值存, 不转换)
        name TEXT,
        close REAL, change_pct REAL,
        main_net_inflow REAL NOT NULL,
        main_net_pct REAL,
        super_net REAL, super_pct REAL,
        big_net REAL, big_pct REAL,
        mid_net REAL, mid_pct REAL,
        small_net REAL, small_pct REAL,
        lead_stock_name TEXT,               -- f128
        lead_stock_code TEXT,               -- f140
        source TEXT DEFAULT 'eastmoney-push2',
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(trade_date, sector_type, sector_code)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sff_date_inflow ON sector_fund_flow(trade_date, sector_type, main_net_inflow DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sff_sector       ON sector_fund_flow(sector_type, sector_code, trade_date DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sff_date         ON sector_fund_flow(trade_date)')

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

    # 同花顺净额选股推荐 (独立表, 数据源 fund_flow_ths, 口径 = 流入-流出 总资金净流入)
    c.execute('''CREATE TABLE IF NOT EXISTS stock_picks_ths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        ts_code TEXT NOT NULL,
        name TEXT,
        close REAL,
        change_pct REAL,
        -- MA 同 EM
        ma5 REAL, ma10 REAL, ma20 REAL, ma30 REAL,
        ma5_slope REAL, ma10_slope REAL, ma20_slope REAL, ma30_slope REAL,
        -- THS 净额 4 窗口 (今日 + 3d/5d/10d, 跳过 20d 因历史太短)
        ths_net_today REAL,
        ths_net_3d REAL,
        ths_net_5d REAL,
        ths_net_10d REAL,
        -- 评分 (THS 算法独立, 没有 main_net_pct 占比维度)
        score REAL,
        signal_type TEXT,
        flow_days_available INTEGER,  -- fund_flow_ths 表可用天数
        reasons_json TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(trade_date, ts_code)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_stock_picks_ths_date ON stock_picks_ths(trade_date, score DESC)')

    # ── 同花顺条件选股盘后快照 (独立表, 数据源 fund_flow_ths.net) ──
    # 跟 EM 版 conditional_picks_snapshot 同结构, 但 sum_* 列只用 ths.net 一列 (无 5 档)
    c.execute('''CREATE TABLE IF NOT EXISTS conditional_picks_snapshot_ths (
        trade_date TEXT PRIMARY KEY,
        count INTEGER NOT NULL,
        results_json TEXT NOT NULL,
        next_date TEXT,
        next_date_2 TEXT,
        next_date_3 TEXT,
        next_date_4 TEXT,
        next_date_5 TEXT,
        intraday_used INTEGER DEFAULT 0,
        saved_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 同花顺组合选股盘后快照 (stock_picks_ths ∩ conditional_picks_snapshot_ths) ──
    c.execute('''CREATE TABLE IF NOT EXISTS combined_picks_snapshot_ths (
        trade_date TEXT PRIMARY KEY,
        count INTEGER NOT NULL,
        results_json TEXT NOT NULL,
        note TEXT,
        saved_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 条件选股新盘后快照 (EM 基础 + MA10 + 缩量过滤, 由 api_conditional_picks_new 写入) ──
    c.execute('''CREATE TABLE IF NOT EXISTS conditional_picks_new_snapshot (
        trade_date TEXT PRIMARY KEY,
        count INTEGER NOT NULL,
        results_json TEXT NOT NULL,
        exclusions_json TEXT,
        threshold_yi REAL,
        volume_ratio_pct REAL,
        saved_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # ── 条件优选盘后快照 (条件选股新 + 主力5日强度 + 小单净流出 + outcomes, 由 api_conditional_picks_preferred 写入) ──
    c.execute('''CREATE TABLE IF NOT EXISTS conditional_picks_preferred_snapshot (
        trade_date TEXT PRIMARY KEY,
        count INTEGER NOT NULL,
        results_json TEXT NOT NULL,
        exclusions_json TEXT,
        threshold_yi REAL,
        volume_ratio_pct REAL,
        saved_at TEXT DEFAULT (datetime('now','localtime'))
    )''')

    # 自选股 (单一分组, user 手动维护, 复用 _compute_picks 算法但不入 stock_picks 表)
    c.execute('''CREATE TABLE IF NOT EXISTS user_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT NOT NULL,
        added_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        note TEXT
    )''')
    # 兼容老库: 如果 user_picks 还在用 ts_code 做主键, 迁移到新 schema
    _migrate_user_picks_to_id_pk(c)

    # 旧自选板块功能已移除；历史表保留，不再创建、迁移或清理。

    # 盘面晴雨表 (按日汇总, 单日单行; 手机端晴雨 tab 渲染; comments / signal 本轮不写, 留人工填)
    c.execute('''CREATE TABLE IF NOT EXISTS market_barometer (
        date TEXT PRIMARY KEY,                          -- YYYYMMDD
        shanghai_index REAL, shanghai_change REAL,
        kechuang50_index REAL, kechuang50_change REAL,
        main_money_yi REAL, big_money_yi REAL, small_money_yi REAL,
        turnover_yi REAL,
        advancers INTEGER, decliners INTEGER,
        top1_name TEXT,  top1_rate REAL,  top1_money REAL,  top1_stock TEXT,
        top2_name TEXT,  top2_rate REAL,  top2_money REAL,  top2_stock TEXT,
        top3_name TEXT,  top3_rate REAL,  top3_money REAL,  top3_stock TEXT,
        hot5_sector TEXT,  hot5_stock TEXT,
        hot10_sector TEXT, hot10_stock TEXT,
        hot20_sector TEXT, hot20_stock TEXT,
        comments TEXT,
        signal TEXT,
        synced_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_market_barometer_synced ON market_barometer(synced_at)')

    # 盘面晴雨表 · 手工笔记表 (xlsx 同款字段, 全部可空, 本轮只读)
    c.execute('''CREATE TABLE IF NOT EXISTS market_notes (
        date TEXT PRIMARY KEY,
        weekdays TEXT,
        shanghai_index REAL, change_rate REAL,
        kechuang50_index REAL, kechuang50_rate REAL,
        main_money REAL, big_money REAL, small_money REAL, turnover REAL,
        advancers INTEGER, decliners INTEGER,
        today_top1_concept TEXT, today_top1_concept_rate REAL,
        today_top1_concept_money REAL, today_top1_concept_stock TEXT,
        today_top2_concept TEXT, today_top2_concept_rate REAL,
        today_top2_concept_money REAL, today_top2_concept_stock TEXT,
        today_top3_concept TEXT, today_top3_concept_rate REAL,
        today_top3_concept_money REAL, today_top3_concept_stock TEXT,
        hot5_sector TEXT, hot5_stock TEXT,
        hot10_sector TEXT, hot10_stock TEXT,
        hot20_sector TEXT, hot20_stock TEXT,
        comments TEXT, signal TEXT,
        synced_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_market_notes_synced ON market_notes(synced_at)')

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

    # 涨停合集 (盘中 scheduler 每分钟扫全市场 sina 实时行情收录: high >= 涨停价 即收录, 曾涨停就保留;
    # sealed=当前是否封板, broken=曾封板后炸开. 区别于 limitup 表(韭研 OCR, T+1) 和 akshare_zt_pool(盘后快照))
    c.execute('''CREATE TABLE IF NOT EXISTS limitup_collection (
        trade_date TEXT NOT NULL,         -- YYYYMMDD
        ts_code TEXT NOT NULL,            -- 带后缀, e.g. '000001.SZ'
        name TEXT,
        market TEXT,                      -- 主板/创业板/科创板/北交所
        limit_pct REAL,                   -- 涨停档位 10/20/30/5
        first_limit_time TEXT,            -- 首次被扫到涨停 HH:MM:SS
        sealed INTEGER DEFAULT 1,         -- 最后一轮扫描时是否封板
        broken INTEGER DEFAULT 0,         -- 曾封板后打开过
        last_seen_time TEXT,              -- 最后一轮扫描时间
        PRIMARY KEY (trade_date, ts_code)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_limitup_collection_date ON limitup_collection(trade_date)')

    # 推送配置 (feishu 读阈值用)
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

    # 启用现有业务的外键约束。
    conn.execute('PRAGMA foreign_keys = ON')
    conn.close()

    # 启动时全量重建 sector summary (几百 ms, 1929 只股)
    try:
        n = rebuild_sector_summary()
        print(f'[init_db] stock_sector_summary 重建完成: {n} 只股')
    except Exception as e:
        print(f'[init_db] sector summary 重建失败 (非致命): {e}')


def sync_market_barometer(date: str) -> dict:
    """盘面晴雨表同步: 从本地数据库计算并 UPSERT 某日 (YYYYMMDD) 一行.
    数据源: index_daily (上证/科创50) / overview (成交/涨跌家数) /
            market_fflow.last JSON (5 档资金) / sector_fund_flow (top 概念) /
            leaderboard + limitup (近 5/10/20 日热门板块+个股).
    comments / signal 不写, 留作未来人工填入口子."""
    import json as _json
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
    row = {}
    # 1) 核心指数
    idx = {r['name']: r for r in conn.execute(
        "SELECT name, close, change FROM index_daily WHERE date IN (?, ?) AND name IN ('上证指数','科创50')",
        (date, iso))}
    sh = idx.get('上证指数')
    if sh:
        row['shanghai_index'] = sh['close']
        row['shanghai_change'] = sh['change']
    kc = idx.get('科创50')
    if kc:
        row['kechuang50_index'] = kc['close']
        row['kechuang50_change'] = kc['change']
    # 2) overview
    ov = conn.execute("SELECT totalVolume, netFlow, upCount, downCount FROM overview WHERE date IN (?, ?)",
                      (date, iso)).fetchone()
    if ov:
        row['turnover_yi'] = ov['totalVolume']
        row['advancers'] = ov['upCount']
        row['decliners'] = ov['downCount']
    # 3) market_fflow.last (单位元 → 亿元)
    mf = conn.execute("SELECT last FROM market_fflow WHERE trade_date IN (?, ?) ORDER BY fetched_at DESC LIMIT 1",
                      (date, iso)).fetchone()
    if mf and mf['last']:
        try:
            last = _json.loads(mf['last'])
            for src, dst in (('main','main_money_yi'), ('big','big_money_yi'), ('small','small_money_yi')):
                if last.get(src) is not None:
                    row[dst] = round(last[src] / 1e8, 2)
        except (ValueError, TypeError):
            pass
    # 4) sector_fund_flow top3
    sf_rows = conn.execute(
        "SELECT name, change_pct, main_net_inflow, lead_stock_name "
        "FROM sector_fund_flow WHERE trade_date IN (?, ?) "
        "AND main_net_inflow IS NOT NULL "
        "ORDER BY main_net_inflow DESC LIMIT 3", (date, iso)).fetchall()
    for i, sr in enumerate(sf_rows, 1):
        row[f'top{i}_name'] = sr['name']
        row[f'top{i}_rate'] = sr['change_pct']
        row[f'top{i}_money'] = round((sr['main_net_inflow'] or 0) / 1e8, 2)
        row[f'top{i}_stock'] = sr['lead_stock_name']
    # 5) 5/10/20 日热门: leaderboard 5/10/20日涨幅榜 rank=1 → 关联 stock_sector_summary 取其所属板块
    for period, key in (('5日','5'), ('10日','10'), ('20日','20')):
        top = conn.execute(
            "SELECT name, code FROM leaderboard WHERE date IN (?, ?) AND type=? "
            "ORDER BY rank ASC LIMIT 1", (date, iso, period)).fetchone()
        if not top:
            continue
        row[f'hot{key}_stock'] = top['name']
        # 取其最新一次涨停所属板块 (sectors_json 取 is_latest=true 那条; 排除'公告'/'其他'等噪音)
        s = conn.execute(
            "SELECT sectors_json FROM stock_sector_summary WHERE ts_code=?",
            (top['code'],)).fetchone()
        if not s:
            continue
        try:
            sects = _json.loads(s[0])
        except (ValueError, TypeError):
            continue
        valid = [x['sector'] for x in sects
                 if x.get('is_latest') and x.get('sector') not in ('公告','其他')]
        if valid:
            row[f'hot{key}_sector'] = valid[0]
    conn.close()
    if not row:
        return {'date': date, 'written': False, 'fields_filled': 0,
                'message': '该日期无任何本地数据可填; 跳过入库.'}
    cols = ['date','shanghai_index','shanghai_change','kechuang50_index','kechuang50_change',
            'main_money_yi','big_money_yi','small_money_yi','turnover_yi',
            'advancers','decliners',
            'top1_name','top1_rate','top1_money','top1_stock',
            'top2_name','top2_rate','top2_money','top2_stock',
            'top3_name','top3_rate','top3_money','top3_stock',
            'hot5_sector','hot5_stock','hot10_sector','hot10_stock','hot20_sector','hot20_stock',
            'comments','signal']
    placeholders = ','.join(['?'] * len(cols))
    update_pairs = ','.join(f'{c}=excluded.{c}' for c in cols if c != 'date')
    values = [date] + [row.get(c) for c in cols[1:-2]] + [None, None]  # comments, signal 留 NULL
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f'INSERT INTO market_barometer ({",".join(cols)}) VALUES ({placeholders}) '
        f'ON CONFLICT(date) DO UPDATE SET {update_pairs}',
        values)
    conn.commit()
    conn.close()
    return {'date': date, 'written': True, 'fields_filled': sum(1 for v in row.values() if v is not None)}


# ============ 飞书表格推送 (盘面晴雨表 → Feishu spreadsheet) ============

def _lark_sheets_call(args, *, input_json=None, timeout=None, op_label=''):
    """调一次 lark-cli, 返回 (ok, parsed, stdout, stderr).

    判 ok 同时看 parsed.get('ok') 和 returncode — lark-cli 错误时也会 rc=0 但 stdout 是 {"ok": false, "error": ...}.
    失败时把 args+rc+stdout+stderr 写到 LOG_DIR/lark_sheets_<op_label>_<utc_ts>.json 便于排查.
    """
    if timeout is None:
        timeout = LARK_CMD_TIMEOUT
    cmd = [LARK_CLI_BIN, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, input=input_json,
        )
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        parsed = None
        try:
            parsed = json.loads(stdout) if stdout and stdout.lstrip().startswith('{') else None
        except ValueError:
            parsed = None
        ok = (rc == 0) and (parsed is None or parsed.get('ok') is True)
        if not ok:
            _dump_lark_log(op_label, cmd, rc, stdout, stderr, parsed=parsed)
        return ok, parsed, stdout, stderr
    except subprocess.TimeoutExpired as e:
        _dump_lark_log(op_label, cmd, -1, e.stdout or '', e.stderr or f'timeout {timeout}s')
        return False, None, e.stdout or '', f'timeout {timeout}s'
    except Exception as e:
        _dump_lark_log(op_label, cmd, -1, '', '', exception=str(e))
        return False, None, '', str(e)


def _dump_lark_log(op_label, cmd, rc, stdout, stderr, *, parsed=None, exception=None):
    """失败兜底: 落一份 JSON 日志到 backend/logs/ 方便排查. op_label 含步骤名 (如 'feishu_push_cells-set')."""
    try:
        ts = datetime.utcnow().strftime('%Y%m%dT%H%M%S')
        fname = f'lark_sheets_{op_label}_{ts}.json'
        path = os.path.join(LOG_DIR, fname)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({
                'cmd': cmd, 'rc': rc,
                'stdout': stdout[-4000:] if isinstance(stdout, str) else '',
                'stderr': stderr[-2000:] if isinstance(stderr, str) else '',
                'parsed': parsed,
                'exception': exception,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 日志失败不影响主流程


def _barometer_user_picks_text(iso_date: str) -> str:
    """某日「当日低吸成功票」名单 (user_picks.name, 顿号拼接).

    added_at 前 10 位 = iso_date 的全部记录; 同 ts_code 多次添加只取一条 name;
    按 MAX(added_at) DESC + ts_code ASC 稳定排序.
    整行 push 和单格同步共用这个函数, 保证两边口径不会漂.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return '、'.join(
            r['name'] for r in conn.execute(
                "SELECT b.name FROM user_picks p "
                "LEFT JOIN stock_basic b ON b.ts_code = p.ts_code "
                "WHERE substr(p.added_at, 1, 10) = ? "
                "GROUP BY p.ts_code "
                "ORDER BY MAX(p.added_at) DESC, p.ts_code ASC",
                (iso_date,)
            ) if r['name']
        ) or ''
    finally:
        conn.close()


def push_barometer_to_feishu(date: str) -> dict:
    """把本地最新一行盘面晴雨表 push 到飞书 spreadsheet (A2 插入).

    流程: 幂等检查 → 插入行 → 清格式 → 写 cells → 扩 6 条条件格式 (best-effort).
    列对应走 barometer_history (口径跟手机端一致: super_money 是超大单, chinext_index 是创业板指).
    """
    # 1) 拉指定日期那一行 (date 是 YYYYMMDD 8 位)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        try:
            from mobile_data import SavedData  # 跟手机端共享同一个 SQLite
        except ImportError as e:
            return {'status': 'error', 'step': 'import_mobile_data', 'message': str(e)}
        sd = SavedData(conn)
        rows = sd.barometer_history(date=date)
    finally:
        conn.close()
    if not rows:
        return {'status': 'no_data', 'date': date, 'message': f'{date[:4]}-{date[4:6]}-{date[6:8]} 本地无晴雨表数据'}
    row = rows[0]
    iso_date = f"{row['date'][:4]}-{row['date'][4:6]}-{row['date'][6:8]}"

    # 1b) 拉当日低吸成功票: 口径见 _barometer_user_picks_text
    user_picks_str = _barometer_user_picks_text(iso_date)

    # 2) 拼 32 列 cells (idx 0..31). 百分比列 idx [3,5,7,14,18,22] 加 number_format
    pct_indices = {3, 5, 7, 14, 18, 22}
    def _d2(v):
        """数值 round 2 位; None 透传."""
        return None if v is None else round(float(v), 2)
    def _i(v):
        return None if v is None else int(v)
    iso_d = iso_date  # YYYY-MM-DD 字符串
    raw = [
        iso_d,                                       # 0  日期 (YYYY-MM-DD)
        _d2(row['turnover']),                        # 1  成交额/亿
        _d2(row['shanghai_index']),                  # 2  上证
        _d2(row['change_rate']),                     # 3  上证涨跌幅% (percent)
        _d2(row['kechuang50_index']),                # 4  科创50
        _d2(row['kechuang50_rate']),                 # 5  科创50涨跌幅% (percent)
        _d2(row['chinext_index']),                   # 6  创业板指
        _d2(row['chinext_rate']),                    # 7  创业板涨跌% (percent)
        _d2(row['main_money']),                      # 8  主力净流入/亿
        _d2(row['super_money']),                     # 9  超大单/亿
        _d2(row['small_money']),                     # 10 小单/亿
        _i(row['advancers']),                        # 11 上涨家数 (int)
        _i(row['decliners']),                        # 12 下跌家数 (int)
        row.get('today_top1_concept'),               # 13 TOP1 板块
        _d2(row.get('today_top1_concept_rate')),     # 14 TOP1 板块涨幅% (percent)
        _d2(row.get('today_top1_concept_money')),    # 15 TOP1 板块主力/亿
        row.get('today_top1_concept_stock'),         # 16 TOP1 领涨股
        row.get('today_top2_concept'),               # 17 TOP2 板块
        _d2(row.get('today_top2_concept_rate')),     # 18 TOP2 板块涨幅% (percent)
        _d2(row.get('today_top2_concept_money')),    # 19 TOP2 板块主力/亿
        row.get('today_top2_concept_stock'),         # 20 TOP2 领涨股
        row.get('today_top3_concept'),               # 21 TOP3 板块
        _d2(row.get('today_top3_concept_rate')),     # 22 TOP3 板块涨幅% (percent)
        _d2(row.get('today_top3_concept_money')),    # 23 TOP3 板块主力/亿
        row.get('today_top3_concept_stock'),         # 24 TOP3 领涨股
        row.get('hot5_stock'),                       # 25 5日榜个股
        row.get('hot5_sector'),                      # 26 5日榜板块
        row.get('hot10_stock'),                      # 27 10日榜个股
        row.get('hot10_sector'),                     # 28 10日榜板块
        row.get('hot20_stock'),                      # 29 20日榜个股
        row.get('hot20_sector'),                     # 30 20日榜板块
        user_picks_str,                              # 31 当日低吸成功票 (user_picks.name 全量, 顿号拼接)
    ]
    # 数值列做 None→"" 的兼容; 整型列 (L/M) 也用 None→""
    cells = []
    for i, v in enumerate(raw):
        # 字号 10 = 跟飞书表存量行一致 (表里所有行都是默认 10, 写 13 会让新行跟老行一眼看出不一样)
        style = {'font_size': 10, 'vertical_alignment': 'middle'}
        if i in pct_indices:
            style['number_format'] = '0.00"%"'
        cells.append({'value': (v if v is not None else ''), 'cell_styles': style})
    cells_payload = [cells]

    token = FEISHU_BAROMETER_SPREADSHEET_TOKEN

    # 3) 幂等检查 — 读 A2 日期对比
    ok, parsed, _, stderr = _lark_sheets_call(
        ['sheets', '+cells-get', '--spreadsheet-token', token, '--range', 'A2:A2', '--as', 'user'],
        op_label='feishu_push_idempotency',
    )
    if not ok:
        return {'status': 'error', 'step': 'cells-get', 'message': '读取飞书 A2 失败', 'stderr': stderr}
    try:
        a2_date = parsed['data']['ranges'][0]['cells'][0][0].get('value', '') if parsed else ''
        # lark 返回的 value 可能是 "2026-09-21" 字符串或数字
        a2_date_str = a2_date if isinstance(a2_date, str) else ''
    except (KeyError, IndexError, TypeError):
        a2_date_str = ''
    if a2_date_str == iso_date:
        return {'status': 'no_change', 'date': iso_date, 'message': '已是最新'}

    # 4) 插入行 (A2 前 1 行, 把原 A2..A492 下移到 A3..A493)
    ok, parsed, _, stderr = _lark_sheets_call(
        ['sheets', '+dim-insert', '--spreadsheet-token', token,
         '--position', '2', '--count', '1', '--inherit-style', 'before', '--as', 'user'],
        op_label='feishu_push_dim-insert',
    )
    if not ok:
        return {'status': 'error', 'step': 'dim-insert', 'message': '插入行失败', 'stderr': stderr}

    # 5) 清 A2:AF2 的格式 (继承自 A1 header, 不清值)
    ok, parsed, _, stderr = _lark_sheets_call(
        ['sheets', '+cells-clear', '--spreadsheet-token', token,
         '--range', 'A2:AF2', '--scope', 'formats', '--yes', '--as', 'user'],
        op_label='feishu_push_cells-clear',
    )
    if not ok:
        return {'status': 'error', 'step': 'cells-clear', 'message': '清格式失败', 'stderr': stderr}

    # 6) 写 cells (从 stdin 喂 JSON)
    ok, parsed, _, stderr = _lark_sheets_call(
        ['sheets', '+cells-set', '--spreadsheet-token', token,
         '--range', 'A2:AF2', '--cells', '-', '--as', 'user'],
        input_json=json.dumps(cells_payload, ensure_ascii=False),
        op_label='feishu_push_cells-set',
    )
    if not ok:
        return {'status': 'error', 'step': 'cells-set', 'message': '写 cells 失败', 'stderr': stderr}

    # 7) 扩 6 条条件格式 (best-effort: 失败仅计数, 不影响主流程)
    extended = _extend_cond_formats(token)
    return {
        'status': 'success',
        'date': iso_date,
        'wrote_range': 'A2:AF2',
        'cond_formats_extended': extended,
    }


# 「当日低吸成功票」= push payload 的第 32 列 (idx 31), 表头 A1 起算 → AF 列
BAROMETER_USER_PICKS_COL = 'AF'


def _feishu_find_date_row(token: str, iso_date: str):
    """在飞书表 A 列找 iso_date 所在行号; 找不到返回 (None, debug).

    表结构: row1 = 表头, row2 = 最新一天, 往下按时间倒序.

    注意: +cells-get 返回的 cells 是**行主序** — cells[i] 就是第 i 行 (单列 range 下每行
    只有 1 个元素), 所以真实行号取 ranges[0].row_indices[i]; 之前误按列主序 (cells[0] 当整列)
    解, 结果只扫到 1 个格子.
    """
    ok, parsed, stdout, stderr = _lark_sheets_call(
        ['sheets', '+cells-get', '--spreadsheet-token', token, '--range', 'A1:A500', '--as', 'user'],
        op_label='feishu_find_date_row',
    )
    if not ok or not parsed:
        return None, {'ok': ok, 'why': 'lark 调用失败或未返回 JSON', 'stderr': (stderr or '')[:400]}
    try:
        rng = parsed['data']['ranges'][0]
    except (KeyError, IndexError, TypeError) as e:
        return None, {'ok': True, 'why': f'取 data.ranges[0] 失败: {type(e).__name__}',
                      'parsed_head': json.dumps(parsed, ensure_ascii=False)[:600]}
    if not isinstance(rng, dict) or 'cells' not in rng:
        return None, {'ok': True, 'why': 'range 里没有 cells', 'range_keys': list(rng.keys()) if isinstance(rng, dict) else None,
                      'parsed_head': json.dumps(parsed, ensure_ascii=False)[:600]}
    cells = rng.get('cells')
    row_indices = rng.get('row_indices') or []
    debug = {'ok': True, 'cells_outer_len': len(cells) if isinstance(cells, list) else None,
             'row_indices_len': len(row_indices), 'sample': json.dumps(cells, ensure_ascii=False)[:300]}
    if not isinstance(cells, list) or not cells:
        return None, debug
    for i, row_cells in enumerate(cells):          # 行主序: cells[i] = 第 i 行
        if not isinstance(row_cells, list) or not row_cells:
            continue
        cell = row_cells[0]                        # 单列 range, 只看该行唯一那个格子
        if not isinstance(cell, dict):
            continue
        value = cell.get('value', '')
        if isinstance(value, str) and value.strip() == iso_date:
            # row_indices 缺失时退回"数组下标 +1"(单列 + 起始行为 row1 时才成立)
            return (row_indices[i] if i < len(row_indices) else i + 1), debug
    return None, dict(debug, scanned=len(cells), why='列内没匹配到该日期')


def sync_user_picks_to_feishu(date: str) -> dict:
    """把某日 user_picks 名单刷进盘面晴雨表「当日低吸成功票」列 (加自选按钮专用).

    行为 (和用户确认过):
      - 飞书表里**已有**该日期行 → 原地只改那一行的 AF 单格, 不碰其他 31 列, 不插行.
        这是必须的: push_barometer_to_feishu() 的幂等检查 (读 A2 日期比对) 在当天行
        已存在时会直接返回 no_change 什么都不写, 所以行内追加只能自己改单格.
      - **没有**该日期行 → 退回复用 push_barometer_to_feishu() 补一整行.
    """
    date = (date or '').strip().replace('-', '')
    if not date:
        date = datetime.now().strftime('%Y%m%d')
    if not re.fullmatch(r'\d{8}', date):
        return {'status': 'error', 'message': f'日期格式错误: {date!r} (需 YYYYMMDD)'}
    iso_date = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
    token = FEISHU_BAROMETER_SPREADSHEET_TOKEN
    text = _barometer_user_picks_text(iso_date)

    row, find_debug = _feishu_find_date_row(token, iso_date)
    if row is None:
        pushed = push_barometer_to_feishu(date)
        return {
            'status': 'fell_back_to_full_push', 'date': iso_date, 'user_picks': text,
            'message': f'飞书表无 {iso_date} 行, 已整行补齐 (原 push: {pushed.get("status")})',
            'push': pushed, 'find_debug': find_debug,
        }

    rng = f'{BAROMETER_USER_PICKS_COL}{row}'
    payload = [{'value': text, 'cell_styles': {'font_size': 10, 'vertical_alignment': 'middle'}}]
    # 写前复核: 单格读 A{row}, 日期必须仍是 iso_date.
    # row_indices 语义万一跟预期不符, 这一步会挡住, 不会把名单写到别的日期那一行.
    ok_v, parsed_v, _, _ = _lark_sheets_call(
        ['sheets', '+cells-get', '--spreadsheet-token', token, '--range', f'A{row}:A{row}', '--as', 'user'],
        op_label='feishu_sync_verify_row',
    )
    verified = False
    if ok_v and parsed_v:
        try:
            v = parsed_v['data']['ranges'][0]['cells'][0][0].get('value', '')
            verified = (isinstance(v, str) and v.strip() == iso_date)
        except (KeyError, IndexError, TypeError):
            verified = False
    if not verified:
        return {'status': 'error', 'step': 'verify_row', 'date': iso_date, 'row': row,
                'message': f'第 {row} 行日期复核不通过, 已中止写入 (避免写到错误的日期行)'}

    ok, _, _, stderr = _lark_sheets_call(
        ['sheets', '+cells-set', '--spreadsheet-token', token,
         '--range', rng, '--cells', '-', '--as', 'user'],
        input_json=json.dumps(payload, ensure_ascii=False),
        op_label='feishu_sync_user_picks',
    )
    if not ok:
        return {'status': 'error', 'step': 'cells-set', 'date': iso_date, 'row': row,
                'wrote_range': rng, 'user_picks': text,
                'message': '写单元格失败', 'stderr': stderr}

    # 写后回读: ok=true 只代表请求被接受, 不代表值真的落上了. 必须读回来比对.
    ok_r, parsed_r, _, _ = _lark_sheets_call(
        ['sheets', '+cells-get', '--spreadsheet-token', token, '--range', rng, '--as', 'user'],
        op_label='feishu_sync_readback',
    )
    got = None
    if ok_r and parsed_r:
        try:
            got = parsed_r['data']['ranges'][0]['cells'][0][0].get('value', '')
        except (KeyError, IndexError, TypeError):
            got = None
    if (got or '') != text:
        return {'status': 'error', 'step': 'readback', 'date': iso_date, 'row': row,
                'wrote_range': rng, 'user_picks': text, 'readback': got,
                'message': '写入后回读值与预期不一致, 请人工核对'}

    return {'status': 'success', 'date': iso_date, 'row': row,
            'wrote_range': rng, 'user_picks': text, 'readback': got,
            'message': f'{iso_date} 第 {row} 行「当日低吸成功票」已更新并回读校验通过'}


def _extend_cond_formats(token: str) -> int:
    """lark-cli 的 +dim-insert 操作会把 cond-format 的 ranges 起始 +1, 尾 +1 (实测).
    所以这边反向操作: 读 list 后, 把每个 range 的**起始 -1, 尾 -1**, 再 update;
    dim-insert 写完后 lark 自动 +1 +1, 净效果 = list 时啥样, push 后还是啥样.

    注意: 起始最小是 1 (header 行), 不会 < 1, 因为 dim-insert 在 row 2,起始 < 2 都会被 +1.
    """
    import re as _re
    ok, parsed, _, _ = _lark_sheets_call(
        ['sheets', '+cond-format-list', '--spreadsheet-token', token, '--as', 'user'],
        op_label='feishu_push_cond-format-list',
    )
    if not ok or not parsed:
        return 0
    rules = parsed.get('data', {}).get('sheets', [{}])[0].get('conditional_formats', [])
    extended = 0
    for r in rules:
        rid = r.get('conditional_format_id')
        d = r.get('details', {})
        old_ranges = d.get('ranges', [])
        new_ranges = []
        for rg in old_ranges:
            m = _re.match(r'^([A-Z]+)(\d+):([A-Z]+)(\d+)$', rg)
            if m:
                c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
                new_ranges.append(f'{c1}{max(1, r1 - 1)}:{c2}{max(r1, r2 - 1)}')  # 起始≥1, 尾 ≥ 起始
            else:
                new_ranges.append(rg)
        if new_ranges == old_ranges:
            continue
        rule_type = d.get('rule_type', 'cellIs')
        attrs = d.get('attrs', [])
        style = d.get('style', {})
        props = {'attrs': attrs, 'style': style}
        ok2, _, _, stderr2 = _lark_sheets_call(
            ['sheets', '+cond-format-update', '--spreadsheet-token', token,
             '--rule-id', rid, '--rule-type', rule_type,
             '--ranges', json.dumps(new_ranges),
             '--properties', json.dumps(props, ensure_ascii=False),
             '--as', 'user'],
            op_label=f'feishu_push_cond-format-update_{rid}',
        )
        if ok2:
            extended += 1
    return extended


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


@app.route('/api/stock/daily', methods=['GET'])
def get_stock_daily():
    """个股日线 (K线弹窗数据源): OHLC + 涨跌幅 + 成交量, 日期升序返回.

    Args:
        ts_code: 如 300489.SZ (必传)
        limit: 最近 N 个交易日, 默认 250 (约一年), 上限 800
    """
    ts_code = (request.args.get('ts_code') or '').strip()
    limit = request.args.get('limit', 250, type=int)
    limit = max(1, min(limit or 250, 800))
    if not ts_code:
        return jsonify({'status': 'success', 'ts_code': '', 'name': '', 'count': 0, 'rows': []})
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT trade_date, open, high, low, close, change, volume, amount, name '
        'FROM stock_daily WHERE ts_code = ? ORDER BY trade_date DESC LIMIT ?',
        (ts_code, limit)
    ).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]
    rows.reverse()  # 升序: 图表从左到右
    name = rows[-1]['name'] if rows else ''
    # 盘中补一根今日K (sina 实时价): 交易日 + stock_daily 还没今天 + 已开盘 (今开>0)
    # 盘后日线入库后自动回到 TuShare 口径 (rows 里已有今天就不再补)
    intraday = False
    if request.args.get('intraday') in ('1', 'true', 'True'):
        from datetime import datetime as _dt
        _now = _dt.now()
        today = _now.strftime('%Y%m%d')
        if _now.weekday() < 5 and (not rows or rows[-1]['trade_date'] < today):
            try:
                q = fetch_sina_quotes([ts_code]).get(ts_code)
                if q and q.get('open') and q.get('price'):
                    # 新浪返回的行情自带交易日 (fields[30], 形如 '2026-09-30').
                    # weekday()<5 只挡得住周末, 挡不住国庆/春节等法定休市日: 休市期间
                    # 新浪持续返回**上一交易日**的快照, 直接当成"今天"补 K 就会画出
                    # 一根 10-01 的幽灵蜡烛, 而 OHLC 其实是 9/30 的.
                    # 所以这里只认"行情自带日期 == 今天", 节假日自动不补.
                    quote_day = str(q.get('time') or '').strip()[:10].replace('-', '')
                    if quote_day and quote_day != today:
                        print(f'[stock/daily] {ts_code} 跳过盘中补K: 行情日期 {quote_day} != 今天 {today} (休市/未开盘)',
                              flush=True)
                    elif not quote_day:
                        # 拿不到行情日期就不补, 宁可少一根盘中 K, 也不画假数据
                        print(f'[stock/daily] {ts_code} 跳过盘中补K: 行情无日期字段', flush=True)
                    else:
                        rows.append({
                            'trade_date': today,
                            'open': q['open'],
                            'high': q.get('high') or q['price'],
                            'low': q.get('low') or q['price'],
                            'close': q['price'],
                            'change': q.get('change_pct'),
                            'volume': q.get('volume'),
                            'amount': None,
                            'name': name or q.get('name') or '',
                        })
                        intraday = True
                        if not name and q.get('name'):
                            name = q['name']
            except Exception as _e:
                print(f'[stock/daily] {ts_code} 盘中K线补齐失败: {_e}', flush=True)
    return jsonify({
        'status': 'success', 'ts_code': ts_code, 'name': name or '',
        'count': len(rows), 'rows': rows, 'intraday': intraday,
    })


INDEX_CHART_CODES = {
    '000001.SH': '上证指数',
    '399001.SZ': '深证成指',
    '399006.SZ': '创业板指',
    '000688.SH': '科创50',
}
_INDEX_MINLINE_CACHE = {}  # (完整指数代码, 请求日期) -> (抓取时间, 已核验结果)
_MINLINE_CACHE = {}  # (ts_code, trade_date) -> {'_ts', 'rows', 'prev_close', 'name'} (分时数据 5s 缓存)
_MINLINE_CACHE_TTL = 5
_MINLINE_CACHE_MAX_AGE = 30  # 写入新条目时清掉 > 30s 的, 防 (ts_code,trade_date) 组合爆


def _is_intraday_now():
    """本机时间是否在 A 股盘中交易时段 (交易日 09:25-15:01 闭区间, 含午休).
    用于「指定 date == 今天」路径命中本地时, 盘中强制走 lazy 重拉换实时性.

    必须查交易日历: 只判 weekday() 的话, 国庆/春节这些法定休市日会被当成交易日,
    休市期间 sina 一直返回上一交易日的分时, 贴上今天的日期就是错的."""
    from datetime import datetime as _dt
    now = _dt.now()
    if now.weekday() >= 5:
        return False
    if not _today_is_trading_day():
        return False
    return '092500' <= now.strftime('%H%M%S') <= '150100'


@app.route('/api/index/kline', methods=['GET'])
def get_index_kline():
    """全部已入库指数日线；volume 的历史存储口径是成交额（亿元）。"""
    code = (request.args.get('ts_code') or '').strip()
    if code not in INDEX_CHART_CODES:
        return jsonify({'status': 'error', 'message': '不支持的指数代码'}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            'SELECT date AS trade_date, open, high, low, close, change, volume '
            'FROM index_daily WHERE name=? ORDER BY date ASC',
            (INDEX_CHART_CODES[code],)
        ).fetchall()]
    finally:
        conn.close()
    missing = sum(any(r[k] is None for k in ('open', 'high', 'low', 'close')) for r in rows)
    return jsonify({
        'status': 'success', 'ts_code': code, 'name': INDEX_CHART_CODES[code],
        'rows': rows, 'count': len(rows), 'intraday': False,
        'start_date': rows[0]['trade_date'] if rows else None,
        'end_date': rows[-1]['trade_date'] if rows else None,
        'volume_unit': '亿元', 'volume_label': '成交额', 'missing_ohlc': missing,
        'message': f'{missing} 个交易日缺少完整开高低收，未绘制对应蜡烛' if missing else '',
    })


def _fetch_sina_minline_rows(sina_code):
    """共享新浪分钟序列解析；返回分钟量（手），不推断交易日期。"""
    url = ('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_ml='
           f'/CN_MinlineService.getMinlineData?symbol={sina_code}')
    response = _requests.get(url, headers={
        'Referer': 'https://finance.sina.com.cn',
        'User-Agent': 'Mozilla/5.0',
    }, timeout=5)
    response.raise_for_status()
    text = response.text
    raw = json.loads(text[text.index('(') + 1:text.rindex(')')])
    if not isinstance(raw, list):
        raise ValueError('分时数据格式错误')
    rows = [{
        'm': p.get('m', ''),
        'p': float(p['p']) if p.get('p') not in (None, '') else None,
        'avg_p': float(p['avg_p']) if p.get('avg_p') not in (None, '') else None,
        'v': round(float(p.get('v') or 0) / 100, 0),
    } for p in raw]
    return [r for r in rows if r['p'] is not None and '09:25:00' <= r['m'] <= '15:00:59']


def _fetch_index_chart_quote(code):
    """读取指数行情的真实日期与时刻，不使用本机日期为分钟数据贴标签。"""
    sina_code = _sina_code_for_ts(code)
    response = _requests.get(f'https://hq.sinajs.cn/list={sina_code}', headers={
        'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0',
    }, timeout=5)
    response.raise_for_status()
    match = re.search(r'var\s+hq_str_' + re.escape(sina_code) + r'="([^"]*)"', response.text)
    if not match:
        raise ValueError('无法核验指数行情日期')
    fields = match.group(1).split(',')
    if len(fields) < 32:
        raise ValueError('指数行情缺少日期')
    quote_date = datetime.strptime(fields[30], '%Y-%m-%d').strftime('%Y%m%d')
    quote_time = datetime.strptime(fields[31], '%H:%M:%S').strftime('%H:%M:%S')
    return {'date': quote_date, 'time': quote_time,
            'open': float(fields[1]), 'prev_close': float(fields[2])}


@app.route('/api/index/minline', methods=['GET'])
def get_index_minline():
    """指数分时. 数据源优先查本地 index_minline_bars 表, 没命中再 lazy 抓 sina.

    参数:
      ts_code: 必股, e.g. 000001.SH (必须在 INDEX_CHART_CODES 里)
      date:    可选 YYYYMMDD. 指定时严格本地查询, 命中返回 source=local, 未命中返回 source=none
              (不回 sina, 避免把"最近交易日"凑成"你选的那天"误导).
              不传时: 走 lazy — 现抓 sina + quote 跨日核验 + INSERT OR REPLACE 入库.

    返回字段:
      source:    'local' | 'live' | 'none'
      trade_date: 命中/推断值; source=none 时 = requested_date
      count / rows / prev_close / message
    """
    code = (request.args.get('ts_code') or '').strip()
    if code not in INDEX_CHART_CODES:
        return jsonify({'status': 'error', 'message': '不支持的指数代码'}), 400
    requested_date = (request.args.get('date') or '').strip()
    if requested_date:
        # 指定了 date 就严格校验, 非法直接 400; 空 date 才走 lazy 路径
        if len(requested_date) != 8 or not requested_date.isdigit():
            return jsonify({'status': 'error', 'message': '日期须为有效的 YYYYMMDD'}), 400
        try:
            datetime.strptime(requested_date, '%Y%m%d')
        except ValueError:
            return jsonify({'status': 'error', 'message': '日期须为有效的 YYYYMMDD'}), 400

    payload = {
        'status': 'success', 'ts_code': code, 'name': INDEX_CHART_CODES[code],
        'requested_date': requested_date or None,
        'trade_date': None, 'source': 'none', 'count': 0, 'rows': [],
        'prev_close': None, 'volume_unit': '手', 'message': '', 'reason': None,
    }

    if requested_date:
        today_str = datetime.now().strftime('%Y%m%d')
        # 休市日请求"今天" → 重定向到最近交易日, 避免把上一交易日分时贴成今天的 (同 /api/stock/minline)
        if requested_date == today_str and not _today_is_trading_day():
            actual = last_trading_date(today_str)
            print(f'[index-minline] {code} 今天 {today_str} 非交易日, {requested_date} → {actual}', flush=True)
            requested_date = actual
            payload['requested_date'] = actual
        rows, prev_close = _read_index_minline_from_db(code, requested_date)
        # 今天盘中: 强制 lazy 重拉换实时性, 失败 fallback 到本地
        if requested_date == today_str and _is_intraday_now():
            live = _fetch_index_minline_live(code, requested_date)
            if live is not None:
                return jsonify(live)
        # 命中本地: 盘后 / 历史 直接返 (盘中 lazy 失败时也用本地兜底)
        if rows is not None:
            payload.update(source='local', rows=rows, count=len(rows),
                           trade_date=requested_date, prev_close=prev_close,
                           start_time=rows[0]['m'] if rows else None,
                           end_time=rows[-1]['m'] if rows else None)
            _INDEX_MINLINE_CACHE[(code, requested_date)] = (time.time(), payload)
            return jsonify(payload)
        # 未命中本地: 今天回落 lazy, 历史严格不回退
        if requested_date == today_str:
            live = _fetch_index_minline_live(code, requested_date)
            if live is not None:
                return jsonify(live)
            return jsonify({**payload, 'trade_date': requested_date,
                            'message': payload.get('message') or '盘中分时拉取失败'})
        payload['message'] = f'本地无 {requested_date} 指数分时数据;新浪不支持回填任意历史日期'
        return jsonify(payload)

    # 不传 date → lazy: 推断 trade_date, 抓 sina + quote 核验, 落库
    inferred = _infer_minline_trade_date()
    live = _fetch_index_minline_live(code, inferred)
    if live is not None:
        return jsonify(live)
    # lazy 失败: payload 已被 helper 写入兜底 message
    return jsonify({**payload, 'trade_date': inferred,
                    'message': payload.get('message') or '盘中分时拉取失败'})


def _fetch_index_minline_live(code, inferred):
    """指数分时 lazy 抓 sina + 核验 + 落库; 命中返 payload, 失败返 None (payload 写 message).
    被 /api/index/minline 的「不传 date」和「指定 date == 今天」两条入口共用."""
    key = (code, inferred)
    cached = _INDEX_MINLINE_CACHE.get(key)
    if cached and time.time() - cached[0] < 10:
        return dict(cached[1])

    payload = {
        'status': 'success', 'ts_code': code, 'name': INDEX_CHART_CODES[code],
        'requested_date': inferred,
        'trade_date': None, 'source': 'none', 'count': 0, 'rows': [],
        'prev_close': None, 'volume_unit': '手', 'message': '', 'reason': None,
    }
    try:
        before = _fetch_index_chart_quote(code)
    except Exception as e:
        payload['message'] = f'sina 行情核验失败: {e}'
        return None

    # quote 日期与推断日期不一致: 说明今日尚未开盘/已跨日, 严格不上盘
    if before['date'] != inferred or before['open'] <= 0 or before['time'] < '09:30:00':
        payload['message'] = '该交易日尚无可核验的分时数据，可切换日 K'
        return None

    try:
        rows = _fetch_sina_minline_rows(_sina_code_for_ts(code))
        after = _fetch_index_chart_quote(code)
    except Exception as e:
        payload['message'] = f'sina 分时拉取失败: {e}'
        return None

    # 防止跨日、盘前旧序列混入
    if after['date'] != inferred or not rows or rows[-1]['m'] > after['time']:
        payload['message'] = '无法确认分时数据属于该日期，可切换日 K'
        return None

    # 落库 (幂等 INSERT OR REPLACE)
    try:
        _persist_index_minline_bars(code, inferred, rows, after['prev_close'])
    except Exception as e:
        print(f'[idx_minline] {code} 落库失败: {e}', flush=True)

    payload.update(source='live', rows=rows, count=len(rows),
                   trade_date=inferred, prev_close=after['prev_close'],
                   start_time=rows[0]['m'], end_time=rows[-1]['m'])
    _INDEX_MINLINE_CACHE[key] = (time.time(), payload)
    return payload


@app.route('/api/stock/minline', methods=['GET'])
def get_stock_minline():
    """个股分时. 数据源优先查本地 stock_minline_bars 表, 没命中再 lazy 抓 sina.

    参数:
      ts_code: 必股, e.g. 000001.SZ
      date:    可选 YYYYMMDD. 指定时严格本地查询, 命中返回 source=local, 未命中返回 source=none
              (不回 sina, 避免把"最近交易日"凑成"你选的那天"误导).
              不传时: 走 lazy — 现抓 sina + 推断 trade_date + INSERT OR REPLACE 入库.

    返回字段:
      source:    'local' | 'live' | 'none'
      trade_date: 命中时为本地表/推断的值; source=none 时为请求的 date (让前端展示"无该日")
      count / rows / prev_close / name / message
    """
    ts_code = (request.args.get('ts_code') or '').strip()
    if not ts_code:
        return jsonify({'status': 'success', 'ts_code': '', 'count': 0, 'rows': [],
                        'source': 'none', 'message': ''})

    requested_date = (request.args.get('date') or '').strip()
    # 简单校验 date 形如 YYYYMMDD; 不合法按"未指定"处理
    if requested_date and (len(requested_date) != 8 or not requested_date.isdigit()):
        requested_date = ''

    now_ts = time.time()

    if requested_date:
        today_str = datetime.now().strftime('%Y%m%d')
        # 休市日 (周末/法定节假日) 请求"今天": 直接拿它问 sina, 会把**上一交易日**的
        # 分时原样贴上今天的日期 (国庆期间 10/01 会显示一整天的 9/30 分时, 逐根一模一样).
        # 统一重定向到 <= 今天的最近交易日, 跟 _infer_minline_trade_date 同一口径.
        if requested_date == today_str and not _today_is_trading_day():
            actual = last_trading_date(today_str)
            print(f'[minline] {ts_code} 今天 {today_str} 非交易日, {requested_date} → {actual}', flush=True)
            requested_date = actual
        cache_key = (ts_code, requested_date)
        ent = _MINLINE_CACHE.get(cache_key)
        if ent and now_ts - ent['_ts'] < _MINLINE_CACHE_TTL:
            return jsonify(_build_minline_response(ts_code, requested_date, 'local',
                                                    ent['rows'], ent.get('prev_close'),
                                                    ent.get('name'), None))
        rows, prev_close, name = _read_minline_from_db(ts_code, requested_date)
        # 今天盘中: 强制 lazy 重拉换实时性, 失败 fallback 本地
        if requested_date == today_str and _is_intraday_now():
            live_resp = _fetch_stock_minline_live(ts_code, requested_date)
            if live_resp.get('source') == 'live':
                return jsonify(live_resp)
        # 命中本地: 盘后/历史直接返 (盘中 lazy 失败时也用本地兜底)
        if rows is not None:
            _MINLINE_CACHE[cache_key] = {'_ts': now_ts, 'rows': rows,
                                          'prev_close': prev_close, 'name': name}
            return jsonify(_build_minline_response(ts_code, requested_date, 'local',
                                                    rows, prev_close, name, None))
        # 未命中本地: 今天回落 lazy, 历史严格不回退
        if requested_date == today_str:
            live_resp = _fetch_stock_minline_live(ts_code, requested_date)
            if live_resp.get('source') == 'live':
                return jsonify(live_resp)
            return jsonify(_build_minline_response(ts_code, requested_date, 'none',
                                                    [], None, None,
                                                    live_resp.get('message') or '盘中分时拉取失败'))
        msg = f'本地无 {requested_date} 分时数据;新浪不支持回填任意历史日期'
        return jsonify(_build_minline_response(ts_code, requested_date, 'none',
                                                [], None, None, msg))

    # 不传 date → lazy: 推断 trade_date, 抓 sina, 落库
    inferred = _infer_minline_trade_date()
    return jsonify(_fetch_stock_minline_live(ts_code, inferred))


def _fetch_stock_minline_live(ts_code, inferred):
    """个股分时 lazy 抓 sina + 落库; 命中返 source=live payload, 失败返 source=none + message.
    被 /api/stock/minline 的「不传 date」和「指定 date == 今天」两条入口共用."""
    now_ts = time.time()
    cache_key = (ts_code, inferred)
    ent = _MINLINE_CACHE.get(cache_key)
    if ent and now_ts - ent['_ts'] < _MINLINE_CACHE_TTL:
        return _build_minline_response(ts_code, inferred, 'live',
                                        ent['rows'], ent.get('prev_close'),
                                        ent.get('name'), None)
    sc = _sina_code_for_ts(ts_code)
    if not sc:
        return _build_minline_response(ts_code, inferred, 'none',
                                        [], None, None, f'无法识别股票代码 {ts_code}')
    try:
        rows = _fetch_sina_minline_rows(sc)
    except Exception as e:
        print(f'[minline] {ts_code} 拉取失败: {e}', flush=True)
        return _build_minline_response(ts_code, inferred, 'none',
                                        [], None, None, f'sina 拉取失败: {e}')
    quote = fetch_sina_quotes([ts_code]).get(ts_code) or {}
    prev_close = quote.get('prev_close')
    name = quote.get('name') or ''
    # 落库 (幂等 INSERT OR REPLACE)
    if rows:
        try:
            _persist_minline_bars(ts_code, inferred, rows, name, prev_close)
        except Exception as e:
            print(f'[minline] {ts_code} 落库失败: {e}', flush=True)
    _MINLINE_CACHE[cache_key] = {'_ts': now_ts, 'rows': rows,
                                  'prev_close': prev_close, 'name': name}
    # 写新条目时清旧条目 (避免 (ts_code, trade_date) 组合无限增长)
    cutoff = now_ts - _MINLINE_CACHE_MAX_AGE
    for k in list(_MINLINE_CACHE.keys()):
        if _MINLINE_CACHE[k]['_ts'] < cutoff:
            _MINLINE_CACHE.pop(k, None)
    return _build_minline_response(ts_code, inferred, 'live',
                                    rows, prev_close, name, None)


def _build_minline_response(ts_code, trade_date, source, rows, prev_close, name, message):
    return {
        'status': 'success',
        'ts_code': ts_code,
        'trade_date': trade_date,
        'source': source,
        'message': message or '',
        'count': len(rows or []),
        'rows': rows or [],
        'prev_close': prev_close,
        'name': name or '',
    }


# ── 盘后补充分时数据 (替代 APScheduler): 17:00 后, 任意页面刷新都检查 ──
import threading as _threading

# kind -> {'running': bool, 'started_at': float, 'finished_at': float, 'summary': dict}
_MINLINE_DAILY_TRIGGER = {'stock': None, 'index': None}
_MINLINE_DAILY_THRESHOLD = {'stock': 5000, 'index': 4}  # 全市场 ~5625, 4 大指数


def _count_distinct_minline(table, trade_date):
    """查 (table, trade_date) 下的 DISTINCT ts_code 数."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        row = conn.execute(
            f'SELECT COUNT(DISTINCT ts_code) FROM {table} WHERE trade_date=?',
            (trade_date,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _trigger_minline_async(kind, trade_date):
    """后台线程跑 fetch_*_minline_daily.run(); 用 _MINLINE_DAILY_TRIGGER 锁防重入.
    kind: 'stock' | 'index'"""
    lock_entry = _MINLINE_DAILY_TRIGGER[kind]
    if lock_entry and lock_entry['running']:
        return False
    _MINLINE_DAILY_TRIGGER[kind] = {'running': True, 'started_at': time.time(), 'finished_at': None, 'summary': None}

    def _worker():
        try:
            if kind == 'stock':
                from fetch_stock_minline_daily import run as run_stock
                summary = run_stock(trade_date=trade_date, verbose=True)
            else:
                from fetch_index_minline_daily import run as run_idx
                summary = run_idx(trade_date=trade_date, verbose=True)
            print(f'[minline_daily_trigger] {kind} trade_date={trade_date} 完成: {summary}', flush=True)
            _MINLINE_DAILY_TRIGGER[kind]['summary'] = summary
        except Exception as e:
            print(f'[minline_daily_trigger] {kind} trade_date={trade_date} 异常: {e}', flush=True)
            import traceback; traceback.print_exc()
            _MINLINE_DAILY_TRIGGER[kind]['summary'] = {'error': str(e)}
        finally:
            _MINLINE_DAILY_TRIGGER[kind]['running'] = False
            _MINLINE_DAILY_TRIGGER[kind]['finished_at'] = time.time()

    t = _threading.Thread(target=_worker, name=f'minline_daily_{kind}', daemon=True)
    t.start()
    return True


@app.route('/api/minline/daily-status', methods=['GET'])
def api_minline_daily_status():
    """盘后 17:00 后, 检查个股 / 指数分时今日覆盖情况, 缺数据时 fire-and-forget 异步触发补抓.
    设计为 dashboard init 时调用 (前端不阻塞, 后台慢慢跑)."""
    now = datetime.now()
    hm = now.hour * 100 + now.minute

    # 17:00 之前不触发 (盘中弹窗 lazy 路径已经在处理)
    if hm < 1700:
        return jsonify({
            'status': 'not_yet', 'reason': '未到 17:00',
            'current_time': now.strftime('%H:%M'),
        })

    trade_date = _infer_minline_trade_date(now)
    # 非交易日 (周末/节假日) → _infer 返回上一交易日, 不应触发
    today_str = now.strftime('%Y%m%d')
    if trade_date != today_str or not _today_is_trading_day():
        return jsonify({
            'status': 'not_trading_day', 'date': trade_date,
        })

    stock_count = _count_distinct_minline('stock_minline_bars', trade_date)
    index_count = _count_distinct_minline('index_minline_bars', trade_date)

    stock_th = _MINLINE_DAILY_THRESHOLD['stock']
    index_th = _MINLINE_DAILY_THRESHOLD['index']

    result = {
        'date': trade_date,
        'stock': {'count': stock_count, 'threshold': stock_th, 'complete': stock_count >= stock_th},
        'index': {'count': index_count, 'threshold': index_th, 'complete': index_count >= index_th},
        'actions': [],
    }

    # 个股缺 → 触发
    if stock_count < stock_th:
        lock_entry = _MINLINE_DAILY_TRIGGER['stock']
        if lock_entry and lock_entry['running']:
            result['actions'].append({'kind': 'stock', 'action': 'running'})
        else:
            if _trigger_minline_async('stock', trade_date):
                result['actions'].append({'kind': 'stock', 'action': 'triggered'})

    # 指数缺 → 触发
    if index_count < index_th:
        lock_entry = _MINLINE_DAILY_TRIGGER['index']
        if lock_entry and lock_entry['running']:
            result['actions'].append({'kind': 'index', 'action': 'running'})
        else:
            if _trigger_minline_async('index', trade_date):
                result['actions'].append({'kind': 'index', 'action': 'triggered'})

    if not result['actions']:
        result['status'] = 'already_complete'
    else:
        result['status'] = 'ok'
    return jsonify(result)


def _read_minline_from_db(ts_code, trade_date):
    """从 stock_minline_bars 读取指定 (ts_code, trade_date) 的所有分钟行.
    返回 (rows, prev_close, name); rows=None 表示本地无数据."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        rows_iter = conn.execute(
            '''SELECT m, p, avg_p, v, name, prev_close FROM stock_minline_bars
               WHERE ts_code=? AND trade_date=? ORDER BY m''',
            (ts_code, trade_date))
        rows = [{'m': r[0], 'p': r[1], 'avg_p': r[2], 'v': r[3]} for r in rows_iter]
        if not rows:
            return None, None, None
        # name / prev_close 任一非空就取 (PK 命中行有冗余字段)
        first = conn.execute(
            'SELECT name, prev_close FROM stock_minline_bars WHERE ts_code=? AND trade_date=? LIMIT 1',
            (ts_code, trade_date)).fetchone()
        return rows, first[1] if first else None, first[0] if first else ''
    finally:
        conn.close()


def _persist_minline_bars(ts_code, trade_date, rows, name, prev_close):
    """INSERT OR REPLACE 一只票一天的全部分钟行."""
    if not rows:
        return
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        c = conn.cursor()
        payload = [(ts_code, trade_date, r.get('m'), r.get('p'), r.get('avg_p'),
                    r.get('v') or 0, name, prev_close) for r in rows]
        c.executemany(
            '''INSERT OR REPLACE INTO stock_minline_bars
               (ts_code, trade_date, m, p, avg_p, v, name, prev_close)
               VALUES (?,?,?,?,?,?,?,?)''',
            payload)
        conn.commit()
    finally:
        conn.close()


def _read_index_minline_from_db(ts_code, trade_date):
    """从 index_minline_bars 读取指定 (ts_code, trade_date) 的所有分钟行.
    返回 (rows, prev_close); rows=None 表示本地无数据."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        rows_iter = conn.execute(
            '''SELECT m, p, avg_p, v, prev_close FROM index_minline_bars
               WHERE ts_code=? AND trade_date=? ORDER BY m''',
            (ts_code, trade_date))
        rows = [{'m': r[0], 'p': r[1], 'avg_p': r[2], 'v': r[3]} for r in rows_iter]
        if not rows:
            return None, None
        first = conn.execute(
            'SELECT prev_close FROM index_minline_bars WHERE ts_code=? AND trade_date=? LIMIT 1',
            (ts_code, trade_date)).fetchone()
        return rows, first[0] if first else None
    finally:
        conn.close()


def _persist_index_minline_bars(ts_code, trade_date, rows, prev_close):
    """INSERT OR REPLACE 一只指数一天的全部分钟行."""
    if not rows:
        return
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        c = conn.cursor()
        payload = [(ts_code, trade_date, r.get('m'), r.get('p'), r.get('avg_p'),
                    r.get('v') or 0, prev_close) for r in rows]
        c.executemany(
            '''INSERT OR REPLACE INTO index_minline_bars
               (ts_code, trade_date, m, p, avg_p, v, prev_close)
               VALUES (?,?,?,?,?,?,?)''',
            payload)
        conn.commit()
    finally:
        conn.close()


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


def build_fund_flow_ranked(since_date=None):
    """重建 fund_flow_ranked 物化表.
    since_date=None → 全量 rebuild (8s 一次性).
    since_date='YYYYMMDD' → 仅刷该日及之后 (含 buffer 前 30 天, 防 ROW_NUMBER 错位)
    写入字段: ts_code/trade_date/source + rn (按 ts_code+source 排序) + stock_daily close/change/high + close_anchor.
    """
    conn = sqlite3.connect(DB_PATH, timeout=60)
    c = conn.cursor()
    if since_date is None:
        # 全量: 清空重写
        c.execute('DELETE FROM fund_flow_ranked')
        where_clause = ''
        params = []
    else:
        # 增量: 仅刷 since_date 及之前 30 天起的新数据 (避免窗口边界错位)
        c.execute('DELETE FROM fund_flow_ranked WHERE trade_date >= ?', (since_date,))
        # 前 30 天 buffer
        cutoff = (datetime.strptime(since_date, '%Y%m%d') - timedelta(days=30)).strftime('%Y%m%d')
        where_clause = 'AND f.trade_date >= ?'
        params = [cutoff]
    c.execute(f"""
        INSERT OR IGNORE INTO fund_flow_ranked
            (ts_code, trade_date, source, rn, code, name,
             close, change_pct, high,
             main_net_inflow, super_net, mid_net, small_net,
             close_anchor)
        SELECT
            f.ts_code, f.trade_date, f.source,
            ROW_NUMBER() OVER (PARTITION BY f.ts_code, f.source ORDER BY f.trade_date DESC) AS rn,
            f.code, f.name,
            sd.close, sd.change AS change_pct, sd.high,
            f.main_net_inflow, f.super_net, f.mid_net, f.small_net,
            FIRST_VALUE(sd.close) OVER (PARTITION BY f.ts_code, f.source ORDER BY f.trade_date DESC) AS close_anchor
        FROM fund_flow f
        LEFT JOIN stock_daily sd ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
        WHERE 1=1 {where_clause}
    """, params)
    inserted = c.rowcount
    conn.commit()
    conn.close()
    return inserted


def _init_default_push_config(conn):
    """默认推送配置 (feishu 读阈值用). 已存在的不覆盖."""
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
        '''SELECT o.*, m.last AS em_last FROM overview o
           LEFT JOIN market_fflow m ON m.trade_date = o.date
           WHERE upCount > 0 ORDER BY date DESC LIMIT ?''', (limit,)
    ).fetchall()

    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        em_last = d.pop('em_last', None)
        # 东财大盘主力/超大单净流入 (元 → 亿); 无数据为 None
        d['emMain'] = None
        d['emSuper'] = None
        if em_last:
            try:
                em = json.loads(em_last)
                d['emMain'] = em.get('main', 0) / 1e8
                d['emSuper'] = em.get('super', 0) / 1e8
            except (ValueError, AttributeError):
                pass
        result.append(d)
    return jsonify(result)


@app.route('/api/barometer/sync', methods=['POST'])
def api_barometer_sync():
    """手动触发某日盘面晴雨表同步; date 缺省 = 今天."""
    from datetime import datetime as _dt
    date = (request.args.get('date') or '').strip() or _dt.now().strftime('%Y%m%d')
    if not re.fullmatch(r'\d{8}', date):
        raise ValueError('日期格式错误')
    result = sync_market_barometer(date)
    return jsonify({'status': 'success', **result})


@app.route('/api/barometer', methods=['GET'])
def api_barometer_read():
    """读某日盘面晴雨表; date 缺省 → 最新一日."""
    date = (request.args.get('date') or '').strip()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if date:
        row = conn.execute('SELECT * FROM market_barometer WHERE date=?', (date,)).fetchone()
    else:
        row = conn.execute('SELECT * FROM market_barometer ORDER BY date DESC LIMIT 1').fetchone()
    conn.close()
    if not row:
        return jsonify({'available': False, 'date': date or None, 'message': '该日期尚未同步晴雨表'})
    return jsonify({'available': True, **dict(row)})


@app.route('/api/simple/market-overview', methods=['GET'])
def api_simple_market_overview():
    """简易版「市场总览」卡片数据: 成交额 / 资金净流入 / 涨跌家数 / 涨跌停 + 4 大指数走势.

    口径说明 (都跟 /api/overview 和 /api/index/daily 同源, 不另造数):
      - 成交额/较前日: overview.totalVolume (亿) / volumeDiff (亿)
      - 资金净流入: overview.netFlow (亿); 该表只有 134 天数据, 缺失时回退 market_fflow 的东财主力
      - 涨跌家数: upCount / downCount / flatCount, 上涨占比 = up/(up+down+flat)
      - 涨跌停: overview.limitUp / limitDown
      - 连板家数: limitup 表当日 streak>=2 的只数 (streak 存 'N天M板' 这类文本, 这里按前缀数字解析)
      - 炸板家数: limitup_collection 当日标记为炸板的只数; 该表无数据时返回 null (不编造)
      - 指数卡: index_daily 的 close/change + 近 20 日收盘做 sparkline

    query:
      date  YYYYMMDD, 缺省 = overview 最新有 upCount 的交易日
      spark 近多少日收盘 (默认 20, 上限 60)
    """
    date = (request.args.get('date') or '').strip()
    if date and not re.fullmatch(r'\d{8}', date):
        return jsonify({'error': '日期格式错误, 需 YYYYMMDD'}), 400
    spark_n = min(max(int(request.args.get('spark', 20) or 20), 5), 60)
    index_names = ['上证指数', '深证成指', '创业板指', '科创50']

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # 1) 基准日: overview 最新有 upCount 的交易日 (板块晴雨表也是这个口径, 两块对得上)
        if not date:
            row = conn.execute(
                'SELECT date FROM overview WHERE upCount > 0 ORDER BY date DESC LIMIT 1'
            ).fetchone()
            date = row['date'].replace('-', '') if row else ''
        if not date:
            return jsonify({'available': False, 'message': '暂无盘面概览数据'})
        iso = f'{date[:4]}-{date[4:6]}-{date[6:8]}'

        ov = conn.execute(
            'SELECT totalVolume, volumeDiff, netFlow, upCount, downCount, flatCount, '
            'limitUp, limitDown FROM overview WHERE REPLACE(date, \'-\', \'\') = ?', (date,)
        ).fetchone()

        # 2) 资金净流入: overview.netFlow 单位就是**亿** (表本身就是亿口径), 别再除 1e8.
        #    缺失时回退 market_fflow 的东财主力 (那个是**元**, 要除)
        net_flow = None
        if ov and ov['netFlow'] is not None:
            net_flow = ov['netFlow']
        else:
            mf = conn.execute('SELECT last FROM market_fflow WHERE trade_date = ?', (iso,)).fetchone()
            if mf and mf['last']:
                try:
                    net_flow = json.loads(mf['last']).get('main', 0) / 1e8
                except (ValueError, AttributeError):
                    pass

        # 3) 连板 / 炸板
        # streak 存的是 '4天3板' / '2天2板' 这种文本, 板数在末尾, 不是天数:
        #   连板 = 板数 >= 2 ('首板' / '3天1板' 都不计)
        # ⚠️ limitup.date 是 YYYYMMDD **无横线**, 传 date 别传 iso
        streak_rows = conn.execute(
            'SELECT streak FROM limitup WHERE date = ? AND streak IS NOT NULL AND streak != ""', (date,)
        ).fetchall()
        consecutive = 0
        for r in streak_rows:
            m = re.search(r'(\d+)\s*板', str(r['streak']))
            if m and int(m.group(1)) >= 2:
                consecutive += 1

        # 炸板: limitup_collection 的 broken 列.
        # ⚠️ 该表 trade_date 是 YYYYMMDD **无横线** (不是 iso), 且目前只到 20260929,
        #    缺当日数据时返回 null (不编造).
        broken = None
        try:
            row_b = conn.execute(
                'SELECT COUNT(*) AS n FROM limitup_collection WHERE trade_date = ? AND broken = 1', (date,)
            ).fetchone()
            row_s = conn.execute(
                'SELECT COUNT(*) AS n FROM limitup_collection WHERE trade_date = ? AND sealed = 1', (date,)
            ).fetchone()
            n_b = row_b['n'] if row_b else 0
            n_s = row_s['n'] if row_s else 0
            # 整表该日无记录 → 视为"没数据"而不是"0 炸板"
            if n_b or n_s:
                broken = n_b
        except sqlite3.Error:
            pass

        # 4) 4 大指数: 当日 close/change + **当日分时**走势 (index_minline_bars)
        #    分时比"近 N 日收盘"更符合卡片语义 (卡上就写着"查看分时/日K"),
        #    而且分时表自带 prev_close, 前端能画昨收基准线, 涨跌幅一眼可判.
        #    分时缺失时 (非交易日/没抓) 回退到近 spark_n 日收盘, 保证卡片不空.
        idx_code_map = {
            '上证指数': '000001.SH',
            '深证成指': '399001.SZ',
            '创业板指': '399006.SZ',
            '科创50': '000688.SH',
        }
        indices = []
        for name in index_names:
            cur = conn.execute(
                'SELECT close, change, volume AS amount FROM index_daily '
                'WHERE name = ? AND REPLACE(date,\'-\',\'\') = ? LIMIT 1',
                (name, date)
            ).fetchone()
            if not cur:
                indices.append({'name': name, 'close': None, 'change': None, 'amount': None,
                                'spark': [], 'times': [], 'prev_close': None})
                continue

            # 当日分时 (m 升序 = 时间正序)
            series = []
            prev_close = None
            code = idx_code_map.get(name)
            if code:
                bars = conn.execute(
                    'SELECT m, p, prev_close FROM index_minline_bars '
                    'WHERE ts_code = ? AND trade_date = ? ORDER BY m ASC', (code, date)
                ).fetchall()
                if bars:
                    series = [b['p'] for b in bars if b['p'] is not None]
                    prev_close = bars[0]['prev_close']
            mode = 'minute'
            if not series:   # 分时没有 → 回退日收盘
                hist = conn.execute(
                    'SELECT close FROM index_daily WHERE name = ? AND REPLACE(date,\'-\',\'\') <= ? '
                    'ORDER BY date DESC LIMIT ?', (name, date, spark_n)
                ).fetchall()
                series = [h['close'] for h in reversed(hist)]
                mode = 'daily'

            indices.append({
                'name': name,
                'close': cur['close'],
                'change': cur['change'],
                'amount': cur['amount'],                     # 亿 (index_daily.volume 存的就是亿)
                'spark': series,                             # 正序
                'mode': mode,                                # minute / daily, 前端据此标注
                'times': len(series) if mode == 'minute' else 0,
                'prev_close': prev_close,                    # 分时的昨收, 画基准线用
            })

        # 5) 昨日成交额 (算「较前日」用; overview.volumeDiff 已经是差值, 这里两个都给)
        prev = conn.execute(
            'SELECT totalVolume FROM overview WHERE REPLACE(date, \'-\', \'\') < ? AND upCount > 0 '
            'ORDER BY date DESC LIMIT 1', (date,)
        ).fetchone()
    finally:
        conn.close()

    up = ov['upCount'] if ov else None
    down = ov['downCount'] if ov else None
    flat = ov['flatCount'] if ov else None
    total = (up or 0) + (down or 0) + (flat or 0)
    return jsonify({
        'available': bool(ov),
        'date': iso,
        'turnover': ov['totalVolume'] if ov else None,          # 亿
        'turnover_diff': ov['volumeDiff'] if ov else None,      # 亿
        'prev_turnover': prev['totalVolume'] if prev else None,
        'net_flow': net_flow,                                   # 亿
        'up': up, 'down': down, 'flat': flat,
        'up_ratio': (up / total * 100) if (up is not None and total) else None,
        'limit_up': ov['limitUp'] if ov else None,
        'limit_down': ov['limitDown'] if ov else None,
        'consecutive': consecutive,
        'broken': broken,
        'indices': indices,
    })


# ── 简易版模式专用接口 ──
# 简易版只保留三块: 盘面晴雨表 / 个股搜索 / 选股推荐(EM 条件四选股).
# 桌面端原本没有晴雨表本体 (只有「同步飞书表格」按钮), 这里复用手机端
# SavedData.barometer_history 的同一套 SQL, 保证两端口径一致.

@app.route('/api/simple/dates', methods=['GET'])
def api_simple_dates():
    """简易版日期切换器可选的交易日列表 (倒序, YYYYMMDD).

    以 overview.upCount > 0 为准 —— 市场总览/晴雨表都走这个口径, 保证日期能选到就有数据.
    limit 默认 250 (约一年), 上限 500.
    """
    limit = min(max(int(request.args.get('limit', 250) or 250), 1), 500)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT REPLACE(date, '-', '') AS d FROM overview "
            "WHERE upCount > 0 ORDER BY d DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return jsonify({'dates': [r['d'] for r in rows]})


@app.route('/api/simple/overview-trend', methods=['GET'])
def api_simple_overview_trend():
    """简易版市场总览的四指标走势: 上证 / 科创50 / 大盘超大单 / 大盘小单 (日频).

    双 Y 轴: 两个指数走右轴 (点位), 两条资金流走左轴 (亿).
    资金流口径取 market_fflow.last 里**当日收盘那一刻**的累计值 (不是分时曲线的最后一点,
    两者一致但 last 更直接); 缺失该日就留 null, 前端断线不补 0.

    query:
      days  返回最近多少个交易日 (默认 60, 上限 250)
      end   截止日 YYYYMMDD (默认最新)
    """
    days = min(max(int(request.args.get('days', 60) or 60), 2), 250)
    end = (request.args.get('end') or '').strip()
    if end and not re.fullmatch(r'\d{8}', end):
        return jsonify({'error': '日期格式错误, 需 YYYYMMDD'}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # 指数: 上证 + 科创50, 与 /api/simple/market-overview 同一张 index_daily
        idx_rows = conn.execute(
            'SELECT name, date, close FROM index_daily '
            'WHERE name IN (\'上证指数\', \'科创50\') AND date <= ? '
            'ORDER BY date DESC', (end or '99999999',)
        ).fetchall()
        by_name = {'上证指数': [], '科创50': []}
        for r in idx_rows:
            by_name[r['name']].append((r['date'], r['close']))

        # 资金流: market_fflow.last → {super, small} (元 → 亿)
        flow_rows = conn.execute(
            'SELECT trade_date, last FROM market_fflow WHERE trade_date <= ? '
            'ORDER BY trade_date DESC', (end or '99999999',)
        ).fetchall()
        flows = []
        for r in flow_rows:
            try:
                d = json.loads(r['last']) if r['last'] else {}
            except (ValueError, AttributeError):
                continue
            flows.append((r['trade_date'], d.get('super'), d.get('small')))
    finally:
        conn.close()

    # 取最近 days 个交易日为时间轴 (以指数表为准, 资金流按日期对齐)
    sh = by_name['上证指数'][:days]
    kc = by_name['科创50'][:days]
    flow_map = {d: (sup, sml) for d, sup, sml in flows}

    if not sh and not kc:
        return jsonify({'count': 0, 'series': {}, 'message': '暂无指数数据'})

    # 时间轴: 以两条指数的日期并集为准 (倒序 → 反转成正序)
    dates = sorted({d for d, _ in sh} | {d for d, _ in kc})
    dates = dates[-days:]
    sh_map = dict(sh)
    kc_map = dict(kc)
    series = {
        'dates': dates,
        '上证指数': [sh_map.get(d) for d in dates],
        '科创50': [kc_map.get(d) for d in dates],
        '超大单': [((flow_map.get(d) or (None, None))[0] / 1e8) if flow_map.get(d) else None for d in dates],
        '小单': [((flow_map.get(d) or (None, None))[1] / 1e8) if flow_map.get(d) else None for d in dates],
    }
    return jsonify({
        'count': len(dates),
        'unit_index': '点',
        'unit_flow': '亿',
        'series': series,
    })


@app.route('/api/simple/overview-trend2', methods=['GET'])
def api_simple_overview_trend2():
    """简易版市场总览第二个图: 上涨家数 / 下跌家数 / 大盘成交额 / 大盘主力净流入 (日频).

    与 /api/simple/overview-trend 同一张 overview 表, 同一种口径, 只是换了四个指标.
    三组量纲差很多, 所以要三个 Y 轴 (前端配):
      上涨/下跌家数  右轴, 单位 家
      成交额         右轴, 单位 亿   (1.4~3.4 万亿量级)
      主力净流入     左轴, 单位 亿   (-1794~1200, 有正有负)
    成交额和净流入不能共用一根轴: 差 25 倍, 共用的话净流入会压成贴着 0 的直线.

    query: days (默认 60, 上限 250) / end (YYYYMMDD, 默认最新)
    """
    days = min(max(int(request.args.get('days', 60) or 60), 2), 250)
    end = (request.args.get('end') or '').strip()
    if end and not re.fullmatch(r'\d{8}', end):
        return jsonify({'error': '日期格式错误, 需 YYYYMMDD'}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            'SELECT date, totalVolume, netFlow, upCount, downCount FROM overview '
            'WHERE upCount > 0 AND date <= ? ORDER BY date DESC LIMIT ?',
            (end or '99999999', days)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return jsonify({'count': 0, 'series': {}, 'message': '暂无盘面概览数据'})

    rows = list(reversed(rows))          # 倒序 → 正序
    return jsonify({
        'count': len(rows),
        'unit_count': '家',
        'unit_amount': '亿',
        'series': {
            'dates': [r['date'] for r in rows],
            '上涨家数': [r['upCount'] for r in rows],
            '下跌家数': [r['downCount'] for r in rows],
            '成交额': [r['totalVolume'] for r in rows],
            '主力净流入': [r['netFlow'] for r in rows],
        },
    })


@app.route('/api/simple/barometer', methods=['GET'])
def api_simple_barometer():
    """简易版盘面晴雨表: 近 limit 个交易日, 列对齐飞书晴雨表 A~AF 32 列.

    复用 mobile_data.SavedData.barometer_history — 那条 SQL 才是晴雨表的权威口径
    (走 overview + index_daily + market_fflow + sector_fund_flow + leaderboard),
    不是 market_barometer 表 (那张表只有 3 行, 且大量字段为空).

    query:
      date  YYYYMMDD, 给了就只回那一天
      limit 默认 60, 上限 1000
    """
    date = (request.args.get('date') or '').strip()
    if date and not re.fullmatch(r'\d{8}', date):
        return jsonify({'error': '日期格式错误, 需 YYYYMMDD'}), 400
    limit = min(max(int(request.args.get('limit', 60) or 60), 1), 1000)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        try:
            from mobile_data import SavedData  # 与手机端共享同一份口径
        except ImportError as e:
            return jsonify({'error': f'加载 mobile_data 失败: {e}'}), 500
        sd = SavedData(conn)
        rows = sd.barometer_history(limit=limit, date=date or None)
    finally:
        conn.close()
    if not rows:
        return jsonify({'count': 0, 'rows': [], 'message': '暂无晴雨表数据'})
    return jsonify({'count': len(rows), 'rows': [dict(r) for r in rows]})


@app.route('/api/simple/stock', methods=['GET'])
def api_simple_stock():
    """简易版个股搜索: 主力净流入字段 + 3/5/10/20/30 日累计净流入.

    在 /api/flow/market (period=today) 的基础上, 额外给每只票算近 N 个交易日
    的主力净流入累计 (N ∈ {3,5,10,20,30}), 用 trading_dates_cache 取"近 N 个
    交易日"而不是"近 N 个日历日".

    口径说明: 累计窗口以**每只票自己的最新有数据日**为锚点往回取, 而不是全市场
    最新交易日 — 否则停牌/新股的累计会莫名其妙对齐到别人的日期.

    query:
      search  代码或名称, 模糊匹配 (可空 → 返回全市场 top N)
      market  all / sh / sz / cy / kcb
      date    基准交易日 YYYYMMDD (默认全市场最新有数据日)
      limit   默认 50, 上限 500
    """
    search = (request.args.get('search') or '').strip()
    market = (request.args.get('market') or 'all').strip().lower()
    date = (request.args.get('date') or '').strip()
    if date and not re.fullmatch(r'\d{8}', date):
        return jsonify({'error': '日期格式错误, 需 YYYYMMDD'}), 400
    limit = min(max(int(request.args.get('limit', 50) or 50), 1), 500)
    src = 'eastmoney-push2'   # 基准日兜底取最新, 6/16 之后自动落到它
    window = (3, 5, 10, 20, 30)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # 1) 基准日: 指定则用它, 否则取最新有数据的交易日
        if not date:
            row = conn.execute(
                'SELECT MAX(trade_date) AS d FROM fund_flow WHERE source IN (?, ?)', EM_FLOW_SOURCES
            ).fetchone()
            date = row['d'] or ''
        if not date:
            return jsonify({'count': 0, 'rows': [], 'message': '还没有主力净流入数据'})
        # 该日实际用哪个源 (6/16 之前只有 daykline, 硬编码 push2 会查不到当天数据)
        src = _em_flow_source_for_date(conn, date) or src

        # 2) 当日截面: 该日全部个股 (搜索/市场过滤在 Python 侧做, 与 /api/flow/market 一致)
        rows = conn.execute("""
            SELECT f.trade_date, f.ts_code, f.code, f.name,
                   sd.close AS close, sd.change AS change_pct,
                   f.main_net_inflow, f.main_net_pct,
                   f.super_net, f.big_net, f.mid_net, f.small_net
            FROM fund_flow f
            LEFT JOIN stock_daily sd
              ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
            WHERE f.trade_date = ? AND f.source = ?
        """, (date, src)).fetchall()

        # 3) 多日累计: 以各自最新日 (≤ date) 为锚, 往前取 N 个交易日窗口内的 main_net_inflow 之和.
        #    窗口用 correlated count 取"该票自己"的交易日, 停牌/新股自然只会累加已有的天数.
        sum_cols = ', '.join(
            f'SUM(CASE WHEN rn <= {n} THEN main_net_inflow ELSE 0 END) AS net_{n}d' for n in window
        )
        day_cols = ', '.join(
            f'COUNT(CASE WHEN rn <= {n} THEN 1 END) AS days_{n}d' for n in window
        )
        agg = conn.execute(f"""
            WITH ranked AS (
                SELECT ts_code, main_net_inflow,
                       ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                FROM (SELECT ts_code, trade_date, main_net_inflow,
                             ROW_NUMBER() OVER (PARTITION BY ts_code, trade_date ORDER BY source DESC) AS dup
                      FROM fund_flow
                      WHERE source IN (?, ?) AND trade_date <= ?) AS dedup
                WHERE dup = 1
            )
            SELECT ts_code, {sum_cols}, {day_cols}
            FROM ranked WHERE rn <= 30
            GROUP BY ts_code
        """, EM_FLOW_SOURCES + (date,)).fetchall()
    finally:
        conn.close()

    agg_by = {r['ts_code']: r for r in agg}

    # market 过滤 (按 code 前缀, 与 /api/flow/market 同口径)
    out = []
    for r in rows:
        code = r['code'] or ''
        if market == 'sh' and not code.startswith(('6', '9')):
            continue
        if market == 'sz' and not code.startswith(('0', '2', '3')):
            continue
        if market == 'cy' and not code.startswith('3'):
            continue
        if market == 'kcb' and not code.startswith('688'):
            continue
        d = dict(r)
        a = agg_by.get(r['ts_code'])
        for n in window:
            d[f'net_{n}d'] = a[f'net_{n}d'] if a else None
            d[f'days_{n}d'] = a[f'days_{n}d'] if a else 0
        out.append(d)

    # search 模糊
    if search:
        s = search.lower()
        out = [r for r in out if s in (r['name'] or '').lower() or s in (r['code'] or '')]

    # 默认按当日主力净流入 desc (空搜索时给的是"当日最强票"而不是任意顺序)
    out.sort(key=lambda r: (r['main_net_inflow'] or 0), reverse=True)
    out = out[:limit]
    _attach_sector(out, key='ts_code')  # 补板块, 与主力净流入 tab 一致
    return jsonify({
        'count': len(out), 'date': date, 'search': search, 'market': market,
        'rows': out,
    })


@app.route('/api/barometer/feishu/push', methods=['POST'])
def api_barometer_feishu_push():
    """手动把本地最新一行盘面晴雨表 push 到飞书表格 (A2 插入); date 缺省 = 今天."""
    date = (request.args.get('date') or '').strip()
    if not date:
        date = datetime.now().strftime('%Y%m%d')
    if not re.fullmatch(r'\d{8}', date):
        raise ValueError('日期格式错误')
    return jsonify(push_barometer_to_feishu(date))


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


# 同日去重: 页面每次刷新都会触发 /api/limitup/fetch, 并发任务同时 DELETE+INSERT 同一天的
# limitup 数据, 且前端只轮询最新 job —— 旧 job 成功入库但新 job 挂了, 用户看到"识别失败"
# 而数据其实在库里。同一 trade_date 15 分钟内有 processing job 就直接复用它。
_fetch_jobs_by_date = {}


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
    # 同日已有在跑的 job → 复用, 不起第二个进程
    _ex = _fetch_jobs_by_date.get(trade_date)
    if _ex:
        _ex_job, _ex_ts = _ex
        _st = _read_ocr_status(_ex_job)
        if _st and _st.get('status') == 'processing' and time.time() - _ex_ts < 900:
            print(f'[limitup/fetch] {trade_date} 已有在跑 job {_ex_job}, 复用 (去重)', flush=True)
            return jsonify({'status': 'processing', 'job_id': _ex_job, 'date': trade_date, 'dedup': True})
    job_id = _uuid.uuid4().hex[:8]
    try:
        with open(_ocr_status_path(job_id), 'w', encoding='utf-8') as _f:
            _json.dump({'status': 'processing', 'stage': 'pulling_image'}, _f, ensure_ascii=False)
    except Exception:
        pass
    # Plan C: HTTP 触发 agent 跑 (独立 launchd, 不继承 Flask sandbox 6h 借据过期)
    import urllib.request as _ur
    import urllib.error as _ue
    import socket as _so
    AGENT_URL = os.environ.get('TRADEIQ_AGENT_URL', 'http://127.0.0.1:5556')
    payload = json.dumps({'date': date_str, 'job_id': job_id, 'trade_date': trade_date}).encode()
    try:
        req = _ur.Request(f'{AGENT_URL}/sync/ocr', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with _ur.urlopen(req, timeout=3) as r:
            _ = json.loads(r.read())
    except Exception as e:
        err = f'agent 触发失败 (syncagent 没起来?): {type(e).__name__}: {str(e)[:200]}'
        try:
            with open(_ocr_status_path(job_id), 'w', encoding='utf-8') as _f:
                _json.dump({'status': 'error', 'stage': 'spawn',
                            'error': err, 'job_id': job_id}, _f, ensure_ascii=False)
        except Exception:
            pass
        print(f'[limitup/fetch] {err}', flush=True)
        return jsonify({'status': 'error', 'message': err}), 502
    _fetch_jobs_by_date[trade_date] = (job_id, time.time())
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

    # Plan C: HTTP 触发 agent 跑 OCR (独立 launchd, 不继承 Flask sandbox 6h 借据过期)
    import urllib.request as _ur
    import urllib.error as _ue
    import socket as _so
    AGENT_URL = os.environ.get('TRADEIQ_AGENT_URL', 'http://127.0.0.1:5556')
    payload = json.dumps({'filepath': filepath, 'job_id': job_id, 'trade_date': trade_date}).encode()
    try:
        req = _ur.Request(f'{AGENT_URL}/sync/ocr-image', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with _ur.urlopen(req, timeout=3) as r:
            _ = json.loads(r.read())
    except Exception as e:
        err = f'agent 触发失败 (syncagent 没起来?): {type(e).__name__}: {str(e)[:200]}'
        try:
            with open(_ocr_status_path(job_id), 'w', encoding='utf-8') as _f:
                _json.dump({'status': 'error', 'stage': 'spawn',
                            'error': err, 'job_id': job_id}, _f, ensure_ascii=False)
        except Exception:
            pass
        print(f'[limitup/parse-image] {err}', flush=True)
        return jsonify({'status': 'error', 'message': err}), 502

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
    # processing 也要带上 stage 等字段, 不然前端进度条拿不到阶段 (一直停在默认文案)
    return jsonify({'status': 'processing', **job})


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


# 顶栏「数据拉取情况」: 最近交易日各数据源是否拉全
# (key, 名称, 表, 日期列, 当日算「拉全」的最小行数, 可选 WHERE 额外条件)
SYNC_STATUS_SOURCES = [
    ('index',         '指数日线',        'index_daily',        'date',       4,        ''),
    ('stock_daily',   '个股日线',        'stock_daily',        'trade_date', 5000,     ''),
    ('adj_factor',    '复权因子',        'adj_factor',         'trade_date', 5000,     ''),
    ('overview',      '盘面概览',        'overview',           'date',       1,        ''),
    ('leaderboard',   '阶段涨幅榜',      'leaderboard',        'date',       30,       ''),
    ('limitup',       '涨停拆解',        'limitup',            'date',       1,        ''),
    ('lz',            '涨停合集',        'limitup_collection', 'trade_date', 1,        ''),
    ('flow',          '主力净流入·东财', 'fund_flow',          'trade_date', 4500,     ''),
    ('flow_ths',      '资金流·同花顺',   'fund_flow_ths',      'trade_date', 4500,     ''),
    ('flow_sector_industry', '板块资金流·行业', 'sector_fund_flow', 'trade_date', 496, "sector_type='industry'"),
    ('flow_sector_concept',  '板块资金流·概念', 'sector_fund_flow', 'trade_date', 504, "sector_type='concept'"),
    ('market_fflow',  '大盘资金分时',    'market_fflow',       'trade_date', 1,        ''),
    ('minline_idx',   '指数分钟线',      'index_minline_bars', 'trade_date', 960,      ''),
    ('minline_stock', '个股分钟线',      'stock_minline_bars', 'trade_date', 50000,    ''),
    ('picks',         '选股·东财',       'stock_picks',        'trade_date', 1,        ''),
    ('picks_ths',     '选股·同花顺',     'stock_picks_ths',    'trade_date', 1,        ''),
    ('cond',          '条件选股快照',    'conditional_picks_snapshot',     'trade_date', 1, ''),
    ('cond_new',      '条件选股新',      'conditional_picks_new_snapshot', 'trade_date', 1, ''),
    ('comb',          '组合选股快照',    'combined_picks_snapshot',        'trade_date', 1, ''),
    ('cond_pref',     '条件优选快照',    'conditional_picks_preferred_snapshot', 'trade_date', 1, ''),
    ('cond_ths',      '同花顺条件快照',  'conditional_picks_snapshot_ths', 'trade_date', 1, ''),
    ('comb_ths',      '同花顺组合快照',  'combined_picks_snapshot_ths',    'trade_date', 1, ''),
    # 融资融券 (5 类, 单日同步, ref_date = last_trading_date(today))
    #   汇总: RPTA_RZRQ_LSHJ  全历史 4009+ 行, 1 行/日
    #   个股: RPTA_WEB_RZRQ_GGMX  全历史, 5000+ 行/日
    #   行业: RPTA_WEB_BKJYMXN + BOARD_TYPE_CODE='005'  只当日, 127 行
    #   概念: RPTA_WEB_BKJYMXN + BOARD_TYPE_CODE='006'  只当日, 502 行
    #   账户: RPTA_WEB_MARGIN_DAILYTRADE  全历史 3401 行, 1 行/日, 单位「亿元」混「万名」
    ('rzrq_summary',  '两融汇总',     'rzrq_history_summary',     'dim_date', 1, ''),
    ('rzrq_stock',    '两融·个股',    'rzrq_stock_history',       'date', 5000, ''),
    ('rzrq_industry', '两融·行业',    'rzrq_industry_history',    'trade_date', 100, ''),
    ('rzrq_concept',  '两融·概念',    'rzrq_concept_history',     'trade_date', 400, ''),
    ('rzrq_account',  '两融·账户',    'rzrq_account_history',     'statistics_date', 1, ''),
]
# 5 个 rzrq 表的 date_col 实际存 YYYY-MM-DD 格式 (跟其他表 YYYYMMDD 不同)
# backfill 脚本统一用 r.get('XXX','')[:10] 截断, 见 backfill_rzrq_*.py 的 _row_to_vals
# 这里把 ref (YYYYMMDD) 转换成 YYYY-MM-DD 才能匹配上
_RZRQ_DATE_FORMAT_TABLES = {
    'rzrq_history_summary',    # dim_date
    'rzrq_stock_history',      # date
    'rzrq_industry_history',   # trade_date
    'rzrq_concept_history',    # trade_date
    'rzrq_account_history',    # statistics_date
}
_sync_status_cache = {}  # {ref_date: (timestamp, data)}


def _index_minline_status(count, latest):
    """指数分钟线特殊判断: 盘中由 fetch_index_minline_daily 盘后跑, 盘前/盘中不强求满 960.

    盘前 (< 09:30):  无当日数据 → empty(还没开盘); 有当日数据(历史 lazy) → ok
    盘中 (09:30 ~ 15:30): 任何 count > 0 → ok (允许 partial, 收盘后 fetch 会补)
    盘后 (>= 15:30): >= 960 → ok; < 960 但 > 0 → partial (yellow); == 0 → missing
    """
    if latest is None:
        return 'empty'
    hm = datetime.now().hour * 60 + datetime.now().minute
    if hm < 9 * 60 + 30:
        return 'ok' if count > 0 else 'empty'
    if hm < 15 * 60 + 30:
        return 'ok' if count > 0 else 'missing'
    if count >= 960:
        return 'ok'
    if count == 0:
        return 'missing'
    return 'partial'


def _adj_factor_status(count, latest, expect, ref, today):
    """复权因子: 当天盘后 17:00 才拉, 之前没数据是正常的 (灰字, 不当成缺失).

    17:00 之后当天还缺 → missing (红色, 可以点面板补); 过去的日期缺 → 一样算 missing.
    """
    if count >= expect:
        return 'ok'
    if count > 0:
        return 'partial'
    if ref == today and datetime.now().hour < 17:
        return 'empty'
    if latest is None:
        return 'empty'
    return 'missing'


@app.route('/api/sync/overview', methods=['GET'])
def sync_status_overview():
    """各数据源在指定/最近交易日的拉取情况。60s 内存缓存(按日期); 各表日期格式统一为 YYYYMMDD 且有索引, 全表扫描不参与。"""
    today = datetime.now().strftime('%Y%m%d')
    requested = request.args.get('date', '')
    ref = requested if re.fullmatch(r'\d{8}', requested) else last_trading_date(today)
    now = time.time()
    cached = _sync_status_cache.get(ref)
    if cached and now - cached[0] < 60:
        return jsonify(cached[1])
    sources = []
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        for key, name, table, col, expect, extra_where in SYNC_STATUS_SOURCES:
            try:
                row = conn.execute(f'SELECT {col} FROM {table} ORDER BY {col} DESC LIMIT 1').fetchone()
                latest = row[0] if row else None
                where_extra = f' AND {extra_where}' if extra_where else ''
                # 5 个 rzrq 表用 YYYY-MM-DD 格式, 其他表用 YYYYMMDD
                if table in _RZRQ_DATE_FORMAT_TABLES and ref:
                    date_val = f'{ref[0:4]}-{ref[4:6]}-{ref[6:8]}'
                else:
                    date_val = ref
                count = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {col} = ?{where_extra}', (date_val,)).fetchone()[0] if ref else 0
            except sqlite3.Error:
                latest, count = None, 0
            if key == 'minline_idx':
                status = _index_minline_status(count, latest)
            elif key == 'adj_factor':
                status = _adj_factor_status(count, latest, expect, ref, today)
            elif latest is None:
                status = 'empty'
            elif count == 0:
                status = 'missing'
            elif count < expect:
                status = 'partial'
            else:
                status = 'ok'
            sources.append({'key': key, 'name': name, 'latest': latest,
                            'count': count, 'expected': expect, 'status': status})
    finally:
        conn.close()
    data = {
        'ref_date': ref, 'today': today, 'is_trading_today': ref == today,
        'sources': sources,
        'ok': sum(1 for s in sources if s['status'] == 'ok'),
        'partial': sum(1 for s in sources if s['status'] == 'partial'),
        'missing': sum(1 for s in sources if s['status'] in ('missing', 'empty')),
        'generated_at': datetime.now().strftime('%H:%M:%S'),
    }
    _sync_status_cache[ref] = (now, data)
    return jsonify(data)


# ── 单项拉取 (点击「拉取目标」面板里某项触发) ──
# sync 模式: 同步阻塞, 等内部函数跑完返回结果. 一次请求一个完整任务.
# async 模式: 后台线程 fire-and-forget (主力净流入/板块资金流/同花顺/大盘资金分时/韭研爬取),
#             立即返回 started, 前端轮询 _xxx_sync_status 端点看 done.
def _post_internal(path, force=False, type_param=None):
    with app.test_client() as c:
        url = path + (f'?type={type_param}' if type_param else '')
        body = {'force': True} if force else {}
        r = c.post(url, json=body)
        try:
            return r.get_json()
        except Exception:
            return {'status_code': r.status_code, 'text': r.get_data(as_text=True)[:200]}

def _get_internal(path, date_str):
    with app.test_client() as c:
        qs = f'?date={date_str}' if date_str else ''
        r = c.get(path + qs)
        try:
            return r.get_json()
        except Exception:
            return {'status_code': r.status_code, 'text': r.get_data(as_text=True)[:200]}

def _yyyymmdd_to_dash(s):
    return f'{s[0:4]}-{s[4:6]}-{s[6:8]}' if s and len(s) == 8 and re.fullmatch(r'\d{8}', s) else (s or '')

def _resolve_ref_date(date_str):
    return date_str or last_trading_date(datetime.now().strftime('%Y%m%d'))

def _run_index(date):           return _post_internal('/api/index/sync')
def _run_stock_daily(date):     return _post_internal('/api/stock/sync')
def _run_overview(date):        return _post_internal('/api/overview/sync')
def _run_leaderboard(date):     return _post_internal('/api/leaderboard/sync')
def _run_picks(date):           return _post_internal('/api/picks/sync')
def _run_picks_ths(date):       return _post_internal('/api/picks/ths/sync')

def _run_limitup_ak(date):
    ds = _yyyymmdd_to_dash(date)
    with app.test_client() as c:
        r = c.post('/api/limitup/ak/sync', json={'date': ds} if ds else {})
        try:
            return r.get_json()
        except Exception:
            return {'status_code': r.status_code, 'text': r.get_data(as_text=True)[:200]}

def _run_lz(date):
    from app import collect_limitup_snapshot
    return collect_limitup_snapshot()

def _run_minline_idx(date):
    from fetch_index_minline_daily import run as run_idx_minline
    # skip_existing=False: 点击 = 强制重抓 (覆盖已有), 符合"重新拉取"语义
    return run_idx_minline(skip_existing=False, verbose=True)

def _run_minline_stock(date):
    from fetch_stock_minline_daily import run as run_minline
    return run_minline(skip_existing=False, verbose=True)

# cond/cond_new/comb/cond_pref/cond_ths/comb_ths 走 GET 触发实时计算后写快照
def _run_cond(date):        return _get_internal('/api/conditional-picks', _resolve_ref_date(date))
def _run_cond_new(date):    return _get_internal('/api/conditional-picks/new', _resolve_ref_date(date))
def _run_comb(date):        return _get_internal('/api/combined-picks', _resolve_ref_date(date))
def _run_cond_pref(date):   return _get_internal('/api/conditional-picks/preferred', _resolve_ref_date(date))
def _run_cond_ths(date):    return _get_internal('/api/picks/ths/conditional', _resolve_ref_date(date))
def _run_comb_ths(date):    return _get_internal('/api/picks/ths/combined', _resolve_ref_date(date))

# async fire-and-forget (后台线程, 立即返回 started)
def _start_flow(date):                  return _post_internal('/api/flow/sync-market', force=True)
def _start_flow_ths(date):              return _post_internal('/api/flow/ths/sync', force=True)
def _start_flow_sector_industry(date):  return _post_internal('/api/flow/sector/sync', force=True, type_param='industry')
def _start_flow_sector_concept(date):   return _post_internal('/api/flow/sector/sync', force=True, type_param='concept')
def _start_market_fflow(date):
    with app.test_client() as c:
        r = c.post('/api/market/fflow/sync')
        try:
            return r.get_json()
        except Exception:
            return {'status_code': r.status_code, 'text': r.get_data(as_text=True)[:200]}
def _start_limitup_fetch(date):
    ds = _yyyymmdd_to_dash(date) or datetime.now().strftime('%Y-%m-%d')
    with app.test_client() as c:
        r = c.post('/api/limitup/fetch', json={'date': ds})
        try:
            return r.get_json()
        except Exception:
            return {'status_code': r.status_code, 'text': r.get_data(as_text=True)[:200]}

# 融资融券 5 类 (sync 模式, 秒级)
# 关键: 用 _resolve_ref_date 显式把 ref_date 传下去 — 避免像 6 个 async runner 那样丢失 date.
# 10-8 点击 → ref_date='20260930' → fetch_one_date 写入 9-30 → 状态变 ok.
def _run_rzrq_summary(date):
    target = _resolve_ref_date(date)
    from backfill_rzrq_history import fetch_one_date
    rows = fetch_one_date(target)
    return {'rows': rows, 'date': target, 'source': 'eastmoney-rzrq-summary'}

def _run_rzrq_stock(date):
    target = _resolve_ref_date(date)
    from backfill_rzrq_stock import fetch_one_date
    rows = fetch_one_date(target)
    return {'rows': rows, 'date': target, 'source': 'eastmoney-rzrq-stock'}

def _run_rzrq_industry(date):
    target = _resolve_ref_date(date)
    from backfill_rzrq_board import fetch_one_date, TYPE_INDUSTRY
    rows = fetch_one_date(target, board_type_code=TYPE_INDUSTRY)
    return {'rows': rows, 'date': target, 'source': 'eastmoney-rzrq-industry'}

def _run_rzrq_concept(date):
    target = _resolve_ref_date(date)
    from backfill_rzrq_board import fetch_one_date, TYPE_CONCEPT
    rows = fetch_one_date(target, board_type_code=TYPE_CONCEPT)
    return {'rows': rows, 'date': target, 'source': 'eastmoney-rzrq-concept'}

def _run_rzrq_account(date):
    target = _resolve_ref_date(date)
    from backfill_rzrq_account import fetch_one_date
    rows = fetch_one_date(target)
    return {'rows': rows, 'date': target, 'source': 'eastmoney-rzrq-account'}

# 复权因子: 盘后 17:00 后 TuShare 才入库当天因子, 点一下拉当天 (可重复拉, 覆盖写)
def _run_adj_factor(date):
    target = _resolve_ref_date(date)
    with app.test_client() as c:
        r = c.post(f'/api/adj-factor/sync?date={target}')
        try:
            return r.get_json()
        except Exception:
            return {'status_code': r.status_code, 'text': r.get_data(as_text=True)[:200]}

SYNC_RUNNERS = {
    'index':                ('sync',  _run_index),
    'stock_daily':          ('sync',  _run_stock_daily),
    'overview':             ('sync',  _run_overview),
    'leaderboard':          ('sync',  _run_leaderboard),
    'limitup':              ('sync',  _run_limitup_ak),
    'lz':                   ('sync',  _run_lz),
    'flow':                 ('async', _start_flow),
    'flow_ths':             ('async', _start_flow_ths),
    'flow_sector_industry': ('async', _start_flow_sector_industry),
    'flow_sector_concept':  ('async', _start_flow_sector_concept),
    'market_fflow':         ('async', _start_market_fflow),
    'minline_idx':          ('sync',  _run_minline_idx),
    'minline_stock':        ('sync',  _run_minline_stock),
    'picks':                ('sync',  _run_picks),
    'picks_ths':            ('sync',  _run_picks_ths),
    'cond':                 ('sync',  _run_cond),
    'cond_new':             ('sync',  _run_cond_new),
    'comb':                 ('sync',  _run_comb),
    'cond_pref':            ('sync',  _run_cond_pref),
    'cond_ths':             ('sync',  _run_cond_ths),
    'comb_ths':             ('sync',  _run_comb_ths),
    # 融资融券 5 类
    'rzrq_summary':         ('sync',  _run_rzrq_summary),
    'rzrq_stock':           ('sync',  _run_rzrq_stock),
    'rzrq_industry':        ('sync',  _run_rzrq_industry),
    'rzrq_concept':         ('sync',  _run_rzrq_concept),
    'rzrq_account':         ('sync',  _run_rzrq_account),
    # 复权因子 (盘后 17:00 后 TuShare 才入库当天因子)
    'adj_factor':           ('sync',  _run_adj_factor),
}

@app.route('/api/sync/run/<key>', methods=['POST'])
def sync_run_one(key):
    """点击「拉取目标」面板里某项触发.

    sync 模式: 内部函数跑完才返回 (一般几秒, 韭研类的我们走 limitup 走 ak/sync 几秒即可).
    async 模式: 后台线程启动后立刻返回 started (主力净流入/同花顺/板块资金流/大盘资金分时/韭研 OCR 4-5 分钟),
                前端轮询 _xxx_sync_status 端点看 done 后再刷新.
    """
    if key not in SYNC_RUNNERS:
        return jsonify({'status': 'error', 'message': f'unknown key: {key}, 有效 key 列表见 SYNC_RUNNERS'}), 400
    mode, runner = SYNC_RUNNERS[key]
    date_str = request.args.get('date', '')
    if date_str and not re.fullmatch(r'\d{8}', date_str):
        return jsonify({'status': 'error', 'message': 'date 格式应为 YYYYMMDD'}), 400
    t0 = time.time()
    try:
        result = runner(date_str)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'key': key, 'message': str(e)}), 500
    elapsed = round(time.time() - t0, 1)
    _sync_status_cache.clear()  # 让下次 refreshSyncStatus 立刻看到新数据
    return jsonify({
        'status': 'success',
        'key': key,
        'mode': mode,
        'date': date_str,
        'elapsed': elapsed,
        'result': result,
    })


def _is_trading_day(date_str):
    """本地交易日历里有没有这一天 (trading_dates_cache 只存交易日)."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        return bool(conn.execute(
            'SELECT 1 FROM trading_dates_cache WHERE cal_date=?', (date_str,)).fetchone())
    finally:
        conn.close()


@app.route('/api/adj-factor/sync', methods=['POST'])
def api_adj_factor_sync():
    """拉复权因子入库 (TuShare pro.adj_factor → adj_factor 表).

    date 缺省 = 最近一个交易日; recent=N 则补最近 N 个交易日里缺的日子 (盘后定时任务用).
    非交易日 / TuShare 当天还没入库 → skipped, 不算失败
    (盘后 17:00 前本来就拿不到, 面板上显示灰字「无数据」而不是红色缺失)."""
    date_str = (request.args.get('date') or '').strip()
    if date_str and not re.fullmatch(r'\d{8}', date_str):
        return jsonify({'status': 'error', 'message': 'date 格式应为 YYYYMMDD'}), 400
    try:
        recent_days = max(0, min(30, int(request.args.get('recent') or 0)))
    except ValueError:
        recent_days = 0
    target = date_str or last_trading_date(datetime.now().strftime('%Y%m%d'))
    if recent_days:
        from adj_factor_sync import sync_recent
        result = sync_recent(days=recent_days, token=TUSHARE_TOKEN)
    else:
        if not _is_trading_day(target):
            return jsonify({'status': 'success', 'date': target, 'rows': 0,
                            'skipped': True, 'reason': '非交易日'})
        from adj_factor_sync import fetch_one_date
        result = fetch_one_date(target, token=TUSHARE_TOKEN)
    _sync_status_cache.clear()   # 让「拉取目标」面板立刻看到新状态
    return jsonify({'status': 'success', **result})


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


# ============ 交割分析 ============

@app.route('/api/delivery/stocks', methods=['GET'])
def delivery_stocks():
    """交割分析: 所有有买卖记录的股票列表 (按最后交易日倒序)"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('''SELECT ts_code, MAX(name) AS name,
        SUM(CASE WHEN direction='buy' THEN 1 ELSE 0 END) AS buys,
        SUM(CASE WHEN direction='sell' THEN 1 ELSE 0 END) AS sells,
        MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
        MAX(CASE WHEN direction='buy' THEN trade_date END) AS last_buy_date
        FROM trades
        WHERE direction IN ('buy','sell') AND applied >= 0
        GROUP BY ts_code ORDER BY last_buy_date DESC''').fetchall()
    conn.close()
    return jsonify({'stocks': [dict(r) for r in rows], 'count': len(rows)})


@app.route('/api/delivery/chart', methods=['GET'])
def delivery_chart():
    """交割分析单股数据: 日K + 买卖点 + 主力净流入, 一次返回.
    K线窗口 = 首次交易往前 20 个交易日 → 最新. 全程只读库, 不走外部回补."""
    raw = (request.args.get('ts_code') or '').strip().upper()
    if not raw:
        return jsonify({'error': 'missing ts_code'}), 400
    if '.' not in raw and raw.isdigit() and len(raw) == 6:
        suffix = 'SH' if raw[0] == '6' else ('BJ' if raw[0] in '48' else 'SZ')
        raw = f'{raw}.{suffix}'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    trades = [dict(r) for r in conn.execute(
        """SELECT direction, price, shares, amount, trade_date, trade_time
           FROM trades WHERE ts_code = ? AND direction IN ('buy','sell') AND applied >= 0
           ORDER BY trade_date, trade_time""", (raw,)).fetchall()]
    if not trades:
        conn.close()
        return jsonify({'error': 'no trades', 'ts_code': raw}), 404
    name_row = conn.execute(
        'SELECT name FROM trades WHERE ts_code = ? AND name IS NOT NULL LIMIT 1', (raw,)).fetchone()
    first = trades[0]['trade_date'].replace('-', '')

    # 起点: 首次交易往前 20 个交易日 (日历不足则就用首日)
    start_row = conn.execute(
        """SELECT cal_date FROM trading_dates_cache WHERE cal_date < ?
           ORDER BY cal_date DESC LIMIT 1 OFFSET 19""", (first,)).fetchone()
    start = start_row[0] if start_row else first

    daily = [dict(r) for r in conn.execute(
        """SELECT trade_date, open, high, low, close, volume, amount
           FROM stock_daily WHERE ts_code = ? AND trade_date >= ?
           ORDER BY trade_date""", (raw, start)).fetchall()]

    flow = {r['trade_date']: r['main_net_inflow'] for r in conn.execute(
        'SELECT trade_date, main_net_inflow FROM fund_flow WHERE ts_code = ? AND trade_date >= ?',
        (raw, start)).fetchall()}
    conn.close()

    for t in trades:
        t['trade_date'] = t['trade_date'].replace('-', '')  # 对齐 daily 的 YYYYMMDD
    return jsonify({'ts_code': raw, 'name': name_row[0] if name_row else '',
                    'trades': trades, 'daily': daily, 'flow': flow})


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


# ── 个股搜索 + 个股交易档案 (持仓模块顶栏搜索用) ──

@app.route('/api/stocks/search', methods=['GET'])
def search_stocks():
    """模糊搜股票 (用于持仓 tab 顶栏搜索)
    候选池: stock_basic ∪ trades.ts_code ∪ positions.ts_code (后者拿来支持退市股 / 历史股)
    匹配: ts_code LIKE 'q%' OR name LIKE '%q%' (大小写不敏感)
    排序: stock_basic 命中优先, 然后 ts_code 前缀优先, 然后名字序
    """
    q = (request.args.get('q') or '').strip()
    limit = request.args.get('limit', 20, type=int)
    if limit > 50:
        limit = 50
    if not q:
        return jsonify({'results': []})

    like = f'%{q}%'
    prefix = f'{q}%'
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 候选池: 上市股 + 历史交易里出现过的 (给退市股兜底)
    rows = conn.execute(
        """SELECT ts_code, MAX(name) AS name,
                  MAX(listing_date) AS listing_date,
                  MAX(market) AS market,
                  MAX(status) AS status,
                  'listed' AS source
           FROM stock_basic
           WHERE ts_code LIKE ? OR name LIKE ?
           GROUP BY ts_code
           UNION
           SELECT ts_code, MAX(name) AS name, NULL, NULL, 'delisted', 'history'
           FROM (
               SELECT DISTINCT ts_code, name FROM trades WHERE ts_code NOT IN (SELECT ts_code FROM stock_basic)
               UNION
               SELECT DISTINCT ts_code, name FROM positions WHERE ts_code NOT IN (SELECT ts_code FROM stock_basic)
           )
           WHERE ts_code LIKE ? OR name LIKE ?
           GROUP BY ts_code""",
        (prefix, like, prefix, like)
    ).fetchall()
    conn.close()

    norm_q = q.lower().replace('sh', '').replace('sz', '').replace('bj', '').lstrip('shsz').lower()

    def _score(r):
        code = (r['ts_code'] or '').lower()
        name = (r['name'] or '')
        # 排序键: source='listed' 优先 (0 < 1), 然后 ts_code 前缀优先, 然后名字前缀优先
        s = 0 if r['source'] == 'listed' else 100
        if code.startswith(norm_q):
            s += 0
        elif code.replace('.', '').startswith(norm_q):
            s += 5
        else:
            s += 20
        if name.startswith(q):
            s += 0
        elif name.startswith(q.upper()):
            s += 1
        else:
            s += 10
        return s

    results = sorted([dict(r) for r in rows], key=_score)
    # 最精简: 只返回 ts_code, name, source
    out = [{'ts_code': r['ts_code'], 'name': r['name'] or '', 'source': r['source']} for r in results[:limit]]
    return jsonify({'results': out, 'query': q, 'count': len(out)})


def _build_trade_history(ts_code):
    """单股交易档案 (stock + position + buys + sells)
    - stock.stock_basic 没这只股 (退市) 也保证能返回 (用 trades.name)
    - buys 含 buy_fees / current_price / post_buy_change_pct
    - sells 含 sell_fees / current_price / post_sell_change_pct / realized_pnl (FIFO 单股版)
    - position 取 active (closed_at IS NULL) 那条, 算市值/浮盈
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 股票名: stock_basic > trades > positions (优先用 stock_basic 的官方名)
    name = None
    source = 'delisted'
    sb = conn.execute('SELECT name, listing_date, market, status FROM stock_basic WHERE ts_code=?', (ts_code,)).fetchone()
    if sb:
        name = sb['name']
        source = 'listed'
    else:
        trow = conn.execute('SELECT name FROM trades WHERE ts_code=? AND name IS NOT NULL LIMIT 1', (ts_code,)).fetchone()
        if trow and trow['name']:
            name = trow['name']
        else:
            prow = conn.execute('SELECT name FROM positions WHERE ts_code=? AND name IS NOT NULL LIMIT 1', (ts_code,)).fetchone()
            if prow and prow['name']:
                name = prow['name']

    # 行情: 一次拉, 单只股
    try:
        quote_map = fetch_sina_quotes([ts_code])
    except Exception:
        quote_map = {}
    cur_price = quote_map.get(ts_code, {}).get('price')

    # 现持仓: active 那条
    pos = conn.execute(
        'SELECT * FROM positions WHERE ts_code=? AND closed_at IS NULL ORDER BY created_at DESC LIMIT 1',
        (ts_code,)
    ).fetchone()
    position = None
    if pos:
        position = dict(pos)
        if cur_price and position.get('cost_price') and position.get('shares'):
            mv = round(cur_price * position['shares'], 2)
            cost = round(position['cost_price'] * position['shares'], 2)
            position['market_value'] = mv
            position['floating_pnl'] = round(mv - cost, 2)
            position['floating_pnl_pct'] = round((cur_price - position['cost_price']) / position['cost_price'] * 100, 3)
            position['current_price'] = cur_price
        else:
            position['market_value'] = None
            position['floating_pnl'] = None
            position['floating_pnl_pct'] = None
            position['current_price'] = cur_price

    # buys
    buy_rows = conn.execute(
        """SELECT t.*, p.position_type AS pos_type, p.shares AS pos_shares,
                  p.original_shares AS pos_original_shares, p.closed_at AS pos_closed_at
           FROM trades t
           LEFT JOIN positions p
             ON p.ts_code = t.ts_code AND p.closed_at IS NULL
           WHERE t.direction = 'buy' AND t.ts_code = ?
           ORDER BY t.trade_date DESC, t.trade_time DESC, t.id DESC""",
        (ts_code,)
    ).fetchall()
    buys = []
    total_buy_amount = 0.0
    total_buy_fees = 0.0
    for r in buy_rows:
        b = dict(r)
        buy_amt = b.get('amount') or (b.get('price', 0) * b.get('shares', 0))
        b['buy_fees'] = round(calc_buy_fees(buy_amt, ts_code), 2) if buy_amt > 0 else 0.0
        if cur_price and b.get('price'):
            b['current_price'] = cur_price
            b['post_buy_change_pct'] = round((cur_price - b['price']) / b['price'] * 100, 3)
        else:
            b['current_price'] = None
            b['post_buy_change_pct'] = None
        buys.append(b)
        total_buy_amount += buy_amt
        total_buy_fees += b['buy_fees']

    # sells + 单股版 FIFO (只对当前股票跑, 性能可控)
    # 1) 拉该股全部 trades (含 buy/sell/bonus/transfer_out/dividend/tax) 用于 FIFO
    all_ts = conn.execute(
        """SELECT id, ts_code, direction, price, shares, amount, trade_date, trade_time, trade_no
           FROM trades
           WHERE ts_code = ? AND direction IN ('buy','sell','bonus','transfer_out','dividend','tax')
             AND applied != -1
           ORDER BY trade_date, trade_time, trade_no, id""",
        (ts_code,)
    ).fetchall()
    all_ts = [dict(r) for r in all_ts]
    # 2) FIFO 队列
    fifo_q = []  # [(raw_cps, remaining, buy_date), ...]
    realized_by_trade_id = {}  # trade.id -> realized_pnl
    for t in all_ts:
        amt = t.get('amount') or 0
        sh = t.get('shares') or 0
        if t['direction'] == 'buy':
            bf = calc_buy_fees(amt, ts_code)
            raw_cps = (amt + bf) / sh if sh else t['price']
            fifo_q.append([raw_cps, sh, t['trade_date']])
        elif t['direction'] == 'sell':
            sf = calc_sell_fees(amt, ts_code)
            raw_sps = (amt - sf) / sh if sh else t['price']
            # 同日 div/tax 摊销
            net_div_total = 0
            max_div_date = None
            for prior in all_ts:
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
            if net_div_total != 0 and max_div_date is not None:
                eligible = sum(q[1] for q in fifo_q if q[2] <= max_div_date and q[1] > 0)
                if eligible > 0:
                    per_share = net_div_total / eligible
                    for q in fifo_q:
                        if q[2] <= max_div_date and q[1] > 0:
                            q[0] = q[0] - per_share
            seg_total = 0.0
            remaining = sh
            while remaining > 0 and fifo_q:
                head = fifo_q[0]
                take = min(head[1], remaining)
                seg = _round_half_up((raw_sps - head[0]) * take)
                seg_total = _round_half_up(seg_total + seg)
                head[1] -= take
                remaining -= take
                if head[1] < 1e-9:
                    fifo_q.pop(0)
            realized_by_trade_id[t['id']] = seg_total
        elif t['direction'] == 'bonus':
            fifo_q.append([0.0, sh, t['trade_date']])
        elif t['direction'] == 'transfer_out':
            remaining = sh
            while remaining > 0 and fifo_q:
                head = fifo_q[0]
                take = min(head[1], remaining)
                head[1] -= take
                remaining -= take
                if head[1] < 1e-9:
                    fifo_q.pop(0)
        # dividend / tax 单股场景下 pending 不需要 (sell 段内已摊销完)

    sell_rows = conn.execute(
        """SELECT * FROM trades
           WHERE direction = 'sell' AND ts_code = ? AND applied != -1
           ORDER BY trade_date DESC, trade_time DESC, id DESC""",
        (ts_code,)
    ).fetchall()
    sells = []
    total_sell_amount = 0.0
    total_sell_fees = 0.0
    total_realized_pnl = 0.0
    for r in sell_rows:
        s = dict(r)
        sell_amt = s.get('amount') or (s.get('price', 0) * s.get('shares', 0))
        s['sell_fees'] = round(calc_sell_fees(sell_amt, ts_code), 2) if sell_amt > 0 else 0.0
        if cur_price and s.get('price'):
            s['current_price'] = cur_price
            s['post_sell_change_pct'] = round((cur_price - s['price']) / s['price'] * 100, 3)
        else:
            s['current_price'] = None
            s['post_sell_change_pct'] = None
        s['realized_pnl'] = realized_by_trade_id.get(s['id'])
        sells.append(s)
        total_sell_amount += sell_amt
        total_sell_fees += s['sell_fees']
        if s['realized_pnl'] is not None:
            total_realized_pnl += s['realized_pnl']

    conn.close()

    return {
        'stock': {
            'ts_code': ts_code,
            'name': name or '',
            'source': source,
            'listing_date': sb['listing_date'] if sb else None,
            'market': sb['market'] if sb else None,
        },
        'position': position,
        'buys': buys,
        'sells': sells,
        'summary': {
            'buy_count': len(buys),
            'sell_count': len(sells),
            'total_buy_amount': round(total_buy_amount, 2),
            'total_buy_fees': round(total_buy_fees, 2),
            'total_sell_amount': round(total_sell_amount, 2),
            'total_sell_fees': round(total_sell_fees, 2),
            'total_realized_pnl': round(total_realized_pnl, 2),
            'current_price': cur_price,
        }
    }


@app.route('/api/trades/history/<path:code>', methods=['GET'])
def get_trade_history(code):
    """个股交易档案: stock + position + buys + sells 一次返回 (持仓 tab 搜索命中后用)"""
    ts_code = _normalize_code(code)
    if not ts_code:
        return jsonify({'status': 'error', 'message': f'代码格式无效: {code!r}'}), 400
    data = _build_trade_history(ts_code)
    return jsonify({'status': 'success', **data})


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
                # 字段含义: 0=名称 1=今开 2=昨收 3=当前 4=日高 5=日低 8=成交量(股) ...
                name = fields[0]
                try:
                    current = float(fields[3])
                    prev_close = float(fields[2])
                except (ValueError, IndexError):
                    continue
                if prev_close <= 0:
                    continue
                change_pct = (current - prev_close) / prev_close * 100
                try:
                    day_high = float(fields[4])
                    if day_high <= 0:
                        day_high = None
                except (ValueError, IndexError):
                    day_high = None
                # 今开/日低/成交量: K线弹窗盘中那根K用 (盘前今开=0 → 调用方自行判空)
                def _f(idx):
                    try:
                        v = float(fields[idx])
                        return v if v > 0 else None
                    except (ValueError, IndexError):
                        return None
                day_open = _f(1)
                day_low = _f(5)
                vol_shares = _f(8)  # sina 单位: 股
                amount_yuan = _f(9)  # sina 单位: 元
                # 时间: index 30 起始 HHMMSS (不同市场略有差异)
                quote_time = (fields[30] + ' ' + fields[31]) if len(fields) > 31 else (fields[30] if len(fields) > 30 else '')
                ts_code = _ts_code_for_sina(sina_code)
                if ts_code:
                    entry = {
                        'price': current,
                        'prev_close': prev_close,
                        'change_pct': round(change_pct, 2),
                        'high': day_high,
                        'open': day_open,
                        'low': day_low,
                        'volume': round(vol_shares / 100, 2) if vol_shares else None,  # 股 → 手 (对齐 stock_daily)
                        'amount': amount_yuan,  # 成交额 (元)
                        'name': name,
                        'time': quote_time,
                    }
                    _QUOTE_CACHE[ts_code] = {**entry, '_ts': now}
                    result[ts_code] = entry
        except Exception as e:
            print(f'fetch_sina_quotes 错误: {e}')

    # 3) 合并: 缓存 + 新拉
    return {**cached, **result}


def fetch_sina_quotes_all(cache_ttl=None):
    """拉全市场 ~5500 只股的实时行情. 复用 fetch_sina_quotes 的 30s 全局缓存."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT ts_code FROM stock_basic WHERE status != 'delisting'").fetchall()
    finally:
        conn.close()
    ts_codes = [r[0] for r in rows]
    return fetch_sina_quotes(ts_codes, cache_ttl=cache_ttl)


# ============ 涨停合集 (盘中实时收集) ============

def _limit_pct_for(market, status):
    """涨停档位: 创业板/科创板 20% (ST 也是 20%), 北交所 30%, 主板 ST 5%, 主板正常 10%"""
    if market in ('创业板', '科创板'):
        return 20.0
    if market == '北交所':
        return 30.0
    if status in ('st', 'star'):
        return 5.0
    return 10.0


def _limit_up_price(prev_close, limit_pct):
    """A股涨停价 = 昨收 × (1+档位), 四舍五入到分 (Decimal 避免 Python round 银行家舍入)"""
    from decimal import Decimal, ROUND_HALF_UP
    p = Decimal(str(prev_close)) * (Decimal('1') + Decimal(str(limit_pct)) / Decimal('100'))
    return float(p.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _today_is_trading_day():
    """今天是否交易日: 查 trading_dates_cache, 缓存没覆盖到今天就从 TuShare 补一次"""
    today = datetime.now().strftime('%Y%m%d')
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute('SELECT MAX(cal_date) FROM trading_dates_cache').fetchone()
        if row and row[0] and row[0] >= today:
            return conn.execute(
                'SELECT 1 FROM trading_dates_cache WHERE cal_date = ?', (today,)
            ).fetchone() is not None
    finally:
        conn.close()
    # 缓存没覆盖到今天 → 拉一次 TuShare 补
    try:
        pro = get_pro()
        df = pro.trade_cal(exchange='SSE', start_date=today, end_date=today)
        is_open = len(df) > 0 and int(df.iloc[0]['is_open']) == 1
        if is_open:
            conn = sqlite3.connect(DB_PATH)
            conn.execute('INSERT OR IGNORE INTO trading_dates_cache (cal_date) VALUES (?)', (today,))
            conn.commit()
            conn.close()
        return is_open
    except Exception as e:
        print(f'[lz] 交易日判断失败 (保守跳过): {e}')
        return False


def last_trading_date(date_str):
    """返回 <= date_str 的最近一个交易日 (YYYYMMDD)。date_str 本身是交易日 → 返回自身。

    与 fetch_fund_flow_today.py 同名函数对齐, 但只依赖本地 trading_dates_cache (不挂 akshare 兜底),
    让 minline 持久化 / scheduler / lazy 路径都能轻量复用。"""
    if not date_str or len(date_str) != 8 or not str(date_str).isdigit():
        return date_str
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        row = conn.execute(
            'SELECT MAX(cal_date) FROM trading_dates_cache WHERE cal_date <= ?',
            (date_str,),
        ).fetchone()
        if row and row[0]:
            return row[0]
    finally:
        conn.close()
    return date_str  # 兜底用 date_str (上层有错位风险, 但比报错好)


def _infer_minline_trade_date(now=None):
    """推断 sina 分时返回数据应归属的 trade_date (YYYYMMDD).

    规则:
    - 09:25 (含) ~ 当日收盘 (sina 09:25 集合竞价起算今日 bars) + 今天交易日 → today
    - 09:25 集合竞价之前 (含开盘前夜) → 上一交易日 (sina 返回的还是上一交易日的延续)
    - 非交易日 (周末/节假日) → 上一交易日
    """
    now = now or datetime.now()
    today = now.strftime('%Y%m%d')
    if not _today_is_trading_day():
        return last_trading_date(today)
    hm = now.hour * 100 + now.minute
    if hm < 925:
        return last_trading_date((now - timedelta(days=1)).strftime('%Y%m%d'))
    return today


def collect_limitup_snapshot():
    """盘中扫全市场, 把今天摸过涨停价的股票收录进 limitup_collection.

    判定: high >= 涨停价 → 曾涨停 (收录; high 全天不回落, 漏扫几轮也不丢).
    状态: price >= 涨停价 → 封板中; 曾封板后打开 → broken=1 (炸板, 不掉出列表).
    scheduler 盘中每分钟跑一轮; 也可 POST /api/limitup/collection/scan 手动触发.
    """
    now = datetime.now()
    hm = now.hour * 100 + now.minute
    if not (930 <= hm <= 1135 or 1255 <= hm <= 1505):
        return {'skipped': '非交易时段'}
    if not _today_is_trading_day():
        return {'skipped': '非交易日'}
    trade_date = now.strftime('%Y%m%d')
    now_hms = now.strftime('%H:%M:%S')

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT ts_code, name, market, status FROM stock_basic
           WHERE status != 'delisting'
             AND ts_code NOT LIKE '200%.SZ' AND ts_code NOT LIKE '900%.SH'"""  # 排除 B 股
    ).fetchall()
    conn.close()
    meta = {r['ts_code']: dict(r) for r in rows}

    quotes = fetch_sina_quotes_all(cache_ttl=2)

    hits = []
    for ts_code, q in quotes.items():
        prev_close = q.get('prev_close')
        if not prev_close or prev_close <= 0:
            continue
        m = meta.get(ts_code) or {}
        limit_pct = _limit_pct_for(m.get('market'), m.get('status'))
        limit_price = _limit_up_price(prev_close, limit_pct)
        high = q.get('high')
        price = q.get('price')
        touched = (high is not None and high >= limit_price - 1e-6) or \
                  (price is not None and price >= limit_price - 1e-6)
        if not touched:
            continue
        sealed = 1 if (price is not None and price >= limit_price - 1e-6) else 0
        hits.append((ts_code, m.get('name') or q.get('name') or '',
                     m.get('market') or '', limit_pct, sealed))

    conn = sqlite3.connect(DB_PATH, timeout=30)
    new_count = 0
    for ts_code, name, market, limit_pct, sealed in hits:
        cur = conn.execute('''INSERT OR IGNORE INTO limitup_collection
            (trade_date, ts_code, name, market, limit_pct, first_limit_time, sealed, broken, last_seen_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)''',
            (trade_date, ts_code, name, market, limit_pct, now_hms, sealed, now_hms))
        if cur.rowcount:
            new_count += 1
        else:
            conn.execute('''UPDATE limitup_collection SET
                sealed = ?,
                broken = CASE WHEN ? = 0 AND sealed = 1 THEN 1 ELSE broken END,
                last_seen_time = ?
                WHERE trade_date = ? AND ts_code = ?''',
                (sealed, sealed, now_hms, trade_date, ts_code))
    conn.commit()
    total = conn.execute('SELECT COUNT(*) FROM limitup_collection WHERE trade_date = ?',
                         (trade_date,)).fetchone()[0]
    conn.close()
    return {'date': trade_date, 'new': new_count, 'total': total, 'scanned': len(quotes)}


@app.route('/api/limitup/collection/scan', methods=['POST'])
def scan_limitup_collection():
    """手动触发一轮涨停合集采集 (scheduler 盘中每分钟自动跑, 这里是测试/兜底入口)"""
    try:
        return jsonify({'status': 'success', **collect_limitup_snapshot()})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/limitup/collection', methods=['GET'])
def get_limitup_collection():
    """涨停合集查询. ?date=YYYYMMDD 缺省=今天.
    今天: 叠加 sina 实时价/涨幅/成交额/换手率(成交量÷流通股本), 并按实时价重算封板状态.
    历史: LEFT JOIN stock_daily 补收盘价/涨跌幅, 封板状态用最后一轮扫描的快照."""
    date = request.args.get('date') or datetime.now().strftime('%Y%m%d')
    today = datetime.now().strftime('%Y%m%d')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT * FROM limitup_collection WHERE trade_date = ? ORDER BY first_limit_time',
        (date,)).fetchall()
    items = [dict(r) for r in rows]
    if not items:
        conn.close()
        return jsonify({'date': date, 'items': [],
                        'stats': {'total': 0, 'sealed': 0, 'broken': 0}})
    codes = [r['ts_code'] for r in items]
    ph = ','.join('?' * len(codes))

    # 连板数: 从 date 往前按交易日数, 该 code 在合集里连续出现几天 (数据从上线日起积累, 首日全是首板)
    prev_rows = conn.execute(
        f'SELECT ts_code, trade_date FROM limitup_collection WHERE trade_date < ? AND ts_code IN ({ph})',
        [date] + codes).fetchall()
    code_dates = {}
    for r in prev_rows:
        code_dates.setdefault(r['ts_code'], set()).add(r['trade_date'])
    cal = [r[0] for r in conn.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date < ? ORDER BY cal_date DESC LIMIT 30',
        (date,)).fetchall()]
    for item in items:
        streak = 1
        ds = code_dates.get(item['ts_code'], set())
        for d in cal:
            if d in ds:
                streak += 1
            else:
                break
        item['streak'] = streak

    if date == today:
        try:
            quotes = fetch_sina_quotes(codes, cache_ttl=_quote_cache_ttl_from_request())
        except Exception as e:
            print(f'[lz] sina 实时拉取失败 (降级快照状态): {e}')
            quotes = {}
        # 流通股本 (换手率用; tushare float_share 单位万股, sina volume 单位手 → 手/万股 直接得换手率%)
        float_shares = {r[0]: r[1] for r in conn.execute(
            f'SELECT ts_code, float_share FROM stock_basic WHERE ts_code IN ({ph})', codes).fetchall()}
        for item in items:
            q = quotes.get(item['ts_code'])
            if not q:
                continue
            item['price'] = q.get('price')
            item['change_pct'] = q.get('change_pct')
            item['volume'] = q.get('volume')
            item['amount'] = q.get('amount')
            item['quote_time'] = q.get('time', '')
            fs = float_shares.get(item['ts_code'])
            if fs and q.get('volume'):
                item['turnover_pct'] = round(q['volume'] / fs, 2)
            # 按实时价重算封板状态 (比上一轮扫描快照更新)
            pc = q.get('prev_close')
            if pc and item.get('limit_pct') and q.get('price'):
                lp = _limit_up_price(pc, item['limit_pct'])
                item['sealed'] = 1 if q['price'] >= lp - 1e-6 else 0
                if not item['sealed']:
                    item['broken'] = 1
    else:
        dmap = {r[0]: r for r in conn.execute(
            f'SELECT ts_code, close, change, volume, amount FROM stock_daily WHERE trade_date = ? AND ts_code IN ({ph})',
            [date] + codes).fetchall()}
        for item in items:
            d = dmap.get(item['ts_code'])
            if d:
                item['price'] = d[1]
                item['change_pct'] = d[2]
                item['volume'] = d[3]
                item['amount'] = d[4] * 1000 if d[4] else None  # tushare 千元 → 元, 跟 sina 实时口径一致
    conn.close()

    # 板块标签 (复用 stock_sector_summary, 韭研涨停板块历史; 无记录的股票显示"近期未涨停")
    _attach_sector(items)

    sealed_n = sum(1 for i in items if i.get('sealed'))
    return jsonify({'date': date, 'items': items,
                    'stats': {'total': len(items), 'sealed': sealed_n, 'broken': len(items) - sealed_n}})


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
    输出: {ts_code: {price, prev_close, change_pct, high, ma5, ma10, ma20, ma30}}
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
            'high': float(q['high']) if q.get('high') is not None else None,
            'ma5': _ma(closes, 5),
            'ma10': _ma(closes, 10),
            'ma20': _ma(closes, 20),
            'ma30': _ma(closes, 30),
            'name': q.get('name', ''),
            'time': q.get('time', ''),
        }
    conn.close()
    return jsonify(result)


@app.route('/api/picks/ths/quote', methods=['GET'])
def api_picks_ths_quote():
    """同花顺选股推荐专用 quote 端点: 行为同 /api/picks/quote (实时价 + 重算 MA).
    独立路由是为了前端 ths sub-tab 可以按 group 路由, 不污染 EM 数据缓存."""
    # 共用 sina 全局缓存 (cross-tab 30s 内同股只拉一次, 见 fetch_sina_quotes 实现)
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if c.strip()][:200]
    if not codes:
        return jsonify({})
    quotes = fetch_sina_quotes(codes, cache_ttl=_quote_cache_ttl_from_request())
    if not quotes:
        return jsonify({})

    conn = sqlite3.connect(DB_PATH, timeout=30)
    result = {}
    for ts_code, q in quotes.items():
        if not q or q.get('price') is None:
            continue
        rows = conn.execute("""
            SELECT trade_date, close FROM stock_daily
            WHERE ts_code = ? ORDER BY trade_date DESC LIMIT 31
        """, (ts_code,)).fetchall()
        closes = [r[1] for r in rows[1:31] if r[1] is not None]
        closes = list(reversed(closes))[-30:]
        if not closes:
            continue
        closes.append(float(q['price']))
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
            (trade_date, ts_code, code, name,
             main_net_inflow, main_net_pct,
             super_net, super_pct, big_net, big_pct,
             mid_net, mid_pct, small_net, small_pct, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (td, ts_code, code, r.get('name', ''),
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






# ── 大盘实时资金流向 (沪深合计, 东财 push2 分钟K线) ──
# 与 data.eastmoney.com/zjlx/dpzjlx.html "实时资金流向图(净流入)" 同口径:
# 上证指数(1.000001) + 深证成指(0.399001) 的分钟资金流按时间对齐相加,
# 净比 = 净流入 / 两市成交额 (f48). 已验证与网页数值完全一致.
# 注意: 两市必须都拉到才能出数, 单边数据(如仅上证 -815.91亿)是错的, 宁可 502.
#
# 数据流: scheduler 15:35 拉一次 → 落 market_fflow 表 → endpoint 直接查表.
# 不做盘中实时: 主力净流入 tab 整体是历史回看, 实时数字对决策无意义, 还能省一波反爬请求.

_EM_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Referer': 'https://data.eastmoney.com/',
    'Accept': '*/*',
}

# app.py 顶部全局禁了 requests 代理 (保 AKShare), 所以这里的请求默认全直连.
# 但本机直连 push2 会被东财反爬掐 (RemoteDisconnected), 走本地代理反而通.
# 策略: 先直连, 失败兜底走 Clash 代理 (proxies= 参数级覆盖, 不受全局 patch 影响).
_EM_LOCAL_PROXY = 'http://127.0.0.1:7897'


def _em_get_json(url, params, tag=''):
    """GET json: 直连 2 次 (快速失败) → 本地代理兜底 2 次. 全失败返回 None.
    直连重试少是有意的: 被反爬掐时重试也通不过, 赶紧转代理, 压住冷缓存延迟."""
    for attempt in range(2):
        try:
            return _requests.get(url, params=params, headers=_EM_HEADERS, timeout=8).json()
        except Exception as e:
            print(f'[mkt fflow] {tag} direct attempt {attempt+1}/2 failed: {e}', flush=True)
        time.sleep(1 + attempt * 2)  # 1s, 3s
    for attempt in range(2):
        try:
            return _requests.get(url, params=params, headers=_EM_HEADERS, timeout=8,
                                 proxies={'http': _EM_LOCAL_PROXY, 'https': _EM_LOCAL_PROXY}).json()
        except Exception as e:
            print(f'[mkt fflow] {tag} proxy attempt {attempt+1}/2 failed: {e}', flush=True)
        time.sleep(2)
    return None


def _em_fflow_minline(secid, end_date=None):
    """东财指数分钟资金流 (09:31~15:00 累计净流入). end_date='YYYYMMDD' 拉历史某日, 缺省 = 今天."""
    url = 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get'
    params = dict(secid=secid, klt=1, lmt=0,
                  fields1='f1,f2,f3,f7', fields2='f51,f52,f53,f54,f55,f56')
    if end_date:
        params['end'] = end_date
    d = _em_get_json(url, params, tag=secid)
    return d.get('data') if d and d.get('data') and d['data'].get('klines') else None


def _em_index_amount(secid, end_date=None):
    """东财指数当日成交额 f48 (元). 失败返回 0."""
    d = _em_get_json('https://push2.eastmoney.com/api/qt/stock/get',
                     dict(secid=secid, fields='f48'), tag=secid + ' amt')
    try:
        return float((d.get('data') or {}).get('f48') or 0) if d else 0
    except (ValueError, TypeError):
        return 0


def _build_market_fflow_payload(end_date=None):
    """拉两市 push2 分钟资金流 → 合并 → 算 amount/last/pct. 返回 payload dict 或 None.
    end_date='YYYYMMDD' 拉历史某日 (用于回溯补盘); 缺省 = 今天.
    两市必须都拉到才出数, 单边是错误口径."""
    sh = _em_fflow_minline('1.000001', end_date=end_date)
    sz = _em_fflow_minline('0.399001', end_date=end_date)
    if not (sh and sz):
        return None

    # 按时间对齐相加 (两市 bar 数正常一致, dict 对齐兜底单边缺 bar)
    merged = {}  # 'HH:MM' -> [main, small, mid, big, super]
    date = ''
    for d in (sh, sz):
        for line in d['klines']:
            p = line.split(',')
            if len(p) < 6:
                continue
            if not date:
                date = p[0][:10]
            vals = merged.setdefault(p[0][-5:], [0.0] * 5)
            for i in range(5):
                try:
                    vals[i] += float(p[i + 1] or 0)
                except ValueError:
                    pass
    times = sorted(merged.keys())
    amount = _em_index_amount('1.000001') + _em_index_amount('0.399001')
    keys = ['main', 'small', 'mid', 'big', 'super']
    series = {k: [merged[t][i] for t in times] for i, k in enumerate(keys)}
    last = {k: (series[k][-1] if series[k] else 0.0) for k in keys}
    pct = {k: (last[k] / amount * 100 if amount else None) for k in keys}

    return {
        'date': date,          # 'YYYY-MM-DD'
        'times': times,        # ['09:31', ..., '15:00']
        'series': series,      # 单位: 元, 累计值
        'amount': amount,      # 两市成交额 (元)
        'last': last,
        'pct': pct,
        'complete': bool(times and times[-1] == '15:00'),  # 收盘定格标记
    }


def _save_market_fflow(payload, trade_date=None):
    """把 payload 写 market_fflow 表 (按 trade_date upsert).
    trade_date 缺省 = 从 payload['date'] 推 (YYYY-MM-DD → YYYYMMDD)."""
    if not payload or not payload.get('date'):
        return False
    td = (trade_date or payload['date']).replace('-', '')  # 'YYYYMMDD'
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute('''INSERT OR REPLACE INTO market_fflow
            (trade_date, times, series, amount, last, pct, complete, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))''',
            (td,
             json.dumps(payload['times']),
             json.dumps(payload['series']),
             payload['amount'],
             json.dumps(payload['last']),
             json.dumps(payload['pct']),
             1 if payload.get('complete') else 0))
        conn.commit()
        return True
    finally:
        conn.close()


@app.route('/api/market/fflow', methods=['GET'])
def api_market_fflow():
    """大盘实时资金流向 (沪深合计). 不做盘中实时, 只读 market_fflow 落盘数据.
    ?date=YYYYMMDD (默认 = 最新已落盘日期). 表里没数据返 {empty: true}."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        date = (request.args.get('date') or '').strip()
        if date:
            row = conn.execute(
                'SELECT * FROM market_fflow WHERE trade_date = ?', (date,)
            ).fetchone()
        else:
            row = conn.execute(
                'SELECT * FROM market_fflow ORDER BY trade_date DESC LIMIT 1'
            ).fetchone()
        if not row:
            return jsonify({'empty': True, 'date': date})
        return jsonify({
            'date': f"{row['trade_date'][:4]}-{row['trade_date'][4:6]}-{row['trade_date'][6:8]}",
            'times': json.loads(row['times']),
            'series': json.loads(row['series']),
            'amount': row['amount'],
            'last': json.loads(row['last']),
            'pct': json.loads(row['pct']),
            'complete': bool(row['complete']),
        })
    finally:
        conn.close()


@app.route('/api/market/fflow/daily', methods=['GET'])
def api_market_fflow_daily():
    """大盘 5档资金流向 - 逐日趋势 (沪深合计).
    入参: ?days=N (默认 60, 范围 7~250).
    出参: {dates, amount, series{5档}, pct{5档}}. 5档 = 主力/超大单/大单/中单/小单.
    series[k][i] = 第 i 日 5档净额 (元); pct[k][i] = 该档净额 / 当日两市成交额 * 100 (%).
    数据源: market_fflow.last (5档) + market_fflow.amount (元). 跨源一致, 都是大盘直推, 不混 fund_flow 全市场聚合."""
    days = max(7, min(int(request.args.get('days', 60)), 250))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('''
            SELECT trade_date, last, pct, amount
            FROM market_fflow
            ORDER BY trade_date DESC LIMIT ?
        ''', (days,)).fetchall()
        if not rows:
            return jsonify({'empty': True})
        rows = list(reversed(rows))  # 升序返回
        keys = ['main', 'super', 'big', 'mid', 'small']
        series, pct, dates, amounts = {}, {}, [], []
        for r in rows:
            try:
                last_obj = json.loads(r['last']) if isinstance(r['last'], str) else (r['last'] or {})
                pct_obj  = json.loads(r['pct'])  if isinstance(r['pct'],  str) else (r['pct']  or {})
            except (ValueError, TypeError):
                continue
            dates.append(r['trade_date'])
            amounts.append(r['amount'])
            for k in keys:
                series.setdefault(k, []).append(last_obj.get(k))
                pct.setdefault(k, []).append(pct_obj.get(k))
        if not dates:
            return jsonify({'empty': True})
        return jsonify({
            'dates': [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in dates],
            'amount': amounts,
            'series': series,
            'pct': pct,
        })
    finally:
        conn.close()


@app.route('/api/stock/<ts_code>/fflow/daily', methods=['GET'])
def api_stock_fflow_daily(ts_code):
    """个股 5档资金流向 - 逐日趋势. 出参格式跟大盘版一致 (dates/series/pct),
    额外带 name/code/amount/close/change_pct 给弹窗标题和 tooltip 用.
    数据源: fund_flow (东财 push2 个股资金流). 同花顺表 fund_flow_ths 没 5档, 不接.
    days 默认 60 (fund_flow 最早 0420, 满打满算 ~100 天, 默认窗口仍设 60)."""
    days = max(7, min(int(request.args.get('days', 60)), 250))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('''
            SELECT f.trade_date, f.name, f.code,
                   d.close, d.change, d.amount,
                   f.main_net_inflow  AS main,  f.main_net_pct  AS main_pct,
                   f.super_net        AS super, f.super_pct    AS super_pct,
                   f.big_net          AS big,   f.big_pct      AS big_pct,
                   f.mid_net          AS mid,   f.mid_pct      AS mid_pct,
                   f.small_net        AS small, f.small_pct    AS small_pct
            FROM fund_flow f
            LEFT JOIN stock_daily d
              ON d.ts_code = f.ts_code AND d.trade_date = f.trade_date
            WHERE f.ts_code = ?
            ORDER BY f.trade_date DESC LIMIT ?
        ''', (ts_code, days)).fetchall()
        if not rows:
            return jsonify({'empty': True, 'ts_code': ts_code})
        rows = list(reversed(rows))  # 升序
        keys = ['main', 'super', 'big', 'mid', 'small']
        series, pct, amounts, closes, changes = {}, {}, [], [], []
        dates, name = [], rows[0]['name']
        # 算 change_pct: tushare 的 d.change 是绝对涨幅(元), change_pct = (close-pre_close)/pre_close
        # 前一日 close 要从下一行拿 (row 是升序, i-1 是前一天)
        for i, r in enumerate(rows):
            dates.append(r['trade_date'])
            amounts.append(r['amount'])           # 元
            closes.append(r['close'])
            if r['change'] is not None and r['close'] is not None:
                pre_close = r['close'] - r['change']
                changes.append(round(r['change'] / pre_close * 100, 2) if pre_close else None)
            else:
                changes.append(None)
            for k in keys:
                series.setdefault(k, []).append(r[k])
                pct.setdefault(k, []).append(r[k + '_pct'])
        return jsonify({
            'ts_code': ts_code,
            'code': rows[0]['code'] or ts_code.split('.')[0],
            'name': name,
            'dates': [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in dates],
            'amount': amounts,     # 元
            'close': closes,
            'change_pct': changes,
            'series': series,      # 元
            'pct': pct,            # 百分数 (已是 *100 后的 %)
        })
    finally:
        conn.close()


@app.route('/api/market/fflow/sync', methods=['POST'])
def api_market_fflow_sync():
    """手动同步今日大盘资金流. push2 不支持历史 (已验证), 所以只拉 today.
    失败返 502; 成功返 {trade_date, complete, times_count}.
    用法: 用户在主力净流入 tab 空状态点"立即刷新"按钮触发."""
    from datetime import datetime
    today = datetime.now().strftime('%Y%m%d')
    try:
        payload = _build_market_fflow_payload()
        if not payload:
            return jsonify({'error': 'push2 unavailable', 'trade_date': today}), 502
        _save_market_fflow(payload)
        return jsonify({
            'trade_date': today,
            'complete': bool(payload.get('complete')),
            'times_count': len(payload.get('times') or []),
        })
    except Exception as e:
        return jsonify({'error': str(e), 'trade_date': today}), 500


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


@app.route('/api/flow/stock-count', methods=['GET'])
def api_flow_stock_count():
    """某交易日某数据源录入了多少只个股的主力净流入.
    用于前端在主力净流入 tab 顶部显示当日入库完整度 (正常 ~5300, 偏低说明抓取残缺).
    """
    # 默认东财口径 (两个源), 否则 6/16 之前会报 0 只
    source = (request.args.get('source') or 'eastmoney').strip()
    date = (request.args.get('date') or '').strip()
    if not re.fullmatch(r'\d{8}', date):
        return jsonify({'error': 'date must be YYYYMMDD'}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # source='eastmoney' 时统计两个源; 指定具体源则只数那一个
        if source == 'eastmoney':
            row = conn.execute(
                "SELECT COUNT(*) AS stocks, COUNT(DISTINCT ts_code) AS uniq_stocks, "
                "       MIN(updated_at) AS first_seen, MAX(updated_at) AS last_seen "
                "FROM fund_flow WHERE trade_date=? AND source IN (?, ?)",
                (date,) + EM_FLOW_SOURCES,
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS stocks, COUNT(DISTINCT ts_code) AS uniq_stocks, "
                "       MIN(updated_at) AS first_seen, MAX(updated_at) AS last_seen "
                "FROM fund_flow WHERE trade_date=? AND source=?",
                (date, source),
            ).fetchone()
        return jsonify({
            'date': date,
            'source': source,
            'stocks': row['stocks'] or 0,
            'uniq_stocks': row['uniq_stocks'] or 0,
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen'],
            'is_low': (row['stocks'] or 0) < 4500,  # 正常 ~5300, 低于 4500 提示数据不完整
        })
    finally:
        conn.close()


@app.route('/api/flow/market', methods=['GET'])
def api_flow_market():
    """全市场主力净流入排行. 支持两种口径:
      - period=today (默认): 每只股票最新一日的 main_net_inflow, 兼容旧行为.
      - period=3d/5d/10d/20d: SQL 聚合最近 N 个交易日的主力气净额, limit 100, 按 sum_main_net desc.
    支持 ?date=YYYYMMDD 指定某日 (today 模式才生效).
    默认 source=eastmoney-push2 (Skill 抓取的数据). 传 source=all 看全部来源.
    """
    period = request.args.get('period', 'today').lower().strip()
    period_map = {'today': 1, '3d': 3, '5d': 5, '10d': 10, '20d': 20, '30d': 30, '60d': 60, '90d': 90}
    if period not in period_map:
        period = 'today'
    period_days = period_map[period]

    # today 模式兼容老 days 参数; Nd 模式忽略 days
    days = min(max(int(request.args.get('days', 1)), 1), 200)
    if period != 'today':
        days = period_days
    sort = request.args.get('sort', '').strip()
    order = 'desc' if request.args.get('order', 'desc').lower() == 'desc' else 'asc'
    market = request.args.get('market', 'all').lower()
    search = request.args.get('search', '').strip()
    if period == 'today':
        limit = min(int(request.args.get('limit', 200)), 10000)
    else:
        limit = min(int(request.args.get('limit', 200)), 10000)
    date = request.args.get('date', '').strip()  # 指定日期 (YYYYMMDD, today 模式才生效)
    source = request.args.get('source', 'all').strip()  # 默认包含全部来源 (push2 + daykline + tushare), 便于回看历史

    # 排序白名单: today 模式 vs Nd 模式字段不同
    if period == 'today':
        sort_whitelist = {'main_net_inflow', 'main_net_pct', 'change_pct', 'close'}
        if not sort or sort not in sort_whitelist:
            sort = 'main_net_inflow'
    else:
        sort_whitelist = {'sum_main_net', 'sum_super_net', 'sum_big_net', 'sum_mid_net',
                          'sum_small_net', 'change_pct', 'close', 'flow_days'}
        if not sort or sort not in sort_whitelist:
            sort = 'sum_main_net'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if period == 'today':
        # ===== today 模式: 每只股票最新一日的 main_net_inflow (兼容旧行为) =====
        # close / change_pct 改为 JOIN stock_daily (fund_flow 这两列已弃用, 走权威源)
        if date:
            if source == 'all':
                rows = conn.execute("""
                    SELECT f.trade_date, f.ts_code, f.code, f.name,
                           sd.close AS close, sd.change AS change_pct,
                           f.main_net_inflow, f.main_net_pct,
                           f.super_net, f.big_net, f.mid_net, f.small_net, f.source
                    FROM fund_flow f
                    LEFT JOIN stock_daily sd
                      ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
                    WHERE f.trade_date = ?
                """, (date,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT f.trade_date, f.ts_code, f.code, f.name,
                           sd.close AS close, sd.change AS change_pct,
                           f.main_net_inflow, f.main_net_pct,
                           f.super_net, f.big_net, f.mid_net, f.small_net, f.source
                    FROM fund_flow f
                    LEFT JOIN stock_daily sd
                      ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
                    WHERE f.trade_date = ? AND f.source = ?
                """, (date, source)).fetchall()
        else:
            if source == 'all':
                rows = conn.execute("""
                    SELECT f.trade_date, f.ts_code, f.code, f.name,
                           sd.close AS close, sd.change AS change_pct,
                           f.main_net_inflow, f.main_net_pct,
                           f.super_net, f.big_net, f.mid_net, f.small_net, f.source
                    FROM fund_flow f
                    INNER JOIN (
                        SELECT ts_code, MAX(trade_date) AS max_date
                        FROM fund_flow
                        WHERE trade_date >= strftime('%Y%m%d', date('now', '-' || ? || ' days', 'localtime'))
                        GROUP BY ts_code
                    ) m ON f.ts_code = m.ts_code AND f.trade_date = m.max_date
                    LEFT JOIN stock_daily sd
                      ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
                """, (days,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT f.trade_date, f.ts_code, f.code, f.name,
                           sd.close AS close, sd.change AS change_pct,
                           f.main_net_inflow, f.main_net_pct,
                           f.super_net, f.big_net, f.mid_net, f.small_net, f.source
                    FROM fund_flow f
                    INNER JOIN (
                        SELECT ts_code, MAX(trade_date) AS max_date
                        FROM fund_flow
                        WHERE trade_date >= strftime('%Y%m%d', date('now', '-' || ? || ' days', 'localtime'))
                          AND source = ?
                        GROUP BY ts_code
                    ) m ON f.ts_code = m.ts_code AND f.trade_date = m.max_date
                    LEFT JOIN stock_daily sd
                      ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
                """, (days, source)).fetchall()
    else:
        # ===== Nd 模式: SQL 聚合最近 N 个交易日, 按 sum_main_net desc, limit 200 =====
        # 关键: 窗口是"近 N 个交易日", 不是"近 N 个日历日". 用 trading_dates_cache 表
        # 按 cal_date 倒序取第 N 个 (= OFFSET N-1), >= 那个日期的所有 fund_flow 行都算入.
        source_clause = "" if source == 'all' else "AND source = ?"
        source_params = () if source == 'all' else (source,)
        rows = conn.execute(f"""
            SELECT
                ts_code, code, name,
                SUM(main_net_inflow) AS sum_main_net,
                SUM(super_net)       AS sum_super_net,
                SUM(big_net)         AS sum_big_net,
                SUM(mid_net)         AS sum_mid_net,
                SUM(small_net)       AS sum_small_net,
                COUNT(*)             AS flow_days,
                MIN(trade_date)      AS start_date,
                MAX(trade_date)      AS end_date,
                (SELECT close FROM stock_daily
                 WHERE ts_code = f.ts_code
                 ORDER BY trade_date DESC LIMIT 1) AS close,
                (SELECT change FROM stock_daily
                 WHERE ts_code = f.ts_code
                 ORDER BY trade_date DESC LIMIT 1) AS change_pct
            FROM fund_flow f
            WHERE trade_date >= (
                SELECT cal_date FROM trading_dates_cache
                WHERE cal_date <= strftime('%Y%m%d', date('now', 'localtime'))
                ORDER BY cal_date DESC
                LIMIT 1 OFFSET ?
            )
              {source_clause}
            GROUP BY ts_code
            ORDER BY sum_main_net DESC
            LIMIT ?
        """, (period_days - 1, *source_params, limit)).fetchall()
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

    # 排序 (Nd 模式 SQL 已经按 sum_main_net desc, 这里尊重用户二次排序)
    if period == 'today' or sort != 'sum_main_net':
        rows.sort(key=lambda r: (r[sort] or 0) if sort in r.keys() else 0, reverse=(order == 'desc'))

    out = [dict(r) for r in rows[:limit]]
    _attach_sector(out, key='ts_code')  # 补 sector_history
    return jsonify({
        'count': len(out),
        'total_matched': len(rows),
        'days': days,
        'date': date,
        'period': period,
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
        SELECT f.trade_date, f.ts_code, f.code, f.name,
               sd.close AS close, sd.change AS change_pct,
               f.main_net_inflow, f.main_net_pct,
               f.super_net, f.big_net, f.mid_net, f.small_net
        FROM fund_flow f
        LEFT JOIN stock_daily sd
          ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
        WHERE f.ts_code IN ({placeholders})
          AND f.trade_date >= date('now', '-' || ? || ' days', 'localtime')
        ORDER BY f.ts_code, f.trade_date DESC
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
        SELECT f.trade_date,
               sd.close AS close, sd.change AS change_pct,
               f.main_net_inflow, f.main_net_pct,
               f.super_net, f.super_pct, f.big_net, f.big_pct,
               f.mid_net, f.mid_pct, f.small_net, f.small_pct, f.name
        FROM fund_flow f
        LEFT JOIN stock_daily sd
          ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
        WHERE f.ts_code=? AND f.trade_date >= date('now', '-' || ? || ' days', 'localtime')
        ORDER BY f.trade_date DESC
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
            SELECT f.trade_date,
                   sd.close AS close, sd.change AS change_pct,
                   f.main_net_inflow, f.main_net_pct,
                   f.super_net, f.super_pct, f.big_net, f.big_pct,
                   f.mid_net, f.mid_pct, f.small_net, f.small_pct, f.name
            FROM fund_flow f
            LEFT JOIN stock_daily sd
              ON sd.ts_code = f.ts_code AND sd.trade_date = f.trade_date
            WHERE f.ts_code=? AND f.trade_date >= date('now', '-' || ? || ' days', 'localtime')
            ORDER BY f.trade_date DESC
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
    'is_already_synced': False, 'is_holiday': False, 'actual_date': '',
    # wait_for_user 模式 (2026-08-18): 弹浏览器等用户刷新
    'waiting_for_user': False, 'wait_elapsed_sec': 0, 'wait_remaining_sec': 0,
}

# 主力净流入 sync 状态文件路径, 跟 _em_sync_market_bg 起的子进程共享
# (子进程不能直接改 _AKSYNC_STATE, 用 JSON 文件中转)
_FLOW_SYNC_STATE_FILE = '/tmp/tradeiq-flow-sync-state.json'


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
                (trade_date, ts_code, code, name,
                 main_net_inflow, main_net_pct,
                 super_net, super_pct, big_net, big_pct,
                 mid_net, mid_pct, small_net, small_pct, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (r['trade_date'], r['ts_code'], r['code'], r['name'],
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


def _em_sync_market_bg(force=False, wait_for_user=False):
    """后台线程: 抓取今日主力净流入 (JSONP + 东财 detail.html).
    数据写入 fund_flow 表, trade_date = 今天. 每天点一次同步即积累一天历史.
    历史日期查询通过前端日历切换 (只是查 DB, 不重新抓).
    force=True → skip_if_exists 关闭, 已有数据也重抓覆盖 (手动点同步按钮的场景).
    注意不要把 force 透传给 fetch_today_market — 那个 force 会连交易日归属判断一起跳过,
    节日/凌晨抓的数据会错标成今天.

    实现: 子进程模式 (2026-10-08)
      - 后端跑在 launchd Aqua session, 直接 Playwright launch Chrome 被 macOS 拒 (TargetClosedError)
      - 用 launchctl asuser 把子进程送进用户 GUI session 跑 Playwright
      - 子进程通过 _FLOW_SYNC_STATE_FILE 跟父进程共享进度
      - 父进程只负责 spawn + 监听子进程退出
    """
    import time as _t
    import subprocess as _sp

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
    # 清掉旧 state file, 让前端从 "started" 状态开始
    try:
        if os.path.exists(_FLOW_SYNC_STATE_FILE):
            os.remove(_FLOW_SYNC_STATE_FILE)
    except Exception:
        pass
    print(f'[em-sync] 开始: 同步 {today}' + (' (强制刷新, 忽略已有数据)' if force else ''), flush=True)

    # 旧的内联 Playwright 逻辑被搬到 _sync_market_runner.py (子进程模式).
    # 这里改成: 父线程 spawn 一个子进程, 通过 launchctl asuser 送进 GUI session 跑 Playwright.
    # 子进程通过 _FLOW_SYNC_STATE_FILE 写进度, 父进程在 _em_sync_market_bg_watch 线程里 poll.
    _em_sync_market_bg_spawn(force, wait_for_user, today, t0)


def _em_sync_market_bg_spawn(force: bool, wait_for_user: bool, today: str, t0: float):
    """通过 HTTP 触发独立 launchd agent 跑主力净流入 sync.

    Plan C: 同步逻辑跑在 sync_agent.py (独立 launchd job, GUI session 借据由 launchd 注入,
    不继承 Flask 6h 后的过期 sandbox). Flask 只负责 trigger + 读 state file.
    """
    import urllib.request as _ur
    import urllib.error as _ue
    import socket as _so
    import time as _t
    AGENT_URL = os.environ.get('TRADEIQ_AGENT_URL', 'http://127.0.0.1:5556')
    payload = json.dumps({
        'force': bool(force),
        'wait_for_user': bool(wait_for_user),
        'state_file': _FLOW_SYNC_STATE_FILE,
    }).encode()
    try:
        req = _ur.Request(f'{AGENT_URL}/sync/em', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with _ur.urlopen(req, timeout=3) as r:
            resp = json.loads(r.read())
        _AKSYNC_STATE['current_code'] = f'agent 已接收 (pid={resp.get("pid")})'
        print(f'[em-sync] 触发 agent 成功: {resp}', flush=True)
    except Exception as e:
        err = f'agent 触发失败 (syncagent 没起来?): {type(e).__name__}: {str(e)[:200]}'
        _AKSYNC_STATE['errors'].append(('agent', err))
        _AKSYNC_STATE['failed'] = 1
        _AKSYNC_STATE['running'] = False
        _AKSYNC_STATE['finished_at'] = datetime.now().isoformat()
        _AKSYNC_STATE['current_code'] = err
        print(f'[em-sync] {err}', flush=True)
        return

    # 后台 watcher: 轮询 state file, 同步到 _AKSYNC_STATE
    # agent 是独立 daemon, 这里没有 proc 句柄, 改成 mtime + running 标志判断卡死
    def _watcher():
        last_mtime = None
        for _ in range(60 * 30):  # 最多等 30 分钟
            _t.sleep(1.0)
            state = None
            try:
                if os.path.exists(_FLOW_SYNC_STATE_FILE):
                    mtime = os.path.getmtime(_FLOW_SYNC_STATE_FILE)
                    with open(_FLOW_SYNC_STATE_FILE, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    if last_mtime and mtime == last_mtime and state and state.get('running'):
                        # 5 分钟没动 + 仍在跑 = 卡死
                        if (time.time() - mtime) > 300:
                            err = 'state 文件 >5min 没动且 running=true, agent 卡了/死了'
                            _AKSYNC_STATE['errors'].append(('agent-stuck', err))
                            _AKSYNC_STATE['failed'] = max(_AKSYNC_STATE.get('failed', 0), 1)
                            _AKSYNC_STATE['running'] = False
                            _AKSYNC_STATE['finished_at'] = datetime.now().isoformat()
                            _AKSYNC_STATE['current_code'] = f'⚠ {err}'
                            print(f'[em-sync] {err}', flush=True)
                            # kickstart agent
                            try:
                                import subprocess as _sp
                                _sp.run(['launchctl', 'kickstart', '-k',
                                         f'gui/{os.getuid()}/com.tradeiq.syncagent'],
                                        check=False, timeout=3)
                            except Exception:
                                pass
                            return
                    last_mtime = mtime
            except Exception:
                pass
            if state:
                for k in ('total', 'done', 'success', 'failed', 'current_code',
                          'elapsed_sec', 'is_already_synced', 'is_holiday',
                          'actual_date', 'waiting_for_user',
                          'wait_elapsed_sec', 'wait_remaining_sec', 'source',
                          'target_date', 'errors'):
                    if k in state:
                        _AKSYNC_STATE[k] = state[k]
            # running=False 即视为结束
            if state and not state.get('running') and state.get('finished_at'):
                _AKSYNC_STATE['running'] = False
                _AKSYNC_STATE['finished_at'] = state['finished_at']
                _AKSYNC_STATE['elapsed_sec'] = state.get('elapsed_sec', _AKSYNC_STATE.get('elapsed_sec', 0))
                print(f'[em-sync] 完成 success={_AKSYNC_STATE.get("success")} '
                      f'failed={_AKSYNC_STATE.get("failed")} elapsed={_AKSYNC_STATE["elapsed_sec"]}s', flush=True)
                return
        # 30 分钟兜底
        _AKSYNC_STATE['current_code'] = '⚠ 父进程观察 30min 兜底退出'
        _AKSYNC_STATE['running'] = False
        _AKSYNC_STATE['finished_at'] = datetime.now().isoformat()

    import threading as _th
    _th.Thread(target=_watcher, daemon=True).start()




@app.route('/api/flow/dates', methods=['GET'])
def api_flow_dates():
    """返回 fund_flow 表里有数据的 distinct trade_date 列表 (倒序).
    默认东财口径 (push2 + daykline 两个源, 覆盖 6/16 前后的全部区间).
    用于前端日历控件的上一日/下一日导航.
    """
    source = request.args.get('source', 'eastmoney').strip()
    conn = sqlite3.connect(DB_PATH)
    if source == 'all':
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM fund_flow ORDER BY trade_date DESC"
        ).fetchall()]
    elif source in EM_FLOW_SOURCES or source == 'eastmoney':
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM fund_flow WHERE source IN (?, ?) "
            "ORDER BY trade_date DESC",
            EM_FLOW_SOURCES
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
    ?force=1 (或 body.force) → 忽略"已有数据"守卫, 强制重抓覆盖 (手动点同步按钮).
    ?wait_for_user=1 → 第 1 页拿不到时弹出浏览器窗口让用户手动 Cmd+R 刷新 (手动同步专用, 2026-08-18 加).
    不带 force (页面加载的自动同步) → 幂等, 已有数据就跳过.
    """
    if _AKSYNC_STATE['running']:
        return jsonify({'status': 'already_running', 'state': _AKSYNC_STATE})
    _force = (request.args.get('force') in ('1', 'true', 'True')
              or bool((request.get_json(silent=True) or {}).get('force')))
    _wait_for_user = (request.args.get('wait_for_user') in ('1', 'true', 'True')
                      or bool((request.get_json(silent=True) or {}).get('wait_for_user')))
    import threading as _th
    t = _th.Thread(target=_em_sync_market_bg, args=(_force, _wait_for_user), daemon=True)
    t.start()
    return jsonify({'status': 'started', 'force': _force, 'wait_for_user': _wait_for_user, 'state': _AKSYNC_STATE})


@app.route('/api/flow/sync-status', methods=['GET'])
def api_flow_sync_status():
    """查询 AKShare 全市场拉取状态. 前端 setInterval 轮询."""
    return jsonify(_AKSYNC_STATE)


# ── 东方财富板块资金流 (行业 + 概念, push2 clist/get) ──
# 跟 _em_sync_market_bg 同模式, 但走 sector_fund_flow 表.
# sector_type 区分行业/概念, 各自一次 sync 各自一次 status 推一份 _BKSYNC_STATE (共享).
# 行业先跑 (15:35), 概念后跑 (15:36), 错开避免 push2 同时段被双调用限流.
_BKSYNC_STATE = {
    'running': False, 'started_at': None, 'finished_at': None,
    'total': 0, 'done': 0, 'success': 0, 'failed': 0,
    'sector_type': '',                    # 当前跑的 hy / gn (空 = 没在跑)
    'errors': [], 'current_code': '', 'elapsed_sec': 0,
    'source': 'eastmoney-push2', 'target_date': '',
    'is_already_synced': False, 'is_holiday': False, 'actual_date': '',
    'waiting_for_user': False, 'wait_elapsed_sec': 0, 'wait_remaining_sec': 0,
}


def _save_bk_fund_flow_rows(rows, trade_date, sector_type):
    """写 sector_fund_flow 表. 实现在 fetch_fund_flow_sector.save_rows ——
    agent 进程那边也调同一个实现, 别在这里再抄一份字段映射."""
    from fetch_fund_flow_sector import save_rows
    return save_rows(rows, trade_date, sector_type)


def _bk_sync_market_bg(sector_type='industry', force=False, wait_for_user=False):
    """后台线程: 抓取今日板块资金流 (行业/概念) → sector_fund_flow 表.

    浏览器必须在 GUI session 里起: Flask 进程内 launch Chrome 会被 macOS 直接拒掉
    (TargetClosedError: Target page, context or browser has been closed),
    所以这块走独立 launchd agent (sync_agent.py, 5556 端口) 跑, Flask 只负责
    trigger + 轮询 state file —— 跟个股主力净流入 (_em_sync_market_bg) 同一套架构.
    """
    import time as _t
    import threading as _th
    import urllib.request as _ur

    t0 = _t.time()
    today = datetime.now().strftime('%Y%m%d')
    state_file = f'/tmp/tradeiq-sector-sync-{sector_type}.json'

    _BKSYNC_STATE.update({
        'running': True, 'started_at': datetime.now().isoformat(),
        'finished_at': None, 'total': 0, 'done': 0,
        'success': 0, 'failed': 0, 'errors': [],
        'sector_type': sector_type,
        'current_code': f'准备同步 {sector_type}/{today}...',
        'elapsed_sec': 0, 'source': 'eastmoney-push2',
        'target_date': today,
        'is_already_synced': False, 'is_holiday': False, 'actual_date': '',
        'waiting_for_user': False, 'wait_elapsed_sec': 0, 'wait_remaining_sec': 0,
    })
    # 清掉旧 state file, 让前端从「已启动」开始看
    try:
        if os.path.exists(state_file):
            os.remove(state_file)
    except Exception:
        pass
    print(f'[bk-sync] 开始: {sector_type}/{today}' + (' (强制刷新)' if force else ''), flush=True)

    agent_url = os.environ.get('TRADEIQ_AGENT_URL', 'http://127.0.0.1:5556')
    payload = json.dumps({'sector_type': sector_type, 'force': bool(force),
                          'state_file': state_file,
                          'wait_for_user': bool(wait_for_user)}).encode()
    try:
        req = _ur.Request(f'{agent_url}/sync/sector', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with _ur.urlopen(req, timeout=3) as r:
            resp = json.loads(r.read())
        _BKSYNC_STATE['current_code'] = f'agent 已接收 (pid={resp.get("pid")})'
        print(f'[bk-sync] 触发 agent 成功: {resp}', flush=True)
    except Exception as e:
        err = f'agent 触发失败 (syncagent 没起来?): {type(e).__name__}: {str(e)[:200]}'
        _BKSYNC_STATE['errors'].append((f'bk-{sector_type}', err))
        _BKSYNC_STATE['failed'] = 1
        _BKSYNC_STATE['running'] = False
        _BKSYNC_STATE['finished_at'] = datetime.now().isoformat()
        _BKSYNC_STATE['current_code'] = err
        print(f'[bk-sync] {err}', flush=True)
        return

    def _watcher():
        # 最多等 2 小时: clist 拿不到时 agent 会兜底走 daykline 通道 (1000 个板块 ≈ 80 分钟)
        for _ in range(60 * 120):
            _t.sleep(1.0)
            state = None
            try:
                if os.path.exists(state_file):
                    with open(state_file, 'r', encoding='utf-8') as f:
                        state = json.load(f)
            except Exception:
                state = None
            if state:
                for k in ('total', 'done', 'success', 'failed', 'current_code',
                          'elapsed_sec', 'is_already_synced', 'is_holiday',
                          'actual_date', 'waiting_for_user', 'wait_elapsed_sec',
                          'wait_remaining_sec', 'source', 'target_date', 'errors'):
                    if k in state:
                        _BKSYNC_STATE[k] = state[k]
            if state and not state.get('running') and state.get('finished_at'):
                _BKSYNC_STATE['running'] = False
                _BKSYNC_STATE['finished_at'] = state['finished_at']
                _BKSYNC_STATE['elapsed_sec'] = state.get(
                    'elapsed_sec', _BKSYNC_STATE.get('elapsed_sec', 0))
                print(f'[bk-sync] 完成: {sector_type}/{state.get("actual_date", today)} '
                      f'success={_BKSYNC_STATE["success"]} '
                      f'elapsed={_BKSYNC_STATE["elapsed_sec"]}s', flush=True)
                return
        _BKSYNC_STATE['running'] = False
        _BKSYNC_STATE['failed'] = max(_BKSYNC_STATE.get('failed', 0), 1)
        _BKSYNC_STATE['finished_at'] = datetime.now().isoformat()
        _BKSYNC_STATE['current_code'] = '⚠ 2 小时没结束, 判定卡住'
        print(f'[bk-sync] {sector_type} 超时未完成', flush=True)

    _th.Thread(target=_watcher, daemon=True).start()


@app.route('/api/flow/sector/dates', methods=['GET'])
def api_flow_sector_dates():
    """返回 sector_fund_flow 表里有数据的 distinct trade_date 列表 (倒序).
    ?type=industry|concept|all (默认 industry).
    """
    stype = request.args.get('type', 'industry').strip()
    conn = sqlite3.connect(DB_PATH)
    if stype == 'all':
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM sector_fund_flow ORDER BY trade_date DESC"
        ).fetchall()]
    elif stype in ('industry', 'concept'):
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM sector_fund_flow WHERE sector_type=? ORDER BY trade_date DESC",
            (stype,)
        ).fetchall()]
    else:
        conn.close()
        return jsonify({'error': f'invalid type: {stype}'}), 400
    conn.close()
    return jsonify({'dates': dates, 'count': len(dates), 'type': stype})


@app.route('/api/flow/sector/sync', methods=['POST'])
def api_flow_sector_sync():
    """启动后台抓取今日板块资金流 (行业/概念). 立即返回 'started'.
    ?type=industry|concept (默认 industry).
    ?force=1 → 忽略"已有数据"守卫, 强制重抓覆盖.
    ?wait_for_user=1 → 第 1 页拿不到时弹出浏览器窗口让用户手动 Cmd+R 刷新.
    """
    if _BKSYNC_STATE['running']:
        return jsonify({'status': 'already_running', 'state': _BKSYNC_STATE})
    stype = request.args.get('type', 'industry').strip()
    if stype not in ('industry', 'concept'):
        return jsonify({'error': f'invalid type: {stype}'}), 400
    _force = (request.args.get('force') in ('1', 'true', 'True')
              or bool((request.get_json(silent=True) or {}).get('force')))
    _wait_for_user = (request.args.get('wait_for_user') in ('1', 'true', 'True')
                      or bool((request.get_json(silent=True) or {}).get('wait_for_user')))
    import threading as _th
    t = _th.Thread(target=_bk_sync_market_bg, args=(stype, _force, _wait_for_user), daemon=True)
    t.start()
    return jsonify({'status': 'started', 'type': stype, 'force': _force,
                    'wait_for_user': _wait_for_user, 'state': _BKSYNC_STATE})


@app.route('/api/flow/sector/sync-status', methods=['GET'])
def api_flow_sector_sync_status():
    """查询板块资金流拉取状态. 前端 setInterval 轮询."""
    return jsonify(_BKSYNC_STATE)


@app.route('/api/flow/sector/market', methods=['GET'])
def api_flow_sector_market():
    """板块资金流表格数据. ?type=industry|concept (默认 industry).
    period=today (单日, 支持 ?date=YYYYMMDD) / 3d|5d|10d|20d (SQL 聚合近 N 个交易日).
    跟 /api/flow/market (EM 个股) 和 /api/flow/ths/market (THS 个股) 口径一致.
    today: ?date=YYYYMMDD 默认最新一日, ?sort=main_net_inflow|...
    agg:   ?date 忽略 (走 trading_dates_cache 窗口), ?sort=sum_main_net|...
    """
    stype = request.args.get('type', 'industry').strip()
    if stype not in ('industry', 'concept'):
        return jsonify({'error': f'invalid type: {stype}'}), 400
    period = request.args.get('period', 'today').lower().strip()
    period_map = {'today': 1, '3d': 3, '5d': 5, '10d': 10, '20d': 20, '30d': 30, '60d': 60, '90d': 90}
    if period not in period_map:
        period = 'today'
    period_days = period_map[period]
    is_agg = period != 'today'

    date_q = request.args.get('date', '').strip()
    sort_col = request.args.get('sort', '').strip()
    order = request.args.get('order', 'desc').strip().lower()
    if order not in ('asc', 'desc'):
        order = 'desc'
    try:
        limit = int(request.args.get('limit', '200'))
    except (ValueError, TypeError):
        limit = 200
    limit = max(1, min(limit, 1000))
    search = request.args.get('search', '').strip()

    # 字段白名单, 防 SQL 注入 (today vs agg 各一套, agg 模式 *_pct 不聚合)
    if is_agg:
        sort_whitelist = {
            'close', 'change_pct',
            'sum_main_net', 'sum_super_net', 'sum_big_net', 'sum_mid_net', 'sum_small_net',
            'flow_days',
        }
        default_sort = 'sum_main_net'
    else:
        sort_whitelist = {
            'close', 'change_pct', 'main_net_inflow', 'main_net_pct',
            'super_net', 'big_net', 'mid_net', 'small_net',
        }
        default_sort = 'main_net_inflow'
    if not sort_col or sort_col not in sort_whitelist:
        sort_col = default_sort

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if not is_agg:
        # ── today: 单日查询 ──
        if date_q:
            target_date = date_q
        else:
            row = conn.execute(
                "SELECT trade_date FROM sector_fund_flow WHERE sector_type=? ORDER BY trade_date DESC LIMIT 1",
                (stype,)
            ).fetchone()
            target_date = row['trade_date'] if row else ''

        if not target_date:
            conn.close()
            return jsonify({
                'type': stype, 'trade_date': '', 'period': period,
                'count': 0, 'rows': [],
                'message': f'暂无 {stype} 数据',
            })

        where = ['trade_date = ?', 'sector_type = ?']
        params = [target_date, stype]
        if search:
            where.append('(name LIKE ? OR sector_code LIKE ?)')
            like = f'%{search}%'
            params.extend([like, like])
        where_sql = ' AND '.join(where)

        sql = f'''SELECT sector_code, name, close, change_pct,
                         main_net_inflow, main_net_pct,
                         super_net, super_pct, big_net, big_pct,
                         mid_net, mid_pct, small_net, small_pct,
                         lead_stock_name, lead_stock_code
                  FROM sector_fund_flow
                  WHERE {where_sql}
                  ORDER BY {sort_col} {order.upper()}
                  LIMIT ?'''
        params.append(limit)
        raw_rows = conn.execute(sql, params).fetchall()

        rows = []
        for i, r in enumerate(raw_rows, 1):
            rows.append({
                'rank': i,
                'sector_code': r['sector_code'],
                'name': r['name'],
                'close': r['close'],
                'change_pct': r['change_pct'],
                'main_net_inflow': r['main_net_inflow'],
                'main_net_pct': r['main_net_pct'],
                'super_net': r['super_net'], 'super_pct': r['super_pct'],
                'big_net': r['big_net'], 'big_pct': r['big_pct'],
                'mid_net': r['mid_net'], 'mid_pct': r['mid_pct'],
                'small_net': r['small_net'], 'small_pct': r['small_pct'],
                'lead_stock_name': r['lead_stock_name'],
                'lead_stock_code': r['lead_stock_code'],
                'trade_date': target_date,
            })
        conn.close()
        return jsonify({
            'type': stype, 'trade_date': target_date, 'period': period,
            'count': len(rows), 'rows': rows,
            'sort': sort_col, 'order': order,
        })

    # ── agg: Nd 累计, SQL 聚合近 N 个交易日 (窗口按 trading_dates_cache, 跟 EM/THS 一致) ──
    # 取最近 N 个交易日窗口里所有 (sector_type, sector_code) 的资金流总和.
    # close/change_pct/lead_stock_* 取最新一日 (相关子查询走 idx_sff_sector).
    where_sql = 'sector_type = ?'
    params = [stype]
    if search:
        where_sql += ' AND (name LIKE ? OR sector_code LIKE ?)'
        like = f'%{search}%'
        params.extend([like, like])

    sql = f'''SELECT sector_type, sector_code, name,
                     SUM(main_net_inflow) AS sum_main_net,
                     SUM(super_net)       AS sum_super_net,
                     SUM(big_net)         AS sum_big_net,
                     SUM(mid_net)         AS sum_mid_net,
                     SUM(small_net)       AS sum_small_net,
                     COUNT(*)             AS flow_days,
                     MIN(trade_date)      AS start_date,
                     MAX(trade_date)      AS end_date,
                     (SELECT close      FROM sector_fund_flow f2
                       WHERE f2.sector_type=f.sector_type AND f2.sector_code=f.sector_code
                       ORDER BY trade_date DESC LIMIT 1) AS close,
                     (SELECT change_pct FROM sector_fund_flow f2
                       WHERE f2.sector_type=f.sector_type AND f2.sector_code=f.sector_code
                       ORDER BY trade_date DESC LIMIT 1) AS change_pct,
                     (SELECT lead_stock_name FROM sector_fund_flow f2
                       WHERE f2.sector_type=f.sector_type AND f2.sector_code=f.sector_code
                       ORDER BY trade_date DESC LIMIT 1) AS lead_stock_name,
                     (SELECT lead_stock_code FROM sector_fund_flow f2
                       WHERE f2.sector_type=f.sector_type AND f2.sector_code=f.sector_code
                       ORDER BY trade_date DESC LIMIT 1) AS lead_stock_code
              FROM sector_fund_flow f
              WHERE {where_sql}
                AND trade_date >= (
                  SELECT cal_date FROM trading_dates_cache
                  WHERE cal_date <= strftime('%Y%m%d', date('now', 'localtime'))
                  ORDER BY cal_date DESC
                  LIMIT 1 OFFSET ?
                )
              GROUP BY sector_type, sector_code
              ORDER BY {sort_col} {order.upper()}
              LIMIT ?'''
    params.extend([period_days - 1, limit])
    raw_rows = conn.execute(sql, params).fetchall()
    conn.close()

    rows = []
    for i, r in enumerate(raw_rows, 1):
        rows.append({
            'rank': i,
            'sector_code': r['sector_code'],
            'name': r['name'],
            'close': r['close'],
            'change_pct': r['change_pct'],
            'sum_main_net': r['sum_main_net'],
            'sum_super_net': r['sum_super_net'],
            'sum_big_net': r['sum_big_net'],
            'sum_mid_net': r['sum_mid_net'],
            'sum_small_net': r['sum_small_net'],
            'flow_days': r['flow_days'],
            'start_date': r['start_date'],
            'end_date': r['end_date'],
            'lead_stock_name': r['lead_stock_name'],
            'lead_stock_code': r['lead_stock_code'],
        })
    return jsonify({
        'type': stype, 'period': period,
        'count': len(rows), 'rows': rows,
        'sort': sort_col, 'order': order,
    })


# ── 同花顺个股资金流 (data.10jqka.com.cn/funds/ggzjl) ──
# 抓取走 fetch_fund_flow_ths.py: 真实 Chrome + 有界面 (藏屏幕外) + 反自动化标记,
# 同花顺 WAF 检测自动化标记, 无头/JSONP 全部 401/403 (2026-08-20 实测).
# 每次全量 105 页 ≈ 5-8 分钟, 所以走后台线程 + 状态轮询 (跟东财 _AKSYNC_STATE 一个模式).
_THSSYNC_STATE = {
    'running': False, 'started_at': None, 'finished_at': None,
    'total': 0, 'done': 0, 'success': 0, 'failed': 0,
    'errors': [], 'current_code': '', 'elapsed_sec': 0,
    'source': '10jqka-ggzjl', 'target_date': '',
    'is_already_synced': False, 'actual_date': '',
}


def _ths_sync_market_bg(force=False):
    """后台线程: 抓同花顺全市场个股资金流 → fund_flow_ths 表.

    同花顺页面必须用真 Chrome (channel='chrome') + 有界面 + 反检测标记, 没有退路;
    而 Flask 进程内 launch Chrome 会被 macOS 拒 (TargetClosedError), 所以这块走
    独立 launchd agent (sync_agent.py, 5556 端口) 跑, Flask 只负责 trigger +
    轮询 state file —— 跟个股主力净流入 (_em_sync_market_bg) 同一套架构.
    """
    import time as _t
    import threading as _th
    import urllib.request as _ur

    t0 = _t.time()
    today = datetime.now().strftime('%Y%m%d')
    state_file = '/tmp/tradeiq-ths-sync-state.json'

    _THSSYNC_STATE.update({
        'running': True, 'started_at': datetime.now().isoformat(),
        'finished_at': None, 'total': 0, 'done': 0,
        'success': 0, 'failed': 0, 'errors': [],
        'current_code': f'准备同步 {today}...',
        'elapsed_sec': 0, 'target_date': today,
        'is_already_synced': False, 'actual_date': today,
    })
    # 清掉旧 state file, 让前端从「已启动」开始看
    try:
        if os.path.exists(state_file):
            os.remove(state_file)
    except Exception:
        pass
    print(f'[ths-sync] 开始: 同步 {today}' + (' (强制刷新)' if force else ''), flush=True)

    agent_url = os.environ.get('TRADEIQ_AGENT_URL', 'http://127.0.0.1:5556')
    payload = json.dumps({'force': bool(force), 'state_file': state_file}).encode()
    try:
        req = _ur.Request(f'{agent_url}/sync/ths', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with _ur.urlopen(req, timeout=3) as r:
            resp = json.loads(r.read())
        _THSSYNC_STATE['current_code'] = f'agent 已接收 (pid={resp.get("pid")})'
        print(f'[ths-sync] 触发 agent 成功: {resp}', flush=True)
    except Exception as e:
        err = f'agent 触发失败 (syncagent 没起来?): {type(e).__name__}: {str(e)[:200]}'
        _THSSYNC_STATE['errors'].append(('agent', err))
        _THSSYNC_STATE['failed'] = 1
        _THSSYNC_STATE['running'] = False
        _THSSYNC_STATE['finished_at'] = datetime.now().isoformat()
        _THSSYNC_STATE['current_code'] = err
        print(f'[ths-sync] {err}', flush=True)
        return

    def _watcher():
        # 同花顺全市场翻页要 5-8 分钟, 给 30 分钟
        for _ in range(60 * 30):
            _t.sleep(1.0)
            state = None
            try:
                if os.path.exists(state_file):
                    with open(state_file, 'r', encoding='utf-8') as f:
                        state = json.load(f)
            except Exception:
                state = None
            if state:
                for k in ('total', 'done', 'success', 'failed', 'current_code',
                          'elapsed_sec', 'is_already_synced', 'actual_date',
                          'target_date', 'source', 'errors'):
                    if k in state:
                        _THSSYNC_STATE[k] = state[k]
            if state and not state.get('running') and state.get('finished_at'):
                _THSSYNC_STATE['running'] = False
                _THSSYNC_STATE['finished_at'] = state['finished_at']
                _THSSYNC_STATE['elapsed_sec'] = state.get(
                    'elapsed_sec', _THSSYNC_STATE.get('elapsed_sec', 0))
                print(f'[ths-sync] 完成: success={_THSSYNC_STATE["success"]} '
                      f'elapsed={_THSSYNC_STATE["elapsed_sec"]}s', flush=True)
                return
        _THSSYNC_STATE['running'] = False
        _THSSYNC_STATE['failed'] = max(_THSSYNC_STATE.get('failed', 0), 1)
        _THSSYNC_STATE['finished_at'] = datetime.now().isoformat()
        _THSSYNC_STATE['current_code'] = '⚠ 30 分钟没结束, 判定卡住'
        print('[ths-sync] 超时未完成', flush=True)

    _th.Thread(target=_watcher, daemon=True).start()


@app.route('/api/flow/ths/sync', methods=['POST'])
def api_flow_ths_sync():
    """启动后台抓同花顺个股资金流. 立即返回 'started'.
    ?force=1 → 忽略幂等守卫强制重抓 (手动点同步按钮).
    """
    if _THSSYNC_STATE['running']:
        return jsonify({'status': 'already_running', 'state': _THSSYNC_STATE})
    _force = (request.args.get('force') in ('1', 'true', 'True')
              or bool((request.get_json(silent=True) or {}).get('force')))
    import threading as _th
    t = _th.Thread(target=_ths_sync_market_bg, args=(_force,), daemon=True)
    t.start()
    return jsonify({'status': 'started', 'force': _force, 'state': _THSSYNC_STATE})


@app.route('/api/flow/ths/sync-status', methods=['GET'])
def api_flow_ths_sync_status():
    """查询同花顺抓取状态. 前端 setInterval 轮询."""
    return jsonify(_THSSYNC_STATE)


@app.route('/api/flow/ths/dates', methods=['GET'])
def api_flow_ths_dates():
    """fund_flow_ths 有数据的日期列表 (倒序), 给前端日历导航."""
    conn = sqlite3.connect(DB_PATH)
    dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM fund_flow_ths ORDER BY trade_date DESC'
    ).fetchall()]
    conn.close()
    return jsonify({'dates': dates, 'count': len(dates), 'source': '10jqka'})


@app.route('/api/flow/ths/market', methods=['GET'])
def api_flow_ths_market():
    """同花顺全市场资金流排行. 镜像 /api/flow/market, 但读 fund_flow_ths.
    period=today (单日, 支持 ?date=YYYYMMDD) / 3d/5d/10d/20d (SQL 聚合近 N 个交易日).
    注意口径: net = 流入-流出 (总资金净流入), 不是东财的主力净流入.
    close/change_pct/turnover_pct 直接用同花顺页面的值, 不 JOIN stock_daily.
    """
    period = request.args.get('period', 'today').lower().strip()
    period_map = {'today': 1, '3d': 3, '5d': 5, '10d': 10, '20d': 20, '30d': 30, '60d': 60, '90d': 90}
    if period not in period_map:
        period = 'today'
    period_days = period_map[period]

    sort = request.args.get('sort', '').strip()
    order = 'desc' if request.args.get('order', 'desc').lower() == 'desc' else 'asc'
    market = request.args.get('market', 'all').lower()
    search = request.args.get('search', '').strip()
    limit = min(int(request.args.get('limit', 200)), 10000)
    date = request.args.get('date', '').strip()  # YYYYMMDD, today 模式才生效

    # 排序白名单: today vs 聚合字段不同
    if period == 'today':
        sort_whitelist = {'net', 'inflow', 'outflow', 'amount', 'turnover_pct',
                          'change_pct', 'close', 'big_inflow'}
        if not sort or sort not in sort_whitelist:
            sort = 'net'
    else:
        sort_whitelist = {'sum_net', 'sum_inflow', 'sum_outflow', 'sum_amount',
                          'sum_big_inflow', 'change_pct', 'close', 'flow_days'}
        if not sort or sort not in sort_whitelist:
            sort = 'sum_net'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if period == 'today':
        if date:
            rows = conn.execute("""
                SELECT trade_date, ts_code, code, name, close, change_pct, turnover_pct,
                       inflow, outflow, net, amount, big_inflow
                FROM fund_flow_ths
                WHERE trade_date = ?
            """, (date,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT trade_date, ts_code, code, name, close, change_pct, turnover_pct,
                       inflow, outflow, net, amount, big_inflow
                FROM fund_flow_ths
                WHERE trade_date = (SELECT MAX(trade_date) FROM fund_flow_ths)
            """).fetchall()
    else:
        # 聚合近 N 个交易日 (窗口按 trading_dates_cache, 跟东财版一致)
        rows = conn.execute("""
            SELECT
                ts_code, code, name,
                SUM(net)        AS sum_net,
                SUM(inflow)     AS sum_inflow,
                SUM(outflow)    AS sum_outflow,
                SUM(amount)     AS sum_amount,
                SUM(big_inflow) AS sum_big_inflow,
                COUNT(*)        AS flow_days,
                MIN(trade_date) AS start_date,
                MAX(trade_date) AS end_date,
                (SELECT close FROM fund_flow_ths f2
                 WHERE f2.ts_code = f.ts_code
                 ORDER BY trade_date DESC LIMIT 1) AS close,
                (SELECT change_pct FROM fund_flow_ths f2
                 WHERE f2.ts_code = f.ts_code
                 ORDER BY trade_date DESC LIMIT 1) AS change_pct
            FROM fund_flow_ths f
            WHERE trade_date >= (
                SELECT cal_date FROM trading_dates_cache
                WHERE cal_date <= strftime('%Y%m%d', date('now', 'localtime'))
                ORDER BY cal_date DESC
                LIMIT 1 OFFSET ?
            )
            GROUP BY ts_code
            ORDER BY sum_net DESC
            LIMIT ?
        """, (period_days - 1, limit)).fetchall()
    conn.close()

    # market 过滤 (按 code 前缀, 跟东财版一致; 北交所 920 归 sh 组之外的独立判断没必要, 保持简单)
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
        rows = [r for r in rows if s in (r['name'] or '').lower() or s in r['code']]

    # 排序 (聚合模式 SQL 已按 sum_net desc, 这里尊重用户二次排序)
    if period == 'today' or sort != 'sum_net':
        rows.sort(key=lambda r: (r[sort] or 0) if sort in r.keys() else 0, reverse=(order == 'desc'))

    out = [dict(r) for r in rows[:limit]]
    _attach_sector(out, key='ts_code')  # 补 sector_history, 跟东财版一致
    return jsonify({
        'count': len(out),
        'total_matched': len(rows),
        'date': date,
        'period': period,
        'sort': sort,
        'order': order,
        'rows': out,
    })


# ── 补数: 5 日反推缺失日的主力净流入 ──
_BACKFILL_STATE = {
    'running': False, 'target_date': '', 'phase': '',
    'total': 0, 'done': 0, 'failed': 0, 'current_code': '',
    'started_at': None, 'finished_at': None,
    'result': None, 'error': None,
}

_PHASE_TEXT = {
    'fetch': '抓东财 5 日排行...', 'infer': '反推当日值...',
    'write': '写入数据库...', 'validate': '健康检查...',
}


def _backfill_bg(target_date, force):
    """后台线程: 调 backfill_flow 反推补数, 进度写入 _BACKFILL_STATE."""
    import backfill_flow
    _BACKFILL_STATE.update({
        'running': True, 'target_date': target_date, 'phase': 'starting',
        'total': 0, 'done': 0, 'failed': 0, 'current_code': f'准备补 {target_date}...',
        'started_at': datetime.now().isoformat(), 'finished_at': None,
        'result': None, 'error': None,
    })

    def _cb(stage, **kw):
        if stage == 'total':
            _BACKFILL_STATE['total'] = kw.get('total', 0)
            _BACKFILL_STATE['current_code'] = f'共 {kw.get("total", 0)} 只, 开始翻页...'
        elif stage == 'page':
            _BACKFILL_STATE['done'] = kw.get('rows_count', 0)
            _BACKFILL_STATE['failed'] = len(kw.get('failed', []))
            _BACKFILL_STATE['current_code'] = (
                f'pn={kw.get("pn")}/{kw.get("total_pages")} 累计 {kw.get("rows_count")} 行')
        elif stage == 'phase':
            ph = kw.get('phase', '')
            _BACKFILL_STATE['phase'] = ph
            if ph in _PHASE_TEXT:
                _BACKFILL_STATE['current_code'] = _PHASE_TEXT[ph]

    try:
        anchor = datetime.now().strftime('%Y%m%d')
        res = backfill_flow.backfill_fund_flow(
            target_date, anchor_date=anchor, force=force,
            progress_cb=_cb, headless=False)
        _BACKFILL_STATE['result'] = res
        if res.get('ok'):
            _BACKFILL_STATE['done'] = res.get('inserted', 0)
            _BACKFILL_STATE['current_code'] = f'✅ {target_date} 已补 {res.get("inserted", 0)} 行'
        else:
            _BACKFILL_STATE['current_code'] = '❌ ' + res.get('reason_text', res.get('reason', '补数失败'))
    except Exception as e:
        _BACKFILL_STATE['error'] = str(e)[:300]
        _BACKFILL_STATE['current_code'] = f'❌ {str(e)[:120]}'
        print(f'[backfill] 异常: {e}', flush=True)
    finally:
        _BACKFILL_STATE['running'] = False
        _BACKFILL_STATE['finished_at'] = datetime.now().isoformat()


@app.route('/api/flow/gaps', methods=['GET'])
def api_flow_gaps():
    """检测最近交易日里 fund_flow(push2) 的缺口, 分 fillable/unfillable."""
    import backfill_flow
    anchor = (request.args.get('anchor') or datetime.now().strftime('%Y%m%d')).strip()
    try:
        lookback = int(request.args.get('lookback', 15))
    except ValueError:
        lookback = 15
    try:
        return jsonify({'status': 'success', **backfill_flow.compute_gaps(anchor, lookback)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:200]}), 500


@app.route('/api/flow/backfill', methods=['POST'])
def api_flow_backfill():
    """启动后台补数. body: {date:'YYYYMMDD', force?:bool}. 立即返回 started."""
    if _BACKFILL_STATE['running']:
        return jsonify({'status': 'already_running', 'state': _BACKFILL_STATE})
    data = request.get_json(silent=True) or {}
    target = (data.get('date') or request.args.get('date') or '').strip()
    force = bool(data.get('force'))
    if not (len(target) == 8 and target.isdigit()):
        return jsonify({'status': 'error', 'message': 'date 需 YYYYMMDD 格式'}), 400
    import threading as _th
    _th.Thread(target=_backfill_bg, args=(target, force), daemon=True).start()
    return jsonify({'status': 'started', 'target_date': target})


@app.route('/api/flow/backfill-status', methods=['GET'])
def api_flow_backfill_status():
    """查询补数进度 + 最终健康检查. 前端每秒轮询."""
    return jsonify(_BACKFILL_STATE)


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


def _fund_flow_window_ths(ts_code, end_date, window):
    """拉 ts_code 在 end_date 之前 window 个交易日的同花顺资金净额列表 (新→旧).
    口径: fund_flow_ths.net = 流入-流出 (总资金净流入, 非主力).
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute("""
        SELECT net FROM fund_flow_ths
        WHERE ts_code = ? AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT ?
    """, (ts_code, end_date, window)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _ths_flow_days_available(end_date):
    """fund_flow_ths 表在 end_date 及之前有多少个有数据的交易日.
    用 net IS NOT NULL 过滤无效行.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    row = conn.execute("""
        SELECT COUNT(DISTINCT trade_date) FROM fund_flow_ths
        WHERE trade_date <= ? AND net IS NOT NULL
    """, (end_date,)).fetchone()
    conn.close()
    return row[0] if row else 0


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


def _ths_flow_consecutive_sign(ts_code, end_date, n, want_positive=True):
    """最近 n 日全部 THS 净额为正(>0) 或 负(<0). 数据不足返 False."""
    flows = _fund_flow_window_ths(ts_code, end_date, n)
    if len(flows) < n:
        return False
    return all(v > 0 for v in flows) if want_positive else all(v < 0 for v in flows)


def _ths_flow_turn(ts_code, end_date, n, want_positive):
    """最近 n 日 THS 净额由反向转正向 (want_positive=True) 或 反之."""
    flows = _fund_flow_window_ths(ts_code, end_date, n)
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
    """fund_flow 表在 end_date 及之前, 东方财富口径有多少"有效"资金流数据日。

    口径跟主力净流入 tab 的东财自动口径一致 (push2 优先, 缺则 daykline),
    main_net_inflow != 0 过滤 ifind 那种全 0 的空壳日 (tushare 已清, 不会再出现).
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    row = conn.execute("""
        SELECT COUNT(DISTINCT trade_date) FROM fund_flow
        WHERE trade_date <= ?
          AND source IN (?, ?)
          AND main_net_inflow IS NOT NULL
          AND main_net_inflow != 0
    """, (end_date,) + EM_FLOW_SOURCES).fetchone()
    conn.close()
    return row[0] if row else 0


def _write_stock_picks_for_date(trade_date, picks):
    """_compute_picks 结果写入 stock_picks 表 (先删当日再插, 收盘口径覆盖盘中口径).
    供 scheduler 的 picks_resync 任务和 /api/picks/sync-date 共用."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM stock_picks WHERE trade_date = ?', (trade_date,))
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
            VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)''',
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


def _write_stock_picks_ths_for_date(trade_date, picks):
    """_compute_picks_ths 结果写入 stock_picks_ths 表 (先删当日再插).
    供 scheduler 的 ths_picks_resync 任务和 /api/picks/ths/sync 共用."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM stock_picks_ths WHERE trade_date = ?', (trade_date,))
    for p in picks:
        c.execute('''INSERT OR REPLACE INTO stock_picks_ths
            (trade_date, ts_code, name, close, change_pct,
             ma5, ma10, ma20, ma30,
             ma5_slope, ma10_slope, ma20_slope, ma30_slope,
             ths_net_today, ths_net_3d, ths_net_5d, ths_net_10d,
             score, signal_type, flow_days_available, reasons_json)
            VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)''',
            (trade_date, p['ts_code'], p['name'], p['close'], p['change_pct'],
             p['ma5'], p['ma10'], p['ma20'], p['ma30'],
             p['ma5_slope'], p['ma10_slope'], p['ma20_slope'], p['ma30_slope'],
             p['ths_net_today'], p['ths_net_3d'], p['ths_net_5d'], p['ths_net_10d'],
             p['score'], p['signal_type'], p['flow_days_available'],
             json.dumps(p['reasons'], ensure_ascii=False)))
    conn.commit()
    conn.close()


def _compute_picks(trade_date=None, ts_codes=None, intraday=False):
    """按《均线 + 资金流》文档计算选股推荐.

    双维度评分:
      1) 均线: MA5/10/20/30 + 斜率 + 多头排列 + 金叉死叉
      2) 资金流: 当日主力净流入 + 3/5/10/20 日累计主力净流入 + 5 档占比

    数据不足时降级: 缺几天的累计就跳过哪个维度, 不强行估算.

    Args:
        trade_date: 计算日期 (YYYYMMDD), None = 最新
        ts_codes: 限定只算这些 ts_code (None = 全市场). 用于自选股 sub-tab 只算用户加的代码.
        intraday: 盘中模式. trade_date 是今天且还没收盘入库时, 用 sina 实时价
                  合成"今天"这根 K 线参与计算 (资金流维度仍是截至昨日累计).
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
    intraday_used = False
    if trade_date not in dates_30:
        # 盘中模式: 请求的日期 (典型=今天) 还没收盘入库, 允许用 dates_30 最新一天做基底,
        # 后面拿 sina 实时价给每只股补上 trade_date 这个点
        today = datetime.now().strftime('%Y%m%d')
        if intraday and trade_date == today and dates_30:
            intraday_used = True
        else:
            conn.close()
            return []

    # fund_flow 可用天数 (决定哪些累计资金流维度能算)
    # 口径 B: 数东财两个源 (push2 每日任务 + daykline 回补), 跟主力净流入 tab 一致;
    # 只数 push2 会把 6/16 之前整个区间算成 0 天, 3/5/10/20 日维度全算不出来.
    # (ifind 全 0 是空壳, tushare 已被清掉, 都不再参与累计算法)
    flow_dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM fund_flow '
        'WHERE trade_date <= ? '
        '  AND source IN (?, ?) '
        '  AND main_net_inflow IS NOT NULL AND main_net_inflow != 0 '
        'ORDER BY trade_date DESC',
        (trade_date,) + EM_FLOW_SOURCES
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

    # 盘中模式: 拉全市场 sina 实时价, 给每只股合成"今天"的数据点
    live_quotes = {}
    if intraday_used:
        try:
            live_quotes = fetch_sina_quotes_all()
            print(f'[picks intraday] {trade_date} sina 实时价 {len(live_quotes)} 只', flush=True)
        except Exception as e:
            print(f'[picks intraday] sina 拉取异常: {e}', flush=True)
        if not live_quotes:
            # 拿不到实时价就别硬算 — 否则等于把昨日收盘的名单盖上今天的日期戳
            print(f'[picks intraday] 无实时价, 放弃 {trade_date} 盘中计算', flush=True)
            conn.close()
            return []

    picks = []
    for ts_code, data_list in stock_data.items():
        if len(data_list) < 30:
            continue
        data_list.sort(key=lambda x: x['date'], reverse=True)

        if intraday_used:
            q = live_quotes.get(ts_code)
            if not q or not q.get('price') or q['price'] <= 0:
                continue  # 停牌/无实时价, 盘中模式跳过
            data_list.insert(0, {
                'date': trade_date,
                'close': q['price'],
                'change': None,
                'volume': None,
                'name': data_list[0]['name'],
            })

        latest = data_list[0]
        if not latest['close'] or latest['close'] <= 0:
            continue
        # 成交量过滤 (盘中模式的合成点没有成交量, 停牌股上面已被实时价挡掉)
        if not intraday_used and (not latest['volume'] or latest['volume'] == 0):
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
        # 当日资金流详情 (精确匹配当日行: 不存在 → None.
        # 盘中模式若上面已先抓过当日资金流, 这里正常有值)
        flow_today_row = conn.execute("""
            SELECT main_net_inflow, main_net_pct, super_net, super_pct, big_net, big_pct,
                   mid_net, mid_pct, small_net, small_pct
            FROM fund_flow WHERE ts_code = ? AND trade_date = ?
        """, (ts_code, trade_date)).fetchone()
        main_net_today = flow_today_row['main_net_inflow'] if flow_today_row else None
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
        # 盘中模式的合成点 volume=None, 过滤掉再算量比
        volumes = [d['volume'] for d in reversed(data_list) if d['volume'] is not None]
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


# ============ 同花顺选股推荐 (独立 sub-tab, 数据源 fund_flow_ths) ============

def _compute_picks_ths(trade_date=None, ts_codes=None, intraday=False):
    """同花顺净额选股推荐. 与 _compute_picks 并列但完全独立:
      - 数据源: fund_flow_ths.net (口径 = 流入-流出, 总资金净流入, 非主力)
      - 窗口: today / 3d / 5d / 10d (20d 跳过, THS 历史不够)
      - 没有 5 档分摊, 没有净流入占比 (THS 页面没有这两类数据)
      - 算法骨架 (均线信号 + 黄金/死亡组合 + 葛兰碧法则) 与 EM 同步演化
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 确定计算日期: 优先 fund_flow_ths 的最新 (THS picks 的核心数据源日期),
    # 否则 fallback stock_daily 最新.
    # 区别于 EM: EM 的 trade_date 跟 stock_daily 走 (东财 push2 当日就有), 而 THS 数据通常要等盘后抓
    if not trade_date:
        row = conn.execute('SELECT MAX(trade_date) as d FROM fund_flow_ths').fetchone()
        if row and row['d']:
            trade_date = row['d']
        else:
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
    intraday_used = False
    if trade_date not in dates_30:
        # THS 与 EM 不同: fund_flow_ths 是盘后抓,日期可能比 stock_daily 当天还新一天
        # (例: stock_daily 最新 20260820, fund_flow_ths 已经有 20260821 当日数据)
        # 这里允许 trade_date 暂时无对应 K 线, 直接用 stock_daily 最新一天作为"今天"基点
        # (资金流用 trade_date 当日的, K 线用 stock_daily 最新一天的, 不冲突)
        today = datetime.now().strftime('%Y%m%d')
        if intraday and trade_date == today and dates_30:
            intraday_used = True

    # fund_flow_ths 可用天数 (决定哪些累计资金流维度能算)
    flow_days = _ths_flow_days_available(trade_date)
    can_3d = flow_days >= 3
    can_5d = flow_days >= 5
    can_10d = flow_days >= 10
    # 20d 跳过

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

    # 盘中模式: 拉全市场 sina 实时价
    live_quotes = {}
    if intraday_used:
        try:
            live_quotes = fetch_sina_quotes_all()
            print(f'[ths picks intraday] {trade_date} sina 实时价 {len(live_quotes)} 只', flush=True)
        except Exception as e:
            print(f'[ths picks intraday] sina 拉取异常: {e}', flush=True)
        if not live_quotes:
            print(f'[ths picks intraday] 无实时价, 放弃 {trade_date} 盘中计算', flush=True)
            conn.close()
            return []

    picks = []
    for ts_code, data_list in stock_data.items():
        if len(data_list) < 30:
            continue
        data_list.sort(key=lambda x: x['date'], reverse=True)

        if intraday_used:
            q = live_quotes.get(ts_code)
            if not q or not q.get('price') or q['price'] <= 0:
                continue
            data_list.insert(0, {
                'date': trade_date,
                'close': q['price'],
                'change': None,
                'volume': None,
                'name': data_list[0]['name'],
            })

        latest = data_list[0]
        if not latest['close'] or latest['close'] <= 0:
            continue
        if not intraday_used and (not latest['volume'] or latest['volume'] == 0):
            continue
        if latest['name'] and ('*ST' in latest['name'] or 'ST' in latest['name']):
            continue

        # ===== 均线 (同 EM) =====
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

        # ===== THS 资金流 (4 窗口) =====
        flows = {}
        if can_3d:
            flows['3d'] = sum(_fund_flow_window_ths(ts_code, trade_date, 3))
        if can_5d:
            flows['5d'] = sum(_fund_flow_window_ths(ts_code, trade_date, 5))
        if can_10d:
            flows['10d'] = sum(_fund_flow_window_ths(ts_code, trade_date, 10))
        # 当日 THS 净额
        flow_today_row = conn.execute("""
            SELECT net FROM fund_flow_ths
            WHERE ts_code = ? AND trade_date = ?
        """, (ts_code, trade_date)).fetchone()
        ths_net_today = flow_today_row['net'] if flow_today_row else None

        # 涨跌幅
        change_pct = 0
        if len(data_list) >= 2:
            prev_close = data_list[1]['close']
            if prev_close and prev_close > 0:
                change_pct = (latest['close'] - prev_close) / prev_close * 100

        # ===== 评分 =====
        # 与 EM 同骨架, 资金流维度全部走 THS; 占比类验证去掉 (THS 没有 net_pct 维度);
        # 净流入绝对值验证保留 (用 ths_net_today / flows[...] 替代 main_net)
        score = 0
        reasons = []
        buy_signals = 0
        sell_signals = 0

        # ---------- 均线信号 ----------
        if ma5 > ma10 > ma20 > ma30:
            score += 30
            reasons.append('多头排列 MA5>MA10>MA20>MA30')
        elif ma5 > ma10 > ma20:
            score += 18
            reasons.append('短中期多头排列 (MA5>MA10>MA20)')
        if ma5 < ma10 < ma20 < ma30:
            score -= 20
            reasons.append('空头排列 MA5<MA10<MA20<MA30')
            sell_signals += 1

        if latest['close'] > ma5: score += 5; reasons.append('站上MA5')
        if latest['close'] > ma10: score += 5; reasons.append('站上MA10')
        if latest['close'] > ma20: score += 5; reasons.append('站上MA20')
        if latest['close'] > ma30: score += 5; reasons.append('站上MA30')

        if ma5_slope is not None and ma5_slope > 45:
            score += 8
            reasons.append(f'MA5陡峭向上 ({ma5_slope:.1f}°>45°)')
        elif ma5_slope is not None and ma5_slope > 30:
            score += 3
            reasons.append(f'MA5向上 ({ma5_slope:.1f}°)')

        # ---------- 金叉死叉 (无占比验证, 改用 THS 净流入绝对值验证) ----------
        prev_ma5 = sum(closes[-6:-1]) / 5
        prev_ma10 = sum(closes[-11:-1]) / 10
        prev_ma30 = sum(closes[-31:-1]) / 30

        # 5日上穿10日 +15 (验证: THS 当日净流入 > 1亿)
        if prev_ma5 <= prev_ma10 and ma5 > ma10:
            if ths_net_today is not None and ths_net_today > 1e8:
                score += 15; reasons.append('5日金叉10日 ★ THS当日净流入>1亿'); buy_signals += 1
            else:
                score += 8; reasons.append('5日金叉10日 (资金验证待 THS 当日数据)')

        # 10日上穿30日 +20 (验证: 30日拐头 + 10日 THS 累计>5000万)
        if prev_ma10 <= prev_ma30 and ma10 > ma30:
            if ma30_slope is not None and ma30_slope > 0 and can_10d and flows.get('10d', 0) > 5e7:
                score += 20; reasons.append('10日金叉30日 ★★★ (30日拐头+10日 THS 净额>5000万)'); buy_signals += 1
            elif can_10d:
                score += 10; reasons.append('10日金叉30日 (资金验证待 THS N≥10天数据)')

        # 10日下穿30日 (死叉) -30 (无 20d 验证, 改用 10d THS 由正转负)
        if prev_ma10 >= prev_ma30 and ma10 < ma30:
            if can_10d and flows.get('10d', 0) < 0:
                score -= 30; reasons.append('10日死叉30日 ★★★ (10日 THS 净额<0)'); sell_signals += 1
            elif can_10d:
                score -= 18; reasons.append('10日死叉30日 (THS 资金验证待 N≥10天数据)')
            else:
                score -= 10; reasons.append('10日死叉30日 (THS 数据待积累)')

        # ---------- 黄金组合 ----------
        # 黄金 1: 突破20日线 + 20日线拐头 + 连续 3 日 THS 净额为正
        if (latest['close'] > ma20 and ma20_slope is not None and ma20_slope > 0
            and can_3d and _ths_flow_consecutive_sign(ts_code, trade_date, 3, want_positive=True)):
            score += 25
            reasons.append('黄金1: 突破20日线+连续3日 THS 净额为正 ★★★')
            buy_signals += 1
        elif latest['close'] > ma20 and ma20_slope is not None and ma20_slope > 0:
            reasons.append('黄金1部分: 突破20日线+拐头向上 (需 N≥3天连续 THS 数据)')

        # 黄金 2: 回踩 MA10 + THS 由流出转流入
        near_ma10 = abs(latest['close'] - ma10) / ma10 < 0.02
        if near_ma10 and can_3d and _ths_flow_turn(ts_code, trade_date, 3, want_positive=True):
            score += 20
            reasons.append('黄金2: 回踩MA10+THS 流出转流入 ★★')
            buy_signals += 1
        elif near_ma10:
            reasons.append('黄金2部分: 回踩MA10 (资金由流出转流入需 N≥3天数据)')

        # 黄金 3: 10日金叉30日 + 30日拐头 + 10日 THS 净额 > 0 (信号, 不重复加分)
        if (prev_ma10 <= prev_ma30 and ma10 > ma30
            and ma30_slope is not None and ma30_slope > 0
            and can_10d and flows.get('10d', 0) > 0):
            reasons.append('黄金3: 10日金叉30日+30日拐头+10日 THS>0 ★★★')

        # 黄金 4: 跌破 MA5 但 MA10 支撑 + THS 当日净流入 > 0
        if (latest['close'] < ma5 and latest['close'] > ma10
            and ths_net_today is not None and ths_net_today > 0):
            score += 15
            reasons.append('黄金4: 跌破MA5但MA10支撑+THS 当日>0 ★★ (洗盘结束)')
            buy_signals += 1

        # ---------- 死亡组合 ----------
        # 死亡 1: 跌破 MA10 + 5 日内 THS 由正转负
        if latest['close'] < ma10 and can_5d and _ths_flow_turn(ts_code, trade_date, 3, want_positive=False):
            score -= 25
            reasons.append('死亡1: 跌破MA10+5日内THS由正转负 ★★★')
            sell_signals += 1
        elif latest['close'] < ma10:
            reasons.append('死亡1部分: 跌破MA10 (THS 验证需 N≥5天数据)')

        # 死亡 2: 10 日死叉30日 + 10 日 THS < 0 (跟前面死叉合并处理, 这里补信号标注)
        if (prev_ma10 >= prev_ma30 and ma10 < ma30
            and can_10d and flows.get('10d', 0) < 0):
            reasons.append('死亡2: 10日死叉30日+10日 THS<0 ★★★')

        # 死亡 3: 创新高 + 连续 3 日 THS 净流出 (顶背离)
        if (change_pct > 0 and _is_new_high(closes, 60)
            and can_3d and _ths_flow_consecutive_sign(ts_code, trade_date, 3, want_positive=False)):
            score -= 20
            reasons.append('死亡3: 创新高+连续3日 THS 净流出 ★★★ (顶背离)')
            sell_signals += 1
        elif _is_new_high(closes, 60):
            reasons.append('死亡3部分: 创新高 (连续3日 THS 流出需 N≥3天数据)')

        # 死亡 4: 多头排列但 10 日 THS 开始流出 (20d 跳过)
        if (ma5 > ma10 > ma20 > ma30
            and can_10d
            and flows.get('10d', 0) < 0):
            score -= 15
            reasons.append('死亡4: 多头排列但10日 THS<0 ★★ (趋势即将反转)')
            sell_signals += 1
        elif ma5 > ma10 > ma20 > ma30 and not can_10d:
            reasons.append('死亡4待验证: 多头排列 (10日 THS 验证需 N≥10天数据)')

        # ---------- 真假突破 (无占比验证) ----------
        is_breakout, brk_level = _is_breakout_confirmed(closes, 20)
        if is_breakout and ths_net_today is not None and ths_net_today > 5e7:
            score += 20
            reasons.append(f'真突破确认: 站稳3日+THS 当日>{5e7/1e8:.1f}亿 ★★★')
            buy_signals += 1
        elif is_breakout:
            reasons.append('突破中: 站稳3日 (THS 当日 < 5000万, 验证待积累)')

        # ---------- 真洗盘识别 (THS 5 日累计 > 0) ----------
        if latest['close'] < ma10 and can_5d and flows.get('5d', 0) > 0:
            score += 8
            reasons.append('真洗盘: 跌破MA10但5日 THS 累计仍>0 (承接)')

        # ---------- 葛兰碧八大法则 (净流入绝对值, 无占比) ----------
        bias = (latest['close'] - ma5) / ma5 * 100 if ma5 else 0
        bias10 = (latest['close'] - ma10) / ma10 * 100 if ma10 else 0
        bias20 = (latest['close'] - ma20) / ma20 * 100 if ma20 else 0
        bias30 = (latest['close'] - ma30) / ma30 * 100 if ma30 else 0

        volumes = [d['volume'] for d in reversed(data_list) if d['volume'] is not None]
        vol_ratio = _volume_ratio(volumes) if len(volumes) >= 6 else None

        # 1. 突破买入
        if (latest['close'] > ma20 and prev_ma5 <= prev_ma10 and ma5 > ma10
            and vol_ratio is not None and vol_ratio > 1.5
            and ths_net_today is not None and ths_net_today > 1e8):
            score += 18
            reasons.append(f'葛兰碧突破买入: 突破MA20+量>50%+THS 当日>1亿 ★★')
            buy_signals += 1

        # 2. 回踩不破
        if ((abs(bias10) < 3 and latest['close'] > ma10)
            and ths_net_today is not None and ths_net_today > 0):
            score += 12
            reasons.append(f'葛兰碧回踩不破: 回踩MA10不破+THS 当日>0 ★')
            buy_signals += 1

        # 3. 假跌破买入
        if (latest['close'] < ma5 * 1.005 and latest['close'] > ma5 * 0.99
            and ths_net_today is not None and ths_net_today > 0):
            reasons.append('葛兰碧假跌破: 触及MA5后收回+THS>0 (洗盘结束信号) ★')

        # 4. 超跌反弹
        if bias < -15 and ths_net_today is not None and ths_net_today > 0:
            score += 10
            reasons.append(f'葛兰碧超跌反弹: 偏离MA5 {bias:.1f}%+THS>0 ★')
            buy_signals += 1

        # 5. 跌破卖出
        if ((latest['close'] < ma20 or latest['close'] < ma30)
            and ths_net_today is not None and ths_net_today < 0):
            score -= 15
            reasons.append(f'葛兰碧跌破卖出: 跌破MA20/MA30+THS<0 ★★')
            sell_signals += 1

        # 6. 反弹不过
        if ((abs(bias10) < 2 or abs(bias20) < 2)
            and latest['close'] < ma10
            and ths_net_today is not None and ths_net_today < 0):
            score -= 10
            reasons.append(f'葛兰碧反弹不过: 反弹至MA10/MA20受阻+THS<0 ★')
            sell_signals += 1

        # 7. 假突破卖出 (THS 无占比, 简化为 ths_net_today < 0)
        if is_breakout and ths_net_today is not None and ths_net_today < 0:
            score -= 15
            reasons.append(f'葛兰碧假突破: 突破但 THS 当日<0 (诱多嫌疑) ★★')
            sell_signals += 1

        # 8. 超涨卖出
        if bias > 15 and ths_net_today is not None and ths_net_today < 0:
            score -= 10
            reasons.append(f'葛兰碧超涨: 偏离MA5 {bias:.1f}%+THS<0 ★')
            sell_signals += 1

        # ---------- 资金流累计信号 ----------
        # THS 无净流入占比, 用绝对值
        if ths_net_today is not None and ths_net_today > 1e8:
            score += 15
            reasons.append(f'当日 THS 净额 {ths_net_today/1e8:.2f}亿 (>1亿 启动信号)')
            buy_signals += 1
        if ths_net_today is not None and ths_net_today < -5e7:
            score -= 15
            reasons.append(f'当日 THS 净流出 {-ths_net_today/1e8:.2f}亿 (短线走弱)')
            sell_signals += 1

        # 3 日累计
        if can_3d:
            v = flows['3d']
            if v > 5e7:
                score += 8
                reasons.append(f'3日累计 THS 净额 {v/1e8:.2f}亿 (波段健康)')
            elif v < -5e7:
                score -= 8
                reasons.append(f'3日累计 THS 净流出 {-v/1e8:.2f}亿 (波段走坏)')
                sell_signals += 1
        else:
            reasons.append(f'3日累计 THS 净额: 待 N≥3天数据 (当前 {flow_days}天)')

        # 5 日累计
        if can_5d:
            v = flows['5d']
            if v > 5e7:
                score += 10
                reasons.append(f'5日累计 THS 净额 {v/1e8:.2f}亿 (波段健康)')
            else:
                score -= 10
                reasons.append(f'5日累计 THS 净流出 {-v/1e8:.2f}亿 (波段走坏)')
                sell_signals += 1
        else:
            reasons.append(f'5日累计 THS 净额: 待 N≥5天数据 (当前 {flow_days}天)')

        # 10 日累计
        if can_10d:
            v = flows['10d']
            if v > 0:
                score += 12
                reasons.append(f'10日累计 THS 净额 {v/1e8:.2f}亿 (中期强势)')
            else:
                score -= 12
                reasons.append(f'10日累计 THS 净流出 {-v/1e8:.2f}亿 (中期转弱)')
                sell_signals += 1
        else:
            reasons.append(f'10日累计 THS 净额: 待 N≥10天数据 (当前 {flow_days}天)')

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

        picks.append({
            'ts_code': ts_code,
            'name': latest['name'] or ts_code.split('.')[0],
            'trade_date': trade_date,
            'close': latest['close'],
            'change_pct': round(change_pct, 2),
            'ma5': round(ma5, 2), 'ma10': round(ma10, 2),
            'ma20': round(ma20, 2), 'ma30': round(ma30, 2),
            'ma5_slope': round(ma5_slope, 1) if ma5_slope is not None else None,
            'ma10_slope': round(ma10_slope, 1) if ma10_slope is not None else None,
            'ma20_slope': round(ma20_slope, 1) if ma20_slope is not None else None,
            'ma30_slope': round(ma30_slope, 1) if ma30_slope is not None else None,
            'ths_net_today': ths_net_today,
            'ths_net_3d': flows.get('3d'),
            'ths_net_5d': flows.get('5d'),
            'ths_net_10d': flows.get('10d'),
            'score': round(score, 1),
            'signal_type': signal_type,
            'flow_days_available': flow_days,
            'reasons': reasons,
        })

    conn.close()
    picks.sort(key=lambda x: x['score'], reverse=True)
    return picks[:100]


@app.route('/api/picks/sync', methods=['POST'])
def api_picks_sync():
    """触发选股推荐计算。支持 ?date=YYYYMMDD 指定日期。

    body.intraday=1 且 date=今天 (盘中, 今天还没收盘入库) 时:
    用 sina 实时价暂代今日 close 参与计算, 结果写 stock_picks(trade_date=今天),
    返回 intraday_used=true. 收盘后 16:00 定时任务会用真实收盘价覆盖重算.
    """
    try:
        req_date = request.json.get('date', '') if request.is_json else ''
        req_intraday = bool(request.json.get('intraday')) if request.is_json else False

        # 盘中模式且当日资金流还没抓过: 先同步一次当日主力净流入 (playwright ~1-2 分钟),
        # 让"当日主力净流入/占比"维度在盘中推荐里也有值. 幂等: 已有当日数据就跳过.
        flow_synced = False
        if req_intraday and req_date == datetime.now().strftime('%Y%m%d'):
            try:
                _fc = sqlite3.connect(DB_PATH)
                _has_flow = _fc.execute(
                    "SELECT 1 FROM fund_flow WHERE trade_date = ? AND source = 'eastmoney-push2' LIMIT 1",
                    (req_date,)).fetchone()
                _fc.close()
                if not _has_flow and not _AKSYNC_STATE.get('running'):
                    print(f'[picks intraday] {req_date} 当日资金流未同步, 先拉取...', flush=True)
                    _AKSYNC_STATE['running'] = True  # 防止主力净流入 tab 同时重复抓
                    try:
                        from fetch_fund_flow_today import fetch_today_market
                        _rows, _total, _failed, _actual = fetch_today_market(
                            progress_callback=None, headless=False, verbose=True, skip_if_exists=True)
                        flow_synced = bool(_rows)
                        print(f'[picks intraday] 当日资金流写入 {_actual}: {len(_rows)} 行', flush=True)
                    finally:
                        _AKSYNC_STATE['running'] = False
            except Exception as e:
                # 资金流拉不到不阻塞推荐 (当日资金维度留空, 累计窗口仍是截至昨日)
                print(f'[picks intraday] 当日资金流同步失败, 继续算推荐: {e}', flush=True)

        picks = _compute_picks(req_date or None, intraday=req_intraday)

        if not picks:
            return jsonify({'status': 'success', 'message': '无推荐股票', 'count': 0, 'intraday_used': False})

        # 保存到数据库
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        if req_date:
            trade_date = req_date
        else:
            latest_row = c.execute('SELECT MAX(trade_date) FROM stock_daily').fetchone()
            trade_date = latest_row[0] if latest_row and latest_row[0] else datetime.now().strftime('%Y%m%d')

        # 是否真的走了盘中口径 (trade_date 还没收盘入库才算)
        intraday_used = False
        if req_intraday:
            has_daily = c.execute(
                'SELECT 1 FROM stock_daily WHERE trade_date = ? LIMIT 1', (trade_date,)).fetchone()
            intraday_used = not has_daily

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
                VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)''',
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
            'date': trade_date,
            'intraday_used': intraday_used,
            'flow_synced': flow_synced
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/picks/ths/sync', methods=['POST'])
def api_picks_ths_sync():
    """触发同花顺选股推荐计算。支持 body.date=YYYYMMDD 指定日期。
    body.intraday=1: 用 sina 实时价参与计算 (盘中模式, 与 EM 同思路).

    注意: 不会自动触发 fetch_ths_market (数据抓取是 5-8 分钟重活, 由 scheduler 17:05/19:30 + 手动触发管).
    若 fund_flow_ths 当日还没入库, 计算会返 0 条, 用户等定时跑完即可.
    """
    try:
        req_date = request.json.get('date', '') if request.is_json else ''
        req_intraday = bool(request.json.get('intraday')) if request.is_json else False

        picks = _compute_picks_ths(req_date or None, intraday=req_intraday)

        if not picks:
            return jsonify({'status': 'success', 'message': '无推荐股票', 'count': 0, 'intraday_used': False})

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # trade_date 优先用 req_date (请求指定), 否则用 picks 自带的 (与 _compute_picks_ths 计算口径一致,
        # THS flow 日期可能领先 stock_daily, 不能 fallback stock_daily 否则日期错乱)
        if req_date:
            trade_date = req_date
        else:
            trade_date = picks[0]['trade_date']

        intraday_used = False
        if req_intraday:
            has_daily = c.execute(
                'SELECT 1 FROM stock_daily WHERE trade_date = ? LIMIT 1', (trade_date,)).fetchone()
            intraday_used = not has_daily

        c.execute('DELETE FROM stock_picks_ths WHERE trade_date = ?', (trade_date,))

        for p in picks:
            c.execute('''INSERT OR REPLACE INTO stock_picks_ths
                (trade_date, ts_code, name, close, change_pct,
                 ma5, ma10, ma20, ma30,
                 ma5_slope, ma10_slope, ma20_slope, ma30_slope,
                 ths_net_today, ths_net_3d, ths_net_5d, ths_net_10d,
                 score, signal_type, flow_days_available, reasons_json)
                VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)''',
                (trade_date, p['ts_code'], p['name'], p['close'], p['change_pct'],
                 p['ma5'], p['ma10'], p['ma20'], p['ma30'],
                 p['ma5_slope'], p['ma10_slope'], p['ma20_slope'], p['ma30_slope'],
                 p['ths_net_today'], p['ths_net_3d'], p['ths_net_5d'], p['ths_net_10d'],
                 p['score'], p['signal_type'], p['flow_days_available'],
                 json.dumps(p['reasons'], ensure_ascii=False)))

        conn.commit()
        conn.close()

        return jsonify({
            'status': 'success',
            'count': len(picks),
            'date': trade_date,
            'intraday_used': intraday_used,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


# ============ 优选分 (edge_score) ============
# 2026-09-29 立项. 背景: 用 6626 条可统计样本 (20260616~20260928, 73 个入选日) 回测发现,
# 现行 `score` 在入选池内对 T+1~T+5 命中率**几乎没有排序能力** (walk-forward 每日 Top20
# 命中率 33.11%, 与随机基准 34.43% 无差异). 优选分是另起炉灶的经验加权分, 只用
# 「训练段/测试段方向一致」的特征, 换掉默认排序.
#
# 验证 (backend/scripts/validate_picks_score_oos.py):
#   walk-forward 每 5 个交易日重训一次, 权重只见过过去的数据 ——
#     每日 Top10: 现行评分 32.26% → 优选分 43.21% (随机 30.38%)
#     每日 Top20: 现行评分 33.11% → 优选分 40.75% (随机 34.43%)
#   按入选日 bootstrap 2000 次, Top20 差值 95%CI [+3.68, +11.70]pp, 不含 0.
#
# 刻意**不**纳入的项 (有共性但不该固化进打分):
#   - 行业 (电子/有色/轻工/传媒 命中率高 8pp): 板块轮动性强, 静态名单会随时间失效,
#     只在前端做展示标签, 不进分数.
#   - 当日主力强度: 训练段 +4.02pp / 测试段 -3.16pp, 符号翻转, 不可靠.
#   - MA5 陡峭 / 5 日均额 / 入选前 20 日涨幅: 方向对但两段幅度差 5 倍以上, 权重不稳.
#
# 口径: 输入全部是入选日当天及之前可得的数据, 无未来函数. 数据缺失按"中性"处理
# (该特征贡献 0), 不用极端值猜.
EDGE_WEIGHTS = {
    'limitup': 0.51659,        # 入选日涨停 (涨跌幅 ≥9.8%) — 最强单项, 训练 +14.6 / 测试 +8.9pp
    'below_ma60': 0.18917,    # 收盘跌破 MA60 — 未过度拉伸, 训练 +5.6 / 测试 +2.9pp
    'low_price': -0.16056,    # 股价 < 20 元 — 低价小票明显差, 训练 -1.7 / 测试 -8.8pp
    'bull3_not4': 0.14785,    # MA5>MA10>MA20 但没站上 MA30 — 启动初期好, 涨透了就差
    'vol_mult': -0.12646,     # 量比 (对前 5 日均量) — 放量反而差, 训练 -1.5 / 测试 -3.8pp
    'drawdown20': -0.04250,   # 距 20 日高点百分比 — 追高惩罚, 训练 -2.4 / 测试 -6.2pp
    'small_pct': -0.01133,    # 小单净额占比 (负=散户净流出) — 散户流出越大越好
    'change_pct': 0.01126,    # 入选日涨跌幅 (线性项)
    'change_pct2': 0.00013,   # 入选日涨跌幅平方 — 与线性项合成 U 型 (0-2% 最差, 涨停最好)
    'prev5': 0.00565,         # 入选前 5 日涨幅 — 有启动迹象的更好
}
EDGE_INTERCEPT = -0.91723


def _attach_edge_scores(picks, trade_date):
    """给 picks 原地挂 edge_score (0~100, 越大越好) + edge_flags (可读标签).

    只依赖 stock_daily 的历史 (入选日往前 60 根) + picks 自带的字段, 因此历史日期也能
    立刻算出来, 不需要回填数据库.
    """
    import math
    if not picks:
        return picks
    codes = [p.get('ts_code') for p in picks if p.get('ts_code')]
    if not codes:
        return picks
    cal_conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    try:
        cal_rows = [r[0] for r in cal_conn.execute(
            'SELECT cal_date FROM trading_dates_cache WHERE cal_date<=? '
            'ORDER BY cal_date DESC LIMIT 61', (trade_date,)).fetchall()]
    finally:
        cal_conn.close()
    cal_rows = sorted(cal_rows)
    if len(cal_rows) < 61:
        return picks
    start_day = cal_rows[0]
    placeholders = ','.join('?' * len(codes))
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            f'SELECT ts_code,trade_date,close,high,volume FROM stock_daily '
            f'WHERE ts_code IN ({placeholders}) AND trade_date>=? AND trade_date<=?',
            [*codes, start_day, trade_date]).fetchall()
    finally:
        conn.close()
    hist = {}
    for r in rows:
        hist.setdefault(r[0], []).append(r)
    for k in hist:
        hist[k].sort(key=lambda x: x[1])

    flags_all = []
    for p in picks:
        p.setdefault('trade_date', trade_date)
        series = hist.get(p.get('ts_code')) or []
        idx = next((i for i, r in enumerate(series) if r[1] == p['trade_date']), -1)
        feats, flags = {}, []
        close = p.get('close')
        chg = p.get('change_pct')
        feats['change_pct'] = chg if chg is not None else 0.0
        feats['change_pct2'] = (chg or 0.0) ** 2
        feats['limitup'] = 1.0 if (chg is not None and chg >= 9.8) else 0.0
        if feats['limitup']:
            flags.append('涨停启动')
        if close is not None and close < 20:
            feats['low_price'] = 1.0
            flags.append('低价股')
        ma5, ma10, ma20, ma30 = p.get('ma5'), p.get('ma10'), p.get('ma20'), p.get('ma30')
        if None not in (ma5, ma10, ma20, ma30):
            bull3 = ma5 > ma10 > ma20
            bull4 = bull3 and ma20 > ma30
            feats['bull3_not4'] = (1.0 if bull3 else 0.0) - (1.0 if bull4 else 0.0)
            if bull3 and not bull4:
                flags.append('三线启动未涨透')
            elif bull4:
                flags.append('多头已涨透')
        if close is not None and idx > 0:
            prior = [r for r in series[:idx] if r[2] and r[2] > 0]
            if len(prior) >= 60:
                ma60 = sum(r[2] for r in prior[-60:]) / 60
                feats['below_ma60'] = 1.0 if close < ma60 else 0.0
                if close < ma60:
                    flags.append('仍在MA60下')
            if len(prior) >= 20:
                hi20 = max(r[3] for r in prior[-20:] if r[3]) or 0
                if hi20 > 0:
                    feats['drawdown20'] = (close / hi20 - 1) * 100
                    if feats['drawdown20'] > -2:
                        flags.append('紧贴20日高')
            vols = [r[4] for r in prior[-5:] if r[4]]
            if len(vols) >= 5 and sum(vols) > 0 and series[idx][4]:
                feats['vol_mult'] = series[idx][4] / (sum(vols) / 5)
            ref5 = prior[-6][2] if len(prior) >= 6 else 0
            if ref5 > 0:
                feats['prev5'] = (close / ref5 - 1) * 100
        sp = p.get('small_pct')
        if sp is not None and math.isfinite(sp):
            feats['small_pct'] = sp
            if sp < -8:
                flags.append('散户大幅流出')
        raw = EDGE_INTERCEPT + sum(EDGE_WEIGHTS.get(k, 0.0) * v for k, v in feats.items())
        # logistic 概率 → 0~100 分, 便于展示和排序
        p['edge_score'] = round(100 / (1 + math.exp(-max(-30, min(30, raw)))), 1)
        p['edge_flags'] = flags
        flags_all.append(flags)
    return picks


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

    # 入选后 T+1~T+5 (收盘复利 + 第N日最高/入选日收盘)
    next_dates, live_next_index = [], 0
    if picks:
        try:
            next_dates, live_next_index = _attach_preferred_outcomes(
                picks, picks[0]['trade_date'], key='post_pick_outcomes')
        except Exception as e:
            print(f'[api_picks] T+N 计算失败 (不影响主流程): {e}')

    # 优选分 (替代 score 做默认排序, 见 _attach_edge_scores 注释)
    try:
        _attach_edge_scores(picks, picks[0]['trade_date'] if picks else '')
    except Exception as e:
        print(f'[api_picks] 优选分计算失败 (不影响主流程): {e}')

    return jsonify({
        'picks': picks,
        'date': rows[0]['trade_date'] if rows else '',
        'count': len(picks),
        'next_dates': next_dates,
        'live_next_index': live_next_index,
    })


@app.route('/api/picks/ths', methods=['GET'])
def api_picks_ths():
    """同花顺选股推荐列表 (镜像 api_picks 但读 stock_picks_ths).
    走 sina 实时价算入选后涨幅, 共用 fetch_sina_quotes 全局缓存.
    """
    limit = min(int(request.args.get('limit', 30)), 100)
    date = request.args.get('date', '').strip()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if date:
        rows = conn.execute(
            'SELECT * FROM stock_picks_ths WHERE trade_date = ? ORDER BY score DESC LIMIT ?',
            (date, limit)
        ).fetchall()
    else:
        latest = conn.execute('SELECT MAX(trade_date) as d FROM stock_picks_ths').fetchone()
        if latest and latest['d']:
            rows = conn.execute(
                'SELECT * FROM stock_picks_ths WHERE trade_date = ? ORDER BY score DESC LIMIT ?',
                (latest['d'], limit)
            ).fetchall()
        else:
            rows = []
    conn.close()

    picks = []
    for r in rows:
        raw_reasons = r['reasons_json'] if 'reasons_json' in r.keys() else (
            r['reasons'] if 'reasons' in r.keys() else ''
        )
        try:
            reasons = json.loads(raw_reasons) if raw_reasons else []
        except Exception:
            reasons = []

        def _f(v):
            return v if v is not None else None

        picks.append({
            'ts_code': r['ts_code'],
            'name': r['name'],
            'trade_date': r['trade_date'],
            'close': r['close'],
            'change_pct': r['change_pct'],
            # MA
            'ma5': _f(r['ma5']) if 'ma5' in r.keys() else None,
            'ma10': _f(r['ma10']) if 'ma10' in r.keys() else None,
            'ma20': _f(r['ma20']) if 'ma20' in r.keys() else None,
            'ma30': _f(r['ma30']) if 'ma30' in r.keys() else None,
            'ma5_slope': _f(r['ma5_slope']) if 'ma5_slope' in r.keys() else None,
            'ma10_slope': _f(r['ma10_slope']) if 'ma10_slope' in r.keys() else None,
            'ma20_slope': _f(r['ma20_slope']) if 'ma20_slope' in r.keys() else None,
            'ma30_slope': _f(r['ma30_slope']) if 'ma30_slope' in r.keys() else None,
            # THS 资金流 (4 窗口)
            'ths_net_today': _f(r['ths_net_today']) if 'ths_net_today' in r.keys() else None,
            'ths_net_3d': _f(r['ths_net_3d']) if 'ths_net_3d' in r.keys() else None,
            'ths_net_5d': _f(r['ths_net_5d']) if 'ths_net_5d' in r.keys() else None,
            'ths_net_10d': _f(r['ths_net_10d']) if 'ths_net_10d' in r.keys() else None,
            # 评分
            'score': r['score'],
            'signal_type': r['signal_type'] if 'signal_type' in r.keys() else 'hold',
            'flow_days_available': r['flow_days_available'] if 'flow_days_available' in r.keys() else 0,
            'reasons': reasons,
        })

    # 板块历史
    _attach_sector(picks, key='ts_code')

    # 入选后涨幅: sina 实时价对比入选当日 close
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
                print(f'[api_picks_ths] sina 拉取失败 (不影响主流程): {e}')

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

    # 入选后 T+1~T+5 (跟系统推荐/条件优选同口径; 我的选股跟系统推荐共用同一张表)
    next_dates, live_next_index = [], 0
    if picks:
        try:
            next_dates, live_next_index = _attach_preferred_outcomes(
                picks, picks[0]['trade_date'], key='post_pick_outcomes')
        except Exception as e:
            print(f'[api_user_picks] T+N 计算失败 (不影响主流程): {e}')

    try:
        _attach_edge_scores(picks, picks[0]['trade_date'] if picks else '')
    except Exception as e:
        print(f'[api_user_picks] 优选分计算失败 (不影响主流程): {e}')

    return jsonify({
        'picks': picks,
        'date': picks[0]['trade_date'] if picks else '',
        'count': len(picks),
        'group': 'user',
        'added_count': len(codes),
        'added_at_map': added_at_map,
        'next_dates': next_dates,
        'live_next_index': live_next_index,
    })


# ============ 条件选股 (按固定规则筛选全市场, 不入库) ============

THRESHOLD_3YI = 3e8  # 至少一项累计 ≥ 3 亿元


def _filter_conditional_base(rows):
    """条件选股基础规则 (与资金门槛无关):
      - 当日涨跌幅必须 < 0
      - close < 200 → 看主力 (sum_main_Nd); close >= 200 → 看超大单 (sum_super_Nd)
      - 4 项累计必须 > 0
    附 used_bucket / sum_3d..20d, 不设 hit_threshold (由 _apply_cond_threshold 按门槛标).
    """
    hits = []
    for r in rows:
        cp = r.get('change_pct')
        close = r.get('close')
        if cp is None or close is None:
            continue
        if cp >= 0:
            continue
        bucket = 'main' if close < 200 else 'super'
        a = r.get(f'sum_{bucket}_3d') or 0
        b = r.get(f'sum_{bucket}_5d') or 0
        c = r.get(f'sum_{bucket}_10d') or 0
        d = r.get(f'sum_{bucket}_20d') or 0
        # 4 项都必须 > 0
        if not (a > 0 and b > 0 and c > 0 and d > 0):
            continue
        r['used_bucket'] = bucket
        r['sum_3d'] = a
        r['sum_5d'] = b
        r['sum_10d'] = c
        r['sum_20d'] = d
        hits.append(r)
    return hits


def _apply_cond_threshold(rows, threshold_amt):
    """按资金门槛过滤 + 标 hit_threshold (实时算和快照直读共用, 保证任意门槛口径一致).
    入参 rows 需已过 _filter_conditional_base (有 sum_3d..20d). 不排序 (调用方定)."""
    out = []
    for r in rows:
        a = r.get('sum_3d') or 0
        b = r.get('sum_5d') or 0
        c = r.get('sum_10d') or 0
        d = r.get('sum_20d') or 0
        # 至少 1 项 ≥ threshold_amt
        max_v = max(a, b, c, d)
        if max_v < threshold_amt:
            continue
        hit = None
        if a >= threshold_amt: hit = '3d'
        elif b >= threshold_amt: hit = '5d'
        elif c >= threshold_amt: hit = '10d'
        elif d >= threshold_amt: hit = '20d'
        r['hit_threshold'] = hit
        out.append(r)
    return out


def _parse_volume_ratio_pct(value, default=80):
    """解析缩量阈值。约定 0 = 关闭缩量过滤 (只保留收盘 ≥ MA10 等其余规则);
    合法开启区间是 1~100, 越界/无法解析一律回落到 default。"""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return default
    if pct == 0:
        return 0
    if pct < 0 or pct > 100 or pct < 1:
        return default
    return pct


def _volume_rule_text(volume_ratio_pct):
    """缩量规则说明 (给前端汇总行/预览图用)。"""
    if not volume_ratio_pct:
        return '缩量过滤已关闭 (不按成交量比剔除)'
    return f'当天成交量 ≤ 前一有效交易记录成交量 × {volume_ratio_pct:g}%'


def _filter_conditional_new(rows, trade_date, volume_ratio_pct=80, conn=None):
    """在既有条件选股结果上叠加收盘价/MA10 与成交量缩量规则。

    volume_ratio_pct == 0 时只跳过"缩量"这一项 (收盘 ≥ MA10 照旧), 开关由前端控制。"""
    volume_filter_on = float(volume_ratio_pct or 0) > 0
    stats = {
        'base_count': len(rows),
        'missing_current_daily': 0,
        'insufficient_history': 0,
        'invalid_volume': 0,
        'below_ma10': 0,
        'volume_not_shrunk': 0,
        'matched': 0,
        'volume_filter_on': volume_filter_on,
    }
    if not rows:
        return [], stats

    codes = list(dict.fromkeys(r.get('ts_code') for r in rows if r.get('ts_code')))
    if not codes:
        stats['missing_current_daily'] = len(rows)
        return [], stats

    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # stock_daily 的唯一索引是 (ts_code, trade_date)。逐代码 LIMIT 10 能直接走索引，
    # 比在 220 万行上执行 ROW_NUMBER 窗口排序快两个数量级。
    daily_rows = []
    for code in codes:
        daily_rows.extend(conn.execute('''
            SELECT ts_code, trade_date, close, volume
            FROM stock_daily
            WHERE ts_code = ? AND trade_date <= ? AND close IS NOT NULL
            ORDER BY trade_date DESC LIMIT 10
        ''', (code, trade_date)).fetchall())
    if owns_conn:
        conn.close()

    by_code = defaultdict(list)
    for daily in daily_rows:
        by_code[daily['ts_code']].append(daily)

    ratio_limit = float(volume_ratio_pct) / 100
    hits = []
    for source_row in rows:
        history = by_code.get(source_row.get('ts_code'), [])
        if not history or history[0]['trade_date'] != trade_date:
            stats['missing_current_daily'] += 1
            continue
        if len(history) < 10:
            stats['insufficient_history'] += 1
            continue
        current, previous = history[0], history[1]
        try:
            current_close = float(current['close'])
            closes = [float(item['close']) for item in history[:10]]
            current_volume = float(current['volume'])
            previous_volume = float(previous['volume'])
        except (TypeError, ValueError):
            stats['invalid_volume'] += 1
            continue
        if current_volume <= 0 or previous_volume <= 0:
            stats['invalid_volume'] += 1
            continue
        ma10 = sum(closes) / 10
        if current_close < ma10:
            stats['below_ma10'] += 1
            continue
        actual_ratio_pct = current_volume / previous_volume * 100
        if volume_filter_on and current_volume > previous_volume * ratio_limit:
            stats['volume_not_shrunk'] += 1
            continue
        item = dict(source_row)
        item.update({
            'close': round(current_close, 4),
            'ma10': round(ma10, 4),
            'volume': round(current_volume, 4),
            'prev_volume': round(previous_volume, 4),
            'volume_ratio_pct': round(actual_ratio_pct, 2),
            'volume_shrink_pct': round((1 - current_volume / previous_volume) * 100, 2),
        })
        hits.append(item)
    stats['matched'] = len(hits)
    return hits, stats


def _filter_conditional_new_intraday(rows, trade_date, volume_ratio_pct=80):
    """盘中版 MA10/缩量过滤: 用 sina 实时价当今天收盘、盘中累计量当今天量,
    stock_daily 取今天之前的最近 9 根补齐 10 日窗口。盘后请用 _filter_conditional_new。
    返回 (hits, stats, quotes), quotes 供优选复用 (同一批代码, 走 30s 全局缓存)。"""
    stats = {
        'base_count': len(rows),
        'missing_current_daily': 0,   # 盘中含义: 无实时报价
        'insufficient_history': 0,
        'invalid_volume': 0,
        'below_ma10': 0,
        'volume_not_shrunk': 0,
        'matched': 0,
        'volume_filter_on': float(volume_ratio_pct or 0) > 0,
    }
    if not rows:
        return [], stats, {}
    codes = list(dict.fromkeys(r.get('ts_code') for r in rows if r.get('ts_code')))
    if not codes:
        return [], stats, {}
    try:
        quotes = fetch_sina_quotes(codes)
    except Exception as _e:
        print(f'[conditional new intraday] sina 拉取异常: {_e}', flush=True)
        quotes = {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    history = {}
    for code in codes:
        history[code] = conn.execute('''
            SELECT close, volume FROM stock_daily
            WHERE ts_code = ? AND trade_date < ? AND close IS NOT NULL
            ORDER BY trade_date DESC LIMIT 9
        ''', (code, trade_date)).fetchall()
    conn.close()

    ratio_limit = float(volume_ratio_pct) / 100
    hits = []
    for source_row in rows:
        quote = quotes.get(source_row.get('ts_code')) or {}
        price = quote.get('price')
        if price is None or price <= 0:
            stats['missing_current_daily'] += 1
            continue
        prev = history.get(source_row.get('ts_code')) or []
        if len(prev) < 9:
            stats['insufficient_history'] += 1
            continue
        try:
            closes = [float(price)] + [float(h['close']) for h in prev]
            current_volume = float(quote.get('volume') or 0)
            previous_volume = float(prev[0]['volume'])
        except (TypeError, ValueError):
            stats['invalid_volume'] += 1
            continue
        if current_volume <= 0 or previous_volume <= 0:
            stats['invalid_volume'] += 1
            continue
        ma10 = sum(closes) / 10
        if price < ma10:
            stats['below_ma10'] += 1
            continue
        # volume_ratio_pct == 0 = 关闭缩量过滤 (前端开关), 只跳过这一项, 收盘 ≥ MA10 照旧
        if stats['volume_filter_on'] and current_volume > previous_volume * ratio_limit:
            stats['volume_not_shrunk'] += 1
            continue
        item = dict(source_row)
        item.update({
            'close': round(float(price), 4),
            'ma10': round(ma10, 4),
            'volume': round(current_volume, 4),
            'prev_volume': round(previous_volume, 4),
            'volume_ratio_pct': round(current_volume / previous_volume * 100, 2),
            'volume_shrink_pct': round((1 - current_volume / previous_volume) * 100, 2),
        })
        hits.append(item)
    stats['matched'] = len(hits)
    return hits, stats, quotes


# 东方财富资金流的两个来源标签, 口径已逐字段验证一致 (见 backfill_flow_daykline.py 头部说明):
#   eastmoney-push2    每日 clist/get 抓的「当日排行」, 只有 20260616 之后
#   eastmoney-daykline 逐股 daykline 补的历史, 只有 20260615 及之前
# 两段在库里有重叠区, 拼接使用, 但**任何按 source 过滤的查询都会把其中一段整段漏掉**
# (5 月的选股全空就是这么来的). 下面两个辅助函数是唯一的取数入口, 别再硬编码 source.
EM_FLOW_SOURCES = ('eastmoney-push2', 'eastmoney-daykline')


def _em_flow_source_for_date(conn, ymd):
    """返回该交易日实际用哪个源: 优先 push2, 没有则退回 daykline.

    重叠区统一取 push2 (每日任务的实时口径, 跟前端展示一致), 只有 push2 缺的日子
    才用 daykline 兜底. 两个源都缺返回 None —— 调用方要当缺数据处理, 不能当成空结果.
    """
    if not ymd:
        return None
    row = conn.execute(
        'SELECT source FROM fund_flow WHERE trade_date = ? '
        'AND source IN (?, ?) AND main_net_inflow IS NOT NULL AND main_net_inflow != 0 '
        'ORDER BY CASE source WHEN ? THEN 0 ELSE 1 END LIMIT 1',
        (ymd,) + EM_FLOW_SOURCES + (EM_FLOW_SOURCES[0],)
    ).fetchone()
    return row[0] if row else None


def _resolve_conditional_new_date(date_value, source):
    """按条件选股的资金流口径确定目标交易日。"""
    if date_value:
        return date_value
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        'SELECT MAX(trade_date) FROM fund_flow '
        'WHERE source IN (?, ?) AND main_net_inflow IS NOT NULL AND main_net_inflow != 0',
        EM_FLOW_SOURCES
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else ''


def _conditional_new_daily_ready(trade_date, now=None, conn=None):
    """确认目标日已存在收盘日线；今日还要求已收市且入库规模基本完整。"""
    if not trade_date:
        return False, 'missing_date'
    now = now or datetime.now()
    today = now.strftime('%Y%m%d')
    if trade_date > today:
        return False, 'future_date'
    if trade_date == today and (now.hour, now.minute) < (15, 5):
        return False, 'market_not_closed'

    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(DB_PATH)
    current_count = conn.execute(
        'SELECT COUNT(*) FROM stock_daily WHERE trade_date = ? AND close IS NOT NULL',
        (trade_date,)
    ).fetchone()[0]
    if current_count <= 0:
        if owns_conn:
            conn.close()
        return False, 'daily_not_loaded'
    if trade_date == today:
        previous = conn.execute(
            'SELECT trade_date, COUNT(*) AS c FROM stock_daily '
            'WHERE trade_date < ? AND close IS NOT NULL GROUP BY trade_date '
            'ORDER BY trade_date DESC LIMIT 1',
            (trade_date,)
        ).fetchone()
        if previous and previous[1] and current_count < previous[1] * 0.9:
            if owns_conn:
                conn.close()
            return False, 'daily_incomplete'
    if owns_conn:
        conn.close()
    return True, 'ready'


def _filter_conditional(rows, threshold_yi=THRESHOLD_3YI):
    """应用条件选股规则 (基础规则 + 门槛), 返回命中行 (附 used_bucket / hit_threshold)."""
    return _apply_cond_threshold(_filter_conditional_base(rows), threshold_yi)


# ============ 同花顺条件选股 (独立口径, 用 fund_flow_ths.net) ============
# 跟 EM 版最大区别:
#   ① THS 没有 5 档分摊, 全部只看 net (净额), 无 close<200/≥200 价格桶
#   ② THS 历史太短 (2026-08-20 才开始), 只算 3d/5d/10d 累计, 跳过 20d
#   ③ 没有 sum_mid_small_* (THS 无中小单字段)
# 触发: api_picks_ths_conditional + api_picks_ths_combined, 复用 _compute_conditional_picks_internal_ths


def _filter_conditional_base_ths(rows):
    """THS 条件选股基础规则 (无价格桶, 无中小单维度):
      - 当日涨跌幅必须 < 0
      - 3 项 net 累计 (3d/5d/10d) 必须 > 0
    附 used_bucket='net' (统一口径, 前端档位列固定显示), 不设 hit_threshold.
    fund_flow_ths 表已有 name/code, 但 _compute 不 SELECT, 此处一次性回查补齐.
    """
    hits = []
    ts_codes = list({r['ts_code'] for r in rows if r.get('ts_code')})
    name_map, code_map = {}, {}
    if ts_codes:
        ph = ','.join('?' * len(ts_codes))
        with sqlite3.connect(DB_PATH) as _c:
            for ts, name, code in _c.execute(
                f'SELECT ts_code, name, code FROM fund_flow_ths WHERE ts_code IN ({ph})',
                ts_codes).fetchall():
                name_map[ts] = name
                code_map[ts] = code
    for r in rows:
        cp = r.get('change_pct')
        close = r.get('close')
        if cp is None or close is None:
            continue
        if cp >= 0:
            continue
        a = r.get('sum_net_3d') or 0
        b = r.get('sum_net_5d') or 0
        c = r.get('sum_net_10d') or 0
        if not (a > 0 and b > 0 and c > 0):
            continue
        r['used_bucket'] = 'net'
        r['sum_3d'] = a
        r['sum_5d'] = b
        r['sum_10d'] = c
        r['sum_20d'] = None  # THS 历史太短没 20d, 前端 _fmtCondFlow 会渲染成 —
        ts = r.get('ts_code')
        if ts in name_map:
            r['name'] = name_map[ts]
        if ts in code_map:
            r['code'] = code_map[ts]
        hits.append(r)
    return hits


def _apply_cond_threshold_ths(rows, threshold_amt):
    """THS 版门槛过滤: 至少 1 项 (3d/5d/10d) ≥ threshold_amt."""
    out = []
    for r in rows:
        a = r.get('sum_3d') or 0
        b = r.get('sum_5d') or 0
        c = r.get('sum_10d') or 0
        max_v = max(a, b, c)
        if max_v < threshold_amt:
            continue
        hit = None
        if a >= threshold_amt: hit = '3d'
        elif b >= threshold_amt: hit = '5d'
        elif c >= threshold_amt: hit = '10d'
        r['hit_threshold'] = hit
        out.append(r)
    return out


def _compute_conditional_picks_internal_ths(date, intraday_close=False):
    """THS 条件选股 raw 数据管道 (SQL+后处理).
    直接读 fund_flow_ths (不走 ranked 物化表 — THS 单源, 没 5 档, ranked 物化没意义).

    Returns:
        (data, next_dates) tuple.
        data: list[dict] 含 sum_net_3d/5d/10d + next_1..5_close/change_pct/high.
        next_dates: list[NEXT_DAYS] T+1..T+5 trade_date.
        交易日窗口 < 3 天时返回 (None, [None]*NEXT_DAYS).
    """
    NEXT_DAYS = 5
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1) window_dates: 入选日及入选日前 19 个交易日 (10d 累计需要)
    window_dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM fund_flow_ths WHERE trade_date <= ? '
        'ORDER BY trade_date DESC LIMIT 19',
        (date,)).fetchall()]
    if len(window_dates) < 3:
        conn.close()
        return None, [None] * NEXT_DAYS

    # 2) 入选日 T 的下 1..5 个交易日 (工作日历推算)
    next_dates, _cal_ok = _next_trading_days(date, NEXT_DAYS)
    if len(next_dates) < NEXT_DAYS:
        print('[ths cond] 交易日历覆盖不足, 兜底 stock_daily/fund_flow_ths', flush=True)
        cur_lower = date
        next_dates = []
        for _ in range(NEXT_DAYS):
            row = conn.execute(
                'SELECT MIN(trade_date) AS d FROM stock_daily WHERE trade_date > ?',
                (cur_lower,)).fetchone()
            nd = row['d'] if row and row['d'] else None
            if not nd:
                nd = (conn.execute(
                    'SELECT MIN(trade_date) AS d FROM fund_flow_ths WHERE trade_date > ?',
                    (cur_lower,)).fetchone() or {}).get('d')
            next_dates.append(nd)
            if not nd:
                next_dates += [None] * (NEXT_DAYS - len(next_dates))
                break
            cur_lower = nd

    placeholders = ','.join(['?' for _ in window_dates])
    # next_day CTE
    cte_parts = []
    for i, d in enumerate(next_dates, start=1):
        if d:
            cte_parts.append(f"""
                next_day_{i} AS (
                    SELECT ts_code AS next_{i}_ts,
                           MAX(close) AS next_{i}_close,
                           MAX(change) AS next_{i}_change_pct,
                           MAX(high) AS next_{i}_high
                    FROM stock_daily
                    WHERE trade_date = ?
                    GROUP BY ts_code
                )""")
        else:
            cte_parts.append(f"""
                next_day_{i} AS (
                    SELECT NULL AS next_{i}_ts, NULL AS next_{i}_close,
                           NULL AS next_{i}_change_pct, NULL AS next_{i}_high
                    WHERE 0
                )""")
    ctes_sql = ','.join(cte_parts)
    select_next_cols = ', '.join(
        f'n{i}.next_{i}_close, n{i}.next_{i}_change_pct, n{i}.next_{i}_high'
        for i in range(1, NEXT_DAYS + 1)
    )
    join_parts = ' '.join(f'LEFT JOIN next_day_{i} n{i} ON a.ts_code = n{i}.next_{i}_ts'
                          for i in range(1, NEXT_DAYS + 1))

    sql = f"""
        WITH target_d AS (
            SELECT ts_code, close, change_pct
            FROM fund_flow_ths WHERE trade_date = ?
        ),
        ranked AS (
            SELECT f.ts_code, f.trade_date, f.close, f.change_pct, f.net,
                   ROW_NUMBER() OVER (PARTITION BY f.ts_code ORDER BY f.trade_date DESC) AS window_rn
            FROM fund_flow_ths f
            WHERE f.trade_date IN ({placeholders})
        ),
        agg AS (
            SELECT r.ts_code,
                   MAX(td.close)      AS close,
                   MAX(td.change_pct) AS change_pct,
                   MAX(r.trade_date)  AS trade_date,
                   SUM(CASE WHEN window_rn <= 3  THEN net END) AS sum_net_3d,
                   SUM(CASE WHEN window_rn <= 5  THEN net END) AS sum_net_5d,
                   SUM(CASE WHEN window_rn <= 10 THEN net END) AS sum_net_10d
            FROM ranked r
            INNER JOIN target_d td ON td.ts_code = r.ts_code
            GROUP BY r.ts_code
        ),
        {ctes_sql}
        SELECT a.ts_code, a.close, a.change_pct, a.trade_date,
               a.sum_net_3d, a.sum_net_5d, a.sum_net_10d,
               {select_next_cols}
        FROM agg a
        {join_parts}
    """
    sql_args = [date] + list(window_dates)
    for d in next_dates:
        if d:
            sql_args += [d]
    rows = conn.execute(sql, sql_args).fetchall()
    conn.close()

    data = [dict(r) for r in rows]
    # 入选日=今天但 fund_flow_ths.close 还没入库: 用 sina 实时价补齐 (30s 全局缓存)
    today = datetime.now().strftime('%Y%m%d')
    if date == today and any(d.get('close') is None or d.get('change_pct') is None for d in data):
        try:
            _quotes = fetch_sina_quotes([d['ts_code'] for d in data])
            _filled = 0
            for d in data:
                if d.get('close') is not None and d.get('change_pct') is not None:
                    continue
                q = _quotes.get(d['ts_code'])
                if q and q.get('price') and q['price'] > 0:
                    d['close'] = q['price']
                    d['change_pct'] = q['change_pct']
                    d['_selday_live'] = True
                    _filled += 1
        except Exception as _e:
            print(f'[ths cond] 入选日实时价补齐失败: {_e}', flush=True)
    data = [d for d in data if d.get('close') is not None and d.get('change_pct') is not None]
    # T+1..T+N 涨幅
    def _daily_or_fallback(d, i):
        i = str(i)
        cp = d.get(f'next_{i}_change_pct')
        if cp is not None:
            return round(cp, 2)
        nc = d.get(f'next_{i}_close')
        if nc is not None and d.get('close'):
            return round((nc - d['close']) / d['close'] * 100, 2)
        return None
    for d in data:
        for i in range(1, NEXT_DAYS + 1):
            if i == 1:
                d['post_pick_next_change_pct'] = _daily_or_fallback(d, i)
            else:
                d[f'post_pick_next{i}_change_pct'] = _daily_or_fallback(d, i)
        for i in range(1, NEXT_DAYS + 1):
            if next_dates[i - 1] is None:
                d[f'max_high_t{i}'] = None
    return data, next_dates


def _compute_conditional_picks_internal(date, source, intraday_close=False):
    """条件选股 raw 数据管道 (SQL+后处理).
    供 api_conditional_picks 和 api_combined_picks 共用, 避免 ~150 行 SQL 重复.

    Args:
        date: 已归一化的入选日 (YYYYMMDD)
        source: 已归一化的数据源

    Returns:
        (data, next_dates) tuple.
        data: list[dict] 全市场每只股的 sum_main_*/sum_super_*/sum_mid_small_*/
              next_1..5_close 等字段 (未经过 _filter_conditional 过滤).
        next_dates: list[NEXT_DAYS] 入选后 T+1..T+5 trade_date (None 表示无数据).
        当交易日窗口 < 3 天时返回 (None, [None]*NEXT_DAYS).
    """
    NEXT_DAYS = 5
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1.5) 自愈: fund_flow 有该日数据但 fund_flow_ranked 物化表缺 → 增量补建.
    #      ranked 表只在启动时全量 rebuild, 盘中/盘后新同步的数据不会自动进表,
    #      不补的话条件选股会静默返回 0 命中 (2026-07-28 实际踩过).
    _ff_has = conn.execute(
        'SELECT 1 FROM fund_flow WHERE trade_date = ? AND source = ? LIMIT 1',
        (date, source)).fetchone()
    _rk_has = conn.execute(
        'SELECT 1 FROM fund_flow_ranked WHERE trade_date = ? AND source = ? LIMIT 1',
        (date, source)).fetchone()
    if _ff_has and not _rk_has:
        print(f'[conditional] fund_flow_ranked 缺 {date}, 增量补建...', flush=True)
        build_fund_flow_ranked(since_date=date)
    elif _rk_has:
        # 盘中补建时 stock_daily 还没入库 → 该日 close 全 NULL 落库 (2026-08-06 踩过:
        # 隔天再看该日, sina 兜底不触发, NULL 行全被过滤 → 0 命中覆盖好快照).
        # 日线到位后这里自愈: close 全 NULL 且 stock_daily 已有该日 → 增量重建.
        _nc = conn.execute(
            'SELECT COUNT(*) AS n, SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END) AS nn '
            'FROM fund_flow_ranked WHERE trade_date = ? AND source = ?',
            (date, source)).fetchone()
        if _nc and _nc['n'] and _nc['nn'] == _nc['n']:
            _sd_has = conn.execute(
                'SELECT 1 FROM stock_daily WHERE trade_date = ? LIMIT 1', (date,)).fetchone()
            if _sd_has:
                print(f'[conditional] fund_flow_ranked {date} close 全 NULL 且日线已入库, 增量重建自愈...', flush=True)
                build_fund_flow_ranked(since_date=date)

    # 2) window_dates: 入选日及入选日前 20 天
    window_dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM fund_flow WHERE trade_date <= ? AND source = ? '
        'ORDER BY trade_date DESC LIMIT 20',
        (date, source)
    ).fetchall()]
    if len(window_dates) < 3:
        conn.close()
        return None, [None] * NEXT_DAYS

    # 3) 入选日 T 的下 1..5 个交易日
    next_dates = []
    # 真实交易日历推算 (跳周末 + 法定节假日, 与 _attach_preferred_outcomes / 顶栏标注同源)
    next_dates, _cal_ok = _next_trading_days(date, NEXT_DAYS)
    if len(next_dates) < NEXT_DAYS:
        # 历表不可用 → 兜底查 stock_daily; 再不行 fallback fund_flow
        print('[next_dates] 交易日历覆盖不足, 兜底 stock_daily/fund_flow', flush=True)
        cur_lower = date
        next_dates = []
        for _ in range(NEXT_DAYS):
            row = conn.execute(
                'SELECT MIN(trade_date) AS d FROM stock_daily WHERE trade_date > ?',
                (cur_lower,)
            ).fetchone()
            nd = row['d'] if row and row['d'] else None
            if not nd:
                row = conn.execute(
                    'SELECT MIN(trade_date) AS d FROM fund_flow WHERE trade_date > ? AND source = ?',
                    (cur_lower, source)
                ).fetchone()
                nd = row['d'] if row and row['d'] else None
            next_dates.append(nd)
            if not nd:
                next_dates += [None] * (NEXT_DAYS - len(next_dates))
                break
            cur_lower = nd

    placeholders = ','.join(['?' for _ in window_dates])
    # 动态生成 N 个 next_day CTE — 优先 stock_daily (权威日线)
    cte_parts = []
    for i, d in enumerate(next_dates, start=1):
        if d:
            cte_parts.append(f"""
                next_day_{i} AS (
                    SELECT ts_code AS next_{i}_ts,
                           MAX(close) AS next_{i}_close,
                           MAX(change) AS next_{i}_change_pct,
                           MAX(high) AS next_{i}_high
                    FROM stock_daily
                    WHERE trade_date = ?
                    GROUP BY ts_code
                )""")
        else:
            cte_parts.append(f"""
                next_day_{i} AS (
                    SELECT NULL AS next_{i}_ts, NULL AS next_{i}_close, NULL AS next_{i}_change_pct, NULL AS next_{i}_high
                    WHERE 0
                )""")
    ctes_sql = ','.join(cte_parts)

    select_next_cols = ', '.join(
        f'n{i}.next_{i}_close, n{i}.next_{i}_change_pct, n{i}.next_{i}_high' for i in range(1, NEXT_DAYS + 1)
    )
    join_parts = ' '.join(f'LEFT JOIN next_day_{i} n{i} ON a.ts_code = n{i}.next_{i}_ts' for i in range(1, NEXT_DAYS + 1))
    # max_high_t{i} = 入选后第 i 个交易日那天的 high
    max_high_select = ', '.join(
        f'n{i}.next_{i}_high AS max_high_t{i}'
        for i in range(1, NEXT_DAYS + 1)
    )

    sql = f"""
        WITH target_rn AS (
            SELECT ts_code, rn AS target_rn
            FROM fund_flow_ranked
            WHERE trade_date = ? AND source = ?
        ),
        ranked AS (
            SELECT r.ts_code, r.code, r.name, r.trade_date,
                   r.close, r.change_pct, r.high,
                   r.main_net_inflow, r.super_net, r.mid_net, r.small_net,
                   ROW_NUMBER() OVER (PARTITION BY r.ts_code ORDER BY r.trade_date DESC) AS window_rn
            FROM fund_flow_ranked r
            INNER JOIN target_rn t ON t.ts_code = r.ts_code
            WHERE r.trade_date IN ({placeholders}) AND r.source = ?
              AND r.rn BETWEEN t.target_rn AND t.target_rn + 19
        ),
        post_close AS (
            SELECT ts_code, close AS post_close FROM ranked WHERE window_rn = 1
        ),
        agg AS (
            SELECT r.ts_code, MAX(r.code) AS code, MAX(r.name) AS name,
                   MAX(CASE WHEN window_rn = 1 THEN close       END) AS close,
                   MAX(CASE WHEN window_rn = 1 THEN change_pct  END) AS change_pct,
                   MAX(trade_date) AS trade_date,
                   SUM(CASE WHEN window_rn <= 3  THEN main_net_inflow END) AS sum_main_3d,
                   SUM(CASE WHEN window_rn <= 5  THEN main_net_inflow END) AS sum_main_5d,
                   SUM(CASE WHEN window_rn <= 10 THEN main_net_inflow END) AS sum_main_10d,
                   SUM(CASE WHEN window_rn <= 20 THEN main_net_inflow END) AS sum_main_20d,
                   SUM(CASE WHEN window_rn <= 3  THEN super_net       END) AS sum_super_3d,
                   SUM(CASE WHEN window_rn <= 5  THEN super_net       END) AS sum_super_5d,
                   SUM(CASE WHEN window_rn <= 10 THEN super_net       END) AS sum_super_10d,
                   SUM(CASE WHEN window_rn <= 20 THEN super_net       END) AS sum_super_20d,
                   SUM(CASE WHEN window_rn <= 3  THEN CASE WHEN pc.post_close < 200 THEN COALESCE(small_net,0)
                                                   ELSE COALESCE(mid_net,0) + COALESCE(small_net,0) END END) AS sum_mid_small_3d,
                   SUM(CASE WHEN window_rn <= 5  THEN CASE WHEN pc.post_close < 200 THEN COALESCE(small_net,0)
                                                   ELSE COALESCE(mid_net,0) + COALESCE(small_net,0) END END) AS sum_mid_small_5d,
                   SUM(CASE WHEN window_rn <= 10 THEN CASE WHEN pc.post_close < 200 THEN COALESCE(small_net,0)
                                                   ELSE COALESCE(mid_net,0) + COALESCE(small_net,0) END END) AS sum_mid_small_10d,
                   SUM(CASE WHEN window_rn <= 20 THEN CASE WHEN pc.post_close < 200 THEN COALESCE(small_net,0)
                                                   ELSE COALESCE(mid_net,0) + COALESCE(small_net,0) END END) AS sum_mid_small_20d,
                   MAX(CASE WHEN window_rn BETWEEN 2  AND 2  THEN high END) AS max_high_t1,
                   MAX(CASE WHEN window_rn BETWEEN 2  AND 3  THEN high END) AS max_high_t2,
                   MAX(CASE WHEN window_rn BETWEEN 2  AND 4  THEN high END) AS max_high_t3,
                   MAX(CASE WHEN window_rn BETWEEN 2  AND 5  THEN high END) AS max_high_t4,
                   MAX(CASE WHEN window_rn BETWEEN 2  AND 6  THEN high END) AS max_high_t5
            FROM ranked r
            INNER JOIN post_close pc ON pc.ts_code = r.ts_code
            GROUP BY r.ts_code
        ),
        {ctes_sql}
        SELECT a.ts_code, a.code, a.name, a.close, a.change_pct, a.trade_date,
               a.sum_main_3d, a.sum_main_5d, a.sum_main_10d, a.sum_main_20d,
               a.sum_super_3d, a.sum_super_5d, a.sum_super_10d, a.sum_super_20d,
               a.sum_mid_small_3d, a.sum_mid_small_5d, a.sum_mid_small_10d, a.sum_mid_small_20d,
               {max_high_select},
               {select_next_cols}
        FROM agg a
        {join_parts}
        GROUP BY a.ts_code, a.code, a.name, a.close, a.change_pct, a.trade_date,
                 a.sum_main_3d, a.sum_main_5d, a.sum_main_10d, a.sum_main_20d,
                 a.sum_super_3d, a.sum_super_5d, a.sum_super_10d, a.sum_super_20d,
                 a.sum_mid_small_3d, a.sum_mid_small_5d, a.sum_mid_small_10d, a.sum_mid_small_20d
    """
    sql_args = [date, source] + list(window_dates) + [source]
    for d in next_dates:
        if d:
            sql_args += [d]
    rows = conn.execute(sql, sql_args).fetchall()
    print(f'[DEBUG api_cond] rows={len(rows)}', flush=True)
    conn.close()

    data = [dict(r) for r in rows]
    # 入选日=今天且 stock_daily 还没收盘入库: close/change_pct 全是 NULL,
    # 不补的话下面过滤后 0 命中. 拉 sina 实时价补齐 (30s 全局缓存,
    # 刚在系统推荐同步过的话基本全命中缓存, 否则全市场 ~25s)
    today = datetime.now().strftime('%Y%m%d')
    if date == today and any(d.get('close') is None or d.get('change_pct') is None for d in data):
        try:
            _quotes = fetch_sina_quotes([d['ts_code'] for d in data])
            _filled = 0
            for d in data:
                if d.get('close') is not None and d.get('change_pct') is not None:
                    continue
                q = _quotes.get(d['ts_code'])
                if q and q.get('price') and q['price'] > 0:
                    d['close'] = q['price']
                    d['change_pct'] = q['change_pct']
                    d['_selday_live'] = True
                    _filled += 1
            print(f'[conditional] 入选日 {date} 用 sina 实时价补齐 {_filled}/{len(data)} 只', flush=True)
        except Exception as _e:
            print(f'[conditional] 入选日实时价补齐失败: {_e}', flush=True)
    # 排除 close/change_pct 异常
    data = [d for d in data if d.get('close') is not None and d.get('change_pct') is not None]
    # T+1..T+N 涨幅: daily 优先, 兜底 (next_close - close) / close
    # (盘中实时价覆盖挪到 _apply_intraday_next, 只作用于最终入选的几十只票,
    #  在这里做会拉全市场 5500 只 ≈ 110 个 sina batch ≈ 25s, 2026-07-29 实测)
    def _daily_or_fallback(d, i):
        i = str(i)
        cp = d.get(f'next_{i}_change_pct')
        if cp is not None:
            return round(cp, 2)
        nc = d.get(f'next_{i}_close')
        if nc is not None and d.get('close'):
            return round((nc - d['close']) / d['close'] * 100, 2)
        return None
    for d in data:
        for i in range(1, NEXT_DAYS + 1):
            if i == 1:
                d['post_pick_next_change_pct'] = _daily_or_fallback(d, i)
            else:
                d[f'post_pick_next{i}_change_pct'] = _daily_or_fallback(d, i)
        # 入选日是最新交易日时, max_high_t{i} 应当 NULL 而非"窗口反向高价"
        for i in range(1, NEXT_DAYS + 1):
            if next_dates[i - 1] is None:
                d[f'max_high_t{i}'] = None
    return data, next_dates


def _apply_intraday_next(rows, next_dates, today=None, quotes=None, has_daily=None):
    """若任一 T+N 是今天且今天日线还没入库: 只对最终入选的 rows (几十只) 拉 sina 实时价,
    覆盖 next_i_close / next_i_change_pct / post_pick_next{i}_change_pct / max_high_t{i},
    保证今天所在的 T+1～T+5 在 daily 入库前有盘中值.

    盘后口径: stock_daily 已有今天数据时直接跳过 — SQL 里 next_day CTE 给的
    TuShare 收盘价/最高价才是权威值, 不能用 sina 覆盖 (2026-08-04 用户确认:
    盘中用新浪, 盘后用 TuShare).

    返回是否实际做了 sina 覆盖.
    """
    from datetime import datetime as _dt
    today = today or _dt.now().strftime('%Y%m%d')
    idxs = [i for i, d in enumerate(next_dates, start=1) if d == today]
    if not idxs or not rows:
        return False
    if has_daily is None:
        try:
            _c = sqlite3.connect(DB_PATH)
            has_daily = _c.execute(
                'SELECT 1 FROM stock_daily WHERE trade_date = ? LIMIT 1', (today,)).fetchone()
            _c.close()
        except Exception:
            has_daily = None
    if has_daily:
        print(f'[intraday next] {today} 日线已入库, 跳过 sina 覆盖 (盘后 TuShare 口径)', flush=True)
        return False
    if quotes is None:
        quotes = fetch_sina_quotes([r['ts_code'] for r in rows if r.get('ts_code')])
    updated = 0
    for d in rows:
        q = quotes.get(d.get('ts_code'))
        if not q or q.get('price') is None or not q.get('prev_close') or q['prev_close'] <= 0:
            continue
        p = float(q['price'])
        qp = float(q['prev_close'])
        for i in idxs:
            d[f'next_{i}_close'] = round(p, 2)
            daily_pct = q.get('change_pct')
            if daily_pct is None:
                daily_pct = (p - qp) / qp * 100
            daily_pct = round(float(daily_pct), 2)
            d[f'next_{i}_change_pct'] = daily_pct
            if i == 1:
                d['post_pick_next_change_pct'] = daily_pct
            else:
                d[f'post_pick_next{i}_change_pct'] = daily_pct
            # 累计最高价保留前 N-1 日高点，再并入今天截至当前的日高。
            live_high = float(q['high']) if q.get('high') else None
            previous_high = d.get(f'max_high_t{i - 1}') if i > 1 else None
            highs = [float(value) for value in (previous_high, live_high) if value is not None]
            d[f'max_high_t{i}'] = round(max(highs), 2) if highs else None
            updated += 1
    print(f'[intraday next] today={today} idxs={idxs} updated={updated}/{len(rows)} (sina 盘中口径)', flush=True)
    return updated > 0


def _enrich_intraday_payload(payload, today=None, quotes=None, has_daily=None):
    """给快照响应补齐“今天所在的 T+N”，而不仅限于 T+1。"""
    if not payload:
        return payload
    from datetime import datetime as _dt
    today = today or _dt.now().strftime('%Y%m%d')
    next_dates = [payload.get('next_date')] + [payload.get(f'next_date_{i}') for i in range(2, 6)]
    live_index = next((i for i, day in enumerate(next_dates, start=1) if day == today), 0)
    payload['live_next_index'] = live_index
    payload['next_1_is_today'] = live_index == 1
    if live_index:
        used = _apply_intraday_next(payload.get('results') or [], next_dates,
                                    today=today, quotes=quotes, has_daily=has_daily)
        # 快照中可能残留旧 intraday_used 标记；当前是否真正补到报价以本次结果为准。
        payload['intraday_used'] = bool(used)
    return payload


def _next_workdays_weekend_only(ymd, n):
    """纯周末顺延 (不含法定节假日) —— 只作 _next_workdays 的降级兜底, 不要直接用."""
    from datetime import date as _d, timedelta as _td
    out = []
    cur = _d(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    for _ in range(n):
        cur = cur + _td(days=1)
        while cur.weekday() >= 5:
            cur = cur + _td(days=1)
        out.append(cur.strftime('%Y%m%d'))
    return out


def _trading_calendar_max(conn):
    row = conn.execute('SELECT MAX(cal_date) FROM trading_dates_cache').fetchone()
    return row[0] if row and row[0] else ''


def _extend_trading_calendar(horizon, conn=None):
    """把 trading_dates_cache 向后补到 horizon (YYYYMMDD), 含未来日期 (交易所已公布全年日历).

    历表原本只灌到当天, 导致跨节假日时 T+N 推不出来 (如节后首个交易日缺失).
    TuShare 不可用时静默跳过 —— 调用方会退回纯周末顺延, 不因日历缺失而报错."""
    owns = conn is None
    if owns:
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
        except Exception:
            return False
    try:
        if _trading_calendar_max(conn) >= horizon:
            return True
        start = _trading_calendar_max(conn) or horizon
        try:
            pro = get_pro()
            df = pro.trade_cal(exchange='SSE', start_date=start, end_date=horizon)
            dates = df[df['is_open'] == 1]['cal_date'].tolist() if df is not None else []
        except Exception as exc:
            print(f'[trading calendar] TuShare 补日历失败 ({start}→{horizon}): {exc}', flush=True)
            return False
        if not dates:
            return False
        conn.executemany('INSERT OR IGNORE INTO trading_dates_cache (cal_date) VALUES (?)',
                         [(d,) for d in dates])
        conn.commit()
        print(f'[trading calendar] 已补 {len(dates)} 个交易日 → {max(dates)}', flush=True)
        return True
    except Exception as exc:
        print(f'[trading calendar] 写入失败: {exc}', flush=True)
        return False
    finally:
        if owns:
            try:
                conn.close()
            except Exception:
                pass


_TRADING_CAL_LAST_ATTEMPT = 0.0
_TRADING_CAL_THROTTLE_SEC = 600.0


def _trading_days_after(ymd, n):
    """取 ymd 之后的前 n 个真实交易日 (含法定节假日), 历表覆盖不足时先向后补.
    返回升序日期列表; 历表完全不可用时返回空列表.

    补日历是「拉 TuShare + 写库」的重操作, 而本函数在每个选股请求里都会走到 —— 所以按
    horizon 节流: 同一进程内对相同 horizon 最多每 10 分钟补一次, 避免历表够不到时把每个
    请求都拖成一次网络往返 + 写锁竞争."""
    global _TRADING_CAL_LAST_ATTEMPT
    from datetime import date as _d, timedelta as _td
    base = _d(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    # 覆盖 n 个交易日所需日历天: A股最长连休 (春节) 约 9 天, n*3+20 是安全余量
    horizon = (base + _td(days=n * 3 + 20)).strftime('%Y%m%d')
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        if _trading_calendar_max(conn) < horizon:
            now = time.time()
            if now - _TRADING_CAL_LAST_ATTEMPT >= _TRADING_CAL_THROTTLE_SEC:
                _TRADING_CAL_LAST_ATTEMPT = now
                _extend_trading_calendar(horizon, conn)
        return [r[0] for r in conn.execute(
            'SELECT cal_date FROM trading_dates_cache WHERE cal_date > ? AND cal_date <= ? '
            'ORDER BY cal_date LIMIT ?', (ymd, horizon, n)).fetchall()]
    except Exception as exc:
        print(f'[trading calendar] 读取失败: {exc}', flush=True)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _next_trading_days(ymd, n):
    """取入选日之后 T+1..T+n 的真实交易日.

    返回 (dates, from_calendar): from_calendar=False 表示交易日历覆盖不足 (TuShare 不可用等),
    dates 为空列表 —— 调用方需要自己兜底 (查 stock_daily / fund_flow), 别直接当成空数据."""
    if not ymd or not n:
        return [], False
    days = _trading_days_after(ymd, n)
    return (days[:n], True) if len(days) >= n else ([], False)


def _next_workdays(ymd, n):
    """推算入选日之后 T+1..T+n 对应的真实交易日 (跳周末 + 法定节假日).

    口径与 _attach_preferred_outcomes 取数用的 trading_dates_cache 一致, 保证界面顶栏
    标注的 T+N 日期和表格 T+N 列里实际装的行情是同一天 (历史版本这里按"纯周末顺延"数日子,
    遇中秋/国庆会整体错位). 日历不可用时降级为纯周末顺延, 调用方若能兜底应改用 _next_trading_days."""
    days, ok = _next_trading_days(ymd, n)
    return days if ok else _next_workdays_weekend_only(ymd, n)



def _snapshot_tn_stale(conn, results, next_dates):
    """快照 T+N 数据缺口检查: next_date_i 是已收盘的交易日 (stock_daily 有该日)
    但快照里该列全 NULL → 快照是在窗口未收齐时存的, 已过时."""
    real_dates = [d for d in next_dates if d]
    closed = {r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date IN (%s)'
        % ','.join('?' * len(real_dates)),
        real_dates).fetchall()} if real_dates else set()
    for i, nd in enumerate(next_dates, start=1):
        if nd and nd in closed and all(r.get(f'next_{i}_close') is None for r in results):
            return i
    return 0


def _read_conditional_snapshot(date, threshold_yi):
    """盘后快照直读: conditional_picks_snapshot 表 → 按 threshold_yi 重新过滤 + 排序.
    返回 payload dict; 不适用 (无快照/今日/T+1 是今天/T+N 数据缺口) 返回 None → 调用方回落实时算."""
    today = datetime.now().strftime('%Y%m%d')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM conditional_picks_snapshot').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return None
    row = conn.execute(
        'SELECT count, results_json, next_date, next_date_2, next_date_3, next_date_4, next_date_5, intraday_used '
        'FROM conditional_picks_snapshot WHERE trade_date = ?', (date,)).fetchone()
    if not row or not (row['count'] or 0):
        conn.close()
        return None
    # 今日入选 / T+1 是今天 → 实时算 (盘中 sina 口径 + 自愈逻辑只在实时路径)
    if date >= today or row['next_date'] == today:
        conn.close()
        return None
    try:
        results = json.loads(row['results_json'] or '[]')
    except Exception:
        results = []
    if not results:
        conn.close()
        return None
    next_dates = [row['next_date'], row['next_date_2'], row['next_date_3'],
                  row['next_date_4'], row['next_date_5']]
    if not any(next_dates):  # 很旧的快照没存 next_date → 工作日历推算补齐
        next_dates = _next_workdays(date, 5)
    stale_i = _snapshot_tn_stale(conn, results, next_dates)
    conn.close()
    if stale_i:
        print(f'[conditional snapshot] {date} T+{stale_i} 已收盘但快照缺数据, 回落实时算自愈', flush=True)
        return None
    threshold_amt = threshold_yi * 1e8
    hits = _apply_cond_threshold(results, threshold_amt)
    hits.sort(key=lambda r: max(r['sum_3d'], r['sum_5d'], r['sum_10d'], r['sum_20d']), reverse=True)
    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    return {
        'status': 'success',
        'date': date,
        'next_date':   next_dates[0] if len(next_dates) > 0 else None,
        'next_date_2': next_dates[1] if len(next_dates) > 1 else None,
        'next_date_3': next_dates[2] if len(next_dates) > 2 else None,
        'next_date_4': next_dates[3] if len(next_dates) > 3 else None,
        'next_date_5': next_dates[4] if len(next_dates) > 4 else None,
        'next_1_is_today': False,
        'intraday_used': bool(row['intraday_used']),
        'snapshot_saved': True,
        'snapshot_count': len(hits),
        'count': len(hits),
        'results': hits,
        'filter': {
            'change_pct_required': '< 0',
            'price_bucket': 'close < 200 → 主力 (main); close >= 200 → 超大单 (super)',
            'threshold_yi': threshold_yi,
            'cumulative_window': '3d / 5d / 10d / 20d 必须 > 0',
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        },
        'note': '快照库直读',
    }


def _snapshot_day_is_evaluable(date, base_count=None):
    """该日是否具备落快照的原料 —— 用来挡住「数据没就位算出 0 命中」把当天锁死.

    问题背景: 快照直读会短路实时计算, 所以一张 count=0 的快照等于宣告「这天没票入选」,
    之后每次请求都读它、永远返回 0. 原料没到位时的 0 是假的, 绝不能落库.
    判定: 当日 stock_daily / fund_flow 规模要接近该股池, 且基础选股要有候选池 (base_count>0);
    base_count 传 None 时跳过该项 (有些策略的 base 是另一层算的, 传进来才判).
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        daily = conn.execute(
            'SELECT COUNT(*) FROM stock_daily WHERE trade_date=? AND close IS NOT NULL',
            (date,)).fetchone()[0]
        flow = conn.execute(
            'SELECT COUNT(*) FROM fund_flow WHERE trade_date=? AND main_net_inflow IS NOT NULL',
            (date,)).fetchone()[0]
        pool = conn.execute('SELECT COUNT(*) FROM stock_basic').fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        print(f'[snapshot guard] {date} 查原料失败, 跳过落快照: {exc}', flush=True)
        return False
    # 阈值取 1000 只 (约 1/5 盘口), 低于此认为原料没就位
    if daily < 1000 or flow < 1000 or (pool and daily < pool * 0.15):
        print(f'[snapshot guard] {date} 原料不足 (daily={daily} flow={flow} pool={pool}), 不落快照',
              flush=True)
        return False
    if base_count is not None and base_count <= 0:
        print(f'[snapshot guard] {date} 基础池为空 (base_count=0), 判定原料未就位, 不落快照',
              flush=True)
        return False
    return True


def _save_conditional_new_snapshot(date, hits, exclusions, threshold_yi, volume_ratio_pct):
    """条件选股新落库: 把命中 + exclusions 写到 conditional_picks_new_snapshot.
    守卫: 日期 > 今天, 或日期 == 今天但还没收盘 (15:05 之前) → 不写 (盘中会污染快照).
    另: 0 命中且原料未就位 → 不写 (空快照会永久短路该日的实时计算, 见 _snapshot_day_is_evaluable).
    收盘后用户手动触发 sync-date 可正常入库今天快照. 失败仅打印, 不抛."""
    today = datetime.now()
    today_ymd = today.strftime('%Y%m%d')
    if date > today_ymd or (date == today_ymd and (today.hour, today.minute) < (15, 5)):
        return
    if not hits and not _snapshot_day_is_evaluable(date, exclusions.get('base_count')):
        return
    try:
        # 同 _save_conditional_preferred_snapshot: REPLACE 会删旧行, 漏写 next_date 列会被清空
        next_dates = _next_workdays(date, 5)
        _conn = sqlite3.connect(DB_PATH)
        _conn.execute(
            'INSERT OR REPLACE INTO conditional_picks_new_snapshot '
            '(trade_date, count, results_json, exclusions_json, threshold_yi, volume_ratio_pct, '
            'next_date, next_date_2, next_date_3, next_date_4, next_date_5) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (date, len(hits), json.dumps(hits, ensure_ascii=False),
             json.dumps(exclusions, ensure_ascii=False),
             threshold_yi, volume_ratio_pct,
             next_dates[0] if len(next_dates) > 0 else None,
             next_dates[1] if len(next_dates) > 1 else None,
             next_dates[2] if len(next_dates) > 2 else None,
             next_dates[3] if len(next_dates) > 3 else None,
             next_dates[4] if len(next_dates) > 4 else None))
        _conn.commit()
        _conn.close()
    except Exception as _e:
        print(f'[conditional new snapshot] 入库失败 {date}: {_e}', flush=True)


def _read_conditional_new_snapshot(date, threshold_yi, volume_ratio_pct):
    """条件选股新盘后快照直读: 命中 → 完整 payload (跟实时路径字段对齐), 不适用 → 返回 None.
    不适用: 无快照 / 今日 / T+1 是今天 / T+N 数据缺口 → 返回 None 让调用方回落实时算."""
    today = datetime.now().strftime('%Y%m%d')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM conditional_picks_new_snapshot').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return None
    row = conn.execute(
        'SELECT count, results_json, exclusions_json, threshold_yi, volume_ratio_pct '
        'FROM conditional_picks_new_snapshot WHERE trade_date = ?', (date,)).fetchone()
    if not row or not (row['count'] or 0):
        conn.close()
        return None
    if date >= today:
        conn.close()
        return None
    next_dates = _next_workdays(date, 5)
    if next_dates and next_dates[0] == today:
        conn.close()
        return None
    try:
        results = json.loads(row['results_json'] or '[]')
        exclusions = json.loads(row['exclusions_json'] or '{}')
    except Exception:
        results = []
        exclusions = {}
    if not results:
        conn.close()
        return None
    stale_i = _snapshot_tn_stale(conn, results, next_dates)
    conn.close()
    if stale_i:
        print(f'[conditional new snapshot] {date} T+{stale_i} 已收盘但快照缺数据, 回落实时算自愈', flush=True)
        return None
    saved_threshold_yi = row['threshold_yi'] if row['threshold_yi'] is not None else 3
    saved_volume_ratio = row['volume_ratio_pct'] if row['volume_ratio_pct'] is not None else 80
    if abs(saved_threshold_yi - threshold_yi) > 1e-6 or abs(saved_volume_ratio - volume_ratio_pct) > 1e-6:
        return None  # 参数不一致 → 回落实时算重新生成
    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    return {
        'status': 'success',
        'date': date,
        'next_date':   next_dates[0] if len(next_dates) > 0 else None,
        'next_date_2': next_dates[1] if len(next_dates) > 1 else None,
        'next_date_3': next_dates[2] if len(next_dates) > 2 else None,
        'next_date_4': next_dates[3] if len(next_dates) > 3 else None,
        'next_date_5': next_dates[4] if len(next_dates) > 4 else None,
        'next_1_is_today': False,
        'count': len(results),
        'results': results,
        'exclusions': exclusions,
        'daily_ready': True,
        'readiness_reason': 'ready',
        'filter': {
            'change_pct_required': '< 0',
            'price_bucket': 'close < 200 → 主力 (main); close >= 200 → 超大单 (super)',
            'threshold_yi': threshold_yi,
            'cumulative_window': '3d / 5d / 10d / 20d 必须 > 0',
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿',
            'base': '复用条件选股全部规则及资金门槛',
            'close_vs_ma10': '收盘价 ≥ MA10 (含当天最近 10 个有效收盘价)',
            'max_volume_ratio_pct': volume_ratio_pct,
            'volume_rule': _volume_rule_text(volume_ratio_pct),
            'completed_daily_only': True,
        },
        'note': '快照库直读',
    }


def _save_conditional_preferred_snapshot(date, hits, stats, threshold_yi, volume_ratio_pct):
    """条件优选落库: 写 conditional_picks_preferred_snapshot.
    守卫同 _save_conditional_new_snapshot (含 0 命中的空快照保护)."""
    today = datetime.now()
    today_ymd = today.strftime('%Y%m%d')
    if date > today_ymd or (date == today_ymd and (today.hour, today.minute) < (15, 5)):
        return
    if not hits and not _snapshot_day_is_evaluable(date, stats.get('base_count')):
        return
    try:
        # 必须带上 next_date_1..5: INSERT OR REPLACE 会删旧行重建, 不写这几列就会被清成
        # NULL, 之后读快照直读时顶栏 T+N 标注就空了 (历史日期尤其明显)
        next_dates = _next_workdays(date, 5)
        _conn = sqlite3.connect(DB_PATH)
        _conn.execute(
            'INSERT OR REPLACE INTO conditional_picks_preferred_snapshot '
            '(trade_date, count, results_json, exclusions_json, threshold_yi, volume_ratio_pct, '
            'next_date, next_date_2, next_date_3, next_date_4, next_date_5) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (date, len(hits), json.dumps(hits, ensure_ascii=False),
             json.dumps(stats, ensure_ascii=False),
             threshold_yi, volume_ratio_pct,
             next_dates[0] if len(next_dates) > 0 else None,
             next_dates[1] if len(next_dates) > 1 else None,
             next_dates[2] if len(next_dates) > 2 else None,
             next_dates[3] if len(next_dates) > 3 else None,
             next_dates[4] if len(next_dates) > 4 else None))
        _conn.commit()
        _conn.close()
    except Exception as _e:
        print(f'[conditional preferred snapshot] 入库失败 {date}: {_e}', flush=True)


def _read_conditional_preferred_snapshot(date, threshold_yi, volume_ratio_pct):
    """条件优选盘后快照直读: 命中 → 完整 payload, 不适用 → 返回 None.
    字段跟 api_conditional_picks_preferred 实时路径对齐 (含 strategy / preferred_exclusions / outcomes)."""
    today = datetime.now().strftime('%Y%m%d')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM conditional_picks_preferred_snapshot').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return None
    row = conn.execute(
        'SELECT count, results_json, exclusions_json, threshold_yi, volume_ratio_pct '
        'FROM conditional_picks_preferred_snapshot WHERE trade_date = ?', (date,)).fetchone()
    if not row or not (row['count'] or 0):
        conn.close()
        return None
    if date >= today:
        conn.close()
        return None
    next_dates = _next_workdays(date, 5)
    if next_dates and next_dates[0] == today:
        conn.close()
        return None
    try:
        results = json.loads(row['results_json'] or '[]')
        stats = json.loads(row['exclusions_json'] or '{}')
    except Exception:
        results = []
        stats = {}
    if not results:
        conn.close()
        return None
    saved_threshold_yi = row['threshold_yi'] if row['threshold_yi'] is not None else 3
    saved_volume_ratio = row['volume_ratio_pct'] if row['volume_ratio_pct'] is not None else 80
    if abs(saved_threshold_yi - threshold_yi) > 1e-6 or abs(saved_volume_ratio - volume_ratio_pct) > 1e-6:
        conn.close()
        return None
    stale_i = _snapshot_tn_stale(conn, results, next_dates)
    conn.close()
    if stale_i:
        print(f'[conditional preferred snapshot] {date} T+{stale_i} 已收盘但快照缺数据, 回落实时算自愈', flush=True)
        return None
    return {
        'status': 'success',
        'date': date,
        'count': len(results),
        'results': results,
        'preferred_exclusions': stats,
        'strategy': 'conditional-preferred-v1',
        'daily_ready': True,
        'readiness_reason': 'ready',
        'next_date':   next_dates[0] if len(next_dates) > 0 else None,
        'next_date_2': next_dates[1] if len(next_dates) > 1 else None,
        'next_date_3': next_dates[2] if len(next_dates) > 2 else None,
        'next_date_4': next_dates[3] if len(next_dates) > 3 else None,
        'next_date_5': next_dates[4] if len(next_dates) > 4 else None,
        'next_1_is_today': False,
        'note': '快照库直读',
    }


@app.route('/api/conditional-picks/snapshot', methods=['GET'])
def api_conditional_picks_snapshot():
    """条件选股盘后快照: 优先直读 conditional_picks_snapshot 表 (历史日期秒出);
    无快照 / 今日 / T+1 是今天 / T+N 数据缺口 → 回落实时算 (算完自动重写快照)."""
    date = (request.args.get('date') or '').strip()
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    payload = _read_conditional_snapshot(date, threshold_yi)
    if payload is not None:
        _enrich_intraday_payload(payload)
        return jsonify(payload)
    return api_conditional_picks()


@app.route('/api/conditional-picks/snapshot/legacy', methods=['GET'])
def api_conditional_picks_snapshot_legacy():
    """盘后快照: 读 conditional_picks_snapshot 表 (init_db 自动建) . 表为空时 fallback 实时计算."""
    date = (request.args.get('date') or '').strip()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM conditional_picks_snapshot').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return jsonify({'status': 'success', 'date': '', 'count': 0, 'results': [],
                        'note': '尚未落库任何盘后快照 (请等待 17:00 调度任务入库或调 /api/conditional-picks 实时算)'})
    row = conn.execute(
        'SELECT count, results_json, next_date, next_date_2, next_date_3, next_date_4, next_date_5, intraday_used '
        'FROM conditional_picks_snapshot WHERE trade_date = ?', (date,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'status': 'success', 'date': date, 'count': 0, 'results': [],
                        'note': f'{date} 盘后快照未落库, 请直接调 /api/conditional-picks 实时计算'})
    try:
        results = json.loads(row['results_json'] or '[]')
    except Exception:
        results = []
    return jsonify({
        'status': 'success',
        'date': date,
        'count': row['count'] or 0,
        'results': results,
        'next_date':   row['next_date'],
        'next_date_2': row['next_date_2'],
        'next_date_3': row['next_date_3'],
        'next_date_4': row['next_date_4'],
        'next_date_5': row['next_date_5'],
        'intraday_used': bool(row['intraday_used']),
        'next_1_is_today': False,
    })


def _read_combined_snapshot(date, threshold_yi):
    """组合选股盘后快照直读: combined_picks_snapshot 表 → 按 threshold_yi 重新过滤.
    返回 payload dict; 不适用 (无快照/今日/T+1 是今天/T+N 数据缺口) 返回 None → 回落实时算."""
    today = datetime.now().strftime('%Y%m%d')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM combined_picks_snapshot').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return None
    row = conn.execute(
        'SELECT count, results_json, note FROM combined_picks_snapshot WHERE trade_date = ?',
        (date,)).fetchone()
    if not row or not (row['count'] or 0):
        conn.close()
        return None
    if date >= today:
        conn.close()
        return None
    try:
        results = json.loads(row['results_json'] or '[]')
    except Exception:
        results = []
    if not results:
        conn.close()
        return None
    next_dates = _next_workdays(date, 5)
    # T+1 是今天 → 实时算 (盘中 sina 口径)
    if next_dates[0] == today:
        conn.close()
        return None
    stale_i = _snapshot_tn_stale(conn, results, next_dates)
    conn.close()
    if stale_i:
        print(f'[combined snapshot] {date} T+{stale_i} 已收盘但快照缺数据, 回落实时算自愈', flush=True)
        return None
    threshold_amt = threshold_yi * 1e8
    hits = _apply_cond_threshold(results, threshold_amt)  # 保持 stock_picks score 顺序
    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    return {
        'status': 'success',
        'date': date,
        'count': len(hits),
        'results': hits,
        'next_1_is_today': False,
        'filter': {
            'cumulative_window': '3d / 5d / 10d / 20d 必须 > 0',
            'threshold_yi': threshold_yi,
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        },
        'note': ((row['note'] or '') + ' · ' if row['note'] else '') + '快照库直读',
    }


@app.route('/api/combined-picks/snapshot', methods=['GET'])
def api_combined_picks_snapshot():
    """组合选股盘后快照: 优先直读 combined_picks_snapshot 表 (历史日期秒出);
    无快照 / 今日 / T+1 是今天 / T+N 数据缺口 → 回落实时算 (算完自动重写快照)."""
    date = (request.args.get('date') or '').strip()
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    payload = _read_combined_snapshot(date, threshold_yi)
    if payload is not None:
        return jsonify(payload)
    return api_combined_picks()


@app.route('/api/combined-picks/snapshot/legacy', methods=['GET'])
def api_combined_picks_snapshot_legacy():
    """盘后快照: 读 combined_picks_snapshot 表."""
    date = (request.args.get('date') or '').strip()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM combined_picks_snapshot').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return jsonify({'status': 'success', 'date': '', 'count': 0, 'results': [],
                        'note': '尚未落库任何盘后快照'})
    row = conn.execute(
        'SELECT count, results_json FROM combined_picks_snapshot WHERE trade_date = ?', (date,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'status': 'success', 'date': date, 'count': 0, 'results': [],
                        'note': f'{date} 盘后快照未落库, 请直接调 /api/combined-picks 实时计算'})
    try:
        results = json.loads(row['results_json'] or '[]')
    except Exception:
        results = []
    return jsonify({
        'status': 'success',
        'date': date,
        'count': row['count'] or 0,
        'results': results,
    })


def _conditional_picks_payload(date, source, intraday_flag, threshold_yi):
    """api_conditional_picks 的数据部分 (抽出来给预览图接口复用).

    规则: change_pct < 0; close < 200 看主力, >= 200 看超大单;
          4 项累计 > 0 且至少 1 项 ≥ threshold
    """
    threshold_amt = threshold_yi * 1e8

    # 1) 选定 trade_date (不传 = 用最新可用日) 并定下该日用哪个资金源
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute(
            'SELECT MAX(trade_date) AS d FROM fund_flow '
            'WHERE source IN (?, ?) AND main_net_inflow IS NOT NULL AND main_net_inflow != 0',
            EM_FLOW_SOURCES
        ).fetchone()
        if not row or not row['d']:
            conn.close()
            return {'status': 'success', 'date': '', 'count': 0, 'results': [],
                    'snapshot_saved': False, 'snapshot_count': None, 'note': 'fund_flow 表无数据'}
        date = row['d']
    # source 为空 = 东财自动口径: 该日 push2 优先, 缺则 daykline (5 月只有后者)
    if source not in EM_FLOW_SOURCES:
        source = _em_flow_source_for_date(conn, date) or ''
    if not source:
        conn.close()
        return {
            'status': 'success', 'date': date, 'count': 0, 'results': [],
            'snapshot_saved': False, 'snapshot_count': None,
            'note': f'该日 ({date}) 无东方财富资金流数据'
        }
    conn.close()

    # 2-3) 调内部函数
    data, next_dates = _compute_conditional_picks_internal(date, source, intraday_close=intraday_flag)
    if data is None:
        return {'status': 'success', 'date': date, 'count': 0, 'results': [],
                'note': f'交易日窗口不足 3 天'}

    # 4) 基础规则 (与资金门槛无关) + 板块; 门槛等快照入库之后再套,
    #    这样快照存的是全量候选, 之后任何门槛都能从快照直读过滤
    base_hits = _filter_conditional_base(data)
    _attach_sector(base_hits, key='ts_code')
    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    # T+1 就是今天 → 只对入选的票拉 sina 实时价覆盖 (不再全市场拉)
    _intraday_used = _apply_intraday_next(base_hits, next_dates)
    # 入选日=今天且用了 sina 实时价补齐 close/change_pct → 也算盘中口径
    if any(h.get('_selday_live') for h in base_hits):
        _intraday_used = True
    from datetime import datetime as _dt2
    _today = _dt2.now().strftime('%Y%m%d')
    _live_next_index = next((i for i, day in enumerate(next_dates, start=1) if day == _today), 0)
    _next_1_is_today = _live_next_index == 1
    # 计算完即写入盘后快照库 (INSERT OR REPLACE, 后算的覆盖先算的) — 存全量候选 (不带门槛)
    # 守卫: 本次 0 命中但该日已有非空快照 → 不覆盖 (原料未就位时算出的空结果不能冲掉好数据)
    # 只保存完整日线口径；今天落在任一 T+N 时，响应含盘中报价，不能覆盖盘后快照。
    # 日期 == 今天时：未到 15:05 视为盘中, 不入库; 收盘后可入库今天快照
    _is_today = (date == _today)
    _market_closed_today = (datetime.now().hour, datetime.now().minute) >= (15, 5)
    _snapshot_saved = (not _is_today or _market_closed_today) and not _live_next_index
    try:
        _conn = sqlite3.connect(DB_PATH)
        if _snapshot_saved and not base_hits:
            _old = _conn.execute(
                'SELECT count FROM conditional_picks_snapshot WHERE trade_date = ?',
                (date,)).fetchone()
            if _old and _old[0]:
                _snapshot_saved = False
                print(f'[conditional snapshot] {date} 本次 0 命中, 保留已有非空快照 ({_old[0]} 条), 不覆盖', flush=True)
        if _snapshot_saved:
            _conn.execute(
                'INSERT OR REPLACE INTO conditional_picks_snapshot '
                '(trade_date, count, results_json, next_date, next_date_2, next_date_3, next_date_4, next_date_5, intraday_used) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (date, len(base_hits), json.dumps(base_hits, ensure_ascii=False),
                 next_dates[0] if len(next_dates) > 0 else None,
                 next_dates[1] if len(next_dates) > 1 else None,
                 next_dates[2] if len(next_dates) > 2 else None,
                 next_dates[3] if len(next_dates) > 3 else None,
                 next_dates[4] if len(next_dates) > 4 else None,
                 1 if _intraday_used else 0))
            _conn.commit()
        _conn.close()
    except Exception as _e:
        print(f'[conditional snapshot] 入库失败 {date}: {_e}', flush=True)
    # 门槛过滤 + 排序 (只作用于本次返回, 不影响已存的全量快照)
    hits = _apply_cond_threshold(base_hits, threshold_amt)
    hits.sort(key=lambda r: max(r['sum_3d'], r['sum_5d'], r['sum_10d'], r['sum_20d']), reverse=True)
    return {
        'status': 'success',
        'date': date,
        'next_date':   next_dates[0] if len(next_dates) > 0 else None,
        'next_date_2': next_dates[1] if len(next_dates) > 1 else None,
        'next_date_3': next_dates[2] if len(next_dates) > 2 else None,
        'next_date_4': next_dates[3] if len(next_dates) > 3 else None,
        'next_date_5': next_dates[4] if len(next_dates) > 4 else None,
        'live_next_index': _live_next_index,
        'next_1_is_today': _next_1_is_today,
        'intraday_used': _intraday_used,
        'snapshot_saved': _snapshot_saved,   # 本次计算完已即时入库 (0 命中且已有非空快照时保留旧快照)
        'snapshot_count': len(hits),
        'count': len(hits),
        'results': hits,
        'filter': {
            'change_pct_required': '< 0',
            'price_bucket': 'close < 200 → 主力 (main); close >= 200 → 超大单 (super)',
            'threshold_yi': threshold_yi,
            'cumulative_window': '3d / 5d / 10d / 20d 必须 > 0',

            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        }
    }


def _conditional_picks_payload_ths(date, intraday_flag, threshold_yi):
    """api_picks_ths_conditional 的数据部分 (THS 版).

    规则 (无价格桶, 无中小单):
      - change_pct < 0
      - 3d / 5d / 10d 累计 (fund_flow_ths.net) 必须 > 0
      - 至少 1 项 ≥ threshold
    """
    threshold_amt = threshold_yi * 1e8

    # 1) 选定 trade_date (不传 = 用最新可用日; THS 单源, 直接 MAX)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if date:
        row = conn.execute(
            'SELECT MIN(trade_date) AS d FROM fund_flow_ths WHERE trade_date = ?',
            (date,)).fetchone()
        if not row or not row['d']:
            conn.close()
            return {
                'status': 'success', 'date': date, 'count': 0, 'results': [],
                'snapshot_saved': False, 'snapshot_count': None,
                'note': f'该日 ({date}) 无同花顺资金流数据'
            }
    else:
        row = conn.execute(
            'SELECT MAX(trade_date) AS d FROM fund_flow_ths WHERE net IS NOT NULL'
        ).fetchone()
        if not row or not row['d']:
            conn.close()
            return {'status': 'success', 'date': '', 'count': 0, 'results': [],
                    'snapshot_saved': False, 'snapshot_count': None, 'note': 'fund_flow_ths 表无数据'}
        date = row['d']
    conn.close()

    # 2-3) raw 数据 (SQL+next_dates)
    data, next_dates = _compute_conditional_picks_internal_ths(date, intraday_close=intraday_flag)
    if data is None:
        return {'status': 'success', 'date': date, 'count': 0, 'results': [],
                'note': f'交易日窗口不足 3 天'}

    # 4) 基础规则 + 板块; 门槛等快照入库之后再套
    base_hits = _filter_conditional_base_ths(data)
    _attach_sector(base_hits, key='ts_code')
    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    _intraday_used = _apply_intraday_next(base_hits, next_dates)
    if any(h.get('_selday_live') for h in base_hits):
        _intraday_used = True
    from datetime import datetime as _dt2
    _next_1_is_today = bool(next_dates and next_dates[0] == _dt2.now().strftime('%Y%m%d'))
    # 写入盘后快照库 (跟 EM 同款: 计算完即入库)
    _snapshot_saved = True
    try:
        _conn = sqlite3.connect(DB_PATH)
        if not base_hits:
            _old = _conn.execute(
                'SELECT count FROM conditional_picks_snapshot_ths WHERE trade_date = ?',
                (date,)).fetchone()
            if _old and _old[0]:
                _snapshot_saved = False
                print(f'[ths cond snapshot] {date} 本次 0 命中, 保留已有非空快照 ({_old[0]} 条), 不覆盖', flush=True)
        if _snapshot_saved:
            _conn.execute(
                'INSERT OR REPLACE INTO conditional_picks_snapshot_ths '
                '(trade_date, count, results_json, next_date, next_date_2, next_date_3, next_date_4, next_date_5, intraday_used) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (date, len(base_hits), json.dumps(base_hits, ensure_ascii=False),
                 next_dates[0] if len(next_dates) > 0 else None,
                 next_dates[1] if len(next_dates) > 1 else None,
                 next_dates[2] if len(next_dates) > 2 else None,
                 next_dates[3] if len(next_dates) > 3 else None,
                 next_dates[4] if len(next_dates) > 4 else None,
                 1 if _intraday_used else 0))
            _conn.commit()
        _conn.close()
    except Exception as _e:
        print(f'[ths cond snapshot] 入库失败 {date}: {_e}', flush=True)
    hits = _apply_cond_threshold_ths(base_hits, threshold_amt)
    hits.sort(key=lambda r: max(r['sum_3d'], r['sum_5d'], r['sum_10d']), reverse=True)
    return {
        'status': 'success',
        'date': date,
        'next_date':   next_dates[0] if len(next_dates) > 0 else None,
        'next_date_2': next_dates[1] if len(next_dates) > 1 else None,
        'next_date_3': next_dates[2] if len(next_dates) > 2 else None,
        'next_date_4': next_dates[3] if len(next_dates) > 3 else None,
        'next_date_5': next_dates[4] if len(next_dates) > 4 else None,
        'next_1_is_today': _next_1_is_today,
        'intraday_used': _intraday_used,
        'snapshot_saved': _snapshot_saved,
        'snapshot_count': len(hits),
        'count': len(hits),
        'results': hits,
        'filter': {
            'change_pct_required': '< 0',
            'price_bucket': '无 (THS 只有 net, 不分主力/超大单)',
            'threshold_yi': threshold_yi,
            'cumulative_window': '3d / 5d / 10d (THS 历史太短, 跳过 20d) 必须 > 0',
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        }
    }


def _read_conditional_snapshot_ths(date, threshold_yi):
    """THS 条件选股盘后快照直读 (跟 EM 同模式)."""
    today = datetime.now().strftime('%Y%m%d')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM conditional_picks_snapshot_ths').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return None
    row = conn.execute(
        'SELECT count, results_json, next_date, next_date_2, next_date_3, next_date_4, next_date_5, intraday_used '
        'FROM conditional_picks_snapshot_ths WHERE trade_date = ?', (date,)).fetchone()
    if not row or not (row['count'] or 0):
        conn.close()
        return None
    if date >= today or row['next_date'] == today:
        conn.close()
        return None
    try:
        results = json.loads(row['results_json'] or '[]')
    except Exception:
        results = []
    if not results:
        conn.close()
        return None
    next_dates = [row['next_date'], row['next_date_2'], row['next_date_3'],
                  row['next_date_4'], row['next_date_5']]
    if not any(next_dates):
        next_dates = _next_workdays(date, 5)
    stale_i = _snapshot_tn_stale(conn, results, next_dates)
    conn.close()
    if stale_i:
        print(f'[ths cond snapshot] {date} T+{stale_i} 已收盘但快照缺数据, 回落实时算自愈', flush=True)
        return None
    threshold_amt = threshold_yi * 1e8
    hits = _apply_cond_threshold_ths(results, threshold_amt)
    hits.sort(key=lambda r: max(r['sum_3d'], r['sum_5d'], r['sum_10d']), reverse=True)
    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    return {
        'status': 'success',
        'date': date,
        'next_date':   next_dates[0] if len(next_dates) > 0 else None,
        'next_date_2': next_dates[1] if len(next_dates) > 1 else None,
        'next_date_3': next_dates[2] if len(next_dates) > 2 else None,
        'next_date_4': next_dates[3] if len(next_dates) > 3 else None,
        'next_date_5': next_dates[4] if len(next_dates) > 4 else None,
        'next_1_is_today': False,
        'intraday_used': bool(row['intraday_used']),
        'snapshot_saved': True,
        'snapshot_count': len(hits),
        'count': len(hits),
        'results': hits,
        'filter': {
            'change_pct_required': '< 0',
            'price_bucket': '无 (THS 只有 net, 不分主力/超大单)',
            'threshold_yi': threshold_yi,
            'cumulative_window': '3d / 5d / 10d (THS 历史太短, 跳过 20d) 必须 > 0',
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        },
        'note': '快照库直读',
    }


@app.route('/api/picks/ths/conditional', methods=['GET'])
def api_picks_ths_conditional():
    """同花顺条件选股: 用 fund_flow_ths.net 筛选全市场.
    Args:
        date: 选股交易日期 (YYYYMMDD), 不传 = 最新有数据的交易日
        threshold_yi: 累计净流入门槛, 默认 3 亿
        intraday: 1/true 强制实时算 (跳过盘后快照直读)
    """
    date = (request.args.get('date') or '').strip()
    intraday_flag = request.args.get('intraday') in ('1', 'true', 'True')
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    return jsonify(_conditional_picks_payload_ths(date, intraday_flag, threshold_yi))


@app.route('/api/picks/ths/conditional/snapshot', methods=['GET'])
def api_picks_ths_conditional_snapshot():
    """同花顺条件选股盘后快照: 优先直读 conditional_picks_snapshot_ths;
    无快照 / 今日 / T+1 是今天 / T+N 数据缺口 → 回落实时算 (算完自动重写快照)."""
    date = (request.args.get('date') or '').strip()
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    payload = _read_conditional_snapshot_ths(date, threshold_yi)
    if payload is not None:
        return jsonify(payload)
    return jsonify(_conditional_picks_payload_ths(date, False, threshold_yi))


@app.route('/api/conditional-picks', methods=['GET'])
def api_conditional_picks():
    """条件选股: 按固定规则筛选 fund_flow 全市场 (不依赖 stock_picks / _compute_picks)

    Args:
        date: 选股交易日期 (YYYYMMDD), 不传 = 最新交易日
              (fund_flow 主源 eastmoney-push2, 且 main_net_inflow != 0, 防 ifind 空壳)
        source: 数据源, 默认 eastmoney-push2
    """
    """条件选股: 按固定规则筛选 fund_flow 全市场 (不依赖 stock_picks / _compute_picks)

    Args:
        date: 选股交易日期 (YYYYMMDD), 不传 = 最新交易日
              (fund_flow 主源 eastmoney-push2, 且 main_net_inflow != 0, 防 ifind 空壳)
        source: 数据源, 默认 eastmoney-push2
    """
    date = (request.args.get('date') or '').strip()
    source = (request.args.get('source') or '').strip()
    # 不传 source = 东财自动口径 (按日兜底: 6/16 后 push2, 之前 daykline);
    # 传了才锁定, 例如前端要复现某天的回填口径时显式指定.
    if source not in EM_FLOW_SOURCES:
        source = ''
    intraday_flag = request.args.get('intraday') in ('1', 'true', 'True')
    # 累计净流入门槛: ?threshold_yi=N (单位亿元, 默认 3, 允许 0 ~ 100); 负数/非法回退默认
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    return jsonify(_conditional_picks_payload(date, source, intraday_flag, threshold_yi))


@app.route('/api/conditional-picks/new', methods=['GET'])
def api_conditional_picks_new():
    """条件选股新：既有东方财富条件 + 收盘价不低于 MA10 + 成交量缩量。"""
    date_value = (request.args.get('date') or '').strip()
    source = (request.args.get('source') or 'eastmoney-push2').strip() or 'eastmoney-push2'
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    try:
        volume_ratio_pct = _parse_volume_ratio_pct(request.args.get('volume_ratio_pct'))
    except Exception:
        volume_ratio_pct = 80

    trade_date = _resolve_conditional_new_date(date_value, source)
    daily_ready, readiness_reason = _conditional_new_daily_ready(trade_date)
    rule_meta = {
        'base': '复用条件选股全部规则及资金门槛',
        'close_vs_ma10': '收盘价 ≥ MA10（含当天最近 10 个有效收盘价）',
        'max_volume_ratio_pct': volume_ratio_pct,
        'volume_rule': _volume_rule_text(volume_ratio_pct),
        'completed_daily_only': True,
    }
    if not daily_ready:
        today = datetime.now().strftime('%Y%m%d')
        if trade_date == today and readiness_reason in ('market_not_closed', 'daily_not_loaded', 'daily_incomplete'):
            # 盘中实时估算: 基础条件选股盘中口径来自当日 fund_flow (盘中已同步),
            # MA10/缩量用 sina 实时价 + 盘中累计量近似, 收盘日线入库后由完整口径覆盖
            payload = _conditional_picks_payload(trade_date, source, False, threshold_yi)
            hits, exclusions, quotes = _filter_conditional_new_intraday(
                payload.get('results') or [], trade_date, volume_ratio_pct)
            payload.update({
                'count': len(hits),
                'results': hits,
                'daily_ready': False,
                'readiness_reason': 'intraday_live',
                'intraday_estimate': True,
                'exclusions': exclusions,
                'filter': {**(payload.get('filter') or {}), **rule_meta,
                           'completed_daily_only': False},
            })
            base_note = payload.get('note')
            payload['note'] = (base_note + ' · ' if base_note else '') + \
                '盘中实时估算 (sina 价量), 收盘日线入库后以完整口径为准'
            _enrich_intraday_payload(payload, quotes=quotes)
            return jsonify(payload)
        messages = {
            'missing_date': '暂无可用资金流日期',
            'future_date': '目标日期尚未到来',
            'market_not_closed': '等待收盘数据',
            'daily_not_loaded': '等待收盘日线入库',
            'daily_incomplete': '等待收盘日线完整入库',
        }
        return jsonify({
            'status': 'success', 'date': trade_date, 'count': 0, 'results': [],
            'daily_ready': False, 'readiness_reason': readiness_reason,
            'exclusions': {'base_count': 0, 'matched': 0},
            'filter': rule_meta, 'note': messages.get(readiness_reason, '等待收盘数据'),
        })

    payload = _read_conditional_snapshot(trade_date, threshold_yi)
    if payload is None:
        payload = _conditional_picks_payload(trade_date, source, False, threshold_yi)
    else:
        _enrich_intraday_payload(payload)
    # 快照直读短路: 历史日期 + 落过库 + 参数一致 → 直接返回, 不重算 _filter_conditional_new
    cached = _read_conditional_new_snapshot(trade_date, threshold_yi, volume_ratio_pct)
    if cached is not None:
        _enrich_intraday_payload(cached)
        return jsonify(cached)
    hits, exclusions = _filter_conditional_new(
        payload.get('results') or [], trade_date, volume_ratio_pct
    )
    payload.update({
        'count': len(hits),
        'results': hits,
        'daily_ready': True,
        'readiness_reason': 'ready',
        'exclusions': exclusions,
        'filter': {**(payload.get('filter') or {}), **rule_meta},
    })
    base_note = payload.get('note')
    payload['note'] = (base_note + ' · ' if base_note else '') + '收盘日线已就绪'
    # 落库: 今日 / T+1 是今天 已被 _save 内部守卫排除
    _save_conditional_new_snapshot(trade_date, hits, exclusions, threshold_yi, volume_ratio_pct)
    return jsonify(payload)


def _parse_preferred_ma5(value):
    """条件优选「站上5日线」开关。默认关 (= 原条件), 只认明确的真值。"""
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('1', 'true', 'on', 'yes')


def _filter_conditional_preferred(rows, trade_date, conn=None, require_ma5=False):
    """新策略只读增强：完整五交易日主力强度 + 当日小单净流出。

    require_ma5=True 时额外要求「入选日收盘价 > MA5」。MA5 = 含入选日在内的最近 5 个
    有效收盘价均值, 跟条件选股新的 ma10 / 历史涨幅分布的 MA5 同一套窗口口径。
    开关关闭时逐股判定结果与开关引入前完全一致。"""
    import math
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        conn.execute('PRAGMA query_only=ON')
    conn.row_factory = sqlite3.Row
    # 逐股五交易日主力强度必须用同一个源, 混源会算出假的强度曲线:
    # 按入选日实际存在的源来定 (5 月及更早 = daykline, 6/16 之后 = push2).
    flow_source = _em_flow_source_for_date(conn, trade_date)
    stats = {'base_count': len(rows), 'missing_flow': 0, 'small_not_outflow': 0,
             'weak_main': 0, 'below_ma5': 0, 'missing_ma5': 0,
             'matched': 0, 'flow_source': flow_source or ''}
    if not flow_source:
        # 两个源都没有这一天 —— 是缺数据, 不是「没票入选」
        stats['missing_flow'] = len(rows)
        return [], stats
    hits = []
    try:
        days = [r[0] for r in conn.execute(
            'SELECT cal_date FROM trading_dates_cache WHERE cal_date <= ? '
            'ORDER BY cal_date DESC LIMIT 5', (trade_date,))]
        # 盘中当日 stock_daily 未入库: 成交额 / MA60 用 sina 实时数据补齐
        today = datetime.now().strftime('%Y%m%d')
        quotes = {}
        if trade_date == today and rows:
            try:
                quotes = fetch_sina_quotes([r['ts_code'] for r in rows if r.get('ts_code')])
            except Exception as exc:
                print(f'[preferred intraday] 实时行情失败: {exc}', flush=True)
        for row in rows:
            flows = [dict(f) for f in conn.execute('''
                SELECT f.trade_date, f.main_net_inflow, f.small_net, f.small_pct, d.amount
                FROM fund_flow f LEFT JOIN stock_daily d
                  ON d.ts_code=f.ts_code AND d.trade_date=f.trade_date
                WHERE f.ts_code=? AND f.trade_date<=? AND f.source=?
                ORDER BY f.trade_date DESC LIMIT 5
            ''', (row['ts_code'], trade_date, flow_source)).fetchall()]
            if flows and trade_date == today and flows[0]['trade_date'] == today:
                q = quotes.get(row['ts_code']) or {}
                amt = flows[0].get('amount')
                if (amt is None or not math.isfinite(amt) or amt <= 0) and q.get('amount'):
                    flows[0]['amount'] = q['amount'] / 1000  # sina 元 → 千元 (tushare 口径)
            valid = (len(days) == 5 and days[0] == trade_date
                     and [f['trade_date'] for f in flows] == days
                     and all(f['amount'] is not None and math.isfinite(f['amount'])
                             and f['amount'] > 0 and f['main_net_inflow'] is not None
                             and math.isfinite(f['main_net_inflow']) for f in flows)
                     and flows[0]['small_net'] is not None
                     and math.isfinite(flows[0]['small_net']))
            if not valid:
                stats['missing_flow'] += 1
                continue
            if flows[0]['small_net'] >= 0:
                stats['small_not_outflow'] += 1
                continue
            strength = sum(f['main_net_inflow'] for f in flows) / sum(f['amount'] * 1000 for f in flows) * 100
            if strength < 5:
                stats['weak_main'] += 1
                continue
            daily = conn.execute(
                'SELECT trade_date, close FROM stock_daily WHERE ts_code=? AND trade_date<=? '
                'AND close>0 ORDER BY trade_date DESC LIMIT 60',
                (row['ts_code'], trade_date)).fetchall()
            closes60 = [d['close'] for d in daily]
            closes60_dates = [d['trade_date'] for d in daily]
            if len(closes60) == 59 and trade_date == today:
                q = quotes.get(row['ts_code']) or {}
                if q.get('price'):
                    closes60.insert(0, q['price'])  # 盘中: 实时价补当第 60 根
                    closes60_dates.insert(0, trade_date)
            ma60 = sum(closes60) / 60 if len(closes60) == 60 else None
            # MA5 只有在窗口第一根确实就是入选日时才算数: 缺入选日日线时不能拿上一根顶替
            aligned = len(closes60) >= 5 and closes60_dates[0] == trade_date
            ma5 = sum(closes60[:5]) / 5 if aligned else None
            if require_ma5:
                close = row.get('close')
                if ma5 is None or not isinstance(close, (int, float)) or not math.isfinite(close):
                    stats['missing_ma5'] += 1
                    continue
                if close <= ma5:
                    stats['below_ma5'] += 1
                    continue
            ratio = (row['volume'] / row['prev_volume'] * 100
                     if row.get('prev_volume') else row.get('volume_ratio_pct') or 0)
            mild_shrink = 70 <= ratio <= 80
            below_ma60 = ma60 is not None and row['close'] <= ma60
            item = dict(row)
            item.update(preferred_strength5=round(strength, 4),
                        preferred_small_net=flows[0]['small_net'],
                        preferred_small_pct=flows[0]['small_pct'],
                        preferred_ma60=round(ma60, 4) if ma60 is not None else None,
                        preferred_ma5=round(ma5, 4) if ma5 is not None else None,
                        preferred_below_ma5=bool(ma5 is not None and row['close'] <= ma5),
                        preferred_mild_shrink=mild_shrink, preferred_below_ma60=below_ma60,
                        preferred_priority=int(mild_shrink) + int(below_ma60))
            hits.append(item)
        hits.sort(key=lambda r: (-r['preferred_priority'], -r['preferred_strength5'], r['ts_code']))
        stats['matched'] = len(hits)
        return hits, stats
    finally:
        if owns_conn:
            conn.close()


def _attach_preferred_outcomes(rows, trade_date, conn=None, key='preferred_outcomes'):
    """复利口径；历史日用收盘日线，今天所在的 T+N 用已核验实时价/日高。

    key: 挂载结果的字段名 (条件优选用 preferred_outcomes, 系统推荐/我的选股用 post_pick_outcomes)。
    返回 (入选日之后 T+1..T+5 的交易日, 其中"今天"落在第几个; 没落在窗口内为 0)。

    口径 (2026-10 改, 与条件选股表格统一):
      close_sum_pct = 第 N 日收盘 / 入选日收盘 - 1          —— 复利, 真实可成交
      high_sum_pct  = 第 N 日最高 / 入选日收盘 - 1 —— 当天冲高相对成本
    旧实现是逐日涨跌幅相加 (复利会漏、先跌后涨会高估), 最高值也只混了第 N 日, 不是这个分母。
    """
    import math
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        conn.execute('PRAGMA query_only=ON')
    conn.row_factory = sqlite3.Row
    try:
        days = [r[0] for r in conn.execute(
            'SELECT cal_date FROM trading_dates_cache WHERE cal_date>? ORDER BY cal_date LIMIT 5',
            (trade_date,))]
        now = datetime.now()
        today = now.strftime('%Y%m%d')
        live_index = next((i for i, day in enumerate(days, start=1) if day == today), 0)
        live_quotes = {}
        if live_index and (now.hour, now.minute) < (15, 0) and rows:
            try:
                live_quotes = fetch_sina_quotes([row['ts_code'] for row in rows if row.get('ts_code')])
            except Exception as exc:
                print(f'[preferred intraday] 实时行情失败: {exc}', flush=True)
        for row in rows:
            row[key] = []
            entry_close = row.get('close')
            previous = entry_close
            blocked = None
            for index in range(5):
                day = days[index] if index < len(days) else None
                result = {'date': day, 'high_sum_pct': None, 'close_sum_pct': None,
                          'reason': blocked or 'future_unavailable'}
                if not blocked and day and day <= today:
                    if day == today and (now.hour, now.minute) < (15, 0):
                        quote = live_quotes.get(row.get('ts_code')) or {}
                        price = quote.get('price')
                        high = quote.get('high')
                        quote_prev = quote.get('prev_close')
                        if (price is None or high is None or quote_prev is None
                                or price <= 0 or high <= 0 or quote_prev <= 0):
                            blocked = 'waiting_close'
                        elif previous <= 0 or abs((quote_prev / previous - 1) * 100) > .15:
                            blocked = 'reference_mismatch'
                        else:
                            result.update(
                                high_sum_pct=round((high / entry_close - 1) * 100, 4) if entry_close else None,
                                close_sum_pct=round((price / entry_close - 1) * 100, 4) if entry_close else None,
                                reason='intraday')
                            blocked = 'waiting_close'
                    else:
                        d = conn.execute(
                            'SELECT open,high,close,change FROM stock_daily WHERE ts_code=? AND trade_date=?',
                            (row['ts_code'], day)).fetchone()
                        if not d:
                            blocked = 'missing_daily'
                        elif (previous <= 0 or any(d[k] is None or not math.isfinite(d[k])
                                                  or d[k] <= 0 for k in ('open', 'high', 'close'))
                              or d['high'] < max(d['open'], d['close']) - .001):
                            blocked = 'invalid_daily'
                        else:
                            change = (d['close'] / previous - 1) * 100
                            if d['change'] is None or not math.isfinite(d['change']) or abs(change - d['change']) > .15:
                                blocked = 'reference_mismatch'
                            else:
                                result.update(high_sum_pct=round((d['high'] / entry_close - 1) * 100, 4),
                                              close_sum_pct=round((d['close'] / entry_close - 1) * 100, 4),
                                              reason=None)
                                previous = d['close']
                if blocked and result['high_sum_pct'] is None:
                    result['reason'] = blocked
                row[key].append(result)
        return days, live_index
    finally:
        if owns_conn:
            conn.close()


def _preferred_market_overview(trade_date, conn=None):
    """Return exact-date market breadth for 条件优选; never substitute another day."""
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        conn.execute('PRAGMA query_only=ON')
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute('''
                SELECT date, upCount, downCount, flatCount, limitUp, limitDown
                FROM overview WHERE date=? LIMIT 1
            ''', (trade_date,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        if not row:
            return {'date': trade_date, 'available': False,
                    'message': '该日涨跌家数尚未入库'}
        result = dict(row)
        counts = [result.get('upCount'), result.get('downCount'), result.get('flatCount')]
        if any(value is None for value in counts):
            return {'date': trade_date, 'available': False,
                    'message': '该日涨跌家数不完整'}
        result.update(available=True, totalCount=sum(counts))
        return result
    finally:
        if owns_conn:
            conn.close()


@app.route('/api/conditional-picks/preferred', methods=['GET'])
def api_conditional_picks_preferred():
    """条件选股新名单的资金优选；沿用门槛和缩量参数。"""
    # 只挡同花顺口径 (fund_flow_ths), 东财两个源都是合法资金源, 按日自动兜底
    if (request.args.get('source') or '') not in ('', 'eastmoney-push2', 'eastmoney-daykline'):
        return jsonify({'status': 'error', 'error': '条件优选仅支持东方财富资金源'}), 400
    require_ma5 = _parse_preferred_ma5(request.args.get('require_ma5'))
    payload = api_conditional_picks_new().get_json()
    # 快照直读短路: 实时路径已经在 new 那边直读/落过; 这里再尝试一次 (new 没落库时 preferred 可能仍可直读)
    # 「站上5日线」开关打开时必须跳过: 快照里存的是原条件名单, 带着它短路会把过滤条件吃掉。
    if payload.get('daily_ready') and not require_ma5:
        try:
            threshold_yi = float(request.args.get('threshold_yi') or '3')
        except Exception:
            threshold_yi = 3
        try:
            volume_ratio_pct = _parse_volume_ratio_pct(request.args.get('volume_ratio_pct'))
        except Exception:
            volume_ratio_pct = 80
        cached = _read_conditional_preferred_snapshot(payload['date'], threshold_yi, volume_ratio_pct)
        if cached is not None:
            _enrich_intraday_payload(cached)
            _attach_preferred_outcomes(cached.get('results') or [], payload['date'])
            cached['live_next_index'] = next((i for i in range(1, 6)
                if cached.get('next_date' if i == 1 else f'next_date_{i}') == datetime.now().strftime('%Y%m%d')), 0)
            preferred_live = any(any(outcome.get('reason') == 'intraday'
                                     for outcome in row.get('preferred_outcomes') or [])
                                 for row in cached.get('results') or [])
            cached['intraday_used'] = bool(cached.get('intraday_used') or preferred_live)
            cached['market_overview'] = _preferred_market_overview(payload['date'])
            return jsonify(cached)
    rows = payload.get('results') or []
    intraday_live = payload.get('readiness_reason') == 'intraday_live'
    if payload.get('daily_ready') or intraday_live:
        rows, stats = _filter_conditional_preferred(rows, payload['date'], require_ma5=require_ma5)
        _attach_preferred_outcomes(rows, payload['date'])
    else:
        stats = {'base_count': 0, 'matched': 0, 'missing_flow': 0}
    payload.update(results=rows, count=len(rows), preferred_exclusions=stats,
                   strategy='conditional-preferred-v1', require_ma5=require_ma5,
                   market_overview=_preferred_market_overview(payload.get('date')))
    _enrich_intraday_payload(payload)
    preferred_live = any(any(outcome.get('reason') == 'intraday'
                             for outcome in row.get('preferred_outcomes') or [])
                         for row in rows)
    payload['intraday_used'] = bool(payload.get('intraday_used') or preferred_live)
    # 落库 (今日 / T+1 是今天 由 _save 内部守卫排除)
    # 开关打开时同样不落库: 快照一个日期只存一套名单, 存进去会污染关闭状态的直读结果
    if payload.get('daily_ready') and not require_ma5:
        try:
            threshold_yi = float(request.args.get('threshold_yi') or '3')
        except Exception:
            threshold_yi = 3
        try:
            volume_ratio_pct = _parse_volume_ratio_pct(request.args.get('volume_ratio_pct'))
        except Exception:
            volume_ratio_pct = 80
        _save_conditional_preferred_snapshot(payload['date'], rows, stats, threshold_yi, volume_ratio_pct)
    return jsonify(payload)


# ===== 条件优选历史涨幅分布 (跨全部入选日回测 T+N 累计最高涨幅分桶占比) =====
# 8 档固定分桶: 涨跌幅区间 → (标签, 下界含, 上界不含)
PREFERRED_HIST_BUCKETS = (
    ('drop',   '跌 (<0%)', None,  0.0),
    ('b0_3',   '0~3%',     0.0,   3.0),
    ('b3_5',   '3~5%',     3.0,   5.0),
    ('b5_10',  '5~10%',    5.0,  10.0),
    ('b10_15', '10~15%',  10.0,  15.0),
    ('b15_20', '15~20%',  15.0,  20.0),
    ('b20_30', '20~30%',  20.0,  30.0),
    ('b30',    '≥30%',    30.0,  None),
)
PREFERRED_HIST_WINDOWS = (1, 3, 5)


def _bucket_preferred_high_gain(gain_pct):
    """把累计最高涨幅落进 8 档之一, 返回 (key, label) 或 (None, None)."""
    for key, label, low, high in PREFERRED_HIST_BUCKETS:
        if (low is None or gain_pct >= low) and (high is None or gain_pct < high):
            return key, label
    return None, None


# 历史涨幅分布的入选日均线条件 (互斥单选)。全为「入选当日」的收盘价与均线比较:
#   MA5 / MA10 = 含入选日在内的最近 5 / 10 个有效收盘价均值, 与条件优选列表的 ma10 同口径。
# 「站上」= 严格大于; 「没有站上」= 小于等于, 所以 ma5_only / ma10_only 两档互斥。
PREFERRED_HIST_MA_FILTERS = {
    'all': '不过滤（全部历史入选样本）',
    'ma5': '入选日收盘价 > MA5',
    'ma10': '入选日收盘价 > MA10',
    'ma5_only': 'MA5 < 入选日收盘价 ≤ MA10（只在5日线上）',
    'ma10_only': 'MA10 < 入选日收盘价 ≤ MA5（只在10日线上）',
}


def _parse_preferred_hist_ma(value):
    """历史涨幅分布的均线条件键。未知/空值一律回落到不过滤, 不报错也不静默当成某一档。"""
    key = (value or 'all').strip()
    return key if key in PREFERRED_HIST_MA_FILTERS else 'all'


def _preferred_hist_sample_ma(samples, conn=None):
    """给每条历史样本补入选日的 ma5 / ma10, 返回 {(ts_code, trade_date): (ma5, ma10)}。

    口径跟条件优选列表算 ma10 完全一致 (含入选日的最近 N 个有效收盘价均值), 已用全库
    420 条快照逐条核对: 重算 ma10 与快照里存的 ma10 最大差异 0.0, 所以 MA5 / MA10 同源,
    「只在某条线上」这类互斥判定才自洽。缺入选日或窗口不足 10 根的样本不出现在返回值里。
    """
    if not samples:
        return {}
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        codes = sorted({s['ts_code'] for s in samples if s.get('ts_code')})
        if not codes:
            return {}
        first_date = min(s['trade_date'] for s in samples if s.get('trade_date'))
        last_date = max(s['trade_date'] for s in samples if s.get('trade_date'))
        # 往前多留 30 个自然日, 保证最早那条也有满 10 根可用收盘价
        start = (datetime.strptime(first_date, '%Y%m%d') - timedelta(days=30)).strftime('%Y%m%d')
        series = defaultdict(list)
        for start_index in range(0, len(codes), 500):
            chunk = codes[start_index:start_index + 500]
            placeholders = ','.join('?' * len(chunk))
            for row in conn.execute(
                'SELECT ts_code, trade_date, close FROM stock_daily '
                f'WHERE ts_code IN ({placeholders}) AND trade_date BETWEEN ? AND ? '
                'AND close IS NOT NULL ORDER BY ts_code, trade_date',
                (*chunk, start, last_date)
            ):
                try:
                    series[row['ts_code']].append((row['trade_date'], float(row['close'])))
                except (TypeError, ValueError):
                    continue
    finally:
        if owns_conn:
            conn.close()
    position = {code: {day: i for i, (day, _) in enumerate(days)} for code, days in series.items()}
    out = {}
    for sample in samples:
        code, day = sample.get('ts_code'), sample.get('trade_date')
        days = series.get(code) or []
        i = (position.get(code) or {}).get(day)
        if i is None or i + 1 < 10:
            continue
        out[(code, day)] = (
            sum(v for _, v in days[i - 4:i + 1]) / 5,
            sum(v for _, v in days[i - 9:i + 1]) / 10,
        )
    return out


def _preferred_hist_ma_pass(ma_filter, close, ma5, ma10):
    """单个样本是否满足当前均线条件。close / ma5 / ma10 任一不可用时返回 False。"""
    if ma_filter == 'all':
        return True
    if close is None or ma5 is None or ma10 is None:
        return False
    try:
        close, ma5, ma10 = float(close), float(ma5), float(ma10)
    except (TypeError, ValueError):
        return False
    if ma_filter == 'ma5':
        return close > ma5
    if ma_filter == 'ma10':
        return close > ma10
    if ma_filter == 'ma5_only':
        return ma5 < close <= ma10
    if ma_filter == 'ma10_only':
        return ma10 < close <= ma5
    return True


def _collect_preferred_history(window, volume_ratio_pct=0, ma_filter='all'):
    """扫 conditional_picks_preferred_snapshot 全量入选记录, 算每条记录在 T+1..T+window 的
    累计最高涨幅 (当日最高价 / 入选日收盘价 - 1, 取窗口内最大值).
    每次入选算一个独立样本 (同一只股票多次入选计多次).
    窗口内任一天缺数据 (future_unavailable / reference_mismatch 等) 的记录整条排除并计数.

    volume_ratio_pct > 0 时再按「该条记录自己的成交量比」筛一遍样本 —— 只影响这里的统计,
    不改条件优选名单, 也不动库里的快照。0 = 不过滤 (默认)。
    ma_filter 见 PREFERRED_HIST_MA_FILTERS: 按入选日收盘价与 MA5/MA10 的关系再筛一遍,
    同样只影响这里的统计, 两级筛选可叠加。
    注意: 快照表一个日期只存一套参数, 早期用 90/99 算的日期里, 成交量比高于那套阈值的股票
    当时就没被记录, 无法事后补回来; 这部分覆盖缺口由 filtered_out / unfiltered_days 暴露出来."""
    volume_filter_on = float(volume_ratio_pct or 0) > 0
    volume_limit = float(volume_ratio_pct or 0)
    ma_filter = _parse_preferred_hist_ma(ma_filter)
    samples = []
    pending = 0
    filtered_out = 0
    dates = []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            'SELECT trade_date, count, results_json, threshold_yi, volume_ratio_pct '
            'FROM conditional_picks_preferred_snapshot ORDER BY trade_date').fetchall()
    except Exception as exc:
        conn.close()
        return {'error': f'读取条件优选快照失败: {exc}'}
    for row in rows:
        trade_date = row['trade_date']
        try:
            results = json.loads(row['results_json'] or '[]')
        except Exception:
            results = []
        day_total = 0
        for item in results or []:
            outcomes = (item.get('preferred_outcomes') or [])[:window]
            day_total += 1
            values = [o.get('high_sum_pct') for o in outcomes]
            if len(values) < window or any(v is None for v in values):
                pending += 1
                continue
            gain = max(values)
            key, label = _bucket_preferred_high_gain(gain)
            if key is None:
                pending += 1
                continue
            # 样本级缩量筛选: 只看这条记录自己那天的成交量比 (不追溯选股时的快照参数)
            sample_ratio = item.get('volume_ratio_pct')
            if volume_filter_on and (sample_ratio is None or float(sample_ratio) > volume_limit):
                filtered_out += 1
                continue
            samples.append({
                'trade_date': trade_date,
                'ts_code': item.get('ts_code'),
                'name': item.get('name'),
                'close': item.get('close'),
                'volume_ratio_pct': sample_ratio,
                'ma5': None,
                'ma10': None,
                'gain_pct': round(gain, 4),
                'bucket': key,
                'bucket_label': label,
                'path': [round(v, 4) for v in values],
            })
        if day_total:
            dates.append({
                'trade_date': trade_date,
                'count': day_total,
                'settled': 0,
                'threshold_yi': row['threshold_yi'],
                'volume_ratio_pct': row['volume_ratio_pct'],
            })
    conn.close()

    # 入选日均线条件: 一次批量算出 MA5 / MA10, 再逐条判定。分档计数在筛完之后才统计,
    # 所以 buckets 的 pct 分母始终等于真正进入分布的样本数。
    ma_filtered_out = 0
    ma_missing = 0
    if ma_filter != 'all' and samples:
        mas = _preferred_hist_sample_ma(samples)  # 上面的快照连接已关, 这里自己开只读连接
        kept = []
        for sample in samples:
            pair = mas.get((sample['ts_code'], sample['trade_date']))
            if pair is None:
                ma_missing += 1
                continue
            ma5, ma10 = pair
            sample['ma5'] = round(ma5, 4)
            sample['ma10'] = round(ma10, 4)
            if _preferred_hist_ma_pass(ma_filter, sample.get('close'), ma5, ma10):
                kept.append(sample)
            else:
                ma_filtered_out += 1
        samples = kept

    counts = {key: 0 for key, _, _, _ in PREFERRED_HIST_BUCKETS}
    per_day = defaultdict(int)
    for sample in samples:
        counts[sample['bucket']] += 1
        per_day[sample['trade_date']] += 1
    settled = len(samples)
    for day in dates:
        day['settled'] = per_day.get(day['trade_date'], 0)

    samples.sort(key=lambda s: (-s['gain_pct'], s['trade_date']))
    buckets = []
    for key, label, low, high in PREFERRED_HIST_BUCKETS:
        cnt = counts[key]
        buckets.append({
            'key': key, 'label': label, 'low': low, 'high': high,
            'count': cnt,
            'pct': round(cnt / settled * 100, 2) if settled else 0.0,
        })
    # 覆盖缺口: 快照本身带缩量阈值的天数, 其样本天然缺 "高于该阈值" 的票, 事后补不回来
    prefiltered_days = sum(1 for d in dates if (d.get('volume_ratio_pct') or 0) > 0)
    return {
        'window': window,
        'buckets': buckets,
        'samples': samples,
        'dates': dates,
        'settled': settled,
        'pending': pending,
        'filtered_out': filtered_out,
        'ma_filter': ma_filter,
        'ma_filter_label': PREFERRED_HIST_MA_FILTERS[ma_filter],
        'ma_filtered_out': ma_filtered_out,
        'ma_missing': ma_missing,
        'total': settled + pending + filtered_out + ma_filtered_out + ma_missing,
        'date_count': len(dates),
        'date_from': dates[0]['trade_date'] if dates else None,
        'date_to': dates[-1]['trade_date'] if dates else None,
        'unique_stocks': len({s['ts_code'] for s in samples}),
        'volume_filter_on': volume_filter_on,
        'volume_ratio_pct': volume_limit if volume_filter_on else None,
        'prefiltered_days': prefiltered_days,
    }


@app.route('/api/conditional-picks/preferred/historical-distribution', methods=['GET'])
def api_conditional_picks_preferred_historical_distribution():
    """条件优选历史涨幅分布: 入参 days=1|3|5 (默认 5), volume_ratio_pct=0 表示不按成交量比筛,
    返 8 档占比 + 逐条明细 + 每日入选数.
    口径: 每次入选算独立样本; 累计最高涨幅 = 入选后 T+1..T+days 中任一天的最高价 / 入选日收盘价 - 1.

    volume_ratio_pct 只作用于这里的样本筛选, 与条件优选名单用的缩量开关互不影响。
    ma 见 PREFERRED_HIST_MA_FILTERS: 入选日收盘价与 MA5/MA10 的关系, 互斥单选, 同样只作用于这里。"""
    try:
        window = int(request.args.get('days') or 5)
    except Exception:
        window = 5
    if window not in PREFERRED_HIST_WINDOWS:
        window = min(PREFERRED_HIST_WINDOWS, key=lambda w: abs(w - window))
    volume_ratio_pct = _parse_volume_ratio_pct(request.args.get('volume_ratio_pct'), default=0)
    ma_filter = _parse_preferred_hist_ma(request.args.get('ma'))
    data = _collect_preferred_history(window, volume_ratio_pct, ma_filter)
    if data.get('error'):
        return jsonify({'status': 'error', 'note': data['error']}), 500
    return jsonify({
        'status': 'success',
        'strategy': 'conditional-preferred-v1',
        'metric': 'max_high_sum_pct',
        'metric_label': f'入选后 T+1~T+{window} 累计最高涨幅',
        'windows': list(PREFERRED_HIST_WINDOWS),
        'ma_filters': [{'key': k, 'label': v} for k, v in PREFERRED_HIST_MA_FILTERS.items()],
        **data,
    })


# ===== 条件选股预览图: 手动生成 PNG 长图存 backend/previews/ =====
import html as _html_mod

PREVIEW_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'previews'))
PAGE_PREVIEW_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), 'previews_page'))
# 自定义选股条件: 存后端, 同一台后端上的设备 (多台电脑/手机) 共用一份
CUSTOM_PICKS_CONFIG_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), 'custom_picks_config.json'))


@app.route('/previews/<path:filename>')
def serve_preview(filename):
    """预览图静态访问 (send_from_directory 自带防 path traversal)."""
    return send_from_directory(PREVIEW_DIR, filename)


@app.route('/page-previews/<path:filename>')
def serve_page_preview(filename):
    """单次手动截图静态访问 (dashboard 条件优选『生成预览图』按钮产物,
    跟 CLI 批量产物分目录,文件名带毫秒时间戳防覆盖)."""
    return send_from_directory(PAGE_PREVIEW_DIR, filename)


def _build_conditional_preview_html(payload):
    """把条件选股结果渲染成精简 HTML (只有名称+代码, 深色风), 供 playwright 截图.
    多列网格排布, 避免票多时拉成超长图.
    title='条件选股新' 时标题显示「新」(条件选股新带缩量过滤); title='条件优选' 时
    按 dashboard 上条件优选 sub-tab 的 14 列表格 (说明+摘要+排名/名称/辅助命中/收盘/涨幅/
    量比/5 日主力/小单/MA60/T+1~T+5) 一比一复刻; 默认显示「条件选股」."""
    if payload.get('title_kind') == 'preferred':
        return _build_preferred_preview_html(payload)
    date = payload['date']
    rows = payload.get('results') or []
    cols = 4
    cells = []
    for i, r in enumerate(rows):
        code = (r.get('ts_code') or '').replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
        name = _html_mod.escape(r.get('name') or code)
        cells.append(f'''<div style="display:flex;align-items:baseline;gap:8px;padding:7px 12px;border-top:1px solid #1e2a3a">
  <span style="color:#4a5568;font-size:11px;min-width:22px;text-align:right">{i + 1}</span>
  <span style="font-size:14px;font-weight:600;white-space:nowrap">{name}</span>
  <span style="color:#4a5568;font-size:11px">{code}</span>
</div>''')
    if not cells:
        cells.append('<div style="padding:40px;text-align:center;color:#4a5568;grid-column:1/-1">'
                     '无命中 (该日没有满足全部规则的股票)</div>')
    if payload.get('title_kind') == 'new':
        title_label = '条件选股新'
    elif payload.get('title_kind') == 'custom':
        title_label = '自定义选股'
    else:
        title_label = '条件选股'
    subtitle = payload.get('subtitle')
    subtitle_html = (f'<div style="margin-top:7px;font-size:12px;color:#8892a4">{_html_mod.escape(subtitle)}</div>'
                     if subtitle else '')
    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body {{ margin:0; background:#0a0e17; font-family:'PingFang SC','Helvetica Neue',Arial,sans-serif; color:#e2e8f0; }}
</style></head><body>
<div id="capture" style="display:inline-block;padding:24px 28px;background:#0a0e17">
  <div style="font-size:18px;font-weight:700">📋 {title_label} · {date[:4]}年{date[4:6]}月{date[6:8]}日
    <span style="font-size:12px;font-weight:400;color:#8892a4">命中 {payload["count"]} 只</span>
  </div>
  {subtitle_html}
  <div style="margin-top:14px;display:grid;grid-template-columns:repeat({cols},max-content);column-gap:24px">
    {''.join(cells)}
  </div>
</div>
</body></html>'''


_OUTCOME_REASONS = {
    'waiting_close': '等待该日收盘',
    'missing_daily': '缺少对应交易日日线',
    'invalid_daily': '日线不完整',
    'reference_mismatch': '涨跌幅参考价不一致',
    'future_unavailable': '尚无对应交易日收盘数据',
}


def _build_preferred_preview_html(payload):
    """复刻 dashboard 条件优选 sub-tab 的 14 列完整表格 + 说明区 + 摘要行."""
    import math as _math
    date = payload.get('date') or ''
    rows = payload.get('results') or []
    stats = payload.get('preferred_exclusions') or {}
    threshold_yi = payload.get('threshold_yi', 3)
    volume_ratio_pct = payload.get('volume_ratio_pct', 90)
    base_count = stats.get('base_count') or len(rows)

    def _color(value):
        if value is None or not _math.isfinite(value): return '#8892a4'
        return '#ff5252' if value >= 0 else '#00e676'

    def _pct(value):
        if value is None or not _math.isfinite(value): return '—'
        sign = '+' if value >= 0 else ''
        return f'{sign}{value:.2f}%'

    body_rows = []
    for i, r in enumerate(rows):
        tags = []
        if r.get('preferred_mild_shrink'): tags.append('温和缩量')
        if r.get('preferred_below_ma60'): tags.append('不高于 MA60')
        tags_html = _html_mod.escape(' · '.join(tags) if tags else '仅核心条件')

        ma60 = r.get('preferred_ma60')
        if ma60 is None:
            ma60_tip = '不足60个有效收盘价，不计此项'
            ma60_html = f'<div title="{_html_mod.escape(ma60_tip)}">—</div>'
        else:
            ma60_tip = '入选日及此前60个有效收盘价均值'
            ma60_html = f'<div title="{_html_mod.escape(ma60_tip)}">{ma60:.2f}</div>'

        outcomes = (r.get('preferred_outcomes') or [])[:5]
        out_cells = []
        for o in outcomes:
            high = o.get('high_sum_pct')
            close = o.get('close_sum_pct')
            reason = o.get('reason')
            odate = o.get('date')
            tip_parts = [odate, _OUTCOME_REASONS.get(reason) if reason else '前期收盘涨幅之和 + 本日最高涨幅']
            tip = ' · '.join([t for t in tip_parts if t])
            out_cells.append(
                f'<td style="text-align:right;white-space:nowrap" title="{_html_mod.escape(tip)}">'
                f'<div style="color:{_color(high)}">{_pct(high)}</div>'
                f'<div style="font-size:10px;color:{_color(close)};opacity:0.6;margin-top:4px">收盘 {_pct(close)}</div>'
                f'</td>')
        while len(out_cells) < 5:
            out_cells.append('<td></td>')

        name = _html_mod.escape(r.get('name') or r.get('ts_code') or '')
        code = _html_mod.escape(r.get('ts_code') or '')
        small_net_yi = float(r.get('preferred_small_net') or 0) / 1e8
        body_rows.append(f'''<tr>
  <td style="text-align:center;color:#8892a4">{i + 1}</td>
  <td style="white-space:nowrap"><span style="font-weight:600">{name}</span><div style="color:#8892a4;font-size:11px;margin-top:3px">{code}</div></td>
  <td style="font-size:11px;color:#7ac8ff;white-space:nowrap">{int(r.get('preferred_priority') or 0)}/2 项<div style="color:#cbd5e0;margin-top:4px;font-size:10px">{tags_html}</div></td>
  <td style="text-align:right">{float(r.get('close') or 0):.2f}</td>
  <td style="text-align:right;color:{_color(r.get("change_pct"))}">{_pct(r.get("change_pct"))}</td>
  <td style="text-align:right">{float(r.get('volume_ratio_pct') or 0):.1f}%</td>
  <td style="text-align:right;color:#7ac8ff">{float(r.get('preferred_strength5') or 0):.2f}%</td>
  <td style="text-align:right">{small_net_yi:.2f} 亿</td>
  <td style="text-align:right">{ma60_html}</td>
  {''.join(out_cells)}
</tr>''')

    if not body_rows:
        body_rows.append(
            '<tr><td colspan="14" style="text-align:center;padding:35px;color:#8892a4">'
            '没有满足全部优选条件的股票</td></tr>')

    summary = (f'条件选股新 {base_count} 只 → 优选 {len(rows)} 只 · '
               f'资金门槛 ≥{threshold_yi} 亿 / 成交量 ≤{volume_ratio_pct}%')
    date_md = f'{date[:4]}年{date[4:6]}月{date[6:8]}日' if date else '—'

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body {{ margin:0; background:#0a0e17; font-family:'PingFang SC','Helvetica Neue',Arial,sans-serif; color:#e2e8f0; }}
  table {{ border-collapse:collapse; font-family:'JetBrains Mono','SF Mono','Menlo',monospace; font-size:12px; }}
  th {{ font-weight:500; color:#8892a4; padding:10px 8px; border-bottom:1px solid #1e2a3a; text-align:left; white-space:nowrap; }}
  td {{ padding:10px 8px; border-bottom:1px solid #1e2a3a; vertical-align:top; }}
</style></head><body>
<div id="capture" style="display:inline-block;padding:24px 28px;background:#0a0e17;min-width:1280px">
  <div style="color:#7ac8ff;font-weight:600;font-size:18px;margin-bottom:7px">条件优选 <span style="font-size:11px;font-weight:400;color:#8892a4">· 资金筛选 / 量价排序</span></div>
  <div style="font-size:12px;color:#cbd5e0;line-height:1.7;background:rgba(122,200,255,0.06);padding:10px 12px;border-radius:4px">
    继承「条件选股新」与上方门槛：<strong>当日小单净流出</strong>，且<strong>最近 5 个交易日主力净流入 / 对应成交额 ≥ 5%</strong>。五日记录须完整。<br>
    辅助排序：量比 70%～80%、收盘 ≤ MA60，每项命中优先一级；同级按主力强度排序。MA60 不足仅不加分。<br>
    <span style="color:#8892a4">累计收盘 = 第 N 日收盘 / 入选日收盘（复利，真实可成交）；累计最高 = 第 N 日最高 / 入选日收盘（当天冲高相对成本，非可成交收益）。</span>
  </div>
  <div style="margin-top:12px;font-size:12px;color:#cbd5e0">{_html_mod.escape(summary)}</div>
  <div style="font-size:12px;color:#7ac8ff;margin-top:4px;font-weight:600">📋 {date_md} · 命中 {len(rows)} 只</div>
  <table style="margin-top:14px;width:100%">
    <thead><tr>
      <th>排名</th><th>名称 / 代码</th><th>辅助命中</th>
      <th style="text-align:right">收盘价</th><th style="text-align:right">当日涨幅</th><th style="text-align:right">成交量比</th>
      <th style="text-align:right">5 日主力强度</th><th style="text-align:right">当日小单净额</th><th style="text-align:right">MA60</th>
      <th style="text-align:right">T+1</th><th style="text-align:right">T+2</th><th style="text-align:right">T+3</th><th style="text-align:right">T+4</th><th style="text-align:right">T+5</th>
    </tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table>
</div>
</body></html>'''


async def _render_html_to_png(html, out_path):
    """playwright 无头 chromium 截 #capture 元素 → PNG."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={'width': 1760, 'height': 1200}, device_scale_factor=2)
            page = await context.new_page()
            await page.set_content(html, wait_until='load')
            el = await page.query_selector('#capture')
            await el.screenshot(path=out_path)
        finally:
            await browser.close()


@app.route('/api/conditional-picks/preview-image', methods=['POST'])
def api_conditional_picks_preview_image():
    """手动生成条件选股预览图: body {date?, threshold_yi?} → PNG 存 PREVIEW_DIR, 返回可访问 url."""
    body = request.get_json(silent=True) or {}
    date = str(body.get('date') or '').strip()
    try:
        threshold_yi = float(body.get('threshold_yi') or 3)
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3

    payload = _conditional_picks_payload(date, '', False, threshold_yi)
    if not payload.get('date'):
        return jsonify({'status': 'error', 'note': payload.get('note') or '无可用数据日期'}), 400

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    # 文件名带门槛: 条件选股_20260728_3亿.png — 不同门槛各存各的, 不互相覆盖
    _th_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    out_name = f"条件选股_{payload['date']}_{_th_str}亿.png"
    out_path = os.path.join(PREVIEW_DIR, out_name)
    try:
        # 标题显示「条件选股」(不带新)
        asyncio.run(_render_html_to_png(_build_conditional_preview_html({**payload, 'title_kind': 'old'}), out_path))
    except Exception as e:
        return jsonify({'status': 'error', 'note': f'截图失败: {e}'}), 500
    return jsonify({
        'status': 'success', 'date': payload['date'], 'count': payload['count'],
        'url': '/previews/' + out_name, 'path': out_path
    })


@app.route('/api/conditional-picks-new/preview-image', methods=['POST'])
def api_conditional_picks_new_preview_image():
    """手动生成条件选股新预览图: body {date?, threshold_yi?, volume_ratio_pct?} → PNG 存 PREVIEW_DIR, 返回 url.
    复用 /api/conditional-picks/new 的完整链路 (含 volume_ratio 缩量过滤), HTML 复用老 builder (只看 results+date+count)."""
    body = request.get_json(silent=True) or {}
    date = str(body.get('date') or '').strip()
    try:
        threshold_yi = float(body.get('threshold_yi') or 3)
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    try:
        volume_ratio_pct = _parse_volume_ratio_pct(body.get('volume_ratio_pct'))
    except Exception:
        volume_ratio_pct = 80

    # 跟 /api/conditional-picks/new 完全一致: 先解析 trade_date, 跑 base 命中, 再套 volume_ratio 过滤
    # source 传空 = 东财自动口径 (按日兜底), 硬编码 push2 会让 6/16 之前的预览图空白
    source = ''
    trade_date = _resolve_conditional_new_date(date, source)
    daily_ready, readiness_reason = _conditional_new_daily_ready(trade_date)
    if not daily_ready:
        return jsonify({'status': 'error', 'note': f'当前不满足生成条件 ({readiness_reason})'}), 400

    payload = _read_conditional_snapshot(trade_date, threshold_yi)
    if payload is None:
        payload = _conditional_picks_payload(trade_date, source, False, threshold_yi)
    hits, exclusions = _filter_conditional_new(
        payload.get('results') or [], trade_date, volume_ratio_pct
    )
    payload.update({'count': len(hits), 'results': hits})
    if not payload.get('date'):
        return jsonify({'status': 'error', 'note': payload.get('note') or '无可用数据日期'}), 400

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    _th_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    _vr_str = ('%.0f' % volume_ratio_pct)
    # 文件名带门槛 + 缩量: 条件选股新_20260913_3亿_缩量80%.png — 不同门槛/缩量各存各的
    out_name = f"条件选股新_{payload['date']}_{_th_str}亿_缩量{_vr_str}%.png"
    out_path = os.path.join(PREVIEW_DIR, out_name)
    try:
        # 标题显示「条件选股新」(区分条件选股老的预览图)
        asyncio.run(_render_html_to_png(_build_conditional_preview_html({**payload, 'title_kind': 'new'}), out_path))
    except Exception as e:
        return jsonify({'status': 'error', 'note': f'截图失败: {e}'}), 500
    return jsonify({
        'status': 'success', 'date': payload['date'], 'count': payload['count'],
        'url': '/previews/' + out_name, 'path': out_path
    })


@app.route('/api/conditional-picks/preferred/market-overview', methods=['GET'])
def api_conditional_picks_preferred_market_overview():
    """条件优选任意日期市场温度: 入参 date=YYYYMMDD, 返 overview 行 (无则 available=false).
    供前端入选日/ T+N 日期切换拉取."""
    date = (request.args.get('date') or '').strip()
    if not re.fullmatch(r'\d{8}', date):
        return jsonify({'date': date, 'available': False,
                        'message': '日期格式需为 YYYYMMDD'}), 400
    return jsonify(_preferred_market_overview(date))


@app.route('/api/conditional-picks/preferred/preview-image', methods=['POST'])
def api_conditional_picks_preferred_preview_image():
    """手动生成条件优选预览图: body {date?, threshold_yi?, volume_ratio_pct?} → PNG.
    内部走 preferred 完整链路 (条件选股新 + 五交易日主力强度 + 当日小单净流出 + volume_ratio 缩量)."""
    body = request.get_json(silent=True) or {}
    date = str(body.get('date') or '').strip()
    try:
        threshold_yi = float(body.get('threshold_yi') or 3)
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    try:
        volume_ratio_pct = _parse_volume_ratio_pct(body.get('volume_ratio_pct'))
    except Exception:
        volume_ratio_pct = 80

    source = ''   # 东财自动口径 (按日兜底), 硬编码 push2 会让 6/16 之前空白
    trade_date = _resolve_conditional_new_date(date, source)
    daily_ready, readiness_reason = _conditional_new_daily_ready(trade_date)
    if not daily_ready:
        return jsonify({'status': 'error', 'note': f'当前不满足生成条件 ({readiness_reason})'}), 400

    payload = _read_conditional_snapshot(trade_date, threshold_yi)
    if payload is None:
        payload = _conditional_picks_payload(trade_date, source, False, threshold_yi)
    cached_new = _read_conditional_new_snapshot(trade_date, threshold_yi, volume_ratio_pct)
    if cached_new is not None:
        payload = {**payload, **cached_new}
    hits, exclusions = _filter_conditional_new(
        payload.get('results') or [], trade_date, volume_ratio_pct
    )
    payload.update({'count': len(hits), 'results': hits,
                    'daily_ready': True, 'readiness_reason': 'ready',
                    'exclusions': exclusions})
    rows = payload.get('results') or []
    rows, stats = _filter_conditional_preferred(rows, trade_date)
    _attach_preferred_outcomes(rows, trade_date)
    payload['results'] = rows
    payload['count'] = len(rows)
    payload['preferred_exclusions'] = stats
    payload['strategy'] = 'conditional-preferred-v1'

    if not payload.get('date'):
        return jsonify({'status': 'error', 'note': payload.get('note') or '无可用数据日期'}), 400

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    _th_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    _vr_str = ('%.0f' % volume_ratio_pct)
    out_name = f"条件优选_{payload['date']}_{_th_str}亿_缩量{_vr_str}%.png"
    out_path = os.path.join(PREVIEW_DIR, out_name)
    try:
        asyncio.run(_render_html_to_png(_build_conditional_preview_html({**payload, 'title_kind': 'preferred'}), out_path))
    except Exception as e:
        return jsonify({'status': 'error', 'note': f'截图失败: {e}'}), 500
    return jsonify({
        'status': 'success', 'date': payload['date'], 'count': payload['count'],
        'url': '/previews/' + out_name, 'path': out_path
    })


# ===== 条件优选 dashboard 真页面截图: 整面板所见即所得 =====
async def _render_dashboard_panel_to_png(page_url, out_path, threshold_yi=3, volume_ratio_pct=80, require_ma5=False):
    """访问 dashboard 真页面 → 等待条件优选面板渲染完 → 截 #conditionalPreferredPicksContainer.

    不复用 _render_html_to_png 路径,因为那个走的是 _build_preferred_preview_html 字符串模板,
    跟 dashboard 真页面有 drift (intraday reason / _OUTCOME_REASONS / null fallback 等).
    此处 Playwright 真访问前端,所见即所得.
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            context = await browser.new_context(
                # viewport 高度要 >= container 实际 bottom (panel 顶部 515 + container 高 ~1010 = 1525 + buffer)
                # 否则 element.screenshot 截 viewport-visible 部分, 底部行丢失
                viewport={'width': 1760, 'height': 1700}, device_scale_factor=2)
            page = await context.new_page()
            page.set_default_timeout(20000)

            await page.goto(page_url, wait_until='domcontentloaded', timeout=20000)

            # 后端先预热 preferred API (lazy 模块首次 import + SQL 编译, 约 20s)
            import urllib.request as _ur
            try:
                _ur.urlopen(f'{backend_base}/api/conditional-picks/preferred?threshold_yi=3&volume_ratio_pct=80', timeout=30).read()
            except Exception:
                pass

            # 等页面初始 JS 跑完
            await page.wait_for_timeout(2000)

            # 强制显示容器 + 把传入参数注入到前端 input (前端不会自动从 URL 读 query string)
            # 必须先激活 picks 主 tab, 否则 panel-picks 是 display:none, 子容器不可见
            await page.evaluate("""(params) => {
              document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
              const picksPanel = document.getElementById('panel-picks');
              if (picksPanel) picksPanel.classList.add('active');
              const c = document.getElementById('conditionalPreferredPicksContainer');
              if (c) c.style.display = '';
              // 把 threshold_yi 写进前端控件
              const sel = document.getElementById('picksThresholdSel');
              const customInp = document.getElementById('picksThresholdCustom');
              const yi = Number(params.threshold_yi);
              const allowed = ['3', '4', '5'];
              if (sel && customInp) {
                if (allowed.includes(String(yi))) {
                  sel.value = String(yi);
                } else {
                  sel.value = 'custom';
                  customInp.value = String(yi);
                }
              }
              // 把 volume_ratio_pct 写进前端 input
              const ratioInp = document.getElementById('picksVolumeRatioInput');
              if (ratioInp) ratioInp.value = String(Number(params.volume_ratio_pct));
              // 「站上5日线」开关存在 localStorage 里, 截图用独立浏览器, 必须显式写进去
              try {
                localStorage.setItem('tradeiq.conditionalPreferred.requireMa5', params.require_ma5 ? '1' : '0');
              } catch (e) {}
              if (typeof _paintPrefMa5Toggle === 'function') _paintPrefMa5Toggle();
              // 触发 fetch (loadConditionalPreferredPicks 内部会读 input 当前值)
              if (typeof loadConditionalPreferredPicks === 'function') loadConditionalPreferredPicks();
            }""", {'threshold_yi': threshold_yi, 'volume_ratio_pct': volume_ratio_pct,
                   'require_ma5': 1 if require_ma5 else 0})

            # 等 表行 + 4 charts (canvas 数量) + 摘要包含传入的 volumePct 才算渲染完
            # 用 canvas 数量判断 echarts (因为 _conditionalPreferredIndexCharts 是 const 不在 window 上)
            # 注意: 不能光等 "盘中实时", 因为 dashboard 加载时可能自动 fetch 一次默认阈值,
            # summary 会立刻满足但用错阈值; 必须等 summary 里出现传入的 volumePct 才是新 fetch 落地.
            # 把目标值写进 dataset, wait_for_function 读它.
            await page.evaluate(
                "(p) => { const s = document.getElementById('conditionalPreferredSummary'); if (s) s.dataset.targetRatio = String(p.volume_ratio_pct); s.dataset.targetYi = String(p.threshold_yi); }",
                {'threshold_yi': threshold_yi, 'volume_ratio_pct': volume_ratio_pct})
            await page.wait_for_function("""
              () => {
                const s = document.getElementById('conditionalPreferredSummary');
                const body = document.querySelector('#conditionalPreferredPicksBody tr[data-ts-code]');
                const charts = document.querySelectorAll('#conditionalPreferredIndexGrid canvas').length;
                if (!s || !body || charts < 4) return false;
                const summary = s.textContent || '';
                const targetRatio = s.dataset.targetRatio || '';
                const targetYi = s.dataset.targetYi || '';
                return /收盘确认|盘中实时|等待收盘数据/.test(summary)
                  && summary.includes(`≤${targetRatio}%`)
                  && summary.includes(`≥${targetYi} 亿`);
              }
            """, timeout=40000)

            # chart.resize + 等 200ms 让字体稳定
            await page.evaluate("() => Object.values(window._conditionalPreferredIndexCharts || {}).forEach(c => c && c.resize())")
            await page.wait_for_timeout(200)

            # 只藏摘要行的操作按钮 (📷生成预览图 / 刷新结果), 不藏表格 cell 里的 kline/flow 按钮 (用 :not 排除)
            await page.evaluate("() => document.querySelectorAll('#conditionalPreferredPicksContainer button:not([data-preferred-action])').forEach(b => b.style.visibility = 'hidden')")

            container = await page.query_selector('#conditionalPreferredPicksContainer')
            await container.screenshot(path=out_path)
        finally:
            await browser.close()


@app.route('/api/conditional-picks/preferred/preview-page-image', methods=['POST'])
def api_conditional_picks_preferred_page_preview():
    """条件优选整面板截图: Playwright 真实访问 dashboard, 截 #conditionalPreferredPicksContainer.

    与 /api/conditional-picks/preferred/preview-image (CLI 合成 PDF 用, 走 HTML 模板) 并存.
    用户点击 dashboard 上『📷 生成预览图』按钮 → 看到的是 dashboard 真页面, 零 drift.
    """
    import time as _time
    body = request.get_json(silent=True) or {}
    date = str(body.get('date') or '').strip()
    try:
        threshold_yi = float(body.get('threshold_yi') or 3)
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    try:
        volume_ratio_pct = _parse_volume_ratio_pct(body.get('volume_ratio_pct'))
    except Exception:
        volume_ratio_pct = 80
    require_ma5 = _parse_preferred_ma5(body.get('require_ma5'))

    source = ''   # 东财自动口径 (按日兜底), 硬编码 push2 会让 6/16 之前空白
    trade_date = _resolve_conditional_new_date(date, source)
    daily_ready, readiness_reason = _conditional_new_daily_ready(trade_date)
    if not daily_ready:
        return jsonify({'status': 'error', 'note': f'当前不满足生成条件 ({readiness_reason})'}), 400

    os.makedirs(PAGE_PREVIEW_DIR, exist_ok=True)
    ts_ms = int(_time.time() * 1000)
    _th_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    _vr_str = ('%.0f' % volume_ratio_pct)
    _ma5_str = '站上5日线' if require_ma5 else '原条件'
    out_name = f"条件优选页面_{trade_date or 'latest'}_{_th_str}亿_缩量{_vr_str}%_{_ma5_str}-{ts_ms}.png"
    out_path = os.path.join(PAGE_PREVIEW_DIR, out_name)

    backend_base = os.environ.get('BACKEND_BASE_URL') or 'http://127.0.0.1:5555'
    page_url = (f'{backend_base}/?tab=picks&sub=conditionalPreferred'
                f'&date={trade_date or ""}&threshold_yi={threshold_yi}'
                f'&volume_ratio_pct={volume_ratio_pct}')

    started = _time.time()
    try:
        asyncio.run(_render_dashboard_panel_to_png(page_url, out_path,
                                                   threshold_yi=threshold_yi,
                                                   volume_ratio_pct=volume_ratio_pct,
                                                   require_ma5=require_ma5))
    except Exception as e:
        return jsonify({'status': 'error', 'note': f'截图失败: {e}'}), 500
    return jsonify({
        'status': 'success', 'date': trade_date,
        'url': '/page-previews/' + out_name, 'path': out_path,
        'duration_ms': int((_time.time() - started) * 1000),
    })


@app.route('/api/combined-picks', methods=['GET'])
def api_combined_picks():
    """组合选股: 同日同时被 (系统推荐 stock_picks) 和 (条件选股) 命中的票.

    Args:
        date: 交易日 YYYYMMDD. 不传 = 取 stock_picks 表最新 trade_date
    """
    date = (request.args.get('date') or '').strip()
    intraday_flag = request.args.get('intraday') in ('1', 'true', 'True')
    # 累计净流入门槛: ?threshold_yi=N (单位亿元, 默认 3)
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    threshold_amt = threshold_yi * 1e8


    # 1) 选定 date: 不传 -> stock_picks 最新 trade_date
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM stock_picks').fetchone()
        if not row or not row['d']:
            conn.close()
            return jsonify({
                'status': 'success', 'date': '', 'count': 0, 'results': [],
                'note': '系统推荐未同步, 请先在「系统推荐」tab 点击同步今日'
            })
        date = row['d']

    # 2) 拿当日 stock_picks 全量 (系统推荐)
    system_rows = conn.execute(
        'SELECT * FROM stock_picks WHERE trade_date = ? ORDER BY score DESC', (date,)
    ).fetchall()
    system_codes = {r['ts_code'] for r in system_rows}

    # 3) 同日条件选股 raw (复用 _compute_conditional_picks_internal, source 必须已归一化)
    #    按日兜底: 6/16 之前只有 daykline, 硬编码 push2 会让那天全空
    src = _em_flow_source_for_date(conn, date) or 'eastmoney-push2'
    cond_data, _comb_next_dates = _compute_conditional_picks_internal(date, src, intraday_close=intraday_flag)
    # 基础规则 (与门槛无关): 交集和快照都按全量候选存, 门槛等返回前再套
    cond_base = _filter_conditional_base(cond_data) if cond_data else []
    cond_by_code = {r['ts_code']: dict(r) for r in cond_base}
    conn.close()

    # 4) 交集: 以 stock_picks 为主体 (TS_code, name, score, signal_type 等)
    #    再合并 cond 那一侧渲染需要的字段, 让组合选股 tab 跟条件选股 tab 字段一致 (只多一个 score)
    _COND_RENDER_KEYS = (
        'close', 'change_pct',
        'used_bucket', 'hit_threshold',
        'sum_3d', 'sum_5d', 'sum_10d', 'sum_20d',
        'sum_mid_small_3d', 'sum_mid_small_5d', 'sum_mid_small_10d', 'sum_mid_small_20d',
        'next_1_close', 'next_1_change_pct', 'next_1_high',
        'next_2_close', 'next_2_change_pct', 'next_2_high',
        'next_3_close', 'next_3_change_pct', 'next_3_high',
        'next_4_close', 'next_4_change_pct', 'next_4_high',
        'next_5_close', 'next_5_change_pct', 'next_5_high',
        'max_high_t1', 'max_high_t2', 'max_high_t3', 'max_high_t4', 'max_high_t5',
        'post_pick_next_change_pct',
        'post_pick_next2_change_pct', 'post_pick_next3_change_pct',
        'post_pick_next4_change_pct', 'post_pick_next5_change_pct',
    )
    intersected = []
    for r in system_rows:
        cond_row = cond_by_code.get(r['ts_code'])
        if not cond_row:
            continue
        merged = dict(r)  # system: score / signal_type / ma* / etc.
        for k in _COND_RENDER_KEYS:
            if k in cond_row:
                merged[k] = cond_row[k]
        intersected.append(merged)
    _attach_sector(intersected, key='ts_code')
    # T+i 是今天 → 只对交集票拉 sina 实时价覆盖 (几十只, 避免全市场拉)
    _apply_intraday_next(intersected, _comb_next_dates)

    if not system_rows:
        return jsonify({
            'status': 'success', 'date': date, 'count': 0, 'results': [],
            'note': f'{date} 当日系统推荐未同步, 无交集'
        })

    # 计算完即写入盘后快照 (同条件选股的"计算完即入库";
    # 之前漏了这步, combined_picks_snapshot 表一直停在 2026-07-24)
    # 守卫: 本次 0 命中但该日已有非空快照 → 不覆盖
    try:
        _c = sqlite3.connect(DB_PATH)
        _skip = False
        if not intersected:
            _old = _c.execute(
                'SELECT count FROM combined_picks_snapshot WHERE trade_date = ?',
                (date,)).fetchone()
            _skip = bool(_old and _old[0])
            if _skip:
                print(f'[combined] {date} 本次 0 命中, 保留已有非空快照 ({_old[0]} 条), 不覆盖', flush=True)
        if not _skip:
            _c.execute(
                'INSERT OR REPLACE INTO combined_picks_snapshot '
                '(trade_date, count, results_json, note) VALUES (?,?,?,?)',
                (date, len(intersected), json.dumps(intersected, ensure_ascii=False),
                 '' if intersected else f'{date} 系统推荐与条件选股无交集'))
            _c.commit()
        _c.close()
    except Exception as _e:
        print(f'[combined] 快照入库失败 (不影响返回): {_e}', flush=True)

    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    # 门槛过滤只作用于本次返回 (快照已存全量候选); 保持 stock_picks score 顺序不重排
    displayed = _apply_cond_threshold(intersected, threshold_amt)
    # 同 _conditional_picks_payload: T+1 是今天 → 前端渲染 "⚡ 待实时" + 实时刷新
    from datetime import datetime as _dt3
    _comb_next_1_is_today = bool(_comb_next_dates and _comb_next_dates[0] == _dt3.now().strftime('%Y%m%d'))
    return jsonify({
        'status': 'success',
        'date': date,
        'count': len(displayed),
        'results': displayed,
        'next_1_is_today': _comb_next_1_is_today,
        'filter': {
            'cumulative_window': '3d / 5d / 10d / 20d 必须 > 0',
            'threshold_yi': threshold_yi,
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        },
        'note': '' if displayed else f'{date} 系统推荐与条件选股无交集 (同日两边都命中的票为 0)'
    })


def _combined_picks_payload_ths(date, intraday_flag, threshold_yi):
    """api_picks_ths_combined 的数据部分 (THS 版).
    交集: 同日 stock_picks_ths (系统推荐) ∩ conditional_picks_snapshot_ths (条件选股) 命中."""
    threshold_amt = threshold_yi * 1e8

    # 1) 选定 date: 不传 -> stock_picks_ths 最新 trade_date
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM stock_picks_ths').fetchone()
        if not row or not row['d']:
            conn.close()
            return {
                'status': 'success', 'date': '', 'count': 0, 'results': [],
                'note': '同花顺系统推荐未同步, 请先在「系统推荐」tab 点击同步今日'
            }
        date = row['d']

    # 2) stock_picks_ths (系统推荐) 全量
    system_rows = conn.execute(
        'SELECT * FROM stock_picks_ths WHERE trade_date = ? ORDER BY score DESC', (date,)
    ).fetchall()
    system_codes = {r['ts_code'] for r in system_rows}

    # 3) 同日 THS 条件选股 raw
    cond_data, _comb_next_dates = _compute_conditional_picks_internal_ths(date, intraday_close=intraday_flag)
    cond_base = _filter_conditional_base_ths(cond_data) if cond_data else []
    cond_by_code = {r['ts_code']: dict(r) for r in cond_base}
    conn.close()

    # 4) 交集: stock_picks_ths (主体) ∩ cond_by_code; 合并 cond 字段
    _THS_COND_RENDER_KEYS = (
        'close', 'change_pct',
        'code',
        'used_bucket', 'hit_threshold',
        'sum_3d', 'sum_5d', 'sum_10d', 'sum_20d',
        'next_1_close', 'next_1_change_pct', 'next_1_high',
        'next_2_close', 'next_2_change_pct', 'next_2_high',
        'next_3_close', 'next_3_change_pct', 'next_3_high',
        'next_4_close', 'next_4_change_pct', 'next_4_high',
        'next_5_close', 'next_5_change_pct', 'next_5_high',
        'max_high_t1', 'max_high_t2', 'max_high_t3', 'max_high_t4', 'max_high_t5',
        'post_pick_next_change_pct',
        'post_pick_next2_change_pct', 'post_pick_next3_change_pct',
        'post_pick_next4_change_pct', 'post_pick_next5_change_pct',
    )
    intersected = []
    for r in system_rows:
        cond_row = cond_by_code.get(r['ts_code'])
        if not cond_row:
            continue
        merged = dict(r)  # system: score / signal_type / ma* / ths_net_* / etc
        for k in _THS_COND_RENDER_KEYS:
            if k in cond_row:
                merged[k] = cond_row[k]
        intersected.append(merged)
    _attach_sector(intersected, key='ts_code')
    _apply_intraday_next(intersected, _comb_next_dates)

    if not system_rows:
        return {
            'status': 'success', 'date': date, 'count': 0, 'results': [],
            'note': f'{date} 当日同花顺系统推荐未同步, 无交集'
        }

    # 5) 写入盘后快照
    try:
        _c = sqlite3.connect(DB_PATH)
        _skip = False
        if not intersected:
            _old = _c.execute(
                'SELECT count FROM combined_picks_snapshot_ths WHERE trade_date = ?',
                (date,)).fetchone()
            _skip = bool(_old and _old[0])
            if _skip:
                print(f'[ths combined] {date} 本次 0 命中, 保留已有非空快照 ({_old[0]} 条), 不覆盖', flush=True)
        if not _skip:
            _c.execute(
                'INSERT OR REPLACE INTO combined_picks_snapshot_ths '
                '(trade_date, count, results_json, note) VALUES (?,?,?,?)',
                (date, len(intersected), json.dumps(intersected, ensure_ascii=False),
                 '' if intersected else f'{date} 同花顺系统推荐与条件选股无交集'))
            _c.commit()
        _c.close()
    except Exception as _e:
        print(f'[ths combined] 快照入库失败 (不影响返回): {_e}', flush=True)

    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    displayed = _apply_cond_threshold_ths(intersected, threshold_amt)
    from datetime import datetime as _dt3
    _comb_next_1_is_today = bool(_comb_next_dates and _comb_next_dates[0] == _dt3.now().strftime('%Y%m%d'))
    return {
        'status': 'success',
        'date': date,
        'count': len(displayed),
        'results': displayed,
        'next_1_is_today': _comb_next_1_is_today,
        'filter': {
            'cumulative_window': '3d / 5d / 10d (THS 历史太短, 跳过 20d) 必须 > 0',
            'threshold_yi': threshold_yi,
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        },
        'note': '' if displayed else f'{date} 同花顺系统推荐与条件选股无交集 (同日两边都命中的票为 0)'
    }


def _read_combined_snapshot_ths(date, threshold_yi):
    """THS 组合选股盘后快照直读."""
    today = datetime.now().strftime('%Y%m%d')
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not date:
        row = conn.execute('SELECT MAX(trade_date) AS d FROM combined_picks_snapshot_ths').fetchone()
        date = (row['d'] or '') if row else ''
    if not date:
        conn.close()
        return None
    row = conn.execute(
        'SELECT count, results_json, note FROM combined_picks_snapshot_ths WHERE trade_date = ?',
        (date,)).fetchone()
    if not row or not (row['count'] or 0):
        conn.close()
        return None
    if date >= today:
        conn.close()
        return None
    try:
        results = json.loads(row['results_json'] or '[]')
    except Exception:
        results = []
    if not results:
        conn.close()
        return None
    next_dates = _next_workdays(date, 5)
    if next_dates[0] == today:
        conn.close()
        return None
    stale_i = _snapshot_tn_stale(conn, results, next_dates)
    conn.close()
    if stale_i:
        print(f'[ths combined snapshot] {date} T+{stale_i} 已收盘但快照缺数据, 回落实时算自愈', flush=True)
        return None
    threshold_amt = threshold_yi * 1e8
    hits = _apply_cond_threshold_ths(results, threshold_amt)
    _th_yi_str = ('%.1f' % threshold_yi).rstrip('0').rstrip('.') if threshold_yi != int(threshold_yi) else str(int(threshold_yi))
    return {
        'status': 'success',
        'date': date,
        'count': len(hits),
        'results': hits,
        'next_1_is_today': False,
        'filter': {
            'cumulative_window': '3d / 5d / 10d (THS 历史太短, 跳过 20d) 必须 > 0',
            'threshold_yi': threshold_yi,
            'threshold': f'至少 1 项累计 ≥ {_th_yi_str} 亿'
        },
        'note': ((row['note'] or '') + ' · ' if row['note'] else '') + '快照库直读',
    }


@app.route('/api/picks/ths/combined', methods=['GET'])
def api_picks_ths_combined():
    """同花顺组合选股: 同日 stock_picks_ths (系统推荐) ∩ conditional_picks_snapshot_ths (条件选股) 命中."""
    date = (request.args.get('date') or '').strip()
    intraday_flag = request.args.get('intraday') in ('1', 'true', 'True')
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    return jsonify(_combined_picks_payload_ths(date, intraday_flag, threshold_yi))


@app.route('/api/picks/ths/combined/snapshot', methods=['GET'])
def api_picks_ths_combined_snapshot():
    """同花顺组合选股盘后快照: 优先直读 combined_picks_snapshot_ths;
    无快照 / 今日 / T+1 是今天 / T+N 数据缺口 → 回落实时算."""
    date = (request.args.get('date') or '').strip()
    try:
        threshold_yi = float(request.args.get('threshold_yi') or '3')
        if threshold_yi < 0 or threshold_yi > 100:
            threshold_yi = 3
    except Exception:
        threshold_yi = 3
    payload = _read_combined_snapshot_ths(date, threshold_yi)
    if payload is not None:
        return jsonify(payload)
    return jsonify(_combined_picks_payload_ths(date, False, threshold_yi))


@app.route('/api/picks/sync-date', methods=['POST'])
def api_picks_sync_date():
    """按日期一键同步: 日线 → 物化表重建 → 系统推荐 → 条件/组合选股快照.

    顺序就是药效: 先补原料 (stock_daily), 再重建 fund_flow_ranked
    (治盘中补建留下的 NULL close), 最后算选股写快照.
    供条件/组合选股 tab 的「同步数据」按钮 + 17 点后空快照自动触发.

    body: {date?: 'YYYYMMDD'}  不传 = 今天. 未来日期 / 非交易日直接拒绝.
    """
    data = request.get_json(silent=True) or {}
    date = (data.get('date') or request.args.get('date') or '').strip()
    today = datetime.now().strftime('%Y%m%d')
    if not date:
        date = today
    if date > today:
        return jsonify({'status': 'error', 'message': f'{date} 是未来日期'}), 400

    conn = sqlite3.connect(DB_PATH)
    is_trading_day = conn.execute(
        'SELECT 1 FROM trading_dates_cache WHERE cal_date = ?', (date,)).fetchone()
    if not is_trading_day:
        conn.close()
        return jsonify({'status': 'success', 'date': date, 'steps': {},
                        'note': f'{date} 非交易日, 跳过'})
    ff_has = conn.execute(
        'SELECT 1 FROM fund_flow WHERE trade_date = ? LIMIT 1', (date,)).fetchone()
    sd_n = conn.execute(
        'SELECT COUNT(*) FROM stock_daily WHERE trade_date = ?', (date,)).fetchone()[0]
    picks_n = conn.execute(
        'SELECT COUNT(*) FROM stock_picks WHERE trade_date = ?', (date,)).fetchone()[0]
    conn.close()

    steps = {}

    # 0) 主力资金流: 今天的没同步 (白天没打开过页面) → 先拉 (沿用盘中同款逻辑)
    if not ff_has and date == today and not _AKSYNC_STATE.get('running'):
        try:
            _AKSYNC_STATE['running'] = True
            from fetch_fund_flow_today import fetch_today_market
            _rows, _total, _failed, _actual = fetch_today_market(
                progress_callback=None, headless=False, verbose=True, skip_if_exists=True)
            steps['fund_flow'] = len(_rows) if _rows else 0
            ff_has = bool(_rows)
        except Exception as e:
            print(f'[picks sync-date] 当日资金流同步失败: {e}', flush=True)
            steps['fund_flow'] = f'失败: {e}'
        finally:
            _AKSYNC_STATE['running'] = False

    if not ff_has:
        return jsonify({'status': 'success', 'date': date, 'steps': steps,
                        'note': f'{date} 无主力资金流数据, 无法计算选股 (历史日期需先用回填脚本补主力净流入)'})

    # 1) 日线: 缺则从 TuShare 补 (盘后 16:00 前可能还没发布, sync 返 0 不算错)
    if sd_n == 0:
        steps['stock_daily'] = sync_stock_daily(date)
    else:
        steps['stock_daily'] = f'已有 {sd_n} 行'

    # 2) 物化表: 增量重建该日及之后 (重新 JOIN stock_daily, 治 NULL close)
    steps['ranked_rebuilt'] = build_fund_flow_ranked(since_date=date)

    # 3) 系统推荐: 缺则重算写表 (TuShare 未入库时可能 0 条, 留 note 提示)
    if picks_n == 0:
        picks = _compute_picks(trade_date=date)
        if picks:
            _write_stock_picks_for_date(date, picks)
            steps['stock_picks'] = len(picks)
        else:
            steps['stock_picks'] = '0 (日线未就位?)'
    else:
        steps['stock_picks'] = f'已有 {picks_n} 条'

    # 4) 条件 + 组合选股: 内部自写快照 (复用路由, 行为跟手动打开 tab 一致)
    with app.test_client() as c:
        r1 = (c.get(f'/api/conditional-picks?date={date}').get_json() or {})
        r2 = (c.get(f'/api/combined-picks?date={date}').get_json() or {})
        # 4.5) 条件选股新 + 条件优选: 同样靠路由内部 _save_conditional_*_snapshot 落库
        r3 = (c.get(f'/api/conditional-picks/new?date={date}&threshold_yi=3&volume_ratio_pct=80').get_json() or {})
        r4 = (c.get(f'/api/conditional-picks/preferred?date={date}&threshold_yi=3&volume_ratio_pct=80').get_json() or {})
    steps['conditional_picks'] = r1.get('count', 0)
    steps['combined_picks'] = r2.get('count', 0)
    steps['conditional_picks_new'] = r3.get('count', 0)
    steps['conditional_picks_preferred'] = r4.get('count', 0)

    print(f'[picks sync-date] {date} -> {steps}', flush=True)
    return jsonify({'status': 'success', 'date': date, 'steps': steps,
                    'note': r2.get('note') or r1.get('note') or ''})


@app.route('/api/conditional-combined/backfill', methods=['POST'])
def api_conditional_combined_backfill():
    """扫描最近 N 个交易日, 补齐条件选股 + 组合选股的盘后快照.
    两张快照表都已有该日期的 → 跳过; 缺哪个补哪个.
    组合选股依赖当日 stock_picks (系统推荐), 没同步的日期记 fail.
    同步执行, 60 天约 2-4 分钟. body: {days?: 60}
    """
    data = request.get_json(silent=True) or {}
    try:
        days = max(1, min(int(data.get('days') or 60), 250))
    except (TypeError, ValueError):
        days = 60

    conn = sqlite3.connect(DB_PATH)
    dates = [r[0] for r in conn.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date <= ? ORDER BY cal_date DESC LIMIT ?',
        (datetime.now().strftime('%Y%m%d'), days)).fetchall()]
    cond_have = {r[0] for r in conn.execute('SELECT trade_date FROM conditional_picks_snapshot').fetchall()}
    comb_have = {r[0] for r in conn.execute('SELECT trade_date FROM combined_picks_snapshot').fetchall()}
    picks_have = {r[0] for r in conn.execute('SELECT DISTINCT trade_date FROM stock_picks').fetchall()}
    conn.close()

    details = []
    n_ok = n_skip = n_fail = 0
    with app.test_client() as c:
        for d in dates:
            need_cond = d not in cond_have
            need_comb = d not in comb_have
            if not need_cond and not need_comb:
                n_skip += 1
                continue
            try:
                filled = []
                if need_cond:
                    j1 = c.get(f'/api/conditional-picks?date={d}').get_json() or {}
                    # payload 内部已写 conditional_picks_snapshot; 无数据日期不入库, 记 fail
                    if not j1.get('count') and j1.get('note'):
                        raise RuntimeError(j1['note'])
                    filled.append('条件')
                if need_comb:
                    if d not in picks_have:
                        raise RuntimeError('当日系统推荐未同步, 组合选股无法计算')
                    c.get(f'/api/combined-picks?date={d}')  # 路由内部已写 combined_picks_snapshot
                    filled.append('组合')
                n_ok += 1
                details.append({'date': d, 'status': 'ok', 'reason': '补' + '+'.join(filled)})
            except Exception as e:
                n_fail += 1
                details.append({'date': d, 'status': 'fail', 'reason': str(e)[:100]})
            print(f"[backfill snapshots] {d} -> {details[-1]['status']} {details[-1]['reason']}", flush=True)

    return jsonify({
        'status': 'success',
        'total_dates': len(dates),
        'success': n_ok,
        'skipped': n_skip,
        'failed': n_fail,
        'details': details,
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


@app.route('/api/user-picks/feishu-sync', methods=['POST'])
def api_user_picks_feishu_sync():
    """加自选按钮专用: 把某日 user_picks 名单刷进飞书盘面晴雨表「当日低吸成功票」列.

    只由前端「➕ 自选」按钮调用 (手动输入 / 上传识别 不走这条).
    date 缺省 = 今天; 传 YYYYMMDD 或 YYYY-MM-DD 都认.
    """
    data = request.get_json(silent=True) or request.form or {}
    date = (data.get('date') or request.args.get('date') or '').strip()
    return jsonify(sync_user_picks_to_feishu(date))


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

# 冲高观察独立只读查询；沿用现有数据库路径。
from surge_picks import register_surge_picks
register_surge_picks(app, lambda: DB_PATH)

from dip_picks import register_dip_picks
from intraday_picks import IntradayPicks, fetch_eastmoney_intraday
_intraday_picks = IntradayPicks(fetch_sina_quotes, fund_loader=fetch_eastmoney_intraday)
register_dip_picks(app, lambda: DB_PATH, outcome_loader=_attach_preferred_outcomes,
                   intraday_provider=_intraday_picks)

from custom_picks import register_custom_picks
register_custom_picks(app, lambda: DB_PATH, outcome_loader=_attach_preferred_outcomes,
                       preview_dir=PREVIEW_DIR,
                       preview_builder=_build_conditional_preview_html,
                       preview_renderer=_render_html_to_png,
                       config_path=CUSTOM_PICKS_CONFIG_PATH, intraday_provider=_intraday_picks)

from hot_sectors import register_hot_sectors
register_hot_sectors(app, lambda: DB_PATH)

# 在线设备统计 (独立 visitors.db, 手机端 5556 也注册同一个文件, 两边看到的数字一致)
from visitor_stats import register_visitor_stats
register_visitor_stats(app)

# 模拟盘: 历史行情回放 + 人工纸面交易。时钟由该模块权威持有, 手续费复用上面的算法。
from replay_paper import register_replay_paper
register_replay_paper(app, lambda: DB_PATH, buy_fee=calc_buy_fees, sell_fee=calc_sell_fees)


if __name__ == '__main__':
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")
    # 启动时全量 rebuild fund_flow_ranked (8s 一次性, 让条件选股走物化表)
    n = build_fund_flow_ranked()
    print(f"[fund_flow_ranked] 全量 rebuild 完成: {n} 行")
    # 启动 APScheduler 调度 (各功能任务在各模块里注册)
    try:
        from scheduler import (start_scheduler, register_daily_review, register_push_jobs,
                               register_stock_daily_backfill, register_picks_resync_after_close,
                               register_conditional_combined_snapshot,
                               register_market_fflow_after_close,
                               register_adj_factor_after_close,
                               register_barometer_after_close,
                               register_stock_minline_after_close,
                               register_index_minline_after_close,
                               register_ths_flow_after_close,
                               register_ths_picks_resync_after_close,
                               register_ths_conditional_combined_snapshot,
                               register_limitup_collection_intraday,
                               register_bk_flow_after_close)
        register_limitup_collection_intraday() # 盘中涨停合集采集 (每分钟, 函数内有时段/交易日守卫)
        register_stock_daily_backfill()          # 盘后同步日线 (15:35 抢跑 / 16:30 兜底)
        register_picks_resync_after_close()      # 收盘口径重算系统推荐 (16:00)
        register_conditional_combined_snapshot() # 盘后写条件/组合选股快照 (17:00)
        register_market_fflow_after_close()      # 盘后落盘大盘资金流 (15:35)
        register_adj_factor_after_close(token=TUSHARE_TOKEN)  # 盘后拉复权因子 (17:05 主跑 / 18:30 兜底)
        register_barometer_after_close()       # 盘后汇总盘面晴雨表 (15:37, 供手机端晴雨 tab)
        register_bk_flow_after_close()           # 盘后落盘板块资金流 (15:35 行业 / 15:36 概念 / 16:30 兜底)
        register_stock_minline_after_close()     # 盘后批量抓全市场个股分时入库 (15:05 / 16:30)
        register_index_minline_after_close()     # 盘后批量抓 4 大指数分时入库 (15:05 / 16:30)
        register_ths_flow_after_close()          # 盘后抓同花顺个股资金流 (17:05 / 19:30 兜底)
        register_ths_picks_resync_after_close()  # THS 资金流入库后重算 THS 推荐 (17:10 / 19:35)
        register_ths_conditional_combined_snapshot()  # THS 条件/组合选股快照入库 (17:15 / 19:40)
        register_daily_review()          # 盘后复盘
        register_push_jobs()             # 定时推送
        start_scheduler()
    except Exception as e:
        print(f'[scheduler] 启动失败 (非致命, Flask 继续): {e}')
    print("启动 Flask 服务...")
    app.run(debug=True, host='0.0.0.0', port=5555)
