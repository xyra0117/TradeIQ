/* Shared live batch identity. Historical result preferences remain independent. */
(function(root){
  'use strict';
  const KEY='tradeiq.picks.intraday-mode.v1', snapshots=new Map();
  let mode='close',epoch=0,lastPayload=null;
  try{if(root.localStorage.getItem(KEY)==='intraday')mode='intraday';}catch(_){}
  const today=()=>new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'}).replace(/-/g,'');
  const selected=()=>typeof picksSelDate==='string'?picksSelDate:'';
  const supported=()=>typeof currentPicksGroup==='string'&&['dip','dip2','custom'].includes(currentPicksGroup);
  const isLive=(date=selected())=>mode==='intraday'&&(!date||date===today());
  const token=()=>mode+'|'+selected()+'|'+epoch;
  function options(date=selected(),force=false){
    if(!isLive(date))return {};
    const day=date||today();
    if(force){snapshots.delete(day);epoch++;lastPayload=null;}
    return {intraday:1,...(snapshots.has(day)?{snapshot_id:snapshots.get(day)}:{}),...(force?{refresh:1}:{})};
  }
  const query=(date,force=false)=>new URLSearchParams(options(date,force)).toString();
  function begin(){lastPayload=null;sync();}
  function sync(){
    const group=root.document.getElementById('picksLiveModeGroup'),notice=root.document.getElementById('picksLiveNotice');
    if(group)group.style.display=supported()?'inline-flex':'none';
    if(notice)notice.style.display=supported()?'':'none';
    for(const value of ['intraday','close']){
      const b=root.document.getElementById('picksMode_'+value);
      if(b){b.setAttribute('aria-pressed',String(mode===value));b.style.color=mode===value?'var(--accent-cyan)':'var(--text-secondary)';}
    }
    const funds=root.document.getElementById('picksLiveFunds');
    if(funds)funds.style.display=supported()&&isLive()?'':'none';
    if(notice&&!lastPayload)notice.textContent=isLive()?'盘中预选 · 正在准备当前行情；资金与价格可能有不同更新时间。':'收盘确认 · 使用所选日期完整日线。';
  }
  function capture(payload,basis){
    if(basis!==token())return false;
    if(payload.snapshot_id)snapshots.set(payload.date,payload.snapshot_id);
    else if(payload.daily_ready)snapshots.delete(payload.date);
    lastPayload=payload;sync();
    const notice=root.document.getElementById('picksLiveNotice');
    if(!notice)return true;
    const m=payload.intraday;
    if(payload.intraday_estimate&&m){
      const b=m.breadth||{};
      notice.textContent=`${payload.date} · ${payload.awaiting_close?'待收盘确认':'盘中预选'} · 新浪行情 ${m.quote_from||'缺失'}${m.quote_to&&m.quote_to!==m.quote_from?' 至 '+m.quote_to:''} · 东方财富资金 ${m.funds_from||m.funds_latest_seen||'缺失'}${m.funds_to&&m.funds_to!==m.funds_from?' 至 '+m.funds_to:''} · 有效行情 ${m.quoted}/${m.universe} 只 · 有效当日资金 ${m.fresh_funds} 只 · 下跌家数范围 ${b.down??'—'}～${(b.down||0)+(b.unknown||0)} · ${m.note} ${payload.note&&payload.note!==m.note?payload.note:''}`;
    }else notice.textContent=(payload.daily_ready?'收盘确认 · ':'等待数据 · ')+(payload.note||'使用完整收盘数据');
    return true;
  }
  function setMode(value){
    if(!['close','intraday'].includes(value))return;
    mode=value;epoch++;lastPayload=null;snapshots.clear();
    try{root.localStorage.setItem(KEY,value);}catch(_){}
    if(value==='intraday'){
      picksSelDate=today();
      const dateInput=root.document.getElementById('picksDate');
      if(dateInput)dateInput.value=today().replace(/(\d{4})(\d{2})(\d{2})/,'$1-$2-$3');
      if(root.fpInstances?.picksDate)root.fpInstances.picksDate.setDate(dateInput.value,false);
    }
    sync();
    if(typeof _loadCurrentPicksSub==='function')_loadCurrentPicksSub();
  }
  async function syncFunds(){
    if(supported()&&typeof _loadCurrentPicksSub==='function')return _loadCurrentPicksSub(true);
  }

  root.PicksIntraday={isLive,options,query,token,capture,sync,begin,setMode,syncFunds};
  if(root.document.readyState==='loading')root.document.addEventListener('DOMContentLoaded',sync);else sync();
})(typeof window==='undefined'?globalThis:window);
