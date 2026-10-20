"""复权因子当日同步: 落库口径 / 接口契约 / 拉取目标面板状态. 不碰真实 TuShare 与真实库写入."""
import os
import sqlite3
import tempfile
import unittest
from collections import namedtuple
from unittest.mock import patch

import adj_factor_sync as afs


Row = namedtuple('Row', 'ts_code trade_date adj_factor')


class FakeDF:
    def __init__(self, rows):
        self.rows = list(rows)

    def __len__(self):
        return len(self.rows)

    def itertuples(self):
        return iter(self.rows)


class FakePro:
    def __init__(self, rows_by_date=None, error=None):
        self.rows_by_date = rows_by_date or {}
        self.error = error
        self.calls = []

    def adj_factor(self, trade_date=''):
        self.calls.append(trade_date)
        if self.error:
            raise self.error
        return FakeDF(self.rows_by_date.get(trade_date, []))


def make_db(path, codes=('000001.SZ', '600000.SH'), days=('20260105', '20260106')):
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE stock_basic (ts_code TEXT);
      CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL,
        PRIMARY KEY (ts_code, trade_date));
      CREATE TABLE trading_dates_cache (cal_date TEXT);
    ''')
    conn.executemany('INSERT INTO stock_basic VALUES (?)', [(c,) for c in codes])
    conn.executemany('INSERT INTO trading_dates_cache VALUES (?)', [(d,) for d in days])
    conn.commit()
    conn.close()


class AdjFactorWriteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.db = os.path.join(self.folder, 'test.db')
        make_db(self.db)

    def test_writes_only_local_stocks_and_valid_factors(self):
        pro = FakePro({'20260105': [
            Row('000001.SZ', '20260105', 1.5),
            Row('600000.SH', '20260105', 2.0),
            Row('999999.SH', '20260105', 3.0),   # 本地没有 → 跳过
            Row('000001.SZ', '20260105', float('nan')),  # 同一只, 因子非法 → 跳过
        ]})
        result = afs.fetch_one_date('20260105', db_path=self.db, pro=pro)
        self.assertEqual(result['rows'], 2)
        self.assertFalse(result['skipped'])
        conn = sqlite3.connect(self.db)
        rows = dict(conn.execute('SELECT ts_code, adj_factor FROM adj_factor'))
        conn.close()
        self.assertEqual(rows, {'000001.SZ': 1.5, '600000.SH': 2.0})

    def test_rerun_replaces_same_day_rows(self):
        pro = FakePro({'20260105': [Row('000001.SZ', '20260105', 1.5)]})
        afs.fetch_one_date('20260105', db_path=self.db, pro=pro)
        pro.rows_by_date['20260105'] = [Row('000001.SZ', '20260105', 9.0)]
        afs.fetch_one_date('20260105', db_path=self.db, pro=pro)
        conn = sqlite3.connect(self.db)
        value = conn.execute('SELECT adj_factor FROM adj_factor').fetchone()[0]
        conn.close()
        self.assertEqual(value, 9.0)

    def test_empty_day_is_skipped_not_failed(self):
        pro = FakePro({})
        result = afs.fetch_one_date('20260105', db_path=self.db, pro=pro)
        self.assertTrue(result['skipped'])
        self.assertEqual(result['rows'], 0)
        self.assertIn('没有复权因子', result['reason'])

    def test_tushare_error_is_reported_as_skipped(self):
        pro = FakePro(error=RuntimeError('每分钟调用超限'))
        result = afs.fetch_one_date('20260105', db_path=self.db, pro=pro)
        self.assertTrue(result['skipped'])
        self.assertIn('拉取失败', result['reason'])

    def test_bad_date_rejected(self):
        result = afs.fetch_one_date('2026-1-5', db_path=self.db, pro=FakePro())
        self.assertTrue(result['skipped'])


class AdjFactorSyncRouteTests(unittest.TestCase):
    """接口契约 + 「拉取目标」面板状态. 只 mock 掉 TuShare 那一段."""

    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()

    def test_route_pulls_requested_day(self):
        with patch.object(afs, 'fetch_one_date', return_value={'date': '20260105', 'rows': 5570, 'skipped': False, 'reason': ''}) as fetch:
            response = self.client.post('/api/adj-factor/sync?date=20260105')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['status'], 'success')
        self.assertEqual(response.json['rows'], 5570)
        self.assertEqual(fetch.call_args.args[0], '20260105')

    def test_route_rejects_bad_date(self):
        self.assertEqual(self.client.post('/api/adj-factor/sync?date=2026-1-5').status_code, 400)

    def test_route_uses_recent_mode_for_scheduler(self):
        with patch.object(afs, 'sync_recent', return_value={'targets': ['20260105'], 'results': [], 'rows': 5570}) as recent:
            response = self.client.post('/api/adj-factor/sync?recent=6')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['rows'], 5570)
        self.assertEqual(recent.call_args.kwargs['days'], 6)

    def test_sync_overview_lists_adj_factor_source(self):
        response = self.client.get('/api/sync/overview')
        self.assertEqual(response.status_code, 200)
        source = next((s for s in response.json['sources'] if s['key'] == 'adj_factor'), None)
        self.assertIsNotNone(source)
        self.assertEqual(source['name'], '复权因子')
        self.assertIn(source['status'], ('ok', 'partial', 'missing', 'empty'))

    def test_status_helper_rules(self):
        status = self.app_module._adj_factor_status
        self.assertEqual(status(5570, '20260105', 5000, '20260105', '20260105'), 'ok')
        self.assertEqual(status(3000, '20260105', 5000, '20260105', '20260105'), 'partial')
        self.assertEqual(status(0, None, 5000, '20260105', '20260105'), 'empty')
        # 过去的日期缺数据 → 红色缺失 (可以点面板补)
        self.assertEqual(status(0, '20260105', 5000, '20260105', '20260106'), 'missing')

    def test_status_before_1700_on_same_day_is_empty_not_missing(self):
        import datetime as dt

        class FakeDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 5, 10, 30)

        with patch.object(self.app_module, 'datetime', FakeDateTime):
            status = self.app_module._adj_factor_status(0, '20260105', 5000, '20260105', '20260105')
        self.assertEqual(status, 'empty')

    def test_sync_run_registers_adj_factor_key(self):
        self.assertIn('adj_factor', self.app_module.SYNC_RUNNERS)
        mode, runner = self.app_module.SYNC_RUNNERS['adj_factor']
        self.assertEqual(mode, 'sync')
        with patch.object(afs, 'fetch_one_date', return_value={'date': '20260105', 'rows': 10, 'skipped': False, 'reason': ''}):
            result = runner('20260105')
        self.assertEqual(result['rows'], 10)


class AdjFactorSchedulerTests(unittest.TestCase):
    """盘后定时任务: 注册两个时间点, 且不依赖 Flask app context (调度线程没有)."""

    def test_registers_two_after_close_jobs(self):
        import scheduler
        scheduler.register_adj_factor_after_close(token='test-token')
        jobs = {j.id: j for j in scheduler.get_scheduler().get_jobs()}
        self.assertIn('adj_factor_after_close', jobs)
        self.assertIn('adj_factor_after_close_late', jobs)
        self.assertEqual(str(jobs['adj_factor_after_close'].trigger), "cron[hour='17', minute='5']")
        self.assertEqual(str(jobs['adj_factor_after_close_late'].trigger), "cron[hour='18', minute='30']")

    def test_job_runs_without_flask_app_context(self):
        import scheduler
        # 任务在注册时就把 sync_recent 绑到自己作用域里了, 所以要���注册再 patch
        with patch.object(afs, 'sync_recent', return_value={'targets': ['20260105'], 'results': [], 'rows': 12}) as recent:
            scheduler.register_adj_factor_after_close(token='test-token')
            jobs = {j.id: j for j in scheduler.get_scheduler().get_jobs()}
            jobs['adj_factor_after_close'].func()
        self.assertEqual(recent.call_args.kwargs['days'], 6)
        self.assertEqual(recent.call_args.kwargs['token'], 'test-token')


class TokenResolveTests(unittest.TestCase):
    def test_explicit_token_wins(self):
        self.assertEqual(afs.resolve_token('abc'), 'abc')

    def test_env_token_used(self):
        with patch.dict(os.environ, {'TUSHARE_API_KEY': 'from-env'}):
            self.assertEqual(afs.resolve_token(), 'from-env')

    def test_falls_back_to_loaded_app_module(self):
        import app as app_module
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(afs.resolve_token(), app_module.TUSHARE_TOKEN)


if __name__ == '__main__':
    unittest.main()
