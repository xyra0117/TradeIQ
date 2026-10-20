'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const icon = (name, small = true) => `<svg class="icon${small ? ' small' : ''}" aria-hidden="true"><use href="icons.svg#i-${name}"/></svg>`;
  const escape = value => String(value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const readPreference = (key, fallback) => { try { return localStorage.getItem(`tradeiq_redesign_${key}`) || fallback; } catch { return fallback; } };
  const savePreference = (key, value) => { try { localStorage.setItem(`tradeiq_redesign_${key}`, value); } catch { /* The design preview also works without storage. */ } };
  const dates = ['20260921','20260922','20260923','20260924','20260925','20260928','20260929','20260930'];
  const names = ['远景科技','星河材料','云峰电子','新川智造','华辰能源','海岳通信','北辰装备','天行软件','瑞川科技','华域新材','恒盛精工','启航电气','清源医疗','盛景光电'];
  const sectors = ['计算机','新材料','电子','机械设备','电力设备','通信','机械设备','计算机','电子','新材料','机械设备','电力设备','医药生物','电子'];
  const prices = [32.86,21.64,52.70,17.43,12.85,41.28,33.65,28.14,24.75,36.92,18.26,27.54,43.16,15.84];
  const sampleStocks = names.map((name, index) => ({
    code: `EX.${1001 + index}`, name, sector: sectors[index], price: prices[index],
    change: -(0.46 + (index * 0.37) % 3.6), ratio: .710 + index * .006,
    strength: 14.60 - index * .55, small: -(.08 + index * .014),
    ma60: prices[index] * (index < 7 ? 1.065 : .975), hits: index < 7 ? 2 : 1,
    amount20: 7.40 - index * .30,
    outcomes: Array.from({length: 5}, (_, period) => {
      const close = (period + 1) * (.72 + index % 4 * .21) - index % 5 * .84;
      return {close, high: close + 1.32 + period * .18 + index % 3 * .23};
    })
  }));
  const indices = [
    {name: '上证指数', value: '3,846.12', change: -.68, points: '0,12 8,8 16,14 24,9 32,12 40,15 48,12 56,18 64,14 72,17'},
    {name: '深证成指', value: '12,764.38', change: -.94, points: '0,7 8,10 16,6 24,13 32,11 40,17 48,13 56,16 64,15 72,19'},
    {name: '创业板指', value: '2,916.45', change: -1.21, points: '0,6 8,7 16,11 24,8 32,16 40,14 48,18 56,14 64,19 72,18'},
    {name: '科创50', value: '1,348.76', change: .37, points: '0,18 8,14 16,16 24,11 32,13 40,8 48,11 56,6 64,8 72,5'}
  ];
  const columns = [
    {key:'hits', label:'辅助命中'}, {key:'price', label:'收盘价'}, {key:'change', label:'当日涨幅'},
    {key:'ratio', label:'成交量比'}, {key:'strength', label:'5日主力强度'}, {key:'small', label:'当日小单净额'},
    {key:'ma60', label:'MA60'}, {key:'outcomes', label:'T+1～T+5'}
  ];
  let hiddenColumns = [];
  try { const stored = JSON.parse(readPreference('hidden_columns','[]')); if (Array.isArray(stored)) hiddenColumns = stored.filter(key => columns.some(column => column.key === key)); } catch { /* Use the complete table if a saved preview setting is invalid. */ }
  const query = new URLSearchParams(location.search);
  const initialDate = dates.includes(query.get('date')) ? query.get('date') : dates[1];
  const state = {page:'picks', source:'em', strategy:'preferred', date:initialDate, search:'', threshold:3, volume:true, ratio:80, sort:'default', direction:-1, preview:'normal', selected:null, expanded:false, chart:'daily', chartRange:100, fund:'main', hiddenColumns:new Set(hiddenColumns), refreshRevision:0,dateMode:'date',marketViewDate:initialDate,simple:query.get('simple')==='1'};
  const pageLabels = {index:'核心指数',leaderboard:'阶段涨幅榜',limitup:'涨停拆解',collection:'涨停合集',flow:'主力净流入',picks:'选股工作台',sectors:'热门板块',positions:'持仓'};
  const strategyLabels = {system:'系统推荐',conditional:'条件选股',new:'条件选股新',preferred:'条件优选',dip:'低吸选股',chase:'追高选股',custom:'自定义选股',surge:'冲高观察',combined:'组合选股'};
  const previewMode = () => state.page === 'picks' && state.source === 'em' && state.strategy === 'preferred';
  const percentage = number => `${number >= 0 ? '+' : ''}${number.toFixed(2)}%`;
  const tone = number => number >= 0 ? 'up' : 'down';
  const shortDate = date => `${date.slice(4,6)}.${date.slice(6,8)}`;
  if (Object.hasOwn(pageLabels,query.get('page'))) state.page = query.get('page');
  if (['em','ths','user'].includes(query.get('source'))) state.source = query.get('source');
  if(query.get('strategy')==='dip2')query.set('strategy','chase');
  if (Object.hasOwn(strategyLabels,query.get('strategy'))) state.strategy = query.get('strategy');
  if (state.source === 'ths' && !['system','conditional','combined'].includes(state.strategy)) state.strategy = 'system';
  if (state.source === 'user') state.strategy = 'user';
  const pageDates = {}, pageModes = {};
  const sourceStrategies = {em:state.source === 'em' ? state.strategy : 'preferred',ths:state.source === 'ths' ? state.strategy : 'system',user:'user'};
  const pages = window.TradeIQPages.create({state,stocks:sampleStocks,indices,dates,icon,escape,pct:percentage,tone,shortDate,toast,openDetail,candleChart,rerender:renderNavigation,preferredRows:visibleStocks,refreshPreferred:renderTable,setDate});
  let toastTimer;
  function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 2300); }
  function closePopovers() {
    ['statePopover','datePopover','columnsPopover'].forEach(id => { $(id).hidden = true; });
    ['previewStateBtn','dateBtn','columnsBtn'].forEach(id => { $(id).setAttribute('aria-expanded','false'); });
  }
  function togglePopover(buttonId, panelId) {
    const opening = $(panelId).hidden;
    closePopovers();
    $(panelId).hidden = !opening;
    $(buttonId).setAttribute('aria-expanded', String(opening));
  }
  function setTheme(theme) {
    const chosen = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.theme = chosen;
    all('.theme-button').forEach(button => { const active = button.dataset.theme === chosen; button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active)); });
    savePreference('theme',chosen);
  }
  function setSidebar(collapsed) {
    $('appShell').classList.toggle('sidebar-collapsed',collapsed);
    $('collapseSidebar').setAttribute('aria-expanded',String(!collapsed));
    $('collapseSidebar').setAttribute('aria-label',collapsed ? '展开侧栏' : '收起侧栏');
    $('collapseSidebar').querySelector('span').textContent = collapsed ? '展开侧栏' : '收起侧栏';
    savePreference('sidebar',collapsed ? 'collapsed' : 'expanded');
  }
  function dateModes() {
    if(state.page==='sectors')return [['closed','最近收盘日'],['date','指定日期']];
    if(['collection','positions'].includes(state.page)) return [['live','今日实时'],['date','指定日期']];
    if(state.page==='picks'&&['dip','chase','custom','surge'].includes(state.strategy))return [['closed','最近收盘日'],['date','指定日期']];
    return [['all','全部日期'],['date','指定日期']];
  }
  function renderCalendar() {
    const weekdays=['一','二','三','四','五','六','日'].map(day=>`<span class="calendar-day-name">${day}</span>`).join('');
    const days=Array.from({length:30},(_,i)=>{const date=`202609${String(i+1).padStart(2,'0')}`;return `<button data-date="${date}" ${dates.includes(date)?'':'disabled'} class="${state.date===date?'active':''}" aria-label="2026年9月${i+1}日">${i+1}</button>`;}).join('');
    $('datePopover').innerHTML=`<div class="date-mode-options">${dateModes().map(([key,label])=>`<button data-date-mode="${key}" aria-pressed="${state.dateMode===key}">${label}</button>`).join('')}</div><div class="calendar-title">2026 年 9 月<span>示例交易日</span></div><div class="calendar-grid">${weekdays}<span class="calendar-blank"></span>${days}</div><div class="calendar-footnote">仅演示各页面日期语义；实时模式没有真实行情。</div>`;
  }
  function renderDate() {
    if(!dateModes().some(([key])=>key===state.dateMode))state.dateMode='date';
    const labels={all:'全部日期',closed:'最近收盘',live:'今日实时'};
    $('dateLabel').textContent=state.dateMode==='date'?`${state.date.slice(0,4)}.${shortDate(state.date)}`:`${labels[state.dateMode]} · 示例`;
    const viewDate=state.marketViewDate||state.date,offset=dates.indexOf(viewDate)-dates.indexOf(state.date);
    $('marketDate').textContent=`${shortDate(viewDate)} / ${offset?'T+'+offset:'入选日'}`;
    $('marketViewPrev').disabled=offset<=0; $('marketViewNext').disabled=offset>=5||dates.indexOf(viewDate)===dates.length-1;
    $('prevDate').disabled=dates.indexOf(state.date)===0; $('nextDate').disabled=dates.indexOf(state.date)===dates.length-1;
    renderCalendar();
  }
  function setDate(date,mode='date') {
    state.refreshRevision++;state.date=date;state.dateMode=mode;state.marketViewDate=date;
    pageDates[state.page]=date;pageModes[state.page]=mode;state.preview='normal';
    closePopovers();renderDate();renderMarket();renderTable();if(state.selected)renderDetail();
  }
  function shiftMarket(delta) {
    const next=dates[dates.indexOf(state.marketViewDate||state.date)+delta];
    if(!next)return;state.marketViewDate=next;renderDate();renderMarket();
  }
  function renderMarket() {
    $('marketIndices').innerHTML = indices.map(index => `<div class="index-stat"><div class="index-stat-top"><span>${index.name}</span><svg viewBox="0 0 72 24" aria-hidden="true"><polyline points="${index.points}" stroke="var(--chart-secondary)" fill="none" stroke-width="1.2"/></svg></div><div class="index-stat-value"><b>${index.value}</b><span class="${tone(index.change)}">${percentage(index.change)}</span></div></div>`).join('');
    $('marketCharts').innerHTML = indices.map((index, position) => {
      const points = Array.from({length:45},(_,i) => `${10+i*6.0},${57+Math.sin(i*.68+position+dates.indexOf(state.marketViewDate)*.3)*8+i*(position === 3 ? -.7 : .65)}`).join(' ');
      return `<div class="market-chart"><div class="market-chart-title">${index.name}<span>分时 · 示例</span></div><svg viewBox="0 0 300 120" role="img" aria-label="${index.name}示例分时图"><path d="M10 25h270M10 55h270M10 85h270" stroke="var(--chart-grid)" fill="none"/><path d="M10 55h270" stroke="var(--chart-secondary)" stroke-dasharray="3 3" fill="none"/><polyline points="${points}" stroke="var(--chart-line)" stroke-width="1.5" fill="none"/><g fill="var(--muted)" font-size="8" font-family="Menlo,monospace"><text x="10" y="114">09:30</text><text x="130" y="114">11:30</text><text x="252" y="114">15:00</text></g></svg></div>`;
    }).join('');
  }
  function visibleStocks() {
    if (!previewMode()) return pages.stockList();
    const filtered = sampleStocks.filter(stock => (!state.search || `${stock.name} ${stock.code} ${stock.sector}`.toLowerCase().includes(state.search.toLowerCase())) && stock.amount20 >= state.threshold && (!state.volume || stock.ratio * 100 <= state.ratio));
    return filtered.sort((a,b) => state.sort === 'default' ? b.hits-a.hits || b.strength-a.strength || a.code.localeCompare(b.code) : state.direction * (a[state.sort]-b[state.sort]) || a.code.localeCompare(b.code));
  }
  function sortHeading(key,label) {
    const selected = state.sort === key;
    return `<button data-sort="${key}" aria-label="按${label}排序">${label}<span class="sort-glyph${selected ? '' : ' sort-neutral'}">${icon(selected ? (state.direction === -1 ? 'sort-down' : 'sort-up') : 'sort')}</span></button>`;
  }
  function renderColumns() {
    $('columnsPopover').innerHTML = `<div class="popover-title">表格显示列</div><label><input type="checkbox" checked disabled>股票名称 / 代码（固定）</label>${columns.map(column => `<label><input type="checkbox" data-column="${column.key}" ${state.hiddenColumns.has(column.key) ? '' : 'checked'}>${column.label}</label>`).join('')}`;
  }
  function renderTable() {
    const scrollTop = $('tableScroll').scrollTop;
    const scrollLeft = $('tableScroll').scrollLeft;
    const show = key => !state.hiddenColumns.has(key);
    const nextDates = dates.slice(dates.indexOf(state.date)+1, dates.indexOf(state.date)+6);
    const sortable = ['price','change','ratio','strength','small','ma60'];
    let header = '<th class="rank">#</th><th class="stock-header">股票名称 / 代码</th>';
    for (const column of columns.filter(column => column.key !== 'outcomes' && show(column.key))) {
      header += `<th${state.sort === column.key ? ` aria-sort="${state.direction === -1 ? 'descending' : 'ascending'}"` : ''}>${sortable.includes(column.key) ? sortHeading(column.key,column.label) : column.label}</th>`;
    }
    if (show('outcomes')) header += Array.from({length:5},(_,i) => `<th class="outcome-head${i === 0 ? ' first' : ''}">T+${i+1}<span class="column-date">${nextDates[i] ? shortDate(nextDates[i]) : '待交易日'}</span></th>`).join('');
    $('stockTableHead').innerHTML = `<tr>${header}</tr>`;
    const rows = visibleStocks();
    const empty = state.preview === 'empty' || !rows.length;
    const hidden = ['loading','error'].includes(state.preview) || empty;
    $('stockTable').hidden = hidden;
    $('tableState').hidden = !hidden;
    $('missingNotice').hidden = state.preview !== 'missing';
    $('resultCount').textContent = state.preview === 'empty' ? '0' : state.preview === 'loading' || state.preview === 'error' ? '—' : String(rows.length);
    $('dataStatus').innerHTML = state.preview === 'loading' ? '正在加载示例…' : state.preview === 'error' ? '读取失败 · 示例状态' : state.preview === 'missing' ? `${icon('info')}部分记录缺失` : `${icon('check')}示例已加载`;
    if (hidden) {
      const states = {
        loading: {icon:'refresh', title:'正在读取示例数据', description:'设计状态演示，可从顶部菜单切回正常数据。'},
        error: {icon:'info', title:'暂时无法读取结果', description:'保留当前筛选条件，重试后恢复示例列表。'},
        empty: {icon:'search', title:'没有符合条件的示例', description:state.search || state.threshold !== 3 || state.ratio !== 80 ? '试试清除搜索或恢复默认筛选。' : '空结果演示，不代表当前市场的实际筛选结果。'}
      };
      const current = states[state.preview] || states.empty;
      $('tableState').innerHTML = `<div class="state-symbol${state.preview === 'loading' ? ' is-refreshing' : ''}">${icon(current.icon,false)}</div><strong>${current.title}</strong><p>${current.description}</p>${state.preview !== 'loading' ? `<button class="button" id="resetResults">${state.preview === 'error' ? '重试' : '恢复示例'}</button>` : ''}`;
      $('stockTableBody').innerHTML = '';
    } else {
      $('stockTableBody').innerHTML = rows.map((stock,index) => {
        let cells = `<td class="rank">${String(index+1).padStart(2,'0')}</td><td class="stock-cell"><button class="stock-button" data-stock="${stock.code}" aria-label="查看${stock.name}详情"><span class="stock-title"><strong>${escape(stock.name)}</strong>${window.TradeIQPages.sectorTag(stock.sector)}</span><span class="stock-code">${escape(stock.code)}</span></button>${pages.quickPick(stock.code,state.date)}</td>`;
        if (show('hits')) cells += `<td class="hit-cell"><span class="hit-pill">${icon('check')}${stock.hits} 项命中</span></td>`;
        if (show('price')) cells += `<td>${stock.price.toFixed(2)}</td>`;
        if (show('change')) cells += `<td class="down">${percentage(stock.change)}</td>`;
        if (show('ratio')) cells += `<td>${(stock.ratio*100).toFixed(1)}<span class="muted">%</span></td>`;
        if (show('strength')) cells += `<td class="strength-cell">${stock.strength.toFixed(2)}<span class="muted">%</span></td>`;
        if (show('small')) cells += `<td class="down">${stock.small.toFixed(2)}<span class="muted"> 亿</span></td>`;
        if (show('ma60')) cells += `<td>${stock.ma60.toFixed(2)}</td>`;
        if (show('outcomes')) cells += stock.outcomes.map((outcome,period) => {
          const missing = !nextDates[period] || state.preview === 'missing' && index % 3 === 0 && period >= 2;
          return `<td class="outcome-cell${period === 0 ? ' first' : ''}" title="${missing ? '缺少对应交易日的完整示例数据' : '示例最高累计 / 收盘累计；正式数据将保留原策略口径'}">${missing ? '<span class="missing-value">—</span>' : `<span class="outcome-high ${tone(outcome.high)}">${percentage(outcome.high)}</span><span class="outcome-close">${percentage(outcome.close)}</span>`}</td>`;
        }).join('');
        return `<tr data-code="${stock.code}" class="${state.selected === stock.code ? 'selected' : ''}">${cells}</tr>`;
      }).join('');
    }
    $('visibleCount').textContent = state.preview === 'loading' ? '读取中…' : state.preview === 'error' ? '结果尚未加载' : `${state.preview === 'empty' ? 0 : rows.length} / ${sampleStocks.length} 条示例记录 · 已完整加载`;
    const visibleCount = columns.filter(column => show(column.key)).reduce((sum,column) => sum + (column.key === 'outcomes' ? 5 : 1),2);
    $('stockTable').style.minWidth = `${Math.max(450,1370 - (14-visibleCount)*82)}px`;
    $('tableScroll').scrollTop = scrollTop;
    $('tableScroll').scrollLeft = scrollLeft;
    if (previewMode() && state.selected && !rows.some(stock => stock.code === state.selected) && !state.globalSelection) closeDetail(false);
    if (!previewMode()) pages.render();
  }
  function resetResults() {
    state.refreshRevision++; state.preview = 'normal'; state.search = ''; state.threshold = 3; state.ratio = 80; state.volume = true;
    $('stockSearch').value = ''; $('thresholdSelect').value = '3'; $('volumeSelect').value = '80'; $('volumeSelect').disabled = false; $('volumeToggle').setAttribute('aria-pressed','true'); renderTable();
  }
  function renderNavigation() {
    const preview = previewMode();
    const simpleTitle=state.simple&&state.page==='index'?(pages.indexView()==='barometer'?'盘面晴雨表':'市场总览'):null;
    $('pageTitle').textContent = simpleTitle||pageLabels[state.page];
    $('simpleNav').hidden=!state.simple; $('appShell').classList.toggle('simple-mode',state.simple);
    all('.nav-item').forEach(button=>button.hidden=state.simple&&!['index','picks'].includes(button.dataset.page));
    all('[data-simple]').forEach(button=>{const active=state.page==='picks'?button.dataset.simple==='picks':state.page==='index'&&button.dataset.simple===pages.indexView();button.setAttribute('aria-pressed',String(active));button.classList.toggle('active',active);});
    $('simpleModeBtn').setAttribute('aria-pressed',String(state.simple)); $('simpleModeBtn').innerHTML=icon('grid')+(state.simple?'完整版':'简易版');
    renderDate();
    $('breadcrumbPage').textContent = simpleTitle||(state.page === 'picks' ? '选股推荐' : pageLabels[state.page]);
    all('.nav-item').forEach(button => { const active = button.dataset.page === state.page; button.classList.toggle('active',active); if (active) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current'); let dot=button.querySelector('.nav-active-dot');if(active&&!dot){dot=document.createElement('span');dot.className='nav-active-dot';button.append(dot);}if(dot)dot.hidden=!active; });
    all('[data-source]').forEach(button => { const active = button.dataset.source === state.source; button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active)); });
    document.querySelector('.source-tabs').hidden = state.page !== 'picks';
    const strategies = state.source === 'ths' ? ['system','conditional','combined'] : Object.keys(strategyLabels);
    document.querySelector('.strategy-tabs').innerHTML = strategies.map(key => `<button data-strategy="${key}" class="${state.strategy === key ? 'active' : ''}" aria-pressed="${state.strategy === key}">${strategyLabels[key]}${key === 'surge' ? '<span class="strategy-tag">观察池</span>' : ''}</button>`).join('');
    document.querySelector('.strategy-tabs').hidden = state.page !== 'picks' || state.source === 'user';
    $('marketStrip').hidden = !preview; $('resultsPanel').hidden = !preview; $('pageContent').hidden = preview;
    if (!preview) {
      $('marketCharts').hidden = true; $('marketToggle').setAttribute('aria-expanded','false');
    }
    if (preview) pages.render(); renderTable();
    const route = new URL(location.href); route.searchParams.set('page',state.page);
    if (state.page === 'picks') { route.searchParams.set('source',state.source); route.searchParams.set('strategy',state.strategy); }
    else { route.searchParams.delete('source'); route.searchParams.delete('strategy'); }
    if(state.simple)route.searchParams.set('simple','1');else route.searchParams.delete('simple');
    history.replaceState(null,'',route);
  }
  function candleChart(stock,chartMode=state.chart,range=state.chartRange) {
    return window.TradeIQPreviewCharts.render({key:'inline-'+(stock.code||stock.name),stock:{...stock,code:stock.code||stock.name},date:state.date,mode:chartMode,funds:false,range:range===100?'all':'20'});
  }
  function renderDetail() {
    const stock=pages.detailStock?.(state.selected)||sampleStocks.find(item=>item.code===state.selected);if(!stock)return;
    const stocks=visibleStocks(),position=stocks.findIndex(item=>item.code===stock.code);
    $('prevStock').disabled=position<=0;$('nextStock').disabled=position<0||position>=stocks.length-1;
    $('detailContent').innerHTML=`<section class="stock-summary"><div class="stock-name-row"><div><h2>${escape(stock.name)}${window.TradeIQPages.sectorTag(stock.sector)}</h2><div class="stock-identity"><span>${escape(stock.code)}</span><span>虚构示例</span></div></div><span class="stock-date-chip">${shortDate(state.date)} / 收盘</span></div><div class="stock-price-row"><b class="stock-price">${stock.price.toFixed(2)}</b><span class="stock-change ${tone(stock.change)}">${percentage(stock.change)}</span></div><div class="stock-summary-grid"><div class="summary-stat"><span>5日主力强度</span><b>${stock.strength.toFixed(2)}%</b></div><div class="summary-stat"><span>成交量比</span><b>${(stock.ratio*100).toFixed(1)}%</b></div></div><div class="summary-actions">${pages.quickPick(stock.code,state.date)}<button class="text-button" data-ui-action="external-detail" data-code="${stock.code}">${icon('arrow')}东方财富详情</button></div></section><section class="detail-section"><div class="detail-section-heading"><h3>价格与资金走势</h3><div class="chart-tabs"><button data-chart="daily" aria-pressed="${state.chart==='daily'}" class="${state.chart==='daily'?'active':''}">日K</button><button data-chart="minute" aria-pressed="${state.chart==='minute'}" class="${state.chart==='minute'?'active':''}">分时</button></div></div>${state.preview==='missing'&&state.chart==='minute'?'<div class="view-note">所选日分时示例缺失，保留日期，不用其他日期替代。</div>':window.TradeIQPreviewCharts.render({key:'detail-'+stock.code+'-'+state.chart,stock,date:state.date,mode:state.chart,funds:state.chart==='daily',range:'all'})}</section><div class="detail-footnote">所有价格、资金与图形均为内置虚构示例。累计提示随开启的资金档位变化，区间与指针同步；正式数据与收益计算在后续开发接入。</div>`;
  }
  function setDetailExpanded(expanded) {
    state.expanded = expanded; $('appShell').classList.toggle('detail-expanded',expanded);
    $('expandDetail').setAttribute('aria-expanded',String(expanded));
    $('expandDetail').setAttribute('aria-label',expanded ? '还原详情' : '放大详情');
    $('expandDetail').innerHTML = icon(expanded ? 'shrink' : 'expand',false);
  }
  function openDetail(code) {
    state.selected = code; state.chart = 'daily'; state.chartRange = 100; state.fund = 'main'; state.globalSelection = !visibleStocks().some(stock => stock.code === code);
    $('detailPanel').hidden = false; $('detailBackdrop').hidden = false;
    renderTable(); renderDetail(); $('detailPanel').focus({preventScroll:true});
  }
  function closeDetail(restoreFocus = true) {
    const code = state.selected;
    state.selected = null; state.globalSelection = false; setDetailExpanded(false); $('detailPanel').hidden = true; $('detailBackdrop').hidden = true;
    all('#stockTableBody tr').forEach(row => row.classList.remove('selected'));
    all('#pageContent tr').forEach(row => row.classList.remove('selected'));
    if (restoreFocus && code) document.querySelector(`[data-stock="${code}"], [data-ui-stock="${code}"]`)?.focus({preventScroll:true});
  }
  function shiftStock(delta) {
    const stocks = visibleStocks(); const position = stocks.findIndex(stock => stock.code === state.selected);
    const target = stocks[position+delta]; if (!target) return;
    state.selected = target.code; renderTable(); renderDetail();
  }
  function refresh() {
    const revision = ++state.refreshRevision;
    state.preview = 'loading'; $('refreshBtn').classList.add('is-refreshing'); $('refreshBtn').disabled = true; renderTable();
    setTimeout(() => {
      $('refreshBtn').classList.remove('is-refreshing'); $('refreshBtn').disabled = false;
      if (revision !== state.refreshRevision) return;
      state.preview = 'normal'; renderTable(); toast('示例已刷新 · 未请求实际行情');
    },450);
  }

  all('.theme-button').forEach(button => button.addEventListener('click',() => setTheme(button.dataset.theme)));
  $('collapseSidebar').addEventListener('click',() => setSidebar(!$('appShell').classList.contains('sidebar-collapsed')));
  $('previewStateBtn').addEventListener('click',() => togglePopover('previewStateBtn','statePopover'));
  $('dateBtn').addEventListener('click',() => togglePopover('dateBtn','datePopover'));
  $('columnsBtn').addEventListener('click',() => togglePopover('columnsBtn','columnsPopover'));
  $('statePopover').addEventListener('click',event => { const button = event.target.closest('[data-state]'); if (!button) return; state.refreshRevision++; state.preview = button.dataset.state; closePopovers(); renderTable(); });
  $('datePopover').addEventListener('click',event=>{const mode=event.target.closest('[data-date-mode]');if(mode){if(mode.dataset.dateMode==='date'){state.dateMode='date';renderDate();}else setDate(dates.at(-1),mode.dataset.dateMode);return;}const button=event.target.closest('[data-date]');if(button&&!button.disabled)setDate(button.dataset.date);});
  $('prevDate').addEventListener('click',() => { const index = dates.indexOf(state.date); if (index > 0) setDate(dates[index-1]); });
  $('nextDate').addEventListener('click',() => { const index = dates.indexOf(state.date); if (index < dates.length-1) setDate(dates[index+1]); });
  $('refreshBtn').addEventListener('click',refresh);
  $('marketViewPrev').addEventListener('click',()=>shiftMarket(-1));$('marketViewNext').addEventListener('click',()=>shiftMarket(1));
  $('simpleModeBtn').addEventListener('click',()=>{state.simple=!state.simple;if(state.simple){state.fullPage=state.page;state.page='index';pages.simpleHome();}else state.page=state.fullPage||'picks';closeDetail(false);renderNavigation();});
  $('simpleNav').addEventListener('click',event=>{const key=event.target.closest('[data-simple]')?.dataset.simple;if(key==='search')pages.action('global-search');else if(key){state.page=key==='picks'?'picks':'index';if(['overview','barometer'].includes(key))pages.simpleHome(key);renderNavigation();}});
  $('marketToggle').addEventListener('click',() => { const opening = $('marketCharts').hidden; $('marketCharts').hidden = !opening; $('marketToggle').setAttribute('aria-expanded',String(opening)); });
  $('rulesToggle').addEventListener('click',() => { const opening = $('rulesPanel').hidden; $('rulesPanel').hidden = !opening; $('rulesToggle').setAttribute('aria-expanded',String(opening)); });
  $('stockSearch').addEventListener('input',event => { state.search = event.target.value.trim(); renderTable(); });
  function updatePreferredFilters(){const custom=$('thresholdSelect').value==='custom';$('thresholdCustom').hidden=!custom;const raw=custom?$('thresholdCustom').value:$('thresholdSelect').value,threshold=Number(raw),ratio=Number($('volumeSelect').value),validRatio=Number.isInteger(ratio)&&ratio>=1&&ratio<=100;const valid=raw!==''&&Number.isFinite(threshold)&&threshold>=0&&(!state.volume||validRatio);$('preferredFilterError').hidden=valid;$('preferredFilterError').textContent='门槛须为非负数，缩量比例须为1～100的整数。';if(valid){state.threshold=threshold;if(validRatio)state.ratio=ratio;renderTable();}}
  $('thresholdSelect').addEventListener('change',updatePreferredFilters);$('thresholdCustom').addEventListener('input',updatePreferredFilters);
  $('volumeSelect').addEventListener('input',updatePreferredFilters);
  $('volumeToggle').addEventListener('click',() => { state.volume = !state.volume; $('volumeToggle').setAttribute('aria-pressed',String(state.volume)); $('volumeSelect').disabled = !state.volume; updatePreferredFilters(); renderTable(); });
  $('columnsPopover').addEventListener('change',event => { const input = event.target.closest('[data-column]'); if (!input) return; input.checked ? state.hiddenColumns.delete(input.dataset.column) : state.hiddenColumns.add(input.dataset.column); savePreference('hidden_columns',JSON.stringify([...state.hiddenColumns])); renderTable(); });
  $('stockTableHead').addEventListener('click',event => { const button = event.target.closest('[data-sort]'); if (!button) return; state.direction = state.sort === button.dataset.sort ? -state.direction : -1; state.sort = button.dataset.sort; renderTable(); });
  $('stockTableBody').addEventListener('click',event => { const button = event.target.closest('[data-stock]'); if (button) openDetail(button.dataset.stock); });
  $('tableState').addEventListener('click',event => {
    if (!event.target.closest('#resetResults')) return;
    if (state.preview === 'error') { state.refreshRevision++; state.preview = 'normal'; renderTable(); }
    else resetResults();
  });
  function changeSection(change){state.refreshRevision++;pageDates[state.page]=state.date;pageModes[state.page]=state.dateMode;closeDetail(false);closePopovers();change();state.date=pageDates[state.page]||state.date;state.dateMode=pageModes[state.page]||'date';state.marketViewDate=state.date;state.preview='normal';renderDate();renderMarket();renderNavigation();}
  all('.nav-item').forEach(button => button.addEventListener('click',() => changeSection(() => { state.page = button.dataset.page; pages.resetSector(); })));
  all('[data-source]').forEach(button => button.addEventListener('click',() => changeSection(() => { sourceStrategies[state.source] = state.strategy; state.source = button.dataset.source; state.strategy = sourceStrategies[state.source]; })));
  document.querySelector('.strategy-tabs').addEventListener('click',event => { const button = event.target.closest('[data-strategy]'); if (button) changeSection(() => { state.strategy = button.dataset.strategy; sourceStrategies[state.source] = state.strategy; }); });
  $('globalSearchBtn').addEventListener('click',() => pages.action('global-search'));
  $('dataCenterBtn').addEventListener('click',() => pages.action('data-center'));
  $('closeDetail').addEventListener('click',() => closeDetail());
  $('detailBackdrop').addEventListener('click',() => closeDetail());
  $('expandDetail').addEventListener('click',() => setDetailExpanded(!state.expanded));
  $('prevStock').addEventListener('click',() => shiftStock(-1)); $('nextStock').addEventListener('click',() => shiftStock(1));
  $('detailContent').addEventListener('click',event => { const button = event.target.closest('[data-chart], [data-fund]'); if (!button) return; if(button.dataset.chart)state.chart = button.dataset.chart;else state.fund = button.dataset.fund; renderDetail(); });
  $('detailContent').addEventListener('change',event => { if(event.target.id !== 'detailRange')return;state.chartRange=Number(event.target.value);renderDetail(); });
  document.addEventListener('click',event => { if (!event.target.closest('.popover-wrap')) closePopovers(); });
  document.addEventListener('keydown',event => {
    if (event.key !== 'Escape' || $('designDialog').open) return;
    if (['statePopover','datePopover','columnsPopover'].some(id => !$(id).hidden)) closePopovers();
    else if (state.expanded) setDetailExpanded(false);
    else if (state.selected) closeDetail();
  });
  setTheme(query.get('theme') || readPreference('theme','light'));
  setSidebar(readPreference('sidebar','expanded') === 'collapsed');
  renderDate(); renderMarket(); renderColumns(); renderNavigation();
  if(sampleStocks.some(stock=>stock.code===query.get('detail')))openDetail(query.get('detail'));
  if(query.get('focus')==='trends'&&state.simple&&state.page==='index')requestAnimationFrame(()=>document.getElementById('market-trends')?.scrollIntoView({block:'start'}));
  const review=query.get('review');if(['history','preview','global-search','history-search','data-center','market-fund-history','exit-plan','calculator','import-trades','design-edit-rule','design-open-diagnostics'].includes(review))pages.action(review,{dataset:{code:'EX.1001'}});
})();
