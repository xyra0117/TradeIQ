"""Three overlapping historical outcome labels; all entry features are causal."""
import argparse
import csv
import importlib.util
import json
import math
from itertools import combinations
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PREFIX = ROOT/'docs'/'preferred-three-outcomes-20261008'
SPEC = importlib.util.spec_from_file_location('isolated_macd',Path(__file__).with_name('analyze_preferred_macd_20261008.py'))
macd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macd)
GROUPS = {'all_positive':'五日全正','all_negative':'五日全负','above5':'五日内至少一天>5%'}
EXTRA = {
    'above_ma20':'收盘在MA20上', 'below_ma60':'收盘在MA60下',
    'momentum20':'20日涨幅>15%', 'amplitude20_high':'20日均振幅≥5%',
    'dip3':'入选日跌幅<−3%', 'volume_le80':'真实成交量比≤80%',
    'mild_shrink':'真实成交量比70–80%', 'flow5_ge10':'5日主力强度≥10%',
    'main_today_positive':'当日主力净流入', 'small_today_negative':'当日小单净流出',
    'growth_board':'创业板或科创板', 'index_positive':'上证当日上涨',
    'breadth_positive':'当日上涨比例>50%',
}
CONTINUOUS = {
    'change0':'入选日涨跌幅%', 'ret5':'此前5日涨幅%', 'ret20':'此前20日涨幅%',
    'bias10':'收盘距MA10%', 'bias20':'收盘距MA20%', 'bias60':'收盘距MA60%',
    'amp20':'20日均振幅%', 'volatility20':'20日涨跌幅标准差%',
    'volume_ratio':'真实成交量比%', 'volume_to_mean5':'当日量/此前5日均量%',
    'close_position':'收盘在当天高低区间的位置%', 'amount_yi':'当日成交额亿元',
    'strength5':'5日主力强度%', 'strength1':'当日主力强度%',
    'small_strength1':'当日小单强度%', 'main3_yi':'3日主力净额亿元',
    'main5_yi':'5日主力净额亿元', 'main10_yi':'10日主力净额亿元',
    'main20_yi':'20日主力净额亿元', 'breadth':'当日上涨比例%',
    'down_count':'当日下跌家数', 'index_change':'上证当日涨跌幅%',
}
RISK_LABELS = {
    'main_board':'主板', 'low_amplitude':'20日均振幅<5%', 'weak_ret20':'20日涨幅<15%',
    'below_ma20':'收盘≤MA20', 'volume_gt80':'真实量比>80%', 'volume_ge100':'真实量比≥100%',
    'strong_flow5':'5日主力强度≥10%', 'main_outflow':'当日主力流出',
    'index_up':'上证当日上涨', 'breadth_up':'上涨比例>50%', 'mild_dip':'当日跌幅小于1%',
    'ret5_ge10':'此前5日涨幅≥10%', 'ret5_ge15':'此前5日涨幅≥15%', 'weak_close':'收盘位于当天区间下30%',
}
SEED = 20261008
EPS = 1e-10


def classify(path):
    if len(path)!=5 or any(v is None or not math.isfinite(v) for v in path):
        raise ValueError('Exactly five complete high-return values are required')
    a = all(v>EPS for v in path)
    b = all(v<-EPS for v in path)
    c = max(path)>5+EPS
    assert not(a and b) and not(b and c)
    exclusive = 'A且C' if a and c else 'A非C' if a else 'B' if b else '其他且C' if c else '其他非C'
    return dict(all_positive=a,all_negative=b,above5=c,exclusive=exclusive)


def finite(value):
    return value is not None and math.isfinite(value)


def price_window(daily,day,calendar,pos,length):
    p=pos.get(day)
    if p is None or p-length+1<0:
        return None
    rows=[daily.get(d) for d in calendar[p-length+1:p+1]]
    if len(rows)!=length or any(r is None or not macd.valid_bar(r) for r in rows):
        return None
    if any(not macd.close_reference_ok(rows[i],rows[i-1]['close']) for i in range(1,length)):
        return None
    return rows


