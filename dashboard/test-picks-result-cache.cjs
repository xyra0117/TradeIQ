const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',style:{},value:id==='surgeSort'?'code':''});
  return elements.get(id);
};
let requests = 0;
const storage = new Map();
const ctx = vm.createContext({console, URL, URLSearchParams, setTimeout, clearTimeout,
  document:{getElementById:element}, currentPicksGroup:'surge', picksSelDate:'20260911',
  fmtDate:d=>d, escapeHtml:s=>String(s), stopPicksAutoRefresh(){}, renderSectorPills(){return '';},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
  fetch:async url=>{requests++; const date=new URL(url,'http://localhost').searchParams.get('date');return {ok:true,status:200,json:async()=>({status:'success',date,daily_ready:true,count:0,results:[],stats:{eligible:4500}})};},
});
const cachePath = `${__dirname}/picks-result-cache.js`;
const source = fs.existsSync(cachePath) ? fs.readFileSync(cachePath,'utf8') : '';
if (fs.existsSync(cachePath)) vm.runInContext(fs.readFileSync(cachePath,'utf8'),ctx);
else ctx._fetchPicksResult=(...args)=>ctx.fetch(...args);
vm.runInContext(html.slice(html.indexOf('// ── 冲高观察：'),html.indexOf('// ── 冲高观察结束 ──')),ctx);
(async()=>{
  await vm.runInContext('loadSurgePicks()',ctx);
  ctx.picksSelDate='20260910'; await vm.runInContext('loadSurgePicks()',ctx);
  ctx.picksSelDate='20260911'; await vm.runInContext('loadSurgePicks()',ctx);
  assert.equal(requests,2,'A日期 → B日期 → A日期：成功的空名单应直接复用，不发第三次筛选请求');
  assert.match(element('surgeSummary').textContent,/20260911/);
  await vm.runInContext('loadSurgePicks(true)',ctx);
  assert.equal(requests,3,'显式刷新必须重新请求');
  await verifySharedCache();
  console.log('PASS: date round trip, all strategy/query isolation, persistent empty results, refresh, coalescing, races, cross-day T+N, live guards and storage failures');
})().catch(error=>{console.error(error);process.exitCode=1;});

