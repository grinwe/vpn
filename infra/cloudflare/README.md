# Cloudflare sub-link proxy (Stage 8)

`sub-proxy-worker.js` is a thin Cloudflare Worker that fronts the
backend's `/api/sub/{token}` endpoint on a separate "boring" domain.
It exists so that an RKN block on the primary domain
(`grinwer.online`) does not silently kill installed Hiddify / v2rayNG
clients — those clients only know about one URL, the dynamic sub link.

## Why a separate domain
The sub link is the only client-facing surface that *cannot* be
re-issued in-place. Once a config is installed, the URL is baked into
the user's app. If that domain becomes unreachable, the user's VPN
keeps working until the next node migration and then breaks silently.
A second, throwaway-friendly domain insulates the link from the rest
of the brand domain (web app, landing, admin) so blocking one does not
take down the others.

## Deploy
1. Pick a boring domain. The cheapest option is a Cloudflare-managed
   subdomain like `c1.cloudfn.app`. Anything that resolves through
   Cloudflare and is not branded to the VPN service works.
2. Create a Worker, paste `sub-proxy-worker.js`.
3. Add env vars on the Worker:
   - `BACKEND_ORIGIN` — `https://grinwer.online` (or whichever host
     serves the canonical backend).
   - `SHARED_SECRET` *(optional)* — string the backend can use to
     recognize Worker traffic for separate rate limits.
4. Bind a route: `c1.cloudfn.app/sub/*` → this Worker.
5. Set `SUB_LINK_BASE_URL=https://c1.cloudfn.app/sub` on **backend**,
   **worker** (the RQ worker, not the Cloudflare one), and **bot**
   containers. `docker-compose.yml` and the Ansible `env.j2` template
   already accept the variable.
6. New subscriptions provisioned after this point will bake the boring
   URL into `Device.connection_uri` and the WebApp will copy that URL
   into the Hiddify QR. Already-installed clients keep their old URL —
   accept this; they will migrate naturally as users replace devices.

## Rotating the boring domain
When the boring domain itself gets blocked, register the next one
(`c2.cloudfn.app`), redeploy the Worker, bump `SUB_LINK_BASE_URL`.
Only newly-issued subs will pick up the new URL — that is the price
of the per-device URL stability we trade for in exchange.
