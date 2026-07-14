const CACHE_NAME='mirjin-shell-v17';
const STATIC_FILES=[
  './',
  './index.html',
  './boss.html',
  './manifest.webmanifest',
  './assets/scene.webp',
  './assets/fire-sprite.png',
  './assets/boss-hero.png',
  './assets/app-icon-192.png',
  './assets/app-icon-512.png',
  './assets/ara-clan.webp'
];

self.addEventListener('install',event=>{
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache=>cache.addAll(STATIC_FILES))
      .then(()=>self.skipWaiting())
  );
});

self.addEventListener('activate',event=>{
  event.waitUntil(
    caches.keys()
      .then(keys=>Promise.all(
        keys.filter(key=>key!==CACHE_NAME).map(key=>caches.delete(key))
      ))
      .then(()=>self.clients.claim())
  );
});

self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET')return;

  const url=new URL(event.request.url);
  const dynamic=
    url.pathname.endsWith('.json') ||
    url.pathname.endsWith('complaints-data.js') ||
    url.pathname.includes('firebaseio.com');

  if(dynamic){
    event.respondWith(fetch(event.request,{cache:'no-store'}));
    return;
  }

  if(event.request.mode==='navigate'){
    event.respondWith(
      fetch(event.request)
        .then(response=>{
          const copy=response.clone();
          caches.open(CACHE_NAME).then(cache=>cache.put(event.request,copy));
          return response;
        })
        .catch(()=>caches.match('./index.html'))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request)
      .then(cached=>cached||fetch(event.request).then(response=>{
        const copy=response.clone();
        caches.open(CACHE_NAME).then(cache=>cache.put(event.request,copy));
        return response;
      }))
  );
});
