// 验证条件选股 / 条件选股新 localStorage 缓存
// 条件选股: snapshot 端点本身就是历史语义, 写缓存安全
// 条件选股新: 收紧到 daily_ready && live_next_index===0 (盘中数据不缓存, 跟条件优选对齐)
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');

const elements = new Map();
const calls = [];
function el() { return {innerHTML:'', textContent:'', style:{}, children:[], addEventListener(){}, removeEventListener(){}, appendChild(){}, insertBefore(){}, removeChild(){}, querySelector(){return null;}, querySelectorAll(){return [];}, setAttribute(){}, getAttribute(){return null;}, classList:{add(){},remove(){},toggle(){}}}; }
const fakeStorage = new Map();
const ctx = vm.createContext({
  document: {
    getElementById(id) { if (!elements.has(id)) elements.set(id, el()); return elements.get(id); },
    createElement() { return el(); },
    body: el(),
    addEventListener(){}, removeEventListener(){},
  },
  URL, URLSearchParams, console, currentPicksGroup: 'conditional', setTimeout, clearTimeout, Date, JSON, Math,
  // 条件选股 / 条件选股新 需要的 globals
  picksSelDate: '20260915',
  conditionalPicksCache: [], conditionalNewPicksCache: [],
  _condRespCache: {}, _condNewRespCache: {},
  _conditionalNewRequestId: 0,
  _liveNextIndex: 0,
  _picksThresholdYI: () => 3, _picksVolumeRatioPct: () => 80,
  _todayYmd: () => '20260915',
  isTradingDay: () => false,  // 强制 useSnapshot=true (盘后语义)
  isTradingTime: () => false,
  fmtDate: d => d, fmtDateMD: d => d,
  escapeHtml: s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
  _maybeAutoSyncPicks() {},
  startPicksAutoRefresh() {},
  fetch(url) { return new Promise(resolve=>calls.push({url,resolve})); },
  localStorage: {
    getItem(k){ return fakeStorage.has(k) ? fakeStorage.get(k) : null; },
    setItem(k,v){ fakeStorage.set(k, String(v)); },
    removeItem(k){ fakeStorage.delete(k); },
  },
});

vm.runInContext(fs.readFileSync(`${__dirname}/picks-result-cache.js`,'utf8'), ctx);
// 切 loadConditionalPicks + loadConditionalNewPicks + helpers (render 函数 mock 掉, slice 不用拉太长)
const startIdx = html.indexOf('async function loadConditionalPicks(');
const endIdx = html.indexOf('// ── 同花顺条件选股 / 组合选股 ──');
vm.runInContext(html.slice(startIdx, endIdx), ctx);
ctx.renderConditionalPicksTable = () => {};
ctx.renderConditionalNewPicksTable = () => {};
ctx.renderCombinedPicksTable = () => {};

(async () => {
  // === 条件选股: snapshot 端点缓存 ===
  console.log('[t] 条件选股: snapshot 缓存');
  calls.length = 0; ctx.PicksResultCache.invalidate();
  ctx.picksSelDate = '20260915';
  const p1 = vm.runInContext('loadConditionalPicks()', ctx);
  // 1 个 snapshot fetch (picksSelDate=20260915 → 加 date 参数)
  await new Promise(r=>setTimeout(r,30));
  assert.ok(calls.length === 1, '首次 load 触发 1 个 snapshot 抓取');
  assert.match(calls[0].url, /\/api\/conditional-picks\/snapshot/);
  calls[0].resolve({ok:true,json:async()=>({count:5,date:'20260915',snapshot_saved:true,snapshot_count:5,results:[{ts_code:'A',name:'A',close:1,change_pct:1},{ts_code:'B',name:'B',close:2,change_pct:2}]})});
  await p1;
  await new Promise(r=>setTimeout(r,30));
  assert.ok(fakeStorage.size > 0, 'snapshot 有结果应写入 localStorage');
  // 二次进入 → 缓存命中，不重复请求
  calls.length = 0;
  const p2 = vm.runInContext('loadConditionalPicks()', ctx);
  await new Promise(r=>setTimeout(r,30));
  assert.equal(ctx.conditionalPicksCache.length, 2, '缓存命中: table 应立即渲染 2 条');
  assert.equal(calls.length, 0, '缓存命中不重新请求 snapshot');
  await p2;

  // === 条件选股新: 盘后定稿 (live_next_index=0) 缓存 ===
  console.log('[t] 条件选股新: 盘后定稿缓存');
  calls.length = 0; ctx.PicksResultCache.invalidate();
  ctx.picksSelDate = '20260915'; ctx.currentPicksGroup = 'conditionalNew';
  const p3 = vm.runInContext('loadConditionalNewPicks()', ctx);
  await new Promise(r=>setTimeout(r,30));
  assert.ok(calls.length === 1, '首次 load 触发 1 个 new 抓取');
  calls[0].resolve({ok:true,json:async()=>({count:3,date:'20260915',daily_ready:true,live_next_index:0,next_1_is_today:false,results:[{ts_code:'C',name:'C',close:3,change_pct:3}]})});
  await p3;
  await new Promise(r=>setTimeout(r,30));
  assert.ok(fakeStorage.size > 0, 'live_next_index=0 应写入 localStorage');
  // 二次进入
  calls.length = 0;
  const p4 = vm.runInContext('loadConditionalNewPicks()', ctx);
  await new Promise(r=>setTimeout(r,30));
  assert.equal(ctx.conditionalNewPicksCache.length, 1, '缓存命中: table 应立即渲染 1 条');
  assert.equal(calls.length, 0, '缓存命中不重新请求 new');
  await p4;

  // === 条件选股新: 盘中 (live_next_index=1) 不应缓存 ===
  console.log('[t] 条件选股新: 盘中数据不缓存');
  calls.length = 0; ctx.PicksResultCache.invalidate();
  const p5 = vm.runInContext('loadConditionalNewPicks()', ctx);
  await new Promise(r=>setTimeout(r,30));
  assert.ok(calls.length === 1);
  calls[0].resolve({ok:true,json:async()=>({count:3,date:'20260915',daily_ready:true,live_next_index:1,next_1_is_today:true,results:[{ts_code:'D',name:'D',close:4,change_pct:4}]})});
  await p5;
  await new Promise(r=>setTimeout(r,30));
  assert.equal(JSON.parse(fakeStorage.get(ctx.PicksResultCache.storageKey)||'[]').length, 0, '盘中结果仅留在本页面，不持久化');

  console.log('PASS: cond/cond-new persistent successful results, no repeated requests, live guard');
})().catch(e => { console.error('FAIL:', e.message, e.stack); process.exitCode = 1; });