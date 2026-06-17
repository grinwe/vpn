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
- [ ] (бэклог A) Кап размера тул-вывода + дедуп/кэш `provider_balance`/`offerings`
      внутри прогона (квадратичный токен-кост + N ударов по API провайдера).
- [ ] (бэклог A) Тот же рейт-лимит/таймаут на `POST /api/agent/triage/{id}` —
      такой же дорогой LLM-эндпоинт.
- [ ] (бэклог A) Редактированный лог вместо безусловного трейсбека в
      `ops.py` на сбое провайдер-тула (тонкая щель утечки секретов).

## Блок B — фундамент Phase 3 (7 гейтов, до исполнения)

1. **Персистить план + связать confirm→execute.** Таблица `OpsPlan` (id, command,
   полный план+params JSONB, server-cost, hash, TTL, status); `/execute` принимает
   **только** `plan_id`, грузит сохранённый план, перепроверяет prerequisites. Никаких
   params от клиента на исполнении.
2. **Не доверять выводу LLM.** Строгая per-kind схема params (`additionalProperties:false`),
   `kind` — закрытый enum; **сервер** пересчитывает cost из живых offerings и **сам**
   выводит `needs_confirmation` из tier; флаги модели для гейтинга игнорируются.
3. **Spend-cap / max_nodes / rate-limit** на costly/destructive — жёстко на сервере,
   независимо от плана (roadmap строки 29/77/83).
4. **Scoped-токен вместо мастер-ключа.** `agent:plan`/`agent:execute` в `api_tokens`;
   `actor_type=agent` (добавить значение в `AuditActor`); аппрув привязан к
   аутентифицированному принципалу, а не к подделываемому `X-Admin-Actor`.
5. **Исполнение через валидированные API-эндпоинты**, а не прямой `get_driver()` —
   per-action аудит, валидация, скоуп (принцип #2).
6. **destructive ≠ costly.** Server-derive tier; инвариант «нельзя снести/реинсталлить
   ноду с `assigned_subscriptions>0` без миграции»; отдельный усиленный ack.
7. **Идемпотентность/откат.** Per-step id + idempotency key + статусы; политика
   частичного отказа (не сносить старую ноду, пока новая не здорова и юзеры не
   мигрированы).

Плюс: **data-fence** в системном промпте (тул-вывод = ДАННЫЕ, не инструкции) +
слугификация `region` перед сохранением (mirror `validate_node_name`).

## Открытые вопросы

- Канал подтверждения для destructive: inline-кнопка в боте / admin SPA / отдельный
  ack-токен?
- `OpsPlan` TTL и что считать «протух» (изменился флот → переплан).
- Где живёт исполнитель: RQ-воркер (как provisioning) vs синхронный эндпоинт?
