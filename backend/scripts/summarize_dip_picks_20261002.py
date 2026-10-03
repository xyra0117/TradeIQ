"""Summarize a frozen read-only dip ledger; exploratory tests control date and board."""
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs'
STAMP = '20261002'
TARGETS = ['all_positive', 'touch10', 'both']
LABELS = {
    'price':'入选收盘价', 'day_return':'入选日涨跌幅', 'amount_yi':'入选日成交额/亿元',
    'amount5_daily_yi':'近5日日均成交额/亿元', 'opening_gap':'入选日开盘涨幅',
    'volume_vs_prior':'当日量/前日量', 'volume_ratio5':'当日量/前5日均量',
    'volume_trend5_20':'前5日均量/前20日均量', 'daily_amplitude':'入选日振幅',
    'amplitude20':'20日平均振幅', 'close_location':'收盘在当日高低区间的位置/%',
    'lower_wick':'下影线占当日区间/%', 'upper_wick':'上影线占当日区间/%',
    'drawdown20':'距20日最高价/%', 'drawdown60':'距60日最高价/%',
    'volatility20':'近20日涨跌幅标准差', 'negative_days5':'近5日下跌天数',
    'prior_ret5':'入选日前5日涨幅', 'listing_years':'上市年数',
    'main_positive_days5':'近5日主力净流入天数', 'main_acceleration5_20':'5日/20日日均主力净额比',
    'main_recent3_share':'3日主力净额/20日主力净额/%', 'ma5_above_ma20':'MA5高于MA20',
    'bull_stack':'MA5>MA10>MA20>MA60', 'industry_day_return':'所属行业当日等权涨跌幅',
    'industry_breadth':'所属行业上涨家数占比', 'relative_industry_return':'个股相对行业当日涨跌幅',
    'prior_limitup_count20':'近20日涨停表出现天数', 'prior_limitup_count5':'近5日涨停表出现天数',
    'prior_up9_days20':'此前19日涨幅超过9%的天数', 'ths_turnover':'同花顺换手率/%',
    'ths_net_yi':'同花顺当日净额/亿元', 'ths_strength':'同花顺当日净额/成交额/%',
    'minute_recovery_from_low':'收盘相对分时最低价涨幅', 'minute_time_below_prev_pct':'分时低于昨收时间占比',
    'minute_vwap_bias':'收盘相对分时均价偏离', 'minute_afternoon_return':'下午涨幅',
    'minute_last30_return':'最后30分钟涨幅', 'main_pct_provider':'东财当日主力净额占比',
    'super_pct_provider':'东财当日超大单净额占比', 'small_pct_provider':'东财当日小单净额占比',
}
for n in [3, 5, 10, 20, 30, 60, 120]:
    LABELS[f'return{n}'] = f'入选日{n}日涨幅/%'
for n in [5, 10, 20, 30, 60, 120]:
    LABELS[f'bias{n}'] = f'收盘相对MA{n}偏离/%'
    if n <= 60:
        LABELS[f'ma{n}_slope5'] = f'MA{n}较5日前变化/%'
for alias, name in [('main','主力'),('super','超大单'),('big','大单'),('mid','中单'),('small','小单')]:
    for n in [1, 3, 5, 10, 20]:
        LABELS[f'{alias}{n}_yi'] = f'{n}日{name}净额/亿元'
        LABELS[f'{alias}{n}_strength'] = f'{n}日{name}净额/同期成交额/%'


def clean(value):
    if isinstance(value, dict): return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [clean(v) for v in value]
    if isinstance(value, (np.integer, np.bool_)): return value.item()
    if isinstance(value, (float, np.floating)): return float(value) if np.isfinite(value) else None
    return value


