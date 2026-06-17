# AI-агент: ops + support (роадмап)

Статус: **черновик-идея**. Это план «прикрутить LLM-агента ко всей системе» — чтобы он отвечал юзерам как саппорт, делал диагностику и (под контролем) дёргал операционные действия. Документ — мысли и фазы, не закоммиченный код.

> Связано: [operations/control_channel_roadmap.md](operations/control_channel_roadmap.md) (failover + report-failure), [infrastructure/nodes.md](infrastructure/nodes.md) (жизненный цикл нод), [data-model.md](data-model.md) (`audit_logs`, `api_tokens`).

## Зачем

У нас уже есть **детерминированные** примитивы: автоскейл (`autoscale.py` по watermark'ам), control-channel + smart-diagnose, health-probes, traffic-stats, миграция подписок, `NodeUserBan`. Чего нет — **NL-слоя** поверх них: триаж инцидентов человеческим языком, черновики ответов юзерам, «сделай X по промту». Агент — это слой оркестрации/триажа, **а не замена** детерминированной логики.

## Несущие принципы (не нарушать)

1. **Два РАЗНЫХ агента с разными грантами.** Недоверенный ввод (сообщения юзеров) и привилегированные действия (мутация инфры) **никогда не делят один контекст** — иначе prompt-injection («удали все ноды») = confused deputy.
   - **Support-агент:** читает сообщения юзеров → только **read + draft**. Физически нет тулов мутации инфры.
   - **Ops-агент:** работает с **доверенными** промтами оператора → мутации, но по тирам (ниже).
2. **Инструменты = существующие API-ручки.** Никакого прямого доступа к БД/SSH. Тонкие обёртки над FastAPI-эндпоинтами → бесплатно получаем валидацию, `require_admin`, `AuditLog`.
3. **Least privilege.** Агент ходит под scoped-токеном из `api_tokens`, не под мастер-ключом. `ADMIN_ACTOR_HEADER="agent"` → все действия в `audit_logs` атрибутируются агенту (`actor_type` уже поддерживает не-человека).
4. **Автономия падает с ростом blast-radius** (тиры ниже). Destructive/$$$ — только «агент предлагает → оператор жмёт».
5. **Kill switch + dry-run.** Глобальный флаг `agent_enabled` (как `mute` / `sharing_enforcer_enabled`), dry-run по умолчанию для мутаций, всё логируется.

## Тиры действий

| Тир | Примеры | Режим |
|---|---|---|
| **Read** | diagnose, health, traffic, summarize, draft-ответ, RAG по `docs/` | автономно |
| **Reversible** | mute ноды, mark draining, migrate подписки, ban/unban-node, switch-exit | автономно с rate-limit + аудит (легко откатить) |
| **Destructive / $$$** | spawn/delete ноды, mass-операции, batch-detach | **proposal → one-click approve оператором** |

Скейл нод — горячий путь оставляем детерминированному `autoscale`; агент тут только для **исключительных** случаев с гардрейлами (spend-cap, `max_nodes`, rate-limit).

## Что строим на (существующие примитивы)

- **Tool-слой:** обёртки над `api/nodes.py`, `api/exits.py`, `api/subscriptions.py`, `api/users.py`, `services/health.py::smart-diagnose`, control-channel. Каждый tool → существующий эндпоинт.
- **Аудит/атрибуция:** `AuditLog` + `actor_type` + `ADMIN_ACTOR_HEADER`.
- **Токены/скоупы:** `api_tokens` (расширить per-endpoint scope при необходимости).
- **Канал в юзеров:** aiogram-бот (support-агент draft'ит, человек аппрувит на старте).
- **Реализация LLM:** Claude API tool-use (см. харнес-скилл `claude-api`), системный промт = доки (или RAG), отдельный worker-сервис / ops-команда в боте.

## Фазы

### Phase 1 — Диагностический триаж (read-only) ✅ реализовано (по запросу; за флагом)
**Риск нулевой, польза мгновенная, юзеров не трогает.** Агент берёт overview +
health_probes + traffic_stats + configs + provisioning-таски, коррелирует Claude
tool-use'ом и выдаёт человекочитаемый **root-cause + рекомендованное действие**
(без выполнения).
- Реализация: `backend/app/services/agent/tools.py` (read-only tool-слой:
  `get_node_overview`, `get_node_configs`, `get_node_health_probes`,
  `get_node_traffic`, `get_node_provisioning_tasks`), `services/agent/triage.py`
  (manual tool-use loop, `claude-sonnet-4-6` по умолчанию, adaptive thinking),
  `POST /api/agent/triage/{node_id}` (`api/agent.py`).
- Гардрейлы: kill switch `AGENT_ENABLED` (по умолчанию OFF), отдельный
  `ANTHROPIC_API_KEY` (не мастер-ключ системы), `AGENT_MAX_ITERATIONS` кап,
  все тулы read-only, действие в `audit_logs` (`agent_node_triaged`).
- Выход: отчёт по запросу (`POST .../triage/{id}`). TODO: авто-триггер при
  node→error + кнопка в админке + RAG по `docs/`.
- Env (vault): `vault_anthropic_api_key` → `deploy_app_stack_anthropic_api_key`.

### Phase 2 — Draft-саппорт + propose-remediation
- **Ops-планировщик (dry-run) ✅ реализовано (за флагом).** NL-команда оператора
  («закажи 2 ноды в Германии, подними туннель, перевези юзеров с ноды X») →
  агент через READ-ONLY fleet-тулы (`services/agent/ops_tools.py`: list_providers /
  provider_balance / provider_offerings / list_nodes / list_exits / node_load /
  list_pools) собирает состояние флота и ОБЯЗАН вызвать терминальный `submit_plan` →
  структурированный план: пошагово, с оценкой стоимости (₽), влияния (сколько
  юзеров затронем) и tier'ом (read/reversible/costly/destructive).
  `services/agent/ops.py::plan_ops`, `POST /api/agent/ops/plan {command}`
  (`api/agent.py`), audit `agent_ops_planned`. **Ничего не выполняет** — чистый
  dry-run; за флагом `AGENT_ENABLED` + `ANTHROPIC_API_KEY`, кап итераций.
  Решено с оператором: модель автономии = «план → одно подтверждение»; старт = dry-run.
  - Точка входа: TG admin-команда `/ops <NL>` (`bot/handlers.py`).
  - **Секьюрити-эпик:** [operations/ops_agent_security_epic.md](operations/ops_agent_security_epic.md)
    — ревью 2026-06-17 (26 находок, 0 false-pos). Блок A (hardening dry-run:
    рейт-лимит по actor + бот-кулдаун + Anthropic-таймаут/дедлайн/семафор +
    безопасный показ плана) ✅. Блок B (7 гейтов до исполнения) — до Phase 3.
  - TODO: выполнение плана за одним подтверждением — только поверх блока B
    (персист плана + confirm-binding, валидация params, spend-cap/max_nodes,
    scoped-токен, destructive-инвариант). costly/destructive — order/destroy/migrate.
- **Support-агент:** RAG по `docs/` с цитатами → черновик ответа юзеру. На старте — **human-approve** перед отправкой; потом автоответ на FAQ-класс. Анти-галлюцинации: только из доков, с источником.
- **Ops-агент (reversible):** предлагает действие (`migrate sub X`, `mute node Y`, `ban-node`) с обоснованием → кнопка «применить». После обкатки — часть reversible переводим в автономный режим с rate-limit.

### Phase 3 — Ограниченная автономия (reversible) + guarded destructive
- Reversible-действия автономно под гардрейлами (rate-limit, аудит, откат).
- Destructive (spawn/delete/mass) — **остаётся** proposal+approve. Spend-cap + `max_nodes` + dry-run обязательны.

## Гардрейлы (чек-лист перед каждой фазой с мутациями)

- [ ] scoped-токен, не мастер-ключ; least privilege на эндпоинты
- [ ] `agent_enabled` kill switch + dry-run по умолчанию для мутаций
- [ ] spend-cap + `max_nodes` + rate-limit на действия
- [ ] всё в `audit_logs` (`actor="agent"`), out-of-band approval для destructive
- [ ] RAG с цитатами для support (анти-галлюцинации)
- [ ] недоверенный ввод юзеров НЕ втекает в контекст с привилегированными тулами

## Открытые вопросы

- Где живёт ops-агент: отдельный worker-сервис vs команда в админ-боте?
- Approval-канал для destructive: inline-кнопка в боте / в admin SPA / отдельный ack-токен?
- RAG: эмбеддинги по `docs/` (что обновлять при изменении доков) vs прямой подсос md в контекст?
- Метрика успеха support-агента (deflection rate) и порог авто-отправки.
