// life-os service worker. The server is far away (a round trip takes ~0.3 s from Korea), so:
// - pages open at once from the last copy seen, and the fresh copy is fetched alongside; if it
//   differs, the page is told and swaps it in (stale-while-revalidate);
// - images and versioned files are immutable: served from the cache after the first time;
// - any write (POST) clears the saved pages, so you never see a page without your own change.
const PAGES = "pages-v1";
const ASSETS = "assets-v1";
const CDN = /^(unpkg\.com|cdn\.jsdelivr\.net|fonts\.googleapis\.com|fonts\.gstatic\.com)$/;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (![PAGES, ASSETS].includes(k)) await caches.delete(k);
  await self.clients.claim();
})()));

function isAsset(url) {
  if (url.origin !== location.origin) return CDN.test(url.host);
  return (url.pathname.startsWith("/static/") && url.searchParams.has("v"))
    || /^\/media\/[0-9a-f]{64}\/(thumb|display)$/.test(url.pathname)
    || /^\/faces\/\d+\.webp$/.test(url.pathname);
}

function isPage(req, url) {
  if (req.method !== "GET" || url.origin !== location.origin) return false;
  if (/\.(json|vcf|webp|js|png|svg|webmanifest)$/.test(url.pathname) || /^\/(static|media|auth)\//.test(url.pathname)) return false;
  // Whole pages only: a navigation, or htmx's boosted (whole-page) request, or its preload of one.
  return req.mode === "navigate" || req.headers.get("HX-Boosted") === "true";
}

async function asset(req) {
  const cache = await caches.open(ASSETS);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === "opaque") cache.put(req, res.clone());
  return res;
}

async function page(e, req, url) {
  const cache = await caches.open(PAGES);
  const key = url.href;
  const cached = await cache.match(key);
  // Read the saved copy now: once handed to the page its body is used up and can't be cloned.
  const old = cached ? await cached.clone().text() : null;
  const fresh = fetch(req).then(async (res) => {
    if (res.ok && !res.redirected && (res.headers.get("content-type") || "").startsWith("text/html")) {
      const text = await res.clone().text();
      await cache.put(key, new Response(text, { headers: { "content-type": "text/html; charset=utf-8" } }));
      if (old !== null && old !== text) {
        for (const c of await self.clients.matchAll()) c.postMessage({ type: "fresh", url: key });
      }
    }
    return res;
  });
  if (cached && req.headers.get("X-Fresh") !== "1") {
    e.waitUntil(fresh.catch(() => {}));
    return cached;
  }
  try {
    return await fresh;
  } catch {
    return cached || new Response("<!doctype html><meta name=viewport content='width=device-width'>"
      + "<body style='font:16px -apple-system,sans-serif;padding:40px 20px;text-align:center'>"
      + "<p>Can't reach life-os. Is Tailscale on?</p><button onclick='location.reload()'>Try again</button>",
      { status: 503, headers: { "content-type": "text/html; charset=utf-8" } });
  }
}

self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET") {
    if (url.origin === location.origin) {
      e.respondWith(fetch(req).then(async (res) => { await caches.delete(PAGES); return res; }));
    }
    return;
  }
  if (isAsset(url)) e.respondWith(asset(req));
  else if (isPage(req, url)) e.respondWith(page(e, req, url));
});
