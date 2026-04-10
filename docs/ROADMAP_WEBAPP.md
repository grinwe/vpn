# Roadmap: Telegram WebApp + UX-апгрейд

## Контекст

Сейчас вся работа с пользователем — через бот-команды (`/plans`, `/status`, `/config`, `/renew`, `/referral`). Это «бот из 2019»: 15 команд, плоский список тарифов, никакой визуализации баланса, троублшутинг текстом без структуры. Конкуренты (hitvpn и им подобные) уже несколько лет как живут в формате **Telegram WebApp** — нативный вебвью внутри Telegram с React-страницей «Личный кабинет». Это качественный скачок UX, и без него мы всегда будем выглядеть как поделка.

Параллельно есть архитектурный долг: invoice-per-period биллинг плохо масштабируется на «добавь четвёртый телефон жене на полмесяца». Балансовая модель (списание `tariff/30 ₽/день` с активного устройства) решает это элегантно — но это **крупный** сдвиг, его делаем отдельным циклом после WebApp.

## Принципы

1. **WebApp — фронт, бот — нотификатор**. Всё, что сложнее одной кнопки, переезжает в WebApp. Бот остаётся для onboarding'а, push'ей и аварийного входа.
2. **Не ломаем то, что работает**. Существующие команды остаются alive весь переходный период. Переключение через feature-флаг.
3. **Без spam-механик**. Никаких принудительных подписок на канал, никаких free-trial без anti-fraud.
4. **Сначала UX-параллель, потом архитектурный сдвиг**. WebApp поверх invoice-биллинга → потом миграция на balance.

## Этапы

### 🚧 Этап 1 — WebApp scaffolding (этот спринт)

**Цель:** на `https://grinwer.online/app` отдаётся React-bundle, который Telegram умеет открывать как WebApp. Внутри — один экран «Мои подписки» с реальными данными из бэка. Auth через одноразовый токен в URL.

**Что делаем:**

1. **Frontend-проект `vpn/webapp/`** — Vite + React + Tailwind, отдельно от `admin/`. Тема Telegram (`tg.themeParams`), `tg.WebApp.expand()` в `useEffect`. Тёмная по умолчанию, под Telegram.
2. **Backend-эндпоинты для auth**:
   - `POST /api/webapp/auth` — принимает `initData` от Telegram WebApp, валидирует HMAC по `BOT_TOKEN` (стандартная схема Telegram), находит/создаёт `User` по `telegram_id`, возвращает короткоживущий JWT (30 мин)
   - `GET /api/webapp/me` — возвращает пользователя + список его подписок (требует JWT в `Authorization: Bearer ...`)
3. **Бот: команда `/start` и кнопка «Личный кабинет»** — генерируют ссылку `https://t.me/<bot_name>/app?startapp=...` (Telegram сам прокинет initData) ИЛИ `https://grinwer.online/app/?tgWebAppData=...`. Используем первое — стандартный путь.
4. **Nginx**: новый location `/app/` → отдаёт статику `vpn/webapp/dist/`. Источник правды по сборке — Docker multistage.
5. **Содержимое первого экрана:**
   - Шапка: `@username` + balance/expiry (пока: «Активна до DD.MM.YY» — баланс в этапе 4)
   - Список подписок: для каждой — план, нода/регион, статус, кнопка `Конфиг` (открывает QR + sub URL внутри WebApp)
   - Кнопка `Купить тариф` → переход на `/app/plans` (новый экран в этапе 2)

**Файлы, которые меняются:**
- `vpn/webapp/` — новый проект (package.json, vite.config.ts, src/)
- `vpn/backend/app/api_webapp.py` — новый router `/api/webapp/*`
- `vpn/backend/app/main.py` — подключить router
- `vpn/bot/handlers.py` — `/start` отдаёт inline-кнопку «🔐 Личный кабинет» с `web_app=WebAppInfo(url=...)`
- `vpn/infra/ansible/roles/deploy_web_frontend/templates/nginx-site.conf.j2` — location `/app/`
- `vpn/docker-compose.yml` — новый сервис или multistage внутри backend для билда webapp
- `vpn/.env.example` — `WEBAPP_BASE_URL`, `WEBAPP_JWT_SECRET`

**Acceptance:**
- [ ] Юзер шлёт `/start`, видит кнопку «🔐 Личный кабинет», тапает → открывается WebApp с тёмной темой
- [ ] WebApp показывает реальные данные (имя, активные подписки)
- [ ] Auth работает без логина/пароля — initData валидируется HMAC'ом
- [ ] Существующие команды бота продолжают работать
- [ ] JWT истекает за 30 мин, при истечении WebApp показывает «Откройте через бота заново»

