const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
const start = html.indexOf('function _bindPicksFlowDelegation');
const end = html.indexOf('function renderConditionalPicksTable', start);
assert.ok(start >= 0 && end > start, '缺少统一资金流点击委托函数');
assert.equal((html.match(/_bindPicksFlowDelegation\(tbody\);/g) || []).length, 4,
  '四类选股表都应使用不捕获旧列表的统一委托');
assert.equal((html.match(/data-stock-name="\$\{escapeHtml\(name\)\}"/g) || []).length, 4,
  '四类选股表的当前行都应携带股票名称');

const calls = [];
const tbody = {
  handler: null,
  contains() { return true; },
  addEventListener(_type, handler) { this.handler = handler; },
};
const context = vm.createContext({
  openStockFlowDetail(...args) { calls.push(args); },
});
vm.runInContext(html.slice(start, end), context);
context.tbody = tbody;
vm.runInContext('_bindPicksFlowDelegation(tbody)', context);

// 首次列表渲染后绑定；切到旧日期只会替换 innerHTML，不会重新绑定监听器。
// 点击必须读取当前行 data，而不能继续查找首次 render 捕获的旧 list。
const currentRow = { dataset: { tsCode: '000002.SZ', stockName: '历史股票' } };
const cell = { closest(selector) { return selector === '.picks-flow-cell' ? this : currentRow; } };
tbody.handler({ target: cell, stopPropagation() {} });
assert.deepEqual(calls, [['000002.SZ', '历史股票', 'main']]);

console.log('PASS: 切换历史日期后资金列仍使用当前行打开弹窗');
