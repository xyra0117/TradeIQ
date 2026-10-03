"""Dip market gate, individual rule boundaries and read-only HTTP contract."""
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from flask import Flask
import dip_picks as dip


class KeepConnection(sqlite3.Connection):
    def close(self):
        pass


class DipTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', factory=KeepConnection)
        self.db.executescript('''
            CREATE TABLE trading_dates_cache (cal_date TEXT);
            CREATE TABLE stock_daily (ts_code TEXT,name TEXT,trade_date TEXT,open REAL,
                high REAL,low REAL,close REAL,change REAL,volume REAL,amount REAL);
            CREATE TABLE fund_flow (ts_code TEXT,trade_date TEXT,source TEXT,main_net_inflow REAL);
            CREATE TABLE overview (date TEXT,downCount INTEGER);
            CREATE TABLE index_daily (date TEXT,name TEXT,change REAL);
        ''')
        start = datetime(2026, 1, 5)
        self.days = [(start + timedelta(days=i)).strftime('%Y%m%d') for i in range(200)
                     if (start + timedelta(days=i)).weekday() < 5]
        self.date = self.days[120]
        self.now = datetime(2026, 10, 2, 16)
        self.db.executemany('INSERT INTO trading_dates_cache VALUES (?)', [(d,) for d in self.days])
        self.db.execute('INSERT INTO overview VALUES (?,?)', (self.date, 2801))
        self.db.executemany('INSERT INTO index_daily VALUES (?,?,?)',
                            [(d, '上证指数', v) for d, v in zip(self.days[117:121], [-.1, .5, .5, -1])])
        self.add_stock('000001.SZ')

    def tearDown(self):
        sqlite3.Connection.close(self.db)

    def add_stock(self, code, amplitude=6, ret120=100, volume_ratio=1):
        growth = ((1 + ret120 / 100) / .99) ** (1 / 119)
        previous = 100
        for i, day in enumerate(self.days[:121]):
            close = 100 * growth ** i if i < 120 else 100 * (1 + ret120 / 100)
            spread = previous * amplitude / 200
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (code, '测试股票', day, close, close + spread, close - spread, close,
                             (close / previous - 1) * 100, 100 * (volume_ratio if i == 120 else 1), 100))
            self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)', (code, day, 'eastmoney-push2', 3_000_000))
            previous = close

    def screen(self, date=None, now=None):
        return dip.screen_dip(self.db, self.date if date is None else date, now or self.now)

    def test_fields_formulas_and_independent_universe(self):
        result = self.screen()
        self.assertTrue(result['daily_ready'])
        self.assertTrue(result['market_passed'])
        self.assertEqual(result['count'], 1)
        row = result['results'][0]
        self.assertAlmostEqual(row['change_pct'], -1)
        self.assertAlmostEqual(row['ret120'], 100)
        self.assertAlmostEqual(row['amplitude20'], 6)
        self.assertEqual(row['volume_ratio5'], 1)
        self.assertGreater(row['close'], row['ma20'])
        for key, n in [('main_net_today', 1), ('main_net_3d', 3), ('main_net_5d', 5),
                       ('main_net_10d', 10), ('main_net_20d', 20)]:
            self.assertEqual(row[key], n * 3_000_000)

    def test_decline_count_strict_boundary_for_both_index_branches(self):
        self.db.execute('UPDATE overview SET downCount=2800')
        for change in [-1, -3]:
            self.db.execute('UPDATE index_daily SET change=? WHERE date=?', (change, self.date))
            result = self.screen()
            self.assertFalse(result['market_passed'])
            self.assertEqual(result['results'], [])
            self.assertIn('2800', result['note'])

    def test_index_strict_negative_and_minus_two_boundaries(self):
        for change, passed in [(1, False), (0, False), (-1.999, True), (-2, False), (-2.001, True)]:
            with self.subTest(change=change):
                self.db.execute('UPDATE index_daily SET change=? WHERE date=?', (change, self.date))
                self.assertEqual(self.screen()['market_passed'], passed)

    def test_previous_three_excludes_today_and_flat_is_not_up(self):
        self.db.execute('UPDATE index_daily SET change=.5 WHERE date<?', (self.date,))
        self.assertFalse(self.screen()['market_passed'])
        self.db.execute('UPDATE index_daily SET change=0 WHERE date=?', (self.days[117],))
        self.assertTrue(self.screen()['market_passed'])
        self.assertEqual([r['date'] for r in self.screen()['market']['previous_three']], self.days[117:120])

    def test_deep_index_decline_ignores_previous_three(self):
        self.db.execute('DELETE FROM index_daily WHERE date<?', (self.date,))
        self.assertEqual(self.screen()['market']['status'], 'missing')
        self.db.execute('UPDATE index_daily SET change=-3 WHERE date=?', (self.date,))
        self.assertTrue(self.screen()['market_passed'])
        self.assertEqual(self.screen()['count'], 1)

    def test_missing_market_never_uses_another_day(self):
        self.db.execute('DELETE FROM overview')
        self.db.execute('INSERT INTO overview VALUES (?,9999)', (self.days[119],))
        self.assertFalse(self.screen()['market_passed'])
        self.assertEqual(self.screen()['market']['status'], 'missing')
        self.db.execute('INSERT INTO overview VALUES (?,2801)', (self.date,))
        self.db.execute('DELETE FROM index_daily WHERE date=?', (self.date,))
        self.assertEqual(self.screen()['market']['status'], 'missing')

    def test_iso_market_dates_are_normalized(self):
        self.db.execute("UPDATE overview SET date=substr(date,1,4)||'-'||substr(date,5,2)||'-'||substr(date,7,2)")
        self.db.execute("UPDATE index_daily SET date=substr(date,1,4)||'-'||substr(date,5,2)||'-'||substr(date,7,2)")
        self.assertEqual(self.screen()['count'], 1)

    def test_today_stock_must_decline(self):
        self.db.execute('UPDATE stock_daily SET change=0 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['count'], 0)
        self.db.execute('UPDATE stock_daily SET change=1 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['stats']['declining'], 0)

    def test_ret120_strictly_less_than_300(self):
        self.add_stock('000002.SZ', ret120=300)
        self.add_stock('000003.SZ', ret120=299.99)
        self.add_stock('000004.SZ', ret120=301)
        result = self.screen()
        self.assertEqual([r['ts_code'] for r in result['results']], ['000001.SZ', '000003.SZ'])
        self.assertEqual(result['stats']['return_too_high'], 2)

    def test_volume_uses_prior_five_not_current_or_prior_one(self):
        self.add_stock('000002.SZ', volume_ratio=1.2)
        self.add_stock('000003.SZ', volume_ratio=1.1999)
        result = self.screen()
        self.assertEqual([r['ts_code'] for r in result['results']], ['000001.SZ', '000003.SZ'])
        self.assertEqual(result['stats']['volume_too_high'], 1)

    def test_ma20_strict_boundary_and_includes_today(self):
        self.db.execute('UPDATE stock_daily SET open=100,close=100,high=103,low=97,change=0')
        self.db.execute('UPDATE stock_daily SET open=101,close=101,high=104,change=1 WHERE trade_date=?',
                        (self.days[119],))
        # Today is exactly the 20-day MA: 19*x = 18*100 + 101.
        close = 1901 / 19
        self.db.execute('UPDATE stock_daily SET open=?,close=?,change=? WHERE trade_date=?',
                        (close, close, (close / 101 - 1) * 100, self.date))
        self.assertEqual(self.screen()['stats']['below_ma20'], 1)

    def test_activity_inclusive_five(self):
        self.add_stock('000002.SZ', amplitude=5)
        self.add_stock('000003.SZ', amplitude=4.999)
        result = self.screen()
        self.assertEqual([r['ts_code'] for r in result['results']], ['000001.SZ', '000002.SZ'])
        self.assertEqual(result['stats']['inactive'], 1)

    def test_funds_strict_amount_boundary_in_yuan(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=2500000')
        self.assertEqual(self.screen()['stats']['funds_below_threshold'], 1)
        self.db.execute('UPDATE fund_flow SET main_net_inflow=2500001')
        self.assertEqual(self.screen()['count'], 1)

    def test_any_fund_window_can_pass_amount_threshold(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=100000')
        self.db.execute('UPDATE fund_flow SET main_net_inflow=50000001 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['count'], 1)

    def test_today_zero_or_negative_is_allowed_and_still_displayed(self):
        for today in [0, -1_000_000]:
            with self.subTest(today=today):
                self.db.execute('UPDATE fund_flow SET main_net_inflow=? WHERE trade_date=?', (today, self.date))
                result = self.screen()
                self.assertEqual(result['count'], 1)
                row = result['results'][0]
                self.assertEqual(row['main_net_today'], today)
                self.assertEqual(row['main_net_3d'], 6_000_000 + today)
                self.assertEqual(row['main_net_20d'], 57_000_000 + today)

    def test_today_alone_cannot_satisfy_amount_threshold(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=0')
        self.db.execute('UPDATE fund_flow SET main_net_inflow=60000000 WHERE trade_date=?', (self.date,))
        self.db.execute('UPDATE fund_flow SET main_net_inflow=-59000000 WHERE trade_date=?', (self.days[119],))
        result = self.screen()
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['stats']['funds_not_red'], 0)
        self.assertEqual(result['stats']['funds_below_threshold'], 1)

    def test_all_cumulative_fund_windows_must_be_strictly_positive(self):
        for window in [3, 5, 10, 20]:
            for boundary in [0, -1]:
                with self.subTest(window=window, boundary=boundary):
                    self.db.execute('UPDATE fund_flow SET main_net_inflow=20000000')
                    self.db.execute('UPDATE fund_flow SET main_net_inflow=? WHERE trade_date=?',
                                    (20_000_000 * (1 - window) + boundary, self.days[121 - window]))
                    result = self.screen()
                    self.assertEqual(result['count'], 0)
                    self.assertEqual(result['stats']['funds_not_red'], 1)

    def test_fund_gaps_and_other_source_do_not_fill_window(self):
        self.db.execute('DELETE FROM fund_flow WHERE trade_date=?', (self.days[110],))
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)', ('000001.SZ', self.days[110], 'tushare', 1e12))
        result = self.screen()
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['stats']['missing_funds'], 1)

    def test_missing_calendar_bar_reference_and_invalid_ohlc(self):
        self.db.execute('DELETE FROM stock_daily WHERE trade_date=?', (self.days[50],))
        self.assertEqual(self.screen()['stats']['missing_history'], 1)
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('000001.SZ', '测试', self.days[50], 100, 103, 97, 100, 0, 100, 100))
        self.assertEqual(self.screen()['stats']['reference_mismatch'], 1)
        self.db.execute('UPDATE stock_daily SET high=0 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['stats']['invalid_prices'], 1)

    def test_120_rows_are_not_enough_for_120_day_return(self):
        self.db.execute('DELETE FROM trading_dates_cache WHERE cal_date=?', (self.days[0],))
        result = self.screen()
        self.assertEqual(result['count'], 0)
        self.assertIn('121', result['note'])

    def test_future_data_does_not_change_membership_or_fields(self):
        before = self.screen()
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('000001.SZ', '测试', self.days[121], 1000, 1100, 900, 1000, 400, 1000, 1000))
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)', ('000001.SZ', self.days[121], 'eastmoney-push2', -1e12))
        self.db.execute('INSERT INTO overview VALUES (?,0)', (self.days[121],))
        after = self.screen()
        self.assertEqual(before['results'], after['results'])
        self.assertEqual(before['market'], after['market'])

    def test_intraday_explicit_waits_latest_falls_back(self):
        now = datetime.strptime(self.date + ' 11:00', '%Y%m%d %H:%M')
        result = self.screen(now=now)
        self.assertFalse(result['daily_ready'])
        self.assertEqual(result['results'], [])
        self.assertEqual(self.screen(date='', now=now)['date'], self.days[119])

    def test_partial_latest_batch_is_not_ready(self):
        self.db.execute('INSERT INTO stock_daily SELECT "000002.SZ",name,trade_date,open,high,low,close,change,volume,amount '
                        'FROM stock_daily WHERE trade_date=?', (self.days[119],))
        self.assertFalse(self.screen()['daily_ready'])
        self.assertEqual(self.screen(date='')['date'], self.days[119])

    def test_missing_or_invalid_dates(self):
        for date in ['20260230', '2026-06-22', 'bad']:
            with self.assertRaises(ValueError):
                self.screen(date)
        self.assertFalse(self.screen('20270101')['daily_ready'])
        self.assertFalse(self.screen('20260101')['daily_ready'])

    def test_unmatched_market_does_not_read_fund_or_long_history(self):
        statements = []
        self.db.set_trace_callback(statements.append)
        self.db.execute('UPDATE overview SET downCount=10')
        self.screen()
        self.assertFalse(any('FROM fund_flow' in sql for sql in statements))

    def test_http_readonly_transaction_invalid_date_and_database_error(self):
        self.db.commit()
        app = Flask(__name__)
        dip.register_dip_picks(app, lambda: '/tmp/not-used.db')
        statements = []
        self.db.set_trace_callback(statements.append)
        with patch.object(dip.sqlite3, 'connect', return_value=self.db) as connect:
            response = app.test_client().get('/api/picks/dip?date=' + self.date)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['count'], 1)
            self.assertIn('mode=ro', connect.call_args.args[0])
            self.assertTrue(connect.call_args.kwargs['uri'])
            # A real connection close rolls back BEGIN; this fixture deliberately stays open.
            self.db.rollback()
            self.assertEqual(app.test_client().get('/api/picks/dip?date=bad').status_code, 400)
        self.assertEqual(self.db.execute('PRAGMA query_only').fetchone()[0], 1)
        self.assertTrue(any(sql == 'BEGIN' for sql in statements))
        self.assertFalse(any(sql.split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER') for sql in statements))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with patch.object(dip.sqlite3, 'connect', side_effect=sqlite3.OperationalError('private path')):
            response = app.test_client().get('/api/picks/dip')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private path', response.text)

    def add_outcome_bars(self):
        previous = 200
        for day, close, high in zip(self.days[121:126], [220, 198, 190, 200, 210], [240, 220, 200, 210, 220]):
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                            ('000001.SZ', '测试股票', day, close, high, close * .98, close,
                             (close / previous - 1) * 100, 100, 100))
            previous = close

    def shared_outcomes(self):
        # Use the actual same implementation injected by app.py; never start the application.
        import app as backend
        rows = self.screen()['results']
        days, live_index = backend._attach_preferred_outcomes(rows, self.date, self.db, key='dip_outcomes')
        return rows[0]['dip_outcomes'], days, live_index

    def test_tn_close_and_today_high_relative_to_entry_not_sum_or_running_max(self):
        self.add_outcome_bars()
        outcomes, days, live_index = self.shared_outcomes()
        self.assertEqual(days, self.days[121:126])
        self.assertEqual(live_index, 0)
        self.assertEqual([r['high_sum_pct'] for r in outcomes], [20, 10, 0, 5, 10])
        self.assertEqual([r['close_sum_pct'] for r in outcomes], [10, -1, -5, 0, 5])
        self.assertTrue(all(r['reason'] is None for r in outcomes))

    def test_tn_missing_bar_blocks_that_day_and_later_days(self):
        self.add_outcome_bars()
        self.db.execute('DELETE FROM stock_daily WHERE trade_date=?', (self.days[122],))
        outcomes, _, _ = self.shared_outcomes()
        self.assertEqual(outcomes[0]['close_sum_pct'], 10)
        self.assertTrue(all(r['high_sum_pct'] is None and r['close_sum_pct'] is None for r in outcomes[1:]))
        self.assertTrue(all(r['reason'] == 'missing_daily' for r in outcomes[1:]))

    def test_tn_reference_mismatch_and_bad_high_do_not_emit_values(self):
        self.add_outcome_bars()
        self.db.execute('UPDATE stock_daily SET change=2 WHERE trade_date=?', (self.days[122],))
        self.assertEqual(self.shared_outcomes()[0][1]['reason'], 'reference_mismatch')
        self.db.execute('UPDATE stock_daily SET change=-10,high=100 WHERE trade_date=?', (self.days[122],))
        self.assertEqual(self.shared_outcomes()[0][1]['reason'], 'invalid_daily')

    def test_tn_future_dates_without_bars_and_calendar_without_future_dates(self):
        outcomes, days, _ = self.shared_outcomes()
        self.assertEqual(len(days), 5)
        self.assertTrue(all(r['high_sum_pct'] is None for r in outcomes))
        self.assertTrue(all(r['reason'] == 'missing_daily' for r in outcomes))
        self.db.execute('DELETE FROM trading_dates_cache WHERE cal_date>?', (self.date,))
        outcomes, days, _ = self.shared_outcomes()
        self.assertEqual(days, [])
        self.assertEqual(len(outcomes), 5)
        self.assertTrue(all(r['date'] is None and r['reason'] == 'future_unavailable' for r in outcomes))

    def test_tn_intraday_uses_entry_price_and_validates_previous_close(self):
        import app as backend
        self.add_outcome_bars()
        now = datetime.strptime(self.days[123] + ' 10:00', '%Y%m%d %H:%M')
        with patch.object(backend, 'datetime') as clock, patch.object(backend, 'fetch_sina_quotes') as quotes:
            clock.now.return_value = now
            quotes.return_value = {'000001.SZ': {'price': 204, 'high': 214, 'prev_close': 198}}
            outcomes, days, live_index = self.shared_outcomes()
            self.assertEqual(live_index, 3)
            self.assertEqual(outcomes[2]['close_sum_pct'], 2)
            self.assertEqual(outcomes[2]['high_sum_pct'], 7)
            self.assertEqual(outcomes[2]['reason'], 'intraday')
            self.assertTrue(all(r['reason'] == 'waiting_close' for r in outcomes[3:]))
            quotes.return_value['000001.SZ']['prev_close'] = 205
            outcomes, _, _ = self.shared_outcomes()
            self.assertEqual(outcomes[2]['reason'], 'reference_mismatch')
            self.assertIsNone(outcomes[2]['close_sum_pct'])
            quotes.return_value = {}
            self.assertEqual(self.shared_outcomes()[0][2]['reason'], 'waiting_close')

    def test_http_enrichment_reuses_preferred_and_keeps_membership_readonly(self):
        import app as backend
        self.add_outcome_bars()
        before = self.screen()['results']
        self.db.commit()
        app = Flask(__name__)
        dip.register_dip_picks(app, lambda: '/tmp/not-used.db', outcome_loader=backend._attach_preferred_outcomes)
        statements = []
        self.db.set_trace_callback(statements.append)
        with patch.object(dip.sqlite3, 'connect', return_value=self.db):
            response = app.test_client().get('/api/picks/dip?date=' + self.date)
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data['count'], len(before))
        self.assertEqual(data['next_dates'], self.days[121:126])
        self.assertFalse(data['intraday_used'])
        actual = data['results'][0].copy()
        outcomes = actual.pop('dip_outcomes')
        self.assertEqual(actual, before[0])
        self.assertEqual(outcomes[1]['close_sum_pct'], -1)
        self.assertEqual(outcomes[1]['high_sum_pct'], 10)
        self.assertFalse(any(sql.split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER') for sql in statements))


if __name__ == '__main__':
    unittest.main()
