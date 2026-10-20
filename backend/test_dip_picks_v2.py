"""Independent 2.0 market, rising-stock/limit/MA rules and read-only HTTP."""
import math
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
        self.db.execute('UPDATE overview SET downCount=2599')
        self.db.execute('UPDATE index_daily SET change=.5 WHERE date=?', (self.date,))
        self.prices()

    def prices(self, change=6, older_close=80, closes=None):
        closes = closes or ([older_close] * 101 + [90 + i * .5 for i in range(18)]
                            + [100, 100 * (1 + change / 100)])
        previous = closes[0]
        for day, close in zip(self.days[:121], closes):
            self.db.execute('UPDATE stock_daily SET open=?,close=?,high=?,low=?,change=?,amount=60000 '
                            'WHERE ts_code=? AND trade_date=?',
                            (close, close, close + previous * .03, close - previous * .03,
                             (close / previous - 1) * 100, '000001.SZ', day))
            previous = close
        self.db.execute('UPDATE stock_daily SET change=? WHERE ts_code=? AND trade_date=?',
                        (change, '000001.SZ', self.date))

    def screen(self):
        return dip.screen_dip_v2(self.db, self.date, self.now)

    def test_formulas_units_and_original_unchanged(self):
        original = dip.screen_dip(self.db, self.date, self.now)
        result = self.screen()
        self.assertEqual(result['count'], 1)
        row = result['results'][0]
        closes = [r[0] for r in self.db.execute('SELECT close FROM stock_daily ORDER BY trade_date')]
        for period in [5, 10, 20, 60]:
            self.assertAlmostEqual(row[f'ma{period}'], math.fsum(closes[-period:]) / period)
        self.assertGreater(row['close'], row['ma5'])
        self.assertGreater(row['ma5'], row['ma10'])
        self.assertGreater(row['ma10'], row['ma20'])
        self.assertGreater(row['ma20'], row['ma60'])
        self.assertEqual(row['limit_up_price'], 110)
        self.assertEqual(row['limit_pct'], 10)
        self.assertAlmostEqual(row['change_pct'], 6)
        self.assertNotIn('main_strength5_pct', row)
        self.assertNotIn('ma20_bias_pct', row)
        self.assertEqual(result['v2_stats']['base_count'], 1)
        self.assertEqual(original['count'], 0)
        self.assertFalse(original['market_passed'])
        self.assertEqual(original, dip.screen_dip(self.db, self.date, self.now))
        self.assertEqual(result['strategy'], 'dip-picks-v2')

    def test_daily_gain_strict_five_percent_boundary(self):
        for change, expected in [(-1, 0), (0, 0), (4.999, 0), (5, 0), (5.001, 1), (6, 1)]:
            with self.subTest(change=change):
                self.prices(change=change)
                result = self.screen()
                self.assertEqual(result['stats']['strong_rising'], expected)
                self.assertEqual(result['count'], expected)

    def test_limit_price_boundary_and_intraday_touch_is_allowed(self):
        for change, expected in [(9.99, 1), (10, 0), (10.01, 0)]:
            with self.subTest(change=change):
                self.prices(change=change)
                result = self.screen()
                self.assertEqual(result['count'], expected)
                self.assertEqual(result['v2_stats']['base_count'], 1)
                self.assertEqual(result['v2_stats']['limit_up'], 1 - expected)
        self.prices()
        self.db.execute('UPDATE stock_daily SET high=110 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['count'], 1)

    def test_limit_price_uses_round_half_up_and_historical_board_rules(self):
        cases = [('000001.SZ', '普通股票', '20260703', 10),
                 ('600001.SH', '*ST股票', '20260703', 5),
                 ('000001.SZ', 'ST股票', '20260703', 5),
                 ('600001.SH', '*ST股票', '20260706', 10),
                 ('000001.SZ', 'ST股票', '20260706', 10),
                 ('300001.SZ', 'ST股票', '20261006', 20),
                 ('688001.SH', '*ST股票', '20261006', 20),
                 ('920001.BJ', '普通股票', '20261006', 30),
                 ('300001.SZ', 'ST股票', '20200821', 5),
                 ('300001.SZ', 'ST股票', '20200824', 20)]
        for code, name, day, pct in cases:
            with self.subTest(code=code, name=name, day=day):
                self.assertEqual(dip._limit_up_price_for(code, name, 100, day), (100 + pct, pct))
        self.assertEqual(dip._limit_up_price_for('000001.SZ', '普通股票', 10.05, self.date), (11.06, 10))
        for code, name in [('999001.SH', '普通股票'), ('000001.SZ', None)]:
            self.assertIsNone(dip._limit_up_price_for(code, name, 100, self.date))
        self.assertIsNone(dip._limit_up_price_for('000001.SZ', '普通股票', None, self.date))

    def test_board_specific_limit_applies_to_selection(self):
        for code, change, expected in [('300001.SZ', 15, 1), ('300001.SZ', 20, 0),
                                       ('688001.SH', 15, 1), ('688001.SH', 20, 0),
                                       ('920001.BJ', 25, 1), ('920001.BJ', 30, 0)]:
            with self.subTest(code=code, change=change):
                self.db.execute('UPDATE stock_daily SET ts_code=?', (code,))
                self.db.execute('UPDATE fund_flow SET ts_code=?', (code,))
                self.db.execute('UPDATE stock_daily SET ts_code="000001.SZ"')
                self.prices(change=change)
                self.db.execute('UPDATE stock_daily SET ts_code=?', (code,))
                self.assertEqual(self.screen()['count'], expected)

    def test_old_three_rules_and_turnover_dependency_are_removed(self):
        for amount in [None, 0, -1, float('inf'), 1e10]:
            with self.subTest(amount=amount):
                self.db.execute('UPDATE stock_daily SET amount=?', (amount,))
                result = self.screen()
                self.assertEqual(result['count'], 1)
                row = result['results'][0]
                self.assertGreater((row['close'] / row['ma20'] - 1) * 100, 5)
                self.assertGreater(row['close'], row['ma60'])
        statements = []
        self.db.set_trace_callback(statements.append)
        self.screen()
        self.db.set_trace_callback(None)
        self.assertFalse(any('amount' in sql.lower() for sql in statements))

    def test_all_ma_comparisons_are_strict(self):
        baseline = [r[0] for r in self.db.execute('SELECT close FROM stock_daily ORDER BY trade_date')]
        variants = []
        closes = baseline.copy()
        closes[116:119] = [108] * 3
        variants.append(('close=ma5', closes))
        closes = baseline.copy()
        closes[111:116] = [100] * 5
        variants.append(('ma5=ma10', closes))
        closes = baseline.copy()
        closes[101:111] = [math.fsum(closes[-10:]) / 10] * 10
        variants.append(('ma10=ma20', closes))
        closes = baseline.copy()
        closes[61:101] = [math.fsum(closes[-20:]) / 20] * 40
        variants.append(('ma20=ma60', closes))
        for name, closes in variants:
            with self.subTest(name=name):
                self.prices(closes=closes)
                result = self.screen()
                self.assertEqual(result['v2_stats']['base_count'], 1)
                self.assertEqual(result['v2_stats']['not_bullish'], 1)
                self.assertEqual(result['count'], 0)
        self.prices(older_close=110)
        self.assertEqual(self.screen()['v2_stats']['not_bullish'], 1)

    def test_missing_limit_reference_excludes_stock(self):
        self.db.execute('UPDATE stock_daily SET name=NULL')
        result = self.screen()
        self.assertEqual(result['v2_stats']['base_count'], 1)
        self.assertEqual(result['v2_stats']['missing_limit'], 1)
        self.assertEqual(result['count'], 0)

    def test_today_negative_funds_still_allowed(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=-1000000 WHERE trade_date=?', (self.date,))
        self.db.execute('UPDATE stock_daily SET amount=40000')
        row = self.screen()['results'][0]
        self.assertEqual(row['main_net_today'], -1_000_000)

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
        self.db.execute('UPDATE overview SET downCount=2600')
        result = self.screen()
        self.assertFalse(result['market_passed'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['v2_stats']['base_count'], 0)
        self.db.execute('UPDATE overview SET downCount=2599')
        self.db.execute('DELETE FROM fund_flow WHERE trade_date=?', (self.days[110],))
        result = self.screen()
        self.assertTrue(result['market_passed'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['stats']['missing_funds'], 1)

    def test_market_count_strict_boundary(self):
        for down, passed in [(0, True), (2599, True), (2600, False), (2601, False)]:
            with self.subTest(down=down):
                self.db.execute('UPDATE overview SET downCount=?', (down,))
                result = self.screen()
                self.assertEqual(result['market_passed'], passed)
                self.assertEqual(result['count'], int(passed))

    def test_market_requires_strictly_positive_index(self):
        for change, passed in [(1e-9, True), (.5, True), (1.6, True), (0, False),
                               (-.001, False), (-1.6, False), (-1.601, False)]:
            with self.subTest(change=change):
                self.db.execute('UPDATE index_daily SET change=? WHERE date=?', (change, self.date))
                result = self.screen()
                self.assertEqual(result['market_passed'], passed)
                self.assertEqual(result['count'], int(passed))

    def test_market_gates_are_independent_and_original_list_unchanged(self):
        self.add_stock('000002.SZ')
        self.db.execute('UPDATE overview SET downCount=2601')
        for change, original_passed in [(-1.599, True), (-1.6, False), (-1.601, True), (.5, False)]:
            self.db.execute('UPDATE index_daily SET change=? WHERE date=?', (change, self.date))
            original = dip.screen_dip(self.db, self.date, self.now)
            self.assertEqual(original['market_passed'], original_passed)
            self.assertFalse(self.screen()['market_passed'])
            self.assertEqual(original, dip.screen_dip(self.db, self.date, self.now))
            if original_passed:
                self.assertEqual(original['count'], 1)
                self.assertIn('ma60', original['results'][0])

    def test_previous_three_days_not_required_or_queried(self):
        for previous in [None, .5, -.5]:
            with self.subTest(previous=previous):
                self.db.execute('UPDATE index_daily SET change=? WHERE date<?', (previous, self.date))
                statements = []
                self.db.set_trace_callback(statements.append)
                result = self.screen()
                self.db.set_trace_callback(None)
                self.assertEqual(result['count'], 1)
                self.assertEqual(result['market']['previous_three'], [])
                self.assertIsNone(result['market']['previous_three_up'])
                index_queries = [sql for sql in statements if 'FROM index_daily' in sql]
                self.assertEqual(len(index_queries), 1)
                self.assertIn(self.date, index_queries[0])

    def test_missing_market_never_uses_another_day(self):
        self.db.execute('DELETE FROM overview')
        self.db.execute('INSERT INTO overview VALUES (?,1000)', (self.days[119],))
        result = self.screen()
        self.assertEqual(result['market']['status'], 'missing')
        self.assertEqual(result['count'], 0)
        self.db.execute('INSERT INTO overview VALUES (?,2599)', (self.date,))
        self.db.execute('DELETE FROM index_daily WHERE date=?', (self.date,))
        result = self.screen()
        self.assertEqual(result['market']['status'], 'missing')
        self.assertEqual(result['count'], 0)

    def test_invalid_market_values_do_not_trigger(self):
        for field, value in [('downCount', None), ('downCount', -1), ('downCount', float('inf')),
                             ('change', None), ('change', float('inf')), ('change', float('nan'))]:
            with self.subTest(field=field, value=value):
                self.db.execute('UPDATE overview SET downCount=2599')
                self.db.execute('UPDATE index_daily SET change=.5 WHERE date=?', (self.date,))
                if field == 'downCount':
                    self.db.execute('UPDATE overview SET downCount=?', (value,))
                else:
                    self.db.execute('UPDATE index_daily SET change=? WHERE date=?', (value, self.date))
                result = self.screen()
                self.assertEqual(result['market']['status'], 'missing')
                self.assertFalse(result['market_passed'])
                self.assertEqual(result['count'], 0)

    def test_iso_market_dates_are_normalized(self):
        self.db.execute("UPDATE overview SET date=substr(date,1,4)||'-'||substr(date,5,2)||'-'||substr(date,7,2)")
        self.db.execute("UPDATE index_daily SET date=substr(date,1,4)||'-'||substr(date,5,2)||'-'||substr(date,7,2)")
        self.assertEqual(self.screen()['count'], 1)

    def test_stock_below_five_percent_does_not_enter_rising_universe(self):
        for change in [0, .5]:
            self.db.execute('UPDATE stock_daily SET change=? WHERE trade_date=?', (change, self.date))
            result = self.screen()
            self.assertTrue(result['market_passed'])
            self.assertEqual(result['count'], 0)
            self.assertEqual(result['stats']['strong_rising'], 0)

    def test_exclusion_counts_account_for_every_base_candidate(self):
        self.prices(change=10, older_close=110)
        stats = self.screen()['v2_stats']
        self.assertEqual(stats['base_count'], 1)
        self.assertEqual(stats['limit_up'], 1)
        self.assertEqual(sum(v for k, v in stats.items() if k != 'base_count'), 1)

    def switched(self, *disabled):
        return dip.screen_dip_v2(self.db, self.date, self.now, disabled=disabled)

    def test_switch_defaults_echo_and_validation(self):
        before = self.screen()
        self.assertEqual(before, self.switched())
        self.assertEqual(len(before['conditions']), 11)
        self.assertTrue(all(before['conditions'].values()))
        self.assertEqual(before['required_history_days'], 121)
        changed = self.switched('volume', 'amplitude')
        self.assertFalse(changed['conditions']['volume'])
        self.assertFalse(changed['conditions']['amplitude'])
        self.assertTrue(changed['conditions']['bullish'])
        for invalid in ['unknown', 'volume,unknown', '__proto__']:
            with self.assertRaises(ValueError):
                dip.screen_dip_v2(self.db, self.date, self.now, disabled=invalid)
        self.assertEqual(self.screen(), before)

    def test_independent_market_switches_and_missing_dependencies(self):
        self.db.execute('UPDATE overview SET downCount=2600')
        self.assertFalse(self.screen()['market_passed'])
        self.assertEqual(self.switched('market_down')['count'], 1)
        self.db.execute('DELETE FROM overview')
        self.assertEqual(self.switched('market_down')['count'], 1)
        self.assertFalse(self.switched('market_up')['market_passed'])
        self.db.execute('UPDATE index_daily SET change=-1 WHERE date=?', (self.date,))
        self.assertFalse(self.switched('market_down')['market_passed'])
        self.assertEqual(self.switched('market_down', 'market_up')['count'], 1)
        self.db.execute('DELETE FROM index_daily')
        self.assertEqual(self.switched('market_down', 'market_up')['count'], 1)
        self.db.execute('INSERT INTO overview VALUES (?,2599)', (self.date,))
        self.assertEqual(self.switched('market_up')['count'], 1)
        self.assertFalse(self.switched('market_down')['market_passed'])

    def test_independent_gain_return_volume_amplitude_limit_and_bullish_switches(self):
        for key in ['gain', 'return120', 'volume', 'amplitude', 'not_limit', 'bullish']:
            with self.subTest(key=key):
                self.prices()
                self.db.execute('UPDATE stock_daily SET volume=100')
                if key == 'gain':
                    self.prices(change=4)
                elif key == 'return120':
                    self.prices(older_close=20)
                elif key == 'volume':
                    self.db.execute('UPDATE stock_daily SET volume=200 WHERE trade_date=?', (self.date,))
                elif key == 'amplitude':
                    self.db.execute('UPDATE stock_daily SET high=close*1.001,low=close*.999')
                elif key == 'not_limit':
                    self.prices(change=10)
                else:
                    self.prices(older_close=110)
                self.assertEqual(self.screen()['count'], 0)
                result = self.switched(key)
                self.assertEqual(result['count'], 1)
                self.assertEqual(self.screen()['count'], 0)

    def test_above_ma20_switch_and_bullish_overlap(self):
        self.prices(closes=[130] * 119 + [100, 106])
        self.assertEqual(self.switched('bullish')['count'], 0)
        self.assertEqual(self.switched('above_ma20')['count'], 0)
        result = self.switched('above_ma20', 'bullish')
        self.assertEqual(result['count'], 1)
        self.assertGreater(result['results'][0]['ma20'], result['results'][0]['close'])

    def test_fund_switches_are_independent(self):
        # Short sums can be negative while the 20-day sum still exceeds 50 million yuan.
        self.db.execute('UPDATE fund_flow SET main_net_inflow=4000000')
        self.db.execute('UPDATE fund_flow SET main_net_inflow=-10000000 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['stats']['funds_not_red'], 1)
        self.assertEqual(self.switched('funds_red')['count'], 1)
        self.assertEqual(self.switched('funds_threshold')['count'], 0)
        self.db.execute('UPDATE fund_flow SET main_net_inflow=1000000')
        self.assertEqual(self.screen()['stats']['funds_below_threshold'], 1)
        self.assertEqual(self.switched('funds_threshold')['count'], 1)
        self.assertEqual(self.switched('funds_red')['count'], 0)

    def test_disabled_funds_allow_missing_windows_without_zero_fill(self):
        self.db.execute('DELETE FROM fund_flow WHERE trade_date=?', (self.days[110],))
        self.assertEqual(self.switched('funds_red')['count'], 0)
        self.assertEqual(self.switched('funds_threshold')['count'], 0)
        result = self.switched('funds_red', 'funds_threshold')
        row = result['results'][0]
        self.assertEqual(result['stats']['missing_funds'], 0)
        self.assertEqual(result['stats']['funds_display_missing'], 1)
        self.assertEqual(row['main_net_5d'], 15_000_000)
        self.assertIsNone(row['main_net_20d'])
        self.db.execute('DELETE FROM fund_flow')
        result = self.switched('funds_red', 'funds_threshold')
        self.assertEqual(result['count'], 1)
        self.assertTrue(all(result['results'][0][key] is None for key in
                            ('main_net_today', 'main_net_3d', 'main_net_5d', 'main_net_10d', 'main_net_20d')))

    def test_history_dependencies_shrink_only_for_disabled_rules(self):
        self.db.execute('DELETE FROM stock_daily WHERE trade_date<?', (self.days[61],))
        self.assertEqual(self.screen()['count'], 0)
        result = self.switched('return120')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['required_history_days'], 60)
        self.assertIsNone(result['results'][0]['ret120'])
        self.db.execute('DELETE FROM stock_daily WHERE trade_date<?', (self.days[100],))
        self.assertEqual(self.switched('return120')['count'], 0)
        result = self.switched('return120', 'bullish')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['required_history_days'], 21)
        self.assertIsNone(result['results'][0]['ma60'])

    def test_all_disabled_accepts_only_valid_same_day_without_long_history(self):
        self.prices(change=-1)
        self.db.execute('DELETE FROM stock_daily WHERE trade_date<?', (self.date,))
        self.db.execute('DELETE FROM fund_flow')
        self.db.execute('DELETE FROM overview')
        self.db.execute('DELETE FROM index_daily')
        self.db.execute('DELETE FROM trading_dates_cache WHERE cal_date<?', (self.date,))
        result = self.switched(*dip.CHASE_CONDITIONS)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['required_history_days'], 1)
        row = result['results'][0]
        self.assertLess(row['change_pct'], 0)
        self.assertIsNone(row['ma20'])
        self.assertIsNone(row['limit_up_price'])
        self.db.execute('UPDATE stock_daily SET low=0')
        result = self.switched(*dip.CHASE_CONDITIONS)
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['stats']['invalid_prices'], 1)

    def test_disabled_old_window_does_not_hide_required_reference_checks(self):
        self.db.execute('UPDATE stock_daily SET change=999 WHERE trade_date=?', (self.days[110],))
        result = self.switched('return120')
        self.assertEqual(result['count'], 0)
        self.assertEqual(result['stats']['reference_mismatch'], 1)
        self.db.execute('UPDATE stock_daily SET change=999 WHERE trade_date=?', (self.days[50],))
        self.prices()
        self.db.execute('UPDATE stock_daily SET change=999 WHERE trade_date=?', (self.days[50],))
        self.assertEqual(self.screen()['count'], 0)
        self.assertEqual(self.switched('return120')['count'], 1)

    def test_disabled_rules_still_isolate_future_selection(self):
        before = self.switched('volume', 'amplitude', 'bullish')
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('000001.SZ', '未来', self.days[121], 1000, 1200, 900, 1000, 900, 100, 1e12))
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)',
                        ('000001.SZ', self.days[121], 'eastmoney-push2', -1e12))
        after = self.switched('volume', 'amplitude', 'bullish')
        for key in ['results', 'count', 'market', 'stats', 'v2_stats', 'conditions']:
            self.assertEqual(before[key], after[key])

    def condition_counts(self, date=None, now=None):
        return dip.chase_condition_counts(self.db, date or self.date, now or self.now)

    def test_condition_counts_all_keys_and_default_predicates(self):
        result = self.condition_counts()
        self.assertEqual(result['total'], 1)
        self.assertEqual(set(result['counts']), set(dip.CHASE_CONDITIONS))
        for key, count in result['counts'].items():
            with self.subTest(key=key):
                self.assertEqual(count, dict(total=1, evaluated=1, missing=0, matched=1))
        self.assertNotIn('results', result)

    def test_condition_counts_are_independent_of_market_and_other_stock_rules(self):
        self.add_stock('000002.SZ')
        self.db.execute('UPDATE stock_daily SET volume=200 WHERE ts_code="000002.SZ" AND trade_date=?', (self.date,))
        self.db.execute('UPDATE overview SET downCount=2600')
        result = self.condition_counts()
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['counts']['market_down']['matched'], 0)
        self.assertEqual(result['counts']['market_up']['matched'], 2)
        self.assertEqual(result['counts']['gain']['matched'], 1)
        self.assertEqual(result['counts']['volume']['matched'], 1)
        self.assertEqual(result['counts']['return120']['matched'], 2)
        self.assertEqual(self.screen()['count'], 0)
        before = result['counts']
        self.switched('gain', 'funds_red', 'volume', 'market_down')
        self.assertEqual(before, self.condition_counts()['counts'])

    def test_condition_counts_strict_boundaries_match_selection(self):
        for change, matched in [(5, 0), (5.001, 1), (10, 1)]:
            self.prices(change=change)
            counts = self.condition_counts()['counts']
            self.assertEqual(counts['gain']['matched'], matched)
            self.assertEqual(counts['not_limit']['matched'], int(change < 10))
        self.prices(older_close=26.5)
        self.assertEqual(self.condition_counts()['counts']['return120']['matched'], 0)
        self.prices()
        for ratio, matched in [(1.2, 0), (1.199, 1)]:
            self.db.execute('UPDATE stock_daily SET volume=? WHERE trade_date=?', (100 * ratio, self.date))
            self.assertEqual(self.condition_counts()['counts']['volume']['matched'], matched)
        previous = 80
        for day, close in self.db.execute('SELECT trade_date,close FROM stock_daily ORDER BY trade_date').fetchall():
            self.db.execute('UPDATE stock_daily SET high=?,low=? WHERE trade_date=?',
                            (close + previous * .025, close - previous * .025, day))
            previous = close
        self.assertEqual(self.condition_counts()['counts']['amplitude']['matched'], 1)

    def test_condition_counts_funds_are_independent_and_never_use_today_threshold(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=4000000')
        self.db.execute('UPDATE fund_flow SET main_net_inflow=-10000000 WHERE trade_date=?', (self.date,))
        counts = self.condition_counts()['counts']
        self.assertEqual(counts['funds_red']['matched'], 0)
        self.assertEqual(counts['funds_threshold']['matched'], 1)
        self.db.execute('UPDATE fund_flow SET main_net_inflow=1000000')
        counts = self.condition_counts()['counts']
        self.assertEqual(counts['funds_red']['matched'], 1)
        self.assertEqual(counts['funds_threshold']['matched'], 0)
        self.db.execute('UPDATE fund_flow SET main_net_inflow=3000000')
        self.db.execute('UPDATE fund_flow SET main_net_inflow=-1000000 WHERE trade_date=?', (self.date,))
        counts = self.condition_counts()['counts']
        self.assertEqual(counts['funds_red']['matched'], 1)
        self.assertEqual(counts['funds_threshold']['matched'], 1)

    def test_condition_counts_missing_data_not_zero_matching(self):
        self.db.execute('DELETE FROM fund_flow WHERE trade_date=?', (self.days[110],))
        self.db.execute('DELETE FROM stock_daily WHERE trade_date=?', (self.days[0],))
        counts = self.condition_counts()['counts']
        for key in ['funds_red', 'funds_threshold', 'return120']:
            self.assertEqual(counts[key], dict(total=1, evaluated=0, missing=1, matched=0))
        for key in ['gain', 'volume', 'above_ma20', 'amplitude', 'not_limit', 'bullish']:
            self.assertEqual(counts[key]['matched'], 1)
        self.db.execute('DELETE FROM overview')
        counts = self.condition_counts()['counts']
        self.assertEqual(counts['market_down']['evaluated'], 0)
        self.assertEqual(counts['market_up']['matched'], 1)

    def test_condition_counts_short_calendar_and_price_quality_are_per_window(self):
        self.db.execute('DELETE FROM trading_dates_cache WHERE cal_date<?', (self.days[100],))
        counts = self.condition_counts()['counts']
        self.assertEqual(counts['return120']['evaluated'], 0)
        self.assertEqual(counts['bullish']['evaluated'], 0)
        self.assertEqual(counts['amplitude']['matched'], 1)
        self.assertEqual(counts['gain']['matched'], 1)
        self.db.execute('UPDATE stock_daily SET high=0 WHERE trade_date=?', (self.date,))
        counts = self.condition_counts()['counts']
        for key in dip.CHASE_CONDITIONS[2:]:
            self.assertEqual(counts[key]['evaluated'], 0)
        self.assertEqual(counts['market_up']['matched'], 1)

    def test_condition_counts_old_reference_error_does_not_hide_shorter_rules(self):
        self.db.execute('UPDATE stock_daily SET change=999 WHERE trade_date=?', (self.days[50],))
        counts = self.condition_counts()['counts']
        self.assertEqual(counts['return120']['evaluated'], 0)
        self.assertEqual(counts['bullish']['matched'], 1)
        self.assertEqual(counts['gain']['matched'], 1)
        self.db.execute('UPDATE stock_daily SET change=999 WHERE trade_date=?', (self.date,))
        counts = self.condition_counts()['counts']
        self.assertEqual(counts['gain']['evaluated'], 0)
        self.assertEqual(counts['not_limit']['evaluated'], 0)
        self.assertEqual(counts['funds_red']['matched'], 1)

    def test_condition_counts_future_isolation_and_closed_day_readiness(self):
        before = self.condition_counts()
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('000001.SZ', '未来', self.days[121], 1000, 1200, 900, 1000, 900, 100, 1e12))
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)',
                        ('000001.SZ', self.days[121], 'eastmoney-push2', -1e12))
        after = self.condition_counts()
        for key in ['date', 'daily_ready', 'total', 'market', 'counts']:
            self.assertEqual(before[key], after[key])
        from datetime import datetime
        waiting = self.condition_counts(now=datetime.strptime(self.date + ' 12', '%Y%m%d %H'))
        self.assertFalse(waiting['daily_ready'])
        self.assertTrue(all(c['evaluated'] == 0 for c in waiting['counts'].values()))

    def test_condition_counts_route_is_read_only_and_never_loads_outcomes(self):
        self.db.commit()
        app = Flask(__name__)
        def no_future(*args, **kwargs):
            self.fail('Counts must not read outcomes')
        dip.register_dip_picks(app, lambda: '/tmp/not-used.db', outcome_loader=no_future)
        statements = []
        self.db.set_trace_callback(statements.append)
        with patch.object(dip.sqlite3, 'connect', return_value=self.db) as connect:
            response = app.test_client().get('/api/picks/dip-v2/condition-counts?date=' + self.date + '&disabled=gain,volume')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['counts']['gain']['matched'], 1)
            self.assertNotIn('next_dates', response.json)
            self.assertIn('mode=ro', connect.call_args.args[0])
            self.db.rollback()
            self.assertEqual(app.test_client().get('/api/picks/dip-v2/condition-counts?date=bad').status_code, 400)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.db.execute('PRAGMA query_only').fetchone()[0], 1)
        self.assertFalse(any(sql.split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER') for sql in statements))
        with patch.object(dip.sqlite3, 'connect', side_effect=sqlite3.OperationalError('private')):
            response = app.test_client().get('/api/picks/dip-v2/condition-counts')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private', response.text)

    def test_route_switch_query_validation_and_original_isolation(self):
        self.db.execute('UPDATE stock_daily SET volume=200 WHERE trade_date=?', (self.date,))
        self.db.commit()
        app = Flask(__name__)
        dip.register_dip_picks(app, lambda: '/tmp/not-used.db')
        with patch.object(dip.sqlite3, 'connect', return_value=self.db):
            response = app.test_client().get('/api/picks/dip-v2?date=' + self.date + '&disabled=volume')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['count'], 1)
            self.assertFalse(response.json['conditions']['volume'])
            self.db.rollback()
            invalid = app.test_client().get('/api/picks/dip-v2?disabled=unknown')
            self.assertEqual(invalid.status_code, 400)
            self.db.rollback()
            original = app.test_client().get('/api/picks/dip?date=' + self.date + '&disabled=market_down')
            self.assertFalse(original.json['market_passed'])
            self.assertFalse(original.json['conditions']['market_down'])
            self.assertTrue(original.json['conditions']['market_index'])
            self.assertNotIn('market_up', original.json['conditions'])
        self.assertEqual(self.db.execute('PRAGMA query_only').fetchone()[0], 1)

    def test_route_read_only_outcomes_and_errors(self):
        self.db.commit()
        app = Flask(__name__)
        loaded = []

        def outcomes(rows, day, conn, key):
            loaded.append((len(rows), day, key, rows[0]['limit_up_price']))
            rows[0][key] = [{'date': self.days[121], 'high_sum_pct': 10, 'close_sum_pct': -1}]
            return self.days[121:126], 0

        dip.register_dip_picks(app, lambda: '/tmp/not-used.db', outcome_loader=outcomes)
        statements = []
        self.db.set_trace_callback(statements.append)
        with patch.object(dip.sqlite3, 'connect', return_value=self.db) as connect:
            response = app.test_client().get('/api/picks/dip-v2?date=' + self.date)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['results'][0]['dip_outcomes'][0]['close_sum_pct'], -1)
            self.assertEqual(loaded, [(1, self.date, 'dip_outcomes', 110)])
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
