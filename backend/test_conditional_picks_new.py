"""“条件选股新”规则回归：使用内存日线，不读写实际行情库。"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import app as backend


class ConditionalPicksNewTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute(
            'CREATE TABLE stock_daily '
            '(ts_code TEXT, trade_date TEXT, close REAL, volume REAL)'
        )

    def tearDown(self):
        self.db.close()

    def _insert_history(self, code, closes, volumes):
        for index, (close, volume) in enumerate(zip(closes, volumes), start=1):
            self.db.execute(
                'INSERT INTO stock_daily VALUES (?,?,?,?)',
                (code, f'202601{index:02d}', close, volume)
            )
        self.db.commit()

    @staticmethod
    def _candidate(code):
        return {
            'ts_code': code, 'name': code, 'close': 10, 'change_pct': -1,
            'used_bucket': 'main', 'hit_threshold': '3d',
            'sum_3d': 4e8, 'sum_5d': 4e8, 'sum_10d': 4e8, 'sum_20d': 4e8,
        }

    def test_ma10_and_volume_boundaries_are_inclusive(self):
        self._insert_history('EQ.SZ', [10] * 10, [100] * 9 + [80])
        self._insert_history('ABOVE.SZ', [10] * 9 + [11], [100] * 9 + [70])
        hits, stats = backend._filter_conditional_new(
            [self._candidate('EQ.SZ'), self._candidate('ABOVE.SZ')],
            '20260110', 80, self.db
        )
        self.assertEqual([row['ts_code'] for row in hits], ['EQ.SZ', 'ABOVE.SZ'])
        self.assertEqual(hits[0]['ma10'], 10)
        self.assertEqual(hits[0]['volume_ratio_pct'], 80)
        self.assertEqual(hits[0]['volume_shrink_pct'], 20)
        self.assertEqual(stats['matched'], 2)

    def test_existing_conditional_rules_are_applied_first(self):
        self._insert_history('PASS.SZ', [10] * 10, [100] * 9 + [70])
        passing = self._candidate('PASS.SZ')
        passing.update({f'sum_main_{days}d': 4e8 for days in (3, 5, 10, 20)})
        failing = dict(self._candidate('FAIL.SZ'), change_pct=1)
        failing.update({f'sum_main_{days}d': 4e8 for days in (3, 5, 10, 20)})
        base_hits = backend._filter_conditional([passing, failing], 3e8)
        hits, stats = backend._filter_conditional_new(base_hits, '20260110', 80, self.db)
        self.assertEqual([row['ts_code'] for row in hits], ['PASS.SZ'])
        self.assertEqual(stats['base_count'], 1)

    def test_exclusion_reasons_and_custom_ratio(self):
        self._insert_history('BELOW.SZ', [10] * 9 + [9], [100] * 9 + [50])
        self._insert_history('LOUD.SZ', [10] * 10, [100] * 9 + [81])
        self._insert_history('SHORT.SZ', [None] + [10] * 9, [100] * 10)
        self._insert_history('ZERO.SZ', [10] * 10, [100] * 8 + [0, 50])
        rows = [self._candidate(code) for code in ['BELOW.SZ', 'LOUD.SZ', 'SHORT.SZ', 'ZERO.SZ']]
        hits, stats = backend._filter_conditional_new(rows, '20260110', 80, self.db)
        self.assertEqual(hits, [])
        self.assertEqual(stats['below_ma10'], 1)
        self.assertEqual(stats['volume_not_shrunk'], 1)
        self.assertEqual(stats['insufficient_history'], 1)
        self.assertEqual(stats['invalid_volume'], 1)

        hits, _ = backend._filter_conditional_new([self._candidate('LOUD.SZ')], '20260110', 90, self.db)
        self.assertEqual(len(hits), 1)

    def test_missing_selected_day_is_not_replaced_by_latest(self):
        self._insert_history('OLD.SZ', [10] * 10, [100] * 10)
        hits, stats = backend._filter_conditional_new(
            [self._candidate('OLD.SZ')], '20260111', 80, self.db
        )
        self.assertEqual(hits, [])
        self.assertEqual(stats['missing_current_daily'], 1)

    def test_intraday_readiness_waits_for_close(self):
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?)', ('A.SZ', '20260110', 10, 100))
        self.db.commit()
        ready, reason = backend._conditional_new_daily_ready(
            '20260110', datetime(2026, 1, 10, 14, 59), self.db
        )
        self.assertFalse(ready)
        self.assertEqual(reason, 'market_not_closed')

    def test_api_invalid_volume_ratio_falls_back_to_80(self):
        base = {'status': 'success', 'date': '20260110', 'count': 1,
                'results': [self._candidate('A.SZ')], 'filter': {}}
        filtered = ([dict(self._candidate('A.SZ'), ma10=10)], {'base_count': 1, 'matched': 1})
        with patch.object(backend, '_resolve_conditional_new_date', return_value='20260110'), \
                patch.object(backend, '_conditional_new_daily_ready', return_value=(True, 'ready')), \
                patch.object(backend, '_read_conditional_snapshot', return_value=base), \
                patch.object(backend, '_filter_conditional_new', return_value=filtered) as apply_new:
            data = backend.app.test_client().get(
                '/api/conditional-picks/new?volume_ratio_pct=0&threshold_yi=3'
            ).get_json()
        self.assertEqual(data['filter']['max_volume_ratio_pct'], 80)
        self.assertEqual(data['count'], 1)
        self.assertEqual(apply_new.call_args.args[2], 80)

    def test_api_waiting_state_skips_base_calculation(self):
        with patch.object(backend, '_resolve_conditional_new_date', return_value='20260110'), \
                patch.object(backend, '_conditional_new_daily_ready', return_value=(False, 'daily_not_loaded')), \
                patch.object(backend, '_read_conditional_snapshot') as read_snapshot:
            data = backend.app.test_client().get('/api/conditional-picks/new').get_json()
        read_snapshot.assert_not_called()
        self.assertFalse(data['daily_ready'])
        self.assertEqual(data['note'], '等待收盘日线入库')

    def test_save_and_read_new_snapshot_roundtrip(self):
        """落库 → 直读: 写一次, 立刻直读应该返回相同 results / exclusions (参数一致)."""
        import json, sqlite3
        from datetime import datetime
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            tmp_path = f.name
        try:
            # 让 helper 用临时库 (替换 DB_PATH)
            old_path, old_today = backend.DB_PATH, datetime.now().strftime('%Y%m%d')
            backend.DB_PATH = tmp_path
            backend.sqlite3.connect(tmp_path).executescript('''
                CREATE TABLE conditional_picks_new_snapshot (
                    trade_date TEXT PRIMARY KEY, count INTEGER NOT NULL,
                    results_json TEXT NOT NULL, exclusions_json TEXT,
                    threshold_yi REAL, volume_ratio_pct REAL,
                    saved_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE conditional_picks_snapshot (
                    trade_date TEXT PRIMARY KEY, count INTEGER NOT NULL,
                    results_json TEXT NOT NULL, next_date TEXT,
                    next_date_2 TEXT, next_date_3 TEXT, next_date_4 TEXT, next_date_5 TEXT,
                    intraday_used INTEGER DEFAULT 0,
                    saved_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, close REAL, volume REAL);
            ''').close()
            hits = [{'ts_code': 'X.SZ', 'name': 'X', 'close': 10, 'volume_ratio_pct': 80}]
            excl = {'matched': 1, 'base_count': 2}
            date = '20260110' if old_today > '20260110' else '20200101'  # 任何历史日
            backend._save_conditional_new_snapshot(date, hits, excl, 3, 80)
            cached = backend._read_conditional_new_snapshot(date, 3, 80)
            self.assertIsNotNone(cached)
            self.assertEqual(cached['count'], 1)
            self.assertEqual(cached['results'], hits)
            self.assertEqual(cached['exclusions'], excl)
            self.assertEqual(cached['note'], '快照库直读')
            backend.DB_PATH = old_path
        finally:
            os.unlink(tmp_path)

    def test_read_new_snapshot_param_mismatch_falls_back(self):
        """参数不一致 (门槛变了) → 直读应返回 None, 不污染实时路径."""
        import tempfile
        from datetime import datetime
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            tmp_path = f.name
        try:
            old_path = backend.DB_PATH
            backend.DB_PATH = tmp_path
            backend.sqlite3.connect(tmp_path).executescript('''
                CREATE TABLE conditional_picks_new_snapshot (
                    trade_date TEXT PRIMARY KEY, count INTEGER NOT NULL,
                    results_json TEXT NOT NULL, exclusions_json TEXT,
                    threshold_yi REAL, volume_ratio_pct REAL,
                    saved_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, close REAL, volume REAL);
            ''').close()
            date = '20260110' if datetime.now().strftime('%Y%m%d') > '20260110' else '20200101'
            backend._save_conditional_new_snapshot(date, [{'ts_code': 'A.SZ'}], {}, 3, 80)
            # 门槛变成 5 亿 → 不命中, 走实时
            self.assertIsNone(backend._read_conditional_new_snapshot(date, 5, 80))
            # 缩量变 70 → 不命中, 走实时
            self.assertIsNone(backend._read_conditional_new_snapshot(date, 3, 70))
            backend.DB_PATH = old_path
        finally:
            os.unlink(tmp_path)


if __name__ == '__main__':
    unittest.main()
