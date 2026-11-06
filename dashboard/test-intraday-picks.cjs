const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const elements=new Map(),saved=new Map();
const element=id=>{
  if(!elements.has(id))elements.set(id,{style:{},textContent:'',value:'',attrs:{},setAttribute(k,v){this.attrs[k]=v;}});
  return elements.get(id);
};
let loads=0;
const ctx={console,Date,Map,URLSearchParams,currentPicksGroup:'dip',picksSelDate:'',
  localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},
  document:{readyState:'complete',getElementById:element},_loadCurrentPicksSub(){loads++;},
  setTimeout(fn){fn();},fetch:async()=>({ok:true,json:async()=>({running:false,status:'started'})})};
ctx.window=ctx;vm.createContext(ctx);
const source=fs.readFileSync(`${__dirname}/intraday-picks.js`,'utf8');vm.runInContext(source,ctx);
const ui=ctx.PicksIntraday;
assert.deepEqual({...ui.options()},{});
ui.setMode('intraday');
assert.match(ctx.picksSelDate,/^\d{8}$/);assert.equal(loads,1);
assert.equal(saved.get('tradeiq.picks.intraday-mode.v1'),'intraday');
let token=ui.token();
const live={date:ctx.picksSelDate,daily_ready:false,selection_ready:true,intraday_estimate:true,snapshot_id:'batch1',
  intraday:{quote_from:'2026-11-02 10:00:00',funds_from:'2026-11-02 09:59:00',quoted:5000,universe:5500,fresh_funds:4900,
    breadth:{down:2800,unknown:500},note:'当前满足，收盘可能变化'}};
assert.equal(ui.capture(live,token),true);
assert.equal(ui.options().snapshot_id,'batch1');
assert.match(ui.query(),/intraday=1&snapshot_id=batch1/);
assert.match(element('picksLiveNotice').textContent,/新浪行情.*东方财富资金.*2800～3300.*收盘可能变化/);
assert.equal(ui.options(ctx.picksSelDate,true).refresh,1);
assert.equal(ui.options().snapshot_id,undefined);
assert.equal(ui.capture(live,token),false,'迟到批次不得恢复过期数据');
token=ui.token();ui.capture(live,token);
ui.begin();assert.doesNotMatch(element('picksLiveNotice').textContent,/2800～3300/,'新筛选不得显示旧批次提示');
ctx.picksSelDate='20200102';assert.deepEqual({...ui.options()},{});ui.sync();
assert.equal(element('picksLiveFunds').style.display,'none');
ui.setMode('close');assert.deepEqual({...ui.options()},{});
assert.equal(ui.capture(live,token),false,'切换模式后拒绝旧响应');
vm.runInContext(source,ctx);assert.equal(ctx.PicksIntraday.isLive(),false,'模式偏好持久化');
assert.equal(saved.has('tradeiq.dip.disabled.v1'),false);assert.equal(saved.has('tradeiq.custom-picks.v1'),false);
console.log('PASS: shared live mode, today entry, persisted mode isolation, snapshot pin/refresh, historical fallback, timestamps, stale response rejection');
