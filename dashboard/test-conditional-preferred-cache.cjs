// 验证 localStorage 缓存命中 + 落库逻辑
// 盘后定稿: outcomes 全是 future_unavailable → 应该缓存并瞬开
// 盘中: outcomes 含 reason='intraday' → 不应缓存
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');

const elements = new Map();
const calls = [];
function el() { return {innerHTML:'', textContent:'', style:{}}; }
const cards = new Map(['000001.SH','399001.SZ','399006.SZ','000688.SH'].map(code => {
  const meta=el(), chart=el();
  return [code, {querySelector(selector){ return selector.includes('meta') ? meta : chart; }, meta, chart}];
}));
const fakeStorage = new Map();
const ctx = vm.createContext({
  document: {
    getElementById(id) { if (!elements.has(id)) elements.set(id, el()); return elements.get(id); },
    querySelector(selector) { const m=selector.match(/data-preferred-index="([^"]+)"/); return m ? cards.get(m[1]) : null; },
  },
  URL, URLSearchParams, console, currentPicksGroup: 'conditionalPreferred',
  _conditionalPreferredRequestId:0, conditionalPreferredPicksCache:[], picksSelDate:'20260915',
  CONDITIONAL_PREFERRED_INDEXES:[{code:'000001.SH'},{code:'399001.SZ'},{code:'399006.SZ'},{code:'000688.SH'}],
  _conditionalPreferredIndexCharts:{},
  _liveNextIndex:0,
  _picksThresholdYI:()=>3, _picksVolumeRatioPct:()=>80, _picksPreferredVolumeParam:()=>80,
  // 声明在切片起点之前 (index.html:8071), 单独给桩
  _preferredViewDate:'', _preferredViewDates:[],
  // 「加自选」按钮 (定义在 deleteUserPick 附近, 不在本切片里, 单独给桩)
  _selfPickDate(row) { return row.trade_date || ''; },
  _renderAddSelfBtn() { return ''; },
  fmtDate:d=>d, escapeHtml:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
  fetch(url) { return new Promise(resolve=>calls.push({url,resolve})); },
  startPicksAutoRefresh() {},
  echarts:{init(box){return {box,option:null,setOption(o){this.option=o;},clear(){},resize(){},dispose(){}};}},
  localStorage: {
    getItem(k){ return fakeStorage.has(k) ? fakeStorage.get(k) : null; },
    setItem(k,v){ fakeStorage.set(k, String(v)); },
    removeItem(k){ fakeStorage.delete(k); },
  },
});

vm.runInContext(fs.readFileSync(`${__dirname}/picks-result-cache.js`,'utf8'), ctx);
const preferredStart = html.indexOf('function _renderConditionalPreferredBreadth(');
const preferredEnd = html.indexOf('async function loadConditionalNewPicks(');
vm.runInContext(html.slice(html.indexOf('function renderSectorPills(stock)'), html.indexOf('function initFilters()')),ctx);
vm.runInContext(html.slice(html.indexOf('// 条件优选「站上5日线」开关'), html.indexOf('const _VOLUME_RATIO_SPEC')), ctx);
vm.runInContext(html.slice(preferredStart, preferredEnd), ctx);

const item = {ts_code:'A.SZ',name:'A',close:1,change_pct:1,preferred_priority:1,
  preferred_outcomes:[{date:'20260916',high_sum_pct:1,close_sum_pct:1,reason:'future_unavailable'}]};
const minute = {status:'success',requested_date:'20260915',trade_date:'20260915',prev_close:100,count:2,rows:[{m:'09:30:00',p:100},{m:'15:00:00',p:101}]};

(async () => {
  // 场景 1: 盘后定稿 → 应缓存 (含 4 个指数分时)
  ctx.picksSelDate='20260915';
  const p1 = vm.runInContext('loadConditionalPreferredPicks()', ctx);
  calls[0].resolve({ok:true,json:async()=>({
    date:'20260915',daily_ready:true,intraday_used:false,count:1,results:[item],
    preferred_exclusions:{base_count:3},market_overview:{date:'20260915',available:true,upCount:1,downCount:1,flatCount:1,totalCount:3},
  })});
  await p1;
  for (let i=1; i<calls.length; i++) calls[i].resolve({ok:true,json:async()=>minute});
  await new Promise(r=>setTimeout(r,80));  // 等 chartsPromise.then 写缓存
  assert.ok(fakeStorage.size > 0, '盘后定稿应写入 localStorage');
  const entries = JSON.parse(fakeStorage.get(ctx.PicksResultCache.storageKey));
  const cached = entries.find(([key])=>key.includes('/api/conditional-picks/preferred?'))[1];
  assert.ok(cached.payload && cached.payload.daily_ready, '缓存 payload 含 daily_ready');
  assert.equal(cached.payload.results.length, 1, '缓存 payload 含 results');
  assert.equal(entries.filter(([key])=>key.includes('/api/index/minline?')).length, 4, '四个分时独立缓存');

  // 场景 2: 二次进入 → 缓存命中 → 结果与分时直接复用
  calls.length = 0;
  const p2 = vm.runInContext('loadConditionalPreferredPicks()', ctx);
  await new Promise(r=>setTimeout(r,30));
  assert.equal(ctx.conditionalPreferredPicksCache.length, 1, '缓存命中: table 应立即渲染 1 条');
  assert.equal(calls.length, 0, '结果和分时命中后不重复请求');
  await p2;

  // 场景 3: outcomes 含 reason='intraday' → 不应缓存
  ctx._conditionalPreferredRequestId = 0;
  ctx.conditionalPreferredPicksCache = [];
  calls.length = 0;
  ctx.PicksResultCache.invalidate();
  ctx.picksSelDate = '20260914';  // 换 cacheKey, 绕过场景 1/2 缓存
  const liveItem = {ts_code:'B.SZ',name:'B',close:1,change_pct:1,preferred_priority:1,
    preferred_outcomes:[{date:'20260915',high_sum_pct:1,close_sum_pct:1,reason:'intraday'}]};
  const p3 = vm.runInContext('loadConditionalPreferredPicks()', ctx);
  calls[0].resolve({ok:true,json:async()=>({date:'20260914',daily_ready:true,count:1,results:[liveItem],preferred_exclusions:{},market_overview:{date:'20260914',available:true,upCount:1,downCount:1,flatCount:1,totalCount:3}})});
  await p3;
  for (let i=0; i<50 && calls.length < 5; i++) await new Promise(r=>setTimeout(r,5));
  for (let i=1; i<calls.length; i++) calls[i].resolve({ok:true,json:async()=>minute});
  await new Promise(r=>setTimeout(r,80));
  const saved = JSON.parse(fakeStorage.get(ctx.PicksResultCache.storageKey)||'[]');
  assert.equal(saved.filter(([key])=>key.includes('/api/conditional-picks/preferred?')).length, 0, '盘中名单不持久化，已确认分时可独立保存');

  console.log('PASS: cache hit/miss + intraday guard');
})().catch(e => { console.error('FAIL:', e.message, e.stack); process.exitCode = 1; });
