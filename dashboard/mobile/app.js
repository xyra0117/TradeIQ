/* TradeIQ mobile: same-origin GET only; no data persisted in browser storage. */
'use strict';
const Mobile = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  const fmt = (v, digits = 2) => finite(v) ? Number(v).toLocaleString('zh-CN', {minimumFractionDigits:digits,maximumFractionDigits:digits}) : '—';
  const pct = v => finite(v) ? `${Number(v)>0?'+':''}${fmt(v)}%` : '—';
  const tone = v => !finite(v) || Number(v)===0 ? 'neutral' : Number(v)>0?'up':'down';
  const cash = v => !finite(v) ? '—' : Math.abs(v)>=1e8 ? `${fmt(v/1e8)}亿` : Math.abs(v)>=1e4 ? `${fmt(v/1e4)}万` : fmt(v);
  const dateText = v => /^\d{8}$/.test(v||'') ? `${v.slice(0,4)}-${v.slice(4,6)}-${v.slice(6)}` : v||'—';
  function filterRows(items, query, sort, key) {
    const q = query.toLowerCase().trim();
    const result = items.filter(r => [r.name,r.code,r.ts_code,r.sector].some(v => String(v||'').toLowerCase().includes(q)));
    if (sort!=='default' && key) result.sort((a,b) => {
      if (!finite(a[key])) return finite(b[key])?1:0;
      if (!finite(b[key])) return -1;
      return (Number(a[key])-Number(b[key]))*(sort==='asc'?1:-1);
    });
    return result;
  }
  function movingAverage(rows, days) {
    return rows.map((_, i) => i<days-1 || rows.slice(i-days+1,i+1).some(r=>!finite(r.close)) ? null :
      rows.slice(i-days+1,i+1).reduce((sum,r)=>sum+Number(r.close),0)/days);
  }
  function candle(r) { return ['open','close','low','high'].every(k=>finite(r[k])) ? [r.open,r.close,r.low,r.high] : [null,null,null,null]; }
  return {esc,finite,fmt,pct,tone,cash,dateText,filterRows,movingAverage,candle};
})();
if (typeof module !== 'undefined') module.exports = Mobile;
if (typeof document !== 'undefined') boot();

