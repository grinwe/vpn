# Deployment Guide

## Prerequisites

- Docker + Docker Compose v2
- Domain with Cloudflare DNS (for VLESS+WS+CDN)
- SSH access to VPN nodes
- PostgreSQL 16+
- Redis 7+

## Initial Setup

### 1. Clone and configure

```bash
git clone <repo-url> vpn
cd vpn
cp .env.example .env
# Edit .env with your secrets
```

### 2. Generate encryption key

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Set as APP_SECRET_KEY in .env
```

### 3. Start services

```bash
docker-compose up -d
```

### 4. Create initial plan

```bash
curl -X POST http://localhost:8000/api/plans \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Basic 1m", "duration_days": 30, "price": 2.15, "max_devices": 2}'
```

### 5. Create annual plan (with discount)

```bash
curl -X POST http://localhost:8000/api/plans \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Basic 12m", "duration_days": 365, "price": 16.00, "max_devices": 2}'
```

## Adding a VPN Node

### 1. Register the node

```bash
curl -X POST http://localhost:8000/api/nodes \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "nl-hetzner-01",
    "region": "eu",
    "host": "1.2.3.4",
    "ssh_port": 22,
    "pool_id": 1
  }'
```

### 2. Add VPN configs (protocols)

```bash
# VLESS Reality (generate keys first via backend)
curl -X POST http://localhost:8000/api/nodes/1/configs \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "vless-reality",
    "protocol": "vless-reality",
    "port": 47443,
    "sni": "www.asus.com",
    "settings": {"dest": "www.asus.com:443"}
  }'

# VLESS+WS+CDN (requires Cloudflare setup)
curl -X POST http://localhost:8000/api/nodes/1/configs \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "vless-ws-cdn",
    "protocol": "vless-ws-cdn",
    "port": 443,
    "sni": "vpn.example.com",
    "settings": {"ws_path": "/ws", "cdn_domain": "vpn.example.com"}
  }'

# Hysteria2
curl -X POST http://localhost:8000/api/nodes/1/configs \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "hysteria2",
    "protocol": "hysteria2",
    "port": 8443,
    "sni": "vpn.example.com",
    "settings": {"obfs": "salamander", "obfs_password": "your-secret"}
  }'
```

### 3. Provision the node

```bash
# Run site.yml against the node
ansible-playbook -i inventories/prod infra/ansible/site.yml --limit nl-hetzner-01
```

## Cloudflare Setup (for VLESS+WS+CDN)

1. Add your domain to Cloudflare
2. Create A record pointing to VPN node IP (proxied / orange cloud)
3. SSL/TLS → Full (Strict)
4. Create origin certificate in Cloudflare → install on node
5. Network → WebSockets → Enabled

## Auto-Renewal Setup

The worker automatically:
- Checks for expiring subscriptions every hour (configurable: `RENEWAL_CHECK_INTERVAL`)
- Creates renewal invoices for auto-renew subscribers 3 days before expiry
- Sends reminders to non-auto-renew users
- Expires overdue subscriptions

## Dynamic Subscription Links

Set `SUB_LINK_BASE_URL` (e.g., `https://vpn.example.com/sub`) and users get a stable
URL that auto-updates when their server changes. Compatible with Hiddify, v2rayNG,
Streisand, and other clients that support subscription import.

## Monitoring

Business metrics are available via the Grafana dashboard at `docs/dashboards/business-metrics.json`.
Import it into your Grafana instance to track MRR, churn, LTV, and other KPIs.
