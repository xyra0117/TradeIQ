const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(`${__dirname}/index.html`,'utf8');
for(const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
assert(!html.includes('/api/sector-collections'));
assert(!html.includes('/api/user-sectors'));
assert(!html.includes('板块合集'));
assert(html.includes('function escapeHtml('));
assert(!html.includes('_updateSectorsNavButtons'));
assert(html.includes('data-tab="sectors">热门板块'));
const elements=new Map(),calls=[],charts=[];
function el(id){if(!elements.has(id))elements.set(id,{value:'',innerHTML:'',textContent:'',style:{},disabled:false,listeners:{},querySelectorAll(){return filters;},addEventListener(event,handler){this.listeners[event]=handler;},dispatchEvent(){}});return elements.get(id);}
const filters=['all','active','cooling'].map(key=>({dataset:{hotFilter:key},disabled:false,attrs:{},classList:{toggle(){}},setAttribute(name,value){this.attrs[name]=value;}}));
const ctx=vm.createContext({console,active:true,
  document:{getElementById:el,documentElement:{},querySelector:()=>ctx.active?{}:null},
  window:{addEventListener(){}},fpInstances:{},Event:class{},
  getComputedStyle:()=>({getPropertyValue:()=> '#222'}),
  escapeHtml:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  showKlineModal(){},formatDateForInput:d=>d,
  fetch(url){return new Promise((resolve,reject)=>calls.push({url,resolve,reject}));},
  echarts:{init(){const chart={disposed:false,dispose(){this.disposed=true;},setOption(option){this.option=option;},resize(){}};charts.push(chart);return chart;}}
});
vm.runInContext(html.slice(html.indexOf('// ── 热门板块：'),html.indexOf('// ── 热门板块结束 ──')),ctx);
el('hotSectorSort').value='recent';
const stock={ts_code:'600241.SH',name:'时代万恒',rank:1,consecutive:3,rank_change:'昨日龙1 → 今日龙1',reason:'3连板 · 09:25',status:'ranked'};
const item={sector:'电池产业链',today_count:7,delta:-3,appearance5:2,unique5:13,recent5_count:17,
  leaders:[stock],pending_count:0,not_sealed_count:0,previous_ready:true,yesterday_leader:{...stock,status:'broken',status_label:'断板',change_pct:-2},
  heat:{key:'new',label:'新近活跃',reason:'此前3日0，近2日10、7只',rank:2,in_board:true},
  stocks:[stock],history:[{...stock,date:'20260930'}],timeline:[{date:'20260929',count:null},{date:'20260930',count:7}]};
const payload={date:'20260930',status:'ready',sectors:[item],available_dates:['20260930','20260929'],summary:{stock_count:52,raw_records:52,excluded_stock_count:11,recent5_complete:true,heat_groups:{active:[],new:['电池产业链'],cooling:[]},heat_rules:'统计规则'},market:{overview:{upCount:2567,downCount:2824,flatCount:170},indices:[{name:'上证指数',change:-.8}]}};
ctx.item=item;ctx.payload=payload;ctx.stock=stock;
const resolve=(call,data)=>call.resolve({ok:true,json:async()=>data});
const setDate=date=>vm.runInContext(`_hotSectors.selectedDate=${JSON.stringify(date)}`,ctx);
const call=script=>vm.runInContext(script,ctx);
(async()=>{
  let pending=call('loadHotSectors()');resolve(calls.at(-1),payload);await pending;
  assert(el('hotSectorGrid').innerHTML.includes('时代万恒'));
  assert(el('hotSectorGrid').innerHTML.includes('暂无符合股票'));
  assert(el('hotSectorGrid').innerHTML.includes('hot-down'));
  assert(el('hotSectorMarket').innerHTML.includes('-0.80%'));
  assert(el('hotSectorCoverage').textContent.includes('2026-09-30'));
  assert.equal(el('sectorsPrevBtn').disabled,false);
  assert.equal(el('sectorsNextBtn').disabled,true);
  assert(el('hotSectorOverview').innerHTML.includes('近两日新热点'));
  assert(el('hotSectorBoard').innerHTML.includes('新近活跃'));
  assert(el('hotSectorBoard').innerHTML.includes('缺数据'));
  assert(call('_hotHeatCell({date:"20260930",count:0},"<板块>")').includes('hot-cell-0'));
  assert(call('_hotHeatCell({date:"20260930",count:null},"<板块>")').includes('hot-cell-missing'));
  assert(call('_hotHeatCell({date:"20260930",count:null},"<板块>")').includes('&lt;板块&gt;'));
  assert(call('_hotRecords([{...stock,reason:"<script>bad</script>",name:"<bad>"}])').includes('&lt;script&gt;'));
  const sorted=call('_hotSorted([{sector:"A",today_count:1,delta:null},{sector:"B",today_count:1,delta:-7},{sector:"C",today_count:7,delta:3}],"cooling","")');
  assert.equal(sorted[0].sector,'B');assert.equal(sorted[2].sector,'A');
  assert.equal(call('_hotSorted([{sector:"今日多",today_count:9,recent5_count:9,appearance5:1},{sector:"近期多",today_count:0,recent5_count:24,appearance5:3}],"recent","")')[0].sector,'近期多');
  assert.equal(call('_hotSorted([{sector:"阿板块",today_count:3,recent5_count:12,appearance5:4,heat:{rank:2}},{sector:"紫板块",today_count:3,recent5_count:12,appearance5:4,heat:{rank:1}}],"recent","")')[0].heat.rank,1);

  // Filters affect the heat board, not the full leader pool; ranks stay tied to the full board.
  ctx.mixed={...payload,sectors:[{...item,sector:'PCB',today_count:0,recent5_count:24,heat:{key:'cooling',label:'已经降温',reason:'今日0只',rank:1,in_board:true}},item]};
  call('_hotSectors.data=mixed;_renderHotSectorGrid()');
  el('hotSectorBoardFilters').listeners.click({target:{closest:()=>filters[2]}});
  assert(el('hotSectorBoard').innerHTML.includes('PCB'));
  assert(!el('hotSectorBoard').innerHTML.includes('电池产业链'));
  assert(el('hotSectorGrid').innerHTML.includes('电池产业链'));
  el('hotSectorSearch').value='电池';call('_renderHotSectorGrid()');
  assert(el('hotSectorBoard').innerHTML.includes('没有匹配'));
  el('hotSectorBoardFilters').listeners.click({target:{closest:()=>filters[0]}});
  assert(el('hotSectorBoard').innerHTML.includes('02'));
  el('hotSectorSearch').value='';

  // Slow old date may not replace a new date, even when requests finish in reverse order.
  setDate('20260929');const old=call('loadHotSectors()'),oldCall=calls.at(-1);
  assert.equal(el('hotSectorMarket').innerHTML,'');
  assert.equal(el('hotSectorOverview').innerHTML,'');
  assert(!el('hotSectorBoard').innerHTML.includes('PCB'));
  setDate('20260930');const fresh=call('loadHotSectors()');resolve(calls.at(-1),payload);await fresh;
  resolve(oldCall,{...payload,date:'20260929',sectors:[{...item,sector:'迟到数据'}]});await old;
  assert(!el('hotSectorGrid').innerHTML.includes('迟到数据'));

  // Hidden view cannot accept a response.
  const hidden=call('loadHotSectors()');ctx.active=false;resolve(calls.at(-1),payload);await hidden;
  assert(!el('hotSectorGrid').innerHTML.includes('时代万恒'));
  ctx.active=true;pending=call('loadHotSectors()');resolve(calls.at(-1),payload);await pending;

  // Returning to cards invalidates the pending detail request.
  const detail=call('showHotSectorDetail("电池产业链")'),detailCall=calls.at(-1);
  call('backToHotSectors()');resolve(detailCall,{...payload,detail:item});await detail;
  assert.equal(charts.length,0);
  const validDetail=call('showHotSectorDetail("电池产业链")');resolve(calls.at(-1),{...payload,detail:item});await validDetail;
  assert.equal(charts.length,1);assert.equal(charts[0].option.series[0].connectNulls,false);
  assert.equal(charts[0].option.series[0].data[0],null);
  assert(el('hotSectorStocks').innerHTML.includes('时代万恒'));
  assert.equal(el('hotSectorMain').style.display,'none');
  call('backToHotSectors()');assert(charts[0].disposed);
  assert.equal(el('hotSectorMain').style.display,'block');

  // Explicit missing date stays missing, never showing the previous list.
  setDate('20260713');const missing=call('loadHotSectors()');resolve(calls.at(-1),{date:'20260713',status:'missing',message:'该日没有数据',sectors:[],available_dates:payload.available_dates});await missing;
  assert(el('hotSectorGrid').innerHTML.includes('2026-07-13'));
  assert(!el('hotSectorGrid').innerHTML.includes('时代万恒'));
  assert(!el('hotSectorBoard').innerHTML.includes('电池产业链'));
  assert.equal(el('hotSectorOverview').innerHTML,'');

  ctx.incomplete={...payload,summary:{...payload.summary,recent5_complete:false},sectors:[{...item,recent5_count:null,appearance5:null,heat:{key:'missing',label:'数据不足',in_board:true,rank:null}}]};
  call('_hotSectors.data=incomplete;_renderHotSectorGrid()');
  assert(el('hotSectorOverview').innerHTML.includes('数据不足，暂不判断'));
  assert(el('hotSectorBoard').innerHTML.includes('数据不足'));
  assert(filters.every(button=>button.disabled));

  // Clearing the picker makes exactly one latest request; duplicate calendar events are ignored while loading.
  ctx.fpInstances.sectorsDate={clear(trigger){assert.equal(trigger,false);}};
  const countBefore=calls.length;
  const latest=el('hotSectorLatest').listeners.click();
  assert.equal(calls.length,countBefore+1);assert.equal(calls.at(-1).url,'/api/hot-sectors');
  resolve(calls.at(-1),payload);await latest;
  el('sectorsDate').value='2026-09-29';
  const changed=el('sectorsDate').listeners.change(),countChanged=calls.length;
  el('sectorsDate').listeners.change();assert.equal(calls.length,countChanged);
  resolve(calls.at(-1),{...payload,date:'20260929'});await changed;
  console.log('热门板块：排序、展示、缺失日期、异步隔离、趋势留空及旧入口移除验证通过');
})().catch(error=>{console.error(error);process.exitCode=1;});
