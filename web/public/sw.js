const CACHE = "projects-hub-shell-v2";
const SHELL = ["/", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(cache =>
      Promise.all(SHELL.map(url => fetch(url, { cache: "no-store" }).then(response => {
        if (response.ok) return cache.put(url, response.clone());
        return undefined;
      })))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.pathname.startsWith("/api/")) return;

  if (url.pathname.startsWith("/guest/")) {
    event.respondWith(fetch(new Request(request, { cache: "no-store" })));
    return;
  }

  const freshRequest = new Request(request, { cache: "no-store" });
  event.respondWith(
    fetch(freshRequest)
      .then(response => {
        if (
          response.ok
          && request.mode === "navigate"
          && url.origin === self.location.origin
        ) {
          const copy = response.clone();
          event.waitUntil(caches.open(CACHE).then(cache => cache.put("/", copy)));
        }
        return response;
      })
      .catch(async () => {
        const exact = await caches.match(request);
        if (exact) return exact;
        if (request.mode === "navigate") {
          const shell = await caches.match("/");
          if (shell) return shell;
        }
        throw new Error("offline resource unavailable");
      })
  );
});
