# Read-only research; outputs are dated files in docs/. Run analysis, then verification.
import sqlite3,json,collections
from pathlib import Path
import numpy as np
import pandas as pd
DB=str(Path(__file__).resolve().parents[1]/'market_data.db')
OUT=Path(__file__).resolve().parents[2]/'docs'
c=sqlite3.connect('file:'+DB+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
q=lambda sql:pd.read_sql_query(sql,c)
lb=q('select * from leaderboard'); basic=q('select * from stock_basic').set_index('ts_code')
cal=q("select cal_date from trading_dates_cache where cal_date between '20250401' and '20260928' order by cal_date").cal_date.tolist()
daily=q("select ts_code,trade_date,open,high,low,close,change,volume,amount from stock_daily where trade_date>='20250401'")
codes=sorted(daily.ts_code.unique()); ds={d:i for i,d in enumerate(cal)};cs={s:i for i,s in enumerate(codes)}
a={k:daily.pivot(index='trade_date',columns='ts_code',values=k).reindex(index=cal,columns=codes) for k in ['open','high','low','close','change','volume','amount']}
cl=a['close']; prev=cl.shift(); mismatch=((cl/(1+a['change']/100)/prev-1).abs()>.005)
valid=cl.notna() & ~mismatch
ma={n:cl.rolling(n).mean().where(valid.rolling(n).sum()==n) for n in [5,10,20,60]}
feat={'ret5':(cl/cl.shift(5)-1)*100,'ret20':(cl/cl.shift(20)-1)*100,'bias5':(cl/ma[5]-1)*100,'bias20':(cl/ma[20]-1)*100,'vr':a['volume']/a['volume'].shift().rolling(5).mean(),'dayret':a['change'],'amount_yi':a['amount']/100000,'ma5above20':(ma[5]>ma[20]).astype(float).where(ma[20].notna()),'above60':(cl>ma[60]).astype(float).where(ma[60].notna())}
flow=q("select * from fund_flow where source='eastmoney-push2'")
for key,col in [('main','main_net_inflow'),('small','small_net'),('super','super_net')]:
 f=flow.pivot(index='trade_date',columns='ts_code',values=col).reindex(index=cal,columns=codes)
 feat[key]=f;feat[key+'5pct']=f.rolling(5).sum()/(a['amount'].rolling(5).sum()*1000)*100
ths=q('select trade_date,ts_code,net,turnover_pct from fund_flow_ths')
for key in ['net','turnover_pct']:feat['ths_'+key]=ths.pivot(index='trade_date',columns='ts_code',values=key).reindex(index=cal,columns=codes)
ov=q('select * from overview').set_index('date');idx=q("select * from index_daily where name='上证指数'").set_index('date')
lu=q('select * from limitup');lu['key']=list(zip(lu.date,lu.code));lu_map=lu.groupby('key').agg(sector=('sector',lambda x:' / '.join(sorted(set(x.dropna())))),keyword=('keyword',lambda x:' / '.join(sorted(set(x.dropna()))))).to_dict('index')
# Only dates with continuous actual market days for each stated ranking horizon.
verified=[];bad=[]
for r in lb.itertuples():
 i=ds.get(r.date);j=cs.get(r.code);n=int(r.type.replace('日',''))
 if i is None or j is None or i<n:bad.append((r.id,'history'));continue
 path=cl.iloc[i-n:i+1,j]
 if path.isna().any() or mismatch.iloc[i-n+1:i+1,j].any():bad.append((r.id,'gap_or_adjustment'));continue
 change=(path.iloc[-1]/path.iloc[0]-1)*100
 if abs(change-r.change)>.08:bad.append((r.id,'rank_return_mismatch'));continue
 verified.append(r.id)
lv=lb[lb.id.isin(verified)]
allpairs=lb.sort_values('date').drop_duplicates(['date','code']);vp=lv.sort_values('date').groupby(['date','code']).agg(types=('type',lambda x:','.join(sorted(x))),rank=('rank','min'),name=('name','last')).reset_index()
# new episodes: no stored appearance for 20 local exchange sessions, 20-day washout at start.
last={};eventkeys=set()
for r in allpairs.itertuples():
 i=ds.get(r.date)
 if i is not None:
  if i>=40 and (r.code not in last or i-last[r.code]>20):eventkeys.add((r.date,r.code))
  last[r.code]=i
rows=[]
for r in vp.itertuples():
 i,j=ds[r.date],cs[r.code]
 z={'date':r.date,'code':r.code,'name':r.name,'types':r.types,'rank':r.rank,'episode':(r.date,r.code) in eventkeys}
 z.update({k:float(v.iloc[i,j]) for k,v in feat.items()});z['industry']=basic.loc[r.code,'industry_name'] if r.code in basic.index else None
 z['breadth']=ov.loc[r.date,'upCount']/(ov.loc[r.date,'upCount']+ov.loc[r.date,'downCount']) if r.date in ov.index else np.nan
 z.update(lu_map.get((r.date,r.code),{'sector':'','keyword':''}));rows.append(z)
events=pd.DataFrame(rows); ep=events[events.episode].copy()
# Returns: next open, 5/10/20-session holding. Exclude discontinuity/corporate actions and limit-up opens.
arr={k:v.to_numpy() for k,v in a.items()};vf=valid.to_numpy();mm=mismatch.to_numpy();M={k:v.to_numpy() for k,v in ma.items()};F={k:v.to_numpy() for k,v in feat.items()}
def outcome(i,j,h):
 e=i+1;x=e+h-1
 if x>=len(cal):return None,'end'
 if not vf[e:x+1,j].all() or not np.isfinite(arr['open'][e,j]) or not np.isfinite(arr['close'][i,j]):return None,'gap_adjustment'
 limit=30 if codes[j].endswith('.BJ') else 20 if codes[j].startswith(('300','301','688','689')) else 10
 # current ST names used only to conservatively restrict, historical name unavailable
 if codes[j] in basic.index and ('ST' in str(basic.loc[codes[j],'name']).upper()):limit=5
 gap=(arr['open'][e,j]/arr['close'][i,j]-1)*100
 if gap>=limit-.3:return None,'limit_open'
 p=arr['open'][e,j]
 return {'entry':cal[e],'exit':cal[x],'gross':(arr['close'][x,j]/p-1)*100,'net':(arr['close'][x,j]/p-1)*100-.3,'adverse':min(0,(np.nanmin(arr['low'][e:x+1,j])/p-1)*100),'favorable':(np.nanmax(arr['high'][e:x+1,j])/p-1)*100,'gap':gap},'ok'
for h in [5,10,20]:
 vals=[]
 for r in events.itertuples():
  o,_=outcome(ds[r.date],cs[r.code],h);vals.append(o['net'] if o else np.nan)
 events['next_'+str(h)]=vals
ep=events[events.episode].copy()
# Same-day eligible universe comparison (non-ST current names, >=20 prior valid sessions).
control=[]
allowed=np.array([s in basic.index and not any(v in str(basic.loc[s,'name']).upper() for v in ['ST','退']) for s in codes])
for date,g in events.groupby('date'):
 i=ds[date];mask=allowed & (valid.iloc[max(0,i-20):i+1].sum().to_numpy()==21);mask[[cs[s] for s in g.code]]=False
 for k in ['vr','bias5','bias20','ma5above20','above60','amount_yi','main','main5pct','small','ths_turnover_pct']:
  v=F[k][i,mask];v=v[np.isfinite(v)]
  control.append({'date':date,'feature':k,'median':np.median(v) if len(v) else np.nan,'positive':np.mean(v>0) if len(v) else np.nan,'count':len(v)})
controls=pd.DataFrame(control)
# Candidate entry rules fixed in advance, triggered on closes only, entry next open.
strategies=['上榜后次日','次日开盘涨幅不超3%','十日内缩量回踩MA5','十日内缩量回踩MA10','十日内缩量企稳MA10','十日内突破前20日高点']
trades=[];skip=collections.Counter();last_exit={}
for r in ep.sort_values(['date','code']).itertuples():
 i=ds[r.date];j=cs[r.code]
 for st in strategies:
  sig=None
  if st in strategies[:2]:sig=i
  else:
   for t in range(i+1,min(i+11,len(cal)-1)):
    if not vf[i+1:t+1,j].all():break
    px=arr['close'][t,j];vol=arr['volume'][t,j];vprev=arr['volume'][t-1,j]
    if st==strategies[2]:ok=arr['low'][t,j]<=M[5][t,j]*1.02 and px>=M[5][t,j] and vol<vprev and arr['change'][t,j]<=3
    elif st==strategies[3]:ok=arr['low'][t,j]<=M[10][t,j]*1.02 and px>=M[10][t,j] and vol<vprev and arr['change'][t,j]<=3
    elif st==strategies[4]:ok=px>=M[10][t,j] and arr['low'][t,j]<=M[10][t,j]*1.03 and arr['change'][t,j]>0 and vol<np.nanmean(arr['volume'][t-5:t,j])
    else:ok=t>=20 and np.isfinite(arr['high'][t-20:t,j]).all() and px>np.max(arr['high'][t-20:t,j]) and vol>np.mean(arr['volume'][t-5:t,j])
    if ok:sig=t;break
  if sig is None:skip[(st,'no_signal')]+=1;continue
  if st==strategies[1] and sig+1<len(cal) and (arr['open'][sig+1,j]/arr['close'][sig,j]-1)*100>3:skip[(st,'gap_gt3')]+=1;continue
  for h in [5,10,20]:
   o,status=outcome(sig,j,h)
   if o is None:skip[(st+'_'+str(h),status)]+=1;continue
   if o['entry']<=last_exit.get((r.code,st,h),''):continue
   last_exit[(r.code,st,h)]=o['exit']
   b=dict(r._asdict());b.pop('Index',None);b.update(o);b.update(strategy=st,hold=h,signal=cal[sig]);trades.append(b)
tr=pd.DataFrame(trades)
def stat(v):
 v=pd.Series(v).dropna();return {'n':len(v),'mean':v.mean(),'median':v.median(),'win':(v>0).mean()*100,'p10':v.quantile(.1)}
summary=[]
for (st,h),g in tr.groupby(['strategy','hold']):
 z={'strategy':st,'hold':int(h),**stat(g.net),'adverse_median':g.adverse.median(),'adverse_p10':g.adverse.quantile(.1)}
 for period,sub in [('early',g[g.entry<'20260401']),('late',g[g.entry>='20260401'])]:
  z.update({period+'_'+k:v for k,v in stat(sub.net).items()})
 summary.append(z)
# Cross-sectional exploratory factor tests on independent appearances, no post-entry filtering.
factors={'量比1至2':(ep.vr>=1)&(ep.vr<=2),'量比大于3':ep.vr>3,'MA5乖离不超5%':ep.bias5.between(0,5),'MA5乖离大于15%':ep.bias5>15,'当日主力净流入':ep.main>0,'当日主力净流出':ep.main<0,'五日主力净流入':ep.main5pct>0,'五日主力净流出':ep.main5pct<0,'小单净流出':ep.small<0,'小单净流入':ep.small>0,'上涨家数占比超60%':ep.breadth>.6,'上涨家数占比低于40%':ep.breadth<.4,'5日榜出现':ep.types.str.contains('5日'),'仅10或20日榜':~ep.types.str.contains('5日')}
factorstats=[{'factor':k,**stat(ep.loc[m,'next_10'])} for k,m in factors.items()]
# Historical sector attribution only: tags on/before first appearance, within 20 exchange sessions.
sectorrows=[]
for r in ep.itertuples():
 i=ds[r.date];sub=lu[(lu.code==r.code)&(lu.date<=r.date)&(lu.date>=cal[max(0,i-20)])]
 if len(sub):
  lastrow=sub.sort_values('date').iloc[-1];sectorrows.append({'code':r.code,'date':r.date,'sector':lastrow.sector,'keyword':lastrow.keyword})
sectors=pd.DataFrame(sectorrows)
# Full 1645-stock ledger, including sparse history without forced inference.
ledger=lb.groupby('code').agg(name=('name','last'),first=('date','min'),last=('date','max'),appearances=('date','nunique'),records=('id','count'),max_stage_gain=('change','max')).reset_index()
for typ in ['5日','10日','20日']:ledger=ledger.merge(lb[lb.type==typ].groupby('code')['change'].max().rename('max_'+typ),on='code',how='left')
ledger=ledger.merge(events.groupby('code').agg(valid_days=('date','count'),avg_next10=('next_10','mean'),sample_next10=('next_10','count')),on='code',how='left')
ledger=ledger.merge(ep.groupby('code').agg(episodes=('date','count'),episode_avg_next10=('next_10','mean'),episode_sample_next10=('next_10','count')),on='code',how='left')
ledger['industry_current']=ledger.code.map(basic.industry_name)
recent_tags=lu.sort_values('date').groupby('code').tail(1).set_index('code');ledger['last_historical_sector']=ledger.code.map(recent_tags.sector);ledger['last_historical_keyword']=ledger.code.map(recent_tags.keyword)
ledger['analysis_status']=np.where(ledger.valid_days.notna(),'有连续区间可检验','仅稀疏历史或无法核验')
# metadata catalog excluding private portfolio and credentials contents
catalog=[]
for name, in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite%'").fetchall():
 cols=[r[1] for r in c.execute('pragma table_info('+name+')')];d=next((x for x in ['trade_date','date','cal_date'] if x in cols),None)
 query='select count(*)'+(',min('+d+'),max('+d+'),count(distinct '+d+')' if d else '')+' from '+name
 catalog.append({'table':name,'coverage':c.execute(query).fetchone()})
result={'universe':{'records':len(lb),'stocks':lb.code.nunique(),'dates':lb.date.nunique(),'stockdays':len(allpairs),'verified_records':len(lv),'verified_stocks':lv.code.nunique(),'verified_stockdays':len(events),'episodes':len(ep),'episode_stocks':ep.code.nunique(),'bad':dict(collections.Counter(x[1] for x in bad)),'date_min':events.date.min(),'date_max':events.date.max(),'missing_global_dates':sorted(set(cal)-set(daily.trade_date))},'catalog':catalog,'strategies':summary,'factors':factorstats,'skips':{str(k):v for k,v in skip.items()},'sector_counts':sectors.sector.value_counts().head(20).to_dict(),'sector_episodes':len(sectors),'cohort_features':{},'control_features':{}}
for k in ['vr','bias5','bias20','ma5above20','above60','amount_yi','main','main5pct','small','ths_turnover_pct']:
 result['cohort_features'][k]={'n':int(ep[k].notna().sum()),'median':float(ep[k].median()),'positive':float((ep[k].dropna()>0).mean())}
 # match event-date weights
 ct=ep[['date']].merge(controls[controls.feature==k],on='date');result['control_features'][k]={'median_of_daily_medians':ct['median'].mean(),'positive':ct.positive.mean()}
ledger.to_csv(OUT/'leaderboard-all-stocks-20260929.csv',index=False,encoding='utf-8-sig')
events.to_csv(OUT/'leaderboard-events-20260929.csv',index=False,encoding='utf-8-sig')
tr.to_csv(OUT/'leaderboard-entry-tests-20260929.csv',index=False,encoding='utf-8-sig')
(OUT/'leaderboard-analysis-20260929.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=lambda v:float(v)),encoding='utf8')
c.rollback();c.close()
print(json.dumps(result,ensure_ascii=False,indent=2,default=lambda v:float(v)))