function boot() {
  const {esc,finite,fmt,pct,tone,cash,dateText,filterRows,movingAverage,candle} = Mobile;
  const $ = id => document.getElementById(id);
  const names = {index:'核心指数',leaderboard:'阶段涨幅榜单',limitup:'涨停拆解明细',lz:'涨停合集',flow:'主力净流入','flow-sector':'板块资金流向',picks:'选股推荐',sectors:'板块合集',barometer:'盘面晴雨表'};
  const groups = {market:['index','leaderboard','flow'],picks:['picks'],more:['limitup','lz','sectors'],barometer:['barometer']};
  const state = {group:'market',module:'index',kind:'system',source:'eastmoney-push2',period:'5日',flowPeriod:'today',flowView:'stock',threshold:'3',date:'',allDates:false,availableDates:[],data:null,seq:0,limit:100,freqRange:'1个月'};
  // 板块资金流向并入「主力净流入」tab, 作为 flowView=sector 子页; 后端仍走 flow-sector 模块
  const activeModule = () => state.module==='flow'&&state.flowView==='sector' ? 'flow-sector' : state.module;
  let requestController, modalController, modalSeq=0, modalChart, modalFlowChart, modalHistChart, flowChart, chartData, flowChartData, returnSector, lastFocus;
  const endpoint = (path, args={}) => '/api/mobile/'+path+'?'+new URLSearchParams(Object.entries(args).filter(([,v])=>v!==''&&v!==null&&v!==undefined));
  async function api(path,args,signal) {
    const response = await fetch(endpoint(path,args), {method:'GET',cache:'no-store',signal,credentials:'same-origin'});
    if (!response.ok) throw new Error(response.status===503?'电脑数据库暂不可用，请在电脑端检查。':`读取失败（${response.status}）`);
    return response.json();
  }
  function connection(text, error=false) { $('connection').textContent=text; $('connection').classList.toggle('error',error); }
  function disposeCharts(){ if(flowChart){flowChart.dispose();flowChart=null;} }
  function updateDateNav(){
    const dates=state.availableDates||[];const prev=$('date-prev'),next=$('date-next'),all=$('date-all');
    if(!prev||!next||!all)return;
    if(state.allDates){
      all.setAttribute('aria-pressed','true');
      if(dates.length){
        const cur=state.date||dates[0];
        const i=dates.indexOf(cur);
        prev.disabled=i<=0||i===-1;
        next.disabled=i===-1||i>=dates.length-1;
      }else{prev.disabled=next.disabled=true;}
    }else{
      all.setAttribute('aria-pressed','false');
      prev.disabled=next.disabled=true;
    }
  }
  function params(){return {date:state.date,kind:state.kind,source:state.source,period:activeModule()==='flow'?state.flowPeriod:state.period,threshold_yi:state.threshold};}
  function option(id,label,options,current){return `<label class="sr-label" for="${id}">${label}</label><select id="${id}">${options.map(([v,n])=>`<option value="${v}" ${v===current?'selected':''}>${n}</option>`).join('')}</select>`;}
  function controls(){
    const mod=activeModule();
    // source 在 flow(数据源) 和 flow-sector(板块类型) 间复用, 切子页时纠回各自合法值
    if(mod==='flow-sector'&&!['industry','concept'].includes(state.source))state.source='industry';
    if(mod==='flow'&&!['eastmoney-push2','ths','all'].includes(state.source))state.source='eastmoney-push2';
    $('title').textContent=names[state.module];
    $('eyebrow').textContent={market:'MARKET OVERVIEW',picks:'STOCK SELECTION',more:'MARKET EXPLORER',barometer:'MARKET BAROMETER'}[state.group];
    document.querySelectorAll('[data-nav]').forEach(b=>{b.classList.toggle('active',b.dataset.nav===state.group);b.setAttribute('aria-current',b.dataset.nav===state.group?'page':'false');});
    // 晴雨 tab 显示近 60 天历史, 不需要单日选择, 隐藏顶部日期工具栏
    document.querySelector('.toolbar.date-toolbar')?.toggleAttribute('hidden', state.group==='barometer');
    $('module-tabs').innerHTML=groups[state.group].length>1?groups[state.group].map(m=>`<button class="chip ${m===state.module?'active':''}" data-module="${m}" aria-pressed="${m===state.module}">${names[m]}</button>`).join(''):'';
    let html='';
    if(state.module==='leaderboard') html=option('period','周期',[['5日','5日涨幅'],['10日','10日涨幅'],['20日','20日涨幅']],state.period);
    if(state.module==='flow'){
      html=[['stock','个股'],['sector','板块']].map(([v,n])=>`<button class="freq-tab ${state.flowView===v?'active':''}" data-flow-view="${v}" aria-pressed="${state.flowView===v}">${n}</button>`).join('');
      if(state.flowView==='sector'){
        html+=option('source','板块类型',[['industry','行业'],['concept','概念']],['industry','concept'].includes(state.source)?state.source:'industry');
      }else{
        html+=option('source','来源',[['eastmoney-push2','东方财富 · 主力'],['ths','同花顺 · 总净额'],['all','东财全部来源']],state.source);
        html+=option('flowPeriod','区间',[['today','当日'],['3d','3日累计'],['5d','5日累计'],['10d','10日累计'],['20d','20日累计']],state.flowPeriod);
      }
    }
    if(state.module==='picks') html=option('kind','选股类型',[['system','系统推荐 · 东财'],['ths','系统推荐 · 同花顺'],['conditional','条件选股 · 已保存快照'],['conditional-new','条件选股新 · 已保存快照'],['conditional-preferred','条件优选 · 已保存快照'],['combined','组合选股 · 已保存快照'],['conditional-ths','同花顺条件 · 已保存快照'],['combined-ths','同花顺组合 · 已保存快照']],state.kind);
    if(state.module==='picks' && ['conditional','combined','conditional-ths','combined-ths'].includes(state.kind)) html+=option('threshold','资金门槛',[['0','≥ 0 亿'],['1','≥ 1 亿'],['3','≥ 3 亿'],['5','≥ 5 亿'],['10','≥ 10 亿']],state.threshold);
    $('options').innerHTML=html;
    $('filterbar').hidden=state.module==='index' || state.module==='barometer';
    for(const id of ['period','source','kind','threshold','flowPeriod']) if($(id)) $(id).addEventListener('change',()=>{state[id]=$(id).value;if(id!=='threshold'){state.date='';state.allDates=false;}controls();load(true);});
    updateDateNav();
  }
  async function load(dates=false){
    const seq=++state.seq;
    if(requestController)requestController.abort();
    requestController=new AbortController();
    const controller=requestController;
    const timeout=setTimeout(()=>controller.abort('timeout'),20000);
    disposeCharts();state.data=null;state.limit=100;
    $('refresh').disabled=true;$('content').setAttribute('aria-busy','true');
    $('content').innerHTML='<div class="empty">正在读取已保存数据…</div>';
    $('data-note').textContent='';connection('正在连接电脑…');
    try{
      if(dates){
        const result=await api('dates',{module:activeModule(),kind:state.kind,source:state.source},controller.signal);
        if(seq!==state.seq)return;
        state.availableDates = result.dates || [];
        // 全部日期开关打开 → 维持当前 date; 否则回到"最新已保存"
        const dateInput = $('date');
        if(state.availableDates.length){
          dateInput.min = dateText(state.availableDates.at(-1));
          dateInput.max = dateText(state.availableDates[0]);
        }
        if(!state.allDates){
          state.date = '';
          dateInput.value = '';
        }else if(state.date && !state.availableDates.includes(state.date)){
          state.date = state.availableDates[0] || '';
          dateInput.value = state.date ? dateText(state.date) : '';
        }else{
          dateInput.value = state.date ? dateText(state.date) : '';
        }
        updateDateNav();
      }
      const result=await api('modules/'+activeModule(),params(),controller.signal);
      if(seq!==state.seq)return;
      state.data=result;
      $('date-note').textContent=result.date?`显示 ${dateText(result.date)}`:'暂无日期';
      $('data-note').textContent=state.module==='barometer'?'':(result.note||'仅展示电脑已保存的数据，不代表当前实时行情。')+(result.saved_at?' 快照保存于 '+result.saved_at:'');
      connection('已连接电脑 · 已保存数据 '+dateText(result.date));render();
    }catch(e){
      if(seq!==state.seq)return;
      const text=controller.signal.reason==='timeout'?'连接超时，请确认电脑和 Tailscale 在线。':e.message.startsWith('读取失败')||e.message.startsWith('电脑数据库')?e.message:'无法连接电脑，请检查电脑是否开机、联网，以及 Tailscale 是否已连接。';
      connection(text,true);$('date-note').textContent='未连接';
      $('content').innerHTML='<div class="empty">'+esc(text)+'<br>连接恢复后点击右上角刷新。</div>';
    }finally{clearTimeout(timeout);if(seq===state.seq){$('refresh').disabled=false;$('content').setAttribute('aria-busy','false');}}
  }
  const col = (key,label,format='text')=>({key,label,format});
  function columns(){
    switch(activeModule()){
      case 'leaderboard':return [col('change','阶段涨幅','pct'),col('close','收盘价','num'),col('rank','排名','int'),col('reason','原因')];
      case 'limitup':return [col('sector','板块'),col('streak','连板'),col('time','涨停时间'),col('marketCap','流通市值 / 亿','num'),col('change_pct','当日涨幅','pct'),col('keyword','关键词')];
      case 'lz':return [col('change_pct','已存涨幅','pct'),col('sealed','封板','bool'),col('broken','曾炸板','bool'),col('first_limit_time','首次涨停'),col('last_seen_time','最后扫描'),col('market','市场')];
      case 'flow':
        if(state.flowPeriod!=='today')
          return state.source==='ths'
            ?[col('sum_net','区间净流入','cash'),col('flow_days','覆盖天数','int'),col('close','收盘价','num'),col('change_pct','涨幅','pct')]
            :[col('sum_main_net','区间主力净流入','cash'),col('sum_super_net','超大单','cash'),col('sum_big_net','大单','cash'),col('sum_mid_net','中单','cash'),col('sum_small_net','小单','cash'),col('flow_days','覆盖天数','int'),col('close','收盘价','num'),col('change_pct','涨幅','pct')];
        return state.source==='ths'?[col('net','总净流入','cash'),col('change_pct','涨幅','pct'),col('close','收盘价','num'),col('turnover_pct','换手率','pct')]:[col('main_net_inflow','主力净流入','cash'),col('main_net_pct','主力占比','pct'),col('change_pct','涨幅','pct'),col('close','收盘价','num'),col('super_net','超大单','cash'),col('big_net','大单','cash'),col('source','来源')];
      case 'flow-sector':
        return [col('main_net_inflow','主力净流入','cash'),col('main_net_pct','主力占比','pct'),col('change_pct','涨跌幅','pct'),col('close','收盘价','num'),col('super_net','超大单','cash'),col('big_net','大单','cash'),col('lead_stock_name','领涨股')];
      case 'picks':
        if(['conditional','combined','conditional-ths','combined-ths'].includes(state.kind))return [col('close','收盘价','num'),col('change_pct','涨幅','pct'),col('sum_3d','3日累计','cash'),col('sum_5d','5日累计','cash'),col('sum_10d','10日累计','cash'),col('sum_20d','20日累计','cash')];
        if(state.kind==='conditional-new')return [col('close','收盘价','num'),col('change_pct','涨幅','pct'),col('sum_main_5d','5日主力','cash'),col('sum_super_5d','5日超大单','cash'),col('sum_mid_small_5d','5日中单小单','cash'),col('volume_shrink_pct','缩量比例','pct'),col('ma10','MA10','num')];
        if(state.kind==='conditional-preferred')return [col('close','收盘价','num'),col('change_pct','涨幅','pct'),col('preferred_strength5','5日主力占比','pct'),col('preferred_small_net','当日小单','cash'),col('preferred_small_pct','当日小单占比','pct'),col('preferred_priority','优先级','num')];
        return [col('close','收盘价','num'),col('change_pct','涨幅','pct'),col('score','评分','num'),col(state.kind==='ths'?'ths_net_today':'main_net_today',state.kind==='ths'?'今日总净额':'今日主力','cash'),col('ma5','MA5','num'),col('ma10','MA10','num'),col('signal_type','信号')];
      default:return [col('close','收盘价','num'),col('change_pct','涨幅','pct')];
    }
  }
  function cell(r,c){
    const v=r[c.key];
    const str=c.format==='pct'?pct(v):c.format==='num'?fmt(v):c.format==='int'?fmt(v,0):c.format==='cash'?cash(v):c.format==='date'?dateText(v):c.format==='bool'?(v===1?'是':v===0?'否':'—'):v??'—';
    const colored=c.format==='pct'||['profit','main_net_inflow','net','super_net','big_net','sum_net','sum_main_net','sum_super_net','sum_big_net','sum_mid_net','sum_small_net'].includes(c.key);
    return `<td class="${colored?tone(v):''}">${esc(str)}</td>`;
  }
  function table(items,cols,limit=100,firstLabel='股票'){
    if(!items.length)return '<div class="empty">没有符合条件的已保存数据。<br>缺失数据请在电脑端更新。</div>';
    return `<div class="table-wrap"><table><thead><tr><th scope="col">${firstLabel}</th>${cols.map(c=>`<th scope="col">${c.label}</th>`).join('')}</tr></thead><tbody>${items.slice(0,limit).map(r=>{
      const raw=r.ts_code||r.code||'';
      const isStock=/^\d{6}\.(SH|SZ|BJ)$/.test(raw);
      const label=esc(r.name||raw||'—');
      const first=isStock?`<button class="stock-link" data-stock="${esc(raw)}" data-name="${esc(r.name||raw)}">${label}<span class="code">${esc(raw)}</span></button>`:`<span class="stock-plain">${label}<span class="code">${esc(raw)}</span></span>`;
      return `<tr><td>${first}</td>${cols.map(c=>cell(r,c)).join('')}</tr>`;
    }).join('')}</tbody></table></div><div class="table-hint"><span>共 ${items.length} 条 · 已显示 ${Math.min(limit,items.length)} 条</span><span>左右滑动查看更多 →</span></div>`;
  }
  function metric(label,value,cls=''){return `<div class="metric"><label>${esc(label)}</label><strong class="${cls}">${esc(value)}</strong></div>`;}
  function render(){
    if(!state.data)return;disposeCharts();
    const d=state.data;
    try {
    if(state.module==='barometer'){ renderBarometer(d); return; }
    if(state.module==='index'){
      $('content').innerHTML=(d.items.length?`<div class="index-grid">${d.items.map(r=>`<button class="index-card" data-index="${esc(r.ts_code)}" data-name="${esc(r.name)}"><p class="index-name">${esc(r.name)}</p><span class="code">${esc(r.ts_code)}</span><strong class="index-price ${tone(r.change_pct)}">${fmt(r.close)}</strong><span class="index-change ${tone(r.change_pct)}">${pct(r.change_pct)}</span><div class="index-caption">成交额 ${fmt(r.volume)} 亿 <span>↗</span></div></button>`).join('')}</div>`:'<div class="empty">'+esc(d.message)+'</div>');
      if(d.overview){const o=d.overview;$('content').insertAdjacentHTML('beforeend',`<div class="section-label">盘面概览<small>与所选日期一致</small></div><div class="metrics">${metric('两市成交额 / 亿',fmt(o.totalVolume))}${metric('上涨家数',fmt(o.upCount,0),'up')}${metric('下跌家数',fmt(o.downCount,0),'down')}${metric('涨停',fmt(o.limitUp,0),'up')}${metric('跌停',fmt(o.limitDown,0),'down')}${metric('平盘',fmt(o.flatCount,0))}</div>`);}
      if(d.market_flow){$('content').insertAdjacentHTML('beforeend',`<div class="section-label">沪深资金流向<small>${d.market_flow.complete?'收盘快照':'未完整收盘'}</small></div><div class="chart flow-chart" id="flow-chart"></div>`);drawFlow(d.market_flow);}
      if(d.overview_history?.length)renderOverviewHistory(d.overview_history);
      return;
    }
    if(state.module==='sectors'){
      const items=filterRows(d.items,$('search').value,$('sort').value,'hot_count');
      $('content').innerHTML=items.map(r=>`<button class="sector-card" data-sector="${esc(r.sector)}"><em>${fmt(r.hot_count,0)} 次</em><strong>${esc(r.sector)}</strong><small>${fmt(r.stock_count,0)} 只股票 · 最近涨停 ${esc(dateText(r.latest_date))}</small></button>`).join('')+(!items.length?'<div class="empty">没有符合条件的板块数据。</div>':'');return;
    }
    const cols=columns();
    const mod=activeModule();
    const key={leaderboard:'change',limitup:'change_pct',lz:'change_pct',flow:state.flowPeriod!=='today'?(state.source==='ths'?'sum_net':'sum_main_net'):(state.source==='ths'?'net':'main_net_inflow'),'flow-sector':'main_net_inflow',picks:state.kind==='conditional-new'?'sum_main_5d':state.kind==='conditional-preferred'?'preferred_strength5':state.kind==='system'||state.kind==='ths'?'score':'change_pct'}[mod];
    const items=filterRows(d.items,$('search').value,$('sort').value,key);
    let summary='';
    let flowChartHtml='';
    if(mod==='flow'&&d.market_flow)flowChartHtml=`<div class="section-label">沪深资金流向<small>${d.market_flow.complete?'收盘快照':'未完整收盘'}</small></div><div class="chart flow-chart" id="flow-chart"></div>`;
    let bodyHtml;
    if(mod==='limitup')bodyHtml=renderLimitupGrouped(items);
    else bodyHtml=table(items,cols,state.limit,mod==='flow-sector'?'板块':'股票')+(items.length>state.limit?'<button class="load-more" id="load-more">再显示 100 条</button>':'');
    let histEntry='';
    if(mod==='picks'&&state.kind==='conditional-preferred')histEntry=`<button class="hist-entry" data-preferred-history="1"><span>📊 历史涨幅分布</span><small>历次入选 T+1~T+5 最高涨幅分档占比</small></button>`;
    $('content').innerHTML=flowChartHtml+summary+histEntry+bodyHtml;
    if(mod==='flow'&&d.market_flow)drawFlow(d.market_flow);
    if(mod==='leaderboard'&&d.frequency)renderFreq(d.frequency);
    } catch(e) {
      const msg = (e && e.stack) ? e.stack : String(e);
      console.error('render failed:', e);
      $('content').innerHTML = `<div class="empty" style="white-space:pre-wrap;text-align:left;font-family:monospace;font-size:11px;max-height:60vh;overflow:auto;color:#ff8a9a">渲染出错：\n${msg.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</div>`;
    }
  }
  // 上榜频次: 当前周期榜, 1/6/12 个月区间切换 (跟桌面端同口径)
  function renderFreq(freq){
    $('freq-section')?.remove();
    const range=state.freqRange;
    const items=[...freq.items].sort((a,b)=>b[range]-a[range]).filter(s=>s[range]>0).slice(0,20);
    const max=items.length?items[0][range]:1;
    const tabs=freq.tabs.map(t=>`<button class="freq-tab ${t===range?'active':''}" data-freq-range="${t}">${t}</button>`).join('');
    const rows=items.map(s=>{
      const ratio=max>0?s[range]/max:0;
      const w=(ratio*100).toFixed(0);
      const tier=ratio>=.8?'t1':ratio>=.5?'t2':ratio>=.25?'t3':'t4';
      return `<button class="freq-cell stock-link ${tier}" data-stock="${esc(s.code)}" data-name="${esc(s.name)}">
        <span class="freq-name">${esc(s.name)}</span>
        <span class="freq-num">${s[range]}</span>
        <span class="freq-bar-wrap"><span class="freq-bar" style="width:${w}%"></span></span>
      </button>`;
    }).join('');
    $('content').insertAdjacentHTML('beforeend',
      `<div id="freq-section"><div class="section-label">上榜频次统计<small>${state.period}榜 · 各股在区间内出现的次数</small></div>
      <div class="freq-tabs">${tabs}</div>
      ${rows?`<div class="freq-grid">${rows}</div>`:'<div class="empty">区间内没有上榜记录。</div>'}</div>`);
  }
  // 涨停拆解明细: 按板块分组; 板块顺序按成员数 desc (公告/其他置尾),
  // 板块内按「连板数 desc, 涨停时间 asc」, 跟桌面端 renderLUTable 同口径
  function renderLimitupGrouped(items){
    if(!items.length)return '<div class="empty">没有符合条件的涨停数据。</div>';
    const boardCount=t=>{
      if(t==='首板'||!t)return -1;
      const m=String(t).match(/(\d+)天(\d+)板/);
      return m?parseInt(m[2]):0;
    };
    const toMin=t=>{
      const p=String(t||'0:0').split(':').map(Number);
      return p[0]*60+p[1];
    };
    const buckets=new Map();
    for(const r of items){
      const sec=r.sector||'其他';
      if(!buckets.has(sec))buckets.set(sec,[]);
      buckets.get(sec).push(r);
    }
    for(const list of buckets.values()){
      list.sort((a,b)=>{
        const sa=boardCount(a.streak),sb=boardCount(b.streak);
        if(sb!==sa)return sb-sa;
        return toMin(a.time)-toMin(b.time);
      });
    }
    const ordered=[...buckets.entries()].sort((a,b)=>{
      const ta=a[0]==='公告'||a[0]==='其他'?1:0;
      const tb=b[0]==='公告'||b[0]==='其他'?1:0;
      if(ta!==tb)return ta-tb;
      return b[1].length-a[1].length;
    });
    return ordered.map(([sec,list])=>{
      const rows=list.map(r=>{
        const sk=String(r.streak||'').trim();
        const cell=sk?`<span class="streak-tag${sk==='首板'?'':' streak-multi'}">${esc(sk)}</span>`:'—';
        return `<tr><td><button class="stock-link" data-stock="${esc(r.ts_code||'')}" data-name="${esc(r.name||'')}">${esc(r.name||'')}<span class="code">${esc(r.code||'')}</span></button></td><td>${cell}</td><td>${esc(r.time||'—')}</td><td class="up">${pct(r.change_pct)}</td><td>${esc(r.keyword||'')}</td></tr>`;
      }).join('');
      return `<div class="limitup-group">
        <div class="limitup-group-head"><strong>${esc(sec)}</strong><span>${list.length} 只</span></div>
        <div class="table-wrap"><table><thead><tr><th scope="col">股票</th><th scope="col">连板</th><th scope="col">涨停时间</th><th scope="col">当日涨幅</th><th scope="col">关键词</th></tr></thead><tbody>${rows}</tbody></table></div>
      </div>`;
    }).join('')+'<div class="table-hint"><span>左右滑动查看更多 →</span></div>';
  }
  // 盘面晴雨表: 复用盘面数据历史的双行吸顶表格; 概念板块 3 组×3 子列 + 涨幅榜 3 组×2 子列
  // 当前为 UI 层清空态: 表头字段全部保留, body 单元格留空, 后端/数据库完全不动
  function renderBarometer(d){
    const items=d.items||[];
    if(!items.length){$('content').innerHTML='<div class="empty">该日期没有已保存数据，请在电脑端更新。</div>';return;}
    const sgn=v=>finite(v)?(Number(v)>0?'+':'')+fmt(v):'—';
    const money=v=>finite(v)?fmt(v):'—';
    // 基础列
    const base=[
      ['date','日期',r=>dateText(r.date)],
      ['turnover','成交额 / 亿',r=>fmt(r.turnover)],
      ['shanghai_index','上证',r=>fmt(r.shanghai_index)],
      ['change_rate','涨跌幅',r=>`<span class="${tone(r.change_rate)}">${pct(r.change_rate)}</span>`],
      ['kechuang50_index','科创50',r=>fmt(r.kechuang50_index)],
      ['kechuang50_rate','科创50 涨跌',r=>`<span class="${tone(r.kechuang50_rate)}">${pct(r.kechuang50_rate)}</span>`],
      ['chinext_index','创业板指',r=>fmt(r.chinext_index)],
      ['chinext_rate','创业板涨跌',r=>`<span class="${tone(r.chinext_rate)}">${pct(r.chinext_rate)}</span>`],
      ['main_money','主力 / 亿',r=>`<span class="${tone(r.main_money)}">${sgn(r.main_money)}</span>`],
      ['big_money','超大单 / 亿',r=>`<span class="${tone(r.super_money)}">${sgn(r.super_money)}</span>`],
      ['small_money','小单 / 亿',r=>`<span class="${tone(r.small_money)}">${sgn(r.small_money)}</span>`],
      ['advancers','上涨',r=>`<span class="up">${fmt(r.advancers,0)}</span>`],
      ['decliners','下跌',r=>`<span class="down">${fmt(r.decliners,0)}</span>`],
    ];
    // 概念板块: TOP1/2/3 三组, 每组 3 子列
    const conceptGroups=[
      {cls:'top1',n:'TOP1',name:r=>r.today_top1_concept,rate:r=>r.today_top1_concept_rate,money:r=>r.today_top1_concept_money},
      {cls:'top2',n:'TOP2',name:r=>r.today_top2_concept,rate:r=>r.today_top2_concept_rate,money:r=>r.today_top2_concept_money},
      {cls:'top3',n:'TOP3',name:r=>r.today_top3_concept,rate:r=>r.today_top3_concept_rate,money:r=>r.today_top3_concept_money},
    ];
    // 涨幅榜: 5/10/20 日 三组, 每组 2 子列 (板块 / 个股)
    const hotGroups=[
      {cls:'hot5', n:'5日', sector:r=>r.hot5_sector,  stock:r=>r.hot5_stock},
      {cls:'hot10',n:'10日',sector:r=>r.hot10_sector, stock:r=>r.hot10_stock},
      {cls:'hot20',n:'20日',sector:r=>r.hot20_sector, stock:r=>r.hot20_stock},
    ];
    // 单层表头: 基础列 + 概念 TOP1/2/3 各 3 列 + 涨幅榜 5/10/20 日各 2 列 = 11 + 9 + 6 = 26 列
    const baseHeads=base.map(c=>`<th scope="col">${esc(c[1])}</th>`).join('');
    const conceptHeads=conceptGroups.flatMap(g=>[
      `<th scope="col" class="concept-sub ${g.cls}">${esc(g.n)} 板块名称</th>`,
      `<th scope="col" class="concept-sub ${g.cls}">${esc(g.n)} 板块涨幅</th>`,
      `<th scope="col" class="concept-sub ${g.cls}">${esc(g.n)} 板块主力 / 亿</th>`,
    ]).join('');
    const hotHeads=hotGroups.flatMap(g=>[
      `<th scope="col" class="concept-sub ${g.cls}">${esc(g.n)}榜 个股</th>`,
      `<th scope="col" class="concept-sub ${g.cls}">${esc(g.n)}榜 板块名称</th>`,
    ]).join('');
    // body 列渲染 (基础 11 + 概念 9 + 涨幅榜 6 = 26)
    const conceptCols=conceptGroups.flatMap(g=>[
      [`concept-${g.cls}-name`,null,r=>esc(g.name(r)||'')],
      [`concept-${g.cls}-rate`,null,r=>{const v=g.rate(r);return `<span class="${tone(v)}">${pct(v)}</span>`;}],
      [`concept-${g.cls}-money`,null,r=>{const v=g.money(r);return `<span class="${tone(v)}">${money(v)}</span>`;}],
    ]);
    const hotCols=hotGroups.flatMap(g=>[
      [`${g.cls}-stock`, null,r=>esc(g.stock(r)||'')],
      [`${g.cls}-sector`,null,r=>esc(g.sector(r)||'')],
    ]);
    const allRenderCols=[...base, ...conceptCols, ...hotCols];
    // 数据填充版: cellRow 用每列的 fn 渲染数据; CSS gradient 网格线 (避开 Safari sticky 渲染不稳)
    const cellRow=r=>allRenderCols.map(([, ,fn])=>`<td>${fn(r)}</td>`).join('');
    // 找相近: barometer 关键指标 (上证/科创/创业板/成交/主力/超大/涨跌家数), 跟盘面数据历史同口径
    const simFieldsDef=[['sh','上证',r=>r.shanghai_index,30],['shRate','上证涨跌',r=>r.change_rate,.5],['kc','科创50',r=>r.kechuang50_index,30],['cy','创业板',r=>r.chinext_index,30],['turnover','成交额',r=>r.turnover,600],['main','主力',r=>r.main_money,30],['super','超大单',r=>r.super_money,30],['advancers','上涨',r=>r.advancers,300],['decliners','下跌',r=>r.decliners,300]];
    let simFields=new Set(),simTol=1,simHits=[],simIdx=-1;
    const [latest,...hist]=items;
    const active=simFieldsDef.filter(([k])=>simFields.has(k));
    const hitDates=new Set();
    if(active.length){
      items.slice(1).forEach(r=>{
        if(active.every(([, ,get,tol])=>{const v=get(r),t=get(latest);return finite(v)&&finite(t)&&Math.abs(Number(v)-Number(t))<=tol*simTol;}))hitDates.add(r.date);
      });
    }
    simHits=items.filter(r=>hitDates.has(r.date)).map(r=>r.date);simIdx=-1;
    const chips=simFieldsDef.map(([k,label])=>`<button class="sim-chip ${simFields.has(k)?'on':''}" data-sim="${k}" aria-pressed="${simFields.has(k)}">${label}</button>`).join('');
    $('content').innerHTML=`<div class="ov-vert-outer"><div class="sim-bar"><span>找相近</span>${chips}<select id="bar-sim-tol" aria-label="相似阈值">${[[.5,'严格'],[1,'适中'],[2,'宽松']].map(([v,n])=>`<option value="${v}" ${Number(v)===simTol?'selected':''}>${n}</option>`).join('')}</select><span id="bar-sim-count" class="sim-count">${active.length?simHits.length+' 天相近':''}</span><button id="bar-sim-jump" class="sim-jump" ${active.length?'':'hidden'}>下一个 (0/${simHits.length})</button></div><table id="ov-hist-wrap"><thead><tr class="ov-head">${baseHeads}${conceptHeads}${hotHeads}</tr></thead><tbody><tr class="ov-latest" data-date="${esc(latest.date)}">${cellRow(latest)}</tr>${hist.map(r=>`<tr class="${hitDates.has(r.date)?'sim-hit':''}" data-date="${esc(r.date)}">${cellRow(r)}</tr>`).join('')}</tbody></table></div>
      <div class="table-hint"><span>左右滑动查看更多 →</span></div>`;
    const wrap=document.querySelector('.ov-vert-outer');
    _bindVerticalOnlyScroll(wrap);
    _stackStickyThead(wrap);
    $('bar-sim-tol').addEventListener('change',e=>{simTol=parseFloat(e.target.value);renderBarometer(d);});
    document.querySelectorAll('.sim-chip[data-sim]').forEach(b=>{b.addEventListener('click',()=>{const k=b.dataset.sim;if(simFields.has(k))simFields.delete(k);else simFields.add(k);renderBarometer(d);});});
    const jump=$('bar-sim-jump');
    if(active.length)jump.addEventListener('click',()=>{
      if(!simHits.length)return;
      simIdx=(simIdx+1)%simHits.length;
      document.querySelector(`#ov-hist-wrap tbody tr[data-date="${simHits[simIdx]}"]`)?.scrollIntoView({block:'center',behavior:'smooth'});
      jump.textContent=`下一个 (${simIdx+1}/${simHits.length})`;
    });
  }
  function drawFlow(d){
    if(!window.echarts)return;
    flowChart=echarts.init($('flow-chart'),null,{renderer:'canvas'});
    const defs=[['main','主力','#ffbd69'],['super','超大单','#ff6471'],['big','大单','#73b7ff'],['mid','中单','#b699ff'],['small','小单','#40d5a0']];
    flowChart.setOption({textStyle:{color:'#8a9bb0'},legend:{data:defs.map(x=>x[1]),textStyle:{color:'#8a9bb0',fontSize:10},top:10},tooltip:{trigger:'axis',renderMode:'richText'},grid:{left:48,right:12,top:45,bottom:30},xAxis:{type:'category',data:d.times,axisLabel:{fontSize:9}},yAxis:{type:'value',name:'',axisLabel:{fontSize:9,formatter:v=>fmt(v,0)+'亿'},splitLine:{lineStyle:{color:'#233044'}}},series:defs.map(([key,name,color])=>({name,type:'line',data:(d.series[key]||[]).map(v=>finite(v)?v/1e8:null),showSymbol:false,lineStyle:{width:1.5,color},itemStyle:{color}}))});
  }
  // 盘面数据历史「找相近」: 点亮字段全落在最新日 ± 容差×倍率 内才算命中 (跟桌面端同口径)
  const OV_SIM_FIELDS=[['sh','上证指数',r=>r.sh_change,.5],['totalVolume','成交额',r=>r.totalVolume,600],['emMain','主力-东财',r=>r.emMain,50],['emSuper','超大单-东财',r=>r.emSuper,50],['netFlow','净流入-T',r=>r.netFlow,50],['upCount','上涨',r=>r.upCount,300],['downCount','下跌',r=>r.downCount,300],['limitUp','涨停',r=>r.limitUp,10],['limitDown','跌停',r=>r.limitDown,10]];
  let simFields=new Set(),simTol=1,simHits=[],simIdx=-1;
  function renderOverviewHistory(rows){
    const sgn=v=>finite(v)?(Number(v)>0?'+':'')+fmt(v):'—';
    const active=OV_SIM_FIELDS.filter(([k])=>simFields.has(k));
    const hitDates=new Set();
    if(active.length){
      const latest=rows[0];
      rows.slice(1).forEach(r=>{
        if(active.every(([, ,get,tol])=>{const v=get(r),t=get(latest);return finite(v)&&finite(t)&&Math.abs(Number(v)-Number(t))<=tol*simTol;}))hitDates.add(r.date);
      });
    }
    simHits=rows.filter(r=>hitDates.has(r.date)).map(r=>r.date);simIdx=-1;
    const chips=OV_SIM_FIELDS.map(([k,label])=>`<button class="sim-chip ${simFields.has(k)?'on':''}" data-sim="${k}" aria-pressed="${simFields.has(k)}">${label}</button>`).join('');
    const cellRow=r=>`<td>${esc(dateText(r.date))}</td><td class="${tone(r.sh_change)}">${finite(r.sh_change)?pct(r.sh_change):'—'}</td><td>${fmt(r.totalVolume)}</td><td class="${tone(r.emMain)}">${sgn(r.emMain)}</td><td class="${tone(r.emSuper)}">${sgn(r.emSuper)}</td><td class="${tone(r.netFlow)}">${sgn(r.netFlow)}</td><td class="up">${fmt(r.upCount,0)}</td><td class="down">${fmt(r.downCount,0)}</td><td>${fmt(r.limitUp,0)}</td><td>${fmt(r.limitDown,0)}</td>`;
    const headCols=`<th scope="col">日期</th><th scope="col">上证指数</th><th scope="col">成交额</th><th scope="col">主力-东财</th><th scope="col">超大单-东财</th><th scope="col">净流入-T</th><th scope="col">上涨</th><th scope="col">下跌</th><th scope="col">涨停</th><th scope="col">跌停</th>`;
    const latest=rows[0], hist=rows.slice(1);
    $('content').insertAdjacentHTML('beforeend',`<div id="ov-hist-section"><div class="section-label">盘面数据历史<small>近 ${rows.length} 个交易日 · 资金单位为亿</small></div><div class="sim-bar"><span>找相近</span>${chips}<select id="sim-tol" aria-label="相似阈值">${[[.5,'严格'],[1,'适中'],[2,'宽松']].map(([v,n])=>`<option value="${v}" ${Number(v)===simTol?'selected':''}>${n}</option>`).join('')}</select><span id="sim-count" class="sim-count">${active.length?simHits.length+' 天相近':''}</span><button id="sim-jump" class="sim-jump" ${active.length?'':'hidden'}>下一个 (0/${simHits.length})</button></div><div class="ov-vert-outer"><table id="ov-hist-wrap"><thead><tr class="ov-head">${headCols}</tr></thead><tbody><tr class="ov-latest" data-date="${esc(latest.date)}">${cellRow(latest)}</tr>${hist.map(r=>`<tr class="${hitDates.has(r.date)?'sim-hit':''}" data-date="${esc(r.date)}">${cellRow(r)}</tr>`).join('')}</tbody></table></div><div class="table-hint"><span>左右滑动查看更多 →</span></div></div>`);
    $('sim-tol').addEventListener('change',e=>{simTol=parseFloat(e.target.value);rerenderHistory();});
    _bindVerticalOnlyScroll(document.querySelector('.ov-vert-outer'));
    _stackStickyThead(document.querySelector('#ov-hist-section .ov-vert-outer'));
    const jump=$('sim-jump');
    if(active.length)jump.addEventListener('click',()=>{
      if(!simHits.length)return;
      simIdx=(simIdx+1)%simHits.length;
      document.querySelector(`#ov-hist-wrap tbody tr[data-date="${simHits[simIdx]}"]`)?.scrollIntoView({block:'center',behavior:'smooth'});
      jump.textContent=`下一个 (${simIdx+1}/${simHits.length})`;
    });
  }
  function rerenderHistory(){
    $('ov-hist-section')?.remove();
    renderOverviewHistory(state.data.overview_history);
  }
  // 双轴滚动: 同轴交给原生 (带 iOS 惯性), JS 只 axis lock 后 preventDefault 另一轴
  // 关键: wrap + 所有 sticky 子元素都 touch-action:pan-x pan-y, 否则 sticky 拦截 touch 链
  function _bindVerticalOnlyScroll(wrap){
    if(!wrap||wrap._scrollLocked)return;wrap._scrollLocked=true;
    let sx0,sy0,sx,sy,axis=null,locked=false;
    wrap.addEventListener('touchstart',e=>{const t=e.touches[0];sx0=t.clientX;sy0=t.clientY;sx=t.clientX;sy=t.clientY;axis=null;locked=false;},{passive:true});
    wrap.addEventListener('touchmove',e=>{
      if(!e.touches[0])return;
      const t=e.touches[0];
      const dx=t.clientX-sx, dy=t.clientY-sy;
      if(!locked){
        const adx=Math.abs(t.clientX-sx0), ady=Math.abs(t.clientY-sy0);
        if(adx+ady<8)return;
        axis=adx>ady?'x':'y';
        locked=true;
      }
      sx=t.clientX; sy=t.clientY;
      // 同轴不阻止 → 原生滚动 + 惯性; 另一轴 preventDefault 阻止串轴
      if(axis==='x' && Math.abs(dy)>1) e.preventDefault();
      else if(axis==='y' && Math.abs(dx)>1) e.preventDefault();
    },{passive:false});
    const reset=()=>{axis=null;locked=false;};
    wrap.addEventListener('touchend',reset,{passive:true});
    wrap.addEventListener('touchcancel',reset,{passive:true});
  }
  // 盘面晴雨表有 3 行 thead (ov-head + ov-sub + ov-latest), CSS 把 ov-sub 和 ov-latest 都钉在 top:43px 会互相盖住
  // 这里按实际渲染高度算每个 sticky 行的 top, 让它们依次紧贴在上一行下面
  function _stackStickyThead(wrap){
    if(!wrap)return;
    // 单层表头: latest 行 sticky top 紧贴 head 下方
    const headTh=wrap.querySelector('tr.ov-head th');
    if(!headTh)return;
    const headH=headTh.getBoundingClientRect().height;
    wrap.querySelectorAll('tr.ov-latest td').forEach(td=>{td.style.top=headH+'px';});
  }
  function resetModal(){modalSeq++;if(modalController)modalController.abort();if(modalChart){modalChart.dispose();modalChart=null;}if(modalFlowChart){modalFlowChart.dispose();modalFlowChart=null;}if(modalHistChart){modalHistChart.dispose();modalHistChart=null;}chartData=null;flowChartData=null;preferredHist.data=null;}
  function openModal(title,code){resetModal();if(!$('detail').open){lastFocus=document.activeElement;$('detail').showModal();document.body.style.overflow='hidden';}$('detail-title').textContent=title;$('detail-code').textContent=code;$('detail-body').innerHTML='<div class="empty">正在读取…</div>';$('detail-back').hidden=!returnSector;}
  function closeModal(){resetModal();$('detail').close();document.body.style.overflow='';returnSector=null;if(lastFocus?.isConnected)lastFocus.focus();}
  async function showChart(code,name,kind){
    if(!code)return;
    const day=state.data?.date||state.date;
    openModal(name||code,code+' · 已保存日线');
    const seq=modalSeq;modalController=new AbortController();const controller=modalController;const timer=setTimeout(()=>controller.abort(),20000);
    try{
      const result=await api('chart',{ts_code:code,kind,date:day},controller.signal);
      if(seq!==modalSeq)return;chartData=result;
      if(!result.rows.length){$('detail-body').innerHTML='<div class="empty">'+esc(result.message)+'</div>';return;}
      const first=result.rows[0].trade_date,last=result.rows.at(-1).trade_date;
      $('detail-body').innerHTML=`<div class="chart-controls" id="chart-periods"><button data-range="30">近30日</button><button data-range="90" class="active">近90日</button><button data-range="all">全部</button></div><div class="chart-controls"><label>起始<input id="chart-start" type="date" min="${dateText(first)}" max="${dateText(last)}"></label><label>结束<input id="chart-end" type="date" min="${dateText(first)}" max="${dateText(last)}"></label></div><div id="price-chart" class="chart"></div><p id="chart-note" class="data-note"></p><p class="data-note">${esc(result.minline_message)}<br>日期范围内只展示已入库日线；成交额单位为亿元。点按图表查看数值，拖动下方滑块调整范围。</p>`;
      if(!window.echarts){$('price-chart').innerHTML='<div class="empty">图表组件不可用，请刷新页面。</div>';return;}
      modalChart=echarts.init($('price-chart'),null,{renderer:'canvas'});
      // 拖拽下方 dataZoom 滑块时, 把可视区间同步到日期输入 + 5档资金流向
      modalChart.on('datazoom', e => {
        const b=e.batch&&e.batch[0];
        if(!b||!chartData)return;
        const n=chartData.rows.length;
        const s=Math.max(0,Math.floor((b.start??0)/100*(n-1)));
        const en=Math.min(n-1,Math.floor((b.end??100)/100*(n-1)));
        const se=$('chart-start'),ee=$('chart-end');
        if(se&&ee){se.value=dateText(chartData.rows[s].trade_date);ee.value=dateText(chartData.rows[en].trade_date);}
        document.querySelectorAll('[data-range]').forEach(x=>x.classList.remove('active'));
        drawChart();
      });
      setChartRange(90);
      for(const id of ['chart-start','chart-end'])$(id).addEventListener('change',()=>{document.querySelectorAll('[data-range]').forEach(b=>b.classList.remove('active'));drawChart();});
      // flow 个股子页: 弹窗底部追加该股 5 档资金流向逐日走势 (只读已有数据)
      if(kind==='stock')appendStockFlow(code,day,seq);
    }catch(e){if(seq===modalSeq)$('detail-body').innerHTML='<div class="empty">图表读取失败，请检查电脑连接后重试。</div>';}
    finally{clearTimeout(timer);}
  }
  async function appendStockFlow(code,day,seq){
    try{
      const result=await api('flow/stock-history',{ts_code:code,date:day||'',days:60},modalController.signal);
      if(seq!==modalSeq)return;
      if(result.empty||!result.dates?.length){$('detail-body').insertAdjacentHTML('beforeend','<p class="data-note">该股暂无已保存资金流向数据。</p>');return;}
      flowChartData=result;
      $('detail-body').insertAdjacentHTML('beforeend','<div class="section-label" id="stock-flow-head">5档资金流向 · 逐日<small>随日K区间联动</small></div><div id="stock-flow-chart" class="chart" style="height:280px"></div>');
      if(!window.echarts)return;
      modalFlowChart=echarts.init($('stock-flow-chart'),null,{renderer:'canvas'});
      drawStockFlow($('chart-start')?.value, $('chart-end')?.value);
    }catch(e){if(seq===modalSeq)$('detail-body').insertAdjacentHTML('beforeend','<p class="data-note">资金流向读取失败。</p>');}
  }
  // 5档资金流向按当前 K 线 start/end 过滤 (YYYY-MM-DD 与 YYYYMMDD 都归一化为紧凑比对)
  function drawStockFlow(start, end){
    if(!modalFlowChart||!flowChartData)return;
    const dates=flowChartData.dates||[];
    const s=String(start||'').replace(/-/g,''), e=String(end||'').replace(/-/g,'');
    const idx=[];
    dates.forEach((d,i)=>{const dc=String(d).replace(/-/g,'');if((!s||dc>=s)&&(!e||dc<=e))idx.push(i);});
    const defs=[['main','主力','#ffbd69'],['super','超大单','#ff6471'],['big','大单','#73b7ff'],['mid','中单','#b699ff'],['small','小单','#40d5a0']];
    const head=$('stock-flow-head')?.querySelector('small');
    if(head)head.textContent=idx.length?`${idx.length} 个交易日 · 单位亿元`:'区间内无数据';
    modalFlowChart.setOption({textStyle:{color:'#8a9bb0'},legend:{data:defs.map(x=>x[1]),textStyle:{color:'#8a9bb0',fontSize:10},top:8},tooltip:{trigger:'axis',appendTo:'body',extraCssText:'max-width:none;white-space:nowrap;background:rgba(20,22,30,0.96);border:1px solid rgba(255,255,255,0.12);border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,0.45)',position:(point,params,dom,rect,size)=>{
      // 固定到 5 档资金流向图表上方, 不遮挡折线
      // ECharts position 返回相对容器的偏移; appendTo:'body' 下 tooltip 仍按 chart 局部坐标定位.
      const chartEl = document.getElementById('stock-flow-chart');
      const cw = chartEl?.clientWidth || size.viewSize[0];
      const w = size.contentSize[0];
      const localX = Math.max(6, (cw - w)/2);
      const desiredTop = -size.contentSize[1] - 8;
      // 视口上方没空间时落到 chart 内部顶部 (让浮窗顶部对齐 chart 顶, 但折线在 grid 内, 通常仍有 60px 间距不遮挡).
      const top = desiredTop < -chartEl.getBoundingClientRect().top + 40 ? 4 : desiredTop;
      return {left: localX, top};
    },formatter:params=>{
      const i=params[0].dataIndex;
      const dateLabel=dateText(dates[i]);
      const sumN=(key,n)=>(flowChartData.series[key]||[]).slice(Math.max(0,i-n+1),i+1).reduce((a,v)=>finite(v)?a+v:a,0);
      const cellsHtml=(days,isFirst)=>defs.map(([k,n,c])=>{
        const v=days===1?(flowChartData.series[k]||[])[i]:sumN(k,days);
        const text=v==null?'—':(v>=0?'+':'')+fmt(v/1e8,2)+' 亿';
        const color=v==null?'#7d8699':v>=0?'#ff6471':'#40d5a0';
        return `<td style="padding:2px 6px;text-align:right;color:${color};font-family:-apple-system,monospace;font-size:11px">${text}</td>`;
      }).join('');
      const heads=defs.map(([k,n,c])=>`<th style="padding:0 6px 4px;color:${c};font-weight:600;text-align:right;font-size:11px">${n.replace('净流入','')}</th>`).join('');
      const rows=[1,3,5,10,20].map(d=>{
        const label=d===1?'当日':`${d}日`;
        return `<tr><td style="padding:3px 6px 3px 0;color:#c9d1e0;font-weight:600;font-size:11px">${label}</td>${cellsHtml(d)}</tr>`;
      }).join('');
      return `<div style="margin-bottom:6px;color:#e2e8f0;font-weight:600">${dateLabel} · 累计净流入</div>`
        + `<table style="border-collapse:collapse;white-space:nowrap"><thead><tr><th></th>${heads}</tr></thead><tbody>${rows}</tbody></table>`;
    }},grid:{left:52,right:12,top:40,bottom:28},xAxis:{type:'category',data:idx.map(i=>dateText(dates[i])),axisLabel:{fontSize:9}},yAxis:{type:'value',axisLabel:{fontSize:9,formatter:v=>fmt(v,2)},splitLine:{lineStyle:{color:'#233044'}}},series:defs.map(([key,name,color])=>({name,type:'line',data:idx.map(i=>{const v=(flowChartData.series[key]||[])[i];return finite(v)?v/1e8:null;}),showSymbol:false,lineStyle:{width:key==='main'?2:1.2,color},itemStyle:{color},connectNulls:true}))},true);
  }
  function setChartRange(n){const rows=chartData.rows;$('chart-start').value=dateText(rows[n==='all'?0:Math.max(0,rows.length-Number(n))].trade_date);$('chart-end').value=dateText(rows.at(-1).trade_date);document.querySelectorAll('[data-range]').forEach(b=>b.classList.toggle('active',b.dataset.range===String(n)));drawChart();}
  function drawChart(){
    if(!chartData||!modalChart)return;
    const start=$('chart-start').value,end=$('chart-end').value;
    if(!start||!end||start>end){$('chart-note').textContent='请选择有效范围：起始日期不能晚于结束日期。';modalChart.clear();return;}
    const all=chartData.rows,indices=all.map((r,i)=>({r,i})).filter(({r})=>dateText(r.trade_date)>=start&&dateText(r.trade_date)<=end),rows=indices.map(x=>x.r);
    const missing=rows.filter(r=>!['open','high','low','close'].every(k=>finite(r[k]))).length;
    $('chart-note').textContent=`${dateText(rows[0]?.trade_date)} — ${dateText(rows.at(-1)?.trade_date)} · ${rows.length} 个交易日${missing?' · '+missing+' 日缺少完整 OHLC，已跳过对应蜡烛':''}`;
    // Compute on full history so changing the visible range does not change MA values.
    const movingAverages=Object.fromEntries([5,10,20,30].map(n=>[n,movingAverage(all,n)]));
    const ma=[5,10,20,30].map(n=>({name:'MA'+n,type:'line',data:indices.map(x=>movingAverages[n][x.i]),showSymbol:false,lineStyle:{width:1},xAxisIndex:0,yAxisIndex:0}));
    modalChart.setOption({animation:false,color:['#ffd078','#73b7ff','#cc9dff','#68dac0'],textStyle:{color:'#8a9bb0'},legend:{data:['MA5','MA10','MA20','MA30'],top:12,textStyle:{color:'#8a9bb0',fontSize:10}},tooltip:{trigger:'axis',renderMode:'richText',confine:true,axisPointer:{type:'cross'}},axisPointer:{link:[{xAxisIndex:'all'}]},grid:[{left:52,right:15,top:48,height:'45%'},{left:52,right:15,top:'70%',height:'13%'}],xAxis:[{type:'category',data:rows.map(r=>dateText(r.trade_date)),axisLabel:{show:false}},{type:'category',gridIndex:1,data:rows.map(r=>dateText(r.trade_date)),axisLabel:{fontSize:9,formatter:v=>v.slice(5)}}],yAxis:[{scale:true,splitLine:{lineStyle:{color:'#233044'}},axisLabel:{fontSize:9}},{gridIndex:1,scale:true,name:'亿元',splitNumber:2,splitLine:{show:false},axisLabel:{fontSize:9}}],dataZoom:[{type:'slider',xAxisIndex:[0,1],height:18,bottom:12,borderColor:'#233044',textStyle:{color:'#8a9bb0'},start:0,end:100}],series:[{name:chartData.name,type:'candlestick',data:rows.map(candle),itemStyle:{color:'#ff6471',color0:'#40d5a0',borderColor:'#ff6471',borderColor0:'#40d5a0'}},...ma,{name:'成交额',type:'bar',xAxisIndex:1,yAxisIndex:1,data:rows.map(r=>({value:r.volume,itemStyle:{color:finite(r.change)&&r.change>=0?'#ff6471':'#40d5a0'}}))}]},true);
    drawStockFlow(start, end);
  }
  // Sector views retain their context when opening and returning from a stock chart.
  async function showSector(name){
    returnSector=null;openModal(name,'板块合集 · 历史成员');const seq=modalSeq;
    modalController=new AbortController();const controller=modalController;const timer=setTimeout(()=>controller.abort(),20000);
    try{const result=await api('sectors/detail',{name,date:state.data?.date},controller.signal);if(seq!==modalSeq)return;$('detail-body').innerHTML=table(result.items,[col('date','最近上榜','date'),col('streak','连板'),col('close','所选日收盘价','num'),col('change_pct','涨幅','pct')],result.items.length);returnSector={type:'sector',name};}
    catch(e){if(seq===modalSeq)$('detail-body').innerHTML='<div class="empty">读取失败，请检查电脑连接。</div>';}
    finally{clearTimeout(timer);}
  }
  // 条件优选历史涨幅分布 (跟桌面 /api/conditional-picks/preferred/historical-distribution 同口径)
  const preferredHist={window:5,data:null,seq:0};
  async function showPreferredHistory(windowDays){
    const w=[1,3,5].includes(windowDays)?windowDays:5;
    preferredHist.window=w;
    returnSector=null;openModal(`历史涨幅分布 · T+1~T+${w}`,'条件优选 · 已保存快照');
    $('detail-body').innerHTML='<div class="empty">正在读取…</div>';
    const seq=++preferredHist.seq;
    modalController=new AbortController();const controller=modalController;const timer=setTimeout(()=>controller.abort(),20000);
    try{
      const d=await api('preferred-history',{days:w},controller.signal);
      if(seq!==preferredHist.seq)return;
      preferredHist.data=d;renderPreferredHistory();
    }catch(e){
      if(seq===preferredHist.seq)$('detail-body').innerHTML='<div class="empty">读取失败，请检查电脑连接后重试。</div>';
    }finally{clearTimeout(timer);}
  }
  function renderPreferredHistory(){
    const d=preferredHist.data;if(!d)return;
    $('detail-title').textContent=`历史涨幅分布 · T+1~T+${d.window}`;
    const rate=d.total?(d.settled/d.total*100).toFixed(1):'0.0';
    const winTabs=(d.windows||[1,3,5]).map(n=>`<button class="freq-tab ${n===d.window?'active':''}" data-hist-window="${n}" aria-pressed="${n===d.window}">T+${n}</button>`).join('');
    const metrics=`<div class="metrics">${metric('可统计样本',fmt(d.settled,0))}${metric('入选次数',fmt(d.total,0))}${metric('数据未满排除',fmt(d.pending,0))}${metric('唯一股票',fmt(d.unique_stocks,0))}</div>`;
    const chart=d.buckets.some(b=>b.count)?'<div class="chart hist-chart" id="hist-chart"></div>':'';
    const samples=d.samples||[];
    const rows=samples.map(s=>`<tr><td>${esc(s.name||'—')}<span class="code">${esc(dateText(s.trade_date))}</span></td><td class="${tone(s.gain_pct)}">${pct(s.gain_pct)}</td><td>${esc(s.bucket_label||'')}</td></tr>`).join('');
    const tbl=samples.length?`<div class="table-wrap"><table><thead><tr><th scope="col">名称 / 入选日</th><th scope="col">最高涨幅</th><th scope="col">区间</th></tr></thead><tbody>${rows}</tbody></table></div><div class="table-hint"><span>共 ${samples.length} 条 · 按涨幅由高到低</span></div>`:'<div class="empty">暂无满足条件的样本。</div>';
    $('detail-body').innerHTML=`<div class="freq-tabs">${winTabs}</div>${metrics}<p class="data-note">${esc(d.metric_label)} · ${esc(dateText(d.date_from))} ~ ${esc(dateText(d.date_to))}（${d.date_count} 个入选日）· 可统计率 ${rate}%</p>${chart}<p class="data-note">每次入选算 1 个样本（同股多次入选计多次）；累计最高涨幅 = 入选后窗口内任一天最高价 / 入选日收盘价 − 1。盘后数据，非可成交收益。</p>${tbl}`;
    if(chart)drawHistChart(d);
  }
  function drawHistChart(d){
    if(modalHistChart){modalHistChart.dispose();modalHistChart=null;}
    const box=$('hist-chart');if(!box)return;
    modalHistChart=echarts.init(box,null,{renderer:'canvas'});
    modalHistChart.setOption({
      animation:false,textStyle:{color:'#8a9bb0'},
      grid:{left:34,right:34,top:26,bottom:22},
      tooltip:{trigger:'axis',confine:true,axisPointer:{type:'shadow'}},
      xAxis:{type:'category',data:d.buckets.map(b=>b.label),axisLabel:{fontSize:9,interval:0,rotate:d.buckets.length>6?32:0},axisTick:{show:false}},
      yAxis:[{type:'value',name:'只数',nameTextStyle:{fontSize:9,color:'#8a9bb0'},axisLabel:{fontSize:9},splitLine:{lineStyle:{color:'#233044'}}},
             {type:'value',name:'占比',max:100,nameTextStyle:{fontSize:9,color:'#8a9bb0'},axisLabel:{fontSize:9,formatter:'{value}%'},splitLine:{show:false}}],
      series:[
        {type:'bar',name:'只数',data:d.buckets.map(b=>({value:b.count,itemStyle:{color:b.key==='drop'?'rgba(64,213,160,.75)':'rgba(115,183,255,.8)'}})),barMaxWidth:26},
        {type:'line',name:'占比',yAxisIndex:1,data:d.buckets.map(b=>b.pct),symbol:'circle',symbolSize:4,lineStyle:{width:1.3,color:'#ffd078'},itemStyle:{color:'#ffd078'}},
      ],
    },true);
  }
  document.addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.nav){state.group=b.dataset.nav;state.module=groups[state.group][0];state.date='';state.allDates=false;$('search').value='';$('sort').value='default';controls();load(true);window.scrollTo(0,0);}
    else if(b.dataset.module){state.module=b.dataset.module;state.date='';state.allDates=false;$('search').value='';$('sort').value='default';controls();load(true);}
    else if(b.dataset.flowView){state.flowView=b.dataset.flowView;state.date='';state.allDates=false;$('search').value='';controls();load(true);}
    else if(b.dataset.index){returnSector=null;showChart(b.dataset.index,b.dataset.name,'index');}
    else if(b.dataset.stock){if(!$('detail').contains(b))returnSector=null;showChart(b.dataset.stock,b.dataset.name,'stock');}
    else if(b.dataset.sector)showSector(b.dataset.sector);
    else if(b.dataset.range)setChartRange(b.dataset.range);
    else if(b.dataset.sim){simFields.has(b.dataset.sim)?simFields.delete(b.dataset.sim):simFields.add(b.dataset.sim);rerenderHistory();}
    else if(b.dataset.freqRange){state.freqRange=b.dataset.freqRange;if(state.data?.frequency)renderFreq(state.data.frequency);}
    else if(b.dataset.preferredHistory){showPreferredHistory(b.dataset.histWindow?Number(b.dataset.histWindow):preferredHist.window);}
    else if(b.dataset.histWindow){showPreferredHistory(Number(b.dataset.histWindow));}
    else if(b.id==='load-more'){state.limit+=100;render();}
  });
  $('refresh').addEventListener('click',()=>load(true));
  $('date').addEventListener('change',()=>{
    const raw=$('date').value;
    if(!raw){state.date='';load();return;}
    const compact=raw.replace(/-/g,'');
    if(state.availableDates.includes(compact)){
      state.allDates=true;state.date=compact;updateDateNav();load();
    }else{
      // 用户选了 min/max 外的日期 → 回弹到最近的有效日期
      $('date').value=state.date?dateText(state.date):'';
    }
  });
  $('date-prev').addEventListener('click',()=>{
    const dates=state.availableDates;if(!dates.length||!state.allDates)return;
    const i=dates.indexOf(state.date||dates[0]);if(i<=0)return;
    state.date=dates[i-1];$('date').value=dateText(state.date);updateDateNav();load();
  });
  $('date-next').addEventListener('click',()=>{
    const dates=state.availableDates;if(!dates.length||!state.allDates)return;
    const i=dates.indexOf(state.date||dates[0]);if(i===-1||i>=dates.length-1)return;
    state.date=dates[i+1];$('date').value=dateText(state.date);updateDateNav();load();
  });
  $('date-all').addEventListener('click',()=>{
    state.allDates=!state.allDates;
    if(state.allDates){
      // 进入"全部日期"模式: 默认跳到最近可用日期
      state.date=state.availableDates[0]||state.date;
      $('date').value=state.date?dateText(state.date):'';
    }else{
      state.date='';$('date').value='';
    }
    updateDateNav();load();
  });
  $('search').addEventListener('input',()=>{state.limit=100;render();});$('sort').addEventListener('change',render);
  $('detail-close').addEventListener('click',closeModal);
  $('detail').addEventListener('cancel',e=>{e.preventDefault();closeModal();});
  $('detail-back').addEventListener('click',()=>{const target=returnSector;if(target?.type==='sector')showSector(target.name);});
  window.addEventListener('resize',()=>{modalChart?.resize();modalHistChart?.resize();flowChart?.resize();});
  window.addEventListener('offline',()=>{connection('网络已断开 · 当前屏幕内容不会继续更新。',true);});
  window.addEventListener('online',()=>load(true));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)load(true);});
  if('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(()=>{});
  controls();load(true);
}
