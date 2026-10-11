/* LAFhvst 원격 제어 PWA 서비스 워커.
 *
 * 원칙
 * - 앱 셸(정적 자산)만 캐시한다. API JSON 응답은 절대 캐시하지 않는다(인증/권한 문제).
 * - 썸네일 이미지(GET, image/*)만 캐시해 릴레이 대역폭을 줄인다(개수 상한 + 오래된 것부터 제거).
 * - 탐색(navigation)은 네트워크 우선, 실패 시 캐시된 셸로 폴백한다.
 *
 * 참고: 서비스 워커는 보안 컨텍스트(https 또는 localhost)에서만 등록된다.
 * 릴레이(https)로 접속하면 동작하고, 같은 네트워크의 http 접속에서는 등록되지 않는다.
 *
 * 셸(HTML/JS/CSS)을 바꾼 릴리스에서는 SHELL_CACHE 버전을 올려야 한다.
 * (캐시 이름이 같으면 activate 가 이전 셸을 지우지 않아 예전 JS 가 계속 서빙된다)
 */

var SHELL_CACHE = "lafhvst-shell-v2";
var IMAGE_CACHE = "lafhvst-img-v1";
var IMAGE_LIMIT = 150;
var CACHE_PREFIX = "lafhvst-";

var SHELL_ASSETS = [
  "/",
  "/index.html",
  "/pair.html",
  "/css/style.css",
  "/js/api.js",
  "/js/e2e.js",
  "/js/app.js",
  "/js/remote.js",
  "/js/nav.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      return Promise.all(
        SHELL_ASSETS.map(function (url) {
          var target = new URL(url, self.location.origin).href;
          return cache.add(new Request(target, { cache: "reload" })).catch(function () {
            return null; // 개별 실패는 무시한다(오프라인 셸은 최선 노력)
          });
        })
      );
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (key) {
          if (key.indexOf(CACHE_PREFIX) === 0 && key !== SHELL_CACHE && key !== IMAGE_CACHE) {
            return caches.delete(key);
          }
          return null;
        })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

function isImageRequest(request, url) {
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return false;
  }
  if (url.pathname.indexOf("/api/") !== 0) {
    return false;
  }
  if (url.pathname.indexOf("/api/pair/") === 0 || url.pathname.indexOf("/api/remote/") === 0) {
    return false;
  }
  if (request.destination === "image") {
    return true;
  }
  return /(thumbnail|images\/\d+|favicon)/.test(url.pathname);
}

function trimCache(cache, limit) {
  return cache.keys().then(function (keys) {
    if (keys.length <= limit) {
      return null;
    }
    return Promise.all(
      keys.slice(0, keys.length - limit).map(function (key) {
        return cache.delete(key);
      })
    );
  });
}

function cacheFirstImage(request) {
  return caches.open(IMAGE_CACHE).then(function (cache) {
    return cache.match(request).then(function (cached) {
      if (cached) {
        return cached;
      }
      return fetch(request).then(function (response) {
        var type = response.headers.get("content-type") || "";
        if (response.ok && type.indexOf("image/") === 0) {
          cache.put(request, response.clone()).then(function () {
            return trimCache(cache, IMAGE_LIMIT);
          });
        }
        return response;
      });
    });
  });
}

function networkFirstShell(request) {
  return fetch(request).catch(function () {
    return caches.match("/index.html").then(function (cached) {
      return cached || caches.match("/");
    });
  });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") {
    return;
  }
  var url;
  try {
    url = new URL(request.url);
  } catch (error) {
    return;
  }
  if (url.origin !== self.location.origin) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirstShell(request));
    return;
  }
  if (isImageRequest(request, url)) {
    event.respondWith(cacheFirstImage(request));
    return;
  }
  if (url.pathname.indexOf("/api/") === 0) {
    return; // API JSON 은 캐시하지 않는다(기본 네트워크 경로)
  }
  if (/^\/(css|js|icons|assets)\//.test(url.pathname)) {
    event.respondWith(
      caches.match(request).then(function (cached) {
        if (cached) {
          return cached;
        }
        return fetch(request).then(function (response) {
          if (response.ok) {
            caches.open(SHELL_CACHE).then(function (cache) {
              cache.put(request, response.clone());
            });
          }
          return response;
        });
      })
    );
  }
});
