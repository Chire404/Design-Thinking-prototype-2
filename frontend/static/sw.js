/* VeloPass Safe PWA Service Worker
 * Strictly caches ONLY static UI assets (/static/*).
 * NEVER caches or intercepts /api/*, QR tokens, balances, or transactions offline.
 */
const CACHE_NAME = "velopass-shell-v1";
const STATIC_ASSETS = [
  "/static/css/user.css",
  "/static/css/admin.css",
  "/static/js/api.js",
  "/static/js/user_app.js",
  "/static/js/admin_app.js",
  "/static/icons/logo.svg",
  "/static/icons/icon-192.svg",
  "/static/icons/icon-512.svg",
  "/manifest.json"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never cache API, SSE, or dynamic HTML pages — all financial & QR actions require live Backend
  if (url.pathname.startsWith("/api/") || event.request.method !== "GET") {
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        const networkFetch = fetch(event.request)
          .then((res) => {
            if (res && res.status === 200) {
              const clone = res.clone();
              caches.open(CACHE_NAME).then((c) => c.put(event.request, clone));
            }
            return res;
          })
          .catch(() => cached);
        return cached || networkFetch;
      })
    );
  }
});
