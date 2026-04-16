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
| VLESS + Reality | TCP (high port) | Under active attack; works on ports 47000+ |
| VLESS + XHTTP | TCP (TLS-fronted) | Working, обход 16KB curtain |
| VLESS + WebSocket + CDN | WebSocket via Cloudflare | Working (CDN IPs whitelisted) |
| Hysteria2 | QUIC/UDP | Working on broadband, unstable on mobile |

> ShadowTLS v3 + Shadowsocks — **deprecated (0.2, April 2026)**, UI не создаёт новые конфиги.  Enum и роль оставлены до 0.4 для легаси-нод.

### Components

- **Backend** (FastAPI): REST API, provisioning, payments, health, autoscale, warm credential pool
- **Bot** (aiogram): Telegram bot — purchase, config delivery, status, renewal, referrals
- **WebApp** (React + Vite, Telegram Mini App): customer-facing личный кабинет served at `/app`
- **Worker** (RQ): async Ansible execution, renewal cron, autoscale ticks, warm-pool warmer. Only container with `ansible-playbook` + SSH key — API image is slim.
- **Probes** (agent): active health checks from multiple regions
- **Admin** (React SPA): dashboard for nodes, users, invoices, API tokens
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
| `/balance` | Show wallet balance, runway in days, per-sub daily cost |
| `/config` | Get VPN connection URIs + QR instructions |
| `/status` | Show subscription status |
| `/renew` | Renew subscription (legacy invoice flow) |
| `/referral` | Get referral link (see Referral program below for payout mechanics) |
| `/newconfig` | Self-service: regenerate config on a different node |

## Payment Providers

| Provider | Status | Notes |
|----------|--------|-------|
| Telegram Stars | Primary | Native UX, 35-50% commission |
| CryptoBot (USDT/TON) | Secondary | Low commission (1-3%) |
| SBP (generic) | Manual | Card-to-card via SBP webhook |

## Key Features

