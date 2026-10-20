const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);

// 开关按钮在条件优选自己的控件组里, 不跟「条件选股新」的缩量开关混用
assert.match(html, /id="picksMa5GroupPref"[\s\S]{0,400}?id="picksMa5TogglePref"/);
assert.match(html, /id="picksMa5TogglePref"[^>]*aria-pressed="false"/);
assert.ok(html.indexOf('id="picksMa5TogglePref"') < html.indexOf('id="picksSyncDateBtn"'));
// 只在条件优选 tab 显示
assert.match(html, /getElementById\('picksMa5GroupPref'\)\.style\.display = isPreferred \? 'inline-flex' : 'none'/);

const elements = new Map();
const calls = [];
const saved = new Map();
const storage = {
  getItem: key => (saved.has(key) ? saved.get(key) : null),
  setItem: (key, value) => saved.set(key, value),
};
function node() {
  return {innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, attributes: {},
    setAttribute(k, v) { this.attributes[k] = v; }, addEventListener() {}, disabled: false};
}
let listeners = {};
function el() {
  const n = node();
  n.addEventListener = (ev, fn) => { (listeners[n.id] = listeners[n.id] || {})[ev] = fn; };
  return n;
}
const ctx = vm.createContext({
  console, localStorage: storage, URLSearchParams,
  document: {
    getElementById(id) {
      if (!elements.has(id)) { const n = el(); n.id = id; elements.set(id, n); }
      return elements.get(id);
    },
  },
  picksSelDate: '20260924', currentPicksGroup: 'conditionalPreferred',
  _conditionalPreferredRequestId: 0, conditionalPreferredPicksCache: [], _liveNextIndex: 0,
  CONDITIONAL_PREFERRED_INDEXES: [],
  PicksResultCache: {invalidate() {}},
  _picksThresholdYI: () => 3,
  _picksPreferredVolumeParam: () => 80,
  _fetchPicksResult(url) { return new Promise(resolve => calls.push({url, resolve})); },
  renderConditionalPreferredPicksTable() {}, _renderConditionalPreferredBreadth() {},
  _loadConditionalPreferredIndexCharts() {}, _initPreferredViewDates() {}, startPicksAutoRefresh() {},
  _setPreferredIndexStatus() {},
  fmtDate: d => d,
  escapeHtml: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'),
});
vm.runInContext(html.slice(
  html.indexOf('const CONDITIONAL_NEW_RATIO_KEY'),
  html.indexOf('const _VOLUME_RATIO_SPEC')), ctx);
vm.runInContext(html.slice(
  html.indexOf('async function loadConditionalPreferredPicks('),
  html.indexOf('// ── 条件优选『生成预览图』')), ctx);

const KEY = 'tradeiq.conditionalPreferred.requireMa5';
const btn = ctx.document.getElementById('picksMa5TogglePref');

// 默认关 = 原条件
assert.equal(vm.runInContext('_prefMa5FilterOn()', ctx), false);
vm.runInContext('_paintPrefMa5Toggle()', ctx);
assert.equal(btn.textContent, '站上5日线: 关');
assert.equal(btn.attributes['aria-pressed'], 'false');

// 只有严格等于 '1' 才算开, 脏值一律按关处理
for (const [raw, want] of [['1', true], ['0', false], ['true', false], ['', false], ['../../etc', false]]) {
  saved.set(KEY, raw);
  assert.equal(vm.runInContext('_prefMa5FilterOn()', ctx), want, `raw=${JSON.stringify(raw)}`);
}
saved.delete(KEY);

// 切换: 写本地 + 重绘 + 重新拉名单
const callsBefore = calls.length;
vm.runInContext('_togglePrefMa5Filter()', ctx);
assert.equal(saved.get(KEY), '1');
assert.equal(btn.textContent, '站上5日线: 开');
assert.equal(btn.attributes['aria-pressed'], 'true');
assert.equal(calls.length, callsBefore + 1);
assert.match(calls.at(-1).url, /require_ma5=1/);
calls.at(-1).resolve({ok: true, json: async () => ({
  date: '20260924', count: 2, results: [{ts_code: '000001.SZ'}, {ts_code: '000002.SZ'}],
  daily_ready: true, preferred_exclusions: {base_count: 5, below_ma5: 3, missing_ma5: 0},
})});

(async () => {
  // 打开时: 参数带上、摘要说明条件已开并报出剔除数
  await new Promise(r => setTimeout(r, 0));
  const sum = ctx.document.getElementById('conditionalPreferredSummary');
  assert.match(sum.textContent, /站上5日线: 开/);
  assert.match(sum.textContent, /3 只未站上5日线/);
  assert.doesNotMatch(sum.textContent, /缺5日线数据/);

  // 再切回关: 请求与摘要回到原条件
  vm.runInContext('_togglePrefMa5Filter()', ctx);
  assert.equal(saved.get(KEY), '0');
  assert.equal(btn.textContent, '站上5日线: 关');
  assert.match(calls.at(-1).url, /require_ma5=0/);
  calls.at(-1).resolve({ok: true, json: async () => ({
    date: '20260924', count: 5, results: [], daily_ready: true,
    preferred_exclusions: {base_count: 5},
  })});
  await new Promise(r => setTimeout(r, 0));
  assert.match(sum.textContent, /站上5日线: 关/);
  assert.doesNotMatch(sum.textContent, /未站上5日线/);

  // 迟到的旧开关响应不能覆盖当前开关状态下的名单
  vm.runInContext('_togglePrefMa5Filter()', ctx);          // 切到开
  const staleCall = calls.at(-1);
  vm.runInContext('_togglePrefMa5Filter()', ctx);          // 再切回关, 旧的作废
  staleCall.resolve({ok: true, json: async () => ({
    date: '20260924', count: 99, results: [], daily_ready: true,
    preferred_exclusions: {base_count: 5, below_ma5: 4},
  })});
  await new Promise(r => setTimeout(r, 0));
  assert.doesNotMatch(sum.textContent, /99 只/);   // 迟到响应被丢弃, 没写进摘要
  // 当前这次请求正常落地
  calls.at(-1).resolve({ok: true, json: async () => ({
    date: '20260924', count: 5, results: [], daily_ready: true,
    preferred_exclusions: {base_count: 5},
  })});
  await new Promise(r => setTimeout(r, 0));
  assert.match(sum.textContent, /站上5日线: 关/);
  assert.match(sum.textContent, /优选 5 只/);

  console.log('PASS: preferred MA5 toggle — default off, strict truthy parsing, persistence, require_ma5 query param, summary line and stale-response guard');
})().catch(error => { console.error(error); process.exitCode = 1; });