def flow_window(flows,day,calendar,pos,length,source):
    p=pos.get(day)
    if p is None or p-length+1<0 or not source:
        return None
    rows=[flows.get(d) for d in calendar[p-length+1:p+1]]
    if len(rows)!=length or any(r is None or r['source']!=source or not finite(r.get('main_net_inflow')) for r in rows):
        return None
    return rows


def past_features(daily,flows,day,calendar,pos,source):
    row=daily.get(day)
    if row is None or not macd.valid_bar(row):
        return {}
    out=dict(change0=row.get('change'),amount_yi=row['amount']/100000 if macd.valid_price(row.get('amount')) else None,
             close_position=(row['close']-row['low'])/(row['high']-row['low'])*100 if row['high']>row['low'] else None)
    # Previous valid volume record matches the existing selection rule.
    previous=[r for d,r in daily.items() if d<day and macd.valid_price(r.get('close'))]
    prev=max(previous,key=lambda r:r['trade_date']) if previous else None
    out['volume_ratio']=row['volume']/prev['volume']*100 if prev and macd.valid_price(row.get('volume')) and macd.valid_price(prev.get('volume')) else None
    for length in [5,20]:
        window=price_window(daily,day,calendar,pos,length+1)
        out[f'ret{length}']=(row['close']/window[0]['close']-1)*100 if window else None
    for length in [10,20,60]:
        window=price_window(daily,day,calendar,pos,length)
        out[f'bias{length}']=(row['close']/np.mean([r['close'] for r in window])-1)*100 if window else None
    window=price_window(daily,day,calendar,pos,21)
    if window:
        out['amp20']=float(np.mean([(window[i]['high']-window[i]['low'])/window[i-1]['close']*100 for i in range(1,21)]))
        out['volatility20']=float(np.std([r['change'] for r in window[1:]],ddof=1))
    else:
        out['amp20']=out['volatility20']=None
    window=price_window(daily,day,calendar,pos,6)
    out['volume_to_mean5']=row['volume']/np.mean([r['volume'] for r in window[:-1]])*100 if window and all(macd.valid_price(r.get('volume')) for r in window) else None
    current=flows.get(day)
    if current and current['source']==source and macd.valid_price(row.get('amount')):
        out['strength1']=current['main_net_inflow']/(row['amount']*1000)*100 if finite(current.get('main_net_inflow')) else None
        out['small_strength1']=current['small_net']/(row['amount']*1000)*100 if finite(current.get('small_net')) else None
    else:
        out['strength1']=out['small_strength1']=None
    for length in [3,5,10,20]:
        window=flow_window(flows,day,calendar,pos,length,source)
        out[f'main{length}_yi']=sum(r['main_net_inflow'] for r in window)/1e8 if window else None
        if length==5:
            days=calendar[pos[day]-4:pos[day]+1]
            amounts=[daily.get(d,{}).get('amount') for d in days]
            out['strength5']=sum(r['main_net_inflow'] for r in window)/(sum(amounts)*1000)*100 if window and len(amounts)==5 and all(macd.valid_price(a) for a in amounts) else None
    return out


def make_flags(r):
    flags={}
    tests={
        'above_ma20':('bias20',lambda v:v>0),'below_ma60':('bias60',lambda v:v<=0),
        'momentum20':('ret20',lambda v:v>15),'amplitude20_high':('amp20',lambda v:v>=5),
        'dip3':('change0',lambda v:v<-3),'volume_le80':('volume_ratio',lambda v:v<=80),
        'mild_shrink':('volume_ratio',lambda v:70<=v<=80),'flow5_ge10':('strength5',lambda v:v>=10),
        'main_today_positive':('strength1',lambda v:v>0),'small_today_negative':('small_strength1',lambda v:v<0),
        'index_positive':('index_change',lambda v:v>0),'breadth_positive':('breadth',lambda v:v>50),
    }
    for k,(field,test) in tests.items():
        flags[k]=bool(test(r[field])) if finite(r.get(field)) else None
    flags['growth_board']=r['board'] in ['创业板','科创板']
    return flags


def distribution(values):
    v=np.array([x for x in values if finite(x)],dtype=float)
    if not len(v):
        return dict(n=0,mean=None,median=None,q25=None,q75=None)
    return dict(n=len(v),mean=float(v.mean()),median=float(np.median(v)),q25=float(np.percentile(v,25)),q75=float(np.percentile(v,75)))


