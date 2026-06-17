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
   - [x] **`/execute` (приземлено, за флагом `OPS_EXECUTE_ENABLED=0`).** `POST
         /api/agent/ops/execute {plan_id}` — принимает ТОЛЬКО `plan_id`, грузит
         план, проверяет флаг/status/TTL, ре-валидирует, **атомарно армит**
         (условный UPDATE proposed→executing под row-lock — анти-двойной-заказ),
         энкьюит RQ-джобу `run_ops_plan_execute` (детерминированный `job_id`).
         Воркер (`execute_plan`) перепроверяет TTL + `content_hash` (целостность),
         ре-валидирует, pre-flight, исполняет. Колонка `ops_plans.execution`
         (миграция `0053`) — per-step результат.
2. **Не доверять выводу LLM.**
   - [x] **Валидатор (структурная/DB-часть, приземлено):** `services/agent/ops_execution.py::validate_plan`
         — закрытый allowlist `kind` (reject `other`/неизвестных), ре-резолв
         `provider_id`/`node_id`/`exit_id` в существующие (активные) строки, серверный
         tier из `kind` (флаг модели игнор), серверный `needs_confirmation`. Покрыт
         тестами (без сети).
   - [x] **Сетевой pre-flight (`_preflight`):** регион/тариф по живым offerings,
         cost = цена×count, проверка баланса провайдера — перед заказом. Диспетчер
         читает ТОЛЬКО `step["resolved"]` (валидированный спек), не сырые params.
3. **Spend-cap / max_nodes / rate-limit** на costly/destructive — жёстко на сервере.
   - [x] **Структурные капы:** `OPS_MAX_ORDER_COUNT` + `OPS_MAX_NODES_PER_PLAN` (валидатор).
   - [x] **Денежный spend-cap** (`OPS_MAX_SPEND_RUB`) по реальным ценам — в `_preflight`.
4. **Scoped-токен вместо мастер-ключа.** `agent:plan`/`agent:execute` в `api_tokens`;
   `actor_type=agent` (добавить значение в `AuditActor`); аппрув привязан к
   аутентифицированному принципалу, а не к подделываемому `X-Admin-Actor`. — НЕ начато.
5. **Исполнение через валидированные пути.**
   - [x] **order_node → `spawn_node_async`** (тот же путь, что кнопка «Заказать
         ноду», не raw driver). Остальные kind'ы исполнитель пока ЯВНО пропускает
         (skipped) — непротестированные destructive-пути не стреляют.
   - [ ] **destroy/reinstall/migrate/tunnel** — отдельными ревьюируемыми проходами.
6. **destructive ≠ costly.**
   - [x] **Server-derive tier + инвариант** «нельзя `destroy`/`reinstall` ноду с
         `assigned_subscriptions>0` без предшествующей `migrate_users`» — валидатор.
   - [ ] **Усиленный ack** — оператор отказался от PIN; берём обычную кнопку confirm.
7. **Идемпотентность/откат.**
   - [x] **Идемпотентность:** атомарный арм (proposed→executing условным UPDATE) +
         детерминированный `job_id` + отказ от терминальных статусов. Stop-on-first-
         failure; created-id оплаченных нод сохраняются даже при сбое на середине.
   - [ ] **Авто-recovery** застрявших `executing` (sweep/reaper) — НЕ начато (manual reset).

Плюс: **data-fence** в системном промпте (тул-вывод = ДАННЫЕ, не инструкции) +
слугификация `region` перед сохранением (mirror `validate_node_name`).

### Исполнитель MVP — adversarial-ревью (2026-06-17, перед коммитом)

Фокусный ревью деньги-тратящего кода: **19 находок (3 critical, 5 high, 3 medium,
8 low), все confirmed.** Исправлено перед коммитом:
- **CRITICAL ×3 + HIGH (двойной заказ):** неатомарный арминг (read-check-write под
  READ COMMITTED) + нет идемпотентности джобы → два POST'а / re-enqueue заказывали
  ноды дважды. → **атомарный условный UPDATE** + детерминированный `job_id`.
- **HIGH `pool_id`** из сырых params → деньги потрачены, потом FK-падение на commit.
  → валидируем `pool_id` (резолв в `ServerPool`) до заказа.
- **HIGH/MEDIUM** потеря id оплаченных нод при сбое на середине → `_exec_order_node`
  возвращает created даже при ошибке.
- **LOW:** TTL не перепроверялся в воркере → добавлено; `content_hash` не сверялся →
  сверяем (ловит мутацию плана); семантика `partial`/`executed`; безопасный парсинг
  env-капов (set-but-empty не обнуляет cap молча).

**Известные ограничения (приняты для MVP):** только `order_node` исполняется;
застрявший `executing` чинится вручную (нет reaper); баланс-TOCTOU между pre-flight
и заказом ограничен провайдерским отказом при заказе + spend-cap; `image` валидирует
драйвер (битый → провайдер отклоняет заказ до списания); нет глобального
кумулятивного fleet-cost кэпа (только per-execution).

### Осталось до бот-доступности
- **Бот:** inline-кнопка «Исполнить» под планом → callback с `plan_id` → `/execute`.
- **Включение:** `OPS_EXECUTE_ENABLED=1` (через ansible) — только после бота + проверки.

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
