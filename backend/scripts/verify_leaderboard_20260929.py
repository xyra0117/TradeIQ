# Read-only research; outputs are dated files in docs/. Run analysis, then verification.
import sqlite3,json
import numpy as np,pandas as pd
from pathlib import Path
p=Path(__file__).resolve().parents[2]/'docs';r=json.loads((p/'leaderboard-analysis-20260929.json').read_text());e=pd.read_csv(p/'leaderboard-events-20260929.csv',dtype={'date':str,'code':str});t=pd.read_csv(p/'leaderboard-entry-tests-20260929.csv',dtype={'date':str,'entry':str,'exit':str,'signal':str,'code':str});l=pd.read_csv(p/'leaderboard-all-stocks-20260929.csv',dtype={'code':str})
c=sqlite3.connect('file:'+str(p.parent/'backend/market_data.db')+'?mode=ro',uri=True);c.execute('pragma query_only=on');c.execute('BEGIN');q=lambda s:pd.read_sql_query(s,c)
ix=q("select date,open,close from index_daily where name='上证指数'").set_index('date')
t['benchmark']=[(ix.loc[x,'close']/ix.loc[d,'open']-1)*100 if d in ix.index and x in ix.index and ix.loc[d,'open']>0 else np.nan for d,x in zip(t.entry,t.exit)]
t['excess']=t.net-t.benchmark
rng=np.random.default_rng(42)
def ci(g,col='net'):
 z=g.assign(month=g.entry.str[:6]).groupby('month')[col].agg(['sum','count']);s=z['sum'].to_numpy();n=z['count'].to_numpy();k=rng.integers(0,len(z),(3000,len(z)));v=s[k].sum(axis=1)/n[k].sum(axis=1);return np.percentile(v,[2.5,97.5]).tolist()
rob=[]
for st,g in t[t.hold==10].groupby('strategy'):
 sg=g.sort_values('net');cut=int(len(sg)*.05);rob.append({'strategy':st,'n':len(g),'CI_month_block':ci(g),'mean_without_top5pct':sg.iloc[:len(sg)-cut].net.mean(),'trimmed5pct_mean':sg.iloc[cut:len(sg)-cut].net.mean(),'mean_benchmark':g.benchmark.mean(),'mean_excess':g.excess.mean(),'loss20pct':(g.net<=-20).mean()*100})
# Paired rule comparison on same original episode.
dir=t[(t.strategy=='上榜后次日')&(t.hold==10)][['date','code','net']].rename(columns={'net':'direct_net'})
paired=[]
for st,g in t[t.hold==10].groupby('strategy'):
 z=g.merge(dir,on=['date','code']);z['difference']=z.net-z.direct_net;paired.append({'strategy':st,'n':len(z),'direct_mean':z.direct_net.mean(),'wait_mean':z.net.mean(),'difference':z.difference.mean(),'CI':ci(z,'difference')})
# Independent direct SQL checks and sell-limit exposure.
checks=[]
for z in t.sample(30,random_state=4).itertuples():
 en=c.execute('select open from stock_daily where ts_code=? and trade_date=?',(z.code,z.entry)).fetchone()[0]
 ex=c.execute('select close from stock_daily where ts_code=? and trade_date=?',(z.code,z.exit)).fetchone()[0]
 assert abs((ex/en-1)*100-.3-z.net)<1e-8
 assert z.signal<z.entry<z.exit
checks.append('30 random trade returns independently recomputed from SQL; entry strictly after signal')
assert len(l)==1645 and l.code.nunique()==1645
assert not t.duplicated(['strategy','hold','code','date']).any()
checks.append('Full stock ledger unique; no duplicate strategy/holding/episode records')
locked=[]
for z in t[t.hold==10].itertuples():
 x=c.execute('select high,low,change from stock_daily where ts_code=? and trade_date=?',(z.code,z.exit)).fetchone();lim=30 if z.code.endswith('.BJ') else 20 if z.code.startswith(('300','301','688','689')) else 10
 if abs(x[0]-x[1])<1e-6 and x[2]<=-lim+.3:locked.append({'strategy':z.strategy,'date':z.date,'code':z.code,'exit':z.exit,'net':z.net})
