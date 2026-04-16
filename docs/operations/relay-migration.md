# Runbook — миграция на relay‑архитектуру (D.3)

Пошаговый план для миграции **одной** существующей foreign direct‑ноды на схему «RU‑relay → WG → foreign exit». Запускается админом **поштучно**, по одной foreign‑ноде за проход. Между фазами — выдержка (сутки), чтобы клиенты успели рефетчить sub‑link.

Архитектурный контекст — `docs/RELAY_ROADMAP.md`. Код‑предпосылки (✅ на 2026‑04‑16): все стадии 0.1/0.2/0.3/A/B/C/D.1/D.2 закрыты. 0.4 и D.3 — оперативные.

## Инвариант для юзера

`sub_token` сохраняется на каждом шаге. Клиент в v2rayNG/Hiddify хранит один URL `https://grinwer.online/api/sub/<token>` — этот URL не меняется. Меняется только содержимое ответа: backend отдаёт другой набор `vless://...@host:port`. QR пересканировать НЕ нужно.

Если клиент давно не выходил в сеть — увидит новый профиль при первом рефетче sub‑link.

## Допущения

- `foreign-XX` — существующая нода в таблице `vpn_nodes`, статус `active`, у неё N активных подписок.
- Мы готовим пару: `exit-XX` (зарубежный WG‑сервер, таблица `wg_exit_nodes`) + `ru-relay-XX` (RU‑нода с VLESS, таблица `vpn_nodes`, `relay_config` указывает на `exit-XX`).
- Между шагами — выдержка минимум сутки. Это даёт клиентам время рефетчить sub‑link.

## P1. Превратить foreign‑XX в exit

**Цель:** на foreign‑XX поднять WG‑сервер (`wg_exit_node` роль). VLESS‑сервисы на ней продолжают работать как прежде — трафик текущих юзеров не трогается.

1. **Открыть админку → `/exits`, нажать «+ Создать exit».**
   - `name` = `exit-foreign-XX` (уникально).
   - `host` / `ssh_port` = как у foreign‑XX (обычно 22).
   - `provider_id` = тот же cloud provider, что у foreign‑XX (если cloud), иначе `null`.
   - Сохранить → запись появится в таблице с `status=registering`.

2. **Сгенерировать WG‑ключи:** в строке exit нажать **keygen**. Это X25519 keypair, private хранится через Fernet‑encrypt, public показан в UI.

3. **Bootstrap WG‑роли на ноде** (пока вручную, см. раздел «Известные ограничения» ниже):
   ```bash
   cd /opt/vpn/infra/ansible
   ansible-playbook -i inventories/prod/hosts.yml site.yml \
     --limit exit-foreign-XX --tags wg-exit --check --diff
   # если diff устраивает — без --check
   ```
   > **Важно:** на этом шаге роль `wg_exit_node` падает при пустом `wg_exit_peers`. Это ожидаемо — P2 ниже добавит первого peer'а. В `--check` увидишь, что sysctl `net.ipv4.ip_forward=1` и NAT‑правила в iptables собираются корректно.

4. **Проверка:** после успешного P2 (когда добавится peer) — `wg show` на exit‑XX покажет `interface: wg0`, peer с `latest handshake`, `allowed ips`.

5. **Переключить exit в `active`** через PATCH в админке (кнопка «статус» в строке).

**Acceptance P1:** `GET /api/exits` возвращает строку с `status=active`, `wg_public_key != ""`, `peers_count=0`.

## P2. Создать RU‑relay, прикрепить к exit‑XX

**Цель:** RU‑нода с VLESS + WG‑клиентом, выходящим через `exit-foreign-XX`.

1. **Создать ноду в `/nodes`** (кнопка «+ Добавить ноду»): имя `ru-relay-XX`, host — RU‑IP, region — RU. `pool_id` — тот же, что у foreign‑XX (чтобы `choose_node` её увидел).

2. **Дождаться `status=active`** после автоматического bootstrap (3–5 минут).
   > На этом шаге нода — обычная direct VLESS‑нода без relay_config. Роль `relay_jump_node` запускается в **disable‑режиме** (no‑op на свежей direct‑ноде — см. D.1 acceptance).

