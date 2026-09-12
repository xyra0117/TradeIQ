"""指数图表接口回归：内存数据库、模拟上游，不触碰实际行情或持仓。"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import app as backend


class KeptConnection(sqlite3.Connection):
    def close(self):
        pass  # 同一内存库跨多个 HTTP 请求；tearDown 显式释放。


class IndexChartTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', factory=KeptConnection)
        self.db.execute('CREATE TABLE index_daily (date TEXT, name TEXT, open REAL, '
                        'high REAL, low REAL, close REAL, change REAL, volume REAL)')
        for code, name in backend.INDEX_CHART_CODES.items():
            for i in range(310):
                day = (datetime(2025, 1, 1) + timedelta(days=i)).strftime('%Y%m%d')
                self.db.execute('INSERT INTO index_daily VALUES (?,?,?,?,?,?,?,?)',
                                (day, name, None if i == 2 else 10, 12, 9, 11, 1, 123.45))
        self.db.commit()
        self.connect = patch.object(backend.sqlite3, 'connect', return_value=self.db)
        self.connect.start()
        backend._INDEX_MINLINE_CACHE.clear()
        backend._MINLINE_CACHE.clear()
        self.client = backend.app.test_client()
        self.quote = {'date': '20260911', 'time': '15:00:03', 'open': 10, 'prev_close': 9}
        self.rows = [{'m': '09:30:00', 'p': 10.0, 'avg_p': 10.0, 'v': 20},
                     {'m': '15:00:00', 'p': 11.0, 'avg_p': 10.5, 'v': 30}]

    def tearDown(self):
        self.connect.stop()
        sqlite3.Connection.close(self.db)

    def test_all_four_indices_full_history_and_missing_ohlc(self):
        for code, name in backend.INDEX_CHART_CODES.items():
            with self.subTest(code=code):
                url = '/api/index/kline?ts_code=' + code
                result = self.client.get(url).get_json()
                self.assertEqual(result['name'], name)
                self.assertEqual(result['count'], 310)  # 不得沿用个股的 250 条限制。
                self.assertEqual(result['start_date'], '20250101')
                self.assertEqual(result['end_date'], '20251106')
                self.assertEqual(result['missing_ohlc'], 1)
                self.assertIsNone(result['rows'][2]['open'])
                self.assertEqual(result['volume_label'], '成交额')
                self.assertEqual(result['volume_unit'], '亿元')
                self.assertEqual(result['rows'][0]['volume'], 123.45)
                self.assertEqual(result, self.client.get(url + '&date=20250102&limit=1').get_json())

    def test_unknown_stock_and_invalid_dates_rejected(self):
        for route in ['kline', 'minline']:
            self.assertEqual(self.client.get('/api/index/' + route + '?ts_code=000001.SZ').status_code, 400)
        for day in ['', '20260230', '2026-09-11', '2026911']:
            self.assertEqual(self.client.get('/api/index/minline?ts_code=000001.SH&date=' + day).status_code, 400)

    def test_empty_kline(self):
        with patch.dict(backend.INDEX_CHART_CODES, {'000001.SH': '无数据指数'}):
            data = self.client.get('/api/index/kline?ts_code=000001.SH').get_json()
        self.assertEqual(data['count'], 0)
        self.assertIsNone(data['start_date'])

    def test_historical_minute_never_substitutes_latest(self):
        with patch.object(backend, '_fetch_index_chart_quote', return_value=self.quote), \
                patch.object(backend, '_fetch_sina_minline_rows') as fetch:
            data = self.client.get('/api/index/minline?ts_code=000001.SH&date=20260910').get_json()
        fetch.assert_not_called()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['reason'], 'date_unavailable')
        self.assertIsNone(data['trade_date'])

    def test_verified_minute_cache_isolated_by_code_and_date(self):
        with patch.object(backend, '_fetch_index_chart_quote', return_value=self.quote), \
                patch.object(backend, '_fetch_sina_minline_rows', return_value=self.rows) as fetch:
            for code in backend.INDEX_CHART_CODES:
                url = '/api/index/minline?ts_code=' + code + '&date=20260911'
                data = self.client.get(url).get_json()
                self.assertEqual(data['rows'], self.rows)
                self.assertEqual(data['trade_date'], '20260911')
                self.assertEqual(data, self.client.get(url).get_json())
            self.assertEqual(fetch.call_count, 4)
            old = self.client.get('/api/index/minline?ts_code=000001.SH&date=20260910').get_json()
            self.assertEqual(old['count'], 0)

    def test_midnight_transition_and_stale_preopen_rows_rejected(self):
        for second in [dict(self.quote, date='20260912'), dict(self.quote, time='10:00:00')]:
            with self.subTest(second=second), \
                    patch.object(backend, '_fetch_index_chart_quote', side_effect=[self.quote, second]), \
                    patch.object(backend, '_fetch_sina_minline_rows', return_value=self.rows):
                data = self.client.get('/api/index/minline?ts_code=000001.SH&date=20260911').get_json()
                self.assertEqual(data['count'], 0)
                self.assertEqual(data['reason'], 'unverified_date')
        with patch.object(backend, '_fetch_index_chart_quote', return_value=dict(self.quote, open=0)):
            data = self.client.get('/api/index/minline?ts_code=000001.SH&date=20260911').get_json()
            self.assertEqual(data['reason'], 'not_open')

    def test_failed_fetch_can_retry(self):
        url = '/api/index/minline?ts_code=000001.SH&date=20260911'
        with patch.object(backend, '_fetch_index_chart_quote', side_effect=ValueError('no date')):
            self.assertEqual(self.client.get(url).status_code, 502)
        with patch.object(backend, '_fetch_index_chart_quote', return_value=self.quote), \
                patch.object(backend, '_fetch_sina_minline_rows', return_value=self.rows):
            self.assertEqual(self.client.get(url).get_json()['count'], 2)

    def test_quote_date_parser_and_missing_date(self):
        fields = ['0'] * 33
        fields[0:3] = ['上证指数', '10', '9']
        fields[30:32] = ['2026-09-11', '15:00:03']
        response = Mock(text='var hq_str_sh000001="' + ','.join(fields) + '";')
        with patch.object(backend._requests, 'get', return_value=response):
            self.assertEqual(backend._fetch_index_chart_quote('000001.SH'), self.quote)
        fields[30] = ''
        response.text = 'var hq_str_sh000001="' + ','.join(fields) + '";'
        with patch.object(backend._requests, 'get', return_value=response):
            with self.assertRaises(ValueError):
                backend._fetch_index_chart_quote('000001.SH')

    def test_shared_minute_parser_and_stock_compatibility(self):
        raw = [{'m': '09:30:00', 'p': '11', 'avg_p': '10.5', 'v': '3000'},
               {'m': '15:05:00', 'p': '12', 'avg_p': '11', 'v': '4000'}]
        response = Mock(text='var _ml=(' + json.dumps(raw) + ');')
        with patch.object(backend._requests, 'get', return_value=response):
            rows = backend._fetch_sina_minline_rows('sz000001')
        self.assertEqual(rows, [{'m': '09:30:00', 'p': 11, 'avg_p': 10.5, 'v': 30}])
        with patch.object(backend, '_fetch_sina_minline_rows', return_value=rows), \
                patch.object(backend, 'fetch_sina_quotes', return_value={'000001.SZ': {'name': '平安银行', 'prev_close': 10}}):
            data = self.client.get('/api/stock/minline?ts_code=000001.SZ').get_json()
        self.assertEqual(data['name'], '平安银行')
        self.assertEqual(data['rows'], rows)


if __name__ == '__main__':
    unittest.main()