# board-level stratification
base=t[(t.strategy=='上榜后次日')&(t.hold==10)].copy();base['board']=np.select([base.code.str.endswith('.BJ'),base.code.str.startswith(('300','301')),base.code.str.startswith(('688','689'))],['北交所','创业板','科创板'],default='主板')
board=base.groupby('board').net.agg(['count','mean','median']).reset_index().to_dict('records')
# industry memberships descriptive only
members=q("select m.ts_code,i.industry_name from sw_industry_member m join sw_industry i on i.index_code=m.index_code where i.level='L1' and (m.out_date is null or m.out_date='')"); imap=members.groupby('ts_code').industry_name.agg(lambda x:' / '.join(sorted(set(x))));l['industry_current']=l.code.map(imap)
# Preserve raw max, add verified max so sparse ranking records not mistaken for verified results.
lb=q('select date,code,type,change from leaderboard');keys=e[['date','code','types']].copy();v=lb.merge(keys,on=['date','code']);v=v[v.apply(lambda x:x['type'] in x['types'].split(','),axis=1)]
vm=v.groupby('code').change.max();l['max_verified_stage_gain']=l.code.map(vm)
# Dates and context at episode first appearance, never borrow later news for historical attribution.
lu=q('select date,code,sector,keyword from limitup');cal=q("select cal_date from trading_dates_cache where cal_date between '20250401' and '20260928' order by cal_date").cal_date.tolist();di={x:i for i,x in enumerate(cal)}
context=[]
for z in e[e.episode].itertuples():
 lo=cal[max(0,di[z.date]-20)];sub=lu[(lu.code==z.code)&(lu.date>=lo)&(lu.date<=z.date)].sort_values('date')
 if len(sub):
  a=sub.iloc[-1];context.append({'code':z.code,'date':z.date,'sector':a.sector,'keyword':a.keyword})
ctx=pd.DataFrame(context);cm=ctx.groupby('code').apply(lambda x:'；'.join(x.apply(lambda r:r.date+' '+str(r.sector)+'：'+str(r.keyword),axis=1)),include_groups=False)
l['episode_historical_context']=l.code.map(cm)
l.to_csv(p/'leaderboard-all-stocks-20260929.csv',index=False,encoding='utf-8-sig');t.to_csv(p/'leaderboard-entry-tests-20260929.csv',index=False,encoding='utf-8-sig')
# Public market data short-span usability: actual date coverage and minute completeness, no trading rule optimized on 9 dates.
short={}
for table in ['stock_minline_bars','index_minline_bars']:
 short[table]=q('select trade_date,min(m) first_m,max(m) last_m,count(distinct ts_code) stocks from '+table+' group by trade_date').to_dict('records')
