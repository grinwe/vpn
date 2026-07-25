# Admin-claim для orphan-credentials (post-incident recovery)

Описание админ-эндпоинта, которым оператор поддержки привязывает
warm-pool credential к реальному юзеру, после того как тот написал
«я был юзером, у меня не работает после ваших обновлений».

> **Статус v1 (реализована):** backend-эндпоинт + UI-форма в Users.tsx.
> Не реализованы: отдельная страница `/orphan-credentials`, авто-Telegram
> уведомление юзеру, авто-compensation за дни бесплатного пользования.
> См. § «Что НЕ вошло в v1» в конце документа.

## Контекст

После disaster-recovery 2026-05-19 в БД лежат 15 «orphan»-подписок, привязанных
к placeholder-юзеру (`user_id=999999`, `telegram_id='__recovery_orphans__'`).
Каждая = Subscription + Device + N Credential rows, expires_at = NOW + 30 дней.

```sql
-- Список всех orphan'ов:
SELECT s.id AS sub_id, s.node_id, s.expires_at, d.access_username, c.proto,
       c.config_text AS vless_url
FROM subscriptions s
JOIN devices d ON d.subscription_id = s.id
JOIN credentials c ON c.subscription_id = s.id
WHERE s.user_id = 999999
ORDER BY s.node_id, d.access_username;
```

> ⚠️ С 2026-07-25 `config_text` в этих выборках отдаёт **шифртекст**
> `enc:v1:…` (дошифровка легаси-секретов, `scripts/encrypt_legacy_secrets.py`).
> Чтобы увидеть саму ссылку, расшифруй значение бэкендом, а не глазами:
> `docker compose exec -T backend python -c "from app.security import decrypt; print(decrypt('enc:v1:...'))"`.
> Сам эндпоинт claim-orphan расшифровывает креды сам — ему шифртекст не мешает.

В каждой:
* `access_username` — `warm-<node_id>-<hex>` (то же что в xray.clients[])
* `config_text` — полноценный VLESS URL с реальным UUID, который юзер
  сейчас использует в Hiddify (в БД — под шифрованием, см. врезку выше)
* `subscription.user_id = 999999` (placeholder), `subscription.expires_at`
  через 30 дней — после чего backend revoke'нёт девайс автоматически.

## User flow

1. Юзер пишет в поддержку: «не вижу свою подписку в боте».
2. Оператор: «пришли свою vless:// ссылку из Hiddify».
3. Юзер шлёт `vless://<uuid>@host:port?...`.
4. Оператор копирует UUID из ссылки в админ-форму, выбирает плательщика
   (по `telegram_id`), нажимает «Восстановить».
5. Backend создаёт Subscription, Device, привязывает Credential, юзер
   видит подписку в боте/webapp. Существующее подключение в Hiddify
   продолжает работать без переустановки.

## API: POST /api/admin/claim-orphan

Реализация: [backend/app/api/admin_claim.py](../../backend/app/api/admin_claim.py).
Только `require_admin` (заголовок `X-Admin-Token`). Опциональный
`X-Admin-Actor` пишется в `audit_log.actor`. Принимает JSON:

```json
{
  "user_id": 9,                                    // ИЛИ telegram_id ниже
  "telegram_id": "1678661092",                     // одно из двух обязательно
  "uuid": "vless://abcdef12-...@host:443?...",     // bare UUID ИЛИ полный vless:// URL
  "plan_id": 1,                                    // дефолт = текущий plan_id подписки
  "expires_at": "2026-06-19T00:00:00Z",            // default = NOW + plan.duration_days
  "device_name": "iPhone мамы"                     // дефолт = текущее имя девайса (не переименовываем)
}
```

Поле `uuid` принимает либо чистый UUID, либо полную `vless://UUID@host:port?...`
строку — backend выпарсивает UUID через
[`extract_uuid_from_vless_url`](../../backend/app/services/vless.py).
Юзер обычно копипастит готовую ссылку из Hiddify, оператор просто вставляет
в форму без ручной обработки.

Ответ:
```json
{
  "subscription_id": 100,
  "device_id": 12,
  "old_user_id": 999999,
  "new_user_id": 9,
  "new_expires_at": "2026-06-19T00:00:00Z",
  "claimed_credentials": [
    {"id": 5, "proto": "vless-reality"},
    {"id": 6, "proto": "vless-xhttp"}
  ]
}
```

`device_id` — id «primary» девайса (минимальный из всех девайсов сабки).
На multi-device подписках все девайсы получают `user_id = new_user_id`
в одной транзакции, но в ответе фиксируется один stable handle.

## Внутренняя логика

