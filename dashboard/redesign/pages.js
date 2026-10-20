'use strict';

window.TradeIQPages = {
  escapeTag(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  },
  statusTag(label, kind='') {
    const groups = {
      active:['持续活跃'], hot:['新近热点'], cool:['已经降温','断板'], pulse:['今日异动'],
      success:['一般活跃','已采用','已应用','有效','数据完整','完整','已核对','已核对 · 示例','符合','当前组合符合','继续涨停','已成交','正常'],
      warning:['数据不足','待处理','待确认','待更新','待核对','待重新应用','部分缺失','连续不足','未通过','到价提醒 · 示例'],
      danger:['读取失败','应用失败','已失效','无效','缺必需列','持仓变化 · 已失效'],
      info:['历史研究','试验策略','观察池']
    };
    const semantic = Object.entries(groups).find(([,values])=>values.includes(label))?.[0] || 'neutral';
    return `<span class="status-tag${kind?' '+window.TradeIQPages.escapeTag(kind):''}"${kind?'':` data-status-tone="${semantic}"`}>${window.TradeIQPages.escapeTag(label)}</span>`;
  },
  sectorTag(label) {
    const text = window.TradeIQPages.escapeTag(label || '未分类');
    return `<span class="sector-tag${label?'':' is-missing'}" title="${text}">${text}</span>`;
  },
  create(ctx) {
    const {state, stocks, indices, dates, icon, escape: e, pct, tone, shortDate, toast, openDetail, candleChart} = ctx;
    const $ = id => document.getElementById(id);
    const qs = new URLSearchParams(location.search);
    const ui = {index: 'overview', leaderboard: 'ranking', collection: 'sealed', flow: 'em', period: '3d', flowSector: 'industry', positions: 'open', sectors: 'recent', sector: null, search: {}, filters: {}, sort: {}, columns: {}, legend: new Set(), watch: new Set(['计算机','新材料','电子']), userPicks: new Set(stocks.slice(0,6).map(s=>s.code)), positionStocks: new Set(stocks.slice(0,8).map(s=>s.code)), positionValues:new Map(),sectorMembers:new Map(), exitPlans:new Map(),visibleStocks:stocks, importStep: 1, importKind: '', fileName: '', dialog: '', dialogStock: '', indexChart: 'daily',indexRange:'all',overviewRange:'20',historyRange:'all',leaderPeriod:'all',historyPeriod:'1',historyEnabled:false,historyPct:80,freqRange:'1',searchMarket:'all',searchSort:'main',similarFields:new Set(['sh','amount','emMain','up','down']),similarTolerance:'normal',similarHit:0,ocrValues:{},importScenario:'valid',importMapping:{},importChecked:new Set([0,1,2,3,4,5]),removedSectors:new Set(),sectorRoles:new Map(),sectorNotes:new Map(),userPickDates:new Map(stocks.slice(0,6).map(stock=>[stock.code+':'+state.date,{code:stock.code,date:state.date,origin:'manual'}]))};
    const allowedViews = {index:['overview','history','barometer'],leaderboard:['ranking','frequency','top1'],collection:['sealed','broken'],flow:['em','ths','sector'],positions:['open','buy','sell','reconcile','delivery'],sectors:['recent']};
    if (allowedViews[state.page]?.includes(qs.get('view'))) ui[state.page] = qs.get('view');
    const descriptions = {index:'MARKET / DAILY OVERVIEW',leaderboard:'MOMENTUM / LEADERBOARD',limitup:'MARKET / LIMIT-UP ANALYSIS',collection:'MARKET / LIMIT-UP MONITOR',flow:'CAPITAL / FUND FLOW',positions:'ACCOUNT / POSITIONS',picks:'RESEARCH / STOCK SCREENER',sectors:'THEMES / RECENT HOT SECTORS'};
    const strategyNames = {system:'系统推荐',conditional:'条件选股',new:'条件选股新',preferred:'条件优选',dip:'低吸选股',chase:'追高选股',custom:'自定义选股',surge:'冲高观察',combined:'组合选股',user:'我的选股'};
    const sectorNames = [...new Set(stocks.map(s=>s.sector)), '人工智能','半导体','低空经济','新能源','消费电子'];
    if(state.page==='sectors'&&sectorNames.includes(qs.get('sector')))ui.sector=qs.get('sector');
    if(['industry','concept'].includes(qs.get('category')))ui.flowSector=qs.get('category');
    const enrich = (s,i) => ({...s, rank:i+1, main:.42+i*.17, super:.25+i*.11, large:.17+i*.06, medium:-.07-i*.02, netPct:4.2+i*.63, amount:3.64+i*.41, cap:81.4+i*23.1, score:96-i*1.7, rise5:42.8-i*2.1, rise10:68.3-i*3.2, rise20:94.6-i*4.4, times:12-i%8, streak:i<3 ? 5-i : i<7 ? 2 : 1, sealTime:`${i<5?'09':'10'}:${String(31+i*3%28).padStart(2,'0')}`, broken:i%3, turnover:6.3+i*.51, ma10:s.price*.97, ma20:s.price*.958, amplitude:6.9+i*.24, bias:1.9+i*.16, increase120:35.8+i*4.6, vol5:.88+i*.015, highest10:7.8+i*.72, close10:4.2+i*.33, cost:s.price*(i%3===0?1.04:.92), shares:100*(8+i*2), profit:(s.price-s.price*(i%3===0?1.04:.92))*100*(8+i*2), date:state.date, kind:i%2?'加仓':'建仓', status:'已应用'});
    const rows = () => stocks.map((s,i) => { const row=enrich(s,i),override=ui.positionValues.get(s.code);Object.assign(row,{ma5:s.price*.99,ma30:s.price*.94,mid3:row.medium*3,mid5:row.medium*5,mid10:row.medium*10,mid20:row.medium*20,small3:s.small*3,small5:s.small*5,small10:s.small*10,small20:s.small*20,origin:i%2?'ocr':'manual',market:String(i%4),status:i%4===0?'待处理':'已应用',edgeScore:90-i*2.1,signal:i%2?'均线多头':'量价观察',bucket:i%3?'主力':'超大单',momentum:i%2?'20日涨幅>15%':'突破20日高点',quoteTime:'15:00:00',historyState:i<8?'现持仓':i===13?'退市示例':'已清仓',net30:(.42+i*.17)*30}); if(override){Object.assign(row,override);row.profit=(row.price-row.cost)*row.shares;}return row; });
    const viewKey = () => `${state.page}:${state.page==='picks' ? `${state.source}:${state.strategy}` : ui[state.page] || 'main'}:${ui.sector||''}`;
    const searchText = () => ui.search[viewKey()] || '';
    const option = (key,fallback='all') => ui.filters[`${viewKey()}:${key}`] || fallback;
    const button = (label,action,glyph='arrow',primary=false,data='') => `<button class="button${primary?' primary':''}" data-ui-action="${action}" ${data}>${glyph?icon(glyph):''}${label}</button>`;
    const tag = (label,kind='') => window.TradeIQPages.statusTag(label,kind);
    const stockButton = row => `<button class="stock-button" data-ui-stock="${e(row.code)}" aria-label="查看${e(row.name)}详情"><span class="stock-title"><strong>${e(row.name)}</strong>${window.TradeIQPages.sectorTag(row.sector)}</span><span class="stock-code">${e(row.code)}</span></button>`;
    const field = (key,label,options,fallback='all') => `<label class="view-label">${label}<select class="view-select" data-ui-filter="${key}" id="filter-${key}">${options.map(([value,text])=>`<option value="${value}" ${option(key,fallback)===String(value)?'selected':''}>${text}</option>`).join('')}</select></label>`;
    const search = (placeholder='搜索名称或代码') => `<label class="search-field">${icon('search')}<input type="search" id="pageSearch" data-ui-search value="${e(searchText())}" placeholder="${placeholder}" aria-label="${placeholder}" autocomplete="off"></label>`;
    const toolbar = (left='',right='',withSearch=true) => `<div class="view-toolbar"><div class="view-toolbar-left">${left}</div><div class="view-toolbar-right">${withSearch?search():''}${right}</div></div>`;
    const tabs = (key,list) => `<div class="view-tabs" role="group" aria-label="${state.page}子页面">${list.map(([value,label])=>`<button data-ui-tab="${key}" data-value="${value}" class="${ui[key]===value?'active':''}" aria-pressed="${ui[key]===value}">${label}</button>`).join('')}<span class="tab-trailing">${shortDate(state.date)} · 示例收盘</span></div>`;
    const pills = (key,list) => `<div class="period-pills" role="group">${list.map(([value,label])=>`<button data-ui-tab="${key}" data-value="${value}" class="${ui[key]===value?'active':''}" aria-pressed="${ui[key]===value}">${label}</button>`).join('')}</div>`;
    const metricStrip = values => `<div class="metric-strip">${values.map(([label,value,note='',color='',unit=''])=>`<div class="metric-tile"><div class="metric-label">${label}</div><div class="metric-value ${color}">${value}${unit?`<span class="metric-unit">${unit}</span>`:''}</div><div class="metric-note">${note}</div></div>`).join('')}</div>`;
    const note = text => `<div class="view-note">${icon('info')}${text}</div>`;
    function filterRows(list) {
      const text = searchText().toLowerCase();
      let result = list.filter(row=>!text || `${row.name||''} ${row.code||''} ${row.sector||''} ${row.date||''}`.toLowerCase().includes(text));
      const sector = option('sector'); if (sector !== 'all') result = result.filter(row=>row.sector===sector);
      const market = option('market'); if (market !== 'all') result = result.filter(row=>row.market===market);
      const streak = option('streak'); if (streak !== 'all') result=result.filter(row=>streak==='first'?row.streak===1:row.streak>1);
      const status=option('status');if(status!=='all')result=result.filter(row=>row.status===(status==='applied'?'已应用':'待处理'));
      const origin=option('origin');if(origin!=='all')result=result.filter(row=>row.origin===origin);
      if(state.page==='picks'&&['conditional','new','combined'].includes(state.strategy)&&state.source!=='user'){const threshold=Number(option('threshold','3'));result=result.filter(row=>row.amount20>=threshold);if(state.strategy==='new')result=result.filter(row=>row.ratio*100<=Number(option('volumePct','80')));}
      const sort = ui.sort[viewKey()];
      if (sort) result.sort((a,b)=>typeof a[sort.key]==='number'?sort.direction*(a[sort.key]-b[sort.key]):sort.direction*String(a[sort.key]||'').localeCompare(String(b[sort.key]||''),'zh-CN'));
      return result;
    }
    function cell(column,row) {
      const value = row[column.key];
      if (column.render) return column.render(row);
      if (column.kind==='stock') return stockButton(row)+(state.page==='picks'&&state.source!=='user'&&!$('designDialog').open?quickPick(row.code,row.date):'');
      if (value === null || value === undefined || state.preview==='missing' && ['outcome','money'].includes(column.kind) && row.rank%3===0) return '<span class="missing-value">—</span>';
      if (column.kind==='pct') return `<span class="${tone(value)}">${pct(value)}</span>`;
      if (column.kind==='money') return `<span class="${tone(value)}">${value>=0?'+':''}${Number(value).toFixed(2)}<span class="muted"> 亿</span></span>`;
      if (column.kind==='price') return Number(value).toFixed(2);
      if (column.kind==='number') return Number(value).toLocaleString('en-US',{maximumFractionDigits:2});
      if (column.kind==='tag') return tag(value);
      if(column.kind==='outcome'&&!dates[dates.indexOf(row.date||state.date)+Number(column.key.slice(1))])return '<span class="missing-value" title="示例后续交易日不足">—</span>';
      if (column.kind==='outcome') return `<span class="outcome-high ${tone(value.high)}">${pct(value.high)}</span><span class="outcome-close">${pct(value.close)}</span>`;
      return e(value);
    }
    function table(title,list,columns,{minWidth=1040,actions='',id='main',searchable=true,totalCount=null}={}) {
      const hidden = ui.columns[`${viewKey()}:${id}`] || new Set();
      ui.columnChoices||={};ui.columnChoices[`${viewKey()}:${id}`]=columns.map(column=>({key:column.key,label:column.label}));
      const visible = columns.filter(c=>!hidden.has(c.key));
      const filtered = searchable?filterRows(list):[...list];
      if(!searchable&&ui.sort[viewKey()]){const sort=ui.sort[viewKey()];filtered.sort((a,b)=>typeof a[sort.key]==='number'?sort.direction*(a[sort.key]-b[sort.key]):sort.direction*String(a[sort.key]||'').localeCompare(String(b[sort.key]||''),'zh-CN'));}
      if(searchable&&list.some(row=>row.code))ui.visibleStocks=filtered;
      const isEmpty = state.preview==='empty' || !filtered.length;
      const failed = ['loading','error'].includes(state.preview);
      let content;
      if (isEmpty || failed) {
        const title = state.preview==='loading'?'正在加载示例数据':state.preview==='error'?'暂时无法读取结果':'没有符合条件的示例记录';
        content=`<div class="view-empty">${icon(state.preview==='loading'?'refresh':'search',false)}<strong>${title}</strong><p>${state.preview==='loading'?'可从右上角切换其他预览状态。':'保留当前页面和筛选，恢复示例后可继续评审。'}</p>${state.preview==='loading'?'':button('恢复示例','reset-view','refresh')}</div>`;
      } else {
        content=`<div class="view-table-scroll" data-table-scroll="${id}"><table class="page-table" style="min-width:${Math.max(550,minWidth-hidden.size*85)}px"><thead><tr>${visible.map(c=>`<th class="${c.kind==='stock'?'stock-header':c.kind==='text'||c.kind==='tag'?'text-cell':''}"><button data-ui-sort="${c.key}" aria-label="按${e(c.label.replace(/<[^>]+>/g,' '))}排序">${c.label}${c.kind==='price'||c.kind==='number'||c.kind==='pct'||c.kind==='money'?icon(ui.sort[viewKey()]?.key===c.key?(ui.sort[viewKey()].direction<0?'sort-down':'sort-up'):'sort'):''}</button></th>`).join('')}</tr></thead><tbody>${filtered.map(row=>`<tr data-code="${row.code||''}" class="${state.selected===row.code?'selected':''}">${visible.map(c=>`<td class="${c.kind==='stock'?'stock-cell':c.kind==='text'||c.kind==='tag'?'text-cell':''}">${cell(c,row)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      }
      return `<section class="view-table-card" data-view-table="${id}"><div class="view-table-heading"><div class="view-table-heading-left"><h2>${title}</h2><span class="count-badge mono">${failed?'—':isEmpty?'0':totalCount??filtered.length}</span><span class="view-subtle">虚构示例</span></div><div class="view-toolbar-right">${actions}<button class="text-button" data-ui-action="table-columns" data-table-id="${id}">${icon('columns')}列设置</button></div></div>${state.preview==='missing'?'<div class="missing-notice">部分示例记录缺失，对应指标留空。</div>':''}${content}<div class="table-footer"><span>${failed?'尚未加载':`${isEmpty?0:filtered.length} 条示例记录`} · ${shortDate(state.date)}</span><span>${columns.some(c=>c.kind==='stock')?'股票名称固定 · 点击查看详情':'点击表头排序 · 示例记录'}</span></div></section>`;
    }
    const c = (key,label,kind='number',render=null) => ({key,label,kind,render});
    const stockCols = [c('rank','#'),c('name','股票名称 / 代码','stock')];
    const outcomeCols=()=>Array.from({length:5},(_,i)=>{const date=dates[dates.indexOf(state.date)+i+1];return c(`t${i+1}`,`T+${i+1}<span class="column-date">${date?shortDate(date):'待交易日'}</span>`,'outcome');});
    const withOutcomes = list => list.map(row=>({...row,...Object.fromEntries(row.outcomes.map((value,i)=>[`t${i+1}`,value]))}));
    function plot(seed=1,{height=200,bars=false,series=1,label='趋势',signed=false}={}) {
      seed+=dates.indexOf(state.date)*.13+(ui.overviewRange==='60'?.2:0);
      const values=Array.from({length:36},(_,i)=>signed?Math.sin(i*.42+seed)*23+i*.2:30+i*.74+Math.sin(i*.44+seed)*9);
      const y=v=>signed?80-v*1.8:175-v*1.7;
      let shapes='<path d="M35 28H615M35 74H615M35 120H615M35 166H615" fill="none" stroke="var(--chart-grid)"/>';
      if(bars) shapes+=values.map((v,i)=>`<rect x="${39+i*16}" y="${Math.min(100,y(v))}" width="8" height="${Math.max(3,Math.abs(100-y(v)))}" fill="${v>=0?'var(--up)':'var(--down)'}" opacity=".65"/>`).join('');
      else for(let line=0;line<series;line++) if(!ui.legend.has(`${label}:${line}`)) shapes+=`<polyline points="${values.map((v,i)=>`${39+i*16},${y(v)+line*14+Math.sin(i*.3)*line*7}`).join(' ')}" fill="none" stroke="${line===0?'var(--chart-line)':line===1?'var(--secondary)':'var(--muted)'}" stroke-width="${line===0?'1.8':'1.2'}" ${line===2?'stroke-dasharray="4 3"':''}/>`;
      return `<div class="chart-surface${height<180?' compact':''}" data-chart-values="${e(JSON.stringify(values.map(v=>v.toFixed(2))))}" data-chart-label="${e(label)}"><svg class="view-plot${height<180?' compact':''}" viewBox="0 0 650 210" preserveAspectRatio="none" role="img" aria-label="${e(label)}虚构趋势图">${shapes}</svg><div class="chart-y-labels">${(signed?['40','20','0','−20']:['60','40','20','0']).map(v=>`<span>${v}</span>`).join('')}</div><div class="chart-x-labels"><span>09.01</span><span>09.08</span><span>09.15</span><span>${shortDate(state.date)}</span></div><div class="chart-guide" hidden></div><div class="chart-tip" hidden></div></div>`;
    }
    function panel(title,content,action='') { return `<section class="view-panel"><div class="panel-title"><h2>${title}</h2>${action}</div>${content}</section>`; }
    const miniPlot = seed => `<svg class="mini-plot" viewBox="0 0 300 50" aria-hidden="true"><polyline points="${Array.from({length:36},(_,i)=>`${i*8.5},${40-i*.6+Math.sin(i*.63+seed)*7}`).join(' ')}" fill="none" stroke="var(--chart-secondary)" stroke-width="1.4"/></svg>`;
    const sectorFilter = () => field('sector','板块',[['all','全部板块'],...sectorNames.slice(0,7).map(name=>[name,name])]);
    const marketFilter = () => field('market','市场',[['all','全部市场'],['0','沪市'],['1','深市'],['2','创业板'],['3','科创板']]);
    const pageTitles = {index:'核心指数',leaderboard:'阶段涨幅榜',limitup:'涨停拆解',collection:'涨停合集',flow:'主力净流入',positions:'持仓',picks:'选股工作台',sectors:'热门板块'};
    function pageActions() {
      if(state.page==='positions') return button('导入持仓','import-positions','upload')+button('导入成交','import-trades','file')+button('手动添加','add-position','add',true);
      if(state.page==='limitup') return button('上传简图','import-image','image')+button('拉取韭研','fetch-limitup','download');
      if(state.page==='flow'||state.page==='index') return button('数据同步','sync','refresh');
      if(state.page==='sectors') return designAction('热度说明','hot-info','info');
      if(state.page==='picks'&&state.source==='em'&&state.strategy==='custom')return '';
      if(state.page==='picks'&&state.source==='em'&&['dip','chase'].includes(state.strategy))return designAction('条件诊断','open-diagnostics','info');
      if(state.page==='picks'&&state.source==='user') return button('上传识别','import-picks','image')+button('手动添加','add-pick','add',true);
      if(state.page==='picks'&&state.strategy==='preferred') return button('历史统计','history','history')+button('生成预览图','preview','image');
      if(state.page==='picks') return button('同步数据','sync','refresh')+(['conditional','combined'].includes(state.strategy)?button('补最近60天','batch-backfill','database'):'')+button('生成预览图','preview','image');
      return button('导出示例','export','download');
    }
    function renderIndex() {
      let html=state.simple?'':tabs('index',[['overview','市场总览'],['history','指数与盘面历史'],['barometer','盘面晴雨表']]);
      if(ui.index==='barometer') {
        const data=dates.map((date,i)=>{const row={date:`2026.${shortDate(date)}`,amount:17834+i*123,sh:3790+i*8,shPct:-.68+i*.23,kc:1310+i*5,kcPct:.37+i*.12,cy:2840+i*11,cyPct:-1.21+i*.29,main:-128+i*31,super:-76+i*19,small:33-i*4,up:2397+i*93,down:2814-i*83};for(let n=1;n<=3;n++)Object.assign(row,{['sector'+n]:sectorNames[(i+n)%sectorNames.length],['sectorPct'+n]:3.5-n*.4+i*.1,['sectorNet'+n]:7.4-n+i*.2,['leader'+n]:stocks[(i+n)%stocks.length].name});[5,10,20].forEach((n,j)=>Object.assign(row,{['top'+n]:stocks[(i+j)%stocks.length].name,['topSector'+n]:sectorNames[(i+j)%sectorNames.length]}));return row;});
        const base=[c('date','日期','text'),c('amount','成交额 / 亿'),c('sh','上证','price'),c('shPct','上证涨跌','pct'),c('kc','科创50','price'),c('kcPct','科创涨跌','pct'),c('cy','创业板','price'),c('cyPct','创业板涨跌','pct'),c('main','主力 / 亿','money'),c('super','超大单 / 亿','money'),c('small','小单 / 亿','money'),c('up','上涨'),c('down','下跌')];
        const concept=[1,2,3].flatMap(n=>[c('sector'+n,`TOP${n}板块`,'text'),c('sectorPct'+n,`TOP${n}涨幅`,'pct'),c('sectorNet'+n,`TOP${n}主力`,'money'),c('leader'+n,`TOP${n}领涨股`,'text')]);
        const leaders=[5,10,20].flatMap(n=>[c('top'+n,`${n}日榜个股`,'text'),c('topSector'+n,`${n}日榜板块`,'text')]);
        return html+toolbar(note('晴雨表保留31个数据字段；列设置可调整，表格内横向滚动。'),button('同步飞书示例','sync','refresh'),false)+table('盘面晴雨表',data,[...base,...concept,...leaders],{searchable:false,minWidth:3400,id:'barometer'});
      }
      if(ui.index==='history') {
        const history=dates.map((date,i)=>({date:`2026.${shortDate(date)}`,sh:3790+i*8,shPct:-.68+i*.23,shAmount:4680+i*120,sz:12540+i*27,szPct:-.94+i*.18,szAmount:7340+i*90,cy:2840+i*11,cyPct:-1.21+i*.29,cyAmount:1830+i*40,kc:1310+i*5,kcPct:.37+i*.12,kcAmount:640+i*20,amount:17834+i*123,up:2397+i*19,down:2814-i*19,flat:143,main:-128+i*8,super:-76+i*4,thsNet:-96+i*9,limitUp:76+i,limitDown:12-i}));
        const range=ui.historyRange==='30'?history.slice(-5):ui.historyRange==='90'?history.slice(-7):history;
        html+=toolbar(field('index','指数',[['all','全部指数'],...indices.map((index,i)=>[String(i),index.name])])+pills('historyRange',[['all','全部日期'],['30','近30日'],['90','近90日']]),button('找相近盘面','similar','search'),false);
        const chosen=option('index'),keys=['sh','sz','cy','kc'];
        const cols=[c('date','交易日','text'),...indices.flatMap((index,i)=>chosen==='all'||chosen===String(i)?[c(keys[i],index.name,'price'),c(keys[i]+'Pct',index.name+'涨跌','pct'),c(keys[i]+'Amount',index.name+'成交额 / 亿')]:[])];
        return html+panel('收盘点位趋势',plot(2,{series:chosen==='all'?4:1,label:'指数收盘'}))+table('指数历史 · 点位 / 涨跌 / 成交额',range,cols,{searchable:false,minWidth:1700,id:'index-history'})+table('盘面历史 · 同源资金与涨跌家数',range,[c('date','日期','text'),c('amount','成交额 / 亿'),c('shPct','上证涨跌','pct'),c('main','东财主力 / 亿','money'),c('super','东财超大单 / 亿','money'),c('thsNet','同花顺总资金 / 亿','money'),c('up','上涨'),c('down','下跌'),c('flat','平盘'),c('limitUp','涨停'),c('limitDown','跌停')],{searchable:false,minWidth:1550,id:'overview-history'});
      }
      html+=metricStrip([['总成交额','1.83','较前日 +620 亿元','','万亿'],['主力净流入','−128.46','沪深市场合计 · 示例','down','亿元'],['上涨 / 下跌','2,397 / 2,814','平盘 143 家 · 上涨占比 44.8%'],['涨停 / 跌停','76 / 12','连板 19 家 · 炸板 24 家']]);
      html+=`<div class="index-card-grid">${indices.map((index,i)=>`<button class="index-large-card" data-ui-action="index-chart" data-index="${i}"><div class="index-card-heading">${index.name}${icon('arrow')}</div><div class="index-card-quote"><b>${index.value}</b><span class="${tone(index.change)}">${pct(index.change)}</span></div>${miniPlot(i)}<div class="index-card-meta"><span>成交额 ${4680-i*612} 亿</span><span>查看分时 / 日K</span></div></button>`).join('')}</div>`;
      if(state.simple)return html+`<div id="market-trends" class="market-trend-grid">${['indices','breadth'].map(kind=>window.TradeIQPreviewCharts.renderMarketTrend({key:'workspace-trend-'+kind,kind,date:state.date,state:state.preview})).join('')}</div>`;
      html+=`<div class="panel-grid">${panel('市场趋势 · 涨跌幅对比',plot(2,{series:3,label:'市场趋势'}),pills('overviewRange',[['20','20日'],['60','60日'],['all','全部']]))}${panel('涨跌幅区间分布',`<div class="distribution-list">${[['涨停',76,10,'up'],['+5%～涨停',183,21,'up'],['0～+5%',2138,76,'up'],['−5%～0',2486,92,'down'],['跌停～−5%',316,35,'down'],['跌停',12,6,'down']].map(([name,value,width,color])=>`<div class="distribution-row"><span>${name}</span><div class="distribution-track"><span class="${color}" style="width:${width}%"></span></div><b>${value}</b></div>`).join('')}</div>`)}</div>`;
      html+=`<div class="panel-grid equal">${panel('板块热度',`<div class="heatmap-grid">${sectorNames.slice(0,10).map((name,i)=>`<button class="heatmap-cell ${tone(2.3-i*.39)}" data-ui-action="sector-open" data-sector="${name}">${name}<b>${pct(2.3-i*.39)}</b></button>`).join('')}</div>`)}${panel('连板梯队',`<div class="ladder">${[5,4,3,2,1].map((level,i)=>`<div class="ladder-row"><b>${level} 板</b><div>${stocks.slice(i,i+3).map(stock=>`<button class="ladder-stock" data-ui-stock="${stock.code}">${stock.name}</button>`).join('')}</div></div>`).join('')}</div>`)}</div>`;
      return html+panel('成交额分布',plot(3,{bars:true,label:'成交额分布'}));
    }
    function renderLeaderboard() {
      let html=tabs('leaderboard',[['ranking','阶段榜单'],['frequency','上榜频次'],['top1','TOP1 历史']]);
      html+=toolbar(pills('leaderPeriod',[['all','全部区间'],['5','5日'],['10','10日'],['20','20日']])+(ui.leaderboard==='frequency'?pills('freqRange',[['1','1个月'],['6','6个月'],['12','12个月']]):''),button('导出示例','export','download'),ui.leaderboard!=='top1');
      const periods=ui.leaderPeriod!=='all'?[+ui.leaderPeriod]:[5,10,20];
      if(ui.leaderboard==='ranking')return html+`<div class="ranking-grid" style="grid-template-columns:repeat(${periods.length},minmax(0,1fr))">${periods.map(period=>`<section class="view-panel ranking-panel"><div class="ranking-panel-head"><h2>${period}日涨幅榜</h2><span class="mono">TOP10 / 示例</span></div>${filterRows(rows()).slice(0,10).map((row,i)=>`<div class="ranking-row"><span class="rank-number">${i+1}</span>${stockButton(row)}<div><div class="rank-value">${pct(row['rise'+period])}</div><div class="rank-delta">${row.times}次上榜</div></div></div><div class="ranking-extra"><span>现价 ${row.price.toFixed(2)}</span><span class="${tone(row.change)}">当日 ${pct(row.change)}</span><span>成交 ${row.amount.toFixed(2)}亿</span></div>`).join('')}</section>`).join('')}</div>`;
      if(ui.leaderboard==='frequency')return html+note(`近${ui.freqRange}个月统计范围 · 示例频次按5/10/20日榜分别列出。`)+periods.map(period=>table(`${period}日榜 · ${ui.freqRange}个月频次`,rows().map((row,i)=>({...row,times:row.times*Number(ui.freqRange)+i%3,rise:row['rise'+period]})),[...stockCols,c('times','上榜次数'),c('rise','区间涨幅','pct'),c('price','最新价','price'),c('amount','成交额 / 亿'),c('date','最近上榜','text')],{minWidth:1050,id:'frequency-'+period})).join('');
      return html+panel('TOP1 涨幅轨迹 · 5 / 10 / 20日',plot(4,{series:periods.length,label:'TOP1涨幅'}))+table('TOP1 历史',dates.map((date,i)=>({...enrich(stocks[i],i),date:`2026.${shortDate(date)}`,period:`${periods[i%periods.length]}日`})),[c('date','日期','text'),c('period','周期','tag'),c('name','领涨股票','stock'),c('rise5','区间涨幅','pct'),c('price','价格','price'),c('times','上榜频次')],{searchable:false,minWidth:1050});
    }
    function renderLimitup() {
      const list=rows().map(row=>({...row,change:row.rank>7?19.99:9.99,keyword:`${row.sector} / 产业升级`,seal:'封板中'}));
      return metricStrip([['涨停家数','76','其中首板 57 家'],['连板家数','19','最高高度 5 板'],['封板率','76.0%','曾涨停 100 家'],['涨停成交额','628.40','沪深市场 · 示例','','亿元']])+toolbar(sectorFilter()+field('streak','连板',[['all','全部'],['first','仅首板'],['streak','仅连板']]),button('查看原图','image-view','image'))+table('涨停拆解明细',list,[...stockCols,c('sector','板块','text'),c('change','涨幅','pct'),c('streak','连板高度'),c('sealTime','首次封板','text'),c('cap','流通市值 / 亿'),c('amount','成交额 / 亿'),c('keyword','涨停关键词','text'),c('seal','状态','tag')],{minWidth:1260});
    }
    function renderCollection() {
      const list=rows().filter(row=>ui.collection==='broken'?row.broken>0:row.broken===0).map(row=>({...row,change:ui.collection==='sealed'?9.99:row.rank%3*1.61+3.25,status:ui.collection==='sealed'?'封板中':'已炸板'}));
      return tabs('collection',[['sealed','封板中'],['broken','炸板']])+metricStrip([['封板中','76','强势封板记录'],['已炸板','24','曾涨停后回落'],['连板数量','19','首板 / 连板分开查看'],['当前状态',state.dateMode==='live'?'实时演示':'历史收盘','与顶栏日期模式一致']])+toolbar(field('refresh','自动刷新',[['0','关闭'],['3','3秒'],['30','30秒'],['60','60秒']],'0')+sectorFilter(),note('刷新周期只演示控件，不开启后台轮询。'))+table(ui.collection==='sealed'?'封板中的股票':'炸板跟踪',list,[...stockCols,c('price','价格','price'),c('market','市场','text',row=>['沪市','深市','创业板','科创板'][Number(row.market)]),c('change','当日涨幅','pct'),c('streak','连板'),c('sealTime','封板时间','text'),c('broken','炸板次数'),c('amount','成交额 / 亿'),c('turnover','换手率','pct'),c('status','封板状态','tag')],{minWidth:1100});
    }
    function renderFlow() {
      let html=tabs('flow',[['em','东方财富'],['ths','同花顺'],['sector','板块资金']]);
      if(ui.flow==='sector') html+=toolbar(pills('flowSector',[['industry','行业'],['concept','概念']]),button('数据覆盖','data-center','database'));
      else html+=toolbar(pills('period',[['today','当日'],['3d','3日'],['5d','5日'],['10d','10日'],['20d','20日'],['30d','30日'],['60d','60日'],['90d','90日']]),button('补指定日期','backfill','date'),false);
      html+=ui.flow==='ths'?metricStrip([['总资金净额','−96.84','同花顺口径 · 示例','down','亿元'],['流入资金','986.42','使用总资金，不混用主力字段','','亿元'],['流出资金','1,083.26','同源资金覆盖','','亿元'],['资金覆盖','5,214','10日窗口 · 部分示例缺失','','只']]):metricStrip([['主力净流入','−128.46','沪深合计 · 示例','down','亿元'],['超大单','−76.32','总成交额占比 −4.17%','down','亿元'],['大单','−52.14','总成交额占比 −2.85%','down','亿元'],['资金覆盖','5,354','记录完整度 99.2%','','只']]);
      html+=ui.flow==='ths'?panel('同花顺 · 总资金趋势',plot(3,{signed:true,series:1,height:126,label:'同花顺总资金'})):panel('沪深资金流向 · 五档',plot(3,{signed:true,series:5,height:126,label:'沪深资金流'}),`<div class="chart-legend-controls">${['主力','超大单','大单','中单','小单'].map((label,i)=>`<button data-ui-legend="沪深资金流:${i}" aria-pressed="${!ui.legend.has(`沪深资金流:${i}`)}"><i></i>${label}</button>`).join('')}${button('每日净额 / 净占比','market-fund-history','chart')}</div>`);
      if(ui.flow==='sector') {
        const data=(ui.flowSector==='industry'?sectorNames.slice(0,7):sectorNames.slice(7)).map((name,i)=>({name,sector:name,rank:i+1,main:4.62-i*.63,netPct:4.1+i*.9,super:2.5-i*.3,large:2.12-i*.33,medium:-1.1+i*.1,small:-3.52+i*.53,lead:stocks[i%stocks.length].name,date:state.date,change:3.2-i*.51,amount:183-i*11.2,count:32+i*7}));
        data.sort((a,b)=>option('order','net')==='pct'?b.netPct-a.netPct:b.main-a.main);
        html+=toolbar(field('order','排序',[['net','主力净额'],['pct','净占比']],'net'),'',true);
        return html+table(ui.flowSector==='industry'?'行业资金排行':'概念资金排行',data,[c('rank','#'),c('name','板块','text',row=>`<button class="row-action" data-ui-action="sector-open" data-sector="${row.name}">${row.name}</button>`),c('change','涨跌幅','pct'),c('main','主力净流入','money'),c('netPct','净占比','pct'),c('super','超大单','money'),c('large','大单','money'),c('medium','中单','money'),c('small','小单','money'),c('lead','领涨股','text'),c('date','交易日期','text'),c('amount','成交额 / 亿'),c('count','股票数')],{minWidth:1580});
      }
      html+=toolbar(marketFilter(),button('个股查询','global-search','search'));
      const multiplier=ui.period==='today'?1:parseInt(ui.period,10)||3;
      const list=rows().map(row=>({...row,main:row.main*multiplier,super:row.super*multiplier,large:row.large*multiplier,medium:row.medium*multiplier,small:row.small*multiplier}));
      const common=[...stockCols,c('price','最新价','price'),c('change','涨跌幅','pct')];
      const funds=ui.flow==='ths'?[c('main','同花顺净额','money'),c('amount','流入资金 / 亿'),c('cap','流出资金 / 亿'),c('turnover','换手率','pct')]:[c('main','主力净流入','money'),c('netPct','净占比','text',row=>ui.period==='today'?pct(row.netPct):'—'),c('super','超大单','money'),c('large','大单','money'),c('medium','中单','money'),c('small','小单','money')];
      return html+table(`${ui.flow==='ths'?'同花顺总资金':'东方财富主力'} · ${ui.period==='today'?'当日':multiplier+'日累计'}`,list,[...common,...funds],{minWidth:1300});
    }
    function renderPositions() {
      let html=tabs('positions',[['open','现持仓'],['buy','买入'],['sell','卖出'],['reconcile','对账日志'],['delivery','交割分析']]);
      const list=rows().filter(row=>ui.positionStocks.has(row.code));
      if(ui.positions==='delivery') return html+toolbar(field('range','交易区间',[['all','全部交易'],['30','近30天'],['90','近90天']]),button('查历史股票','history-search','search')+button('导出示例','export','download'))+note('图中B/S为虚构成交点；鼠标查看同日价格与资金，点击成交明细可展开。')+`<div class="delivery-grid">${filterRows(list).map(row=>`<section class="delivery-card"><div class="panel-title">${stockButton(row)}${button('成交明细','trade-history','history',false,`data-code="${row.code}"`)}</div>${window.TradeIQPreviewCharts.render({key:'delivery-'+row.code,stock:row,date:state.date,delivery:true,compact:true,funds:true,range:option('range')==='30'?'20':'all'})}</section>`).join('')}</div>`;
      if(ui.positions==='reconcile') {
        const log=list.slice(0,5).map((row,i)=>({...row,date:`2026.${shortDate(state.date)} 15:${32+i}`,field:i%2?'成本价':'持仓数量',old:i%2?28.42:800,new:i%2?27.64:1000,delta:i%2?-.78:200,trigger:`买入成交 #${1203+i}`}));
        return html+metricStrip([['对账记录','5','示例修复记录'],['涉及股票','5','所有记录可追溯'],['最近修复','15:36','入库成交后对照展示'],['待处理','0','记录状态一致']])+toolbar('',button('清空示例日志','clear-demo','delete'))+table('对账修复记录',log,[c('date','修复时间','text'),c('name','股票','stock'),c('field','修复字段','text'),c('old','旧值'),c('new','新值'),c('delta','差值'),c('trigger','触发成交','text')],{minWidth:1060});
      }
      if(ui.positions==='buy'||ui.positions==='sell') {
        const selling=ui.positions==='sell';
        const tradeRows=list.map(row=>({...row,price:row.cost,amount:row.cost*row.shares,fee:row.shares*.015,realized:row.profit*.68,post:row.change*-.8}));
        return html+metricStrip([[selling?'卖出笔数':'买入笔数',String(tradeRows.length),'示例成交明细'],[selling?'卖出金额':'买入金额',selling?'86,420.00':'142,680.00','按示例成交价计算','','元'],['交易税费','128.40','用于版式演示','','元'],[selling?'已实现盈亏':'买入后涨幅',selling?'+6,240.00':'+3.82%',selling?'仅示例收益':'不同股票分别展示','up',selling?'元':'']])+toolbar(field('status','处理状态',[['all','全部'],['applied','已应用'],['pending','待处理']]),button('应用示例成交','apply-trades','check'))+table(selling?'卖出成交记录':'买入成交记录',tradeRows,[...stockCols,c('shares','成交数量'),c('price','成交价格','price'),c('amount','成交金额 / 元'),c('fee','税费 / 元'),c('history','成交详情','text',row=>button('查看','trade-history','history',false,`data-code="${row.code}"`)),...(selling?[c('realized','已实现盈亏 / 元')]:[]),c('post',selling?'卖出后涨幅':'买入后涨幅','pct'),c('date','交易日期','text'),c('status','状态','tag')],{minWidth:1330});
      }
      list.forEach(row=>{row.dailyProfit=row.price*row.shares*row.change/100;row.marketValue=row.price*row.shares;row.profitPct=(row.price/row.cost-1)*100;row.plan=ui.exitPlans.has(row.code)?(ui.exitPlans.get(row.code).status||'已采用'):'未设置';});
      const total=list.reduce((sum,row)=>sum+row.price*row.shares,0);
      const profit=list.reduce((sum,row)=>sum+row.profit,0);
      html+=metricStrip([['持仓股票',String(list.length),'账户数据全部为示例','','只'],['持仓市值',total.toLocaleString('en-US',{maximumFractionDigits:2}),'使用示例现价计算','','元'],['浮动盈亏',`${profit>=0?'+':''}${profit.toFixed(2)}`,'示例汇总，不对应实际账户',tone(profit),'元'],['仓位分布','62.4%','可用资金 37.6%']]);
      html+=toolbar(field('refresh','自动刷新',[['0','关闭'],['3','3秒'],['30','30秒'],['60','60秒'],['300','5分钟']],'0'),button('查历史股票','history-search','search')+button('账户汇总','account-summary','wallet'));
      const actions=row=>`<div class="actions-cell"><button class="row-action" data-ui-action="trade-history" data-code="${row.code}">${icon('history')}成交</button><button class="row-action" data-ui-action="calculator" data-code="${row.code}">${icon('calc')}减仓</button><button class="row-action" data-ui-action="exit-plan" data-code="${row.code}">${icon('settings')}方案</button><button class="row-action" data-ui-action="remove-position" data-code="${row.code}" aria-label="移除${row.name}示例持仓">${icon('delete')}</button></div>`;
      return html+table('现持仓',list,[...stockCols,c('shares','持仓数量'),c('cost','成本价','price'),c('price','现价','price'),c('change','今日涨跌','pct'),c('dailyProfit','今日盈亏 / 元','number',row=>`<span class="${tone(row.dailyProfit)}">${row.dailyProfit.toFixed(2)}</span>`),c('marketValue','持仓市值 / 元'),c('profit','浮动盈亏 / 元','number',row=>`<span class="${tone(row.profit)}">${row.profit.toFixed(2)}</span>`),c('profitPct','盈亏比例','pct'),c('main','5日主力','money'),c('amount20','20日主力','money'),c('date','交易日期','text'),c('kind','持仓类型','tag'),c('plan','减仓方案','tag'),c('actions','操作','text',actions)],{minWidth:2050});
    }
    // October UI review: fixed fictional samples, independent of business services.
    const cloneDesign = value => JSON.parse(JSON.stringify(value));
    const readDesign = (key,fallback) => {try {const value=JSON.parse(localStorage.getItem('tradeiq_redesign_'+key));return value||cloneDesign(fallback);}catch{return cloneDesign(fallback);}};
    const design = {dip:readDesign('dip_ui_v1',{disabled:[],days:120,op:'<',limit:300,mode:'single',changeOp:'<',change:0,lower:-3,upper:0}),chase:readDesign('chase_ui_v1',{disabled:[]}),custom:readDesign('custom_ui_v1',{mode:'all',groups:[{id:'g1',name:'资金与趋势',mode:'all',rules:[{id:'r1',name:'近10日主力流入至少7天',formula:'COUNT(main_net > 0, 10) >= 7',enabled:true},{id:'r2',name:'当日缩量',formula:'volume / AVG(volume, 5, 1) < 1',enabled:true},{id:'r3',name:'收盘高于20日线',formula:'close > MA(20)',enabled:true}]}]}),conditionOpen:{dip:true,chase:true},folds:{},marketOffset:0,marketBasis:state.date,hotFilter:'all',hotSearch:'',hotSort:'recent',editorMode:'visual',editorRule:null,editorGroup:null,undo:null,customPage:0,draftReady:false};
    const persistDesign = key => {try{localStorage.setItem('tradeiq_redesign_'+key+'_ui_v1',JSON.stringify(design[key]));design.saveError='';}catch{design.saveError='浏览器暂时无法保存，当前修改仅在本页有效。';}};
    const designId = () => 'd'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);
    const designAction = (label,key,glyph=null,extra='',primary=false) => button(label,'design-'+key,glyph,primary,extra);
    const switchDesign = (label,checked,data) => `<button class="design-switch${checked?' is-on':''}" role="switch" aria-label="${e(label)}" aria-checked="${checked}" ${data}><span></span></button>`;
    const conditionCatalog = chase => [
      ['breadth',chase?'下跌家数 <2600':'下跌家数 >2600','同日大盘条件 · 不与个股数量混算',5354],
      ['market',chase?'上证指数上涨':'上证下跌及前三日走势',chase?'上证当日涨跌幅严格大于0':'超过1.6%直接触发；不足1.6%须前三日非连涨',5354],
      ['change',chase?'个股涨幅 >5%':'当日涨跌幅','按当日收盘比较，恰好边界不符合',chase?186:2809],
      ['return','120日涨幅 <300%','收盘相对120个交易日前的累计涨幅',4810],
      ['funds','累计资金全红','3 / 5 / 10 / 20日主力净额全部 >0',725],
      ['threshold','累计净额 >0.5亿元','任一3 / 5 / 10 / 20日主力累计超过门槛',976],
      ['volume','成交量比 <1.2','当日量 / 此前5个交易日均量，不含当日',4542],
      ['ma20','收盘高于MA20','均线包含筛选日',1659],
      ['amplitude','20日平均振幅 ≥5%','近20日逐日振幅的平均值',1039],
      ...(chase?[['unsealed','收盘未涨停','盘中触板后打开仍符合',5108]]:[]),
      ['aligned','均线多头排列','收盘 > MA5 > MA10 > MA20 > MA60',349]
    ];
    function designCountBadge(count,key='') {return `<span class="design-count" title="独立核验全市场；不是最终入选数。${key==='funds'?'资金缺失示例112只，不计入符合数。':''}">${state.preview==='loading'?'待统计':state.preview==='error'?'读取失败':state.preview==='missing'&&key==='funds'?'数据不足':count===0?'全军覆没':count.toLocaleString()+'个符合'}</span>`;}
    function conditionPanel(chase=false) {
      const key=chase?'chase':'dip',config=design[key],catalog=conditionCatalog(chase),enabled=catalog.length-config.disabled.length;
      return `<section class="view-panel design-conditions"><div class="design-section-head"><div><h2>${strategyNames[key]} · 条件设置 ${chase?tag('试验策略'):''}</h2><span class="view-subtle">已开启 ${enabled} / ${catalog.length} 项 · 各项独立核验</span></div><div class="design-head-actions">${designAction('恢复全部开启','reset-conditions','refresh',`data-kind="${key}"`)}<button class="text-button" data-ui-action="design-fold-conditions" data-kind="${key}" aria-expanded="${design.conditionOpen[key]}">${design.conditionOpen[key]?'收起':'展开'}条件${icon(design.conditionOpen[key]?'up':'down')}</button></div></div>${design.conditionOpen[key]?`<div class="design-condition-grid" role="group" aria-label="${strategyNames[key]}条件">${catalog.map(([id,label,desc,count])=>{const on=!config.disabled.includes(id);if(!chase&&id==='change')label=config.mode==='range'?`${config.lower}% < 当日涨跌幅 < ${config.upper}%`:`当日涨跌幅 ${config.changeOp} ${config.change}%`;if(!chase&&id==='return')label=`${config.days}日涨幅 ${config.op} ${config.limit}%`;return `<div class="design-condition${on?'':' is-off'}"><div class="design-condition-top">${switchDesign(label,on,`data-ui-action="design-toggle-condition" data-kind="${key}" data-condition="${id}"`)}<strong>${e(label)}</strong></div><p>${e(desc)}</p><div class="design-condition-foot">${designCountBadge(count,id)}<span>${on?'开启':'已关闭'}</span></div></div>`;}).join('')}</div>${!chase?`<div class="design-parameters"><div><span class="design-param-label">累计涨幅</span><input id="designDipDays" type="number" min="1" max="250" value="${config.days}" aria-label="累计涨幅交易日数"><span>交易日</span><select id="designDipOp" aria-label="累计涨幅比较方向">${['<','≤','>','≥'].map(v=>`<option ${config.op===v?'selected':''}>${e(v)}</option>`).join('')}</select><input id="designDipLimit" type="number" value="${config.limit}" aria-label="累计涨幅百分比"><span>%</span></div><div><span class="design-param-label">当日涨跌幅</span><select id="designDipMode" aria-label="当日涨跌幅模式"><option value="single" ${config.mode==='single'?'selected':''}>单项比较</option><option value="range" ${config.mode==='range'?'selected':''}>严格区间</option></select>${config.mode==='single'?`<select id="designChangeOp" aria-label="当日比较方向">${['<','≤','>','≥'].map(v=>`<option ${config.changeOp===v?'selected':''}>${e(v)}</option>`).join('')}</select><input id="designChangeValue" type="number" value="${config.change}" aria-label="当日涨跌幅百分比"><span>%</span>`:`<input id="designRangeLower" type="number" value="${config.lower}" aria-label="涨跌幅下限"><span>% 至</span><input id="designRangeUpper" type="number" value="${config.upper}" aria-label="涨跌幅上限"><span>%</span>`}</div>${designAction('应用参数','apply-dip','check')}<span id="designParamError" class="form-error" role="alert" hidden></span></div>`:''}<div class="design-panel-note">数量标签单独统计全市场，不受开关或组合名单影响。${chase?'大盘与个股条件独立；各条件均使用当前追高口径。':'恰好跌−1.6%不触发上证条件；区间上下限不包含边界。'}</div>`:''}${design.saveError?note(e(design.saveError)):''}</section>`;
    }
    function designFold(key,title,content,extra='') {const opened=!!design.folds[key];return `<section class="view-panel design-fold"><button class="design-fold-toggle" data-ui-action="design-fold" data-fold="${key}" aria-expanded="${opened}"><span>${title}</span><span>${extra}${icon(opened?'up':'down')}</span></button>${opened?`<div class="design-fold-content">${content}</div>`:''}</section>`;}
    function designMarket() {
      if(design.marketBasis!==state.date){design.marketBasis=state.date;design.marketOffset=0;}
      const offset=design.marketOffset,viewDate=dates[dates.indexOf(state.date)+offset],missing=state.preview==='missing'&&offset>0,chase=state.strategy==='chase';
      return `<section class="view-panel design-market"><div class="design-section-head"><div class="design-market-date"><h2>市场温度</h2><div class="date-navigation"><button class="icon-button" data-ui-action="design-market-shift" data-delta="-1" aria-label="前一日市场温度" ${offset===0?'disabled':''}>${icon('left')}</button><span class="mono">${shortDate(viewDate)} · ${offset?'T+'+offset:'入选日'}</span><button class="icon-button" data-ui-action="design-market-shift" data-delta="1" aria-label="后一日市场温度" ${offset>=5||!dates[dates.indexOf(state.date)+offset+1]?'disabled':''}>${icon('right')}</button></div></div><div class="design-market-counts">${missing?'所选日市场记录缺失':`<span>上涨 <b class="up">${chase?'3,138':'2,397'}</b></span><span>下跌 <b class="down">${chase?'2,083':'2,814'}</b></span><span>平盘 <b>143</b></span>`}<button class="text-button" data-ui-action="design-fold" data-fold="market" aria-expanded="${!!design.folds.market}">${design.folds.market?'收起':'展开'}指数${icon('down')}</button></div></div><div class="design-breadth"><i style="width:${chase?58:45}%"></i><i style="width:${chase?39:52}%"></i><i style="width:3%"></i></div>${missing?note('当前查看日缺数据，保留日期，不替换其他日期。'):design.folds.market?`<div class="design-market-charts">${indices.map((item,i)=>`<div><div class="panel-title"><h3>${item.name}</h3><span class="view-subtle">${shortDate(viewDate)} · 分时示例</span></div>${plot(i+offset,{height:100,label:item.name})}</div>`).join('')}</div>`:''}<div class="design-panel-note">市场日期仅用于查看背景，名单仍按 ${shortDate(state.date)} 入选日。${chase?'上证 +0.64% · 下跌家数少于2600，示例大盘通过。':'上证 −0.68% · 前三日非连续上涨，示例大盘通过。'}</div></section>`;
    }
    const hotSamples = [
      ['机器人',[2,4,1,3,2,6,5,4,7,8],20,0],['大消费',[1,3,4,2,5,4,5,6,5,6],19,1],['医药医疗',[0,1,2,1,3,4,3,3,4,5],14,2],['PCB',[3,6,5,8,7,8,7,5,3,0],16,3],['AI应用',[0,2,3,4,6,6,4,3,2,1],12,4],['电池产业链',[0,0,1,0,0,0,0,0,4,5],9,5],['低空经济',[0,0,0,0,0,0,0,0,0,4],4,6],['半导体',[1,0,2,0,1,0,2,0,1,2],5,7]
    ];
    function hotData(){return hotSamples.map(([name,values,unique,seed])=>{const recent=values.slice(-5),total=recent.reduce((a,b)=>a+b,0),active=recent.filter(v=>v>0).length,today=values.at(-1),yesterday=values.at(-2),candidate=total>=10&&active>=3,stage=candidate?(today===0||(today<yesterday&&today<recent.slice(0,4).reduce((a,b)=>a+b,0)/4)?'已经降温':'持续活跃'):recent.slice(0,3).every(v=>v===0)&&recent[3]>=3&&recent[4]>=3&&recent[3]+recent[4]>=8?'新近热点':recent.slice(0,4).every(v=>v===0)&&today>=3?'今日异动':today?'一般活跃':'暂无热度';return {name,values,total,active,today,yesterday,delta:today-yesterday,unique,seed,stage};}).sort((a,b)=>b.total-a.total||b.active-a.active||b.today-a.today).map((item,i)=>({...item,rank:i+1}));}
    function hotDates(date=state.date){const end=new Date(`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}T12:00:00`),list=[];while(list.length<10){if(end.getDay()!==0&&end.getDay()!==6)list.unshift(`${end.getMonth()+1}`.padStart(2,'0')+'.'+`${end.getDate()}`.padStart(2,'0'));end.setDate(end.getDate()-1);}return list;}
    const heatLevel = value => value===0?0:value<=2?1:value<=5?2:value<=9?3:4;
    function hotBoard(list=hotData(),compact=false,date=state.date) {
      const labels=hotDates(date),missing=state.preview==='missing';
      return `<div class="design-heat-legend"><span>每日收录涨停数 · 冷色 → 暖色</span>${['0','1–2','3–5','6–9','10+'].map((v,i)=>`<span><i class="design-heat level-${i}" aria-hidden="true"></i>${v}</span>`).join('')}<span>虚构示例 · 近5日窗口由竖线标明</span></div><div class="design-heat-scroll"><table class="design-heat-table${compact?' compact':''}" aria-label="近5日热门榜及10日热度"><thead><tr><th>#</th><th>板块</th><th>近5日累计</th><th>活跃天数</th>${labels.map((date,i)=>`<th class="${i===5?'window-start':''}">${date}</th>`).join('')}<th>当前状态</th></tr></thead><tbody>${list.map(row=>`<tr><td class="mono muted">${String(row.rank).padStart(2,'0')}</td><td><button class="row-action" data-ui-action="sector-open" data-sector="${e(row.name)}">${e(row.name)}${icon('arrow')}</button></td><td><b class="mono">${missing?'—':row.total}</b><span class="muted"> 次</span></td><td class="mono">${missing?'—':row.active+'/5'}</td>${row.values.map((v,i)=>`<td class="${i===5?'window-start':''}"><span class="design-heat ${missing&&i===7?'missing':`level-${heatLevel(v)}`}" title="${labels[i]} · ${row.name} · ${missing&&i===7?'缺记录':v+'只'}">${missing&&i===7?'—':v}</span></td>`).join('')}<td>${tag(missing?'数据不足':row.stage)}</td></tr>`).join('')||'<tr><td colspan="15" class="design-empty">没有匹配的板块；试试清除搜索或切换状态。</td></tr>'}</tbody></table></div><div class="design-panel-note">累计为每日收录股票数相加，同一股票跨日重复计次；去重股票数另列。缺记录留空，不按0处理。</div>`;
    }
    function designDiagnostics(custom=false) {
      const key=state.strategy,empty=state.preview==='empty',all=custom?design.custom.groups.flatMap(g=>g.rules).filter(r=>r.enabled).map(r=>[r.id,r.name]):conditionCatalog(key==='chase').filter(([id])=>!design[key].disabled.includes(id));
      return designFold('diagnostics',empty?'为什么没选出来 · 组合无交集':'条件影响诊断',`<p class="design-copy">${custom?'分别试算移除一项，保留组内/组间关系。任一满足组移除条件也可能减少名单。':'只关闭所列一项，其他条件保持。单项仍全部为0时再查看两项组合。'}试算不改变已保存设置。</p><div class="design-diagnostics-grid">${all.slice(0,4).map(([id,label],i)=>`<div><span>${e(label)}</span><b class="mono">${i===3||empty?'0':4+i*2+3}<small> 只模拟入选</small></b>${designAction('查看名单','diagnostic-preview','arrow',`data-remove="${id}" data-count="${i===3||empty?0:4+i*2+3}"`)}</div>`).join('')}</div>${!custom&&empty?designFold('pairs','两项同时关闭的示例',`<div class="design-pair-row"><span>上证走势 ＋ 均线多头</span><b class="mono">6 只</b>${designAction('查看这6只','diagnostic-preview','arrow','data-remove="market,aligned" data-count="6"')}</div>`):''}<div class="design-panel-note">缺失/异常示例：连续记录184、资金112、参考价50；数量可能重叠，不相加。模拟只检查所展示的单项和双项。</div>${custom?`<div class="design-inspect">${designAction('核验某只股票','inspect','search')}</div>`:''}`,design.folds.diagnostics?'':'查看条件影响');
    }
    function designComparison(){const item=design.comparisons?.[state.strategy];return `<span class="design-comparison">${item&&item.date===state.date?`同日对比 · 上次 ${item.before}只 → 本次 ${item.after}只 · ${item.after>item.before?'增加':item.after<item.before?'减少':'不变'}${item.after===item.before?'':Math.abs(item.after-item.before)+'只'}`:'调整条件后，可查看同日上次与本次的名单数量变化。'} · 虚构示例</span>`;}
    function designCoverage(){const key=state.strategy,disabled=design[key].disabled,values=[['funds','资金非全红',84],['threshold','净额未过门槛',42],['return','累计涨幅未过',16],['volume','量比未过',25],['ma20','未高于MA20',28],['amplitude','平均振幅不足',31],['aligned','均线非多头',154]];showDialog('筛选覆盖与缺失 · 虚构示例',metricStrip([['当日日线','5,354只','大盘与个股分开'],['可完整评估','5,120只','必要窗口核验后'],['历史记录不足','184只','不使用非连续窗口'],['行情/参考价异常','50只','必要价格无法确认']])+`<div class="design-coverage-grid">${[['资金缺失','112只'],['基础候选',key==='chase'?'186只':'2,809只'],...values.map(([id,label,count])=>[label,disabled.includes(id)?'未启用':count+'只']),...(key==='chase'?[['涨停价无法核验',disabled.includes('unsealed')?'未启用':'7只'],['收盘已涨停',disabled.includes('unsealed')?'未启用':'21只']]:[]),['资金缺失仅展示','开关免除依赖时不排除']].map(([label,value])=>`<div><span>${label}</span><strong>${value}</strong></div>`).join('')}</div>`+note('大盘未通过、大盘无法核验、个股组合无交集与必要数据缺失分别说明。各项数量可能重叠，不相加冒充总数。所有统计为UI示例。')+dialogActions('关闭','dialog-close'),true);}

    function renderDesignStrategy(chase) {
      const key=chase?'chase':'dip',disabled=design[key].disabled.length,count=design.demoResultCounts?.[key]??(4+Math.min(5,disabled*2)),list=withOutcomes(rows().slice(0,count).map(row=>({...row,change:chase?5.4+row.rank*.24:row.change,ma60:row.price*.90})));
      ui.visibleStocks=filterRows(list);
      if(state.preview==='empty')design.folds.diagnostics=true;
      const columns=[...stockCols,c('price','收盘价','price'),c('change','当日涨幅','pct'),c('increase120',`${chase?120:design.dip.days}日涨幅`,'pct'),c('vol5','5日均量比'),c('ma5','MA5','price'),c('ma10','MA10','price'),c('ma20','MA20','price'),c('ma60','MA60','price'),c('amplitude','20日平均振幅','pct'),c('main','当日主力','money'),c('sum3','3日主力','money',r=>pctMoney(r.main*3)),c('sum5','5日主力','money',r=>pctMoney(r.main*5)),c('sum10','10日主力','money',r=>pctMoney(r.main*10)),c('amount20','20日主力','money'),...outcomeCols()];
      return conditionPanel(chase)+designMarket()+designFold('hot','当前背景日 · 近5日热门榜',hotBoard(hotData().filter(r=>r.stage!=='一般活跃'),true,dates[dates.indexOf(state.date)+design.marketOffset]),'7个方向 · 示例')+designDiagnostics()+toolbar(`<span class="view-subtle">按 ${shortDate(state.date)} 收盘筛选 · 开关与参数仅演示</span>${designComparison()}`,designAction('筛选覆盖','coverage','info')+designAction('刷新结果','refresh','refresh'))+table(strategyNames[key]+' · 入选名单',list,columns,{minWidth:2240,id:'design-strategy'})+note('T+1～T+5：上行第N日最高累计、下行收盘累计，均相对入选日收盘；未来仅供回看。均线含当日，5日均量不含当日。全部股票与数值为虚构示例。');
    }
    const pctMoney = n => `<span class="${tone(n)}">${n>=0?'+':''}${n.toFixed(2)}<span class="muted"> 亿</span></span>`;
    function customDesignRows() {const rules=design.custom.groups.flatMap(g=>g.rules).filter(r=>r.enabled);if(!rules.length)return [];return withOutcomes(rows().slice(0,design.demoResultCounts?.custom??(design.custom.mode==='any'?12:6)).map(r=>({...r,check:'已核对 · 示例'})));}
    function renderDesignCustom() {
      const config=design.custom,allRules=config.groups.flatMap(g=>g.rules),enabled=allRules.filter(r=>r.enabled).length;
      const list=filterRows(customDesignRows()),maxPage=Math.max(0,Math.ceil(list.length/5)-1);
      design.customPage=Math.min(design.customPage,maxPage);ui.visibleStocks=list;
      const open=design.customOpen!==false,relation=config.mode==='all'?'全部满足 · 且':'任一满足 · 或';
      const groups=config.groups.map((g,gi)=>`<section class="custom-config-group" aria-label="${e(g.name)}条件组">
        <header class="custom-group-heading"><span class="custom-group-index mono">${String(gi+1).padStart(2,'0')}</span><input id="customGroupName-${g.id}" class="design-group-name" data-design-group-name="${g.id}" value="${e(g.name)}" aria-label="${e(g.name)}名称"><span class="custom-group-count">${g.rules.filter(r=>r.enabled).length}/${g.rules.length}项开启</span><div class="custom-group-actions"><label class="view-label">组内<select class="view-select" data-design-group-mode="${g.id}" aria-label="${e(g.name)}组内关系"><option value="all" ${g.mode==='all'?'selected':''}>全部满足 · 且</option><option value="any" ${g.mode==='any'?'selected':''}>任一满足 · 或</option></select></label><button class="icon-button" data-ui-action="design-edit-rule" data-group="${g.id}" title="向${e(g.name)}添加条件" aria-label="向${e(g.name)}添加条件">${icon('add')}</button><button class="icon-button" data-ui-action="design-remove-group" data-group="${g.id}" title="移除${e(g.name)}" aria-label="移除${e(g.name)}">${icon('delete')}</button></div></header>
        ${g.rules.map((r,i)=>`<div class="custom-rule-row${r.enabled?'':' is-off'}">${switchDesign(r.name,r.enabled,`data-ui-action="design-toggle-rule" data-rule="${r.id}"`)}<div class="custom-rule-description"><strong>${e(r.name)}</strong>${!r.enabled?'<span class="custom-off-label">已关闭</span>':''}${design.customFormulas?`<code>${e(r.formula)}</code>`:''}</div><div class="custom-rule-count">${designCountBadge([1354,3821,1649][i%3],r.formula.includes('main_net')?'funds':'')}</div><div class="custom-rule-actions"><button class="icon-button" data-ui-action="design-edit-rule" data-rule="${r.id}" data-group="${g.id}" title="编辑${e(r.name)}" aria-label="编辑${e(r.name)}">${icon('settings')}</button><button class="icon-button" data-ui-action="design-remove-rule" data-rule="${r.id}" data-group="${g.id}" title="移除${e(r.name)}" aria-label="移除${e(r.name)}">${icon('delete')}</button></div></div>`).join('')||`<div class="custom-empty-group">这个组还没有条件${designAction('添加第一项','edit-rule','add',`data-group="${g.id}"`)}</div>`}
      </section>${gi<config.groups.length-1?`<div class="custom-group-connector"><span>${config.mode==='all'?'且 · 所有组都满足':'或 · 任一组满足'}</span></div>`:''}`).join('');
      const conditions=`<section class="view-panel custom-config"><div class="custom-config-heading"><div class="custom-section-title"><h2>条件配置</h2><span class="view-subtle">${config.groups.length}组 · ${enabled}项开启</span></div><div class="custom-config-actions">${designAction('示例模板','templates','file')}${designAction('添加条件组','add-group','add')}${designAction('添加条件','edit-rule','add','',true)}<button class="text-button custom-collapse" data-ui-action="design-custom-collapse" aria-expanded="${open}" aria-controls="customConfigBody">${open?'收起':'展开'}${icon(open?'up':'down')}</button></div></div>
        ${open?`<div id="customConfigBody"><div class="custom-config-toolbar"><label class="view-label">条件组之间<select class="view-select" data-design-custom-mode aria-label="条件组之间关系"><option value="all" ${config.mode==='all'?'selected':''}>全部满足 · 且</option><option value="any" ${config.mode==='any'?'selected':''}>任一满足 · 或</option></select></label><span class="custom-logic-explanation">${config.mode==='all'?'股票须同时通过所有条件组':'股票通过任意一个条件组即可'}</span><button class="text-button custom-formula-toggle" data-ui-action="design-custom-formulas" aria-pressed="${!!design.customFormulas}">${icon('file')}${design.customFormulas?'隐藏公式':'显示公式'}</button>${designAction('公式与指标','catalog','info')}</div><div class="custom-config-scroll" tabindex="0" role="region" aria-label="条件组配置列表">${groups||'<div class="custom-empty-group">先添加条件组，或从示例模板开始。</div>'}</div><div class="custom-config-foot"><span>数量独立核验，不代表组合入选数。</span><span>${e(design.saveError||'设置已自动保存')}</span></div></div>`:`<div class="custom-collapsed-summary">组间${relation} · ${enabled} / ${allRules.length}项开启${design.saveError?` · ${e(design.saveError)}`:''}</div>`}
      </section>${design.undo?`<div class="design-undo" role="status">已移除「${e(design.undo.name)}」${designAction('撤销','undo','history')}</div>`:''}`;
      const diagnosticsOpen=state.preview==='empty'||!!design.folds.diagnostics;
      if(diagnosticsOpen)design.folds.diagnostics=true;
      const resultActions=`<button class="text-button" data-ui-action="design-custom-diagnostics" aria-expanded="${diagnosticsOpen}" aria-controls="customDiagnostics">${icon('info')}条件影响${icon(diagnosticsOpen?'up':'down')}</button>${designAction('核验股票','inspect','search')}${search()}${designAction('刷新结果','refresh','refresh')}`;
      const diagnostics=diagnosticsOpen?`<div id="customDiagnostics" class="custom-diagnostics">${designDiagnostics(true)}</div>`:'';
      const tableMarkup=table('选股结果',list.slice(design.customPage*5,design.customPage*5+5),[...stockCols,c('price','收盘价','price'),c('change','当日涨幅','pct'),c('main','当日主力','money'),...outcomeCols(),c('check','条件核验','text',r=>designAction('逐项核验','inspect','info',`data-code="${r.code}"`))],{id:'design-custom',minWidth:1320,searchable:false,actions:resultActions,totalCount:list.length});
      return `${conditions}<div class="custom-results">${tableMarkup.replace('<div class="view-table-scroll"',diagnostics+'<div class="view-table-scroll"').replace('<div class="view-empty"',diagnostics+'<div class="view-empty"')}<div class="design-pagination"><span>${state.preview==='empty'?0:list.length}条虚构结果 · 第${design.customPage+1}/${maxPage+1}页</span><button class="button" data-ui-action="design-page" data-delta="-1" ${design.customPage===0?'disabled':''}>上一页</button><button class="button" data-ui-action="design-page" data-delta="1" ${design.customPage>=maxPage?'disabled':''}>下一页</button></div></div>${note('虚构数据 · T列上行为第N日最高累计，下行为收盘累计；均以入选日收盘为基准。')}`;
    }
    function designTemplatesDialog() {
      ui.dialog='design-templates';
      showDialog('选择示例模板',`<p class="design-copy">加入一个条件组，再按自己的思路调整。现有条件会保留。</p><div class="custom-template-list">${[['缩量站上均线','量价 · 2项条件','当日缩量、收盘高于20日线'],['资金持续流入','资金 · 2项条件','近10日主力流入至少7天、5日主力强度≥5%'],['上涨未涨停','趋势 · 3项条件','当日涨幅>5%、收盘未涨停、均线多头']].map(([name,label,description],i)=>`<button data-ui-action="design-template" data-template="${i}"><span class="custom-template-icon">${icon('file')}</span><span><strong>${name}</strong><small>${label}</small><p>${description}</p></span><span class="custom-template-add">${icon('add')}加入</span></button>`).join('')}</div><div class="dialog-actions"><span class="view-subtle">仅包含虚构示例条件</span>${button('关闭','dialog-close',null)}</div>`);
    }

    function renderHotSectors() {
      design.foldViews||={};design.folds=design.foldViews.sectors||=({});
      const all=hotData(),row=all.find(r=>r.name===ui.sector);
      if(ui.sector){if(!row)return designAction('返回热门板块','hot-back','left')+note('此板块不在当前虚构样例中，可返回榜单查看已提供的方向。');return renderHotDetail(row);}
      if(['loading','error','empty'].includes(state.preview))return table('热门板块 · 示例状态',[],[c('name','板块','text')],{id:'hot-state',searchable:false});
      let list=all.filter(r=>(!design.hotSearch||r.name.toLowerCase().includes(design.hotSearch.toLowerCase()))&&(design.hotFilter==='all'||(design.hotFilter==='cooling'?r.stage==='已经降温':['持续活跃','新近热点','今日异动'].includes(r.stage))));
      const summaries=[['持续活跃','持续活跃','近5日反复出现，今天仍有涨停群体'],['新近热点','新近热点','此前安静，近两日集中出现'],['已经降温','已经降温','保留近期热门、当天已经退潮的方向']];
      const board=list.filter(r=>r.stage!=='一般活跃');const order=design.hotSort;list.sort((a,b)=>order==='today'?b.today-a.today:order==='warming'?b.delta-a.delta:order==='cooling'?a.delta-b.delta:order==='persistent'?b.active-a.active:b.total-a.total);
      return `<div class="design-hot-intro"><h2>近期哪些板块反复走热？</h2><p>先比较近5日的持续性，再看今天的变化。全部为虚构示例。</p></div><div class="design-hot-summary">${summaries.map(([title,stage,desc])=>`<section class="view-panel"><div class="design-summary-label">${tag(title)}<span>${state.preview==='missing'?'—':all.filter(r=>r.stage===stage).length+'个'}</span></div><div class="design-summary-names">${state.preview==='missing'?'数据不足':all.filter(r=>r.stage===stage).map(r=>`<button data-ui-action="sector-open" data-sector="${r.name}">${r.name}</button>`).join(' · ')||'暂无符合方向'}</div><p>${desc}</p></section>`).join('')}</div><section class="view-panel design-hot-board"><div class="design-section-head"><h2>近5日热门榜</h2><div class="design-head-actions"><label class="search-field">${icon('search')}<input id="designHotSearch" placeholder="搜索板块" type="search" aria-label="搜索热门板块" value="${e(design.hotSearch)}"></label><div class="period-pills" role="group" aria-label="近期热度状态">${[['all','全部'],['active','仍在活跃'],['cooling','已经降温']].map(([k,l])=>`<button data-ui-action="design-hot-filter" data-filter="${k}" class="${design.hotFilter===k?'active':''}" aria-pressed="${design.hotFilter===k}">${l}</button>`).join('')}</div></div></div>${hotBoard(board)}</section>${designFold('hot-rules','状态与热度怎么算',`<p class="design-copy">近5日累计≥10次且出现≥3天是近期热门候选；今天为0，或同时低于昨日及此前4日平均，标为降温。前三日为0、近2日各≥3且合计≥8为新近热点；此前4日为0、今天≥3为今日异动。缺记录不判状态，阈值只描述热度。</p>`)}${designFold('hot-market','所选日市场背景',metricStrip([['上涨家数','2,397','虚构所选日记录'],['下跌家数','2,814','与顶部日期一致'],['平盘家数','143','缺记录不填0'],['上证涨跌','−0.68%','收盘市场背景','down']]))}<div class="design-section-head"><div><h2>板块里的龙一、龙二、龙三</h2><span class="view-subtle">近5日出现过的全部板块 · 自动判断与昨日跟踪</span></div><label class="view-label">排序<select class="view-select" id="designHotSort" aria-label="热门板块排序">${[['recent','近5日热度'],['today','当日热度'],['warming','升温最多'],['cooling','降温最多'],['persistent','近5日持续性']].map(([k,l])=>`<option value="${k}" ${design.hotSort===k?'selected':''}>${l}</option>`).join('')}</select></label></div><div class="design-hot-cards">${list.map(r=>hotLeaderCard(r)).join('')||'<div class="design-empty">没有匹配的板块</div>'}</div>`;
    }
    function hotLeaderRows(row){return rows().slice(row.seed,row.seed+Math.min(3,row.today)).map((s,i)=>({...s,streak:Math.max(1,4-i),change:9.96+i*.04,role:'龙'+['一','二','三'][i],sameTimes:5-i,sector:row.name,rankMove:i===0?'昨日龙二 → 今日龙一':i===1?(row.seed===0?'新进入前三':'昨日龙一 → 今日龙二'):'新进入前三'}));}
    function hotLeaderCard(row){const leaders=hotLeaderRows(row),prior=rows()[(row.seed+(row.today===0||row.seed===0?4:1))%stocks.length];return `<section class="view-panel design-hot-card"><div class="design-section-head"><h3><button class="row-action" data-ui-action="sector-open" data-sector="${row.name}">${row.name}${icon('arrow')}</button></h3>${tag(state.preview==='missing'?'数据不足':row.stage)}</div><div class="design-hot-metrics"><span>今日<b>${row.today}<small>只</small></b></span><span>5日累计<b>${state.preview==='missing'?'—':row.total}<small>次</small></b></span><span>出现<b>${state.preview==='missing'?'—':row.active}<small>/5天</small></b></span><span>去重股票<b>${state.preview==='missing'?'—':row.unique}<small>只</small></b></span></div><div class="design-leaders">${leaders.map((s,i)=>`<div><span class="design-leader-rank">${s.role}</span>${stockButton(s)}<span>${state.preview==='missing'&&i===2?'待确认':s.streak+'连板'}<small>${s.rankMove}</small></span></div>`).join('')||'<p class="design-copy">当日无可确认龙头，保留历史跟踪。</p>'}${leaders.length>0&&leaders.length<3?'<p class="design-panel-note">仅有'+leaders.length+'只可确认候选，不补齐龙三。</p>':''}</div><div class="design-yesterday"><span>昨日龙一</span><button data-ui-stock="${prior.code}">${prior.name}</button>${tag(row.today===0?'断板':row.seed===0?'断板':'继续涨停')}<span class="view-subtle">${row.today===0?'今天不在涨停名单，仍保留跟踪':'按所选日收盘核验 · 示例'}</span></div><div class="design-hot-card-foot"><span>较昨日 ${row.delta>=0?'+':''}${row.delta}只</span>${designAction('排名依据','leader-basis','info',`data-sector="${row.name}"`)}</div></section>`;}
    function renderHotDetail(row) {
      const labels=hotDates(),today=Array.from({length:row.today},(_,i)=>({...rows()[(row.seed+i)%stocks.length],change:9.98,sector:row.name,streak:Math.max(1,4-i),status:state.preview==='missing'&&i===2?'待确认':'已核验'})),history=labels.flatMap((date,i)=>state.preview==='missing'&&i===7?[]:Array.from({length:row.values[i]},(_,j)=>({...rows()[(row.seed+j)%stocks.length],date:'2026.'+date,sector:row.name,status:'拆解收录'}))).reverse();ui.visibleStocks=today.length?today:rows().slice(row.seed,row.seed+3);
      const svg=`<svg viewBox="0 0 800 170" class="design-hot-chart" role="img" aria-label="${row.name}近10日涨停数量示例"><path d="M30 30H775M30 85H775M30 140H775" stroke="var(--chart-grid)" fill="none"/>${row.values.map((v,i)=>`${state.preview==='missing'&&i===7?'':`<rect x="${45+i*72}" y="${140-v*10}" width="32" height="${v*10}" rx="2" fill="var(--chart-secondary)"/>`}<text x="${61+i*72}" y="${Math.max(18,130-v*10)}" text-anchor="middle" fill="var(--secondary)" font-size="11">${state.preview==='missing'&&i===7?'—':v}</text><text x="${61+i*72}" y="160" text-anchor="middle" fill="var(--muted)" font-size="10">${labels[i]}</text>`).join('')}</svg>`;
      return `<div class="sector-detail-top">${designAction('返回热门板块','hot-back','left')}<h2>${row.name}</h2>${tag(state.preview==='missing'?'数据不足':row.stage)}<span class="view-subtle">${shortDate(state.date)} · 本地拆解口径 / 虚构示例</span></div>${metricStrip([['当日涨停',row.today+'只','按板块/股票去重'],['近5日累计',(state.preview==='missing'?'—':row.total)+'次','同一股票跨日期会重复计次'],['活跃天数',(state.preview==='missing'?'—':row.active)+'/5','窗口完整才判断状态'],['去重股票',(state.preview==='missing'?'—':row.unique)+'只','与累计次数分开']])}${panel('近10交易日涨停数量',svg)}${table('当日板块涨停记录',today,[...stockCols,c('streak','实际连板'),c('sealTime','涨停时间','text'),c('amount','成交额 / 亿'),c('change','收盘涨幅','pct'),c('status','核验状态','tag')],{minWidth:1080,id:'hot-today',searchable:false})}${table('近10交易日拆解明细',history,[c('date','日期','text'),c('name','股票','stock'),c('sector','归属板块','tag'),c('streak','连板'),c('sealTime','涨停时间','text'),c('status','记录','tag')],{minWidth:1080,id:'hot-history',searchable:false})}${note('收录范围为本地涨停拆解，缺记录不补0。自动排名：连板数 → 5日同板块次数 → 涨停时间 → 成交额。全部为虚构UI示例。')}`;
    }

    function renderPicks() {
      design.foldViews||={};design.folds=design.foldViews[state.source+':'+state.strategy]||=( {} );
      if(state.source==='em'&&['dip','chase'].includes(state.strategy))return renderDesignStrategy(state.strategy==='chase');
      if(state.source==='em'&&state.strategy==='custom')return renderDesignCustom();
      const ths=state.source==='ths',strategy=state.source==='user'?'user':state.strategy;
      let list=withOutcomes(rows());
      if(strategy==='combined')list=list.filter((_,i)=>i%2===0);
      if(strategy==='surge')list=list.slice(0,9);
      if(strategy==='user')list=[...ui.userPickDates.values()].map(pick=>({...withOutcomes(rows()).find(row=>row.code===pick.code),...pick}));
      const cumulative=[c('main','3日累计','money'),c('super','5日累计','money'),c('large','10日累计','money'),...(ths?[]:[c('amount20','20日累计','money')])];
      const middleSmall=ths?[]:[c('bucket','资金档位','tag'),...[3,5,10,20].map(n=>c('midSmall'+n,`${n}日中小单口径`,'money',row=>`<span class="down">${(row.price<100?row['small'+n]:row['small'+n]+row['mid'+n]).toFixed(2)} 亿</span>`))];
      const rules={system:'展示旧版既有评分、优选分与信号，评分不是收益预测。',conditional:ths?'同花顺使用总资金净额：3/5/10日累计。':'当日下跌，保留原累计资金门槛及主力/超大单档位。股价<100元取小单，≥100元取中单＋小单。',new:'继承条件选股；收盘不低于MA10，成交量不超过前日的独立设定比例。',surge:'平均振幅≥5%、收盘>MA20，20日涨幅>15%或突破20日高点。资金仅作辅助，缺资金仍可观察。',combined:'同日系统推荐与条件选股的交集，保留对应来源字段。',user:'按股票和关注日期保留独立记录；支持手动与图片演示。'};
      let columns=[...stockCols,c('price','收盘价','price'),c('change','当日涨幅','pct')];
      if(['system','combined','user'].includes(strategy))columns.push(c('score','既有评分'),...(ths?[]:[c('edgeScore','优选分')]),c('ma5','MA5','price'),c('ma10','MA10','price'),c('ma20','MA20','price'),c('ma30','MA30','price'),c('signal','信号','tag'),c('main',ths?'当日总资金':'当日主力','money'),...cumulative,...(strategy==='combined'?middleSmall:[]),...outcomeCols());
      else if(['conditional','new'].includes(strategy))columns.push(...(strategy==='new'?[c('ma10','MA10','price'),c('ratio','成交量比','number',row=>(row.ratio*100).toFixed(1)+'%')]:[]),...middleSmall,...cumulative,...outcomeCols());
      else if(strategy==='surge')columns.push(c('ma20','MA20','price'),c('amplitude','20日平均振幅','pct'),c('rise20','20日涨幅','pct'),c('momentum','动能条件','tag'),c('strength','5日主力强度','pct'),c('fundTag','资金辅助','text',row=>tag(row.rank%4===0?'资金缺失 · 不影响观察':'5日主力≥5%')),c('highest10','10日最高涨幅','pct'),c('close10','第10日收盘涨幅','pct'));
      if(strategy==='user')columns.push(c('date','关注日期','text'),c('origin','来源','tag'),c('remove','操作','text',row=>button('移除示例','remove-pick','delete',false,`data-code="${row.code}" data-pick-date="${row.date}"`)));
      const rulesHtml=`<details class="view-panel strategy-rules"><summary>${icon('info')}${strategyNames[strategy]} · 策略规则<span class="rules-chevron">${icon('down')}</span></summary><p>${rules[strategy]}</p><span class="view-subtle">虚构名单，不执行真实策略；正式开发沿用旧接口规则与收益口径。</span></details>`;
      let top=rulesHtml;
      if(strategy==='surge')top+=note(strategy==='surge'?'冲高观察是观察池，不是买入信号；资金缺失不剔除。':'大盘触发：示例下跌2,814家，上证−0.68%，此前三日非连续上涨。')+`<details class="view-panel coverage-panel"><summary>筛选覆盖与缺失说明 · 示例</summary><div class="coverage-grid"><span>日线覆盖 <b>5,120 / 5,354</b></span><span>连续记录不足 <b>184</b></span><span>价格参考异常 <b>50</b></span><span>资金缺失 <b>${strategy==='surge'?'仅标注，不剔除':'112'}</b></span></div></details>`;
      const configurable=['conditional','new','combined'].includes(strategy);
      const params=strategy==='user'?field('origin','来源',[['all','全部'],['manual','手动添加'],['ocr','图片识别']]):(configurable?`<label class="view-label">资金门槛<input id="strategyThreshold" data-ui-number="threshold" class="view-input compact-number" type="number" min="0" step="0.5" value="${e(option('threshold','3'))}" aria-label="自定义资金门槛">亿元</label>`+(strategy==='new'?`<label class="view-label">成交量≤前日<input id="strategyVolume" data-ui-number="volumePct" class="view-input compact-number" type="number" min="1" max="100" step="1" value="${e(option('volumePct','80'))}" aria-label="条件选股新缩量比例">%</label>`:''):'')+sectorFilter();
      return top+toolbar(params,strategy==='user'?button('同步飞书示例','sync','refresh'):button('生成预览图','preview','image'))+table(`${ths?'同花顺 · ':''}${strategyNames[strategy]}`,list,columns,{minWidth:Math.max(1300,columns.length*92),actions:strategy==='combined'?button('查看交集规则','rules','info'):''})+note(strategy==='surge'?'10日最高涨幅为窗口内最高触及，第10日收盘结果另列。':'T+N的最高/收盘双行仅作示例；正式接入按每种策略原口径独立核对。');
    }
    function sectorRows() {
      const windowSize=Number(option('window','7'))||120;
      return sectorNames.map((name,i)=>({name,sector:name,count:ui.sectors==='user'?sectorMembers(name).length:12+i*3,hot:Math.round((48-i*3)*windowSize/7),change:2.86-i*.42,latest:shortDate(dates[Math.max(0,dates.indexOf(state.date)-(i*3)%5)]),latestOrder:(i*3)%5,rank:i+1})).filter(row=>ui.sectors!=='user'||ui.watch.has(row.name)&&!ui.removedSectors.has(row.name));
    }
    function renderSectors() {return renderHotSectors();}
    function render() {
      const host=$('pageContent');
      const key=viewKey(),previous=ui.renderedView;
      const focused=document.activeElement;
      const focusId=previous===key&&host.contains(focused)?focused.id:null;
      const caret=focused?.selectionStart;
      const configScroll=host.querySelector('.custom-config-scroll')?.scrollTop||0;
      const tableScrolls=[...host.querySelectorAll('[data-table-scroll]')].map(node=>({id:node.dataset.tableScroll,top:node.scrollTop,left:node.scrollLeft}));
      ui.scrolls||={};if(previous)ui.scrolls[previous]={top:host.scrollTop,tables:tableScrolls,config:configScroll};
      const saved=ui.scrolls[key]||{top:0,tables:[]};ui.renderedView=key;
      $('pageActions').innerHTML=pageActions(); $('pageEyebrow').textContent=descriptions[state.page];
      if(allowedViews[state.page]){const url=new URL(location.href);url.searchParams.set('view',ui[state.page]);history.replaceState(null,'',url);}else{const url=new URL(location.href);url.searchParams.delete('view');history.replaceState(null,'',url);}
      if(state.page==='picks'&&state.source==='em'&&state.strategy==='preferred') { host.hidden=true; return; }
      host.hidden=false;
      const renderers={index:renderIndex,leaderboard:renderLeaderboard,limitup:renderLimitup,collection:renderCollection,flow:renderFlow,positions:renderPositions,picks:renderPicks,sectors:renderSectors};
      host.innerHTML=renderers[state.page]();
      if(state.preview!=='normal'&&!host.querySelector('.view-empty')&&!host.querySelector('.missing-notice')) host.insertAdjacentHTML('afterbegin',`<div class="view-state-notice">${state.preview==='loading'?'加载中':state.preview==='empty'?'空结果':state.preview==='missing'?'部分数据缺失':'读取失败'} · 示例状态。${button('恢复示例','reset-view','refresh')}</div>`);
      host.scrollTop=saved.top;
      const configNode=host.querySelector('.custom-config-scroll');if(configNode)configNode.scrollTop=saved.config||0;
      saved.tables.forEach(position=>{const node=host.querySelector(`[data-table-scroll="${position.id}"]`);if(node){node.scrollTop=position.top;node.scrollLeft=position.left;}});
      if(focusId){const target=$(focusId);if(target&&host.contains(target)){target.focus({preventScroll:true});if(typeof caret==='number'&&target.type==='search') target.setSelectionRange(caret,caret);}}
    }
    const dialogActions = (label,action) => `<div class="dialog-actions"><span class="view-subtle">仅影响本次设计演示</span>${button('取消','dialog-close',null)}${button(label,action,'check',true)}</div>`;
    function showDialog(title,html,wide=false) {
      const dialog=$('designDialog'); dialog.classList.toggle('wide',wide); $('dialogTitle').textContent=title; $('dialogBody').innerHTML=html;
      makeDialogTablesReadOnly();
      if(!dialog.open) dialog.showModal();
    }
    function makeDialogTablesReadOnly() {
      $('dialogBody').querySelectorAll('.quick-pick').forEach(button=>{button.hidden=true;});
      $('dialogBody').querySelectorAll('[data-ui-sort]').forEach(button=>{const label=document.createElement('span');label.textContent=button.textContent;button.replaceWith(label);});
      $('dialogBody').querySelectorAll('[data-ui-action="table-columns"]').forEach(button=>{button.hidden=true;});
    }
    function closeDialog(){ ui.previewRevision=(ui.previewRevision||0)+1; $('designDialog').close(); ui.dialog=''; }
    function quickPick(code,date=state.date) {
      const added=ui.userPickDates.has(code+':'+date);
      return `<button class="quick-pick" data-ui-action="quick-pick" data-code="${e(code)}" data-pick-date="${e(date)}" ${added?'disabled':''} aria-label="${added?'已加入':'加入我的选股'}">${icon(added?'check':'add')}${added?'已加入':'加入我的选股'}</button>`;
    }
    function sampleChart(key,stock,options={}) {
      return window.TradeIQPreviewCharts.render({key,stock,date:state.date,...options});
    }
    function historyDialog() {
      ui.dialog='preferred-history';
      const n=Number(ui.historyPeriod),all=rows().flatMap((row,i)=>dates.slice(0,4).map((date,j)=>({...row,date,rank:i*4+j+1,entry:row.price,ratio:row.ratio+j*.025,gain:row.outcomes[n-1].high+j*.45,complete:!(j===3&&i%3===0)})));
      const selected=all.filter(row=>!ui.historyEnabled||row.ratio*100<=ui.historyPct),complete=selected.filter(row=>row.complete),values=complete.map(row=>row.gain).sort((a,b)=>a-b);
      const median=values.length?(values[Math.floor((values.length-1)/2)]+values[Math.ceil((values.length-1)/2)])/2:0;
      const buckets=[['≤0%',v=>v<=0],['0～3%',v=>v>0&&v<=3],['3～5%',v=>v>3&&v<=5],['5～10%',v=>v>5&&v<=10],['>10%',v=>v>10]];
      const counts=buckets.map(([label,test])=>({label,count:values.filter(test).length}));
      const bars=counts.map(({label,count})=>`<div class="distribution-row"><span>${label}</span><div class="distribution-track"><span style="width:${values.length?count/values.length*100:0}%"></span></div><b>${count} / ${values.length?(count/values.length*100).toFixed(1):'0'}%</b></div>`).join('');
      const list=selected.map(row=>({...row,gain:row.complete?row.gain:null,bucket:row.complete?counts[buckets.findIndex(([,test])=>test(row.gain))].label:'窗口未完整'}));
      showDialog('条件优选 · 历史涨幅分布',toolbar(pills('historyPeriod',[['1','T+1'],['3','T+3'],['5','T+5']]),`<label class="view-label"><input type="checkbox" data-review-history-toggle ${ui.historyEnabled?'checked':''}>独立样本缩量</label><label class="view-label">≤<input id="historyPct" type="number" class="view-input compact-number" min="1" max="100" value="${ui.historyPct}" ${ui.historyEnabled?'':'disabled'} data-review-history-pct aria-label="历史样本缩量比例">%</label>${button('刷新示例','history','refresh')}`,false)+metricStrip([['筛选样本',String(selected.length),'含未完成窗口'],['有效样本',String(complete.length),`${selected.length-complete.length}条窗口不足，未纳入分布`],['中位最高涨幅',values.length?pct(median):'—','最高触及，不是可成交收益'],['不同股票',String(new Set(complete.map(row=>row.code)).size),'同股票多次入选独立计样本']])+note(`样本范围2026.09.21～09.24 · 全部虚构。${complete.length<20?'样本不足20条，请谨慎解释示例分布。':'样本覆盖与不足原因可在下表追溯。'} 本弹窗筛选不改变入选名单。`)+panel('最高涨幅分桶 · 数量 / 百分比',`<div class="distribution-list history-buckets">${bars}</div>`)+table('逐条样本明细',list,[c('rank','#'),c('date','入选日','text'),c('name','股票','stock'),c('entry','入选收盘','price'),c('ratio','成交量比','number',row=>(row.ratio*100).toFixed(1)+'%'),c('gain',`T+${n}窗口最高`,'pct'),c('bucket','区间 / 缺失说明','text')],{searchable:false,minWidth:960,id:'history-samples'}),true);
    }
    function tradeHistory(code) {
      const item=rows().find(row=>row.code===code)||rows()[0];ui.dialog='trade-history';ui.dialogStock=item.code;
      const held=item.historyState==='现持仓'?item.shares:0,bought=Math.max(2000,held+600),sold=bought-held,realized=sold*item.cost*.04-19.8;
      const records=[{...item,date:'2026.09.08',direction:'买入',shares:1000,price:item.cost,fee:7.4,realized:null},{...item,date:'2026.09.12',direction:'买入',shares:bought-1000,price:item.cost,fee:7.8,realized:null},{...item,date:'2026.09.18',direction:'卖出',shares:sold,price:item.cost*1.04,fee:19.8,realized}].map((row,i)=>({...row,tradeId:'DEMO-'+(1201+i),amount:row.price*row.shares,post:(item.price/row.price-1)*100}));
      showDialog(`${item.name} · 持仓与成交历史`,toolbar(`<label class="view-label">切换股票<select id="historyStock" class="view-select">${rows().map(row=>`<option value="${row.code}" ${item.code===row.code?'selected':''}>${row.name} · ${row.historyState}</option>`).join('')}</select></label>`,button('重新搜索','history-search','search'),false)+note('所有本地历史股票的查询位置：包括已清仓/退市。此处仅用14只虚构股票演示。')+metricStrip([['累计买入',bought.toLocaleString()+'股','含买入费用15.20元'],['累计卖出',sold.toLocaleString()+'股','示例卖出费用19.80元'],['剩余仓位',held.toLocaleString()+'股',item.historyState],['已实现盈亏',realized.toFixed(2)+'元','示例，含费用']])+table('持仓摘要',[{...item,shares:held,profit:held*(item.price-item.cost)}],[c('name','股票','stock'),c('shares','示例股数'),c('cost','成本价','price'),c('price','现价','price'),c('profit','浮动盈亏 / 元'),c('historyState','历史状态','tag')],{searchable:false,minWidth:850,id:'history-position'})+table('全部买卖记录',records,[c('tradeId','成交编号','text'),c('date','日期','text'),c('direction','买卖方向','tag'),c('shares','股数'),c('price','成交价','price'),c('amount','成交额 / 元'),c('fee','费用 / 元'),c('realized','已实现盈亏 / 元'),c('post','成交后涨幅','pct'),c('status','应用状态','tag')],{searchable:false,minWidth:1180,id:'history-trades'}),true);
    }
    function historySearch() {
      ui.dialog='history-search';showDialog('查询个股持仓与成交历史',`<label class="search-field">${icon('search')}<input id="historySearchInput" type="search" aria-label="历史股票搜索" placeholder="名称或代码，包括已清仓 / 退市股票"></label><div id="historySearchResults"></div>`,true);renderHistorySearch();
    }
    function renderHistorySearch() {
      const text=$('historySearchInput').value.trim().toLowerCase();
      $('historySearchResults').innerHTML=table('历史股票',rows().filter(row=>`${row.name} ${row.code}`.toLowerCase().includes(text)),[c('name','股票','stock'),c('historyState','状态','tag'),c('price','参考现价','price'),c('history','操作','text',row=>button('查成交','trade-history','history',false,`data-code="${row.code}"`))],{searchable:false,minWidth:680,id:'history-search'});makeDialogTablesReadOnly();
    }
    function globalSearchDialog() {
      ui.dialog='search';showDialog('个股资金搜索',toolbar(`<label class="search-field">${icon('search')}<input id="globalSearchInput" type="search" aria-label="全局个股搜索" placeholder="名称 / 代码 / 板块"></label><label class="view-label">市场<select id="searchMarket" class="view-select">${[['all','全部市场'],['0','沪市'],['1','深市'],['2','创业板'],['3','科创板']].map(([value,label])=>`<option value="${value}" ${ui.searchMarket===value?'selected':''}>${label}</option>`).join('')}</select></label>`,button('重置','reset-search','refresh'),false)+`<div id="globalSearchResults"></div>`,true);renderGlobalSearch();
    }
    function sectorForm(kind) {
      ui.dialog=kind;
      const stockOptions=selected=>`<option value="">留空</option>${stocks.map(stock=>`<option value="${stock.code}" ${stock.code===selected?'selected':''}>${stock.name} / ${stock.code}</option>`).join('')}`;
      let fields;
      if(kind==='sector') fields=`<label class="form-field full">板块名称<input name="sector" required maxlength="30" placeholder="例如：人工智能"></label><label class="form-field full">备注<input name="note" maxlength="120" placeholder="可选，显示在板块详情"></label>${['core','peer'].map(role=>`<fieldset class="sector-slots"><legend>${role==='core'?'核心三杰':'同领域优质企业'}</legend>${[1,2,3].map(rank=>`<label class="form-field">${rank}号位置<select name="${role}${rank}">${stockOptions('')}</select></label>`).join('')}</fieldset>`).join('')}`;
      else fields=`<label class="form-field">示例股票<select name="stock">${stockOptions(stocks[0].code)}</select></label><label class="form-field">角色<select name="role"><option value="core">核心三杰</option><option value="peer">同领域优质企业</option></select></label><label class="form-field">排名 / 位置<select name="rank"><option>1</option><option>2</option><option>3</option></select></label><div class="view-note">同角色、同位置已有股票时，保存将替换该位置的示例。</div>`;
      showDialog(kind==='sector'?'添加自选板块':'添加 / 替换板块股票',`<form id="reviewSectorForm"><div class="form-grid">${fields}</div><div class="dialog-actions"><span class="view-subtle">仅改变内存中的虚构板块</span>${button('取消','dialog-close',null)}<button class="button primary" type="submit">${icon('check')}保存示例</button></div></form>`);
    }
    function sectorMembers(name) {
      if(!ui.sectorRoles.has(name)) ui.sectorRoles.set(name,stocks.slice(0,6).map((stock,i)=>({code:stock.code,role:i<3?'core':'peer',rank:i%3+1})));
      return ui.sectorRoles.get(name);
    }
    function planDialog(code) {
      const stock=rows().find(row=>row.code===code)||rows()[0];ui.dialog='plans';ui.dialogStock=stock.code;
      const plan=ui.exitPlans.get(stock.code);
      const current=plan?`<section class="plan-card current"><div class="panel-title"><h3>当前方案</h3>${tag(plan.status||'已采用')}</div><p>触发价 ${plan.price.toFixed(2)} · 减仓 ${plan.shares}股 · 回本价 ${plan.breakeven.toFixed(2)}</p>${plan.status==='持仓变化 · 已失效'?note('示例持仓变化，请调整并重新确认。'):''}${button('调整','edit-plan','settings',false,`data-code="${stock.code}"`)}${button('取消方案','cancel-plan','close',false,`data-code="${stock.code}"`)}${button('演示持仓变化','stale-plan','info',false,`data-code="${stock.code}"`)}${button('演示到价提醒','trigger-plan','info',false,`data-code="${stock.code}"`)}</section>`:note('当前没有采用方案，可在下方比较候选，或手动设定。');
      const cards=[['保守',.25,1.03],['均衡',.5,1.06],['积极',.75,1.12]].map(([style,ratio,mult],i)=>{const price=stock.price*mult,shares=Math.floor(stock.shares*ratio/100)*100;return `<section class="plan-card"><div class="panel-title"><h3>${style}方案</h3>${tag(i===2?'示例不可行':'可采用')}</div><dl><dt>触发价 / 距现价</dt><dd>${price.toFixed(2)} / +${((mult-1)*100).toFixed(1)}%</dd><dt>到价减仓</dt><dd>${shares}股</dd><dt>示例减后回本价</dt><dd>${(stock.cost*1.015).toFixed(2)}</dd></dl><p class="view-subtle">按每日涨1%的固定假设示算：约${3+i*2}个交易日；不是预测。</p>${i===2?note('示例：减后成本条件不满足，无法采用。'):button('采用此方案','adopt-plan','check',false,`data-code="${stock.code}" data-price="${price}" data-shares="${shares}"`)}</section>`;}).join('');
      showDialog(`${stock.name} · 减仓方案`,note('候选、金额和状态全部是虚构账户演示。')+current+`<div class="plan-grid">${cards}</div>`+dialogActions('手动调整','edit-plan'),true);
    }
    function previewDialog(failed=false) {
      ui.dialog='preview';
      const raw=ui.previewRaw,selected=state.page==='picks'&&state.strategy==='preferred'?ctx.preferredRows():filterRows(rows());
      const caption=raw?'涨停拆解 · 简图示例':`${state.source==='ths'?'同花顺':'东方财富'} · ${strategyNames[state.strategy]||'选股'}`;
      const content=`<div class="snapshot-preview"><div class="snapshot-header"><div class="brand-word">TradeIQ</div><div><h3>${e(caption)}</h3><span>${state.date} · 虚构示例</span></div></div><div class="snapshot-summary">当前日期与筛选的排版示例<b>${selected.length}条</b></div><table class="snapshot-table"><thead><tr><th>股票 / 代码</th><th>收盘价</th><th>5日主力强度</th><th>20日累计 / 亿</th></tr></thead><tbody>${selected.map(row=>`<tr><td><strong>${e(row.name)}</strong><span>${row.code}</span></td><td>${row.price.toFixed(2)}</td><td>${row.strength.toFixed(2)}%</td><td>${row.amount20.toFixed(2)}</td></tr>`).join('')}</tbody></table><div class="snapshot-footer">DESIGN SAMPLE · 全部数据虚构，仅供UI评审</div></div>`;
      showDialog(raw?'查看简图示例':'选股PNG预览',failed?note('示例生成失败：演示渲染异常，已保留日期和筛选。')+dialogActions('重试生成','preview-retry'):note('预览与PNG下载仅包含内置虚构数据；没有上传或发布。')+content+`<div class="dialog-actions">${button('演示失败','preview-fail','info')}${button('复制本地演示链接','copy-preview','arrow')}${button('下载示例PNG','download-preview','download',true)}${button('导出示例CSV','export','file')}</div>`,true);
    }
    async function downloadPreview() {
      const list=state.page==='picks'&&state.strategy==='preferred'?ctx.preferredRows():filterRows(rows());
      const canvas=document.createElement('canvas');canvas.width=1100;canvas.height=180+list.length*48;const draw=canvas.getContext('2d'),dark=document.documentElement.dataset.theme==='dark';
      draw.fillStyle=dark?'#171819':'#ffffff';draw.fillRect(0,0,canvas.width,canvas.height);draw.fillStyle=dark?'#edece8':'#1d1e20';draw.font='24px sans-serif';draw.fillText('TradeIQ · 虚构数据 / DESIGN SAMPLE',30,45);draw.font='17px sans-serif';draw.fillText(`${state.date} · ${strategyNames[state.strategy]||'预览'} · ${list.length}条示例`,30,82);draw.fillText('股票名称 / 代码              收盘价       5日主力强度       20日累计 / 亿',30,128);
      list.forEach((row,i)=>{const y=165+i*48;draw.font='17px sans-serif';draw.fillText(row.name+' / '+row.code,30,y);draw.fillText(row.price.toFixed(2),455,y);draw.fillText(row.strength.toFixed(2)+'%',635,y);draw.fillText(row.amount20.toFixed(2),890,y);});
      canvas.toBlob(blob=>{if(!blob){previewDialog(true);return;}const link=document.createElement('a'),url=URL.createObjectURL(blob);link.href=url;link.download=`tradeiq-sample-${state.date}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已下载虚构示例PNG');},'image/png');
    }
    function dataCenter(batch=false) {
      ui.dialog=batch?'batch-backfill':'data-center';
      const gaps=[{date:'2026.09.22',state:'可补充',why:'资金记录未完整'},{date:'2026.09.23',state:'不可补充',why:'示例来源未提供该日历史'},{date:'2026.09.24',state:'完整',why:'无需补数'}];
      showDialog(batch?'最近60天历史补入库 · 演示':'数据覆盖与补数',note('所有日期、计数与健康情况都是示例；不会发起采集或写入。')+toolbar(batch?tag('最近60天 · 条件 / 组合历史'):tag('近15个交易日缺口'),button('开始演示','job-start','refresh',true),false)+`<details class="view-panel"><summary>各数据源覆盖 · 示例</summary><div class="health-list">${[['指数与盘面','完整','8个示例交易日'],['东方财富资金','完整','20日窗口'],['同花顺资金','部分缺失','10日窗口'],['股票日线','连续不足','234条不足'],['涨停拆解','待更新','所选日期'],['持仓与成交','待核对','8只虚构持仓']].map(([label,status,count])=>`<div class="health-row">${label}${tag(status)}<span>${count}</span></div>`).join('')}</div></details>`+table('日期缺口与原因',gaps,[c('date','日期','text'),c('state','状态','tag'),c('why','原因','text')],{searchable:false,minWidth:640,id:'gaps'})+`<div class="job-status" id="jobStatus"><strong>待开始</strong><progress max="100" value="0"></progress><span>可补：1日 · 不可补：1日 · 示例覆盖5,120/5,354只</span></div><div class="dialog-actions">${button('演示失败','job-fail','info')}${button('演示部分成功','job-partial','info')}${button('关闭','dialog-close','close')}</div>`,true);
    }
    function importSelection() {
      return [...ui.importChecked].filter(index=>index<6 && !(ui.importScenario==='partial'&&index===2) && !(ui.importScenario==='stale'&&index===0&&!ui.forceReapply));
    }
    function importRequiredFields() { return ui.importKind==='trades'?[0,2,3,4,5]:[0,2,3]; }
    function importMissing() {
      return ['image','picks'].includes(ui.importKind)?[0,1].some(index=>ui.ocrValues[index]!==undefined&&!ui.ocrValues[index].trim()):importRequiredFields().some(index=>ui.importMapping[index]==='none');
    }
    function updateImportConfirm() {
      const button=$('dialogBody').querySelector('[data-ui-action="import-next"],[data-ui-action="import-complete"]');
      if(button)button.disabled=ui.importStep>1&&(importMissing()||ui.importStep===3&&!importSelection().length);
      const error=$('importRequiredError');if(error)error.hidden=!importMissing();
    }
    function importPreview(kind) {
      ui.dialog='import';ui.importKind=kind;const image=['image','picks'].includes(kind),trades=kind==='trades';
      const title={positions:'导入资金股份',trades:'导入当日成交',image:'涨停简图识别',picks:'选股图片识别'}[kind];
      const steps=`<div class="dialog-stepper">${['选择文件',image?'识别校对':'字段映射','结果确认'].map((label,i)=>`<span class="${ui.importStep===i+1?'active':''}"><i>${i+1}</i>${label}</span>`).join('<span class="step-line"></span>')}</div>`;
      const scenario=`<div class="review-state-pills">${[['valid','正常'],['missing','缺必需列'],['stale','历史失效'],['partial','部分失败']].map(([value,label])=>button(label,'import-scenario','info',false,`data-value="${value}" aria-pressed="${ui.importScenario===value}"`)).join('')}</div>`;
      let content;
      if(ui.importStep===1)content=`<div class="upload-zone" id="demoDropZone">${icon(image?'image':'upload')}<strong>${image?'选择图片，或使用示例识别':'选择CSV / Excel，或使用示例文件'}</strong><p>只显示文件名，不读取文件内容；后续预览使用虚构记录。</p><input type="file" id="demoFile" accept="${image?'image/*':'.csv,.xls,.xlsx'}" hidden>${button('选择本地文件','choose-file','file')}<span id="chosenFile">${e(ui.fileName)}</span></div>`;
      else if(ui.importStep===2) {
        const names=image?['股票名称','股票代码','识别置信状态']:trades?['股票代码','股票名称','买卖方向','成交股数','成交价格','成交编号','税费','交易日期']:['股票代码','股票名称','持仓数量','成本价','交易日期'];
        content=note(image?'示例识别结果可校对名称与代码；未执行实际OCR。':'必需字段映射后才能进入确认，成交与持仓使用独立字段。')+names.map((name,i)=>`<label class="mapping-row"><span>${name}${(image?i<2:importRequiredFields().includes(i))?' *':''}</span>${image?`<input class="view-input" data-review-ocr="${i}" aria-label="校对${name}" value="${e(ui.ocrValues[i]??(i===0?stocks[0].name:i===1?stocks[0].code:'待校对'))}">`:`<select data-review-mapping="${i}" aria-label="映射${name}"><option value="mapped" ${ui.importMapping[i]==='none'?'':'selected'}>${name}</option><option value="none" ${ui.importMapping[i]==='none'?'selected':''}>未映射</option></select>`}</label>`).join('');
      } else {
        const list=rows().slice(0,7).map((row,i)=>({...row,...(image&&i===0?{name:ui.ocrValues[0]||row.name,code:ui.ocrValues[1]||row.code}:{}),tradeNo:'DEMO-'+(1301+i),direction:i%2?'卖出':'买入',fee:8+i,status:i===6?'重复 · 跳过':ui.importScenario==='stale'&&i===0?'历史失效 · 待重应用':ui.importScenario==='partial'&&i===2?'无效代码 · 跳过':'有效'}));
        content=note(`全部为虚构示例 · ${ui.importScenario==='partial'?'5':'6'}条有效，1条重复${ui.importScenario==='partial'?'，1条无效':''}。${ui.importScenario==='stale'?'检测到已存在成交的持仓失效，可单独勾选重新应用。':''}`)+table('逐条确认',list,[c('choose','选择','text',row=>`<input type="checkbox" data-review-import-check="${row.rank-1}" aria-label="选择第${row.rank}条" ${row.rank===7||ui.importScenario==='partial'&&row.rank===3?'disabled':ui.importChecked.has(row.rank-1)?'checked':''}>`),c('name','股票','stock'),...(trades?[c('direction','买卖方向','tag'),c('tradeNo','成交编号','text')]:[]),c('shares','数量'),c('price',trades?'成交价':'成本价','price'),...(trades?[c('fee','税费 / 元')]:[]),c('date','日期','text'),c('status','结果状态','tag')],{searchable:false,minWidth:trades?1120:760,id:'import-preview'})+(ui.importScenario==='stale'?`<label class="view-label"><input id="forceReapply" type="checkbox" ${ui.forceReapply?'checked':''}>仅重新应用选中的失效示例记录</label>`:'');
      }
      const missing=importMissing();
      showDialog(title,steps+scenario+(missing&&ui.importStep>1?`<div id="importRequiredError">${note('缺少必需字段，已禁用下一步 / 导入。')}</div>`:'')+content+`<div class="dialog-actions"><span class="view-subtle">文件名日期兜底：2026.09.22 · 仅演示</span>${ui.importStep>1?button('上一步','import-back','left'):''}${button('取消','dialog-close',null)}<button class="button primary" data-ui-action="${ui.importStep===3?'import-complete':'import-next'}" ${missing&&ui.importStep>1?'disabled':''}>${ui.importStep===3?'确认示例导入':'使用示例 / 下一步'}</button></div>`,ui.importStep===3);
      updateImportConfirm();
    }
    function similarDialog() {
      ui.dialog='similar';
      const fields=[['sh','上证指数'],['amount','成交额'],['emMain','主力-东方'],['emSuper','超大单-东方'],['thsNet','总资金-同花顺'],['up','上涨'],['down','下跌'],['limitUp','涨停'],['limitDown','跌停']];
      const strict=ui.similarTolerance==='strict'?2:ui.similarTolerance==='wide'?6:4,count=ui.similarFields.size?strict:0;
      const matches=dates.slice(0,count).map((date,i)=>({date:`2026.${shortDate(date)}`,similarity:98-i*2.1,up:2400+i*82,down:2800-i*82,selected:i===ui.similarHit%Math.max(1,count)?'当前匹配':''}));
      showDialog('查找相近盘面',note('参照当前日期，以下匹配全部是虚构样例；每个资金来源独立表达。')+`<div class="form-grid">${fields.map(([key,label])=>`<label class="view-label"><input type="checkbox" data-review-sim-field="${key}" ${ui.similarFields.has(key)?'checked':''}>${label}</label>`).join('')}</div>`+toolbar(`<label class="view-label">匹配范围<select id="similarTolerance" class="view-select">${[['strict','严格'],['normal','适中'],['wide','宽松']].map(([key,label])=>`<option value="${key}" ${ui.similarTolerance===key?'selected':''}>${label}</option>`).join('')}</select></label>`,button('下一个匹配','similar-next','right',false,count?'':'disabled')+tag(matches[ui.similarHit%Math.max(1,count)]?.date||'无匹配'),false)+table('匹配样例',matches,[c('date','日期','text'),c('similarity','示例相近程度'),c('up','上涨'),c('down','下跌'),c('selected','定位','tag')],{searchable:false,minWidth:650,id:'similar'}),true);
      const selectedRow=$('dialogBody').querySelectorAll('tbody tr')[ui.similarHit%Math.max(1,count)];if(selectedRow){selectedRow.classList.add('selected');selectedRow.scrollIntoView({block:'nearest'});}
    }
    function handleExtraAction(name,target) {
      const code=target?.dataset.code||ui.dialogStock;
      if(name==='market-fund-history'){ui.dialog='market-fund-history';showDialog('沪深市场 · 每日资金净额与净占比',note('五档多选、指针和区间联动均使用虚构示例。')+sampleChart('market-funds',{code:'DEMO-MARKET',name:'沪深市场',price:100},{market:true,funds:true,range:'all'}),true);return true;}
      if(name==='quick-pick'){const date=target.dataset.pickDate||state.date;ui.userPickDates.set(code+':'+date,{code,date,origin:'manual'});ui.userPicks.add(code);render();ctx.refreshPreferred();target.disabled=true;target.innerHTML=icon('check')+'已加入';toast('已加入示例我的选股 · 股票与日期独立去重');return true;}
      if(name==='external-detail'){showDialog('东方财富详情入口',note('此处为虚构股票，不跳转真实行情。正式版沿用旧版个股链接，按真实代码打开对应详情。')+dialogActions('关闭','dialog-close'));return true;}
      if(name==='history'){historyDialog();return true;}
      if(name==='global-search'){globalSearchDialog();return true;}
      if(name==='reset-search'){ui.searchMarket='all';globalSearchDialog();return true;}
      if(name==='history-search'){historySearch();return true;}
      if(name==='trade-history'){tradeHistory(code);return true;}
      if(name==='add-sector'||name==='add-sector-stock'){sectorForm(name==='add-sector'?'sector':'sector-stock');return true;}
      if(name==='exit-plan'){planDialog(code);return true;}
      if(name==='edit-plan'){calculator(code,true);return true;}
      if(name==='adopt-plan'){const stock=rows().find(row=>row.code===code);ui.exitPlans.set(code,{price:Number(target.dataset.price),shares:Number(target.dataset.shares),breakeven:stock.cost*1.015,status:'已采用'});planDialog(code);render();return true;}
      if(['cancel-plan','stale-plan','trigger-plan'].includes(name)){if(name==='cancel-plan')ui.exitPlans.delete(code);else if(ui.exitPlans.has(code))ui.exitPlans.get(code).status=name==='stale-plan'?'持仓变化 · 已失效':'到价提醒 · 示例';planDialog(code);render();return true;}
      if(name==='save-calculator'){if(ui.dialog==='exit-plan'){const stock=rows().find(row=>row.code===ui.dialogStock);ui.exitPlans.set(ui.dialogStock,{price:Number($('calcPrice').value),shares:Number($('calcShares').value),breakeven:stock.cost*1.015,status:'已采用'});}closeDialog();render();return true;}
      if(name==='preview'||name==='image-view'){ui.dialog='preview-loading';ui.previewRaw=name==='image-view';showDialog('正在生成示例预览',note('生成中 · 仅渲染内置虚构内容。'));const revision=ui.previewRevision=(ui.previewRevision||0)+1;setTimeout(()=>{if($('designDialog').open&&ui.dialog==='preview-loading'&&revision===ui.previewRevision)previewDialog();},250);return true;}
      if(name==='preview-fail'){previewDialog(true);return true;}
      if(name==='preview-retry'){previewDialog();return true;}
      if(name==='download-preview'){downloadPreview();return true;}
      if(name==='copy-preview'){const url=new URL(location.href);url.searchParams.set('date',state.date);navigator.clipboard.writeText(url.href).then(()=>toast('已复制本地演示链接')).catch(()=>showDialog('本地演示链接',`<input class="view-input" style="width:100%" readonly value="${e(url.href)}">`+note('浏览器未允许自动复制，可选中链接手动复制。')));return true;}
      if(['data-center','sync','batch-backfill','backfill'].includes(name)){dataCenter(name==='batch-backfill');return true;}
      if(['job-start','job-fail','job-partial'].includes(name)){const job=$('jobStatus');if(!job)return true;ui.jobRevision=(ui.jobRevision||0)+1;const revision=ui.jobRevision;job.querySelector('progress').value=name==='job-start'?25:100;job.querySelector('strong').textContent=name==='job-fail'?'示例任务失败':name==='job-partial'?'部分成功 · 1日缺历史来源':'示例处理中 · 1/4';job.querySelector('span').textContent=name==='job-fail'?'示例错误：来源读取失败，可重试。':'仅演示，不运行采集。';if(name==='job-start')setTimeout(()=>{if(ui.jobRevision===revision&&job.isConnected){job.querySelector('progress').value=100;job.querySelector('strong').textContent='示例完成 · 3/4，1日不可补';job.querySelector('span').textContent='示例健康：5,120条有效 · 日期一致 · 234条记录不足，未冒充完整。';}},500);return true;}
      if(name==='import-next'){ui.importStep=Math.min(3,ui.importStep+1);importPreview(ui.importKind);return true;}
      if(name==='import-back'){ui.importStep=Math.max(1,ui.importStep-1);importPreview(ui.importKind);return true;}
      if(name==='import-scenario'){ui.importScenario=target.dataset.value;ui.importMapping={};ui.ocrValues={};ui.forceReapply=false;if(ui.importScenario==='missing'){if(['image','picks'].includes(ui.importKind))ui.ocrValues[1]='';else ui.importMapping[0]='none';}importPreview(ui.importKind);return true;}
      if(['import-positions','import-trades','import-image','import-picks'].includes(name)){ui.importStep=1;ui.importScenario='valid';ui.importMapping={};ui.ocrValues={};ui.forceReapply=false;ui.importChecked=new Set([0,1,2,3,4,5]);ui.fileName='';importPreview(name.slice(7));return true;}
      if(name==='import-complete'){const selected=importSelection();if(importMissing()||!selected.length)return true;const partial=ui.importScenario==='partial',reapply=ui.importScenario==='stale'&&selected.includes(0)?1:0,count=selected.length-reapply;ui.dialog='import-result';showDialog('示例导入与应用结果',note(`入库示例${count}笔 · ${partial?`${Math.max(0,count-1)}笔应用成功 / 1笔应用失败`:'选中有效记录已应用'} · 重复1笔${partial?'、无效1笔':''}跳过。没有执行真实入库。`)+(ui.importScenario==='stale'?note(reapply?'1笔选中的历史失效记录已演示重新应用。':'历史失效记录未选择重新应用，已保留原状态。'):'' )+dialogActions(partial?'返回修正预览':'返回工作台',partial?'import-back':'dialog-close'));return true;}
      if(name==='similar'){similarDialog();return true;}
      if(name==='similar-next'){ui.similarHit=(ui.similarHit+1)%(ui.similarTolerance==='strict'?2:ui.similarTolerance==='wide'?6:4);similarDialog();return true;}
      if(name==='remove-pick'){ui.removePickDate=target.dataset.pickDate||state.date;}
      if(name==='confirm-remove'&&ui.dialog==='remove-pick'){ui.userPickDates.delete(ui.dialogStock+':'+ui.removePickDate);if(![...ui.userPickDates.values()].some(pick=>pick.code===ui.dialogStock))ui.userPicks.delete(ui.dialogStock);closeDialog();render();return true;}
      if(name==='remove-sector-member'||name==='remove-sector'){ui.removeSector=target.dataset.sector||ui.sector;ui.removeMember=target.dataset.member;ui.dialog=name;showDialog('移除自选示例',note(name==='remove-sector'?'移除此自选板块及其演示成员？全市场板块仍保留。':'移除此角色位置的示例成员？实际自选记录不受影响。')+dialogActions('移除示例','confirm-sector-remove'));return true;}
      if(name==='confirm-sector-remove'){if(ui.dialog==='remove-sector'){ui.watch.delete(ui.removeSector);ui.removedSectors.add(ui.removeSector);ui.sectorRoles.delete(ui.removeSector);ui.sector=null;}else ui.sectorRoles.set(ui.removeSector,sectorMembers(ui.removeSector).filter(member=>member.role+member.rank!==ui.removeMember));closeDialog();render();return true;}
      return false;
    }

    function addDialog(kind) {
      ui.dialog=kind;
      const title={position:'手动添加持仓',pick:'添加我的选股',sector:'添加自选板块','sector-stock':'添加板块股票'}[kind];
      const stockField=`<label class="form-field">示例股票<select name="stock">${stocks.map(stock=>`<option value="${stock.code}">${stock.name} / ${stock.code}</option>`).join('')}</select></label>`;
      const fields=kind==='sector'?`<label class="form-field full">板块名称<input name="sector" required placeholder="例如：人工智能" maxlength="30"></label><label class="form-field full">示例成分股<select name="stock" multiple style="height:110px">${stocks.slice(0,7).map(stock=>`<option value="${stock.code}">${stock.name}</option>`).join('')}</select></label>`:stockField+`<label class="form-field">日期<input name="date" type="date" value="${state.date.slice(0,4)}-${state.date.slice(4,6)}-${state.date.slice(6,8)}" required></label>`+(kind==='position'?'<label class="form-field">持仓数量<input name="shares" type="number" min="1" step="1" value="1000" required></label><label class="form-field">成本价<input name="cost" type="number" min="0.01" step="0.01" value="30.00" required></label>':'')+'<label class="form-field full">备注<textarea name="note" placeholder="可选，仅用于当前演示"></textarea></label>';
      showDialog(title,`<form id="demoAddForm"><div class="form-grid">${fields}</div><div class="dialog-actions"><span class="view-subtle">仅添加虚构示例，不写入实际账户。</span>${button('取消','dialog-close',null)}<button class="button primary" type="submit">${icon('check')}保存示例</button></div></form>`);
    }
    function calculator(code,plan=false) {
      const stock=rows().find(row=>row.code===code)||rows()[0]; ui.dialog=plan?'exit-plan':'calculator';ui.dialogStock=stock.code;
      const content=note(`${stock.name} · 示例持仓 ${stock.shares} 股 · 成本 ${stock.cost.toFixed(2)} · 现价 ${stock.price.toFixed(2)}`)+`<div class="form-grid" style="margin-top:18px"><label class="form-field">${plan?'触发价格':'减仓价格'}<input id="calcPrice" type="number" min="0.01" step="0.01" value="${stock.price.toFixed(2)}"></label><label class="form-field">减仓股数<input id="calcShares" type="number" min="0" max="${stock.shares}" step="100" value="${Math.floor(stock.shares/200)*100}"></label></div><div class="quick-amounts"><span class="view-subtle">快捷</span>${button('减1手','calc-quick',null,false,`data-shares="${Math.min(100,stock.shares)}"`)}${[.25,.5,.75,1].map(ratio=>`<button data-ui-action="calc-quick" data-shares="${Math.floor(stock.shares*ratio/100)*100}">${ratio===1?'全部':ratio===.25?'1/4':ratio===.5?'1/2':'3/4'}</button>`).join('')}</div><div id="calcResult" class="calculator-result"></div><div id="calcError" class="form-error" hidden></div>`;
      showDialog(plan?'调整减仓方案':'减仓计算器',content+dialogActions(plan?'保存示例方案':'完成演示','save-calculator')); updateCalculator();
    }
    function updateCalculator() {
      const stock=rows().find(row=>row.code===ui.dialogStock);if(!stock)return;
      const price=Number($('calcPrice').value),shares=Number($('calcShares').value);
      const valid=Number.isFinite(price)&&price>0&&Number.isInteger(shares)&&shares>=0&&shares<=stock.shares&&(ui.dialog!=='exit-plan'||shares>0);
      $('calcError').hidden=valid;$('calcError').textContent='价格须大于0，股数须为非负整数且不超过示例持仓。';
      const fee=shares?Math.max(price*shares*.0001,5)+price*shares*.0005+(['0','3'].includes(stock.market)?price*shares*.00001:0):0,realized=(price-stock.cost)*shares-fee,remaining=(stock.price-stock.cost)*(stock.shares-shares),adjusted=stock.cost*stock.shares-price*shares;
      const before=(stock.price/stock.cost-1)*100,after=adjusted>0?(realized+remaining)/adjusted*100:null;
      const result=[['减仓前整体盈亏',pct(before)],['减仓后整体盈亏',after===null?'调整后成本≤0，比例不适用':pct(after)],['示例税费',fee.toFixed(2)+'元'],['剩余仓位',String(stock.shares-shares)+'股'],['已实现盈亏',realized.toFixed(2)+'元'],['剩余浮动盈亏',remaining.toFixed(2)+'元']];
      $('calcResult').innerHTML=result.map(([label,value])=>`<div><span>${label}</span><b>${valid?value:'—'}</b></div>`).join('')+'<p class="calc-disclaimer">虚构账户示算：含示例佣金、印花税、过户费；正式版复用旧计算口径与实际市场费用参数。</p>';
      $('dialogBody').querySelector('[data-ui-action="save-calculator"]').disabled=!valid;
    }
    function indexDialog(index=0) {
      ui.dialog='index-chart';ui.dialogStock=String(index);
      const item=indices[index],stock={code:'INDEX-SAMPLE-'+index,name:item.name,price:Number(item.value.replaceAll(',','')),hits:2};
      showDialog(`${item.name} · 指数行情`,toolbar(pills('indexChart',[['daily','日K'],['minute','分时']]),ui.indexChart==='daily'?pills('indexRange',[['all','全部历史'],['60','60日'],['20','20日']]):tag(shortDate(state.date)+'所选日'),false)+note(`示例日期${shortDate(state.date)}；正式分时核验所选日，缺失时不替换最新行情。`)+(ui.indexChart==='minute'&&state.preview==='missing'?note('所选日分时数据缺失示例，保留日期，无替代图。'):sampleChart(`index-${index}-${ui.indexChart}-${ui.indexRange}`,stock,{mode:ui.indexChart,range:ui.indexRange,funds:false,amount:true})),true);
    }
    const customRule = id => design.custom.groups.flatMap(g=>g.rules).find(r=>r.id===id);
    const customGroup = id => design.custom.groups.find(g=>g.id===id);
    const defaultDrafts = [
      ['近10日主力流入至少7天','COUNT(main_net > 0, 10) >= 7'],
      ['当日缩量','volume / AVG(volume, 5, 1) < 1'],
      ['收盘高于20日线','close > MA(20)']
    ];
    function ensureDesignGroup(){if(!design.custom.groups.length)design.custom.groups.push({id:designId(),name:'条件组1',mode:'all',rules:[]});return design.custom.groups[0];}
    function openDesignEditor(target) {
      const rule=customRule(target?.dataset.rule);
      design.editorRule=rule?.id||null;
      design.editorGroup=target?.dataset.group||ensureDesignGroup().id;
      design.editorMode=target?.dataset.mode||(rule?'formula':'visual');
      design.editorDraft={name:rule?.name||'收盘高于20日线',formula:rule?.formula||'close > MA(20)',field:'close',op:'>',right:'MA(20)',chinese:'近10日主力流入至少7天；当日缩量；收盘高于20日线'};
      Object.assign(design.editorDraft,{leftField:'close',leftCalc:'current',leftPeriod:5,leftOffset:0,rightType:'metric',rightField:'close',rightCalc:'AVG',rightPeriod:20,rightOffset:0,value:0,boolean:'True'});
      design.draftReady=false;design.draftBlocked=false;renderDesignEditor();
    }
    const designFields=[['close','收盘价 / 元'],['open','开盘价 / 元'],['high','最高价 / 元'],['low','最低价 / 元'],['change','涨跌幅 / %'],['volume','成交量'],['amount','成交额 / 亿元'],['main_net','主力净额 / 亿元'],['super_net','超大单 / 亿元'],['big_net','大单 / 亿元'],['mid_net','中单 / 亿元'],['small_net','小单 / 亿元'],['amplitude','日振幅 / %'],['up_count','上涨家数'],['down_count','下跌家数'],['flat_count','平盘家数'],['sh_change','上证涨跌幅 / %'],['limit_up','收盘涨停 / 真假']];
    const designCalcs=[['current','当前值'],['AVG','平均值 / 均线'],['SUM','合计'],['MAX','最大值'],['MIN','最小值'],['positive','大于0的天数'],['decline','连续递减'],['increase','连续递增'],['ret','累计收盘涨幅']];
    function visualOperand(prefix,title) {
      const d=design.editorDraft,reference=prefix==='right',metric=!reference||d.rightType==='metric';
      const fields=`<label class="form-field full">指标<select data-design-builder="${prefix}Field">${designFields.map(([v,l])=>`<option value="${v}" ${d[prefix+'Field']===v?'selected':''}>${l}</option>`).join('')}</select></label><label class="form-field full">计算方式<select data-design-builder="${prefix}Calc">${designCalcs.map(([v,l])=>`<option value="${v}" ${d[prefix+'Calc']===v?'selected':''}>${l}</option>`).join('')}</select></label><label class="form-field">周期 / 交易日<input data-design-builder="${prefix}Period" type="number" min="1" max="250" value="${d[prefix+'Period']}"></label><label class="form-field">向前偏移<input data-design-builder="${prefix}Offset" type="number" min="0" max="250" value="${d[prefix+'Offset']}"></label>`;
      return `<section class="custom-operand"><h3>${title}</h3><div class="form-grid">${reference?`<label class="form-field full">参照类型<select data-design-builder="rightType"><option value="value" ${d.rightType==='value'?'selected':''}>固定数值</option><option value="metric" ${d.rightType==='metric'?'selected':''}>另一个指标</option><option value="boolean" ${d.rightType==='boolean'?'selected':''}>是 / 否</option></select></label>`:''}${metric?fields:d.rightType==='boolean'?`<label class="form-field full">真假结果<select data-design-builder="boolean"><option ${d.boolean==='True'?'selected':''}>True</option><option ${d.boolean==='False'?'selected':''}>False</option></select></label>`:`<label class="form-field full">数值 / 沿用左侧单位<input data-design-builder="value" type="number" step="any" value="${d.value}"></label>`}</div></section>`;
    }

    function visualExpression(prefix) {
      const d=design.editorDraft,field=d[prefix+'Field'],calc=d[prefix+'Calc'],n=d[prefix+'Period'],offset=d[prefix+'Offset'];
      if(calc==='current')return Number(offset)>0?`REF(${field}, ${offset})`:field;
      if(calc==='ret')return `RET(${n})`;
      if(calc==='positive')return `COUNT(${field} > 0, ${n}, ${offset})`;
      if(calc==='decline'||calc==='increase')return `ALL(${field} ${calc==='decline'?'<':'>'} REF(${field}, 1), ${n}, ${offset})`;
      return `${calc}(${field}, ${n}, ${offset})`;
    }
    function visualDesignFormula(){const d=design.editorDraft;return `${visualExpression('left')} ${d.op} ${d.rightType==='metric'?visualExpression('right'):d.rightType==='boolean'?d.boolean:d.value}`;}
    function visualBuilder() {
      const d=design.editorDraft;
      return `<div class="custom-operand-grid">${visualOperand('left','判断指标')}<div class="custom-compare"><label class="form-field">比较关系<select data-design-builder="op">${[['>','大于 >'],['>=','大于等于 ≥'],['<','小于 <'],['<=','小于等于 ≤'],['==','等于 ='],['!=','不等于 ≠']].map(([v,l])=>`<option value="${v}" ${d.op===v?'selected':''}>${e(l)}</option>`).join('')}</select></label></div>${visualOperand('right','参照值')}</div><details class="custom-editor-details"><summary>查看生成的公式</summary><pre class="design-formula" id="designVisualFormula">${e(visualDesignFormula())}</pre></details>`;
    }

    function renderDesignEditor() {
      const d=design.editorDraft,mode=design.editorMode;
      ui.dialog='design-editor';
      const formula=`<label class="form-field">条件公式<textarea id="designEditorFormula" class="design-code-input" spellcheck="false">${e(d.formula)}</textarea></label><div class="design-head-actions">${designAction('预览校验反馈','validate-formula','check')}${designAction('查看字段与函数','catalog','info')}</div><div id="designFormulaStatus" class="view-subtle">待校验 · 此稿仅演示校验反馈</div>`;
      const chinese=`<label class="form-field">描述选股条件<textarea id="designEditorChinese">${e(d.chinese)}</textarea></label>${designAction('生成示例草稿','parse-draft','file')}<div id="designDraftResult">${design.draftReady?designDraftMarkup():note('生成后核对全部草稿，再加入条件组。此稿演示输入框内的三项原句。')}</div>`;
      showDialog(design.editorRule?'编辑条件':'添加选股条件',`<div class="custom-editor"><div class="view-tabs design-editor-tabs" aria-label="添加条件方式">${[['visual','点选指标'],['formula','输入公式'],['chinese','中文生成草稿']].map(([v,l])=>`<button data-ui-action="design-editor-mode" data-mode="${v}" class="${mode===v?'active':''}" aria-pressed="${mode===v}">${l}</button>`).join('')}</div><div class="form-grid custom-editor-identity">${mode!=='chinese'?`<label class="form-field">条件名称<input id="designEditorName" value="${e(d.name)}" maxlength="60"></label>`:''}<label class="form-field">目标条件组<select id="designEditorGroup">${design.custom.groups.map(g=>`<option value="${g.id}" ${g.id===design.editorGroup?'selected':''}>${e(g.name)} · ${g.mode==='all'?'全部满足':'任一满足'}</option>`).join('')}</select></label></div>${mode==='visual'?visualBuilder():mode==='formula'?formula:chinese}<div id="designEditorError" class="form-error" role="alert" hidden></div><details class="custom-editor-details"><summary>数据口径与录入说明</summary><p>窗口1～250个交易日，金额填亿元，涨幅填百分数；偏移0含当日。关闭的条件不参与组合，独立数量仍保留。中文须核对全部草稿；未识别原句会保留并阻止加入。公式和中文在此稿不执行真实筛选。</p></details><div class="dialog-actions"><span class="view-subtle">示例设置自动保存</span>${button('取消','dialog-close',null)}<button class="button primary" id="designSaveRule" data-ui-action="design-save-rule" ${mode==='chinese'&&(!design.draftReady||design.draftBlocked)?'disabled':''}>${icon('check')}${mode==='chinese'?'加入全部草稿':design.editorRule?'保存条件':'加入条件组'}</button></div></div>`,mode==='visual');
    }

    function designDraftMarkup(){return design.draftBlocked?`<div class="form-error" role="alert">未识别内容：${e(design.editorDraft.chinese)}<br>本UI稿仅演示提示中的三项原句。保留全文，阻止加入。</div>`:`<p class="design-copy">识别3项 · 目标组按“${customGroup(design.editorGroup)?.mode==='any'?'任一满足':'全部满足'}”组合。请核对全部草稿。</p><div class="design-draft-list">${defaultDrafts.map(([name,formula],i)=>`<div><strong>${i+1}. ${name}</strong><code>${e(formula)}</code></div>`).join('')}</div>`;}
    function designCatalogDialog(){showDialog('公式与指标 · 口径参考',`<div class="design-catalog">${[
      ['价格与量价','open / high / low / close（元）；change、amplitude（百分数）；volume（同口径比较）；amount（亿元）；limit_up（收盘涨停真假）'],
      ['东方财富资金','main_net / super_net / big_net / mid_net / small_net，金额统一为亿元。缺来源或窗口记录显示数据不足。'],
      ['市场状态','up_count / down_count / flat_count（家）；sh_change（上证涨跌幅百分数）。'],
      ['窗口与逻辑','SUM / AVG / MAX / MIN(expr, n, offset=0)；REF(expr, n)；MA(n)、RET(n)；COUNT / ALL / ANY(predicate, n, offset=0)；CROSS、ABS；and / or / not。']
      ].map(([l,v])=>`<div><h3>${l}</h3><p>${e(v)}</p></div>`).join('')}</div><p class="design-copy" style="margin-top:16px">只使用筛选日及此前记录；offset=1排除当日，REF引用过去。窗口1～250，累计引用不能超过250日。非法字段、未来引用与非真假表达式应拒绝；除零、缺日、价格异常显示数据不足。</p><pre class="design-formula">SUM(main_net, 5) / SUM(amount, 5) * 100 >= 5</pre><div class="dialog-actions">${designAction('返回条件编辑','editor-return','left')}${button('关闭','dialog-close',null)}</div>`,true);}
    function designDiagnosticDialog(remove,count,page=0) {
      design.diagnostic={remove,count,page};ui.dialog='design-diagnostic';
      const labels=state.strategy==='custom'?design.custom.groups.flatMap(g=>g.rules).filter(r=>remove.includes(r.id)).map(r=>r.name):conditionCatalog(state.strategy==='chase').filter(r=>remove.includes(r[0])).map(r=>r[1]);
      const list=withOutcomes(rows().slice(0,count)).map((r,i)=>({...r,failed:labels.join(' / '),reason:state.preview==='missing'&&i===2?'必要记录不足':'未通过所关闭条件'}));
      showDialog('条件诊断 · 试算名单',note(`仅演示移除「${e(labels.join('、')||'所列条件')}」后的${count}只模拟结果。当前开关尚未改变。`)+`<div class="design-diagnostic-head"><p class="design-copy">${shortDate(state.date)} · 当前策略与参数保持</p>${designAction(state.strategy==='custom'?'关闭所列条件并演示重筛':'应用此组合','apply-combination','check','',true)}</div>`+table('模拟候选与未通过项',list.slice(page*5,page*5+5),[...stockCols,c('price','收盘','price'),c('failed','所移除条件','text'),c('reason','核验说明','tag')],{id:'diagnostic',searchable:false,minWidth:1050})+`<div class="design-pagination"><span>${count}只 · 第${page+1}/${Math.max(1,Math.ceil(count/5))}页</span><button class="button" data-ui-action="design-diagnostic-page" data-delta="-1" ${page===0?'disabled':''}>上一页</button><button class="button" data-ui-action="design-diagnostic-page" data-delta="1" ${(page+1)*5>=count?'disabled':''}>下一页</button></div>`+note('试算不保存开关；只有点击上方应用才改变此UI稿的设置。缺失数量可能重叠。'),true);
    }
    function designInspection(code='EX.1001') {
      ui.dialog='design-inspect';design.inspectCode=code;
      const row=rows().find(r=>r.code===code),rules=design.custom.groups.flatMap(g=>g.rules).filter(r=>r.enabled);
      showDialog('股票逐项核验 · 虚构示例',`<label class="form-field">示例代码<input id="designInspectCode" value="${e(code)}" placeholder="EX.1001"></label><div class="kit-row" style="margin-top:12px">${designAction('核验示例','inspect-run','search')}</div><div class="design-inspection">${row?`<div>${stockButton(row)}${tag(state.preview==='missing'?'数据不足':'当前组合符合')}</div>${rules.map((r,i)=>`<div><strong>${e(r.name)}</strong>${tag(state.preview==='missing'&&i===1?'数据不足':'符合')}</div>`).join('')}`:'<div>没有该示例代码。可使用 EX.1001～EX.1014。</div>'}</div>`+note('逐项核验的表现用于评审，未读取个股行情或执行规则。')+dialogActions('关闭','dialog-close'),true);
    }
    function handleDesignAction(name,target) {
      if(!name.startsWith('design-'))return false;
      const key=name.slice(7),data=target?.dataset||{};
      design.demoResultCounts||={};
      const compares=['dip','chase'].includes(state.strategy)&&['toggle-condition','reset-conditions','apply-dip','apply-combination'].includes(key);
      const beforeCount=compares?(state.preview==='empty'?0:design.demoResultCounts[state.strategy]??(4+Math.min(5,design[state.strategy].disabled.length*2))):null;
      if(['toggle-condition','reset-conditions','apply-dip','toggle-rule','remove-rule','remove-group','undo','save-rule','template'].includes(key))delete design.demoResultCounts[state.strategy];
      if(key==='custom-collapse'){design.customOpen=design.customOpen===false;render();}
      else if(key==='custom-formulas'){design.customFormulas=!design.customFormulas;render();}
      else if(key==='templates'){designTemplatesDialog();}
      else if(key==='custom-diagnostics'){design.folds.diagnostics=!design.folds.diagnostics;render();}
      else if(key==='fold'){design.folds[data.fold]=!design.folds[data.fold];render();}
      else if(key==='fold-conditions'){design.conditionOpen[data.kind]=!design.conditionOpen[data.kind];render();}
      else if(key==='reset-conditions'){design[data.kind].disabled=[];persistDesign(data.kind);render();}
      else if(key==='toggle-condition'){const config=design[data.kind],id=data.condition;config.disabled=config.disabled.includes(id)?config.disabled.filter(v=>v!==id):[...config.disabled,id];persistDesign(data.kind);render();}
      else if(key==='apply-dip'){
        const input=id=>$(id)?.value,number=id=>input(id)===''?NaN:Number(input(id));
        const next={...design.dip,days:number('designDipDays'),op:input('designDipOp'),limit:number('designDipLimit'),mode:input('designDipMode')};
        if(next.mode==='range'){next.lower=number('designRangeLower');next.upper=number('designRangeUpper');}else{next.changeOp=input('designChangeOp');next.change=number('designChangeValue');}
        const valid=Number.isInteger(next.days)&&next.days>=1&&next.days<=250&&Number.isFinite(next.limit)&&(next.mode==='range'?Number.isFinite(next.lower)&&Number.isFinite(next.upper)&&next.lower<next.upper:Number.isFinite(next.change));
        if(!valid){$('designParamError').hidden=false;$('designParamError').textContent='交易日数须为1～250的整数；数值须有效，区间下限须小于上限。';return true;}
        design.dip=next;persistDesign('dip');render();toast('已保存UI参数示例，未运行真实筛选');
      }
      else if(key==='market-shift'){design.marketOffset=Math.max(0,Math.min(5,dates.length-1-dates.indexOf(state.date),design.marketOffset+Number(data.delta)));render();}
      else if(key==='refresh'){state.preview='normal';ctx.rerender();toast('虚构示例已刷新');}
      else if(key==='open-diagnostics'){design.folds.diagnostics=true;render();$('pageContent').querySelector('[data-fold="diagnostics"]')?.scrollIntoView({block:'center'});}
      else if(key==='diagnostic-preview'){designDiagnosticDialog(data.remove.split(','),Number(data.count));}
      else if(key==='diagnostic-page'){const d=design.diagnostic;designDiagnosticDialog(d.remove,d.count,d.page+Number(data.delta));}
      else if(key==='apply-combination'){
        if(state.strategy==='custom'){design.diagnostic.remove.forEach(id=>{const r=customRule(id);if(r)r.enabled=false;});persistDesign('custom');}
        else{const config=design[state.strategy];config.disabled=[...new Set([...config.disabled,...design.diagnostic.remove])];persistDesign(state.strategy);}
        design.demoResultCounts[state.strategy]=design.diagnostic.count;
        closeDialog();state.preview='normal';render();toast('已应用到UI示例，老版设置保持');
      }
      else if(key==='add-group'){design.customOpen=true;design.custom.groups.push({id:designId(),name:'条件组'+(design.custom.groups.length+1),mode:'all',rules:[]});persistDesign('custom');render();}
      else if(key==='toggle-rule'){const r=customRule(data.rule);if(r)r.enabled=!r.enabled;persistDesign('custom');render();}
      else if(key==='remove-rule'){const g=customGroup(data.group),i=g.rules.findIndex(r=>r.id===data.rule);design.undo={type:'rule',group:g.id,index:i,name:g.rules[i].name,value:cloneDesign(g.rules[i])};g.rules.splice(i,1);persistDesign('custom');render();}
      else if(key==='remove-group'){const i=design.custom.groups.findIndex(g=>g.id===data.group);design.undo={type:'group',index:i,name:design.custom.groups[i].name,value:cloneDesign(design.custom.groups[i])};design.custom.groups.splice(i,1);persistDesign('custom');render();}
      else if(key==='undo'){const u=design.undo;if(u?.type==='group')design.custom.groups.splice(u.index,0,u.value);else if(u){let g=customGroup(u.group);if(!g){g={id:u.group,name:'恢复的条件组',mode:'all',rules:[]};design.custom.groups.push(g);}g.rules.splice(u.index,0,u.value);}design.undo=null;persistDesign('custom');render();}
      else if(key==='edit-rule'){openDesignEditor(target);}
      else if(key==='editor-mode'){if(design.editorMode==='visual')design.editorDraft.formula=visualDesignFormula();design.editorMode=data.mode;design.draftReady=false;renderDesignEditor();}
      else if(key==='editor-return'){if(design.editorDraft)renderDesignEditor();else openDesignEditor({dataset:{mode:'formula'}});}
      else if(key==='parse-draft'){design.draftReady=true;const normalized=design.editorDraft.chinese.replace(/[；;。\s]/g,'');design.draftBlocked=normalized!==('近10日主力流入至少7天当日缩量收盘高于20日线');$('designDraftResult').innerHTML=designDraftMarkup();$('designSaveRule').disabled=design.draftBlocked;}
      else if(key==='save-rule'){
        const d=design.editorDraft,g=customGroup(design.editorGroup);if(!g)return true;
        if(design.editorMode==='chinese'){if(!design.draftReady||design.draftBlocked)return true;defaultDrafts.forEach(([name,formula])=>g.rules.push({id:designId(),name,formula,enabled:true}));}
        else{const formula=design.editorMode==='visual'?visualDesignFormula():d.formula;
          if(design.editorMode==='visual'&&['left','right'].some(p=>!Number.isInteger(Number(d[p+'Period']))||Number(d[p+'Period'])<1||Number(d[p+'Period'])>250||!Number.isInteger(Number(d[p+'Offset']))||Number(d[p+'Offset'])<0||Number(d[p+'Offset'])>250)){const error=$('designEditorError');error.hidden=false;error.textContent='周期须为1–250、偏移须为0–250的整数。';return true;}
          if(!d.name.trim()||!formula.trim()){const error=$('designEditorError');error.hidden=false;error.textContent='名称和公式不能为空。';return true;}
          const old=customRule(design.editorRule),value={id:old?.id||designId(),name:d.name.trim(),formula:formula.trim(),enabled:old?.enabled??true};
          if(old){design.custom.groups.forEach(group=>{group.rules=group.rules.filter(r=>r.id!==old.id);});}g.rules.push(value);
        }
        persistDesign('custom');design.customPage=0;closeDialog();render();toast('已保存条件样例；公式尚未接入校验与筛选');
      }
      else if(key==='template'){
        const sets=[defaultDrafts.slice(1),[defaultDrafts[0],['5日主力强度 ≥5%','SUM(main_net, 5) / SUM(amount, 5) * 100 >= 5']],[['当日涨幅 >5%','change > 5'],['收盘未涨停','not limit_up'],['均线多头','close > MA(5) and MA(5) > MA(10) and MA(10) > MA(20) and MA(20) > MA(60)']]],labels=['缩量站上均线','资金持续流入','上涨未涨停'];
        design.custom.groups.push({id:designId(),name:labels[Number(data.template)],mode:'all',rules:sets[Number(data.template)].map(([name,formula])=>({id:designId(),name,formula,enabled:true}))});persistDesign('custom');closeDialog();design.customOpen=true;render();
      }
      else if(key==='page'){design.customPage=Math.max(0,design.customPage+Number(data.delta));render();}
      else if(key==='validate-formula'){const failed=state.preview==='error'||!design.editorDraft.formula.trim();$('designFormulaStatus').textContent=failed?'校验失败示例：请检查表达式，保留全部输入。':'校验通过的UI反馈示例 · 实际公式未执行。';}
      else if(key==='catalog'){designCatalogDialog();}
      else if(key==='inspect'){designInspection(data.code);}
      else if(key==='inspect-run'){designInspection($('designInspectCode').value.trim());}
      else if(key==='coverage'){designCoverage();}
      else if(key==='hot-filter'){design.hotFilter=data.filter;render();}
      else if(key==='hot-back'){ui.sector=null;ctx.rerender();}
      else if(key==='hot-info'){showDialog('热门板块 · 统计说明',`<p class="design-copy">先看近5日累计与持续性，再看当日热度及自动龙头。累计次数与去重股票数分开。缺少某日记录显示“数据不足”，不按0处理。只描述所选日及以前的涨停拆解，不预测收益。</p>`+hotBoard(hotData().slice(0,3))+dialogActions('关闭','dialog-close'),true);}
      else if(key==='leader-basis'){const sector=hotData().find(r=>r.name===data.sector);showDialog(sector.name+' · 自动排名依据',note('当日板块涨停记录中自动判断，不手动指定；未核验价格或连板时待确认。')+table('当前前三名 · 虚构示例',hotLeaderRows(sector),[c('role','角色','tag'),c('name','股票','stock'),c('streak','实际连板'),c('sameTimes','5日同板块次数'),c('sealTime','涨停时间','text'),c('amount','成交额 / 亿'),c('rankMove','较昨日变化','text')],{id:'leader-basis',searchable:false,minWidth:1070})+note('先核验收盘涨停及连板，依次比较连板数 → 5日同板块次数 → 涨停时间 → 当日成交额；最后按代码稳定排序。昨日龙一断板仍保留。')+dialogActions('关闭','dialog-close'),true);}
      if(compares){design.comparisons||={};design.comparisons[state.strategy]={date:state.date,before:beforeCount,after:design.demoResultCounts[state.strategy]??(4+Math.min(5,design[state.strategy].disabled.length*2))};render();}
      return true;
    }
    function designInput(target) {
      if(target.dataset.designBuilder){design.editorDraft[target.dataset.designBuilder]=target.value;const output=$('designVisualFormula');if(output)output.textContent=visualDesignFormula();}
      const bindings={designEditorName:'name',designEditorFormula:'formula',designEditorChinese:'chinese'};
      if(bindings[target.id]&&design.editorDraft){design.editorDraft[bindings[target.id]]=target.value;if(target.id==='designEditorChinese'){design.draftReady=false;$('designSaveRule').disabled=true;$('designDraftResult').innerHTML=note('描述已修改，请重新生成并核对全部草稿。');}}
      if(target.id==='designHotSearch'){design.hotSearch=target.value;render();}
    }
    function designChange(target) {
      if(target.dataset.designBuilder){design.editorDraft[target.dataset.designBuilder]=target.value;if(target.dataset.designBuilder==='rightType')renderDesignEditor();else $('designVisualFormula').textContent=visualDesignFormula();}
      if(target.id==='designDipMode'){design.dip.mode=target.value;render();return;}
      if(target.hasAttribute('data-design-custom-mode')){delete design.demoResultCounts?.custom;design.custom.mode=target.value;design.customPage=0;persistDesign('custom');render();}
      if(target.hasAttribute('data-design-group-mode')){delete design.demoResultCounts?.custom;customGroup(target.dataset.designGroupMode).mode=target.value;persistDesign('custom');render();}
      if(target.hasAttribute('data-design-group-name')){customGroup(target.dataset.designGroupName).name=target.value.trim()||'未命名条件组';persistDesign('custom');render();}
      if(target.id==='designEditorGroup'){design.editorGroup=target.value;}
      const fields={designEditorField:'field',designEditorOp:'op',designEditorRight:'right'};
      if(fields[target.id]){design.editorDraft[fields[target.id]]=target.value;$('designVisualFormula').textContent=design.editorDraft.field+' '+design.editorDraft.op+' '+design.editorDraft.right;}
      if(target.id==='designHotSort'){design.hotSort=target.value;render();}
    }

    function action(name,target) {
      if(handleDesignAction(name,target))return;
      const code=target?.dataset.code,sector=target?.dataset.sector;
      if(handleExtraAction(name,target))return;
      if(name==='dialog-close'){closeDialog();return;}
      if(name==='choose-file'){$('demoFile').click();return;}
      if(name==='add-position'||name==='add-pick'){addDialog(name.slice(4));return;}
      if(name==='calculator'){calculator(code);return;}
      if(name==='calc-quick'){$('calcShares').value=target.dataset.shares;updateCalculator();return;}
      if(name==='index-chart'){indexDialog(Number(target.dataset.index));return;}
      if(name==='sector-open'){ui.sector=sector;state.page='sectors';ctx.rerender();return;}
      if(name==='sector-back'){ui.sector=null;render();return;}
      if(name==='watch-sector'){const selected=sector||ui.sector;if(ui.watch.has(selected))ui.watch.delete(selected);else{ui.watch.add(selected);ui.removedSectors.delete(selected);}render();return;}
      if(name==='remove-position'||name==='remove-pick'){
        ui.dialog=name;ui.dialogStock=code;const item=stocks.find(stock=>stock.code===code);
        showDialog('移除示例记录',note(`从当前演示移除「${e(item.name)}」？实际账户不受影响。`)+dialogActions('移除示例','confirm-remove'));return;
      }
      if(name==='confirm-remove'){ui.positionStocks.delete(ui.dialogStock);closeDialog();render();toast('示例持仓已移除，刷新可恢复');return;}
      if(name==='table-columns'){
        ui.dialog='columns';const id=target.dataset.tableId;ui.columnTable=`${viewKey()}:${id}`;
        const hidden=ui.columns[ui.columnTable]||new Set();
        showDialog('表格列设置',`<div class="form-grid">${ui.columnChoices[ui.columnTable].map(({key,label})=>`<label class="view-label"><input type="checkbox" data-ui-column="${key}" ${hidden.has(key)?'':'checked'} ${key==='name'?'disabled':''}>${label}</label>`).join('')}</div>`+dialogActions('完成','dialog-close'));return;
      }
      if(name==='reset-view'){state.preview='normal';ui.search[viewKey()]='';render();return;}
      if(name==='clear-demo'||name==='apply-trades'){
        ui.dialog=name;showDialog(name==='clear-demo'?'清空示例日志':'应用示例成交',note('仅演示完成状态，不触发实际数据修改。')+dialogActions('完成演示','demo-done'));return;
      }
      if(name==='fetch-limitup'){
        ui.dialog=name;showDialog('拉取韭研涨停简图 · 演示',`<label class="form-field">示例日期<input type="date" value="2026-09-22"></label>`+note('演示进度与结果，不发起采集。')+dialogActions('模拟处理','sync-demo'));return;
      }
      if(name==='sync-demo'){
        target.disabled=true;target.textContent='演示处理中…';setTimeout(()=>{if(target.isConnected){target.disabled=false;target.textContent='示例已更新';toast('示例已更新，未采集真实数据');}},400);return;
      }
      if(name==='rules'){showDialog('组合选股 · 交集规则',note('同日系统推荐 ∩ 同日条件选股，按数据源使用原有口径。')+dialogActions('关闭','dialog-close'));return;}
      if(name==='account-summary'){
        showDialog('账户汇总 · 虚构示例',metricStrip([['总资产','368,420','示例汇总','','元'],['持仓市值','229,880','虚构持仓','','元'],['可用资金','138,540','虚构资金','','元'],['仓位','62.4%','无真实账户连接']])+note('持仓成本：210,000元 · 累计已实现盈亏：6,240元 · 示例口径。')+panel('仓位变化',plot(2,{label:'仓位变化'})));return;
      }
      if(name==='export'){
        const tableNode=$('designDialog').open?$('dialogBody').querySelector('table'):state.page==='picks'&&state.strategy==='preferred'?$('stockTable'):$('pageContent').querySelector('table');
        const records=tableNode?[...tableNode.rows].map(row=>[...row.cells].map(cell=>cell.innerText.replace(/\s+/g,' ').trim())):[['股票名称','示例代码','板块','示例收盘价'],...filterRows(rows()).map(row=>[row.name,row.code,row.sector,row.price.toFixed(2)])];
        const quote=value=>'"'+String(value).replaceAll('"','""')+'"',csv='\ufeff'+[['TradeIQ 虚构示例导出',state.date],...records].map(row=>row.map(quote).join(',')).join('\r\n');
        const link=document.createElement('a'),blob=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));link.href=blob;link.download=`tradeiq-demo-${state.page}-${state.date}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(blob),1000);toast('已导出当前虚构示例CSV');return;
      }
      if(name==='demo-done'){closeDialog();toast('示例流程完成，实际数据未改变');}
    }

    function renderGlobalSearch() {
      const search=$('globalSearchInput').value.trim().toLowerCase();
      const found=rows().filter(row=>`${row.name} ${row.code} ${row.sector}`.toLowerCase().includes(search)&&(ui.searchMarket==='all'||row.market===ui.searchMarket));
      $('globalSearchResults').innerHTML=table('东方财富个股资金 · 五档与累计窗口',found,[c('name','股票','stock'),c('price','现价','price'),c('change','当日涨跌','pct'),c('main','主力净额','money'),c('netPct','净占比','pct'),c('super','超大单','money'),c('large','大单','money'),c('medium','中单','money'),c('small','小单','money'),c('sum3','3日累计','money',row=>`+${(row.main*3).toFixed(2)}亿`),c('sum5','5日累计','money',row=>`+${(row.main*5).toFixed(2)}亿`),c('sum10','10日累计','money',row=>`+${(row.main*10).toFixed(2)}亿`),c('amount20','20日累计','money'),c('net30','30日累计','money')],{searchable:false,minWidth:1780,id:'stock-search'});makeDialogTablesReadOnly();
    }
    document.addEventListener('click',event=>{
      const tab=event.target.closest('[data-ui-tab]');
      if(tab){ui[tab.dataset.uiTab]=tab.dataset.value;if(tab.dataset.uiTab==='indexChart'||tab.dataset.uiTab==='indexRange')indexDialog(Number(ui.dialogStock));else if(ui.dialog==='preferred-history'&&tab.dataset.uiTab==='historyPeriod')historyDialog();else render();return;}
      const sort=event.target.closest('[data-ui-sort]');if(sort){const old=ui.sort[viewKey()];ui.sort[viewKey()]={key:sort.dataset.uiSort,direction:old?.key===sort.dataset.uiSort?-old.direction:-1};render();return;}
      const legend=event.target.closest('[data-ui-legend]');if(legend){const key=legend.dataset.uiLegend;ui.legend.has(key)?ui.legend.delete(key):ui.legend.add(key);render();return;}
      const stock=event.target.closest('[data-ui-stock]');if(stock){if($('designDialog').open)closeDialog();openDetail(stock.dataset.uiStock);return;}
      const target=event.target.closest('[data-ui-action]');if(target){event.preventDefault();action(target.dataset.uiAction,target);}
    });
    document.addEventListener('input',event=>{
      const target=event.target;
      designInput(target);
      if(target.hasAttribute('data-ui-search')){ui.search[viewKey()]=target.value;render();}
      if(target.id==='globalSearchInput')renderGlobalSearch();
      if(target.hasAttribute('data-review-ocr')){ui.ocrValues[target.dataset.reviewOcr]=target.value;updateImportConfirm();}
      if(target.id==='historySearchInput')renderHistorySearch();
      if(target.hasAttribute('data-ui-number')){const value=Number(target.value),valid=target.value!==''&&Number.isFinite(value)&&value>=0&&(target.dataset.uiNumber!=='volumePct'||Number.isInteger(value)&&value>=1&&value<=100);target.setCustomValidity(valid?'':'请输入有效范围内的数值');let error=$('strategyFilterError');if(!valid){if(!error){error=document.createElement('div');error.id='strategyFilterError';error.className='form-error';target.closest('.view-toolbar').after(error);}error.hidden=false;error.textContent='门槛须为非负数，缩量比例须为1～100的整数。';}else{if(error)error.hidden=true;ui.filters[`${viewKey()}:${target.dataset.uiNumber}`]=target.value;render();}}
      if(target.id==='calcPrice'||target.id==='calcShares') updateCalculator();
    });
    document.addEventListener('change',event=>{
      const target=event.target;
      designChange(target);
      if(target.id==='historyStock'){tradeHistory(target.value);return;}
      if(target.id==='searchMarket'){ui.searchMarket=target.value;renderGlobalSearch();return;}
      if(target.hasAttribute('data-review-history-toggle')){ui.historyEnabled=target.checked;historyDialog();return;}
      if(target.hasAttribute('data-review-history-pct')){const value=Number(target.value);if(Number.isInteger(value)&&value>=1&&value<=100){ui.historyPct=value;historyDialog();}else target.reportValidity();return;}
      if(target.hasAttribute('data-review-mapping')){ui.importMapping[target.dataset.reviewMapping]=target.value;importPreview(ui.importKind);return;}
      if(target.hasAttribute('data-review-import-check')){const index=Number(target.dataset.reviewImportCheck);target.checked?ui.importChecked.add(index):ui.importChecked.delete(index);updateImportConfirm();return;}
      if(target.id==='forceReapply'){ui.forceReapply=target.checked;updateImportConfirm();return;}
      if(target.hasAttribute('data-review-sim-field')){const key=target.dataset.reviewSimField;target.checked?ui.similarFields.add(key):ui.similarFields.delete(key);ui.similarHit=0;similarDialog();return;}
      if(target.id==='similarTolerance'){ui.similarTolerance=target.value;ui.similarHit=0;similarDialog();return;}
      if(target.dataset.uiFilter){ui.filters[`${viewKey()}:${target.dataset.uiFilter}`]=target.value;render();}
      if(target.dataset.uiColumn){const hidden=ui.columns[ui.columnTable]||=new Set();target.checked?hidden.delete(target.dataset.uiColumn):hidden.add(target.dataset.uiColumn);render();}
      if(target.id==='demoFile'&&target.files[0]){ui.fileName=target.files[0].name;$('chosenFile').textContent=ui.fileName;}
    });
    document.addEventListener('submit',event=>{
      if(event.target.id==='reviewSectorForm'){event.preventDefault();const data=new FormData(event.target);if(ui.dialog==='sector'){const name=String(data.get('sector')||'').trim();if(!name)return;if(!sectorNames.includes(name))sectorNames.push(name);const members=['core','peer'].flatMap(role=>[1,2,3].filter(rank=>data.get(role+rank)).map(rank=>({role,rank,code:data.get(role+rank)})));ui.sectorRoles.set(name,members);ui.sectorNotes.set(name,String(data.get('note')||''));ui.watch.add(name);ui.removedSectors.delete(name);ui.sectors='user';ui.sector=name;}else{const code=data.get('stock');if(!code)return;const role=data.get('role'),rank=Number(data.get('rank')),members=sectorMembers(ui.sector).filter(member=>member.role!==role||member.rank!==rank);members.push({code,role,rank});ui.sectorRoles.set(ui.sector,members);}closeDialog();render();toast('示例板块已保存，实际记录未改变');return;}

      if(event.target.id!=='demoAddForm')return;event.preventDefault();const data=new FormData(event.target);
      if(ui.dialog==='sector'){const name=String(data.get('sector')||'').trim();if(!name)return;if(!sectorNames.includes(name))sectorNames.push(name);ui.sectorMembers.set(name,new Set(data.getAll('stock')));ui.watch.add(name);ui.sectors='user';ui.sector=null;}
      else if(ui.dialog==='pick'){const code=data.get('stock'),date=String(data.get('date')).replaceAll('-','');ui.userPicks.add(code);ui.userPickDates.set(code+':'+date,{code,date,origin:'manual'});}
      else if(ui.dialog==='position'){ui.positionStocks.add(data.get('stock'));ui.positionValues.set(data.get('stock'),{shares:Number(data.get('shares')),cost:Number(data.get('cost')),date:String(data.get('date')).replaceAll('-','')});}
      else if(ui.dialog==='sector-stock'){if(!ui.sectorMembers.has(ui.sector))ui.sectorMembers.set(ui.sector,new Set(rows().filter(row=>row.sector===ui.sector||row.rank%3===0).map(row=>row.code)));ui.sectorMembers.get(ui.sector).add(data.get('stock'));}
      closeDialog();render();toast('示例已保存 · 未写入真实数据');
    });
    document.addEventListener('mousemove',event=>{
      const host=event.target.closest('.chart-surface');if(!host)return;
      const bounds=host.getBoundingClientRect();const progress=Math.max(0,Math.min(1,(event.clientX-bounds.left)/bounds.width));const values=JSON.parse(host.dataset.chartValues);
      const tip=host.querySelector('.chart-tip'), guide=host.querySelector('.chart-guide');tip.hidden=false;guide.hidden=false;
      guide.style.left=`${progress*100}%`;tip.style.left=`${Math.min(bounds.width-160,Math.max(0,event.clientX-bounds.left+10))}px`;tip.style.top='10px';tip.textContent=`${host.dataset.chartLabel} / 示例 ${values[Math.round(progress*(values.length-1))]}`;
    });
    document.addEventListener('mouseout',event=>{const host=event.target.closest('.chart-surface');if(host&&!host.contains(event.relatedTarget)){host.querySelector('.chart-tip').hidden=true;host.querySelector('.chart-guide').hidden=true;}});
    document.addEventListener('dragover',event=>{if(event.target.closest('#demoDropZone'))event.preventDefault();});
    document.addEventListener('drop',event=>{if(event.target.closest('#demoDropZone')){event.preventDefault();const file=event.dataTransfer.files[0];if(file){ui.fileName=file.name;$('chosenFile').textContent=file.name;}}});
    $('closeDialog').addEventListener('click',closeDialog);
    $('designDialog').addEventListener('click',event=>{if(event.target===$('designDialog'))closeDialog();});
    $('designDialog').addEventListener('close',()=>{if(!$('designDialog').open){ui.dialog='';ui.previewRevision=(ui.previewRevision||0)+1;}});
    return {render,quickPick,simpleHome:(view='overview')=>{ui.index=view;},indexView:()=>ui.index,actions:pageActions,description:()=>descriptions[state.page],pageTitles,action,stockList:()=>state.preview==='empty'?[]:ui.visibleStocks,detailStock:code=>ui.visibleStocks.find(r=>r.code===code),resetSector:()=>{ui.sector=null;},views:allowedViews};
  }
};
