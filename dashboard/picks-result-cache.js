/* Date/parameter-scoped desktop screening results. No database writes. */
(function (root) {
  'use strict';
  const STORAGE_KEY = 'tradeiq.picks.results.v1';
  const MAX_BYTES = 3 * 1024 * 1024;
  const ARCHIVE_NAME = 'tradeiq-picks-results-v1';
  const records = new Map(), pending = new Map(), versions = new Map(), active = new Map();
  let generation = 0;
  let archiveWork = Promise.resolve();
  const archive = root.caches ? root.caches.open(ARCHIVE_NAME).catch(()=>null) : Promise.resolve(null);
  const archiveUrl = key => (root.location?.origin || 'http://localhost') + '/__tradeiq_picks_result_cache__/' + encodeURIComponent(key);
  function archiveWrite(operation) {
    archiveWork = archiveWork.then(()=>archive).then(store=>store && operation(store)).catch(()=>{});
  }
  const copy = value => JSON.parse(JSON.stringify(value));
  const day = () => new Date().toLocaleDateString('sv-SE', {timeZone:'Asia/Shanghai'}).replace(/-/g,'');
  const stable = value => Array.isArray(value) ? value.map(stable) : value && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map(key=>[key,stable(value[key])])) : value;
  try {
    const saved = JSON.parse(root.localStorage.getItem(STORAGE_KEY) || '[]');
    if (Array.isArray(saved)) saved.forEach(([key,record])=>{
      if (typeof key === 'string' && record?.payload && record.day && record.at) records.set(key,record);
    });
  } catch (_) { /* Storage unavailable/corrupt: retain page-local caching. */ }
  // Migrate fast copies written before the complete archive was available.
  for (const [key,record] of records) {
    if (!record.live) archiveWrite(store=>store.put(archiveUrl(key),new root.Response(JSON.stringify(record))));
  }

  function request(url, init) {
    const parsed = new URL(url, 'http://localhost');
    parsed.searchParams.sort();
    const body = init?.body ? JSON.parse(init.body) : null;
    const method = init?.method || 'GET';
    const date = body?.date || parsed.searchParams.get('date') || '';
    const key = method + ' ' + parsed.pathname + '?' + parsed.searchParams + (body ? ' ' + JSON.stringify(stable(body)) : '');
    return {key,path:parsed.pathname,date,parsed,body,method};
  }
  function response(data, ok = true, status = 200) {
    return {ok,status,json:async()=>copy(data)};
  }
  function cacheable(data, req) {
    if (data.intraday_estimate || data.readiness_reason === 'intraday_live') return false;
    const date = data.date || data.trade_date;
    if (!/^\d{8}$/.test(date || '') || (req.date && date !== req.date) || (data.requested_date && req.date && data.requested_date !== req.date)) return false;
    if (['error','fail'].includes(data.status) || data.daily_ready === false || data.available === false) return false;
    if (/未落库|等待.*数据|尚无.*数据/.test(data.note || '')) return false;
    if (req.path === '/api/index/minline') return data.status === 'success' && Array.isArray(data.rows) && data.rows.length > 0;
    return Array.isArray(data.results) || Array.isArray(data.picks) || data.counts || data.singles || data.available === true
      || (data.status === 'ready' && Array.isArray(data.sectors));
  }
  function live(data) {
    return !!(data.live_next_index || data.intraday_used || data.readiness_reason === 'intraday_live'
      || (data.results || data.picks || []).some(row=>Object.values(row).some(value=>Array.isArray(value) && value.some(o=>o?.reason === 'intraday'))));
  }
  function unsettled(data) {
    const rows = data.results || data.picks || [];
    const nextDates = [...(data.next_dates || []), ...Object.keys(data).filter(key=>/^next_date(?:_\d+)?$/.test(key)).map(key=>data[key])];
    return rows.length > 0 && nextDates.some(date=>date && date > day()) || rows.some(row=>row.outcome10?.status !== undefined && row.outcome10.status !== 'complete'
      || Object.keys(row).some(key=>/^max_high_t[1-5]$/.test(key) && row[key] == null)
      || Object.values(row).some(value=>Array.isArray(value) && value.some(o=>o?.reason && o.reason !== 'intraday')));
  }
  function persist() {
    try {
      const saved = [...records].filter(([,record])=>!record.live).sort((a,b)=>(b[1].usedAt||b[1].at)-(a[1].usedAt||a[1].at));
      const kept = []; let bytes = 2;
      for (const entry of saved) {
        const size = JSON.stringify(entry).length * 2 + 2;
        if (bytes + size <= MAX_BYTES) { kept.push(entry); bytes += size; }
      }
      root.localStorage.setItem(STORAGE_KEY,JSON.stringify(kept));
    } catch (_) { /* Quota/disabled storage must not interrupt rendering. */ }
  }
  function save(req, data) {
    const record = {payload:copy(data),day:day(),at:Date.now(),live:live(data),unsettled:unsettled(data)};
    records.set(req.key,record);
    archiveWrite(store=>record.live ? store.delete(archiveUrl(req.key)) : store.put(archiveUrl(req.key),new root.Response(JSON.stringify(record))));
    // A successful latest lookup also serves the concrete date in the date picker.
    if (!req.date) {
      const date = data.date || data.trade_date;
      const body = req.body ? {...req.body,date} : null;
      const parsed = new URL(req.parsed);
      if (!body) parsed.searchParams.set('date',date);
      const alias = request(parsed.pathname + parsed.search,{method:req.method,body:body ? JSON.stringify(body) : undefined}).key;
      records.set(alias,record);
      archiveWrite(store=>record.live ? store.delete(archiveUrl(alias)) : store.put(archiveUrl(alias),new root.Response(JSON.stringify(record))));
    } else {
      const parsed = new URL(req.parsed); parsed.searchParams.delete('date');
      const body = req.body ? {...req.body,date:''} : null;
      const alias = request(parsed.pathname + parsed.search,{method:req.method,body:body ? JSON.stringify(body) : undefined}).key;
      const previous = records.get(alias);
      if ((previous?.payload.date || previous?.payload.trade_date) === (data.date || data.trade_date)) {
        records.set(alias,record);
        archiveWrite(store=>record.live ? store.delete(archiveUrl(alias)) : store.put(archiveUrl(alias),new root.Response(JSON.stringify(record))));
      }
    }
    persist();
  }
  function stale(record, req) {
    if (record.live && Date.now()-record.at >= 30000) return true;
    if ((!req.date || req.date >= day()) && Date.now()-record.at >= 30000) return true;
    const payload = record.payload, today = day();
    const dates = [...(payload.next_dates || []), ...Object.keys(payload).filter(key=>/^next_date(?:_\d+)?$/.test(key)).map(key=>payload[key])];
    const hour = Number(new Date().toLocaleTimeString('en-GB',{timeZone:'Asia/Shanghai',hour:'2-digit',hour12:false}));
    if (dates.includes(today) && hour >= 9 && hour < 16 && Date.now()-record.at >= 30000) return true;
    return record.day !== day() && (!req.date || req.date >= record.day || record.unsettled || record.live);
  }
  function network(url, init, req) {
    if (pending.has(req.key)) return pending.get(req.key);
    const epoch = generation, version = versions.get(req.key) || 0;
    const task = (async()=>{
      const res = await root.fetch(url,init);
      const data = await res.json();
      if (res.ok && cacheable(data,req) && epoch === generation && version === (versions.get(req.key)||0)) {
        save(req,data);
        // A successful historical combination fallback also answers its snapshot lookup.
        if (req.path === '/api/combined-picks' && !req.parsed.searchParams.has('intraday')) {
          save(request('/api/combined-picks/snapshot' + req.parsed.search,init),data);
        }
      }
      return response(data,res.ok,res.status);
    })();
    pending.set(req.key,task);
    task.finally(()=>{if (pending.get(req.key)===task) pending.delete(req.key);}).catch(()=>{});
    return task;
  }
  function reuse(record, req, url, init) {
    record.usedAt = Date.now();
    if (stale(record,req)) {
      network(url,init,req).then(()=>{
        if (records.get(req.key) !== record && active.get(req.path) === req.key && root.dispatchEvent && root.CustomEvent)
          root.dispatchEvent(new root.CustomEvent('tradeiq:picks-result-updated',{detail:{path:req.path,key:req.key}}));
      }).catch(()=>{});
    }
    return response(record.payload);
  }
  function fetchResult(url, init, force = false) {
    const req = request(url,init);
    active.set(req.path,req.key);
    if (force) {
      records.delete(req.key); versions.set(req.key,(versions.get(req.key)||0)+1); pending.delete(req.key);
      archiveWrite(store=>store.delete(archiveUrl(req.key)));
    }
    const record = records.get(req.key);
    if (record && !cacheable(record.payload,req)) { records.delete(req.key); return network(url,init,req); }
    if (record && !force) {
      return Promise.resolve(reuse(record,req,url,init));
    }
    if (!force && root.caches) return (async()=>{
      const epoch = generation, version = versions.get(req.key)||0;
      try {
        await archiveWork;
        const store = await archive, saved = store && await store.match(archiveUrl(req.key));
        const restored = saved && await saved.json();
        if (restored?.payload && !restored.live && cacheable(restored.payload,req) && epoch === generation && version === (versions.get(req.key)||0)) {
          records.set(req.key,restored); persist();
          return reuse(restored,req,url,init);
        }
      } catch (_) { /* Archive unavailable: fall back to the query. */ }
      return network(url,init,req);
    })();
    return network(url,init,req);
  }
  function invalidate(date) {
    generation++; pending.clear();
    if (date) {
      for (const [key,record] of records) {
        if ((record.payload.date || record.payload.trade_date) === date || (!key.includes('date=') && !/"date":"\d{8}"/.test(key))) records.delete(key);
      }
    } else records.clear();
    archiveWrite(async store=>{
      for (const key of await store.keys()) {
        const identity = decodeURIComponent(new URL(key.url).pathname.split('/__tradeiq_picks_result_cache__/')[1] || '');
        if (!date || identity.includes('date='+date) || identity.includes('"date":"'+date+'"') || (!identity.includes('date=') && !/"date":"\d{8}"/.test(identity))) await store.delete(key);
      }
    });
    persist();
  }
  root._fetchPicksResult = fetchResult;
  root.PicksResultCache = {invalidate,storageKey:STORAGE_KEY};
})(typeof window !== 'undefined' ? window : globalThis);
