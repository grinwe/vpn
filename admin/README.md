# vpn-admin

Small React SPA для управления пользователями и подписками VPN.
Стек: Vite + React 18 + TypeScript + TanStack Query + Tailwind,
роутинг — react-router-dom.

## Архитектура

```
[ browser ] --https--> [ nginx on nl-web :443 ]
                            |
                            +-- /admin/  -> 127.0.0.1:8080 (admin container)
                            +-- /api/    -> 127.0.0.1:8000 (backend container)
```

- Статика (SPA) билдится multi-stage `Dockerfile` и раздаётся внутри
  alpine-nginx на порту 80. Контейнер публикуется на
  `127.0.0.1:8080` — доступ только через хостовый nginx.
- Публичный nginx + TLS для `grinwer.online` ставится ролью
  `infra/ansible/roles/deploy_web_frontend` (certbot --nginx).
- API вызывается с заголовком `X-Admin-Token`, токен хранится в
  `localStorage` и валидируется при логине через `GET /api/users?limit=1`.

## Dev

```bash
cd admin
npm install
npm run dev
# → http://localhost:5173/admin/
# Vite проксирует /api -> http://127.0.0.1:8000 (подними backend локально).
```

## Prod

Через общий `docker-compose.yml` на хосте:

```bash
cd vpn
docker-compose build admin
docker-compose up -d admin
```

И поверх — один раз на хосте — публичный nginx:

```bash
cd infra/ansible
ansible-playbook -i inventories/prod/hosts.yml site.yml --tags web
```

Убедись, что в `group_vars/web.yml` корректно указан
`deploy_web_frontend_letsencrypt_email` и DNS `grinwer.online` указывает
на `nl-web` — иначе certbot --nginx упадёт.

## Страницы

- `/admin/login` — ввод admin-токена. По умолчанию это общий `ADMIN_API_TOKEN`
  из `.env` бэка, но можно завести отдельный scoped API-token через
  страницу «API tokens» и логиниться им — в логах `AuditLog` это развяжет
  действия человека-админа от серверных (bot/worker).
- `/admin/` — **Dashboard**: сводные счётчики (users, subs active/total,
  invoices pending, nodes active/total, devices, provisioning tasks
  pending/failed). Для графиков и time-series — Grafana.
- `/admin/users` — **Users**: список с поиском по `telegram_id`/`email`
  и боковой панелью с подписками выбранного юзера.
- `/admin/invoices` — **Invoices**: pending/paid/failed инвойсы, фильтры,
  ручное `mark_paid` для ручных переводов.
- `/admin/plans` — **Plans**: CRUD по тарифам (цена, длительность,
  max_devices, traffic_limit_mb, visibility).
- `/admin/tasks` — **Provisioning tasks**: последние `ProvisioningTask` с
  фильтрами, expandable строки (error_message / payload / result), кнопка
  **rerun** на failed/pending. Нужно, чтобы диагностировать застрявший
  bootstrap без `docker compose logs worker`.
- `/admin/nodes` — **Nodes**: список VPN-нод с автообновлением раз в 5 сек
  (для отслеживания `registering → active` во время bootstrap). Кнопка
  «+ Добавить ноду» открывает форму создания — бэкенд автоматически
  enqueue'ит таску на `site.yml` через worker. Клик на строку разворачивает
  панель конфигов протоколов (VLESS Reality, VLESS XHTTP, VLESS+WS+CDN)
  с формой «+ Добавить конфиг» — протокольные дефолты (порт/SNI)
  проставляются автоматически.  ShadowTLS+SS и Hysteria2 deprecated
  (0.2/0.3), через UI не создаются.
- `/admin/tokens` — **API tokens**: scoped токены с отдельными правами для
  интеграций (probes, внешние сервисы) и самих админов.

## Что дальше

- Delete node из UI и ручной drain.
- Замена ручных TS-типов на `openapi-typescript` codegen из `/openapi.json`.
- Tree-shake страниц на роли: отдельный scoped token → отдельный набор пунктов
  в навигации.
