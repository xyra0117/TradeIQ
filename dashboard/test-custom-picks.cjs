const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const cp = require('./custom-picks.js');

const preferences = {mode:'all',groups:[{name:'自定义组',mode:'all',rules:[{id:'r1',name:'站上均线',formula:'close > MA(20)',enabled:false}]}]};
const store = new Map([['tradeiq.dip.disabled.v1','["decline"]'],['tradeiq.chase.disabled.v1','["gain"]']]);
const storage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)};
assert.equal(cp.save(storage,preferences),'');
assert.deepEqual(cp.read(storage).config,preferences);
assert.equal(cp.read(storage).config.groups[0].rules[0].enabled,false);
assert.equal(store.get('tradeiq.dip.disabled.v1'),'["decline"]');
assert.equal(store.get('tradeiq.chase.disabled.v1'),'["gain"]');
assert.match(cp.save({setItem(){throw Error('blocked');}},preferences),/不允许保存/);
assert.match(cp.read({getItem(){return '{bad';}}).warning,/尚未覆盖/);
assert.throws(()=>cp.normalize({mode:'all',groups:[{name:'x',mode:'all',rules:[{...preferences.groups[0].rules[0],enabled:'false'}]}]}));
assert.throws(()=>cp.normalize({mode:'all',groups:[{name:'x',mode:'all',rules:[preferences.groups[0].rules[0],preferences.groups[0].rules[0]]}]}));
assert.equal(cp.operand('main_net','positive',10,0),'COUNT(main_net > 0, 10, 0)');
assert.equal(cp.operand('volume','decline',3,0),'ALL(volume < REF(volume, 1), 3, 0)');
assert.equal(cp.operand('close','AVG',60,0),'AVG(close, 60, 0)');
assert.equal(cp.operand('close','ret',120,1),'REF(RET(120), 1)');
assert.throws(()=>cp.operand('close','AVG',0,0));
assert.throws(()=>cp.operand('close','AVG',5,-1));
assert.throws(()=>cp.operand('close','AVG',1.5,0));
assert.equal(cp.operand('close','current',0,0),'close');
const undoConfig=cp.normalize(preferences);
const undoGroup=undoConfig.groups[0];const removed=undoGroup.rules.splice(0,1)[0];
undoGroup.rules.push({...removed,id:'new',name:'后来新增'});
cp.restoreDeletion(undoConfig,{kind:'rule',group:undoGroup,rule:removed,index:0});
assert.deepEqual(undoGroup.rules.map(r=>r.id),['r1','new']);

