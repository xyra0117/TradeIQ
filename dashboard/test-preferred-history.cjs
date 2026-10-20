const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);

// 弹窗结构: 均线筛选条夹在缩量条和汇总行之间, 互斥单选用 radiogroup
assert.match(html, /id="preferredHistMaFilters" role="radiogroup"/);
assert.ok(html.indexOf('id="preferredHistMaBar"') > html.indexOf('id="preferredHistVolumeBar"'));
assert.ok(html.indexOf('id="preferredHistSummary"') > html.indexOf('id="preferredHistMaBar"'));

const elements = new Map();
const calls = [];
const saved = new Map();
const storage = {
  getItem: key => (saved.has(key) ? saved.get(key) : null),
  setItem: (key, value) => saved.set(key, value),
};
function node() {
  return {innerHTML: '', textContent: '', style: {}, dataset: {}, attributes: {},
    setAttribute(k, v) { this.attributes[k] = v; }, querySelectorAll: () => []};
}
const ctx = vm.createContext({
  console, localStorage: storage,
  document: {
    getElementById(id) { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); },
    body: {insertAdjacentHTML() {}},
    addEventListener() {}, removeEventListener() {},
  },
  fmtDate: d => d,
  escapeHtml: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  fetch(url) { return new Promise((resolve, reject) => calls.push({url, resolve, reject})); },
  echarts: {init() { return {setOption() {}, dispose() {}}; }},
});
vm.runInContext(html.slice(
  html.indexOf('// ── 条件优选历史涨幅分布'),
  html.indexOf('// ── 条件优选盘后缓存')), ctx);

// 均线筛选的按钮行要能真的派发 onclick, 方便断言互斥
const box = elements.get('preferredHistMaFilters') || ctx.document.getElementById('preferredHistMaFilters');
let cachedHtml = null, cachedNodes = [];
box.querySelectorAll = function (selector) {
  if (selector !== '[data-hist-ma]') return [];
  if (this.innerHTML !== cachedHtml) {   // 按 innerHTML 缓存, 保证拿到的是渲染时绑定过 onclick 的那批
    cachedHtml = this.innerHTML;
    cachedNodes = Array.from(this.innerHTML.matchAll(/data-hist-ma="(\w+)"/g)).map(m => ({dataset: {histMa: m[1]}}));
  }
  return cachedNodes;
};

// 五个互斥档, 未知/损坏的本地值回落到不过滤
assert.equal(vm.runInContext('_PREFERRED_HIST_MA_OPTIONS.map(([k]) => k).join(",")', ctx),
  'all,ma5,ma10,ma5_only,ma10_only');
assert.equal(vm.runInContext('_preferredHistMaFilter()', ctx), 'all');
for (const [raw, want] of [['ma10', 'ma10'], ['ma5_only', 'ma5_only'],
  ['ma7', 'all'], ['ALL', 'all'], ['', 'all'], ['../../etc', 'all']]) {
  saved.set('tradeiq.preferredHist.maFilter', raw);
  assert.equal(vm.runInContext('_preferredHistMaFilter()', ctx), want);
}
saved.delete('tradeiq.preferredHist.maFilter');

const checked = key => {
  const on = new RegExp(`aria-checked="true"[^>]*data-hist-ma="${key}"`);
  const alt = new RegExp(`data-hist-ma="${key}"[^>]*aria-checked="true"`);
  return (box.innerHTML.match(on) || box.innerHTML.match(alt) || []).length;
};
function pick(key) {
  vm.runInContext('renderPreferredHistMaFilters()', ctx);
  const target = box.querySelectorAll('[data-hist-ma]').find(n => n.dataset.histMa === key);
  assert.ok(target, `找不到按钮 ${key}`);
  target.onclick();   // 走真实绑定: 存本地 → 重绘 → 重新统计
}

pick('all');
assert.equal((box.innerHTML.match(/data-hist-ma="/g) || []).length, 5);
assert.equal((box.innerHTML.match(/role="radio"/g) || []).length, 5);
assert.equal(checked('all'), 1);
for (const key of ['ma5', 'ma10', 'ma5_only', 'ma10_only']) assert.equal(checked(key), 0);

pick('ma5_only');
assert.equal(saved.get('tradeiq.preferredHist.maFilter'), 'ma5_only');
assert.equal(vm.runInContext('_preferredHistMaFilter()', ctx), 'ma5_only');
assert.equal(checked('all'), 0);
assert.equal(checked('ma5_only'), 1);

pick('ma10');
assert.equal(checked('ma10'), 1);
assert.equal(checked('ma5_only'), 0);

const sum = ctx.document.getElementById('preferredHistSummary');
const payload = (ma_filter, extra = {}) => ({
  status: 'success', metric_label: '入选后 T+1~T+5 累计最高涨幅',
  windows: [1, 3, 5], date_from: '20260413', date_to: '20261008', date_count: 116,
  settled: 40, total: 420, pending: 5, unique_stocks: 30, ma_missing: 0,
  volume_filter_on: false, volume_ratio_pct: null, prefiltered_days: 0,
  ma_filter, ma_filter_label: 'MA5 < 入选日收盘价 ≤ MA10（只在5日线上）',
  ma_filtered_out: 0, filtered_out: 0,
  buckets: [{key: 'b5_10', label: '5~10%', count: 40, pct: 100}],
  samples: [], dates: [], ...extra,
});

(async () => {
  let p = vm.runInContext('loadPreferredHistory()', ctx);
  assert.match(calls.at(-1).url, /historical-distribution\?days=5&volume_ratio_pct=0&ma=ma10$/);
  calls.at(-1).resolve({json: async () => payload('all')}); await p;
  assert.match(sum.innerHTML, /均线筛选: 关 \(全部历史入选样本\)/);

  pick('ma5_only');
  p = vm.runInContext('loadPreferredHistory()', ctx);
  assert.match(calls.at(-1).url, /&ma=ma5_only$/);
  calls.at(-1).resolve({json: async () => payload('ma5_only', {ma_filtered_out: 33})}); await p;
  assert.match(sum.innerHTML, /MA5 &lt; 入选日收盘价 ≤ MA10/);
  assert.match(sum.innerHTML, /已滤除 33 条/);

  // 筛剩样本太少时给提醒
  pick('ma10_only');
  p = vm.runInContext('loadPreferredHistory()', ctx);
  assert.match(calls.at(-1).url, /&ma=ma10_only$/);
  calls.at(-1).resolve({json: async () => payload('ma10_only', {settled: 12, ma_filtered_out: 51})}); await p;
  assert.match(sum.innerHTML, /只剩 12 条样本/);
  assert.match(sum.innerHTML, /建议 ≥30 条再看/);
  // 均线窗口不足的样本单独说明, 不跟「没通过条件」混在一起
  p = vm.runInContext('loadPreferredHistory()', ctx);
  calls.at(-1).resolve({json: async () => payload('ma10_only', {settled: 12, ma_missing: 4})}); await p;
  assert.match(sum.innerHTML, /均线窗口不足 4 条/);

  console.log('PASS: preferred history MA filter — five mutually exclusive options, single selection, persistence and fallback, ma query param, summary line and thin-sample warning');
})().catch(error => { console.error(error); process.exitCode = 1; });