def summarize(rows):
    n=len(rows)
    return dict(n=n,stocks=len({r['ts_code'] for r in rows}),dates=len({r['trade_date'] for r in rows}),
                first=min((r['trade_date'] for r in rows),default=None),last=max((r['trade_date'] for r in rows),default=None),
                peak=distribution([r['peak_pct'] for r in rows]),close5=distribution([r['close5_pct'] for r in rows]),
                worst_low=distribution([r['low_min_pct'] for r in rows]),
                close5_positive=sum(r['close5_pct']>EPS for r in rows),close5_negative=sum(r['close5_pct']<-EPS for r in rows),
                above10=sum(r['winner'] for r in rows),
                daily_high_mean=[float(np.mean([r['high_path'][i] for r in rows])) if n else None for i in range(5)],
                daily_high_median=[float(np.median([r['high_path'][i] for r in rows])) if n else None for i in range(5)],
                first_touch5=dict(Counter(str(r['first_touch5']) for r in rows if r['first_touch5'] is not None)),
                board_counts=dict(Counter(r['board'] for r in rows)))


def comparisons(rows):
    labels=EXTRA
    output=[]
    for group,group_label in GROUPS.items():
        for feature,label in labels.items():
            sample=[r for r in rows if r['entry_flags'][feature] is not None]
            target=[r for r in sample if r[group]]
            rest=[r for r in sample if not r[group]]
            a=sum(r['entry_flags'][feature] for r in target)
            b=len(target)-a
            c=sum(r['entry_flags'][feature] for r in rest)
            d=len(rest)-c
            coverage=a/len(target)*100 if target else None
            other=c/len(rest)*100 if rest else None
            output.append(dict(group=group,label=group_label,feature=feature,feature_label=label,
                               group_n=len(target),group_matched=a,group_coverage=coverage,
                               other_n=len(rest),other_matched=c,other_coverage=other,
                               feature_n=a+c,target_rate_when_feature=a/(a+c)*100 if a+c else None,
                               target_rate_without_feature=b/(b+d)*100 if b+d else None,
                               difference_pp=coverage-other if coverage is not None and other is not None else None,
                               fisher_p=macd.fisher_two_sided(a,b,c,d),
                               insufficient=min(len(target),len(rest),a+c,b+d)<20))
    q=macd.bh_adjust([r['fisher_p'] for r in output])
    for row,v in zip(output,q):
        row['fisher_q']=v
    return output


def controlled_comparisons(rows,draws=10000):
    """Permute only among eligible rows; share draws for identical missing masks."""
    labels=list(EXTRA)
    x=np.array([[np.nan if r['entry_flags'][k] is None else float(r['entry_flags'][k]) for k in labels] for r in rows])
    grouped=defaultdict(list)
    for i,r in enumerate(rows):
        grouped[(r['trade_date'][:6],r['snapshot_volume'],r['board'])].append(i)
    output=[]
    rng=np.random.default_rng(SEED)
    for group in GROUPS:
        y=np.array([float(r[group]) for r in rows])
        observed=np.zeros(len(labels));permuted=np.zeros((draws,len(labels)))
        informative_n=np.zeros(len(labels),dtype=int);informative_groups=np.zeros(len(labels),dtype=int)
        for ids in grouped.values():
            xx=x[ids]
            masks=defaultdict(list)
            for j in range(len(labels)):
                masks[np.isfinite(xx[:,j]).tobytes()].append(j)
            for columns in masks.values():
                available=np.isfinite(xx[:,columns[0]])
                yy=y[ids][available]
                if len(yy)<2 or yy.min()==yy.max():
                    continue
                xc=xx[available][:,columns]
                xc=xc-xc.mean(axis=0)
                info=(xc.max(axis=0)>0)&(xc.min(axis=0)<0)
                if not info.any():
                    continue
                informative_n[columns]+=len(yy)*info
                informative_groups[columns]+=info
                observed[columns]+=yy@xc
                shuffled=np.array([rng.permutation(yy) for _ in range(draws)])
                permuted[:,columns]+=shuffled@xc
        for j,k in enumerate(labels):
            p=(1+int((np.abs(permuted[:,j])>=abs(observed[j])-1e-12).sum()))/(draws+1) if informative_n[j] else None
            output.append(dict(group=group,feature=k,p=p,n=int(informative_n[j]),groups=int(informative_groups[j]),statistic=float(observed[j])))
    q=macd.bh_adjust([r['p'] if r['p'] is not None else 1 for r in output])
    for row,v in zip(output,q):
        row['q']=v
    return output


