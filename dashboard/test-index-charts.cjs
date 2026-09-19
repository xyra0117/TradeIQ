// 无浏览器依赖：运行真实弹窗逻辑，模拟网络乱序和 ECharts 接收的数据。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
const source = html.slice(html.indexOf('const KLINE_UP ='), html.indexOf('// ── 交割分析:'));
const deliverySource = html.slice(html.indexOf('// ── 交割分析:'), html.indexOf('let _mktFlowDetailPct'));
const elements = new Map(), requests = [], charts = [];
function element() {
  return { style: {}, clientWidth: 900, textContent: '', children: [],
    replaceChildren() { this.children = []; },
    append(...nodes) { this.children.push(...nodes); },
    remove() { elements.clear(); } };
}
const context = vm.createContext({
  console, fmtDate: d => d,
  window: { addEventListener() {} },
  document: {
    getElementById: id => elements.get(id), createElement: element,
    addEventListener() {}, removeEventListener() {},
    body: { insertAdjacentHTML(_where, text) {
      for (const match of text.matchAll(/id="([^"]+)"/g)) elements.set(match[1], element());
    } },
  },
  fetch(url) { return new Promise(resolve => requests.push({ url, resolve })); },
  echarts: { init(box) {
    const chart = { box, disposed: false, setOption(option) { this.option = option; },
      dispose() { this.disposed = true; }, resize(size) { this.size = size; } };
    charts.push(chart); return chart;
  } },
});
vm.runInContext(source, context);
const run = code => vm.runInContext(code, context);
const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
const reply = async (request, data, ok = true) => {
  request.resolve({ ok, json: async () => data }); await settle();
};

