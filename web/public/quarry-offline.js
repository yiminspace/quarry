/* Cache only the public platform editor shell. Identity and data never enter CacheStorage. */
const prefix = 'quarry-platform-shell-';
const version = new URL(self.location.href).searchParams.get('build') || 'development';
const cacheName = prefix + version;
const shell = '/quarry/';
function publicAsset(url) {
  return url.origin === self.location.origin && /^\/quarry\/assets\/[A-Za-z0-9_.-]+$/.test(url.pathname) && !url.search;
}
async function rememberShell(response) {
  if (response.ok && response.headers.get('content-type')?.includes('text/html')) {
    const html = await response.clone().text();
    // A standalone Quarry on this origin must never replace the hosted shell.
    if (/<meta\s+name="quarry-mode"\s+content="platform"\s*\/?\s*>/.test(html)) {
      const cache = await caches.open(cacheName);
      await cache.put(shell, response.clone());
    }
  }
}
self.addEventListener('install', (event) => {
  event.waitUntil((async () => { await rememberShell(await fetch(shell, { cache: 'reload' })); await self.skipWaiting(); })());
});
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    await self.clients.claim();
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.startsWith(prefix) && name !== cacheName).map((name) => caches.delete(name)));
  })());
});
self.addEventListener('message', (event) => {
  if (event.data?.type !== 'quarry-warm-shell' || !Array.isArray(event.data.assets)) return;
  event.waitUntil((async () => {
    const cache = await caches.open(cacheName);
    await rememberShell(await fetch(shell, { cache: 'reload' }));
    await Promise.allSettled(event.data.assets.slice(0, 40).map(async (value) => {
      const url = new URL(value, self.location.origin);
      if (!publicAsset(url)) return;
      const response = await fetch(url.href, { cache: 'reload', credentials: 'omit' });
      if (response.ok) await cache.put(url.href, response);
    }));
  })());
});
self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const isShell = request.mode === 'navigate' && (url.pathname === shell || url.pathname === '/quarry/index.html');
  if (!isShell && !publicAsset(url)) return;
  event.respondWith((async () => {
    const cache = await caches.open(cacheName);
    try {
      const response = await fetch(request);
      if (isShell) await rememberShell(response);
      else if (response.ok) await cache.put(url.href, response.clone());
      return response;
    } catch (error) {
      const cached = await cache.match(isShell ? shell : url.href);
      if (cached) return cached;
      throw error;
    }
  })());
});
