# Эпик: безопасность ops-агента (`/ops` → план → исполнение)

**Цель.** Довести `/ops` (NL-команда оператора → агент-планировщик → в перспективе
исполнение) до состояния, в котором ему можно доверить трату денег и операции над
живой инфрой. Сейчас это **dry-run-планировщик** (Phase 2): агент через read-only
fleet-тулы строит план, но НИЧЕГО не исполняет. Опасность появляется в Phase 3
(исполнение «по одному подтверждению»), поэтому гардрейлы закладываем **до** того,
как написана первая строка исполнения.

> Связано: [../AI_AGENT_ROADMAP.md](../AI_AGENT_ROADMAP.md) (фазы агента, несущие
> принципы), [hoster_api_epic.md](hoster_api_epic.md) (заказ/снос/переустановка нод,
> на которые ляжет исполнение), [../data-model.md](../data-model.md) (`audit_logs`,
> `api_tokens`).

## Контекст: что уже есть

- Бот: `/ops <NL>` — гейт `_is_admin`, POST `/api/agent/ops/plan` с админ-заголовками,
  рендер плана с HTML-escape (`bot/handlers.py`).
- API: `POST /api/agent/ops/plan` за `require_admin` → `plan_ops` → аудит
  `agent_ops_planned` (`backend/app/api/agent.py`).
- Сервис: агентная петля (Claude Sonnet 4.6, до 12 итераций), **только read-тулы** +
  терминальный `submit_plan` (`backend/app/services/agent/ops.py`).
- Тулы: read-only интроспекция флота (`ops_tools.py`).

## Итог секьюрити-ревью (2026-06-17)

Многоагентный adversarial-ревью, 5 линз, **26 находок, 0 ложноположительных**.
Главный вывод: **сегодня (dry-run) поверхность мала** — агент физически ничего не
исполняет. Реальная опасность — на стыке с Phase 3, и сводится к 7 повторяющимся
корневым причинам (часть из них — отход кода от принципов `AI_AGENT_ROADMAP.md`).

| Тема | Severity | Сейчас / Phase 3 |
|------|----------|------------------|
| Нет рейт-лимита на дорогой LLM-эндпоинт (cost-asymmetry DoS) | HIGH | сейчас |
| Anthropic-клиент без таймаута → зависший прогон забивает threadpool | MEDIUM | сейчас |
| Показ плана в Telegram молча ломается (обрезка посреди HTML-тега) | MEDIUM | сейчас |
| Квадратичный рост контекста + N живых провайдерских вызовов за прогон | MEDIUM | сейчас |
| Трейсбек в логах из пути с расшифрованным провайдерским токеном | LOW | сейчас |
| План не персистится → TOCTOU (нечем связать confirm→execute) | HIGH | Phase 3 |
| Вывод LLM считается доверенным (`additionalProperties:True`, tier/cost от модели) | HIGH | Phase 3 |
| Нет spend-cap / max_nodes / rate-limit на costly/destructive | HIGH | Phase 3 |
| Мастер-токен, без скоупа, подделываемый `X-Admin-Actor`, нет `actor=agent` | HIGH | Phase 3 |
| Тулы ходят в БД/драйверы напрямую (минуя API-слой) — принцип #2 | MEDIUM | Phase 3 |
| destructive не отделён от costly; `destroy_node` без проверки assigned-юзеров | HIGH | Phase 3 |
| Нет идемпотентности/отката для многошаговых планов | MEDIUM | Phase 3 |
| Индирект prompt-injection: `_display_region` пишет сырую строку провайдера в контекст | MEDIUM | оба |

## Блок A — hardening dry-run (сейчас) 🚧

Маленькие безопасные правки, не меняют поведение планировщика, закрывают то, что
кусается уже сегодня. Все новые тюнинг-кнобы — `os.getenv` с дефолтами (как
`AGENT_MAX_ITERATIONS`/`AGENT_MODEL`), без проводки в compose/vault.

- [x] **Рейт-лимит** на `POST /api/agent/ops/plan`: `30/min` глобально (бот = один IP)
      + `6/min` на оператора по `X-Admin-Actor` (`backend/app/api/agent.py`).
- [x] **Кулдаун в боте** на `/ops` (`_OPS_COOLDOWN_S`, анти-даблтап), по образцу
      `_self_report_last` (`bot/handlers.py`).
- [x] **Anthropic-таймаут**: `timeout`/`max_retries` на клиенте + общий wall-clock
      дедлайн на весь цикл (< 120с бот-таймаута) + семафор на число параллельных
      прогонов, чтобы burst не выжрал sync-threadpool (`services/agent/ops.py`).
      Кнобы: `AGENT_REQUEST_TIMEOUT`, `AGENT_DEADLINE_S`, `AGENT_MAX_RETRIES`,
      `AGENT_MAX_CONCURRENCY`.
- [x] **Безопасный показ плана**: резка по целым строкам (не посреди тега) +
      `try/except TelegramBadRequest` с фолбэком на plain-text (`bot/handlers.py`).
- [x] **Кап размера тул-вывода** (`AGENT_TOOL_OUTPUT_CAP`, дефолт 16k) + **дедуп**
      одинаковых `(тул, аргументы)` за прогон — снимает квадратичный токен-кост и
      повторные удары `provider_balance`/`offerings` по API провайдера.