// Delayed HTTP responses must not repaint another tab/date or an edited condition set.
const elements = new Map();
class Element {
  constructor(key=''){this.id=key;this.value='';this.dataset={};this.style={};this.listeners={};this.classList={toggle(){}};this.open=false;}
  set innerHTML(html){this._html=html;for(const match of html.matchAll(/\bid="([^"]+)"/g))get(match[1]);}
  get innerHTML(){return this._html||'';}
  addEventListener(name,fn){this.listeners[name]=fn;}
  querySelectorAll(){return [];}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
function get(key){if(!elements.has(key))elements.set(key,new Element(key));return elements.get(key);}
const badge={dataset:{count:'r1'},textContent:'',title:''};
let picksActive=true;
const doc={getElementById:get,querySelector(){return picksActive?{}:null;},querySelectorAll(selector){return selector==='[data-count]'?[badge]:[];},createElement(){return new Element();},body:{appendChild(el){elements.set(el.id,el);}}};
const calls=[];
const toasts=[];
const window={document:doc,localStorage:storage};
const sandbox={window,document:doc,currentPicksGroup:'custom',picksSelDate:'20260805',fmtDate:v=>v,stopPicksAutoRefresh(){},showToast(message){toasts.push(message);},setTimeout,clearTimeout,console,
  fetch(path,opts){if(path.endsWith('catalog'))return Promise.resolve({json:async()=>({status:'error'})});
    const entry={path,method:(opts&&opts.method)||'GET',body:opts&&opts.body?JSON.parse(opts.body):null,taken:false};
    return new Promise(resolve=>{entry.resolve=value=>{entry.taken=true;resolve(value);};calls.push(entry);});}};
sandbox._fetchPicksResult=(...args)=>sandbox.fetch(...args);
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(require.resolve('./custom-picks.js'),'utf8'),sandbox);
const take=(path,method='POST',nth=0)=>calls.filter(c=>c.path===path&&c.method===method&&!c.taken)[nth];
const result=(date,count=3)=>({status:'success',date,daily_ready:true,count,filtered_count:count,results:[],offset:0,limit:50,note:'测试',stats:{total:5,evaluated:5,missing:0},counts:{r1:{total:5,evaluated:5,matched:count,missing:0}},diagnostics:[]});
const reply=(call,data)=>call.resolve({ok:true,json:async()=>data});
const tick=async(n=4)=>{for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r));};
(async()=>{
  const first=window.CustomPicks.load();
  sandbox.currentPicksGroup='dip2';reply(take('/api/picks/custom/screen'),result('20260805',9));await first;
  assert.match(get('customSummary').textContent,/正在筛选/);
  sandbox.currentPicksGroup='custom';
  const old=window.CustomPicks.load();sandbox.picksSelDate='20260806';const latest=window.CustomPicks.load();
  reply(take('/api/picks/custom/screen','POST',1),result('20260806',2));await latest;
  reply(take('/api/picks/custom/screen','POST',0),result('20260805',8));await old;
  assert.equal(get('customSummary').textContent,'20260806 · 入选 2 只');
  assert.equal(badge.textContent,'2个符合');
  const editing=window.CustomPicks.load();
  get('customGroups').listeners.change({target:{dataset:{toggle:'r1'},checked:true}});
  assert.equal(JSON.parse(store.get(cp.KEY)).groups[0].rules[0].enabled,true);
  reply(take('/api/picks/custom/screen'),result('20260806',7));await editing;
  assert.match(get('customSummary').textContent,/正在筛选/);
  reply(take('/api/picks/custom/screen'),result('20260806',0));await tick();
  assert.equal(badge.textContent,'全军覆没');
  const missing=window.CustomPicks.load();const p=result('20260806',0);p.counts.r1.evaluated=0;p.counts.r1.missing=5;
  reply(take('/api/picks/custom/screen'),p);await missing;assert.equal(badge.textContent,'数据不足');
  const hidden=window.CustomPicks.load();picksActive=false;reply(take('/api/picks/custom/screen'),result('20260806',99));await hidden;
  assert.match(get('customSummary').textContent,/正在筛选/);
  const html=fs.readFileSync(require.resolve('./index.html'),'utf8');
  assert.match(html,/key: 'dip2', label: '追高选股'\}, \{key: 'custom', label: '自定义选股'\}/);
  assert.match(html,/currentPicksGroup === 'custom'\) return CustomPicks.load\(force\)/);
  assert.match(html,/customPicksContainer.*?display:none/);
  assert.match(html,/custom-picks.css\?v=20261102-4/);
  assert.match(html,/custom-picks.js\?v=20261102-4/);
  // 预览图按钮: 带上当前日期和当前条件, 成功后提示结果, 失败提示原因
  const previewButton=get('customPreviewBtn');
  assert.equal(typeof previewButton.onclick,'function');
  const pending=previewButton.onclick();
  const previewCall=calls[calls.length-1];
  assert.equal(previewCall.body.date,sandbox.picksSelDate);
  assert.equal(previewCall.body.config.groups[0].rules.length,1);
  reply(previewCall,{status:'success',count:12,url:'/previews/自定义选股_20260806_ab12cd.png'});
  await pending;
  assert.ok(toasts.some(message=>/预览图已生成: 12 只/.test(message)),toasts.join('|'));
  const failing=previewButton.onclick();
  const failCall=calls[calls.length-1];
  reply(failCall,{status:'error',message:'没有选出股票'});
  await failing;
  assert.ok(toasts.some(message=>/没有选出股票/.test(message)),toasts.join('|'));
  // 条件同步: 服务器还没有条件时本机认领上传; 服务器版本更新时本机换成服务器那份
  reply(take('/api/picks/custom/config','GET'),{status:'success',config:null,rev:0});
  await tick();
  const claim=take('/api/picks/custom/config','POST');
  assert.ok(claim,'本机条件应上传到服务器');
  assert.equal(claim.body.config.groups[0].rules[0].id,'r1');
  reply(claim,{status:'success',rev:1,updated_at:'2026-10-19 10:00:00'});
  await tick();
  assert.equal(JSON.parse(store.get(cp.META_KEY)).rev,1);
  const remote={mode:'all',groups:[{name:'远端组',mode:'all',rules:[{id:'rx',name:'远端条件',formula:'close > MA(60)',enabled:true,explanation:'远端'}]}]};
  await new Promise(r=>setTimeout(r,800));   // 等本机未上传的改动先传完, 别把远端版本吃掉
  window.CustomPicks.load();
  reply(take('/api/picks/custom/config','GET'),{status:'success',config:remote,rev:7});
  await tick(6);
  assert.equal(JSON.parse(store.get(cp.KEY)).groups[0].rules[0].id,'rx');
  assert.equal(JSON.parse(store.get(cp.META_KEY)).rev,7);
  assert.ok(toasts.some(message=>/已从另一台设备同步最新条件/.test(message)),toasts.join('|'));
  // 本机有从没同步过的条件 → 以本机为准推上去, 不被服务器上的旧版本顶掉
  for(const call of calls.filter(c=>c.path==='/api/picks/custom/config'&&c.method==='POST'&&!c.taken))
    call.resolve({ok:false,json:async()=>({status:'error',message:'测试丢弃'})});
  store.set(cp.META_KEY,JSON.stringify({rev:0,updated_at:'',local_at:''}));
  const otherConfig={mode:'all',groups:[{name:'别的组',mode:'all',rules:[{id:'ry',name:'别的条件',formula:'close > MA(90)',enabled:true}]}]};
  window.CustomPicks.load();
  reply(take('/api/picks/custom/config','GET'),{status:'success',config:otherConfig,rev:9});
  await tick(6);
  const push=take('/api/picks/custom/config','POST');
  assert.ok(push,'本机没同步过的条件应推上去, 而不是被服务器顶掉');
  assert.equal(push.body.config.groups[0].rules[0].id,'rx');
  assert.equal(JSON.parse(store.get(cp.KEY)).groups[0].rules[0].id,'rx');
  console.log('Custom picks: persistence, formula builder, missing/zero counts, delayed responses, preview image and condition sync passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
