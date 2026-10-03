# Run after analyze_ten_percent_20260929.py; output documentation and complete stock universe.
import json,sqlite3
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs'
p=OUT/'ten-percent-analysis-20260929.json';r=json.loads(p.read_text())
c=sqlite3.connect(f'file:{ROOT}/backend/market_data.db?mode=ro',uri=True);c.execute('PRAGMA query_only=ON')
names=pd.read_sql_query('SELECT ts_code code,name,MAX(trade_date) last_daily_date FROM stock_daily GROUP BY ts_code',c).set_index('code')
s=pd.read_csv(OUT/'ten-percent-all-stocks-20260929.csv',dtype={'code':str,'first':str,'last':str,'max_peak_start':str})
parts=[]
for h in [5,10,20]:
 g=s[s.horizon==h].set_index('code').reindex(names.index);g['horizon']=h;g['name']=g.name.fillna(names.name).replace('',np.nan).fillna('本地未存名称')
 g['board']=['北交所' if code.endswith('.BJ') else '科创板' if code.startswith(('688','689')) else '创业板' if code.startswith(('300','301')) else '主板' for code in g.index]
 g[['observations','hits']]=g[['observations','hits']].fillna(0).astype(int)
 g['hit_rate_pct']=100*g.hits/g.observations.replace(0,np.nan)
 g['status']=np.where(g.observations==0,'无完整可核验窗口',np.where(g.hits>0,'至少一次超过10%','有效窗口均未超过10%'))
 parts.append(g.reset_index())
s=pd.concat(parts);s.to_csv(OUT/'ten-percent-all-stocks-20260929.csv',index=False,encoding='utf-8-sig')
s[s.hits>0].rename(columns={'code':'股票代码','name':'股票名称','horizon':'观察窗口_交易日','board':'交易板块','observations':'有效观察次数','hits':'超过10%的次数','hit_rate_pct':'超过10%的比例_pct','max_peak':'累计最高涨幅_pct','max_peak_start':'最高涨幅对应起点日','first':'最早有效起点日','last':'最新有效起点日','status':'数据状态'}).to_csv(OUT/'ten-percent-winners-20260929.csv',index=False,encoding='utf-8-sig')
z=pd.read_pickle('/tmp/tradeiq_tenpct_events10.pkl')
def rate(a):return {'n':len(a),'rate':float(a.hit.mean()*100)}
extra={}
extra['volatility_bins']=[]
for lo,hi in [(0,3),(3,5),(5,8),(8,np.inf)]:
 a=z[(z.amplitude20>=lo)&(z.amplitude20<hi)];extra['volatility_bins'].append({'from':lo,'to':hi if np.isfinite(hi) else None,**rate(a)})
extra['fund_splits']=[]
for col,label in [('main5pct','5日主力强度≥5%'),('main1pct','当日主力净流入'),('small1pct','当日小单净流出')]:
 for start,end in [('20260616','20260731'),('20260801','20260911')]:
  a=z[z.date.between(start,end)&z[col].notna()];m=a[col]>=5 if col=='main5pct' else a[col]<0 if col=='small1pct' else a[col]>0
  extra['fund_splits'].append({'feature':label,'start':start,'end':end,'yes':rate(a[m]),'no':rate(a[~m])})
extra['vol_months']=[]
for month,a in z[z.amplitude20.notna()].groupby(z.date.str[:6]):
 m=a.amplitude20>=5
 extra['vol_months'].append({'month':month,'yes':rate(a[m]),'no':rate(a[~m])})