---

### 🚧 Этап 2.75 — Admin UI hardening (текущий фокус)

**Цель:** закрыть последние «курлы в проде» для админа нод. Сейчас создание ноды и конфигов уже живёт в `admin/src/pages/Nodes.tsx`, но провижининг-таски видны только через API/логи, и нет кнопок на retry/delete — это упирается при первом же фейле ансибла.

**Что делаем:**

1. **Provisioning tasks page** — новая вкладка в admin UI (`/admin/tasks`). Таблица последних N `ProvisioningTask`: `id, target_type, target_id, action, status, created_at, finished_at`. Клик по строке разворачивает панель с `stdout`/`stderr`/`extra` (pre-formatted). Источник: `GET /api/provisioning/tasks?limit=50`.
2. **Retry failed tasks** — кнопка «↻ Повторить» в развёрнутой панели для задач со `status=failed`. `POST /api/provisioning/tasks/{id}/retry` — пере-enqueue'ит ту же таску (копия row с новым id, ссылкой на предыдущий через `extra.retry_of`). Идемпотентно — повторный клик на уже-enqueue'нную копию возвращает 409.
3. **Delete node** — кнопка «🗑 Удалить» в `Nodes.tsx` на строке ноды. Модалка с подтверждением + чек-бокс «убрать также с ансибла» (вызовет playbook в `state=absent` перед удалением row). Soft-fail: если на ноде ещё есть активные подписки — 409 с списком подписок, требует миграции перед удалением.

**Файлы:**
- `vpn/admin/src/pages/Tasks.tsx` — новая страница
- `vpn/admin/src/App.tsx` — роут `/tasks` + ссылка в навигации
- `vpn/admin/src/api.ts` — типы `ProvisioningTaskOut`
- `vpn/admin/src/pages/Nodes.tsx` — кнопка Delete + модалка
- `vpn/backend/app/api.py` — `POST /api/provisioning/tasks/{id}/retry`, `DELETE /api/nodes/{id}` (если ещё нет)

**Acceptance:**
- [ ] Админ видит последние таски и их stdout прямо в UI, без `docker compose logs`
- [ ] Failed task можно перезапустить одной кнопкой
- [ ] Ноду можно удалить из UI, с подтверждением и предупреждением об активных подписках

---

### Этап 2 — Покупка через WebApp (Telegram Stars)

**Цель:** довести цикл «открыл WebApp → выбрал тариф → оплатил → получил конфиг» до состояния «работает целиком, не выходя из Telegram». Без редиректов в браузер. YooKassa отложена (риск блокировки), но провайдерный слой пишем расширяемо, чтобы её добавить было одной задачей.

**Почему именно Stars сначала:**
- Нативный UX: `tg.openInvoice(slug)` открывает Stars-checkout поверх WebApp, юзер платит, окно закрывается, callback приходит с `paid` — никакого `return_url`, никаких редиректов, работает на iOS/Android/Desktop одинаково.
- Нет KYC, нет рисков платёжной блокировки.
- Минус: Stars ≠ рубли, у TG конская комиссия, аудитория уже. Поэтому это **первый**, а не **единственный**.

**Что делаем:**

1. **Backend — провайдерный слой:**
   - `POST /api/webapp/plans` (или `GET`) — список планов в формате, заточенном под карточки WebApp: `id, name, tier (Solo|Family|Pro), period (month|year), price_rub, price_stars, max_devices, badge`. Поле `badge` — `popular` для Family.
   - `POST /api/webapp/checkout` — `{plan_id, provider: "stars"}`. Создаёт `Invoice(status=pending)`, дёргает Bot API `createInvoiceLink` (currency `XTR`, prices в звёздах), сохраняет `provider_invoice_id` в Invoice, возвращает `{invoice_id, slug}`. Slug — это `https://t.me/$<hash>`, его фронт скармливает в `tg.openInvoice`.
   - `GET /api/webapp/invoices/{id}` — статус инвойса для поллинга после оплаты.
   - Расширение `models.Invoice` (если надо): поле `provider` (enum: `stars`, `yookassa`, `cryptobot`, `manual`), `provider_invoice_id` (string, nullable). Alembic-ревизия.

2. **Bot — обработчики Stars-платежа:**
   - `pre_checkout_query` хендлер: всегда `answer_pre_checkout_query(ok=True)` (валидацию мы уже сделали при `createInvoiceLink`). Оборачиваем в try/except, чтобы гарантированно ответить за 10 сек — иначе TG отменит платёж.
   - `successful_payment` хендлер: достаёт `invoice_payload` (туда зашит `invoice_id`), помечает `Invoice.status=paid`, создаёт Payment, вызывает `provision_subscription` (как сейчас в `/api/invoices/{id}/mark_paid`). Это переиспользование существующего пути, никакой дубликации.

