/* COA Time Clock service worker — offline support for the worker app.
   Bump CACHE when /coa/clock or its core assets change. */
const CACHE = 'coa-clock-v1';
const CORE = ['/coa/clock', '/coa/contacts', '/static/cm/style.css'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(CORE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return; // API POSTs pass through
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  // Cache the clock + contacts pages (any query string) and the stylesheet.
  const cacheable = url.pathname === '/coa/clock' ||
                    url.pathname === '/coa/contacts' ||
                    url.pathname === '/static/cm/style.css';
  if (!cacheable) return;
  e.respondWith(
    caches.match(e.request).then((hit) => {
      const net = fetch(e.request).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
