/* Patrol Board service worker — offline support for the dashboard.
 *
 * - Dashboard (/) and QR image : stale-while-revalidate, always viewable offline
 * - App static files + Leaflet CDN : cache-first
 * - Admin/phone pages (/settings, /timecard, /login, /patrol-clock):
 *   network only; when offline a "Connection needed" page is shown
 * - APIs : network-first, falling back to the last cached response
 */
const CACHE = 'patrol-board-v1';
const PRECACHE = [
  '/patrol/',
  '/static/patrol/style.css',
  '/static/patrol/logo.png',
  '/patrol/qr.png',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'
];
const ONLINE_ONLY = ['/patrol/settings', '/patrol/timecard', '/patrol/login', '/patrol/patrol-clock'];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(PRECACHE.map(async (u) => {
      try {
        const r = await fetch(u);
        if (r.ok) await cache.put(u, r);
      } catch (_) { /* offline during install: skip */ }
    }));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

function offlinePage() {
  const html = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>Connection needed</title><style>' +
    'body{font-family:system-ui,sans-serif;background:#f1f5f9;color:#0f172a;' +
    'display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}' +
    '.box{background:#fff;border-radius:16px;padding:40px 48px;text-align:center;' +
    'box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:420px}' +
    'h1{font-size:1.4rem;margin:0 0 12px}.muted{color:#64748b}' +
    'a{display:inline-block;margin-top:20px;background:#2563eb;color:#fff;' +
    'padding:10px 24px;border-radius:10px;text-decoration:none;font-weight:700}' +
    '</style></head><body><div class="box"><h1>Connection needed</h1>' +
    '<p class="muted">This page needs an internet connection.<br>' +
    'The Patrol Board dashboard stays available offline.</p>' +
    '<a href="/patrol/">Back to Patrol Board</a></div></body></html>';
  return new Response(html, { headers: { 'Content-Type': 'text/html;charset=utf-8' } });
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  const fresh = fetch(req).then((res) => {
    if (res.ok) cache.put(req, res.clone());
    return res;
  }).catch(() => cached);
  return cached || fresh;
}

async function cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  if (cached) return cached;
  const res = await fetch(req);
  if (res.ok) cache.put(req, res.clone());
  return res;
}

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (_) {
    const cached = await cache.match(req);
    if (cached) return cached;
    throw _;
  }
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Admin / phone pages: online only
  if (ONLINE_ONLY.some((p) => url.pathname === p || url.pathname.startsWith(p + '/'))) {
    if (req.mode === 'navigate') {
      e.respondWith(fetch(req).catch(offlinePage));
    }
    return;
  }

  // Dashboard + QR: show instantly from cache, refresh in background
  if (url.pathname === '/patrol/' || url.pathname === '/patrol' || url.pathname === '/patrol/qr.png') {
    e.respondWith(staleWhileRevalidate(req));
    return;
  }

  // App static files + Leaflet CDN: cache-first
  if (url.pathname.startsWith('/static/') || url.pathname === '/patrol/sw.js' ||
      (url.origin === 'https://unpkg.com' && url.pathname.indexOf('/leaflet@') === 0)) {
    e.respondWith(cacheFirst(req));
    return;
  }

  // APIs: network-first, last cached response as fallback
  if (url.pathname.startsWith('/patrol/api/')) {
    e.respondWith(networkFirst(req));
    return;
  }
  // Everything else (e.g. map tiles): default network behavior
});