3. **Frontend — WebApp:**
   - Экран `/app/plans`: карточки с `tier × period`, бейдж ⭐ Popular на Family, кнопка «🤔 Какой выбрать?» открывает bottom-sheet с короткой логикой (1 устр. → Solo, семья → Family, …).
   - Тап на «Купить» → `POST /api/webapp/checkout` → `tg.openInvoice(slug, callback)`.
   - Экран `/app/checkout/pending?invoice_id=N`: callback вернул `paid` → переход сюда, фронт поллит `GET /api/webapp/invoices/{id}` каждые 2 сек. Сначала ждёт `status=paid` (бот должен успеть обработать `successful_payment`), затем ждёт `subscription_id` ≠ null + полит `GET /api/webapp/me` пока не появится новая активная подписка с готовыми credentials. Это самый медленный шаг — Ansible 1–2 минуты — он же будет полностью убран в этапе 2.5 (см. ниже).
   - Экран показывает прогресс: «Оплата получена ✓ → Готовим конфиг… (примерно 1 мин) → Готово! [Открыть]».

4. **Существующий поток в боте остаётся.** Команды `/plans` + `/renew` живы — это fallback на случай если что-то поломается в WebApp.

**Файлы:**
- `vpn/backend/app/api_webapp.py` — новые ручки `/plans`, `/checkout`, `/invoices/{id}`
- `vpn/backend/app/services/payments/telegram_stars.py` — `create_stars_invoice_link(invoice, plan)` (Bot API client)
- `vpn/backend/app/models.py` + alembic — `provider`, `provider_invoice_id` на Invoice (если ещё нет)
- `vpn/bot/handlers.py` — `pre_checkout_query` + `successful_payment` хендлеры
- `vpn/webapp/src/pages/Plans.tsx`, `CheckoutPending.tsx` — новые экраны + роутер
- `vpn/webapp/src/api.ts` — клиенты для новых ручек

**Acceptance:**
- [ ] Юзер открывает WebApp → видит карточки тарифов с ⭐ на Family
- [ ] Тап на «Купить Solo» → нативный Stars-checkout → оплата → callback `paid` → экран ожидания → через ≤2 мин у юзера активная подписка с конфигом
- [ ] При отмене checkout'а WebApp возвращает на экран тарифов, Invoice остаётся pending (можно повторить или отдать в очистку cron'у)
- [ ] Старый `/plans` в боте по-прежнему работает
- [ ] `/api/webapp/checkout` отказывает, если у юзера уже есть `max_devices` активных подписок

---

### ✅ Этап 2.5 — Pre-warmed credentials pool (instant activation)

**Цель:** убрать 1–2-минутный «готовим конфиг» после оплаты. После этапа 2 покупка работает, но Ansible-латенция видна юзеру. После 2.5 — конфиг выдаётся за миллисекунды, потому что он уже лежит на ноде, ждёт ассайна.

⚠️ Это **отдельный PR**, не смешивать с этапом 2. Подсистема трогает credentials/provisioning, которые мы только что устаканили multi-protocol рефакторингом — два больших риска в одном диффе → нечитаемая отладка.

**Архитектура:**

1. **Schema**:
   - `Credential.subscription_id` → nullable (warm credential ещё не привязан к подписке)
   - Новый enum `CredentialPoolState`: `warm` (готов, не выдан) / `assigned` (выдан подписке) / `revoked` (отозван, ждёт удаления с ноды)
   - Колонки на `Credential`: `pool_state`, `warmed_at`, `assigned_at`
   - Индексы: `(node_id, pool_state) WHERE pool_state = 'warm'` — для быстрого поиска свободных

2. **Warmer worker** (новая периодическая таска в `worker.py`):
   - Каждые N секунд: для каждой активной ноды считаем `count(warm)`. Если меньше `WARM_POOL_TARGET` (по умолчанию 10) — enqueue'им `warm_credential(node_id)` × разница.
   - `warm_credential` task: генерит `username/uuid/password`, прогоняет ансибл в режиме `state=present`, сохраняет `Credential(subscription_id=NULL, pool_state=warm)` со всеми протоколами ноды.
   - Метрики: `vpn_warm_pool_depth{node}`, `vpn_warm_pool_misses_total`, `vpn_warm_credential_provision_seconds`.

