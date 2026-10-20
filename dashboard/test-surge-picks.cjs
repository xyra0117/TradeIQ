const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
assert.match(html, /key: 'surge', label: '冲高观察'/);
assert.match(html, /currentPicksGroup === 'surge'\) return loadSurgePicks/);
assert.match(html, /currentPicksGroup === 'surge'\) return surgePicksCache/);
assert.match(html, /if \(currentPicksGroup === 'surge'\) return;/);
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',style:{},value:id==='surgeSort'?'code':''});
  return elements.get(id);
};
const calls=[];
const ctx=vm.createContext({console,Number,Infinity,encodeURIComponent,
  document:{getElementById:element},
  currentPicksGroup:'surge',picksSelDate:'20260911',
  fmtDate:d=>d, renderSectorPills(){return "";},
  escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
  stopPicksAutoRefresh(){ctx.stopped=(ctx.stopped||0)+1;},
  fetch(url){return new Promise((resolve,reject)=>calls.push({url,resolve,reject}));},
  showKlineModal(...args){ctx.kline=args;},openStockFlowDetail(...args){ctx.flow=args;},
});
ctx._fetchPicksResult = (...args) => ctx.fetch(...args);
ctx.PicksResultCache = {invalidate(){}};
vm.runInContext(html.slice(html.indexOf('// ── 冲高观察：'),html.indexOf('// ── 冲高观察结束 ──')),ctx);
const row={ts_code:'000001.SZ',name:'<测试>',trade_date:'20260911',close:12,change_pct:2,amplitude20:5.5,
  ma20:11,ret20:21,momentum:true,breakout:false,main_strength5:5,fund_tag:true,
  outcome10:{status:'complete',high_pct:15,close_pct:-2}};
ctx.rows=[row,{...row,ts_code:'000002.SZ',name:'另一只',main_strength5:null,fund_tag:false,
  outcome10:{status:'pending',completed_days:2}}];
vm.runInContext('surgePicksCache=rows; _surgePayload={daily_ready:true}; renderSurgePicksTable()',ctx);
const body=element('surgePicksBody');
assert.match(body.innerHTML,/&lt;测试&gt;/);
assert.match(body.innerHTML,/\+15\.00%/);
assert.match(body.innerHTML,/-2\.00%/);
assert.match(body.innerHTML,/观察中 2\/10日/);
assert.match(body.innerHTML,/数据不足/);
assert.equal((body.innerHTML.match(/<td/g)||[]).length,22);
let stopped=false;
const click = action => body.onclick({stopPropagation(){stopped=true;},target:{closest(){return {
  dataset:{surgeAction:action},closest(){return {dataset:{tsCode:'000001.SZ',stockName:'测试',pickDate:'20260911'}};}
};}}});
click('kline');assert.ok(stopped);assert.equal(ctx.kline[2].pickDate,'20260911');
click('flow');assert.equal(ctx.flow[2],'main');
element('surgeSearch').value='000002';vm.runInContext('renderSurgePicksTable()',ctx);
assert.doesNotMatch(body.innerHTML,/000001/);assert.match(body.innerHTML,/000002/);
element('surgeSearch').value='无此股';vm.runInContext('renderSurgePicksTable()',ctx);assert.match(body.innerHTML,/没有匹配/);
element('surgeSearch').value='';element('surgeSort').value='main_strength5';vm.runInContext('renderSurgePicksTable()',ctx);
assert.ok(body.innerHTML.indexOf('000001')<body.innerHTML.indexOf('000002'),'缺失资金放最后');
const payload=(date,results=[row])=>({date,daily_ready:true,count:results.length,results,stats:{total:5000,eligible:4500,missing_history:500}});
(async()=>{
  const first=vm.runInContext('loadSurgePicks()',ctx);
  ctx.picksSelDate='20260910';const second=vm.runInContext('loadSurgePicks()',ctx);
  assert.equal(calls[1].url,'/api/picks/surge?date=20260910');
  calls[1].resolve({ok:true,json:async()=>payload('20260910')});await second;
  calls[0].resolve({ok:true,json:async()=>payload('20260911',[])});await first;
  assert.match(element('surgeSummary').textContent,/20260910/);
  assert.match(body.innerHTML,/000001/);
  const away=vm.runInContext('loadSurgePicks()',ctx);ctx.currentPicksGroup='system';
  calls[2].resolve({ok:true,json:async()=>payload('20260909')});await away;
  assert.doesNotMatch(element('surgeSummary').textContent,/20260909/);
  ctx.currentPicksGroup='surge';const empty=vm.runInContext('loadSurgePicks()',ctx);
  calls[3].resolve({ok:true,json:async()=>({...payload('20260910',[]),daily_ready:false,note:'等待完整收盘数据'})});await empty;
  assert.match(body.innerHTML,/等待完整收盘数据/);
  const retry=vm.runInContext('loadSurgePicks()',ctx);
  calls[4].resolve({ok:false,json:async()=>({message:'本地行情无法读取'})});await retry;
  assert.match(body.innerHTML,/本地行情无法读取/);assert.equal(element('surgeVisibleCount').textContent,'');
  assert.ok(ctx.stopped>=5);
  console.log('PASS: surge rendering, search, sort, missing data, date/tab races, errors and chart delegation');
})().catch(error=>{console.error(error);process.exitCode=1;});
