# Документация VPN-сервиса

Это навигационный индекс по структурированной документации, живущей в `docs/` (не путать со старыми одноразовыми файлами типа `BILLING_V2.md`, `ROADMAP_*.md` — те выше в каталоге и тематически узкие). Структура здесь — ответ на вопрос «я новый разработчик, куда смотреть, чтобы понять, как это работает».

## Для кого это

Документы в `architecture.md`, `data-model.md`, `components/*`, `infrastructure/*`, `operations/*` — для **нового разработчика**, который уже умеет Python/FastAPI/Ansible/Docker, но ещё не видел этот проект. Цель — **«как и почему»**, не «полный API-reference». Формат — Russian, код цитируется короткими фрагментами с ссылками `file/path.py:line`.

Если нужен reference-формат (сигнатуры всех endpoint'ов, полный список метрик и т.д.), смотрите код — он авторитетен. Эта документация **намеренно** не дублирует код, а объясняет его.

## С чего начать

Зависит от того, что вы делаете:

- **Просто разбираюсь** → `architecture.md` → `data-model.md` → `components/backend-api.md`.
- **Буду писать код в backend'е** → `components/backend-api.md` + соответствующий `components/<фича>.md`.
- **Буду трогать провижининг/ansible** → `components/provisioning.md` → `infrastructure/ansible.md` → `infrastructure/nodes.md`.
- **Буду деплоить / дежурить** → `infrastructure/deployment.md` → `operations/env-reference.md` → `operations/runbook.md`.
- **Хочу понять payments** → `components/payments.md` (после `components/backend-api.md`).

## Карта документов

### Верхний уровень

- **[architecture.md](architecture.md)** — высокоуровневая топология: кто с кем разговаривает, что поверх чего. Единственный файл с «большой картиной».
- **[data-model.md](data-model.md)** — сущности БД, ключевые таблицы и FK, инварианты. Почему `User`, `Subscription`, `Device`, `Credential`, `Invoice`, `Payment` устроены именно так.
- **[unclear-points.md](unclear-points.md)** — агрегированный срез всех `⚠️ Неясные места` из 13 документов в одном файле. Удобно для «что в системе непонятно/странно», без переключения между разделами. Производен от per-file `⚠️` секций.

### `components/` — как устроены процессы

- **[backend-api.md](components/backend-api.md)** — FastAPI-приложение: три роутера в одном процессе (api/webapp/ext), auth-модели (X-Admin-Token + scoped tokens + WebApp JWT), audit logging, rate limiting.
- **[bot.md](components/bot.md)** — aiogram long-poll бот: handlers, Stars-форвард, X-Admin-Actor trust model, FSM support-тикеты, notification poller.
- **[worker.md](components/worker.md)** — RQ-воркер: self-rescheduling cron-ticks (renewal, balance, warm-pool, autoscale, drain), провижининг-jobs, concurrency caps.
- **[warm-pool.md](components/warm-pool.md)** — warm credential pool: почему ansible заранее, anchor-lock `FOR UPDATE SKIP LOCKED` pattern, two-stage revoke, invariants.
- **[payments.md](components/payments.md)** — интерфейс `Provider`, провайдеры (CryptoBot, Telegram Stars, Generic SBP), rotation, webhook'и, `_mark_invoice_paid_core` three-way branch.
- **[provisioning.md](components/provisioning.md)** — ProvisioningOrchestrator: `choose_node`, credential builders, ansible execution, task outcome handling, warm-pool hot path, migrate/revoke.

### `infrastructure/` — как это живёт на хостах

- **[ansible.md](infrastructure/ansible.md)** — `ansible.cfg`, inventory (статика + dynamic через temp-file), `site.yml`, protocol-роли и их guard-pattern (`meta: end_role` when variable missing), `provision_device.yml`, backend↔ansible интеграционная таблица.
- **[deployment.md](infrastructure/deployment.md)** — single-host топология control-plane, docker-compose стек (7 сервисов), host nginx + Cloudflare + Let's Encrypt, `deploy_app_stack` роль пошагово, мирроринг env-vars backend↔worker.
- **[nodes.md](infrastructure/nodes.md)** — `VPNNode` и её колонки, lifecycle (`registering → active → draining → disabled` + `error`), health/cooldown, две архитектуры трафика (standalone exit vs relay jump→exit), autoscale pool math.

### `operations/` — как дежурить

- **[env-reference.md](operations/env-reference.md)** — полный справочник env-переменных, кто читает (backend/worker/bot), что значит дефолт, какие обязательные, какие зеркалятся между сервисами.
- **[runbook.md](operations/runbook.md)** — 12 сценариев «сломалось → что делать»: crash-loop, зависший provisioning, ansible fail на ноде, warm-pool пустой, webhook не приходит, disk grow, полный rollback, и т.д.

## Как читать и обновлять

### Соглашения

- **Ссылки на код** — формат `file/path.py:142`. Всегда с абсолютным путём от корня репо.
- **ASCII-диаграммы** — никаких mermaid / graphviz. Причина: читаются в любом viewer'е, редактируются in-place, diff'аются git'ом.
- **Неопределённость фиксируется явно.** Каждый документ имеет раздел `⚠️ Неясные места` в конце, куда собирается «я увидел это в коде, но не понял **почему именно так** — read это с осторожностью». Не догадки, не предложения «надо переделать». Только честное «код говорит X, намерение неясно».
- **Audit-ссылки** — италиком `> ⚠️ См. audit/...`. Детали security/performance/reliability-находок живут в отдельной audit-документации, которую эта документация **не** дублирует.
- **Нет cross-file repetition.** Если тема «X» подробно описана в `components/warm-pool.md`, другие файлы делают ссылку, не перекопируют.

### Когда обновлять

- Меняете **поведение** кода → обновляйте соответствующий `components/*.md` или `infrastructure/*.md`. Строковые правки OK, переписывание раздела — тоже OK.
- Меняете **env/config** → `operations/env-reference.md` + `.env.example` (они должны совпадать).
- Нашли новое «неясное место» → добавляйте в `⚠️` раздел **того** файла, где оно живёт.
- Добавляете новый компонент / фичу → новый `components/<name>.md` по паттерну существующих.

Что **не** нужно делать:

- **Не** переносите тикет-tracking сюда (это не TODO-доска).
- **Не** пишите «рекомендуется делать X» — документация описывает **то, что есть**, не **то, что хотелось бы**. Для roadmap'а — отдельные файлы уровнем выше (`ROADMAP_*.md`).
- **Не** копируйте большие блоки кода. 3–10 строк цитаты с ссылкой ок; длинные функции читаются в коде.
- **Не** дублируйте то, что уже в `CLAUDE.md` или в комментариях кода.

## Текущее состояние документации

На момент последней реорганизации:

```
docs/
├── README.md                     (этот файл)
├── architecture.md               (220 строк)
├── data-model.md                 (400 строк)
├── components/
│   ├── backend-api.md            (218)
│   ├── bot.md                    (203)
│   ├── worker.md                 (194)
│   ├── warm-pool.md              (207)
│   ├── payments.md               (276)
│   └── provisioning.md           (287)
├── infrastructure/
│   ├── ansible.md                (318)
│   ├── deployment.md             (314)
│   └── nodes.md                  (289)
└── operations/
    ├── runbook.md                (387)
    └── env-reference.md          (212)
```

Итого: 13 файлов, ~3.5к строк. Каждый файл в диапазоне 200–400 строк — читается за один сеанс.

## Старые файлы рядом (не часть этой иерархии)

В каталоге `docs/` выше есть файлы, которые **не** являются частью этой структурированной иерархии. Они остались от более ранних этапов разработки и тематически узкие:

- `ADMIN_UI.md`, `WEBAPP_REFERENCE.md`, `BALANCE_REFERENCE.md` — refrence-формат, скорее «список endpoint'ов»;
- `BILLING_V2.md`, `TRIAL_SYSTEM.md`, `PLAN_BALANCE_BILLING.md` — описания конкретных фич в историческом разрезе;
- `ROADMAP_MVP.md`, `ROADMAP_WEBAPP.md` — планы, не состояние;
- `ANALYSIS.md`, `AUDIT_STAGE_2_5.md` — аналитические срезы;
- `DEPLOY.md`, `NODES.md` — более старые версии того, что теперь живёт в `infrastructure/deployment.md` и `infrastructure/nodes.md` (удалять не стали до ревью).

Приоритет у **этой** структурированной иерархии. Если данные расходятся — код > структурированный `docs/` > старые файлы.
