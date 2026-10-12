// Lets the GPS tracker open with no mobile signal (at a depot, in a basement
// car park). Network first, so a new version is picked up whenever there is
// signal; the saved copy is used only when the network fails. Only the
// tracker's own files are handled; every other request is left alone.
const CACHE = "bus-tracker-v1";
const FILES = ["track.html", "track.webmanifest", "track-icon-192.png", "track-icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  const file = url.pathname.split("/").pop();
  if (e.request.method !== "GET" || url.origin !== location.origin || !FILES.includes(file)) return;
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(file, copy)); }
        return res;
      })
      .catch(() => caches.match(file))
  );
});
