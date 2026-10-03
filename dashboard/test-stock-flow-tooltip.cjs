const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
const klineBoxIndex = html.indexOf('id="stockFlowDetailChartKline"');
const amountBoxIndex = html.indexOf('id="stockFlowDetailChartAmt"');
assert.ok(klineBoxIndex >= 0 && klineBoxIndex < amountBoxIndex, '日 K 图必须位于资金流图上方');
assert.match(html, /function _loadStockFlowKline\(tsCode, requestId\)/);
assert.match(html, /\/api\/stock\/daily\?ts_code=.*limit=250&intraday=1/);
assert.match(html, /name: '日K', type: 'candlestick'/);
assert.match(html, /name: 'VOL', type: 'bar'/);
assert.match(html, /chart\.on\('datazoom', \(\) => _syncStockFlowZoom\(chart\)\)/);
assert.match(html, /chart\.on\('updateAxisPointer', event => _syncStockFlowPointer\(chart, event\)\)/);
const start = html.indexOf('const _MKT_FLOW_DEFS =');
const end = html.indexOf('// 跟随 _flowSelDate', start);
assert.ok(start >= 0 && end > start, '缺少个股资金流累计提示计算函数');

const context = vm.createContext({
  KLINE_UP: '#ff5252',
  KLINE_DOWN: '#00e676',
  window: { addEventListener() {} },
});
vm.runInContext(html.slice(start, end), context);

assert.equal(vm.runInContext("_stockFlowDateKey('2026-08-20')", context), '20260820');
assert.deepEqual(
  Array.from(vm.runInContext("_stockFlowDateRangeIndexes(['20260818', '20260820', '20260821'], '20260819', '20260821')", context)),
  [1, 2],
  '日期缺口存在时应对齐目标图中实际可见的日期范围',
);
context.zoomChart = {
  _stockFlowDates: ['2026-08-18', '2026-08-19', '2026-08-20'],
  getOption: () => ({ dataZoom: [{ start: 50, end: 100 }] }),
};
assert.deepEqual(
  Array.from(vm.runInContext('_stockFlowVisibleDateRange(zoomChart)', context)),
  ['20260819', '20260820'],
  '缩放范围应换算为标准交易日期后再同步',
);

const dates = Array.from({ length: 20 }, (_, i) => `2026-08-${String(i + 1).padStart(2, '0')}`);
const d = {
  dates,
  amount: Array(20).fill(1000), // Tushare 千元；每天成交额 100 万元
  series: {
    main: Array(20).fill(1_000_000),
    super: Array(20).fill(2_000_000),
    big: Array(20).fill(-1_000_000),
    mid: Array(20).fill(500_000),
    small: Array(20).fill(-500_000),
  },
};
context.d = d;

assert.equal(vm.runInContext("_stockFlowPeriodValue(d, 'main', 19, 3, 'amount')", context), 3_000_000);
assert.equal(vm.runInContext("_stockFlowPeriodValue(d, 'main', 19, 3, 'pct')", context), 100);
assert.equal(vm.runInContext("_stockFlowPeriodValue(d, 'main', 1, 3, 'amount')", context), null,
  '不足 3 个交易日时不得伪造 3 日累计');

d.series.mid[18] = null;
assert.equal(vm.runInContext("_stockFlowPeriodValue(d, 'mid', 19, 3, 'amount')", context), null,
  '窗口内存在缺失值时应显示缺失');

const amountTip = vm.runInContext("_stockFlowPeriodTooltip(d, 19, 'amount')", context);
for (const label of ['当日', '3日', '5日', '10日', '20日', '主力', '超大单', '大单', '中单', '小单']) {
  assert.match(amountTip, new RegExp(label));
}
assert.match(amountTip, /2026-08-20 · 累计净流入/);
assert.match(amountTip, /\+0\.03亿/);

const pctTip = vm.runInContext("_stockFlowPeriodTooltip(d, 19, 'pct')", context);
assert.match(pctTip, /累计净占比/);
assert.match(pctTip, /\+100\.00%/);

const mainOnlyTip = vm.runInContext("_stockFlowPeriodTooltip(d, 19, 'amount', ['main'])", context);
assert.match(mainOnlyTip, /主力/);
for (const hiddenLabel of ['超大单', '大单', '中单', '小单']) {
  assert.doesNotMatch(mainOnlyTip, new RegExp(hiddenLabel), `关闭的${hiddenLabel}不应出现在悬浮提示中`);
}

context.chart = {
  getOption: () => ({ legend: [{ selected: {
    '主力净流入': true,
    '超大单净流入': false,
    '大单净流入': true,
    '中单净流入': false,
    '小单净流入': false,
  } }] }),
};
assert.deepEqual(
  Array.from(vm.runInContext('_stockFlowVisibleKeys(chart)', context)),
  ['main', 'big'],
  '悬浮列应跟随当前图例开关状态',
);

console.log('PASS: 个股资金流悬浮提示按图例开关显示当日及 3/5/10/20 日累计');