- **Row-level locking** on node selection and warm-pool assignment (`SELECT FOR UPDATE SKIP LOCKED`)
- **Warm credential pool** — pre-provisioned bundles per node, atomic assignment on purchase, two-stage revoke. Cold-path fallback if pool empty. Tunable via `WARM_POOL_*` env vars.
- **Secrets encrypted at rest** via Fernet (APP_SECRET_KEY)
- **Dynamic subscription links** — stable URL that auto-updates on migration
- **Pay-as-you-go balance billing** — wallet (`User.balance_kopecks`) + append-only ledger, hourly tick burns `daily_rate × devices` per sub, anchor-based (no drift on late ticks), low-balance warning at ≤3 days
- **Freeze / pause** — up to 14 days per period, 30 days/year budget, devices revoked on freeze, sub_token preserved across unfreeze so installed clients keep working
- **Auto-renewal** — cron checks expiry, creates invoices, notifies users (legacy plan-purchase flow)
- **Health probes** with regional granularity + auto-migration
- **Autoscale** via Hetzner API when utilization > high watermark, with Prometheus visibility (`vpn_autoscale_pool_utilization`, `vpn_autoscale_events_total{outcome=…}`)
- **Downscale** (Stage 5) — when utilization < low watermark, drain tick flips youngest auto-spawned node to `draining`, migrates active subs to other nodes in the pool (sub_token preserved), destroys VM after grace window. Hysteresis (low << high) + min_nodes floor + 1-draining-per-pool cap. Off by default — `AUTOSCALE_DOWNSCALE_ENABLED=1` to enable.
- **Telegram Mini App** — full личный кабинет with native Stars checkout (`tg.openInvoice`)
- **Free trial** — any user can one-shot activate a trial via WebApp: credits the price of Basic 1m (read from DB, not hardcoded) as `kind=bonus` onto the balance. Gated by `User.trial_activated_at IS NULL`, row-locked. Worker sends a warning `TRIAL_EXPIRY_WARN_DAYS` before `trial_expires_at`; on expiry, users without any real `kind=topup` get a `kind=adjust` clawback of `min(15000, balance)`. Paid users keep everything. See [docs/TRIAL_SYSTEM.md](docs/TRIAL_SYSTEM.md).
- **Referral program** — three-stage, anti-farm: (1) **attribution** happens on any `/start ref_XXX` while `user.referred_by_id IS NULL`, no bonus at this point; (2) **referee bonus** `REFERRAL_BONUS_KOPECKS` is credited when the referee activates their trial; (3) **referrer payout** of the same amount is credited only when the referee makes their **first real `kind=topup`** via a payment webhook. Idempotent via `referral_payout:{user_id}` reference. Fake-account farming yields zero payout until real money flows.
- **Self-service** — users can regenerate configs via bot
- **Credential sharing protection** — per-device sub_tokens (each device gets its own sub link) + local enforcer daemon on nodes (xray access log → detect 2+ IPs per UUID → xray gRPC rmuser/adduser cycle, instant disconnect)
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
| `AUTOSCALE_INTERVAL` | No | Autoscale tick interval, seconds. Default 300 in compose; set 0 to disable. |
| `WEBAPP_JWT_SECRET` | Yes (if WebApp) | HMAC secret for the WebApp bearer token. Provisioned via ansible vault. |
| `WEBAPP_BASE_URL` | No | Public URL of the Telegram Mini App, used by the bot to build deep links. |
| `WARM_POOL_ENABLED` | No | Master switch for the warm credential pool. `0` forces cold path. |
| `WARM_POOL_TARGET` | No | Bundles to keep warm per active node (default 10). |
| `WARM_POOL_CHECK_INTERVAL` | No | Warmer tick interval, seconds (default 120). |
| `BALANCE_CHARGE_INTERVAL` | No | Balance tick interval, seconds (default 3600). |
| `MAX_FREEZE_DAYS_PER_PERIOD` | No | Per-freeze cap (default 14). |
| `FREEZE_YEAR_BUDGET_DAYS` | No | Yearly freeze budget per sub (default 30). |
| `REFERRAL_BONUS_KOPECKS` | No | Referral bonus amount (default 5000 = ₽50). Paid to referee on trial activation, to referrer on referee's first real topup. |
| `TRIAL_DURATION_DAYS` | No | Trial window from activation (default 30). |
| `TRIAL_EXPIRY_WARN_DAYS` | No | Warning sent this many days before trial expiry (default 3). |
| `MIN_TOPUP_KOPECKS` | No | Minimum topup amount (default 10000 = ₽100). |
| `LOW_BALANCE_WARN_DAYS` | No | Threshold for low-balance bot warning (default 3). |
| `AUTOSCALE_DOWNSCALE_ENABLED` | No | Master switch for downscale (default 0). |
| `AUTOSCALE_LOW_WATERMARK` | No | Below this utilization the drain tick fires (default 0.3). |
| `AUTOSCALE_MIN_NODES` | No | Floor — never shrink a pool below this (default 1). |
| `AUTOSCALE_DRAIN_GRACE_HOURS` | No | Quiet window before destroying an emptied draining node (default 24). |
| `DRAIN_TICK_INTERVAL` | No | Drain tick interval, seconds (default 600). |
| `DRAIN_MIGRATE_BATCH` | No | Subs migrated per draining node per tick (default 10). |

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
├── webapp/            # Telegram Mini App (React + Vite + Tailwind)
├── admin/             # React SPA admin dashboard
├── probes/            # Health probe agent
├── infra/ansible/     # Ansible playbooks + 12 roles
│   └── roles/
│       ├── install_shadowtls_stack/   ← deprecated (0.2), закомменчена в site.yml
│       ├── install_vless_reality/
│       ├── install_vless_ws_cdn/     # NEW
│       ├── install_hysteria2/        # NEW
│       ├── install_traffic_collector/
│       ├── monitoring_stack/
│       └── ...
└── docs/