def continuous_comparisons(rows):
    return {g:{k:dict(label=label,group=distribution([r.get(k) for r in rows if r[g]]),
                     other=distribution([r.get(k) for r in rows if not r[g]]))
               for k,label in CONTINUOUS.items()} for g in GROUPS}


def splits(rows):
    result={}
    for dimension,key in dict(period=lambda r:'4–6月' if r['trade_date']<'20260701' else '7–9月' if r['trade_date']<'20261001' else '10月',
                              month=lambda r:r['trade_date'][:6],snapshot_volume=lambda r:str(r['snapshot_volume']),board=lambda r:r['board']).items():
        grouped=defaultdict(list)
        for r in rows:
            grouped[key(r)].append(r)
        result[dimension]={k:dict(all=summarize(v),groups={g:summarize([r for r in v if r[g]]) for g in GROUPS},
                                  continuous=continuous_comparisons(v),comparisons=comparisons(v)) for k,v in sorted(grouped.items())}
    return result


def load_dataset(db_path,asof=None):
    conn=macd.open_db(db_path)
    try:
        calendar=[r[0] for r in conn.execute('SELECT cal_date FROM trading_dates_cache ORDER BY cal_date')]
        pos={d:i for i,d in enumerate(calendar)}
        now=datetime.now(ZoneInfo('Asia/Shanghai'));today=now.strftime('%Y%m%d')
        complete=[d for d in calendar if d<today or (d==today and (now.hour,now.minute)>=(15,0))]
        latest=conn.execute('SELECT max(trade_date) FROM stock_daily').fetchone()[0]
        asof=asof or min(latest,complete[-1])
        snaps=[dict(r) for r in conn.execute('SELECT trade_date,count,results_json,threshold_yi,volume_ratio_pct FROM conditional_picks_preferred_snapshot ORDER BY trade_date')]
        picks=[];issues=[]
        for sn in snaps:
            items=json.loads(sn['results_json'] or '[]')
            if len(items)!=sn['count']:
                issues.append(dict(date=sn['trade_date'],count=sn['count'],actual=len(items)))
            seen=set()
            for item in items:
                code=item.get('ts_code')
                if code in seen:
                    issues.append(dict(date=sn['trade_date'],duplicate=code));continue
                seen.add(code)
                picks.append(dict(trade_date=sn['trade_date'],ts_code=code,name=item.get('name'),entry_close=item.get('close'),
                                  snapshot_volume=sn['volume_ratio_pct'],snapshot_threshold=sn['threshold_yi'],
                                  saved_strength5=item.get('preferred_strength5'),saved_volume_ratio=item.get('volume_ratio_pct'),
                                  saved_small_net=item.get('preferred_small_net'),saved_change=item.get('change_pct'),
                                  saved_ma10=item.get('ma10'),saved_mild_shrink=item.get('preferred_mild_shrink'),
                                  saved_below_ma60=item.get('preferred_below_ma60'),board=macd.board(code or '')))
        codes=sorted({r['ts_code'] for r in picks if r['ts_code']})
        daily={};flows={}
        for code in codes:
            daily[code]=[dict(r) for r in conn.execute('SELECT trade_date,open,high,low,close,change,volume,amount FROM stock_daily '
                                                     'WHERE ts_code=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date',(code,macd.START,asof))]
            flows[code]={r['trade_date']:dict(r) for r in conn.execute('SELECT trade_date,source,main_net_inflow,small_net FROM fund_flow '
                                                                     'WHERE ts_code=? AND trade_date<=? ORDER BY trade_date',(code,asof))}
        source_by_day={}
        for r in conn.execute("SELECT trade_date,source FROM fund_flow WHERE source IN ('eastmoney-push2','eastmoney-daykline') "
                              'AND main_net_inflow IS NOT NULL AND main_net_inflow!=0 GROUP BY trade_date,source'):
            if r['trade_date'] not in source_by_day or r['source']=='eastmoney-push2':
                source_by_day[r['trade_date']]=r['source']
        overview={r['date'].replace('-',''):dict(r) for r in conn.execute('SELECT date,upCount,downCount,flatCount FROM overview')}
        index={r['date'].replace('-',''):dict(r) for r in conn.execute("SELECT date,change FROM index_daily WHERE name='上证指数'")}
    finally:
        conn.rollback();conn.close()
    maps={code:{r['trade_date']:r for r in hist} for code,hist in daily.items()}
    ledger=[]
    for pick in picks:
        r=dict(pick);day=r['trade_date'];code=r['ts_code'];bars=maps.get(code,{})
        r.update(macd.outcome(bars,day,calendar,pos,r['entry_close'],asof))
        r.update(past_features(bars,flows.get(code,{}),day,calendar,pos,source_by_day.get(day)))
        r['flow_source']=source_by_day.get(day)
        ov=overview.get(day)
        total=sum(ov[k] for k in ['upCount','downCount','flatCount']) if ov and all(finite(ov[k]) for k in ['upCount','downCount','flatCount']) else 0
        r['breadth']=ov['upCount']/total*100 if total else None
        r['down_count']=ov.get('downCount') if ov else None
        r['index_change']=index.get(day,{}).get('change')
        r['entry_flags']=make_flags(r)
        if not r['outcome_reason']:
            r.update(classify(r['high_path']))
            days=calendar[pos[day]+1:pos[day]+6]
            r['low_min_pct']=(min(bars[d]['low'] for d in days)/r['entry_close']-1)*100
            r['close_path']=[(bars[d]['close']/r['entry_close']-1)*100 for d in days]
            r['first_touch5']=next((i+1 for i,v in enumerate(r['high_path']) if v>5+EPS),None)
        ledger.append(r)
    audit=dict(asof=asof,snapshot_dates=len(snaps),saved_events=len(picks),saved_stocks=len(codes),snapshot_issues=issues,
               excluded=dict(Counter(r['outcome_reason'] for r in ledger if r['outcome_reason'])),
               price_complete=sum(not r['outcome_reason'] for r in ledger),
               amplitude20_valid=sum(not r['outcome_reason'] and finite(r.get('amp20')) for r in ledger),
               flow5_valid=sum(not r['outcome_reason'] and finite(r.get('strength5')) for r in ledger),
               strength5_discrepancies=[dict(date=r['trade_date'],code=r['ts_code'],saved=r['saved_strength5'],recomputed=r['strength5'])
                                      for r in ledger if finite(r.get('strength5')) and finite(r.get('saved_strength5')) and abs(r['strength5']-r['saved_strength5'])>.05])
    return ledger,pos,audit


