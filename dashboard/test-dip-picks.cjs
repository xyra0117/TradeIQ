const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'', textContent:'', dataset:{}, style:{}, value:id==='dipSort'?'code':''});
  return elements.get(id);
};
const calls = [];
const ctx = vm.createContext({console, Number, encodeURIComponent,
  document:{getElementById:element}, currentPicksGroup:'dip', picksSelDate:'20260924',
  fmtDate:d=>d,
  escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
  stopPicksAutoRefresh(){ctx.stopped=(ctx.stopped||0)+1;},
  fetch(url){return new Promise((resolve,reject)=>calls.push({url,resolve,reject}));},
  showKlineModal(...args){ctx.kline=args;}, openStockFlowDetail(...args){ctx.flow=args;},
});
vm.runInContext(html.slice(html.indexOf('// ── 低吸选股：'), html.indexOf('// ── 低吸选股结束 ──')), ctx);
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
assert.equal((body.innerHTML.match(/<td/g)||[]).length, 36);
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
vm.runInContext(html.slice(html.indexOf('function _loadCurrentPicksSub()'), html.indexOf('// ===== 累计净流入门槛控件')),ctx);
vm.runInContext(html.slice(html.indexOf('function _showBucketContainer(group)'), html.indexOf('// 兼容旧 call site')),ctx);
vm.runInContext('_showBucketContainer("dip")',ctx);
assert.equal(element('dipPicksContainer').style.display, '');
assert.equal(element('surgePicksContainer').style.display, 'none');
assert.equal(element('posPicksTable').style.display, 'none');
assert.equal(vm.runInContext('_getCurrentPicksCache().length',ctx), 2);
const tabsSource = html.slice(html.indexOf('const PICKS_SUB_TABS ='),html.indexOf('function _renderPicksSubTabs'));
vm.runInContext(tabsSource,ctx);
assert.equal(vm.runInContext('PICKS_SUB_TABS.em.find(t=>t.key==="dip").label',ctx),'低吸选股');
assert.equal(vm.runInContext('PICKS_SUB_TABS.em.find(t=>t.key==="dip2").label',ctx),'低吸选股2.0');

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
  const away=vm.runInContext('loadDipPicks()',ctx); ctx.currentPicksGroup='system';
  calls[2].resolve({ok:true,json:async()=>payload('20260922')}); await away;
  assert.doesNotMatch(element('dipSummary').textContent,/20260922/);
  ctx.currentPicksGroup='dip';
  const empty=vm.runInContext('loadDipPicks()',ctx);
  calls[3].resolve({ok:true,json:async()=>({...payload('20260923',[]),market_passed:false,
    market:{status:'unmet',down_count:2000,index_change:-1,note:'未超过2800家，不推荐'},note:'大盘未触发，不推荐'})}); await empty;
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
  const v2row={...row,ts_code:'002980.SZ',name:'华盛昌',trade_date:'20260902',
    ma60:14,ma20_bias_pct:4.2,main_strength5_pct:7.3,main_net_today:-1e6};
  const v2payload=(rows=[v2row])=>({...payload('20260902',rows),strategy:'dip-picks-v2',
    v2_stats:{base_count:35,too_far_ma20:20,above_ma60:5,invalid_amount:2,weak_main5:7,matched:rows.length},
    note:rows.length?'三个新增条件匹配':'原低吸名单中没有同时满足三个新增条件的股票，不推荐'});
  const v2=vm.runInContext('_loadCurrentPicksSub()',ctx);
  assert.equal(calls[7].url,'/api/picks/dip-v2?date=20260902');
  calls[7].resolve({ok:true,json:async()=>v2payload()}); await v2;
  assert.match(element('dipSummary').textContent,/原低吸 35 只 · 2.0入选 1 只/);
  assert.match(element('dipCoverage').textContent,/离20日线过远 20 只/);
  assert.match(element('dipCoverage').textContent,/5日主力强度不足 7 只/);
  assert.equal(element('dipStrategyTitle').textContent,'低吸选股2.0');
  assert.equal(element('dip2Rules').style.display,'');
  assert.equal(element('dipMa60Head').style.display,'');
  assert.equal((body.innerHTML.match(/<td/g)||[]).length,21);
  assert.match(body.innerHTML,/4\.20%/); assert.match(body.innerHTML,/7\.30%/);
  assert.match(body.innerHTML,/color:#4caf50[^>]*>-0\.01/);
  assert.equal(vm.runInContext('_getCurrentPicksCache()[0].ts_code',ctx),'002980.SZ');
  vm.runInContext('_showBucketContainer("dip2")',ctx);
  assert.equal(element('dipPicksContainer').style.display,'');
  const count=calls.length; await vm.runInContext('refreshPicksQuotes()',ctx);
  assert.equal(calls.length,count);
  ctx.currentPicksGroup='dip'; vm.runInContext('renderDipPicksTable()',ctx);
  assert.match(body.innerHTML,/000001/); assert.doesNotMatch(body.innerHTML,/华盛昌/);
  assert.equal((body.innerHTML.match(/<td/g)||[]).length,18);
  assert.equal(element('dipStrategyTitle').textContent,'低吸选股');
  assert.equal(element('dip2Rules').style.display,'none');
  assert.equal(element('dipMa60Head').style.display,'none');
  const oldVersion=vm.runInContext('loadDipPicks()',ctx);
  ctx.currentPicksGroup='dip2'; const newVersion=vm.runInContext('loadDipPicks()',ctx);
  calls[9].resolve({ok:true,json:async()=>v2payload()}); await newVersion;
  calls[8].resolve({ok:true,json:async()=>payload('20260902')}); await oldVersion;
  assert.match(body.innerHTML,/华盛昌/); assert.doesNotMatch(body.innerHTML,/000001/);
  const v2empty=vm.runInContext('loadDipPicks()',ctx);
  calls[10].resolve({ok:true,json:async()=>v2payload([])}); await v2empty;
  assert.match(body.innerHTML,/三个新增条件/); assert.match(body.innerHTML,/colspan="21"/);
  const v2error=vm.runInContext('loadDipPicks()',ctx);
  calls[11].resolve({ok:false,json:async()=>({message:'本地行情无法读取'})}); await v2error;
  assert.match(body.innerHTML,/本地行情无法读取/);
  console.log('PASS: both dip versions, independent caches and tab races, research fields/coverage, T+1..T+5, fund colors, search/sort and charts');
})().catch(error=>{console.error(error);process.exitCode=1;});
