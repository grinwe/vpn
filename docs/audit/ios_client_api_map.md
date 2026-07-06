# Карта клиентского API VPN-сервиса (фундамент для iOS-клиента)

> Составлено 2026-07-05 read-only обходом кода. Все ссылки — `file:line` на момент ветки dev/fix.
> Назначение: зафиксировать реальную поверхность API и форматы конфигов, на которых строится нативный iOS-клиент.

Клиентская поверхность разбита на 4 канала, различающихся аутентификацией:

| Канал | Кто дёргает | Аутентификация | Файл |
|---|---|---|---|
| Sub-ссылка | любой VPN-клиент (Hiddify/v2rayNG/HAPP/будущий iOS) | сам `token` в URL (секрет) | `backend/app/api_extensions.py` |
| Webapp | Telegram Mini App (`webapp/src/api.ts`) | Telegram initData → короткоживущий Bearer-JWT | `backend/app/api_webapp.py` |
| Control-channel | кастомный клиент через CF Worker | `X-Control-Channel-Secret` (=`APP_SECRET_KEY`) + `X-Client-ID` (=`client_id_hmac`) | `backend/app/api/client_control.py` |
| Bot-прокси | Telegram-бот (от имени юзера) | shared admin-token (`require_admin`) | `client_control.py`, `api_extensions.py` |

**Ключевой вывод для iOS:** прямого клиент-аутентифицированного REST нет, кроме sub-ссылки. Всё «умное» (self-report, failover, per-device) сегодня проходит либо через Telegram initData (webapp), либо через bot/admin-token, либо через control-channel HMAC. Нативный iOS-клиент без бэкенд-изменений может использовать только (а) sub-ссылку и (б) control-channel `/api/client/report-failure`.

---

## 1. Эндпоинты

### 1a. Sub-ссылка (обращена к VPN-клиенту)

| Путь | Метод | Auth | Назначение | Ответ | file:line |
|---|---|---|---|---|---|
| `/api/sub/{token}` | GET | token в URL | Отдать конфиги устройства/подписки | `text/plain`, **base64(список URI через `\n`)** + спец-заголовки | `api_extensions.py:138` |

Логика (`dynamic_sub_link`, `api_extensions.py:138-318`):
- **Lookup order**: сначала `Device.sub_token` (per-device, `:153`), потом legacy `Subscription.sub_token` (`:264`).
- Гейты: sub `active` иначе `403` (`:156/:268`); не истёкшей иначе `403` (`:158/:271`); токен не найден → `404` (`:266`).
- **Alias на «живого соседа»** (seamless-migration, `:161-211`): если Device по токену уже не `active` или без активных creds — находит самый свежий (`updated_at` desc) активный sibling-device в той же подписке и отдаёт его creds. Инвариант: старый Device-row не удаляется, сохранённый URL юзера продолжает резолвиться.
- **Safety net**: если ни прямой device, ни alias не дали ни одного конфига → `503` (не пустой `200`), чтобы клиент не перезаписал локальный кэш пустотой (`:226-234/:289-297`).
- Тело: `base64.b64encode("\n".join(uris))` — стандартная base64-подписка; клиент декодирует и парсит построчно.

### 1b. Webapp (Telegram Mini App → `webapp/src/api.ts`)

Все под `require_webapp_user` (Bearer-JWT), кроме `/auth` и `/plans`.