# Verify feature construction independently from raw records, in addition to the 90 outcome checks.
cal=[x[0] for x in c.execute("SELECT cal_date FROM trading_dates_cache WHERE cal_date<='20260928' ORDER BY cal_date")];ordinal={d:i for i,d in enumerate(cal)}
checks=[]
for row in z[z.main5pct.notna()&z.amplitude20.notna()&z.volume_vs5.notna()].sample(15,random_state=29).itertuples():
 days=cal[ordinal[row.date]-20:ordinal[row.date]+1]
 daily=pd.read_sql_query('SELECT trade_date,high,low,close,volume,amount FROM stock_daily WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date',c,params=(row.code,days[0],days[-1]))
 assert daily.trade_date.tolist()==days
 amp=((daily.high-daily.low)/daily.close.shift()*100).iloc[-20:].mean()
 assert abs(amp-row.amplitude20)<1e-8
 vr=daily.volume.iloc[-1]/daily.volume.iloc[-6:-1].mean();assert abs(vr-row.volume_vs5)<1e-8
 flow=c.execute("SELECT SUM(main_net_inflow),COUNT(*) FROM fund_flow WHERE source='eastmoney-push2' AND ts_code=? AND trade_date BETWEEN ? AND ?",(row.code,days[-5],days[-1])).fetchone()
 assert flow[1]==5
 strength=flow[0]/(daily.amount.iloc[-5:].sum()*1000)*100;assert abs(strength-row.main5pct)<1e-8
 checks.append([row.code,row.date])
