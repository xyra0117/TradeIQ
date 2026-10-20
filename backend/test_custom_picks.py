"""Meaningful rule, data coverage and HTTP contracts; fixtures never touch live data."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from flask import Flask

import custom_picks as cp
import test_dip_picks as dip_fixture


def config(*formulas, mode='all'):
    return {'mode': 'all', 'groups': [{'name': '自定义组', 'mode': mode, 'rules': [
        {'id': f'r{i}', 'name': f'条件{i}', 'formula': f, 'enabled': True} for i, f in enumerate(formulas)]}]}


class FormulaTests(unittest.TestCase):
    def test_window_and_future_boundaries(self):
        self.assertEqual(cp.Formula('REF(close, 250) > 0').lookback, 250)
        self.assertEqual(cp.Formula('COUNT(change > 0, 10, 1) >= 7').lookback, 11)
        for formula in ['REF(close,-1)>0','MA(0)>0','SUM(close,251)>0',
                        'SUM(REF(close,2),250)>0','REF(close,change)>0']:
            with self.subTest(formula=formula), self.assertRaises(ValueError): cp.Formula(formula)

    def test_rejects_code_and_wrong_types(self):
        for formula in ["__import__('os').system('id')", 'close.__class__', 'close[0]',
                        '(lambda: True)()', '[close] > 0', 'close', '1',
                        'COUNT(close,10)>0', 'SUM(change>0,10)>0', 'close and change',
                        'close > limit_up', 'RET(5,1)>0', 'AVG(close, n=5)>0', '1e999 > 0',
                        'sum(close,5)>0', 'future_high > 0']:
            with self.subTest(formula=formula), self.assertRaises(ValueError): cp.Formula(formula)

    def test_chinese_draft_is_complete_or_blocked(self):
        result=cp.parse_chinese('最近10天至少7天主力流入，今天缩量，而且收盘站上20日线。')
        self.assertTrue(result['complete']); self.assertEqual(len(result['rules']),3)
        self.assertEqual(result['rules'][0]['formula'],'COUNT(main_net > 0, 10) >= 7')
        result=cp.parse_chinese('当日涨幅大于5%，机构即将拉升')
        self.assertFalse(result['complete']); self.assertEqual(result['unknown'],['机构即将拉升'])
        self.assertEqual(len(result['rules']),1)

    def test_chinese_money_range_or_and_breakout(self):
        samples={
            '10日主力累计净额大于1亿元':'SUM(main_net, 10) > 1',
            '主力净额大于5000万元':'main_net > 0.5',
            '下跌家数小于2600家':'down_count < 2600',
            '当日涨幅介于3%～8%':'3 <= change <= 8',
            '收盘突破此前20日最高价':'close > MAX(high, 20, 1)',
            '5日主力净额/同期成交额超过8%':'SUM(main_net, 5) / SUM(amount, 5) * 100 > 8',
            '当日成交量÷此前5日平均成交量小于1.5':'volume / AVG(volume, 5, 1) < 1.5',
            '连续3交易日成交量递减':'ALL(volume < REF(volume, 1), 3)',
        }
        for text, formula in samples.items():
            with self.subTest(text=text):
                result=cp.parse_chinese(text);self.assertTrue(result['complete'],result)
                self.assertEqual(result['rules'][0]['formula'],formula)
        self.assertTrue(cp.parse_chinese('今天上涨或者今天下跌')['complete'])
        self.assertFalse(cp.parse_chinese('10日主力净额大于5%')['complete'])
        self.assertFalse(cp.parse_chinese('当日涨幅介于8%～3%')['complete'])
        self.assertEqual(cp.parse_chinese('10日涨幅大于5%')['rules'][0]['formula'],'RET(10) > 5')
        self.assertFalse(cp.parse_chinese('10日收盘价大于5元')['complete'])

    def test_three_state_groups(self):
        c=config('close>0','close<0')
        self.assertIs(cp.combine(c,{'r0':False,'r1':None}),False)
        self.assertIsNone(cp.combine(c,{'r0':True,'r1':None}))
        c['groups'][0]['mode']='any'
        self.assertIs(cp.combine(c,{'r0':True,'r1':None}),True)
        self.assertIsNone(cp.combine(c,{'r0':False,'r1':None}))


class CustomFixture(unittest.TestCase):
    """共用测试库 (dip_picks 的假数据), 不碰真实行情库."""

    def setUp(self):
        self.fixture=dip_fixture.DipTests();self.fixture.setUp()
        self.db,self.date,self.now=self.fixture.db,self.fixture.date,self.fixture.now
        self.db.execute('ALTER TABLE fund_flow ADD COLUMN small_net REAL')
        self.db.execute('ALTER TABLE overview ADD COLUMN upCount INTEGER')
        self.db.execute('ALTER TABLE overview ADD COLUMN flatCount INTEGER')
        self.db.execute('UPDATE overview SET upCount=1000,flatCount=100')

    def tearDown(self): self.fixture.tearDown()
    def screen(self,c,**kwargs): return cp.screen_custom(self.db,self.date,c,self.now,**kwargs)


class CustomDataTests(CustomFixture):

    def test_amount_money_conversion_and_entry_outputs(self):
        self.db.execute('UPDATE stock_daily SET amount=100000')
        self.db.execute('UPDATE fund_flow SET main_net_inflow=10000000')
        result=self.screen(config('SUM(main_net,5) / SUM(amount,5)*100 == 10'))
        self.assertEqual(result['count'],1)
        self.assertEqual(result['results'][0]['main_net_today'],10000000)
        result=self.screen(config('amount == 1'))
        self.assertEqual(result['count'],1)
        self.assertEqual(result['results'][0]['main_net_today'],10000000)

    def test_numeric_windows_ratio_ret_and_abs(self):
        c=config('RET(120) > 99', 'close > MA(20)', 'volume / AVG(volume,5,1) == 1',
                 'MAX(close,20) >= close', 'MIN(close,20) < close', 'ABS(change) < 2')
        self.assertEqual(self.screen(c)['count'],1)
        self.assertAlmostEqual(self.screen(c)['results'][0]['change_pct'],-.1)

    def test_limit_rule_and_amplitude_use_previous_close(self):
        result=self.screen(config('not limit_up', 'amplitude >= 5'))
        self.assertEqual(result['count'],1)
        self.assertEqual(self.screen(config('limit_up'))['count'],0)

    def test_other_fund_provider_does_not_fill_em_gap(self):
        self.db.execute("UPDATE fund_flow SET source='ths' WHERE trade_date=?",(self.date,))
        result=self.screen(config('SUM(main_net,5)>0'))
        self.assertEqual(result['count'],0)
        self.assertEqual(result['counts']['r0']['missing'],1)

    def test_count_and_all_current_day_included(self):
        days=self.fixture.days[111:121]
        self.db.execute('UPDATE fund_flow SET main_net_inflow=-1')
        self.db.executemany('UPDATE fund_flow SET main_net_inflow=1 WHERE trade_date=?',[(d,) for d in days[:7]])
        self.assertEqual(self.screen(config('COUNT(main_net>0,10)>=7'))['count'],1)
        self.assertEqual(self.screen(config('COUNT(main_net>0,10)>7'))['count'],0)
        self.assertEqual(self.screen(config('ANY(main_net>0,10)'))['count'],1)
        self.assertEqual(self.screen(config('ALL(main_net>0,10)'))['count'],0)

    def test_continuous_decrease_and_cross(self):
        self.db.executemany('UPDATE stock_daily SET volume=? WHERE trade_date=?',
                            [(v,d) for v,d in zip([400,300,200,100],self.fixture.days[117:121])])
        self.assertEqual(self.screen(config('ALL(volume < REF(volume,1),3)'))['count'],1)
        self.db.execute('UPDATE stock_daily SET volume=150 WHERE trade_date=?',(self.fixture.days[119],))
        self.assertEqual(self.screen(config('CROSS(150,volume)'))['count'],1)
        self.assertEqual(self.screen(config('CROSS(volume,150)'))['count'],0)

    def test_disabled_rule_still_has_independent_count_and_diagnostic(self):
        c=config('close>0','change>5');c['groups'][0]['rules'][1]['enabled']=False
        result=self.screen(c)
        self.assertEqual(result['count'],1)
        self.assertEqual(result['counts']['r1']['matched'],0)
        self.assertEqual(result['counts']['r1']['evaluated'],1)
        c['groups'][0]['rules'][1]['enabled']=True;result=self.screen(c)
        self.assertEqual(result['count'],0)
        self.assertEqual(result['diagnostics'][1]['without_count'],1)
        self.assertEqual(result['counts']['r0']['matched'],1)

    def test_fund_missing_is_unknown_and_or_can_still_pass(self):
        self.db.execute('DELETE FROM fund_flow WHERE trade_date=?',(self.fixture.days[117],))
        c=config('COUNT(main_net>0,10)>=7','close>0',mode='any')
        result=self.screen(c)
        self.assertEqual(result['count'],1)
        self.assertEqual(result['counts']['r0']['missing'],1)
        self.assertEqual(result['counts']['r0']['evaluated'],0)
        c['groups'][0]['mode']='all';self.assertEqual(self.screen(c)['count'],0)

    def test_price_reference_and_old_gaps_do_not_hide_short_metrics(self):
        self.db.execute('DELETE FROM stock_daily WHERE trade_date=?',(self.fixture.days[100],))
        c=config('RET(120)>0','close>MA(20)');c['groups'][0]['rules'][0]['enabled']=False
        result=self.screen(c)
        self.assertEqual(result['count'],1);self.assertEqual(result['counts']['r0']['missing'],1)
        self.db.execute('UPDATE stock_daily SET change=80 WHERE trade_date=?',(self.date,))
        result=self.screen(config('change>5'))
        self.assertEqual(result['count'],0);self.assertEqual(result['counts']['r0']['missing'],1)

    def test_zero_division_and_non_finite_are_missing(self):
        result=self.screen(config('close / (volume-volume) > 0'))
        self.assertEqual(result['counts']['r0']['missing'],1)
        self.db.execute('UPDATE stock_daily SET volume=1e308')
        result=self.screen(config('SUM(volume,5)>0'))
        self.assertEqual(result['counts']['r0']['missing'],1)

    def test_money_sum_equality_is_not_changed_by_float_roundoff(self):
        self.db.execute('UPDATE fund_flow SET main_net_inflow=10000000')
        self.assertEqual(self.screen(config('SUM(main_net,7)==0.7'))['count'],1)
        self.assertEqual(self.screen(config('SUM(main_net,7)<0.7'))['count'],0)

    def test_market_only_fields_and_dates(self):
        result=self.screen(config('down_count>2600 and up_count==1000 and sh_change<0'))
        self.assertEqual(result['count'],1)
        self.db.execute('DELETE FROM index_daily WHERE date=?',(self.date,))
        result=self.screen(config('sh_change<0'))
        self.assertEqual(result['counts']['r0']['missing'],1)

    def test_no_conditions_no_selection_and_no_enabled_no_selection(self):
        self.assertEqual(self.screen({'groups':[]})['count'],0)
        c=config('close>0');c['groups'][0]['rules'][0]['enabled']=False
        result=self.screen(c)
        self.assertEqual(result['count'],0);self.assertEqual(result['counts']['r0']['matched'],1)

    def test_removing_last_enabled_rule_matches_the_no_conditions_policy(self):
        result=self.screen(config('close>0'))
        self.assertEqual(result['count'],1)
        self.assertEqual(result['diagnostics'][0]['without_count'],0)
        self.assertEqual(result['diagnostics'][0]['added'],-1)

    def test_pagination_query_and_stock_inspection(self):
        self.db.execute("INSERT INTO stock_daily SELECT '000002.SZ',name,trade_date,open,high,low,close,change,volume,amount FROM stock_daily")
        result=self.screen(config('close>0'),limit=1,offset=1,inspect='000001.SZ')
        self.assertEqual(result['count'],2);self.assertEqual(result['results'][0]['ts_code'],'000002.SZ')
        self.assertEqual(result['inspected']['rule_values']['r0']['state'],'matched')
        result=self.screen(config('close>0'),query='000002')
        self.assertEqual(result['count'],2);self.assertEqual(result['filtered_count'],1)
        self.assertEqual(self.screen(config('close>0'),inspect='600001.SH')['inspected']['state'],'missing')

    def test_future_cannot_affect_selection(self):
        c=config('close>MA(20)');before=self.screen(c)
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('000001.SZ','未来行情',self.fixture.days[121],999,999,999,999,500,100,100))
        after=self.screen(c)
        self.assertEqual(before['counts'],after['counts']);self.assertEqual(before['results'],after['results'])

    def test_invalid_entry_cannot_pass_market_only_rule(self):
        self.db.execute('UPDATE stock_daily SET high=1 WHERE trade_date=?',(self.date,))
        result=self.screen(config('down_count>2600'))
        self.assertEqual(result['count'],0);self.assertEqual(result['stats']['missing'],1)

    def test_http_read_only_and_enrichment_only_after_membership(self):
        app=Flask(__name__)
        def enrich(rows,date,conn,key):
            self.assertEqual([r['ts_code'] for r in rows],['000001.SZ'])
            with self.assertRaises(sqlite3.OperationalError):conn.execute('DELETE FROM stock_daily')
            rows[0][key]=[{'close_sum_pct':999}]
            return [],0
        cp.register_custom_picks(app,lambda:'unused.db',enrich)
        self.db.commit()
        with patch.object(cp.sqlite3,'connect',return_value=self.db):
            client=app.test_client()
            response=client.post('/api/picks/custom/screen',json={'date':self.date,'config':config('close>0')})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['count'],1)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.db.execute('PRAGMA query_only=OFF')

    def test_http_input_errors(self):
        app=Flask(__name__);cp.register_custom_picks(app,lambda:'unused.db');client=app.test_client()
        for path,data in [('validate',{'formula':'open.__class__'}),('parse',{'text':None}),('screen',{'config':None})]:
            self.assertEqual(client.post('/api/picks/custom/'+path,json=data).status_code,400)
        self.assertEqual(client.get('/api/picks/custom/catalog').status_code,200)


class CustomPreviewTests(CustomFixture):
    """预览图: 一次拿全量命中 → 画成 PNG, 样式同条件选股新."""

    async def _done(self): return None

    def preview_client(self, folder):
        app=Flask(__name__)
        captured={}
        def builder(payload):
            captured['payload']=payload
            return '<html><body>capture</body></html>'
        def renderer(html,path):
            captured['html']=html
            with open(path,'wb') as handle:handle.write(b'png')
            return self._done()
        cp.register_custom_picks(app,lambda:'unused.db',None,
                                 preview_dir=folder,preview_builder=builder,preview_renderer=renderer)
        return app.test_client(),captured

    def test_preview_image_returns_png_url_and_all_matches(self):
        self.db.execute("INSERT INTO stock_daily SELECT '000002.SZ',name,trade_date,open,high,low,close,change,volume,amount FROM stock_daily")
        self.db.commit()
        with tempfile.TemporaryDirectory() as folder:
            client,captured=self.preview_client(folder)
            with patch.object(cp.sqlite3,'connect',return_value=self.db):
                response=client.post('/api/picks/custom/preview-image',
                                     json={'date':self.date,'config':config('close>0')})
            self.assertEqual(response.status_code,200,response.json)
            data=response.json
            self.assertEqual(data['count'],2)
            self.assertEqual(data['shown'],2)
            self.assertTrue(data['url'].startswith('/previews/自定义选股_'))
            self.assertTrue(data['url'].endswith('.png'))
            self.assertTrue(os.path.exists(data['path']))
            payload=captured['payload']
            self.assertEqual(payload['title_kind'],'custom')
            self.assertEqual(payload['count'],2)
            self.assertEqual(len(payload['results']),2)
            self.assertIn('条件0',payload['subtitle'])
            self.assertEqual(captured['html'],'<html><body>capture</body></html>')

    def test_preview_image_rejects_bad_config_and_disabled_feature(self):
        self.db.commit()
        with tempfile.TemporaryDirectory() as folder:
            client,_=self.preview_client(folder)
            with patch.object(cp.sqlite3,'connect',return_value=self.db):
                bad=client.post('/api/picks/custom/preview-image',
                                json={'date':self.date,'config':{'groups':[{'name':'x','mode':'all','rules':[{'id':'r','name':'n','formula':'open.__class__','enabled':True}]}]}})
            self.assertEqual(bad.status_code,400)
        app=Flask(__name__);cp.register_custom_picks(app,lambda:'unused.db')
        self.assertEqual(app.test_client().post('/api/picks/custom/preview-image',
                        json={'date':self.date,'config':config('close>0')}).status_code,400)

    def test_preview_summary_marks_truncated_list(self):
        cfg={'groups':[{'rules':[{'name':'条件A','enabled':True},{'name':'条件B','enabled':False}]}]}
        summary=cp._preview_summary(cfg,845,200)
        self.assertIn('条件A',summary)
        self.assertNotIn('条件B',summary)
        self.assertIn('共 845 只',summary)
        self.assertNotIn('共',cp._preview_summary(cfg,10,10))

    def test_all_results_returns_every_match_beyond_one_page(self):
        self.db.execute("INSERT INTO stock_daily SELECT '000002.SZ',name,trade_date,open,high,low,close,change,volume,amount FROM stock_daily")
        result=self.screen(config('close>0'),limit=1,all_results=True)
        self.assertEqual(result['count'],2)
        self.assertEqual(len(result['results']),2)
        paged=self.screen(config('close>0'),limit=1,offset=1)
        self.assertEqual(len(paged['results']),1)


class CustomConfigSyncTests(unittest.TestCase):
    """条件同步: 后端存一份, 连同一台后端的设备共用."""

    def client_with_config(self, folder):
        app=Flask(__name__)
        cp.register_custom_picks(app,lambda:'unused.db',None,
                                 config_path=os.path.join(folder,'custom_picks_config.json'))
        return app.test_client()

    def test_save_then_read_and_rev_increments(self):
        with tempfile.TemporaryDirectory() as folder:
            client=self.client_with_config(folder)
            empty=client.get('/api/picks/custom/config')
            self.assertEqual(empty.status_code,200)
            self.assertIsNone(empty.json['config'])
            self.assertEqual(empty.json['rev'],0)
            saved=client.post('/api/picks/custom/config',json={'config':config('close>0')})
            self.assertEqual(saved.status_code,200,saved.json)
            self.assertEqual(saved.json['rev'],1)
            loaded=client.get('/api/picks/custom/config')
            self.assertEqual(loaded.json['config']['groups'][0]['rules'][0]['formula'],'close > 0')
            self.assertEqual(loaded.json['rev'],1)
            again=client.post('/api/picks/custom/config',json={'config':config('close<0')})
            self.assertEqual(again.json['rev'],2)
            self.assertEqual(client.get('/api/picks/custom/config').json['config']['groups'][0]['rules'][0]['formula'],'close < 0')

    def test_bad_config_rejected_and_broken_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            client=self.client_with_config(folder)
            bad=client.post('/api/picks/custom/config',json={'config':{'groups':[{'name':'x','mode':'all','rules':[{'id':'r','name':'n','formula':'open.__class__','enabled':True}]}]}})
            self.assertEqual(bad.status_code,400)
            self.assertIsNone(client.get('/api/picks/custom/config').json['config'])
            path=os.path.join(folder,'custom_picks_config.json')
            with open(path,'w',encoding='utf-8') as handle:handle.write('{坏文件')
            self.assertIsNone(client.get('/api/picks/custom/config').json['config'])

    def test_saved_file_is_normalized_and_written_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            client=self.client_with_config(folder)
            client.post('/api/picks/custom/config',json={'config':config('close>0')})
            path=os.path.join(folder,'custom_picks_config.json')
            self.assertFalse(os.path.exists(path+'.tmp'))
            with open(path,encoding='utf-8') as handle:saved=json.load(handle)
            self.assertEqual(saved['rev'],1)
            self.assertTrue(saved['updated_at'])
            self.assertIn('explanation',saved['config']['groups'][0]['rules'][0])


if __name__=='__main__':unittest.main()