def describe(group):
    n = len(group)
    if not n: return {'n':0}
    result = {'n':n, 'stocks':group.ts_code.nunique(), 'dates':group.trade_date.nunique()}
    for target in TARGETS: result[target + '_rate'] = group[target].mean() * 100
    for column in ['peak5','close5','adverse5','excess_sh_close5','excess_board_close5']:
        values = group[column].dropna()
        result[column] = {'n':len(values), 'mean':values.mean(), 'median':values.median(),
                          'p05':values.quantile(.05), 'p95':values.quantile(.95),
                          'min':values.min(), 'max':values.max(), 'positive_rate':(values > 0).mean() * 100}
    result['close5_loss5_rate'] = (group.close5 < -5).mean() * 100
    result['close5_loss10_rate'] = (group.close5 < -10).mean() * 100
    result['close5_after_removing_top5_mean'] = group.sort_values('close5').iloc[:max(1, n - math.ceil(n * .05))].close5.mean()
    return result


def bh(values):
    values = np.array(values, dtype=float)
    order = np.argsort(values)
    adjusted = np.minimum.accumulate((values[order] * len(values) / np.arange(1, len(values) + 1))[::-1])[::-1]
    result = np.zeros(len(values)); result[order] = np.minimum(1, adjusted)
    return result


PERM_CACHE = {}
def controlled_permutation(sub, column, target, repetitions=4999):
    """Permutation within entry-date and trading-board strata; not a causal test."""
    key = (target, tuple(sub.index))
    grouping = sub.groupby(['trade_date','board'], sort=False)
    ranks = grouping[column].rank(pct=True)
    centered = ranks - ranks.groupby([sub.trade_date,sub.board]).transform('mean')
    x, y = centered.to_numpy(), sub[target].to_numpy(dtype=float)
    score = float(x @ y)
    if np.dot(x,x) < 1e-12: return None, None
    if key not in PERM_CACHE:
        rng = np.random.default_rng(20261002 + len(PERM_CACHE))
        permuted = np.empty((repetitions,len(sub)))
        for indices in grouping.indices.values():
            indices = np.asarray(indices)
            order = np.argsort(rng.random((repetitions,len(indices))),axis=1)
            permuted[:,indices] = y[indices][order]
        PERM_CACHE[key] = permuted
    scores = PERM_CACHE[key] @ x
    p = (1 + np.count_nonzero(np.abs(scores) >= abs(score) - 1e-12)) / (repetitions + 1)
    return score, float(p)


def adjusted_difference(sub, mask, target, bootstrap=True):
    if sub.empty or not mask.any() or mask.all(): return {}
    work = sub[['trade_date','board',target]].copy(); work['feature'] = mask.to_numpy()
    groups = work.groupby(['trade_date','board','feature'])[target].agg(['mean','size']).unstack('feature')
    if True not in groups['mean'] or False not in groups['mean']: return {}
    groups = groups.dropna()
    if not len(groups): return {}
    weights = groups['size'][True] * groups['size'][False] / (groups['size'][True] + groups['size'][False])
    difference = (groups['mean'][True] - groups['mean'][False]) * 100
    bydate = pd.DataFrame({'num':weights * difference,'den':weights}).groupby(level=0).sum()
    result = {'adjusted_difference_pp':float(np.average(difference,weights=weights)),
              'matched_dates':len(bydate), 'matched_events':int(groups['size'].to_numpy().sum())}
    if bootstrap and len(bydate) >= 3:
        rng = np.random.default_rng(20261002)
        sample = rng.integers(0,len(bydate),size=(5000,len(bydate)))
        contrast = bydate['num'].to_numpy()[sample].sum(axis=1) / bydate['den'].to_numpy()[sample].sum(axis=1)
        result['date_cluster_ci95'] = np.quantile(contrast,[.025,.975]).tolist()
    return result


