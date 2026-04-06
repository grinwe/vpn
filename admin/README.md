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

- `/admin/login` — ввод admin-токена (тот же `ADMIN_API_TOKEN` из .env бэка).
- `/admin/` — Dashboard: базовые счётчики по последним 200 юзерам.
  Для полноценных метрик — Grafana (`ssh -L 3000:localhost:3000`).
- `/admin/users` — список с поиском по `telegram_id`/`email` и
  боковой панелью с подписками выбранного юзера.

## Что дальше

- Страницы Invoices / Subscriptions / Nodes (CRUD уже есть в бэке).
- Замена ручных TS-типов на `openapi-typescript` codegen из `/openapi.json`.
- Отдельный scoped admin-token (сейчас используется тот же, что у бота).
