const CACHE_NAME = "parthunter-shell-v3";
const APP_FILES = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icons/parthunter-192.png",
  "./icons/parthunter-512.png",
  "./icons/parthunter.svg"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_FILES)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => (key.startsWith("parthunter-shell-") || key.startsWith("partscout-shell-")) && key !== CACHE_NAME).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

// Network first so updates to the app show up straight away; the cache is the offline fallback.
// Search requests (/api/) are never cached.
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(event.request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request).then(cached =>
      cached || (event.request.mode === "navigate" ? caches.match("./index.html") : Response.error())
    ))
  );
});
