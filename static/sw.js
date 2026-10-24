// 반나절 서비스워커 — 홈 화면 앱(PWA)용.
// 화면 틀(HTML·아이콘)만 저장해 두고, 검색·코스·날씨(/api/*)는 절대 저장하지 않는다(늘 최신).
// HTML 은 '네트워크 먼저' — 배포하면 바로 새 화면, 오프라인일 때만 저장본.
const VERSION = "banajeol-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;   // 지도·사진·글꼴은 브라우저에 맡김
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/admin")) return;
  if (e.request.mode === "navigate"){
    e.respondWith(fetch(e.request).then(res => {
      const copy = res.clone(); caches.open(VERSION).then(c => c.put("/", copy)); return res;
    }).catch(() => caches.match("/")));
    return;
  }
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
