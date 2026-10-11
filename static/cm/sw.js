/* On Site service worker — offline support for the worker app.
   Bump CACHE when /cm/onsite or its core assets change. */
const CACHE = 'onsite-v1';
const CORE = ['/cm/onsite', '/static/cm/style.css'];

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

function isQrLib(url) {
  return url.origin === 'https://unpkg.com' &&
         url.pathname.startsWith('/html5-qrcode');
}

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return; // API POSTs pass through
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin && !isQrLib(url)) return;
  const cacheable = CORE.includes(url.pathname) || isQrLib(url);
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
