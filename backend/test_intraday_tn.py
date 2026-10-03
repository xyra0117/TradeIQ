"""盘中 T+N 回归：历史入选日也必须补今天所在的 T 列。"""
from datetime import datetime as RealDateTime, timedelta
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app as backend


class _TenAm(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        current = RealDateTime.now(tz)
        return cls(current.year, current.month, current.day, 10, 0, 0, tzinfo=current.tzinfo)


class IntradayTNTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(suffix='.db')
        db = sqlite3.connect(self.temp.name)
        db.execute('CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, close REAL)')
        db.close()
        self.old_path = backend.DB_PATH
        backend.DB_PATH = self.temp.name
        self.today = RealDateTime.now().strftime('%Y%m%d')
        self.quote = {'A.SZ': {'price': 10.5, 'prev_close': 10.2,
                               'change_pct': 2.94, 'high': 10.8}}

    def tearDown(self):
        backend.DB_PATH = self.old_path
        self.temp.close()

    def test_snapshot_t2_is_enriched_with_today_quote(self):
        payload = {
            'status': 'success', 'date': '20260911', 'count': 1,
            'next_date': '20260914', 'next_date_2': self.today,
            'next_date_3': None, 'next_date_4': None, 'next_date_5': None,
            'results': [{'ts_code': 'A.SZ', 'close': 10,
                         'next_1_close': 10.2, 'max_high_t1': 11.0}],
        }
        with patch.object(backend, '_read_conditional_snapshot', return_value=payload), \
                patch.object(backend, 'fetch_sina_quotes', return_value=self.quote):
            data = backend.app.test_client().get(
                '/api/conditional-picks/snapshot?date=20260911').get_json()
        self.assertEqual(data.get('live_next_index'), 2)
        row = data['results'][0]
        self.assertEqual(row.get('post_pick_next2_change_pct'), 2.94)
        self.assertEqual(row.get('next_2_close'), 10.5)
        self.assertEqual(row.get('max_high_t2'), 11.0)

    def test_missing_quote_does_not_claim_intraday_used(self):
        row = {'ts_code': 'A.SZ', 'close': 10}
        payload = {'next_date': self.today, 'results': [row], 'intraday_used': True}
        backend._enrich_intraday_payload(
            payload, today=self.today, quotes={}, has_daily=False)
        self.assertFalse(payload['intraday_used'])
        self.assertNotIn('next_1_close', row)

    def test_picks_quote_exposes_intraday_high(self):
        db = sqlite3.connect(self.temp.name)
        db.executemany('INSERT INTO stock_daily VALUES (?,?,?)', [
            ('A.SZ', '20260911', 10), ('A.SZ', '20260914', 10.2)])
        db.commit()
        db.close()
        with patch.object(backend, 'fetch_sina_quotes', return_value=self.quote):
            data = backend.app.test_client().get('/api/picks/quote?codes=A.SZ').get_json()
        self.assertEqual(data['A.SZ']['high'], 10.8)

    def test_t2_intraday_rows_are_not_saved_as_close_snapshots(self):
        prior = (RealDateTime.now() - timedelta(days=1)).strftime('%Y%m%d')
        with patch.object(backend, '_next_workdays', return_value=[prior, self.today]), \
                patch.object(backend.sqlite3, 'connect', side_effect=AssertionError('must not write')):
            backend._save_conditional_new_snapshot(prior, [], {}, 3, 80)
            backend._save_conditional_preferred_snapshot(prior, [], {}, 3, 80)

    def test_preferred_t1_uses_live_price_and_high(self):
        trade_date = (RealDateTime.now() - timedelta(days=1)).strftime('%Y%m%d')
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE trading_dates_cache (cal_date TEXT)')
        db.execute('CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, close REAL, change REAL, amount REAL)')
        db.execute('INSERT INTO trading_dates_cache VALUES (?)', (self.today,))
        row = {'ts_code': 'A.SZ', 'close': 10}
        try:
            with patch.object(backend, 'datetime', _TenAm), \
                    patch.object(backend, 'fetch_sina_quotes', return_value={
                        'A.SZ': {'price': 10.3, 'prev_close': 10,
                                 'change_pct': 3, 'high': 10.5}}):
                backend._attach_preferred_outcomes([row], trade_date, db)
        finally:
            db.close()
        outcome = row['preferred_outcomes'][0]
        self.assertEqual(outcome.get('reason'), 'intraday')
        self.assertEqual(outcome.get('high_sum_pct'), 5)
        self.assertEqual(outcome.get('close_sum_pct'), 3)


if __name__ == '__main__':
    unittest.main()
