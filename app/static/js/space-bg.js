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
 * Framing check
 * -------------
 * `background-size: cover` crops to fill the box, which looks fine for a
 * full-bleed photo but produces a diagonal sliver of image surrounded by
 * black when the source itself has black padding baked into its pixels --
 * common in older reprojected Hubble/WFPC2 mosaics that were rotated to
 * north-up from an instrument field-of-view that wasn't aligned with the
 * sky. That's real image content, not a CSS letterboxing bug, so no CSS
 * setting fixes it; the image itself has to be rejected before it's shown.
 *
 * checkFraming() below loads the candidate into an offscreen canvas and
 * samples thin strips along all four edges. A strip that is both very dark
 * *and* has almost no pixel-to-pixel variance is flagged as padding --
 * real astrophotography backgrounds are dark but noisy (stars, gradient,
 * compression artifacting); a flat #000 band is not. If two or more edges
 * are flagged, the candidate is rejected and the next one in the pool is
 * tried (a handful of attempts, then just keep the CSS gradient). NASA's
 * asset CDN doesn't reliably send CORS headers, so if the canvas ends up
 * tainted and getImageData() throws, this fails *open* (accepts the image
 * rather than blocking the background) -- consistent with this file's
 * existing policy of never blocking on anything.
 *
 * This check is a heuristic safety net, not a substitute for a curated
 * list of known-good nasa_ids (the more reliable fix, but one that has to
 * be built by actually querying images-api.nasa.gov and eyeballing
 * results -- not something to hardcode from memory here).
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
    "james webb galaxy",
    "james webb nebula",
    "webb deep field",
    "hubble nebula",
    "spiral galaxy",
    "saturn rings",
    "orion nebula",
    "pillars of creation",
    "carina nebula",
    "andromeda galaxy",
    "milky way core",
  ];

  // How many candidates to try (in order) before giving up and keeping the
  // CSS gradient. Each attempt is a normal image download, so this is
  // capped low to bound worst-case data usage on a bad pool.
  var MAX_FRAMING_ATTEMPTS = 4;

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
      img.crossOrigin = "anonymous"; // needed for pixel access; harmless if ignored/absent
      img.onload = function () {
        resolve(img);
      };
      img.onerror = reject;
      img.src = url;
    });
  }

  // Mean and variance of grayscale luminance over a rectangular pixel
  // region, read from an offscreen canvas.
  function luminanceStats(ctx, x, y, w, h) {
    var data = ctx.getImageData(x, y, w, h).data;
    var n = w * h;
    var sum = 0;
    var lums = new Float32Array(n);
    for (var i = 0; i < n; i++) {
      var o = i * 4;
      var lum = 0.2126 * data[o] + 0.7152 * data[o + 1] + 0.0722 * data[o + 2];
      lums[i] = lum;
      sum += lum;
    }
    var mean = sum / n;
    var variance = 0;
    for (i = 0; i < n; i++) {
      variance += (lums[i] - mean) * (lums[i] - mean);
    }
    variance /= n;
    return { mean: mean, variance: variance };
  }

  var DARK_LUMINANCE = 10; // out of 255
  var FLAT_VARIANCE = 4; // very low pixel-to-pixel variance = uniform fill, not noisy sky

  // Returns true if the image looks like a full-bleed photo, false if it
  // looks like content padded onto a black canvas (see comment above).
  // Never throws -- on any failure (including a CORS-tainted canvas) it
  // resolves true, i.e. fails open and just shows the image.
  function checkFraming(img) {
    try {
      var w = img.naturalWidth,
        h = img.naturalHeight;
      if (!w || !h) return true;
      var canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      var ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0);

      var band = Math.max(4, Math.round(Math.min(w, h) * 0.03));
      var edges = [
        [0, 0, w, band], // top
        [0, h - band, w, band], // bottom
        [0, 0, band, h], // left
        [w - band, 0, band, h], // right
      ];
      var paddedEdges = 0;
      for (var i = 0; i < edges.length; i++) {
        var e = edges[i];
        var stats = luminanceStats(ctx, e[0], e[1], e[2], e[3]);
        if (stats.mean < DARK_LUMINANCE && stats.variance < FLAT_VARIANCE) {
          paddedEdges++;
        }
      }
      return paddedEdges < 2;
    } catch (err) {
      // Most likely a tainted canvas from a CORS-less response. Fail open:
      // we can't check framing, but we'd rather show a possibly-imperfect
      // image than silently drop the feature.
      return true;
    }
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

  // Shuffles the pool once, minus whichever item we showed last time (so
  // reloads/navigations don't repeat the same photo back to back), and
  // returns an ordered list of candidates to try in turn.
  function candidateOrder(pool) {
    if (!pool.length) return [];
    var lastId = null;
    try {
      lastId = sessionStorage.getItem(LAST_ID_KEY);
    } catch (err) {
      /* ignore */
    }
    var candidates = pool.filter(function (item) {
      return item.id !== lastId;
    });
    if (!candidates.length) candidates = pool.slice();
    for (var i = candidates.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = candidates[i];
      candidates[i] = candidates[j];
      candidates[j] = tmp;
    }
    return candidates;
  }

  // Tries candidates in order until one both resolves to an image URL and
  // passes the framing check, or we run out of attempts. Every step here
  // is best-effort: any single candidate failing (bad manifest, 404,
  // decode error, badly framed) just moves on to the next one.
  function tryCandidates(candidates, attemptsLeft) {
    if (!candidates.length || attemptsLeft <= 0) return Promise.resolve(null);
    var choice = candidates[0];
    var rest = candidates.slice(1);

    return resolveImageUrl(choice.id)
      .then(function (url) {
        if (!url) return tryCandidates(rest, attemptsLeft - 1);
        return preload(url).then(function (img) {
          if (!checkFraming(img)) {
            return tryCandidates(rest, attemptsLeft - 1);
          }
          return { url: url, item: choice };
        });
      })
      .catch(function () {
        return tryCandidates(rest, attemptsLeft - 1);
      });
  }

  getPool()
    .then(function (pool) {
      var candidates = candidateOrder(pool);
      return tryCandidates(candidates, MAX_FRAMING_ATTEMPTS);
    })
    .then(function (result) {
      if (!result) return;
      try {
        sessionStorage.setItem(LAST_ID_KEY, result.item.id);
      } catch (err) {
        /* ignore */
      }
      applyBackground(result.url, result.item);
    })
    .catch(function () {
      // Any failure anywhere in the chain: do nothing, the CSS gradient
      // fallback stays exactly as it was.
    });
})();
