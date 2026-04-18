# Runbook — что делать, когда сломалось

Это не «как выкатить» (про это — `infrastructure/deployment.md`) и не «как работает» (про это — `components/*`). Это сценарии **«что-то пошло не так, быстрое восстановление»**, с конкретными командами. Для каждого сценария: симптом → как подтвердить → быстрое действие → что посмотреть после.

Хост для control-plane — `45.14.244.140` (см. `infrastructure/deployment.md`), все команды предполагают, что вы залогинены туда как `root` и находитесь в `/opt/vpn`.

## Базовые команды «посмотреть что происходит»

```bash
# Состояние контейнеров
docker compose ps
docker compose logs -f --tail=100 backend
docker compose logs -f --tail=100 worker
docker compose logs -f --tail=100 bot

# Последние события в БД
docker compose exec db psql -U vpn -d vpn -c \
  "SELECT id, action, actor, created_at FROM audit_logs ORDER BY id DESC LIMIT 30;"

# Метрики backend'а (protected by X-Admin-Token)
curl -s -H "X-Admin-Token: ${ADMIN_API_TOKEN}" http://127.0.0.1:8000/metrics | grep -E 'vpn_|warm_pool|payment'

# RQ
docker compose exec worker python -c "from app.queue import get_queue; q=get_queue(); print('queued=', q.count, 'failed=', q.failed_job_registry.count)"
```

Префер: `docker compose logs` первым делом, **любой** инцидент. 99% ответов — там.

---

## 1. Backend не отвечает на `/healthz`

**Симптом:** `curl http://127.0.0.1:8000/healthz` висит или 502, `deploy_app_stack` падает на "Wait for backend to be healthy".

**Диагностика:**

```bash
docker compose ps backend
docker compose logs --tail=200 backend
```

Частые причины (в порядке убывания вероятности):

- **Alembic гонка с воркером.** Если `SKIP_MIGRATIONS=1` в worker случайно снят — оба процесса уснули на advisory lock. См. `docker-compose.yml:98-102`.
  - **Fix:** `docker compose restart worker backend` + проверить что в `worker.environment` стоит `SKIP_MIGRATIONS: "1"`.
- **Неверный `DATABASE_URL` / Postgres не поднялся.** `backend` стартует, но падает на подключении.
  - **Fix:** `docker compose logs db` + `docker compose exec db pg_isready -U vpn -d vpn`.
- **Отсутствует обязательный env.** `APP_SECRET_KEY`, `WEBAPP_JWT_SECRET` и т.д. — FastAPI раскручивает роутеры на старте, отсутствие ключей ломает импорт.
  - **Fix:** `cat /opt/vpn/.env | grep -E 'SECRET|TOKEN'`, перепрогнать `deploy_app_stack`.

**После восстановления:**

```bash
# Убедиться, что миграции применились
docker compose exec backend alembic -c /app/alembic.ini current
# Последний healthz + метрика
curl http://127.0.0.1:8000/healthz
```

---

## 2. Crash-loop `worker` / `bot`

**Симптом:** `docker compose ps` показывает `Restarting` напротив сервиса; `deploy_app_stack` падает на ассерте «Fail if any compose service is restarting or exited».

**Диагностика:**

```bash
docker compose logs --tail=200 worker
docker compose logs --tail=200 bot
```

Частые причины:

- **Worker:** битый `PROVISIONING_SSH_KEY` путь, ansible-root недоступен, отсутствующий `DATABASE_URL`.
  - Смотреть первые 20 строк лога — туда падает traceback импорта.
- **Bot:** `BOT_TOKEN` битый → aiogram падает в auth; `ADMIN_IDS` — невалидный формат; backend недоступен → бот ещё попытается `depends_on: backend`, но если backend только что поднялся, stale DNS в контейнере может подождать.
  - **Fix:** `docker compose restart bot` после того, как backend зелёный.

**Частная проблема — `PROVISIONING_SSH_KEY` default.** Если в `.env` пусто, compose монтирует literal строку-ключ как путь (см. `docker-compose.yml:143-144`, подробнее в `infrastructure/deployment.md`). Симптом: worker живой, но любая provisioning-таска падает на `Permission denied (publickey)`.

