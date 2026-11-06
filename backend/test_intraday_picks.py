"""Live inputs and real screen contracts using an isolated fixture database."""
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
import dip_picks as dip
import custom_picks as custom
from intraday_picks import IntradayPicks, picks_connection, fresh, local_now, fetch_eastmoney_intraday
import test_dip_picks as fixtures
from test_custom_picks import config


class IntradayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DipTests()
        self.fixture.setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'fixture.db'
        self.day = self.fixture.date
        self.now = datetime.strptime(self.day+' 13:31:00', '%Y%m%d %H:%M:%S')
        db = self.fixture.db
        db.execute('CREATE TABLE stock_basic(ts_code TEXT,status TEXT)')
        db.execute("INSERT INTO stock_basic VALUES ('000001.SZ','listed')")
        for key, kind in [('updated_at','TEXT'),('super_net','REAL'),('big_net','REAL'),('mid_net','REAL'),('small_net','REAL')]:
            db.execute(f'ALTER TABLE fund_flow ADD COLUMN {key} {kind}')
        db.execute('ALTER TABLE overview ADD COLUMN upCount INTEGER')
        db.execute('ALTER TABLE overview ADD COLUMN flatCount INTEGER')
        db.execute('UPDATE fund_flow SET updated_at=? WHERE trade_date=?', (self.now.strftime('%Y-%m-%d %H:%M:%S'),self.day))
        row = db.execute('SELECT open,high,low,close,volume FROM stock_daily WHERE trade_date=?',(self.day,)).fetchone()
        prev = db.execute('SELECT close FROM stock_daily WHERE trade_date=?',(self.fixture.days[119],)).fetchone()[0]
        stamp = self.now.strftime('%Y-%m-%d %H:%M:%S')
        self.quotes = {'000001.SZ':dict(open=row[0],high=row[1],low=row[2],price=row[3],volume=row[4],amount=1e8,prev_close=prev,time=stamp,name='盘中测试'),
                       '000001.SH':dict(price=99,prev_close=100,time=stamp)}
        db.commit()
        disk = sqlite3.connect(self.path)
        db.backup(disk)
        disk.close()
        self.loader = Mock(side_effect=lambda codes,**kw:{k:self.quotes[k].copy() for k in codes if k in self.quotes})
        self.provider = IntradayPicks(self.loader, lambda:self.now)

    def tearDown(self):
        self.fixture.tearDown()
        self.temp.cleanup()

    def connection(self, **kw):
        return picks_connection(self.path,self.provider,self.day,True,**kw)

    def test_live_price_and_units_use_today_once_without_disk_writes(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.connection() as conn:
            p = custom.screen_custom(conn,self.day,config('amount == 1','close > MA(20)','volume / AVG(volume,5,1) == 1'),self.now)
            self.assertTrue(p['selection_ready'])
            self.assertFalse(p['daily_ready'])
            self.assertEqual(p['count'],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stock_daily WHERE trade_date=?',(self.day,)).fetchone()[0],1)
            self.assertEqual(p['results'][0]['main_net_today'],3e6)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('INSERT INTO stock_daily(ts_code) VALUES (?)',('bogus',))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)

    def test_lists_counts_and_diagnostics_share_the_same_frozen_batch(self):
        with self.connection() as conn:
            p = dip.screen_dip(conn,self.day,self.now,disabled='market_down')
            c = dip.dip_condition_counts(conn,self.day,self.now)
            d = dip.dip_diagnostics(conn,self.day,self.now,disabled='market_down')
            self.assertEqual(p['count'],1)
            self.assertEqual(p['count'],d['current_count'])
            self.assertEqual(c['counts']['volume']['matched'],1)
            sid = p['snapshot_id']
        self.quotes['000001.SZ']['price'] = 1
        with self.connection(snapshot_id=sid) as conn:
            self.assertEqual(dip.screen_dip(conn,self.day,self.now,disabled='market_down')['count'],1)
        with self.connection(refresh=True) as conn:
            self.assertNotEqual(conn.intraday_context['snapshot_id'],sid)
            self.assertEqual(dip.screen_dip(conn,self.day,self.now,disabled='market_down')['count'],0)

    def test_stale_funds_do_not_block_price_only_or_fill_with_yesterday(self):
        db=sqlite3.connect(self.path)
        db.execute('UPDATE fund_flow SET updated_at=? WHERE trade_date=?',((self.now-timedelta(minutes=6)).strftime('%Y-%m-%d %H:%M:%S'),self.day));db.commit();db.close()
        with self.connection() as conn:
            p=dip.screen_dip(conn,self.day,self.now,disabled='market_down')
            self.assertEqual(p['stats']['missing_funds'],1)
            self.assertEqual(p['count'],0)
            p=dip.screen_dip(conn,self.day,self.now,disabled='market_down,funds_red,funds_threshold')
            self.assertEqual(p['count'],1)
            self.assertIsNone(p['results'][0]['main_net_today'])
            p=custom.screen_custom(conn,self.day,config('main_net > 0','close > 0',mode='any'),self.now)
            self.assertEqual(p['count'],1)
            self.assertEqual(p['counts']['r0']['missing'],1)

    def test_wrong_day_stale_missing_time_and_invalid_ohlc_are_rejected(self):
        original=self.quotes['000001.SZ'].copy()
        for patch in [dict(time=self.now.strftime('%Y-%m-%d')),dict(time=(self.now-timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')),
                      dict(time=(self.now-timedelta(minutes=3)).strftime('%Y-%m-%d %H:%M:%S')),dict(low=999),dict(amount=float('inf'))]:
            self.quotes['000001.SZ']={**original,**patch}
            with self.connection(refresh=True) as conn:
                self.assertFalse(conn.intraday_context['selection_ready'])
        self.quotes['000001.SZ']=original

    def test_chase_live_changes_not_limit_and_custom_windows(self):
        q=self.quotes['000001.SZ'];q.update(price=q['prev_close']*1.06,high=q['prev_close']*1.07,low=q['prev_close']*.99)
        self.quotes['000001.SH']['price']=101
        with self.connection() as conn:
            p=dip.screen_dip_v2(conn,self.day,self.now,disabled='bullish')
            self.assertEqual(p['count'],1)
            self.assertAlmostEqual(p['results'][0]['change_pct'],6)
            c=custom.screen_custom(conn,self.day,config('change > 5','not limit_up','REF(close,1) < close'),self.now)
            self.assertEqual(c['count'],1)
            self.assertEqual(dip.chase_diagnostics(conn,self.day,self.now,disabled='bullish')['current_count'],p['count'])

    def test_incomplete_breadth_is_unknown_near_threshold_but_can_prove_bounds(self):
        with self.connection() as conn:
            conn.intraday_breadth.update(down=2599,unknown=2)
            self.assertEqual(dip._market_gate_v2(conn,self.day,[self.day])['status'],'missing')
            conn.intraday_breadth.update(down=2601,unknown=2)
            self.assertTrue(dip._market_gate(conn,self.day,self.fixture.days[:121])['passed'])
            conn.intraday_breadth.update(down=1000,unknown=100)
            self.quotes['000001.SH']['price']=101
            self.assertIsNone(custom.Evaluator({}, {}, {}, []).field('down_count',0))

    def test_lunch_anchor_and_afternoon_stale_quote(self):
        lunch=local_now(self.now.replace(hour=12,minute=45))
        stamp=self.now.replace(hour=11,minute=30).strftime('%Y-%m-%d %H:%M:%S')
        self.assertTrue(fresh(stamp,lunch,120))
        self.assertFalse(fresh(stamp,local_now(self.now),120))

    def test_provider_updates_after_close_are_not_misclassified_as_future(self):
        now=local_now(self.now.replace(hour=15,minute=10))
        self.assertTrue(fresh(now.strftime('%Y-%m-%d %H:%M:%S'),now,120))
        self.assertTrue(fresh(now.replace(minute=0).strftime('%Y-%m-%d %H:%M:%S'),now,120))
        self.assertFalse(fresh((now+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'),now,120))

    def test_em_provider_preserves_zero_and_missing_fields_and_checks_actual_time(self):
        now=local_now(self.now)
        item=dict(f12='000001',f13=0,f124=now.timestamp(),f62=0,f66=-1e6,f72=2e6,f78='-',f84=3e6)
        session=Mock()
        session.get.return_value.json.return_value={'data':{'total':2,'diff':[
            item,dict(item,f12='000002',f124=(now-timedelta(days=1)).timestamp())]}}
        with patch('requests.Session',return_value=session):
            rows=fetch_eastmoney_intraday(now)
        self.assertEqual(list(rows),['000001.SZ'])
        self.assertEqual(rows['000001.SZ']['main_net_inflow'],0)
        self.assertIsNone(rows['000001.SZ']['mid_net'])
        self.assertEqual(rows['000001.SZ']['big_net'],2e6)
        self.assertEqual(rows['000001.SZ']['source'],'eastmoney-push2')
        session.close.assert_called_once()

    def test_live_em_funds_replace_stale_local_values_only_in_memory(self):
        db=sqlite3.connect(self.path)
        db.execute('UPDATE fund_flow SET updated_at=? WHERE trade_date=?',('2000-01-01 00:00:00',self.day));db.commit();db.close()
        row=dict(ts_code='000001.SZ',trade_date=self.day,source='eastmoney-push2',main_net_inflow=4e6,
                 updated_at=self.now.strftime('%Y-%m-%d %H:%M:%S'))
        funds=Mock(return_value={'000001.SZ':row})
        self.provider.fund_loader=funds
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.connection() as conn:
            p=dip.screen_dip(conn,self.day,self.now,disabled='market_down')
            self.assertEqual(p['count'],1)
            self.assertEqual(p['results'][0]['main_net_today'],4e6)
            self.assertEqual(p['results'][0]['main_net_20d'],61e6)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        funds.assert_called_once()

    def test_failed_live_fund_request_retains_only_fresh_local_records(self):
        self.provider.fund_loader=Mock(side_effect=RuntimeError('unavailable'))
        with self.connection(refresh=True) as conn:
            p=dip.screen_dip(conn,self.day,self.now,disabled='market_down')
            self.assertEqual(p['count'],1)
            self.assertEqual(p['results'][0]['main_net_today'],3e6)
            self.assertIn('读取失败',p['intraday']['note'])

    def test_expired_batch_cannot_silently_switch_input(self):
        with self.connection() as conn:
            sid=conn.intraday_context['snapshot_id']
        self.now += timedelta(minutes=16)
        with self.assertRaisesRegex(ValueError,'过期'):
            with self.connection(snapshot_id=sid):pass

    def test_partial_close_is_still_preselection(self):
        self.now=self.now.replace(hour=16)
        db=sqlite3.connect(self.path)
        # Yesterday had ten symbols; today's lone loaded bar is not a complete close.
        db.executemany('INSERT INTO stock_daily SELECT ?,name,trade_date,open,high,low,close,change,volume,amount FROM stock_daily WHERE ts_code=? AND trade_date=?',
            [(f'{i:06d}.SZ','000001.SZ',self.fixture.days[119]) for i in range(2,12)])
        db.commit();db.close()
        self.quotes['000001.SZ']['time']=self.now.replace(hour=15,minute=0).strftime('%Y-%m-%d %H:%M:%S')
        with self.connection() as conn:
            p=dip.screen_dip(conn,self.day,self.now,disabled='market_down,market_index,funds_red,funds_threshold')
            self.assertFalse(p['daily_ready'])
            self.assertTrue(p['selection_ready'])
            self.assertIn('等待完整日线',p['note'] if '等待完整日线' in p['note'] else conn.intraday_context['note'])

    def test_preopen_and_holiday_do_not_fetch_quotes(self):
        self.now=self.now.replace(hour=9,minute=0)
        with self.connection() as conn:
            self.assertFalse(dip.screen_dip(conn,self.day,self.now)['daily_ready'])
        self.loader.assert_not_called()
        self.now=self.now.replace(hour=13)
        db=sqlite3.connect(self.path);db.execute('DELETE FROM trading_dates_cache WHERE cal_date=?',(self.day,));db.commit();db.close()
        with self.connection() as conn:
            self.assertFalse(dip.screen_dip(conn,self.day,self.now)['selection_ready'])
        self.loader.assert_not_called()

    def test_confirmed_close_and_historical_requests_never_fetch(self):
        self.now=self.now.replace(hour=16)
        with self.connection() as conn:
            p=dip.screen_dip(conn,self.day,self.now)
            self.assertTrue(p['daily_ready'])
            self.assertNotIn('snapshot_id',p)
        self.loader.assert_not_called()

    def test_http_preselection_never_computes_future_outcomes(self):
        app=Flask(__name__);outcomes=Mock()
        dip.register_dip_picks(app,lambda:str(self.path),outcome_loader=outcomes,intraday_provider=self.provider)
        custom.register_custom_picks(app,lambda:str(self.path),outcome_loader=outcomes,intraday_provider=self.provider)
        # Request-scoped readiness uses actual injectable provider time, not wall time.
        with app.test_client() as client:
            p=client.get(f'/api/picks/dip?date={self.day}&intraday=1&disabled=market_down').get_json()
            self.assertEqual(p['count'],1)
            self.assertFalse(p['daily_ready'])
            c=client.post('/api/picks/custom/screen',json=dict(date=self.day,intraday=True,config=config('close>0'))).get_json()
            self.assertEqual(c['count'],1)
            self.assertEqual(client.get(f'/api/picks/dip?date={self.day}&intraday=1&snapshot_id=missing').status_code,400)
        outcomes.assert_not_called()


if __name__=='__main__':
    unittest.main()