| Путь | Метод | Auth | Назначение | Ответ (ключевое) | file:line |
|---|---|---|---|---|---|
| `/api/webapp/auth` | POST | нет (initData внутри) | Обмен initData на JWT | `{token, expires_in, user_id}` | `api_webapp.py:172` |
| `/api/webapp/me` | GET | JWT | Профиль+подписки+баланс+устройства | `MeResponse` | `api_webapp.py:430` |
| `/api/webapp/plans` | GET | нет | Список тарифов | `list[WebAppPlan]` | `api_webapp.py:536` |
| `/api/webapp/checkout` | POST | JWT | Инвойс на покупку/продление | `{invoice_id, provider, pay_url, amount, currency}` | `api_webapp.py:582` |
| `/api/webapp/invoices/{id}` | GET | JWT | Статус оплаты (поллинг) | `{status, subscription_id, subscription_active, has_credentials}` | `api_webapp.py:704` |
| `/api/webapp/topup` | POST | JWT | Пополнить баланс | `{invoice_id, pay_url, ...}` | `api_webapp.py:771` |
| `/api/webapp/trial/activate` | POST | JWT | Активировать триал | `{trial_amount_kopecks, ...}` | `api_webapp.py:866` |
| `/api/webapp/subscriptions/activate` | POST | JWT | Активировать за баланс | `ActivateResponse` (409 `insufficient_balance`) | `api_webapp.py:980` |
| `/api/webapp/subscriptions/{id}/devices` | POST | JWT | Добавить устройство (платный слот) | `{device_id, device_count, new_daily_cost_kopecks}` | `api_webapp.py:1166` |
| `/api/webapp/devices/{id}` | PATCH/DELETE | JWT | Переименовать / удалить | `{device_id, ...}` | `api_webapp.py:1627/1658` |
| `/api/webapp/subscriptions/{id}/freeze|unfreeze|cancel|auto_renew|change_plan` | POST | JWT | Управление подпиской | соответствующие Response | `api_webapp.py:1382/1416/1449/1312/1492` |
| `/api/webapp/transactions` | GET | JWT | История (`?limit&offset`) | `{items[], has_more}` | `api_webapp.py:1735` |
| `/api/webapp/referral` | GET | JWT | Реф.программа | `ReferralInfoResponse` | `api_webapp.py:1778` |
| `/api/webapp/health-ping-report` | POST | JWT | «VPN не работает» (whole-sub) | `HealthPingReportResponse` | `api_webapp.py:1856` |
| `/api/webapp/report-operator` | POST | JWT | Указать оператора | `{report_id, operator}` | `api_webapp.py:1947` |
| `/api/webapp/report-broken-device` | POST | JWT | «Это устройство не работает» | `HealthPingReportResponse` | `api_webapp.py:1972` |

### 1c. Control-channel / self-report (`client_control.py`)

| Путь | Метод | Auth | Назначение | Запрос | Ответ | file:line |
|---|---|---|---|---|---|---|
| `/api/client/report-failure` | POST | `X-Control-Channel-Secret` + `X-Client-ID` | Кастомный клиент сообщает о сбое → failover | `{kind, ts, current_node_id?, fail_count}` | `ReportFailureResponse` | `client_control.py:135` |
| `/api/admin/client-control/report-broken` | POST | admin (bot) | Юзер тапнул в боте «не работает» | `{telegram_id, operator?}` | `ReportBrokenResponse` | `client_control.py:638` |
| `/api/admin/client-control/devices-by-telegram` | GET | admin (bot) | Список живых устройств (пикер) | `?telegram_id` | `{devices:[{device_id,name,status}]}` | `client_control.py:781` |
| `/api/admin/client-control/report-broken-device` | POST | admin (bot) | Failover одного устройства | `{telegram_id, device_id, operator?}` | `ReportBrokenResponse` | `client_control.py:831` |
| `/api/admin/client-control/report-still-broken|report-ok|report-status/{id}` | POST/GET | admin | Жизненный цикл репорта | — | — | `client_control.py:969/989/1014` |

Ответ `/api/client/report-failure` (`client_control.py:98-129`): `{ok, retry_after_sec, target_node_id?, target_node_name?, task_id?, report_id?, action}`, где `action ∈ migrated|no_target_available|throttled|subscription_inactive`. Клиент обязан уважать `retry_after_sec` (экспоненциальный backoff).

---

## 2. Форматы протоколов (билдеры в `provisioning.py`)

Все vless-URI собираются **сырой конкатенацией** `"&".join(f"{k}={v}")` — значения НЕ urlencode'ятся, кроме `path` (через `urlquote`). Тег после `#` — `<proto>-<node.region>`. Критичная анти-РКН деталь: **`fp=firefox`** (uTLS ClientHello fingerprint), зашита в `_VLESS_UTLS_FP` (`provisioning.py:220`). **Требование к iOS-ядру: поддержка uTLS fingerprint=firefox** (chrome/safari/ios флагаются DPI РКН, июнь-2026).

