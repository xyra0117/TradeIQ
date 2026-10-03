"""优选核心规则与累计涨幅：仅内存数据，不触碰实际数据库。"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app as backend


class ConditionalPreferredTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
            CREATE TABLE trading_dates_cache (cal_date TEXT);
            CREATE TABLE fund_flow (ts_code TEXT, trade_date TEXT, source TEXT,
                main_net_inflow REAL, small_net REAL, small_pct REAL);
            CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, open REAL,
                high REAL, close REAL, change REAL, amount REAL);
            CREATE TABLE overview (date TEXT, upCount INTEGER, downCount INTEGER,
                flatCount INTEGER, limitUp INTEGER, limitDown INTEGER);
        ''')
        for day in ('20260105', '20260106', '20260107', '20260108', '20260109'):
            self.db.execute('INSERT INTO trading_dates_cache VALUES (?)', (day,))
            self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?,?,?)',
                            ('A.SZ', day, 'eastmoney-push2', 5000, -1000, -1))
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?)',
                            ('A.SZ', day, 10, 10, 10, 0, 100))
        self.row = {'ts_code': 'A.SZ', 'close': 10, 'volume_ratio_pct': 75}

    def tearDown(self):
        self.db.close()

    def test_strength_units_boundary_and_missing_ma60(self):
        hits, stats = backend._filter_conditional_preferred([self.row], '20260109', self.db)
        self.assertEqual(stats['matched'], 1)
        self.assertEqual(hits[0]['preferred_strength5'], 5)
        self.assertEqual(hits[0]['preferred_priority'], 1)
        self.assertIsNone(hits[0]['preferred_ma60'])
        self.assertNotIn('preferred_priority', self.row)

    def test_future_money_does_not_change_selection(self):
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?,?,?)',
                        ('A.SZ', '20260112', 'eastmoney-push2', 1e12, -1e12, -99))
        hits, _ = backend._filter_conditional_preferred([self.row], '20260109', self.db)
        self.assertEqual(hits[0]['preferred_strength5'], 5)

    def test_ma60_bonus_and_priority_sorting(self):
        from datetime import datetime, timedelta
        for index in range(55):
            day = (datetime(2025, 10, 1) + timedelta(days=index)).strftime('%Y%m%d')
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?)',
                            ('A.SZ', day, 10, 10, 10, 0, 100))
        self.db.execute("INSERT INTO fund_flow SELECT 'B.SZ',trade_date,source,10000,small_net,small_pct FROM fund_flow")
        self.db.execute("INSERT INTO stock_daily SELECT 'B.SZ',trade_date,open,high,close,change,amount FROM stock_daily")
        other = dict(self.row, ts_code='B.SZ', volume_ratio_pct=60)
        hits, _ = backend._filter_conditional_preferred([other, self.row], '20260109', self.db)
        self.assertEqual([r['ts_code'] for r in hits], ['A.SZ', 'B.SZ'])
        self.assertEqual(hits[0]['preferred_priority'], 2)
        self.assertEqual(hits[0]['preferred_ma60'], 10)

    def test_volume_bonus_uses_unrounded_values(self):
        row = dict(self.row, volume_ratio_pct=70, volume=69.999, prev_volume=100)
        hits, _ = backend._filter_conditional_preferred([row], '20260109', self.db)
        self.assertFalse(hits[0]['preferred_mild_shrink'])

    def test_missing_trading_day_is_not_replaced(self):
        self.db.execute("DELETE FROM fund_flow WHERE trade_date='20260107'")
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?,?,?)',
                        ('A.SZ', '20260102', 'eastmoney-push2', 5000, -1000, -1))
        hits, stats = backend._filter_conditional_preferred([self.row], '20260109', self.db)
        self.assertEqual(hits, [])
        self.assertEqual(stats['missing_flow'], 1)

    def test_small_zero_and_weak_main_excluded(self):
        self.db.execute("UPDATE fund_flow SET small_net=0 WHERE trade_date='20260109'")
        self.assertEqual(backend._filter_conditional_preferred([self.row], '20260109', self.db)[1]['small_not_outflow'], 1)
        self.db.execute('UPDATE fund_flow SET small_net=-1, main_net_inflow=4999')
        self.assertEqual(backend._filter_conditional_preferred([self.row], '20260109', self.db)[1]['weak_main'], 1)

    def test_bad_amount_not_zero_filled(self):
        self.db.execute("UPDATE stock_daily SET amount=NULL WHERE trade_date='20260108'")
        self.assertEqual(backend._filter_conditional_preferred([self.row], '20260109', self.db)[1]['missing_flow'], 1)

    def test_market_overview_requires_exact_date(self):
        self.db.execute('INSERT INTO overview VALUES (?,?,?,?,?,?)',
                        ('20260108', 3000, 2000, 100, 80, 20))
        missing = backend._preferred_market_overview('20260109', self.db)
        self.assertFalse(missing['available'])
        exact = backend._preferred_market_overview('20260108', self.db)
        self.assertTrue(exact['available'])
        self.assertEqual(exact['totalCount'], 5100)
        self.assertEqual(exact['upCount'], 3000)

    def test_outcomes_are_additive_not_running_max_or_compound(self):
        close = 10
        for i, (day, change, high_change) in enumerate([
            ('20260112', 2, 6), ('20260113', 3, 7), ('20260114', -5, 4),
            ('20260115', 1, 2), ('20260116', 0, 3),
        ]):
            self.db.execute('INSERT INTO trading_dates_cache VALUES (?)', (day,))
            next_close = close * (1 + change / 100)
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?)',
                            ('A.SZ', day, close, close * (1 + high_change / 100), next_close, change, 100))
            close = next_close
        backend._attach_preferred_outcomes([self.row], '20260109', self.db)
        self.assertEqual([r['high_sum_pct'] for r in self.row['preferred_outcomes']], [6, 9, 9, 2, 4])
        self.assertEqual([r['close_sum_pct'] for r in self.row['preferred_outcomes']], [2, 5, 0, 1, 1])
        self.db.execute("DELETE FROM stock_daily WHERE trade_date='20260113'")
        backend._attach_preferred_outcomes([self.row], '20260109', self.db)
        self.assertEqual(self.row['preferred_outcomes'][0]['high_sum_pct'], 6)
        self.assertTrue(all(r['high_sum_pct'] is None for r in self.row['preferred_outcomes'][1:]))

    def test_waiting_api_skips_enrichment(self):
        with patch.object(backend, 'api_conditional_picks_new') as base, \
                patch.object(backend, '_filter_conditional_preferred') as preferred:
            base.return_value.get_json.return_value = {'daily_ready': False, 'results': [], 'date': '20260109'}
            response = backend.app.test_client().get('/api/conditional-picks/preferred')
            self.assertEqual(response.get_json()['count'], 0)
            preferred.assert_not_called()

    def test_other_source_rejected(self):
        response = backend.app.test_client().get('/api/conditional-picks/preferred?source=all')
        self.assertEqual(response.status_code, 400)

    def test_save_and_read_preferred_snapshot_roundtrip(self):
        """优选落库 → 直读 round-trip."""
        from datetime import datetime
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            tmp_path = f.name
        try:
            old_path = backend.DB_PATH
            backend.DB_PATH = tmp_path
            backend.sqlite3.connect(tmp_path).executescript('''
                CREATE TABLE conditional_picks_preferred_snapshot (
                    trade_date TEXT PRIMARY KEY, count INTEGER NOT NULL,
                    results_json TEXT NOT NULL, exclusions_json TEXT,
                    threshold_yi REAL, volume_ratio_pct REAL,
                    saved_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, close REAL, volume REAL);
            ''').close()
            hits = [{'ts_code': 'Y.SZ', 'name': 'Y', 'preferred_priority': 2,
                     'preferred_outcomes': [{'date': '20260112', 'high_sum_pct': 5}]}]
            stats = {'matched': 1, 'base_count': 3, 'missing_flow': 1, 'small_not_outflow': 1}
            date = '20260110' if datetime.now().strftime('%Y%m%d') > '20260110' else '20200101'
            backend._save_conditional_preferred_snapshot(date, hits, stats, 3, 80)
            cached = backend._read_conditional_preferred_snapshot(date, 3, 80)
            self.assertIsNotNone(cached)
            self.assertEqual(cached['count'], 1)
            self.assertEqual(cached['results'], hits)
            self.assertEqual(cached['preferred_exclusions'], stats)
            self.assertEqual(cached['strategy'], 'conditional-preferred-v1')
            self.assertEqual(cached['note'], '快照库直读')
            backend.DB_PATH = old_path
        finally:
            os.unlink(tmp_path)


class PageImagePreviewTests(unittest.TestCase):
    """条件优选 dashboard 真页面截图端点 (Playwright 路径) 测试.
    全部 mock 掉 Playwright / 落盘 IO, 只验端点契约."""

    def test_page_preview_returns_url_on_success(self):
        """成功路径: 返回 status=success + url 含 /page-previews/ + 文件落盘."""
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = backend.PAGE_PREVIEW_DIR
            backend.PAGE_PREVIEW_DIR = tmp
            try:
                with patch.object(backend, '_render_dashboard_panel_to_png') as render:
                    async def fake_render(url, path, threshold_yi=3, volume_ratio_pct=80):
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        # 写一个最小有效 PNG (1x1 红像素)
                        from PIL import Image
                        Image.new('RGB', (1, 1), 'red').save(path)
                    render.side_effect = fake_render
                    with patch.object(backend, '_conditional_new_daily_ready', return_value=(True, 'ready')):
                        with patch.object(backend, '_resolve_conditional_new_date', return_value='20260109'):
                            resp = backend.app.test_client().post(
                                '/api/conditional-picks/preferred/preview-page-image',
                                json={'date': '20260109', 'threshold_yi': 3, 'volume_ratio_pct': 80},
                            )
                            self.assertEqual(resp.status_code, 200)
                            data = resp.get_json()
                            self.assertEqual(data['status'], 'success')
                            self.assertIn('/page-previews/', data['url'])
                            self.assertIn('20260109', data['url'])
                            self.assertIn('3亿', data['url'])
                            self.assertIn('缩量80%', data['url'])
                            self.assertTrue(os.path.exists(data['path']))
                            self.assertGreater(data['duration_ms'], 0)
                            render.assert_called_once()
                            # 验证 page_url 带了 query string (确保条件优选 sub-tab 被激活)
                            call_args = render.call_args[0]
                            self.assertIn('sub=conditionalPreferred', call_args[0])
            finally:
                backend.PAGE_PREVIEW_DIR = old_dir

    def test_page_preview_blocks_when_daily_not_ready(self):
        """daily_ready=False → 400 + 不调用 Playwright."""
        with patch.object(backend, '_conditional_new_daily_ready', return_value=(False, '测试原因')):
            with patch.object(backend, '_resolve_conditional_new_date', return_value='20990101'):
                with patch.object(backend, '_render_dashboard_panel_to_png') as render:
                    resp = backend.app.test_client().post(
                        '/api/conditional-picks/preferred/preview-page-image',
                        json={'date': '20990101'},
                    )
                    self.assertEqual(resp.status_code, 400)
                    data = resp.get_json()
                    self.assertEqual(data['status'], 'error')
                    self.assertIn('当前不满足', data['note'])
                    self.assertIn('测试原因', data['note'])
                    render.assert_not_called()

    def test_page_preview_returns_500_when_render_raises(self):
        """Playwright 抛异常 → 500 + 错误信息透传."""
        with patch.object(backend, '_conditional_new_daily_ready', return_value=(True, 'ready')):
            with patch.object(backend, '_resolve_conditional_new_date', return_value='20260109'):
                with patch.object(backend, '_render_dashboard_panel_to_png', side_effect=RuntimeError('chromium 挂了')):
                    resp = backend.app.test_client().post(
                        '/api/conditional-picks/preferred/preview-page-image',
                        json={'date': '20260109'},
                    )
                    self.assertEqual(resp.status_code, 500)
                    data = resp.get_json()
                    self.assertEqual(data['status'], 'error')
                    self.assertIn('chromium 挂了', data['note'])

    def test_page_preview_param_clamping(self):
        """threshold_yi 越界 (负数/超 100) → 落回 3; volume_ratio 越界 (≤0/超 100) → 落回 80."""
        with patch.object(backend, '_conditional_new_daily_ready', return_value=(True, 'ready')):
            with patch.object(backend, '_resolve_conditional_new_date', return_value='20260109'):
                with patch.object(backend, '_render_dashboard_panel_to_png') as render:
                    async def fake(url, path, threshold_yi=3, volume_ratio_pct=80):
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        open(path, 'wb').close()
                    render.side_effect = fake
                    with tempfile.TemporaryDirectory() as tmp:
                        backend.PAGE_PREVIEW_DIR = tmp
                        try:
                            resp = backend.app.test_client().post(
                                '/api/conditional-picks/preferred/preview-page-image',
                                json={'date': '20260109', 'threshold_yi': -99, 'volume_ratio_pct': 999},
                            )
                            self.assertEqual(resp.status_code, 200)
                            data = resp.get_json()
                            # 文件名应含 3亿 (默认) 和 缩量80% (默认)
                            self.assertIn('3亿', data['url'])
                            self.assertIn('缩量80%', data['url'])
                        finally:
                            backend.PAGE_PREVIEW_DIR = old_dir if False else backend.PAGE_PREVIEW_DIR


if __name__ == '__main__':
    unittest.main()
