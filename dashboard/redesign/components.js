(() => {
  'use strict';

  // This catalog reuses the workbench CSS. All quotes and workflows are fictional.
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const icon = (name, small = false) => `<svg class="icon${small ? ' small' : ''}" aria-hidden="true"><use href="icons.svg#i-${name}"/></svg>`;
  const action = (name, text, glyph, extra = '', primary = false) => `<button class="button${primary ? ' primary' : ''}" data-kit-action="${name}" ${extra}>${glyph ? icon(glyph, true) : ''}${text}</button>`;
  const signed = value => `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
  const tone = value => value >= 0 ? 'up' : 'down';
  const tag = (label, kind='') => window.TradeIQPages.statusTag(label, kind);
  const sectorTag = label => window.TradeIQPages.sectorTag(label);
  const preferenceKey = 'tradeiq_redesign_components_theme';
  const themes = readThemeVariables();
  const families = [
    ['colors','foundation','语义色彩','颜色 / 分层 / 涨跌','灰阶、行情与语义点缀，共用工作台变量','点击色块查看颜色角色和数值'],
    ['typography','foundation','文字与数字','字体 / 字号 / 数字','中文界面字体与等宽数字分工','字号建立层次，数值按小数位对齐'],
    ['spacing','foundation','间距与形状','布局 / 间距 / 圆角','紧凑、清晰、有节奏的灰阶空间','4px基础节奏；控件与容器使用不同圆角'],
    ['icons','foundation','Remix 图标','图标 / 图形 / Remix','40个本地图标，统一线性语言','点击图标检查15 / 18 / 24px表现'],
    ['navigation','component','导航与切换','导航 / Tab / 来源 / 策略','侧栏、来源分段、策略下划线','点击切换，当前路径始终可见'],
    ['buttons','component','按钮与操作','按钮 / 加载 / 焦点 / 禁用','主次操作明确；状态变化可感知','普通34px、紧凑28px、图标30px'],
    ['forms','component','输入与选择','表单 / 输入 / 单选 / 开关','完整输入状态与字段校验','填写示例表单查看校验和本地预览'],
    ['metrics','component','标签与数据卡','标签 / 指标 / 卡片 / 涨跌','价格方向与系统状态分别表达','指标先显示数值，再说明单位与口径'],
    ['table','component','数据表格','表格 / 排序 / 搜索 / 列设置','固定表头和名称列，内部滚动','支持搜索、排序、显隐列与打开详情'],
    ['charts','component','价格与资金图','图表 / K线 / 分时 / 资金','灰阶曲线、方向色K线与资金柱','指针和区间同步；图形为虚构示例'],
    ['feedback','component','反馈与状态','状态 / 加载 / 空 / 缺失 / 失败','明确说明发生了什么和下一步','可演示重试、清除筛选、加载完成'],
    ['overlays','component','日期与浮层','日期 / 日历 / 弹窗 / 抽屉','日历、普通弹窗、宽弹窗及详情','可选择日期、操作导入流程、放大详情'],
    ['pattern','pattern','工作台组合','组合 / 筛选 / 推荐 / 工作台','将原子组件放入真实界面构成','示例名单仅演示UI，不代表选股结果'],
    ['business','pattern','业务功能组件','参数 / 样本 / 方案 / 导入 / 板块 / 功能补齐','本轮新增业务UI与完整图表组件','完整业务交互使用独立工作台的虚构示例']
  ].map(([id,category,name,keywords,description,note], index) => ({id,category,name,keywords,description,note,number:String(index+1).padStart(2,'0')}));
  const stocks = [
    {name:'示例科技',code:'DEMO.001',price:26.38,change:2.16,strength:8.42,volume:.76,amount:5.82,high:4.18,close:2.61},
    {name:'示例材料',code:'DEMO.002',price:18.62,change:-1.34,strength:7.61,volume:.73,amount:3.46,high:2.65,close:-.84},
    {name:'示例能源',code:'DEMO.003',price:32.75,change:.86,strength:6.84,volume:.81,amount:4.12,high:3.46,close:1.42},
    {name:'示例制造',code:'DEMO.004',price:14.29,change:-2.04,strength:6.12,volume:.79,amount:2.67,high:1.32,close:-1.06},
    {name:'示例电子',code:'DEMO.005',price:41.06,change:3.28,strength:5.96,volume:.77,amount:6.31,high:5.19,close:3.72},
    {name:'示例消费',code:'DEMO.006',price:23.14,change:-.62,strength:5.72,volume:.84,amount:1.93,high:null,close:null},
    {name:'示例医药',code:'DEMO.007',price:56.82,change:1.46,strength:5.34,volume:.75,amount:7.26,high:3.64,close:1.81}
  ].map((stock,index)=>({...stock,sector:['计算机','新材料','电力设备','机械设备','电子','消费电子','医药生物'][index]}));
  const iconNames = [
    ['grid','布局'],['chart','指数'],['layers','合集'],['bolt','涨停'],['flow','资金'],['picks','选股'],['sector','板块'],['wallet','持仓'],['panel','侧栏'],['search','搜索'],['date','日期'],['left','前一项'],['right','后一项'],['down','展开'],['refresh','刷新'],['sun','浅色'],['moon','深色'],['filter','筛选'],['columns','列设置'],['info','说明'],['close','关闭'],['expand','放大'],['shrink','还原'],['arrow','前往'],['check','完成'],['sort-up','升序'],['sort-down','降序'],['sort','排序'],['add','添加'],['upload','上传'],['file','文件'],['download','导出'],['star','收藏'],['starred','已收藏'],['delete','移除'],['settings','设置'],['calc','计算'],['history','历史'],['image','图片'],['database','数据']
  ];
  const state = {view:'comparison',category:'all',section:'all',query:'',table:new Map(),charts:new Map(),drawerIndex:0,returnFocus:null,toastTimer:null};
  const sectionHost = $('#kitSections');

  function readThemeVariables() {
    const base = {}, dark = {};
    const sheet = [...document.styleSheets].find(item => item.href && new URL(item.href).pathname.endsWith('/workspace.css'));
    if (!sheet) throw new Error('Workbench stylesheet is unavailable');
    for (const rule of sheet.cssRules) {
      if (![':root', ':root[data-theme="dark"]'].includes(rule.selectorText)) continue;
      const target = rule.selectorText === ':root' ? base : dark;
      for (const property of rule.style) if (property.startsWith('--')) target[property] = rule.style.getPropertyValue(property).trim();
    }
    return {light:base,dark:{...base,...dark}};
  }
  function applyTheme(element, theme) {
    element.dataset.storyTheme = theme;
    element.style.colorScheme = theme;
    Object.entries(themes[theme]).forEach(([key,value]) => element.style.setProperty(key,value));
    element.style.setProperty('--kit-error', themes[theme]['--up']);
  }
  function frameContext(element) { return element.closest('.kit-frame') || element.closest('dialog'); }
  function tableState(frame) {
    const key = `${frame.dataset.family}:${frame.dataset.storyTheme}`;
    if (!state.table.has(key)) state.table.set(key,{query:'',sort:'code',ascending:true,hidden:new Set(),minimum:0,selected:null});
    return state.table.get(key);
  }
  function chartState(frame) {
    const key = `${frame.dataset.family || frame.id}:${frame.dataset.storyTheme}`;
    if (!state.charts.has(key)) state.charts.set(key,{mode:'daily',tier:'主力',count:30});
    return state.charts.get(key);
  }
  function swatches(theme) {
    const roles = [['page','页面底色'],['surface','内容表面'],['sidebar','侧栏底色'],['surface-muted','次级表面'],['hover','悬停'],['selected','选中'],['ink','主要文字'],['secondary','次级文字'],['muted','辅助文字'],['line','细分隔线'],['line-strong','强分隔线'],['inverse','反白文字'],['up','上涨 / 流入'],['down','下跌 / 流出'],['up-soft','上涨底色'],['down-soft','下跌底色'],['chart-line','主要曲线'],['chart-secondary','次级曲线'],['chart-grid','图表网格'],['tag-active-fill','持续活跃'],['tag-hot-fill','新近热点 / 异常'],['tag-cool-fill','降温 / 信息'],['tag-pulse-fill','今日异动'],['tag-success-fill','成功 / 一般活跃'],['tag-warning-fill','待处理 / 缺失'],['tag-sector-fill','板块归属']];
    return `<div class="kit-color-grid">${roles.map(([key,label]) => `<button class="kit-swatch" data-kit-action="swatch" data-key="${key}" data-label="${label}" aria-label="${label} ${themes[theme][`--${key}`]}"><span class="kit-swatch-color" style="background:var(--${key})"></span><span class="kit-swatch-name">${label}</span><span class="kit-swatch-hex">${themes[theme][`--${key}`].toUpperCase()}</span></button>`).join('')}</div><output class="kit-color-output"><span>页面底色</span><code>--page / ${themes[theme]['--page']}</code></output>`;
  }
  function typography() {
    return [['页面标题','25px / 550','kit-type-page','选股推荐'],['关键价格','32px / 500','kit-type-price','26.38 <span class="up" style="font-size:13px;letter-spacing:0">+2.16%</span>'],['界面正文','14px / 1.5','kit-type-body','让每一项数据，都有清楚的解释。'],['表格数值','13px / mono','kit-type-data','5.82 亿　　8.42%　　2026-09-25'],['辅助说明','10px / regular','kit-type-help','虚构数据样例 · 缺失值显示“—”']].map(([name,size,cls,text]) => `<div class="kit-type-row"><div class="kit-type-meta"><span>${name}</span>${size}</div><div class="${cls}">${text}</div></div>`).join('');
  }
  function spacing() {
    return `<p class="kit-sublabel">SPACING / 基础间距</p><div class="kit-space-grid">${[4,8,12,16,20,24,32].map(size => `<div class="kit-space-item"><span class="kit-space-mark" style="--measure:${size}px"><i></i></span>${size}px</div>`).join('')}</div><div class="kit-component-divider"></div><p class="kit-sublabel">SHAPE / 控件与容器</p><div class="kit-radius-grid">${[['4px','标签'],['5px','输入框'],['6px','按钮'],['8px','内容卡']].map(([radius,label]) => `<div class="kit-radius-item" style="--radius:${radius}">${label}<span>R ${radius}</span></div>`).join('')}</div><p class="kit-demo-note">主区边距24px · 侧栏208 / 64px · 顶部工具条64px · 个股详情560px。</p>`;
  }
  function icons() {
    return `<div class="kit-icon-grid">${iconNames.map(([name,label]) => `<button class="kit-icon-tile${name==='search'?' active':''}" data-kit-action="icon" data-name="${name}" data-label="${label}" aria-label="查看${label}图标">${icon(name)}<span>${label}</span></button>`).join('')}</div><div class="kit-icon-info"><div class="kit-row" data-icon-preview>${icon('search',true)}${icon('search')}<svg class="icon" style="width:24px;height:24px" aria-hidden="true"><use href="icons.svg#i-search"/></svg><span>search / 15 · 18 · 24px</span></div></div>`;
  }
  function navigation() {
    return `<div class="kit-nav-demo"><aside class="kit-nav-demo-aside">${[['chart','核心指数'],['layers','阶段涨幅榜'],['wallet','持仓'],['picks','选股推荐']].map(([glyph,name]) => `<button class="nav-item${name==='选股推荐'?' active':''}" data-kit-action="nav" data-group="page" aria-pressed="${name==='选股推荐'}">${icon(glyph,true)}${name}</button>`).join('')}</aside><div class="kit-nav-demo-content"><div class="source-tabs" role="group" aria-label="示例数据来源">${['东方财富','同花顺','我的选股'].map((text,index) => `<button data-kit-action="nav" data-group="source" class="${index===0?'active':''}" aria-pressed="${index===0}">${text}</button>`).join('')}</div><div class="strategy-tabs" role="group" aria-label="示例策略">${['系统推荐','条件选股','条件优选'].map((text,index) => `<button data-kit-action="nav" data-group="strategy" class="${index===2?'active':''}" aria-pressed="${index===2}">${text}</button>`).join('')}</div><div class="kit-nav-demo-current">选股推荐 / 东方财富 / 条件优选</div></div></div><p class="kit-demo-note">侧栏指示所在页面，分段控件选择来源，下划线Tab选择策略。样例只展示各层级的选中状态。</p><div class="simple-nav"><div class="underline-tabs" role="group" aria-label="简易版页面切换样例">${[["overview","市场总览"],["barometer","盘面晴雨表"],["search","个股搜索"],["picks","选股推荐"]].map(([key,label])=>`<button data-kit-action="simple-nav" data-simple-demo="${key}" class="${key==='overview'?'active':''}" aria-pressed="${key==='overview'}">${label}</button>`).join('')}</div></div><p class="kit-demo-note">简易版使用同一套39px下划线Tab。个股搜索为独立弹窗入口，保留当前页面选中态。</p>`;
  }
  function buttons() {
    const variants = [['默认','',''],['悬停','kit-forced-hover',''],['焦点','kit-forced-focus',''],['禁用','','disabled'],['加载中','','disabled aria-busy="true"']];
    return `<p class="kit-sublabel">SECONDARY / 普通操作</p><div class="kit-variant-grid">${variants.map(([name,cls,attrs],index) => `<div class="kit-variant"><span>${name}</span><button class="button ${cls}" data-kit-action="button" ${attrs}>${icon(index===4?'refresh':'filter',true)}${index===4?'加载中':'应用筛选'}</button></div>`).join('')}</div><div class="kit-component-divider"></div><p class="kit-sublabel">PRIMARY / 主要操作</p><div class="kit-row">${action('button','确认添加','add','',true)}${action('loading','刷新示例','refresh')}${action('button','紧凑操作','', 'style="height:28px;font-size:11px"')}<button class="icon-button" data-kit-action="favorite" aria-label="收藏示例" aria-pressed="false">${icon('star')}</button><button class="icon-button" data-kit-action="button" aria-label="设置示例">${icon('settings')}</button><button class="icon-button" disabled aria-label="当前无法导出">${icon('download')}</button></div><div class="kit-component-divider"></div><div class="kit-row">${action('button','按钮 34px','','style="height:34px"')}${action('button','按钮 28px','','style="height:28px;font-size:11px"')}<span class="kit-field-help">图标控件 30 × 30px</span></div>`;
  }
  function forms() {
    return `<div class="form-grid kit-form-grid"><label class="kit-demo-field">默认输入<input placeholder="输入股票名称或代码" aria-label="默认输入样例"></label><label class="kit-demo-field">已填写<input value="示例科技" aria-label="已填写样例"></label><label class="kit-demo-field">焦点示例<input class="kit-forced-focus" value="26.38" aria-label="焦点样例"></label><label class="kit-demo-field">错误示例<input class="kit-input-error" value="-100" aria-invalid="true" aria-label="错误输入样例"><span class="kit-field-error">数量必须大于0</span></label><label class="kit-demo-field">禁用输入<input value="收盘数据" disabled></label><label class="kit-demo-field">选择来源<select aria-label="来源选择样例"><option>东方财富</option><option>同花顺</option><option>本地导入</option></select></label></div><div class="kit-component-divider"></div><div class="kit-row"><label class="kit-choice"><input type="checkbox" checked>包含已收藏</label><label class="kit-choice"><input type="checkbox">仅有完整资金</label><label class="kit-choice"><input type="checkbox" disabled>不可修改</label></div><div class="kit-row"><label class="kit-choice"><input type="radio"  checked data-kit-radio="rank">主力强度优先</label><label class="kit-choice"><input type="radio" data-kit-radio="rank">按代码排序</label><button class="mini-toggle" data-kit-action="toggle" aria-pressed="true" aria-label="仅看有效数据"><span></span></button><span class="kit-field-help">仅看有效数据</span></div><div class="kit-component-divider"></div><form data-kit-form novalidate><div class="form-grid"><label class="kit-demo-field">示例数量<input name="quantity" type="number" placeholder="例如 100" step="1" min="1" inputmode="numeric"><span class="kit-field-error" data-form-error="quantity" hidden></span></label><label class="kit-demo-field">示例价格 / 元<input name="price" type="number" placeholder="例如 26.38" step="0.01" min="0.01" inputmode="decimal"><span class="kit-field-error" data-form-error="price" hidden></span></label><label class="kit-demo-field form-field full">备注<textarea name="note" placeholder="仅演示，不写入持仓"></textarea></label></div><div class="kit-row" style="margin-top:16px"><button class="button primary" type="submit">${icon('check',true)}查看校验结果</button><button class="text-button" type="reset">重置</button></div><output class="kit-form-preview" data-form-preview hidden></output></form>`;
  }
  function metrics() {
    return `<p class="kit-sublabel">STATUS / 状态标签</p><div class="kit-row">${tag('数据完整')}${tag('历史研究')}${tag('部分缺失')}<span class="count-badge">24只</span></div><div class="kit-row"><span class="status-tag positive">上涨 +2.16%</span><span class="status-tag negative">下跌 −1.34%</span><span class="hit-pill">${icon('check')}缩量命中</span><span class="hit-pill">${icon('check')}低于MA60</span></div><div class="kit-component-divider"></div><div class="kit-demo-quote"><div><h3>示例科技 ${sectorTag('计算机')}</h3><p>DEMO.001 · 虚构样例</p></div><b>26.38</b><span class="up">+2.16%</span></div><div class="metric-strip kit-demo-metrics" style="margin-top:16px">${[['入选数量','24','只','仅演示信息密度'],['主力净额','+5.82','亿元','方向色仅用于资金','up'],['5日主力强度','8.42','%','净额 / 成交额'],['下跌家数','2,836','家','虚构同日市场摘要','down']].map(([label,value,unit,note,cls])=> `<div class="metric-tile"><div class="metric-label">${label}</div><div class="metric-value ${cls||''}">${value}<span class="metric-unit">${unit}</span></div><div class="metric-note">${note}</div></div>`).join('')}</div>`;
  }
  const columns = [['price','收盘 / 元'],['change','涨跌幅'],['strength','5日主力强度'],['volume','量比'],['amount','成交额 / 亿'],['high','T+1最高'],['close','T+1收盘']];
  function tableRows(frame, compact = false) {
    const s = tableState(frame);
    const visible = columns.filter(([key]) => !s.hidden.has(key) && (!compact || ['price','change','strength'].includes(key)));
    const list = stocks.filter(stock => `${stock.name} ${stock.code}`.toLowerCase().includes(s.query.toLowerCase()) && stock.strength>=s.minimum).sort((a,b) => {
      const av=a[s.sort],bv=b[s.sort];
      if(av==null) return bv==null?0:1;
      if(bv==null) return -1;
      return (typeof av==='number'?av-bv:av.localeCompare(bv))*(s.ascending?1:-1);
    });
    const cell = (stock,key) => {
      if (stock[key]==null) return '<span class="missing-value" title="缺少该交易日记录">—</span>';
      if (['change','high','close'].includes(key)) return `<span class="${tone(stock[key])}">${signed(stock[key])}</span>`;
      return `${stock[key].toFixed(2)}${key==='strength'?'%':key==='volume'?' ×':''}`;
    };
    return `<table class="kit-table" aria-label="虚构股票表格"><thead><tr><th class="stock-header">股票 / 示例</th>${visible.map(([key,label])=>`<th aria-sort="${s.sort===key?(s.ascending?'ascending':'descending'):'none'}"><button data-kit-action="sort" data-key="${key}">${label}${icon(s.sort===key?(s.ascending?'sort-up':'sort-down'):'sort',true)}</button>${['high','close'].includes(key)?'<span class="column-date">2026-09-28</span>':''}</th>`).join('')}</tr></thead><tbody>${list.length?list.map(stock=>`<tr${s.selected===stock.code?' class="selected"':''}><td class="stock-cell"><button class="stock-button" data-kit-action="stock" data-code="${stock.code}"><span class="stock-title"><strong>${escape(stock.name)}</strong>${sectorTag(stock.sector)}</span><span class="stock-code">${escape(stock.code)}</span></button></td>${visible.map(([key])=>`<td>${cell(stock,key)}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${visible.length+1}"><div class="view-empty"><strong>没有匹配的示例</strong><p>调整搜索内容或清除筛选。</p>${action('table-reset','清除筛选','refresh')}</div></td></tr>`}</tbody></table>`;
  }
  function table() {
    return `<div class="kit-table-tools"><label class="search-field">${icon('search',true)}<input data-table-search type="search" placeholder="搜索示例股票" aria-label="搜索示例股票"></label><div class="popover-wrap">${action('columns','列设置','columns','aria-expanded="false"')}<div class="popover column-popover" data-columns-popover hidden><div class="popover-title">固定名称列 · 可调整指标</div>${columns.map(([key,label])=>`<label class="column-option"><input type="checkbox" data-column="${key}" checked>${label}</label>`).join('')}</div></div></div><div class="kit-table-wrap"><div class="kit-table-scroll" tabindex="0" aria-label="横向或纵向滚动数据表格" data-table-host></div><div class="kit-table-foot"><span data-table-count>7只虚构样例</span><span>表头 / 名称列固定</span></div></div><p class="kit-demo-note">点击股票打开详情；示例消费的T+1记录缺失，显示“—”。日期与结果仅演示表格表达。</p>`;
  }
  function priceSvg(mode='daily', count=30) {
    const width=620,height=205,left=38,right=16,top=16,bottom=28;
    const w=width-left-right,h=height-top-bottom;
    const series=Array.from({length:count},(_,i)=>26+Math.sin(i*.66)*1.24+Math.cos(i*.22)*.5+i*.085);
    const min=Math.min(...series)-1,max=Math.max(...series)+1;
    const y=value=>top+(max-value)/(max-min)*h,x=i=>left+(i+.5)*w/count;
    const grid=[0,1,2,3].map(i=>{const yy=top+h*i/3;return `<line x1="${left}" x2="${width-right}" y1="${yy}" y2="${yy}" stroke="var(--chart-grid)"/><text x="0" y="${yy+3}" fill="var(--muted)" font-size="9">${(max-(max-min)*i/3).toFixed(2)}</text>`;}).join('');
    const candles=series.map((close,i)=>{const open=close+Math.sin(i*1.41)*.6,color=close>=open?'var(--up)':'var(--down)';return `<line x1="${x(i)}" x2="${x(i)}" y1="${y(Math.max(close,open)+.5)}" y2="${y(Math.min(close,open)-.5)}" stroke="${color}"/><rect x="${x(i)-Math.max(2,w/count*.25)}" y="${y(Math.max(close,open))}" width="${Math.max(4,w/count*.5)}" height="${Math.max(1,Math.abs(y(close)-y(open)))}" fill="${color}"/>`;}).join('');
    const curve=series.map((close,i)=>`${x(i)},${y(close+(mode==='daily'?.3:0))}`).join(' ');
    const second=series.map((close,i)=>`${x(i)},${y(close*.35+26*.65)}`).join(' ');
    return `<svg class="kit-candle-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="虚构${mode==='daily'?'日K':'分时'}价格图">${grid}${mode==='daily'?candles:''}<polyline points="${second}" fill="none" stroke="var(--chart-secondary)" stroke-width="1.2" stroke-dasharray="4 3"/><polyline points="${curve}" fill="none" stroke="var(--chart-line)" stroke-width="1.4"/><text x="${left}" y="${height-3}" fill="var(--muted)" font-size="9">${mode==='daily'?'08-17':'09:30'}</text><text x="${width-right}" y="${height-3}" text-anchor="end" fill="var(--muted)" font-size="9">${mode==='daily'?'09-25':'15:00'}</text></svg>`;
  }
  function fundSvg(count=30,tier='主力') {
    const offset=['主力','超大单','大单','中单','小单'].indexOf(tier), w=566;
    return `<svg class="kit-fund-svg" viewBox="0 0 620 96" preserveAspectRatio="none" role="img" aria-label="虚构${tier}资金柱图"><line x1="38" y1="48" x2="604" y2="48" stroke="var(--line-strong)"/><text x="0" y="51" fill="var(--muted)" font-size="9">0</text>${Array.from({length:count},(_,i)=>{const v=Math.sin(i*.66+offset)*32+Math.cos(i*.24)*6;return `<rect x="${38+(i+.15)*w/count}" y="${v>=0?48-v:48}" width="${w/count*.7}" height="${Math.abs(v)}" fill="var(--${v>=0?'up':'down'})" opacity=".75"/>`;}).join('')}</svg>`;
  }
  function charts() {
    return `<div class="kit-chart-toolbar"><div class="chart-tabs" role="group" aria-label="价格图类型"><button class="active" data-kit-action="chart-mode" data-mode="daily" aria-pressed="true">日K</button><button data-kit-action="chart-mode" data-mode="minute" aria-pressed="false">分时</button></div><span>示例科技 · 虚构走势</span></div><div class="chart-legend"><span><i class="legend-line"></i><span data-primary-legend>MA20</span></span><span><i class="legend-line secondary"></i><span data-secondary-legend>MA60</span></span><span class="up">上涨</span><span class="down">下跌</span></div><div class="kit-chart-host" data-chart-host="price">${priceSvg()}<div class="kit-chart-guide" hidden></div><div class="kit-chart-tip" hidden></div></div><div class="kit-chart-title">资金净额<span>单位 / 亿元 · 同一区间</span></div><div class="detail-fund-tiers" role="group" aria-label="资金分层">${['主力','超大单','大单','中单','小单'].map((tier,i)=>`<button class="${i===0?'active':''}" data-kit-action="chart-tier" data-tier="${tier}" aria-pressed="${i===0}">${tier}</button>`).join('')}</div><div class="kit-chart-host" data-chart-host="fund">${fundSvg()}<div class="kit-chart-guide" hidden></div><div class="kit-chart-tip" hidden></div></div><div class="chart-range"><span data-range-label>30个样例点</span><input data-chart-range type="range" min="10" max="40" value="30" aria-label="同步调整示例价格和资金图的区间"><span>区间联动</span></div>`;
  }
  function feedback() {
    return `<div class="kit-inline-alert">${icon('info',true)}部分资金数据缺失；有效价格仍可查看。</div><div class="kit-status-grid"><div class="kit-state-card" data-feedback="loading">${icon('refresh')}<strong>正在读取数据</strong><p>保留当前日期与筛选条件。</p>${action('state-load','演示加载完成','','aria-label="演示加载完成"')}</div><div class="kit-state-card" data-feedback="empty">${icon('search')}<strong>没有符合条件的记录</strong><p>可以调整条件或选择其他日期。</p>${action('state-empty','清除筛选','refresh')}</div><div class="kit-state-card" data-feedback="missing">${icon('database')}<strong>该日资金记录缺失</strong><p>对应指标显示“—”，不按0处理。</p>${action('state-missing','查看缺失说明','info')}</div><div class="kit-state-card kit-state-error" data-feedback="error">${icon('info')}<strong>暂时无法读取数据</strong><p>保留操作上下文，可重试。</p>${action('state-retry','重试','refresh')}</div></div><p class="kit-demo-note">以上状态可同时评审；按钮仅模拟状态变化，不发起数据请求。</p>`;
  }
  function calendar() {
    const days=[...Array.from({length:30},(_,i)=>i+1)];
    return `<div class="kit-calendar" data-calendar hidden><div class="calendar-title">2026年9月<span>样例交易日</span></div><div class="calendar-grid">${['一','二','三','四','五','六','日'].map(name=>`<span class="calendar-day-name">${name}</span>`).join('')}<span class="calendar-blank"></span>${days.map(day=>`<button data-kit-action="date-select" data-day="${day}" ${[21,22,23,24,25,28,29,30].includes(day)?'':'disabled'} class="${day===25?'active':''}" aria-label="2026年9月${day}日">${day}</button>`).join('')}</div><div class="calendar-footnote">仅开放示例日期，不查询真实交易日历。</div></div>`;
  }
  function overlays() {
    return `<p class="kit-sublabel">DATE / 日期选择</p><div class="kit-calendar-wrap"><div class="date-navigation" style="width:max-content"><button class="icon-button" data-kit-action="date-shift" data-offset="-1" aria-label="前一个示例交易日">${icon('left',true)}</button><button class="date-button" data-kit-action="calendar" aria-expanded="false">${icon('date',true)}<span data-date-label>2026-09-25</span>${icon('down',true)}</button><button class="icon-button" data-kit-action="date-shift" data-offset="1" aria-label="后一个示例交易日">${icon('right',true)}</button></div>${calendar()}</div><p class="kit-date-detail">日期语义：筛选日 / 收盘数据 · 虚构日期样例</p><div class="kit-component-divider"></div><p class="kit-sublabel">OVERLAY / 弹窗与详情</p><div class="kit-overlay-grid"><div class="kit-mini-window"><div class="kit-mini-window-top"><span>普通弹窗 / 780px</span>${icon('close',true)}</div><div class="kit-mini-window-content"><p>单一任务独立操作，标题与关闭按钮固定。</p><div class="kit-row">${action('dialog','普通弹窗','expand','data-kind="normal"')}${action('dialog','宽弹窗','columns','data-kind="wide"')}</div><div class="kit-row">${action('dialog','导入流程','upload','data-kind="import"')}</div></div></div><div class="kit-mini-drawer"><h3>右侧详情</h3><b>560<span style="font-size:11px;margin-left:5px">px</span></b><p>摘要 → 价格图 → 资金图<br>支持切换股票、放大和关闭。</p>${action('stock','打开详情','arrow','data-code="DEMO.001"')}</div></div>`;
  }
  function pattern() {
    return `<div class="kit-pattern"><div class="kit-pattern-header"><div><h3>条件优选</h3><span class="view-subtle">东方财富 · 2026-09-25 · 虚构样例</span></div><span class="count-badge" data-pattern-count>7只</span></div><div class="kit-pattern-filter"><label class="view-label">主力强度<select class="view-select" data-pattern-minimum aria-label="示例主力强度筛选"><option value="0">全部示例</option><option value="6">≥6%</option><option value="8">≥8%</option></select></label><label class="search-field">${icon('search',true)}<input type="search" data-table-search placeholder="搜索示例股票" aria-label="组合样例搜索"></label></div><div class="kit-table-scroll" tabindex="0" aria-label="滚动组合样例表格" data-table-host></div><div class="kit-table-foot"><span>指标、日期、单位同屏可见</span><span>UI组合 / 虚构样例</span></div></div><p class="kit-demo-note">默认收起规则，保留常用筛选和名单空间；示例不调用策略接口，也不解释为投资推荐。</p>`;
  }
  function strategyPatterns(theme,link) {
    return `<section class="view-panel"><h3>条件开关与独立数量</h3><div class="design-rule"><button class="design-switch is-on" role="switch" aria-label="均线多头组件示例" aria-checked="true" data-kit-action="design-switch"><span></span></button><div class="design-rule-copy"><div><strong>均线多头排列</strong><span class="design-count">349个符合</span></div><code>收盘 &gt; MA5 &gt; MA10 &gt; MA20 &gt; MA60</code></div></div><div class="kit-row"><span class="design-count">全军覆没</span><span class="design-count">数据不足</span>${link('低吸与参数',{strategy:'dip'})}${link('追高与诊断',{strategy:'chase'})}</div></section><section class="view-panel"><h3>自定义条件配置</h3><div class="custom-config-sample"><div class="custom-rule-row"><button class="design-switch is-on" role="switch" aria-label="自定义条件组件示例" aria-checked="true" data-kit-action="design-switch"><span></span></button><div class="custom-rule-description"><strong>收盘高于20日线</strong></div><div class="custom-rule-count"><span class="design-count">1,649个符合</span></div></div></div><p class="design-copy">配置可收起，公式按需显示，模板在弹窗中选择。</p><div class="kit-row">${tag('组内 · 全部满足')}${tag('组间 · 任一满足')}</div><div class="view-tabs"><button class="active">点选指标</button><button>输入公式</button><button>中文生成草稿</button></div><p class="design-copy" style="margin-top:12px">中文先核对全部草稿；未识别时保留原句并阻止加入。</p>${link('打开条件编辑与模板',{strategy:'custom'})}</section><section class="view-panel"><h3>每日涨停数 · 冷暖热度</h3><div class="kit-row">${[0,1,3,7,12].map((v,i)=>`<span class="design-heat level-${i}">${v}</span>`).join('')}<span class="design-heat missing">—</span></div><p class="design-copy" style="margin-top:12px">蓝色 → 青色 → 橙色 → 暖红；数量越多颜色越暖。0为中性，缺记录用虚线与“—”。</p>${link('热门榜 / 龙头 / 昨日跟踪',{page:'sectors'})}</section>`;
  }

  function business(theme) {
    const link=(label,params)=>`<a class="button" href="index.html?${new URLSearchParams({...params,theme})}">${label}${icon('arrow',true)}</a>`;
    return `<div class="kit-business-patterns"><section class="view-panel" data-semantic-demo="status"><h3>热门板块 · 当前状态</h3><div class="kit-row">${['持续活跃','新近热点','已经降温','今日异动','一般活跃'].map(label=>tag(label)).join('')}</div><div class="kit-row">${['数据不足','待确认','暂无热度'].map(label=>tag(label)).join('')}</div><p class="design-copy">橙色活跃、暖红新热点、蓝色降温、紫色异动；缺数据用琥珀色提醒。</p></section><section class="view-panel" data-semantic-demo="sector"><h3>股票名称 · 板块归属</h3><div class="kit-sector-samples">${stocks.slice(0,2).map(stock=>`<button class="stock-button" data-kit-action="stock" data-code="${stock.code}" aria-label="查看${stock.name}详情"><span class="stock-title"><strong>${stock.name}</strong>${sectorTag(stock.sector)}</span><span class="stock-code">${stock.code}</span></button>`).join('')}</div><p class="design-copy">名称与板块标签同一行，代码放在下一行。板块色只表示归属。</p></section><section class="view-panel"><h3>自定义参数与行内添加</h3><div class="kit-row"><label class="view-label">门槛<input class="compact-number" type="number" min="0" step="0.5" value="3" aria-label="业务门槛样例">亿元</label><label class="view-label">缩量≤<input class="compact-number" type="number" min="1" max="100" value="80" aria-label="业务缩量样例">%</label></div><div class="kit-row">${action('business-add','加入我的选股','add')}<button class="button" disabled>${icon('check',true)}已加入</button>${link('样本明细',{review:'history'})}</div></section><section class="plan-card"><div class="panel-title"><h3>方案状态组件</h3>${tag('已采用')}</div><p>触发价32.86 · 减仓400股</p><div class="kit-row">${tag('持仓变化 · 已失效')}${tag('到价提醒 · 示例')}</div>${link('比较 / 调整 / 取消',{page:'positions',review:'exit-plan'})}</section><section class="view-panel"><h3>导入与异常组件</h3><div class="kit-row">${tag('有效')}${tag('重复 · 跳过')}${tag('缺必需列')}${tag('待重新应用')}</div><div class="kit-row">${link('成交字段与确认',{page:'positions',review:'import-trades'})}${link('近期热门 / 自动龙头',{page:'sectors'})}</div></section><section class="view-panel"><h3>完整价格与资金组件</h3>${window.TradeIQPreviewCharts.render({key:'kit-business-'+theme,stock:{code:'KIT.SAMPLE',name:'组件示例',price:32.86,hits:2},date:'20260922',funds:true,compact:true,range:'all'})}</section><section class="view-panel"><h3>市场总览 · 双组多轴趋势</h3><div class="market-trend-grid kit-market-trends">${['indices','breadth'].map(kind=>window.TradeIQPreviewCharts.renderMarketTrend({key:'kit-trend-'+theme+'-'+kind,kind,date:'20260930'})).join('')}</div><div class="kit-row">${link('打开简易版市场总览',{page:'index',view:'overview',simple:'1'})}</div></section>${strategyPatterns(theme,link)}<p class="kit-demo-note">新增组件沿用灰阶结构，状态与板块标签使用语义色点缀。所有数据虚构，工作台与组件页均不接业务API。</p></div>`;
  }
  const renderers={business,colors:swatches,typography,spacing,icons,navigation,buttons,forms,metrics,table,charts,feedback,overlays,pattern};
  function render() {
    const visible=families.filter(item=>(state.category==='all'||item.category===state.category)&&(state.section==='all'||item.id===state.section)&&`${item.name} ${item.keywords} ${item.description}`.toLowerCase().includes(state.query.toLowerCase()));
    const lanes=state.view==='comparison'?['light','dark']:[state.view];
    sectionHost.innerHTML=visible.map(item=>`<section class="kit-section" id="family-${item.id}" data-family-section="${item.id}"><div class="kit-section-heading"><div class="kit-section-heading-left"><span>${item.number}</span><h2>${item.name}</h2></div><p>${item.description}</p></div><div class="kit-specimens">${lanes.map(theme=>`<article class="kit-frame" data-family="${item.id}" data-story-theme="${theme}" aria-label="${item.name} / ${theme==='light'?'浅色':'深色'}"><div class="kit-frame-header"><span>${item.name}</span><span>${theme==='light'?'LIGHT / 浅色':'DARK / 深色'}</span></div><div class="kit-frame-body">${renderers[item.id](theme)}</div><div class="kit-frame-foot">${item.note}</div></article>`).join('')}</div></section>`).join('');
    $$('.kit-frame').forEach(frame=>{
      applyTheme(frame,frame.dataset.storyTheme);
      const radioName=`rank-${frame.dataset.family}-${frame.dataset.storyTheme}`;
      $$('[data-kit-radio]',frame).forEach(input=>input.name=radioName);
      if(['table','pattern'].includes(frame.dataset.family)) updateTable(frame);
      if(frame.dataset.family==='charts') updateCharts(frame);
    });
    $('#visibleFamilyCount').textContent=`${visible.length} / ${families.length} 类`;
    $('#kitNoResults').hidden=visible.length>0;
    $('#comparisonLabel').hidden=state.view!=='comparison'||visible.length===0;
    $('#kitMain').dataset.kitView=state.view;
    document.documentElement.dataset.theme=state.view==='dark'?'dark':'light';
    $$('[data-kit-theme]').forEach(button=>{const active=button.dataset.kitTheme===state.view;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
    $$('[data-kit-category]').forEach(button=>{const active=button.dataset.kitCategory===state.category;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
    $$('[data-kit-section]').forEach(button=>{const active=button.dataset.kitSection===state.section;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  }
  function updateTable(frame) {
    const host=$('[data-table-host]',frame), s=tableState(frame), left=host.scrollLeft,top=host.scrollTop;
    host.innerHTML=tableRows(frame,frame.dataset.family==='pattern');
    host.scrollLeft=left;host.scrollTop=top;
    const count=$$('tbody .stock-button',host).length;
    const input=$('[data-table-search]',frame);if(input)input.value=s.query;
    const select=$('[data-pattern-minimum]',frame);if(select)select.value=String(s.minimum);
    $$('[data-column]',frame).forEach(input=>input.checked=!s.hidden.has(input.dataset.column));
    const countLabel=$('[data-table-count]',frame);if(countLabel)countLabel.textContent=`${count}只虚构样例`;
    const patternLabel=$('[data-pattern-count]',frame);if(patternLabel)patternLabel.textContent=`${count}只`;
  }
  function updateCharts(frame) {
    const s=chartState(frame);
    $('[data-chart-host="price"]',frame).innerHTML=`${priceSvg(s.mode,s.count)}<div class="kit-chart-guide" hidden></div><div class="kit-chart-tip" hidden></div>`;
    $('[data-chart-host="fund"]',frame).innerHTML=`${fundSvg(s.count,s.tier)}<div class="kit-chart-guide" hidden></div><div class="kit-chart-tip" hidden></div>`;
    $$('[data-mode]',frame).forEach(button=>{const active=button.dataset.mode===s.mode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
    $$('[data-tier]',frame).forEach(button=>{const active=button.dataset.tier===s.tier;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
    $('[data-chart-range]',frame).value=s.count;
    $('[data-range-label]',frame).textContent=`${s.count}个样例点`;
    $('[data-primary-legend]',frame).textContent=s.mode==='daily'?'MA20':'价格';
    $('[data-secondary-legend]',frame).textContent=s.mode==='daily'?'MA60':'均价';
  }
  function toast(message,theme='light') {
    const element=$('#kitToast');applyTheme(element,theme);element.textContent=message;element.hidden=false;
    clearTimeout(state.toastTimer);state.toastTimer=setTimeout(()=>element.hidden=true,2600);
  }
  function closePopovers(except=null) {
    $$('[data-calendar], [data-columns-popover]').forEach(popover=>{if(popover!==except){popover.hidden=true;const trigger=popover.parentElement.querySelector('[aria-expanded]');if(trigger)trigger.setAttribute('aria-expanded','false');}});
  }
  function setDate(frame,day) {
    const activeDays=[21,22,23,24,25,28,29,30];
    frame.dataset.day=String(day);
    $('[data-date-label]',frame).textContent=`2026-09-${String(day).padStart(2,'0')}`;
    $$('[data-day]',frame).forEach(button=>button.classList.toggle('active',Number(button.dataset.day)===day));
    $$('[data-offset]',frame).forEach(button=>button.disabled=Number(button.dataset.offset)<0?day===activeDays[0]:day===activeDays.at(-1));
    closePopovers();
  }
  function dialogContent(kind,step=1) {
    const footer=action('dialog-close','关闭样例','close');
    if(kind==='normal')return `<p class="kit-demo-note" style="margin-top:0">查看规则，保持工作台上下文。下面仅演示信息弹窗的层级与动作。</p><div class="strategy-rules" style="margin-top:18px;background:var(--surface-muted);border:1px solid var(--line);border-radius:6px"><strong>条件优选 / 展示样例</strong><p>正式策略以项目规则文档为准。设计页只用于评审UI，不执行筛选，也不展示真实名单。</p></div><div class="dialog-actions">${footer}${action('dialog-complete','已了解','check','',true)}</div>`;
    if(kind==='wide')return `<div class="kit-dialog-columns"><div><p class="kit-sublabel">价格区域 / 示例科技</p>${priceSvg()}<p class="kit-demo-note">宽弹窗用于并排查看内容，避免图表与说明互相挤压。</p></div><div><p class="kit-sublabel">资金区域 / 虚构数据</p>${fundSvg()}<div class="health-list" style="margin-top:20px"><div class="health-row">主力净额<span class="mono up">+0.58亿元</span></div><div class="health-row">数据日期<span class="mono">2026-09-25</span></div><div class="health-row">数据用途${tag('UI演示')}</div></div></div></div><div class="dialog-actions"><span class="view-subtle">宽度上限1160px</span>${footer}</div>`;
    const stepper=`<div class="dialog-stepper">${['选择文件','字段映射','预览完成'].map((text,i)=>`${i?'<div class="step-line"></div>':''}<span class="${i+1===step?'active':''}"><i>${i+1}</i>${text}</span>`).join('')}</div>`;
    if(step===1)return `${stepper}<div class="upload-zone">${icon('upload')}<strong>导入流程的UI样例</strong><p>使用内置虚构文件，演示确认之前的检查流程。</p>${action('import-next','使用示例文件','file','data-step="2"',true)}</div><div class="dialog-actions"><span class="view-subtle">不读取本地文件，不写入持仓</span>${footer}</div>`;
    if(step===2)return `${stepper}<div class="result-alert">示例文件：demo-portfolio.csv · 3条虚构记录</div>${['股票代码','成交数量','成交价格'].map((name,i)=>`<label class="mapping-row"><span>${name}</span><select aria-label="${name}字段映射"><option>${['code','quantity','price'][i]}</option><option>忽略该列</option></select></label>`).join('')}<div class="dialog-actions">${action('import-next','上一步','left','data-step="1"')}${action('import-next','检查预览','right','data-step="3"',true)}</div>`;
    return `${stepper}<div class="simulation-result">${icon('check')}<h3>3条示例记录可预览</h3><p>实际导入前会显示校验结果。此处演示已完成，数据未写入项目。</p></div><div class="dialog-actions">${action('import-next','返回映射','left','data-step="2"')}${action('dialog-complete','完成演示','check','',true)}</div>`;
  }
  function openDialog(button,kind) {
    const frame=frameContext(button),dialog=$('#kitDialog');
    closePopovers();applyTheme(dialog,frame?.dataset.storyTheme||'light');
    dialog.dataset.kind=kind;dialog.classList.toggle('wide',kind==='wide');
    $('#kitDialogTitle').textContent=kind==='wide'?'宽弹窗 / 图表对照':kind==='import'?'导入流程 / 交互样例':'规则说明 / 普通弹窗';
    $('#kitDialogBody').innerHTML=dialogContent(kind);
    state.returnFocus=button;dialog.showModal();
  }
  function drawDrawer() {
    const stock=stocks[state.drawerIndex];
    $('#kitDrawerBody').innerHTML=`<section class="stock-summary"><div class="stock-name-row"><div><h2 id="kitDrawerTitle">${escape(stock.name)}${sectorTag(stock.sector)}</h2><div class="stock-identity">${stock.code} / 虚构样例</div></div><span class="stock-date-chip">2026-09-25</span></div><div class="stock-price-row"><span class="stock-price">${stock.price.toFixed(2)}</span><span class="stock-change ${tone(stock.change)}">${signed(stock.change)}</span><span class="stock-price-label">示例收盘 / 元</span></div><div class="stock-summary-grid">${[['主力强度',`${stock.strength.toFixed(2)}%`],['量比',`${stock.volume.toFixed(2)}×`],['成交额',`${stock.amount.toFixed(2)}亿`],['数据用途','UI展示']].map(([label,value])=>`<div class="summary-stat"><span>${label}</span><b>${value}</b></div>`).join('')}</div></section><section class="detail-section" style="grid-column:1"><div class="detail-section-heading"><h3>价格与资金 / 虚构图形</h3><span class="view-subtle">可操作图表组件</span></div>${charts()}</section><section class="detail-section"><div class="detail-section-heading"><h3>数据说明</h3></div><div class="view-note">${icon('info')}该抽屉仅用于评审视觉与交互，图形不对应实际行情。</div><p class="kit-demo-note">关闭详情返回原列表，保留搜索、排序、列设置与滚动位置。放大后按Esc先恢复抽屉，再按Esc关闭。</p></section>`;
    updateCharts($('#kitDrawer'));
    $('#kitDrawerBody').scrollTop=0;
  }
  function openDrawer(button) {
    const frame=frameContext(button),dialog=$('#kitDrawer');closePopovers();
    applyTheme(dialog,frame?.dataset.storyTheme||'light');
    state.drawerIndex=Math.max(0,stocks.findIndex(stock=>stock.code===button.dataset.code));
    state.returnFocus=button;
    if(frame && ['table','pattern'].includes(frame.dataset.family)) {
      tableState(frame).selected=stocks[state.drawerIndex].code;
      $$('tbody tr',frame).forEach(row=>row.classList.toggle('selected',$('[data-code]',row)?.dataset.code===button.dataset.code));
    }
    dialog.classList.remove('is-expanded');setExpanded(false);drawDrawer();dialog.showModal();
  }
  function setExpanded(expanded) {
    const dialog=$('#kitDrawer'),button=$('[data-kit-expand]',dialog);
    dialog.classList.toggle('is-expanded',expanded);button.setAttribute('aria-expanded',String(expanded));button.setAttribute('aria-label',expanded?'还原详情':'放大详情');button.innerHTML=icon(expanded?'shrink':'expand');
  }
  function simulate(button,done,message) {
    if(button.disabled)return;
    const original=button.innerHTML,frame=frameContext(button),theme=frame?.dataset.storyTheme||'light';
    button.disabled=true;button.setAttribute('aria-busy','true');button.innerHTML=`${icon('refresh',true)}处理中`;
    setTimeout(()=>{if(!button.isConnected)return;button.disabled=false;button.removeAttribute('aria-busy');button.innerHTML=original;done?.();toast(message,theme);},700);
  }
  const actions={
    'design-switch':button=>{const checked=button.getAttribute('aria-checked')!=='true';button.setAttribute('aria-checked',String(checked));button.classList.toggle('is-on',checked);},
    'business-add':button=>{button.disabled=true;button.innerHTML=icon('check',true)+'已加入';},
    swatch:(button,frame)=>{$('.kit-color-output',frame).innerHTML=`<span>${escape(button.dataset.label)}</span><code>--${button.dataset.key} / ${themes[frame.dataset.storyTheme][`--${button.dataset.key}`]}</code>`;},
    icon:(button,frame)=>{$$('.kit-icon-tile',frame).forEach(item=>item.classList.toggle('active',item===button));const name=button.dataset.name;$('[data-icon-preview]',frame).innerHTML=`${icon(name,true)}${icon(name)}<svg class="icon" style="width:24px;height:24px" aria-hidden="true"><use href="icons.svg#i-${name}"/></svg><span>${name} / 15 · 18 · 24px</span>`;},
    "simple-nav":(button,frame)=>{if(button.dataset.simpleDemo==='search'){toast('个股搜索为独立弹窗入口，当前页面选中态保留。');return;}$$('[data-simple-demo]',frame).forEach(item=>{item.classList.toggle('active',item===button);item.setAttribute('aria-pressed',String(item===button));});},
    nav:(button,frame)=>{const group=button.dataset.group;$$(`[data-group="${group}"]`,frame).forEach(item=>{item.classList.toggle('active',item===button);item.setAttribute('aria-pressed',String(item===button));});$('.kit-nav-demo-current',frame).textContent=['page','source','strategy'].map(key=>$(`[data-group="${key}"].active`,frame).textContent).join(' / ');},
    button:(button,frame)=>toast(`${button.textContent.trim()||button.getAttribute('aria-label')} · 操作反馈样例`,frame.dataset.storyTheme),
    loading:button=>simulate(button,null,'刷新演示已完成'),
    favorite:(button,frame)=>{const active=button.getAttribute('aria-pressed')!=='true';button.setAttribute('aria-pressed',String(active));button.setAttribute('aria-label',active?'取消收藏示例':'收藏示例');button.innerHTML=icon(active?'starred':'star');toast(active?'已收藏示例':'已取消示例收藏',frame.dataset.storyTheme);},
    toggle:button=>button.setAttribute('aria-pressed',String(button.getAttribute('aria-pressed')!=='true')),
    sort:(button,frame)=>{const s=tableState(frame);s.ascending=s.sort===button.dataset.key?!s.ascending:false;s.sort=button.dataset.key;updateTable(frame);},
    columns:(button,frame)=>{const popup=$('[data-columns-popover]',frame),open=popup.hidden;closePopovers(popup);popup.hidden=!open;button.setAttribute('aria-expanded',String(open));},
    'table-reset':(button,frame)=>{Object.assign(tableState(frame),{query:'',minimum:0});updateTable(frame);},
    stock:button=>openDrawer(button),
    'chart-mode':(button,frame)=>{chartState(frame).mode=button.dataset.mode;updateCharts(frame);},
    'chart-tier':(button,frame)=>{chartState(frame).tier=button.dataset.tier;updateCharts(frame);},
    'state-load':button=>simulate(button,()=>{const card=button.closest('.kit-state-card');$('strong',card).textContent='示例数据已加载';$('p',card).textContent='当前日期与筛选条件已保留。';},'加载完成状态已展示'),
    'state-empty':(button,frame)=>{const card=button.closest('.kit-state-card');$('strong',card).textContent='筛选已清除';$('p',card).textContent='可重新选择示例日期或条件。';toast('清除筛选演示已完成',frame.dataset.storyTheme);},
    'state-missing':(button,frame)=>toast('缺少该日资金记录；数值留空，不按0处理。',frame.dataset.storyTheme),
    'state-retry':button=>simulate(button,()=>{const card=button.closest('.kit-state-card');$('strong',card).textContent='示例重试成功';$('p',card).textContent='日期、筛选与排序均已保留。';card.classList.remove('kit-state-error');},'重试演示已完成'),
    calendar:(button,frame)=>{const popup=$('[data-calendar]',frame),open=popup.hidden;closePopovers(popup);popup.hidden=!open;button.setAttribute('aria-expanded',String(open));},
    'date-select':(button,frame)=>setDate(frame,Number(button.dataset.day)),
    'date-shift':(button,frame)=>{const days=[21,22,23,24,25,28,29,30],index=days.indexOf(Number(frame.dataset.day||25));setDate(frame,days[Math.min(days.length-1,Math.max(0,index+Number(button.dataset.offset)))]);},
    dialog:button=>openDialog(button,button.dataset.kind),
    'dialog-close':()=>$('#kitDialog').close(),
    'dialog-complete':()=>{const dialog=$('#kitDialog');dialog.close();toast('组件操作演示完成',dialog.dataset.storyTheme);},
    'import-next':button=>{$('#kitDialogBody').innerHTML=dialogContent('import',Number(button.dataset.step));}
  };
  document.addEventListener('click',event=>{
    const button=event.target.closest('button');
    if(!event.target.closest('.popover-wrap, .kit-calendar-wrap'))closePopovers();
    if(!button||button.disabled)return;
    if(button.dataset.kitTheme){state.view=button.dataset.kitTheme;try{localStorage.setItem(preferenceKey,state.view);}catch{}render();return;}
    if(button.dataset.kitCategory){state.category=button.dataset.kitCategory;state.section='all';render();return;}
    if(button.dataset.kitSection){state.section=button.dataset.kitSection;state.category='all';state.query='';$('#kitSearch').value='';render();window.scrollTo({top:0,behavior:'instant'});return;}
    if(button.hasAttribute('data-kit-reset')){state.category='all';state.section='all';state.query='';$('#kitSearch').value='';render();return;}
    if(button.dataset.kitClose){$(`#kit${button.dataset.kitClose==='drawer'?'Drawer':'Dialog'}`).close();return;}
    if(button.hasAttribute('data-kit-shift')){state.drawerIndex=(state.drawerIndex+Number(button.dataset.kitShift)+stocks.length)%stocks.length;drawDrawer();return;}
    if(button.hasAttribute('data-kit-expand')){setExpanded(!$('#kitDrawer').classList.contains('is-expanded'));return;}
    const handler=actions[button.dataset.kitAction];if(handler)handler(button,frameContext(button));
  });
  document.addEventListener('input',event=>{
    const input=event.target;
    if(input.id==='kitSearch'){state.query=input.value;state.section='all';render();return;}
    const frame=frameContext(input);if(!frame)return;
    if(input.hasAttribute('data-table-search')){tableState(frame).query=input.value;updateTable(frame);}
    if(input.hasAttribute('data-chart-range')){chartState(frame).count=Number(input.value);updateCharts(frame);}
    if(input.closest('[data-kit-form]') && input.hasAttribute('aria-invalid')){input.removeAttribute('aria-invalid');input.classList.remove('kit-input-error');const label=$(`[data-form-error="${input.name}"]`,input.closest('form'));if(label)label.hidden=true;}
  });
  document.addEventListener('change',event=>{
    const input=event.target,frame=frameContext(input);if(!frame)return;
    if(input.dataset.column){const s=tableState(frame);input.checked?s.hidden.delete(input.dataset.column):s.hidden.add(input.dataset.column);updateTable(frame);}
    if(input.hasAttribute('data-pattern-minimum')){tableState(frame).minimum=Number(input.value);updateTable(frame);}
  });
  document.addEventListener('submit',event=>{
    const form=event.target;if(!form.hasAttribute('data-kit-form'))return;event.preventDefault();
    const data=new FormData(form),quantity=Number(data.get('quantity')),price=Number(data.get('price'));
    const errors={quantity:!(quantity>0&&Number.isInteger(quantity))?'请输入大于0的整数数量':'',price:!(price>0&&Number.isFinite(price))?'请输入大于0的价格':''};
    Object.entries(errors).forEach(([name,message])=>{const field=form.elements[name],label=$(`[data-form-error="${name}"]`,form);field.setAttribute('aria-invalid',String(Boolean(message)));field.classList.toggle('kit-input-error',Boolean(message));label.textContent=message;label.hidden=!message;});
    const output=$('[data-form-preview]',form);output.hidden=Object.values(errors).some(Boolean);
    if(output.hidden){form.elements[Object.keys(errors).find(key=>errors[key])].focus();return;}
    output.innerHTML=`校验通过 · 数量 ${quantity.toLocaleString()} · 价格 ${price.toFixed(2)} 元<br>示例金额 ${(quantity*price).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})} 元${data.get('note')?`<br>备注：${escape(data.get('note'))}`:''}<br>仅供UI评审，没有保存到实际持仓。`;
  });
  document.addEventListener('reset',event=>{const form=event.target;if(!form.hasAttribute('data-kit-form'))return;$$('[data-form-error]',form).forEach(label=>label.hidden=true);$$('[aria-invalid]',form).forEach(input=>{input.removeAttribute('aria-invalid');input.classList.remove('kit-input-error');});$('[data-form-preview]',form).hidden=true;});
  document.addEventListener('pointermove',event=>{
    const host=event.target.closest('[data-chart-host]');if(!host)return;
    const frame=frameContext(host),bounds=host.getBoundingClientRect(),ratio=Math.min(.96,Math.max(.06,(event.clientX-bounds.left)/bounds.width)),s=chartState(frame),index=Math.min(s.count-1,Math.floor(ratio*s.count));
    $$('[data-chart-host]',frame).forEach(chart=>{
      const tip=$('.kit-chart-tip',chart),guide=$('.kit-chart-guide',chart),width=chart.clientWidth,x=ratio*width;
      guide.style.left=`${x}px`;guide.hidden=false;tip.hidden=false;
      tip.textContent=chart.dataset.chartHost==='price'?`样例点 ${index+1} / 价格 ${(26+Math.sin(index*.66)*1.24+index*.085).toFixed(2)}`:`样例点 ${index+1} / ${s.tier} ${(Math.sin(index*.66)*.48).toFixed(2)}亿`;
      tip.style.left=`${Math.max(8,Math.min(width-tip.offsetWidth-8,x+12))}px`;tip.style.top='8px';
    });
  });
  document.addEventListener('pointerout',event=>{const host=event.target.closest('[data-chart-host]');if(host&&!host.contains(event.relatedTarget)){const frame=frameContext(host);$$('.kit-chart-tip,.kit-chart-guide',frame).forEach(item=>item.hidden=true);}});
  $('#kitDrawer').addEventListener('cancel',event=>{if($('#kitDrawer').classList.contains('is-expanded')){event.preventDefault();setExpanded(false);}});
  ['kitDrawer','kitDialog'].forEach(id=>{
    $(`#${id}`).addEventListener('close',()=>{if(state.returnFocus?.isConnected)state.returnFocus.focus({preventScroll:true});});
    $(`#${id}`).addEventListener('click',event=>{if(event.target.id!==id)return;const bounds=event.target.getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)event.target.close();});
  });
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('dialog[open]')){const popovers=$$('[data-calendar], [data-columns-popover]').filter(element=>!element.hidden);if(popovers.length){event.preventDefault();closePopovers();}}});
  const categories=[['foundation','视觉基础'],['component','界面组件'],['pattern','组合样例']];
  $('#kitNav').innerHTML=categories.map(([key,name])=>`<div class="kit-nav-group"><div class="kit-nav-group-title">${name}</div>${families.filter(item=>item.category===key).map(item=>`<button class="kit-nav-button" data-kit-section="${item.id}"><span class="kit-nav-number">${item.number}</span>${item.name}</button>`).join('')}</div>`).join('');
  $('#familyCount').textContent=families.length;
  const params=new URLSearchParams(location.search);let saved;try{saved=localStorage.getItem(preferenceKey);}catch{}
  const view=params.get('theme')||saved;if(['light','dark','comparison'].includes(view))state.view=view;
  const section=params.get('section');if(families.some(item=>item.id===section))state.section=section;
  render();
})();
