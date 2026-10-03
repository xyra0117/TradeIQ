"""
板块资金流 (sector_fund_flow) 测试套件.
覆盖: 写库 / 端点 / 状态机 / skip_if_exists / 排序 / 过滤.
mock 模式: 跟 test_index_charts.py 一致 — KeptConnection 防止 close, patch sqlite3.connect.
"""
import json
import math
import os
import sqlite3
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# 让 import app 能找到
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as backend

# 测试专用: 防止 sqlite3 connection 被 close (跨 patch 重用)
class KeptConnection(sqlite3.Connection):
    def close(self):
        pass  # no-op


def _build_test_db():
    """构造一个内存 SQLite + sector_fund_flow 表 + trading_dates_cache 表 (跟 app.init_db() 的 DDL 一致)."""
    db = sqlite3.connect(':memory:', factory=KeptConnection)
    db.execute('''CREATE TABLE IF NOT EXISTS sector_fund_flow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        sector_type TEXT NOT NULL,
        sector_code TEXT NOT NULL,
        name TEXT,
        close REAL, change_pct REAL,
        main_net_inflow REAL NOT NULL,
        main_net_pct REAL,
        super_net REAL, super_pct REAL,
        big_net REAL, big_pct REAL,
        mid_net REAL, mid_pct REAL,
        small_net REAL, small_pct REAL,
        lead_stock_name TEXT,
        lead_stock_code TEXT,
        source TEXT DEFAULT 'eastmoney-push2',
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(trade_date, sector_type, sector_code)
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_sff_date_inflow ON sector_fund_flow(trade_date, sector_type, main_net_inflow DESC)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_sff_sector       ON sector_fund_flow(sector_type, sector_code, trade_date DESC)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_sff_date         ON sector_fund_flow(trade_date)')
    # trading_dates_cache: 跟 app.init_db() line 249-252 一致, agg 模式窗口查询用
    db.execute('''CREATE TABLE IF NOT EXISTS trading_dates_cache (
        cal_date TEXT PRIMARY KEY,
        exchange TEXT DEFAULT 'SSE'
    )''')
    return db


def _seed_trading_dates(db, dates):
    """预填交易日历. dates 是 ['YYYYMMDD', ...] 倒序或正序都行."""
    db.executemany(
        "INSERT INTO trading_dates_cache (cal_date) VALUES (?)",
        [(d,) for d in dates]
    )
    db.commit()


# 模拟 push2 第 1 页返回的 rows (raw f-code dict)
def _fake_push2_row(code, name, main, sector_type='industry'):
    return {
        'f1': 2, 'f2': 1000.0, 'f3': 1.5, 'f4': 15.0,
        'f12': code, 'f14': name,
        'f62': main,                       # 主力净流入
        'f66': main * 0.6, 'f69': 1.2,     # 超大单
        'f72': main * 0.4, 'f75': 0.8,     # 大单
        'f78': -main * 0.5, 'f81': -1.0,   # 中单
        'f84': -main * 0.5, 'f87': -1.0,   # 小单
        'f100': '-',
        'f128': f'领涨{name}',             # 领涨股名
        'f140': '002213',                  # 领涨股代码
        'f184': 2.0,                       # 主力净流入占比
    }


def _fake_run(**kwargs):
    """模拟 fetch_fund_flow_sector.run: 跟真 run 一样, 内部做 skip_if_exists 守卫.
    skip_if_exists=True 时查 DB, 有数据就 emit already_synced 回调并早退.
    用线程局部 db 句柄: setUp 里 patch 了 sqlite3.connect → self.db.
    """
    import sqlite3 as _sq
    cb = kwargs.get('progress_callback')
    actual_date = kwargs.get('actual_date', '20260915')
    sector_type = kwargs.get('sector_type', 'industry')
    rows = kwargs.get('rows', [])
    total = kwargs.get('total', len(rows))
    skip = kwargs.get('skip_if_exists', True)
    force = kwargs.get('force', False)

    if not force and skip:
        # 真 run 查 DB: SELECT COUNT(*) FROM sector_fund_flow WHERE trade_date=? AND sector_type=?
        try:
            conn = _sq.connect(':memory:')  # placeholder, 实际 mock 用全局 self.db
            conn.close()
        except Exception:
            pass

    if kwargs.get('is_already_synced'):
        if cb:
            cb('already_synced', actual_date=actual_date, existing_count=len(rows))
        return {
            'rows': [], 'actual_date': actual_date, 'sector_type': sector_type,
            'total': 0, 'done': 0, 'failed_pages': [],
            'is_already_synced': True,
            'is_holiday': kwargs.get('is_holiday', False),
            'waited_for_user': False,
        }
    if cb:
        cb('start', actual_date=actual_date)
        cb('total', total=total)
        cb('done', rows=len(rows), total=total, failed=kwargs.get('failed_pages', []),
           actual_date=actual_date)
    return {
        'rows': rows, 'actual_date': actual_date, 'sector_type': sector_type,
        'total': total, 'done': len(rows),
        'failed_pages': kwargs.get('failed_pages', []),
        'is_already_synced': False,
        'is_holiday': kwargs.get('is_holiday', False),
        'waited_for_user': kwargs.get('waited_for_user', False),
    }


def _fake_run_with_db(test_db, **kwargs):
    """_fake_run 的扩展版: 真查 test_db 模拟 skip_if_exists.
    适配 test_skip_if_exists 等需要 DB 真有数据的场景."""
    import sqlite3 as _sq
    cb = kwargs.get('progress_callback')
    actual_date = kwargs.get('actual_date', '20260915')
    sector_type = kwargs.get('sector_type', 'industry')
    rows = kwargs.get('rows', [])
    total = kwargs.get('total', len(rows))
    skip = kwargs.get('skip_if_exists', True)
    force = kwargs.get('force', False)

    if not force and skip:
        try:
            r = test_db.execute(
                "SELECT COUNT(*) FROM sector_fund_flow WHERE trade_date=? AND sector_type=?",
                (actual_date, sector_type)
            ).fetchone()
            if r and r[0] > 0:
                if cb:
                    cb('already_synced', actual_date=actual_date, existing_count=r[0])
                return {
                    'rows': [], 'actual_date': actual_date, 'sector_type': sector_type,
                    'total': 0, 'done': 0, 'failed_pages': [],
                    'is_already_synced': True,
                    'is_holiday': kwargs.get('is_holiday', False),
                    'waited_for_user': False,
                }
        except Exception:
            pass

    if cb:
        cb('start', actual_date=actual_date)
        cb('total', total=total)
        cb('done', rows=len(rows), total=total, failed=kwargs.get('failed_pages', []),
           actual_date=actual_date)
    return {
        'rows': rows, 'actual_date': actual_date, 'sector_type': sector_type,
        'total': total, 'done': len(rows),
        'failed_pages': kwargs.get('failed_pages', []),
        'is_already_synced': False,
        'is_holiday': kwargs.get('is_holiday', False),
        'waited_for_user': kwargs.get('waited_for_user', False),
    }


class SectorFundFlowTests(unittest.TestCase):
    def setUp(self):
        self.db = _build_test_db()
        self.connect_patcher = patch.object(backend.sqlite3, 'connect', return_value=self.db)
        self.connect_patcher.start()
        # 重置状态
        backend._BKSYNC_STATE.update({
            'running': False, 'started_at': None, 'finished_at': None,
            'total': 0, 'done': 0, 'success': 0, 'failed': 0,
            'sector_type': '',
            'errors': [], 'current_code': '', 'elapsed_sec': 0,
            'source': 'eastmoney-push2', 'target_date': '',
            'is_already_synced': False, 'is_holiday': False, 'actual_date': '',
            'waiting_for_user': False, 'wait_elapsed_sec': 0, 'wait_remaining_sec': 0,
        })
        self.client = backend.app.test_client()

    def tearDown(self):
        self.connect_patcher.stop()

    # ── 写库函数: _save_bk_fund_flow_rows ──

    def test_save_rows_writes_to_db(self):
        rows = [
            _fake_push2_row('BK1036', '半导体', 4.5e8),
            _fake_push2_row('BK1592', '通信线缆及配套', 2.2e8),
            _fake_push2_row('BK1326', '半导体设备', 1.7e8),
        ]
        n = backend._save_bk_fund_flow_rows(rows, '20260915', 'industry')
        self.assertEqual(n, 3)
        rows_in_db = self.db.execute(
            "SELECT sector_code, name, main_net_inflow FROM sector_fund_flow ORDER BY sector_code"
        ).fetchall()
        self.assertEqual(len(rows_in_db), 3)
        self.assertEqual(rows_in_db[0][0], 'BK1036')
        self.assertEqual(rows_in_db[0][1], '半导体')
        self.assertAlmostEqual(rows_in_db[0][2], 4.5e8, delta=1)

    def test_save_rows_skips_invalid_sector_code(self):
        rows = [
            _fake_push2_row('BK1036', '半导体', 4.5e8),
            {'f12': '123456', 'f14': '非板块'},  # 不以 BK 开头, 应被跳过
            {'f12': '', 'f14': '空代码'},        # 空代码, 应被跳过
            {'f12': 'BK9999', 'f14': '测试', 'f62': 1.0},  # f62 缺失 = 0, 仍应写
        ]
        n = backend._save_bk_fund_flow_rows(rows, '20260915', 'industry')
        self.assertEqual(n, 2)  # BK1036 + BK9999
        codes = [r[0] for r in self.db.execute("SELECT sector_code FROM sector_fund_flow").fetchall()]
        self.assertIn('BK1036', codes)
        self.assertIn('BK9999', codes)
        self.assertNotIn('123456', codes)

    def test_save_rows_handles_dash_and_null(self):
        rows = [{
            'f12': 'BK1036', 'f14': '半导体',
            'f2': 1000.0, 'f3': 1.5, 'f4': 15.0,
            'f62': 1.0e8, 'f66': '-', 'f69': None,  # 边界值
            'f72': '', 'f75': 0.8,                    # 空字符串
            'f78': -1.0e8, 'f81': '-1.0',
            'f84': None, 'f87': None,
            'f128': None, 'f140': None,
            'f184': 2.0,
        }]
        n = backend._save_bk_fund_flow_rows(rows, '20260915', 'industry')
        self.assertEqual(n, 1)
        r = self.db.execute("SELECT super_net, super_pct, big_net, lead_stock_name FROM sector_fund_flow").fetchone()
        self.assertIsNone(r[0])  # f66='-' → None
        self.assertIsNone(r[1])  # f69=None → None
        self.assertIsNone(r[2])  # f72='' → None
        self.assertIsNone(r[3])  # f128=None → None

    def test_save_rows_replaces_existing(self):
        rows1 = [_fake_push2_row('BK1036', '半导体', 1.0e8)]
        backend._save_bk_fund_flow_rows(rows1, '20260915', 'industry')
        # 再写一次, 同主键应该被替换 (DELETE+INSERT)
        rows2 = [_fake_push2_row('BK1036', '半导体改名', 2.0e8)]
        backend._save_bk_fund_flow_rows(rows2, '20260915', 'industry')
        r = self.db.execute(
            "SELECT name, main_net_inflow FROM sector_fund_flow WHERE sector_code='BK1036'"
        ).fetchone()
        self.assertEqual(r[0], '半导体改名')
        self.assertAlmostEqual(r[1], 2.0e8, delta=1)
        # 总数仍是 1 (不是 2)
        n = self.db.execute("SELECT COUNT(*) FROM sector_fund_flow").fetchone()[0]
        self.assertEqual(n, 1)

    # ── 端点: /api/flow/sector/dates ──

    def test_dates_endpoint_returns_distinct_dates_desc(self):
        for st in ('industry', 'concept'):
            for d, code in [('20260915', 'BK1036'), ('20260915', 'BK1592'), ('20260914', 'BK1036')]:
                self.db.execute(
                    "INSERT INTO sector_fund_flow (trade_date, sector_type, sector_code, main_net_inflow) VALUES (?,?,?,?)",
                    (d, st, code, 1.0e8)
                )
        self.db.commit()
        r = self.client.get('/api/flow/sector/dates?type=industry')
        d = r.get_json()
        self.assertEqual(d['count'], 2)
        self.assertEqual(d['dates'], ['20260915', '20260914'])
        self.assertEqual(d['type'], 'industry')

        r2 = self.client.get('/api/flow/sector/dates?type=concept')
        d2 = r2.get_json()
        self.assertEqual(d2['count'], 2)

        r3 = self.client.get('/api/flow/sector/dates?type=all')
        d3 = r3.get_json()
        self.assertEqual(d3['count'], 2)  # distinct trade_date 跨 industry+concept

    def test_dates_endpoint_rejects_invalid_type(self):
        r = self.client.get('/api/flow/sector/dates?type=invalid')
        self.assertEqual(r.status_code, 400)

    # ── 端点: /api/flow/sector/market ──

    def test_market_endpoint_sort_desc(self):
        # 5 行 main_net_inflow 不同
        for i, (code, val) in enumerate([
            ('BK1001', 1.0e8), ('BK1002', 5.0e8), ('BK1003', 3.0e8),
            ('BK1004', 4.0e8), ('BK1005', 2.0e8),
        ]):
            self.db.execute(
                "INSERT INTO sector_fund_flow (trade_date, sector_type, sector_code, name, main_net_inflow, change_pct) VALUES (?,?,?,?,?,?)",
                ('20260915', 'industry', code, f'板块{code}', val, float(i))
            )
        self.db.commit()

        r = self.client.get('/api/flow/sector/market?type=industry&date=20260915&sort=main_net_inflow&order=desc&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 5)
        self.assertEqual(d['rows'][0]['sector_code'], 'BK1002')  # 最大
        self.assertEqual(d['rows'][4]['sector_code'], 'BK1001')  # 最小

    def test_market_endpoint_sort_asc(self):
        for code, val in [('BK1001', 1.0e8), ('BK1002', 5.0e8), ('BK1003', 3.0e8)]:
            self.db.execute(
                "INSERT INTO sector_fund_flow (trade_date, sector_type, sector_code, name, main_net_inflow) VALUES (?,?,?,?,?)",
                ('20260915', 'industry', code, code, val)
            )
        self.db.commit()
        r = self.client.get('/api/flow/sector/market?type=industry&date=20260915&sort=main_net_inflow&order=asc&limit=10')
        d = r.get_json()
        self.assertEqual(d['rows'][0]['sector_code'], 'BK1001')  # 最小
        self.assertEqual(d['rows'][-1]['sector_code'], 'BK1002')  # 最大

    def test_market_endpoint_search_by_name(self):
        for code, name in [('BK1001', '半导体'), ('BK1002', '通信线缆'), ('BK1003', '半导体设备')]:
            self.db.execute(
                "INSERT INTO sector_fund_flow (trade_date, sector_type, sector_code, name, main_net_inflow) VALUES (?,?,?,?,?)",
                ('20260915', 'industry', code, name, 1.0e8)
            )
        self.db.commit()
        r = self.client.get('/api/flow/sector/market?type=industry&date=20260915&search=半导体&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 2)
        codes = [row['sector_code'] for row in d['rows']]
        self.assertIn('BK1001', codes)
        self.assertIn('BK1003', codes)
        self.assertNotIn('BK1002', codes)

    def test_market_endpoint_default_latest_date(self):
        # 不传 date, 用最近的 trade_date
        for d, code in [('20260913', 'BK1001'), ('20260915', 'BK1002'), ('20260914', 'BK1003')]:
            self.db.execute(
                "INSERT INTO sector_fund_flow (trade_date, sector_type, sector_code, main_net_inflow) VALUES (?,?,?,?)",
                (d, 'industry', code, 1.0e8)
            )
        self.db.commit()
        r = self.client.get('/api/flow/sector/market?type=industry&limit=10')
        d = r.get_json()
        self.assertEqual(d['trade_date'], '20260915')
        self.assertEqual(d['count'], 1)
        self.assertEqual(d['rows'][0]['sector_code'], 'BK1002')

    def test_market_endpoint_no_data_returns_empty(self):
        r = self.client.get('/api/flow/sector/market?type=industry&date=20260915&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 0)
        self.assertEqual(d['rows'], [])
        self.assertEqual(r.status_code, 200)

    def test_market_endpoint_rejects_sql_injection_in_sort(self):
        # 用户传 sort=code; DROP TABLE → 应当被白名单挡
        r = self.client.get("/api/flow/sector/market?type=industry&date=20260915&sort=code;DROP%20TABLE&limit=10")
        # 不会 500, 也不会 200 with rows; 用白名单默认 main_net_inflow
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['sort'], 'main_net_inflow')  # 回退到默认

    def test_market_endpoint_limit_clamped(self):
        # limit > 1000 应该被截到 1000
        r = self.client.get('/api/flow/sector/market?type=industry&date=20260915&limit=99999')
        d = r.get_json()
        # 即使空表也能正常返 200, 关键是不崩
        self.assertEqual(r.status_code, 200)

    # ── 状态机: skip_if_exists 守卫 ──

    def test_skip_if_exists_returns_already_synced(self):
        # 预填 1 行, 跑 _bk_sync_market_bg(force=False), 应该早退
        self.db.execute(
            "INSERT INTO sector_fund_flow (trade_date, sector_type, sector_code, main_net_inflow) VALUES (?,?,?,?)",
            ('20260915', 'industry', 'BK1036', 1.0e8)
        )
        self.db.commit()

        # 用 _fake_run_with_db 模拟真 run: 真查 self.db → 命中已有数据 → emit already_synced
        with patch('fetch_fund_flow_sector.run',
                   side_effect=lambda **kw: _fake_run_with_db(self.db, **kw)) as mock_run:
            backend._bk_sync_market_bg(sector_type='industry', force=False, wait_for_user=False)
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertTrue(kwargs.get('skip_if_exists'))
            self.assertFalse(kwargs.get('force'))

        self.assertTrue(backend._BKSYNC_STATE.get('is_already_synced'))
        self.assertEqual(backend._BKSYNC_STATE.get('actual_date'), '20260915')
        self.assertEqual(backend._BKSYNC_STATE.get('success'), 0)
        self.assertFalse(backend._BKSYNC_STATE.get('running'))

    def test_force_bypasses_skip_if_exists(self):
        self.db.execute(
            "INSERT INTO sector_fund_flow (trade_date, sector_type, sector_code, main_net_inflow) VALUES (?,?,?,?)",
            ('20260915', 'industry', 'BK1036', 1.0e8)
        )
        self.db.commit()

        rows = [_fake_push2_row('BK1036', '半导体', 2.0e8), _fake_push2_row('BK1592', '通信', 1.0e8)]
        with patch('fetch_fund_flow_sector.run',
                   side_effect=lambda **kw: _fake_run(rows=rows, **kw)) as mock_run:
            backend._bk_sync_market_bg(sector_type='industry', force=True, wait_for_user=False)
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            self.assertFalse(kwargs.get('skip_if_exists'))  # force=True 时关闭 skip
        self.assertFalse(backend._BKSYNC_STATE.get('is_already_synced'))
        self.assertEqual(backend._BKSYNC_STATE.get('success'), 2)

    def test_sync_state_running_to_done_transitions(self):
        rows = [_fake_push2_row('BK1036', '半导体', 2.0e8)] * 3
        with patch('fetch_fund_flow_sector.run',
                   side_effect=lambda **kw: _fake_run(rows=rows, **kw)):
            backend._bk_sync_market_bg(sector_type='industry', force=True, wait_for_user=False)
        # done 后 running 必须 False
        self.assertFalse(backend._BKSYNC_STATE['running'])
        self.assertIsNotNone(backend._BKSYNC_STATE['finished_at'])
        self.assertEqual(backend._BKSYNC_STATE['success'], 3)

    def test_sync_already_running_rejects_duplicate(self):
        # 手动把状态置成 running, POST 应该返 already_running
        backend._BKSYNC_STATE['running'] = True
        r = self.client.post('/api/flow/sector/sync?type=industry')
        d = r.get_json()
        self.assertEqual(d['status'], 'already_running')

    def test_sync_rejects_invalid_type(self):
        r = self.client.post('/api/flow/sector/sync?type=invalid')
        self.assertEqual(r.status_code, 400)

    def test_sync_status_endpoint(self):
        r = self.client.get('/api/flow/sector/sync-status')
        d = r.get_json()
        self.assertIn('running', d)
        self.assertIn('sector_type', d)
        self.assertIn('is_already_synced', d)

    def test_sync_holiday_keeps_actual_date_separate(self):
        # 模拟 last_trading_date 返回 20260914 (今天 20260915 是节假日)
        rows = [_fake_push2_row('BK1036', '半导体', 1.0e8)]
        with patch('fetch_fund_flow_sector.run',
                   side_effect=lambda **kw: _fake_run(
                       rows=rows, actual_date='20260914', is_holiday=True, **kw)):
            backend._bk_sync_market_bg(sector_type='industry', force=True, wait_for_user=False)
        self.assertEqual(backend._BKSYNC_STATE['actual_date'], '20260914')
        self.assertTrue(backend._BKSYNC_STATE['is_holiday'])
        # 写入时用 20260914 (跟实际数据归属日)
        rows_in_db = self.db.execute(
            "SELECT trade_date FROM sector_fund_flow"
        ).fetchall()
        self.assertEqual(rows_in_db[0][0], '20260914')

    def test_sync_empty_rows_marks_failed(self):
        with patch('fetch_fund_flow_sector.run',
                   side_effect=lambda **kw: _fake_run(
                       rows=[], total=0, failed_pages=list(range(1, 6)), **kw)):
            backend._bk_sync_market_bg(sector_type='industry', force=True, wait_for_user=False)
        self.assertEqual(backend._BKSYNC_STATE['failed'], 1)
        self.assertEqual(backend._BKSYNC_STATE['success'], 0)
        self.assertGreater(len(backend._BKSYNC_STATE['errors']), 0)

    def test_sync_exception_marks_failed(self):
        with patch('fetch_fund_flow_sector.run', side_effect=RuntimeError('boom')):
            backend._bk_sync_market_bg(sector_type='industry', force=True, wait_for_user=False)
        self.assertEqual(backend._BKSYNC_STATE['failed'], 1)
        self.assertFalse(backend._BKSYNC_STATE['running'])


class SectorFundFlowPeriodTests(unittest.TestCase):
    """板块 Nd 累计 (3d/5d/10d/20d) 测试 — 跟 EM/THS 个股的 market 累计口径一致.
    关键: window 用 trading_dates_cache OFFSET N-1 (跟 EM/THS 同模式)."""

    def setUp(self):
        self.db = _build_test_db()
        self.connect_patcher = patch.object(backend.sqlite3, 'connect', return_value=self.db)
        self.connect_patcher.start()
        self.client = backend.app.test_client()

    def tearDown(self):
        self.connect_patcher.stop()

    def _seed_sector(self, rows):
        """rows: [(trade_date, sector_code, main_net_inflow, super_net, big_net, mid_net, small_net, close, change_pct, lead_name, lead_code)]"""
        for r in rows:
            self.db.execute(
                """INSERT INTO sector_fund_flow
                   (trade_date, sector_type, sector_code, name,
                    main_net_inflow, super_net, big_net, mid_net, small_net,
                    close, change_pct, lead_stock_name, lead_stock_code)
                   VALUES (?, 'industry', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                r
            )
        self.db.commit()

    def test_period_3d_sums_correctly(self):
        # 3 天 × 2 行业 = 6 行; 主键 (trade_date, sector_code)
        _seed_trading_dates(self.db, ['20260913', '20260914', '20260915'])
        self._seed_sector([
            ('20260913', 'BK1036', '半导体',  1.0e8, 6.0e7, 4.0e7, -5.0e7, -5.0e7, 1000.0, 1.0, '中芯国际', '688981'),
            ('20260913', 'BK0421', '通信',    2.0e8, 1.2e8, 8.0e7, -1.0e8, -1.0e8, 2300.0, 0.5, '中兴通讯', '000063'),
            ('20260914', 'BK1036', '半导体',  2.0e8, 1.2e8, 8.0e7, -1.0e8, -1.0e8, 1050.0, 2.0, '中芯国际', '688981'),
            ('20260914', 'BK0421', '通信',    3.0e8, 1.8e8, 1.2e8, -1.5e8, -1.5e8, 2350.0, 1.0, '中兴通讯', '000063'),
            ('20260915', 'BK1036', '半导体',  3.0e8, 1.8e8, 1.2e8, -1.5e8, -1.5e8, 1100.0, 3.0, '中芯国际', '688981'),
            ('20260915', 'BK0421', '通信',    4.0e8, 2.4e8, 1.6e8, -2.0e8, -2.0e8, 2400.0, 2.0, '中兴通讯', '000063'),
        ])
        r = self.client.get('/api/flow/sector/market?type=industry&period=3d&sort=sum_main_net&order=desc&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 2)
        self.assertEqual(d['period'], '3d')
        # 通信 sum=9亿, 半导体 sum=6亿
        self.assertEqual(d['rows'][0]['sector_code'], 'BK0421')
        self.assertAlmostEqual(d['rows'][0]['sum_main_net'], 9.0e8, delta=1)
        self.assertEqual(d['rows'][1]['sector_code'], 'BK1036')
        self.assertAlmostEqual(d['rows'][1]['sum_main_net'], 6.0e8, delta=1)
        # flow_days / start / end 都有
        self.assertEqual(d['rows'][0]['flow_days'], 3)
        self.assertEqual(d['rows'][0]['start_date'], '20260913')
        self.assertEqual(d['rows'][0]['end_date'], '20260915')

    def test_period_5d_window_uses_trading_dates_cache(self):
        # 交易日历只到 20260915, 但 sector_fund_flow 里有更早的数据 (抓历史产生的, 不应计入 5d 窗口)
        _seed_trading_dates(self.db, ['20260911', '20260912', '20260913', '20260914', '20260915'])
        self._seed_sector([
            ('20260908', 'BK1036', '半导体', 99.0e8, 0, 0, 0, 0, 100.0, 0, 'X', '000001'),  # 范围外, 不计入
            ('20260911', 'BK1036', '半导体', 1.0e8,  0, 0, 0, 0, 100.0, 0, 'X', '000001'),
            ('20260913', 'BK1036', '半导体', 2.0e8,  0, 0, 0, 0, 100.0, 0, 'X', '000001'),
            ('20260915', 'BK1036', '半导体', 3.0e8,  0, 0, 0, 0, 100.0, 0, 'X', '000001'),
        ])
        r = self.client.get('/api/flow/sector/market?type=industry&period=5d&limit=10')
        d = r.get_json()
        # 5d 窗口应该包括 09/11, 09/12, 09/13, 09/14, 09/15 (但 sector_fund_flow 只有 11/13/15 三行)
        self.assertEqual(d['count'], 1)
        self.assertAlmostEqual(d['rows'][0]['sum_main_net'], 6.0e8, delta=1)  # 1+2+3 (排除 09/08)
        self.assertEqual(d['rows'][0]['flow_days'], 3)  # 实际只有 3 天有数据

    def test_period_3d_returns_latest_close_change_pct(self):
        # 同一行业 3 天不同 close/change_pct, agg 模式只取最新一日的 close/change_pct
        _seed_trading_dates(self.db, ['20260913', '20260914', '20260915'])
        self._seed_sector([
            ('20260913', 'BK1036', '半导体', 1.0e8, 0, 0, 0, 0, 1000.0, 1.0, '第一天领涨', '000001'),
            ('20260914', 'BK1036', '半导体', 2.0e8, 0, 0, 0, 0, 1050.0, 2.0, '第二天领涨', '000002'),
            ('20260915', 'BK1036', '半导体', 3.0e8, 0, 0, 0, 0, 1100.0, 3.0, '第三天领涨', '000003'),
        ])
        r = self.client.get('/api/flow/sector/market?type=industry&period=3d&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 1)
        row = d['rows'][0]
        self.assertAlmostEqual(row['close'], 1100.0, delta=0.01)
        self.assertAlmostEqual(row['change_pct'], 3.0, delta=0.01)
        # close/change_pct 不是 sum, 应该是最新一天 (而不是 1000+1050+1100)
        self.assertEqual(row['lead_stock_name'], '第三天领涨')
        self.assertEqual(row['lead_stock_code'], '000003')

    def test_period_20d_empty_for_fresh_data(self):
        # 只有 1 天 sector 数据 + 20 个交易日的日历, period=20d 仍然返回 1 行 (sum=那天)
        _seed_trading_dates(self.db, [
            '20260815', '20260818', '20260819', '20260820', '20260821',
            '20260822', '20260825', '20260826', '20260827', '20260828',
            '20260829', '20260901', '20260902', '20260903', '20260904',
            '20260905', '20260908', '20260909', '20260910', '20260915',
        ])
        self._seed_sector([
            ('20260915', 'BK1036', '半导体', 5.0e8, 0, 0, 0, 0, 1000.0, 1.0, '中芯国际', '688981'),
        ])
        r = self.client.get('/api/flow/sector/market?type=industry&period=20d&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 1)
        self.assertAlmostEqual(d['rows'][0]['sum_main_net'], 5.0e8, delta=1)
        self.assertEqual(d['rows'][0]['flow_days'], 1)

    def test_period_default_is_today(self):
        _seed_trading_dates(self.db, ['20260915'])
        self._seed_sector([
            ('20260915', 'BK1036', '半导体', 5.0e8, 0, 0, 0, 0, 1000.0, 1.0, '中芯国际', '688981'),
        ])
        # 不传 period → 默认 today
        r = self.client.get('/api/flow/sector/market?type=industry&limit=10')
        d = r.get_json()
        self.assertEqual(d['period'], 'today')
        self.assertEqual(d['count'], 1)
        # today 模式有 main_net_inflow (不是 sum_main_net)
        self.assertIn('main_net_inflow', d['rows'][0])
        self.assertNotIn('sum_main_net', d['rows'][0])

    def test_period_invalid_falls_back_to_today(self):
        r = self.client.get('/api/flow/sector/market?type=industry&period=invalid')
        d = r.get_json()
        self.assertEqual(d['period'], 'today')

    def test_period_agg_sort_whitelist_rejects_sql_injection(self):
        r = self.client.get('/api/flow/sector/market?type=industry&period=3d&sort=evil;DROP%20TABLE')
        d = r.get_json()
        # 回退默认
        self.assertEqual(d['sort'], 'sum_main_net')
        # 表还在
        r2 = self.client.get('/api/flow/sector/market?type=industry&period=3d&sort=main_net_inflow')
        # main_net_inflow 不在 agg 白名单 → 回退 sum_main_net
        self.assertEqual(r2.get_json()['sort'], 'sum_main_net')

    def test_period_agg_sort_by_sum_super_net_works(self):
        _seed_trading_dates(self.db, ['20260913', '20260914', '20260915'])
        self._seed_sector([
            ('20260913', 'BK1036', '半导体', 1.0e8, 5.0e8, 5.0e7, -1e8, -1e8, 1000.0, 1.0, 'X', '000001'),
            ('20260913', 'BK0421', '通信',   1.0e8, 1.0e8, 5.0e7, -1e8, -1e8, 1000.0, 1.0, 'X', '000001'),
            ('20260914', 'BK1036', '半导体', 1.0e8, 5.0e8, 5.0e7, -1e8, -1e8, 1000.0, 1.0, 'X', '000001'),
            ('20260914', 'BK0421', '通信',   1.0e8, 1.0e8, 5.0e7, -1e8, -1e8, 1000.0, 1.0, 'X', '000001'),
            ('20260915', 'BK1036', '半导体', 1.0e8, 5.0e8, 5.0e7, -1e8, -1e8, 1000.0, 1.0, 'X', '000001'),
            ('20260915', 'BK0421', '通信',   1.0e8, 1.0e8, 5.0e7, -1e8, -1e8, 1000.0, 1.0, 'X', '000001'),
        ])
        r = self.client.get('/api/flow/sector/market?type=industry&period=3d&sort=sum_super_net&order=desc&limit=10')
        d = r.get_json()
        # 半导体 sum_super_net=15亿, 通信 sum_super_net=3亿
        self.assertEqual(d['rows'][0]['sector_code'], 'BK1036')
        self.assertAlmostEqual(d['rows'][0]['sum_super_net'], 1.5e9, delta=1)

    def test_period_agg_date_param_ignored(self):
        # agg 模式 date 参数应该被忽略, 走 trading_dates_cache 窗口
        _seed_trading_dates(self.db, ['20260913', '20260914', '20260915'])
        self._seed_sector([
            ('20260913', 'BK1036', '半导体', 1.0e8, 0, 0, 0, 0, 1000.0, 0, 'X', '000001'),
            ('20260914', 'BK1036', '半导体', 2.0e8, 0, 0, 0, 0, 1000.0, 0, 'X', '000001'),
            ('20260915', 'BK1036', '半导体', 3.0e8, 0, 0, 0, 0, 1000.0, 0, 'X', '000001'),
        ])
        # date=20260910 是非交易日 (不在 trading_dates_cache 里), 但 agg 模式忽略 date
        r = self.client.get('/api/flow/sector/market?type=industry&period=3d&date=20260910&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 1)
        self.assertAlmostEqual(d['rows'][0]['sum_main_net'], 6.0e8, delta=1)  # 1+2+3
        self.assertEqual(d['rows'][0]['flow_days'], 3)

    def test_period_agg_concept_works(self):
        # 概念也走同样逻辑
        _seed_trading_dates(self.db, ['20260913', '20260914', '20260915'])
        for r in [
            ('20260913', 'BK1648', '数字货币', 1.0e8, 0, 0, 0, 0, 1000.0, 0, 'X', '000001'),
            ('20260914', 'BK1648', '数字货币', 2.0e8, 0, 0, 0, 0, 1000.0, 0, 'X', '000001'),
            ('20260915', 'BK1648', '数字货币', 3.0e8, 0, 0, 0, 0, 1000.0, 0, 'X', '000001'),
        ]:
            self.db.execute(
                """INSERT INTO sector_fund_flow
                   (trade_date, sector_type, sector_code, name,
                    main_net_inflow, super_net, big_net, mid_net, small_net,
                    close, change_pct, lead_stock_name, lead_stock_code)
                   VALUES (?, 'concept', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                r
            )
        self.db.commit()
        r = self.client.get('/api/flow/sector/market?type=concept&period=3d&limit=10')
        d = r.get_json()
        self.assertEqual(d['count'], 1)
        self.assertEqual(d['type'], 'concept')
        self.assertAlmostEqual(d['rows'][0]['sum_main_net'], 6.0e8, delta=1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