Subscription+Device+Credentials уже **существуют** на placeholder-юзера —
admin-claim просто **TRANSFER'ит** ownership, не пересоздаёт. Это
важно, потому что:
* `xray.clients[]` хранит UUID → backend не должен дёргать `manage_vless_user.sh add`
* `sub_token` уже есть → юзер сразу видит подписку в боте без переоформления
* Sub-link invariant (`/api/sub/{token}` оставляет revoked Device rows
  и алиасит к живому sibling'у) сохраняется — мы не трогаем токены.

Алгоритм (реальный код: `backend/app/api/admin_claim.py:claim_orphan`):

1. Распарсить UUID из payload через `extract_uuid_from_vless_url`. Помимо
   ошибки парсинга, отказываем если target user — сам placeholder (999999).
2. Найти все `Credential`, привязанные к подписке (INNER JOIN на
   `subscriptions` — warm-пул с `subscription_id IS NULL` пропускаем),
   расшифровать `config_text` и оставить те, где UUID встречается подстрокой.
   Сгруппировать по `subscription_id`, ожидаем ровно один (409 на multiple
   matches, 404 на none). **Почему не SQL `ILIKE`:** с 2026-07-25 колонка
   `config_text` хранит шифртекст `enc:v1:…` (дошифровка легаси-секретов), и
   SQL-подстрока не находила НИЧЕГО — эндпоинт отдавал 404 всем сиротам.
   Питоновский матчинг работает и с шифртекстом, и с плейнтекстом (его снова
   насыпает DR-восстановление `generate_restore_sql.py`). Если ни один кред не
   расшифровался, 404 явно указывает на `APP_SECRET_KEY`.
3. Проверить инвариант: `subscription.user_id == ORPHAN_OWNER_ID` (409
   иначе — либо уже claim'нута, либо вообще не orphan).
4. Подтянуть **все** sibling devices/credentials этой Subscription —
   перенос атомарен. На multi-device подписках (см. POSTMORTEM §3.3.4)
   каждый Device получает `user_id = new_user_id` в той же транзакции.
5. Резолвить план (по умолчанию — текущий `sub.plan_id`) и
   `expires_at` (по умолчанию `utcnow() + plan.duration_days`).
6. `UPDATE` на одном `db.commit()`: `sub.user_id`, `sub.plan_id`,
   `sub.expires_at`, `sub.notes` (append-only audit line), `d.user_id`
   на каждом девайсе, опционально `d.name`. Плюс `AuditLog` row с
   `action='orphan_claimed'` и `extra={uuid, old_user_id, new_user_id,
   plan_id, credential_ids, device_ids}`.

Insert-free transfer на уровне 2-3 `UPDATE`'ов в одной транзакции — ни
ansible-run, ни `sub_token` mutation, ни перевыдачи UUID не происходит.

## Что про баланс

Юзер с положительным balance_kopecks (например user_id=6 с 99k ₽) пользовался
подпиской БЕСПЛАТНО с момента инцидента, потому что backend не знал про его
подписку и не списывал ежедневный rate. При claim'е стоит ретроактивно списать:

```python
days_used = (utcnow() - INCIDENT_DATE).days
daily_rate = plan.daily_rate_kopecks or (plan.price * 100 / plan.duration_days)
to_charge = daily_rate * days_used
# user.balance_kopecks -= to_charge   ← опционально, по решению оператора
```

В v1 endpoint'а это **не** делается автоматически — пусть оператор
сам решает, не хочется ловить негативные балансы при недостаче.

## Admin-UI

Реализована inline-форма в detail-sidebar страницы `/users`
(`admin/src/pages/Users.tsx`, секция «Восстановить orphan-подписку»),
сразу под блоком «Пополнить баланс». Поля:

* **textarea** — UUID или полная `vless://…` ссылка от юзера (обязательно)
* **input** — имя устройства (опционально, по умолчанию не переименовываем)
* кнопка **«Восстановить»** — confirm dialog → POST `/admin/claim-orphan`
  → alert с `subscription_id`, `device_id`, протоколами и новым `expires_at`
  → invalidate `user-subs` и `users` queries

Plan и Expires_at в UI не выставляются — операторы в реальном flow всегда
берут дефолт (текущий план подписки + NOW + plan.duration_days). Если
понадобится — добавить поля в форму, бэк уже принимает оба override'а.

Отдельная страница `/orphan-credentials` (список всех placeholder-подписок)
в v1 не сделана — flow «юзер прислал свою vless-ссылку» работает и без
обзора пула. Запросная sql:
```sql
SELECT s.id, s.node_id, s.expires_at, d.access_username, c.proto, c.config_text
FROM subscriptions s
JOIN devices d ON d.subscription_id = s.id
JOIN credentials c ON c.subscription_id = s.id
WHERE s.user_id = 999999
ORDER BY s.node_id, d.access_username;
```

## Что НЕ вошло в v1

Сознательно отложено из MVP. По мере необходимости — добавлять отдельными
PR.

1. **Отдельная страница `/orphan-credentials`** — таблица всех Subscription
   `WHERE user_id = 999999` с превью VLESS-URL первого credential'а. Полезно
   если оператор хочет видеть, что вообще ещё не разобрано. Real-world flow
   («юзер прислал свою ссылку, оператор копипастит») работает без этого.
2. **Telegram-нотификация юзеру** после успешного claim'а — оператор сейчас
   копипастит вручную. Можно добавить опциональный флаг `notify=true` в
   body эндпоинта, тогда бэк через bot API отправит юзеру «Восстановили,
   держи ссылку: …».
3. **Авто-charge за прошедшие дни** (compensation за бесплатное
   пользование с инцидента). Описано в § «Что про баланс» — на дату
   реализации (2026-05-26) компенсация решена не в этом эндпоинте.
4. **Парсер диапазона UUID** или batch-режим — если юзер прислал две
   ссылки (по одной на каждое из двух устройств), оператор сейчас зовёт
   эндпоинт дважды. На объёмах ≤15 это терпимо.

## Когда удалить эту фичу

После того как все 15 orphan-credentials распределены или удалены вручную.
Скорее всего за 30-60 дней после инцидента все, кто реально пользуется,
напишут в поддержку. Остаток можно удалить:
```sql
DELETE FROM credentials WHERE pool_state = 'assigned' AND subscription_id IS NULL;
```
А заодно `manage_vless_user.sh delete-user warm-<N>-<hex>` на ноде, чтобы
xray вычистил `clients[]`.

Сам эндпоинт можно оставить как штатную «admin transfer subscription»
утилиту — пригодится для миграций между аккаунтами, семейного шеринга
и пр., где нужно перевесить ownership без пересоздания.