3. **Атомарный assignment при покупке** ([provision_subscription](vpn/backend/app/services/provisioning.py)):
   - Если у выбранной ноды есть warm credential — `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` → `UPDATE pool_state='assigned', subscription_id=...` → готово, ансибл не дёргаем. Юзер получает конфиг мгновенно.
   - Если warm нет (промах пула) — fallback на старый путь (cold provisioning через ансибл) + инкремент `pool_misses_total`. Это говорит warmer'у поднять threshold.

4. **Revoke в две стадии**:
   - `unassign`: `pool_state=assigned → revoked`, ставим в очередь физического удаления
   - `physical_revoke` (worker): отдельная таска, прогоняет ансибл `state=absent`, удаляет Credential
   - Это гарантирует, что revoke не блокирует юзерский поток (например, в админке кнопка «revoke now» отдаёт мгновенный ответ)

5. **Конфликты с multi-protocol**: warm credential должен покрывать **все** включённые протоколы ноды (как и обычная Credential сейчас). Если на ноду включают новый протокол, warm-пул нужно прогнать: либо инвалидировать (drop + warm заново), либо догенерить недостающие протоколы для существующих warm'ов. Простой путь: при изменении `VPNConfig.is_enabled` — `DELETE FROM credentials WHERE node_id=? AND pool_state='warm'`, warmer догонит.

**Файлы:**
- `vpn/backend/app/models.py` + alembic ревизия — pool_state, nullable subscription_id
- `vpn/backend/app/services/warm_pool.py` — новый сервис warmer
- `vpn/backend/app/services/provisioning.py` — assignment-from-pool логика
- `vpn/backend/app/worker.py` — периодическая таска `run_warm_pool_check`
- `vpn/docs/dashboards/business-metrics.json` — pool depth panel
- `vpn/backend/tests/test_warm_pool.py` — atomic assignment race-condition тест (двое юзеров на одну warm credential — должен получить только один)

**Acceptance:**
- [x] Холодный старт: pool пустой → cron поднимает `WARM_POOL_TARGET` warm-creds на каждой ноде
- [x] Покупка с теплым пулом: тап «Купить» → подписка активна за <1 сек → конфиг сразу
- [x] Race test (pytest): 10 параллельных провижинингов на один warm cred → 1 успех + 9 fallback на cold
- [x] Pool depth panel в Grafana показывает живой график
- [x] Revoke не блокирует API: ответ < 100ms, фактический ансибл-`absent` едет в фоне

---

### Этап 3 — Self-service и троублшутер

**Цель:** убрать 80% support-тикетов, дать юзеру самому решить типовые проблемы.

- Экран `/app/devices`: список девайсов, удалить/добавить, перевыпустить config
- Экран `/app/help`: интерактивный троублшутер. «Не работает на Android» → чек-лист с галочками → если не помогло → form для жалобы (создаёт ticket в audit_log + нотификация админу)
- Видеоинструкции встроены контекстно в чек-листы, не отдельным разделом
- Реферальный CTA на главной: блок «Пригласи друга — получи 30 дней» с кнопкой «Поделиться» (нативный TG share)

---

### Этап 4 — Балансовая модель биллинга (отдельный цикл)

**Цель:** перейти с invoice-per-period на pay-as-you-go.

⚠️ Это крупный архитектурный сдвиг, делается **после** этапов 1-3 и **отдельной веткой**, не смешивая с WebApp работой.

- Новая таблица `User.balance_kopecks` (Numeric, в копейках чтобы не трахаться с float)
- `BalanceTransaction` — журнал пополнений/списаний (audit-trail)
- Cron `daily_billing()` каждые 24h — для каждого активного device списывает `plan.daily_rate_kopecks`. Если баланс ≤ 0 → revoke
- Pricing meaning меняется: «Solo 150₽/мес» → «5₽/день/устройство»; UI показывает «100₽ хватит на ≈20 дней»
- Платёжные провайдеры теперь пополняют баланс, а не оплачивают конкретный invoice
- Миграция существующих подписок: для каждой активной → `balance += remaining_days * daily_rate`, status переходит в новую модель
- Старая `Invoice/Subscription` инфраструктура остаётся для истории, но новые покупки идут через balance

**Этот этап планируем отдельным документом, когда дойдём.**

---

## Out of scope

- Спам-механики (forced channel sub, free trials без anti-fraud)
- Видеоинструкции отдельным меню (только встроено в троублшутер)
- Линейный pricing «100₽/устройство» — обсудим после этапа 4, привязано к balance-модели
- Native iOS/Android приложения — Telegram WebApp покрывает обе платформы

## Verification (общая)

После каждого этапа:
1. `pytest backend/tests/` зелёное
2. Смок на staging: вход через бот → WebApp открывается → действия работают → возврат в чат → бот всё ещё в строю
3. Старые юзеры (без WebApp) продолжают пользоваться через бот-команды
