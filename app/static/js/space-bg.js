/**
 * Randomised space background, sourced live from NASA's public Image and
 * Video Library (images-api.nasa.gov). Everything in this file is designed
 * to never block or slow down the page itself:
 *
 *   - The script tag is loaded with `defer`, so it never delays parsing.
 *   - A cheap CSS starfield gradient (see main.css, .space-bg) is what the
 *     user sees immediately; the real photo only fades in on top of it once
 *     it has fully finished downloading, and only replaces the gradient
 *     rather than making the page wait on it.
 *   - We never fetch the `~orig` rendition (those can be tens or hundreds
 *     of MB straight off the telescope/camera). We ask NASA's own asset
 *     manifest for the `~medium` / `~large` renditions instead, which are
 *     normal JPEGs (typically a few hundred KB).
 *   - The list of candidate photos for the session is cached in
 *     sessionStorage after the first page view, so later navigations
 *     (e.g. `/history` -> `/search?q=...`) don't re-query the API - they
 *     just pick a different cached photo and fetch its manifest.
 *   - If anything fails (offline, NASA API hiccup, slow connection, the
 *     visitor has Data Saver on) we simply keep the CSS gradient. The page
 *     never shows a broken image and never waits on the network.
 *
 * Legal note: images.nasa.gov content is, per NASA's media usage
 * guidelines, generally free to use without permission (NASA does not
 * copyright its own photos). A couple of exceptions to be aware of if you
 * expand the search terms below: (1) some imagery is credited to
 * ESA/Roscosmos/other partner agencies and may carry its own terms, and
 * (2) photos featuring identifiable NASA personnel shouldn't be used in a
 * way that implies their endorsement. The search terms below stick to
 * telescopes/deep-space/planetary imagery to stay clear of both.
 * See: https://www.nasa.gov/nasa-brand-center/images-and-media/
 */
