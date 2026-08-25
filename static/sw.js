/* TalkShelf Service Worker（PWA，设计文档 §38）
 *
 * 策略：网络优先 + 缓存兜底（仅限静态资源；离线时可用）。
 * 绝不缓存页面 HTML 与 /api 响应——页面含私密笔记内容、API 是动态数据，
 * 缓存会带来同设备信息泄露与陈旧数据问题（§38.3「明确不做」）。
 *
 * - navigate 请求：网络优先，失败（服务器不可达）回退离线页 /static/offline.html；
 * - /static/* 请求：网络优先并顺手更新缓存，失败（离线）用缓存副本；
 * - 其余（/api/*、跨域）：不拦截，网络直行（错误由页面各自处理）。
 *
 * 更新机制：/static 响应带 Cache-Control: no-cache（main.py），SW 脚本字节变化
 * 即触发浏览器更新检查；install 时 skipWaiting + activate 时 clients.claim 让
 * 新版本立即接管（单用户工具无多标签页兼容负担）。
 */
const CACHE = 'talkshelf-static-v1';
const OFFLINE_URL = '/static/offline.html';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) =>
        cache.addAll([
          OFFLINE_URL,
          '/static/app.js',
          '/static/style.css',
          '/static/manifest.webmanifest',
          '/static/icons/icon-192.png',
          '/static/icons/icon-512.png',
          '/static/icons/apple-touch-icon.png',
        ])
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k.startsWith('talkshelf-static-') && k !== CACHE)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // 写请求一律网络直行（含 CSRF 语义）
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // 跨域直行（无外部资源，防御性）

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            event.waitUntil(caches.open(CACHE).then((c) => c.put(req, copy)));
          }
          return resp;
        })
        .catch(() => caches.match(req))
    );
  }
  // 页面 HTML 与 /api/*：不拦截
});
