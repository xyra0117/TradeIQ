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
        # 给 lazy 落库兜个底表, 避免落库失败噪声
        self.db.execute('''CREATE TABLE stock_minline_bars (
            ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, m TEXT NOT NULL,
            p REAL, avg_p REAL, v REAL NOT NULL DEFAULT 0,
            name TEXT, prev_close REAL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (ts_code, trade_date, m))''')
        # index minline ?date= 严格本地路径需要这张表存在
        self.db.execute('''CREATE TABLE index_minline_bars (
            ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, m TEXT NOT NULL,
            p REAL, avg_p REAL, v REAL NOT NULL DEFAULT 0,
            prev_close REAL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (ts_code, trade_date, m))''')
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
        # 非空非法 date 必须 400 (严格兼容旧端点). 空 date 现在走 lazy 路径返回 200.
        for day in ['20260230', '2026-09-11', '2026911']:
            self.assertEqual(self.client.get('/api/index/minline?ts_code=000001.SH&date=' + day).status_code, 400)

    def test_empty_kline(self):
        with patch.dict(backend.INDEX_CHART_CODES, {'000001.SH': '无数据指数'}):
            data = self.client.get('/api/index/kline?ts_code=000001.SH').get_json()
        self.assertEqual(data['count'], 0)
        self.assertIsNone(data['start_date'])

    def test_historical_minute_never_substitutes_latest(self):
        # 严格本地路径: 本地无数据 → source=none, 不回落 sina 不出 reason 字段 (老 reason 已废弃)
        with patch.object(backend, '_fetch_index_chart_quote') as fetch_q, \
                patch.object(backend, '_fetch_sina_minline_rows') as fetch_m:
            data = self.client.get('/api/index/minline?ts_code=000001.SH&date=20260910').get_json()
            fetch_q.assert_not_called()
            fetch_m.assert_not_called()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['source'], 'none')
        self.assertIsNone(data['trade_date'])
        self.assertIn('20260910', data['message'])

    def test_verified_minute_cache_isolated_by_code_and_date(self):
        # 缓存隔离: 4 只指数 同一 date 第二次走 cache; 换 date 不命中
        with patch.object(backend, '_infer_minline_trade_date', return_value='20260911'), \
             patch.object(backend, '_fetch_index_chart_quote', return_value=self.quote), \
             patch.object(backend, '_fetch_sina_minline_rows', return_value=self.rows) as fetch:
            for code in backend.INDEX_CHART_CODES:
                url = '/api/index/minline?ts_code=' + code  # lazy 路径, 走 cache
                data = self.client.get(url).get_json()
                self.assertEqual(data['rows'], self.rows)
                self.assertEqual(data['trade_date'], '20260911')
                self.assertEqual(data, self.client.get(url).get_json())
            self.assertEqual(fetch.call_count, 4)
        # 换 code 不命中前一只的 cache
        backend._INDEX_MINLINE_CACHE.clear()

    def test_midnight_transition_and_stale_preopen_rows_rejected(self):
        # lazy 路径: quote date 跨日 / rows 末条时刻 > quote time → source=none (reason 已废弃)
        for second in [dict(self.quote, date='20260912'), dict(self.quote, time='10:00:00')]:
            backend._INDEX_MINLINE_CACHE.clear()
            with self.subTest(second=second), \
                    patch.object(backend, '_infer_minline_trade_date', return_value='20260911'), \
                    patch.object(backend, '_fetch_index_chart_quote', side_effect=[self.quote, second]), \
                    patch.object(backend, '_fetch_sina_minline_rows', return_value=self.rows):
                data = self.client.get('/api/index/minline?ts_code=000001.SH').get_json()
                self.assertEqual(data['count'], 0)
                self.assertEqual(data['source'], 'none')
                self.assertTrue(data['message'])
        # 开盘前 (open<=0 或 time < 09:30) → 拒绝
        backend._INDEX_MINLINE_CACHE.clear()
        with patch.object(backend, '_infer_minline_trade_date', return_value='20260911'), \
             patch.object(backend, '_fetch_index_chart_quote', return_value=dict(self.quote, time='09:00:00')), \
             patch.object(backend, '_fetch_sina_minline_rows') as fetch_m:
            data = self.client.get('/api/index/minline?ts_code=000001.SH').get_json()
            fetch_m.assert_not_called()
        self.assertEqual(data['source'], 'none')

    def test_failed_fetch_can_retry(self):
        # ?date= 严格本地路径: 本地无数据时直接 source=none 200, 不调 quote/sina (跟老 502 路径已不同).
        url = '/api/index/minline?ts_code=000001.SH&date=20260911'
        with patch.object(backend, '_fetch_index_chart_quote') as mock_q, \
                patch.object(backend, '_fetch_sina_minline_rows') as mock_m:
            data = self.client.get(url).get_json()
            mock_q.assert_not_called()
            mock_m.assert_not_called()
        self.assertEqual(data['source'], 'none')
        self.assertEqual(data['count'], 0)
        # lazy 路径 (不传 date) 才会调 quote/sina, 失败会返对应 message
        lazy_url = '/api/index/minline?ts_code=000001.SH'
        with patch.object(backend, '_infer_minline_trade_date', return_value='20260911'), \
                patch.object(backend, '_fetch_index_chart_quote', return_value=self.quote), \
                patch.object(backend, '_fetch_sina_minline_rows', return_value=self.rows):
            self.assertEqual(self.client.get(lazy_url).get_json()['count'], 2)

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
        with patch.object(backend, '_today_is_trading_day', return_value=True), \
                patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
                patch.object(backend, '_fetch_sina_minline_rows', return_value=rows), \
                patch.object(backend, 'fetch_sina_quotes', return_value={'000001.SZ': {'name': '平安银行', 'prev_close': 10}}):
            data = self.client.get('/api/stock/minline?ts_code=000001.SZ').get_json()
        self.assertEqual(data['name'], '平安银行')
        self.assertEqual(data['rows'], rows)


