# Неясные места и аудит-находки — консолидированный список

Этот файл — агрегированный срез разделов `⚠️ Неясные места` из всех документов иерархии `docs/`. Каждый пункт — честное «код говорит X, намерение неясно», без рекомендаций и TODO'шных хотелок. Источник — соответствующий файл в `docs/`, где этот же пункт живёт в своём контексте.

**Как обновлять:** этот файл **производен** от `⚠️ Неясные места` разделов в отдельных документах. Если добавляете новый пункт — добавляйте его **сначала** в тематический файл (`architecture.md`, `components/*.md`, и т.д.), а затем синхронизируйте сюда. Если пункт разобрался и больше не «неясен» — удаляйте из обоих мест.

**Audit-ссылки** (`> ⚠️ См. audit/...`) — италиком и под тем же источником, где они указаны в оригинале. Детали audit-находок живут в отдельной audit-документации, которую эта иерархия не дублирует.

---

## architecture.md

- `install.sh` в корне — не документировано официально, когда его запускают vs когда ansible'ом. По коду он отличается от `deploy_app_stack` playbook'а содержимым, но не ясно, какой flow считается каноническим для «поднять на новом хосте с нуля».
- Монтирование приватного SSH-ключа в worker-контейнер через `${PROVISIONING_SSH_KEY:-...}` в `docker-compose.yml:144` — по коду дефолт вместо пути подставляет литеральную строку публичного ключа, что гарантированно сломает `docker compose up` без env. Как оно вообще запускается — непонятно без обхода реального compose-файла на проде.
- Ноды в `inventories/prod/hosts.yml` закомментированы (только `mgmt-1`, `nl-monitoring`, `nl-web` — все на `45.14.244.140`). Неясно, где именно хранится актуальный список VPN-нод: либо они подтягиваются динамически из БД через `ansible_runner.build_inventory`, либо в отдельном inventory, не попавшем в репо.

---

## data-model.md

- `HealthProbe`: есть ли периодический cleanup старых строк (retention)? В коде воркера не сразу видно такой тики, но таблица по смыслу должна расти быстро. Если cleanup'а нет — это отдельная тема.
- Связь `Payment.subscription_id` и `Payment.invoice_id`: оба nullable. Какой из них авторитетен для stage-4 балансного flow — из модели не видно, нужен переход в `services/balance.py` и `_mark_invoice_paid_core`.
- ~~Поле `Subscription.traffic_used_mb` и `traffic_limit_mb` — кто его обновляет?~~ Закрыто 2026-07-29: блокирующий ингест (`api/traffic.py`) удалён, оба поля мертвы. Действующий учёт — `Subscription.traffic_used_bytes`: тик `traffic_stats` копит per-user байты (резолв через `Credential.access_username`), продление обнуляет.
- `has_frozen_this_year` vs `frozen_days_used` / `frozen_year`: три поля одновременно описывают freeze-историю, одно из них — V2 упрощение. Какое правило сейчас в силе — «один раз в год» или «до N дней в год» — из модели нельзя однозначно сказать.

---

## components/backend-api.md

- `api_webapp.py` импортирует `_subscriptions_for_user` прямо из `api.py` (`api_webapp.py:30`). Это единственная точка крест-модульного импорта приватных helper'ов — неясно, является ли это осознанным «этот helper shared» или остатком рефакторинга. Если рефакторинг — есть риск неожиданного поведения при изменениях в api.py.
- `_mark_invoice_paid_core` при уже `paid` инвойсе делает fallback на «latest subscription by (user_id, plan_id)» (`api.py:1810-1820`). Логика защиты от повторной доставки webhook'а, но может отдать пользователю данные **другой** подписки, если он успел купить второй инвойс на тот же план. Код-level ambiguity; см. audit/...
- `/api/healthz?deep=true` проверяет, что queue доступна, но не гоняет SELECT 1 на неё — `get_queue() is not None` верифицирует наличие Redis connection, но не его работоспособность.

---

## components/bot.md

