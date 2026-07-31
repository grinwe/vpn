# Deployment Guide

Порядок выкатки с нуля на prod-хост: backend+bot+webapp+admin в docker-compose, публичный nginx+TLS через Ansible, и первая VPN-нода через Admin UI.

## Prerequisites

- Docker + Docker Compose v2 на управляющем хосте (там, где крутится бэкенд)
- Отдельная VPN-нода (или несколько) — чистая Ubuntu 22.04/24.04, root-доступ по SSH
- Домен с DNS, которым ты управляешь (для backend API + отдельный хост для каждой ноды, если используешь `SUB_LINK_BASE_URL`)
- PostgreSQL 16+ и Redis 7+ — они подняты compose'ом, если не выносишь в managed

## 1. Clone and configure

```bash
git clone <repo-url> /opt/vpn
cd /opt/vpn
cp .env.example .env
```

Отредактируй `.env` — минимально обязательные секреты см. в [README.md](../README.md#environment-variables). Критично не пропустить:

| Переменная | Почему критично |
|------------|-----------------|
| `APP_SECRET_KEY` | Fernet-ключ, которым шифруются секреты (credentials, provider tokens). Если сгенерируешь заново на уже живой БД — старые секреты не расшифруются. **Бэкапь**. |
| `ADMIN_API_TOKEN` | Единый общий токен. Его же использует бот для походов в бэкенд. Для людей-админов сразу после старта заведи отдельные scoped tokens через страницу `/admin/tokens`. |
| `WEBAPP_JWT_SECRET` | HMAC для WebApp-сессии. Если ротируешь — все открытые WebApp'ы выкинет в re-auth. |
| `BOT_TOKEN` | От @BotFather |
| `PROVISIONING_SSH_KEY` | **См. раздел «Provisioning SSH key» ниже.** Без него create-node сработает, а bootstrap упадёт. |

### 1a. Генерация `APP_SECRET_KEY`

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 2. Provisioning SSH key

Backend и worker ходят на VPN-ноды по SSH, чтобы гонять ansible. Нужен отдельный ed25519 keypair:

```bash
mkdir -p /opt/vpn/secrets
chmod 700 /opt/vpn/secrets
ssh-keygen -t ed25519 -f /opt/vpn/secrets/provisioning_key -N '' -C 'provisioning@grinwer'
chmod 600 /opt/vpn/secrets/provisioning_key
```

В `.env`:

```
PROVISIONING_SSH_KEY=/opt/vpn/secrets/provisioning_key
```

Он монтируется в **worker**-контейнер как `/run/secrets/provisioning_key` (см. `docker-compose.yml`). Если переменная не выставлена — compose по умолчанию маунтит `/dev/null` и ansible упадёт на первом же запуске без внятной ошибки.

Публичная часть ключа (`provisioning_key.pub`) должна быть прописана в `authorized_keys` каждой VPN-ноды. Первичная установка — руками при заказе ноды, дальше роль `bootstrap_node` сама держит список из [infra/ansible/group_vars/vpn_nodes.yml](../infra/ansible/group_vars/vpn_nodes.yml) (`ssh_public_keys`) в актуальном состоянии.

Проверка, что ключ реально виден worker'ом:

```bash
docker compose exec worker sh -c 'ls -la /run/secrets/provisioning_key && ssh -i /run/secrets/provisioning_key -o StrictHostKeyChecking=no root@<node-ip> echo OK'
```

## 3. Start services

```bash
docker compose up -d
docker compose logs -f backend worker
```

Backend применяет Alembic миграции на старте автоматически. Если что-то падает — смотри этот лог до того как идти дальше.

## 4. Public nginx + TLS

Поверх compose'а, на том же хосте, Ansible роль `deploy_web_frontend` ставит системный nginx с Let's Encrypt и проксирует:

- `/api/*` → `127.0.0.1:8000` (backend)
- `/admin/*` → `127.0.0.1:8080` (admin SPA)
- `/app/*` → `127.0.0.1:8082` (webapp SPA)

```bash
cd infra/ansible
ansible-playbook -i inventories/prod/hosts.yml site.yml --tags web
```

Перед этим убедись, что `deploy_web_frontend_letsencrypt_email` в `group_vars/web.yml` корректен и DNS публичного домена (например `grinwer.online`) уже указывает на этот хост — иначе certbot упадёт.

**DNS-режим backend-домена:** `grinwer.online` (или аналог) — **Proxied** в Cloudflare (оранжевое облачко). Это HTTP(S)-трафик, CF даёт DDoS-защиту и кеширует статику admin/webapp.

**Кэш-заголовки SPA (webapp/nginx.conf, admin/nginx.conf):** `index.html` отдаётся с `Cache-Control: no-cache` (клиент хранит, но ревалидирует по ETag — дешёвый 304), хэшированные `assets/*` — с `max-age=31536000, immutable`, а отсутствующий ассет даёт честный **404**, не SPA-fallback. Не убирать: без этого Telegram-webview кэширует `index.html` эвристически (часами держит ссылку на уже удалённый с диска бандл), fallback отдаёт HTML под `.js`-URL → module script блокируется по MIME → белый лист у вернувшихся юзеров после каждого выката, плюс CF кэширует этот HTML-под-`.js` на 4 часа (инцидент 2026-07-31, экран «Сменить подписку»).

### 4a. Mgmt-mirror (upstream-зеркало для vpn-нод)

На том же web-host'е поднимается отдельный compose-стек `mgmt-mirror` — nginx, который раздаёт ноды `geoip.dat`, `geosite.dat` и pinned-`Xray-linux-64-*.zip`. Это spasает bootstrap'ы на RU-провайдерах, где outbound к `github.com` (и зеркалам типа ghproxy/jsdelivr) троттлится до неюзабельного состояния. См. подробности в [docs/infrastructure/ansible.md § mgmt-mirror](infrastructure/ansible.md#mgmt-mirror--собственное-зеркало-upstream).

Раскат — частью того же `--tags web`:

```bash
ansible-playbook -i inventories/prod/hosts.yml site.yml --tags web
```

После — `bootstrap_node` на любой новой ноде сам прописывает `MIRROR_URL` в `/etc/default/xray-mirror`, и wrapper'ы качают upstream через зеркало в первую очередь.

## 5. Create initial plans

Через Admin UI: открой `https://<your-domain>/admin/`, залогинься по `ADMIN_API_TOKEN`, перейди в **Plans** → кнопка «+ Добавить тариф».

Минимум один visible 30-day план нужен для работы **trial-системы** (она читает из БД цену самого дешёвого visible 30-day плана — см. [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md#activation-flow)).

Рекомендуемая стартовая сетка:

| Name | duration_days | price (₽) | max_devices | is_visible |
|------|---------------|-----------|-------------|------------|
| Basic 1m | 30 | 150 | 2 | ✓ |
| Basic 12m | 365 | 1200 | 2 | ✓ |

## 6. Adding a VPN Node (Admin UI)

**Весь процесс — через Admin UI**, никаких curl. Backend сам enqueue'ит provisioning-таску в worker'а, которая гоняет `site.yml` против ноды.

### Pre-flight на ноде

1. Чистая Ubuntu 22.04/24.04, root SSH, **публичный ключ из `/opt/vpn/secrets/provisioning_key.pub` уже в `/root/.ssh/authorized_keys`**.
2. DNS для этой ноды (например `n1.grinwer.online`) указывает A-записью на IP. **Для VPN-нод это должно быть DNS-only (серое облачко в Cloudflare), не Proxied.** Cloudflare проксирует только HTTP(S), а ShadowTLS/VLESS Reality/Hysteria2 он либо заблокирует, либо MITM'нет TLS. **Это касается и `vless-ws-cdn`/`vless-xhttp`:** CF-проксирование для них мёртвое (RKN режет CF-плечо на 4G — см. `operations/cdn_front_nodata_investigation.md` и память `project_cf_ws_cdn_dead`). Теперь они тоже **DNS-only** — backend сам минтит случайную DNS-only запись `<rand>.wgse.info` → IP ноды, а нода терминирует TLS своим Let's Encrypt сертом. CF используется ТОЛЬКО как DNS-хостинг.
3. Порты, которые нужно открыть на файрволле провайдера: `22` (SSH), `443`, `8443`, `9443` (см. [group_vars/vpn_nodes.yml](../infra/ansible/group_vars/vpn_nodes.yml) `firewall_allowed_ports`). Роль `bootstrap_node` также ставит ufw на самой ноде.

### Регистрация ноды

1. `/admin/nodes` → **+ Добавить ноду**.
2. Заполни форму:
   - **Имя** — уникальное, kebab-case: `fr-pq-01`, `nl-hetzner-02`
   - **Регион** — логический тег: `eu-west`, `eu-north`. Используется в UI при выборе тарифа.
   - **Host** — IP или DNS-имя
   - **SSH port** — обычно `22`
   - **Pool ID** — опционально, если используешь autoscale-пулы (autoscale-пулы)
3. **Создать + bootstrap**. Статус ноды: `registering` → (через 3-5 минут) → `active`. Таблица авто-рефрешится раз в 5 секунд.
4. Если застряло в `error` — открой `/admin/tasks` (TODO: этап 2.75) или `docker compose logs worker --tail 200`, ищи traceback от ansible-runner.

### Добавить конфиги протоколов

В той же таблице `/admin/nodes` клик на строку ноды разворачивает панель «Конфиги протоколов». Кнопка **+ Добавить конфиг**:

- **vless-reality** — основной протокол, порт `9443`, SNI `www.asus.com` (по дефолту). Ключи генерятся бэкендом автоматически, если оставить `public_key` пустым. Per-user isolation + sharing enforcer.
- **vless-xhttp** — основной TCP-протокол, обход 16KB curtain ТСПУ.
- **vless-ws-cdn** — порт `443`, прямой WS+TLS на случайный `*.wgse.info` сабдомен (DNS-only, LE-серт, БЕЗ CF-прокси — backend создаёт DNS-запись на провиженинге сам). Подходит, когда DPI режет Reality.
- **hysteria2** — UDP/QUIC, порт `8443`. На мобильных бывает нестабилен.
- **shadowtls+shadowsocks** — legacy-протокол, порт `8443`. Нет per-user isolation (общий пароль на ноду), sharing enforcer не покрывает.

Рекомендованный минимум на каждой ноде: **vless-reality**. Остальные — по мере необходимости.

После добавления конфига warm-pool инвалидируется для этой ноды, и warmer'у нужно несколько тиков, чтобы пересобрать предсгенерированные credential-бандлы под новый набор протоколов ([см. warm pool](../README.md#key-features)).

## 7. Cloudflare setup для VLESS+WS+CDN / XHTTP (DNS-only)

⚠️ **CF-проксирование (orange cloud) для ws-cdn/xhttp — МЁРТВОЕ** (RKN режет CF-плечо на 4G; см. `operations/cdn_front_nodata_investigation.md`, память `project_cf_ws_cdn_dead`). Раньше тут был CF-proxy + Origin CA — **больше нет**. Теперь ws-cdn и xhttp раздаются **напрямую**, CF — только DNS.

Сетап минимальный (один раз на зону, не на ноду):

1. Зона `wgse.info` добавлена в Cloudflare и активна (NS делегированы).
2. API-токен `vault_cloudflare_api_token` с `Zone.DNS:Edit` на `wgse.info` (прокинут в backend как `CLOUDFLARE_DNS_TOKEN`). Держи доступ и к `grwr.ink`, пока не снесены старые CF-ноды (teardown резолвит зону по сохранённому `cf_front_domain`).

Дальше — **всё автоматически**: при создании `vless-ws-cdn`/`vless-xhttp` конфига backend минтит случайную **DNS-only** запись `<rand>.wgse.info` → IP ноды, а роль на ноде сама выпускает Let's Encrypt серт (HTTP-01, `:80` уже открыт `bootstrap_node`). Никаких Proxied-записей, Origin CA, Full(strict) — больше не нужно.

## 8. Auto-renewal и balance billing

Worker автоматически:

- **Hourly balance tick** (`BALANCE_CHARGE_INTERVAL`, default 3600s) — для каждой активной подписки списывает `daily_rate × devices / 24`. Anchor-based, не дрейфует при пропущенных тиках. Подписки с балансом ≤ 0 фризятся.
- **Trial expiry tick** — warning за `TRIAL_EXPIRY_WARN_DAYS` до истечения триала, clawback на истечении если юзер не стал платящим. Полная механика — [TRIAL_SYSTEM.md](TRIAL_SYSTEM.md).
- **Legacy renewal cron** (`RENEWAL_CHECK_INTERVAL`) — создаёт auto-renewal инвойсы для старой модели подписок на 3 дня до истечения. Новые юзеры идут через balance-billing, но старые подписки ещё живы на этом пути.
- **Autoscale tick** (`AUTOSCALE_INTERVAL`) — upscale-only, по `high_watermark`. Downscale/drain — не реализован (ручной destroy через admin SPA).

Всё это живёт в [backend/app/worker.py](../backend/app/worker.py) — один `run_balance_charge_tick()` + отдельные phase'ы.

## 9. Dynamic subscription links

Выстави `SUB_LINK_BASE_URL=https://<domain>/sub` в `.env`. Пользователи получают стабильный URL, который auto-update'ится при миграции юзера на другую ноду (drain, downscale, auto-migration). Совместимо с Hiddify, v2rayNG, Streisand, Nekoray, Shadowrocket.

## 10. Monitoring

- **Prometheus metrics**: `GET /metrics` на backend'е. Основные: `vpn_autoscale_*`, `vpn_warm_pool_*`, `vpn_balance_*`, стандартные FastAPI/HTTP метрики.
- **Grafana dashboard**: `docs/dashboards/business-metrics.json` — импортируй в Grafana, тянет MRR, churn, LTV, warm-pool depth.
- **Audit log**: `audit_log` таблица в БД. Каждое админ-действие, trial activation, referral attribution, provisioning-таска, revoke.

## Troubleshooting

| Симптом | Где смотреть |
|---------|--------------|
| Nodes list даёт 500 | `docker compose logs backend --tail 200` + `docker compose exec backend python -c "..."` прямой pydantic-тест |
| Create node → statuses stuck `registering` | `docker compose logs worker --tail 200` — ищи ansible traceback. Чаще всего — `PROVISIONING_SSH_KEY` не примонтирован или pub-key не в authorized_keys ноды. |
| WebApp показывает «Откройте через бота заново» | JWT протух (30 мин) или `WEBAPP_JWT_SECRET` сменился — нужно перезайти из бот-кнопки. |
| WebApp — белый лист (только фон) сразу после выката | Клиент держит старый `index.html` из кэша webview и тянет удалённый бандл. Проверить, что nginx контейнера отдаёт `no-cache` на `index.html` и 404 (не HTML) на отсутствующие `assets/*` (см. §4). Лечится у клиента «Обновить страницу» в меню мини-аппа или само по истечении эвристики кэша. |
| Bot не доставляет trial warning | `SELECT * FROM audit_log WHERE action LIKE 'trial_expiry_warning%' ORDER BY id DESC LIMIT 5` — если там нет новых, tick не отработал; если есть `:delivered` — уже доставил. |
| Balance charge не списывает | `SELECT * FROM balance_transactions ORDER BY id DESC LIMIT 20` — смотри когда последний spend; tick живёт в `worker` контейнере, не в backend. |
