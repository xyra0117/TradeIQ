"""Isolated fixtures for heat, leaders, history and the read-only API contract."""
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from hot_sectors import hot_sectors, normalize_code, register_hot_sectors, _heat_status
from dip_picks import _limit_up_price_for

NOW = datetime(2026, 10, 7, 16)
DATES = [(datetime(2026, 9, 1) + timedelta(days=i)).strftime('%Y%m%d') for i in range(24)
         if (datetime(2026, 9, 1) + timedelta(days=i)).weekday() < 5]


def fixture(conn):
    conn.executescript('''
    CREATE TABLE trading_dates_cache (cal_date TEXT,exchange TEXT);
    CREATE TABLE stock_daily (ts_code TEXT,name TEXT,trade_date TEXT,close REAL,change REAL,volume REAL,amount REAL);
    CREATE TABLE limitup (date TEXT,code TEXT,name TEXT,sector TEXT,time TEXT,streak TEXT,keyword TEXT);
    CREATE TABLE overview (date TEXT,upCount INTEGER,downCount INTEGER,flatCount INTEGER);
    CREATE TABLE index_daily (date TEXT,name TEXT,change REAL,close REAL);
    ''')
    for day in DATES:
        conn.execute('INSERT INTO trading_dates_cache VALUES (?,?)', (day, 'SSE'))
        conn.execute('INSERT INTO limitup VALUES (?,?,?,?,?,?,?)', (day, '600999.SH', '覆盖', '公告', '09:30', '首板', ''))
    conn.execute('INSERT INTO overview VALUES (?,?,?,?)', (DATES[-1], 3000, 2000, 100))
    conn.execute('INSERT INTO index_daily VALUES (?,?,?,?)', (DATES[-1], '上证指数', 1, 3800))


def stock(conn, code, length=1, time='09:40:00', amount=200000, old_indices=(), sector='测试板块', label=None):
    price = 10.0
    sealed_days = set(range(len(DATES) - length, len(DATES))) | set(old_indices)
    for i, day in enumerate(DATES):
        previous = price
        price = _limit_up_price_for(code, '股票'+code, price, day)[0] if i in sealed_days else price
        conn.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?)', (code, '股票'+code, day, price, (price / previous - 1) * 100, 1000, amount))
        if i in sealed_days:
            conn.execute('INSERT INTO limitup VALUES (?,?,?,?,?,?,?)', (day, code, '原名'+code, sector, time, label or f'{length}天{length}板', '关键词'))


class HotSectorTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        fixture(self.conn)

    def tearDown(self):
        self.conn.close()

    def screen(self, day=None, sector=''):
        return hot_sectors(self.conn, day or DATES[-1], sector, now=NOW)

    def item(self):
        return self.screen()['sectors'][0]

    def test_height_before_time(self):
        stock(self.conn, '600001.SH', 3, '14:50')
        stock(self.conn, '600002.SH', 2, '09:25')
        self.assertEqual([s['ts_code'] for s in self.item()['leaders']], ['600001.SH', '600002.SH'])
        self.assertEqual(self.item()['leaders'][0]['consecutive'], 3)

    def test_recent_count_before_time(self):
        stock(self.conn, '600001.SH', 1, '14:50', old_indices=(len(DATES)-3,))
        stock(self.conn, '600002.SH', 1, '09:25')
        self.assertEqual(self.item()['leaders'][0]['ts_code'], '600001.SH')
        self.assertEqual(self.item()['leaders'][0]['recent5_count'], 2)

    def test_time_before_amount_and_amount_breaks_tie(self):
        stock(self.conn, '600001.SH', 1, '09:40', 900000)
        stock(self.conn, '600002.SH', 1, '09:25', 100000)
        stock(self.conn, '600003.SH', 1, '09:25', 200000)
        self.assertEqual([s['ts_code'] for s in self.item()['leaders']], ['600003.SH', '600002.SH', '600001.SH'])
        self.assertEqual(self.item()['leaders'][0]['amount_yi'], 2)

    def test_code_stable_when_all_equal(self):
        stock(self.conn, '600002.SH')
        stock(self.conn, '600001.SH')
        self.assertEqual(self.item()['leaders'][0]['ts_code'], '600001.SH')

    def test_reported_four_days_two_boards_does_not_invent_streak(self):
        stock(self.conn, '600001.SH', 1, old_indices=(len(DATES)-4,), label='4天2板')
        self.assertEqual(self.item()['leaders'][0]['consecutive'], 1)

    def test_code_normalization_and_daily_dedup(self):
        stock(self.conn, '600001.SH', 2)
        self.conn.execute('INSERT INTO limitup VALUES (?,?,?,?,?,?,?)', (DATES[-1], '600001SH', '重复', '测试板块', '09:25', '2天2板', ''))
        item = self.item()
        self.assertEqual(item['today_count'], 1)
        self.assertEqual(item['leaders'][0]['recent5_count'], 2)
        self.assertEqual(item['leaders'][0]['time'], '09:25')
        self.assertEqual(normalize_code('sz002058'), '002058.SZ')
        self.assertIsNone(normalize_code('688286SZ'))
        self.assertIsNone(normalize_code('00020.SZ'))

    def test_missing_reference_pending_no_filled_third(self):
        stock(self.conn, '600001.SH', 3)
        stock(self.conn, '600002.SH', 2)
        self.conn.execute('DELETE FROM stock_daily WHERE ts_code=? AND trade_date=?', ('600001.SH', DATES[-3]))
        item = self.item()
        self.assertEqual(len(item['leaders']), 1)
        self.assertEqual(item['pending_count'], 1)
        self.assertTrue(item['ranking_provisional'])

    def test_price_anomaly_not_ranked(self):
        stock(self.conn, '600001.SH')
        self.conn.execute('UPDATE stock_daily SET change=1 WHERE ts_code=? AND trade_date=?', ('600001.SH', DATES[-1]))
        self.assertEqual(self.item()['pending_count'], 1)
        self.assertEqual(self.item()['leaders'], [])

    def test_bse_highest_valid_cent_does_not_exceed_thirty_percent(self):
        stock(self.conn, '920779.BJ')
        self.conn.execute('UPDATE stock_daily SET close=21.85,change=0 WHERE ts_code=?', ('920779.BJ',))
        self.conn.execute('UPDATE stock_daily SET close=28.40,change=? WHERE ts_code=? AND trade_date=?',
                          ((28.4/21.85-1)*100, '920779.BJ', DATES[-1]))
        self.assertEqual(self.item()['leaders'][0]['consecutive'], 1)
        self.conn.execute('UPDATE stock_daily SET close=28.41,change=? WHERE ts_code=? AND trade_date=?',
                          ((28.41/21.85-1)*100, '920779.BJ', DATES[-1]))
        self.assertEqual(self.item()['pending_count'], 1)

    def test_unsealed_record_not_ranked(self):
        stock(self.conn, '600001.SH')
        self.conn.execute('UPDATE stock_daily SET close=10,change=0 WHERE ts_code=? AND trade_date=?', ('600001.SH', DATES[-1]))
        self.assertEqual(self.item()['not_sealed_count'], 1)
        self.assertEqual(self.item()['leaders'], [])

    def test_missing_ranking_fields_are_pending(self):
        stock(self.conn, '600001.SH', time='错误')
        stock(self.conn, '600002.SH', amount=0)
        self.assertEqual(self.item()['pending_count'], 2)
        self.assertEqual(self.item()['leaders'], [])

    def test_missing_day_is_gap_not_zero(self):
        stock(self.conn, '600001.SH')
        self.conn.execute('DELETE FROM limitup WHERE date=?', (DATES[-3],))
        item = self.item()
        self.assertIsNone(item['appearance5'])
        self.assertIsNone(next(t['count'] for t in item['timeline'] if t['date']==DATES[-3]))
        self.assertEqual(self.screen()['summary']['missing5_dates'], [DATES[-3]])
        self.assertEqual(self.screen(DATES[-3])['status'], 'missing')
        self.assertEqual(self.screen(DATES[-3])['date'], DATES[-3])

    def test_zero_count_is_known_when_other_sectors_present(self):
        stock(self.conn, '600001.SH')
        item = self.item()
        self.assertEqual(item['timeline'][-2]['count'], 0)
        self.assertEqual(item['delta'], 1)

    def test_recent_volume_sorts_before_today_volume(self):
        stock(self.conn, '600001.SH', 5, sector='历史多')
        stock(self.conn, '600002.SH', 1, sector='今日多')
        stock(self.conn, '600003.SH', 1, sector='今日多')
        rows = self.screen()['sectors']
        self.assertEqual([r['sector'] for r in rows], ['历史多', '今日多'])
        self.assertEqual(rows[0]['recent5_count'], 5)
        self.assertEqual(rows[1]['today_count'], 2)

    def test_summary_groups_and_board_rank_share_heat_state(self):
        for code in ('600001.SH', '600002.SH', '600003.SH'):
            stock(self.conn, code, 4, sector='持续')
        for code in ('600004.SH', '600005.SH', '600006.SH', '600007.SH'):
            stock(self.conn, code, 2, sector='新热')
        result = self.screen()
        self.assertEqual(result['summary']['heat_groups']['active'], ['持续'])
        self.assertEqual(result['summary']['heat_groups']['new'], ['新热'])
        self.assertEqual(result['summary']['heat_board_count'], 2)
        self.assertEqual([r['heat']['rank'] for r in result['sectors']], [1, 2])
        self.assertEqual(self.screen(sector='持续')['detail']['heat']['key'], 'active')

    def test_yesterday_leader_retained_after_break(self):
        stock(self.conn, '600001.SH', 1, old_indices=(len(DATES)-2,))
        # Remove its current sector record and change the current close to a valid down day.
        self.conn.execute('DELETE FROM limitup WHERE code=? AND date=?', ('600001.SH', DATES[-1]))
        previous = self.conn.execute('SELECT close FROM stock_daily WHERE ts_code=? AND trade_date=?', ('600001.SH', DATES[-2])).fetchone()[0]
        self.conn.execute('UPDATE stock_daily SET close=?,change=-2 WHERE ts_code=? AND trade_date=?', (previous*.98, '600001.SH', DATES[-1]))
        item = self.item()
        self.assertEqual(item['today_count'], 0)
        self.assertEqual(item['yesterday_leader']['status'], 'broken')
        self.assertEqual(item['yesterday_leader']['change_pct'], -2)

    def test_yesterday_ranking_and_name(self):
        stock(self.conn, '600001.SH', 3)
        leader = self.item()['leaders'][0]
        self.assertEqual(leader['rank_change'], '昨日龙1 → 今日龙1')
        self.assertEqual(leader['name'], '股票600001.SH')
        self.assertEqual(self.item()['yesterday_leader']['status'], 'sealed')

    def test_future_records_do_not_change_historical_results(self):
        stock(self.conn, '600001.SH', 3)
        before = self.screen()
        self.conn.execute('INSERT INTO limitup VALUES (?,?,?,?,?,?,?)', ('20261006', '600001.SH', '未来', '未来板块', '09:25', '9天9板', ''))
        self.conn.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?)', ('600001.SH', '未来名字', '20261006', 10000, 100, 1, 1))
        after = self.screen()
        for key in ('sectors', 'summary', 'market', 'date', 'previous_date'):
            self.assertEqual(before[key], after[key])

    def test_iso_dates_and_market_background(self):
        stock(self.conn, '600001.SH', 3)
        self.conn.execute("UPDATE stock_daily SET trade_date=substr(trade_date,1,4)||'-'||substr(trade_date,5,2)||'-'||substr(trade_date,7,2)")
        self.assertEqual(self.item()['leaders'][0]['consecutive'], 3)
        self.assertEqual(self.screen()['market']['overview']['upCount'], 3000)

    def test_detail_and_invalid_dates(self):
        stock(self.conn, '600001.SH', 2)
        detail = self.screen(sector='测试板块')['detail']
        self.assertEqual(len(detail['timeline']), 10)
        self.assertEqual(len(detail['stocks']), 1)
        self.assertEqual(len(detail['history']), 2)
        self.assertEqual(self.screen(sector='不存在')['status'], 'empty')
        for date in ('20260931', '2026-09-24', 'abc'):
            with self.assertRaises(ValueError):
                self.screen(date)

    def test_no_today_rank_before_close(self):
        stock(self.conn, '600001.SH')
        now = datetime.strptime(DATES[-1]+' 10:00', '%Y%m%d %H:%M')
        result = hot_sectors(self.conn, DATES[-1], now=now)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['sectors'], [])

    def test_api_uses_readonly_connection_and_has_no_old_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.db'
            with sqlite3.connect(path) as conn:
                fixture(conn)
                stock(conn, '600001.SH', 3)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            app = Flask(__name__)
            register_hot_sectors(app, lambda: path)
            client = app.test_client()
            with patch('socket.socket.connect', side_effect=AssertionError('No upstream requests')):
                data = client.get('/api/hot-sectors?date='+DATES[-1])
                self.assertEqual(data.status_code, 200)
                self.assertEqual(data.headers['Cache-Control'], 'no-store')
                self.assertTrue(data.get_json()['readonly'])
                self.assertEqual(client.get('/api/hot-sectors/detail?sector=测试板块&date='+DATES[-1]).status_code, 200)
            self.assertEqual(client.get('/api/hot-sectors?date=bad').status_code, 400)
            self.assertEqual(client.get('/api/hot-sectors/detail').status_code, 400)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            source = Path(__file__).with_name('app.py').read_text()
            self.assertNotIn("@app.route('/api/sector-collections", source)
            self.assertNotIn("@app.route('/api/user-sectors", source)
            missing_app = Flask('missing')
            register_hot_sectors(missing_app, lambda: Path(directory)/'does-not-exist.db')
            self.assertEqual(missing_app.test_client().get('/api/hot-sectors').status_code, 503)
            self.assertFalse((Path(directory)/'does-not-exist.db').exists())