- `ADMIN_IDS` хранится в env и в `bot/config.py`; изменить список = перезапустить контейнер бота. Backend про эти id ничего не знает — только бот фильтрует `/invoices` и support-FSM по ним. Возможны расхождения «админ в боте ≠ тот, кто в SPA», без одного источника истины.
- `NOTIFICATION_POLL_INTERVAL=10` по дефолту означает доставку уведомлений с задержкой до 10с. Для warning'ов это не критично, для `config_ready` — воспринимается как лаг провижининга. Компромисс нагрузки vs latency не зафиксирован. (С 2026-08-25 `config_ready` реально пишется бэкендом — см. `services/config_ready.py`; до этого канал был мёртв, и вопрос лага был чисто теоретическим.)
- `_stars_successful_payment` возвращается юзеру «оплата получена» только после успешного forward'а в backend (`handlers.py:409-413`). Если backend вдруг подтвердил 200 по verified-seen, но операция на уровне БД упала — юзеру скажут «всё ок», а реального provisioning'а не будет. Гарантии «happy path fall-through» стоит проверять на уровне backend'а, не бота.

---

## components/worker.md

- Структура обработки ошибок между тиками неконсистентна (см. выше). Не ясно, намеренно ли `run_autoscale_tick` падает молча при ошибке `evaluate_all_pools`, или это пропущено.
- `WARM_POOL_MAX_CONCURRENT` задаётся в compose (`docker-compose.yml:127`, default 2), но в коде `run_warm_pool_check` сам его не читает — лимит применяется глубже, в `services/warm_pool.py` через `threading.Semaphore`. Поведение этого семафора при горизонтальном масштабировании воркеров — см. `components/warm-pool.md`.
- Retry-политика единичных `run_provisioning_task` при падении ansible'а — не очевидна из самого `worker.py`. RQ имеет встроенный retry через `Job.retry`, но в коде `enqueue_task` в `backend/app/queue.py` его использование нужно проверять отдельно.
- `worker.work(with_scheduler=True)` поднимает scheduler-поток внутри worker-процесса. При двух параллельно запущенных воркерах обa поднимут scheduler — как RQ это разруливает (один scheduler побеждает по advisory lock?) из кода не видно.

---

## components/warm-pool.md