- [x] **Тот же рейт-лимит/таймаут на `POST /api/agent/triage/{id}`** — `triage.py`
      переведён на общий runtime, эндпоинт получил `30/min`+`6/min` по actor.
- [x] **Редактированный лог** вместо безусловного трейсбека на сбое read-тула
      (`_runtime.redact`, тонкая щель утечки провайдерских секретов).
- Рефактор: общие гардрейлы вынесены в `services/agent/_runtime.py` (бюджеты,
  `client_kwargs`, `run_budget` = семафор+дедлайн, `redact`, `cap_json`, единый
  `AgentError`) — ops и triage больше не расходятся по границам. Семафор теперь
  **общий** на оба агента (суммарный кап параллельных прогонов).

## Блок B — фундамент Phase 3 (7 гейтов, до исполнения) 🚧

1. **Персистить план + связать confirm→execute.**
   - [x] **Персист (безопасная половина, приземлено).** Таблица `ops_plans`
         (`models.OpsPlan`, миграция `0052`): id, actor, полная NL-команда (без
         обрезки), весь план+params JSONB, `content_hash` (sha256 каноничного
         плана), feasible, needs_confirmation, status (`proposed`), `expires_at`
         (TTL `OPS_PLAN_TTL_MIN`, дефолт 60м). `plan_ops` пишет КАЖДЫЙ план;
         аудит `agent_ops_planned` теперь ссылается на реальный `plan_id` (раньше
         target_id=0) + кладёт `content_hash`. Ответ отдаёт `plan_id`/`content_hash`/
         `expires_at`. Это закрывает HIGH-находки по аудиту и даёт якорь для confirm.
   - [ ] **`/execute` (решение-зависимая половина, НЕ начато).** Принимает ТОЛЬКО
         `plan_id`, грузит сохранённый план, сверяет `content_hash`, проверяет TTL/
         status, перепроверяет prerequisites. Никаких params от клиента. ← гейтит
         открытый вопрос «где живёт исполнитель + канал подтверждения».
2. **Не доверять выводу LLM.**
   - [x] **Валидатор (структурная/DB-часть, приземлено):** `services/agent/ops_execution.py::validate_plan`
         — закрытый allowlist `kind` (reject `other`/неизвестных), ре-резолв
         `provider_id`/`node_id`/`exit_id` в существующие (активные) строки, серверный
         tier из `kind` (флаг модели игнор), серверный `needs_confirmation`. Покрыт
         тестами (без сети).
   - [ ] **Сетевой pre-flight (в исполнителе):** регион/тариф/ОС по живым offerings,
         пересчёт cost = цена×count, проверка баланса — перед каждым costly-шагом.
3. **Spend-cap / max_nodes / rate-limit** на costly/destructive — жёстко на сервере.
   - [x] **Структурные капы:** `OPS_MAX_ORDER_COUNT` (на шаг) + `OPS_MAX_NODES_PER_PLAN`
         (на план) в валидаторе.
   - [ ] **Денежный spend-cap** (`OPS_MAX_SPEND_RUB`) по реальным ценам — pre-flight.
4. **Scoped-токен вместо мастер-ключа.** `agent:plan`/`agent:execute` в `api_tokens`;
   `actor_type=agent` (добавить значение в `AuditActor`); аппрув привязан к
   аутентифицированному принципалу, а не к подделываемому `X-Admin-Actor`. — НЕ начато.
5. **Исполнение через валидированные пути** (`node_spawner.spawn_node`/`destroy_node`,
   существующий provisioning), а не прямой `get_driver()` — per-action аудит,
   валидация, скоуп (принцип #2). — НЕ начато.
6. **destructive ≠ costly.**
   - [x] **Server-derive tier + инвариант** «нельзя `destroy`/`reinstall` ноду с
         `assigned_subscriptions>0`, пока в плане раньше нет `migrate_users` с неё»
         — в валидаторе, покрыт тестами.
   - [ ] **Усиленный ack** для destructive поверх обычной кнопки confirm.
7. **Идемпотентность/откат.** Per-step id + idempotency key + статусы; политика
   частичного отказа (не сносить старую ноду, пока новая не здорова и юзеры не
   мигрированы). — НЕ начато (исполнитель).

Плюс: **data-fence** в системном промпте (тул-вывод = ДАННЫЕ, не инструкции) +
слугификация `region` перед сохранением (mirror `validate_node_name`).

## Решения (2026-06-17, с оператором)

- **Исполнитель — RQ-воркер** (как provisioning): `/execute` энкьюит job, воркер
  катит шаги в фоне (order/bootstrap уже так работают, без HTTP-таймаута), статусы
  per-step ложатся естественно.
- **Подтверждение — inline-кнопка в боте**: план показывается с кнопкой «Исполнить»
  → callback с `plan_id`. (Для destructive — поверх обычной кнопки нужен усиленный
  ack, см. gate 6.)

## Открытые вопросы

- `OpsPlan` TTL и что считать «протух» (изменился флот → переплан). Сейчас TTL =
  `OPS_PLAN_TTL_MIN` (60м); нужно ли инвалидировать план при изменении флота?
- Усиленный ack для destructive: второй тап с подтверждением vs ввод кода?
