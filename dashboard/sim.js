/* 模拟盘: 历史行情回放 + 人工纸面交易。
 *
 * 一条铁律: **前端不持有时钟**。cur_date / cur_min 由后端 sim_session 决定,
 * 分时接口也不接受客户端传时间 —— 也就是说这里永远拿不到"当前时刻之后"的
 * 分时。这不是为了保密, 是回放的意义所在: 你不知道未来才能练出判断力。
 * 服务端会把订单时间戳盖成它自己的时钟, 所以这里传什么都没用, 也就别传了。
 */
(function () {
  'use strict';

  var root = window;
  var $ = function (id) { return document.getElementById(id); };
  var CHART_KEY = 'tradeiq.sim.watch.v1';
  var ACCT_KEY = 'tradeiq.sim.acct.v1';

  // 分时图纵轴的半幅阶梯(相对昨收的百分比)。取最小的、能装下当日最大偏离的一档,
  // 于是轴按整数百分比「圆整」, 跟同花顺/通达信的普通分时坐标一致。
  var PCT_STEPS = [0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50];

  var state = {
    accountId: null,
    session: null,
    account: null,
    positions: [],
    orders: [],          // 全部成交, 分时图按当前票挑出 B/S 点
    watch: [],          // [{ts_code, name}]
    active: null,       // 当前查看的票
    viewDate: null,     // 图表在看哪一天; 默认等于回放时钟所在日, 可往前回看
    pinnedDate: false,  // 用户手动挑过日期就别再被时钟拽走
    calendar: [],       // 可回放的交易日
    quote: null,        // 该票截至当前时刻的分时
    side: 'buy',
    godMode: false,
    playing: false,
    speed: 5,
    seq: 0,             // 防竞态: 慢响应丢弃
    timer: null,
    chart: null,
    navChart: null
  };

  function fmtMoney(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
  }
  // A 股口径: 涨红跌绿。注意项目里 --accent-green 是红色, --accent-red 是绿色。
  function pnlColor(v) {
    if (v === null || v === undefined || isNaN(v) || v === 0) return 'var(--text-secondary)';
    return v > 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function toast(msg, ok) {
    if (root.showToast) root.showToast(msg);
    else alert(msg);
  }

  // ── API ────────────────────────────────────────────────

  async function api(path, options) {
    var response = await fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json' }
    }, options || {}));
    var data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (!data || data.status !== 'success') {
      var err = new Error((data && data.message) || ('请求失败 (' + response.status + ')'));
      err.status = response.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function loadWatch() {
    try {
      var raw = localStorage.getItem(CHART_KEY);
      state.watch = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(state.watch)) state.watch = [];
    } catch (e) { state.watch = []; }
  }
  function saveWatch() {
    try { localStorage.setItem(CHART_KEY, JSON.stringify(state.watch)); } catch (e) { /* 隐私模式忽略 */ }
  }

  // ── 时钟 ──────────────────────────────────────────────

  async function step(op, steps) {
    var seq = ++state.seq;
    var payload = await api('/api/sim/replay/step', {
      method: 'POST',
      body: JSON.stringify({ account_id: state.accountId, op: op, steps: steps || 1 })
    });
    if (seq !== state.seq) return;
    state.session = payload.session;
    await refresh();
  }

  async function refresh() {
    var seq = ++state.seq;
    var payload = await api('/api/sim/state?account_id=' + state.accountId);
    if (seq !== state.seq) return;
    state.session = payload.session;
    state.account = payload.account;
    state.positions = payload.positions || [];
    state.nav = payload.nav || [];
    renderClock();
    renderSummary();
    renderPositions();
    renderDatePicker();
    // 成交记录要先落地: 分时图上的 B/S 买卖点要用它, 晚一步图上就少一个标记
    await renderOrders();
    if (state.active) {
      await loadQuote(state.godMode);
      if (state.godMode) drawChartWithFuture();
    } else { state.quote = null; drawChart(); }
    drawNav();
  }

  async function loadQuote(god) {
    if (!state.active) { state.quote = null; drawChart(); return; }
    var url = '/api/sim/replay/minline?account_id=' + state.accountId
      + '&ts_code=' + encodeURIComponent(state.active)
      + (god ? '&god=1' : '');
    // 图表看的那一天。不传就用后端默认(= 回放时钟当天), 往前回看时才显式带上。
    if (state.viewDate && state.session && state.viewDate !== state.session.cur_date) {
      url += '&date=' + encodeURIComponent(state.viewDate);
    }
    var payload = await api(url);
    // 服务端会把越权的 date(指向未来/不可回放)静默夹回时钟当天, 这里跟上,
    // 并解除手动锁定 —— 免得下拉显示的日子和图上画的对不上。
    if (payload.view_date && payload.view_date !== state.viewDate) {
      state.viewDate = payload.view_date;
      state.pinnedDate = false;
      renderDatePicker();
    }
    state.quote = payload;
    drawChart();
    renderReadout();
    syncTradePanel();
  }

  // 日期控件: 可回放日历里、且不晚于回放时钟的那些天。
  // 往前回看是为了复盘(比如买点的 B 标在上周五那张图上), 往后一律不给 ——
  // 服务端也会再拒一次, 这里是让用户根本选不到。
  function renderDatePicker() {
    var box = $('simChartDate');
    if (!box) return;
    var cal = state.calendar || [];
    var clock = state.session ? state.session.cur_date : null;
    var allowed = cal.filter(function (d) { return clock && d <= clock; });
    if (!allowed.length) { box.innerHTML = ''; return; }
    // 没手动挑过就跟着回放时钟走, 跨日时图表自动跟进; 手动挑过就一直看那几天,
    // 不会因为时钟往前跑就被拽走。
    if (!state.pinnedDate || allowed.indexOf(state.viewDate) < 0) {
      state.viewDate = clock;
    }
    box.innerHTML = allowed.map(function (d) {
      return '<option value="' + d + '"' + (d === state.viewDate ? ' selected' : '')
        + '>' + d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8) + '</option>';
    }).join('');
  }

  // ── 渲染 ──────────────────────────────────────────────

  function renderClock() {
    var s = state.session;
    if (!s) return;
    $('simClockNow').textContent = s.cur_min.slice(0, 5);
    var flags = [];
    if (s.cur_min === '09:25:00') flags.push('集合竞价');
    if (s.cur_min === '15:00:00') flags.push('收盘');
    $('simClockDay').textContent = s.cur_date.slice(0, 4) + '-'
      + s.cur_date.slice(4, 6) + '-' + s.cur_date.slice(6, 8)
      + (s.next_date ? ('  ·  下一天 ' + s.next_date.slice(4, 6) + '-' + s.next_date.slice(6, 8)) : '  ·  已是最后一天')
      + (flags.length ? ('  ·  ' + flags.join(' · ')) : '');
    $('simTailNote').style.display = '';
    $('simPlay').textContent = state.playing ? '⏸ 暂停' : '▶ 播放';
    $('simPlay').classList.toggle('syncing', state.playing);
  }

  function renderSummary() {
    var a = state.account;
    if (!a) return;
    var profit = a.total_profit;
    var cards = [
      ['总资产', fmtMoney(a.total_asset), 'var(--text-primary)'],
      ['可用资金', fmtMoney(a.cash), 'var(--text-primary)'],
      ['持仓市值', fmtMoney(a.market_value), 'var(--text-primary)'],
      ['累计盈亏', (profit >= 0 ? '+' : '') + fmtMoney(profit), pnlColor(profit)],
      ['收益率', fmtPct(a.total_profit_pct), pnlColor(profit)],
      ['累计手续费', fmtMoney(a.total_fee), 'var(--text-secondary)']
    ];
    $('simSummary').innerHTML = cards.map(function (c) {
      return '<div class="pos-stat"><span class="pos-stat-label">' + c[0]
        + '</span><span class="pos-stat-value" style="color:' + c[2] + '">'
        + c[1] + '</span></div>';
    }).join('');
  }

  function renderPositions() {
    var table = $('simPosTable');
    if (!state.positions.length) {
      table.innerHTML = '<thead><tr><th>持仓</th><th>数量</th><th>可用</th><th>成本</th>'
        + '<th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead><tbody>'
        + '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:30px 0">'
        + '还没有持仓。上面选一只票，在右侧下单。</td></tr></tbody>';
      return;
    }
    table.innerHTML =
      '<thead><tr><th>持仓</th><th>数量</th><th>可用</th><th>成本价</th>'
      + '<th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead><tbody>'
      + state.positions.map(function (p) {
        return '<tr style="cursor:pointer" data-code="' + esc(p.ts_code) + '">'
          + '<td><b>' + esc(p.name || p.ts_code) + '</b><br><span style="font-size:10px;color:var(--text-muted)">'
          + esc(p.ts_code) + '</span></td>'
          + '<td>' + p.shares + '</td>'
          + '<td style="color:' + (p.sellable ? 'inherit' : 'var(--text-muted)') + '">' + p.sellable + '</td>'
          + '<td>' + (p.cost_price === null ? '—' : p.cost_price.toFixed(3)) + '</td>'
          // stale = 停牌/此刻还没出现, 浮盈不可知 —— 灰掉而不是假装 0
          + '<td>' + (p.stale ? '<span style="color:var(--text-muted)" title="此刻该票还没有行情">停牌/无行情</span>'
            : p.price.toFixed(2)) + '</td>'
          + '<td>' + (p.stale ? '—' : fmtMoney(p.market_value)) + '</td>'
          + '<td style="color:' + pnlColor(p.profit) + '">'
          + (p.stale ? '—' : ((p.profit >= 0 ? '+' : '') + fmtMoney(p.profit)
            + '<br><span style="font-size:10px">' + fmtPct(p.profit_pct) + '</span>'))
          + '</td></tr>';
      }).join('') + '</tbody>';
    Array.prototype.forEach.call(table.querySelectorAll('tbody tr[data-code]'), function (tr) {
      tr.addEventListener('click', function () { select(tr.dataset.code); });
    });
  }

  function renderOrders() {
    return api('/api/sim/orders?account_id=' + state.accountId + '&limit=60')
      .then(function (data) {
        state.orders = data.orders || [];   // 分时图的 B/S 买卖点要用
        var table = $('simOrdersTable');
        if (!data.orders.length) {
          table.innerHTML = '<tbody><tr><td style="text-align:center;color:var(--text-muted);padding:22px 0">'
            + '还没有成交记录</td></tr></tbody>';
          return;
        }
        table.innerHTML =
          '<thead><tr><th>时间</th><th>方向</th><th>成交价</th><th>数量</th>'
          + '<th>金额</th><th>费用</th></tr></thead><tbody>'
          + data.orders.map(function (o) {
            var buy = o.side === 'buy';
            return '<tr><td style="color:var(--text-muted)">' + esc(o.trade_date.slice(4))
              + ' ' + esc(o.minute.slice(0, 5)) + '</td>'
              + '<td style="color:' + (buy ? 'var(--accent-green)' : 'var(--accent-red)') + '">'
              + (buy ? '买入' : '卖出') + '</td>'
              + '<td>' + o.price + '</td><td>' + o.shares + '</td>'
              + '<td>' + fmtMoney(o.amount) + '</td>'
              + '<td style="color:var(--text-secondary)">' + o.total_fee.toFixed(2) + '</td></tr>';
          }).join('') + '</tbody>';
      })
      .catch(function () { /* 流水失败不影响主流程 */ });
  }

  function renderReadout() {
    var q = state.quote;
    var box = $('simReadout');
    if (!q || !q.rows.length) { box.innerHTML = ''; return; }
    var last = q.rows[q.rows.length - 1];
    var change = q.prev_close ? ((last.p - q.prev_close) / q.prev_close * 100) : null;
    var avg = last.avg_p;
    // 券商的五档盘口在历史分时里根本不存在(没有买一卖五的挂单量/价),
    // 这里如实给四行能拿到的信息, 不编一个假盘口出来。
    var items = [
      ['现价', last.p.toFixed(2), change === null ? '' : fmtPct(change), pnlColor(change)],
      ['均价', avg ? avg.toFixed(2) : '—', '', 'var(--text-primary)'],
      ['分钟量', (last.v || 0) + ' 手', '', 'var(--text-primary)'],
      ['昨收', q.prev_close ? q.prev_close.toFixed(2) : '—', '', 'var(--text-secondary)']
    ];
    box.innerHTML = items.map(function (i) {
      return '<div class="sim-ro"><b style="color:' + i[3] + '">' + i[1] + '</b>'
        + '<span>' + i[0] + (i[2] ? '  ' + i[2] : '') + '</span></div>';
    }).join('')
      // 回看历史日时把话说清楚: 图是那天的, 单还是按回放时钟那一根成交的。
      + (state.viewDate && state.session && state.viewDate !== state.session.cur_date
        ? '<div style="grid-column:1/-1;margin-top:6px;font-size:11px;color:var(--accent-yellow)">'
          + '正在回看 ' + state.viewDate.slice(0, 4) + '-' + state.viewDate.slice(4, 6) + '-'
          + state.viewDate.slice(6, 8) + '（回放时钟：' + state.session.cur_date.slice(0, 4) + '-'
          + state.session.cur_date.slice(4, 6) + '-' + state.session.cur_date.slice(6, 8)
          + ' ' + state.session.cur_min.slice(0, 5) + '），下单仍按当前时刻成交</div>'
        : '');
  }

  function renderChips() {
    var box = $('simChips');
    if (!state.watch.length) {
      box.innerHTML = '<span style="color:var(--text-muted);font-size:12px">'
        + '候选池是空的 —— 上面搜代码或名称，或从各选股 tab 加进来</span>';
      return;
    }
    box.innerHTML = state.watch.map(function (item) {
      return '<span class="sim-chip' + (item.ts_code === state.active ? ' on' : '')
        + '" data-code="' + esc(item.ts_code) + '">' + esc(item.name || item.ts_code)
        + '<span class="x" data-del="' + esc(item.ts_code) + '">×</span></span>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.sim-chip'), function (chip) {
      chip.addEventListener('click', function (e) {
        if (e.target.dataset.del) { removeWatch(e.target.dataset.del); return; }
        select(chip.dataset.code);
      });
    });
  }

  // ── 分时图 ─────────────────────────────────────────────

  function drawChart() {
    var box = $('simChartBox');
    var msg = $('simChartMsg');
    // 销毁顺序不能反: 先 dispose 再置 null 再清容器再 init, 反了会抛
    // "Cannot read properties of null"(内部 removeChild 时容器已没内容)
    if (state.chart) { state.chart.dispose(); state.chart = null; }
    box.textContent = '';
    var q = state.quote;
    if (!q || !q.rows.length) {
      msg.style.display = '';
      msg.textContent = state.active ? '该票在当前时刻还没有行情（停牌或尚未出现）'
        : '先从候选池选一只票';
      return;
    }
    msg.style.display = 'none';

    var rows = q.rows;
    var base = q.prev_close || rows[0].p;

    // 横轴固定为当日整根时间格 (09:25 → 15:00), 曲线只填到当前时刻。
    // 之前横轴是「已走过的 bar 列表」, 于是推进时钟时轴会跟着重新缩放, 曲线越走
    // 越稀 —— 看着像图在动, 其实是坐标系在变。现在轴不动, 曲线往右长出来。
    var grid = (q.grid && q.grid.length) ? q.grid : rows.map(function (r) { return r.m; });
    var pos = {};
    grid.forEach(function (m, i) { pos[m] = i; });

    var n = grid.length;
    var prices = new Array(n).fill(null);
    var avgs = new Array(n).fill(null);
    var raw = new Array(n).fill(null);
    rows.forEach(function (r) {
      var i = pos[r.m];
      if (i === undefined) return;     // 停牌/脏数据: 该格留空, 轴不塌
      prices[i] = r.p;
      avgs[i] = r.avg_p;
      raw[i] = r.v || 0;
    });
    var labels = grid.map(function (m) { return m.slice(0, 5); });

    // 标准刻度落在哪几格。午休那格(上午最后一根)与 13:00 相邻, 合成一个标签。
    var tickIdx = {};
    var morningEnd = -1;
    grid.forEach(function (m, i) { if (m.slice(0, 5) < '13:00') morningEnd = i; });
    grid.forEach(function (m, i) {
      var t = m.slice(0, 5);
      if (t === '09:30' || t === '10:30' || t === '14:00' || t === '15:00') tickIdx[i] = t;
    });
    if (morningEnd >= 0) tickIdx[morningEnd] = '11:30/13:00';

    // 成交量按「相对上一根是涨是跌」上色, 空格直接跳过
    var volumes = [];
    var prevP = base;
    for (var k = 0; k < n; k++) {
      if (raw[k] === null) { volumes.push(null); continue; }
      volumes.push({ value: raw[k],
        itemStyle: { color: prices[k] >= prevP ? '#ff5252' : '#00e676' } });
      if (prices[k] !== null) prevP = prices[k];
    }

    // 纵轴用券商分时口径: 昨收价钉死在正中, 上下按整数百分比对称扩展。
    //   同花顺「圆整最大最小值百分比」: 纵轴上下限取数据零值的整数百分比倍;
    //   同花顺文档明说「分时走势」的坐标零值就是昨收, 于是纵轴百分比 = 当天涨跌幅。
    //   通达信「普通分时坐标」: 以昨收价线为中轴。
    // 之前是拿已出现数据的 min/max 自适应再补 pad, 昨收会随行情上蹿下跳地漂,
    // 上午 10:00 看到的刻度和下午 14:00 完全不是一套 —— 看着像图在变形。
    var seen = prices.concat(avgs).filter(function (v) { return v !== null && v !== undefined; });
    var maxDev = 0;
    seen.forEach(function (v) { maxDev = Math.max(maxDev, Math.abs(v - base)); });
    var half = PCT_STEPS[PCT_STEPS.length - 1];
    for (var s = 0; s < PCT_STEPS.length; s++) {
      if (base > 0 && maxDev / base <= PCT_STEPS[s]) { half = PCT_STEPS[s]; break; }
    }
    var yMin = base * (1 - half);
    var yMax = base * (1 + half);
    var yStep = base * half / 2;       // 4 等分 → 昨收正好落在正中那一格

    // 宽度必须交给 ECharts 自己从容器量。写成 width: '100%' 会被 zrender 当字符串
    // parseFloat, 直接得到 100 —— 画布被钉死成 100px 宽, 容器再宽也没用。
    // 留着 height 是因为容器高度是内联写死的 420px, 不冲突。
    state.chart = echarts.init(box, null, { height: 420 });
    // 不再画那条黄色的「现在」竖线: 横轴固定成整日后, 曲线走到哪就是当前时刻,
    // 再加一条竖线纯属多餘。上帝视角那条保留 —— 那里要分的是"已知/未知", 不是时刻。
    // 成交记录 → 散点坐标。只标当前这只票、且是回放当天成交的那几笔,
    // 跟券商分时图的 BS 点一个口径。买入红 B 向上, 卖出绿 S 向下。
    var buys = [], sells = [];
    var tradeAt = {};
    (state.orders || []).forEach(function (o) {
      if (o.ts_code !== state.active || o.trade_date !== q.trade_date) return;
      var i = pos[o.minute];
      if (i === undefined) return;
      tradeAt[i] = { side: o.side, shares: o.shares, price: o.price };
      (o.side === 'buy' ? buys : sells).push([i, o.price]);
    });

    state.chart.setOption({
      animation: false,
      grid: [
        { left: 56, right: 56, top: 18, height: '58%' },
        { left: 56, right: 56, top: '72%', height: '19%' }
      ],
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'cross' },
        backgroundColor: 'rgba(17,24,39,.96)', borderColor: '#1e2a3a',
        textStyle: { color: '#e2e8f0', fontSize: 11 },
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var lines = [ps[0].axisValue];
          ps.forEach(function (p) {
            if (p.seriesName === '买入点' || p.seriesName === '卖出点') return;
            if (p.value === null || p.value === undefined || p.value === '-') return;
            lines.push(p.marker + p.seriesName + ' ' + p.value);
          });
          var t = tradeAt[ps[0].dataIndex];
          if (t) {
            lines.push('<span style="color:' + (t.side === 'buy' ? '#ff5252' : '#00e676')
              + '">✦ ' + (t.side === 'buy' ? '买入' : '卖出') + ' ' + t.shares
              + ' 股 @ ' + t.price + '</span>');
          }
          return lines.join('<br/>');
        }
      },
      xAxis: [
        { type: 'category', data: labels, gridIndex: 0, axisLabel: { show: false },
          axisLine: { lineStyle: { color: '#1e2a3a' } }, axisTick: { show: false } },
        { type: 'category', data: labels, gridIndex: 1,
          // 刻度按交易时段打点: 09:30 / 10:30 / 11:30-13:00 / 14:00 / 15:00。
          // 之前是每 N 格等距取一个, 出来的是 10:31 / 13:03 这种对不上任何时段的
          // 时刻 —— 上午 09:30-11:30、下午 13:00-15:00 两段完全看不出来。
          // 午休在轴上是压掉的(11:29 紧挨 13:00), 所以交界处标成一个「11:30/13:00」。
          axisLabel: { color: '#4a5568', fontSize: 9,
            interval: function (i) { return Object.prototype.hasOwnProperty.call(tickIdx, i); },
            formatter: function (v, i) { return tickIdx[i] || ''; } },
          axisLine: { lineStyle: { color: '#1e2a3a' } }, axisTick: { show: false } }
      ],
      yAxis: [
        { scale: true, min: yMin, max: yMax, interval: yStep, gridIndex: 0, position: 'left',
          axisLabel: { color: '#4a5568', fontSize: 10,
            formatter: function (v) { return v.toFixed(2); } },
          splitLine: { lineStyle: { color: 'rgba(30,42,58,.6)' } } },
        // 右轴换成涨跌幅: 纵轴的零值就是昨收, 百分比读数直接是今天的涨跌幅,
        // 这也是同花顺/通达信的做法。
        { scale: true, min: yMin, max: yMax, interval: yStep, gridIndex: 0, position: 'right',
          axisLabel: { color: '#4a5568', fontSize: 10,
            formatter: function (v) { return ((v / base - 1) * 100).toFixed(2) + '%'; } },
          splitLine: { show: false } },
        // 成交量网格(1)自己的 y 轴。之前这里没配, 而成交量柱又指着 yAxisIndex: 0
        // (价格轴), ECharts 解析跨网格的 series 时直接抛
        // "Cannot read properties of undefined (reading 'type')" —— 整个 drawChart()
        // 死在这一行, 于是坐标轴/均线/成交量/「现在」标记全都没了。
        { gridIndex: 1, axisLabel: { show: false }, axisLine: { show: false },
          axisTick: { show: false }, splitLine: { show: false } }
      ],
      series: [
        { name: '价格', type: 'line', data: prices, showSymbol: false, lineStyle: { width: 1.2 },
          areaStyle: { color: 'rgba(68,138,255,.10)' }, z: 3 },
        { name: '均价', type: 'line', data: avgs, showSymbol: false,
          lineStyle: { width: 1, color: '#e8a45a' }, z: 2 },
        { name: '昨收', type: 'line', data: labels.map(function () { return base; }),
          showSymbol: false, lineStyle: { width: 1, type: 'dashed', color: '#4a5568' },
          markLine: { silent: true, symbol: 'none', data: [{ yAxis: base }] }, z: 1 },
        { name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 2, barWidth: '60%' },
        // B/S 买卖点。z 抬到价格线之上, symbolOffset 把 B 压到线下、S 顶到线上,
        // 免得三角盖住分时线本身。
        { name: '买入点', type: 'scatter', data: buys, xAxisIndex: 0, yAxisIndex: 0,
          symbol: 'triangle', symbolSize: 13, symbolOffset: [0, 14], z: 12,
          itemStyle: { color: '#ff5252', borderColor: '#fff', borderWidth: 1 },
          label: { show: true, formatter: 'B', color: '#fff', fontSize: 9, fontWeight: 'bold' } },
        { name: '卖出点', type: 'scatter', data: sells, xAxisIndex: 0, yAxisIndex: 0,
          symbol: 'triangle', symbolRotate: 180, symbolSize: 13, symbolOffset: [0, -14], z: 12,
          itemStyle: { color: '#00e676', borderColor: '#fff', borderWidth: 1 },
          label: { show: true, formatter: 'S', color: '#fff', fontSize: 9, fontWeight: 'bold' } }
      ]
    });
  }

  function drawNav() {
    var box = $('simNavBox');
    if (state.navChart) { state.navChart.dispose(); state.navChart = null; }
    box.textContent = '';
    var points = state.nav || [];
    if (points.length < 2) {
      box.innerHTML = '<div style="height:150px;display:flex;align-items:center;'
        + 'justify-content:center;color:var(--text-muted);font-size:12px">'
        + '推进到第二个交易日就会画出资金曲线</div>';
      return;
    }
    var initial = state.account ? state.account.initial_cash : points[0].total_asset;
    var data = points.map(function (p) { return p.total_asset; });
    var up = data[data.length - 1] >= initial;
    state.navChart = echarts.init(box, null, { width: '100%', height: 150 });
    state.navChart.setOption({
      animation: false,
      grid: { left: 8, right: 46, top: 10, bottom: 22, containLabel: true },
      tooltip: {
        trigger: 'axis', backgroundColor: 'rgba(17,24,39,.96)', borderColor: '#1e2a3a',
        textStyle: { color: '#e2e8f0', fontSize: 11 },
        formatter: function (params) {
          var p = params[0];
          var idx = p.dataIndex;
          var point = points[idx];
          return point.trade_date.slice(0, 4) + '-' + point.trade_date.slice(4, 6) + '-'
            + point.trade_date.slice(6, 8) + '<br>总资产 <b>' + fmtMoney(point.total_asset)
            + '</b><br>浮盈 ' + (point.profit >= 0 ? '+' : '') + fmtMoney(point.profit);
        }
      },
      xAxis: { type: 'category', data: points.map(function (p) {
          return p.trade_date.slice(4, 6) + '-' + p.trade_date.slice(6, 8); }),
        axisLabel: { color: '#4a5568', fontSize: 9 },
        axisLine: { lineStyle: { color: '#1e2a3a' } }, axisTick: { show: false } },
      yAxis: { type: 'value', scale: true,
        axisLabel: { color: '#4a5568', fontSize: 9, formatter: function (v) { return (v / 10000).toFixed(1) + '万'; } },
        splitLine: { lineStyle: { color: 'rgba(30,42,58,.6)' } } },
      series: [{
        type: 'line', data: data, showSymbol: true, symbolSize: 4,
        lineStyle: { width: 1.6, color: up ? '#ff5252' : '#00e676' },
        itemStyle: { color: up ? '#ff5252' : '#00e676' },
        areaStyle: { color: up ? 'rgba(255,82,82,.12)' : 'rgba(0,230,118,.12)' },
        markLine: { silent: true, symbol: 'none',
          data: [{ yAxis: initial }], lineStyle: { color: '#4a5568', type: 'dashed', width: 1 } }
      }]
    });
  }

  // ── 交易面板 ───────────────────────────────────────────

  function syncTradePanel() {
    var q = state.quote;
    var priceInput = $('simPrice');
    var side = state.side;
    if (q && q.last_price && (!priceInput.value || priceInput.dataset.auto === '1')) {
      priceInput.value = q.last_price;
      priceInput.dataset.auto = '1';   // 用户没手动改过就跟着现价走
    }
    var held = state.positions.filter(function (p) { return p.ts_code === state.active; })[0];
    var maxQty = side === 'sell'
      ? (held ? held.sellable : 0)
      : (q && q.last_price ? Math.floor(state.account.cash / q.last_price / 100) * 100 : 0);
    $('simTradeHint').textContent = side === 'sell'
      ? ('可卖 ' + (held ? held.sellable : 0) + ' 股'
        + (held && held.sellable < held.shares ? '（T+1，当天买入的明天才能卖）' : ''))
      : ('可买约 ' + maxQty + ' 股');
    var lotBox = $('simLotRow');
    var fractions = [['1/4', .25], ['1/3', .333], ['1/2', .5], ['全仓', 1]];
    lotBox.innerHTML = fractions.map(function (f) {
      return '<button class="sim-lot-btn" data-frac="' + f[1] + '">' + f[0] + '</button>';
    }).join('');
    Array.prototype.forEach.call(lotBox.querySelectorAll('.sim-lot-btn'), function (btn) {
      btn.addEventListener('click', function () {
        var frac = parseFloat(btn.dataset.frac);
        var qty = side === 'sell'
          ? (held ? Math.floor(held.sellable * frac) : 0)
          : Math.floor(maxQty * frac / 100) * 100;
        $('simShares').value = qty;
        updateTradeAmount();
      });
    });
    updateTradeAmount();
  }

  function updateTradeAmount() {
    var price = parseFloat($('simPrice').value);
    var shares = parseFloat($('simShares').value);
    var box = $('simTradeAmount');
    if (!(price > 0) || !(shares > 0)) { box.textContent = ''; return; }
    var amount = price * shares;
    var q = state.quote;
    var warn = '';
    if (q && q.last_price && Math.abs(price - q.last_price) / q.last_price > 0.2) {
      warn = '  ⚠ 偏离现价 ' + q.last_price + ' 超过 20%';
    }
    box.innerHTML = '金额 ' + fmtMoney(amount)
      + (state.side === 'buy' ? '（另需手续费）' : '（另扣手续费）') + warn;
  }

  function submitOrder(confirmIt) {
    var price = parseFloat($('simPrice').value);
    var shares = parseFloat($('simShares').value);
    if (!(price > 0)) { toast('请填价格'); return; }
    if (!(shares > 0)) { toast('请填数量'); return; }
    if (state.godMode) { toast('上帝视角下不可下单，仅供复盘'); return; }
    var body = {
      account_id: state.accountId,
      ts_code: state.active,
      side: state.side,
      price: price,
      shares: shares,
      confirm: !!confirmIt
    };
    // 带上你看到的时刻。后端若发现时钟已被别的标签页推走, 会回 409 而不是
    // 静默把这笔记成新时刻的成交 —— 那会让你以为操作的是画面里的那一根。
    if (state.session) {
      body.expected_date = state.session.cur_date;
      body.expected_min = state.session.cur_min;
      body.expected_rev = state.session.rev;
    }
    api('/api/sim/order', { method: 'POST', body: JSON.stringify(body) })
      .then(function (data) {
        state.account = data.account;
        state.positions = data.positions || [];
        $('simShares').value = '';
        renderSummary();
        renderPositions();
        renderOrders();
        syncTradePanel();
        toast('已按 ' + data.order.price + ' × ' + data.order.shares
          + ' ' + (state.side === 'buy' ? '买入' : '卖出') + ' 成交', true);
      })
      .catch(function (err) {
        if (err.status === 409) {
          if (err.data && err.data.code === 'price_sanity') {
            if (root.confirm && root.confirm(err.message)) submitOrder(true);
            return;
          }
          // 时钟错位: 刷新拿回真实状态, 别让用户对着过期画面继续下单
          toast(err.message);
          refresh();
          return;
        }
        toast(err.message);
      });
  }

  // 上帝视角只切读权限、不动时钟, 而且刻意不落库: 刷新后恒为关,
  // 免得"开着忘了关, 然后按着未来下单"变成一种习惯。
  function setGodMode(on) {
    state.godMode = !!on;
    $('simGodBanner').style.display = state.godMode ? '' : 'none';
    $('simSubmit').disabled = state.godMode;
    $('simPrice').disabled = state.godMode;
    $('simShares').disabled = state.godMode;
    var box = $('simChartBox');
    if (state.chart) { state.chart.dispose(); state.chart = null; }
    box.textContent = '';
    var q = state.quote;
    if (!q || !q.rows.length) { state.quote = null; drawChart(); return; }
    loadQuote(true)
      .then(function () { drawChartWithFuture(); })
      .catch(function (e) { toast(e.message); });
  }

  // 未来段虚线 + now 竖线: 让"哪一段是我还不知道的"一眼可见
  function drawChartWithFuture() {
    drawChart();
    if (!state.chart || !state.quote || !state.session) return;
    // 必须按 grid(整根时间轴) 对齐, 不能用 rows 的下标 —— 停牌票 rows 会缺格,
    // 那样虚线会整体错位。
    var grid = (state.quote.grid && state.quote.grid.length) ? state.quote.grid : null;
    if (!grid) return;
    var pos = {}, priceByMin = {};
    grid.forEach(function (m, i) { pos[m] = i; });
    state.quote.rows.forEach(function (r) { priceByMin[r.m] = r.p; });
    var nowIdx = pos[state.session.cur_min];
    if (nowIdx === undefined) return;
    var data = new Array(grid.length).fill(null);
    var hasFuture = false;
    for (var i = nowIdx + 1; i < grid.length; i++) {
      if (priceByMin[grid[i]] !== undefined) { data[i] = priceByMin[grid[i]]; hasFuture = true; }
    }
    if (!hasFuture) return;
    data[nowIdx] = priceByMin[grid[nowIdx]];   // 虚线从"现在"这一格接上
    state.chart.setOption({
      series: [{
        name: '未来(不可下单)', type: 'line', data: data,
        showSymbol: false, lineStyle: { width: 1, type: 'dotted', color: '#4a5568' },
        markLine: { silent: true, symbol: 'none', data: [{ xAxis: grid[nowIdx].slice(0, 5) }],
          lineStyle: { color: '#ffd740', width: 1, type: 'dashed' },
          label: { show: true, formatter: '现在', color: '#ffd740', fontSize: 10 } }
      }]
    });
  }

  // ── 候选池 ─────────────────────────────────────────────

  function addWatch(code, name) {
    if (!code) return;
    if (!state.watch.some(function (w) { return w.ts_code === code; })) {
      state.watch.push({ ts_code: code, name: name || code });
      saveWatch();
    }
    renderChips();
    // 还没建账户时只入池: 此时 select 会拿 account_id=null 去拉分时, 必然报错
    if (!state.accountId) return;
    select(code, name);
  }

  function removeWatch(code) {
    state.watch = state.watch.filter(function (w) { return w.ts_code !== code; });
    saveWatch();
    if (state.active === code) { state.active = null; state.quote = null; drawChart(); renderReadout(); }
    renderChips();
  }

  // 把某一天「条件优选」筛出来的票灌进候选池。日期一换就重灌, 于是池子里
  // 永远是那一天选出来的东西, 点一下 chip 就能直接下单, 不用先去搜代码。
  // 已经在池子里的跳过, 所以用户自己加的票不会被冲掉。
  async function loadPicksForDate(date) {
    if (!date) return;
    var data;
    try {
      data = await api('/api/conditional-picks/preferred?date=' + encodeURIComponent(date));
    } catch (e) {
      return;                       // 那天没跑过筛选就别打扰用户
    }
    var results = data.results || [];
    var added = [];
    results.forEach(function (r) {
      if (!r.ts_code) return;
      if (state.watch.some(function (w) { return w.ts_code === r.ts_code; })) return;
      state.watch.push({ ts_code: r.ts_code, name: r.name || r.code || r.ts_code });
      added.push(r.name || r.code || r.ts_code);
    });
    if (!added.length) return;
    saveWatch();
    renderChips();
    toast(fmtDate(date) + ' 条件优选 ' + added.length + ' 只已入池：' + added.join('、'));
  }

  function fmtDate(d) {
    return d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8);
  }

  function select(code, name) {
    state.active = code;
    if (name) {
      state.watch.forEach(function (w) { if (w.ts_code === code) w.name = name; });
      saveWatch();
    }
    renderChips();
    loadQuote(state.godMode).catch(function (e) { toast(e.message); });
  }

  async function doSearch() {
    var keyword = $('simSearch').value.trim();
    if (!keyword) return;
    try {
      var data = await api('/api/sim/candidates?account_id=' + state.accountId
        + '&q=' + encodeURIComponent(keyword) + '&limit=20');
      if (!data.results.length) { toast('没找到「' + keyword + '」'); return; }
      var first = data.results[0];
      addWatch(first.ts_code, first.name);
      $('simSearch').value = '';
      if (data.results.length > 1) {
        // 多于一个候选时把列表给出来, 让用户自己挑 —— 猜错代码比多一步更糟
        state.watch = state.watch.concat(data.results.slice(1, 9).map(function (r) {
          return { ts_code: r.ts_code, name: r.name };
        }));
        saveWatch();
        renderChips();
        toast('已加入 ' + Math.min(data.results.length, 9) + ' 只候选');
      }
    } catch (e) { toast(e.message); }
  }

  // ── 自动播放 ───────────────────────────────────────────
  // 用 setTimeout 链而不是 setInterval: 高速下 interval 会堆积在途请求。
  // 1x = 1 个模拟分钟 / 真实秒, 所以一个 240 分钟的交易日走 4 分钟。
  function tickLoop() {
    if (!state.playing) return;
    var elapsed = state._lastTick ? (Date.now() - state._lastTick) / 1000 : 1;
    state._lastTick = Date.now();
    var steps = Math.max(1, Math.min(Math.round(state.speed * elapsed), 60));
    step('tick', steps)
      .catch(function (e) { stop(); toast(e.message); })
      .then(function () {
        if (state.playing) state.timer = setTimeout(tickLoop, Math.max(200, 1000 / state.speed));
      });
  }

  function play() {
    if (!state.accountId || state.playing) return;
    state.playing = true;
    state._lastTick = Date.now();
    renderClock();
    tickLoop();
  }

  function pause() {
    state.playing = false;
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    renderClock();
  }

  function stop() { pause(); }

  // ── 生命周期 ───────────────────────────────────────────

  async function bootstrap() {
    $('simSetup').style.display = '';
    ['simClockGroup', 'simCtlGroup', 'simWatchGroup'].forEach(function (id) {
      $(id).style.display = 'none';
    });
    $('simSummary').innerHTML = '';
    $('simReadout').innerHTML = '';
    $('simChartMsg').style.display = '';
    $('simChartMsg').textContent = '先建一个模拟账户';
    $('simPosTable').innerHTML = '';
    $('simOrdersTable').innerHTML = '';
    try {
      var data = await api('/api/sim/replay/calendar');
      state.calendar = data.dates || [];
      $('simStartDate').innerHTML = state.calendar.map(function (d) {
        return '<option value="' + d + '">' + d.slice(0, 4) + '-' + d.slice(4, 6)
          + '-' + d.slice(6, 8) + '</option>';
      }).join('');
      if (state.calendar.length) $('simStartDate').value = state.calendar[state.calendar.length - 1];
    } catch (e) {
      $('simChartMsg').textContent = '读取可回放日期失败：' + e.message;
    }
    renderChips();
  }

  async function createAccount() {
    var cash = parseFloat($('simInitCash').value);
    var date = $('simStartDate').value;
    if (!(cash > 0)) { toast('初始资金必须大于 0'); return; }
    if (!date) { toast('没有可回放的日期'); return; }
    try {
      var account = await api('/api/sim/account', {
        method: 'POST', body: JSON.stringify({ initial_cash: cash })
      });
      state.accountId = account.account.id;
      // 记住账户: 刷新后还要能接着用同一份持仓/时钟, 而不是从头建一遍
      try { localStorage.setItem(ACCT_KEY, String(state.accountId)); } catch (e) { /* 隐私模式忽略 */ }
      await api('/api/sim/replay/init', {
        method: 'POST', body: JSON.stringify({ account_id: state.accountId, start_date: date })
      });
      $('simSetup').style.display = 'none';
      ['simClockGroup', 'simCtlGroup', 'simWatchGroup'].forEach(function (id) {
        $(id).style.display = '';
      });
      await refresh();
      toast('模拟账户已就绪，从 09:25 集合竞价开始', true);
      // 起始日那天的条件优选顺手灌进候选池
      loadPicksForDate(state.viewDate || (state.session && state.session.cur_date));
    } catch (e) { toast(e.message); }
  }

  // 回到建账户界面。恢复旧账户时 (restore) 会把 simSetup 永久藏掉, 而界面上
  // 没有回去的路 —— 于是想换个起始日就只能刷新, 刷新又被同一个账户接回去,
  // 起始日永远选不了。这里把路铺上: 只断开会话, 不动库里已存的账户和成交。
  async function resetToSetup() {
    pause();
    state.accountId = null;
    state.session = null;
    state.account = null;
    state.positions = [];
    state.orders = [];
    state.active = null;
    state.quote = null;
    state.viewDate = null;
    state.pinnedDate = false;
    state.godMode = false;
    state.seq++;                      // 让在途的 refresh/step 响应全部作废
    if (state.chart) { state.chart.dispose(); state.chart = null; }
    try { localStorage.removeItem(ACCT_KEY); } catch (e) { /* 隐私模式忽略 */ }
    if ($('simGodMode')) $('simGodMode').checked = false;
    await bootstrap();
    $('simCreateBtn').focus();
    toast('可以重新选起始日和初始资金了');
  }

  function bind() {
    $('simCreateBtn').addEventListener('click', createAccount);
    $('simNewAccount').addEventListener('click', function () {
      resetToSetup().catch(function (e) { toast(e.message); });
    });
    $('simChartDate').addEventListener('change', function () {
      state.viewDate = this.value;
      state.pinnedDate = true;
      loadQuote(state.godMode)
        .then(function () { return loadPicksForDate(state.viewDate); })
        .catch(function (e) {
          // 服务端会拒掉"回放时钟之后"的日期, 这里把提示还原成跟随时钟
          state.pinnedDate = false;
          toast(e.message);
          renderDatePicker();
        });
    });
    $('simNextMin').addEventListener('click', function () { step('next_minute').catch(function (e) { toast(e.message); }); });
    $('simSkipClose').addEventListener('click', function () { step('skip_close').catch(function (e) { toast(e.message); }); });
    $('simNextDay').addEventListener('click', function () { step('next_day').catch(function (e) { toast(e.message); }); });
    $('simPlay').addEventListener('click', function () { state.playing ? pause() : play(); });
    $('simSpeed').addEventListener('change', function () {
      state.speed = parseInt(this.value, 10) || 5;
      if (state.playing) { state._lastTick = Date.now(); }
    });
    $('simSearch').addEventListener('keydown', function (e) { if (e.key === 'Enter') doSearch(); });
    Array.prototype.forEach.call(document.querySelectorAll('.sim-trade-tab'), function (btn) {
      btn.addEventListener('click', function () {
        state.side = btn.dataset.side;
        Array.prototype.forEach.call(document.querySelectorAll('.sim-trade-tab'), function (b) {
          b.classList.toggle('on', b === btn);
        });
        var submit = $('simSubmit');
        submit.textContent = state.side === 'buy' ? '确认买入' : '确认卖出';
        submit.classList.toggle('sell', state.side === 'sell');
        syncTradePanel();
      });
    });
    $('simPrice').addEventListener('input', function () {
      this.dataset.auto = '0';   // 用户接手了, 别再自动覆盖
      updateTradeAmount();
    });
    $('simShares').addEventListener('input', updateTradeAmount);
    $('simSubmit').addEventListener('click', function () { submitOrder(false); });
    $('simGodMode').addEventListener('change', function () { setGodMode(this.checked); });
    // F1 买入 / F2 卖出, 与券商桌面端一致
    document.addEventListener('keydown', function (e) {
      var panel = $('panel-sim');
      if (!panel || !panel.classList.contains('active')) return;
      if (e.target && /input|select|textarea/i.test(e.target.tagName)) return;
      if (e.key === 'F1') { e.preventDefault(); $('simPrice').focus(); }
      if (e.key === 'F2') { e.preventDefault(); $('simSubmit').click(); }
    });
    window.addEventListener('resize', function () {
      if (state.chart) state.chart.resize();
    });
  }

  async function restore() {
    var saved = null;
    try { saved = localStorage.getItem(ACCT_KEY); } catch (e) { saved = null; }
    if (!saved) { bootstrap(); return; }
    state.accountId = parseInt(saved, 10);
    try {
      // 账户还在就直接接着用; state 失败(404/库读不了)才回到建账户界面
      await refresh();
      $('simSetup').style.display = 'none';
      ['simClockGroup', 'simCtlGroup', 'simWatchGroup'].forEach(function (id) {
        $(id).style.display = '';
      });
      loadPicksForDate(state.viewDate || (state.session && state.session.cur_date));
    } catch (e) {
      state.accountId = null;
      try { localStorage.removeItem(ACCT_KEY); } catch (err) { /* 忽略 */ }
      bootstrap();
    }
  }

  root.SimTrading = {
    load: function () {
      // display:none 时容器 clientWidth 为 0, 直接 init 会画出 0 宽的图,
      // 所以进 tab 必须 resize 一次(切 tab 不会自动销毁/重建图表)
      if (state.chart) state.chart.resize();
      if (state.accountId) {
        refresh().catch(function (e) { toast(e.message); });
        return;
      }
      restore();
    },
    pause: pause,
    stop: stop,
    resume: function () { if (state.playing) play(); },
    addToWatch: addWatch,   // 供各选股 tab 的"加入模拟盘"按钮调用
    isActive: function () { return !!state.accountId; }
  };

  loadWatch();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