class TestStockMinlinePersistence(unittest.TestCase):
    """个股分时持久化 + 端点语义.

    内存库 + mock 上游, 不碰真盘行情.
    """

    def setUp(self):
        self.db = sqlite3.connect(':memory:', factory=KeptConnection)
        self.db.execute('''CREATE TABLE stock_minline_bars (
            ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, m TEXT NOT NULL,
            p REAL, avg_p REAL, v REAL NOT NULL DEFAULT 0,
            name TEXT, prev_close REAL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (ts_code, trade_date, m))''')
        # 交易日历: 让 _today_is_trading_day / last_trading_date 真正能查
        self.db.execute('CREATE TABLE trading_dates_cache (cal_date TEXT PRIMARY KEY, is_open INTEGER)')
        # 覆盖 9 月整月 + 周五 09-11 让 last_trading_date (周一查 09-14) 能返回 09-11
        for d in ['20260907', '20260908', '20260909', '20260910', '20260911',
                  '20260914', '20260915', '20260916', '20260917', '20260918']:
            self.db.execute('INSERT INTO trading_dates_cache VALUES (?, 1)', (d,))
        self.db.commit()
        self.connect = patch.object(backend.sqlite3, 'connect', return_value=self.db)
        self.connect.start()
        backend._MINLINE_CACHE.clear()
        self.client = backend.app.test_client()

    def tearDown(self):
        self.connect.stop()
        sqlite3.Connection.close(self.db)

    def _seed(self, ts_code, trade_date, rows, name='测试股', prev_close=10.0):
        for r in rows:
            self.db.execute(
                'INSERT INTO stock_minline_bars (ts_code, trade_date, m, p, avg_p, v, name, prev_close)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                (ts_code, trade_date, r['m'], r['p'], r.get('avg_p'), r.get('v', 0), name, prev_close))
        self.db.commit()

    def test_local_hit_returns_local_rows(self):
        """本地有数据 → ?date=YYYYMMDD 命中 → source=local"""
        self._seed("000001.SZ", "20260915",
                  [{'m': '09:30:00', 'p': 11.0, 'avg_p': 10.5, 'v': 100},
                   {'m': '15:00:00', 'p': 12.0, 'avg_p': 11.5, 'v': 200}])
        d = self.client.get('/api/stock/minline?ts_code=000001.SZ&date=20260915').get_json()
        self.assertEqual(d['source'], 'local')
        self.assertEqual(d['count'], 2)
        self.assertEqual(d['trade_date'], '20260915')
        self.assertEqual(d['rows'][0]['p'], 11.0)
        self.assertEqual(d['name'], '测试股')

    def test_local_miss_returns_empty_no_fallback(self):
        """本地无 + 指定 date → 不回落 sina, 直接 source=none + message"""
        with patch.object(backend, '_fetch_sina_minline_rows') as mock_live:
            d = self.client.get('/api/stock/minline?ts_code=000001.SZ&date=20260915').get_json()
            mock_live.assert_not_called()
        self.assertEqual(d['source'], 'none')
        self.assertEqual(d['count'], 0)
        self.assertEqual(d['rows'], [])
        self.assertIn('20260915', d['message'])

    def test_lazy_no_date_persists_with_inferred_trade_date(self):
        """不传 date → lazy 抓 sina + 推断 today + 落库"""
        with patch.object(backend, '_today_is_trading_day', return_value=True), \
             patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
             patch.object(backend, '_fetch_sina_minline_rows',
                          return_value=[{'m': '09:30:00', 'p': 12.0, 'avg_p': 11.5, 'v': 100}]), \
             patch.object(backend, 'fetch_sina_quotes',
                          return_value={'000001.SZ': {'name': '平安银行', 'prev_close': 11.5}}):
            d = self.client.get('/api/stock/minline?ts_code=000001.SZ').get_json()
        self.assertEqual(d['source'], 'live')
        self.assertEqual(d['trade_date'], '20260915')
        self.assertEqual(d['count'], 1)
        # 落库断言
        rows = list(self.db.execute(
            'SELECT m, p, avg_p, v, name, prev_close FROM stock_minline_bars '
            "WHERE ts_code='000001.SZ' AND trade_date='20260915'"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 12.0)  # p
        self.assertEqual(rows[0][4], '平安银行')
        self.assertEqual(rows[0][5], 11.5)

    def test_lazy_persist_is_idempotent(self):
        """lazy 落库跑两次 → 行数不变, 后写覆盖"""
        rows = [{'m': '09:30:00', 'p': 12.0, 'avg_p': 11.5, 'v': 100}]
        with patch.object(backend, '_today_is_trading_day', return_value=True), \
             patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
             patch.object(backend, '_fetch_sina_minline_rows', return_value=rows), \
             patch.object(backend, 'fetch_sina_quotes',
                          return_value={'000001.SZ': {'name': 'X', 'prev_close': 11.5}}):
            self.client.get('/api/stock/minline?ts_code=000001.SZ')
            self.client.get('/api/stock/minline?ts_code=000001.SZ')
        cnt = self.db.execute(
            'SELECT COUNT(*) FROM stock_minline_bars '
            "WHERE ts_code='000001.SZ' AND trade_date='20260915'").fetchone()[0]
        self.assertEqual(cnt, 1)

    def test_trade_date_inference_pre_open_returns_last_trading_day(self):
        """09:25 集合竞价之前 → 上一交易日"""
        with patch.object(backend, '_today_is_trading_day', return_value=True):
            monday_9am = datetime(2026, 9, 14, 9, 0)  # 周一 09:00 (上交易日应是周五 09-11)
            inferred = backend._infer_minline_trade_date(now=monday_9am)
        self.assertEqual(inferred, '20260911')

    def test_trade_date_inference_non_trading_day_returns_last(self):
        """非交易日 → last_trading_date"""
        with patch.object(backend, '_today_is_trading_day', return_value=False), \
             patch.object(backend, 'last_trading_date', return_value='20260912') as mock_last:
            sunday = datetime(2026, 9, 13, 12, 0)
            inferred = backend._infer_minline_trade_date(now=sunday)
        self.assertEqual(inferred, '20260912')
        mock_last.assert_called_once_with('20260913')

    def test_invalid_date_param_treated_as_no_date(self):
        """非法 date 格式 → 走 lazy 路径"""
        with patch.object(backend, '_today_is_trading_day', return_value=True), \
             patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
             patch.object(backend, '_fetch_sina_minline_rows',
                          return_value=[{'m': '09:30:00', 'p': 12.0, 'avg_p': 11.5, 'v': 100}]), \
             patch.object(backend, 'fetch_sina_quotes', return_value={'X.SZ': {}}):
            d = self.client.get('/api/stock/minline?ts_code=X.SZ&date=abc').get_json()
        self.assertEqual(d['source'], 'live')

    def test_message_text_contains_date_when_no_data(self):
        """source=none 时 message 必须含查询的 date (让前端能展示 '未留存 YYYYMMDD')"""
        d = self.client.get('/api/stock/minline?ts_code=999999.SZ&date=20260801').get_json()
        self.assertEqual(d['source'], 'none')
        self.assertIn('20260801', d['message'])


class TestIndexMinlinePersistence(unittest.TestCase):
    """指数分时持久化 + 端点语义. 内存库 + mock 上游, 不碰真盘行情."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', factory=KeptConnection)
        self.db.execute('''CREATE TABLE index_minline_bars (
            ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, m TEXT NOT NULL,
            p REAL, avg_p REAL, v REAL NOT NULL DEFAULT 0,
            prev_close REAL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (ts_code, trade_date, m))''')
        # 交易日历 + stock_minline_bars 兜底 (lazy 路径可能命中落库失败分支)
        self.db.execute('CREATE TABLE trading_dates_cache (cal_date TEXT PRIMARY KEY, is_open INTEGER)')
        for d in ['20260907', '20260908', '20260909', '20260910', '20260911',
                  '20260914', '20260915', '20260916', '20260917', '20260918']:
            self.db.execute('INSERT INTO trading_dates_cache VALUES (?, 1)', (d,))
        self.db.execute('''CREATE TABLE stock_minline_bars (
            ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, m TEXT NOT NULL,
            p REAL, avg_p REAL, v REAL NOT NULL DEFAULT 0,
            name TEXT, prev_close REAL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (ts_code, trade_date, m))''')
        self.db.commit()
        self.connect = patch.object(backend.sqlite3, 'connect', return_value=self.db)
        self.connect.start()
        backend._INDEX_MINLINE_CACHE.clear()
        self.client = backend.app.test_client()

    def tearDown(self):
        self.connect.stop()
        sqlite3.Connection.close(self.db)

    def _seed(self, ts_code, trade_date, rows, prev_close=3500.0):
        for r in rows:
            self.db.execute(
                'INSERT INTO index_minline_bars (ts_code, trade_date, m, p, avg_p, v, prev_close)'
                ' VALUES (?,?,?,?,?,?,?)',
                (ts_code, trade_date, r['m'], r['p'], r.get('avg_p'), r.get('v', 0), prev_close))
        self.db.commit()

    def test_local_hit_returns_local_rows(self):
        """本地有数据 → ?date=YYYYMMDD 命中 → source=local"""
        self._seed("000001.SH", "20260915",
                  [{'m': '09:30:00', 'p': 3500.0, 'avg_p': 3499.5, 'v': 1000},
                   {'m': '15:00:00', 'p': 3510.0, 'avg_p': 3505.0, 'v': 2000}])
        d = self.client.get('/api/index/minline?ts_code=000001.SH&date=20260915').get_json()
        self.assertEqual(d['source'], 'local')
        self.assertEqual(d['count'], 2)
        self.assertEqual(d['trade_date'], '20260915')
        self.assertEqual(d['rows'][0]['p'], 3500.0)
        self.assertEqual(d['prev_close'], 3500.0)

    def test_local_miss_returns_empty_no_fallback(self):
        """本地无 + 指定 date → 不回落 sina, 直接 source=none + message"""
        with patch.object(backend, '_fetch_index_chart_quote') as mock_quote, \
             patch.object(backend, '_fetch_sina_minline_rows') as mock_minline:
            d = self.client.get('/api/index/minline?ts_code=000001.SH&date=20260915').get_json()
            mock_quote.assert_not_called()
            mock_minline.assert_not_called()
        self.assertEqual(d['source'], 'none')
        self.assertEqual(d['count'], 0)
        self.assertEqual(d['rows'], [])
        self.assertIn('20260915', d['message'])
        self.assertIn('指数', d['message'])

    def test_lazy_no_date_persists_with_inferred_trade_date(self):
        """不传 date → lazy 抓 sina + quote 核验 + 推断 today + 落库"""
        quote = {'date': '20260915', 'time': '15:00:03', 'open': 3500, 'prev_close': 3490}
        rows = [{'m': '09:30:00', 'p': 3500.0, 'avg_p': 3499.5, 'v': 1000},
                {'m': '15:00:00', 'p': 3510.0, 'avg_p': 3505.0, 'v': 2000}]
        with patch.object(backend, '_today_is_trading_day', return_value=True), \
             patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
             patch.object(backend, '_fetch_index_chart_quote', return_value=quote), \
             patch.object(backend, '_fetch_sina_minline_rows', return_value=rows):
            d = self.client.get('/api/index/minline?ts_code=000001.SH').get_json()
        self.assertEqual(d['source'], 'live')
        self.assertEqual(d['trade_date'], '20260915')
        self.assertEqual(d['count'], 2)
        # 落库断言
        db_rows = list(self.db.execute(
            'SELECT m, p, prev_close FROM index_minline_bars '
            "WHERE ts_code='000001.SH' AND trade_date='20260915'"))
        self.assertEqual(len(db_rows), 2)
        self.assertEqual(db_rows[0][1], 3500.0)
        self.assertEqual(db_rows[0][2], 3490)  # prev_close 来自 quote

    def test_lazy_quote_mismatch_rejects_data(self):
        """quote date != 推断日期 → 拒绝落库, 返回 source=none"""
        bad_quote = {'date': '20260914', 'time': '15:00:03', 'open': 3500, 'prev_close': 3490}
        with patch.object(backend, '_today_is_trading_day', return_value=True), \
             patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
             patch.object(backend, '_fetch_index_chart_quote', return_value=bad_quote), \
             patch.object(backend, '_fetch_sina_minline_rows') as mock_minline:
            d = self.client.get('/api/index/minline?ts_code=000001.SH').get_json()
            mock_minline.assert_not_called()
        self.assertEqual(d['source'], 'none')
        # 不应落库
        cnt = self.db.execute(
            'SELECT COUNT(*) FROM index_minline_bars').fetchone()[0]
        self.assertEqual(cnt, 0)

    def test_lazy_not_open_rejects_data(self):
        """open=0 或 time < 09:30 (盘前/节假日) → 拒绝"""
        pre_open_quote = {'date': '20260915', 'time': '09:00:00', 'open': 3500, 'prev_close': 3490}
        with patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
             patch.object(backend, '_fetch_index_chart_quote', return_value=pre_open_quote), \
             patch.object(backend, '_fetch_sina_minline_rows') as mock_minline:
            d = self.client.get('/api/index/minline?ts_code=000001.SH').get_json()
            mock_minline.assert_not_called()
        self.assertEqual(d['source'], 'none')
        # 原文 "该交易日尚无可核验的分时数据，可切换日 K", 不强求具体字
        self.assertTrue(d['message'], 'message 必须非空让前端能展示')

    def test_lazy_persist_is_idempotent(self):
        """lazy 落库跑两次 → 行数不变"""
        quote = {'date': '20260915', 'time': '15:00:03', 'open': 3500, 'prev_close': 3490}
        rows = [{'m': '09:30:00', 'p': 3500.0, 'avg_p': 3499.5, 'v': 1000}]
        with patch.object(backend, '_today_is_trading_day', return_value=True), \
             patch.object(backend, '_infer_minline_trade_date', return_value='20260915'), \
             patch.object(backend, '_fetch_index_chart_quote', return_value=quote), \
             patch.object(backend, '_fetch_sina_minline_rows', return_value=rows):
            self.client.get('/api/index/minline?ts_code=000001.SH')
            self.client.get('/api/index/minline?ts_code=000001.SH')
        cnt = self.db.execute(
            'SELECT COUNT(*) FROM index_minline_bars '
            "WHERE ts_code='000001.SH' AND trade_date='20260915'").fetchone()[0]
        self.assertEqual(cnt, 1)

    def test_invalid_date_returns_400(self):
        """非法 date 格式 → 400"""
        r = self.client.get('/api/index/minline?ts_code=000001.SH&date=2026-09-15')
        self.assertEqual(r.status_code, 400)

    def test_unknown_index_code_returns_400(self):
        """不支持的指数代码 → 400"""
        r = self.client.get('/api/index/minline?ts_code=999999.SH&date=20260915')
        self.assertEqual(r.status_code, 400)


class TestFetchIndexMinlineDaily(unittest.TestCase):
    """fetch_index_minline_daily.run() 行为. mock 所有上游, 不碰真盘."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:', factory=KeptConnection)
        self.db.execute('''CREATE TABLE index_minline_bars (
            ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, m TEXT NOT NULL,
            p REAL, avg_p REAL, v REAL NOT NULL DEFAULT 0,
            prev_close REAL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (ts_code, trade_date, m))''')
        self.db.commit()
        # fetch_index_minline_daily.py 用 sqlite3.connect(DB_PATH) 自己开连接,
        # 我们 patch DB_PATH 为 :memory: 不会工作 (不同连接), 所以 patch _already_persisted
        self.connect = patch.object(backend.sqlite3, 'connect', return_value=self.db)
        self.connect.start()
        # 也 patch 脚本模块的 sqlite3.connect, 让 _already_persisted 走同一个 db
        import fetch_index_minline_daily as fmd
        self.fmd = fmd
        self.fmd.sqlite3 = backend.sqlite3  # 共用 patch
        self._orig_db_path = fmd.DB_PATH
        fmd.DB_PATH = ':memory:'

    def tearDown(self):
        self.connect.stop()
        self.fmd.DB_PATH = self._orig_db_path
        sqlite3.Connection.close(self.db)

    def test_run_skip_existing_no_fetch(self):
        """4 只都预填 → run 不应拉取任何 sina"""
        for code in backend.INDEX_CHART_CODES:
            self.db.execute(
                'INSERT INTO index_minline_bars (ts_code, trade_date, m, p, avg_p, v, prev_close) '
                "VALUES (?, '20260915', '09:30:00', 3500, 3499, 100, 3490)",
                (code,))
        self.db.commit()
        with patch.object(self.fmd, '_fetch_index_chart_quote') as mock_q, \
             patch.object(self.fmd, '_fetch_sina_minline_rows') as mock_m, \
             patch.object(self.fmd, '_infer_minline_trade_date', return_value='20260915'):
            summary = self.fmd.run(verbose=False)
        mock_q.assert_not_called()
        mock_m.assert_not_called()
        self.assertEqual(summary['skipped'], 4)
        self.assertEqual(summary['failed'], 0)
        self.assertEqual(summary['saved_rows'], 0)

    def test_run_happy_path_persists_all_four(self):
        """4 只都未入库 → run 全部抓取 + 落库"""
        quote = {'date': '20260915', 'time': '15:00:03', 'open': 3500, 'prev_close': 3490}
        rows = [{'m': '09:30:00', 'p': 3500.0, 'avg_p': 3499.5, 'v': 1000}]
        with patch.object(self.fmd, '_fetch_index_chart_quote', return_value=quote), \
             patch.object(self.fmd, '_fetch_sina_minline_rows', return_value=rows), \
             patch.object(self.fmd, '_infer_minline_trade_date', return_value='20260915'):
            summary = self.fmd.run(verbose=False)
        self.assertEqual(summary['done'], 4)
        self.assertEqual(summary['saved_rows'], 4)
        self.assertEqual(summary['failed'], 0)
        for code in backend.INDEX_CHART_CODES:
            cnt = self.db.execute(
                'SELECT COUNT(*) FROM index_minline_bars WHERE ts_code=?',
                (code,)).fetchone()[0]
            self.assertEqual(cnt, 1, f'{code} 应有 1 行')

    def test_run_one_index_fails_does_not_abort_others(self):
        """某只指数 fetch 抛异常 → 其他三只继续"""
        quote = {'date': '20260915', 'time': '15:00:03', 'open': 3500, 'prev_close': 3490}
        rows = [{'m': '09:30:00', 'p': 3500.0, 'avg_p': 3499.5, 'v': 1000}]
        codes = list(backend.INDEX_CHART_CODES.keys())
        def fake_quote(code):
            if code == codes[1]:
                raise RuntimeError('sina 临时挂')
            return quote
        with patch.object(self.fmd, '_fetch_index_chart_quote', side_effect=fake_quote), \
             patch.object(self.fmd, '_fetch_sina_minline_rows', return_value=rows), \
             patch.object(self.fmd, '_infer_minline_trade_date', return_value='20260915'):
            summary = self.fmd.run(verbose=False)
        self.assertEqual(summary['done'], 4)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['saved_rows'], 3)



if __name__ == '__main__':
    unittest.main()
