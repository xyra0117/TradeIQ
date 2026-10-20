const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
assert.match(html, /key: 'conditionalNew', label: '条件选股新'/);
assert.match(html, /id="picksVolumeRatioInput"[^>]*min="1"[^>]*max="100"[^>]*value="80"/);
assert.match(html, /\/api\/conditional-picks\/new\?/);
assert.match(html, /_fetchPicksResult\('\/api\/conditional-picks\/new\?/); // Full date/ratio/threshold query is the shared key.
assert.match(html, /requestId !== _conditionalNewRequestId/);
assert.match(html, /currentPicksGroup === 'conditionalPreferred' \|\| currentPicksGroup === 'combined'/,
  '条件优选必须进入 T+N 实时刷新分支');

const formatterStart = html.indexOf('function _fmtPostPickChange');
const formatterSource = html.slice(formatterStart, html.indexOf('// ── 同花顺条件选股', formatterStart));
const formatterContext = vm.createContext({});
vm.runInContext(formatterSource, formatterContext);
const liveCell = vm.runInContext('_fmtPostPickAndCum(2.94, 10.5, 11, 10, {live:true})', formatterContext);
assert.match(liveCell, /⚡ \+2\.94%/);
assert.match(liveCell, /\+5\.00%/);
assert.match(liveCell, /\+10\.00%/);

const input = { value: '80' };
const rule = { textContent: '' };
const stored = new Map();
const ratioSource = html.slice(
  html.indexOf("const CONDITIONAL_NEW_RATIO_KEY ="),
  html.indexOf('function _bindPicksThresholdControl()')
);
const ratioContext = vm.createContext({
  isFinite,
  currentPicksGroup: 'conditionalNew',
  document: { getElementById(id) { return id === 'picksVolumeRatioInput' ? input : rule; } },
  localStorage: {
    getItem(key) { return stored.get(key) || null; },
    setItem(key, value) { stored.set(key, value); },
  },
  loadConditionalNewPicks() { ratioContext.loads = (ratioContext.loads || 0) + 1; },
});
vm.runInContext(ratioSource, ratioContext);
input.value = '65';
vm.runInContext('_applyPicksVolumeRatio()', ratioContext);
assert.equal(stored.get('tradeiq.conditionalNew.volumeRatioPct'), '65');
assert.equal(rule.textContent, '65%');
assert.equal(ratioContext.loads, 1);
input.value = '0';
vm.runInContext('_applyPicksVolumeRatio()', ratioContext);
assert.equal(input.value, '80');

const body = { innerHTML: '' };
const outcomeCalls = [];
const renderSource = html.slice(
  html.indexOf('function renderConditionalNewPicksTable'),
  html.indexOf('// 跟随 sub-tab 切换容器显示')
);
const renderContext = vm.createContext({
  document: { getElementById() { return body; } },
  renderSectorPills() { return ''; },
  _fmtCondFlow(value) { return String(value || 0); },
  _fmtPostPickAndCum(...args) { outcomeCalls.push(args); return args[4]?.live ? '⚡实时' : '—'; },
  _lastNext1IsToday: false,
  // 定义在本切片之后 (index.html:9304), 按真实公式给桩
  _condHighCumPct(r, t) { const d = r[`max_high_t${t}`], e = r.close; return (d > 0 && e > 0) ? (d / e - 1) * 100 : null; },
  _liveNextIndex: 2,
  conditionalPreferredPicksCache: [],
  // 「加自选」按钮 (定义在 deleteUserPick 附近, 不在本切片里, 单独给桩)
  _selfPickDate(row) { return row.trade_date || '20250820'; },
  _renderAddSelfBtn(ts, date, name) { return `<button class="pick-add-self" data-ts="${ts}" data-date="${date}">${name}</button>`; },
  picksSelDate: '20250820',
});
vm.runInContext(renderSource, renderContext);
vm.runInContext('renderConditionalNewPicksTable([], false)', renderContext);
assert.match(body.innerHTML, /等待收盘数据/);
const item = {
  ts_code: '000001.SZ', name: '测试股', close: 10, ma10: 9.5,
  volume: 80, prev_volume: 100, volume_ratio_pct: 80, change_pct: -1,
  used_bucket: 'main', hit_threshold: '3d', sum_3d: 4e8,
  sum_5d: 4e8, sum_10d: 4e8, sum_20d: 4e8,
};
renderContext.item = item;
vm.runInContext('renderConditionalNewPicksTable([item], true)', renderContext);
assert.match(body.innerHTML, />9\.50</);
assert.match(body.innerHTML, />80\.0%</);
assert.match(body.innerHTML, /showKlineModal\('000001\.SZ','测试股',\{pickDate:'20250820'\}\)/);
assert.equal((body.innerHTML.match(/<td/g) || []).length, 20);
assert.equal(outcomeCalls[1][4].live, true, 'T+2 是今天时应标记为盘中实时列');
assert.match(body.innerHTML, /⚡实时/);

console.log('PASS: 新 Tab、80% 默认值、比例记忆、等待状态、MA10/成交量比列、请求隔离');