(async () => {
  assert.match(deliverySource, /data: \[5, 10, 20, 30, 60\]\.map\(n => `MA\$\{n\}`\)/,
    '交割分析图例应显示五条均线名称');
  assert.match(deliverySource, /\.\.\.\[5, 10, 20, 30, 60\]\.map\(n => \(\{/,
    '交割分析应绘制 MA5、MA10、MA20、MA30、MA60');
  assert.match(deliverySource, /const macdData = _klineMACD\(closes\)/,
    '交割分析应使用与日 K 弹窗一致的 MACD 计算');
  assert.match(deliverySource, /xAxisIndex: \[0, 1, 2, 3\]/,
    '交割分析的缩放应联动 K 线、成交量、主力净流入和 MACD');
  assert.match(deliverySource, /name: 'MACD', type: 'bar'/,
    '交割分析应绘制 MACD 柱');
  assert.match(deliverySource, /name: 'DIF', type: 'line'/,
    '交割分析应绘制 DIF 曲线');
  assert.match(deliverySource, /name: 'DEA', type: 'line'/,
    '交割分析应绘制 DEA 曲线');
  assert.match(deliverySource, /id: 'delivery-candles'/,
    '买卖点二次定位应按唯一 id 更新蜡烛图');
  assert.match(deliverySource, /const offsetPx = 16;/,
    '交割分析的 B/S 标签应距离 K 线 16 像素');
  assert.match(source, /const KLINE_MA_COLORS = \{ 5: '#7ac8ff', 10: '#e8a45a', 20: '#c89cff', 30: '#4dd0e1', 60: '#6fbf73' \}/,
    '每条均线应有固定且不同的颜色');
  assert.equal((html.match(/\{pickDate:/g) || []).length, 15, '选股推荐和条件优选的名称/代码入口都应传入选股日期');
  await run("showKlineModal('000001.SH','上证指数',{kind:'index',date:'20250102'})");
  const oldMin = requests.at(-1);
  assert.match(oldMin.url, /index\/minline.*date=20250102/);
  run("_klineSwitchTab('daily')");
  const dailyRequest = requests.at(-1);
  assert.equal(dailyRequest.url, '/api/index/kline?ts_code=000001.SH');
  const rows = Array.from({ length: 310 }, (_, i) => ({
    trade_date: String(20250101 + i), open: i === 2 ? null : 10,
    high: 12, low: 9, close: 11, change: 1, volume: 123.45,
  }));
  await reply(dailyRequest, { count: 310, rows, start_date: '20250101', end_date: '20260911', message: '1 个交易日缺少完整开高低收' });
  const daily = charts.at(-1).option;
  assert.equal(daily.xAxis[0].data.length, 310);
  assert.deepEqual(Array.from(daily.series[0].data[2]), ['-', '-', '-', '-']);
  assert.equal(daily.series[1].data[4], 11); // 缺开盘仍有收盘：MA 不得跳过该日。
  assert.equal(daily.yAxis[1].name, '成交额（亿元）');
  assert.equal(daily.yAxis[2].name, 'MACD (12,26,9)');
  assert.deepEqual(Array.from(daily.dataZoom[0].xAxisIndex), [0, 1, 2]);
  assert.equal(daily.series[9].name, 'MACD');
  assert.equal(daily.series[10].name, 'DIF');
  assert.equal(daily.series[11].name, 'DEA');
  assert.match(daily.tooltip.formatter([{ seriesName: '日K', dataIndex: 0 }]), /成交额 123.45亿元/);
  assert.ok(Math.abs(daily.dataZoom[0].start - (100 - 60 / 310 * 100)) < 0.001);
  assert.match(elements.get('klineContext').textContent, /20260911/);
  elements.get('klineChartBox').clientWidth = 600;
  run('_resizeActiveKlineChart()');
  assert.equal(charts.at(-1).size.width, 600);

  // 先切到日 K，后返回的分时不能把隐藏图重新显示出来。
  await reply(oldMin, { count: 1, trade_date: '20250102', rows: [{ m: '09:30:00', p: 10, avg_p: 10, v: 30 }] });
  assert.equal(elements.get('klineMinBox').style.display, 'none');
  run('closeKlineModal()');
  assert.ok(charts.every(chart => chart.disposed));

  // 关闭后再开另一个指数，旧响应必须被丢弃。
  await run("showKlineModal('000001.SH','上证指数',{kind:'index',date:'20250102'})");
  const stale = requests.at(-1);
  await run("showKlineModal('399001.SZ','深证成指',{kind:'index',date:'20260911'})");
  const fresh = requests.at(-1), countBefore = charts.length;
  await reply(stale, { count: 1, rows: [{ m: '09:30:00', p: 10, v: 1 }] });
  assert.equal(charts.length, countBefore);
  await reply(fresh, { status: 'error', message: '请重试' }, false);
  const errorBox = elements.get('klineMinBox');
  assert.equal(errorBox.children[0].textContent, '请重试');
  errorBox.children[1].onclick();
  assert.equal(requests.at(-1).url, fresh.url);
  await reply(requests.at(-1), { count: 1, rows: [{ m: '09:30:00', p: 11, avg_p: 10, v: 30 }] });
  assert.equal(charts.length, countBefore + 1);

  // 个股仍使用个股接口、250 日和盘中补 K 参数。
  await run("showKlineModal('000001.SZ','平安银行',{pickDate:'20250121'})");
  assert.equal(requests.at(-1).url, '/api/stock/minline?ts_code=000001.SZ&date=');
  run("_klineSwitchTab('daily')");
  assert.equal(requests.at(-1).url, '/api/stock/daily?ts_code=000001.SZ&limit=250&intraday=1');
  assert.equal(elements.get('klineContext').hidden, true);
  const stockRows = Array.from({ length: 100 }, (_, i) => {
    const day = new Date(Date.UTC(2025, 0, i + 1)).toISOString().slice(0, 10).replaceAll('-', '');
    return { trade_date: day, open: 10, high: 12, low: 9, close: 11, change: 1, volume: 10000 };
  });
  await reply(requests.at(-1), { count: stockRows.length, rows: stockRows });
  assert.match(charts.at(-1).option.tooltip.formatter([{ seriesName: '日K', dataIndex: 0 }]), /万手/);
  assert.equal(charts.at(-1).option.series[6].name, 'VOL');
  assert.equal(charts.at(-1).option.series[0].markLine.data[0].xAxis, '20250121');
  assert.equal(charts.at(-1).option.series[0].markLine.lineStyle.width, 1);
  assert.equal(charts.at(-1).option.series[0].markLine.label.show, false);
  assert.equal(charts.at(-1).option.series[0].markPoint, undefined);
  assert.equal(charts.at(-1).option.series[6].markLine.data[0].xAxis, '20250121');
  assert.equal(charts.at(-1).option.series[6].markLine.label.show, false);
  assert.equal(charts.at(-1).option.series[9].markLine.data[0].xAxis, '20250121');
  assert.match(charts.at(-1).option.tooltip.formatter([{ seriesName: '日K', dataIndex: 20 }]), /DIF .*DEA .*MACD/);
  assert.ok(charts.at(-1).option.dataZoom[0].start <= 20 && charts.at(-1).option.dataZoom[0].end >= 20, '初始窗口应包含选股日');
  run('closeKlineModal()');
  console.log('PASS: 全历史、缺失 OHLC、MA、成交额、乱序响应、切换隐藏、重试、选股日标记');
})().catch(error => { console.error(error); process.exitCode = 1; });
