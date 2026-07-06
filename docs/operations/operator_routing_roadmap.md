# Operator-aware routing (краудсорс «нода × оператор»)

Статус: **Phase 1 в работе.** Маршрутизация по оператору на основе сигнала, собранного из юзерского флоу «VPN не работает». Проб-SIM-ок **нет и не будет** (дорого/неэффективно/региональная дисперсия блоков) → краудсорс — **первичный** сигнал, не дополнение.

> **2026-06: закрыт пробел сбора.** Раньше `OperatorNodeReport` писал ТОЛЬКО бот-флоу (`report-broken`), а webapp-кнопка «VPN не работает» (`/webapp/health-ping-report` → `_do_failover`) переселяла юзера, но репорт НЕ создавала → `operator_node_reports` пустела, матрица не строилась. Теперь `_do_failover` пишет репорт на КАЖДЫЙ user-reported failover (operator=None), а webapp после пересадки спрашивает оператора одним тапом (`POST /webapp/report-operator` → `report_id` из ответа health-ping-report) — зеркало бот-флоу. Watcher по `target_access_username` доводит outcome.

> **2026-06: per-device failover.** Sub-level «VPN не работает» гребёт ВСЮ подписку (все устройства → новая нода + user-wide `NodeUserBan`) — для multi-device юзера это передёргивало рабочие устройства и отнимало у них живую ноду. Теперь, если у активной подписки **>1 устройства**, webapp (`Help.tsx`) сперва спрашивает «какое устройство не работает?» → `POST /webapp/report-broken-device {device_id}` → `ProvisioningOrchestrator.failover_device(device)`: перетряхивает ноды ТОЛЬКО этого устройства (diverse-aware — реподнимает на свежей primary + `_maybe_attach_diverse` исключая весь битый набор; `migrate_device_to_node` для diverse запрещён, схлопнул бы набор), соседние устройства и ноду user-wide НЕ трогает. Репорт пишется с правильным `device_id`. Одно устройство → старый whole-sub путь (разницы нет).

> **2026-06-21: per-device пикер и в БОТЕ (паритет с webapp).** Раньше per-device был только в webapp, а бот-кнопка «🆘 VPN не работает» всё ещё гребла всю подписку (юзер с 5 устройствами одним тапом переселял все 5). Теперь бот зеркалит webapp: `self_report_vpn_broken` тянет живые устройства (`GET /api/admin/client-control/devices-by-telegram?telegram_id=`); если **>1 устройства** — показывает inline-пикер с именами `Device.name` (как записал юзер) + строку «🔁 Все мои устройства» (whole-sub); выбранное переносит через `POST /api/admin/client-control/report-broken-device {telegram_id, device_id}` → `failover_device(device)` (соседей не трогает, user-wide `NodeUserBan` НЕ ставит, `sub_token`/UUID сохраняет → установленный клиент не рвётся). **Одно устройство** → переносим сразу (один тап, как раньше; per-user cooldown против спама). **«Все устройства»** → старый whole-sub `report-broken` (со своим per-user 5-мин троттлом). Per-device путь троттла не имеет намеренно: перенесённое устройство сразу `revoked` → повтор по нему отсекается проверкой статуса, а раз бана нет — спам не выжигает пул. Хендлеры: `bot/handlers.py` (`self_report_vpn_broken`, `broken_device_keyboard`, `broken_device_choice`, `_do_device_failover`, `_do_whole_sub_failover`); эндпоинты — `backend/app/api/client_control.py`; тесты — `backend/tests/test_report_broken_device.py`.