class HeatStateTests(unittest.TestCase):
    def heat(self, counts):
        return _heat_status({'timeline': [{'date': str(i), 'count': n} for i, n in enumerate(counts)]})

    def test_sustained_and_cooling_examples(self):
        self.assertEqual(self.heat([0, 3, 4, 5, 6])['key'], 'active')
        self.assertEqual(self.heat([10, 6, 0, 8, 0])['key'], 'cooling')
        self.assertEqual(self.heat([2, 0, 5, 9, 2])['key'], 'cooling')

    def test_hot_candidate_threshold_includes_equal_boundary(self):
        self.assertTrue(self.heat([3, 3, 0, 0, 4])['focus'])
        self.assertFalse(self.heat([3, 2, 0, 0, 4])['focus'])
        self.assertFalse(self.heat([0, 0, 0, 10, 7])['focus'])

    def test_cooling_requires_strictly_below_both_references(self):
        self.assertEqual(self.heat([3, 3, 3, 3, 3])['key'], 'active')
        self.assertEqual(self.heat([1, 1, 5, 5, 3])['key'], 'active')  # Equal to preceding mean.
        self.assertEqual(self.heat([2, 3, 3, 2, 2])['key'], 'active')  # Equal to yesterday.

    def test_new_hotspot_and_sudden_thresholds(self):
        self.assertEqual(self.heat([0, 0, 0, 10, 7])['key'], 'new')
        self.assertEqual(self.heat([0, 0, 0, 4, 4])['key'], 'new')
        self.assertEqual(self.heat([0, 0, 0, 3, 4])['key'], 'ordinary')
        self.assertEqual(self.heat([0, 0, 0, 0, 3])['key'], 'sudden')

    def test_incomplete_window_has_no_invented_state_or_rank(self):
        for counts in ([1, 3, 4, 5], [1, None, 10, 8, 5]):
            heat = self.heat(counts)
            self.assertEqual(heat['key'], 'missing')
            self.assertIsNone(heat['rank'])
            self.assertFalse(heat['focus'])

    def test_ordinary_and_quiet_remain_outside_hot_board(self):
        for counts, key in (([0, 1, 2, 0, 2], 'ordinary'), ([0, 1, 2, 0, 0], 'quiet')):
            heat = self.heat(counts)
            self.assertEqual(heat['key'], key)
            self.assertFalse(heat['in_board'])


if __name__ == '__main__':
    unittest.main()
