# Admin-claim для orphan-credentials (post-incident recovery)

Спецификация админ-эндпоинта, которым оператор поддержки привязывает
warm-pool credential к реальному юзеру, после того как тот написал
«я был юзером, у меня не работает после ваших обновлений».

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

В каждой:
* `access_username` — `warm-<node_id>-<hex>` (то же что в xray.clients[])
* `config_text` — полноценный VLESS URL с реальным UUID, который юзер
  сейчас использует в Hiddify
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

Только `require_admin`. Принимает JSON:

```json
{
  "user_id": 9,                                    // ИЛИ telegram_id ниже
  "telegram_id": "1678661092",                     // одно из двух обязательно
  "uuid": "abcdef12-3456-...",                     // из vless://UUID@... юзера
  "plan_id": 1,                                    // по умолчанию = Solo
  "expires_at": "2026-06-19T00:00:00Z",            // default = NOW + plan.duration_days
  "device_name": "primary"                         // default = "primary"
}
```

Ответ:
```json
{
  "subscription_id": 100,
  "device_id": 12,
  "claimed_credentials": [
    {"id": 5,  "proto": "vless-reality"},
    {"id": 6,  "proto": "vless-xhttp"}
  ]
}
```

## Внутренняя логика

Subscription+Device+Credentials уже **существуют** на placeholder-юзера —
admin-claim просто **TRANSFER'ит** ownership, не пересоздаёт. Это
важно, потому что:
* xray.clients[] хранит UUID = backend не должен дёргать `manage_vless_user.sh add`
* `sub_token` уже есть → юзер сразу видит подписку в боте без переоформления

```python
@router.post("/admin/claim-orphan")
def claim_orphan(body: ClaimRequest, db: Session = Depends(get_db),
                 admin = Depends(require_admin)):
    # 1. Найти юзера-таргета
    user = _resolve_user(db, body.user_id, body.telegram_id)
    if user is None:
        raise HTTPException(404, "User not found")

    # 2. Найти orphan-subscription по UUID, искомому в config_text
    #    кредов. UUID встречается в URL `vless://UUID@...`. Все creds
    #    этого bundle привязаны к одной Subscription.
    cred = (
        db.query(models.Credential)
        .filter(
            models.Credential.config_text.like(f"%{body.uuid}%"),
            models.Subscription.user_id == ORPHAN_OWNER_ID,  # 999999
        )
        .join(models.Subscription, models.Credential.subscription_id == models.Subscription.id)
        .first()
    )
    if cred is None:
        raise HTTPException(404, "Orphan with this UUID not found")
    sub = cred.subscription
    device = cred.device

    # 3. TRANSFER ownership на real-юзера. sub_id/device_id/credential_id
    #    не меняются. Перенос expires_at — пересчитываем от сегодня.
    plan = db.query(models.Plan).get(body.plan_id or sub.plan_id or 1)
    new_expires = body.expires_at or utcnow() + timedelta(days=plan.duration_days)

    sub.user_id = user.id
    sub.plan_id = plan.id
    sub.expires_at = new_expires
    sub.notes = (sub.notes or "") + f" | claimed by admin {admin.actor} @ {utcnow().isoformat()}"
    device.user_id = user.id
    if body.device_name:
        device.name = body.device_name
    db.add(sub)
    db.add(device)
    db.commit()

    _audit(db, admin.actor, "orphan_claimed", "subscription", sub.id,
           extra={"uuid": body.uuid, "to_user_id": user.id, "old_expires": str(sub.expires_at)})

    return {
        "subscription_id": sub.id,
        "device_id": device.id,
        "old_user_id": ORPHAN_OWNER_ID,
        "new_user_id": user.id,
        "new_expires_at": new_expires.isoformat(),
    }
```

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

В первой версии endpoint'а это **не** делаем автоматически — пусть оператор
сам решает, не хочется ловить негативные балансы при недостаче.

## Admin-UI

В админке `/users/<id>` добавить кнопку «Восстановить orphan-подписку»,
открывает модал с полями: UUID, Plan, Expires_at. После успеха — рефреш
страницы юзера.

В `/orphan-credentials` страница: таблица всех `Credential WHERE
subscription_id IS NULL AND pool_state = 'assigned'`. По строке — кнопка
«claim» с автозаполненным UUID, оператор указывает только юзера.

## Когда удалить эту страницу

После того как все 15 orphan-credentials распределены или удалены вручную.
Скорее всего за 30-60 дней после инцидента все, кто реально пользуется,
напишут в поддержку. Остаток можно удалить:
```sql
DELETE FROM credentials WHERE pool_state = 'assigned' AND subscription_id IS NULL;
```
А заодно `manage_vless_user.sh delete-user warm-<N>-<hex>` на ноде, чтобы
xray вычистил clients[].
