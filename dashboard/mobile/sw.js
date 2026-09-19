/* Only this exact set of public app resources may enter Cache Storage. */
'use strict';
const VERSION = 'tradeiq-mobile-shell-v1';
const SHELL = ['/', '/app.css', '/app.js', '/manifest.webmanifest', '/icon-180.png', '/icon-192.png', '/icon-512.png', '/vendor/echarts.min.js'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(cache => cache.addAll(SHELL)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('tradeiq-mobile-shell-')&&key!==VERSION).map(key=>caches.delete(key)))).then(()=>self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // In particular, API requests and their failures are never cached or replayed.
  if(event.request.method!=='GET'||url.origin!==self.location.origin||url.search||!SHELL.includes(url.pathname))return;
  event.respondWith(fetch(event.request).then(response=>{
    if(response.ok) {
      const copy=response.clone();
      event.waitUntil(caches.open(VERSION).then(cache=>cache.put(event.request,copy)));
    }
    return response;
  }).catch(()=>caches.match(event.request).then(response=>response||Response.error())));
});
