"""Regression tests for labels, causal entry features and unit/source checks."""
import importlib.util
import unittest
from pathlib import Path

import numpy as np

SCRIPT=Path(__file__).parent/'scripts'/'analyze_preferred_outcome_groups_20261008.py'
SPEC=importlib.util.spec_from_file_location('preferred_groups',SCRIPT)
study=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(study)


def fixture(days=80):
    calendar=[f'{i:08d}' for i in range(days)]
    pos={d:i for i,d in enumerate(calendar)}
    daily={d:dict(trade_date=d,open=100,high=103,low=97,close=100,change=0,volume=1000,amount=100000) for d in calendar}
    flows={d:dict(trade_date=d,source='eastmoney-push2',main_net_inflow=15000000,small_net=-5000000) for d in calendar}
    return daily,flows,calendar,pos


class LabelTests(unittest.TestCase):
    def test_positive_and_above5_overlap(self):
        r=study.classify([1,2,6,4,3]);self.assertTrue(r['all_positive']);self.assertTrue(r['above5'])
        self.assertFalse(r['all_negative']);self.assertEqual(r['exclusive'],'A且C')

    def test_positive_without_touch(self):
        r=study.classify([1,2,3,4,5]);self.assertEqual(r['exclusive'],'A非C')
        self.assertFalse(r['above5'])

    def test_negative_never_above5(self):
        r=study.classify([-1,-2,-3,-4,-5]);self.assertEqual(r['exclusive'],'B')
        self.assertFalse(r['above5']);self.assertFalse(r['all_positive'])

    def test_mixed_touch_and_not_rolling_max(self):
        r=study.classify([1,-1,6,-2,2]);self.assertEqual(r['exclusive'],'其他且C')
        self.assertFalse(r['all_positive'])

    def test_zero_excludes_both_strict_sign_groups(self):
        r=study.classify([0,1,2,3,4]);self.assertEqual(r['exclusive'],'其他非C')
        r=study.classify([0,-1,-2,-3,-4]);self.assertFalse(r['all_negative'])

    def test_strict_five_percent_tolerance(self):
        self.assertFalse(study.classify([1,2,3,4,5+1e-12])['above5'])
        self.assertTrue(study.classify([1,2,3,4,5.0001])['above5'])

    def test_missing_or_nonfinite_never_gets_label(self):
        for path in [[1,2,3],[1,2,3,4,None],[1,2,3,4,float('nan')]]:
            with self.assertRaises(ValueError):study.classify(path)

    def test_exclusive_partition_exhaustive(self):
        rng=np.random.default_rng(123)
        paths=rng.normal(size=(500,5))*7
        groups=[study.classify(p) for p in paths]
        self.assertEqual(len(groups),sum(sum(r['exclusive']==k for r in groups) for k in ['A且C','A非C','B','其他且C','其他非C']))


class EntryFeatureTests(unittest.TestCase):
    def test_amount_units_and_flow_strength(self):
        daily,flows,cal,pos=fixture();r=study.past_features(daily,flows,cal[70],cal,pos,'eastmoney-push2')
        self.assertAlmostEqual(r['amount_yi'],1)
        self.assertAlmostEqual(r['strength1'],15)
        self.assertAlmostEqual(r['strength5'],15)
        self.assertAlmostEqual(r['small_strength1'],-5)
        self.assertAlmostEqual(r['main5_yi'],.75)
        self.assertAlmostEqual(r['amp20'],6)

    def test_future_changes_do_not_change_entry_features(self):
        daily,flows,cal,pos=fixture();day=cal[65]
        before=study.past_features(daily,flows,day,cal,pos,'eastmoney-push2')
        for d in cal[66:]:
            daily[d].update(close=1000,high=2000,low=1,volume=10000000)
            flows[d].update(main_net_inflow=-1e12)
        self.assertEqual(before,study.past_features(daily,flows,day,cal,pos,'eastmoney-push2'))

    def test_missing_price_day_blocks_window(self):
        daily,flows,cal,pos=fixture();daily.pop(cal[60])
        self.assertIsNone(study.past_features(daily,flows,cal[70],cal,pos,'eastmoney-push2')['ret20'])

    def test_price_reference_break_blocks_longer_feature_only(self):
        daily,flows,cal,pos=fixture();daily[cal[55]]['change']=10
        r=study.past_features(daily,flows,cal[70],cal,pos,'eastmoney-push2')
        self.assertIsNone(r['ret20']);self.assertAlmostEqual(r['ret5'],0)

    def test_mixed_flow_source_never_fills_window(self):
        daily,flows,cal,pos=fixture();flows[cal[68]]['source']='eastmoney-daykline'
        r=study.past_features(daily,flows,cal[70],cal,pos,'eastmoney-push2')
        self.assertIsNone(r['strength5']);self.assertIsNone(r['main5_yi'])

    def test_missing_amount_not_zero(self):
        daily,flows,cal,pos=fixture();daily[cal[69]]['amount']=None
        r=study.past_features(daily,flows,cal[70],cal,pos,'eastmoney-push2')
        self.assertIsNone(r['strength5']);self.assertAlmostEqual(r['main5_yi'],.75)

    def test_volume_uses_previous_valid_record(self):
        daily,flows,cal,pos=fixture();daily[cal[69]]['volume']=2000
        r=study.past_features(daily,flows,cal[70],cal,pos,'eastmoney-push2')
        self.assertAlmostEqual(r['volume_ratio'],50)

    def test_five_day_outcome_requires_full_window(self):
        daily,flows,cal,pos=fixture();daily.pop(cal[68])
        r=study.macd.outcome(daily,cal[65],cal,pos,100,cal[-1])
        self.assertEqual(r['outcome_reason'],'missing_future_daily')