3. **Прикрепить relay к exit** в `/exits`:
   - Раскрыть строку `exit-foreign-XX`.
   - В форме «+ Прикрепить relay» выбрать `ru-relay-XX` из dropdown, адрес оставить пустым (автоаллокация `10.77.0.N/32`).
   - Submit → в БД создаётся `relay_exit_link`, `ru-relay-XX.relay_config` заполняется (encrypted WG client private key + endpoint).

4. **Прогнать ansible для обеих нод:**
   ```bash
   # Обновить peers на exit (добавить нового client'а)
   ansible-playbook -i inventories/prod/hosts.yml site.yml \
     --limit exit-foreign-XX --tags wg-exit
   # Поднять wg0 + пропатчить Xray на relay
   ansible-playbook -i inventories/prod/hosts.yml site.yml \
     --limit ru-relay-XX
   ```

5. **Проверка тоннеля:**
   ```bash
   ssh ru-relay-XX 'wg show wg0 && curl -s --interface wg0 https://ifconfig.me'
   # Должен показать IP exit‑foreign‑XX (а не IP RU‑ноды)
   ```

**Acceptance P2:**
- На RU‑relay: `wg show wg0` показывает `latest handshake` свежий, `curl --interface wg0 ifconfig.me` = IP exit'а.
- В Xray config.json ветки `freedom` / `direct` добавился `sockopt.interface: wg0`.
- `GET /api/exits` → `peers_count=1`.
- В админке колонка «active users» у `ru-relay-XX` = 0 (подписок ещё нет).

**Выдержка: 24ч** на проверку стабильности туннеля.

## P3. Переселить подписки foreign‑XX → ru‑relay‑XX

**Цель:** все активные подписки с foreign‑XX перенести на ru‑relay‑XX. `sub_token` сохраняется.

1. **Сначала исключить foreign‑XX из пула** (кнопка «исключить» в `/nodes`). Это не трогает существующих юзеров, но блокирует создание новых подписок на ней.

2. **Нажать «переселить на…»** в строке foreign‑XX.
   - В модалке выбрать `ru-relay-XX` → «Переселить».
   - Endpoint: `POST /api/nodes/{foreign-XX.id}/migrate-to/{ru-relay-XX.id}` (pool/health/cooldown‑фильтры обходятся — выбор админа).

3. **Ждать, пока все subs переселятся.** В баннере прогресса показывается количество task'ов, поллятся `/provisioning/tasks`.
   - `migrated_count` должен совпасть с `considered_count`. Если `failed[]` непустой — смотреть `audit_logs` (`action=node_bulk_migrated`) за деталями.
   - Клиенты продолжают выходить через foreign‑XX до момента, когда подхватят новый sub‑link (обычно 30 мин — 6 ч в зависимости от клиента).

**Acceptance P3:**
- `GET /api/nodes/{foreign-XX.id}/active-users` → 0 (или только shared/disabled).
- `GET /api/nodes/{ru-relay-XX.id}/active-users` → N (прежнее количество).
- В админке: у foreign‑XX «active users» = 0, у ru‑relay‑XX = N.
- `audit_logs` содержит `node_bulk_migrated` с `migrated_count=N`, `failed_count=0`.

**Выдержка: 24ч** на проверку, что клиенты перерефетчили sub‑link. Мониторить:
- `SELECT COUNT(*) FROM subscriptions WHERE node_id = {foreign-XX.id} AND status = 'active';` — должно быть 0.
- Жалобы юзеров (если есть мониторинг Telegram‑бота — смотреть там).

## P4. Погасить VLESS‑сервисы на foreign‑XX

**Цель:** foreign‑XX превратить в pure‑exit. VLESS больше не нужен.

Два варианта:

### P4a. Сохранить foreign‑XX как ноду в `vpn_nodes` (pure exit‑дубль)
Если планируется хранить запись в основной таблице для мониторинга/метрик:
```bash
# Остановить и задизейблить все xray‑сервисы на ноде вручную
ssh foreign-XX 'systemctl stop xray-reality xray-xhttp xray-ws_cdn 2>/dev/null; \
  systemctl disable xray-reality xray-xhttp xray-ws_cdn 2>/dev/null; true'
```
И в админке: оставить `is_active=false`, либо (лучше) — удалить VPNConfig‑строки через `/nodes/{id}` → раскрыть → «Конфиги» → удалить.

