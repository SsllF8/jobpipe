/* ============================================================
   求职作战台 · Service Worker

   为什么它必须是一个独立文件（而不是内联进 HTML）
   ------------------------------------------------
   Service Worker 的注册要求脚本与页面同源，且不能被内联 —— 这是浏览器强制的。
   所以「单文件 HTML」与「PWA」不是二选一：HTML 依然是自包含的，
   只是在被托管（http/https）时，额外挂上 sw.js 与 manifest 变成可安装的 App。
   用 file:// 双击打开时不会注册 SW（app.js 里按协议判断跳过），
   单文件那份的离线能力一点没丢。

   缓存策略（版本号由 build.py 按产物内容哈希写入）
   ------------------------------------------------
   导航请求（打开页面）  network-first   —— 联网时始终拿最新；断网回退上次缓存
   同源静态资源          cache-first    —— 图标/manifest 几乎不变，命中即返回
   跨域与非法请求        直接放行        —— 不碰，避免缓存住别人的东西

   为什么导航用 network-first 而不是 cache-first
   ---------------------------------------------
   这是一个每天更新岗位的看板。如果导航走 cache-first，部署新版本后
   手机上会永远停在旧数据，且用户无法察觉。宁可每次多一次网络往返，
   也要保证「打开就是最新的」；断网时再退回缓存，两头都不丢。

   升级方式：不在 install 里 skipWaiting()
   --------------------------------------
   新 SW 装好后进入 waiting 状态，由页面弹出「有新版本 · 点此刷新」，
   用户点击才接管并重载。自动接管会让用户看到「旧页面 + 新缓存」的错配。
   ============================================================ */

'use strict';

/* build.py 会把 __VERSION__ 替换成产物内容的哈希前 10 位。
   内容一变，缓存名就变，旧缓存在 activate 时被清掉。 */
var VERSION = '__VERSION__';
var CACHE = 'jobpipe-' + VERSION;

/* 预缓存清单：只有这几样，全部同源、全部是部署产物。
   注意不能写绝对路径（/sw.js 那种）—— 部署在 GitHub Pages 的子路径
   （/jobpipe/）下会 404，必须用相对当前 scope 的写法。 */
var PRECACHE = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './icons/apple-touch-icon.png'
];

/* ------------------------------------------------------------ install */

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      /* 逐个 add 而不是 cache.addAll：addAll 是「全成功或全失败」，
         只要有一个文件 404（比如某个图标还没生成），整个 SW 就装不上，
         离线能力直接没了。逐个处理可以把单个失败降级为警告。 */
      return Promise.all(
        PRECACHE.map(function (url) {
          return cache.add(new Request(url, { cache: 'reload' })).catch(function () {
            console.warn('[sw] 预缓存失败（已跳过）：' + url);
          });
        })
      );
    })
  );
});

/* ------------------------------------------------------------ activate */

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (key) {
          /* 只清理本应用自己的旧缓存，不碰同域下别的应用的缓存 */
          if (key.indexOf('jobpipe-') === 0 && key !== CACHE) {
            return caches.delete(key);
          }
        })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

/* ------------------------------------------------------------ message */

self.addEventListener('message', function (event) {
  var data = event.data || {};
  if (data.type === 'SKIP_WAITING') self.skipWaiting();
  if (data.type === 'VERSION') {
    var reply = { type: 'VERSION', version: VERSION };
    if (event.ports && event.ports[0]) event.ports[0].postMessage(reply);
    else if (event.source && event.source.postMessage) event.source.postMessage(reply);
  }
});

/* ------------------------------------------------------------ fetch */

function isCacheable(request) {
  if (request.method !== 'GET') return false;
  var url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return false;
  }
  /* 同源才算「我们的资源」。跨域直接放行，不缓存也不拦截。 */
  return url.origin === self.location.origin;
}

self.addEventListener('fetch', function (event) {
  var request = event.request;

  if (!isCacheable(request)) return;                       // 交给浏览器默认行为

  /* --- 导航：network-first，断网回退缓存的首页 --- */
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then(function (response) {
          if (response && response.ok) {
            var copy = response.clone();
            caches.open(CACHE).then(function (c) { c.put('./index.html', copy); });
          }
          return response;
        })
        .catch(function () {
          return caches.match('./index.html').then(function (hit) {
            return hit || caches.match('./') || new Response(
              '<!doctype html><meta charset="utf-8"><title>离线</title>' +
              '<body style="background:#070707;color:#ededed;font:15px system-ui;padding:40px 24px">' +
              '<p>当前处于离线状态，且没有可用的缓存副本。</p>' +
              '<p style="color:#8c8c8c">联网后重新打开即可恢复正常。</p>',
              { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
            );
          });
        })
    );
    return;
  }

  /* --- 同源静态资源：cache-first，未命中再去网络并顺手补缓存 --- */
  event.respondWith(
    caches.match(request).then(function (hit) {
      if (hit) return hit;
      return fetch(request).then(function (response) {
        /* 只缓存正常的同源响应；opaque（跨域 no-cors）与错误码不进缓存 */
        if (response && response.ok && response.type === 'basic') {
          var copy = response.clone();
          caches.open(CACHE).then(function (c) { c.put(request, copy); });
        }
        return response;
      });
    })
  );
});