class StatisticalTests(unittest.TestCase):
    def test_missing_indicator_does_not_become_false(self):
        flags=study.make_flags(dict(board='主板'))
        self.assertIsNone(flags['above_ma20']);self.assertIsNone(flags['volume_le80'])
        self.assertFalse(flags['growth_board'])

    def test_multiple_testing_family_is_thirty_nine(self):
        self.assertEqual(len(study.comparisons([])),39)

    def test_description_uses_valid_denominator(self):
        s=study.distribution([1,None,3,float('nan')]);self.assertEqual(s['n'],2)
        self.assertEqual(s['median'],2)

    def test_permutation_does_not_move_labels_into_missing_feature_rows(self):
        keys=list(study.EXTRA)
        rows=[]
        for i,value in enumerate([True,False,None]):
            flags={k:None for k in keys};flags['amplitude20_high']=value
            rows.append(dict(trade_date='20260601',snapshot_volume=80,board='主板',entry_flags=flags,
                             all_positive=False,all_negative=False,above5=i==2))
        result=next(x for x in study.controlled_comparisons(rows,draws=100)
                    if x['group']=='above5' and x['feature']=='amplitude20_high')
        self.assertEqual(result['n'],0);self.assertIsNone(result['p'])

    def test_filter_never_uses_outcome_and_missing_risk_is_kept(self):
        row=dict(board='主板',amp20=4,all_negative=True)
        self.assertTrue(study.exclude_candidate(row,('main_board','low_amplitude')))
        row['all_negative']=False
        self.assertTrue(study.exclude_candidate(row,('main_board','low_amplitude')))
        row['amp20']=None
        self.assertFalse(study.exclude_candidate(row,('main_board','low_amplitude')))

    def test_filter_grid_is_one_hundred_five(self):
        self.assertEqual(len(study.candidate_grid()),105)

    def test_filter_selection_is_not_affected_by_later_rows(self):
        def row(day,bad):
            return dict(trade_date=day,ts_code=day,board='主板' if bad else '创业板',amp20=4 if bad else 7,
                        all_negative=bad,all_positive=not bad,above5=not bad,close5_pct=-10 if bad else 5,end_date=day)
        earlier=[row(f'202605{i:02d}',i<=10) for i in range(1,41)]
        later=[row('20260701',False)]
        first=study.research_risk_filters(earlier+later,earlier+later)
        later=[row('20260701',True)]
        second=study.research_risk_filters(earlier+later,earlier+later)
        self.assertEqual(first['selected']['平衡']['keys'],second['selected']['平衡']['keys'])

    def test_development_windows_ending_in_july_are_purged(self):
        row=dict(trade_date='20260629',ts_code='A',end_date='20260706',board='主板',
                 all_negative=True,all_positive=False,above5=False,close5_pct=-10)
        result=study.research_risk_filters([row],[row])
        self.assertEqual(result['development_n'],0)
        self.assertEqual(result['purged_cross_boundary'],1)

    def test_delayed_confirmation_uses_first_close_only(self):
        row=dict(close_path=[1,-10,-20,-30,-40])
        self.assertTrue(study.next_close_confirmed(row))
        row['close_path'][1:]=[100,100,100,100]
        self.assertTrue(study.next_close_confirmed(row))
        row['close_path'][0]=-1
        self.assertFalse(study.next_close_confirmed(row))

    def test_negative_daily_high_implies_next_close_cannot_confirm(self):
        daily,flows,cal,pos=fixture(days=6)
        for d in cal[1:]:daily[d].update(high=99,low=95,open=97,close=97,change=0)
        daily[cal[1]]['change']=-3
        result=study.macd.outcome(daily,cal[0],cal,pos,100,cal[-1])
        self.assertTrue(study.classify(result['high_path'])['all_negative'])
        self.assertFalse(study.next_close_confirmed(dict(close_path=[-3]*5)))


if __name__=='__main__':unittest.main()