RULES = [
    ('缩量至前5日均量的80%及以下','volume_ratio5',lambda s:s<=.8),
    ('收盘在MA20上方5%以内','bias20',lambda s:s<=5),
    ('收盘高于MA5','bias5',lambda s:s>0),
    ('MA5高于MA20','ma5_above_ma20',lambda s:s>0),
    ('MA5>MA10>MA20>MA60','bull_stack',lambda s:s>0),
    ('收盘在MA60下方','bias60',lambda s:s<=0),
    ('20日平均振幅≥8%','amplitude20',lambda s:s>=8),
    ('入选日跌幅≥5%','day_return',lambda s:s<=-5),
    ('入选日跌幅不足2%','day_return',lambda s:s>-2),
    ('入选日前5日涨幅≤0','prior_ret5',lambda s:s<=0),
    ('近20日涨幅>15%','return20',lambda s:s>15),
    ('120日涨幅<100%','return120',lambda s:s<100),
    ('收盘距20日最高价回落≥10%','drawdown20',lambda s:s<=-10),
    ('收盘在当日高低区间上半部','close_location',lambda s:s>=50),
    ('下影线占当日区间≥30%','lower_wick',lambda s:s>=30),
    ('当日主力净额>0','main1_yi',lambda s:s>0),
    ('当日超大单净额>0','super1_yi',lambda s:s>0),
    ('当日大单净额>0','big1_yi',lambda s:s>0),
    ('当日小单净额<0','small1_yi',lambda s:s<0),
    ('5日主力净额/成交额≥5%','main5_strength',lambda s:s>=5),
    ('20日主力净额/成交额≥5%','main20_strength',lambda s:s>=5),
    ('5日日均主力净额>20日日均','main_acceleration5_20',lambda s:s>1),
    ('近5日主力净流入至少3天','main_positive_days5',lambda s:s>=3),
    ('近20日涨停表至少出现1天','prior_limitup_count20',lambda s:s>=1),
    ('所属行业当日平均跌幅小于个股','relative_industry_return',lambda s:s<0),
    ('同花顺換手率≥5%','ths_turnover',lambda s:s>=5),
    ('同花顺当日净额>0','ths_net_yi',lambda s:s>0),
    ('最后30分钟上涨','minute_last30_return',lambda s:s>0),
]


