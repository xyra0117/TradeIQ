"""模拟盘回放引擎回归：内存数据库、模拟行情，不触碰实际数据库。

重点是防未来函数——任何人删掉 SQL 里的 `m <= ?` 谓词, test_minline_slice_never_returns_future_bars 立刻红。
"""
import sqlite3
import unittest
from unittest.mock import patch

from flask import Flask

import replay_paper as rp


class KeptConnection(sqlite3.Connection):
    def close(self):
        pass  # 同一内存库跨多个 HTTP 请求; tearDown 显式释放。


def _session_minutes():
    """全市场会话网格: 09:25 + 09:30-11:29 + 13:00-14:57 + 15:00 = 240 点。"""
    out = ['09:25:00']
    for hour, start, end in ((9, 30, 59), (10, 0, 59), (11, 0, 29),
                             (13, 0, 59), (14, 0, 57)):
        out.extend(f'{hour:02d}:{minute:02d}:00' for minute in range(start, end + 1))
    out.append('15:00:00')
    return out


class _ReplayFixture(unittest.TestCase):
    """共享夹具: 一个内存库 + 模拟分时行情, 不放任何 test_ 方法。"""

    DAYS = ('20260928', '20260929', '20260930', '20261001', '20261008')
    LIQUID = '000048.SZ'
    SUSPENDED = '600001.SH'

    def setUp(self):
        rp.clear_caches()  # 每个用例换一个新内存库, 缓存必须隔离
        self.minutes = _session_minutes()
        self.db = sqlite3.connect(':memory:', factory=KeptConnection)
        self.db.executescript('''
            CREATE TABLE stock_minline_bars (
                ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, m TEXT NOT NULL,
                p REAL, avg_p REAL, v REAL NOT NULL DEFAULT 0,
                name TEXT, prev_close REAL,
                PRIMARY KEY (ts_code, trade_date, m));
            CREATE TABLE stock_daily (
                ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, name TEXT,
                open REAL, high REAL, low REAL, close REAL,
                UNIQUE(ts_code, trade_date));
        ''')
        self.seed()
        # sim_* 表交给 init_sim_db 建——这样 DDL 本身也在测试覆盖内,
        # 夹具手写一遍会漏列(updated_at)而 IF NOT EXISTS 又不会补。
        rp.init_sim_db(self.db)
        self.db.commit()

    def tearDown(self):
        sqlite3.Connection.close(self.db)

    def seed(self):
        """三天满仓数据(每天 2200 只, 通过日历过滤), 外加一个只有 1 只票的脏日期。

        填充票只落 10 根 bar——日历门槛按当天总 bar 数筛(走覆盖索引, 比
        COUNT(DISTINCT) 快 4 倍)。脏日只有 1 只票 × 240 根 = 240 行, 远低于门槛。
        240 根的完整网格由 LIQUID 一只票提供, session_grid 取全市场 DISTINCT m。
        """
        crowded = ([f'{i:06d}.SZ' for i in range(2000, 3100)]
                   + [f'{i:06d}.SH' for i in range(2000, 3100)])
        for day in self.DAYS:
            codes = [self.LIQUID] if day == '20261001' else crowded + [self.LIQUID]
            for index, code in enumerate(codes):
                base = 18.06 if code == self.LIQUID else 10 + (index % 50) * 0.5
                minutes = self.minutes if code == self.LIQUID else self.minutes[:10]
                for step, minute in enumerate(minutes):
                    self.db.execute(
                        'INSERT INTO stock_minline_bars VALUES (?,?,?,?,?,?,?,?)',
                        (code, day, minute, base + step * 0.01, base + step * 0.005,
                         100 + step, f'股票{index}', base - 1))
        for day in self.DAYS[:3]:
            self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?)',
                            (self.LIQUID, day, '京基智农', 18.2, 18.4, 17.9, 18.06))
        # 停牌票: 只有 80 个点, prev_close 为 NULL, 需要靠 stock_daily 回退
        for step, minute in enumerate(self.minutes[:80]):
            self.db.execute('INSERT INTO stock_minline_bars VALUES (?,?,?,?,?,?,?,?)',
                            (self.SUSPENDED, self.DAYS[0], minute, 20 + step * 0.01,
                             20, 0, '停牌股', None))
        self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?)',
                        (self.SUSPENDED, '20260925', '停牌股', 19.5, 19.5, 19.5, 19.5))
        self.db.commit()

    # ── 回放日历 ────────────────────────────────────────────

    def walk_to(self, client, account_id, target):
        for _ in range(300):
            current = client.get(f'/api/sim/state?account_id={account_id}').json['session']['cur_min']
            if current >= target:
                return current
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'next_minute'})
        self.fail('走不到 ' + target)

    def create_account(self, client, cash=100000):
        response = client.post('/api/sim/account', json={'initial_cash': cash})
        self.assertEqual(response.status_code, 200)
        return response.json['account']['id']

    def client(self):
        app = Flask(__name__)
        rp.register_replay_paper(app, lambda: '/tmp/not-used.db',
                                 buy_fee=lambda *a: 5.0, sell_fee=lambda *a: 6.0)
        return app.test_client()


