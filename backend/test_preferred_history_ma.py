"""条件优选历史涨幅分布的入选日均线筛选: 临时库, 不触碰实际数据库。"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app as backend

ENTRY = '20260130'
DATES = ['20260116', '20260119', '20260120', '20260121', '20260122', '20260123',
         '20260126', '20260127', '20260128', '20260129', ENTRY]

# 每只股票入选日的 10 根收盘价 (最后一根 = 入选日收盘)。刻意造出四种均线关系:
#   A 等于 MA5 也等于 MA10 -> 四档一个都不该算「站上」
#   B 远高于 MA5 和 MA10 -> 只进 ma5 / ma10 两档
#   C 收在 MA10 之上但掉到 MA5 之下 -> 只进 ma10_only
#   D 收在 MA5 之上但没到 MA10 -> 只进 ma5_only
CLOSES = {
    'A.SZ': [10] * 10,
    'B.SZ': [1] * 9 + [12],
    'C.SZ': [1, 1, 1, 1, 1, 20, 20, 20, 20, 12],
    'D.SZ': [20, 20, 20, 20, 20, 5, 5, 5, 5, 10],
}


def _ma(values, n):
    return sum(values[-n:]) / n


class PreferredHistoryMaFilterTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        self.db = sqlite3.connect(self.path)
        self.db.executescript('''
            CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, close REAL);
            CREATE TABLE conditional_picks_preferred_snapshot (
                trade_date TEXT PRIMARY KEY, count INTEGER, results_json TEXT,
                threshold_yi REAL, volume_ratio_pct REAL);
        ''')
        results = []
        for code, closes in CLOSES.items():
            # 只取最后 10 个日期, 让最后一根收盘价正好落在入选日
            for day, close in zip(DATES[-len(closes):], closes):
                self.db.execute('INSERT INTO stock_daily VALUES (?,?,?)', (code, day, close))
            results.append({
                'ts_code': code, 'name': code, 'close': closes[-1],
                'volume_ratio_pct': 50,
                'preferred_outcomes': [{'high_sum_pct': v} for v in (1.0, 2.0, 3.0, 4.0, 5.0)],
            })
        self.db.execute('INSERT INTO conditional_picks_preferred_snapshot VALUES (?,?,?,?,?)',
                        (ENTRY, len(results), json.dumps(results), 3, 0))
        self.db.commit()
        self.db.close()
        self.codes = set(CLOSES)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _collect(self, ma_filter='all', window=5, volume_ratio_pct=0):
        with patch.object(backend, 'DB_PATH', self.path):
            return backend._collect_preferred_history(window, volume_ratio_pct, ma_filter)

    def _codes(self, data):
        return {s['ts_code'] for s in data['samples']}

    def test_ma_ma10_windows_match_snapshot_definition(self):
        samples = [{'ts_code': code, 'trade_date': ENTRY} for code in CLOSES]
        with patch.object(backend, 'DB_PATH', self.path):
            mas = backend._preferred_hist_sample_ma(samples)
        for code, closes in CLOSES.items():
            ma5, ma10 = mas[(code, ENTRY)]
            self.assertAlmostEqual(ma5, _ma(closes, 5), places=9, msg=code)
            self.assertAlmostEqual(ma10, _ma(closes, 10), places=9, msg=code)

    def test_parser_accepts_known_keys_and_falls_back_to_all(self):
        for key in backend.PREFERRED_HIST_MA_FILTERS:
            self.assertEqual(backend._parse_preferred_hist_ma(key), key)
        for bad in (None, '', '  ', 'ma7', 'ALL', 'ma5,ma10', '../../etc'):
            self.assertEqual(backend._parse_preferred_hist_ma(bad), 'all')

    def test_five_modes_pick_the_expected_samples(self):
        self.assertEqual(self._codes(self._collect('all')), self.codes)
        self.assertEqual(self._codes(self._collect('ma5')), {'B.SZ', 'D.SZ'})
        self.assertEqual(self._codes(self._collect('ma10')), {'B.SZ', 'C.SZ'})
        self.assertEqual(self._codes(self._collect('ma5_only')), {'D.SZ'})
        self.assertEqual(self._codes(self._collect('ma10_only')), {'C.SZ'})

    def test_close_equal_to_ma_counts_as_not_above(self):
        # A.SZ 收盘 10, MA5=MA10=10: 恰好等于不算「站上」, 四档互斥档也都不该收它
        data = self._collect('ma5')
        self.assertNotIn('A.SZ', self._codes(data))
        self.assertEqual(data['ma_filtered_out'], 2)
        self.assertEqual(data['ma_missing'], 0)

    def test_only_variants_are_mutually_exclusive(self):
        for code in CLOSES:
            ma5, ma10 = _ma(CLOSES[code], 5), _ma(CLOSES[code], 10)
            close = CLOSES[code][-1]
            in_ma5_only = backend._preferred_hist_ma_pass('ma5_only', close, ma5, ma10)
            in_ma10_only = backend._preferred_hist_ma_pass('ma10_only', close, ma5, ma10)
            self.assertFalse(in_ma5_only and in_ma10_only, msg=code)

    def test_missing_ma_is_excluded_and_counted_separately(self):
        # 某天的入选日没有日线 -> ma_missing, 不进分布, 也不混进 ma_filtered_out
        with patch.object(backend, 'DB_PATH', self.path):
            self.db = sqlite3.connect(self.path)
            self.db.execute("DELETE FROM stock_daily WHERE ts_code='D.SZ' AND trade_date=?", (ENTRY,))
            self.db.commit()
            self.db.close()
            data = backend._collect_preferred_history(5, 0, 'ma5_only')
        self.assertEqual(data['settled'], 0)
        self.assertEqual(data['ma_missing'], 1)
        self.assertEqual(data['ma_filtered_out'], 3)
        self.assertTrue(all(b['pct'] == 0.0 for b in data['buckets']))

    def test_totals_and_bucket_shares_stay_consistent(self):
        for key in backend.PREFERRED_HIST_MA_FILTERS:
            data = self._collect(key)
            self.assertEqual(data['ma_filter'], key)
            self.assertEqual(data['ma_filter_label'], backend.PREFERRED_HIST_MA_FILTERS[key])
            self.assertEqual(data['settled'], len(data['samples']))
            self.assertEqual(sum(b['count'] for b in data['buckets']), data['settled'])
            self.assertEqual(sum(d['settled'] for d in data['dates']), data['settled'])
            if data['settled']:
                self.assertAlmostEqual(sum(b['pct'] for b in data['buckets']), 100.0, places=1)
            self.assertEqual(data['total'], data['settled'] + data['pending'] + data['filtered_out']
                             + data['ma_filtered_out'] + data['ma_missing'])
            self.assertEqual(data['unique_stocks'], len({s['ts_code'] for s in data['samples']}))

    def test_ma_filter_combines_with_volume_filter(self):
        data = self._collect('ma10_only', volume_ratio_pct=40)  # 全部样本成交量比 50 > 40
        self.assertEqual(data['settled'], 0)
        self.assertEqual(data['filtered_out'], 4)
        self.assertTrue(data['volume_filter_on'])
        data = self._collect('ma10_only', volume_ratio_pct=60)
        self.assertEqual(data['settled'], 1)
        self.assertEqual(self._codes(data), {'C.SZ'})

    def test_samples_expose_ma_values_when_filtered(self):
        data = self._collect('ma10_only')
        sample = data['samples'][0]
        self.assertEqual(sample['ts_code'], 'C.SZ')
        self.assertAlmostEqual(sample['ma5'], _ma(CLOSES['C.SZ'], 5), places=4)
        self.assertAlmostEqual(sample['ma10'], _ma(CLOSES['C.SZ'], 10), places=4)

    def test_unfiltered_response_keeps_ma_fields_null(self):
        data = self._collect('all')
        self.assertTrue(all(s['ma5'] is None and s['ma10'] is None for s in data['samples']))
        self.assertEqual(data['ma_filtered_out'], 0)
        self.assertEqual(data['ma_missing'], 0)


if __name__ == '__main__':
    unittest.main()