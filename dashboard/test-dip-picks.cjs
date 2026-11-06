const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
assert.match(html,/大盘：下跌家数 &gt;2600，且上证下跌。跌幅不足1\.6%时/);
assert.match(html,/跌幅超过1\.6%直接触发。恰好跌−1\.6%不触发/);
assert.doesNotMatch(html.slice(html.indexOf('<div id="dip2Rules"'),html.indexOf('<div class="surge-toolbar">',html.indexOf('<div id="dip2Rules"'))), /最多高于20日线5%|收盘不高于60日线|5日主力净额 \/ 成交额/);
assert.match(html,/收盘 &gt; MA5 &gt; MA10 &gt; MA20 &gt; MA60/);
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'', textContent:'', dataset:{}, style:{}, value:id==='dipSort'?'code':'',
    attributes:{}, setAttribute(name,value){this.attributes[name]=value;}, querySelector(){return element(id+'Section');}});
  return elements.get(id);
};
const calls = [];
const countCalls = [];
const dipCountCalls = [];
const diagnosticCalls = [];
const marketCalls = [];
const hotCalls = [];
const savedPreferences = new Map();
const storage = {getItem:key=>savedPreferences.get(key) ?? null,setItem:(key,value)=>savedPreferences.set(key,value)};
const ctx = vm.createContext({console, Number, encodeURIComponent,localStorage:storage,
  document:{getElementById:element,querySelector(){return null;}}, currentPicksGroup:'dip', picksSelDate:'20260924',
  CONDITIONAL_PREFERRED_INDEXES:['000001.SH','399001.SZ','399006.SZ','000688.SH'].map(code=>({code})),
  fmtDate:d=>d,
  escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
  stopPicksAutoRefresh(){ctx.stopped=(ctx.stopped||0)+1;},
  fetch(url){return new Promise((resolve,reject)=>(url.includes('/market-overview')||url.includes('/index/minline')?marketCalls:url.includes('/api/hot-sectors')?hotCalls:url.includes('/diagnostics')?diagnosticCalls:url.includes('/condition-counts')?(url.includes('/dip-v2/')?countCalls:dipCountCalls):calls).push({url,resolve,reject}));},
  showKlineModal(...args){ctx.kline=args;}, openStockFlowDetail(...args){ctx.flow=args;},
});
ctx._fetchPicksResult = (...args) => ctx.fetch(...args);
ctx.PicksResultCache = {invalidate(){}};
vm.runInContext(html.slice(html.indexOf('function renderSectorPills(stock)'), html.indexOf('function initFilters()')), ctx);
vm.runInContext(html.slice(html.indexOf('// ── 低吸选股：'), html.indexOf('// ── 低吸选股结束 ──')), ctx);
vm.runInContext(html.slice(html.indexOf('const _hotNumber ='), html.indexOf('function _renderHotSectorGrid')), ctx);
vm.runInContext(html.slice(html.indexOf('function _renderConditionalPreferredBreadth('),html.indexOf('// ── 条件优选历史涨幅分布')),ctx);
vm.runInContext(html.slice(html.indexOf('const PICK_TN_REASONS ='),html.indexOf('function _renderPostPickTnCells')),ctx);
const row = {ts_code:'000001.SZ', name:'<测试>', trade_date:'20260924', close:12, change_pct:-1,
  ret120:100, amplitude20:5.5, ma20:11, volume_ratio5:1.1,
  main_net_today:3e6, main_net_3d:9e6, main_net_5d:15e6, main_net_10d:30e6, main_net_20d:60e6,
  dip_outcomes:[{date:'20260928',high_sum_pct:20,close_sum_pct:10,reason:null},
    {date:'20260929',high_sum_pct:10,close_sum_pct:-1,reason:null},
    {date:'20260930',high_sum_pct:null,close_sum_pct:null,reason:'missing_daily'},
    {date:'20261008',high_sum_pct:7,close_sum_pct:2,reason:'intraday'},
    {date:'20261009',high_sum_pct:null,close_sum_pct:null,reason:'future_unavailable'}]};
const payload = (date, results=[row]) => ({date, daily_ready:true, market_passed:true,
  count:results.length, results, next_dates:['20260928','20260929','20260930','20261008','20261009'],
  stats:{total:5000, declining:3000, eligible:100},
  market:{status:'matched', down_count:3000, index_change:-1, previous_three_up:false, previous_three:[], note:'大盘条件触发'}});
ctx.rows = [row, {...row, ts_code:'000002.SZ', name:'另一只', main_net_20d:90e6}];
vm.runInContext('dipPicksCache=rows; _dipPayload={daily_ready:true,market_passed:true}; renderDipPicksTable()', ctx);
const body = element('dipPicksBody');
assert.match(body.innerHTML, /&lt;测试&gt;/);
assert.match(body.innerHTML, /-1\.00%/);
assert.match(body.innerHTML, /1\.10倍/);
assert.match(body.innerHTML, /\+100\.00%/);
assert.equal((body.innerHTML.match(/<td/g)||[]).length, 42);
assert.equal((body.innerHTML.match(/class="picks-cell-tn"/g)||[]).length, 10);
assert.match(body.innerHTML, /\+20\.00%/);
assert.match(body.innerHTML, /收盘 -1\.00%/);
assert.match(body.innerHTML, /缺少对应交易日日线/);
assert.match(body.innerHTML, /⚡最高 \+7\.00%/);
assert.match(body.innerHTML, /⚡实时 \+2\.00%/);
assert.match(body.innerHTML, /尚无对应交易日收盘数据/);
for (let i=1;i<=5;i++) assert.match(html,new RegExp(`id="dipTnHead${i}">T\\+${i}`));
assert.equal((body.innerHTML.match(/data-dip-action="flow"/g)||[]).length, 10);
const click = action => body.onclick({stopPropagation(){ctx.propagationStopped=true;}, target:{closest(){return {
  dataset:{dipAction:action}, closest(){return {dataset:{tsCode:'000001.SZ',stockName:'测试',pickDate:'20260924'}};}
};}}});
click('kline'); assert.ok(ctx.propagationStopped); assert.equal(ctx.kline[2].pickDate, '20260924');
click('flow'); assert.equal(ctx.flow[2], 'main');
element('dipSearch').value='000002'; vm.runInContext('renderDipPicksTable()',ctx);
assert.doesNotMatch(body.innerHTML, /000001/);
element('dipSearch').value='无此股'; vm.runInContext('renderDipPicksTable()',ctx); assert.match(body.innerHTML, /没有匹配/);
element('dipSearch').value=''; element('dipSort').value='main_net_20d'; vm.runInContext('renderDipPicksTable()',ctx);
assert.ok(body.innerHTML.indexOf('000002') < body.innerHTML.indexOf('000001'));

