"""Counterfactuals must equal real selector lists, without future data or writes."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from threading import Event
from unittest.mock import patch

from flask import Flask
import dip_picks as dip
import test_dip_picks as dip_fixtures
import test_dip_picks_v2 as chase_fixtures


class DiagnosticTests(unittest.TestCase):
    setUp = dip_fixtures.DipTests.setUp
    tearDown = dip_fixtures.DipTests.tearDown
    add_stock = dip_fixtures.DipTests.add_stock
    prices = chase_fixtures.DipV2Tests.prices

    def diagnose(self, rising=False, **options):
        return (dip.chase_diagnostics if rising else dip.dip_diagnostics)(
            self.db, self.date, self.now, **options)

    def test_count_and_diagnostic_scans_do_not_overlap(self):
        first_started, second_attempted, second_started, release = (Event() for _ in range(4))

        def build(conn, *args):
            if conn == 'first':
                first_started.set()
                release.wait(2)
            else:
                second_started.set()
            return conn

        def second():
            second_attempted.set()
            return dip._condition_snapshot('second')

        with patch.object(dip, '_build_condition_snapshot', side_effect=build), ThreadPoolExecutor(2) as pool:
            first = pool.submit(dip._condition_snapshot, 'first')
            try:
                self.assertTrue(first_started.wait(1))
                later = pool.submit(second)
                self.assertTrue(second_attempted.wait(1))
                self.assertFalse(second_started.wait(.05))
            finally:
                release.set()
            self.assertEqual(first.result(1), 'first')
            self.assertEqual(later.result(1), 'second')

    def test_scan_exception_releases_other_requests(self):
        with patch.object(dip, '_build_condition_snapshot', side_effect=[ValueError('invalid date'), ({}, [])]):
            with self.assertRaises(ValueError):
                dip._condition_snapshot(None)
            with ThreadPoolExecutor(1) as pool:
                self.assertEqual(pool.submit(dip._condition_snapshot, None).result(1), ({}, []))

    def make_chase(self):
        self.db.execute('UPDATE overview SET downCount=2599')
        self.db.execute('UPDATE index_daily SET change=.5 WHERE date=?', (self.date,))
        self.prices()

    def assert_real_combinations(self, rising=False, disabled=()):
        screen = dip.screen_dip_v2 if rising else dip.screen_dip
        result = self.diagnose(rising, disabled=disabled)
        actual = screen(self.db, self.date, self.now, disabled=disabled)
        self.assertEqual(result['current_count'], actual['count'])
        for item in result['singles'] + result['pairs']:
            configuration = set(disabled) | set(item['remove'])
            actual = screen(self.db, self.date, self.now, disabled=configuration)
            self.assertEqual(item['count'], actual['count'], (rising, configuration))
            preview = self.diagnose(rising, disabled=disabled, remove=item['remove'])['preview']
            self.assertEqual(preview['count'], item['count'])
            self.assertEqual([row['ts_code'] for row in preview['rows']],
                             [row['ts_code'] for row in actual['results']][:50])
        return result

    def test_dip_default_and_each_single_match_real_selection(self):
        result = self.assert_real_combinations()
        self.assertEqual(result['kind'], 'selected')
        self.assertEqual(len(result['singles']), 10)
        self.assertFalse(result['pairs_checked'])
        self.assertIsNone(result['preview'])

    def test_chase_default_and_each_single_match_real_selection(self):
        self.make_chase()
        result = self.assert_real_combinations(True)
        self.assertEqual(result['current_count'], 1)
        self.assertEqual(len(result['singles']), 11)

    def test_dip_single_blocker_preview_explains_actual_value(self):
        self.db.execute('UPDATE stock_daily SET volume=120 WHERE trade_date=?', (self.date,))
        result = self.assert_real_combinations()
        self.assertEqual(result['kind'], 'single')
        positive = [item for item in result['singles'] if item['count']]
        self.assertEqual(positive, [dict(remove=['volume'], count=1)])
        preview = self.diagnose(remove='volume')['preview']['rows'][0]
        self.assertAlmostEqual(preview['volume_ratio5'], 1.2)
        self.assertEqual(preview['removed_states'], {'volume': 'unmet'})

    def test_dip_two_numeric_blockers_exhaustive_pairs_match_real_selection(self):
        self.db.execute('UPDATE stock_daily SET volume=120 WHERE trade_date=?', (self.date,))
        self.db.execute('UPDATE stock_daily SET high=close+1,low=close-1')
        result = self.assert_real_combinations()
        self.assertEqual(result['kind'], 'pair')
        self.assertTrue(result['pairs_checked'])
        self.assertEqual(len(result['pairs']), len(list(combinations(dip.DIP_CONDITIONS, 2))))
        self.assertEqual([item for item in result['pairs'] if item['count']],
                         [dict(remove=['volume', 'amplitude'], count=1)])

    def test_chase_limit_and_volume_pair_matches_real_selection(self):
        self.make_chase()
        self.prices(change=10)
        self.db.execute('UPDATE stock_daily SET volume=120 WHERE trade_date=?', (self.date,))
        result = self.assert_real_combinations(True)
        self.assertEqual(result['kind'], 'pair')
        self.assertEqual([item for item in result['pairs'] if item['count']],
                         [dict(remove=['volume', 'not_limit'], count=1)])

    def test_market_failure_can_still_preview_and_ignore_current_disabled(self):
        self.db.execute('UPDATE index_daily SET change=.5 WHERE date=?', (self.date,))
        result = self.assert_real_combinations(disabled={'volume'})
        self.assertEqual(result['kind'], 'market')
        self.assertEqual(result['market_blockers'][0]['key'], 'market_index')
        self.assertNotIn('volume', [item['remove'][0] for item in result['singles']])
        row = self.diagnose(disabled='volume', remove='market_index')['preview']['rows'][0]
        self.assertEqual(row['removed_states'], {'market_index': 'unmet'})

    def test_missing_market_and_index_branch_are_not_numeric_failure(self):
        self.db.execute('DELETE FROM overview')
        result = self.assert_real_combinations()
        self.assertEqual(result['market_blockers'][0]['state'], 'missing')
        self.db.execute('INSERT INTO overview VALUES (?,2601)', (self.date,))
        self.db.execute('DELETE FROM index_daily WHERE date<?', (self.date,))
        self.assertEqual(self.diagnose()['market_blockers'][0]['state'], 'missing')
        self.db.execute('UPDATE index_daily SET change=-1.601')
        self.assertEqual(self.diagnose()['current_count'], 1)
        self.db.execute('UPDATE index_daily SET change=-1.6')
        self.assertEqual(self.diagnose()['market_blockers'][0]['state'], 'unmet')

    def test_missing_funds_need_two_removed_rules_and_are_not_zero(self):
        self.db.execute('DELETE FROM fund_flow')
        result = self.assert_real_combinations()
        self.assertEqual(result['data_summary']['missing_stock_count'], 1)
        self.assertEqual(result['data_summary']['only_missing_count'], 1)
        self.assertEqual([item for item in result['pairs'] if item['count']],
                         [dict(remove=['funds_red', 'funds_threshold'], count=1)])
        row = self.diagnose(remove='funds_red,funds_threshold')['preview']['rows'][0]
        self.assertIsNone(row['main_net_20d'])
        self.assertEqual(set(row['removed_states'].values()), {'missing'})

    def test_shorter_history_only_after_its_rule_is_removed(self):
        self.db.execute('DELETE FROM stock_daily WHERE trade_date=?', (self.days[50],))
        result = self.assert_real_combinations()
        self.assertEqual([item for item in result['singles'] if item['count']],
                         [dict(remove=['return120'], count=1)])
        row = self.diagnose(remove='return120')['preview']['rows'][0]
        self.assertIsNone(row['ret120'])
        self.assertEqual(row['removed_states']['return120'], 'missing')

    def test_all_disabled_still_require_valid_today_prices(self):
        disabled = set(dip.DIP_CONDITIONS)
        self.db.execute('DELETE FROM fund_flow')
        result = self.assert_real_combinations(disabled=disabled)
        self.assertEqual(result['current_count'], 1)
        self.assertEqual(result['singles'], [])
        self.db.execute('UPDATE stock_daily SET high=0 WHERE trade_date=?', (self.date,))
        result = self.diagnose(disabled=disabled)
        self.assertEqual(result['current_count'], 0)
        self.assertEqual(result['data_summary']['invalid_daily_count'], 1)
        self.assertFalse(any(item['count'] for item in result['pairs']))

    def test_more_than_two_data_requirements_do_not_fake_an_explanation(self):
        self.db.execute('DELETE FROM stock_daily WHERE trade_date<?', (self.date,))
        self.db.execute('DELETE FROM fund_flow')
        result = self.assert_real_combinations()
        self.assertEqual(result['kind'], 'missing')
        self.assertTrue(all(item['count'] == 0 for item in result['singles'] + result['pairs']))
        self.assertIn('一项或两项', result['message'])

    def test_preview_pagination_complete_order_and_limits(self):
        for i in range(2, 7):
            self.add_stock(f'{i:06d}.SZ')
        self.db.execute('UPDATE stock_daily SET volume=150 WHERE trade_date=?', (self.date,))
        first = self.diagnose(remove='volume', limit=2)['preview']
        next_page = self.diagnose(remove='volume', offset=2, limit=2)['preview']
        self.assertEqual(first['count'], 6)
        self.assertEqual([r['ts_code'] for r in first['rows']], ['000001.SZ', '000002.SZ'])
        self.assertEqual([r['ts_code'] for r in next_page['rows']], ['000003.SZ', '000004.SZ'])
        self.assertEqual(self.diagnose(remove='volume', offset=10)['preview']['rows'], [])

    def test_waiting_and_future_isolation(self):
        before = self.diagnose(remove='volume')
        self.db.execute('INSERT INTO stock_daily SELECT ts_code,name,?,open,high,low,close,change,volume,amount FROM stock_daily WHERE trade_date=?',
                        (self.days[121], self.date))
        after = self.diagnose(remove='volume')
        before.pop('latest_closed_date'); after.pop('latest_closed_date')
        self.assertEqual(before, after)
        result = dip.dip_diagnostics(self.db, '20261002', self.now.replace(hour=10))
        self.assertEqual(result['kind'], 'waiting')
        self.assertEqual(result['singles'], [])

    def test_immutable_configuration_validation(self):
        disabled = {'volume'}
        removed = {'amplitude'}
        self.diagnose(disabled=disabled, remove=removed)
        self.assertEqual(disabled, {'volume'})
        self.assertEqual(removed, {'amplitude'})
        for options in [dict(disabled='unknown'), dict(remove='unknown'),
                        dict(disabled='volume', remove='volume'),
                        dict(remove='volume,amplitude,return120'), dict(offset=-1),
                        dict(limit=0), dict(limit=101), dict(offset='oops')]:
            with self.assertRaises(ValueError):
                self.diagnose(**options)

    def test_routes_query_only_no_enrichment_and_error_contract(self):
        self.db.commit()
        app = Flask(__name__)
        enriched = []
        dip.register_dip_picks(app, lambda: '/tmp/not-used.db', lambda *a, **k: enriched.append(a))
        sql = []
        self.db.set_trace_callback(sql.append)
        with patch.object(dip.sqlite3, 'connect', return_value=self.db) as connected:
            for route in ['/api/picks/dip/diagnostics', '/api/picks/dip-v2/diagnostics']:
                response = app.test_client().get(route + '?date=' + self.date + '&remove=volume&limit=2')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertNotIn('next_dates', response.json)
                self.assertTrue(connected.call_args.args[0].endswith('?mode=ro'))
                self.db.rollback()
            self.assertEqual(app.test_client().get('/api/picks/dip/diagnostics?remove=unknown').status_code, 400)
            self.db.rollback()
        self.assertEqual(enriched, [])
        self.assertEqual(self.db.execute('PRAGMA query_only').fetchone()[0], 1)
        self.assertFalse(any(statement.split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER') for statement in sql))


if __name__ == '__main__':
    unittest.main()
