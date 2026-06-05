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

### Phase 1 — Диагностический триаж (read-only) ★ старт
**Риск нулевой, польза мгновенная, юзеров не трогает.** Агент берёт smart-diagnose + health_probes + traffic_stats + свежий `audit_logs`, коррелирует и выдаёт человекочитаемый **root-cause + рекомендованное действие** (без выполнения). Здесь отлаживаем tool-слой, scoped-токен, аудит, dry-run — до того как подпускать к мутациям/юзерам.
- Tools (read): `get_node_health`, `get_node_diagnose`, `get_traffic`, `list_recent_audit`, `get_subscription`.
- Выход: отчёт в ops-чат/бот по запросу или по триггеру (нода → `error`).

### Phase 2 — Draft-саппорт + propose-remediation
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