> **2026-06-21: фикс reconnect-детекта + кнопка «✅ Всё работает».** (1) `report_reconnected` (`services/operator_reports.py`) читал верхнеуровневый `details["users"]`, которого НЕТ — реальная форма `details["<proto>"]["users"]` (вложенно по протоколу, см. `traffic_stats.to_details` / канонический толерантный `nodes.py::list_node_users`). Из-за этого функция **всегда возвращала False**: каждый pending-репорт watcher флипал в `inconclusive` (никогда `ok`), бот слал «всё ещё не работает» ВСЕМ (включая переподключившихся), а матрица никогда не получала `ok` на target-ноде. Починено на вложенный обход (per-device гранулярность сохранена: ровно `target_access_username` × `target_node_id` — чужое устройство не зачтётся). (2) В still-broken-prompt добавлена позитивная кнопка «✅ Всё работает» → `POST /api/admin/client-control/report-ok` (зеркало `report-still-broken`) ставит `outcome="ok"` — единственный user-driven путь в `ok`; не понижает явный `fail`/`ok`, апгрейдит `pending`/`inconclusive`; кормит матрицу как `(target_node, operator)=ok` без правок матрицы. Тесты — `test_operator_report.py`.

> **2026-07 (аудит, находки 99/12): бот-репорты в крауд-детекте + TTL/потолок авто-банов.** (1) Раньше `report-broken` / `report-broken-device` мигрировали юзера, но крауд-эскалацию (`_escalate_node_failure_reports`) не звали, а их audit-метаданные писали `failed_node_id` вместо `current_node_id` — бот-канал (основной канал жалоб) вообще не участвовал в пороге `NODE_FAILURE_BAN_THRESHOLD`. Теперь оба бот-пути пишут `current_node_id` и после миграции дёргают эскалацию — массовый отказ ноды, репортнутый через бота, выводит её в cooldown. (2) Авто-баны `NodeUserBan` получили on-access TTL (`NODE_USER_BAN_TTL_HOURS=48`, протухшие снимаются перед каждым user-driven failover'ом; ручные админ-баны не трогаются) и потолок (`NODE_USER_BAN_MAX_PER_USER=3` — дальше мигрируем БЕЗ бана старой ноды): добросовестный юзер с проблемой на своей стороне больше не выжигает себе пул нод в вечный `no_target`. Тесты — `test_auditfix_client_control_py.py`.

> Связано: [data-model.md](../data-model.md) (`operator_node_reports`), `migrate-auto` + `NodeUserBan` (фундамент уже зашиплен), connection-tracking (`NodeTrafficSample.details["<proto>"]["users"]`).

## Идея

99% проблем — **блок РУ-jump-ноды конкретным оператором** (до зарубежного exit из ДЦ блоков нет). Значит полезная матрица — **`(РУ-relay-нода × оператор) → ok/fail`**. Собираем её, не спамя юзеров: исход **наблюдаем** (факт переподключения), а не спрашиваем.

## Флоу (юзер сам инициирует — не спам)

```
[VPN не работает] (тап в боте)
   → backend: migrate-auto (свободная нода B) + бан старой ноды A
   → создаём OperatorNodeReport(outcome=pending, failed_node=A, target_node=B,
                                target_access_username=<имя на B>)
   → бот: «🔄 Поменяли тебе сервер. Попробуй подключиться через пару минут.
          Какой у тебя интернет?» (имя ноды юзеру НЕ показываем)
          + inline: МТС / Билайн / МегаФон (Yota) / Tele2 (Т-Мобайл) /
            Домашний-WiFi / Другое  (Yota — MVNO на МегаФоне, Т-Мобайл — на Tele2)
   → tap → set operator на репорте + бот подтверждает видимым сообщением
     «Понял — у тебя …».   [(operator, A) = сильный FAIL]
   → через делей (_STILL_BROKEN_DELAY_S, 15 мин) бот дёргает бэк
     (GET report-status — reconnect-чек на лету): если юзер НЕ
     переподключился — отдельным сообщением присылает «❌ Всё равно не
     работает»; если переподключился — молчит (не дёргаем зря)
   → [всё равно не работает] → существующий чат с админом (help:support)
                              + (operator, B) = FAIL
```

**Исход без опроса:** воркер через **T_RECONNECT=10 мин** (5 на попытку + 5 на проверку) смотрит `NodeTrafficSample.details["users"]` ноды B на `target_access_username`:
- появился → `outcome=ok`, `(operator, A)=fail` подтверждён, `(operator, B)=ok`;
- не появился → `outcome=inconclusive/fail`.

Самый весомый негативный сигнал — «мигрировали на B → опять тап» = подтверждённый `(operator, B)=fail`.

## Данные

`operator_node_reports` (модель `OperatorNodeReport`, миграция 0040): `user_id, subscription_id, device_id, operator, failed_node_id, target_node_id, target_access_username, outcome[pending/ok/fail/inconclusive], reported_at, resolved_at`.

- **Оператор — контекст, не атрибут юзера** (днём МТС, вечером WiFi). Храним per-репорт «оператор на момент жалобы». Для будущего роутинга (Phase 2) берём **последний заявленный** — best-effort.
- **Таксономия:** `mts / beeline / megafon / tele2 / home_wifi / other / unknown`. WiFi/other/unknown в матрицу почти не идут (порог не наберут), храним для статистики.
- **Регион** не берём в v1 — юзер за CGNAT/впн, чистого региона нет, а спрашивать = лишний тап. Региональная дисперсия блоков просто расширяет нужный порог. Регион — v2.

## Матрица (advisory, Phase 1)

Агрегат из репортов:
- **fail** для `(node, op)`: репорты где `failed_node=node` (сильный, юзер сам пометил) ИЛИ `target_node=node & outcome∈{fail,inconclusive}`.
- **ok** для `(node, op)`: `target_node=node & outcome=ok`.
- **score** = ok / (ok+fail), уверенность при **K≥5 разных device**.
- **recency-decay: окно 24ч** (РКН меняет блоки ежедневно — без decay матрица врёт). Старт консервативный (короткое окно, высокий порог), ослабляем по факту.

В админке — матрица оператор × РУ-нода (цвета по score, счётчики) + сырые репорты. **`choose_node` НЕ трогаем** в Phase 1.

## Глушим шум (проб для кросс-валидации нет)

- Порог **K=5 разных device** на ячейку, не по одному тапу.
- Вес: подтверждённый «мигрировали → опять fail» сильнее одиночного «broken без переподключения».
- **Анти-абьюз:** рейт-лимит на тап «не работает» (есть 5-мин throttle в control-channel) + кап на число само-банов, иначе юзер вычерпает пул себе же.
- «Нет свободной ноды» (`migrate-auto` → 503) → фолбэк в чат с админом.

## Фазы

1. **Сбор + advisory** (текущая): модель + флоу + watcher + матрица в админке. Роутинг не трогаем — смотрим, есть ли устойчивый сигнал.
2. **Operator-aware `choose_node`**: мягкое **предпочтение/штраф** по матрице (не жёсткий exclude — у части оператора нода работает, у части нет; решение вероятностное). Жёстко исключаем только при очень высоком fail-рейте. Включаем **после** того, как матрица докажет устойчивость.
3. **Проактивный per-operator переброс** по спайкам fail от оператора на ноде (только этого оператора, не всю ноду).

## Параметры (залочены)

| Параметр | Значение |
|---|---|
| `_STILL_BROKEN_DELAY_S` (делей кнопки «всё равно не работает», условный пуш) | **15 мин** (должен быть > `TRAFFIC_STATS_INTERVAL` 300с, иначе ложные пуши: к проверке нет свежего traffic-сэмпла → reconnected=False у переподключившегося) |
| `OPERATOR_RECONNECT_WINDOW_MIN` (окно наблюдения исхода для матрицы) | **10 мин** (5 на попытку + 5 на проверку) |
| recency-окно матрицы | **24 ч** |
| `K` (порог доверия ячейке) | **5 разных device** |

## Фундамент (переиспользуем, не строим заново)

`migrate-auto` + `NodeUserBan` (зашиплено), connection-tracking (`NodeTrafficSample`), control-channel report-failure, бот с callback-кнопками, `help:support` (чат с админом).
