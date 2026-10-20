"""条件优选「站上5日线」开关: 临时库, 不触碰实际数据库。

口径: MA5 = 含入选日在内的最近 5 个有效收盘价均值, 与条件选股新的 ma10 同窗口定义。
"""
import sqlite3
import unittest
from datetime import date, timedelta

import app as backend

ENTRY = '20260320'

# 刻意造出三种入选日均线关系 (最后 5 根收盘价, 最后一根 = 入选日):
#   UP    收盘 12, 前面都是 1  -> 远高于 MA5, 该进
#   DOWN  收盘 4, 前面都是 20 -> 远低于 MA5, 不该进
#   EDGE 收盘 8, 前面都是 8 -> MA5 恰好等于收盘, 等于不算「站上」
CLOSES = {
    'UP.SZ':    [1, 1, 1, 1, 1, 1, 1, 1, 1, 12],
    'DOWN.SZ':  [20, 20, 20, 20, 20, 20, 20, 20, 20, 4],
    'EDGE.SZ':  [8] * 10,
}


def _ma5(closes):
    return sum(closes[-5:]) / 5


class PreferredMa5ToggleTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE trading_dates_cache (cal_date TEXT);
            CREATE TABLE fund_flow (ts_code TEXT, trade_date TEXT, source TEXT,
                main_net_inflow REAL, small_net REAL, small_pct REAL);
            CREATE TABLE stock_daily (ts_code TEXT, trade_date TEXT, open REAL,
                high REAL, close REAL, change REAL, amount REAL, volume REAL);
        ''')
        # 60 根日线才够 MA60; 前 50 根填成常数, 最后 10 根用上面的收盘价
        all_days = self._sixty_days()
        self.assertEqual(all_days[-1], ENTRY)
        # 交易日历只插一遍: 重复插会让「最近5个交易日」变成同一天的 5 行, 资金窗口校验直接失败
        for day in all_days:
            self.db.execute('INSERT INTO trading_dates_cache VALUES (?)', (day,))
        for code, closes in CLOSES.items():
            history = [closes[0]] * 50 + closes
            self.assertEqual(len(history), 60)
            for day, close in zip(all_days, history):
                self.db.execute('INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?)',
                                (code, day, close, close, close, 0, 1e8, 1000))
                # 5 日主力强度 = Σ(main_net_inflow) / Σ(amount×1000) × 100 ≥ 5
                # amount=1e8 时每天主力净流入要给到 5e9 才够门槛
                self.db.execute('INSERT INTO fund_flow VALUES (?,?,?,?,?,?)',
                                (code, day, 'eastmoney-push2', 5e9, -1e6, -2))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    @staticmethod
    def _sixty_days():
        end = date(2026, 3, 20)
        return [(end - timedelta(days=i)).strftime('%Y%m%d') for i in range(59, -1, -1)]

    def _row(self, code):
        return {'ts_code': code, 'close': CLOSES[code][-1], 'volume': 1000,
                'prev_volume': 1000, 'volume_ratio_pct': 50}

    def _filter(self, require_ma5):
        hits, stats = backend._filter_conditional_preferred(
            [self._row(c) for c in CLOSES], ENTRY, self.db, require_ma5=require_ma5)
        return {h['ts_code'] for h in hits}, stats

    def test_parser_only_accepts_explicit_truthy(self):
        for truthy in ('1', 'true', 'True', 'on', 'yes', True, 1):
            self.assertTrue(backend._parse_preferred_ma5(truthy), msg=repr(truthy))
        for falsy in (None, '', '0', 'false', 'off', 'no', '随便写', 0, False, []):
            self.assertFalse(backend._parse_preferred_ma5(falsy), msg=repr(falsy))

    def test_toggle_off_keeps_original_conditions(self):
        off_codes, stats = self._filter(False)
        self.assertEqual(off_codes, set(CLOSES))          # 三只全进
        self.assertEqual(stats['matched'], 3)
        self.assertEqual(stats['below_ma5'], 0)          # 关着时不因 MA5 剔除
        self.assertEqual(stats['missing_ma5'], 0)

    def test_toggle_on_only_keeps_stocks_above_ma5(self):
        on_codes, stats = self._filter(True)
        self.assertEqual(on_codes, {'UP.SZ'})
        self.assertEqual(stats['matched'], 1)
        self.assertEqual(stats['below_ma5'], 2)           # DOWN + EDGE 被剔除
        self.assertEqual(stats['missing_ma5'], 0)

    def test_close_equal_to_ma5_is_not_above(self):
        # EDGE 收盘 8, MA5 恰好也是 8 -> 严格大于才算站上
        self.assertEqual(_ma5(CLOSES['EDGE.SZ']), CLOSES['EDGE.SZ'][-1])
        on_codes, _ = self._filter(True)
        self.assertNotIn('EDGE.SZ', on_codes)

    def test_exposes_ma5_and_flag_in_both_modes(self):
        # 关闭时三只都在; 打开时 DOWN/EDGE 已被剔除, 只剩 UP
        for flag in (False, True):
            hits, _ = backend._filter_conditional_preferred(
                [self._row(c) for c in CLOSES], ENTRY, self.db, require_ma5=flag)
            by_code = {h['ts_code']: h for h in hits}
            self.assertAlmostEqual(by_code['UP.SZ']['preferred_ma5'], _ma5(CLOSES['UP.SZ']), places=4)
            self.assertFalse(by_code['UP.SZ']['preferred_below_ma5'])
            self.assertIn('preferred_ma60', by_code['UP.SZ'])   # 原有字段不能丢
            if flag:
                self.assertEqual(set(by_code), {'UP.SZ'})
            else:
                self.assertEqual(set(by_code), set(CLOSES))
                self.assertTrue(by_code['DOWN.SZ']['preferred_below_ma5'])
                self.assertTrue(by_code['EDGE.SZ']['preferred_below_ma5'])

    def test_missing_ma5_counters_always_present(self):
        """两个 MA5 计数字段恒存在, 且不会把别的剔除原因重复计进来。"""
        _, stats = self._filter(True)
        self.assertIn('below_ma5', stats)
        self.assertIn('missing_ma5', stats)
        # 3 只里 1 只进、2 只因未站上 5 日线被剔, 账要对得上
        self.assertEqual(stats['matched'] + stats['below_ma5'] + stats['missing_ma5'], stats['base_count'])

    def test_default_argument_preserves_callers(self):
        # 老调用方式 (不传 require_ma5) 必须跟显式关闭完全一样
        a, sa = backend._filter_conditional_preferred([self._row('UP.SZ')], ENTRY, self.db)
        b, sb = backend._filter_conditional_preferred([self._row('UP.SZ')], ENTRY, self.db,
                                                      require_ma5=False)
        self.assertEqual([h['ts_code'] for h in a], [h['ts_code'] for h in b])
        self.assertEqual(sa['matched'], sb['matched'])


if __name__ == '__main__':
    unittest.main()