### VLESS Reality — `_build_vless_reality_credential` (`provisioning.py:227-243`)
`encryption=none, security=reality, sni, pbk, sid, flow=xtls-rprx-vision, fp=firefox, type=tcp`
```
vless://<uuid>@<node.host>:<port>?encryption=none&security=reality&sni=www.microsoft.com&pbk=<pubkey>&sid=0123abcd&flow=xtls-rprx-vision&fp=firefox&type=tcp#reality-de
```

### VLESS XHTTP — `_build_vless_xhttp_credential` (`provisioning.py:291-314`)
`encryption=none, security=tls, sni, fp=firefox, type=xhttp, host, path(urlencoded), mode`
```
vless://<uuid>@sub123.wgse.info:443?encryption=none&security=tls&sni=sub123.wgse.info&fp=firefox&type=xhttp&host=sub123.wgse.info&path=%2Fxh&mode=auto#xhttp-de
```
Обходит 16KB-«штору» ТСПУ на сырых TLS-туннелях, мультиплексируя через обычные HTTP-запросы.

### VLESS WS CDN — `_build_vless_ws_cdn_credential` (`provisioning.py:246-269`)
`encryption=none, security=tls, sni, fp=firefox, type=ws, host, path(urlencoded)`
```
vless://<uuid>@sub123.wgse.info:443?encryption=none&security=tls&sni=sub123.wgse.info&fp=firefox&type=ws&host=sub123.wgse.info&path=%2Fws#ws-cdn-de
```
Домен — `*.wgse.info` DNS-only, указывает ПРЯМО на ноду (CF-проксирование WS «мертво»). `encryption=none` обязателен в URI.

### Hysteria2 — `_build_hysteria2_credential` (`provisioning.py:272-288`)
```
hy2://<password>@<node.host>:443?sni=sub123.wgse.info&obfs=salamander&obfs-password=<obfspw>#hy2-de
```
`obfs`/`obfs-password` — только если `settings.obfs` задан. `insecure` не проставляется (SNI-валидный серт).

### ShadowTLS+SS2022 — `_build_shadowtls_credential` (`provisioning.py:186-213`) → `ss://` (легаси, per-device идентичности нет).

Роутер по `cfg.protocol`: `provisioning.py:2659-2667` (provision), `:3065-3082` (re-render), `:3433-3441` (reprovision); rebuild — `:4192-4194`.

---

## 3. HTTP-заголовки sub-ответа (`_sub_response_headers`, `api_extensions.py:107-135`)

| Заголовок | Значение | Семантика |
|---|---|---|
| `profile-update-interval` | `"6"` | Через сколько ЧАСОВ перезапрашивать подписку |
| `profile-title` | `"V8-VPN"` | Имя профиля |
| `content-disposition` | `attachment; filename="V8-VPN"` | Имя файла профиля |
| `subscription-userinfo` | `expire=<unix_ts>` | **Только** срок истечения. **Нет `upload/download/total`** → клиент не узнаёт квоту/остаток трафика |
| `subscription-autoconnect` | `"true"` | Гейт `SUB_HAPP_AUTOCONNECT`. HAPP-автоконнект |
| `subscription-autoconnect-type` | `"lowestdelay"` | Тот же гейт. HAPP берёт ноду с лучшим пингом |
| `fallback-url` | `<SUB_LINK_FALLBACK_BASE_URL>/<token>` | Гейт env. Фейловер ИСТОЧНИКА сабы на запасной домен |

По коду (`:113-119`): бесшовного per-server failover у HAPP через плоскую сабу нет; `lowestdelay` — максимум, чисто server-side выбор из набора нод.

---

## 4. Diverse-sub / failover (мульти-нодовая выдача)

**Флаг `DIVERSE_SUB_NODES`** (env, дефолт `1` → диверс выключен, поведение как одно-нодовое).

`_maybe_attach_diverse` (`provisioning.py:2727-2871`): при флаге >1 дотягивает к device бандлы креденшелов с (N−1) дополнительных **разных** нод (гарантия distinct-нод; гео-разнесение по регионам — двухпассовый цикл). Берёт только «тёплые» бандлы из warm-pool. Идемпотентно, best-effort под SAVEPOINT — primary всегда цел.

