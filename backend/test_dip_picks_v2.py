"""Independent 2.0 boundaries, same-day amounts, future isolation and read-only HTTP."""
import sqlite3
import unittest
from unittest.mock import patch

from flask import Flask
import dip_picks as dip
import test_dip_picks as fixtures


class DipV2Tests(unittest.TestCase):
    add_stock = fixtures.DipTests.add_stock
    tearDown = fixtures.DipTests.tearDown

    def setUp(self):
        fixtures.DipTests.setUp(self)
        self.prices()

    def prices(self, bias20=None, older_close=110):
        recent = 98 if bias20 is None else (2000 / (1 + bias20 / 100) - 201) / 18
        previous = older_close
        for index, day in enumerate(self.days[:121]):
            close = older_close if index < 101 else recent if index < 119 else 101 if index == 119 else 100
            self.db.execute('UPDATE stock_daily SET open=?,close=?,high=?,low=?,change=?,amount=60000 '
                            'WHERE ts_code=? AND trade_date=?',
                            (close, close, close + previous * .03, close - previous * .03,
                             (close / previous - 1) * 100, '000001.SZ', day))
            previous = close

    def screen(self):
        return dip.screen_dip_v2(self.db, self.date, self.now)

    def test_subset_formulas_units_and_original_unchanged(self):
        original = dip.screen_dip(self.db, self.date, self.now)
        result = self.screen()
        self.assertEqual(result['count'], 1)
        row = result['results'][0]
        self.assertEqual(row['main_strength5_pct'], 5)
        self.assertAlmostEqual(row['ma20_bias_pct'], (100 / 98.25 - 1) * 100)
        self.assertAlmostEqual(row['ma60'], (40 * 110 + 18 * 98 + 101 + 100) / 60)
        self.assertEqual(result['v2_stats']['base_count'], original['count'])
        self.assertEqual(original, dip.screen_dip(self.db, self.date, self.now))
        self.assertNotIn('ma60', original['results'][0])
        self.assertEqual(result['strategy'], 'dip-picks-v2')

    def test_ma20_five_percent_boundary(self):
        for bias, expected in [(4.999, 1), (5, 1), (5.001, 0)]:
            with self.subTest(bias=bias):
                self.prices(bias20=bias)
                result = self.screen()
                self.assertEqual(result['v2_stats']['base_count'], 1)
                self.assertEqual(result['count'], expected)
                self.assertEqual(result['v2_stats']['too_far_ma20'], 1 - expected)

    def test_ma60_includes_today_and_allows_equality(self):
        boundary = (60 * 100 - 18 * 98 - 101 - 100) / 40
        for value, expected in [(boundary + .001, 1), (boundary, 1), (boundary - .001, 0)]:
            with self.subTest(value=value):
                self.prices(older_close=value)
                self.assertEqual(self.screen()['count'], expected)

    def test_strength_boundary_uses_same_five_days_including_today(self):
        for amount, expected in [(59_999, 1), (60_000, 1), (60_001, 0)]:
            with self.subTest(amount=amount):
                self.db.execute('UPDATE stock_daily SET amount=?', (amount,))
                self.assertEqual(self.screen()['count'], expected)
        self.db.execute('UPDATE stock_daily SET amount=60000')
        self.db.execute('UPDATE stock_daily SET amount=1e10 WHERE trade_date<?', (self.days[116],))
        self.assertEqual(self.screen()['count'], 1)
        self.db.execute('UPDATE stock_daily SET amount=60001 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['v2_stats']['weak_main5'], 1)

    def test_each_amount_must_be_complete_positive_and_finite(self):
        for amount in [None, 0, -1, float('inf')]:
            with self.subTest(amount=amount):
                self.db.execute('UPDATE stock_daily SET amount=60000')
                self.db.execute('UPDATE stock_daily SET amount=? WHERE trade_date=?', (amount, self.days[118]))
                result = self.screen()
                self.assertEqual(result['count'], 0)
                self.assertEqual(result['v2_stats']['invalid_amount'], 1)
                self.assertEqual(dip.screen_dip(self.db, self.date, self.now)['count'], 1)

    def test_today_negative_funds_still_allowed(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=-1000000 WHERE trade_date=?', (self.date,))
        self.db.execute('UPDATE stock_daily SET amount=40000')
        row = self.screen()['results'][0]
        self.assertEqual(row['main_net_today'], -1_000_000)
        self.assertGreaterEqual(row['main_strength5_pct'], 5)

    def test_future_data_never_affects_selection(self):
        before = self.screen()
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('000001.SZ', '未来', self.days[121], 1000, 1200, 900, 1000, 900, 100, 1e12))
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)',
                        ('000001.SZ', self.days[121], 'eastmoney-push2', -1e12))
        after = self.screen()
        for key in ['results', 'count', 'market', 'stats', 'v2_stats']:
            self.assertEqual(before[key], after[key])

    def test_market_and_original_conditions_are_required(self):
        self.db.execute('UPDATE overview SET downCount=2800')
        result = self.screen()
        self.assertFalse(result['market_passed'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['v2_stats']['base_count'], 0)
        self.db.execute('UPDATE overview SET downCount=2801')
        self.db.execute('DELETE FROM fund_flow WHERE trade_date=?', (self.days[110],))
        self.assertEqual(self.screen()['count'], 0)

    def test_exclusion_counts_account_for_every_original_candidate(self):
        self.prices(bias20=5.001, older_close=90)
        self.db.execute('UPDATE stock_daily SET amount=0 WHERE trade_date=?', (self.date,))
        stats = self.screen()['v2_stats']
        self.assertEqual(stats['base_count'], 1)
        self.assertEqual(stats['too_far_ma20'], 1)
        self.assertEqual(sum(v for k, v in stats.items() if k != 'base_count'), 1)

    def test_route_read_only_outcomes_and_errors(self):
        self.db.commit()
        app = Flask(__name__)
        loaded = []

        def outcomes(rows, day, conn, key):
            loaded.append((len(rows), day, key, rows[0]['main_strength5_pct']))
            rows[0][key] = [{'date': self.days[121], 'high_sum_pct': 10, 'close_sum_pct': -1}]
            return self.days[121:126], 0

        dip.register_dip_picks(app, lambda: '/tmp/not-used.db', outcome_loader=outcomes)
        statements = []
        self.db.set_trace_callback(statements.append)
        with patch.object(dip.sqlite3, 'connect', return_value=self.db) as connect:
            response = app.test_client().get('/api/picks/dip-v2?date=' + self.date)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['results'][0]['dip_outcomes'][0]['close_sum_pct'], -1)
            self.assertEqual(loaded, [(1, self.date, 'dip_outcomes', 5)])
            self.assertIn('mode=ro', connect.call_args.args[0])
            self.assertTrue(connect.call_args.kwargs['uri'])
            self.db.rollback()
            self.assertEqual(app.test_client().get('/api/picks/dip-v2?date=bad').status_code, 400)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.db.execute('PRAGMA query_only').fetchone()[0], 1)
        self.assertTrue(any(sql == 'BEGIN' for sql in statements))
        self.assertFalse(any(sql.split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER')
                             for sql in statements))
        with patch.object(dip.sqlite3, 'connect', side_effect=sqlite3.OperationalError('private')):
            response = app.test_client().get('/api/picks/dip-v2')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private', response.text)


if __name__ == '__main__':
    unittest.main()