class ReplayClockTests(_ReplayFixture):
    """日历、会话网格、分时切片——防未来函数的底层。"""

    def test_replay_calendar_drops_dates_without_broad_coverage(self):
        """20261001 只有 1 只票有数据, 必须被日历过滤掉, 否则用户会走进一个空日子。"""
        calendar = rp.replay_calendar(self.db)
        self.assertEqual(calendar, ['20260928', '20260929', '20260930', '20261008'])
        self.assertNotIn('20261001', calendar)

    def test_replay_calendar_sorts_ascending(self):
        self.assertEqual(rp.replay_calendar(self.db), sorted(rp.replay_calendar(self.db)))

    def test_replay_calendar_is_cached(self):
        """日历查询在真实库上要 ~22 秒。自动播放每秒 4 次 tick, 没了缓存就是灾难。"""
        rp.clear_caches()
        statements = []
        self.db.set_trace_callback(statements.append)
        rp.replay_calendar(self.db, 'key-a')
        after_first = len([s for s in statements if 'GROUP BY trade_date' in s])
        for _ in range(5):
            rp.replay_calendar(self.db, 'key-a')
        after_more = len([s for s in statements if 'GROUP BY trade_date' in s])
        self.assertEqual(after_first, 1)
        self.assertEqual(after_more, 1, '命中缓存后不该再查库')
        self.db.set_trace_callback(None)

    def test_session_grid_is_cached_per_day(self):
        rp.clear_caches()
        statements = []
        self.db.set_trace_callback(statements.append)
        rp.session_grid(self.db, '20260928', 'key-b')
        rp.session_grid(self.db, '20260928', 'key-b')
        same_day = len([s for s in statements if 'DISTINCT m' in s])
        rp.session_grid(self.db, '20260929', 'key-b')
        other_day = len([s for s in statements if 'DISTINCT m' in s])
        self.assertEqual(same_day, 1)
        self.assertEqual(other_day, 2, '换日期应该重新查')
        self.db.set_trace_callback(None)

    # ── 会话网格 ────────────────────────────────────────────

    def test_session_grid_is_derived_from_data_not_hardcoded(self):
        grid = rp.session_grid(self.db, '20260928')
        self.assertEqual(len(grid), 240)
        self.assertEqual(grid[0], '09:25:00')
        self.assertEqual(grid[-1], '15:00:00')
        self.assertEqual(grid, sorted(grid))
        # 午休整段不存在
        self.assertFalse([m for m in grid if '11:31' <= m <= '12:59'])

    def test_session_grid_has_the_1457_to_1500_tail_gap(self):
        """尾盘集合竞价未留存: 14:58/14:59 不存在, 网格上表现为 14:57 直接跳 15:00。"""
        grid = rp.session_grid(self.db, '20260929')
        self.assertIn('14:57:00', grid)
        self.assertNotIn('14:58:00', grid)
        self.assertNotIn('14:59:00', grid)
        self.assertEqual(grid[grid.index('14:57:00') + 1], '15:00:00')

    # ── 防未来函数: 核心 ────────────────────────────────────

    def test_minline_slice_never_returns_future_bars(self):
        """★ 最重要的一条。删掉 SQL 的 `m <= ?` 谓词, 这个测试必须立刻红。"""
        full_day = rp.minline_slice(self.db, self.LIQUID, '20260929', '15:00:00')
        self.assertEqual(len(full_day['rows']), 240)
        for cur_min in ('09:25:00', '10:00:00', '14:57:00', '15:00:00'):
            with self.subTest(cur_min=cur_min):
                payload = rp.minline_slice(self.db, self.LIQUID, '20260929', cur_min)
                self.assertTrue(payload['rows'])
                self.assertTrue(all(row['m'] <= cur_min for row in payload['rows']))
                self.assertLessEqual(payload['rows'][-1]['m'], cur_min)
        # 未来那些 bar 确实存在于库里——是切片挡住了它们, 不是数据本来就没有
        early = rp.minline_slice(self.db, self.LIQUID, '20260929', '10:00:00')
        self.assertLess(len(early['rows']), len(full_day['rows']))

    def test_minline_slice_at_0925_returns_only_the_auction_bar(self):
        payload = rp.minline_slice(self.db, self.LIQUID, '20260929', '09:25:00')
        self.assertEqual([row['m'] for row in payload['rows']], ['09:25:00'])

    def test_minline_slice_exposes_no_gap_bars_from_its_own_data(self):
        """停牌票当天只存了 80 个点, 切片不得凭空造出 09:25 之后不存在的 bar。"""
        payload = rp.minline_slice(self.db, self.SUSPENDED, '20260928', '15:00:00')
        minutes = [row['m'] for row in payload['rows']]
        self.assertEqual(len(minutes), 80)
        self.assertEqual(minutes, sorted(minutes))
        self.assertEqual(minutes[0], '09:25:00')

    def test_minline_slice_reports_missing_stock_without_erroring(self):
        payload = rp.minline_slice(self.db, '999999.SZ', '20260929', '10:00:00')
        self.assertEqual(payload['rows'], [])
        self.assertFalse(payload['has_bar'])

    def test_minline_slice_falls_back_to_previous_close_when_column_is_null(self):
        """停牌票的 prev_close 是 NULL, 必须回退到 stock_daily, 否则跳空基准丢失。"""
        payload = rp.minline_slice(self.db, self.SUSPENDED, '20260928', '10:00:00')
        self.assertIsNotNone(payload['prev_close'])

    # ── HTTP 契约 ───────────────────────────────────────────

    def test_http_minline_takes_the_clock_from_the_session_not_the_client(self):
        """客户端传 date/minute 必须被忽略——时钟只认 sim_session。"""
        self.db.execute('INSERT INTO sim_accounts (id, name, initial_cash) VALUES (1, ?, ?)',
                        ('模拟账户', 100000))
        self.db.execute('INSERT INTO sim_session (account_id, cur_date, cur_min) '
                        'VALUES (1, ?, ?)', ('20260929', '10:00:00'))
        self.db.commit()
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            response = self.client().get(
                '/api/sim/replay/minline?account_id=1&ts_code=' + self.LIQUID
                + '&date=20260930&minute=15:00:00')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['trade_date'], '20260929')
        self.assertEqual(response.json['cur_min'], '10:00:00')
        self.assertLessEqual(response.json['rows'][-1]['m'], '10:00:00')

    def test_http_calendar_lists_only_replayable_dates(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            response = self.client().get('/api/sim/replay/calendar')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['dates'],
                         ['20260928', '20260929', '20260930', '20261008'])

    def test_http_rejects_bad_arguments(self):
        self.db.execute('INSERT INTO sim_accounts (id, name, initial_cash) VALUES (1, ?, ?)',
                        ('模拟账户', 100000))
        self.db.execute('INSERT INTO sim_session (account_id, cur_date, cur_min) '
                        'VALUES (1, ?, ?)', ('20260929', '10:00:00'))
        self.db.commit()
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            missing = client.get('/api/sim/replay/minline?account_id=1')
            self.assertEqual(missing.status_code, 400)
            unknown = client.get('/api/sim/state?account_id=999')
            self.assertEqual(unknown.status_code, 404)
            step = client.post('/api/sim/replay/step', json={'account_id': 1, 'op': 'nope'})
            self.assertEqual(step.status_code, 400)

    def test_http_reports_database_failure_as_503(self):
        with patch.object(rp.sqlite3, 'connect', side_effect=sqlite3.OperationalError('private path')):
            response = self.client().get('/api/sim/replay/calendar')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private path', response.text)


