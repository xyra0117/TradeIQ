'use strict';

// Local, deterministic visual samples. No market or account data is read.
window.TradeIQPreviewCharts = (() => {
  const models = new Map();
  const tiers = [['main','主力'],['super','超大单'],['large','大单'],['medium','中单'],['small','小单']];
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = name => `<svg class="icon small" aria-hidden="true"><use href="icons.svg#i-${name}"/></svg>`;
  const signed = value => `${value >= 0 ? '+' : ''}${value.toFixed(2)}`;
  function samples(stock,date) {
    const end = new Date(`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}T12:00:00`);
    const days=[];
    while(days.length<90) { if(end.getDay()!==0&&end.getDay()!==6) days.unshift(`${end.getFullYear()}.${String(end.getMonth()+1).padStart(2,'0')}.${String(end.getDate()).padStart(2,'0')}`);end.setDate(end.getDate()-1); }
    return days.map((date,i)=>{
      const close=stock.price*(.8+i*.002+Math.sin(i*.51+(stock.hits||2))*.025);
      const open=close+stock.price*Math.sin(i*1.73)*.011;
      const funds=Object.fromEntries(tiers.map(([key],t)=>[key,Math.sin(i*.43+t)*.16+(t<3?.035:-.02)]));
      return {date,open,close,high:Math.max(open,close)+stock.price*.006,low:Math.min(open,close)-stock.price*.006,volume:12+Math.abs(Math.sin(i*.53))*29,funds,macd:Math.sin(i*.48)*.12};
    });
  }
  function svgPlot(model,type,title,height) {
    const data=model.data.slice(model.start,model.start+model.count), W=640,left=14,right=580,top=12,bottom=height-23;
    const x=i=>left+i*(right-left)/Math.max(1,data.length-1);
    const grid=`<path d="M${left} ${top}H${right}M${left} ${(top+bottom)/2}H${right}M${left} ${bottom}H${right}" stroke="var(--chart-grid)" fill="none"/>`;
    let drawing=grid, high=0,low=0;
    if(type==='price') {
      high=Math.max(...data.map(d=>d.high))*1.015;low=Math.min(...data.map(d=>d.low))*.985;
      const y=v=>top+(high-v)/(high-low)*(bottom-top);
      if(model.mode==='minute') drawing+=`<polyline points="${data.map((d,i)=>`${x(i)},${y(d.close)}`).join(' ')}" fill="none" stroke="var(--ink)" stroke-width="1.5"/>`;
      else {
        drawing+=data.map((d,i)=>`<path d="M${x(i)} ${y(d.high)}V${y(d.low)}" stroke="${d.close>=d.open?'var(--up)':'var(--down)'}"/><rect x="${x(i)-2}" y="${Math.min(y(d.open),y(d.close))}" width="4" height="${Math.max(1,Math.abs(y(d.open)-y(d.close)))}" fill="${d.close>=d.open?'var(--up)':'var(--down)'}"/>`).join('');
        [5,10,20,30,60].forEach((n,j)=>{if(!model.maVisible.has(n))return;
          const points=data.map((_,i)=>{const at=model.start+i; const sample=model.data.slice(Math.max(0,at-n+1),at+1);return `${x(i)},${y(sample.reduce((s,d)=>s+d.close,0)/sample.length)}`;}).join(' ');
          drawing+=`<polyline points="${points}" fill="none" stroke="var(--ink)" stroke-width="${j===0?1.3:1}" opacity="${1-j*.15}" stroke-dasharray="${j%2?'4 3':'none'}"/>`;
        });
      }
      if(model.delivery) [65,79].forEach((at,j)=>{if(at<model.start||at>=model.start+model.count)return;const d=model.data[at],xx=x(at-model.start),yy=y(j?d.high:d.low);drawing+=`<g><rect x="${xx-8}" y="${yy+(j?-19:3)}" width="16" height="15" rx="2" fill="${j?'var(--down)':'var(--up)'}"/><text x="${xx}" y="${yy+(j?-8:14)}" text-anchor="middle" fill="white" font-size="9">${j?'S':'B'}</text><title>${d.date} · 示例${j?'卖出200':'买入1000'}股 · 价格${d.close.toFixed(2)}</title></g>`;});
    } else if(type==='volume') {
      high=45;low=0;drawing+=data.map((d,i)=>`<rect x="${x(i)-2}" y="${bottom-d.volume/45*(bottom-top)}" width="4" height="${d.volume/45*(bottom-top)}" fill="${d.close>=d.open?'var(--up)':'var(--down)'}" opacity=".55"/>`).join('');
    } else if(type==='macd') {
      high=.15;low=-.15;const middle=(top+bottom)/2;
      drawing+=data.map((d,i)=>`<rect x="${x(i)-2}" y="${d.macd>0?middle-d.macd/.3*(bottom-top):middle}" width="4" height="${Math.abs(d.macd)/.3*(bottom-top)}" fill="${d.macd>=0?'var(--up)':'var(--down)'}" opacity=".6"/>`).join('');
      [0,1].forEach(j=>{drawing+=`<polyline points="${data.map((d,i)=>`${x(i)},${middle-(d.macd*.6+Math.sin(i*.3+j)*.025)/.3*(bottom-top)}`).join(' ')}" fill="none" stroke="${j?'var(--muted)':'var(--ink)'}" stroke-width="1"/>`;});
    } else {
      high=type==='pct'?8:.25;low=-high;const y=v=>top+(high-v)/(high-low)*(bottom-top);
      tiers.forEach(([key],j)=>{if(!model.visible.has(key))return;drawing+=`<polyline points="${data.map((d,i)=>`${x(i)},${y(d.funds[key]*(type==='pct'?28:1))}`).join(' ')}" fill="none" stroke="var(--ink)" opacity="${1-j*.15}" stroke-width="${j===0?1.8:1.1}" stroke-dasharray="${j%2?'4 3':'none'}"/>`;});
    }
    drawing+=`<g fill="var(--muted)" font-size="9" font-family="var(--numeric)"><text x="590" y="${top+5}">${high.toFixed(type==='price'?2:1)}</text><text x="590" y="${bottom}">${low.toFixed(type==='price'?2:1)}</text><text x="14" y="${height-5}">${model.mode==='minute'?'09:30':data[0].date.slice(5)}</text><text x="530" y="${height-5}">${model.mode==='minute'?'15:00':data.at(-1).date.slice(5)}</text></g>`;
    return `<section class="preview-chart-section"><div class="preview-chart-caption">${title}<span>虚构示例</span></div><div class="preview-plot" data-preview-plot="${type}"><svg viewBox="0 0 ${W} ${height}" role="img" aria-label="${escape(title)}，全部为虚构数据">${drawing}</svg><i class="preview-pointer" hidden></i></div></section>`;
  }
  function plots(model) {
    const compact=model.compact;
    return (model.market?'':svgPlot(model,'price',model.mode==='minute'?'所选日分时':'日K · MA5 / 10 / 20 / 30 / 60',compact?150:215)+svgPlot(model,'volume',model.amount?'成交额 / 亿元':'成交量 / 万手',compact?55:65)+(model.mode==='minute'?'':svgPlot(model,'macd','MACD (12,26,9) · DIF / DEA',compact?65:75)))+(model.funds?svgPlot(model,'net','每日资金净额 / 亿元',compact?70:95)+svgPlot(model,'pct','每日资金净占比 / %',compact?70:95):'');
  }
  function render(options) {
    const key=options.key||'stock';const old=models.get(key);
    const model={...options,data:samples(options.stock,options.date),start:0,count:90,visible:new Set(['main','super','large']),maVisible:new Set([5,10,20,30,60]),funds:options.funds!==false};
    if(old&&old.date===model.date&&old.stock.code===model.stock.code)Object.assign(model,{start:old.start,count:old.count,visible:old.visible,maVisible:old.maVisible});
    if(options.range==='20')Object.assign(model,{count:20,start:70});
    if(options.range==='60')Object.assign(model,{count:60,start:30});
    models.set(key,model);
    return `<div class="preview-chart-group" data-preview-group="${escape(key)}"><div class="preview-chart-controls">${!model.market&&model.mode!=='minute'?[5,10,20,30,60].map(n=>`<button data-preview-ma="${n}" aria-pressed="${model.maVisible.has(n)}">MA${n}</button>`).join(''):''}${model.funds?tiers.map(([tier,label])=>`<button data-preview-tier="${tier}" aria-pressed="${model.visible.has(tier)}">${label}</button>`).join(''):''}<span>示例图表 · 多选图例</span></div><div class="preview-plots">${plots(model)}</div><div class="preview-range"><span>示例区间</span><input type="range" min="12" max="90" value="${model.count}" data-preview-range aria-label="调整所有图表区间"><button data-preview-reset>${icon('refresh')}全部</button></div><div class="preview-readout" aria-live="off">移动鼠标查看同日数据 · 滚轮缩放 · 按住图表拖动区间${model.delivery?' · B / S为示例成交标记':''}</div></div>`;
  }
  function redraw(group,model) {
    group.querySelector('.preview-plots').innerHTML=plots(model);
    group.querySelector('[data-preview-range]').value=model.count;
    group.querySelectorAll('[data-preview-ma]').forEach(button=>button.setAttribute('aria-pressed',String(model.maVisible.has(Number(button.dataset.previewMa)))));
    group.querySelectorAll('[data-preview-tier]').forEach(button=>button.setAttribute('aria-pressed',String(model.visible.has(button.dataset.previewTier))));
  }
  function point(group,event) {
    const model=models.get(group.dataset.previewGroup),plot=event.target.closest('[data-preview-plot]');if(!model||!plot)return;
    const rect=plot.getBoundingClientRect(),progress=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width));
    const at=Math.min(89,model.start+Math.round(progress*(model.count-1))),d=model.data[at];
    group.querySelectorAll('.preview-pointer').forEach(pointer=>{pointer.hidden=false;pointer.style.left=`${progress*100}%`;});
    const text=[`${model.mode==='minute'?model.date+' / 分时示例':d.date} · 示例 O ${d.open.toFixed(2)} / H ${d.high.toFixed(2)} / L ${d.low.toFixed(2)} / C ${d.close.toFixed(2)}`];
    if(model.funds)tiers.forEach(([key,label])=>{if(model.visible.has(key)){const totals=[1,3,5,10,20].map(n=>{const window=model.data.slice(Math.max(0,at-n+1),at+1);return {net:window.reduce((s,v)=>s+v.funds[key],0),days:window.length};});text.push(`${label}累计(亿) 1/3/5/10/20：${totals.map(v=>signed(v.net)).join(' / ')}`);text.push(`${label}累计净占比(%)：${totals.map(v=>signed(v.net/(v.days*100/28)*100)).join(' / ')}`);};});
    if(model.delivery) text.push('B 示例买入1000股 · S 示例卖出200股；成交日期见图中标记');
    group.querySelector('.preview-readout').textContent=text.join('\n');
  }
  document.addEventListener('click',event=>{
    const control=event.target.closest('[data-preview-tier],[data-preview-ma],[data-preview-reset]'),group=control?.closest('[data-preview-group]');if(!group)return;
    const model=models.get(group.dataset.previewGroup);
    if(control.hasAttribute('data-preview-reset')){model.start=0;model.count=90;}
    else if(control.hasAttribute('data-preview-ma')){const n=Number(control.dataset.previewMa);model.maVisible.has(n)?model.maVisible.delete(n):model.maVisible.add(n);}
    else{const tier=control.dataset.previewTier;model.visible.has(tier)?model.visible.delete(tier):model.visible.add(tier);}
    redraw(group,model);
  });
  document.addEventListener('input',event=>{if(!event.target.hasAttribute('data-preview-range'))return;const group=event.target.closest('[data-preview-group]'),model=models.get(group.dataset.previewGroup);model.count=Number(event.target.value);model.start=Math.min(model.start,90-model.count);redraw(group,model);});
  document.addEventListener('mousemove',event=>{const group=event.target.closest('[data-preview-group]');if(group)point(group,event);});
  document.addEventListener('mouseout',event=>{const group=event.target.closest('[data-preview-group]');if(group&&!group.contains(event.relatedTarget))group.querySelectorAll('.preview-pointer').forEach(p=>p.hidden=true);});
  document.addEventListener('wheel',event=>{const group=event.target.closest('[data-preview-group]');if(!group||!event.target.closest('[data-preview-plot]'))return;event.preventDefault();const model=models.get(group.dataset.previewGroup);model.count=Math.max(12,Math.min(90,model.count+(event.deltaY>0?6:-6)));model.start=Math.min(model.start,90-model.count);redraw(group,model);},{passive:false});
  let drag=null;
  document.addEventListener('pointerdown',event=>{const plot=event.target.closest('[data-preview-plot]');if(!plot)return;const group=plot.closest('[data-preview-group]'),model=models.get(group.dataset.previewGroup);drag={group,model,x:event.clientX,start:model.start,width:plot.getBoundingClientRect().width};});
  document.addEventListener('pointermove',event=>{if(!drag||!(event.buttons&1))return;drag.model.start=Math.max(0,Math.min(90-drag.model.count,drag.start-Math.round((event.clientX-drag.x)/drag.width*drag.model.count)));redraw(drag.group,drag.model);});
  document.addEventListener('pointerup',()=>{drag=null;});
  // Two independent, multi-axis market charts. Values and calendar are fictional.
  const trendModels=new Map();
  const trendSeries={
    indices:[['sh','上证指数','点','index','var(--ink)',''],['kc','科创50','点','index','var(--secondary)','6 4'],['super','大盘超大单','亿元','fund','var(--ink)','2 3'],['small','大盘小单','亿元','fund','var(--secondary)','9 3 2 3']],
    breadth:[['up','上涨家数','家','breadth','var(--up)',''],['down','下跌家数','家','breadth','var(--down)',''],['amount','成交额','亿元','amount','var(--ink)','6 4'],['main','主力净流入','亿元','fund','var(--secondary)','2 3']]
  };
  function trendData(date,state) {
    return samples({price:1},date).map((row,i)=>({date:row.date,sh:3770+i*.9+Math.sin(i*.3)*30,kc:1260+i*.7+Math.sin(i*.3+.8)*16,super:state==='missing'&&i===84?null:Math.sin(i*.49)*340-90,small:Math.sin(i*.41+2)*260+70,up:Math.round(2700+Math.sin(i*.44)*1400),down:Math.round(2511-Math.sin(i*.44)*1400),amount:17000+Math.sin(i*.27+1)*2200,main:Math.sin(i*.5+.8)*850-200}));
  }
  function trendGeometry(model) {
    const data=model.data.slice(model.start,model.end+1),series=trendSeries[model.kind],left=62,right=model.kind==='indices'?612:558,top=32,bottom=256;
    const bounds={fund:[-600,600],index:[900,4200],breadth:[0,5500],amount:[12000,22000]};
    const fundValues=series.filter(s=>s[3]==='fund'&&model.visible.has(s[0])).flatMap(s=>data.map(d=>d[s[0]])).filter(Number.isFinite),limit=Math.max(100,Math.ceil(Math.max(0,...fundValues.map(Math.abs))/100)*100);bounds.fund=[-limit,limit];
    return {data,series,left,right,top,bottom,bounds,x:i=>left+i*(right-left)/Math.max(1,data.length-1),y:(value,axis)=>bottom-(value-bounds[axis][0])/(bounds[axis][1]-bounds[axis][0])*(bottom-top)};
  }
  function trendSvg(model) {
    const g=trendGeometry(model),{data,series,left,right,top,bottom,bounds,x,y}=g;
    let drawing=Array.from({length:5},(_,i)=>`<path d="M${left} ${top+(bottom-top)*i/4}H${right}" stroke="var(--chart-grid)" fill="none"/>`).join('');
    drawing+=`<path class="trend-zero" d="M${left} ${y(0,'fund')}H${right}" stroke="var(--muted)" stroke-dasharray="4 4" opacity=".65" fill="none"/>`;
    series.forEach(([key,label,unit,axis,color,dash])=>{
      if(!model.visible.has(key))return;let segment=[];const segments=[];
      data.forEach((d,i)=>{if(Number.isFinite(d[key]))segment.push(`${x(i)},${y(d[key],axis)}`);else{if(segment.length)segments.push(segment);segment=[];}});if(segment.length)segments.push(segment);
      drawing+=segments.map(points=>`<polyline data-trend-line="${key}" points="${points.join(' ')}" stroke="${color}" stroke-width="1.8" stroke-dasharray="${dash||'none'}" fill="none"/>`).join('');
    });
    if(!model.visible.size)drawing+=`<text x="${(left+right)/2}" y="130" text-anchor="middle" fill="var(--secondary)" font-size="12">全部指标已隐藏，选择图例恢复</text>`;
    const axes=[['fund','资金 / 亿',left-8,'end'],...(model.kind==='indices'?[['index','指数 / 点',right+10,'start']]:[['amount','成交额 / 亿',right+10,'start'],['breadth','家数',right+80,'start']])];
    drawing+=axes.map(([axis,label,xx,anchor])=>`<g fill="var(--muted)" font-size="10" font-family="var(--numeric)" text-anchor="${anchor}"><text x="${xx}" y="14">${label}</text>${Array.from({length:5},(_,i)=>{const v=bounds[axis][1]-(bounds[axis][1]-bounds[axis][0])*i/4;return `<text x="${xx}" y="${top+(bottom-top)*i/4+3}">${Math.round(v).toLocaleString('en-US')}</text>`;}).join('')}</g>`).join('');
    const ticks=Math.min(6,data.length);drawing+=Array.from({length:ticks},(_,i)=>{const at=Math.round(i*(data.length-1)/Math.max(1,ticks-1));return `<text x="${x(at)}" y="278" fill="var(--muted)" font-family="var(--numeric)" font-size="10" text-anchor="middle">${data[at].date.slice(5).replace('.','-')}</text>`;}).join('');
    return `<svg viewBox="0 0 704 288" preserveAspectRatio="none" role="img" aria-label="${model.kind==='indices'?'指数与分层资金':'市场广度与主力资金'}多轴趋势，虚构示例">${drawing}</svg><i class="trend-pointer" hidden></i><div class="trend-tooltip" hidden></div>`;
  }
  function trendNavigator(model) {
    return `<span class="trend-selection" style="left:${model.start/89*100}%;width:${(model.end-model.start)/89*100}%"></span>${['start','end'].map(endpoint=>`<span class="trend-handle" style="left:${model[endpoint]/89*100}%" data-trend-handle="${endpoint}" tabindex="0" role="slider" aria-label="时间轴${endpoint==='start'?'开始':'结束'}日期" aria-valuemin="${endpoint==='start'?0:model.start+1}" aria-valuemax="${endpoint==='start'?model.end-1:89}" aria-valuenow="${model[endpoint]}" aria-valuetext="${model.data[model[endpoint]].date}"></span>`).join('')}`;
  }
  function renderMarketTrend(options) {
    const {key,kind,date,state='normal'}=options,old=trendModels.get(key),model={key,kind,date,state,data:trendData(date,state),start:80,end:89,visible:new Set(trendSeries[kind].map(s=>s[0]))};
    if(old&&old.date===date)Object.assign(model,{start:old.start,end:old.end,visible:old.visible});trendModels.set(key,model);
    const available=['normal','missing'].includes(state),title=kind==='indices'?'上证指数 · 科创50 · 大盘超大单 · 大盘小单':'上涨家数 · 下跌家数 · 成交额 · 主力净流入';
    const states={loading:['正在加载趋势示例','保留日期和图表位置。'],empty:['没有对应日期的趋势记录','选择其他示例日期或恢复示例。'],error:['暂时无法读取趋势','保留当前任务，可恢复示例重试。']};
    return `<section class="market-trend-card" data-market-trend="${escape(key)}"><div class="market-trend-heading"><h3>${title}</h3><div class="period-pills trend-shortcuts" role="group" aria-label="${title}区间">${[10,20,60,90].map(n=>`<button class="${model.end===89&&model.end-model.start+1===n?'active':''}" data-trend-days="${n}" aria-pressed="${model.end===89&&model.end-model.start+1===n}" ${available?'':'disabled'}>${n===90?'全部':'近'+n+'日'}</button>`).join('')}</div></div><div class="trend-chart-hint">${kind==='indices'?'指数右轴(点) · 资金左轴(亿元)':'家数右外轴 · 成交额右轴(亿元) · 资金左轴(亿元)'} · 虚线为资金零轴</div><div class="trend-legend" role="group" aria-label="${title}图例">${trendSeries[kind].map(([id,label,unit,axis,color,dash])=>`<button data-trend-series="${id}" aria-pressed="${model.visible.has(id)}" ${available?'':'disabled'}><svg viewBox="0 0 20 8" aria-hidden="true"><path d="M1 4H19" stroke="${color}" stroke-width="2" stroke-dasharray="${dash||'none'}"/></svg>${label}</button>`).join('')}</div>${available?`<div class="trend-plot" tabindex="0" role="group" aria-label="趋势图，可用左右键平移，加减键缩放">${trendSvg(model)}</div><div class="trend-navigator" aria-label="拖动时间窗口或两端调节区间">${trendNavigator(model)}</div><div class="trend-footer"><span class="trend-dates">${model.data[model.start].date} — ${model.data[model.end].date}</span><span>${state==='missing'?'部分示例缺失，图线断开':'虚构示例 · 拖动 / 滚轮缩放'}</span></div><div class="trend-live-readout" aria-live="off">悬停查看同日各项指标与单位</div>`:`<div class="trend-state"><span>${icon(state==='loading'?'refresh':'info')}</span><strong>${states[state][0]}</strong><p>${states[state][1]}</p>${state==='loading'?'':'<button class="button" data-ui-action="reset-view">恢复示例</button>'}</div>`}</section>`;
  }
  function updateTrend(card,model) {
    const focusedHandle=card.contains(document.activeElement)?document.activeElement.dataset.trendHandle:null;
    card.querySelector('.trend-plot').innerHTML=trendSvg(model);card.querySelector('.trend-navigator').innerHTML=trendNavigator(model);
    if(focusedHandle)card.querySelector(`[data-trend-handle="${focusedHandle}"]`).focus({preventScroll:true});
    card.querySelectorAll('[data-trend-days]').forEach(b=>{const active=model.end===89&&model.end-model.start+1===Number(b.dataset.trendDays);b.setAttribute('aria-pressed',String(active));b.classList.toggle('active',active);});
    card.querySelectorAll('[data-trend-series]').forEach(b=>b.setAttribute('aria-pressed',String(model.visible.has(b.dataset.trendSeries))));
    card.querySelector('.trend-dates').textContent=`${model.data[model.start].date} — ${model.data[model.end].date}`;
    card.querySelector('.trend-live-readout').textContent='悬停查看同日各项指标与单位';
  }
  function trendPoint(card,event) {
    const model=trendModels.get(card.dataset.marketTrend),plot=card.querySelector('.trend-plot'),rect=plot.getBoundingClientRect(),g=trendGeometry(model),pixel=(event.clientX-rect.left)/rect.width*704,progress=Math.max(0,Math.min(1,(pixel-g.left)/(g.right-g.left))),at=model.start+Math.round(progress*(model.end-model.start)),row=model.data[at],xx=(g.left+progress*(g.right-g.left))/704*rect.width;
    const readout=[row.date+' · 虚构示例',...trendSeries[model.kind].filter(s=>model.visible.has(s[0])).map(([key,label,unit])=>`${label} ${row[key]===null?'—':row[key].toLocaleString('en-US',{maximumFractionDigits:unit==='家'?0:2})} ${unit}`)];
    const pointer=plot.querySelector('.trend-pointer'),tip=plot.querySelector('.trend-tooltip');pointer.hidden=false;pointer.style.left=xx+'px';tip.hidden=false;tip.textContent=readout.join('\n');tip.style.left=Math.max(0,Math.min(rect.width-212,xx+12))+'px';card.querySelector('.trend-live-readout').textContent=readout.join(' · ');
  }
  function trendWindow(model,delta) {const count=model.end-model.start;model.start=Math.max(0,Math.min(89-count,model.start+delta));model.end=model.start+count;}
  document.addEventListener('click',event=>{const button=event.target.closest('[data-trend-days],[data-trend-series]'),card=button?.closest('[data-market-trend]');if(!card||button.disabled)return;const model=trendModels.get(card.dataset.marketTrend);if(button.dataset.trendDays){model.end=89;model.start=90-Number(button.dataset.trendDays);}else{const id=button.dataset.trendSeries;model.visible.has(id)?model.visible.delete(id):model.visible.add(id);}updateTrend(card,model);});
  document.addEventListener('mousemove',event=>{const plot=event.target.closest('.trend-plot');if(plot)trendPoint(plot.closest('[data-market-trend]'),event);});
  document.addEventListener('mouseout',event=>{const plot=event.target.closest('.trend-plot');if(plot&&!plot.contains(event.relatedTarget))plot.querySelectorAll('.trend-tooltip,.trend-pointer').forEach(e=>e.hidden=true);});
  document.addEventListener('wheel',event=>{const plot=event.target.closest('.trend-plot');if(!plot)return;event.preventDefault();const card=plot.closest('[data-market-trend]'),model=trendModels.get(card.dataset.marketTrend),count=Math.max(2,Math.min(90,model.end-model.start+1+(event.deltaY>0?4:-4)));model.start=Math.max(0,model.end-count+1);updateTrend(card,model);},{passive:false});
  let trendDrag=null;
  document.addEventListener('pointerdown',event=>{const target=event.target.closest('.trend-plot,.trend-navigator');if(!target)return;const card=target.closest('[data-market-trend]'),model=trendModels.get(card.dataset.marketTrend);trendDrag={card,model,x:event.clientX,start:model.start,end:model.end,width:target.getBoundingClientRect().width,endpoint:event.target.closest('[data-trend-handle]')?.dataset.trendHandle,navigator:target.classList.contains('trend-navigator')};event.preventDefault();target.setPointerCapture(event.pointerId);});
  document.addEventListener('pointermove',event=>{if(!trendDrag||!(event.buttons&1))return;const d=trendDrag,delta=Math.round((event.clientX-d.x)/d.width*(d.navigator?90:d.end-d.start+1));if(d.endpoint==='start')d.model.start=Math.max(0,Math.min(d.end-1,d.start+delta));else if(d.endpoint==='end')d.model.end=Math.min(89,Math.max(d.start+1,d.end+delta));else{const count=d.end-d.start;d.model.start=Math.max(0,Math.min(89-count,d.start+(d.navigator?delta:-delta)));d.model.end=d.model.start+count;}updateTrend(d.card,d.model);});
  document.addEventListener('pointerup',()=>{trendDrag=null;});document.addEventListener('pointercancel',()=>{trendDrag=null;});
  document.addEventListener('keydown',event=>{
    const handle=event.target.closest('[data-trend-handle]'),plot=event.target.closest('.trend-plot');
    if(handle&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
      event.preventDefault();const card=handle.closest('[data-market-trend]'),model=trendModels.get(card.dataset.marketTrend),endpoint=handle.dataset.trendHandle,min=endpoint==='start'?0:model.start+1,max=endpoint==='start'?model.end-1:89;
      const value=event.key==='Home'?min:event.key==='End'?max:model[endpoint]+(event.key==='ArrowLeft'?-1:1);
      model[endpoint]=Math.max(min,Math.min(max,value));updateTrend(card,model);return;
    }
    if(!plot||!['ArrowLeft','ArrowRight','+','-','='].includes(event.key))return;event.preventDefault();const card=plot.closest('[data-market-trend]'),model=trendModels.get(card.dataset.marketTrend);if(event.key==='ArrowLeft'||event.key==='ArrowRight')trendWindow(model,event.key==='ArrowLeft'?-1:1);else{const count=Math.max(2,Math.min(90,model.end-model.start+1+(event.key==='-'?4:-4)));model.start=Math.max(0,model.end-count+1);}updateTrend(card,model);
  });

  return {render,renderMarketTrend};
})();
