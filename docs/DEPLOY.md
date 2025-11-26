# Развертывание и архитектура

## Целевая архитектура узлов
- 2–3 VPN-ноды (например NL/DE/PL) с выделенным пользователем `vpnsvc`.
- На каждой ноде: ShadowTLS v3 + Shadowsocks (основной протокол, порты 443/8443) и опционально VLESS+Reality (порт 9443).
- Межсервисные связи:
  - Клиент подключается к ShadowTLS/SS или VLESS; ноды не хранят БД, только учётки.
  - Backend управляет учётками через ansible/ssh/agent вызовы.
- Управляющий хост с PostgreSQL и backend (docker-compose).

## Быстрый старт локально
```bash
docker-compose up -d  # поднимет postgres + backend + бота
```
Backend доступен на http://localhost:8000.

## Деплой инфраструктуры Ansible
1. Заполнить `infra/ansible/inventories/prod/hosts.yml` реальными адресами.
2. Заменить SSH ключи и пароли в `group_vars/vpn_nodes.yml` и `group_vars/db.yml`.
3. Выполнить:
```bash
cd infra/ansible
ansible-playbook -i inventories/prod/hosts.yml site.yml
```
Это:
- подготовит базовые настройки безопасности (SSH по ключам, UFW),
- скопирует установочные скрипты ShadowTLS/VLESS и выполнит их,
- развернёт docker-compose с Postgres + backend на хосте db_host.

## Backend API (ключевые маршруты)
- `POST /api/subscriptions` — создать пользователя/подписку, генерирует конфиги.
- `POST /api/users/{id}/disable` — блокировка пользователя и его подписок.
- `GET /api/users/{id}` — данные подписок и конфигов.
- `POST /api/payments` — фиксация платежа (пока вручную).

## Telegram-бот
- Команды `/start`, выбор тарифа через клавиатуру, `/confirm` создаёт подписку через backend, `/status` выводит остаток.
- Бот подключается к backend по HTTP (переменная окружения `BACKEND_URL`).

## Добавление новой VPN-ноды
1. Добавить хост в `inventories/prod/hosts.yml` секцию `vpn_nodes`.
2. При необходимости расширить `firewall_allowed_ports`.
3. Запустить `ansible-playbook ... --limit <hostname>`.
4. Добавить запись сервера в таблицу `servers` (SQL или админ-скрипт) с указанием пулов.

## Добавление пользователя вручную
```bash
curl -X POST http://backend:8000/api/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"telegram_id": "123456", "plan_id": 1}'
```
Полученные строки `ss://` и `vless://` отдаём клиенту.

## Платежи и биллинг (заложено)
- Таблица `payments` со статусами `pending|paid|failed|refunded`.
- Можно связать webhook платежного шлюза с `POST /api/payments` и обновлением статуса.
- Telegram-бот пока использует ручное подтверждение, но в хендлерах оставлены места для интеграции.
