"""Execution and causality checks for the isolated T+5 research script.

Fixtures use temporary SQLite databases and arrays; production market data are
never opened. These tests assert trading constraints, not candidate profitability.
"""
import contextlib
import copy
import importlib.util
import io
import pickle
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


SCRIPT = Path(__file__).parent / 'scripts' / 'research_t5_20261008.py'
SPEC = importlib.util.spec_from_file_location('tradeiq_t5_research', SCRIPT)
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def market_fixture(days=15, stocks=2, price=100.):
    dates = pd.bdate_range('2026-06-01', periods=days).strftime('%Y%m%d').tolist()
    shape = (days, stocks)
    a = {key: np.full(shape, price, dtype=np.float64)
         for key in ['open', 'high', 'low', 'close']}
    a['high'] *= 1.01
    a['low'] *= .99
    a.update(change=np.zeros(shape), volume=np.full(shape, 1000.),
             amount=np.full(shape, 1_000_000.))
    return {'dates': dates, 'codes': [f'{600000 + j:06d}.SH' for j in range(stocks)],
            'names': np.full(shape, '历史股票名', dtype=object), 'a': a,
            'lim': np.full(shape, .1), 'reference_ok': np.ones(shape, dtype=bool),
            'valid': np.ones(shape, dtype=bool), 'base': np.ones(shape, dtype=bool)}


def fixed_selector(mask, score=None):
    score = np.ones(mask.shape) if score is None else score
    return lambda data, cfg: (mask.copy(), score.copy())


def ridge_fixture():
    dates = pd.bdate_range('2025-06-24', '2026-06-15').strftime('%Y%m%d').tolist()
    data = market_fixture(days=len(dates), stocks=3)
    data['dates'] = dates
    shape = data['base'].shape
    rng = np.random.default_rng(20261008)
    close = 100 + np.arange(len(dates))[:, None] * .1 + np.arange(3)[None, :]
    data['a']['close'] = close
    data['a']['open'] = np.concatenate([close[:1], close[:-1]])
    data['a']['high'] = close * 1.01
    data['a']['low'] = close * .99
    keys = ['day', 'ret3', 'ret5', 'ret10', 'ret20', 'bias5', 'bias20',
            'amp20', 'vr', 'rsi2', 'rsi14', 'clv', 'streak', 'rs20', 'atr14',
            'main1', 'main3', 'main5', 'super5', 'small5', 'sector_main_pct5',
            'industry_rs20']
    data['f'] = {name: rng.normal(size=shape) for name in keys}
    data['f']['amount'] = rng.uniform(1, 10, shape)
    data['f']['main5'] = rng.uniform(1, 10, shape)
    data['market'] = {name: rng.normal(size=len(dates)) for name in
                      ['breadth', 'index_ret20', 'index_up_count', 'market_main5_pct']}
    return data


