/* Independent custom strategies. Shared navigation/date/modal are supplied by index.html. */
(function (root) {
  'use strict';
  const KEY = 'tradeiq.custom-picks.v1';
  const META_KEY = 'tradeiq.custom-picks.sync.v1';   // 同步元信息: 服务器版本号 rev
  const clone = value => JSON.parse(JSON.stringify(value));
  const id = () => 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const empty = () => ({mode: 'all', groups: [{name: '条件组 1', mode: 'all', rules: []}]});
  const E = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const templates = [
    {name:'缩量站上均线', note:'缩量 + 收盘高于20日线', rules:[['缩量至前5日均量以下','volume / AVG(volume, 5, 1) < 1'],['站上20日线','close > MA(20)']]},
    {name:'资金持续流入', note:'近10日流入至少7天 + 5日主力强度', rules:[['近10日主力流入至少7天','COUNT(main_net > 0, 10) >= 7'],['5日主力强度≥5%','SUM(main_net, 5) / SUM(amount, 5) * 100 >= 5']]},
    {name:'上涨未涨停', note:'涨幅超过5% + 未涨停 + 多头排列', rules:[['涨幅超过5%','change > 5'],['收盘未涨停','not limit_up'],['均线多头排列','close > MA(5) > MA(10) > MA(20) > MA(60)']]},
  ];
  const fallbackFields = [['close','收盘价','元'],['change','当日涨跌幅','%'],['volume','成交量','本地日线单位'],['amount','成交额','亿元'],['main_net','主力净额','亿元'],['super_net','超大单净额','亿元'],['big_net','大单净额','亿元'],['mid_net','中单净额','亿元'],['small_net','小单净额','亿元'],['high','最高价','元'],['low','最低价','元'],['open','开盘价','元'],['amplitude','日振幅','%'],['limit_up','收盘涨停','真假'],['down_count','下跌家数','家'],['up_count','上涨家数','家'],['flat_count','平盘家数','家'],['sh_change','上证涨跌幅','%']].map(([key,name,unit])=>({key,name,unit}));
  function normalize(value) {
    if (!value || !['all','any'].includes(value.mode) || !Array.isArray(value.groups) || value.groups.length > 6) throw Error('保存的条件格式无法读取');
    const ids = new Set(); let count = 0;
    value.groups.forEach(g => {
      if (!g || !['all','any'].includes(g.mode) || typeof g.name !== 'string' || g.name.length > 60 || !Array.isArray(g.rules)) throw Error('保存的条件组格式无法读取');
      g.rules.forEach(r => {
        if (!r || typeof r.id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(r.id) || ids.has(r.id) || typeof r.name !== 'string' || !r.name.trim() || r.name.length>100 || typeof r.formula !== 'string' || r.formula.length>1000 || typeof r.enabled !== 'boolean') throw Error('保存的条件格式无法读取');
        ids.add(r.id); count++;
      });
    });
    if(count>20) throw Error('保存条件超过20项');
    return clone(value);
  }
  function read(storage) {
    try { const text = storage.getItem(KEY); return {config:text ? normalize(JSON.parse(text)) : empty(), warning:''}; }
    catch (error) { return {config:empty(), warning:'保存的设置暂时无法读取：' + error.message + '。原保存内容尚未覆盖。'}; }
  }
  function save(storage, config) {
    try { storage.setItem(KEY, JSON.stringify(config)); return ''; }
    catch (_) { return '浏览器不允许保存，当前修改仅在本页面有效'; }
  }
  function readMeta(storage) {
    try { const text=storage.getItem(META_KEY); const meta=text?JSON.parse(text):{}; return {rev:Number(meta.rev)||0, updated_at:meta.updated_at||'', local_at:meta.local_at||''}; }
    catch (_) { return {rev:0, updated_at:'', local_at:''}; }
  }
  function writeMeta(storage, meta) {
    try { storage.setItem(META_KEY, JSON.stringify({rev:Number(meta.rev)||0, updated_at:meta.updated_at||'', local_at:meta.local_at||''})); } catch (_) {}
  }
  const nowStamp=()=>{const d=new Date(),p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;};
  function operand(field, calc, period, offset) {
    if (!fallbackFields.some(f=>f.key===field)) throw Error('请选择可用指标');
    const n=Number(period), o=Number(offset);
    if ((calc!=='current'&&(!Number.isInteger(n)||n<1||n>250))||!Number.isInteger(o)||o<0||o>250) throw Error('周期需为1～250，偏移需为0～250的整数');
    if(calc==='current') return o ? `REF(${field}, ${o})` : field;
    if(calc==='positive') return `COUNT(${field} > 0, ${n}, ${o})`;
    if(calc==='decline') return `ALL(${field} < REF(${field}, 1), ${n}, ${o})`;
    if(calc==='increase') return `ALL(${field} > REF(${field}, 1), ${n}, ${o})`;
    if(calc==='ret') return o ? `REF(RET(${n}), ${o})` : `RET(${n})`;
    if(!['SUM','AVG','MAX','MIN'].includes(calc)) throw Error('计算方式不支持');
    return `${calc}(${field}, ${n}, ${o})`;
  }
  function restoreDeletion(config, record) {
    const n=config.groups.reduce((sum,g)=>sum+g.rules.length,0);
    if(record.kind==='group') {
      if(config.groups.length>=6||n+record.group.rules.length>20)throw Error('恢复后会超过6组或20项，请先减少条件');
      config.groups.splice(Math.min(record.index,config.groups.length),0,record.group);
    } else {
      if(n>=20)throw Error('恢复后会超过20项，请先减少条件');
      if(!config.groups.includes(record.group)) {
        if(config.groups.length>=6)throw Error('恢复后会超过6组，请先减少条件组');
        config.groups.push(record.group);
      }
      record.group.rules.splice(Math.min(record.index,record.group.rules.length),0,record.rule);
    }
    return config;
  }
  const api = {KEY, META_KEY, normalize, read, save, readMeta, writeMeta, operand, templates, restoreDeletion};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (!root.document) return;
  const $ = key => document.getElementById(key);
  let storage; try {storage=root.localStorage;} catch (_) {storage={getItem(){throw Error('浏览器存储不可用');},setItem(){throw Error('浏览器存储不可用');}};}
  const loaded=read(storage);
  let config=loaded.config, warning=loaded.warning, payload=null, requestId=0, editId=null, editGroup=0;
  let catalog={fields:fallbackFields, functions:{}, examples:[]}, initialized=false, editorMode='visual', validated=null, drafts=[], undo=null, offset=0, query='', inspect='', debounce;
  let statusNote='条件和开关自动保存 · 最近N日默认包含筛选日';
  let uploadTimer=null, pulling=false, editing=false;
  const rules = () => config.groups.flatMap(g=>g.rules);
  const signature = () => JSON.stringify(config);
  const context = () => ({date:typeof picksSelDate==='undefined'?'':picksSelDate||'', group:typeof currentPicksGroup==='undefined'?'':currentPicksGroup});
  const fresh = (rid,basis) => rid===requestId && context().group==='custom' && !!document.querySelector('.tab-btn.active[data-tab="picks"]') && context().date===basis.date && signature()===basis.signature;
  async function post(path, body, force=false) {
    const init={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
    const res=await (path==='screen' ? _fetchPicksResult('/api/picks/custom/'+path,init,force) : fetch('/api/picks/custom/'+path,init));
    const data=await res.json(); if(!res.ok||data.status==='error') throw Error(data.message||'读取失败'); return data;
  }
  function persist() {
    warning=save(storage,config);
    const meta=readMeta(storage);
    writeMeta(storage,{rev:meta.rev,updated_at:meta.updated_at,local_at:nowStamp()});
    setStatus(warning||'已保存到本机 · 正在同步到服务器…');
    scheduleUpload();
  }
  function badge(count) {
    return !count||!count.evaluated ? '数据不足' : count.matched ? `${count.matched}个符合` : '全军覆没';
  }
  function renderGroups() {
    $('customOuterMode').value=config.mode;
    $('customSaveStatus').textContent=warning||statusNote;
    $('customRuleTotal').textContent=`开启 ${rules().filter(r=>r.enabled).length} / ${rules().length} 项`;
    $('customGroups').innerHTML=config.groups.map((g,gi)=>`<section class="custom-group"><div class="custom-group-head"><input type="text" maxlength="60" value="${E(g.name)}" data-group-name="${gi}" aria-label="条件组名称"><select data-group-mode="${gi}" aria-label="组内条件关系"><option value="all" ${g.mode==='all'?'selected':''}>全部满足 · 且</option><option value="any" ${g.mode==='any'?'selected':''}>任一满足 · 或</option></select><span class="custom-group-count">${g.rules.length}项</span><button type="button" class="custom-btn" data-add="${gi}">+ 条件</button><button type="button" class="custom-btn" data-delete-group="${gi}" aria-label="删除${E(g.name)}">删除组</button></div>${g.rules.length?g.rules.map(r=>`<div class="custom-rule${r.enabled?'':' is-off'}"><input type="checkbox" data-toggle="${E(r.id)}" aria-label="启用${E(r.name)}" ${r.enabled?'checked':''}><div class="custom-rule-copy"><div class="custom-rule-title"><strong>${E(r.name)}</strong><span class="custom-badge" data-count="${E(r.id)}">待统计</span>${r.enabled?'':'<span class="custom-badge">已关闭</span>'}</div><p>${E(r.explanation||'执行下方公式；编辑后可预览中文解释')}</p><code>${E(r.formula)}</code></div><div class="custom-rule-actions"><button type="button" data-edit="${E(r.id)}">编辑</button><button type="button" data-delete="${E(r.id)}">删除</button></div></div>`).join(''):'<div class="custom-group-empty">还没有条件<br>点击“+ 条件”，或从右侧示例开始</div>'}</section>`).join('');
    $('customUndo').hidden=!undo;
    if(undo) $('customUndoText').textContent=undo.label+'已删除';
    paintCounts();
  }
  function paintCounts(loading=false) {
    document.querySelectorAll('[data-count]').forEach(el=> {
      const c=payload?.counts?.[el.dataset.count];
      el.textContent=loading?'统计中…':badge(c);
      el.dataset.state=loading?'loading':!c||!c.evaluated?'missing':c.matched?'matched':'none';
      el.title=c?`全市场 ${c.total}只 · 已核验 ${c.evaluated}只 · 符合 ${c.matched}只 · 缺失或异常 ${c.missing}只；只统计本项，不受其他条件或开关影响`:'等待当前日期条件核验';
    });
  }
  function changed() {requestId++;offset=0;payload=null;persist();renderGroups();if(context().group==='custom'&&document.querySelector('.tab-btn.active[data-tab="picks"]'))load();}
  function findRule(rid) {for(const g of config.groups){const r=g.rules.find(r=>r.id===rid);if(r)return r;}return null;}
  function groupOptions() {return config.groups.map((g,i)=>`<option value="${i}">${E(g.name)} · ${g.mode==='all'?'全部满足':'任一满足'}</option>`).join('');}
  function fieldOptions() {return catalog.fields.map(f=>`<option value="${E(f.key)}">${E(f.name)}${f.unit==='真假'?'':` (${E(f.unit)})`}</option>`).join('');}
  function buildBox(prefix, title) {
    return `<div class="custom-builder-box"><h4>${title}</h4><label class="custom-field">指标<select id="${prefix}Field">${fieldOptions()}</select></label><label class="custom-field">计算方式<select id="${prefix}Calc"><option value="current">当前值</option><option value="AVG">平均值 / 均线</option><option value="SUM">合计</option><option value="MAX">最大值</option><option value="MIN">最小值</option><option value="positive">大于0的天数</option><option value="decline">连续递减</option><option value="increase">连续递增</option><option value="ret">累计收盘涨幅 (%)</option></select></label><div class="custom-mini-fields"><label class="custom-field">周期（交易日）<input id="${prefix}Period" type="number" min="1" max="250" value="5"></label><label class="custom-field">向前偏移（0含当日）<input id="${prefix}Offset" type="number" min="0" max="250" value="0"></label></div></div>`;
  }
  function mount() {
    if(initialized)return; initialized=true;
    $('customPicksContainer').innerHTML=`<div class="custom-picks"><header class="custom-top"><div><h3>自定义选股</h3><p>把自己的想法变成可复用的条件 · 本地收盘数据 · 东方财富资金</p></div><div class="custom-actions"><span id="customRuleTotal" class="custom-hint"></span><button type="button" class="custom-btn" data-open="chinese">中文添加</button><button type="button" class="custom-btn primary" data-open="visual">+ 添加条件</button></div></header><div class="custom-layout"><div><div class="custom-config-head"><label>条件组之间<select id="customOuterMode"><option value="all">全部满足 · 且</option><option value="any">任一满足 · 或</option></select></label><button type="button" class="custom-btn" id="customAddGroup">+ 添加条件组</button></div><div id="customGroups"></div><div id="customUndo" class="custom-undo" hidden><span id="customUndoText"></span><button type="button" class="custom-btn" id="customUndoBtn">撤销删除</button></div><div id="customSaveStatus" class="custom-save" aria-live="polite"></div></div><aside class="custom-aside"><h4>从一个示例开始</h4><p>添加后可以自由修改、关闭或删除。</p>${templates.map((t,i)=>`<button type="button" class="custom-template" data-template="${i}">${E(t.name)}<small>${E(t.note)}</small></button>`).join('')}<hr style="border:0;border-top:1px solid var(--border);margin:16px 0"><h4>怎么看数量标签</h4><p>每项单独统计全市场。组合后仍可能没有入选，展开下方诊断查看哪项挡住了。</p><p>缺少该项所需数据时显示“数据不足”；有数据但零符合时显示“全军覆没”。</p><button type="button" class="custom-btn" data-open="formula">查看公式与指标</button></aside></div><header class="custom-results-head"><div><h4 id="customSummary">先添加条件，开始选股</h4><p id="customCoverage">名单按当前选中日期的收盘数据筛选。</p></div><div class="custom-actions"><input id="customSearch" type="search" placeholder="名称或代码" aria-label="搜索自定义选股结果"><button type="button" class="custom-btn" id="customRefresh">刷新结果</button><button type="button" class="custom-btn" id="customPreviewBtn" title="把当前日期的自定义选股结果画成一张 PNG 长图 (和条件选股新同款), 存到 backend/previews/">🖼 生成预览图</button></div></header><details id="customDiagnostics" class="custom-diagnostics"><summary>为什么没选出来 / 条件影响诊断</summary><p>分别试算只移除这一项，其他条件和分组照常。试算不会修改设置；任一满足组中移除条件也可能减少入选。</p><div id="customDiagnosticGrid" class="custom-diagnostic-grid"></div><p id="customDataGaps"></p><div class="custom-inspect"><label for="customInspectCode">核验某只股票</label><input id="customInspectCode" type="text" placeholder="例如 600489.SH" maxlength="9"><button type="button" class="custom-btn" id="customInspectBtn">逐项检查</button></div><div id="customInspection" class="custom-inspection" aria-live="polite"></div></details><div class="custom-table-scroll"><table id="customPicksTable"><thead><tr><th>序号</th><th>名称 / 代码</th><th>收盘价</th><th>当日涨幅</th><th title="亿元；东方财富当日主力净额">当日主力</th><th>T+1</th><th>T+2</th><th>T+3</th><th>T+4</th><th>T+5</th><th>条件核验</th></tr></thead><tbody id="customPicksBody"><tr><td colspan="11" class="surge-empty">先添加并开启条件</td></tr></tbody></table></div><div class="custom-pagination"><span id="customPageInfo"></span><button type="button" class="custom-btn" id="customPrev">上一页</button><button type="button" class="custom-btn" id="customNext">下一页</button></div><p class="custom-hint" style="padding:0 18px 16px;font-size:11px">T+1～T+5：上行最高累计、下行收盘累计，均相对入选日收盘；最高为第N日当天最高价。未来结果只用于回看，不参与筛选。</p></div>`;
    const dialog=document.createElement('dialog');dialog.id='customEditor';dialog.className='custom-editor';
    dialog.innerHTML=`<div class="custom-editor-head"><h3 id="customEditorTitle">添加自定义条件</h3><button type="button" class="custom-btn" id="customCloseEditor" aria-label="关闭条件编辑器">关闭</button></div><div class="custom-editor-content"><div class="custom-editor-tabs"><button type="button" data-mode="visual">点选指标</button><button type="button" data-mode="formula">公式编辑</button><button type="button" data-mode="chinese">中文生成草稿</button></div><div class="custom-editor-fields"><label class="custom-field" id="customNameWrap">条件名称<input id="customRuleName" maxlength="100" placeholder="例如：近10日主力流入至少7天"></label><label class="custom-field">添加到条件组<select id="customRuleGroup"></select></label></div><div id="customVisual"><div class="custom-builder">${buildBox('customLeft','要判断的指标')}<div><label class="custom-field">比较关系<select id="customOperator"><option value=">">大于 &gt;</option><option value=">=">大于等于 ≥</option><option value="<">小于 &lt;</option><option value="<=">小于等于 ≤</option><option value="==">等于 =</option><option value="!=">不等于 ≠</option></select></label><label class="custom-field">与什么比较<select id="customRightType"><option value="value">固定数值</option><option value="metric">另一个指标</option></select></label><label class="custom-field" id="customValueWrap">数值（沿用左侧单位）<input id="customValue" type="number" step="any" value="0"></label><div id="customRightMetric" hidden>${buildBox('customRight','参照指标')}</div><p class="custom-help">金额填亿元，例如0.5；涨幅填百分数，例如5。连续递减 / 递增的结果请与“是 / 否”比较。</p><label class="custom-field" id="customBooleanWrap" hidden>真假结果<select id="customBoolean"><option value="True">是</option><option value="False">否</option></select></label></div></div><p class="custom-help">更复杂的比例、嵌套和突破条件，可切换到公式编辑。</p></div><div id="customFormulaMode" hidden><label class="custom-field">条件公式<textarea id="customFormulaInput" class="custom-formula" spellcheck="false" placeholder="COUNT(main_net > 0, 10) >= 7"></textarea></label><label class="custom-field">插入示例<select id="customFormulaExample"><option value="">选择一个示例…</option></select></label><p class="custom-help">用 and / or / not 连接判断；金额为亿元，涨幅为百分数。窗口默认含当日，第三个参数1表示排除当日。</p></div><div id="customChineseMode" hidden><label class="custom-field">用中文描述条件<textarea id="customChineseInput" class="custom-chinese" placeholder="最近10天至少7天主力流入，今天缩量，而且收盘站上20日线。"></textarea></label><p class="custom-help">支持涨幅范围、资金门槛、流入天数、缩量、均线、突破前高和主力强度。每句用逗号或“且”分开，数字请用阿拉伯数字。生成后先核对；识别不完整时不能加入。</p><button type="button" class="custom-btn" id="customChineseExample">填入示例</button></div><div id="customEditorPreview" class="custom-preview" hidden aria-live="polite"></div><details class="custom-help"><summary>可用指标与公式说明</summary><div id="customFormulaHelp"></div></details></div><div class="custom-editor-foot"><button type="button" class="custom-btn" id="customValidate">校验并预览</button><div class="custom-actions"><span id="customEditorStatus" class="custom-help"></span><button type="button" class="custom-btn primary" id="customSaveRule" disabled>保存条件</button></div></div>`;
    document.body.appendChild(dialog);
    $('customPicksContainer').addEventListener('click', e=>{
      const b=e.target.closest('button');if(!b)return;
      if(b.dataset.open)openEditor(b.dataset.open);
      if(b.dataset.add!==undefined)openEditor('visual',Number(b.dataset.add));
      if(b.dataset.edit) {const gi=config.groups.findIndex(g=>g.rules.some(r=>r.id===b.dataset.edit));openEditor('formula',gi,b.dataset.edit);}
      if(b.dataset.delete){const g=config.groups.find(g=>g.rules.some(r=>r.id===b.dataset.delete));const ri=g.rules.findIndex(r=>r.id===b.dataset.delete);const r=g.rules[ri];undo={kind:'rule',group:g,rule:r,index:ri,label:`“${r.name}”`};g.rules.splice(ri,1);changed();}
      if(b.dataset.deleteGroup!==undefined){const gi=Number(b.dataset.deleteGroup);undo={kind:'group',group:config.groups[gi],index:gi,label:`“${config.groups[gi].name}”组`};config.groups.splice(gi,1);if(!config.groups.length)config.groups=empty().groups;changed();}
      if(b.dataset.template!==undefined) addTemplate(Number(b.dataset.template));
    });
    $('customGroups').addEventListener('change',e=>{
      if(e.target.dataset.toggle){findRule(e.target.dataset.toggle).enabled=e.target.checked;changed();}
      if(e.target.dataset.groupMode!==undefined){config.groups[Number(e.target.dataset.groupMode)].mode=e.target.value;changed();}
      if(e.target.dataset.groupName!==undefined){config.groups[Number(e.target.dataset.groupName)].name=e.target.value.trim()||'条件组';changed();}
    });
    $('customOuterMode').onchange=()=>{config.mode=$('customOuterMode').value;changed();};
    $('customAddGroup').onclick=()=>{if(config.groups.length>=6)return notify('最多6个条件组');config.groups.push({name:`条件组 ${config.groups.length+1}`,mode:'all',rules:[]});changed();};
    $('customUndoBtn').onclick=()=>{if(!undo)return;try{restoreDeletion(config,undo);undo=null;changed();}catch(error){notify(error.message);}};
    $('customRefresh').onclick=()=>load(true);
    $('customPreviewBtn').onclick=generatePreview;
    $('customSearch').oninput=()=>{clearTimeout(debounce);query=$('customSearch').value.trim();offset=0;requestId++;debounce=setTimeout(()=>load(),300);};
    $('customPrev').onclick=()=>{offset=Math.max(0,offset-50);load();};
    $('customNext').onclick=()=>{offset+=50;load();};
    $('customInspectBtn').onclick=()=>{let v=$('customInspectCode').value.trim().toUpperCase();if(/^\d{6}$/.test(v))v+=v.startsWith('6')?'.SH':/^[489]/.test(v)?'.BJ':'.SZ';if(!/^\d{6}\.(SH|SZ|BJ)$/.test(v))return notify('请输入6位代码，或完整代码如600489.SH');inspect=v;load();};
    $('customCloseEditor').onclick=()=>dialog.close();
    dialog.addEventListener('close',()=>{editing=false;});
    dialog.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
    dialog.addEventListener('input',()=>invalidateEditor());
    dialog.addEventListener('change',e=>{invalidateEditor();syncBuilder();if(e.target.id==='customFormulaExample'&&e.target.value)$('customFormulaInput').value=e.target.value;});
    $('customValidate').onclick=validateEditor;
    $('customSaveRule').onclick=saveEditor;
    $('customChineseExample').onclick=()=>{$('customChineseInput').value='最近10天至少7天主力流入，今天缩量，而且收盘站上20日线。';invalidateEditor();};
    renderGroups();renderResults();
    fetch('/api/picks/custom/catalog').then(r=>r.json()).then(d=>{if(d.status==='success'){catalog=d;renderHelp();}}).catch(()=>{});
    renderHelp();
    watchRemote();
  }
  function notify(message) {if(typeof showToast==='function')showToast(message);else $('customSaveStatus').textContent=message;}
  function renderHelp() {
    $('customFormulaExample').innerHTML='<option value="">选择一个示例…</option>'+(catalog.examples.length?catalog.examples:templates.flatMap(t=>t.rules)).map(([name,f])=>`<option value="${E(f)}">${E(name)}</option>`).join('');
    $('customFormulaHelp').innerHTML=`<p>所有周期都是交易日，仅能引用筛选日及以前，最多向前250日；缺失日线或资金不当作0。</p><table>${catalog.fields.map(f=>`<tr><td><code>${E(f.key)}</code></td><td>${E(f.name)} · ${E(f.unit)}</td></tr>`).join('')}${Object.entries(catalog.functions).map(([f,n])=>`<tr><td><code>${E(f)}</code></td><td>${E(n)}</td></tr>`).join('')}</table><p>例：SUM(main_net, 5) / SUM(amount, 5) * 100 > 8<br>例：close > MAX(high, 20, 1)<br>REF仅引用过去；CROSS要求前日≤、当日>。真假条件可用 not。</p>`;
  }
  function openEditor(mode='visual',gi=0,rid=null) {
    if(!config.groups.length)config.groups=empty().groups;
    editId=rid;editGroup=gi;validated=null;drafts=[];
    $('customEditorTitle').textContent=rid?'编辑自定义条件':'添加自定义条件';
    $('customRuleGroup').innerHTML=groupOptions();$('customRuleGroup').value=String(gi);
    const r=rid?findRule(rid):null;
    $('customRuleName').value=r?.name||'';$('customFormulaInput').value=r?.formula||'';$('customChineseInput').value='';
    $('customLeftField').value='close';$('customLeftCalc').value='current';$('customLeftPeriod').value='5';$('customLeftOffset').value='0';
    $('customRightField').value='close';$('customRightCalc').value='AVG';$('customRightPeriod').value='20';$('customRightOffset').value='0';
    $('customOperator').value='>';$('customRightType').value='value';$('customValue').value='0';$('customBoolean').value='True';
    $('customEditorPreview').hidden=true;$('customEditorStatus').textContent='';
    setMode(mode);editing=true;$('customEditor').showModal();
  }
  function setMode(mode) {
    if(editId&&mode==='chinese')return notify('编辑单条条件请用点选或公式；中文入口用于新增多条');
    if(mode==='formula'&&editorMode==='visual'&&!$('customFormulaInput').value){try{$('customFormulaInput').value=visualFormula();}catch(_){}}
    editorMode=mode;
    $('customVisual').hidden=mode!=='visual';$('customFormulaMode').hidden=mode!=='formula';$('customChineseMode').hidden=mode!=='chinese';$('customNameWrap').hidden=mode==='chinese';
    $('customValidate').textContent=mode==='chinese'?'生成规则草稿':'校验并预览';$('customSaveRule').textContent=mode==='chinese'?'确认并加入条件':'保存条件';
    $('customEditor').querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
    invalidateEditor();syncBuilder();
  }
  function invalidateEditor() {validated=null;drafts=[];$('customSaveRule').disabled=true;$('customEditorPreview').hidden=true;$('customEditorStatus').textContent='';}
  function syncBuilder() {
    for(const prefix of ['customLeft','customRight']) {
      const field=$(prefix+'Field'), calc=$(prefix+'Calc');
      if(calc.value==='ret')field.value='close';
      if(field.value==='limit_up')calc.value='current';
      calc.disabled=field.value==='limit_up';
      $(prefix+'Period').disabled=calc.value==='current';
    }
    const bool=$('customLeftField').value==='limit_up'||['decline','increase'].includes($('customLeftCalc').value);
    $('customBooleanWrap').hidden=!bool;$('customValueWrap').hidden=bool||$('customRightType').value==='metric';$('customRightMetric').hidden=bool||$('customRightType').value!=='metric';
  }
  function visualFormula() {
    const opnd=p=>operand($(p+'Field').value,$(p+'Calc').value,$(p+'Period').value,$(p+'Offset').value);
    const left=opnd('customLeft'), bool=$('customLeftField').value==='limit_up'||['decline','increase'].includes($('customLeftCalc').value);
    if(bool)return `${left} == ${$('customBoolean').value}`;
    let right;if($('customRightType').value==='metric')right=opnd('customRight');
    else {if(!$('customValue').value.trim()||!Number.isFinite(Number($('customValue').value)))throw Error('请输入有效比较数值');right=String(Number($('customValue').value));}
    return `${left} ${$('customOperator').value} ${right}`;
  }
  async function validateEditor() {
    const mode=editorMode;
    $('customValidate').disabled=true;
    try {
      const f=mode==='visual'?visualFormula():$('customFormulaInput').value;
      const token=mode==='chinese'?$('customChineseInput').value:f;
      const data=await post(mode==='chinese'?'parse':'validate',mode==='chinese'?{text:token}:{formula:token});
      if(!$('customEditor').open||editorMode!==mode||(mode==='chinese'?$('customChineseInput').value:mode==='visual'?visualFormula():$('customFormulaInput').value)!==token)return;
      const p=$('customEditorPreview');p.hidden=false;p.className='custom-preview';
      const targetGroup=config.groups[Number($('customRuleGroup').value)];
      const relation=`加入“${targetGroup.name}”，与组内其他已开启条件按“${targetGroup.mode==='all'?'全部满足':'任一满足'}”判断；条件组之间按“${config.mode==='all'?'全部满足':'任一满足'}”判断。`;
      if(mode==='chinese') {
        p.innerHTML=`<strong>${data.complete?'已生成，核对后加入':'未能完整识别'}</strong><p>${E(relation)}</p>${data.rules.map(r=>`<div class="custom-chinese-draft"><b>${E(r.name)}</b><code>${E(r.formula)}</code><p>${E(r.explanation)}</p></div>`).join('')}${data.unknown.length?`<p style="color:var(--accent-orange)">未识别：${data.unknown.map(E).join('；')}</p>`:''}<p>${E(data.note)}</p>`;
        drafts=data.complete?data.rules:[];validated=data.complete?{token,mode}:null;
      } else {
        p.innerHTML=`<strong>执行口径</strong><code>${E(data.formula)}</code><p>${E(data.explanation)}</p><p>${E(relation)}</p><p>需要筛选日及此前最多 ${data.lookback} 个交易日的数据；缺失或异常记为数据不足。</p>`;
        validated={token,mode,...data};
      }
      $('customSaveRule').disabled=!validated;
    }catch(error){const p=$('customEditorPreview');p.hidden=false;p.className='custom-preview error';p.textContent=error.message;}
    finally {$('customValidate').disabled=false;}
  }
  function saveEditor() {
    if(!validated)return;
    const gi=Number($('customRuleGroup').value);
    if(!config.groups[gi])return;
    if(editorMode==='chinese') {
      if(rules().length+drafts.length>20)return notify('最多20项条件，请减少后再加入');
      drafts.forEach(r=>config.groups[gi].rules.push({id:id(),name:r.name,formula:r.formula,explanation:r.explanation,enabled:true}));
    } else {
      const name=$('customRuleName').value.trim()||validated.explanation.slice(0,100);
      if(!editId&&rules().length>=20)return notify('最多20项条件');
      const old=editId?findRule(editId):null;
      const rule={id:editId||id(),name,formula:validated.formula,explanation:validated.explanation,enabled:old?old.enabled:true};
      if(editId)config.groups.forEach(g=>g.rules=g.rules.filter(r=>r.id!==editId));
      config.groups[gi].rules.push(rule);
    }
    $('customEditor').close();changed();
  }
  async function addTemplate(index) {
    const t=templates[index];if(rules().length+t.rules.length>20)return notify('最多20项条件');
    const button=document.querySelector(`[data-template="${index}"]`);if(button)button.disabled=true;
    try {
      const validated=await Promise.all(t.rules.map(([name,formula])=>post('validate',{formula}).then(d=>({name,...d}))));
      if(rules().length+validated.length>20)return notify('最多20项条件，请减少后再添加');
      validated.forEach(r=>config.groups[0].rules.push({id:id(),name:r.name,formula:r.formula,explanation:r.explanation,enabled:true}));changed();
    }catch(error){notify(error.message);}finally{if(button)button.disabled=false;}
  }
  // ── 条件同步: 服务器是唯一真相源, 多台电脑连同一个后端就自动一致 ──
  // 规则: 服务器版本比本机新 → 用服务器的; 本机有从没同步过的条件 (另一台刚设好的那种)
  //       → 以本机为准推上去, 别让先同步的空版本把刚设好的条件顶掉.
  const hasAnyRule = cfg => !!(cfg && Array.isArray(cfg.groups) && cfg.groups.some(g => g.rules && g.rules.length));
  function setStatus(text) { statusNote=text; const el=$('customSaveStatus'); if(el) el.textContent=text; }
  async function fetchRemoteConfig() {
    try {
      const res=await fetch('/api/picks/custom/config');
      const data=await res.json();
      if(!res.ok||data.status!=='success') return null;
      return data;
    } catch (_) { return null; }
  }
  async function uploadConfig() {
    const meta=readMeta(storage);
    try {
      const res=await fetch('/api/picks/custom/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config})});
      const data=await res.json();
      if(res.ok&&data.status==='success'){
        writeMeta(storage,{rev:data.rev,updated_at:data.updated_at,local_at:meta.local_at});
        setStatus('已同步到服务器 · 两台电脑的条件一致');
        return true;
      }
    } catch (_) {}
    setStatus('已保存到本机 · 服务器暂时连不上，条件还没同步');
    return false;
  }
  function scheduleUpload() { clearTimeout(uploadTimer); uploadTimer=setTimeout(()=>{uploadTimer=null;uploadConfig();},700); }
  function applyRemote(data) {
    let next;
    try { next=normalize(data.config); } catch (_) { return; }
    config=next;
    save(storage,config);
    writeMeta(storage,{rev:data.rev,updated_at:data.updated_at,local_at:nowStamp()});
    renderGroups();
    if(context().group==='custom') load();
    setStatus('已从另一台设备同步最新条件');
    notify('已从另一台设备同步最新条件');
  }
  async function pullConfig() {
    if(pulling) return;
    pulling=true;
    try {
      const data=await fetchRemoteConfig();
      if(!data) return;
      const remoteRev=Number(data.rev)||0, meta=readMeta(storage);
      if(!data.config){
        // 服务器上还没有条件: 本机有条件就认领上传 (空条件不认领, 免得顶掉另一台电脑设好的)
        if(remoteRev===0 && meta.rev===0 && hasAnyRule(config)) uploadConfig();
        return;
      }
      if(remoteRev<=meta.rev) return;
      // 服务器版本更新 = 另一台电脑改过; 正在编辑或有未上传的改动时先不打断
      if(editing||uploadTimer) return;
      // 本机有从没同步过的条件且比服务器上的新 → 以本机为准推上去
      const localFresh=!meta.local_at||!data.updated_at||meta.local_at>=data.updated_at;
      if(meta.rev===0 && hasAnyRule(config) && localFresh){ uploadConfig(); return; }
      applyRemote(data);
    } finally { pulling=false; }
  }
  function watchRemote() {
    if(typeof setInterval==='function') setInterval(()=>{ if(!root.document||!root.document.hidden) pullConfig(); },30000);
    if(root.addEventListener) root.addEventListener('focus',()=>pullConfig());
    if(root.document&&root.document.addEventListener) root.document.addEventListener('visibilitychange',()=>{ if(!root.document.hidden) pullConfig(); });
  }
  async function load(force=false) {
    mount();if(typeof stopPicksAutoRefresh==='function')stopPicksAutoRefresh();
    pullConfig();
    if(force)root.PicksResultCache.invalidate(context().date);
    const basis={date:context().date,signature:signature()},rid=++requestId;
    payload=null;paintCounts(true);$('customSummary').textContent=`${basis.date?fmtDate(basis.date):'最近收盘日'} · 正在筛选…`;
    $('customCoverage').textContent='正在读取本地行情和条件所需数据';
    $('customPicksBody').innerHTML='<tr><td colspan="11" class="surge-empty">正在核验条件…</td></tr>';
    $('customDiagnosticGrid').innerHTML='';$('customDataGaps').textContent='';$('customInspection').textContent='';$('customPageInfo').textContent='';
    $('customPrev').disabled=true;$('customNext').disabled=true;
    try {
      const data=await post('screen',{date:basis.date,config,offset,limit:50,query,inspect},force);
      if(!fresh(rid,basis))return;
      payload=data;paintCounts();renderResults();renderDiagnostics();
    }catch(error){if(!fresh(rid,basis))return;$('customSummary').textContent='筛选未完成';$('customCoverage').textContent=error.message;$('customPicksBody').innerHTML=`<tr><td colspan="11" class="surge-empty">${E(error.message)}</td></tr>`;document.querySelectorAll('[data-count]').forEach(el=>{el.textContent='统计失败';el.dataset.state='error';});}
  }
  // 生成预览图: 后端按当前条件全市场筛一次 + playwright 截图, 版式同「条件选股新」
  async function generatePreview() {
    const btn=$('customPreviewBtn'),label=btn.textContent;
    btn.disabled=true;btn.textContent='⏳ 生成中...';
    notify('⏳ 正在生成预览图 (全市场筛选 + 截图, 约 15～40 秒)...');
    // 截图要十几秒, 浏览器只给 5 秒的"用户点击"有效期, 先开空白页占位, 好了再跳过去
    const win=root.open?root.open('','_blank'):null;
    try {
      const res=await fetch('/api/picks/custom/preview-image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:context().date,config})});
      let data={};try{data=await res.json();}catch(_){}
      if(res.ok&&data.status==='success'&&data.url){
        const origin=(root.location&&root.location.origin)||'';
        const href=origin+encodeURI(data.url);
        if(win)win.location.href=href;else notify(`✅ 预览图已生成: ${data.count} 只 · ${data.url}`);
      } else {
        if(win)win.close();
        notify('❌ '+(data.note||data.message||'生成失败'));
      }
    } catch(error) {
      if(win)win.close();
      notify('❌ 请求失败: '+error.message);
    } finally { btn.disabled=false;btn.textContent=label; }
  }
  function renderResults() {
    const p=payload;
    if(!p){$('customPageInfo').textContent='';$('customPrev').disabled=true;$('customNext').disabled=true;return;}
    $('customSummary').textContent=`${p.date?fmtDate(p.date):'—'} · 入选 ${p.count} 只`;
    const s=p.stats;$('customCoverage').textContent=p.daily_ready?`当日日线 ${s.total} 只 · 组合可判断 ${s.evaluated} 只 · 组合数据不足或异常 ${s.missing} 只 · ${p.note}`:p.note;
    const fixed=v=>v==null?'—':Number(v).toFixed(2);
    const signed=v=>v==null?'—':`<span style="color:${v>0?'#ff5050':v<0?'#4caf50':'var(--text-muted)'}">${v>0?'+':''}${fixed(v)}%</span>`;
    $('customPicksBody').innerHTML=p.results.length?p.results.map((r,i)=>`<tr data-code="${E(r.ts_code)}" data-name="${E(r.name)}" data-date="${E(r.trade_date)}"><td>${p.offset+i+1}</td><td><button type="button" class="surge-stock" data-custom-stock>${E(r.name||r.ts_code)}<span>${E(r.ts_code)}</span></button>${typeof renderSectorPills==='function'?`<div style="margin-top:3px">${renderSectorPills(r)}</div>`:''}</td><td>${fixed(r.close)}</td><td>${signed(r.change_pct)}</td><td><button type="button" class="surge-link" data-custom-flow style="color:${r.main_net_today>0?'#ff5050':r.main_net_today<0?'#4caf50':'var(--text-muted)'}">${fixed(r.main_net_today==null?null:r.main_net_today/1e8)}</button></td>${Array.from({length:5},(_,i)=>typeof _fmtPostPickTnCell==='function'?_fmtPostPickTnCell(r.custom_outcomes?.[i],r.custom_outcomes?.[i]?.reason==='intraday'):'<td>—</td>').join('')}<td><button type="button" class="custom-btn" data-custom-inspect>查看逐项</button></td></tr>`).join(''):`<tr><td colspan="11" class="surge-empty">${E(query&&p.count?'没有匹配的名称或代码':p.note)}</td></tr>`;
    $('customPicksBody').onclick=e=>{const b=e.target.closest('button');if(!b)return;const tr=b.closest('tr');if(b.hasAttribute('data-custom-stock'))showKlineModal(tr.dataset.code,E(tr.dataset.name),{pickDate:tr.dataset.date});if(b.hasAttribute('data-custom-flow'))openStockFlowDetail(tr.dataset.code,tr.dataset.name,'main');if(b.hasAttribute('data-custom-inspect')){inspect=tr.dataset.code;$('customInspectCode').value=inspect;$('customDiagnostics').open=true;renderInspection(p.results.find(r=>r.ts_code===inspect));}};
    const total=p.filtered_count;$('customPageInfo').textContent=total?`显示 ${p.offset+1}～${Math.min(p.offset+50,total)} / ${total} 只${query?'（搜索后）':''}`:'0 只';
    $('customPrev').disabled=!p.offset;$('customNext').disabled=p.offset+50>=total;
  }
  function renderDiagnostics() {
    if(!payload)return;
    $('customDiagnosticGrid').innerHTML=(payload.diagnostics||[]).map(d=>`<div class="custom-diagnostic-item">移除 ${E(d.name)}<strong>${d.without_count}只</strong><div class="custom-help">比当前 ${d.added>=0?'多':'少'} ${Math.abs(d.added)} 只</div></div>`).join('')||'<span class="custom-hint">开启条件后提供逐项试算。</span>';
    const gaps=rules().filter(r=>r.enabled&&payload.counts?.[r.id]?.missing).map(r=>`${r.name}：${payload.counts[r.id].missing}只数据不足`);
    $('customDataGaps').textContent=gaps.length?'各项缺口（会重叠）：'+gaps.join('；'):'已开启条件没有缺失数据，或当前没有可核验日线。';
    if(payload.inspected)renderInspection(payload.inspected);
  }
  function renderInspection(row) {
    const label={matched:'符合',unmet:'不符合',missing:'数据不足',inactive:'没有开启条件'};
    $('customInspection').innerHTML=`<p><strong>${E(row.name||row.ts_code)}</strong> · ${E(row.ts_code)} · 当前组合${label[row.state]||'符合'}${row.note?' · '+E(row.note):''}${row.entry_valid===false?' · 入选日价格数据异常':''}</p><ul>${rules().map(r=>{const d=row.rule_values?.[r.id];return `<li><b>${E(r.name)}</b> · ${r.enabled?'':'已关闭 · '}${label[d?.state]||'数据不足'}${d?.values?` · 比较值：${d.values.map(v=>v==null?'缺失':typeof v==='boolean'?v?'是':'否':Number(v).toFixed(4)).join(' / ')}`:''}</li>`;}).join('')}</ul>`;
  }
  root.CustomPicks={load, cache:()=>payload?.results||[], invalidate:()=>{requestId++;if($('customEditor')?.open)$('customEditor').close();}, ...api};
})(typeof window !== 'undefined' ? window : globalThis);
