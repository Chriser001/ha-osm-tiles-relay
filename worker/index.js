// OSM tiles relay — lets Home Assistant 2026.9 map_tiles reach OSMF tile servers
// from networks where vector/tile.openstreetmap.org SNI is blocked (mainland CN).
// Home Assistant fetches this worker's hostname instead; the worker relays 1:1
// to the real OSMF origin from Cloudflare's edge (outside the blocked network).
//
// Path mapping (mirrors homeassistant/components/map_tiles/views.py):
//   /shortbread_v1/tilejson.json  -> vector.openstreetmap.org/shortbread_v1/tilejson.json
//   /shortbread_v1/{z}/{x}/{y}.mvt -> vector.openstreetmap.org
//   /styles/shortbread/fonts/...   -> vector.openstreetmap.org
//   /styles/shortbread/sprites/... -> vector.openstreetmap.org
//   /{z}/{x}/{y}.png               -> tile.openstreetmap.org
const VECTOR_ORIGIN = "https://vector.openstreetmap.org";
const RASTER_ORIGIN = "https://tile.openstreetmap.org";

// Keep in sync with what the map_tiles component can legitimately request:
// z/x/y are digits; glyph ranges look like 0-255.pbf; sprites sets are [a-z0-9_-].
const RASTER_RE = /^\/(\d{1,3})\/(\d{1,3})\/(\d{1,3})\.png$/;

export default {
  async fetch(request) {
    const method = request.method;
    if (method !== "GET" && method !== "HEAD") {
      return new Response("method not allowed", { status: 405 });
    }

    const url = new URL(request.url);
    const path = url.pathname;
    if (path.endsWith("/")) {
      return new Response("not found", { status: 404 });
    }

    let origin;
    if (
      path === "/shortbread_v1/tilejson.json" ||
      path.startsWith("/shortbread_v1/") ||
      path.startsWith("/styles/shortbread/")
    ) {
      origin = VECTOR_ORIGIN;
    } else if (RASTER_RE.test(path)) {
      origin = RASTER_ORIGIN;
    } else {
      return new Response("not found", { status: 404 });
    }

    const upstream = origin + path + url.search;
    const headers = new Headers(request.headers);
    // Only the identifying UA matters to OSMF; strip hop/proxy headers so the
    // origin sees a clean request. Host is set by fetch() from the URL.
    for (const h of [
      "host",
      "cf-connecting-ip",
      "cf-ipcountry",
      "cf-ray",
      "cf-visitor",
      "x-forwarded-for",
      "x-forwarded-proto",
      "x-real-ip",
      "connection",
      "accept-encoding",
    ]) {
      headers.delete(h);
    }
    headers.set("accept-encoding", "gzip");
    headers.set(
      "user-agent",
      "HomeAssistant/2026.9 (+https://www.home-assistant.io; map_tiles relay via Cloudflare Worker; abuse@home-assistant.io)"
    );

    let resp;
    try {
      resp = await fetch(upstream, {
        method,
        headers,
        redirect: "manual",
      });
    } catch (err) {
      return new Response("upstream unreachable: " + err.message, {
        status: 502,
      });
    }

    // Pass the body through untouched (keeps gzip Content-Encoding intact for
    // HA's auto_decompress=False fetches). Strip hop-by-hop headers only.
    const out = new Response(resp.body, resp);
    out.headers.delete("cf-ray");
    out.headers.delete("server");
    return out;
  },
};