def risk_flags(row):
    out={'main_board':row['board']=='主板'}
    definitions={
        'low_amplitude':('amp20',lambda v:v<5), 'weak_ret20':('ret20',lambda v:v<15),
        'below_ma20':('bias20',lambda v:v<=0), 'volume_gt80':('volume_ratio',lambda v:v>80),
        'volume_ge100':('volume_ratio',lambda v:v>=100), 'strong_flow5':('strength5',lambda v:v>=10),
        'main_outflow':('strength1',lambda v:v<0), 'index_up':('index_change',lambda v:v>0),
        'breadth_up':('breadth',lambda v:v>50), 'mild_dip':('change0',lambda v:-1<v<0),
        'ret5_ge10':('ret5',lambda v:v>=10), 'ret5_ge15':('ret5',lambda v:v>=15),
        'weak_close':('close_position',lambda v:v<=30),
    }
    for k,(field,test) in definitions.items():
        out[k]=bool(test(row[field])) if finite(row.get(field)) else None
    return out


def exclude_candidate(row,keys):
    values=risk_flags(row)
    return bool(keys) and all(values[k] is True for k in keys)


def filter_metrics(rows,keys):
    dropped=[r for r in rows if exclude_candidate(r,keys)]
    kept=[r for r in rows if not exclude_candidate(r,keys)]
    n=len(rows);bad=sum(r['all_negative'] for r in rows)
    a=sum(r['all_positive'] for r in rows);c=sum(r['above5'] for r in rows)
    bad_dropped=sum(r['all_negative'] for r in dropped)
    a_dropped=sum(r['all_positive'] for r in dropped);c_dropped=sum(r['above5'] for r in dropped)
    return dict(n=n,removed=len(dropped),kept=len(kept),negative_original=bad,negative_removed=bad_dropped,
                negative_kept=bad-bad_dropped,negative_avoidance=bad_dropped/bad*100 if bad else None,
                negative_rate_before=bad/n*100 if n else None,negative_rate_after=(bad-bad_dropped)/len(kept)*100 if kept else None,
                all_positive_original=a,all_positive_removed=a_dropped,all_positive_retained=(a-a_dropped)/a*100 if a else None,
                above5_original=c,above5_removed=c_dropped,above5_retained=(c-c_dropped)/c*100 if c else None,
                removed_other=len(dropped)-bad_dropped,close5_kept=distribution([r['close5_pct'] for r in kept]),
                missing_any=sum(any(risk_flags(r)[k] is None for k in keys) for r in rows))


