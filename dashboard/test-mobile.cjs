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
const heatItem={sector:'PCB',recent5_count:24,appearance5:3,heat:{key:'cooling',label:'已经降温',reason:'今日0只',rank:1,in_board:true},timeline:[{date:'20260929',count:8},{date:'20260930',count:0}]};
const newItem={...heatItem,sector:'电池',heat:{key:'new',label:'新近活跃',rank:2,in_board:true}};
const heatData={items:[heatItem,newItem],summary:{recent5_complete:true,heat_groups:{active:['<script>'],new:['电池'],cooling:['PCB']}}};
assert(m.hotOverview(heatData.summary).includes('&lt;script&gt;'));
assert(m.hotHeatBoard(heatData,'all').includes('hot-cell-0'));
assert(m.hotHeatBoard(heatData,'cooling').includes('PCB'));
assert(!m.hotHeatBoard(heatData,'cooling').includes('电池'));
assert(m.hotHeatBoard(heatData,'active').includes('电池'));
assert(!m.hotHeatBoard(heatData,'active').includes('PCB'));
assert(m.hotHeatBoard(heatData,'all','电池').includes('#2'));
assert(m.hotHeatBoard(heatData,'cooling','电池').includes('没有匹配'));
assert(m.hotHeatBoard({items:[{...heatItem,timeline:[{date:'20260930',count:null}]}],summary:{recent5_complete:false}}).includes('缺数据'));
assert(m.hotOverview({recent5_complete:false}).includes('数据不足，暂不判断'));
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
