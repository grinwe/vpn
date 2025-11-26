# Развертывание и архитектура

## Целевая архитектура узлов
- 2–3 VPN-ноды (например NL/DE/PL) с выделенным пользователем `vpnsvc`.
- На каждой ноде: ShadowTLS v3 + Shadowsocks (основной протокол, порты 443/8443) и опционально VLESS+Reality (порт 9443).
- Межсервисные связи:
  - Клиент подключается к ShadowTLS/SS или VLESS; ноды не хранят БД, только учётки.
  - Backend управляет учётками через Ansible (динамическое инвентаризация по данным БД) и создаёт записи `ProvisioningTask`.
- Управляющий хост с PostgreSQL и backend (docker-compose).

## Быстрый старт локально
```bash
docker-compose up -d  # поднимет postgres + backend + бота
```
Backend доступен на http://localhost:8000.

### Переменные окружения и секреты
- `ADMIN_API_TOKEN` — обязательный shared-secret для админских методов backend и Telegram-бота (передаётся в заголовке `X-Admin-Token`).
- `DATABASE_URL` — строка подключения к PostgreSQL без дефолтных паролей.
- `BOT_TOKEN`, `ADMIN_IDS`, `BACKEND_URL` — параметры бота; `ADMIN_API_TOKEN` должен совпадать с backend.
Все секреты хранить в CI/CD Secrets или vault, не коммитить в git.

## Деплой инфраструктуры Ansible
1. Заполнить `infra/ansible/inventories/prod/hosts.yml` реальными адресами.
2. Заменить SSH ключи и пароли в `group_vars/vpn_nodes.yml` и `group_vars/db.yml`.
3. Выполнить:
```bash
cd infra/ansible
ansible-playbook -i inventories/prod/hosts.yml site.yml
```
Это:
- подготовит базовые настройки безопасности (`bootstrap_node`),
- установит ShadowTLS+Shadowsocks и скрипты управления пользователями (`install_shadowtls_stack`),
- при включённой опции `vless_enabled` установит VLESS+Reality (`install_vless_reality`),
- выполнит health-check ролями (`check_node_health`),
- развернёт docker-compose с Postgres + backend на хосте db_host.

### Провижининг устройств через Ansible
Для создания/отзыва конкретного VPN-пользователя backend вызывает плейбук `infra/ansible/playbooks/provision_device.yml` с extra-vars, содержащими `username`, `password`, `port`, `method` и `state`. Плейбук заворачивает вызов `manage_vpn_user.sh` и может повторно запускаться без последствий (idempotent при одинаковых параметрах).
Backend и Ansible располагаются в одном репозитории; раннер проверяет наличие каталога `infra/ansible` через `/healthz?deep=true`.

## Backend API (ключевые маршруты)
- `POST /api/nodes`, `GET /api/nodes` — управление нодами; при создании автоматически ставится задача `ProvisioningTask` на bootstrap.
- `POST /api/nodes/{id}/configs`, `GET /api/nodes/{id}/configs` — создание/листинг VPN-листенеров.
- `POST /api/subscriptions` — создать пользователя/подписку/устройство, генерирует конфиги и задачу на провижининг пользователя.
- `POST /api/users/{id}/disable` — блокировка пользователя и его подписок.
- `GET /api/users/{id}` — данные подписок и конфигов.
- `POST /api/payments` — фиксация платежа (пока вручную).
- `POST /api/subscriptions/{id}/traffic` — инкремент трафика подписки; при превышении лимита блокирует подписку и запускает отзыв устройств.
- `GET /api/provisioning/tasks` — аудит задач провижининга.
- `/metrics` и `/healthz` — минимальная наблюдаемость.

## Telegram-бот
- Команды `/start`, выбор тарифа через клавиатуру, `/confirm` создаёт подписку через backend, `/status` выводит остаток (используются поля node/region).
- Бот подключается к backend по HTTP (переменная окружения `BACKEND_URL`).

## Добавление новой VPN-ноды
1. Добавить хост в `inventories/prod/hosts.yml` секцию `vpn_nodes`.
2. При необходимости расширить `firewall_allowed_ports`.
3. Запустить `ansible-playbook ... --limit <hostname>`.
4. Создать запись через API `POST /api/nodes` (обновится статус, создастся `ProvisioningTask`).
5. Добавить `vpn_configs` через `POST /api/nodes/{id}/configs` (ShadowTLS+SS или VLESS Reality).

## Добавление пользователя вручную
```bash
curl -X POST http://backend:8000/api/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"telegram_id": "123456", "plan_id": 1, "device_name": "iphone"}'
```
Полученные строки `ss://`/`vless://` отдаём клиенту; backend создаст задачу провижининга пользователя на выбранной ноде.

## Платежи и биллинг (заложено)
- Таблица `payments` со статусами `pending|paid|failed|refunded`.
- Можно связать webhook платежного шлюза с `POST /api/payments` и обновлением статуса.
- Telegram-бот пока использует ручное подтверждение, но в хендлерах оставлены места для интеграции.

## Учёт трафика и лимиты
- Backend принимает простые обновления потреблённого трафика через `POST /api/subscriptions/{subscription_id}/traffic` с телом `{ "used_mb": <integer> }`.
- Значение суммируется с `traffic_used_mb`; при превышении `traffic_limit_mb` подписка помечается как `blocked`, вызывается отзыв устройств через ProvisioningTask и создаётся audit-запись.
- Экспортёр/коллектор трафика можно подключить позже (например, по логам нод или NetFlow), сейчас требуется лишь HTTP-запрос на backend.
