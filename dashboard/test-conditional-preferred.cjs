const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
assert.match(html, /key: 'conditionalPreferred', label: '条件优选'/);
const source = html.slice(html.indexOf('async function loadConditionalPreferredPicks()'), html.indexOf('async function loadConditionalNewPicks('));
const elements = new Map();
const calls = [];
const charts = [];
function el() { return {innerHTML:'', textContent:'', style:{}}; }
const cards = new Map(['000001.SH','399001.SZ','399006.SZ','000688.SH'].map(code => {
  const meta=el(), chart=el();
  return [code, {querySelector(selector){ return selector.includes('meta') ? meta : chart; }, meta, chart}];
}));
const ctx = vm.createContext({
  document: {
    getElementById(id) { if (!elements.has(id)) elements.set(id, el()); return elements.get(id); },
    querySelector(selector) { const match=selector.match(/data-preferred-index="([^"]+)"/); return match ? cards.get(match[1]) : null; },
  },
  URLSearchParams, console, _conditionalPreferredRequestId:0, conditionalPreferredPicksCache:[], currentPicksGroup:'conditionalPreferred', picksSelDate:'20260911',
  CONDITIONAL_PREFERRED_INDEXES:[{code:'000001.SH'},{code:'399001.SZ'},{code:'399006.SZ'},{code:'000688.SH'}],
  _conditionalPreferredIndexCharts:{},
  _liveNextIndex:1,
  // 声明在切片起点之前 (index.html:8071), 单独给桩
  _preferredViewDate:'', _preferredViewDates:[],
  _picksThresholdYI:()=>3, _picksVolumeRatioPct:()=>80, _picksPreferredVolumeParam:()=>80,
  // 「加自选」按钮 (定义在 deleteUserPick 附近, 不在本切片里, 单独给桩)
  _selfPickDate(row) { return row.trade_date || '20260911'; },
  _renderAddSelfBtn(ts, date, name) { return `<button class="pick-add-self" data-ts="${ts}" data-date="${date}">${name}</button>`; },
  fmtDate:d=>d, escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
  fetch(url) { return new Promise(resolve=>calls.push({url,resolve})); },
  startPicksAutoRefresh() { ctx.autoRefreshes = (ctx.autoRefreshes || 0) + 1; },
  echarts:{init(box){const chart={box,option:null,cleared:false,setOption(option){this.option=option;},clear(){this.cleared=true;},resize(){}};charts.push(chart);return chart;}},
  openStockFlowDetail(...args) { ctx.openedFlow = args; },
});
const preferredStart = html.indexOf('function _renderConditionalPreferredBreadth(');
ctx._fetchPicksResult = (...args) => ctx.fetch(...args);
ctx.PicksResultCache = {invalidate(){}};
vm.runInContext(html.slice(html.indexOf('function renderSectorPills(stock)'), html.indexOf('function initFilters()')),ctx);
vm.runInContext(html.slice(html.indexOf('// 条件优选「站上5日线」开关'), html.indexOf('const _VOLUME_RATIO_SPEC')), ctx);
vm.runInContext(html.slice(preferredStart, html.indexOf('async function loadConditionalNewPicks(')),ctx);
const item = {ts_code:'A.SZ',name:'<测试>',trade_date:'20260911',close:10,change_pct:-1,volume_ratio_pct:75,
  preferred_priority:1,preferred_mild_shrink:true,preferred_below_ma60:false,preferred_strength5:5,
  preferred_small_net:-1e8,preferred_ma60:null,preferred_outcomes:[{date:'20260914',high_sum_pct:6,close_sum_pct:2,reason:'intraday'}]};
ctx.item=item;
vm.runInContext('renderConditionalPreferredPicksTable([item])',ctx);
const body = elements.get('conditionalPreferredPicksBody');
assert.match(body.innerHTML,/&lt;测试>/);
assert.match(body.innerHTML,/\+6\.00%/);
assert.match(body.innerHTML,/⚡实时 \+2\.00%/);
assert.equal((body.innerHTML.match(/<td/g)||[]).length,14);
// 「加自选」内联在名称右边, 不占独立列
assert.match(body.innerHTML,/pick-add-self/);
assert.match(body.innerHTML,/data-date="20260911"/);
let stopped = false;
body.onclick({stopPropagation(){stopped=true;}, target:{closest(){return {dataset:{preferredAction:'flow'},closest(){return {dataset:{tsCode:'A.SZ',stockName:'测试'}};}};}}});
assert.ok(stopped, '弹窗入口不可冒泡到全局点击关闭逻辑');
assert.equal(ctx.openedFlow[0], 'A.SZ');
vm.runInContext('renderConditionalPreferredPicksTable([], false)',ctx);
assert.match(body.innerHTML,/等待完整收盘/);
vm.runInContext('renderConditionalPreferredPicksTable([])',ctx);
assert.match(body.innerHTML,/没有满足全部优选条件/);
(async()=>{
  const first=vm.runInContext('loadConditionalPreferredPicks()',ctx);
  ctx.picksSelDate='20260910';
  const second=vm.runInContext('loadConditionalPreferredPicks()',ctx);
  assert.match(calls[1].url,/date=20260910/);
  calls[1].resolve({ok:true,json:async()=>({date:'20260910',daily_ready:true,count:1,results:[item],preferred_exclusions:{base_count:3},market_overview:{date:'20260910',available:true,upCount:3000,downCount:2000,flatCount:100,totalCount:5100}})});
  await second;
  assert.equal(calls.length,6,'四大指数应各请求一次同日分时');
  const minute={status:'success',requested_date:'20260910',trade_date:'20260910',prev_close:100,count:2,rows:[{m:'09:30:00',p:100},{m:'15:00:00',p:101}]};
  calls.slice(2,6).forEach(call=>call.resolve({ok:true,json:async()=>minute}));
  await Promise.resolve(); await Promise.resolve();
  calls[0].resolve({ok:true,json:async()=>({date:'20260911',daily_ready:true,count:0,results:[]})});
  await first;
  assert.match(elements.get('conditionalPreferredSummary').textContent,/20260910/);
  assert.match(elements.get('conditionalPreferredBreadthDate').textContent,/5,100/);
  assert.match(elements.get('conditionalPreferredBreadth').innerHTML,/3,000/);
  assert.equal(ctx.conditionalPreferredPicksCache.length,1);
  assert.equal(charts.length,4);
  assert.equal(charts[0].option.series[0].data[1],1);
  console.log('PASS: preferred rendering, exact-date breadth, four index charts, additive display, stale-response isolation');
})().catch(e=>{console.error(e);process.exitCode=1;});
