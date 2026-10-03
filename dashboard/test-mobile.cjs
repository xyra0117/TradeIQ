'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const m=require('./mobile/app.js');
assert.equal(m.esc('<script>"&'), '&lt;script&gt;&quot;&amp;');
assert.equal(m.fmt(null),'—');
assert.equal(m.pct(0),'0.00%');
assert.equal(m.cash(100000000),'1.00亿');
assert.equal(m.dateText('20260911'),'2026-09-11');
assert.deepEqual(m.candle({open:null,close:3,low:1,high:4}),[null,null,null,null]);
assert.deepEqual(m.candle({open:2,close:3,low:1,high:4}),[2,3,1,4]);
assert.deepEqual(m.movingAverage([{close:1},{close:2},{close:3}],2),[null,1.5,2.5]);
assert.deepEqual(m.movingAverage([{close:null},{close:2}],2),[null,null]);
const rows=[{name:'测试A',ts_code:'000001.SZ',score:null},{name:'测试B',score:2},{name:'C',score:5}];
assert.deepEqual(m.filterRows(rows,'测试','desc','score').map(r=>r.name),['测试B','测试A']);
assert.equal(rows[0].name,'测试A');
assert.equal(m.filterRows(rows,'000001','default','score').length,1);
const handlers={};let responded=0,cacheWrites=0;
vm.runInNewContext(fs.readFileSync(__dirname+'/mobile/sw.js','utf8'),{
 self:{location:{origin:'https://desk.test'},addEventListener:(name,fn)=>handlers[name]=fn},
 URL,fetch:async()=>({ok:true,clone(){return this;}}),Response,
 caches:{open:async()=>({put:async()=>cacheWrites++})}
});
for(const url of ['/api/mobile/modules/positions','/api/mobile/health','/app.js?private=1','https://other.test/app.js']){
 handlers.fetch({request:{url:new URL(url,'https://desk.test').href,method:'GET'},respondWith:()=>responded++,waitUntil:()=>{}});
}
assert.equal(responded,0,'API responses must never be intercepted by cache code');
assert.equal(cacheWrites,0);
handlers.fetch({request:{url:'https://desk.test/app.js',method:'POST'},respondWith:()=>responded++,waitUntil:()=>{}});
assert.equal(responded,0);
console.log('Mobile formatting, filtering, chart data and cache isolation checks passed');