(function () {
  "use strict";

  var API_SEARCH = "https://images-api.nasa.gov/search";
  var API_ASSET = "https://images-api.nasa.gov/asset/";
  var CACHE_KEY = "spacebg:pool:v1";
  var LAST_ID_KEY = "spacebg:last";
  var CACHE_TTL_MS = 1000 * 60 * 60 * 6; // 6 hours - plenty for one session
  var FETCH_TIMEOUT_MS = 4000;

  // Kept deliberately to deep-space / observatory imagery (see legal note
  // above). Add more terms here any time - the pool just gets bigger.
  var SEARCH_TERMS = [
    "hubble nebula",
    "james webb galaxy",
    "hubble deep field",
    "spiral galaxy",
    "saturn rings",
    "orion nebula",
    "pillars of creation",
    "carina nebula",
    "andromeda galaxy",
    "milky way core",
  ];

  var root = document.getElementById("space-bg");
  if (!root) return;

  var creditEl = document.getElementById("space-bg-credit");
  var prefersReducedData =
    "connection" in navigator &&
    (navigator.connection.saveData ||
      /2g/.test(navigator.connection.effectiveType || ""));

  if (prefersReducedData) {
    // Respect the visitor's data preferences: keep the CSS gradient only.
    return;
  }

  function withTimeout(promise, ms) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, ms);
    return { promise: promise(controller.signal), controller: controller, timer: timer };
  }

  function fetchJson(url) {
    var wrapped = withTimeout(function (signal) {
      return fetch(url, { signal: signal }).then(function (res) {
        if (!res.ok) throw new Error("bad status " + res.status);
        return res.json();
      });
    }, FETCH_TIMEOUT_MS);
    return wrapped.promise.finally(function () {
      clearTimeout(wrapped.timer);
    });
  }

  function readCache() {
    try {
      var raw = sessionStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.items)) return null;
      if (Date.now() - parsed.savedAt > CACHE_TTL_MS) return null;
      return parsed.items;
    } catch (err) {
      return null;
    }
  }

  function writeCache(items) {
    try {
      sessionStorage.setItem(
        CACHE_KEY,
        JSON.stringify({ savedAt: Date.now(), items: items })
      );
    } catch (err) {
      // sessionStorage full/unavailable (private browsing etc) - fine,
      // we just re-query next time.
    }
  }

  // Build the candidate pool by querying a handful of curated search terms
  // in parallel. Only cheap metadata (nasa_id/title) comes back from
  // /search - no image bytes are downloaded at this stage.
  function buildPool() {
    var terms = SEARCH_TERMS.slice();
    // Only query a random subset each time we have to rebuild, so we don't
    // fire ten requests back to back on a cold cache.
    terms.sort(function () {
      return Math.random() - 0.5;
    });
    terms = terms.slice(0, 4);

    var queries = terms.map(function (term) {
      var url =
        API_SEARCH + "?q=" + encodeURIComponent(term) + "&media_type=image";
      return fetchJson(url).catch(function () {
        return null; // one bad term shouldn't sink the others
      });
    });

    return Promise.all(queries).then(function (results) {
      var items = [];
      results.forEach(function (data) {
        if (!data || !data.collection || !Array.isArray(data.collection.items))
          return;
        data.collection.items.forEach(function (item) {
          var meta = item.data && item.data[0];
          if (!meta || !meta.nasa_id) return;
          items.push({ id: meta.nasa_id, title: meta.title || "" });
        });
      });
      return items;
    });
  }

  function getPool() {
    var cached = readCache();
    if (cached && cached.length) return Promise.resolve(cached);
    return buildPool().then(function (items) {
      if (items.length) writeCache(items);
      return items;
    });
  }

  // Resolve one item's manifest to an actual, reasonably-sized image URL.
  // We deliberately skip `~orig` (can be huge) and prefer `~medium`, the
  // best trade-off between "looks like a real space photo" and "doesn't
  // time out on a slow connection".
  function resolveImageUrl(nasaId) {
    return fetchJson(API_ASSET + encodeURIComponent(nasaId)).then(function (data) {
      var items =
        (data && data.collection && data.collection.items) || [];
      var hrefs = items
        .map(function (it) {
          return it.href;
        })
        .filter(function (href) {
          return href && /\.(jpe?g|png)$/i.test(href) && !/~orig\./i.test(href);
        });

      var pick =
        hrefs.find(function (h) {
          return /~medium\./i.test(h);
        }) ||
        hrefs.find(function (h) {
          return /~large\./i.test(h);
        }) ||
        hrefs.find(function (h) {
          return /~small\./i.test(h);
        }) ||
        hrefs[0];

      return pick || null;
    });
  }

  function preload(url) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        resolve(url);
      };
      img.onerror = reject;
      img.src = url;
    });
  }

  function applyBackground(url, item) {
    root.style.backgroundImage = "url('" + url + "')";
    // rAF so the browser registers the background-image before we flip the
    // opacity transition - guarantees a clean fade instead of a pop.
    requestAnimationFrame(function () {
      root.classList.add("is-loaded");
    });
    if (creditEl && item) {
      creditEl.textContent = "Image: NASA \u00B7 " + item.title;
      creditEl.hidden = false;
    }
  }

  function pickNext(pool) {
    if (!pool.length) return null;
    var lastId = null;
    try {
      lastId = sessionStorage.getItem(LAST_ID_KEY);
    } catch (err) {
      /* ignore */
    }
    var candidates = pool.filter(function (item) {
      return item.id !== lastId;
    });
    if (!candidates.length) candidates = pool;
    return candidates[Math.floor(Math.random() * candidates.length)];
  }

  getPool()
    .then(function (pool) {
      var choice = pickNext(pool);
      if (!choice) return;
      try {
        sessionStorage.setItem(LAST_ID_KEY, choice.id);
      } catch (err) {
        /* ignore */
      }
      return resolveImageUrl(choice.id).then(function (url) {
        if (!url) return;
        return preload(url).then(function () {
          applyBackground(url, choice);
        });
      });
    })
    .catch(function () {
      // Any failure anywhere in the chain: do nothing, the CSS gradient
      // fallback stays exactly as it was.
    });
})();