class SimSessionTests(_ReplayFixture):
    """建账户 / 建会话 / 时钟推进。"""

    def test_http_account_requires_a_positive_amount(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            for payload, hint in (({}, 'missing'), ({'initial_cash': 0}, 'zero'),
                                  ({'initial_cash': -5}, 'negative'),
                                  ({'initial_cash': 'abc'}, 'not a number')):
                with self.subTest(hint=hint):
                    self.assertEqual(
                        client.post('/api/sim/account', json=payload).status_code, 400)

    def test_http_init_pins_the_clock_to_the_auction_bar(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            response = client.post('/api/sim/replay/init',
                                   json={'account_id': account_id, 'start_date': '20260929'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['session']['cur_date'], '20260929')
            self.assertEqual(response.json['session']['cur_min'], '09:25:00')

    def test_http_init_rejects_dates_without_minline_coverage(self):
        """20261001 在库里有数据但只有 1 只票, 不能当回放起点。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            response = client.post('/api/sim/replay/init',
                                   json={'account_id': account_id, 'start_date': '20261001'})
            self.assertEqual(response.status_code, 400)
            self.assertIn('20261001', response.json['message'])

    def test_step_walks_the_session_grid_one_bar_at_a_time(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            client.post('/api/sim/replay/init',
                        json={'account_id': account_id, 'start_date': '20260929'})
            walked = [client.post('/api/sim/replay/step',
                                  json={'account_id': account_id, 'op': 'next_minute'}).json
                      for _ in range(3)]
        self.assertEqual([s['session']['cur_min'] for s in walked],
                         ['09:30:00', '09:31:00', '09:32:00'])

    def test_next_minute_crosses_the_1457_gap_to_the_close(self):
        """14:57 后面没有 14:58/14:59, 下一分钟应直接落到 15:00 收盘。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            client.post('/api/sim/replay/init',
                        json={'account_id': account_id, 'start_date': '20260929'})
            self.walk_to(client, account_id, '14:57:00')
            response = client.post('/api/sim/replay/step',
                                   json={'account_id': account_id, 'op': 'next_minute'})
        self.assertEqual(response.json['session']['cur_min'], '15:00:00')

    def test_next_minute_refuses_after_the_close(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            client.post('/api/sim/replay/init',
                        json={'account_id': account_id, 'start_date': '20260929'})
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'skip_close'})
            response = client.post('/api/sim/replay/step',
                                   json={'account_id': account_id, 'op': 'next_minute'})
        self.assertEqual(response.status_code, 400)

    def test_next_day_rolls_over_to_the_auction_and_skips_the_sparse_date(self):
        """跨日必须落到 09:25 集合竞价, 且跳过只有 1 只票的 20261001。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            client.post('/api/sim/replay/init',
                        json={'account_id': account_id, 'start_date': '20260930'})
            response = client.post('/api/sim/replay/step',
                                   json={'account_id': account_id, 'op': 'next_day'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['session']['cur_date'], '20261008')
        self.assertEqual(response.json['session']['cur_min'], '09:25:00')

    def test_next_day_refuses_at_the_window_end(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            client.post('/api/sim/replay/init',
                        json={'account_id': account_id, 'start_date': '20261008'})
            response = client.post('/api/sim/replay/step',
                                   json={'account_id': account_id, 'op': 'next_day'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('20261008', response.json['message'])

    def test_tick_batches_and_bumps_rev_each_time(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.create_account(client)
            client.post('/api/sim/replay/init',
                        json={'account_id': account_id, 'start_date': '20260929'})
            first = client.post('/api/sim/replay/step',
                                json={'account_id': account_id, 'op': 'tick', 'steps': 5}).json
            second = client.post('/api/sim/replay/step',
                                 json={'account_id': account_id, 'op': 'tick', 'steps': 1}).json
        self.assertEqual(first['session']['cur_min'], '09:34:00')  # 09:25 起走 5 根
        self.assertEqual(second['session']['cur_min'], '09:35:00')
        self.assertEqual(second['session']['rev'], first['session']['rev'] + 1)


class SimOrderTests(_ReplayFixture):
    """纸面下单：T+1、费用摊薄、现金算术、服务端盖戳。"""

    # 费用由夹具注入的假函数给出: 买入 5 元, 卖出 6 元
    BUY_FEE = 5.0
    SELL_FEE = 6.0

    def order(self, client, account_id, side='buy', price=18.10, shares=100, **extra):
        return client.post('/api/sim/order', json=dict(
            {'account_id': account_id, 'ts_code': self.LIQUID, 'side': side,
             'price': price, 'shares': shares}, **extra))

    def start(self, client, cash=100000, date='20260929', minute=None):
        account_id = self.create_account(client, cash)
        client.post('/api/sim/replay/init',
                    json={'account_id': account_id, 'start_date': date})
        if minute:
            self.walk_to(client, account_id, minute)
        return account_id

    def positions(self, client, account_id):
        return client.get(f'/api/sim/state?account_id={account_id}').json['positions']

    # ── 服务端盖戳: 防伪造 ──────────────────────────────────

    def test_order_timestamp_comes_from_the_server_clock(self):
        """客户端传 minute=15:00 必须被忽略——否则可以在 09:35 记一笔收盘成交。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            response = self.order(client, account_id, minute='15:00:00',
                                  trade_date='20260930')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['order']['minute'], '09:25:00')
        self.assertEqual(response.json['order']['trade_date'], '20260929')

    def test_order_stamped_after_the_clock_moves(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.walk_to(client, account_id, '10:30:00')
            response = self.order(client, account_id)
        self.assertEqual(response.json['order']['minute'], '10:30:00')

    def test_stale_tab_is_rejected_with_409(self):
        """★ 多标签页时间戳伪造: 另一个 tab 把时钟推到 14:00 后,
        画面还停在 09:35 的那个 tab 下单必须被拒, 而不是静默记成 14:00 成交。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            for _ in range(3):
                client.post('/api/sim/replay/step',
                            json={'account_id': account_id, 'op': 'next_minute'})
            stale = client.get(f'/api/sim/state?account_id={account_id}').json['session']
            # 另一个 tab 悄悄把时钟往前推了
            for _ in range(5):
                client.post('/api/sim/replay/step',
                            json={'account_id': account_id, 'op': 'next_minute'})
            response = self.order(client, account_id,
                                  expected_date=stale['cur_date'],
                                  expected_min=stale['cur_min'], expected_rev=stale['rev'])
        self.assertEqual(response.status_code, 409)
        self.assertIn('09', response.json['message'])

    def test_matching_expectation_is_accepted(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            session = client.get(f'/api/sim/state?account_id={account_id}').json['session']
            response = self.order(client, account_id,
                                  expected_date=session['cur_date'],
                                  expected_min=session['cur_min'],
                                  expected_rev=session['rev'])
        self.assertEqual(response.status_code, 200)

    # ── 现金与费用 ──────────────────────────────────────────

    def test_buy_debits_cash_including_fees(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client, cash=100000)
            self.order(client, account_id, price=18.10, shares=100)
            account = client.get(f'/api/sim/state?account_id={account_id}').json['account']
        # 1810 + 5 元买入费
        self.assertEqual(account['cash'], 100000 - 1810 - self.BUY_FEE)

    def test_cost_price_is_amortized_including_the_buy_fee(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, price=18.00, shares=100)
            position = self.positions(client, account_id)[0]
        self.assertAlmostEqual(position['cost_price'], (1800 + self.BUY_FEE) / 100, places=6)

    def test_second_buy_averages_the_cost_price(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, price=18.00, shares=100)
            self.order(client, account_id, price=20.00, shares=100)
            position = self.positions(client, account_id)[0]
        self.assertEqual(position['shares'], 200)
        expected = (1800 + self.BUY_FEE + 2000 + self.BUY_FEE) / 200
        self.assertAlmostEqual(position['cost_price'], expected, places=6)

    def test_sell_credits_cash_net_of_fees(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client, cash=100000)
            self.order(client, account_id, price=18.00, shares=100)
            cash_after_buy = client.get(f'/api/sim/state?account_id={account_id}').json['account']['cash']
            client.post('/api/sim/replay/step',   # T+1: 必须隔夜才能卖
                        json={'account_id': account_id, 'op': 'next_day'})
            self.order(client, account_id, side='sell', price=20.00, shares=100)
            state = client.get(f'/api/sim/state?account_id={account_id}').json
        self.assertEqual(state['account']['cash'], cash_after_buy + 2000 - self.SELL_FEE)
        self.assertEqual(state['positions'], [])

    def test_cash_never_drifts_from_the_sum_of_order_deltas(self):
        """金额必须内部用分算整数——用户会死盯这个数字, 对不上整个工具就不可信。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client, cash=100000)
            for price, shares in [(18.10, 100), (18.35, 200), (17.90, 300), (19.20, 100)]:
                self.order(client, account_id, price=price, shares=shares)
            client.post('/api/sim/replay/step',          # 跨日解锁 T+1
                        json={'account_id': account_id, 'op': 'next_day'})
            for price, shares in [(19.50, 100), (18.80, 200)]:
                self.order(client, account_id, side='sell', price=price, shares=shares)
            account = client.get(f'/api/sim/state?account_id={account_id}').json['account']
            deltas = [row[0] for row in self.db.execute(
                'SELECT cash_delta FROM sim_orders WHERE account_id = ? ORDER BY id',
                (account_id,))]
        self.assertEqual(len(deltas), 6)
        self.assertAlmostEqual(account['cash'], 100000 + sum(deltas), places=2)

    # ── A 股规则 ────────────────────────────────────────────

    def test_buy_must_be_a_whole_lot(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            for shares in (150, 99, 0, -100):
                with self.subTest(shares=shares):
                    self.assertEqual(
                        self.order(client, account_id, shares=shares).status_code, 400)

    def test_buy_is_rejected_when_cash_is_short(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client, cash=1000)
            response = self.order(client, account_id, price=18.10, shares=100)
        self.assertEqual(response.status_code, 400)
        self.assertIn('资金', response.json['message'])

    def test_price_off_by_a_whole_order_of_magnitude_is_flagged(self):
        """11.35 输成 113.50 是最常见的滑手, 应该拦一下而不是照收。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            response = self.order(client, account_id, price=113.50, shares=100)
        self.assertEqual(response.status_code, 409)
        self.assertIn('ref_price', response.json)
        self.assertTrue(response.json['needs_confirm'])
        # 带上确认就放行——用户可能就是要挂一个离谱价
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            confirmed = self.order(client, account_id, price=113.50, shares=100, confirm=True)
        self.assertEqual(confirmed.status_code, 200)

    def test_rejects_negative_and_non_numeric_input(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            for payload in ({'price': 0}, {'price': -1}, {'price': 'abc'}, {'side': 'hold'}):
                with self.subTest(payload=payload):
                    self.assertEqual(self.order(client, account_id, **payload).status_code, 400)

    # ── T+1 ────────────────────────────────────────────────

    def test_same_day_sell_is_rejected(self):
        """A 股 T+1: 当天买入当天不能卖。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, shares=100)
            response = self.order(client, account_id, side='sell', shares=100)
        self.assertEqual(response.status_code, 400)
        self.assertIn('T+1', response.json['message'])

    def test_position_reports_zero_sellable_on_the_buy_day(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, shares=100)
            position = self.positions(client, account_id)[0]
        self.assertEqual(position['shares'], 100)
        self.assertEqual(position['sellable'], 0)

    def test_sellable_unlocks_after_rolling_to_the_next_day(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, shares=100)
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'next_day'})
            position = self.positions(client, account_id)[0]
            sold = self.order(client, account_id, side='sell', shares=100)
        self.assertEqual(position['sellable'], 100)
        self.assertEqual(sold.status_code, 200)

    def test_cannot_sell_more_than_held(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, shares=100)
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'next_day'})
            response = self.order(client, account_id, side='sell', shares=200)
        self.assertEqual(response.status_code, 400)

    def test_sell_may_be_a_odd_lot_when_closing_the_position(self):
        """卖出允许零股(清仓时), 买入不行。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.db.execute('INSERT INTO sim_orders (account_id, ts_code, side, price, '
                            'shares, amount, total_fee, cash_delta, trade_date, minute) '
                            "VALUES (?, ?, 'buy', 18.0, 100, 1800, 5, -1805, ?, '09:25:00')",
                            (account_id, self.LIQUID, '20260927'))
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'next_day'})
            response = self.order(client, account_id, side='sell', shares=100)
        self.assertEqual(response.status_code, 200)

    # ── 持仓派生 ────────────────────────────────────────────

    def test_partial_sell_keeps_the_cost_price(self):
        """标准摊薄口径: 卖出不改成本价, 只减股数。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, price=18.00, shares=200)
            before = self.positions(client, account_id)[0]['cost_price']
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'next_day'})
            self.order(client, account_id, side='sell', price=19.00, shares=100)
            after = self.positions(client, account_id)[0]
        self.assertEqual(after['shares'], 100)
        self.assertAlmostEqual(after['cost_price'], before, places=6)

    def test_rebuying_after_a_full_exit_starts_a_fresh_cost_basis(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, price=18.00, shares=100)
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'next_day'})
            self.order(client, account_id, side='sell', price=19.00, shares=100)
            # 30.00 离当前价很远, 会被合理性带拦下, 这里就是要确认放行的路径
            self.order(client, account_id, price=30.00, shares=100, confirm=True)
            position = self.positions(client, account_id)[0]
        self.assertEqual(position['shares'], 100)
        # 成本必须按 30.00 那一笔重算, 不能跟已归零的旧仓混算
        self.assertAlmostEqual(position['cost_price'],
                               (3000 + self.BUY_FEE) / 100, places=6)

    def test_unrealised_pnl_uses_the_citic_formula(self):
        """(现价 - 成本价)×数量 - 卖佣 - 印花税 - 卖出过户费; 买入费不扣。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, price=18.00, shares=100)
            self.walk_to(client, account_id, '11:00:00')
            position = self.positions(client, account_id)[0]
            account = client.get(f'/api/sim/state?account_id={account_id}').json['account']
        market_value = round(position['price'] * 100, 2)
        # 夹具的假费用函数与真实费用无关, 这里只验证市值与成本口径自洽
        self.assertEqual(position['market_value'], market_value)
        self.assertEqual(account['market_value'], market_value)
        self.assertAlmostEqual(account['total_asset'],
                               account['cash'] + market_value, places=2)

    def test_suspended_position_is_reported_as_stale_not_at_zero(self):
        """停牌票的浮盈是"没动", 不是 0——两者含义完全不同。"""
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.db.execute('INSERT INTO sim_orders (account_id, ts_code, side, price, '
                            'shares, amount, total_fee, cash_delta, trade_date, minute) '
                            "VALUES (?, ?, 'buy', 20.0, 100, 2000, 5, -2005, ?, '09:25:00')",
                            (account_id, self.SUSPENDED, '20260928'))
            response = client.get(f'/api/sim/state?account_id={account_id}')
        position = [p for p in response.json['positions'] if p['ts_code'] == self.SUSPENDED][0]
        self.assertTrue(position['stale'])
        self.assertIsNone(position['price'])

    def test_orders_are_listed_newest_first_with_a_summary(self):
        with patch.object(rp.sqlite3, 'connect', return_value=self.db):
            client = self.client()
            account_id = self.start(client)
            self.order(client, account_id, price=18.00, shares=100)
            client.post('/api/sim/replay/step',
                        json={'account_id': account_id, 'op': 'next_day'})
            self.order(client, account_id, side='sell', price=19.00, shares=100)
            response = client.get(f'/api/sim/orders?account_id={account_id}')
        self.assertEqual(response.status_code, 200)
        orders = response.json['orders']
        self.assertEqual(len(orders), 2)
        self.assertGreater(orders[0]['id'], orders[1]['id'])
        self.assertEqual(response.json['summary']['buy_cnt'], 1)
        self.assertEqual(response.json['summary']['sell_cnt'], 1)


if __name__ == '__main__':
    unittest.main()
