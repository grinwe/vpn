# Client-driven control channel + failover roadmap

> Статус: **планирование**, ничего не реализовано. Дата: 2026-05-27.
> Будет обновляться по мере прогресса. Когда фаза закроется — отметить
> ссылками на коммиты.

## 1. Проблема

Фундаментальный chicken-and-egg для любого VPN-сервиса в РФ:

* Юзер подключается → RKN/провайдер блокирует TLS-handshake на RU jump-ноду
  (Reality DPI-fingerprinting, IP-block, SNI-block — варианты).
* Юзер пытается зайти в Telegram-бота → бот недоступен, потому что весь
  Telegram через `api.telegram.org` тоже зарезан.
* **Юзер не может маякнуть «не работает», не может получить новую sub-link**,
  не может переключиться на другую ноду. Тишина с обеих сторон.
* Backend этого юзера тоже не видит — `last_handshake_at` / health-monitor
  смотрят на серверную сторону, а **block может быть only-его-провайдером**,
  у остальных юзеров та же нода работает.

### Что отвергнуто и почему

| Вариант | Причина отказа |
|---|---|
| **MTProxy для Telegram** (роль `vpn-setup/roles/mtproto/` уже есть) | (1) Привычка: юзер получает «работающий Telegram», забывает что VPN-подписка нужна. (2) Демаскировка: весь Telegram-трафик (медиа/голос/файлы) идёт через резервный IP → RKN-сканеры ловят его по трафик-pattern'у за неделю. |
| **Email-канал** для уведомлений | Reactive only (юзер должен открыть почту), часть юзеров не оставляет email при покупке. Подойдёт **как secondary** для рассылок о migration окнах, но не как primary control-channel. |
| **WebApp на CDN-fronted домене** | Тот же block-risk что и основной VPN-домен. Не решает корневую проблему. |
| **iOS push-нотификации через APNs** | Однонаправленные (бэк→клиент), нам нужно client→бэк сигнал. |
| **Только статичный backup-endpoint list в подписке** (без custom клиента) | Hiddify/v2rayN не умеют автоматически шарить failover-сигналы — они просто пробуют next server in subscription. Нет signal'а оператору о проблеме у юзера, нет адаптивного reorder'инга. |

## 2. Решение (high-level)

**Custom клиент** (fork sing-box / Streisand) с **dedicated low-traffic
control-channel** через **Cloudflare Workers**, которые форвардят сигналы
на наш backend. Backend по сигналу auto-migrate'ит юзера на лучшую
доступную ноду (механизм `POST /subscriptions/{id}/migrate` уже работает).

### Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│ Android (sing-box fork) / iOS (Streisand fork)              │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │  VPN engine (Reality / xhttp / ws-cdn)  ← НЕ ТРОГАЕМ    │ │
│ │  Подключается к RU jump-нодам как обычно                 │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │  Control-channel client                                  │ │
│ │  - on connect_failed N раз / на ручную кнопку            │ │
│ │  - POST на CF Worker URL                                 │ │
│ │  - body ~150 байт: { hmac, kind, ts, current_node_id }   │ │
│ │  - получает new_sub_url → автоматически перечитывает     │ │
│ │    подписку → переподключается                           │ │
│ └─────────────────────────────────────────────────────────┘ │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTPS POST (TLS 1.3)
                     ▼
        ┌────────────────────────────────────┐
        │ Cloudflare Workers                  │
        │  - control-XXXX.workers.dev (rot.)  │
        │  - URL передаётся в подписочных     │
        │    данных, не публикуется           │
        │  - rate-limit per client_id_hmac    │
        │    через Durable Objects            │
        │  - форвардит на наш backend по      │
        │    shared secret (X-Control-Secret) │
        └─────────────┬──────────────────────┘
                      │ HTTPS POST (mgmt.grinwer-...)
                      ▼
        ┌────────────────────────────────────┐
        │ Backend: POST /api/client/report-   │
        │   failure                           │
        │  - validate X-Control-Secret        │
        │  - HMAC → resolve sub_token         │
        │  - pick target node                 │
        │    (health_score, NOT muted, NOT    │
        │    current)                         │
        │  - migrate_subscription(target)     │
        │  - audit_log + response with        │
        │    new_sub_url                      │
        └────────────────────────────────────┘