- **Идемпотентность `warm_one_bundle` при partial failure.** Если ansible прошёл (нода уже имеет user'а), а `db.commit()` упал (например, конфликт на уникальном индексе, OOM в PG), то запись на ноде останется, а в БД — нет. При следующем тике сгенерится **другой** `access_username` и на ноду pushнётся ещё один user. Ни очистки, ни проверки на orphan'ов не видно.
- **`physical_revoke_credential_bundle` ↔ `threading.Semaphore` ↔ RQ concurrency.** RQ по умолчанию гоняет job'ы в одном worker-процессе последовательно. Но если кто-то поднимет `RQ_WORKER_COUNT>1` (через `supervisor`/compose replicas), физический revoke сможет войти в семафор одновременно с warming'ом на разных процессах — см. комментарий к process-local ограничению.
- **Inconsistency в ordering «DB first vs ansible first».** `warm_one_bundle` делает `ansible → db.commit()`. `physical_revoke_credential_bundle` тоже: `ansible → db.delete()`. А `unassign_bundle` — напротив, `db.flush()` без ansible. Это намеренно (revoke делится на два стадии), но читатель видит один модуль с двумя несимметричными порядками операций — явно это нигде не объяснено.
- **`try_assign_bundle` при частично warm bundle'е.** Если по какой-то причине в bundle'е осталась только одна warm-строка (скажем, ручной DELETE в админке), anchor-lock найдёт её, а `bundle = [...]` вернёт только её — функция назначит подписке credential с неполным набором протоколов. Нет валидации «bundle должен содержать N credential'ов, где N = количество активных конфигов ноды».
- **`invalidate_node_warm_pool` коммитит сама.** В отличие от `unassign_bundle` (только `flush`), она делает `db.commit()` в конце. Несимметрично с общим принципом «caller commits», причина не задокументирована.

> ⚠️ См. audit/... — процессно-локальный `threading.Semaphore` в модели concurrency пула.

---

## components/payments.md

- **Random rotation без веса.** `pick_provider_name` — чистый `random.choice`. Нельзя настроить «80% CryptoBot, 20% SBP», нельзя выключить провайдер для конкретного плана, нельзя упасть обратно на резерв при сбое. Если CryptoBot лёг — `/checkout` будет рандомно успех/502 пока оператор не поправит `.env`.
- **`Payment.external_id` не уникален.** Схема позволяет два `Payment` с одним `(provider, external_id)` для одного `Invoice` — если кто-то дважды нажал checkout. Webhook найдёт последний по `ORDER BY id DESC` — старшие `Payment`-ы так и останутся `pending` навсегда. Чистильщика нет.
- **Webhook rate-limit 30/min** (`api.py:2640`) — общий на все провайдеры. Если CryptoBot начнёт агрессивно ретраить, он съест budget SBP'шных уведомлений. Индивидуальных лимитов нет.
- **Generic SBP template mode.** `PAY_URL_TEMPLATE` — чистый `str.format`, без проверки, что полученный URL вообще валиден для HTTP. Опечатка в env → пользователь получит битую ссылку без ошибки на стороне backend'а.
- **`verify_webhook` у Stars принимает любой currency только через ручную проверку.** `raise` срабатывает только если `sp.currency != "XTR"` — а если поле отсутствует, используется fallback `"XTR"` (`telegram_stars.py:114`). Это нужно, потому что forward от бота иногда не содержит currency, но делает провайдер чуть слепее, чем хотелось бы.
- **Referral-payout на `kind=topup` не атомарен с самим топапом.** Оба идут внутри одной транзакции `_mark_invoice_paid_core`, но обёрнуты разными `try/except`: ошибка бонуса не откатывает топап, но ошибка топапа откатывает бонус через общий rollback. Комментарий в коде сам это признаёт: «Payout will be retried by a nightly reconciliation if we ever add one; for now it's fire-and-forget».

> ✅ Исправлено (#62): native Telegram webhook (`/tg-webhook`) заменяет shared-bearer. Backend регистрируется через `setWebhook` и проверяет `X-Telegram-Bot-Api-Secret-Token`. Старый путь (shared-secret relay через бота) deprecated, но работает для backward compat.

---

## components/provisioning.md

- **TODO: GC half-assigned warm bundles.** Комментарий `provisioning.py:923-927` напрямую: если `_wire_warm_bundle` упадёт после `try_assign_bundle`, bundle станет assigned без Subscription. Warmer'у не предписано такие подбирать обратно — нет кода, который бы сканировал «assigned без subscription_id».
- **`_execute_task` timeout = 300s жёсткий.** Если ansible `site.yml` на новой ноде с нестабильным сетевым линком физически не успевает за 5 минут, task становится failed без возможности продлить окно через env. Worker re-enqueue сработает с такой же 5-минуткой.
- **Cold path credential'ы записываются с `is_active = False`.** `_handle_task_outcome` ставит их в `True` только после success. Но между commit'ом credential-row и проходом success/failure есть окно, в течение которого активная подписка имеет *неактивные* credentials. Для warm fast path этого окна нет (creds сразу активные), так что UI/sub-link отдают credentials по-разному в зависимости от пути провижининга — см. фильтрацию `is_active` в `api_extensions.dynamic_sub_link`.
- **`revoke_device` устанавливает `device.status=disabled`, а не `revoked`.** Терминальный статус «revoked» достигается только после удаления строки из БД. Каждый другой код (admin UI, фильтры capacity) вынужден считать `disabled` и `revoked` эквивалентными — дублирование логики.
- ~~**`_notify_bot_config_ready` не изолирует ошибки на уровне БД.**~~ Снято 2026-08-25. Реальная проблема была глубже: хук писал `_notify` в `ProvisioningTask.result`, который никто не читал, — пуш `config_ready` не доходил никому и никогда (инцидент: юзер 1000054, «сейчас пришлю ссылку» без ссылки). Теперь хук — `services/config_ready.py::notify_config_ready`: обычная строка `AuditLog(action='config_ready')`, гейт «первый рабочий девайс свежей подписки, один раз», два источника (warm — в транзакции вызывающего, cold — отдельной транзакцией после `success`-commit'а). Отдельная транзакция после success — осознанно: сбой уведомления логируется и не роняет провижн, а ссылка всегда доступна в личном кабинете и по `/config`. Down-бот не важен — строка ждёт в очереди.
- **`access_username` с timestamp suffix только в `reprovision_subscription`.** Cold path reprovision добавляет `-<epoch_second>` к username, чтобы избежать TTL collision на ноде (старый username может ещё жить в кэше ansible/xray после state=absent). Обычный `provision_subscription` такого suffix'а не делает — предполагается, что первый username на ноде всегда свежий.

> ⚠️ См. audit/... — subprocess ansible-playbook с расшифрованными секретами в CLI `--extra-vars`.

---

## infrastructure/ansible.md

- **`host_key_checking = False` + динамический inventory.** Каждый новый ansible-run backend'а открывает SSH в ноду, взятую из БД, без какой-либо записи в `known_hosts`. MITM между backend'ом и нодой полностью незаметен ansible'у. Защита остаётся только «доверие к IPv4-адресу в `vpn_nodes.host`».
- **Секреты в CLI-строке `--extra-vars`.** `shadowtls_password`, `shadowtls_ss_password`, `relay_wg_private_key`, `vless_reality_private_key` — все попадают в ansible через `json.dumps(extra_vars)` как аргумент команды. В `ps auxf` на worker-хосте это видно любому пользователю, имеющему читать `/proc/*/cmdline`. В контейнере worker'а root — только process owner'а, но host-level инспектор (если кто-то получит host) увидит секреты в аргументах.
- **`wg_exit_nodes` группа не описана в `inventories/prod/hosts.yml`.** Роль есть, playbook'ная секция есть, но `ansible-playbook site.yml` на бэкенде ничего не сделает с exit-нодами, потому что группа пустая. Как реально развёрнуты существующие exit-ноды — нигде не видно.
- **Реальные VPN-ноды НЕ в git'е inventory.** Запрос «посмотреть, какие ноды сейчас в проде» возможен только через backend.db (`vpn_nodes`), не через `git log` ansible-репо. Для disaster recovery оператору нужен доступ к БД, иначе он не знает, куда деплоиться.
- **Playbook `diagnose_node.yml` не упомянут в `deployment.md`-роутах.** В `playbooks/` есть, orchestrator его зовёт (`action=diagnose`), но как именно оператор триггерит диагностику — через API-эндпоинт `/api/nodes/{id}/diagnose` или напрямую `ansible-playbook` — не документировано в коде однозначно.
- **`forks = 20` в defaults** — но backend процесс ограничивает параллельность через `MAX_CONCURRENT_ANSIBLE=3` (и warm_pool — ещё два). Эффективно используется только один fork на запуск (одна нода в temp-inventory). Высокий `forks` — это наследие, когда site.yml мог бить по нескольким нодам сразу вручную; сейчас никогда не стреляет.

> ⚠️ См. audit/... — plaintext-секреты в `--extra-vars` CLI.

---

## infrastructure/deployment.md

- **`PROVISIONING_SSH_KEY` дефолт — literal ed25519 public key в compose.** При пустом env-var compose пытается смонтировать строку-ключ как путь, поведение платформозависимое и в лучшем случае молчаливое. Никакой pre-flight проверки в `deploy_app_stack` на это нет — роль пройдёт, worker поднимется, provisioning упадёт по SSH при первом же таске.
- **`SKIP_MIGRATIONS=1` жёстко прибит в compose, а не в env.** Убрать его можно только редактированием `docker-compose.yml`. Это хорошо (защита от гонки), но рядом с другими env-vars, которые настраиваются через `.env`, выглядит несимметрично — читатель может не заметить разницы.
- **10-секундный settle после `up -d`.** Число выбрано эвристически (`deploy_app_stack/tasks/main.yml:155`), без замеров. Медленный crash-loop с интервалом >10с пройдёт гейт незамеченным.
- **Single-host для всех трёх логических ролей.** `db_host`, `monitoring`, `web` ссылаются на `45.14.244.140`. Компрометация / отказ этого хоста = полный outage control-plane. HA-стратегия в коде никак не зафиксирована, inventory явно комментирует «splitting is a trivial inventory change later».
- **Cloudflare edge → origin зависимость.** Без CF зоны `grinwer.online` домен не резолвится (origin IP — `45.14.244.140`, но публичного DNS A-record на него без CF нет по дизайну). Отзыв CF API-токена или zone миграция = ломается renewal сертификата через DNS-01, и рендер vhost'а начнёт падать при следующем прогоне роли.
- **Monitoring и web — один и тот же docker daemon.** Два compose-проекта под `/opt/vpn` и `/opt/vpn-monitoring` делят сеть, volume namespace, CPU, диск. Явная изоляция между ними — только через префиксы проектов compose. Для проверки «что ест диск» нужно залезать в оба.
- **Нет backup'ов Postgres/Redis в публичных ролях репо.** `db_data` и `redis_data` — named volumes, любая операция `docker volume rm` необратимо уничтожит БД. Отдельного `pg_dump` cron'а в compose нет.

---

## infrastructure/nodes.md

- **`wg_exit_nodes` группа в `inventories/prod/hosts.yml` пустая.** Код роли `wg_exit_node` готов, `relay_jump_node` готова, но ни одна нода не описана — фактически relay-схема в проде не используется. Неясно, есть ли она хоть где-то в inventory вне git.
- **`relay_config` у jump-ноды хранится как JSONB plaintext.** В отличие от паролей `VPNConfig.settings`, которые зашифрованы Fernet, WG-приватник jump-ноды лежит в БД в открытом виде. Компрометация дампа БД = компрометация туннеля.
- ✅ **Health score агрегация задокументирована.** `services/health.py:recompute_node_health` — единственная точка агрегации: `ok_count / total_count * 100` за 15-минутное окно. NULL = нет проб. Описание — `docs/infrastructure/nodes.md § Health score и cooldown`.
- **Автовосстановления из `error` нет.** Нода, попавшая в error (единичный сбой API провайдера во время destroy, например), остаётся там до ручного вмешательства. Нет self-heal'а, который бы через X часов попробовал снова.
- **Promote `registering → active` требует и ansible-success, и health pass.** Если ansible прошёл, а health-probe стабильно падает (например, UFW неправильно настроен), нода остаётся в `registering` надолго. `choose_node` её всё ещё берёт (registering в whitelist). Это компромисс «лучше отдать свежую ноду, чем задержать подписку», но клинические случаи возможны.
- **Grace-таймер draining'а использует `updated_at`.** Любая операция, которая трогает ноду (даже миграция одной подписки), перезапускает таймер. В пуле с постоянным drip'ом миграций destroy может не случиться никогда.
- **ShadowTLS `manage_vpn_user.sh` — no-op.** Единственный общий пароль per node. Revoke одного устройства **не удаляет его фактический доступ** — пользователь продолжает ходить, пока не ротируется node password для всех сразу. Real per-device isolation ждёт SS2022 EIH.

---

## operations/runbook.md

- **Backup-стратегии нет** в репо. Никакого `pg_dump` cron'а, никакого WAL-shipping'а. Любой incident recovery сценарий предполагает, что БД цела — если нет, восстанавливать неоткуда, кроме ручного последнего снапшота.
- **`audit_logs` и `health_probes` растут без retention.** Очистка — ручная операция, не зафиксирована в cron/timer.
- **RQ failed-jobs очередь не мониторится.** Упавший physical_revoke job останется в failed registry навсегда, если его не чистить вручную. Нет алерта, что `failed_job_registry.count > N`.
- **Sub_token invalidation при компрометации.** Нет документированного пути «я знаю, что у пользователя утёк sub_token, как его отозвать, не трогая подписку». Формально — `UPDATE subscriptions SET sub_token=NULL WHERE id=...`, но последствия (старый клиент перестанет получать конфиг) не документированы.
- **Нет отдельного staging.** Все тесты — напрямую в prod. Rollback описан, но «проверить на staging перед выкаткой» — не работает, staging-inventory нет.

---

## operations/env-reference.md

- **`RENEWAL_CHECK_INTERVAL` в `.env.example` = 300, в коде default = 3600.** `worker.py` имеет `os.getenv("RENEWAL_CHECK_INTERVAL", "3600")`, а `.env.example` ставит `300`. Разница в 12×. Непонятно, какое считается правильным.
- **`AUTOSCALE_INTERVAL` default = `0` в коде, `300` в `.env.example`.** `0` означает «отключить полностью». Чистый env без `.env.example` выключит autoscale — это может быть сюрпризом при dev-разворачивании.
- **`PROVISIONING_SSH_KEY` compose default — literal public key.** Уже упомянуто в `deployment.md`/`runbook.md`. Повторяем: `.env.example` ставит `/opt/vpn/secrets/provisioning_key`, но compose fallback на `ssh-ed25519 AAA...` — чистое compose-only окружение без `.env.example` получит broken mount.
- **`FREEZE_DAYS=7` в коде, но без использования в stage 4 логике.** Похоже, deprecated stage 3 наследие. Неясно, можно ли удалять.
- **Нет env для включения/выключения individual-провайдера.** Включение CryptoBot — только `PAYMENT_PROVIDER=cryptobot` или наличие в `PAYMENT_PROVIDERS` списке. Нет способа «оставить rotation, но временно выключить конкретно SBP» без редактирования списка.
- **`SBP_<SLUG>_*` не валидируются на старте backend'а.** Если `PAYMENT_PROVIDERS=sbp:foo,cryptobot`, но нет `SBP_FOO_HMAC_SECRET` — ошибка всплывёт только в момент первого `/checkout` c этим провайдером. Pre-flight check для SBP не зафиксирован.
- **`BOT_USERNAME` может быть пустым** — рефералки покажут бесшовный код вместо share-link. Warning'а backend не эмитит.