def candidate_grid():
    keys=list(RISK_LABELS)
    return [(k,) for k in keys]+list(combinations(keys,2))


def choose_filter(development,minimum_retained=.8):
    eligible=[]
    for keys in candidate_grid():
        m=filter_metrics(development,keys)
        if (m['removed']>=10 and m['negative_removed']>=2
            and m['all_positive_retained'] is not None and m['all_positive_retained']>=minimum_retained*100
            and m['above5_retained'] is not None and m['above5_retained']>=minimum_retained*100):
            eligible.append((keys,m))
    eligible.sort(key=lambda x:(-x[1]['negative_removed'],
                                200-x[1]['all_positive_retained']-x[1]['above5_retained'],
                                x[1]['removed'],len(x[0]),x[0]))
    return eligible[0][0] if eligible else None


def research_risk_filters(rows,unique):
    development=[r for r in rows if r['trade_date']<'20260701' and r['end_date']<='20260630']
    later=[r for r in rows if '20260701'<=r['trade_date']<'20261001' and r['end_date']<='20260930']
    results={}
    for role,fraction in [('保守',.9),('平衡',.8),('严格',.7)]:
        keys=choose_filter(development,fraction)
        if keys is None:
            results[role]=None;continue
        results[role]=dict(keys=keys,label=' 且 '.join(RISK_LABELS[k] for k in keys),
                           minimum_development_retained=fraction,
                           development=filter_metrics(development,keys),later=filter_metrics(later,keys),
                           all=filter_metrics(rows,keys),nonoverlap=filter_metrics(unique,keys),
                           months={month:filter_metrics([r for r in rows if r['trade_date'].startswith(month)],keys)
                                   for month in sorted({r['trade_date'][:6] for r in rows})})
    candidates=[]
    for keys in candidate_grid():
        candidates.append(dict(keys=keys,label=' 且 '.join(RISK_LABELS[k] for k in keys),
                               development=filter_metrics(development,keys),later=filter_metrics(later,keys)))
    return dict(candidate_count=len(candidates),selection_period='202604–202606',review_period='202607–202609',
                independent_oos=False,development_n=len(development),later_n=len(later),
                purged_cross_boundary=len(rows)-len(development)-len(later),selected=results,candidates=candidates,
                fixed_references={k:dict(label=RISK_LABELS[k],all=filter_metrics(rows,(k,)),
                                         development=filter_metrics(development,(k,)),later=filter_metrics(later,(k,)))
                                  for k in ['low_amplitude','main_board','strong_flow5','index_up']})


def next_close_confirmed(row):
    """Known only after T+1 close; never presented as a signal-day feature."""
    return bool(row.get('close_path')) and finite(row['close_path'][0]) and row['close_path'][0]>=-EPS