```

### Почему именно CF Workers

* CF в РФ **разрешён** (используется тысячами сайтов, RKN не блокирует всё CF).
* `*.workers.dev` сам по себе — generic-домен, не корреляруется с VPN-сервисами.
* Если RKN зарежет один Worker URL → rotation за минуты (несколько Worker'ов прошиты в подписочной URL, клиент перебирает).
* Низкий трафик: 1 POST ≈ 200 байт. CF Workers free tier: 100k req/day. Нашему MVP хватит за глаза.
* Шифрование mgmt-domain'а: клиент **не видит** наш backend URL, только Worker. Если кого-то reverse-engineering пугают APK — узнать наш backend всё равно нельзя.

### Почему НЕ свой домен

* Свой домен в одной AS = одна точка отказа на RKN-блок.
* CF — это глобальная anycast-сеть, RKN режет точечно а не CF целиком (по статистике 2024-2026).
* Управление через Cloudflare Dashboard или Wrangler CLI — деплой нового Worker'а 30 секунд.

## 3. Phases

### Phase A — Control-channel infra (~1 неделя, без клиента)

**Цель:** работающая control-channel инфра, тестируемая через curl, готовая принять клиента когда он будет.

| # | Что | Где | Trade-off / риск |
|---|---|---|---|
| A.1 | Backend endpoint `POST /api/client/report-failure` | `backend/app/api/client_control.py` (новый модуль) | — |
| A.2 | HMAC-based auth: `client_id = base64(HMAC(server_secret, sub_token)[:12])`. Backend ищет sub_token reverse через precomputed lookup table (в БД новая колонка `Device.client_id_hmac`, индексируемая) ИЛИ перебором по active devices (дорого, fine для V1). | `auth.py` helper + `models.py` colonка | Лучше колонка — O(1) lookup. Миграция 0037. |
| A.3 | Rate-limit: 5 reports / 30 min per `client_id` через slowapi или Redis. | `client_control.py` | Слишком жёстко = ложный fail юзера. 5/30min — компромисс. |
| A.4 | Target node selection: `services/failover.py:select_target_node(current_node_id) → VPNNode` — best healthy, NOT muted, NOT current. Учитывать pool_id если sub привязан к pool. | новый модуль или helper в `provisioning.py` | Простая логика для V1; ML-based ranking — потом. |
| A.5 | Trigger existing `migrate_subscription` flow → возвращает новый sub_url (он не меняется, тот же sub_token, но клиент перечитает контент). | re-use orchestrator | sub_token preserved (см. [[sublink_invariant]]). Юзер на старом URL получит новые credentials. |
| A.6 | Audit log: `action='client_reported_failure'`, extra `{symptom, current_node_id, target_node_id, task_id}` | `_audit` | — |
| A.7 | Response: `{ok, new_sub_url, target_node_id, task_id, retry_after}`. `retry_after` — клиент не дёргает повторно следующие N сек после успешного report. | endpoint | — |
| A.8 | CF Worker `control.js`: принимает POST, валидирует X-Control-Secret, форвардит на backend (origin URL — env secret в Worker'е). Также через CF Durable Objects можно поверх ещё один rate-limit на CDN-уровне. | `infra/cloudflare/control_worker/index.js` (новая директория) + `wrangler.toml` | Развёртывается через `wrangler deploy`, в репо коммитим только source. Production secrets через wrangler. |
| A.9 | Admin UI: на странице юзера `/users/<id>` добавить кнопку «🚨 Report failure for me» — оператор может симулировать сигнал от юзера. Помогает тестировать без клиента + полезно для саппорта (юзер написал в саппорт через 2й канал, оператор нажимает кнопку → юзер на следующем рефреше получает новую конфигурацию). | `admin/src/pages/Users.tsx` + новая API-функция в `api.ts` | UI-change через [[no_ui_changes_without_approval]] — подтвердить. |
| A.10 | Документация в `docs/operations/control_channel.md` (отдельно от этой roadmap'ы) — как работает, как поддерживать, как rotate'ить Worker'ы. | docs | [[docs_sync]] |
| A.11 | Smoke-тест: curl POST на Worker URL → видеть `client_reported_failure` в audit_log + migrated subscription. Без клиента. | manual via bash | — |

**Deliverable:** одной командой `curl -H "..." -d '...' https://control-XXXX.workers.dev/report` сигнал доходит до backend, юзер мигрируется на другую ноду, в audit видна запись. Готовая к интеграции с клиентом.

**Открытые вопросы Phase A:**

1. **Reverse lookup HMAC**: добавлять колонку `Device.client_id_hmac` или искать перебором? Колонка чище, миграция простая.
2. **Что считать «failure»** на стороне backend'а — клиент шлёт kind. Минимум: `connect_failed` (auto), `user_reported` (кнопка), `health_check_failed` (периодический ping не дошёл). Может ещё добавим — но это для V1.
3. **Один Worker или N с rotation?** Для V1 — 3 Worker'а, клиент перебирает random'ом. Если все 3 умерли — клиент пробует direct backend (на свой страх и риск, но это fallback).

---

### Phase B — Android клиент (~1-2 месяца, fork sing-box)

