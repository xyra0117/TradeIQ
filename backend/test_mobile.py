"""Mobile regressions: isolated fixture files; never opens the real market database."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_app import create_app
from mobile_data import connect_readonly, SavedData, stock_code


def seed_fixture(path):
    c = sqlite3.connect(path)
    c.executescript('''
    CREATE TABLE index_daily (date TEXT,name TEXT,open REAL,high REAL,low REAL,close REAL,change REAL,volume REAL);
    CREATE TABLE overview (date TEXT,totalVolume REAL,upCount INTEGER,downCount INTEGER,flatCount INTEGER,limitUp INTEGER,limitDown INTEGER,netFlow REAL);
    CREATE TABLE stock_daily (ts_code TEXT,trade_date TEXT,name TEXT,open REAL,high REAL,low REAL,close REAL,change REAL,volume REAL,amount REAL);
    CREATE TABLE leaderboard (date TEXT,type TEXT,rank INTEGER,code TEXT,name TEXT,change REAL,reason TEXT);
    CREATE TABLE limitup (date TEXT,code TEXT,name TEXT,marketCap REAL,time TEXT,sector TEXT,volume REAL,streak TEXT,keyword TEXT);
    CREATE TABLE limitup_collection (trade_date TEXT,ts_code TEXT,name TEXT,market TEXT,sealed INTEGER,broken INTEGER,first_limit_time TEXT,last_seen_time TEXT);
    CREATE TABLE fund_flow (trade_date TEXT,ts_code TEXT,code TEXT,name TEXT,source TEXT,close REAL,change_pct REAL,main_net_inflow REAL,main_net_pct REAL,super_net REAL,big_net REAL);
    CREATE TABLE fund_flow_ths (trade_date TEXT,ts_code TEXT,code TEXT,name TEXT,net REAL,close REAL,change_pct REAL,turnover_pct REAL);
    CREATE TABLE stock_picks (trade_date TEXT,ts_code TEXT,name TEXT,score REAL,close REAL,change_pct REAL,main_net_today REAL,ma5 REAL,ma10 REAL);
    CREATE TABLE stock_picks_ths AS SELECT * FROM stock_picks WHERE 0;
    CREATE TABLE conditional_picks_snapshot (trade_date TEXT,results_json TEXT,saved_at TEXT);
    CREATE TABLE combined_picks_snapshot (trade_date TEXT,results_json TEXT,saved_at TEXT);
    CREATE TABLE conditional_picks_snapshot_ths (trade_date TEXT,results_json TEXT,saved_at TEXT);
    CREATE TABLE combined_picks_snapshot_ths (trade_date TEXT,results_json TEXT,saved_at TEXT);
    CREATE TABLE positions (ts_code TEXT,name TEXT,shares REAL,cost_price REAL,closed_at TEXT,created_at TEXT);
    CREATE TABLE user_picks (ts_code TEXT,added_at TEXT);
    CREATE TABLE user_sectors (id INTEGER,name TEXT,note TEXT,pinned INTEGER);
    CREATE TABLE user_sector_stocks (sector_id INTEGER,ts_code TEXT,name TEXT,role TEXT,rank INTEGER);
    CREATE TABLE market_fflow (trade_date TEXT,times TEXT,series TEXT,last TEXT,pct TEXT,complete INTEGER);
    ''')
    codes=[('000001.SH','上证指数',3800),('399001.SZ','深证成指',12800),('399006.SZ','创业板指',2700),('000688.SH','科创50',1200)]
    from datetime import date, timedelta
    for i in range(119):
        day=(date(2026,5,15)+timedelta(days=i)).strftime('%Y%m%d')
        for code,name,base in codes:
            close=base+i*2+(i%7)*4
            c.execute('INSERT INTO index_daily VALUES (?,?,?,?,?,?,?,?)',(day,name,close-5,close+9,close-12,close,.84,5200+i*10))
        c.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',('600001.SH',day,'示例科技',11,13,10,12,2.5,12000,150000))
    c.execute("INSERT INTO index_daily VALUES ('20260911','上证指数',NULL,4000,3900,3950,1.2,6800)")
    c.execute("INSERT INTO stock_daily VALUES ('600001.SH','20260911','示例科技',12,14,11,13,3.2,15000,200000)")
    c.execute("INSERT INTO stock_daily VALUES ('600001.SH','20260912','示例科技',13,15,12,14,4,15000,210000)")
    c.execute("INSERT INTO stock_daily VALUES ('000002.SZ','2026-09-11','日期测试',10,12,9,11,1,100,100000)")
    c.execute("INSERT INTO stock_daily VALUES ('000002.SZ','2026-09-12','日期测试',11,13,10,12,2,100,100000)")
    for code,name,base in codes[1:]:
        c.execute('INSERT INTO index_daily VALUES (?,?,?,?,?,?,?,?)',('20260911',name,base,base+20,base-10,base+10,-.56,3200))
    c.execute("INSERT INTO overview VALUES ('20260911',21800,3250,1980,81,78,5,10)")
    for period in ('5日','10日','20日'):
        c.execute("INSERT INTO leaderboard VALUES ('20260911',?,1,'600001','示例科技',35,'测试样本')",(period,))
    c.execute("INSERT INTO limitup VALUES ('20260911','600001','示例科技',120,'09:35','机器人',15,'2板','测试')")
    c.execute("INSERT INTO limitup_collection VALUES ('20260911','600001.SH','示例科技','主板',1,0,'09:35','15:00')")
    c.execute("INSERT INTO fund_flow VALUES ('20260911','600001.SH','600001','示例科技','eastmoney-push2',999,999,800000000,12,500000000,300000000)")
    c.execute("INSERT INTO fund_flow_ths VALUES ('20260911','600001.SH','600001','示例科技',58000,999,999,4)")
    c.execute("INSERT INTO stock_picks VALUES ('20260911','600001.SH','示例科技',91,13,3.2,800000000,12,11)")
    c.execute("INSERT INTO stock_picks_ths SELECT * FROM stock_picks")
    for table in ('conditional_picks_snapshot','combined_picks_snapshot','conditional_picks_snapshot_ths','combined_picks_snapshot_ths'):
        c.execute(f'INSERT INTO {table} VALUES (?,?,?)',('20260911',json.dumps([{'ts_code':'600001.SH','name':'示例科技','close':13,'change_pct':3.2,'sum_3d':800000000}]),'2026-09-11 17:00:00'))
    c.execute("INSERT INTO positions VALUES ('600001.SH','示例科技',1000,10,NULL,'2026-09-01')")
    c.execute("INSERT INTO positions VALUES ('000999.SZ','缺失价格示例',100,5,NULL,'2026-09-01')")
    c.execute("INSERT INTO user_picks VALUES ('600001.SH','2026-09-11')")
    c.execute("INSERT INTO user_sectors VALUES (1,'我的机器人','',1)")
    c.execute("INSERT INTO user_sector_stocks VALUES (1,'600001.SH','示例科技','core',1)")
    series={k:[0,10e8,20e8] for k in ('main','super','big','mid','small')}
    c.execute('INSERT INTO market_fflow VALUES (?,?,?,?,?,?)',('20260911',json.dumps(['09:30','11:30','15:00']),json.dumps(series),json.dumps({k:20 for k in series}),json.dumps({k:2 for k in series}),1))
    c.commit();c.close()


class MobileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'fixture.db'
        seed_fixture(self.path)
        self.client=create_app(self.path).test_client()
        self.before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.network=patch('socket.socket.connect',side_effect=AssertionError('Mobile must never request upstream data'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.assertEqual(self.before,hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.tmp.cleanup()

    def get(self,url):
        response=self.client.get('/api/mobile/'+url)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        return response.get_json()

    def test_eight_modules_use_saved_data_only(self):
        for module in ('index','leaderboard','limitup','lz','flow','positions','picks','sectors'):
            with self.subTest(module=module):
                d=self.get('modules/'+module+'?date=20260911')
                self.assertGreater(d['count'],0)
                self.assertTrue(d['readonly'])
                self.assertEqual(d['date'],'20260911')
        self.assertNotIn('scheduler',sys.modules)

    def test_date_is_exact_and_never_falls_back(self):
        for module in ('index','leaderboard','limitup','lz','flow','picks','sectors'):
            d=self.get('modules/'+module+'?date=19990101')
            self.assertEqual(d['items'],[])
            self.assertEqual(d['date'],'19990101')
        self.assertEqual(self.get('dates?module=index')['dates'][0],'20260911')

    def test_all_pick_sources_and_snapshot_provenance(self):
        for kind in ('system','ths','conditional','combined','conditional-ths','combined-ths','self'):
            d=self.get('modules/picks?kind='+kind+'&date=20260911')
            self.assertEqual(d['count'],1)
            if kind not in ('system','ths','self'):
                self.assertEqual(d['saved_at'],'2026-09-11 17:00:00')
        self.assertEqual(self.get('modules/flow?source=ths&date=20260911')['items'][0]['net'],58000)

    def test_snapshot_threshold_matches_desktop_default_without_recomputing(self):
        d=self.get('modules/picks?kind=conditional&date=20260911&threshold_yi=10')
        self.assertEqual(d['items'],[])
        self.assertEqual(d['threshold_yi'],10)
        d=self.get('modules/picks?kind=conditional&date=20260911')
        self.assertEqual(d['threshold_yi'],3)
        self.assertEqual(d['count'],1)

    def test_authoritative_price_does_not_use_flow_quote(self):
        for source in ('eastmoney-push2','ths'):
            r=self.get('modules/flow?source='+source+'&date=20260911')['items'][0]
            self.assertEqual(r['close'],13)
            self.assertEqual(r['change_pct'],3.2)

    def test_positions_saved_price_cutoff_missing_and_fee_convention(self):
        d=self.get('modules/positions?date=20260911')
        p=d['items'][0]
        self.assertEqual(p['current_price'],13)
        self.assertEqual(p['price_date'],'20260911')
        self.assertEqual(p['profit'],2988.37)
        self.assertEqual(p['profit_pct'],30)
        self.assertIsNone(d['items'][1]['current_price'])
        self.assertIsNone(d['items'][1]['profit'])
        self.assertEqual(d['summary']['priced_count'],1)

    def test_chart_cutoff_units_missing_ohlc_and_range(self):
        d=self.get('chart?kind=stock&ts_code=600001.SH&date=20260911&start=20260911')
        self.assertEqual(d['count'],1)
        self.assertEqual(d['rows'][0]['volume'],2)
        d=self.get('chart?kind=index&ts_code=000001.SH&date=20260911&start=20260911')
        self.assertEqual(d['missing_ohlc'],1)
        self.assertEqual(d['rows'][0]['volume'],6800)
        self.assertFalse(d['intraday'])

    def test_iso_date_cutoff_and_stock_market_code(self):
        with connect_readonly(self.path) as conn:
            row=SavedData(conn).daily('000002.SZ','20260911')
            self.assertEqual(row['close'],11)
            self.assertEqual(row['price_date'],'20260911')
        self.assertEqual(stock_code('900901'),'900901.SH')
        self.assertEqual(stock_code('920001'),'920001.BJ')

    def test_hot_sector_members_and_old_routes_removed(self):
        result = self.get('hot-sectors/detail?sector=机器人&date=20260911')
        self.assertEqual(result['detail']['stocks'][0]['ts_code'],'600001.SH')
        self.assertEqual(result['detail']['pending_count'],1)
        self.assertEqual(self.client.get('/api/mobile/sectors/detail?name=机器人').status_code,404)
        self.assertEqual(self.client.get('/api/mobile/user-sectors').status_code,404)

    def test_write_methods_unknown_routes_static_traversal_and_bad_host(self):
        for method in ('post','put','patch','delete','options'):
            self.assertEqual(getattr(self.client,method)('/api/mobile/modules/positions').status_code,405)
        for path in ('/api/positions','/api/index/sync','/market_data.db','/dashboard/index.html','/../backend/.env','/vendor/../../backend/app.py','/vendor/LICENSE-echarts.txt'):
            self.assertEqual(self.client.get(path).status_code,404,path)
        self.assertEqual(self.client.get('/',headers={'Host':'evil.example'}).status_code,400)
        with self.client.get('/') as response:
            self.assertEqual(response.status_code,200)
            self.assertNotIn('Access-Control-Allow-Origin',response.headers)

    def test_invalid_input_and_unavailable_database(self):
        for suffix in ('modules/index?date=20260230','modules/index?date=abc','modules/unknown','modules/flow?source=bad','modules/picks?kind=bad','chart?kind=index&ts_code=bad','chart?kind=stock&ts_code=600001.SH&start=20260912&date=20260911','modules/picks?threshold_yi=nan','modules/picks?threshold_yi=-1'):
            self.assertEqual(self.client.get('/api/mobile/'+suffix).status_code,400,suffix)
        absent=Path(self.tmp.name)/'absent.db'
        response=create_app(absent).test_client().get('/api/mobile/health')
        self.assertEqual(response.status_code,503)
        self.assertFalse(absent.exists())
        self.assertNotIn(str(absent),response.get_data(as_text=True))

    def test_database_write_blocked_even_if_future_handler_attempts_it(self):
        with connect_readonly(self.path) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('DELETE FROM positions')
            conn.execute('PRAGMA query_only=OFF')
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('INSERT INTO user_picks VALUES (?,?)',('600002.SH','20260911'))

    def test_missing_tables_render_empty_without_initialization(self):
        path=Path(self.tmp.name)/'empty.db'
        sqlite3.connect(path).close()
        client=create_app(path).test_client()
        for module in ('index','leaderboard','limitup','lz','flow','positions','picks','sectors'):
            response=client.get('/api/mobile/modules/'+module)
            self.assertEqual(response.status_code,200,response.get_json())
            self.assertEqual(response.get_json()['items'],[])
        with connect_readonly(path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
