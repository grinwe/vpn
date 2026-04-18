// Cloudflare Worker — Stage 8 boring-domain proxy for /sub/{token}.
//
// Why: the dynamic subscription link is the single most fragile
// surface in the system. Hiddify / v2rayNG clients only know that one
// URL — if RKN blocks the primary domain (grinwer.online) every
// installed client silently stops refreshing its config and breaks at
// the next node migration. Putting this Worker on a CDN-friendly
// throwaway domain (e.g. c1.cloudfn.app) gives the sub link its own
// independent blast radius. The Worker is just a thin reverse proxy:
// the actual subscription handler still lives in the backend.
//
// Deploy:
//   1. Create a Cloudflare Worker, paste this file.
//   2. Set the env var BACKEND_ORIGIN to the canonical backend host
//      (e.g. https://grinwer.online).
//   3. Bind a route like c1.cloudfn.app/sub/* → this worker.
//   4. Set SUB_LINK_BASE_URL=https://c1.cloudfn.app/sub on the
//      backend, worker, and bot containers (docker-compose / ansible
//      env.j2 already accept this var).
//   5. Newly-provisioned subs will bake the boring-domain URL into
//      Device.connection_uri and the WebApp will surface it. Existing
//      installed clients keep using the old URL until natural churn —
//      that is acceptable per the Stage 8 design note.
//
// Throwaway-domain resilience: when the boring domain itself gets
// blocked, spin up a new one (cN.cloudfn.app) and bump
// SUB_LINK_BASE_URL — only newly-issued subs migrate. Already-bound
// devices keep their previous URL.

export default {
  /**
   * @param {Request} request
   * @param {{ BACKEND_ORIGIN: string, SHARED_SECRET?: string }} env
   */
  async fetch(request, env) {
    if (!env.BACKEND_ORIGIN) {
      return new Response("BACKEND_ORIGIN not configured", { status: 500 });
    }

    const url = new URL(request.url);
    // Only proxy /sub/<token> — every other path on the boring domain
    // returns 404 so the surface stays minimal and unfingerprintable.
    const match = url.pathname.match(/^\/sub\/([A-Za-z0-9_-]+)\/?$/);
    if (!match) {
      return new Response("Not found", { status: 404 });
    }

    const token = match[1];
    const upstream = new URL(env.BACKEND_ORIGIN.replace(/\/+$/, ""));
    upstream.pathname = `/api/sub/${token}`;

    const upstreamHeaders = new Headers();
    // Forward Accept and User-Agent so subscription-userinfo + the
    // text/plain response shape match what Hiddify expects. Strip
    // everything else to avoid leaking client headers across origins.
    const passthrough = ["accept", "user-agent", "if-none-match"];
    for (const h of passthrough) {
      const v = request.headers.get(h);
      if (v) upstreamHeaders.set(h, v);
    }
    // Optional shared secret so the backend can recognize Worker
    // traffic and apply different rate limits. Plain header — TLS
    // protects it on the wire.
    if (env.SHARED_SECRET) {
      upstreamHeaders.set("x-sub-proxy-secret", env.SHARED_SECRET);
    }

    let upstreamResp;
    try {
      upstreamResp = await fetch(upstream.toString(), {
        method: "GET",
        headers: upstreamHeaders,
        // Workers default cf cache: respect upstream Cache-Control. The
        // backend currently sends none, which is fine — clients poll
        // every 6 minutes per profile-update-interval anyway.
      });
    } catch (err) {
      return new Response(`Upstream fetch failed: ${err}`, { status: 502 });
    }

    // Pass through subscription-userinfo + profile-update-interval +
    // content-type. Drop anything that could fingerprint the backend
    // (server, x-powered-by).
    const respHeaders = new Headers();
    const allowed = [
      "content-type",
      "subscription-userinfo",
      "profile-update-interval",
      "cache-control",
    ];
    for (const h of allowed) {
      const v = upstreamResp.headers.get(h);
      if (v) respHeaders.set(h, v);
    }
    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      headers: respHeaders,
    });
  },
};
