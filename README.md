# VPN Platform

Automated VPN-as-a-Service platform with Telegram bot interface, multi-protocol support, auto-provisioning, health monitoring, and autoscale.

## Architecture

```
Telegram Bot (aiogram) → FastAPI Backend → PostgreSQL + Redis/RQ
                              ↓
                     Ansible Worker (playbooks)
                              ↓
                     VPN Nodes (4 protocols)
```

### Supported Protocols

| Protocol | Transport | TSPU Status (April 2026) |
|----------|-----------|--------------------------|
| ShadowTLS v3 + Shadowsocks | TCP | Working |
| VLESS + Reality | TCP (high port) | Under active attack; works on ports 47000+ |
| VLESS + WebSocket + CDN | WebSocket via Cloudflare | Working (CDN IPs whitelisted) |
| Hysteria2 | QUIC/UDP | Working on broadband, unstable on mobile |

### Components

- **Backend** (FastAPI): REST API, provisioning, payments, health, autoscale
- **Bot** (aiogram): Telegram bot — purchase, config delivery, status, renewal, referrals
- **Worker** (RQ): Async Ansible execution, renewal cron, autoscale ticks
- **Probes** (agent): Active health checks from multiple regions
- **Admin** (React SPA): Dashboard for nodes, users, invoices, API tokens
- **Infra** (Ansible): 12+ roles — from bootstrap to monitoring

## Quick Start

```bash
# Copy and edit environment
cp .env.example .env

# Start all services
docker-compose up -d

# Check status
docker-compose logs -f backend
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Register + welcome (supports referral deep links: `/start ref_XXXXX`) |
| `/plans` | Show available plans and purchase |
| `/config` | Get VPN connection URIs + QR instructions |
| `/status` | Show subscription status |
| `/renew` | Renew subscription |
| `/referral` | Get referral link (+3 days for both) |
| `/newconfig` | Self-service: regenerate config on a different node |

## Payment Providers

| Provider | Status | Notes |
|----------|--------|-------|
| Telegram Stars | Primary | Native UX, 35-50% commission |
| CryptoBot (USDT/TON) | Secondary | Low commission (1-3%) |
| YooKassa | Backup | High block risk for VPN merchants |

## Key Features

- **Row-level locking** on node selection (SELECT FOR UPDATE SKIP LOCKED)
- **Secrets encrypted at rest** via Fernet (APP_SECRET_KEY)
- **Dynamic subscription links** — stable URL that auto-updates on migration
- **Auto-renewal** — cron checks expiry, creates invoices, notifies users
- **Health probes** with regional granularity + auto-migration
- **Autoscale** via Hetzner API when utilization > watermark
- **Referral program** — invite link → +3 days for both parties
- **Self-service** — users can regenerate configs via bot
- **4 VPN protocols** with automatic fallback

## API Endpoints (Key)

### Public
- `GET /api/plans` — list visible plans
- `POST /api/invoices` — create invoice (rate-limited)
- `GET /sub/{token}` — dynamic subscription link (base64 encoded URIs)

### Bot/Admin
- `POST /api/users/register` — register with optional referral
- `GET /api/users/by_telegram/{id}` — get user subscriptions
- `POST /api/users/by_telegram/{id}/regenerate` — self-service config regen
- `POST /api/subscriptions/{id}/auto_renew` — toggle auto-renewal
- `POST /api/referral/code` — get/create referral code
- `GET /api/notifications/pending` — pending bot notifications
- `POST /api/notifications/{id}/ack` — acknowledge delivered notification

### Admin
- Full CRUD for nodes, configs, users, invoices, cloud providers
- `POST /api/autoscale/tick` — manual autoscale trigger
- `POST /api/nodes/{id}/migrate` — manual migration

## Deployment

See [docs/DEPLOY.md](docs/DEPLOY.md) for full deployment guide.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `ADMIN_API_TOKEN` | Yes | Shared secret for admin API |
| `APP_SECRET_KEY` | Yes | Fernet encryption key for secrets |
| `BOT_TOKEN` | Yes | Telegram bot token |
| `REDIS_URL` | Yes | Redis connection for RQ |
| `PAYMENT_PROVIDER` | No | Default: cryptobot |
| `CRYPTOBOT_TOKEN` | If cryptobot | CryptoBot API token |
| `TELEGRAM_STARS_WEBHOOK_SECRET` | If stars | Shared secret for Stars |
| `SUB_LINK_BASE_URL` | No | Base URL for dynamic sub links |
| `RENEWAL_CHECK_INTERVAL` | No | Renewal check interval (default: 3600s) |
| `AUTOSCALE_INTERVAL` | No | Autoscale tick interval (default: 0 = off) |

## Project Structure

```
vpn/
├── backend/           # FastAPI backend
│   ├── app/
│   │   ├── api.py              # Main API routes
│   │   ├── api_extensions.py   # MVP feature routes
│   │   ├── models.py           # SQLAlchemy models
│   │   ├── schemas.py          # Pydantic schemas
│   │   ├── worker.py           # RQ worker + cron jobs
│   │   ├── services/
│   │   │   ├── provisioning.py # Orchestrator + credential builders
│   │   │   ├── payments/       # 3 payment providers
│   │   │   ├── health.py       # Health aggregation + auto-migration
│   │   │   └── autoscale.py    # Pool-based autoscaling
│   │   └── alembic/versions/   # DB migrations
│   └── tests/
├── bot/               # Telegram bot (aiogram)
├── admin/             # React SPA admin dashboard
├── probes/            # Health probe agent
├── infra/ansible/     # Ansible playbooks + 12 roles
│   └── roles/
│       ├── install_shadowtls_stack/
│       ├── install_vless_reality/
│       ├── install_vless_ws_cdn/     # NEW
│       ├── install_hysteria2/        # NEW
│       ├── install_traffic_collector/
│       ├── monitoring_stack/
│       └── ...
└── docs/