def confirmation_metrics(rows):
    kept=[r for r in rows if next_close_confirmed(r)]
    out=dict(n=len(rows),confirmed=len(kept),not_confirmed=len(rows)-len(kept),
             confirmation_time='T+1收盘后',earliest_entry='T+2，实际价格及可成交性尚未回测',
             negative_original=sum(r['all_negative'] for r in rows),negative_kept=sum(r['all_negative'] for r in kept),
             all_positive_original=sum(r['all_positive'] for r in rows),all_positive_kept=sum(r['all_positive'] for r in kept),
             above5_original=sum(r['above5'] for r in rows),above5_kept=sum(r['above5'] for r in kept),
             close5_below_signal_price=sum(r['close5_pct']<-EPS for r in kept))
    return out


def analyze(ledger,pos,audit):
    rows=[r for r in ledger if not r['outcome_reason']]
    unique=macd.nonoverlap(rows,pos)
    result=dict(audit=audit,all=summarize(rows),groups={g:summarize([r for r in rows if r[g]]) for g in GROUPS},
                 exclusive={k:summarize([r for r in rows if r['exclusive']==k]) for k in ['A且C','A非C','B','其他且C','其他非C']},
                 continuous=continuous_comparisons(rows),comparisons=comparisons(rows),controlled=controlled_comparisons(rows),
                 controlled_draws=10000,
                 splits=splits(rows),nonoverlap=dict(all=summarize(unique),groups={g:summarize([r for r in unique if r[g]]) for g in GROUPS},
                                                    continuous=continuous_comparisons(unique),comparisons=comparisons(unique)),
                 risk_filters=research_risk_filters(rows,unique),
                 delayed_confirmation=dict(all=confirmation_metrics(rows),nonoverlap=confirmation_metrics(unique),
                                            periods={period:confirmation_metrics([r for r in rows if ('4–6月' if r['trade_date']<'20260701' else '7–9月')==period])
                                                     for period in ['4–6月','7–9月']}))
    return result


def export(result,ledger,prefix):
    Path(str(prefix)+'-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    columns=['trade_date','ts_code','name','board','entry_close','snapshot_threshold','snapshot_volume','outcome_reason',
             'all_positive','all_negative','above5','exclusive','peak_pct','close5_pct','low_min_pct','first_touch5','peak_day','end_date',
             'saved_strength5','saved_volume_ratio','flow_source']+list(CONTINUOUS)+[f'high_t{i}' for i in range(1,6)]+[f'close_t{i}' for i in range(1,6)]
    columns=list(dict.fromkeys(columns))
    with Path(str(prefix)+'-events.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');writer.writeheader()
        for r in ledger:
            item=dict(r)
            item.update({f'high_t{i+1}':v for i,v in enumerate(r.get('high_path',[]))})
            item.update({f'close_t{i+1}':v for i,v in enumerate(r.get('close_path',[]))})
            writer.writerow(item)
    with Path(str(prefix)+'-comparisons.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(result['comparisons'][0]));writer.writeheader();writer.writerows(result['comparisons'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',type=Path,default=ROOT/'backend/market_data.db')
    parser.add_argument('--asof');parser.add_argument('--output-prefix',type=Path,default=PREFIX)
    parser.add_argument('--dataset-cache',type=Path,help='Use an explicit frozen local JSON dataset; create it once if absent.')
    args=parser.parse_args()
    if args.dataset_cache and args.dataset_cache.exists():
        frozen=json.loads(args.dataset_cache.read_text())
        ledger,pos,audit=frozen['ledger'],frozen['calendar_positions'],frozen['audit']
    else:
        ledger,pos,audit=load_dataset(args.db,args.asof)
        audit['frozen_at']=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        if args.dataset_cache:
            args.dataset_cache.write_text(json.dumps(dict(ledger=ledger,calendar_positions=pos,audit=audit),ensure_ascii=False,allow_nan=False)+'\n')
    result=analyze(ledger,pos,audit);export(result,ledger,args.output_prefix)
    print(json.dumps(dict(audit=result['audit'],groups=result['groups'],exclusive={k:v['n'] for k,v in result['exclusive'].items()}),ensure_ascii=False))


if __name__=='__main__':
    main()
