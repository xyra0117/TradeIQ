const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const ids = ['000001.SH','399001.SZ','399006.SZ','000688.SH'];
const elements = new Map(), calls = [], charts = [];
// 每次刷新固定 6 个请求: 1 个盘面 + 4 个指数分时 + 1 个热门榜
const BATCH = 6;
const element = id => {
  if (!elements.has(id)) elements.set(id,{textContent:'',innerHTML:'',style:{},disabled:false});
  return elements.get(id);
};
const cards = new Map();
for (const grid of ['dipMarketContextIndexGrid','conditionalPreferredIndexGrid']) {
  for (const code of ids) {
    const meta = element(grid+code+'Meta'), box = element(grid+code+'Chart');
    cards.set(grid+code,{meta,box,querySelector(selector){return selector.includes('meta')?meta:box;}});
  }
}
const ctx = vm.createContext({console,currentPicksGroup:'dip',picksSelDate:'20260805',
  CONDITIONAL_PREFERRED_INDEXES:ids.map(code=>({code})),_conditionalPreferredIndexCharts:{},
  _dipParametersSignature:()=>ctx.parametersSignature || '',_diagnosticSignature:()=>ctx.signature || '',fmtDate:d=>d,
  escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'),
  document:{getElementById:element,querySelector(selector){
    const match=selector.match(/^#(\w+) \[data-preferred-index="([^"]+)"\]$/);
    return match ? cards.get(match[1]+match[2]) : null;
  }},
  fetch(url){return new Promise((resolve,reject)=>calls.push({url,resolve,reject}));},
  echarts:{init(box){const chart={box,option:null,disposed:false,setOption(o){this.option=o;},dispose(){this.disposed=true;},resize(){}};charts.push(chart);return chart;}}
});
ctx._fetchPicksResult = (...args) => ctx.fetch(...args);
ctx.PicksResultCache = {invalidate(){}};
vm.runInContext(html.slice(html.indexOf('function _renderConditionalPreferredBreadth('),html.indexOf('// ── 条件优选历史涨幅分布')),ctx);
vm.runInContext(html.slice(html.indexOf('const _hotNumber ='),html.indexOf('function _renderHotSectorGrid')),ctx);
vm.runInContext(html.slice(html.indexOf('// ── 低吸/追高市场温度：'),html.indexOf('// ── 低吸/追高市场温度结束 ──')),ctx);
const payload={date:'20260805',daily_ready:true,results:[],next_dates:['20260806','20260807','20260810','20260811','20260812']};
ctx.payload=payload;
function start(group='dip',date=payload.date) {
  ctx.currentPicksGroup=group;ctx.picksSelDate=date;
  ctx.context=vm.runInContext(`_resetDipMarketContext(currentPicksGroup,picksSelDate,_diagnosticSignature())`,ctx);
  return vm.runInContext('_startDipMarketContext(payload,context)',ctx);
}
function resolveBatch(batch,date,options={}) {
  batch.forEach(call=>{
    const data=call.url.includes('market-overview')
      ? {date,available:true,upCount:3000,downCount:2000,flatCount:100,totalCount:5100,...options.breadth}
      : call.url.includes('/api/hot-sectors')
      ? {status:'ready',date,summary:{recent5_complete:true,recent5_dates:[]},sectors:[]}
      : {status:'success',requested_date:date,trade_date:date,prev_close:100,rows:[{m:'09:30:00',p:100},{m:'15:00:00',p:101}],...options.minute};
    call.resolve({ok:true,json:async()=>data});
  });
}
(async()=>{
  const first=start(); const initial=calls.slice();
  assert.equal(initial.length,BATCH);
  assert(initial.every(c=>c.url.includes('date=20260805')));
  assert.match(element('dipMarketViewDateLabel').textContent,/20260805.*入选日/);
  assert.equal(element('dipMarketViewPrevBtn').disabled,true);
  assert.equal(element('dipMarketViewNextBtn').disabled,false);
  resolveBatch(initial,'20260805');await first;
  assert.match(element('dipMarketContextBreadth').innerHTML,/3,000/);
  assert.match(element('dipMarketContextBreadthDate').textContent,/5,100/);
  // 热门榜与温度同一天: 入选日显示该日, 切到 T+1 后跟着换日。
  assert.match(element('dipHotSectorInfo').textContent,/2026-08-05 · 0个板块/);
  assert.equal(charts.length,4);
  assert.equal(charts[0].option.series[0].data[1],1);
  assert.deepEqual(Object.keys(ctx._conditionalPreferredIndexCharts),[],'优选图表状态保持隔离');
  // View arrows change only market requests, never selection dates or saved rules.
  const next=vm.runInContext('shiftDipMarketViewDate(1)',ctx), nextCalls=calls.slice(-BATCH);
  assert.equal(ctx.picksSelDate,'20260805');
  assert(nextCalls.every(c=>c.url.includes('date=20260806')));
  assert(charts.slice(0,4).every(c=>c.disposed));
  assert.equal(element('dipMarketContextBreadth').innerHTML,'');
  assert.match(element('dipMarketViewDateLabel').textContent,/T\+1/);
  resolveBatch(nextCalls,'20260806',{breadth:{available:false,message:'该日涨跌家数尚未入库'},minute:{rows:[],message:'历史分时未留存'}});await next;
  assert.match(element('dipMarketContextBreadthDate').textContent,/尚未入库/);
  assert.match(element('dipMarketContextBreadth').innerHTML,/未使用其他日期替代/);
  assert.match(cards.get('dipMarketContextIndexGrid000001.SH').box.innerHTML,/历史分时未留存/);
  assert.match(element('dipHotSectorInfo').textContent,/2026-08-06 · 0个板块/);
  // Same selection refresh keeps T+1; switching strategy resets to entry day.
  const refresh=start(); assert.match(element('dipMarketViewDateLabel').textContent,/T\+1/);
  resolveBatch(calls.slice(-BATCH),'20260806');await refresh;
  const chase=start('dip2');assert.match(element('dipMarketViewDateLabel').textContent,/入选日/);
  resolveBatch(calls.slice(-BATCH),'20260805');await chase;
  // Responses carrying another day are never rendered as the requested day.
  const mismatch=vm.runInContext('shiftDipMarketViewDate(1)',ctx);
  resolveBatch(calls.slice(-BATCH),'20260807');await mismatch;
  assert.match(element('dipMarketContextBreadthDate').textContent,/日期与请求不一致/);
  assert.match(cards.get('dipMarketContextIndexGrid000001.SH').box.innerHTML,/未使用其他日期替代/);
  // Two quick view shifts: old response cannot overwrite the newest view.
  const older=vm.runInContext('shiftDipMarketViewDate(1)',ctx), oldCalls=calls.slice(-BATCH);
  const newer=vm.runInContext('shiftDipMarketViewDate(1)',ctx), newCalls=calls.slice(-BATCH);
  resolveBatch(newCalls,'20260810');await newer;
  resolveBatch(oldCalls,'20260807',{breadth:{upCount:9999}});await older;
  assert.match(element('dipMarketContextBreadthDate').textContent,/20260810/);
  assert.doesNotMatch(element('dipMarketContextBreadth').innerHTML,/9,999/);
  // A changed filter signature or strategy invalidates pending market data.
  const stale=vm.runInContext('shiftDipMarketViewDate(1)',ctx), staleCalls=calls.slice(-BATCH);
  ctx.signature='volume';resolveBatch(staleCalls,'20260811',{breadth:{upCount:9999}});await stale;
  assert.doesNotMatch(element('dipMarketContextBreadth').innerHTML,/9,999/);
  ctx.signature='';ctx.currentPicksGroup='conditionalPreferred';
  const count=calls.length;await vm.runInContext('shiftDipMarketViewDate(1)',ctx);
  assert.equal(calls.length,count);
  // Latest selection resolving to a new day resets a previously viewed T date.
  ctx.currentPicksGroup='dip';ctx.picksSelDate='';
  ctx.context=vm.runInContext('_resetDipMarketContext(currentPicksGroup,"","")',ctx);
  const latest=vm.runInContext('_startDipMarketContext(payload,context)',ctx);resolveBatch(calls.slice(-BATCH),'20260805');await latest;
  const latestNext=vm.runInContext('shiftDipMarketViewDate(1)',ctx);resolveBatch(calls.slice(-BATCH),'20260806');await latestNext;
  ctx.payload={...payload,date:'20260806',next_dates:payload.next_dates.slice(1)};
  const changed=start('dip','');assert.match(element('dipMarketViewDateLabel').textContent,/20260806.*入选日/);
  resolveBatch(calls.slice(-BATCH),'20260806');await changed;
  // No guessed dates when the calendar contains no future trading days.
  ctx.payload={...payload,next_dates:[]};const short=start('dip','20260805');
  assert.equal(element('dipMarketViewNextBtn').disabled,true);
  calls.slice(-BATCH).forEach(c=>c.reject(Error('请求失败')));await short;
  assert.match(element('dipMarketContextBreadthDate').textContent,/请求失败/);
  assert(html.includes('id="dipMarketContext"'));
  assert.equal((html.match(/id="dipMarketContextIndexGrid"/g)||[]).length,1);
  console.log('PASS: shared market temperature, four isolated charts, exact-date/missing/error handling, T+1..T+5 controls, refresh and strategy/date/filter race guards, hot board following the same view date');
})().catch(error=>{console.error(error);process.exitCode=1;});
