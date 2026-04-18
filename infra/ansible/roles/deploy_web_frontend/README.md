# deploy_web_frontend

Nginx vhost for the single-domain web frontend (`grinwer.online`): TLS
termination on the host, path-based routing to the admin SPA and
backend FastAPI containers on loopback.

```
https://grinwer.online/admin/  → 127.0.0.1:8080 (admin SPA)
https://grinwer.online/api/    → 127.0.0.1:8000 (FastAPI)
https://grinwer.online/sub/<t> → 127.0.0.1:8000 (dynamic sub links)
```

## Cloudflare + Let's Encrypt (DNS-01)

The domain sits behind Cloudflare's proxy (orange cloud), so the
nginx-plugin HTTP-01 challenge is unreliable — CF either intercepts
`/.well-known/acme-challenge/` or terminates TLS with a mismatched
cert. The role uses **DNS-01 via the Cloudflare API** instead:
certbot writes a `_acme-challenge.<domain>` TXT record via the CF
API, waits for propagation, then Let's Encrypt validates against
the authoritative nameservers. This also works for wildcard certs
if they ever become necessary.

### One-time setup

1. **Cloudflare API token.** Create a token at
   <https://dash.cloudflare.com/profile/api-tokens> with scope
   **Zone → DNS → Edit** on the target zone only. Do NOT use a
   Global API Key.
2. **Vault.** Put the token in `group_vars/web/vault.yml` as
   `vault_cloudflare_api_token` (see `vault.yml.example`). Encrypt
   the file with `ansible-vault encrypt`.
3. **Cloudflare SSL mode.** In the CF dashboard, set SSL/TLS mode
   to **Full (strict)** *before* running the role. Any other mode
   (Flexible in particular) will cause a redirect loop once nginx
   redirects HTTP → HTTPS.
4. **DNS.** A single A record for `grinwer.online` pointing at the
   web host's public IP, proxied through CF (orange cloud).

### Renewal

`certbot.timer` runs `certbot renew` twice daily. DNS-01 is fully
non-interactive — the token is read from
`/etc/letsencrypt/cloudflare.ini` (mode 0600, root:root). No role
re-run is needed for renewal.

### Real client IP

Nginx is configured to trust Cloudflare's published edge CIDRs as
proxies and pull the real client IP from `CF-Connecting-IP`. The
CIDR lists are fetched from `cloudflare.com/ips-v{4,6}` at deploy
time; re-run the role to refresh them. Toggle with
`deploy_web_frontend_trust_cloudflare`.
