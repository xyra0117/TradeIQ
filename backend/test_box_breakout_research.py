"""Causal box-window checks using synthetic in-memory arrays only."""
import unittest

import numpy as np

from scripts import analyze_box_breakouts_20261018 as research


def make_bars(closes, volumes=None):
    close = np.asarray(closes, dtype=float)
    if close.ndim == 1:
        close = close[:, None]
    volume = np.full_like(close, 100.0) if volumes is None else np.asarray(volumes, dtype=float).reshape(close.shape)
    change = np.zeros_like(close)
    change[1:] = (close[1:] / close[:-1] - 1) * 100
    return {
        'open': close.copy(), 'close': close.copy(),
        'high': close + 0.5, 'low': close - 0.5,
        'volume': volume, 'amount': np.full_like(close, 1000.0),
        'change': change,
    }


def copied(bars):
    return {k: v.copy() for k, v in bars.items()}


class BoxWindowTests(unittest.TestCase):
    def test_box_extremes_exclude_signal_day(self):
        bars = make_bars(np.arange(10., 30.))
        n, day = 10, 15
        bars['high'][day] = 1000
        bars['low'][day] = 1
        result = research.window_features(bars, n)
        prior = slice(day - n, day)
        self.assertEqual(result['top'][day, 0], bars['high'][prior, 0].max())
        self.assertEqual(result['bottom'][day, 0], bars['low'][prior, 0].min())
        self.assertEqual(result['close_top'][day, 0], bars['close'][prior, 0].max())
        self.assertAlmostEqual(result['break_high'][day, 0], (bars['close'][day, 0] / result['top'][day, 0] - 1) * 100)
        self.assertAlmostEqual(result['intraday_break'][day, 0], (1000 / result['top'][day, 0] - 1) * 100)

    def test_first_and_last_five_day_offsets(self):
        bars = make_bars(np.arange(10., 50.), np.arange(100., 140.))
        n, day = 20, 25
        result = research.window_features(bars, n)
        first = slice(day - n, day - n + 5)
        last = slice(day - 5, day)
        expected_drift = (bars['close'][last, 0].mean() / bars['close'][first, 0].mean() - 1) * 100
        expected_volume = bars['volume'][last, 0].mean() / bars['volume'][first, 0].mean()
        self.assertAlmostEqual(result['drift'][day, 0], expected_drift)
        self.assertAlmostEqual(result['volume_contraction'][day, 0], expected_volume)
        self.assertAlmostEqual(result['net_return'][day, 0], (bars['close'][day - 1, 0] / bars['close'][day - n, 0] - 1) * 100)

    def test_previous_break_uses_previous_days_own_box(self):
        bars = make_bars([10.] * 20 + [12., 14., 14.])
        result = research.window_features(bars, 10)
        # The first jump was not preceded by a breakout; the second was.
        self.assertLessEqual(result['prior_break'][20, 0], 0)
        self.assertGreater(result['break_high'][20, 0], 0)
        self.assertGreater(result['prior_break'][21, 0], 0)
        self.assertAlmostEqual(result['prior_break'][21, 0], result['break_high'][20, 0])
        self.assertAlmostEqual(result['prior_close_break'][21, 0], result['break_close'][20, 0])
        # Comparing yesterday to a box including yesterday would wrongly say no prior breakout.
        self.assertLess(bars['close'][20, 0], result['top'][21, 0])

    def test_first_break_requires_one_extra_history_day(self):
        result = research.window_features(make_bars([10.] * 15), 10)
        self.assertFalse(result['valid'][9, 0])
        self.assertTrue(result['valid'][10, 0])
        self.assertFalse(result['valid_first'][10, 0])
        self.assertTrue(result['valid_first'][11, 0])
        self.assertTrue(np.isnan(result['prior_break'][10, 0]))

    def test_reference_checks_only_links_inside_window(self):
        bars = make_bars([10.] * 20)
        day, n = 15, 10
        # The oldest included bar does not need a reference to an excluded bar.
        bars['change'][day - n, 0] = 50
        self.assertTrue(research.valid_window(bars, n)[day, 0])
        bars['change'][day - n + 1, 0] = 50
        self.assertFalse(research.valid_window(bars, n)[day, 0])

    def test_reference_anomaly_tolerance_and_recovery(self):
        bars = make_bars([10.] * 30)
        bars['change'][15, 0] = .149
        self.assertTrue(research.valid_window(bars, 10)[15, 0])
        bars['change'][15, 0] = .151
        valid = research.valid_window(bars, 10)
        self.assertFalse(valid[15, 0])
        self.assertFalse(valid[24, 0])
        self.assertTrue(valid[25, 0])

    def test_missing_calendar_bar_invalidates_complete_window(self):
        bars = make_bars([10.] * 30)
        for values in bars.values():
            values[10, 0] = np.nan
        valid = research.valid_window(bars, 10)
        self.assertFalse(valid[20, 0])
        self.assertTrue(valid[21, 0])

    def test_other_stock_missing_data_does_not_leak(self):
        bars = make_bars(np.full((30, 2), 10.))
        bars['volume'][15, 0] = 0
        valid = research.valid_window(bars, 10)
        self.assertFalse(valid[20, 0])
        self.assertTrue(valid[20, 1])

    def test_all_input_fields_reject_nonfinite_values(self):
        for field in ('open', 'high', 'low', 'close', 'volume', 'amount', 'change'):
            for value in (np.nan, np.inf, -np.inf):
                with self.subTest(field=field, value=value):
                    bars = make_bars([10.] * 15)
                    bars[field][12, 0] = value
                    self.assertFalse(research.valid_bars(bars)[12, 0])
                    self.assertFalse(research.valid_window(bars, 10)[12, 0])

    def test_prices_and_liquidity_must_be_valid(self):
        changes = [('low', 0), ('open', 0), ('close', 0), ('volume', 0), ('amount', 0),
                   ('high', 9), ('low', 11)]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                bars = make_bars([10.] * 15)
                bars[field][12, 0] = value
                self.assertFalse(research.valid_bars(bars)[12, 0])

    def test_future_mutations_cannot_change_signal_features(self):
        original = make_bars(np.arange(10., 50.))
        altered = copied(original)
        for values in altered.values():
            values[26:] = np.nan
        before = research.window_features(original, 20)
        after = research.window_features(altered, 20)
        for key in before:
            with self.subTest(feature=key):
                np.testing.assert_array_equal(before[key][:26], after[key][:26])

    def test_shifting_forward_is_forbidden(self):
        with self.assertRaises(ValueError):
            research.shift(np.ones((5, 1)), -1)


if __name__ == '__main__':
    unittest.main()