short['sector_fund_flow']=q('select trade_date,sector_type,count(*) rows from sector_fund_flow group by trade_date,sector_type').to_dict('records')
short['market_fflow']=q('select complete,count(*) rows,min(trade_date) first,max(trade_date) last from market_fflow group by complete').to_dict('records')
r.update(robustness=rob,paired=paired,board=board,sell_oneprice_limitdown=locked,checks=checks,short_coverage=short,industry_available_stocks=int(l.industry_current.notna().sum()))
(p/'leaderboard-analysis-20260929.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps({k:r[k] for k in ['robustness','paired','board','sell_oneprice_limitdown','checks','industry_available_stocks']},ensure_ascii=False,indent=2))
c.rollback();c.close()

# Supplement: readable per-stock aggregates and execution sensitivity, still read-only.
c=sqlite3.connect('file:'+str(p.parent/'backend/market_data.db')+'?mode=ro',uri=True)
c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
ep=e[e.episode]
a=ep.groupby('code').agg(median_entry_vr=('vr','median'),median_entry_bias5=('bias5','median'),median_entry_bias20=('bias20','median'),median_main5pct=('main5pct','median'),flow_samples=('main5pct','count'))
l=l.drop(columns=[k for k in a.columns if k in l.columns]);l=l.merge(a,on='code',how='left')
b=t[(t.strategy=='上榜后次日')&(t.hold==10)].groupby('code').agg(direct10_samples=('net','count'),direct10_mean=('net','mean'),direct10_median=('net','median'),direct10_win_pct=('net',lambda x:(x>0).mean()*100),direct10_worst=('net','min'))
l=l.drop(columns=[k for k in b.columns if k in l.columns]);l=l.merge(b,on='code',how='left');l['buy_point_evidence']='独立事件太少，不能确认个股最优买点';l.loc[l.direct10_samples.isna(),'buy_point_evidence']='缺少有效买入后完整区间，不能评估'
l.to_csv(p/'leaderboard-all-stocks-20260929.csv',index=False,encoding='utf-8-sig')
price=pd.read_sql_query("select ts_code,trade_date,open,high,low,close,change from stock_daily where trade_date>='20250601'",c).set_index(['ts_code','trade_date'])
t['exit_oneprice_down']=False;t['deferred_exit']=t['exit'];t['deferred_net']=t['net']
for k,z in t.iterrows():
 x=price.loc[(z.code,z.exit)];lim=30 if z.code.endswith('.BJ') else 20 if z.code.startswith(('300','301','688','689')) else 10
 if abs(x.high-x.low)<1e-6 and x.change<=-lim+.3:
  t.at[k,'exit_oneprice_down']=True;t.at[k,'deferred_net']=np.nan;t.at[k,'deferred_exit']=''
  en=price.loc[(z.code,z.entry),'open'];old=x.close
  for d in cal[di[z.exit]+1:di[z.exit]+11]:
   if (z.code,d) not in price.index:break
   nx=price.loc[(z.code,d)]
   if abs(nx.close/(1+nx.change/100)/old-1)>.005:break
   old=nx.close
   if not (abs(nx.high-nx.low)<1e-6 and nx.change<=-lim+.3):
    t.at[k,'deferred_exit']=d;t.at[k,'deferred_net']=(nx.close/en-1)*100-.3;break
# Ensure original planned positions do not overlap by code for any fixed rule/horizon.
for _,g in t.groupby(['strategy','hold','code']):
 z=g.sort_values('entry');assert all(a>b for a,b in zip(z.entry.iloc[1:],z.exit.iloc[:-1]))
sensitivity=[]
for st,g in t[t.hold==10].groupby('strategy'):
 sensitivity.append({'strategy':st,'locked_exits':int(g.exit_oneprice_down.sum()),'deferred_mean':g.deferred_net.mean(),'unresolved':int(g.deferred_net.isna().sum())})
r['exit_sensitivity_10']=sensitivity
r['checks'].append('No overlapping planned holdings for a fixed stock/rule/horizon; one-price-down exit sensitivity added')
(p/'leaderboard-analysis-20260929.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf8')
t.to_csv(p/'leaderboard-entry-tests-20260929.csv',index=False,encoding='utf-8-sig')
print('EXIT_SENSITIVITY',json.dumps(sensitivity,ensure_ascii=False));c.rollback();c.close()

from pathlib import Path
import pandas as pd,json
p=Path(__file__).resolve().parents[2]/'docs'
l=pd.read_csv(p/'leaderboard-all-stocks-20260929.csv',dtype={'code':str})
fields={'code':'股票代码','name':'股票名称','first':'首次上榜日期','last':'最后上榜日期','appearances':'上榜天数','max_verified_stage_gain':'核验后最大阶段涨幅%','valid_days':'可核验上榜天数','episodes':'独立入榜事件数','direct10_samples':'次日买入持有10日有效笔数','direct10_mean':'10日平均净收益%','direct10_median':'10日收益中位数%','direct10_win_pct':'10日胜率%','direct10_worst':'10日最差单笔收益%','median_entry_vr':'入榜量比中位数','median_entry_bias5':'入榜高于5日均线中位数%','median_entry_bias20':'入榜高于20日均线中位数%','median_main5pct':'入榜前5日主力净额占成交额中位数%','flow_samples':'有效资金样本数','industry_current':'当前申万行业仅供描述','episode_historical_context':'各次入榜前已存在的题材记录','analysis_status':'数据覆盖结论','buy_point_evidence':'买点证据结论'}
l[list(fields)].rename(columns=fields).round(3).to_csv(p/'leaderboard-stock-review-20260929.csv',index=False,encoding='utf-8-sig')
assert len(l)==1645
print('Chinese ledger exported:',len(l))
