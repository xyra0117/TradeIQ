"""Meaningful arithmetic, timing, completeness and read-only regression checks."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).parent/'scripts'/'analyze_preferred_macd_20261008.py'
SPEC = importlib.util.spec_from_file_location('preferred_macd_research',SCRIPT)
study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(study)


def fixture(prices):
    rows = []
    for i,price in enumerate(prices):
        rows.append(dict(trade_date=f'{i:08d}',open=price,high=price*1.01,low=price*.99,close=price,
                         change=(price/prices[i-1]-1)*100 if i else 0))
    calendar = [r['trade_date'] for r in rows]
    return rows,calendar,{d:i for i,d in enumerate(calendar)}


class IndicatorTests(unittest.TestCase):
    def test_ema_known_second_bar(self):
        rows,_,pos = fixture([10,11])
        out = study.indicators(rows,pos)
        dif = 10+2/13-(10+2/27)
        self.assertAlmostEqual(out[rows[-1]['trade_date']]['dif'],dif)
        self.assertAlmostEqual(out[rows[-1]['trade_date']]['dea'],dif*.2)
        self.assertAlmostEqual(out[rows[-1]['trade_date']]['hist'],dif*1.6)

    def test_no_future_leakage(self):
        prices = list(10+np.sin(np.arange(150)/9))
        rows,_,pos = fixture(prices+[1000,900,800])
        whole = study.indicators(rows,pos)
        prefix = study.indicators(rows[:150],pos)
        self.assertEqual(prefix[rows[149]['trade_date']],whole[rows[149]['trade_date']])

    def test_crossing_on_current_bar_and_five_bar_window(self):
        rows,_,pos = fixture([10]*120+[11,12,13,14,15,16])
        out = study.indicators(rows,pos)
        self.assertTrue(out[rows[120]['trade_date']]['flags']['gold_today'])
        self.assertTrue(out[rows[124]['trade_date']]['flags']['gold5'])
        self.assertFalse(out[rows[125]['trade_date']]['flags']['gold5'])
        self.assertFalse(out[rows[119]['trade_date']]['flags']['gold_today'])

    def test_reference_break_resets_warmup(self):
        rows,_,pos = fixture([10]*130+[5])
        rows[-1]['change'] = 0
        strict = study.indicators(rows,pos,True)
        compat = study.indicators(rows,pos,False)
        self.assertEqual(strict[rows[-1]['trade_date']]['warmup'],1)
        self.assertEqual(strict[rows[-1]['trade_date']]['hist'],0)
        self.assertEqual(compat[rows[-1]['trade_date']]['warmup'],131)
        self.assertLess(compat[rows[-1]['trade_date']]['hist'],0)

    def test_scale_normalization(self):
        prices = [10+np.sin(i/6) for i in range(140)]
        rows,_,pos = fixture(prices)
        bigger,_,_ = fixture([p*10 for p in prices])
        a = study.indicators(rows,pos)[rows[-1]['trade_date']]
        b = study.indicators(bigger,pos)[rows[-1]['trade_date']]
        self.assertAlmostEqual(a['dif_pct'],b['dif_pct'])
        self.assertAlmostEqual(a['hist_pct'],b['hist_pct'])

    def test_twenty_prespecified_features(self):
        self.assertEqual(len(study.FEATURES),20)


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.rows,self.cal,self.pos = fixture([100]*6)
        self.by_day = {r['trade_date']:r for r in self.rows}

    def result(self):
        return study.outcome(self.by_day,self.cal[0],self.cal,self.pos,100,self.cal[-1])

    def test_strict_ten_percent_boundary(self):
        self.rows[2]['high'] = 110
        self.assertFalse(self.result()['winner'])
        self.rows[2]['high'] = 110.01
        self.assertTrue(self.result()['winner'])

    def test_maximum_across_five_days_not_day_five_only(self):
        self.rows[1]['high'] = 115
        result = self.result()
        self.assertAlmostEqual(result['peak_pct'],15)
        self.assertEqual(result['peak_day'],self.cal[1])
        self.assertAlmostEqual(result['close5_pct'],0)

    def test_missing_day_is_not_zero_or_skip(self):
        self.by_day.pop(self.cal[2])
        self.assertEqual(self.result()['outcome_reason'],'missing_future_daily')
        self.assertNotIn('winner',self.result())

    def test_future_reference_mismatch(self):
        self.rows[3]['change'] = 1
        self.assertEqual(self.result()['outcome_reason'],'future_reference_mismatch')

    def test_not_yet_closed_window(self):
        result = study.outcome(self.by_day,self.cal[0],self.cal,self.pos,100,self.cal[-2])
        self.assertEqual(result['outcome_reason'],'future_unavailable')

    def test_entry_snapshot_price_must_match_sql(self):
        result = study.outcome(self.by_day,self.cal[0],self.cal,self.pos,99,self.cal[-1])
        self.assertEqual(result['outcome_reason'],'entry_price_mismatch')


class StatisticalTests(unittest.TestCase):
    def test_fisher_known_table(self):
        self.assertAlmostEqual(study.fisher_two_sided(1,9,11,3),.0027594561852200836)

    def test_bh_multiple_comparison(self):
        np.testing.assert_allclose(study.bh_adjust([.01,.04,.03]),[.03,.04,.04])

    def test_tiny_zero_success_group_has_no_bootstrap_interval(self):
        rows = [dict(trade_date=str(i),winner=False,flags={'test':True}) for i in range(4)]
        rows += [dict(trade_date=str(i),winner=i%2==0,flags={'test':False}) for i in range(4,100)]
        self.assertEqual(study.clustered_ci(rows,'test','trade_date'),[None,None])

    def test_stock_windows_are_deduplicated(self):
        _,cal,pos = fixture([10]*12)
        rows = [dict(ts_code='A',trade_date=cal[i]) for i in [0,1,4,5,7,10]]
        self.assertEqual([r['trade_date'] for r in study.nonoverlap(rows,pos)],[cal[i] for i in [0,5,10]])

    def test_readonly_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'fixture.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE fixture(v INTEGER)')
            conn = study.open_db(path)
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0],1)
            self.assertTrue(conn.in_transaction)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('INSERT INTO fixture VALUES(1)')
            conn.rollback()
            conn.close()


if __name__ == '__main__':
    unittest.main()