async function verifySharedCache() {
  const disk = new Map(), events = [], calls = [];
  let now = Date.parse('2026-10-07T12:00:00+08:00'), next = null;
  class Clock extends Date { constructor(...args){super(...(args.length?args:[now]));} static now(){return now;} }
  const closed = date => ({status:'success',date,daily_ready:true,count:0,results:[]});
  const storage = {getItem:key=>disk.get(key)||null,setItem:(key,value)=>disk.set(key,value)};
  function open(store=storage, caches) {
    const sandbox = vm.createContext({URL, URLSearchParams, Response, Date:Clock, localStorage:store, caches,
      CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
      dispatchEvent:event=>events.push(event),
      fetch:async (url,init)=>{
        calls.push({url,init});
        const req=new URL(url,'http://localhost');
        const date=init?.body ? JSON.parse(init.body).date : req.searchParams.get('date');
        const data=typeof next==='function'?await next(url,init):next||closed(date||'20260930');
        return {ok:data.status!=='error',status:data.status==='error'?503:200,json:async()=>data};
      },
    });
    vm.runInContext(source,sandbox);return sandbox;
  }
  let cache=open();
  const endpoints=['/api/picks','/api/picks/ths','/api/conditional-picks/snapshot','/api/conditional-picks/new','/api/conditional-picks/preferred','/api/combined-picks/snapshot','/api/picks/ths/conditional/snapshot','/api/picks/ths/combined/snapshot','/api/picks/dip','/api/picks/dip-v2','/api/picks/surge'];
  for (const endpoint of endpoints) {
    const url=endpoint+'?date=20260930&threshold_yi=3';
    await cache._fetchPicksResult(url);
    const before=calls.length;
    await cache._fetchPicksResult(endpoint+'?threshold_yi=3&date=20260930');
    assert.equal(calls.length,before,endpoint+' should reuse even successful zero matches');
  }
  const count=calls.length;
  cache=open();
  for (const endpoint of endpoints) await cache._fetchPicksResult(endpoint+'?date=20260930&threshold_yi=3');
  assert.equal(calls.length,count,'a new page restores every strategy from browser storage');
  now+=3*86400000;
  for (const endpoint of endpoints) await cache._fetchPicksResult(endpoint+'?date=20260930&threshold_yi=3');
  assert.equal(calls.length,count,'settled historical dates survive beyond 24 hours');
  // Success without data readiness / wrong date / failure must be retried.
  for (const data of [{...closed('20260922'),daily_ready:false},closed('20260921'),{status:'error',date:'20260922',results:[]}]) {
    cache.PicksResultCache.invalidate();next=data;
    const start=calls.length;
    await cache._fetchPicksResult('/api/picks/dip?date=20260922');
    await cache._fetchPicksResult('/api/picks/dip?date=20260922');
    assert.equal(calls.length,start+2,'failed/missing/mismatched results are never retained');
  }
  next=null;cache.PicksResultCache.invalidate();
  for (const query of ['disabled=decline','disabled=volume','disabled=volume&return_days=60','disabled=volume&return_days=120'])
    await cache._fetchPicksResult('/api/picks/dip?date=20260930&'+query);
  assert.equal(JSON.parse(disk.get(cache.PicksResultCache.storageKey)).length,4,'parameters and disabled switches have separate results');
  // Full custom POST body including config, pagination/search/inspection is part of the identity.
  const config={mode:'all',groups:[{rules:[{id:'r1',enabled:true,formula:'close > MA(20)'}]}]};
  for (const changes of [{},{offset:50},{query:'银行'},{inspect:'600000.SH'},{config:{...config,mode:'any'}}]) {
    const body={date:'20260930',config,offset:0,query:'',inspect:'',...changes};
    const init={method:'POST',body:JSON.stringify(body)};
    await cache._fetchPicksResult('/api/picks/custom/screen',init);
    const before=calls.length;
    await cache._fetchPicksResult('/api/picks/custom/screen',init);
    assert.equal(calls.length,before,'custom query repeats reuse exactly its configuration/page');
  }
  // Current page mutations cannot alter the cached source payload.
  let result=await (await cache._fetchPicksResult('/api/picks/dip?date=20260930&disabled=decline')).json();
  result.results.push({name:'changed'});
  result=await (await cache._fetchPicksResult('/api/picks/dip?date=20260930&disabled=decline')).json();
  assert.equal(result.results.length,0);
  // Concurrent requests share one calculation; a forced refresh supersedes old work.
  cache.PicksResultCache.invalidate();let resolveOld;
  next=()=>new Promise(resolve=>{resolveOld=resolve;});
  const start=calls.length, first=cache._fetchPicksResult('/api/picks/surge?date=20260930');
  const duplicate=cache._fetchPicksResult('/api/picks/surge?date=20260930');
  assert.equal(calls.length,start+1,'coalesce pending requests');
  next={...closed('20260930'),count:1,results:[{name:'new'}]};
  await cache._fetchPicksResult('/api/picks/surge?date=20260930',undefined,true);
  resolveOld(closed('20260930'));await Promise.all([first,duplicate]);
  result=await (await cache._fetchPicksResult('/api/picks/surge?date=20260930')).json();
  assert.equal(result.results[0].name,'new','late calculation cannot replace a forced refresh');
  // Latest response aliases the explicit date, while latest itself can update on a new day.
  cache.PicksResultCache.invalidate();next=null;
  await cache._fetchPicksResult('/api/picks/surge');const latestCount=calls.length;
  await cache._fetchPicksResult('/api/picks/surge?date=20260930');
  assert.equal(calls.length,latestCount);
  next={...closed('20260930'),results:[{name:'refreshed'}]};
  await cache._fetchPicksResult('/api/picks/surge?date=20260930',undefined,true);
  result=await (await cache._fetchPicksResult('/api/picks/surge')).json();
  assert.equal(result.results[0].name,'refreshed','refreshing the concrete date updates its latest alias');
  cache.PicksResultCache.invalidate();next=null;
  await cache._fetchPicksResult('/api/combined-picks?date=20260930&threshold_yi=3');
  const fallbackCount=calls.length;
  await cache._fetchPicksResult('/api/combined-picks/snapshot?date=20260930&threshold_yi=3');
  assert.equal(calls.length,fallbackCount,'a successful combination fallback is reusable from the snapshot entry');
  // Incomplete future performance is shown immediately on later days, then quietly updated.
  cache.PicksResultCache.invalidate();next={...closed('20260930'),results:[{preferred_outcomes:[{reason:'future_unavailable'}]}]};
  await cache._fetchPicksResult('/api/conditional-picks/preferred?date=20260930');
  now+=86400000;let resolveNew;
  next=()=>new Promise(resolve=>{resolveNew=resolve;});
  result=await (await cache._fetchPicksResult('/api/conditional-picks/preferred?date=20260930')).json();
  assert.equal(result.results[0].preferred_outcomes[0].reason,'future_unavailable','show saved content while the update is pending');
  resolveNew({...closed('20260930'),results:[{preferred_outcomes:[{reason:null,high_sum_pct:12}]}]});
  for(let i=0;i<8;i++)await Promise.resolve();
  assert.equal(events.at(-1).detail.path,'/api/conditional-picks/preferred');
  result=await (await cache._fetchPicksResult('/api/conditional-picks/preferred?date=20260930')).json();
  assert.equal(result.results[0].preferred_outcomes[0].high_sum_pct,12);
  // Storage denial still allows reuse in this page; live responses never enter disk storage.
  cache=open({getItem(){throw Error('disabled');},setItem(){throw Error('quota');}});next=null;
  await cache._fetchPicksResult('/api/picks/dip?date=20260930');const localCount=calls.length;
  await cache._fetchPicksResult('/api/picks/dip?date=20260930');assert.equal(calls.length,localCount);
  cache=open();cache.PicksResultCache.invalidate();next={...closed('20260930'),live_next_index:1};
  await cache._fetchPicksResult('/api/picks/dip?date=20260930');
  assert.equal(JSON.parse(disk.get(cache.PicksResultCache.storageKey)).length,0);
  // Bounded disk storage does not prevent larger results being reused during this page.
  next={...closed('20260930'),results:[{name:'x'.repeat(1600000)}]};
  await cache._fetchPicksResult('/api/picks/surge?date=20260930');
  assert(disk.get(cache.PicksResultCache.storageKey).length*2<3*1024*1024);
  const largeCount=calls.length;await cache._fetchPicksResult('/api/picks/surge?date=20260930');assert.equal(calls.length,largeCount);
  // The complete archive retains results outside the size-limited fast copy.
  const archiveData = new Map();
  const archiveStore = {async put(key,value){archiveData.set(key,value.clone());},async match(key){return archiveData.get(key)?.clone();},async delete(key){return archiveData.delete(key.url||key);},async keys(){return [...archiveData.keys()].map(url=>({url}));}};
  const caches = {open:async()=>archiveStore};
  cache=open(storage,caches);cache.PicksResultCache.invalidate();
  await cache._fetchPicksResult('/api/picks/surge?date=20260930');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(archiveData.size,1,'large result goes into the complete archive');
  cache=open(storage,caches);const archivedCount=calls.length;
  await cache._fetchPicksResult('/api/picks/surge?date=20260930');
  assert.equal(calls.length,archivedCount,'reopening reuses archived dates outside localStorage capacity');
  // Refresh one date while preserving a different already viewed date.
  next=null;await cache._fetchPicksResult('/api/picks/surge?date=20260929');
  cache.PicksResultCache.invalidate('20260930');
  await new Promise(resolve=>setImmediate(resolve));
  const otherCount=calls.length;await cache._fetchPicksResult('/api/picks/surge?date=20260929');
  assert.equal(calls.length,otherCount);
  cache=open(storage,caches);await cache._fetchPicksResult('/api/picks/surge?date=20260930');
  assert.equal(calls.length,otherCount+1,'invalidated archive date is recalculated');
}
