# One-off read-only research. Persistent outputs: dated CSV/JSON/Markdown in existing docs/.
import sqlite3,json,time
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[2]; OUT=ROOT/'docs'
c=sqlite3.connect(f'file:{ROOT}/backend/market_data.db?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
q=lambda s:pd.read_sql_query(s,c)
coverage={}
for t,dt in [('stock_daily','trade_date'),('fund_flow','trade_date'),('fund_flow_ths','trade_date'),('limitup','date'),('index_daily','date'),('overview','date'),('sector_fund_flow','trade_date'),('stock_minline_bars','trade_date')]:
 coverage[t]=q(f'SELECT COUNT(*) n,MIN({dt}) first,MAX({dt}) last,COUNT(DISTINCT {dt}) dates FROM {t}').iloc[0].to_dict()
print('coverage read',flush=True)
daily=q('SELECT ts_code,trade_date,open,high,low,close,change,volume,amount FROM stock_daily')
cal=q("SELECT cal_date FROM trading_dates_cache WHERE cal_date<='20260928' ORDER BY cal_date").cal_date.tolist(); ordinal={d:i for i,d in enumerate(cal)}
# Compact the calendar but explicitly insert a NaN separator for EVERY missing market day.
# Restrict each independent continuous segment below; no rolling window crosses a gap.
available=sorted(daily.trade_date.unique()); segments=[]
for d in available:
 if not segments or ordinal[d]!=ordinal[segments[-1][-1]]+1:segments.append([])
 segments[-1].append(d)
print('segments',[(x[0],x[-1],len(x)) for x in segments if len(x)>=6],flush=True)
# Use all continuous pieces with >=6 sessions; short pieces cannot supply any 5-day outcome.
use=[x for x in segments if len(x)>=6]; dates=[d for x in use for d in x]; codes=sorted(daily.ts_code.unique())
A={k:daily.pivot(index='trade_date',columns='ts_code',values=k).reindex(index=dates,columns=codes) for k in ['open','high','low','close','change','volume','amount']}; del daily
segment_id=np.concatenate([np.repeat(i,len(x)) for i,x in enumerate(use)])
# Rolling/shifting restarts at calendar gaps.
def shift(x,n=1):return x.groupby(segment_id).shift(n)
def roll(x,n,how='mean'):
 return x.groupby(segment_id,group_keys=False).apply(lambda g:getattr(g.rolling(n,min_periods=n),how)()).reindex(dates)
cl=A['close'];prev=shift(cl); day=A['change']
barok=(cl>0)&(A['low']>0)&(A['high']>=cl)&(A['low']<=cl)&(A['high']>=A['open'])&(A['low']<=A['open'])&(A['volume']>0)
refok=((cl/prev-1)*100-day).abs()<=.15
# Drop windows with missing sessions or ex-rights/reference changes, not repair unadjusted prices.
valid=barok&refok;hist20=roll(valid.astype(float),20,'sum')==20
ma={n:roll(cl,n).where(roll(valid.astype(float),n,'sum')==n) for n in [5,10,20,60]}
F={
 'day_return':day.where(valid),
 'ret5':((cl/shift(cl,5)-1)*100).where(roll(valid.astype(float),5,'sum')==5),
 'ret20':((cl/shift(cl,20)-1)*100).where(hist20),
 'bias20':(cl/ma[20]-1)*100,
 'bias60':(cl/ma[60]-1)*100,
 'volume_ratio':(A['volume']/shift(A['volume'])).where(valid),
 'volume_vs5':(A['volume']/shift(roll(A['volume'],5))).where(roll(valid.astype(float),6,'sum')==6),
 'amplitude20':roll((A['high']-A['low'])/prev*100,20).where(hist20),
 'amount_yi':A['amount']/100000,
 'trend':(ma[5]>ma[20]).astype(float).where(ma[20].notna()),
 'breakout':(cl>shift(roll(A['high'],20,'max'))).astype(float).where(hist20&shift(hist20)),
}
flow=q("SELECT trade_date,ts_code,main_net_inflow,super_net,big_net,mid_net,small_net FROM fund_flow WHERE source='eastmoney-push2'")
for k,col in [('main','main_net_inflow'),('super','super_net'),('big','big_net'),('mid','mid_net'),('small','small_net')]:
 f=flow.pivot(index='trade_date',columns='ts_code',values=col).reindex(index=dates,columns=codes)
 F[k+'1pct']=f/(A['amount']*1000)*100
 if k in ['main','small']:
  F[k+'5pct']=roll(f,5,'sum')/(roll(A['amount'],5,'sum')*1000)*100
ths=q('SELECT trade_date,ts_code,turnover_pct,net FROM fund_flow_ths')
for col in ['turnover_pct','net']:
 F['ths_'+col]=ths.pivot(index='trade_date',columns='ts_code',values=col).reindex(index=dates,columns=codes)
ov=q('SELECT date,upCount,downCount,flatCount FROM overview').set_index('date')
breadth=(ov.upCount/(ov.upCount+ov.downCount+ov.flatCount)*100).reindex(dates)
basic=q('SELECT ts_code,name FROM stock_basic').set_index('ts_code')
industry=q("SELECT m.ts_code,m.in_date,m.out_date,i.industry_name FROM sw_industry_member m JOIN sw_industry i ON m.index_code=i.index_code WHERE i.level='L1'")
current_ind=industry[industry.out_date.isna() | (industry.out_date=='')].drop_duplicates('ts_code').set_index('ts_code').industry_name.to_dict()
boards=np.array(['北交所' if s.endswith('.BJ') else '科创板' if s.startswith(('688','689')) else '创业板' if s.startswith(('300','301')) else '主板' for s in codes])
# Current identity only for sensitivity, never claim historical ST history is available.
is_st=np.array([any(t in str(basic.loc[s,'name'] if s in basic.index else '') for t in ['ST','退']) for s in codes])
flat={k:v.to_numpy().ravel() for k,v in F.items()}
base=pd.DataFrame(flat)
base['date']=np.repeat(dates,len(codes));base['code']=np.tile(codes,len(dates));base['board']=np.tile(boards,len(dates));base['st_current']=np.tile(is_st,len(dates));base['breadth']=np.repeat(breadth.to_numpy(),len(codes))
conditions={
 '近20日平均振幅≥5%':('amplitude20',lambda x:x>=5),
 '近20日平均振幅<3%':('amplitude20',lambda x:x<3),
 '当日放量≥前5日均量1.5倍':('volume_vs5',lambda x:x>=1.5),
 '温和缩量为前日70%至80%':('volume_ratio',lambda x:x.between(.7,.8)),
 '当日收跌':('day_return',lambda x:x<0),
 '此前20日涨幅>15%':('ret20',lambda x:x>15),
 '收盘高于MA20':('bias20',lambda x:x>0),
 '收盘不高于MA60':('bias60',lambda x:x<=0),
 'MA5高于MA20':('trend',lambda x:x==1),
 '收盘突破此前20日高点':('breakout',lambda x:x==1),
 '当日主力净流入':('main1pct',lambda x:x>0),
 '当日小单净流出':('small1pct',lambda x:x<0),
 '5日主力净流入占成交额≥5%':('main5pct',lambda x:x>=5),
 '当日超大单净流入':('super1pct',lambda x:x>0),
 '当日大单净流入':('big1pct',lambda x:x>0),
 '当日中单净流出':('mid1pct',lambda x:x<0),
 '同花顺换手率≥5%':('ths_turnover_pct',lambda x:x>=5),
 '同花顺资金净流入':('ths_net',lambda x:x>0),
 '市场上涨家数占比≥60%':('breadth',lambda x:x>=60),
}
def groupstats(df,mask):
 a=df[mask];b=df[~mask]
 return {'yes_n':len(a),'no_n':len(b),'yes_rate':a.hit.mean()*100,'no_rate':b.hit.mean()*100,'winner_share':mask[df.hit].mean()*100,'other_share':mask[~df.hit].mean()*100}
def adjusted(df,mask,extra=False):
 z=df[['date','board','hit','amplitude20']].copy();z['feature']=mask.to_numpy()
 keys=['date','board']
 if extra:
  z['vol_bin']=pd.cut(z.amplitude20,[-np.inf,3,5,8,np.inf],labels=False);keys+=['vol_bin']
 g=z.groupby(keys+['feature'],observed=True).hit.agg(['mean','size']).unstack('feature')
 if True not in g['mean'] or False not in g['mean']:return None
 g=g.dropna();w=g['size'][True]*g['size'][False]/(g['size'][True]+g['size'][False])
 return float(np.average((g['mean'][True]-g['mean'][False])*100,weights=w)) if len(g) else None
report={'coverage':coverage,'segments':[(x[0],x[-1],len(x)) for x in use],'horizons':{},'features':{},'medians':{},'boards':{},'monthly':{},'industry':{},'verification':{}}
stock_outputs=[];checks=[]
for h in [5,10,20]:
 highmax=cl*0-np.inf; lowmin=cl*0+np.inf;ok=barok.copy()
 for k in range(1,h+1):
  hi=shift(A['high'],-k);lo=shift(A['low'],-k)
  highmax=np.maximum(highmax,hi);lowmin=np.minimum(lowmin,lo);ok &= shift(valid.astype(float),-k).eq(1)
 peak=(highmax/cl-1)*100;end=(shift(cl,-h)/cl-1)*100;adverse=(lowmin/cl-1)*100
 mask=ok.to_numpy().ravel()&np.isfinite(peak.to_numpy().ravel())
 z=base[mask].copy();z['peak']=peak.to_numpy().ravel()[mask];z['end']=end.to_numpy().ravel()[mask];z['adverse']=adverse.to_numpy().ravel()[mask];z['hit']=z.peak>10+1e-10
 # Remove overlap without selecting based on future outcomes; each stock starts with first eligible date.
 take=np.zeros(len(z),bool);last={}
 for n,r in enumerate(z[['date','code']].itertuples(index=False)):
  i=ordinal[r.date]
  if i>last.get(r.code,-9999):take[n]=True;last[r.code]=i+h
 z['nonoverlap']=take
 modern=z.date>='20250401';winner=z[z.hit]
 report['horizons'][h]={'samples':len(z),'stocks':z.code.nunique(),'hits':int(z.hit.sum()),'hit_stocks':winner.code.nunique(),'rate':z.hit.mean()*100,'first':z.date.min(),'last':z.date.max(),'modern_n':int(modern.sum()),'modern_rate':z.loc[modern,'hit'].mean()*100,'early_n':int((~modern).sum()),'early_rate':z.loc[~modern,'hit'].mean()*100,'nonoverlap_n':int(take.sum()),'nonoverlap_rate':z.loc[take,'hit'].mean()*100,'winner_end_median':winner.end.median(),'winner_end_above10':(winner.end>10).mean()*100,'winner_end_negative':(winner.end<0).mean()*100,'winner_adverse_median':winner.adverse.median(),'winner_adverse_below5':(winner.adverse < -5).mean()*100,'latest_complete_start':dates[-h-1]}
 feature_rows=[]
 for label,(col,fn) in conditions.items():
  w=z[z[col].notna()].copy();m=fn(w[col]);r={'feature':label,**groupstats(w,m)}
  r['adjusted_pp']=adjusted(w,m);r['vol_adjusted_pp']=adjusted(w,m,True)
  for key,sub in [('early',w[w.date<'20260401']),('late',w[w.date>='20260401']),('nonoverlap',w[w.nonoverlap]),('mainboard',w[w.board=='主板']),('nonst',w[~w.st_current])]:
   stats=groupstats(sub,fn(sub[col]));r[key+'_n']=len(sub);r[key+'_diff']=stats['yes_rate']-stats['no_rate']
  feature_rows.append(r)
 report['features'][h]=feature_rows
 report['medians'][h]=[{'feature':k,'winner_n':int(z.loc[z.hit,k].notna().sum()),'other_n':int(z.loc[~z.hit,k].notna().sum()),'winner':z.loc[z.hit,k].median(),'other':z.loc[~z.hit,k].median()} for k in F]
 report['boards'][h]=z.groupby('board').agg(n=('hit','size'),rate=('hit','mean')).reset_index().to_dict('records')
 report['monthly'][h]=z.assign(month=z.date.str[:6]).groupby('month').agg(n=('hit','size'),rate=('hit','mean')).reset_index().to_dict('records')
 zi=z.copy();zi['industry']=zi.code.map(current_ind).fillna('未知')
 report['industry'][h]=zi.groupby('industry').agg(n=('hit','size'),rate=('hit','mean'),stocks=('code','nunique')).sort_values('rate',ascending=False).reset_index().to_dict('records')
 agg=z.groupby('code').agg(observations=('hit','size'),hits=('hit','sum'),max_peak=('peak','max'),first=('date','min'),last=('date','max')).reset_index();agg['horizon']=h;agg['name']=agg.code.map(basic.name);agg['board']=agg.code.map(dict(zip(codes,boards)))
 best=z.loc[z.groupby('code').peak.idxmax(),['code','date']].rename(columns={'date':'max_peak_start'});agg=agg.merge(best,on='code');stock_outputs.append(agg)
 if h==10:
  # Keep compressed all-event audit externally in temp, stock list and aggregate report in docs.
  z.to_pickle('/tmp/tradeiq_tenpct_events10.pkl')
 sample=z.sample(min(30,len(z)),random_state=h)
 for r in sample.itertuples():
  ix=ordinal[r.date];ds=cal[ix+1:ix+h+1];raw=q("SELECT trade_date,high,close,change FROM stock_daily WHERE ts_code='"+r.code+"' AND trade_date BETWEEN '"+ds[0]+"' AND '"+ds[-1]+"' ORDER BY trade_date")
  anchor=c.execute('SELECT close FROM stock_daily WHERE ts_code=? AND trade_date=?',(r.code,r.date)).fetchone()[0]
  assert raw.trade_date.tolist()==ds
  assert abs((raw.high.max()/anchor-1)*100-r.peak)<1e-8
  checks.append((h,r.code,r.date))
 print('finished',h,report['horizons'][h],flush=True)
# JSON safe conversion incl NumPy types/NaN
pd.concat(stock_outputs).to_csv(OUT/'ten-percent-all-stocks-20260929.csv',index=False,encoding='utf-8-sig')
report['verification']={'raw_sql_samples':len(checks),'query_only':c.execute('PRAGMA query_only').fetchone()[0],'reference_tolerance_pp':.15,'threshold':'strictly >10%','calendar_missing_windows':'excluded'}
c.close()
def clean(x):
 if isinstance(x,dict):return {str(k):clean(v) for k,v in x.items()}
 if isinstance(x,(list,tuple)):return [clean(v) for v in x]
 if isinstance(x,np.integer):return int(x)
 if isinstance(x,(float,np.floating)):return float(x) if np.isfinite(x) else None
 return x
(OUT/'ten-percent-analysis-20260929.json').write_text(json.dumps(clean(report),ensure_ascii=False,indent=2))
print('SAVED',flush=True)