**Что видит клиент**: в base64-сабе появляются URI нескольких нод (N×M эндпоинтов). Клиентский Auto/url-test/`lowestdelay` сам прыгает между нодами при деградации — **client-side failover по N×M сабе**, без обращения к бэкенду.

**select_target_node** (`failover.py:21`): active + `is_active` + не замьючена оператором + не текущая + не в exclude + в пуле плана; сортировка `health_score DESC NULLS LAST, RANDOM()`.

**`_do_failover`** (`client_control.py:389-546`): throttle 5 мин/sub; `migrate_subscription_to_free_node` (свободная healthy-нода + авто-бан старой через `NodeUserBan`, потолок `NODE_USER_BAN_MAX_PER_USER`=3); краудсорс-эскалация (порог `NODE_FAILURE_BAN_THRESHOLD`=4 репорта/60 мин → cooldown 2ч). `sub_token` сохраняется всегда.

---

## 5. Аутентификация клиента

- **Sub-token**: per-device `secrets.token_urlsafe(32)`; legacy per-sub `token_urlsafe(16)`. Бессрочный, стабилен через миграции. Единственный секрет sub-ссылки.
- **Control-channel `client_id_hmac`** (`security.py:95`): `HMAC-SHA256(APP_SECRET_KEY, sub_token)[:12]`. Клиент шлёт как `X-Client-ID`; секрет запроса `X-Control-Channel-Secret`=`APP_SECRET_KEY`.
- **Webapp initData → JWT** (`api_webapp.py:53-140`): стандартная Telegram-схема; TTL initData `WEBAPP_INIT_DATA_TTL_SECONDS` (дефолт 300 с), JWT TTL `WEBAPP_JWT_TTL_SECONDS` (дефолт 1800 с). На 401 — реавторизация по initData.

**MeResponse** (`api_webapp.py:284-293`): `user`, `subscriptions[]` (внутри `credentials:[{proto, config_text}]` — **plaintext-URI**, и `sub_token`), `balance`, `subscription_extras[]` (в т.ч. `devices:[{id,name,status,sub_token,created_at}]`), `sub_link_base_url`, `bot_username`.

---

## 6. Готовность к iOS-клиенту и дырки

**Готово (можно строить сразу):**
- Импорт профиля через base64-сабу `/api/sub/{token}` — стандартный формат, iOS-ядра (sing-box/Xray) едят напрямую. Есть seamless-migration alias и 503-safety.
- Все 4 протокола собираются в валидные URI, с `fp=firefox`. Требование к ядру: uTLS fingerprint=firefox и `flow=xtls-rprx-vision` для Reality.
- N×M диверс-саба → client-side failover (Auto/url-test) без бэкенд-вызовов.
- Активный отчёт о сбое: `/api/client/report-failure` (нужны `X-Control-Channel-Secret` и `X-Client-ID`=HMAC(sub_token)).

**Дырки / чего нет (потребует бэкенд-доработок):**
1. **Нет measurement/quality-report эндпоинта** — клиент не может отправить latency/throughput/packet-loss. Только бинарный «сломалось».
2. **Нет трафик-квоты в сабе** — `subscription-userinfo` несёт только `expire=`. iOS не покажет «осталось X ГБ».
3. **Нет клиент-аутентифицированного REST вне Telegram** — `/me`, планы, устройства требуют Telegram initData. Нативный кабинет без Telegram-обёртки требует новой auth-схемы.
4. **`/api/client/report-failure` не отдаёт новую сабу** — только `target_node_id/name` + `task_id`; клиент сам передёргивает sub-ссылку после `action=migrated` (миграция асинхронная).
5. **Control-channel требует shared `APP_SECRET_KEY`** — критичный секрет (тот же, что Fernet'ит БД). Зашивать в iOS-бинарь опасно; в проде подаётся через CF Worker. Для нативного iOS нужен отдельный per-client секрет/подпись — сейчас его нет.
6. **HAPP-autoconnect за env-гейтом** и без бесшовного per-server failover — «умное переключение» придётся реализовывать в iOS-клиенте поверх диверс-набора.
