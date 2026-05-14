const CACHE = 'emlak-pro-v2';

const PRECACHE = [
  '/',
  '/offline',
  '/musteriler',
  '/ilanlar',
  '/randevular',
  '/hatirlaticlar',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

// ── Kurulum: kritik kaynakları önbelleğe al ──
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

// ── Aktivasyon: eski cache'leri temizle ──
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// ── Push bildirimi ──
self.addEventListener('push', e => {
  let payload = {};
  try { payload = e.data ? e.data.json() : {}; } catch {}
  const title   = payload.title  || 'Emlak Pro Hatırlatıcı';
  const options = {
    body:    payload.body  || '',
    icon:    payload.icon  || '/static/icons/icon-192.png',
    badge:   payload.badge || '/static/icons/icon-192.png',
    data:    payload.data  || {},
    vibrate: [200, 100, 200],
    requireInteraction: true,
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/hatirlaticlar';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if ('focus' in c) return c.focus();
      }
      return clients.openWindow(url);
    })
  );
});

// ── Fetch: ağ önce, cache yedek ──
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;

  const url = new URL(e.request.url);

  // CDN kaynakları (Bootstrap, Bootstrap Icons) → cache önce
  if (url.hostname !== location.hostname) {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return res;
        });
      })
    );
    return;
  }

  // Sayfa navigasyonu → ağ önce, offline yedek
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return res;
        })
        .catch(() =>
          caches.match(e.request).then(c => c || caches.match('/offline'))
        )
    );
    return;
  }

  // Diğer (statik dosyalar) → cache önce, ağ yedek
  e.respondWith(
    caches.match(e.request).then(cached => {
      const network = fetch(e.request).then(res => {
        caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        return res;
      });
      return cached || network;
    })
  );
});
