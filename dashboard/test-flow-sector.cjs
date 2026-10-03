// 板块资金流前端测试 - 验证 _sectorFlowRowHtml 渲染 / sub-tab 切换 / API 路由
// vm slice 模式: 切 _sectorFlowRowHtml + _renderFlowSector + _refreshSectorFlowDates +
// 板块二级 sub-tab click handler, 涵盖所有新加的 JS 函数
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');

// ── DOM mock ──
const elements = new Map();
function el() {
  return {
    innerHTML: '', textContent: '', value: '',
    style: {}, dataset: {}, children: [],
    addEventListener() {}, removeEventListener() {},
    appendChild() {}, insertBefore() {}, removeChild() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
}
const calls = [];
const elementsByQuery = new Map();

function getEl(id) { if (!elements.has(id)) elements.set(id, el()); return elements.get(id); }

const ctx = vm.createContext({
  document: {
    getElementById: getEl,
    createElement: () => el(),
    querySelector(sel) {
      if (!elementsByQuery.has(sel)) elementsByQuery.set(sel, el());
      return elementsByQuery.get(sel);
    },
    querySelectorAll(sel) {
      // 模拟 flowSectorSubtabs 下的 sub-tab 集合 (返回带 addEventListener 的数组)
      if (sel === '#flowSectorSubtabs .pos-subtab') {
        return [
          { dataset: { flowsectortype: 'industry' }, classList: { add() {}, remove() {} }, addEventListener() {} },
          { dataset: { flowsectortype: 'concept' },  classList: { add() {}, remove() {} }, addEventListener() {} },
        ];
      }
      if (sel === '#flowSectorSubtabs .pos-subtab[data-flowsectortype]') {
        return [
          { dataset: { flowsectortype: 'industry' }, classList: { add() {}, remove() {} }, addEventListener() {} },
          { dataset: { flowsectortype: 'concept' },  classList: { add() {}, remove() {} }, addEventListener() {} },
        ];
      }
      return [];
    },
    body: el(),
    addEventListener() {}, removeEventListener() {},
  },
  URLSearchParams, console, setTimeout, clearTimeout, Date, JSON, Math, Promise,
  fetch(url, opts) {
    return new Promise(resolve => calls.push({ url, opts, resolve }));
  },
  // ── globals used by sector functions ──
  _flowColor: v => (v > 0 ? 'red' : v < 0 ? 'green' : 'inherit'),
  _flowFmtYuan: v => {
    if (v == null) return '—';
    const n = Number(v);
    const sign = n < 0 ? '-' : '';
    const abs = Math.abs(n);
    if (abs >= 1e8) return sign + (abs / 1e8).toFixed(2) + '亿';
    if (abs >= 1e4) return sign + (abs / 1e4).toFixed(2) + '万';
    return String(n);
  },
  _sectorFlowType: 'industry',
  _sectorFlowDates: [],
  _sectorFlowSelDate: '',
  _sectorFlowSort: { col: 'main_net_inflow', dir: 'desc' },
  _flowPeriod: 'today',
  // _flowSource / _flowSelDate / _flowDates for cross-checks
  _flowSource: 'em',
  _flowSelDate: '',
  _flowDates: [],
  _flowSort: { col: 'main_net_inflow', dir: 'desc' },
  _checkFlowGaps() {}, renderMktFlow() {},
  window: { fpInstances: {} },
  updateSortArrows() {},
});

// ── 切片: 板块相关 JS ──
// 起点: // ── 板块数据源 注释处
// 终点: 顶部 status bar 注释之前
const startIdx = html.indexOf('// ── 板块数据源 (sector_fund_flow');
const endIdx = html.indexOf('// 顶部 status bar');
vm.runInContext(html.slice(startIdx, endIdx), ctx);

(async () => {
  // ── Test 1: _sectorFlowRowHtml 渲染 13 列, 列口径一致 ──
  console.log('[t] _sectorFlowRowHtml 渲染');
  const row = ctx._sectorFlowRowHtml({
    sector_code: 'BK1036',
    name: '半导体',
    close: 1000.5,
    change_pct: 2.3,
    main_net_inflow: 4.5e8,
    main_net_pct: 5.6,
    super_net: 2.7e8,
    super_pct: 3.4,
    big_net: 1.8e8,
    big_pct: 2.2,
    mid_net: -5.0e7,
    mid_pct: -0.6,
    small_net: -1.0e8,
    small_pct: -1.2,
    lead_stock_name: '中芯国际',
    lead_stock_code: '688981',
    trade_date: '20260915',
  }, 0);
  // 验证关键字段
  assert.match(row, /BK1036/, '包含板块代码');
  assert.match(row, /半导体/, '包含板块名');
  assert.match(row, /2\.30%/, '涨跌幅格式化');
  assert.match(row, /4\.50亿/, '主力净流入格式化');
  assert.match(row, /5\.60%/, '主力净占比格式化');
  assert.match(row, /688981/, '领涨股代码');
  assert.match(row, /中芯国际/, '领涨股名');
  assert.match(row, /20260915/, '交易日期');
  // 验证 onclick 调 showKlineModal (688981 是 6 位且不以 6/9 开头? 实际 688981 是科创板, 以 6/8 开头, 走 SH)
  assert.match(row, /showKlineModal\('688981\.SH'/, '6 位代码转 ts_code');

  // ── Test 2: 领涨股空时不调用 showKlineModal ──
  const rowNoLead = ctx._sectorFlowRowHtml({
    sector_code: 'BK9999', name: '测试', change_pct: 1.0,
    main_net_inflow: 1e8, main_net_pct: 1.0,
    super_net: 0, big_net: 0, mid_net: 0, small_net: 0,
    lead_stock_name: null, lead_stock_code: null,
    trade_date: '20260915',
  }, 1);
  assert.doesNotMatch(rowNoLead, /showKlineModal/, '空领涨股不调 showKlineModal');
  assert.match(rowNoLead, /—/, '空字段显示 —');

  // ── Test 3: 领涨股代码 6 位但格式非 BK, 转 ts_code (002213 = SZ) ──
  const rowSz = ctx._sectorFlowRowHtml({
    sector_code: 'BK1036', name: '测试', change_pct: 0, main_net_inflow: 0,
    main_net_pct: 0, super_net: 0, big_net: 0, mid_net: 0, small_net: 0,
    lead_stock_name: '大族激光', lead_stock_code: '002213', trade_date: '20260915',
  }, 2);
  assert.match(rowSz, /showKlineModal\('002213\.SZ'/, '002xxx 走 .SZ');

  // ── Test 4: 颜色染色 (正数红 / 负数绿) ──
  const rowPos = ctx._sectorFlowRowHtml({
    sector_code: 'BK1', name: '涨', change_pct: 5.0, main_net_inflow: 1e8,
    main_net_pct: 0, super_net: 0, big_net: 0, mid_net: 0, small_net: 0,
    lead_stock_name: null, lead_stock_code: null, trade_date: '20260915',
  }, 0);
  assert.match(rowPos, /color:red/, '正值染红');
  const rowNeg = ctx._sectorFlowRowHtml({
    sector_code: 'BK1', name: '跌', change_pct: -3.0, main_net_inflow: -1e8,
    main_net_pct: -1.0, super_net: 0, big_net: 0, mid_net: 0, small_net: 0,
    lead_stock_name: null, lead_stock_code: null, trade_date: '20260915',
  }, 0);
  assert.match(rowNeg, /color:green/, '负值染绿 (A 股习惯)');

  // ── Test 5: _renderFlowSector 发请求参数 ──
  console.log('[t] _renderFlowSector fetch');
  ctx._sectorFlowType = 'industry';
  ctx._sectorFlowSelDate = '20260915';
  ctx._sectorFlowSort = { col: 'main_net_inflow', dir: 'desc' };
  calls.length = 0;
  const tbody = getEl('flowMktTbody');
  const empty = getEl('flowMktEmpty');
  empty.querySelectorAll = () => [{ textContent: '' }, { textContent: '' }];
  ctx.document.getElementById('flowMktSearch').value = '半导';
  const pRender = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 30));
  assert.equal(calls.length, 1, '行业渲染触发 1 次 fetch');
  assert.match(calls[0].url, /\/api\/flow\/sector\/market\?/);
  assert.match(calls[0].url, /type=industry/, 'type=industry');
  assert.match(calls[0].url, /date=20260915/, 'date=20260915');
  assert.match(calls[0].url, /search=/, 'search 参数');
  assert.match(calls[0].url, /sort=main_net_inflow/, 'sort 默认');
  assert.match(calls[0].url, /order=desc/, 'order 默认');
  assert.match(calls[0].url, /limit=500/, 'limit=500 免截断');
  // resolve 5 行, 验渲染
  calls[0].resolve({
    ok: true,
    json: async () => ({
      count: 3, trade_date: '20260915', type: 'industry',
      rows: [
        { sector_code: 'BK1', name: '半导体', change_pct: 5.0, main_net_inflow: 5e8, main_net_pct: 0, super_net: 0, big_net: 0, mid_net: 0, small_net: 0, lead_stock_name: null, lead_stock_code: null, trade_date: '20260915' },
        { sector_code: 'BK2', name: '通信',   change_pct: 3.0, main_net_inflow: 3e8, main_net_pct: 0, super_net: 0, big_net: 0, mid_net: 0, small_net: 0, lead_stock_name: null, lead_stock_code: null, trade_date: '20260915' },
        { sector_code: 'BK3', name: '医药',   change_pct: 1.0, main_net_inflow: 1e8, main_net_pct: 0, super_net: 0, big_net: 0, mid_net: 0, small_net: 0, lead_stock_name: null, lead_stock_code: null, trade_date: '20260915' },
      ],
    }),
  });
  await pRender;
  assert.match(tbody.innerHTML, /半导体/, 'tbody 渲染了行业行');
  assert.match(tbody.innerHTML, /BK1/, 'tbody 包含板块代码');
  assert.match(tbody.innerHTML, /BK3/, 'tbody 包含所有 3 个板块');
  const info = getEl('flowMktInfo');
  assert.match(info.textContent, /3 行 · 行业/, 'info 显示 行业 + 行数');
  assert.match(info.textContent, /main_net_inflow ↓/, 'sort 标签正确');

  // ── Test 6: 空数据 → empty state ──
  console.log('[t] 空数据 empty state');
  calls.length = 0;
  tbody.innerHTML = '';
  const pEmpty = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  calls[0].resolve({ ok: true, json: async () => ({ count: 0, rows: [] }) });
  await pEmpty;
  assert.equal(tbody.innerHTML, '', '空数据 tbody 清空');
  assert.match(info.textContent, /0 行 · 行业/, '空数据 info');

  // ── Test 7: _refreshSectorFlowDates 走正确 API ──
  console.log('[t] _refreshSectorFlowDates');
  calls.length = 0;
  ctx._sectorFlowSelDate = '';
  const pDates = ctx._refreshSectorFlowDates();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(calls.length, 1, 'dates 1 次 fetch');
  assert.match(calls[0].url, /\/api\/flow\/sector\/dates\?type=industry/, 'type=industry dates');
  calls[0].resolve({ ok: true, json: async () => ({ dates: ['20260915', '20260912', '20260911'], count: 3, type: 'industry' }) });
  await pDates;
  assert.deepEqual(ctx._sectorFlowDates, ['20260915', '20260912', '20260911'], 'dates 写入 _sectorFlowDates');
  assert.equal(ctx._sectorFlowSelDate, '20260915', '首次自动选最新日期');

  // ── Test 8: _refreshSectorFlowDates 切到概念 ──
  console.log('[t] 切概念 sub-tab');
  calls.length = 0;
  ctx._sectorFlowType = 'concept';
  ctx._sectorFlowSelDate = '20260915';  // 已选日期不会被覆盖
  const pConcept = ctx._refreshSectorFlowDates();
  await new Promise(r => setTimeout(r, 20));
  assert.match(calls[0].url, /type=concept/, '概念用 type=concept');
  calls[0].resolve({ ok: true, json: async () => ({ dates: ['20260915'], count: 1, type: 'concept' }) });
  await pConcept;
  assert.deepEqual(ctx._sectorFlowDates, ['20260915']);

  // ── Test 9a: API 返 {error:...} → 走 empty 分支, 不崩 ──
  console.log('[t] API error body 不崩');
  calls.length = 0;
  tbody.innerHTML = '';
  const pErr = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  calls[0].resolve({ ok: false, json: async () => ({ error: 'invalid type' }) });
  await pErr;
  // API 错误 → empty 分支 → tbody 清空
  assert.equal(tbody.innerHTML, '', 'API error body → tbody 清空');

  // ── Test 9b: fetch throw (反爬限流 / 网络断) → 走 catch, 显示加载失败 ──
  console.log('[t] fetch throw → catch 分支');
  calls.length = 0;
  // 临时让 fetch reject
  ctx.fetch = () => Promise.reject(new Error('network down'));
  const pThrow = ctx._renderFlowSector();
  await pThrow.catch(() => {});  // _renderFlowSector 内部有 try/catch, 这里吞掉 promise 链未捕获错误
  await new Promise(r => setTimeout(r, 20));
  assert.match(getEl('flowMktTbody').innerHTML, /加载失败/, '网络断 → catch 分支显示加载失败');
  // 恢复 fetch mock
  ctx.fetch = (url, opts) => new Promise(resolve => calls.push({ url, opts, resolve }));

  // ── Test 10: 排序变更后渲染用新 sort ──
  console.log('[t] sort 变更');
  ctx._sectorFlowSort = { col: 'change_pct', dir: 'asc' };
  calls.length = 0;
  const pSort = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  assert.match(calls[0].url, /sort=change_pct/, 'sort=change_pct');
  assert.match(calls[0].url, /order=asc/, 'order=asc');

  // ── Test 11: _sectorFlowRowHtml 走 isAgg 渲染 (period=3d) ──
  console.log('[t] _sectorFlowRowHtml isAgg');
  ctx._flowPeriod = '3d';
  const rowAgg = ctx._sectorFlowRowHtml({
    sector_code: 'BK1036',
    name: '半导体',
    close: 1050.0,
    change_pct: 3.5,
    sum_main_net: 9.0e8,
    sum_super_net: 4.5e8,
    sum_big_net: 3.0e8,
    sum_mid_net: 1.0e8,
    sum_small_net: 5.0e7,
    flow_days: 3,
    start_date: '20260911',
    end_date: '20260915',
    lead_stock_name: '中芯国际',
    lead_stock_code: '688981',
  }, 0);
  // 累计金额格式化
  assert.match(rowAgg, /9\.00亿/, 'sum_main_net 累计格式化');
  assert.match(rowAgg, /4\.50亿/, 'sum_super_net 累计格式化');
  // 净占比 — — (agg 模式)
  assert.match(rowAgg, /<td[^>]*>—<\/td>/, 'agg 模式净占比 —');
  // 日期列 — "3日累计"
  assert.match(rowAgg, /3日累计/, '日期列显示 Nd 累计');
  // tooltip 含日期范围
  assert.match(rowAgg, /20260911/, 'tooltip 含 start_date');
  assert.match(rowAgg, /20260915/, 'tooltip 含 end_date');
  // 染色 — 正数红
  assert.match(rowAgg, /color:red/, '正值染红 (A 股习惯)');
  ctx._flowPeriod = 'today';  // 还原

  // ── Test 12: _renderFlowSector period=3d 不带 date 参数 ──
  console.log('[t] _renderFlowSector period=3d 不带 date');
  ctx._sectorFlowType = 'industry';
  ctx._sectorFlowSelDate = '20260915';
  ctx._sectorFlowSort = { col: 'sum_main_net', dir: 'desc' };
  ctx._flowPeriod = '3d';
  calls.length = 0;
  const p3d = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(calls.length, 1, 'period=3d 1 次 fetch');
  assert.match(calls[0].url, /period=3d/, 'period=3d 参数');
  assert.doesNotMatch(calls[0].url, /date=20260915/, 'agg 模式不传 date');
  assert.match(calls[0].url, /sort=sum_main_net/, 'agg 模式默认 sort=sum_main_net');
  // resolve 1 行, 验 info 文本
  calls[0].resolve({
    ok: true,
    json: async () => ({
      count: 1, period: '3d', type: 'industry',
      rows: [{
        sector_code: 'BK1036', name: '半导体',
        close: 1000, change_pct: 2.0,
        sum_main_net: 5.0e8, sum_super_net: 2.0e8, sum_big_net: 1.5e8,
        sum_mid_net: 1.0e8, sum_small_net: 5.0e7,
        flow_days: 3, start_date: '20260911', end_date: '20260915',
        lead_stock_name: null, lead_stock_code: null,
      }],
    }),
  });
  await p3d;
  assert.match(getEl('flowMktInfo').textContent, /3日累计/, 'info 显示 3日累计');
  assert.match(getEl('flowMktInfo').textContent, /sum_main_net/, 'info 含 sort 标签');

  // ── Test 13: _renderFlowSector period=20d (4 档累计全通) ──
  console.log('[t] _renderFlowSector period=20d');
  ctx._flowPeriod = '20d';
  calls.length = 0;
  const p20 = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  assert.match(calls[0].url, /period=20d/, 'period=20d 参数');
  assert.doesNotMatch(calls[0].url, /date=/, '20d 也不带 date');
  calls[0].resolve({ ok: true, json: async () => ({ count: 0, rows: [], period: '20d', type: 'industry' }) });
  await p20;

  // ── Test 14: agg 模式 + 概念 sub-tab ──
  console.log('[t] agg 模式 + 概念');
  ctx._sectorFlowType = 'concept';
  ctx._flowPeriod = '5d';
  calls.length = 0;
  const pConcept5d = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  assert.match(calls[0].url, /type=concept/, '概念用 type=concept');
  assert.match(calls[0].url, /period=5d/, '5d 累计');
  calls[0].resolve({ ok: true, json: async () => ({ count: 0, rows: [], period: '5d', type: 'concept' }) });
  await pConcept5d;

  // ── Test 15: today 模式带回 date 参数 (regression 保护) ──
  console.log('[t] today 模式带回 date');
  ctx._flowPeriod = 'today';
  ctx._sectorFlowType = 'industry';
  ctx._sectorFlowSelDate = '20260915';
  ctx._sectorFlowSort = { col: 'main_net_inflow', dir: 'desc' };  // today 默认 sort
  calls.length = 0;
  const pToday = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  assert.match(calls[0].url, /period=today/, 'period=today');
  assert.match(calls[0].url, /date=20260915/, 'today 模式带 date=20260915');
  assert.match(calls[0].url, /sort=main_net_inflow/, 'today sort=main_net_inflow');
  calls[0].resolve({ ok: true, json: async () => ({ count: 0, rows: [], period: 'today', type: 'industry' }) });
  await pToday;

  // ── Test 16: agg 模式 sort 透传给后端 (前端不做白名单, 后端负责回退) ──
  console.log('[t] agg sort 透传');
  ctx._flowPeriod = '3d';
  ctx._sectorFlowSort = { col: 'sum_super_net', dir: 'asc' };
  calls.length = 0;
  const pEvil = ctx._renderFlowSector();
  await new Promise(r => setTimeout(r, 20));
  assert.match(calls[0].url, /sort=sum_super_net/, 'agg 模式 sort=sum_super_net 透传');
  assert.match(calls[0].url, /order=asc/, 'order=asc 透传');
  calls[0].resolve({ ok: true, json: async () => ({ count: 0, rows: [], period: '3d', type: 'industry' }) });
  await pEvil;

  console.log('\n✅ 全部测试通过');
})().catch(err => {
  console.error('❌ 测试失败:', err);
  process.exit(1);
});