extra['feature_validation_samples']=len(checks);extra['all_stocks']=len(names);extra['stock_rows']=len(s);extra['codes_without_names']=s.loc[s.name=='本地未存名称','code'].nunique()
assert len(s)==len(names)*3
assert s.groupby('horizon').hits.sum().to_dict()=={int(k):v['hits'] for k,v in r['horizons'].items()}
assert np.isinf(z.select_dtypes(include=np.number)).sum().sum()==0
r['supplement']=extra
p.write_text(json.dumps(r,ensure_ascii=False,indent=2));c.close()
# Write a reviewable full report with denominators and limitations next to findings.
def pct(v):return '—' if v is None or not np.isfinite(v) else f'{v:.2f}%'
def num(v):return f'{int(v):,}'
def table(head,rows):return '\n'.join(['| '+' | '.join(head)+' |','| '+' | '.join(['---']*len(head))+' |']+['| '+' | '.join(map(str,row))+' |' for row in rows])
lines=['# 全市场个股：未来累计最高涨幅超过10%的共性分析','',
'分析日期：2026-09-29；本地已保存日线截止2026-09-28。仅使用本地数据库，SQLite `mode=ro` + `query_only=ON` 读取。未更改行情、应用规则或数据库结构。','',
'## 结论','',
'最稳定的区分项是此前波动较大，其次是近期已有涨势、突破前高；单日放量的关联较弱且随观察期延长而减弱。近5日主力净流入强度有一定关联，但资金数据只有近期一段，不能称为全历史规律。','',
'“某股票曾经涨过10%”几乎没有筛选价值：10日有效样本涉及5,778只股票，其中5,734只至少出现一次。应看每个观察起点随后触及10%的比例，而不是把历史上出现过的股票都当成优质标的。','',
'本报告是在全市场发现事后关联，不是买点回测；此前就有高波动的股票也更容易在未来碰到高点，不能把高点概率直接理解为收益优势。','',
'## 口径与完整样本','',
'- 每个“股票×起点日”是一条观察。从起点日收盘价出发，计算随后5、10、20个本地交易日内最高价的最大值：`max(未来high)/起点close−1`。严格大于10%，刚好10%不算。起点日自身高点不计入。',
'- 本次不采用每日涨跌幅算术相加的口径。未来窗口须完整；缺任一天、未来未走完、价格参考异常均不把它算成失败样本，也不跳过缺日。',
'- 日线为本地未复权价格；未来每天收盘价推导的涨跌幅与存储涨跌幅相差超过0.15个百分点时，整个窗口剔除。该检查只能降低除权/参考价异常影响，不等于正式复权处理。',
'- 所有特征只取起点日及以前；成交额千元换算为元后，再计算资金净额比例。均线、振幅、累计资金均重新计算且要求完整窗口。特征缺失只在该项比较中排除。',
'- 同一股票可有多个观察起点；补做同股未来窗口不重叠检验：按日期从前向后取第一个有效起点，下一次起点必须晚于上次窗口结束，选取不参考结果。日期间及股票间仍有相关性。','',
table(['未来窗口','有效观察数','涉及股票','超过10%的观察数','触及比例','曾触及股票数','最新完整起点'],[[h+'日',num(v['samples']),num(v['stocks']),num(v['hits']),pct(v['rate']),num(v['hit_stocks']),v['last']] for h,v in r['horizons'].items()]),'',
'部分历史股票仅保存代码而缺少名称，清单标为“本地未存名称”，不影响行情计算。数据库共有5,797个有日线的股票代码；完整登记表每个代码都有5/10/20日三行，无可计算窗口的代码单独标记，不能把无数据视为没涨过。不同股票上市/退市时间和缺失情况不同，历史触及次数不能直接作为股票质量排名。','',
'## 1. 波动水平是最强、较稳定的区分项','',
'近期振幅定义为最近20个完整交易日的 `(最高价−最低价)/前收盘价` 的平均值。未来10日触及10%的组，这项振幅中位数为4.37%，未触及组为3.42%。这是分布上的偏移，不表示所有上涨股票都高波动。','',
table(['此前20日平均振幅','有效观察数','未来10日触及10%'],[[f"{x['from']}%～{x['to']}%（不含上限）" if x['to'] is not None else '≥8%',num(x['n']),pct(x['rate'])] for x in extra['volatility_bins']]),'',
'振幅≥5%与其余样本比较，5/10/20日触及比例分别为27.70%/42.06%/55.71%，其余为11.12%/21.97%/38.26%。在同一天、同交易板块比较后，差异仍为16.14/19.16/16.46个百分点；同股去重、主板内部及剔除当前ST名称后方向一致。它更接近“上涨空间与波动风险”特征，而不是低风险特征。','',
'## 2. 已有涨势、突破前高，比单纯缩量更有区分度','',
table(['起点日特征','5日：有/无该特征','10日：有/无该特征','20日：有/无该特征'],[[label]+[pct(next(x for x in r['features'][h] if x['feature']==label)['yes_rate'])+' / '+pct(next(x for x in r['features'][h] if x['feature']==label)['no_rate']) for h in ['5','10','20']] for label in ['此前20日涨幅>15%','收盘突破此前20日高点','收盘高于MA20','当日放量≥前5日均量1.5倍','温和缩量为前日70%至80%','当日收跌']]),'',
'表内是“未来最高价触及10%的比例”，不是平均收益。每行都只使用该特征完整的样本，不能拿所有行与全样本基准机械比较。','',
'此前20日涨幅>15%、突破此前20日高点，在同日同板块并进一步按历史振幅分层后，10日差异仍约8.69、8.82个百分点。不过10日成功样本中，只有19.18%属于前者、7.27%属于后者；它们有区分度，但远非所有成功样本都具备。','',
'“收盘不高于MA60”在全市场中反而对应较低触及比例：10日为22.89%，高于MA60为31.62%。不能把此前条件优选内部关于均线位置、缩量的结论直接推广到全市场；两者基础样本不同。','',
'趋势不是各阶段同样有效：MA5高于MA20在2026-04以前的10日差异约7.91个百分点，之后约0.43个百分点；20日后段差异约−0.74个百分点。动量/突破的方向更一致，但关联强度也会变化。','',
'## 3. 资金：累计强度有一定关联，单日方向较弱','',
table(['资金特征','5日：有/无','10日：有/无','20日：有/无'],[[label]+[pct(next(x for x in r['features'][h] if x['feature']==label)['yes_rate'])+' / '+pct(next(x for x in r['features'][h] if x['feature']==label)['no_rate']) for h in ['5','10','20']] for label in ['5日主力净流入占成交额≥5%','当日主力净流入','当日小单净流出','当日超大单净流入','当日大单净流入','当日中单净流出']]),'',
'5日主力强度 = 同期主力净额合计 / 同期成交额合计。只用东方财富同源数据，不和同花顺/TuShare资金净额混算。10日这一项有17,189个满足条件的观察与254,064个不满足观察；同日同板块再按振幅分层后的差异约4.95个百分点，20日仅约1.45个百分点。','',
'10日成功样本中，有资金数据的部分只有41.77%出现当日主力净流入、43.64%出现小单净流出，均不到一半。因此“上涨股都由主力净流入、小单流出推动”不符合本地数据。主力/超大单/大单相互包含且相关，不能当成多份独立证据；订单大小也不等于真实投资者身份。','',
table(['资金特征','观察起点区间','满足条件数/比例','其余数/比例'],[[x['feature'],x['start']+'～'+x['end'],num(x['yes']['n'])+' / '+pct(x['yes']['rate']),num(x['no']['n'])+' / '+pct(x['no']['rate'])] for x in extra['fund_splits']]),'',
'同花顺换手率≥5%的10日触及比例36.59%，其余20.85%，但额外控制振幅后差异降为约5.03个百分点，20日降至约0.19个百分点。其数据仅26天；20日比较仅约6个有可用特征的起点日期，不能当作稳定规律。同花顺单日资金净流入未呈现正向区分。','',
'## 4. 交易板块、行业、市场背景','',
table(['板块','5日触及比例','10日触及比例','20日触及比例'],[[b]+[pct(next(x for x in r['boards'][h] if x['board']==b)['rate']*100) for h in ['5','10','20']] for b in ['主板','创业板','科创板','北交所']]),'',
'不能据此宣称某板块更容易赚到钱；板块的波动分布、价格涨跌幅空间和样本组成不同。特征比较已另外按同日同板块分层。','',
'以下行业按本地现有申万一级映射描述，字典版本为SW2014，没有完整的逐日历史归属，不能当作历史可交易的行业筛选回测：','',
table(['行业','10日有效观察','涉及股票','10日触及比例'],[[x['industry'],num(x['n']),num(x['stocks']),pct(x['rate']*100)] for x in r['industry']['10'] if x['industry'] in ['电子','有色金属','通信','电气设备','国防军工','银行','食品饮料','非银金融','未知']]),'',
'电子、有色金属、通信在这段本地历史中偏高，银行等偏低；这与行业本身的波动和阶段行情有关，未控制历史市值与行业指数收益，不能归因为行业标签。','',
'市场当天上涨家数占比≥60%并没有明显区分：10日触及比例27.13%，其余27.22%。但各起点月份的基准差异很大，例如2025年11月约19.33%、2026年7月约34.99%，说明不能把一个总体比例当作未来每天固定的概率。','',
'## 5. 触及高点不等于到期收益','',
table(['窗口','触及组到期收盘涨幅中位数','触及后到期仍高于10%的比例','触及组到期亏损比例','窗口内曾低于起点−5%的比例'],[[h+'日',pct(v['winner_end_median']),pct(v['winner_end_above10']),pct(v['winner_end_negative']),pct(v['winner_adverse_below5'])] for h,v in r['horizons'].items()]),'',
'10日内曾涨过10%的观察中，只有45.59%在第10日收盘仍涨超10%，12.87%到期反而低于起点。窗口最低价可能发生在触及10%之前或之后，本表没有推断先后，也没有模拟止盈止损。没有扣交易成本，没有验证能否在起点收盘成交；尤其连续涨停的股票可能无法买入。','',
'## 数据覆盖、排除项与检验','',
table(['数据','行数','最早日期','最新日期','有记录日期数'],[[k,num(v['n']),v['first'],v['last'],num(v['dates'])] for k,v in r['coverage'].items()]),'',
'- 个股日线有431个记录日期，主连续段为2025-04-01～2026-09-28共365个交易日；另纳入2020-01-02～02-19的29日、2020-02-27～03-09的8日连续片段。其余29个散落日期不能提供完整5日未来窗口，保留在覆盖核查但不强行拼接。',
'- 2020年片段与近期数据不是连续历史。仅保留2025-04起，5/10/20日触及比例为15.31%/27.19%/43.10%，与全样本15.54%/27.22%/42.89%接近。',
'- 同股窗口去重后，样本分别344,087/182,086/91,497条，比例15.45%/26.31%/40.49%。核心波动与动量差异仍在；这不等于严格独立样本。',
'- 交易日来自本地trading_dates_cache，只核验本地内部一致性；未联网补全或校准。未纳入本地没有保存的股票，缺失/退市数据仍可能带来选择偏差。',
'- 行业/概念资金8天、分时9天、题材热度1天，观察期不足，不能支撑跨阶段共性。涨停题材仅136个日期且非完整每日记录，未把“没有记录”当作“没有涨停”；当前概念映射、MA缓存、榜单与选股快照属派生或非历史完整信息，未重复作为独立证据。',
'- 申万行业日线、北向资金、详细涨停池、板块累计资金为空。缺少完整历史市值、财报、估值、公告和席位身份数据，无法验证小市值、盈利增长或具体消息催化等说法。个人持仓、交易、配置不属于市场共性，不参与分析。',
'- 资金覆盖2026-06-16起，其中部分日期个股覆盖偏低，要求完整5日窗口会进一步减少样本。基于现有数据的统计不是当时信息发布时间的完整重放。',
'- 特征对比采用同日同板块分组内两组触及率之差，以 n1×n0/(n1+n0) 加权；附加振幅分层为<3%、3～5%、5～8%、≥8%。没有同时控制全部特征，差异不能相加，也不是因果效应或策略收益。市场上涨家数是同日共同变量，不能做同日内两组对比。',
'- 采用固定解释性阈值比较多个特征，没有进行独立样本外检验或多重检验校正；时间分段、去重和分层用于判断方向稳定性，不称为统计显著或预测模型。',
'- 从原始SQL随机独立复算90条未来窗口；另复算15条振幅、量比和主力强度，全部一致。全部股票表的触及次数合计与主报告一致，10日数值特征未发现无穷值。','',
'## 全部特征明细','',
'下表每行列出分母、成功组/失败组的特征占比与分层差异；可用于区分“常见”与“有区分度”。差异单位为百分点。','']
for h in ['5','10','20']:
 lines+=['### 未来'+h+'日','',table(['特征','满足/其余样本数','满足/其余触及率','成功/失败组特征占比','同日同板块差异','再分振幅差异'],[[v['feature'],num(v['yes_n'])+' / '+num(v['no_n']),pct(v['yes_rate'])+' / '+pct(v['no_rate']),pct(v['winner_share'])+' / '+pct(v['other_share']), '—' if v['adjusted_pp'] is None else f"{v['adjusted_pp']:.2f}", '—' if v['vol_adjusted_pp'] is None else f"{v['vol_adjusted_pp']:.2f}"] for v in r['features'][h]]),'']
lines+=['## 文件与复算','',
'- `docs/ten-percent-all-stocks-20260929.csv`：全部5,797个代码，逐期限记录有效窗口数、触及次数/比例、最高涨幅及对应起点；无有效窗口有独立标记。',
'- `docs/ten-percent-winners-20260929.csv`：按期限筛出至少有一次超过10%的全部股票。',
'- `docs/ten-percent-analysis-20260929.json`：机器可读汇总与检验信息。',
'- 先运行 `python3 backend/scripts/analyze_ten_percent_20260929.py`，再运行 `python3 backend/scripts/summarize_ten_percent_20260929.py`。中间10日事件明细存系统临时目录；最终报告与CSV留在docs。','']
(OUT/'ten-percent-research-20260929.md').write_text('\n'.join(lines))
print('报告与全部股票清单已生成；90条涨幅窗口和15条特征复算通过，股票表覆盖5,797个代码。')
