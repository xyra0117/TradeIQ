"""Surge rule boundaries, no lookahead, readiness and read-only route contract."""
import math
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from flask import Flask
import surge_picks as surge


class KeepConnection(sqlite3.Connection):
    def close(self):
        pass


class SurgeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', factory=KeepConnection)
        self.db.executescript('''
          CREATE TABLE trading_dates_cache (cal_date TEXT);
          CREATE TABLE stock_daily (ts_code TEXT,name TEXT,trade_date TEXT,open REAL,high REAL,
            low REAL,close REAL,change REAL,volume REAL,amount REAL);
          CREATE TABLE fund_flow (ts_code TEXT,trade_date TEXT,source TEXT,main_net_inflow REAL);
          CREATE TABLE stock_sector_summary (ts_code TEXT,sectors_json TEXT);
        ''')
        start = datetime(2026, 1, 5)
        self.days = [(start + timedelta(days=i)).strftime('%Y%m%d') for i in range(70)
                     if (start + timedelta(days=i)).weekday() < 5]
        self.db.executemany('INSERT INTO trading_dates_cache VALUES (?)', [(d,) for d in self.days])
        self.date = self.days[20]
        self.now = datetime(2026, 6, 1, 16)
        self.add_stock('000001.SZ')

    def tearDown(self):
        sqlite3.Connection.close(self.db)

    def add_stock(self, code, growth=.01, amplitude=6, total=21):
        previous = 100
        for i, day in enumerate(self.days[:total]):
            close = 100 * (1 + growth) ** i
            spread = previous * amplitude / 200
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (code, '测试股票', day, close, close + spread, close - spread,
                             close, (close / previous - 1) * 100, 100, 100))
            self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)',
                            (code, day, 'eastmoney-push2', 5000))
            previous = close

    def screen(self, date=None, now=None):
        return surge.screen_surge(self.db, self.date if date is None else date, now or self.now)

    def test_core_fields_and_fund_unit_boundary(self):
        r = self.screen()['results'][0]
        self.assertAlmostEqual(r['amplitude20'], 6)
        self.assertAlmostEqual(r['ma20'], sum(100 * 1.01 ** i for i in range(1, 21))/20)
        self.assertTrue(r['momentum'])
        self.assertFalse(r['breakout'])
        self.assertEqual(r['main_strength5'], 5)
        self.assertTrue(r['fund_tag'])
        self.assertEqual(r['outcome10']['status'], 'pending')

    def test_amplitude_inclusive_boundary(self):
        self.add_stock('000002.SZ', amplitude=5)
        self.add_stock('000003.SZ', amplitude=4.999)
        self.assertEqual([r['ts_code'] for r in self.screen()['results']], ['000001.SZ', '000002.SZ'])

    def test_ret20_strictly_greater_than_15(self):
        self.add_stock('000002.SZ', growth=1.15 ** (1/20)-1)
        result = self.screen()
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['stats']['no_momentum'], 1)

    def test_breakout_is_alternative_not_and_and_excludes_today_high(self):
        self.add_stock('000002.SZ', growth=0)
        self.db.execute('UPDATE stock_daily SET open=110,close=110,high=114,low=108,change=10 '
                        'WHERE ts_code=? AND trade_date=?', ('000002.SZ', self.date))
        hit = next(r for r in self.screen()['results'] if r['ts_code']=='000002.SZ')
        self.assertFalse(hit['momentum'])
        self.assertTrue(hit['breakout'])
        self.assertEqual(hit['previous_high20'], 103)

    def test_close_equal_ma20_is_excluded(self):
        self.add_stock('000002.SZ', growth=0)
        self.assertEqual(self.screen()['stats']['below_ma20'], 1)

    def test_missing_history_is_not_skipped(self):
        self.db.execute('DELETE FROM stock_daily WHERE trade_date=?', (self.days[10],))
        data = self.screen()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['stats']['missing_history'], 1)

    def test_reference_mismatch_and_bad_ohlc(self):
        self.db.execute('UPDATE stock_daily SET change=15 WHERE trade_date=?', (self.days[10],))
        self.assertEqual(self.screen()['stats']['reference_mismatch'], 1)
        self.db.execute('UPDATE stock_daily SET high=0 WHERE trade_date=?', (self.date,))
        self.assertEqual(self.screen()['stats']['invalid_prices'], 1)

    def test_missing_funds_do_not_exclude_or_zero_fill(self):
        self.db.execute('DELETE FROM fund_flow WHERE trade_date=?', (self.days[18],))
        self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)', ('000001.SZ', self.days[18], 'tushare', 1e12))
        data = self.screen()
        self.assertEqual(data['count'], 1)
        self.assertIsNone(data['results'][0]['main_strength5'])
        self.assertFalse(data['results'][0]['fund_tag'])
        self.assertEqual(data['stats']['missing_funds'], 1)

    def test_amount_missing_and_below_fund_threshold(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=4999')
        self.assertFalse(self.screen()['results'][0]['fund_tag'])
        self.db.execute('UPDATE stock_daily SET amount=0 WHERE trade_date=?', (self.date,))
        self.assertIsNone(self.screen()['results'][0]['main_strength5'])

    def test_future_data_never_affects_membership(self):
        before = self.screen()['results'][0].copy()
        anchor = before['close']
        for i, day in enumerate(self.days[21:31]):
            close = anchor * 1.01 ** (i+1)
            previous = anchor * 1.01 ** i
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                            ('000001.SZ','测试股票',day,close,close*1.02,close*.98,close,1,100,100))
            self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?)',('000001.SZ',day,'eastmoney-push2',1e12))
        after = self.screen()['results'][0].copy()
        outcome = after.pop('outcome10'); before.pop('outcome10')
        self.assertEqual(before, after)
        self.assertEqual(outcome['status'], 'complete')
        self.assertAlmostEqual(outcome['high_pct'], (1.01**10*1.02-1)*100)
        self.assertAlmostEqual(outcome['close_pct'], (1.01**10-1)*100)
        self.db.execute('DELETE FROM stock_daily WHERE trade_date=?', (self.days[24],))
        self.assertEqual(self.screen()['results'][0]['outcome10']['status'], 'missing')

    def test_outcome_uses_running_high_not_sum(self):
        anchor = {'close': 100}
        rows = {}
        previous = 100
        for i, day in enumerate(self.days[21:31]):
            close = 105 if i == 0 else 100
            rows[day] = dict(open=close,close=close,high=120 if i==0 else 101,low=99,
                             volume=100,change=(close/previous-1)*100)
            previous=close
        out=surge._outcome(rows,self.days[21:31],anchor,self.now)
        self.assertAlmostEqual(out['high_pct'],20)
        self.assertEqual(out['close_pct'],0)
        rows[self.days[23]]['change']=10
        self.assertEqual(surge._outcome(rows,self.days[21:31],anchor,self.now)['status'],'reference_mismatch')

    def test_intraday_and_latest_fallback(self):
        now=datetime.strptime(self.date+' 11:00','%Y%m%d %H:%M')
        explicit=self.screen(now=now)
        self.assertFalse(explicit['daily_ready'])
        self.assertEqual(explicit['results'],[])
        latest=self.screen(date='',now=now)
        self.assertEqual(latest['date'],self.days[19])

    def test_partial_current_batch_not_ready(self):
        self.add_stock('000002.SZ',total=20)
        self.add_stock('000003.SZ',total=20)
        now=datetime.strptime(self.date+' 16:00','%Y%m%d %H:%M')
        self.assertFalse(self.screen(now=now)['daily_ready'])
        self.assertEqual(self.screen(date='',now=now)['date'],self.days[19])

    def test_bad_future_and_missing_dates(self):
        for d in ['20260230','2026-01-30','bad']:
            with self.assertRaises(ValueError):self.screen(d)
        self.assertFalse(self.screen('20270101')['daily_ready'])
        self.assertFalse(self.screen('20260501')['daily_ready'])

    def test_route_uses_readonly_transaction_and_no_writes(self):
        self.db.commit()
        app=Flask(__name__);surge.register_surge_picks(app,lambda:'/tmp/not-used.db')
        statements=[];self.db.set_trace_callback(statements.append)
        with patch.object(surge.sqlite3,'connect',return_value=self.db) as connect:
            response=app.test_client().get('/api/picks/surge?date='+self.date)
        self.assertEqual(response.status_code,200)
        self.assertIn('mode=ro',connect.call_args.args[0])
        self.assertTrue(connect.call_args.kwargs['uri'])
        self.assertEqual(self.db.execute('PRAGMA query_only').fetchone()[0],1)
        self.assertFalse(any(sql.split()[0].upper() in ('INSERT','UPDATE','DELETE','CREATE','ALTER') for sql in statements))
        self.assertEqual(response.headers['Cache-Control'],'no-store')


if __name__=='__main__':unittest.main()