```bash
# Проверить что реально смонтировано
docker compose exec worker cat /run/secrets/provisioning_key
# Ожидаем: -----BEGIN OPENSSH PRIVATE KEY-----
# Если видим литерал public key — .env битый
grep PROVISIONING_SSH_KEY /opt/vpn/.env
```

**Fix:** Положить приватник в `/opt/vpn/secrets/provisioning_key` (mode 0600), вписать `PROVISIONING_SSH_KEY=./secrets/provisioning_key` в `.env`, `docker compose up -d --force-recreate worker`.

---

## 3. Provisioning-таска зависла в `pending`

**Симптом:** Пользователь купил подписку, но в боте «готовим…» висит >5 минут. В БД — `ProvisioningTask.status=pending`.

**Диагностика:**

```bash
docker compose exec db psql -U vpn -d vpn -c \
  "SELECT id, target_type, target_id, action, status, created_at, error
   FROM provisioning_tasks WHERE status IN ('pending','running','failed') ORDER BY id DESC LIMIT 20;"
docker compose exec worker python -c "from app.queue import get_queue; q=get_queue(); print(q.count, q.failed_job_registry.count)"
```

Возможные причины:

- **Воркер не запущен / в crash-loop.** См. сценарий 2.
- **RQ-очередь пуста, но task.status=pending.** Значит `run_task_async` не вызвался (race между backend'ом и enqueue). Случается, если backend упал **после** commit'а таска, но **до** enqueue.
  - **Fix:** вручную энкьюить. В admin SPA есть кнопка «Re-run task», или через SQL:
    ```sql
    UPDATE provisioning_tasks SET status='pending' WHERE id = <id>;
    ```
    и потом из `docker compose exec backend python`:
    ```python
    from app.db import SessionLocal
    from app.services.provisioning import ProvisioningOrchestrator
    with SessionLocal() as db:
        t = db.get(models.ProvisioningTask, <id>)
        ProvisioningOrchestrator(db).run_task_async(t)
    ```
- **Task есть в RQ, но воркер не берёт.** `docker compose exec worker rq info` (если есть) или `docker compose logs worker | grep -i 'worker'`. Обычно worker должен в логах писать `Worker rq:worker:xxx: started`. Если нет — SIGTERM loop, см. сценарий 2.

---

## 4. Ansible упал на ноде

**Симптом:** Task `failed`, `error` поле содержит stderr от `ansible-playbook`.

**Диагностика:**

```bash
docker compose exec db psql -U vpn -d vpn -c \
  "SELECT id, target_type, target_id, error FROM provisioning_tasks WHERE status='failed' ORDER BY id DESC LIMIT 5;"
```

Частые шаблоны в `error`:

- **`Permission denied (publickey)`** — `/run/secrets/provisioning_key` битый или на ноде нет нашего `authorized_keys`. См. сценарий 2. Для ноды — `ssh -i secrets/provisioning_key root@<node_host>` с хоста, проверить руками.
- **`host key verification failed`** — вопреки `host_key_checking=False` в `ansible.cfg`, `ControlPath` кэш на worker'е хранит старый ключ. Для каждой ноды: `docker compose exec worker rm -rf /tmp/ansible-ssh-*`.
- **`Expected ports did not open in time`** из `check_node_health` — на ноде не поднялся xray/shadow-tls/hysteria. Зайти на ноду руками:
  ```bash
  ssh -i secrets/provisioning_key root@<node_host>
  ss -tulpn
  systemctl status xray shadow-tls ssserver hysteria-server
  journalctl -u xray --no-pager -n 50
  ```
  Типичный fix для xray: `config.json` битый (невалидный JSON после ручного редактирования). Откатить на `config.json.1` (backup рендерит сама роль), `systemctl restart xray`.
  > **Примечание:** с апреля 2026 протокольные роли содержат auto-recovery — если xray unit в `failed` state, роль сама делает `reset-failed` + restart. Ручное вмешательство нужно только если auto-recovery тоже падает (читай вывод ansible).
- **`Config validation failed`** (install_vless_reality) — `xray -test` упал. Смотреть вывод в ansible stderr — там точный текст ошибки.

**После fix'а:**

```bash
# Повторный прогон именно этого таска (идемпотентно)
curl -X POST -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  http://127.0.0.1:8000/api/provisioning/tasks/<id>/retry
```

---

## 5. Warm-pool не заполняется

**Симптом:** Покупка подписки стабильно идёт cold path'ом (в боте «готовим…» 20–40с). Метрика `vpn_warm_pool_depth{node}` = 0 или близко к нулю на активных нодах.

**Диагностика:**

```bash
curl -s -H "X-Admin-Token: $ADMIN_API_TOKEN" http://127.0.0.1:8000/metrics | \
  grep -E 'vpn_warm_pool_(depth|hits|misses)_total'

docker compose logs worker | grep -i 'warm_pool\|ensure_pool' | tail -30
```

Возможные причины:

- **`WARM_POOL_ENABLED=0`.** Проверить `.env` — кто-то мог руками выключить. `docker compose exec worker env | grep WARM_POOL`.
- **Warmer не тикает.** `WARM_POOL_CHECK_INTERVAL` слишком большой / self-reschedule упал. В worker-логах должно быть `run_warm_pool_check` каждые ~120с по дефолту.
- **Ансибл фейлится при warming.** В логах видно `warm_one_bundle failed`. Применяется сценарий 4.
- **Ноды в `registering` или `error`.** `ensure_pool` берёт только `status=active`. Проверить:
  ```sql
  SELECT id, name, status, is_active, health_score FROM vpn_nodes ORDER BY id;
  ```

**Fix:** Исправить корень (обычно ansible на одной ноде), дальше warmer сам догонит пул. Форсить вручную:

```python
# docker compose exec worker python
from app.db import SessionLocal
from app.services.warm_pool import ensure_pool
with SessionLocal() as db:
    print(ensure_pool(db))
```

---

## 6. Платежи: webhook провайдера не приходит

**Симптом:** Пользователь оплатил, но invoice остаётся `pending`. `audit_logs` не содержит записи про `payment_*`.

**Диагностика:**

```bash
docker compose logs backend | grep -i 'webhook\|payments' | tail -50
# Pending invoices
docker compose exec db psql -U vpn -d vpn -c \
  "SELECT id, user_id, status, provider, external_id, created_at
   FROM invoices WHERE status='pending' ORDER BY id DESC LIMIT 20;"
```

По провайдерам:

- **CryptoBot:** сигнатура не сошлась → 401. Проверить `CRYPTOBOT_TOKEN` в `.env`, HMAC считается от **sha256(token)** как ключа. Логи backend'а должны показать `Invalid Crypto-Pay-Api-Signature`. Если да — токен перевыпустили, обновить в `.env` + `force-recreate backend`.
- **Telegram Stars (webhook-режим, #62):** TG шлёт update'ы напрямую на `POST /tg-webhook` backend'а. Backend проверяет `X-Telegram-Bot-Api-Secret-Token` и обрабатывает `successful_payment`/`pre_checkout_query` самостоятельно. Если не приходят:
  - Проверить `TELEGRAM_WEBHOOK_SECRET_TOKEN` и `TELEGRAM_WEBHOOK_URL` в `.env`.
  - Проверить nginx location `= /tg-webhook` проксирует на backend.
  - `docker compose logs backend | grep tg-webhook` — видны ли входящие update'ы?
  - `docker compose logs backend | grep setWebhook` — webhook зарегистрировался на startup?
- **Telegram Stars (polling legacy):** Если `BOT_WEBHOOK_PORT=0` — старый поток, webhook форвардится ботом. Смотреть `bot` логи на `successful_payment` event. Проверить `TELEGRAM_STARS_WEBHOOK_SECRET` одинаково в обоих env.
- **SBP (generic):** webhook может не прийти, если провайдер кладёт его на URL, закрытый nginx'ом или CF WAF'ом. Смотреть `/var/log/nginx/access.log` на хосте — есть ли вообще POST на `/api/payments/webhook/<slug>`.

**Manual mark paid:** админский путь — через бота `/invoices` → inline-кнопка, или через SPA. Это триггерит `_mark_invoice_paid_core`, который сделает branch-specific логику (topup vs renewal vs new_subscription). См. `components/payments.md`.

---

## 7. Нода «пропала» из балансировки

**Симптом:** Подписки на ноду `X` больше не назначаются, autoscale начал спавнить новые. Нода жива по SSH.

**Диагностика:**

```sql
SELECT id, name, status, is_active, health_score, cooldown_until, updated_at
FROM vpn_nodes WHERE id = <X>;
```

- `is_active=false` — кто-то нажал в SPA.
- `status='error'` — был fail при bootstrap или `destroy_node`. **Из `error` автомата нет**, см. `infrastructure/nodes.md`.
- `health_score < MIN_HEALTHY_SCORE` — probe'ы упали. Смотреть `health_probes` таблицу по `node_id`.
- `cooldown_until > now` — временный lock. Обычно проходит сам через 5–15 минут.
- `status='draining'` — нода помечена для вывода из пула (автоматика выпилена 2026-04-17; статус меняется руками). Новые subs сюда не едут, старые надо мигрировать через admin SPA. Вернуть в пул: `UPDATE vpn_nodes SET status='active' WHERE id=<X>;`.

**Fix «оживить ноду после error»:**

```sql
UPDATE vpn_nodes SET status='active', health_score=NULL, cooldown_until=NULL WHERE id=<X>;
-- health_score=NULL — score пересчитается автоматически из health_probes при следующем check
```

После этого прогнать health check вручную через `/api/nodes/<X>/health` или перезапустить ansible `site.yml -l <name>`.

---

## 8. Disk / БД растёт без обратной связи

**Симптом:** `df -h` на хосте показывает >80% занятости. Больше всего обычно — postgres volume (`/var/lib/docker/volumes/vpn_db_data/`).

**Диагностика:**

```bash
docker compose exec db psql -U vpn -d vpn -c \
  "SELECT schemaname, relname, pg_size_pretty(pg_total_relation_size(c.oid)) AS size
   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE relkind = 'r' AND n.nspname NOT IN ('pg_catalog','information_schema')
   ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 15;"
```

Обычные виновники:

- **`audit_logs`** — пишется при каждом действии, **никогда не чистится**. За несколько месяцев — миллионы строк. Безопасно обрезать:
  ```sql
  DELETE FROM audit_logs WHERE created_at < now() - interval '90 days';
  VACUUM FULL audit_logs;
  ```
- **`health_probes`** — per-node замеры, растут линейно со временем и количеством нод. Аналогично truncate'ить по дате.
- **`credentials`** (revoked, не удалённые) — stage 2 `physical_revoke_credential_bundle` должен удалять, но при fail'ах ansible строки могут копиться.
  ```sql
  SELECT pool_state, count(*) FROM credentials GROUP BY pool_state;
  ```
  Если много `revoked` — смотреть логи воркера на fail'ы physical revoke.

**На уровне docker:** `docker system prune -a` освобождает кучу от старых образов/слоёв.

---

## 9. Полный deploy rollback

**Симптом:** Новая версия сломала что-то критическое. Нужно откатиться.

**Путь:**

```bash
# 1. На контроллере (рабочей машине)
cd ~/work_ai/vpn
git log --oneline -10             # найти предыдущий хороший SHA
git checkout <good_sha>

# 2. Прогнать deploy_app_stack
cd infra/ansible
ansible-playbook -i inventories/prod/hosts.yml site.yml -l nl-web --ask-vault-pass \
  --tags deploy_app_stack

# 3. Проверить что прод зелёный
ssh root@45.14.244.140 'cd /opt/vpn && docker compose ps && curl -sf http://127.0.0.1:8000/healthz'
```

**Осторожно с миграциями.** Alembic может идти только вперёд без ручного `downgrade`. Если новый релиз применил миграцию, которую старый код не понимает, rollback кода **не** откатит схему. Возможные варианты:

- Если миграция **аддитивная** (новые колонки с nullable), старый код часто работает как есть.
- Если миграция **разрушительная** (DROP COLUMN) — сначала `alembic downgrade` вручную:
  ```bash
  docker compose exec backend alembic -c /app/alembic.ini downgrade -1
  ```
  Потом уже git checkout. Проверять `backend/app/alembic/versions/` на наличие `downgrade()` до rollback'а.

См. также `data-model.md` для того, какие миграции разрушительные.

---

## 10. Metrics/Grafana не видны

**Симптом:** Grafana показывает «No data» на панели. Prometheus вроде жив.

```bash
# SSH tunnel для доступа
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 root@45.14.244.140

# Потом в браузере: http://localhost:9090/targets
```

Возможные причины:

- Prometheus не может достучаться до `backend:8000/metrics` — `docker network` между двумя compose-проектами (`vpn` и `vpn-monitoring`) не связан.
  - **Fix:** В monitoring compose нужно `networks: external: name: vpn_default` (или проверить, что сеть `vpn_default` реально присоединена).
- `backend:/metrics` требует `X-Admin-Token` (см. `main.py:122-124`). Prometheus scrape должен передавать его. Проверить Prometheus config `bearer_token`/`authorization`.
- Scrape target возвращает 401 → проверить, что `ADMIN_API_TOKEN` в backend'е и в Prometheus scrape config совпадают.

---

## 11. Compose/stack hot-reload env-vars

**Симптом:** Изменили `.env` — изменения не применились к уже работающим контейнерам.

**Причина:** `docker compose up -d` без дополнительных флагов **не перезапустит** контейнер, у которого не изменился image/digest/volumes — env-file читается только при создании.

**Fix:**

```bash
docker compose up -d --force-recreate backend worker bot
# или только нужный сервис:
docker compose up -d --force-recreate worker
```

Handler `recreate app stack` в `deploy_app_stack` делает это автоматически, но когда правите `.env` руками — делать вручную.

**Помните:** worker и backend **разделяют** часть env (см. `infrastructure/deployment.md`, секция «Зеркалирование env»). Изменив `WARM_POOL_ENABLED` или `SUB_LINK_BASE_URL`, перезапустите **оба**.

---

## 12. Database migration applied partially

**Симптом:** Backend падает на старте с `column "x" does not exist` или `relation "y" does not exist`. Значит alembic head в БД не совпадает с ожиданиями кода.

```bash
docker compose exec backend alembic -c /app/alembic.ini current
docker compose exec backend alembic -c /app/alembic.ini heads
docker compose exec backend alembic -c /app/alembic.ini history | head -20
```

- Если `current` != `heads` — не применили все миграции. `docker compose exec backend alembic -c /app/alembic.ini upgrade head`.
- Если ошибка «multiple heads» — две ветки слились без merge-миграции. Это баг релиза; откатить код (см. сценарий 9), потом искать причину.

---

## Что делать, если ничего не помогает

1. **Снять снапшот:** `docker compose logs > /tmp/vpn-logs-$(date +%s).txt`, `pg_dump`, `redis-cli -a $REDIS_PASSWORD save`.
2. **Остановить**: `docker compose down` (не `-v` — данные сохранятся в named volumes).
3. **Сделать бэкап volume'ов** на всякий случай: `tar -czf /tmp/vpn_db_$(date +%s).tgz /var/lib/docker/volumes/vpn_db_data/`.
4. **Читать** логи и БД в спокойной обстановке, не под нагрузкой.
5. **Поднять** — `docker compose up -d`.

Не использовать `docker compose down -v` без явного намерения — `-v` сносит named volumes, включая БД.

## ⚠️ Неясные места

- **Backup-стратегии нет** в репо. Никакого `pg_dump` cron'а, никакого WAL-shipping'а. Любой incident recovery сценарий предполагает, что БД цела — если нет, восстанавливать неоткуда, кроме ручного последнего снапшота.
- **`audit_logs` и `health_probes` растут без retention.** Очистка — ручная операция, не зафиксирована в cron/timer.
- **RQ failed-jobs очередь не мониторится.** Упавший physical_revoke job останется в failed registry навсегда, если его не чистить вручную. Нет алерта, что `failed_job_registry.count > N`.
- **Sub_token invalidation при компрометации.** Нет документированного пути «я знаю, что у пользователя утёк sub_token, как его отозвать, не трогая подписку». Формально — `UPDATE subscriptions SET sub_token=NULL WHERE id=...`, но последствия (старый клиент перестанет получать конфиг) не документированы.
- **Нет отдельного staging.** Все тесты — напрямую в prod. Rollback описан, но «проверить на staging перед выкаткой» — не работает, staging-inventory нет.