def main():
    frame = pd.read_csv(OUT / f'dip-picks-events-{STAMP}.csv',dtype={'ts_code':str,'trade_date':str})
    settled = frame[frame.complete].copy()
    report = json.loads((OUT / f'dip-picks-analysis-{STAMP}.json').read_text())
    report['outcomes'] = {'all':describe(settled), **{target:describe(settled[settled[target]]) for target in TARGETS}}
    report['time_slices'] = {}
    for name, group in [('2026年7至8月',settled[settled.trade_date<'20260901']),
                        ('2026年9月',settled[settled.trade_date>='20260901']),
                        ('去除同股重叠5日窗口',settled[settled.nonoverlap]),
                        ('每只股票仅首个入选事件',settled[settled.first_stock_event])]:
        report['time_slices'][name] = describe(group)
    report['monthly'] = {month:describe(group) for month, group in settled.groupby(settled.trade_date.str[:6])}
    perdate = []
    for day, group in frame.groupby('trade_date'):
        complete = group[group.complete]
        row = {'trade_date':day,'selected':len(group),'complete':len(complete),**describe(complete)}
        for col in frame.columns:
            if col.startswith(('market_','index_','sector_','post_index_')):
                row[col] = group[col].dropna().iloc[0] if group[col].notna().any() else None
        perdate.append(row)
    report['perdate'] = perdate
    pd.DataFrame(perdate).to_csv(OUT/f'dip-picks-dates-{STAMP}.csv',index=False,encoding='utf-8-sig')
    report['boards'] = {name:describe(group) for name,group in settled.groupby('board')}
    report['industries'] = {name:describe(group) for name,group in settled.groupby('industry')}
    report['day_balanced_rates'] = {target:settled.groupby('trade_date')[target].mean().mean()*100 for target in TARGETS}
    report['leave_one_date_out'] = {target:{day:describe(settled[settled.trade_date!=day])[target+'_rate']
                                           for day in settled.trade_date.unique()} for target in TARGETS}
    numeric, categorical = [], []
    features = [col for col in LABELS if col in settled and settled[col].notna().sum()>=20 and settled[col].nunique()>1]
    print('summarizing',len(features),'entry-date stock features',flush=True)
    for target in TARGETS:
        target_numeric = []
        for col in features:
            sub = settled[np.isfinite(settled[col])]
            yes, no = sub[sub[target]][col], sub[~sub[target]][col]
            score, p = controlled_permutation(sub,col,target)
            target_numeric.append({'target':target,'feature':col,'label':LABELS[col],'valid_n':len(sub),
                                   'winner_n':len(yes),'other_n':len(no),'winner_median':yes.median(),
                                   'other_median':no.median(),'winner_mean':yes.mean(),'other_mean':no.mean(),
                                   'date_board_rank_score':score,'permutation_p':p})
        for label, col, fn in RULES:
            if col not in settled:continue
            sub = settled[np.isfinite(settled[col])].copy(); mask=fn(sub[col]); sub['rule']=mask.astype(float)
            yes, no = sub[mask], sub[~mask]
            if not len(yes) or not len(no):continue
            score,p = controlled_permutation(sub,'rule',target)
            entry={'target':target,'rule':label,'feature':col,'valid_n':len(sub),'yes_n':len(yes),'no_n':len(no),
                   'yes_rate':yes[target].mean()*100,'no_rate':no[target].mean()*100,
                   'raw_difference_pp':(yes[target].mean()-no[target].mean())*100,
                   'winner_feature_share':mask[sub[target]].mean()*100,'other_feature_share':mask[~sub[target]].mean()*100,
                   'permutation_p':p,**adjusted_difference(sub,mask,target)}
            for name, group in [('early',sub[sub.trade_date<'20260901']),('late',sub[sub.trade_date>='20260901']),
                                ('nonoverlap',sub[sub.nonoverlap]),('first_stock',sub[sub.first_stock_event])]:
                split=fn(group[col]);a,b=group[split],group[~split]
                entry[name+'_difference_pp'] = (a[target].mean()-b[target].mean())*100 if len(a) and len(b) else None
                entry[name+'_adjusted_difference_pp'] = adjusted_difference(group,split,target,False).get('adjusted_difference_pp')
            categorical.append(entry)
        # FDR controls the full searched stock-feature family, including fixed rule thresholds.
        family = [r for r in target_numeric + [r for r in categorical if r['target']==target] if r['permutation_p'] is not None]
        for row,q in zip(family,bh([r['permutation_p'] for r in family])):row['fdr_q']=float(q)
        numeric.extend(target_numeric)
        print('finished',target,flush=True)
    report['feature_count']=len(features)
    report['features']=numeric;report['rules']=categorical
    report['coverage_in_selected']={col:{'all_events':int(frame[col].notna().sum()),'complete_events':int(settled[col].notna().sum())}
                                   for col in frame if col in LABELS or col.startswith(('market_','index_','sector_'))}
    report['first_touch10_days']=settled.loc[settled.touch10,'first_touch10_day'].value_counts().sort_index().to_dict()
    report['peak_days']={target:settled.loc[settled[target],'peak_day'].value_counts().sort_index().to_dict() for target in TARGETS}
    pd.DataFrame(numeric).to_csv(OUT/f'dip-picks-features-{STAMP}.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(categorical).to_csv(OUT/f'dip-picks-rule-comparisons-{STAMP}.csv',index=False,encoding='utf-8-sig')
    stocks=frame.groupby(['ts_code','name','board','industry'],dropna=False).agg(
        selected=('trade_date','size'),complete=('complete','sum'),all_positive=('all_positive','sum'),
        touch10=('touch10','sum'),both=('both','sum'),first_date=('trade_date','min'),last_date=('trade_date','max'),
        best_peak=('peak5','max'),median_close5=('close5','median')).reset_index()
    stocks.to_csv(OUT/f'dip-picks-stocks-{STAMP}.csv',index=False,encoding='utf-8-sig')
    (OUT/f'dip-picks-analysis-{STAMP}.json').write_text(json.dumps(clean(report),ensure_ascii=False,indent=2))
    for target in TARGETS:
        print(target,'adjusted rules',json.dumps(clean(sorted([r for r in categorical if r['target']==target and r.get('adjusted_difference_pp') is not None],key=lambda r:abs(r['adjusted_difference_pp']),reverse=True)[:10]),ensure_ascii=False),flush=True)
        print(target,'best continuous',json.dumps(clean(sorted([r for r in numeric if r['target']==target and r['permutation_p'] is not None],key=lambda r:r['permutation_p'])[:12]),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