// Exercise the real dispatchers and visibility controls, using lightweight peers.
vm.runInContext(html.slice(html.indexOf('function _loadCurrentPicksSub('), html.indexOf('// ===== 累计净流入门槛控件')),ctx);
vm.runInContext(html.slice(html.indexOf('function _showBucketContainer(group)'), html.indexOf('// 兼容旧 call site')),ctx);
vm.runInContext('_showBucketContainer("dip")',ctx);
assert.equal(element('dipPicksContainer').style.display, '');
assert.equal(element('surgePicksContainer').style.display, 'none');
assert.equal(element('posPicksTable').style.display, 'none');
assert.equal(vm.runInContext('_getCurrentPicksCache().length',ctx), 2);
const tabsSource = html.slice(html.indexOf('const PICKS_SUB_TABS ='),html.indexOf('function _renderPicksSubTabs'));
vm.runInContext(tabsSource,ctx);
assert.equal(vm.runInContext('PICKS_SUB_TABS.em.find(t=>t.key==="dip").label',ctx),'低吸选股');
assert.equal(vm.runInContext('PICKS_SUB_TABS.em.find(t=>t.key==="dip2").label',ctx),'追高选股');

(async()=>{
  const first=vm.runInContext('_loadCurrentPicksSub()',ctx);
  ctx.picksSelDate='20260923'; const second=vm.runInContext('loadDipPicks()',ctx);
  assert.equal(calls[1].url, '/api/picks/dip?date=20260923');
  calls[1].resolve({ok:true,json:async()=>payload('20260923')}); await second;
  calls[0].resolve({ok:true,json:async()=>payload('20260924',[])}); await first;
  assert.match(element('dipSummary').textContent,/20260923/);
  assert.match(body.innerHTML,/000001/);
  assert.match(element('dipTnHead1').title,/20260928/);
  assert.match(element('dipTnHead5').title,/20261009/);
  assert.equal(element('dipMarket').dataset.state,'matched');
  assert.match(element('dipMarket').innerHTML,/前三日非连续上涨/);
  assert.match(element('dipMarketRule').textContent,/下跌家数 >2600.*上证下跌/);
  const away=vm.runInContext('loadDipPicks()',ctx); ctx.currentPicksGroup='system';
  calls[2].resolve({ok:true,json:async()=>payload('20260922')}); await away;
  assert.doesNotMatch(element('dipSummary').textContent,/20260922/);
  ctx.currentPicksGroup='dip';
  const empty=vm.runInContext('loadDipPicks()',ctx);
  calls[3].resolve({ok:true,json:async()=>({...payload('20260923',[]),market_passed:false,
    market:{status:'unmet',down_count:2000,index_change:-1,note:'未超过2600家，不推荐'},note:'大盘未触发，不推荐'})}); await empty;
  assert.match(body.innerHTML,/大盘未触发，不推荐/);
  assert.match(element('dipSummary').textContent,/大盘未触发/);
  assert.equal(element('dipMarket').dataset.state,'unmet');
  const pending=vm.runInContext('loadDipPicks()',ctx);
  calls[4].resolve({ok:true,json:async()=>({...payload('20260923',[]),daily_ready:false,market:null,market_passed:false,note:'等待完整收盘数据'})}); await pending;
  assert.match(body.innerHTML,/等待完整收盘数据/);
  const retry=vm.runInContext('loadDipPicks()',ctx);
  calls[5].resolve({ok:false,json:async()=>({message:'本地行情无法读取'})}); await retry;
  assert.match(body.innerHTML,/本地行情无法读取/);
  assert.equal(element('dipVisibleCount').textContent,'');
  ctx.picksSelDate=''; const latest=vm.runInContext('loadDipPicks()',ctx);
  assert.equal(calls[6].url,'/api/picks/dip');
  calls[6].resolve({ok:true,json:async()=>payload('20260930')}); await latest;
  assert.match(element('dipSummary').textContent,/20260930/);
  // The actual quote refresher must return before issuing any request.
  vm.runInContext(html.slice(html.indexOf('async function refreshPicksQuotes()'),html.indexOf('// 三个东方财富条件 Tab：')),ctx);
  await vm.runInContext('refreshPicksQuotes()',ctx);
  assert.equal(calls.length,7);
  assert.ok(ctx.stopped>=7);
  // The new strategy has its own cache but shares the dates, table and chart controls.
  ctx.currentPicksGroup='dip2'; ctx.picksSelDate='20260902';
  const v2row={...row,ts_code:'002980.SZ',name:'华盛昌',trade_date:'20260902',change_pct:6,
    ma5:11.7,ma10:11.5,ma60:10.8,limit_up_price:12.45,main_net_today:-1e6};
  const v2payload=(rows=[v2row])=>({...payload('20260902',rows),strategy:'dip-picks-v2',
    market:{status:'matched',down_count:2599,index_change:.5,previous_three_up:null,previous_three:[],
      note:'下跌家数<2600且上证当日上涨，大盘条件触发'},
    stats:{total:5000,strong_rising:300,eligible:100},
    v2_stats:{base_count:35,missing_limit:2,limit_up:20,not_bullish:12,matched:rows.length},
    note:rows.length?'全部条件匹配才入选':'该日没有同时满足全部个股条件的股票，不推荐'});
  const countPayload=(date='20260902')=>({status:'success',date,daily_ready:true,total:5000,
    counts:Object.fromEntries(vm.runInContext('CHASE_RULES.map(([key])=>key)',ctx).map(key=>[key,{matched:key==='amplitude'?0:key==='funds_red'?0:123,evaluated:key==='funds_red'?0:4500,missing:key==='funds_red'?5000:500,total:5000}]))});
  const v2=vm.runInContext('_loadCurrentPicksSub()',ctx);
  assert.equal(calls[7].url,'/api/picks/dip-v2?date=20260902');
  calls[7].resolve({ok:true,json:async()=>v2payload()}); await v2;
  assert.equal(countCalls[0].url,'/api/picks/dip-v2/condition-counts?date=20260902');
  const firstCounts=vm.runInContext('_chaseCountsPromise',ctx);
  countCalls[0].resolve({ok:true,json:async()=>countPayload()}); await firstCounts;
  assert.equal(element('chaseCount_gain').textContent,'123个符合');
  assert.equal(element('chaseCount_amplitude').textContent,'全军覆没');
  assert.equal(element('chaseCount_funds_red').textContent,'数据不足');
  assert.match(element('chaseCount_gain').title,/已核验 4500.*缺失或异常 500/);
  assert.match(element('dipSummary').textContent,/基础条件 35 只 · 追高入选 1 只/);
  assert.match(element('dipMarketRule').textContent,/下跌家数 <2600家，且上证指数上涨/);
  assert.doesNotMatch(element('dipMarketRule').textContent,/前三|1\.6%/);
  assert.match(element('dipMarket').innerHTML,/2599 家 · 上证 \+0\.50%/);
  assert.doesNotMatch(element('dipMarket').innerHTML,/前三日/);
  assert.match(element('dipCoverage').textContent,/涨幅超5% 300 只/);
  assert.match(element('dipCoverage').textContent,/收盘涨停 20 只/);
  assert.match(element('dipCoverage').textContent,/均线非多头 12 只/);
  assert.doesNotMatch(element('dipCoverage').textContent,/离20日线过远|高于60日线|成交额|主力强度|下跌 300/);
  assert.match(element('dipMovementRule').textContent,/当日涨幅 >5%/);
  assert.equal(element('dipMovementLabel').textContent,'01 / 涨幅');
  assert.equal(element('dipStrategyTitle').textContent,'追高选股');
  assert.equal(element('dipSearch').attributes['aria-label'],'搜索追高选股股票');
  assert.equal(element('dipSort').attributes['aria-label'],'追高选股排序');
  assert.equal(element('dip2Rules').style.display,'');
  assert.equal(element('dipMa60Head').style.display,'');
  assert.equal(element('dipMa5Head').style.display,'');
  assert.equal(element('dipMa10Head').style.display,'');
  assert.equal((body.innerHTML.match(/<td/g)||[]).length,21);
  assert.match(body.innerHTML,/11\.70/); assert.match(body.innerHTML,/11\.50/); assert.match(body.innerHTML,/10\.80/);
  assert.match(body.innerHTML,/\+6\.00%/);
  assert.match(body.innerHTML,/color:#4caf50[^>]*>-0\.01/);
  assert.equal(vm.runInContext('_getCurrentPicksCache()[0].ts_code',ctx),'002980.SZ');
  vm.runInContext('_showBucketContainer("dip2")',ctx);
  assert.equal(element('dipPicksContainer').style.display,'');
  const count=calls.length; await vm.runInContext('refreshPicksQuotes()',ctx);
  assert.equal(calls.length,count);
  ctx.currentPicksGroup='dip'; vm.runInContext('renderDipPicksTable()',ctx);
  assert.match(body.innerHTML,/000001/); assert.doesNotMatch(body.innerHTML,/华盛昌/);
  assert.equal((body.innerHTML.match(/<td/g)||[]).length,21);
  assert.equal(element('dipStrategyTitle').textContent,'低吸选股');
  assert.equal(element('dipSearch').attributes['aria-label'],'搜索低吸选股股票');
  assert.match(element('dipMarketRule').textContent,/下跌家数 >2600.*上证下跌/);
  assert.match(element('dipMarketRule').textContent,/恰好跌−1\.6%不触发/);
  assert.equal(element('dip2Rules').style.display,'none');
  assert.equal(element('dipMa60Head').style.display,'');
  assert.equal(element('dipMa5Head').style.display,'');
  assert.equal(element('dipMa10Head').style.display,'');
  assert.match(element('dipMovementRule').textContent,/当日下跌/);
  const oldVersion=vm.runInContext('loadDipPicks()',ctx);
  ctx.currentPicksGroup='dip2'; const newVersion=vm.runInContext('loadDipPicks()',ctx);
  calls[9].resolve({ok:true,json:async()=>v2payload()}); await newVersion;
  calls[8].resolve({ok:true,json:async()=>payload('20260902')}); await oldVersion;
  assert.match(body.innerHTML,/华盛昌/); assert.doesNotMatch(body.innerHTML,/000001/);
  const v2empty=vm.runInContext('loadDipPicks()',ctx);
  calls[10].resolve({ok:true,json:async()=>v2payload([])}); await v2empty;
  assert.match(body.innerHTML,/全部个股条件/); assert.match(body.innerHTML,/colspan="21"/);
  const v2error=vm.runInContext('loadDipPicks()',ctx);
  calls[11].resolve({ok:false,json:async()=>({message:'本地行情无法读取'})}); await v2error;
  assert.match(body.innerHTML,/本地行情无法读取/);
  const v2unmet=vm.runInContext('loadDipPicks()',ctx);
  calls[12].resolve({ok:true,json:async()=>({...v2payload([]),market_passed:false,
    market:{status:'unmet',down_count:2600,index_change:.5,previous_three_up:null,previous_three:[],
      note:'下跌家数 2600 家，未少于2600家，不推荐'},
    v2_stats:{base_count:0},note:'下跌家数 2600 家，未少于2600家，不推荐'})}); await v2unmet;
  assert.match(element('dipSummary').textContent,/大盘未触发.*基础条件 0 只.*入选 0 只/);
  assert.match(element('dipMarket').innerHTML,/未少于2600家/);
  assert.doesNotMatch(element('dipMarket').innerHTML,/前三日/);
  assert.match(body.innerHTML,/未少于2600家/);
  // Each condition is a real backend parameter; default requests remain unchanged.
  assert.equal(vm.runInContext('CHASE_RULES.length',ctx),11);
  assert.equal((element('chaseConditions').innerHTML.match(/type="checkbox"/g)||[]).length,11);
  assert.equal(element('chaseToggle_volume').checked,true);
  const offVolume=vm.runInContext('_setChaseCondition("volume",false)',ctx);
  assert.equal(calls[13].url,'/api/picks/dip-v2?date=20260902&disabled=volume');
  assert.equal(countCalls.length,1); // Toggling keeps independent whole-market counts.
  assert.equal(element('chaseCount_gain').textContent,'123个符合');
  assert.equal(JSON.parse(savedPreferences.get('tradeiq.chase.disabled.v1'))[0],'volume');
  assert.equal(element('chaseToggle_volume').checked,false);
  assert.match(element('chaseSelection').textContent,/10 \/ 11.*成交量比/);
  calls[13].resolve({ok:true,json:async()=>v2payload([v2row,{...v2row,ts_code:'600489.SH',name:'中金黄金'}])}); await offVolume;
  assert.match(element('chaseComparison').textContent,/上次 0 只 → 本次 2 只.*增加 2/);
  assert.match(element('dipCoverage').textContent,/量比未过 未启用/);
  const offAmplitude=vm.runInContext('_setChaseCondition("amplitude",false)',ctx);
  const offBullish=vm.runInContext('_setChaseCondition("bullish",false)',ctx);
  assert.equal(calls[15].url,'/api/picks/dip-v2?date=20260902&disabled=volume%2Camplitude%2Cbullish');
  calls[15].resolve({ok:true,json:async()=>v2payload([v2row,{...v2row,ts_code:'600489.SH',name:'中金黄金'},{...v2row,ts_code:'600001.SH',name:'另一只'}])}); await offBullish;
  calls[14].resolve({ok:true,json:async()=>v2payload([])}); await offAmplitude;
  assert.match(element('dipSummary').textContent,/入选 3 只.*启用 8 \/ 11/);
  assert.match(body.innerHTML,/中金黄金/);
  assert.match(element('chaseComparison').textContent,/上次 2 只 → 本次 3 只/);
  assert.match(element('dipCoverage').textContent,/振幅不足 未启用.*均线非多头 未启用/);
  // Date and tab switches keep the current choices; the original selector has no switches.
  ctx.currentPicksGroup='dip'; vm.runInContext('renderDipPicksTable()',ctx);
  assert.equal(element('dipRules').style.display,'');
  assert.equal(element('dip2Rules').style.display,'none');
  ctx.currentPicksGroup='dip2'; ctx.picksSelDate='20260901';
  const switchedDate=vm.runInContext('loadDipPicks()',ctx);
  assert.equal(calls[16].url,'/api/picks/dip-v2?date=20260901&disabled=volume%2Camplitude%2Cbullish');
  assert.equal(element('chaseCount_gain').textContent,'统计中…');
  assert.equal(countCalls[1].url,'/api/picks/dip-v2/condition-counts?date=20260901');
  const dateCounts=vm.runInContext('_chaseCountsPromise',ctx);
  countCalls[1].resolve({ok:true,json:async()=>countPayload('20260901')}); await dateCounts;
  calls[16].resolve({ok:true,json:async()=>({...v2payload(),date:'20260901'})}); await switchedDate;
  assert.doesNotMatch(element('chaseComparison').textContent,/上次 .*→/);
  assert.equal(element('chaseToggle_volume').checked,false);
  assert.equal(element('dipBaseRules').style.display,'none');
  const reset=vm.runInContext('_resetChaseConditions()',ctx);
  assert.equal(calls[17].url,'/api/picks/dip-v2?date=20260901');
  calls[17].resolve({ok:true,json:async()=>({...v2payload(),date:'20260901'})}); await reset;
  assert.equal(element('chaseToggle_volume').checked,true);
  assert.equal(element('chaseToggle_amplitude').checked,true);
  assert.equal(element('chaseToggle_bullish').checked,true);
  assert.match(element('chaseSelection').textContent,/11 \/ 11.*原完整策略/);
  // Display-only missing money must remain missing rather than turn into a false zero.
  ctx.nullRow={...v2row,main_net_today:null,main_net_3d:null,main_net_5d:null,main_net_10d:null,main_net_20d:null};
  vm.runInContext('dip2PicksCache=[nullRow]; renderDipPicksTable()',ctx);
  assert.equal((body.innerHTML.match(/主力净额 — 亿元/g)||[]).length,5);
  assert.doesNotMatch(body.innerHTML,/主力净额 0.00 亿元/);
  vm.runInContext('CHASE_RULES.forEach(([key])=>_chaseDisabled.add(key)); _paintChaseConditions()',ctx);
  const allOff=vm.runInContext('loadDipPicks()',ctx);
  assert.match(calls[18].url,/disabled=market_down%2Cmarket_up%2Cgain/);
  calls[18].resolve({ok:true,json:async()=>({...v2payload([ctx.nullRow]),date:'20260901',stats:{total:5000,candidate_count:5000,eligible:5000,funds_display_missing:1}})}); await allOff;
  assert.match(element('dipSummary').textContent,/大盘条件已关闭.*启用 0 \/ 11/);
  assert.match(element('dipCoverage').textContent,/未限制当日涨幅 5000 只/);
  assert.match(element('dipCoverage').textContent,/资金缺失仅展示 1 只（不排除）/);
  const resetAll=vm.runInContext('_resetChaseConditions()',ctx);
  calls[19].resolve({ok:true,json:async()=>({...v2payload(),date:'20260901'})}); await resetAll;
  assert.deepEqual(JSON.parse(savedPreferences.get('tradeiq.chase.disabled.v1')),[]);
  // Simulate reload with the same browser storage, without reusing in-memory variables.
  const chaseSource=html.slice(html.indexOf('// ── 低吸选股：'),html.indexOf('// ── 低吸选股结束 ──'));
  const freshContext=(store=storage)=>{
    const fresh=vm.createContext({console,localStorage:store,document:{getElementById:element},escapeHtml:ctx.escapeHtml,fmtDate:ctx.fmtDate});
    vm.runInContext(chaseSource,fresh);return fresh;
  };
  savedPreferences.set('tradeiq.chase.disabled.v1',JSON.stringify(['volume','bullish','unknown',17]));
  assert.equal(vm.runInContext('_chaseSignature()',freshContext()),'volume,bullish');
  savedPreferences.set('tradeiq.chase.disabled.v1','broken JSON');
  assert.equal(vm.runInContext('_chaseSignature()',freshContext()),'');
  savedPreferences.set('tradeiq.chase.disabled.v1','{"volume":false}');
  assert.equal(vm.runInContext('_chaseSignature()',freshContext()),'');
  const blocked=freshContext({getItem(){throw Error('blocked')},setItem(){throw Error('blocked')}});
  vm.runInContext('_chaseDisabled.add("volume");_saveChasePreferences();_paintChaseConditions()',blocked);
  assert.match(element('chaseSelection').textContent,/浏览器不允许保存/);
  savedPreferences.set('tradeiq.chase.disabled.v1','[]');
  // Statistics have their own date race guard and never show a previous date's number.
  ctx.picksSelDate='20260904';const olderStats=vm.runInContext('_loadChaseCounts(picksSelDate)',ctx);
  ctx.picksSelDate='20260907';const newerStats=vm.runInContext('_loadChaseCounts(picksSelDate)',ctx);
  assert.equal(element('chaseCount_gain').textContent,'统计中…');
  countCalls[3].resolve({ok:true,json:async()=>({...countPayload('20260907'),counts:{...countPayload().counts,gain:{matched:777,evaluated:4500,missing:500,total:5000}}})});await newerStats;
  countCalls[2].resolve({ok:true,json:async()=>countPayload('20260904')});await olderStats;
  assert.equal(element('chaseCount_gain').textContent,'777个符合');
  const forced=vm.runInContext('_loadChaseCounts(picksSelDate,true)',ctx);
  countCalls[4].resolve({ok:false,json:async()=>({message:'统计无法读取'})});await forced;
  assert.equal(element('chaseCount_gain').textContent,'统计失败');
  assert.doesNotMatch(element('chaseCount_gain').textContent,/777/);
  const unavailable=vm.runInContext('_loadChaseCounts(picksSelDate,true)',ctx);
  countCalls[5].resolve({ok:true,json:async()=>({...countPayload('20260907'),daily_ready:false,note:'等待收盘日线'})});await unavailable;
  assert.equal(element('chaseCount_gain').textContent,'数据不足');
  assert.match(element('chaseCount_gain').title,/等待收盘日线/);
  // The system-tab request can finish after entering chase: no date, table or timer writes.
  ctx._picksReqSeq=0;ctx.picksCache=[];ctx.picksLiveNextIndex=0;
  ctx.window={fpInstances:{picksDate:{setDate(){ctx.dateChanged=true;}}}};
  ctx.renderPicksTable=()=>{ctx.systemRendered=(ctx.systemRendered||0)+1;};
  ctx.startPicksAutoRefresh=()=>{ctx.systemAutoStarted=true;};
  ctx.refreshPicksQuotes=async()=>{};
  vm.runInContext(html.slice(html.indexOf('async function loadPicks('),html.indexOf('async function syncPicks()')),ctx);
  ctx.currentPicksGroup='system';ctx.picksSelDate='';
  const lateSystem=vm.runInContext('loadPicks()',ctx);
  ctx.currentPicksGroup='dip2';ctx.picksSelDate='20260907';
  calls[20].resolve({ok:true,json:async()=>({date:'20260930',picks:[row]})});await lateSystem;
  assert.equal(ctx.picksSelDate,'20260907');assert.equal(ctx.systemRendered,undefined);
  assert.equal(ctx.dateChanged,undefined);assert.equal(ctx.systemAutoStarted,undefined);
  // Low-absorption controls use their own preference/cache keys and all nine labels.
  const dipCountPayload = date => ({status:'success',date,daily_ready:true,counts:Object.fromEntries(
    ['market_down','market_index','decline','return120','funds_red','funds_threshold','volume','above_ma20','amplitude','bullish'].map(key=>
      [key,{matched:key==='market_index'?0:key==='decline'?2800:321,evaluated:key==='funds_red'?0:4500,missing:key==='funds_red'?5000:500,total:5000}]))});
  savedPreferences.set('tradeiq.dip.disabled.v1',JSON.stringify(['volume','amplitude','gain','unknown']));
  savedPreferences.set('tradeiq.chase.disabled.v1',JSON.stringify(['bullish']));
  const bothFresh=freshContext();
  assert.equal(vm.runInContext('_dipSignature()',bothFresh),'volume,amplitude');
  assert.equal(vm.runInContext('_chaseSignature()',bothFresh),'bullish');
  vm.runInContext('_paintDipConditions()',bothFresh);
  assert.equal(element('dipToggle_volume').checked,false);
  assert.match(element('dipConditions').innerHTML,/class="chase-count"/);
  assert.equal((element('dipConditions').innerHTML.match(/type="checkbox"/g)||[]).length,10);
  savedPreferences.set('tradeiq.dip.disabled.v1','bad');
  assert.equal(vm.runInContext('_dipSignature()',freshContext()),'');
  savedPreferences.set('tradeiq.dip.disabled.v1','[]');
  vm.runInContext('_dipDisabled.add("volume");_saveDipPreferences();_paintDipConditions()',blocked);
  assert.match(element('dipSelection').textContent,/浏览器不允许保存/);
  ctx.currentPicksGroup='dip';ctx.picksSelDate='20260907';
  const dipLoad=vm.runInContext('loadDipPicks()',ctx);
  assert.equal(calls.at(-1).url,'/api/picks/dip?date=20260907');
  assert.equal(dipCountCalls.at(-1).url,'/api/picks/dip/condition-counts?date=20260907');
  const dipStatsPromise=vm.runInContext('_dipConditionCountsPromise',ctx);
  dipCountCalls.at(-1).resolve({ok:true,json:async()=>dipCountPayload('20260907')});await dipStatsPromise;
  assert.equal(element('dipCount_decline').textContent,'2800个符合');
  assert.equal(element('dipCount_market_index').textContent,'全军覆没');
  assert.equal(element('dipCount_funds_red').textContent,'数据不足');
  calls.at(-1).resolve({ok:true,json:async()=>payload('20260907',[])});await dipLoad;
  assert.equal(element('dipRules').style.display,'');
  assert.equal(element('dip2Rules').style.display,'none');
  const dipStatCount=dipCountCalls.length;
  const dipOff=vm.runInContext('_setDipCondition("volume",false)',ctx);
  assert.equal(calls.at(-1).url,'/api/picks/dip?date=20260907&disabled=volume');
  assert.equal(dipCountCalls.length,dipStatCount);
  assert.equal(element('dipCount_decline').textContent,'2800个符合');
  assert.equal(element('dipToggle_volume').checked,false);
  assert.deepEqual(JSON.parse(savedPreferences.get('tradeiq.dip.disabled.v1')),['volume']);
  assert.deepEqual(JSON.parse(savedPreferences.get('tradeiq.chase.disabled.v1')),['bullish']);
  assert.equal(vm.runInContext('_dipSignature()',freshContext()),'volume');
  calls.at(-1).resolve({ok:true,json:async()=>payload('20260907',[row])});await dipOff;
  assert.match(element('dipComparison').textContent,/上次 0 只 → 本次 1 只/);
  assert.match(element('dipCoverage').textContent,/量比未过 未启用/);
  const dipSlow=vm.runInContext('_setDipCondition("amplitude",false)',ctx);const dipSlowCall=calls.at(-1);
  const dipFast=vm.runInContext('_setDipCondition("decline",false)',ctx);
  calls.at(-1).resolve({ok:true,json:async()=>({...payload('20260907',[row,{...row,ts_code:'000002.SZ'}]),stats:{total:5000,candidate_count:5000,eligible:50}})});await dipFast;
  dipSlowCall.resolve({ok:true,json:async()=>payload('20260907',[])});await dipSlow;
  assert.match(element('dipSummary').textContent,/入选 2 只.*启用 7 \/ 10/);
  assert.match(element('dipCoverage').textContent,/未限制当日涨跌 5000.*振幅不足 未启用/);
  ctx.picksSelDate='20260908';const dipOlder=vm.runInContext('_loadDipCounts(picksSelDate)',ctx);const oldDipCountCall=dipCountCalls.at(-1);
  ctx.picksSelDate='20260909';const dipNewer=vm.runInContext('_loadDipCounts(picksSelDate)',ctx);
  assert.equal(element('dipCount_decline').textContent,'统计中…');
  dipCountCalls.at(-1).resolve({ok:true,json:async()=>({...dipCountPayload('20260909'),counts:{...dipCountPayload().counts,decline:{matched:999,evaluated:5000,total:5000,missing:0}}})});await dipNewer;
  oldDipCountCall.resolve({ok:true,json:async()=>dipCountPayload('20260908')});await dipOlder;
  assert.equal(element('dipCount_decline').textContent,'999个符合');
  const dipForced=vm.runInContext('_loadDipCounts(picksSelDate,true)',ctx);
  dipCountCalls.at(-1).resolve({ok:false,json:async()=>({message:'统计失败原因'})});await dipForced;
  assert.equal(element('dipCount_decline').textContent,'统计失败');
  const dipWaiting=vm.runInContext('_loadDipCounts(picksSelDate,true)',ctx);
  dipCountCalls.at(-1).resolve({ok:true,json:async()=>({...dipCountPayload('20260909'),daily_ready:false,note:'等待收盘'})});await dipWaiting;
  assert.equal(element('dipCount_decline').textContent,'数据不足');
  const dipReset=vm.runInContext('_resetDipConditions()',ctx);
  calls.at(-1).resolve({ok:true,json:async()=>payload('20260909')});await dipReset;
  assert.deepEqual(JSON.parse(savedPreferences.get('tradeiq.dip.disabled.v1')),[]);
  assert.equal(element('dipToggle_decline').checked,true);
  assert.match(element('dipSelection').textContent,/10 \/ 10.*原完整策略/);
  assert.equal(vm.runInContext('_dipSignature()',freshContext()),'');
  // Diagnostics and paged previews never change saved switches until Apply.
  ctx.currentPicksGroup='dip';ctx.picksSelDate='20260930';
  const fakeDiagnostic = (group='dip', date='20260930', remove=null) => ({status:'success',date,daily_ready:true,
    current_count:0,kind:'market',message:'上证当日未下跌，当前大盘条件未通过。',
    singles:[{remove:['volume'],count:0},{remove:[group==='dip'?'market_index':'market_down'],count:6}],
    pairs:[],pairs_checked:false,pair_total:0,market:{down_count:2824,index_change:.31,index_note:'上证当日未下跌'},
    data_summary:{missing_stock_count:23,invalid_daily_count:2,only_missing_count:4,by_condition:[{key:'return120',count:23}]},
    preview:remove?{remove,count:60,offset:0,limit:50,rows:[{...row,name:'<模拟候选>',removed_states:Object.fromEntries(remove.map(key=>[key,'unmet']))}]}:null});
  const storedBefore=JSON.stringify([...savedPreferences]);
  vm.runInContext('_clearPickDiagnostics();_showPickDiagnostics({date:"20260930",daily_ready:true,results:[]},"dip",picksSelDate,_dipSignature())',ctx);
  assert.equal(diagnosticCalls.at(-1).url,'/api/picks/dip/diagnostics?date=20260930');
  assert.match(element('pickDiagnosticTitle').textContent,/为什么没选出来/);
  const diagnostic=vm.runInContext('_loadPickDiagnostics()',ctx);
  diagnosticCalls.at(-1).resolve({ok:true,json:async()=>fakeDiagnostic()});await diagnostic;
  assert.match(element('pickDiagnosticSingles').innerHTML,/查看这6只.*应用此组合/);
  assert.match(element('pickDiagnosticSingles').innerHTML,/仍然0只/);
  assert(element('pickDiagnosticSingles').innerHTML.indexOf('查看这6只') < element('pickDiagnosticSingles').innerHTML.indexOf('仍然0只'));
  assert.match(element('pickDiagnosticDataNote').textContent,/23只.*2只/);
  assert.match(element('pickDiagnosticGaps').textContent,/可能重叠/);
  assert.equal(JSON.stringify([...savedPreferences]),storedBefore);
  const preview=vm.runInContext('_previewPickDiagnostic(["market_index"])',ctx);
  assert.match(diagnosticCalls.at(-1).url,/remove=market_index&offset=0&limit=50/);
  diagnosticCalls.at(-1).resolve({ok:true,json:async()=>fakeDiagnostic('dip','20260930',['market_index'])});await preview;
  assert.match(element('pickDiagnosticPreviewBody').innerHTML,/&lt;模拟候选&gt;/);
  assert.match(element('pickDiagnosticPreviewBody').innerHTML,/上证下跌及前三日走势.*未通过/);
  assert.equal(element('pickDiagnosticNext').disabled,false);
  assert.equal(JSON.stringify([...savedPreferences]),storedBefore);
  const clickDiagnosticStock=action=>element('pickDiagnosticPreviewBody').onclick({target:{closest(){return {dataset:{diagStock:action},closest(){return {dataset:{tsCode:'000001.SZ',stockName:'模拟候选'}};}};}}});
  clickDiagnosticStock('kline');assert.equal(ctx.kline[2].pickDate,'20260930');
  clickDiagnosticStock('flow');assert.equal(ctx.flow[2],'main');
  const nextPreview=vm.runInContext('_previewPickDiagnostic(["market_index"],50)',ctx);
  assert.match(diagnosticCalls.at(-1).url,/offset=50/);
  diagnosticCalls.at(-1).resolve({ok:true,json:async()=>({...fakeDiagnostic('dip','20260930',['market_index']),preview:{...fakeDiagnostic('dip','20260930',['market_index']).preview,offset:50,count:51}})});await nextPreview;
  assert.equal(element('pickDiagnosticNext').disabled,true);
  assert.equal(element('pickDiagnosticPrev').disabled,false);
  const apply=vm.runInContext('_applyPickDiagnostic(["market_index"])',ctx);
  assert.equal(calls.at(-1).url,'/api/picks/dip?date=20260930&disabled=market_index');
  assert.deepEqual(JSON.parse(savedPreferences.get('tradeiq.dip.disabled.v1')),['market_index']);
  assert.deepEqual(JSON.parse(savedPreferences.get('tradeiq.chase.disabled.v1')),['bullish']);
  assert.equal(element('pickDiagnostics').style.display,'none');
  calls.at(-1).resolve({ok:true,json:async()=>payload('20260930',[row])});await apply;
  assert.equal(element('pickDiagnosticTitle').textContent,'条件影响对比');
  const diagRequestsBefore=diagnosticCalls.length;
  vm.runInContext('_applyPickDiagnostic(["market_index"])',ctx); // Already disabled is invalid.
  assert.equal(diagnosticCalls.length,diagRequestsBefore);
  const resetDip=vm.runInContext('_resetDipConditions()',ctx);
  calls.at(-1).resolve({ok:true,json:async()=>payload('20260930',[row])});await resetDip;
  // Two-item diagnosis lists only positive combinations and states its checked range.
  vm.runInContext('_clearPickDiagnostics();_showPickDiagnostics({date:"20260930",daily_ready:true,results:[{}]},"dip",picksSelDate,_dipSignature())',ctx);
  const pairLoad=vm.runInContext('_loadPickDiagnostics()',ctx);
  diagnosticCalls.at(-1).resolve({ok:true,json:async()=>({...fakeDiagnostic(),pairs_checked:true,pair_total:36,pairs:[{remove:['volume','amplitude'],count:3},{remove:['volume','return120'],count:0}]})});await pairLoad;
  assert.match(element('pickDiagnosticPairsNote').textContent,/36.*1.*同时关闭/);
  assert.match(element('pickDiagnosticPairs').innerHTML,/成交量比.*20日平均振幅.*查看这3只/);
  assert.doesNotMatch(element('pickDiagnosticPairs').innerHTML,/120日/);
  const oldPreview=vm.runInContext('_previewPickDiagnostic(["volume","amplitude"])',ctx);const oldPreviewCall=diagnosticCalls.at(-1);
  vm.runInContext('_closeDiagnosticPreview()',ctx);
  oldPreviewCall.resolve({ok:true,json:async()=>fakeDiagnostic('dip','20260930',['volume','amplitude'])});await oldPreview;
  assert.equal(element('pickDiagnosticPreview').style.display,'none');
  const staleDiagnosis=vm.runInContext('_loadPickDiagnostics()',ctx);const staleDiagnosisCall=diagnosticCalls.at(-1);
  ctx.currentPicksGroup='dip2';ctx.picksSelDate='20260929';
  vm.runInContext('_clearPickDiagnostics();_showPickDiagnostics({date:"20260929",daily_ready:true,results:[{}]},"dip2",picksSelDate,_chaseSignature())',ctx);
  const chaseDiagnosis=vm.runInContext('_loadPickDiagnostics()',ctx);
  assert.equal(diagnosticCalls.at(-1).url,'/api/picks/dip-v2/diagnostics?date=20260929');
  diagnosticCalls.at(-1).resolve({ok:true,json:async()=>fakeDiagnostic('dip2','20260929')});await chaseDiagnosis;
  staleDiagnosisCall.resolve({ok:true,json:async()=>fakeDiagnostic('dip','20260930')});await staleDiagnosis;
  assert.match(element('pickDiagnosticMessage').textContent,/20260929/);
  assert.doesNotMatch(element('pickDiagnosticMessage').textContent,/20260930/);
  const lateChasePreview=vm.runInContext('_previewPickDiagnostic(["market_down"])',ctx);const latePreviewCall=diagnosticCalls.at(-1);
  vm.runInContext('_chaseDisabled.add("volume");_saveChasePreferences()',ctx);
  latePreviewCall.resolve({ok:true,json:async()=>fakeDiagnostic('dip2','20260929',['market_down'])});await lateChasePreview;
  assert.equal(vm.runInContext('_validDiagnosticRemove(["market_down"])',ctx),null);
  vm.runInContext('_chaseDisabled.delete("volume");_saveChasePreferences();_clearPickDiagnostics();_showPickDiagnostics({date:"20260929",daily_ready:true,results:[{}]},"dip2",picksSelDate,_chaseSignature())',ctx);
  const diagError=vm.runInContext('_loadPickDiagnostics()',ctx);
  diagnosticCalls.at(-1).resolve({ok:false,json:async()=>({message:'本地读取失败'})});await diagError;
  assert.match(element('pickDiagnosticMessage').textContent,/诊断失败.*本地读取失败/);
  assert.equal(element('pickDiagnosticToggle').textContent,'重试诊断');
  const requestsBeforeWaiting=diagnosticCalls.length;
  vm.runInContext('_clearPickDiagnostics();_showPickDiagnostics({date:"20261008",daily_ready:false,results:[],note:"等待完整收盘数据"},"dip2",picksSelDate,_chaseSignature())',ctx);
  assert.equal(diagnosticCalls.length,requestsBeforeWaiting);
  assert.equal(element('pickDiagnosticToggle').disabled,true);
  assert.match(element('pickDiagnosticMessage').textContent,/等待完整收盘/);
  // ── 条件设置整块折叠 ──
  const toggleButton = element('dipConditionsToggle');
  const panel = element('dipConditionsPanel');
  const panelOpen = html.indexOf('<div id="dipConditionsPanel">');
  const panelInner = html.slice(panelOpen, html.indexOf('</section>', panelOpen));
  assert.match(html, /id="dipConditionsToggle"[^>]*aria-controls="dipConditionsPanel"/);
  assert.match(html, /<div id="dipConditionsPanel">\s*<div id="dipRules">/);
  assert.match(panelInner, /id="dipRules"/);          // 低吸开关在可折叠容器内
  assert.match(panelInner, /id="dip2Rules"/);         // 追高开关在同一个容器内
  assert.doesNotMatch(html.slice(panelOpen, panelOpen + 200), /dipConditionsToggle/); // 开关按钮留在容器外, 折叠后仍可展开
  vm.runInContext('_dipDisabled = new Set(); _chaseDisabled = new Set()', ctx);
  ctx.currentPicksGroup = 'dip';
  vm.runInContext('_paintDipConditions()', ctx);
  assert.equal(panel.style.display, '');              // 默认展开
  assert.equal(toggleButton.attributes['aria-expanded'], 'true');
  assert.match(toggleButton.textContent, /收起条件设置 · 已开启 10 \/ 10/);
  vm.runInContext('_toggleDipConditionsPanel()', ctx);
  assert.equal(panel.style.display, 'none');
  assert.equal(toggleButton.attributes['aria-expanded'], 'false');
  assert.match(toggleButton.textContent, /展开条件设置 · 已开启 10 \/ 10/);
  assert.equal(savedPreferences.get('tradeiq.dip.conditionsCollapsed.v1'), '1');
  vm.runInContext('_dipDisabled.add("volume"); _paintDipConditions()', ctx);  // 折叠时计数仍跟随开关
  assert.match(toggleButton.textContent, /展开条件设置 · 已开启 9 \/ 10/);
  assert.equal(panel.style.display, 'none');
  ctx.currentPicksGroup = 'dip2';
  vm.runInContext('_paintChaseConditions()', ctx);    // 追高复用同一面板
  assert.match(toggleButton.textContent, /展开条件设置 · 已开启 11 \/ 11/);
  assert.equal(panel.style.display, 'none');
  ctx.currentPicksGroup = 'dip';
  vm.runInContext('_toggleDipConditionsPanel()', ctx);
  assert.equal(panel.style.display, '');
  assert.equal(savedPreferences.get('tradeiq.dip.conditionsCollapsed.v1'), '0');
  vm.runInContext('_toggleDipConditionsPanel(); _initDipPanelState(); _paintDipConditions()', ctx);  // 重新载入恢复折叠
  assert.equal(panel.style.display, 'none');
  assert.match(toggleButton.textContent, /展开条件设置/);
  // ── 市场温度下方的近5日热门榜: 同日、只读精简版、迟到响应与切换时不写视图 ──
  const hotInfo = element('dipHotSectorInfo');
  const hotBoard = element('dipHotSectorBoard');
  assert.match(html, /id="dipHotSectorBoard" class="hot-heat-wrap hot-heat-compact"/);
  assert.ok(html.indexOf('id="dipMarketContext"') < html.indexOf('id="dipHotSectorSection"'), '热门榜应排在市场温度之后');
  const hotItem = {sector:'PCB', recent5_count:24, appearance5:3, today_count:8,
    heat:{rank:1, key:'cooling', label:'已经降温', reason:'涨停群体减少', in_board:true},
    timeline:[{date:'20260916', count:11}, {date:'20260924', count:8}]};
  const hotPayload = {status:'ready', date:'20260924',
    summary:{recent5_complete:true, recent5_dates:['20260916','20260924']}, sectors:[hotItem]};
  vm.runInContext('_resetDipHotBoard()', ctx);
  assert.equal(hotInfo.textContent, '正在读取同日热门板块…');
  const hotLoad = vm.runInContext('_loadDipHotBoard("20260924", () => true)', ctx);
  assert.equal(hotCalls.at(-1).url, '/api/hot-sectors?date=20260924');
  assert.match(hotInfo.textContent, /正在读取热门板块/);
  hotCalls.at(-1).resolve({ok:true, json:async()=>hotPayload}); await hotLoad;
  assert.match(hotInfo.textContent, /2026-09-24 · 1个板块/);
  assert.match(hotBoard.innerHTML, /<table class="hot-heat-table">/);
  assert.match(hotBoard.innerHTML, /PCB/);
  assert.match(hotBoard.innerHTML, /hot-stage-cooling/);
  assert.match(hotBoard.innerHTML, /hot-cell-4/);            // 11 只 -> 10只及以上
  assert.match(hotBoard.innerHTML, /09-16/);
  assert.doesNotMatch(hotBoard.innerHTML, /<tfoot/, '精简版不留原表脚的长说明');
  assert.doesNotMatch(hotBoard.innerHTML, /data-hot-sector/, '精简版板块名不可点, 详情去热门板块页');
  const staleHot = vm.runInContext('_loadDipHotBoard("20260922", () => true)', ctx);
  vm.runInContext('_resetDipHotBoard()', ctx);
  hotCalls.at(-1).resolve({ok:true, json:async()=>hotPayload}); await staleHot;
  assert.equal(hotInfo.textContent, '正在读取同日热门板块…');   // 迟到响应不覆盖重置
  assert.equal(hotBoard.innerHTML, '');
  const hotInactive = vm.runInContext('_loadDipHotBoard("20260924", () => false)', ctx);
  hotCalls.at(-1).resolve({ok:true, json:async()=>hotPayload}); await hotInactive;
  assert.equal(hotBoard.innerHTML, '', '已切走时不写入当前视图');
  const hotFail = vm.runInContext('_loadDipHotBoard("20260924", () => true)', ctx);
  hotCalls.at(-1).resolve({ok:false, json:async()=>({message:'本地读取失败'})}); await hotFail;
  assert.match(hotInfo.textContent, /读取失败/);
  assert.match(hotBoard.innerHTML, /本地读取失败/);
  assert.match(hotBoard.innerHTML, /没有用其他日期替代/);
  // Editable low-absorption parameters persist, counts/diagnostics use the same
  // settings, and stale requests cannot replace a newer configuration.
  ctx.currentPicksGroup='dip';ctx.picksSelDate='20260928';
  vm.runInContext('_dipDisabled.clear();_paintDipConditions()',ctx);
  const setInputs=(days,threshold)=>{
    element('dipReturnDays').value=String(days);element('dipreturnOp').value='le';element('dipreturnThreshold').value=String(threshold);
    element('dipchangeOp').value='ge';element('dipchangeThreshold').value='-2';
  };
  setInputs(0,10);const beforeInvalid=calls.length;
  vm.runInContext('_applyDipParameters()',ctx);
  assert.equal(calls.length,beforeInvalid);assert.match(element('dipParameterMessage').textContent,/1～250/);
  setInputs(20,10);const parameterSlow=vm.runInContext('_applyDipParameters()',ctx);
  const oldParameterCall=calls.at(-1),oldParameterCount=dipCountCalls.at(-1);
  const oldParameterCountPromise=vm.runInContext('_dipConditionCountsPromise',ctx);
  assert.match(oldParameterCall.url,/return_days=20&return_op=le&return_threshold=10&change_op=ge&change_threshold=-2/);
  assert.match(oldParameterCount.url,/return_days=20&return_op=le&return_threshold=10/);
  setInputs(30,20);const parameterFast=vm.runInContext('_applyDipParameters()',ctx);
  const latestCount=dipCountCalls.at(-1);const latestCountPromise=vm.runInContext('_dipConditionCountsPromise',ctx);
  calls.at(-1).resolve({ok:true,json:async()=>payload('20260928',[row])});await parameterFast;
  latestCount.resolve({ok:true,json:async()=>({...dipCountPayload('20260928'),counts:{...dipCountPayload().counts,return120:{matched:42,evaluated:5000,missing:0,total:5000}}})});await latestCountPromise;
  oldParameterCall.resolve({ok:true,json:async()=>payload('20260928',[])});await parameterSlow;
  oldParameterCount.resolve({ok:true,json:async()=>dipCountPayload('20260928')});await oldParameterCountPromise;
  assert.match(element('dipSummary').textContent,/入选 1 只/);
  assert.equal(element('dipCount_return120').textContent,'42个符合');
  assert.equal(element('dipLabel_return120').textContent,'30日涨幅 ≤20%');
  assert.equal(element('dipLabel_decline').textContent,'当日涨跌幅 ≥-2%');
  assert.equal(element('dipReturnHead').textContent,'30日涨幅');
  assert.match(vm.runInContext('_diagnosticUrl(_pickDiagnosticBasis)',ctx),/return_days=30&return_op=le&return_threshold=20/);
  const parametersReload=freshContext();
  assert.equal(vm.runInContext('_dipParameters.return_days',parametersReload),30);
  assert.equal(vm.runInContext('_dipParameters.change_threshold',parametersReload),-2);
  assert.equal(vm.runInContext('_chaseSignature()',parametersReload),'');
  savedPreferences.set('tradeiq.dip.parameters.v1','{"return_days":251}');
  assert.equal(vm.runInContext('_dipParameters.return_days',freshContext()),120);
  savedPreferences.set('tradeiq.dip.parameters.v1','not json');
  assert.equal(vm.runInContext('_dipParameters.change_threshold',freshContext()),0);
  // Strict daily ranges share the persistence and request guards of single comparisons.
  assert.equal(vm.runInContext('_dipParameters.change_mode',freshContext()),'single');
  vm.runInContext('_paintDipConditions()',ctx);
  element('dipChangeMode').value='range';
  vm.runInContext('_setDipChangeMode("range")',ctx);
  assert.equal(element('dipChangeSingle').hidden,true);
  assert.equal(element('dipChangeRange').hidden,false);
  element('dipChangeLower').value='-3';element('dipChangeUpper').value='2';
  const rangeApply=vm.runInContext('_applyDipParameters()',ctx);
  assert.match(calls.at(-1).url,/change_mode=range&change_lower=-3&change_upper=2/);
  assert.match(dipCountCalls.at(-1).url,/change_mode=range&change_lower=-3&change_upper=2/);
  const rangeCountPromise=vm.runInContext('_dipConditionCountsPromise',ctx);
  dipCountCalls.at(-1).resolve({ok:true,json:async()=>dipCountPayload('20260928')});await rangeCountPromise;
  calls.at(-1).resolve({ok:true,json:async()=>payload('20260928',[row])});await rangeApply;
  assert.equal(element('dipLabel_decline').textContent,'当日涨跌幅 >-3% 且 <2%');
  assert.match(vm.runInContext('_diagnosticUrl(_pickDiagnosticBasis)',ctx),/change_mode=range&change_lower=-3&change_upper=2/);
  const rangeReload=freshContext();
  assert.equal(vm.runInContext('_dipParameters.change_mode',rangeReload),'range');
  assert.equal(vm.runInContext('_dipParameters.change_lower',rangeReload),-3);
  const rangeSignature=vm.runInContext('_dipParametersSignature()',ctx),rangeCalls=calls.length;
  element('dipChangeLower').value='2';element('dipChangeUpper').value='2';
  vm.runInContext('_applyDipParameters()',ctx);
  assert.equal(calls.length,rangeCalls);
  assert.equal(vm.runInContext('_dipParametersSignature()',ctx),rangeSignature);
  assert.match(element('dipParameterMessage').textContent,/下限必须小于上限/);
  // Live counts wait for the selected batch, and all follow-up requests pin it.
  let liveEpoch=0,liveBatch='';
  ctx.PicksIntraday={query(date,force){if(force){liveEpoch++;liveBatch='';}return 'intraday=1'+(liveBatch?'&snapshot_id='+liveBatch:'');},token(){return 'live'+liveEpoch;},capture(data,basis){if(basis!==this.token())return false;liveBatch=data.snapshot_id;return true;}};
  const countBefore=dipCountCalls.length;
  const liveLoad=vm.runInContext('loadDipPicks(true)',ctx);
  assert.equal(dipCountCalls.length,countBefore,'数量标签必须等待名单批次');
  assert.match(calls.at(-1).url,/intraday=1/);
  calls.at(-1).resolve({ok:true,json:async()=>({...payload('20260928',[row]),daily_ready:false,selection_ready:true,snapshot_id:'pin1',intraday_estimate:true})});
  await liveLoad;
  assert.match(dipCountCalls.at(-1).url,/intraday=1&snapshot_id=pin1/);
  assert.equal(element('dipPriceHead').textContent,'最新价');
  assert.doesNotMatch(element('dipSummary').textContent,/等待数据/);
  assert.match(vm.runInContext('_diagnosticUrl(_pickDiagnosticBasis)',ctx),/snapshot_id=pin1/);
  const liveCountsPromise=vm.runInContext('_dipConditionCountsPromise',ctx);
  dipCountCalls.at(-1).resolve({ok:true,json:async()=>({...dipCountPayload('20260928'),daily_ready:false,selection_ready:true,snapshot_id:'pin1'})});
  await liveCountsPromise;
  assert.notEqual(element('dipCount_volume').textContent,'数据不足');
  console.log('PASS: isolated persisted dip/chase preferences, 10/11 whole-market badges, zero vs missing data, independent counts/config/date races and refresh, original selector, T+1..T+5, independent diagnostics/preview/pagination/Apply and stale-response guards, collapsible condition panel shared by dip/chase, compact hot-sector board under market temperature');
})().catch(error=>{console.error(error);process.exitCode=1;});