**Цель:** rabocha sublink в нашем custom-клиенте с встроенным control-channel.

Базовый кандидат — **sing-box** (https://github.com/SagerNet/sing-box) — это движок Hiddify, NekoBox, многих других. Open-source. Поддерживает Reality, xhttp, ws-cdn out of the box (все наши протоколы).

| # | Что | Trade-off / риск |
|---|---|---|
| B.1 | Развернуть dev environment: Android SDK, NDK, Gradle. Fork базового sing-box-android (`SagerNet/sing-box-for-android`). | Билд time `~3-5 мин на чистый клон. Дальше быстрее. |
| B.2 | Кастомизация UI: rebrand (название, иконка, splash), убрать «advanced» функции, оставить только «Connect / Disconnect / Status». Lock'нуть source profile = наша подписочная URL (нельзя добавить чужие конфиги, чтобы юзер не сломал). | UX-решение: lock vs open. Lock = меньше support, но less flexibility. Открыто оставить «import sub link» для power-юзеров. |
| B.3 | Embed control-channel URL: подписочная URL содержит **расширенные метаданные** beyond стандартного sub formata: `vless://...` plus extra JSON-block с `control_urls: [...]`, `client_id: <hmac>`, `report_after_fails: 3`. sing-box парсит свой формат, но extra-block мы добавим в обвязку. | Нужно патчить sub-link generator на backend'е (`/api/sub/{token}` endpoint), не ломая совместимость с обычными Hiddify-юзерами. Идея: для custom-клиента — отдельный sub-link variant через query-параметр `?client=v1`. |
| B.4 | Control-channel client: `report-failure` worker on connect_fail. Internal counter, после N подряд fail (default 3) — POST на CF Worker. | — |
| B.5 | «Не работает» кнопка в UI — manual trigger того же endpoint. | — |
| B.6 | Auto-refresh подписочной URL после успешного report: получили `new_sub_url` → перезаписали профиль → reconnect. | sing-box умеет hot-reload профиля, тестируем. |
| B.7 | Rate-limit на стороне клиента: не более 1 report per 5 мин (server тоже limit'ит, но клиент-side тоже надо). | — |
| B.8 | APK distribution: `https://grinwer-cdn.example/app/latest.apk` через CF + auto-update check от клиента. Возможно RuStore-публикация (там VPN-категория есть). Google Play через VPN. | RuStore модерация занимает 1-2 недели. Google Play требует VPN-developer-account, тоже сложно. |
| B.9 | Telemetry на клиенте: что вообще шлёт control-channel'у. Минимум: connect_fail count, current_node_id. **НЕ шлём**: список посещаемых сайтов, IP юзера (CF Worker и так логирует source IP — это уже privacy concern, но без него rate-limit невозможен). | Privacy policy надо обновить — это **отдельная задача**. |
| B.10 | CI: GitHub Actions билд APK на коммит, signing-keys в secrets. | — |

**Открытые вопросы Phase B:**

1. **sing-box vs NekoBox**: NekoBox — это UI-обвязка над sing-box, можно fork'нуть NekoBox чтобы получить готовый UI и заменить только бренд + control-channel. Экономия времени **~2-3 недели** vs greenfield. Лицензия: GPL-3.0 (нужно открыть наш fork — нюанс).
2. **Hiddify-Next** — ещё ближе, есть subscription-URL parsing из коробки. Тоже GPL-3.0.
3. **Lock-source profile или нет**: lock проще для support, открыто больше power-user friendly. Решение: lock by default + hidden настройка для power-юзеров.

---

### Phase C — iOS клиент (~2-3 месяца, optional / параллельно)

**Цель:** покрытие iOS-сегмента (минорный в РФ, ~20%, но влиятельный).

| # | Что | Trade-off / риск |
|---|---|---|
| C.1 | Fork **Streisand** (https://github.com/StreisandEffect/streisand-ios) или **Foxray** (https://github.com/yiguous/FoxRay). Оба open-source, поддерживают Reality. | Streisand устаревает; Foxray активнее развивается. |
| C.2 | iOS Developer Account ($99/год). | Юр.вопрос: на чьё имя оформить. |
| C.3 | Bundle ID + provisioning profile + signing keys. | — |
| C.4 | TestFlight setup: 10k testers cap, invites через email/Telegram. | Limit на 10k = достаточно для большинства РФ-юзеров, но не для бесконечного роста. |
| C.5 | App Store submission: review **может занять недели** + отказы. Apple исторически жёстко относится к VPN-приложениям из санкционных юрисдикций. | План B: остаться на TestFlight. |
| C.6 | Дальше — те же B.3-B.7 что и на Android (control-channel client logic). | — |

**Открытые вопросы Phase C:**

1. **AltStore / sideload** через PC — для опытных юзеров. Mass-market пойдёт через TestFlight.
2. **App Store рисков** vs стабильность TestFlight: возможно стартануть с TestFlight only, App Store откладываем.

---

## 4. Cross-cutting concerns

### 4.1. CF Worker rotation

* В подписочных данных храним **список** Worker URL'ов: `["control-1.workers.dev", "control-2.workers.dev", "control-3.workers.dev"]`.
* Клиент перебирает random'ом, retry на next при error.
* Если ВСЕ умерли — клиент пробует direct backend (хардкод? отдельный «emergency endpoint»? — TBD).
* Rotation Worker'ов: 1 раз в N месяцев (или при обнаружении блока) деплоим новый Worker, добавляем его в подписочные данные, удаляем самый старый через 60 дней.
* Worker'ы должны быть **stateless** (вся state в backend'е), чтобы новый Worker подхватывался без миграции.

### 4.2. Monitoring control-channel usage

* Метрика Prometheus: `vpn_client_reports_total{kind, result}`.
* Алерт: если за 5 минут >50 reports (что-то массовое сломалось).
* Dashboard: `по дням — какие kinds, success/fail rate, latency`.

### 4.3. Security: HMAC threat model

* `client_id = HMAC(server_secret, sub_token)[:12]` (12 байт = 96 бит, достаточно против brute force).
* Server secret НЕ хранится на клиенте — клиент уже получил предвычисленный `client_id` через подписочные данные при первом fetch'е `/api/sub/{token}`.
* Если злоумышленник украдёт `client_id` — он сможет дёргать `report-failure` от имени юзера → миграции спама в backend. Защита: rate-limit + manual override в admin UI (оператор может «mute reports» от конкретного client_id если abuse).
* Если злоумышленник украдёт `sub_token` целиком — он уже имеет доступ к credentials, control-channel это не делает хуже.

### 4.4. Privacy policy

* Control-channel шлёт минимум: `client_id_hmac`, `kind`, `ts`, `current_node_id`. **Не шлём** реальный IP юзера (Worker логирует source IP, но мы не сохраняем).
* Privacy policy в боте + на лендинге обновить под новые данные.
* GDPR не применим (РФ-юзеры), но best-practice — описать что собираем.

---

## 5. Что отложено

* **MTProxy раздача** — отвергнуто, см. §1 «Что отвергнуто».
* **Email-канал** — может пригодиться как secondary для рассылок («ваша подписка обновится 15 июня»), но не как primary. Откладываем.
* **Web-based status page** — после Phase A control-channel'а можем показывать «Сервис работает / Известны проблемы» на CDN-fronted странице. Дешёвый bonus, делаем после A.
* **ML-based target node selection** — сейчас простая логика «highest health_score». Когда у нас будут метрики кто реально с какой ноды коннектится — можно ranker'ить умнее.
* **iOS App Store submission** — TestFlight only до тех пор пока ситуация не стабилизируется.

---

## 6. Что не делаем никогда (anti-pattern)

* НЕ передаём через control-channel сам VPN-трафик (= MTProxy замаскированный). Это смерть.
* НЕ публикуем control-channel URL открыто (на сайте, в README, в Telegram). Только встроено в подписочные данные клиента.
* НЕ позволяем control-channel выполнять что-то beyond «migrate to other node». Удаление подписки, изменение баланса, изменение плана — только через основной auth-flow.

---

## 7. Tracker / status

| Phase | Status | Сессии |
|---|---|---|
| A — Control-channel infra | планирование | — |
| B — Android client (sing-box fork) | планирование | — |
| C — iOS client | планирование | — |

При завершении фазы — отметить здесь со ссылкой на коммиты.

---

## 8. Решения по open questions (приняты 2026-05-27)

1. **Domain rotation strategy**: ✅ **3 Worker'а сразу** в V1. Клиент перебирает random'ом, retry на следующий при error. Все три деплоятся одновременно через wrangler.
2. **HMAC reverse lookup**: ✅ **колонка `Device.client_id_hmac`** с index. Миграция 0037 — добавить колонку, backfill в той же миграции из существующих sub_token. Pre-вычисление при выдаче sub_token в orchestrator + при backfill.
3. **«Report failure» button в admin UI**: ✅ **сразу в Phase A** (шаг A.9). Тестирует control-channel без клиента + полезно саппорту до выпуска клиента.
4. **kind список для V1**: `connect_failed`, `user_reported`, `health_check_failed`. Достаточно. Расширим позже.
5. **CF Workers account**: ✅ **существующий** (тот же что используется для `grn-ssync.pro`). Нужен API token с правами Workers Scripts:Edit для deploy через wrangler. Production secrets (server_secret, backend_url) — через `wrangler secret put`.
