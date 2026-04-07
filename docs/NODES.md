# Node Management

## Supported Protocols Per Node

Each VPN node can run any combination of protocols. The backend picks the
first enabled config when provisioning a new subscription (priority order
based on VPNConfig.id).

| Protocol | Ansible Role | Service Name |
|----------|-------------|--------------|
| ShadowTLS+SS | install_shadowtls_stack | shadowtls (built-in) |
| VLESS Reality | install_vless_reality | xray |
| VLESS+WS+CDN | install_vless_ws_cdn | xray-ws-cdn |
| Hysteria2 | install_hysteria2 | hysteria-server |

## Recommended Protocol Stack

1. **Primary:** VLESS+WS+CDN (Cloudflare) — hardest to block
2. **Fallback 1:** VLESS Reality on high port (47000+)
3. **Fallback 2:** ShadowTLS v3 + Shadowsocks
4. **Fallback 3:** Hysteria2 (broadband only)

## Health Monitoring

Probe agent checks all endpoints every 5 minutes from RU/KZ/EU locations.
If a node's health score drops below 50, auto-migration kicks in:
1. All active subscriptions are moved to a healthy node
2. Old devices are revoked (best-effort on dead node)
3. New credentials are generated
4. Users are notified via bot with new config link