### P4b. Удалить foreign‑XX из `vpn_nodes` (рекомендуется)
После 24ч без активных subs:
- В админке `/nodes` → «удалить». Для cloud‑ноды это запустит `destroy_node` (VM убивается через provider API), для manual — просто `DELETE` строки.
- `exit-foreign-XX` в `wg_exit_nodes` остаётся — она теперь независимая сущность.

**Acceptance P4:**
- `foreign-XX` отсутствует в `GET /api/nodes`.
- `exit-foreign-XX` жив в `GET /api/exits`, `peers_count=1` (всё ещё ru‑relay‑XX прикреплён).
- На `ru-relay-XX` `wg show` зелёный, юзеры работают.

## Прохождение по всем 8 нодам

Повторить P1–P4 для каждой foreign‑XX. Между нодами выдержка не обязательна — если P3 на первой ноде прошёл без fail'ов, процесс можно катить конвейером:
- P1+P2 для `foreign-02` пока `foreign-01` в «24ч выдержки» после P2.
- P3 `foreign-01` → P3 `foreign-02` по одной за раз, чтобы не смешивать audit log.

**Финальное состояние:**
- `wg_exit_nodes`: 8 строк, по одной на каждую бывшую foreign‑ноду.
- `vpn_nodes`: ≥1 `ru-relay-XX` (возможно меньше, чем 8, если несколько relay'ев делят один exit).
- Все active subs на relay‑нодах.
- Foreign‑ноды либо удалены, либо помечены `is_active=false` без VLESS‑сервисов.

## Откат

Если на любом шаге что‑то идёт не так — откатить **только текущую пару**:

- **Откат P3** (подписки уже переехали): `POST /api/nodes/{ru-relay-XX.id}/migrate-to/{foreign-XX.id}` (тот же endpoint, наоборот). `sub_token` опять сохраняется.
- **Откат P2** (link создан, тоннель поднят): в `/exits` раскрыть exit, в таблице привязанных relay'ев нажать «detach». В БД линка удалится, `ru-relay-XX.relay_config = NULL`. Прогнать `ansible-playbook ... --limit ru-relay-XX` — роль `relay_jump_node` перейдёт в disable‑режим (snap wg0 вниз, unpatch Xray sockopt, все task'и `ok` без changed).
- **Откат P1** (exit создан, но пусто): в `/exits` удалить exit (блокируется, пока есть link'и). Ручной `systemctl stop wg-quick@wg0 && systemctl disable wg-quick@wg0` на foreign‑XX.

## Известные ограничения (2026‑04‑16)

- **`POST /exits/{id}/bootstrap`** — endpoint из roadmap B не реализован. Временно bootstrap WG‑роли на exit запускается вручную через CLI `ansible-playbook --tags wg-exit --limit`. P1 пункт 3 — эта ручная команда.
- **attach/detach не запускает ansible автоматически** (roadmap C отложил в D). P2 пункт 4 и откат P2 — вручную.
- **Первый прогон `wg_exit` на пустом exit падает** (assert на пустой `wg_exit_peers`). Workaround: делать P1+P2 одним заходом (создать exit → keygen → сразу attach первый relay → ansible на exit пройдёт с peer).
- **Зависшие оффлайн клиенты**: если юзер не рефетчил sub‑link > N дней, P4 на его ноде убьёт VLESS под ним. Пока нет автоматического трекинга `last_sub_fetch_at` — ориентироваться на общий лаг 24ч между P3 и P4.

## Полезные запросы для мониторинга

```sql
-- Активные subs по нодам (посмотреть, откуда ещё не переехали)
SELECT n.id, n.name, n.region, COUNT(s.id) AS active_subs
FROM vpn_nodes n
LEFT JOIN subscriptions s ON s.node_id = n.id AND s.status = 'active'
GROUP BY n.id, n.name, n.region
ORDER BY active_subs DESC;

-- Свежие миграционные аудит‑логи
SELECT id, action, target_id, extra, created_at
FROM audit_logs
WHERE action IN ('node_bulk_migrated', 'subscription_migrated')
ORDER BY id DESC LIMIT 20;

-- Relay→exit карта (кто куда)
SELECT r.id AS relay_id, r.name AS relay, e.id AS exit_id, e.name AS exit,
       l.wg_client_address_v4, l.created_at
FROM relay_exit_links l
JOIN vpn_nodes r ON r.id = l.relay_node_id
JOIN wg_exit_nodes e ON e.id = l.exit_id
ORDER BY l.created_at;
```
