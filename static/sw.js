/**
 * QueryLux Service Worker
 * Caches static assets for offline/fast load. API calls always go to network.
 */

const CACHE = 'querylux-v1';

// Assets to pre-cache on install
const PRECACHE = [
  '/',
  '/firebase-config.js',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  'https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js',
  'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth-compat.js',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js',
  'https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(PRECACHE).catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  // Remove old caches
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  // Always fetch API and WebSocket requests from network
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) {
    e.respondWith(fetch(request));
    return;
  }

  // For navigation requests (page loads), serve index.html from cache or network
  if (request.mode === 'navigate') {
    e.respondWith(
      fetch(request).catch(() => caches.match('/'))
    );
    return;
  }

  // For everything else: cache-first, fallback to network
  e.respondWith(
    caches.match(request).then(cached => {
      if (cached) return cached;
      return fetch(request).then(response => {
        // Cache successful GET responses (not opaque cross-origin)
        if (response.ok && request.method === 'GET') {
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(request, clone));
        }
        return response;
      }).catch(() => cached);
    })
  );
});