def prepare_fixture(path, changed_future=False):
    """Minimum schema accepted by prepare, with two complete historical stocks."""
    dates = pd.bdate_range('2025-04-01', periods=85).strftime('%Y%m%d').tolist()
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE stock_daily(ts_code TEXT, trade_date TEXT, name TEXT,
                open REAL, high REAL, low REAL, close REAL, change REAL,
                volume REAL, amount REAL);
            CREATE TABLE trading_dates_cache(cal_date TEXT);
            CREATE TABLE fund_flow(trade_date TEXT, ts_code TEXT,
                main_net_inflow REAL, super_net REAL, big_net REAL,
                small_net REAL, source TEXT);
            CREATE TABLE index_daily(date TEXT, name TEXT, close REAL, change REAL);
            CREATE TABLE limitup(date TEXT, code TEXT, sector TEXT);
            CREATE TABLE rzrq_history_summary(dim_date TEXT, rzye REAL, rzjme REAL);
        ''')
        conn.executemany('INSERT INTO trading_dates_cache VALUES (?)', [(d,) for d in dates])
        rows, funds = [], []
        for j, code in enumerate(['600001.SH', '600002.SH']):
            previous = 20. + j
            for i, date in enumerate(dates):
                rate = -.04 if i in [35, 60, 75] else .01
                if changed_future and i >= 60:
                    rate = -.07 if i % 2 else .09
                close = previous * (1 + rate)
                rows.append((code, date, '历史股票名', previous,
                             max(previous, close) * 1.04,
                             min(previous, close) * .98,
                             close, rate * 100, 1000., 200000.))
                funds.append((date, code, 100000., 60000., 40000., -100000.,
                              'eastmoney-daykline' if i < 50 else 'eastmoney-push2'))
                previous = close
        conn.executemany('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
        conn.executemany('INSERT INTO fund_flow VALUES (?,?,?,?,?,?,?)', funds)
        conn.executemany('INSERT INTO index_daily VALUES (?,?,?,?)',
                         [(d, '上证指数', 3000. + i, 100 / (2999. + i))
                          for i, d in enumerate(dates)])
        conn.executemany('INSERT INTO rzrq_history_summary VALUES (?,?,?)',
                         [(d, 100000. + i, i) for i, d in enumerate(dates)])
    return dates


class TestT5ReadOnlyAndFeatures(unittest.TestCase):
    def test_sqlite_connection_is_read_only_and_query_only(self):
        with tempfile.TemporaryDirectory(prefix='tradeiq-t5-20261008-test-') as tmp:
            path = Path(tmp) / 'fixture.db'
            with sqlite3.connect(path) as writer:
                writer.execute('CREATE TABLE prices(value REAL)')
                writer.execute('INSERT INTO prices VALUES (10)')
            conn = research.open_db(path)
            try:
                self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
                self.assertTrue(conn.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute('UPDATE prices SET value=20')
                self.assertEqual(conn.execute('SELECT value FROM prices').fetchone()[0], 10)
            finally:
                conn.close()
            with self.assertRaises(sqlite3.OperationalError):
                research.open_db(Path(tmp) / 'absent.db')
            self.assertFalse((Path(tmp) / 'absent.db').exists())

    def test_future_prices_do_not_change_prior_features_or_signals(self):
        real_open = research.open_db
        prepared = []
        with tempfile.TemporaryDirectory(prefix='tradeiq-t5-20261008-test-') as tmp:
            for variant in [False, True]:
                db = Path(tmp) / f'fixture-{variant}.db'
                prepare_fixture(db, changed_future=variant)
                cache = Path(tmp) / f'cache-{variant}.pkl'
                with patch.object(research, 'open_db', lambda p=db: real_open(p)), \
                        patch.object(research, 'CACHE', cache), \
                        contextlib.redirect_stdout(io.StringIO()):
                    research.prepare()
                with cache.open('rb') as file:
                    prepared.append(pickle.load(file))
            before, after = prepared
            for feature in before['f']:
                np.testing.assert_allclose(before['f'][feature][:60], after['f'][feature][:60],
                                           equal_nan=True, err_msg=feature)
            config = {'family': 'pullback', 'momentum': 15, 'volmax': 1.2,
                      'ma': 20, 'rank': 'price', 'k': 3, 'gapmax': 5}
            a_mask, a_score = research.signals(before, config)
            b_mask, b_score = research.signals(after, config)
            self.assertTrue(a_mask[:60].any(), 'Fixture must include an actual prior signal')
            np.testing.assert_array_equal(a_mask[:60], b_mask[:60])
            np.testing.assert_allclose(a_score[:60], b_score[:60], equal_nan=True)

    def test_long_breakout_rejects_invalid_reference_in_older_history(self):
        real_open = research.open_db
        with tempfile.TemporaryDirectory(prefix='tradeiq-t5-20261008-test-') as tmp:
            db = Path(tmp) / 'fixture.db'
            prepare_fixture(db)
            with sqlite3.connect(db) as conn:
                bad_day = conn.execute('SELECT DISTINCT trade_date FROM stock_daily '
                                       'ORDER BY trade_date LIMIT 1 OFFSET 10').fetchone()[0]
                conn.execute('UPDATE stock_daily SET change=50 WHERE trade_date=?', (bad_day,))
            cache = Path(tmp) / 'cache.pkl'
            with patch.object(research, 'open_db', lambda: real_open(db)), \
                    patch.object(research, 'CACHE', cache), \
                    contextlib.redirect_stdout(io.StringIO()):
                research.prepare()
            with cache.open('rb') as file:
                data = pickle.load(file)
            self.assertTrue(data['base'][65].all(), 'Recent 20-day history remains eligible')
            self.assertTrue(np.isnan(data['f']['break60'][65]).all())
            self.assertTrue(np.isnan(data['f']['ma60'][65]).all())


class TestT5Execution(unittest.TestCase):
    def test_t_plus_five_uses_exchange_calendar(self):
        data = market_fixture()
        data['dates'] = ['20260601', '20260602', '20260603', '20260605',
                         '20260608', '20260609'] + data['dates'][6:]
        row = research.planned_outcome(data, 0, 0, len(data['dates']) - 1)
        self.assertEqual(row['entry'], '20260602')
        self.assertEqual(row['planned_exit'], '20260609')
        self.assertEqual(row['exit'], '20260609')
        self.assertEqual(row['status'], 'executed')

    def test_insufficient_future_is_not_mature(self):
        data = market_fixture()
        row = research.planned_outcome(data, 10, 0, 14)
        self.assertEqual(row['status'], 'not_mature')

    def test_open_at_limit_up_is_unfilled(self):
        data = market_fixture()
        data['a']['open'][1, 0] = 110.
        row = research.planned_outcome(data, 0, 0, 14, gapmax=20)
        self.assertEqual(row['status'], 'open_at_limit_up')
        self.assertNotIn('buy_price', row)

    def test_high_open_gap_cap_is_applied_before_purchase(self):
        data = market_fixture()
        data['a']['open'][1, 0] = 105.1
        row = research.planned_outcome(data, 0, 0, 14, gapmax=5)
        self.assertEqual(row['status'], 'gap_too_high')
        allowed = research.planned_outcome(data, 0, 0, 14, gapmax=6)
        self.assertEqual(allowed['status'], 'executed')

    def test_missing_open_has_no_fill(self):
        data = market_fixture()
        data['a']['open'][1, 0] = np.nan
        self.assertEqual(research.planned_outcome(data, 0, 0, 14)['status'], 'no_open')

    def test_limit_down_exit_is_delayed_and_does_not_cross_period_end(self):
        data = market_fixture()
        data['a']['close'][5, 0] = 90.
        data['a']['close'][6, 0] = 81.
        data['a']['close'][7, 0] = 85.
        row = research.planned_outcome(data, 0, 0, 14)
        self.assertEqual(row['exit_i'], 7)
        self.assertEqual(row['delay'], 2)
        blocked = research.planned_outcome(data, 0, 0, 6)
        self.assertEqual(blocked['status'], 'exit_unresolved')
        self.assertIsNone(blocked['net'])

    def test_reference_mismatch_keeps_trade_but_withholds_verified_net(self):
        data = market_fixture()
        data['reference_ok'][3, 0] = False
        row = research.planned_outcome(data, 0, 0, 14)
        self.assertEqual(row['status'], 'reference_unknown')
        self.assertIn('buy_price', row)
        self.assertIsNone(row['net'])
        self.assertTrue(np.isfinite(row['raw_net']))

    def test_quote_gap_is_not_silently_a_verified_return(self):
        data = market_fixture()
        data['a']['close'][3, 0] = np.nan
        data['reference_ok'][3:5, 0] = False
        row = research.planned_outcome(data, 0, 0, 14)
        self.assertEqual(row['status'], 'reference_unknown')
        self.assertIsNone(row['net'])

    def test_missing_entry_identity_is_not_verified(self):
        data = market_fixture()
        data['names'][1, 0] = ''
        row = research.planned_outcome(data, 0, 0, 14)
        self.assertEqual(row['status'], 'reference_unknown')
        self.assertIsNone(row['net'])

    def test_costs_are_charged_and_worse_slippage_reduces_net(self):
        data = market_fixture()
        base = research.planned_outcome(data, 0, 0, 14, slippage=.001)
        stress = research.planned_outcome(data, 0, 0, 14, slippage=.005)
        expected = ((100 * .999 * (1 - .0003 - .00001 - .0005)) /
                    (100 * 1.001 * (1 + .0003 + .00001)) - 1) * 100
        self.assertAlmostEqual(base['net'], expected, places=8)
        self.assertLess(stress['net'], base['net'])
        self.assertLess(base['net'], base['gross'])

    def test_slippage_does_not_use_future_daily_extremes(self):
        data = market_fixture()
        other = copy.deepcopy(data)
        other['a']['high'][1:, 0] = 500.
        other['a']['low'][1:, 0] = 1.
        a = research.planned_outcome(data, 0, 0, 14, slippage=.005)
        b = research.planned_outcome(other, 0, 0, 14, slippage=.005)
        self.assertEqual(a['buy_price'], b['buy_price'])
        self.assertEqual(a['sell_price'], b['sell_price'])
        self.assertEqual(a['net'], b['net'])

    def test_top_candidate_is_not_replaced_using_future_fill_information(self):
        data = market_fixture()
        data['a']['open'][1, 0] = 110.
        mask = np.zeros(data['base'].shape, dtype=bool)
        mask[0] = True
        score = np.zeros(mask.shape)
        score[0] = [2., 1.]
        config = {'k': 1, 'gapmax': 20}
        with patch.object(research, 'signals', fixed_selector(mask, score)):
            ledger = research.event_backtest(data, config, data['dates'][0], data['dates'][-1])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['stock_i'], 0)
        self.assertEqual(ledger[0]['status'], 'open_at_limit_up')


class TestT5Portfolio(unittest.TestCase):
    def test_overlapping_batches_respect_cash_and_do_not_duplicate_holdings(self):
        data = market_fixture(days=20, stocks=20, price=10.)
        mask = np.ones(data['base'].shape, dtype=bool)
        scores = np.broadcast_to(np.arange(20., 0., -1.), mask.shape).copy()
        cfg = {'k': 2, 'gapmax': 5}
        ledger, nav, result = research.portfolio(
            data, cfg, data['dates'][0], data['dates'][-1],
            selector=fixed_selector(mask, scores))
        self.assertTrue((nav.cash >= -1e-8).all())
        self.assertTrue((nav.exposure <= 1 + 1e-8).all())
        self.assertGreaterEqual(nav.positions.max(), 8)
        filled = [r for r in ledger if r.get('shares', 0) > 0]
        by_code = {}
        for row in filled:
            prior = by_code.get(row['code'])
            if prior:
                self.assertLess(prior['exit_i'], row['entry_i'])
            by_code[row['code']] = row
            self.assertLessEqual(row['shares'] * row['buy_price'],
                                 float(data['a']['amount'][row['signal_i'], row['stock_i']]) * 1000 * .001)
        self.assertEqual(result['filled'], len(filled))
        self.assertLess(result['portfolio_return'], 0., 'Flat-price strategy must lose costs')

    def test_same_day_close_sale_cannot_fund_that_mornings_purchases(self):
        data = market_fixture(days=16, stocks=18, price=10.)
        mask = np.zeros(data['base'].shape, dtype=bool)
        for signal in range(6):
            mask[signal, signal * 3:(signal + 1) * 3] = True
        # First cohort cannot sell on planned day 5; all earlier cohorts release
        # capital on day 6, after the sixth cohort's opening purchase attempt.
        data['a']['close'][5, :] = 9.
        data['a']['close'][6, :] = 9.5
        data['a']['open'][6, :] = 9.5
        ledger, nav, _ = research.portfolio(
            data, {'k': 3, 'gapmax': 20}, data['dates'][0], data['dates'][-1],
            selector=fixed_selector(mask))
        morning_orders = [r for r in ledger if r['entry_i'] == 6]
        spent = sum(r.get('invested', 0) for r in morning_orders)
        available = nav.cash.iloc[5]
        self.assertLess(available, 15000., 'Fixture must actually exhaust opening cash')
        self.assertLessEqual(spent, available + 1e-8)
        self.assertTrue(any(r.get('portfolio_status') == 'no_cash' for r in morning_orders))
        released = [r for r in ledger if r.get('shares', 0) > 0 and r['exit_i'] == 6]
        self.assertGreaterEqual(len(released), 3)
        self.assertGreater(nav.cash.iloc[6], nav.cash.iloc[5])

    def test_small_trade_uses_minimum_commission_and_round_lot(self):
        data = market_fixture(stocks=1, price=10.)
        data['a']['amount'][:] = 2500.  # 2,500 yuan maximum order at 0.1% capacity.
        mask = np.zeros(data['base'].shape, dtype=bool)
        mask[0, 0] = True
        ledger, _, _ = research.portfolio(
            data, {'k': 1, 'gapmax': 5}, data['dates'][0], data['dates'][-1],
            selector=fixed_selector(mask))
        row = ledger[0]
        self.assertEqual(row['shares'], 200)
        cost = row['shares'] * row['buy_price']
        self.assertAlmostEqual(row['invested'], cost + 5 + cost * .00001)
        self.assertLess(row['actual_net'], row['net'])

    def test_unresolved_trade_retains_cash_usage_and_unknown_sensitivity(self):
        data = market_fixture(stocks=1)
        data['a']['close'][5:, 0] = np.nan
        data['reference_ok'][5:, 0] = False
        mask = np.zeros(data['base'].shape, dtype=bool)
        mask[0, 0] = True
        ledger, nav, result = research.portfolio(
            data, {'k': 1, 'gapmax': 5}, data['dates'][0], data['dates'][-1],
            selector=fixed_selector(mask))
        self.assertEqual(ledger[0]['status'], 'exit_unresolved')
        self.assertEqual(result['filled'], 1)
        self.assertEqual(result['unresolved'], 1)
        self.assertEqual(result['open_positions'], 1)
        self.assertEqual(result['stressed_unknown_mean'], -30)
        self.assertLess(nav.cash.iloc[-1], 1_000_000.)

    def test_block_bootstrap_requires_explicit_calendar(self):
        ledger = [{'status': 'executed', 'signal': '20260601', 'net': 1.},
                  {'status': 'executed', 'signal': '20260610', 'net': 3.}]
        with self.assertRaises(ValueError):
            research.summarize(ledger, bootstrap=True)
        with patch.object(research, 'load_data', side_effect=AssertionError('Hidden cache read')):
            summary = research.summarize(ledger, bootstrap=True,
                                         calendar=pd.bdate_range('2026-06-01', '2026-06-10')
                                         .strftime('%Y%m%d').tolist())
        self.assertIn('date_block_ci95', summary)
        self.assertEqual(summary['n'], 2)


class TestT5RidgeTraining(unittest.TestCase):
    def test_vector_training_labels_match_scalar_execution_and_identity_rules(self):
        template = ridge_fixture()
        first, end = research.period_bounds(template, '20250701', research.TRAIN_END)
        aliases = ['', '600000.SH', '600000', ' 600000.SH ', None, np.nan]
        scenarios = ['normal', 'open_limit', 'gap_limit', 'delayed_exit',
                     'reference_unknown', 'beyond_training_end', 'over_delay_limit']
        scenarios += [f'unknown_name_{i}' for i in range(len(aliases))]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                data = copy.deepcopy(template)
                signal, stock = first + 10, 0
                if scenario == 'beyond_training_end':
                    signal = end - 5
                data['base'][:] = False
                data['base'][signal, stock] = True
                entry, planned = signal + 1, signal + 5
                anchor = data['a']['close'][signal, stock]
                if scenario == 'open_limit':
                    data['a']['open'][entry, stock] = research.limit_price(anchor, .1)
                elif scenario == 'gap_limit':
                    data['a']['open'][entry, stock] = anchor * 1.06
                elif scenario in ['delayed_exit', 'beyond_training_end', 'over_delay_limit']:
                    delays = 2 if scenario == 'delayed_exit' else 21
                    for day in range(planned, min(planned + delays, len(data['dates']))):
                        data['a']['close'][day, stock] = research.limit_price(
                            data['a']['close'][day - 1, stock], .1, up=False)
                    if scenario == 'delayed_exit':
                        data['a']['close'][planned + 2, stock] = data['a']['close'][planned + 1, stock] * 1.02
                elif scenario == 'reference_unknown':
                    data['reference_ok'][signal + 3, stock] = False
                    data['valid'][signal + 3, stock] = False
                elif scenario.startswith('unknown_name_'):
                    data['names'][entry, stock] = aliases[int(scenario.rsplit('_', 1)[1])]
                scalar = research.planned_outcome(data, signal, stock, end, gapmax=5, slippage=.001)
                days, stocks, targets, exits = research.ridge_training_rows(data, False)
                if scalar['status'] == 'executed':
                    np.testing.assert_array_equal(days, [signal])
                    np.testing.assert_array_equal(stocks, [stock])
                    np.testing.assert_array_equal(exits, [scalar['exit_i']])
                    self.assertAlmostEqual(targets[0], np.clip(scalar['net'], -40, 40), places=8)
                else:
                    self.assertEqual(len(targets), 0, scalar)

    def test_post_training_prices_features_and_returns_do_not_change_model(self):
        for short_history, cutoff in [(False, research.TRAIN_END), (True, '20260529')]:
            with self.subTest(short_history=short_history):
                data = ridge_fixture()
                expected = research.fit_ridge(data, short_history, .1)
                changed = copy.deepcopy(data)
                last = changed['dates'].index(cutoff)
                for values in changed['a'].values():
                    values[last + 1:] = values[last + 1:] * 10 + 12345
                for values in changed['f'].values():
                    values[last + 1:] = values[last + 1:] * -1000
                changed['f']['amount'][last + 1:] = np.abs(changed['f']['amount'][last + 1:])
                for values in changed['market'].values():
                    values[last + 1:] = values[last + 1:] * -1000
                changed['valid'][last + 1:] = False
                changed['base'][last + 1:] = False
                changed['reference_ok'][last + 1:] = False
                actual = research.fit_ridge(changed, short_history, .1)
                self.assertEqual(expected['features'], actual['features'])
                self.assertEqual(expected['train_examples'], actual['train_examples'])
                self.assertLessEqual(actual['latest_label_date'], cutoff)
                for key in ['low', 'high', 'center', 'scale', 'weights', 'intercept']:
                    np.testing.assert_allclose(expected[key], actual[key], rtol=0, atol=0,
                                               err_msg=key)

    def test_signal_labels_not_mature_by_training_end_are_excluded(self):
        data = ridge_fixture()
        first, end = research.period_bounds(data, '20250701', research.TRAIN_END)
        data['base'][:] = False
        data['base'][first] = True
        # These five rows have a future T+5 outside the training interval.
        data['base'][end - 4:end + 1] = True
        model = research.fit_ridge(data, False, .1)
        self.assertEqual(model['train_examples'], len(data['codes']))
        self.assertEqual(model['latest_label_date'], data['dates'][first + 5])
        changed = copy.deepcopy(data)
        for values in changed['f'].values():
            values[end - 4:end + 1] = 1e8
        late = research.fit_ridge(changed, False, .1)
        self.assertEqual(model, late)

    def test_stock_code_used_as_name_retains_unknown_identity_trade(self):
        for alias in ['600000.SH', '600000', ' 600000.SH ']:
            with self.subTest(alias=alias):
                data = market_fixture()
                data['names'][1, 0] = alias
                self.assertFalse(research.identity_known(alias, data['codes'][0]))
                row = research.planned_outcome(data, 0, 0, 14)
                self.assertEqual(row['status'], 'reference_unknown')
                self.assertTrue(row['unknown_identity'])
                self.assertIsNone(row['net'])
                self.assertIn('buy_price', row)


if __name__ == '__main__':
    unittest.main()
