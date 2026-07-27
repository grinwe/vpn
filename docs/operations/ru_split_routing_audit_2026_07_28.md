# Аудит RU split-routing — 2026-07-28

> Статический аудит кода (7 линз, 72 подтверждённые находки, 18 опровергнутых).
> Прод-состояние НЕ проверялось — см. §6 «Что проверить на проде».

## 1. Прямой ответ: работает ли RU-маршрутизация

**Работает — но только в одной из четырёх комбинаций, и ни один слой системы этого не гарантирует.**

RU split-tunnel физически существует только там, где совпало всё сразу:
нода является relay (есть `RelayExitLink` → непустой `xray_primary_interface`) **И** клиент подключился по **Reality** или **XHTTP** **И** назначение попало в domain-regexp (`\.ru$`, `\.su$`, `\.xn--p1ai$`, 22 бренда) по отснифанному SNI.

| Протокол | RU-правила в конфиге | Привязка к WG | Итог на РУ relay-ноде |
|---|---|---|---|
| VLESS Reality | есть (с 21-22.04.2026) | `direct` + `direct-wgN` через sockopt | **работает**, кроме хвоста доменов (см. §3-Б2) |
| VLESS XHTTP | есть (с 05.06.2026) | то же | **работает**, те же оговорки |
| VLESS WS-CDN | **нет ни одного** | `direct` получает sockopt на wgN из шаблона **и** из jq-реконсайла | **100% трафика, включая РУ, уходит в зарубежный exit** |
| Hysteria2 | секций `routing`/`acl`/`outbounds` нет вообще | **никакой** — отдельный демон, сокеты не биндятся | **100% трафика, включая зарубежный, выходит с РУ-IP ноды — VPN не работает вовсе** |

На зарубежных и на РУ-нодах без прикреплённых exit'ов вопрос не стоит: `_primary` пуст, `direct` и `direct-local` egress'ят одинаково, правила — безвредный no-op. Ломается исключительно relay-периметр, а по прод-инвентарю все клиентские `vpn_nodes` — РФ, все `wg_exit_nodes` — зарубеж, то есть relay-схема это норма, а не край выборки.

**Почему «не всегда и не у всех» — четыре независимые оси разброса:**
1. **Какой лег выбрал клиент в этот раз.** Саба плоская, `_relabel_uri` намеренно прячет и страну, и протокол за «V8 сервер N»; юзер не может отличить hy2-на-relay от reality. Плюс `subscription-autoconnect-type: lowestdelay` включён в проде для всех (`deploy_app_stack_sub_happ_autoconnect: "all"`), и hy2 — единственный лег без WG-хопа, то есть системно выигрышный по задержке (для HAPP с включённым «Пинг при запуске»; Hiddify/v2rayNG/iOS заголовки игнорируют).
2. **Relay нода или нет.** Пользователи на прямых нодах не видят ни одной из этих проблем.
3. **Есть ли у кредa `exit_id`.** У кого есть fan-out-правило по email — geoip-safety-net мёртв (§3-Б2); у кого нет (холодный warm-pool, легаси) — работает.
4. **Какой конкретно РУ-сайт открывают.** `.ru/.su/.рф` + 22 бренда — ок; `userapi.com`, `mycdn.me`, `yastatic.net`, `okko.tv`, `premier.one`, `zvuk.com` — нет.

**Датировка.** Оба крупных разрыва — не свежие баги, а протухшие осознанные допущения. `docs/infrastructure/nodes.md:218` фиксирует «`vless_ws_cdn` и `hysteria2` RU-обхода **не несут** (на данный момент неактуальны)» — это писалось 05.06.2026, когда hy2 был закомментирован в `site.yml`, а ws-cdn стоял на 5 нодах из 10. Реанимация hy2 (db9c3e8, 22.07), фикс hy2-URI (6c88d49, 25.07 — до него hy2 не пускал никого) и 443-унификация (23-25.07) аннулировали предпосылку, но ни один из этих коммитов раздел не тронул и гейта не добавил. Жалоба пользователя приходит через 2-3 дня после этого.

---

## 2. (а) Ломает VPN полностью — юзер выходит с РУ-IP на всё

### А1. Hysteria2 на relay-ноде не туннелируется вообще
**Severity: critical.** Найдено независимо шестью линзами.

`infra/ansible/roles/install_hysteria2/templates/config.yaml.j2` (47 строк) содержит только `listen/tls/obfs/bandwidth/auth`. Ни `outbounds:`, ни `acl:` — per-destination routing у hy2 отсутствует как класс.

Замыкающее звено доказательства — маршрутизация, а не отсутствие acl: `relay_jump_node/templates/wg-client.conf.j2:8-11` задаёт `Table = off` + `PostUp = ip route add 0.0.0.0/0 dev %i metric 200`, то есть WG-дефолт **намеренно хуже** основного (metric 0/100). В туннель попадает только то, что xray явно биндит через `sockopt.interface` (SO_BINDTODEVICE), а патчит sockopt исключительно `xray_reconcile.jq` по маске `config*.json`. `hysteria-server.service` — отдельный процесс без `BindToDevice`/netns. Итог структурный, а не побочный: 100% hy2-трафика выходит с РУ-IP relay-ноды. Проверка от противного: если бы wg-маршрут выигрывал, SSH к relay-ам сломался бы — раз ansible работает, основной дефолт заведомо < 200.

**Важно про направление отказа.** RU-половина у hy2 получается случайно правильной (РУ-сайты и так видят РУ-IP). Ломается зеркальная: заграничный трафик тоже идёт напрямую → заблокированные РКН ресурсы остаются заблокированными при индикации «подключено». Это второй сценарий из ТЗ аудита.

**Гейта нет ни на одном уровне:** `provisioning.py:3206-3247` (cold path), `:3565+` (backfill), `:4300-4330` (reprovision/миграция), `warm_pool.py:172-174` создают кред на каждый enabled VPNConfig без единой проверки relay-ности; `node_spawner.ensure_hysteria2_config:420-526` relay-агностична; `api_extensions._decrypt_configs` не фильтрует саб по протоколу. Роль в `site.yml:15` без `when:` (гейт фактически `hysteria2_port` из БД).

**Побочные эффекты, о которых стоит знать отдельно:**
- hy2-кредам всё равно штампуется `exit_id = bundle_exit_id` (`provisioning.py:3204+3230`, `:4297`, `warm_pool.py:271`). БД и админка утверждают, что лег выходит через конкретный exit, которого он никогда не касается → битая атрибуция; `relay.py:151-161` считает нагрузку на exit по `Credential.exit_id`, то есть hy2-строки перекашивают выбор least-loaded exit'а для реально туннелируемых кредов.
- Админская операция «сменить exit» для hy2-юзера молча не делает ничего — это признано в докстринге `switch_subscription_exit` (`provisioning.py:5192-5196`).
- Мотивация реанимации hy2 — именно жёсткие РУ-регионы, где TCP-Reality зарезан. То есть в целевом сценарии, где hy2 единственный рабочий лег, юзер гарантированно осядет на нём и гарантированно получит коннект без VPN.

**Ограничение проверки:** статически нельзя подтвердить, на каких именно прод-нодах есть одновременно enabled hy2-конфиг и RelayExitLink — это состояние БД. Кода, который бы это предотвращал, нет. См. §4, запрос 1.

### А2. Blackhole relay→exit: зарубежный трафик умирает при живом РУ
Не утечка, а отказ терминуса — но для юзера выглядит идентично («VPN не работает, РУ-сайты открываются»). Четыре независимых пути:

- **NAT на exit-ноде не переживает ребут.** `iptables-persistent` ставит только `bootstrap_node` (по группе `vpn_nodes`), а на `wg_exit_nodes` катаются лишь `wg_exit_node` + `node_exporter`. Handler `persist iptables` уходит в ветку без применяющего юнита; после ребута `wg-quick@wg0` поднимается, `ip_forward` выживает, а MASQUERADE и FORWARD ACCEPT исчезают. Автоматической ремедиации нет: реконсайлер ходит только по `vpn_nodes`. Отягчающее: `site.yml:71` гейтит роль на `wg_exit_private_key`, которого нет в `group_vars/all.yml`, — ручной `site.yml` на exit'е роль **пропустит**. Восстановление только через бэкендовые exit-bootstrap/rebootstrap или как побочный эффект attach/detach.
- **`PATCH /exits/{id}` меняет host/wg_port/wg_public_key без единой таски** (`api/exits.py:468-479`, commit :496). Endpoint зашит в `wgN.conf` на relay'е, WG сам не переприцеливается, `PersistentKeepalive` шлёт кипэлайвы на старый адрес, `wg-quick@` не имеет `Restart=`. То же у `keygen_exit` (`:545-566`) — рассинхрон двусторонний. Exit не может спасти роумингом: `wg0.conf.j2:6-11` рендерит peer'ов без `Endpoint`. Смена `wg_port` ломает обе стороны. Наблюдаемость есть, лечения нет: `relay_link_health` даёт `links_no_match`/протухший handshake, auto-diagnose плодит `action="diagnose"` таски — это и есть уже наблюдавшийся «спам диагностики» вокруг exit-инцидента.
- **Нет фолбэка при мёртвом линке.** `run_relay_link_health_tick` пишет handshake/rx/tx в БД и всё; единственная реакция — read-only диагностика (намеренно, `docs/operations/diagnostics.md:291-295` — анти-flapping). Хуже: распределение вообще не читает health-данные — `choose_exit_for_relay` пиннит новые креды на «least-loaded» без проверки `last_handshake_at`, `primary_wg_interface` возвращает просто наименьший wgN, `choose_node`/`_do_failover` линк-слепые. Восстановление реактивное: по жалобе юзера (health-ping/webapp/`/client/report-failure`, проактивный опрос раз в 7-14 суток) или руками.
- **Гонка адресов WG.** `allocate_client_address` (`relay.py:29-66`) без блокировки, уникальности `(exit_id, wg_client_address_v4)` в схеме нет (`models.py:1033-1046`). Два параллельных attach на один exit → два peer'а с одинаковым AllowedIP → у проигравшего трафик умирает в обе стороны, победитель недетерминирован (запрос линков без `ORDER BY`). Внутри batch-attach гонки нет (flush-guard), нужен параллельный ручной/скриптовый вызов. Это переоткрытие уже задокументированной находки `docs/audit/code_audit_report.md:461-469`.
- **Порядок старта:** `xray.service:4` (и `xray-xhttp`, `xray-ws-cdn`) — `After=network.target`, wg-quick — `After=network-online.target`, поэтому после ребута xray почти всегда стартует раньше туннеля (самолечится) — а вот упавший в failed `wg-quick@wgN` не поднимет никто (нет `Restart=`, реконсайлер событийный). Детект есть (auto-diagnose ~5-10 мин), ремонт ручной.

### А3. Усилитель экспозиции: клиент систематически выбирает сломанный лег
`api_extensions.py:151` отдаёт `subscription-autoconnect: true` + `lowestdelay`, прод включает это для всех. Все xray-протоколы на relay получают WG-хоп (+40-120 мс до Турции/Франции/UK), hy2 — единственный без него. Строгий вывод «hy2 всегда выигрывает» требует знания семантики замера в HAPP (через прокси или сырой RTT) — из репозитория не проверяется, confidence medium. Но второй путь экспозиции работает всегда: ручной перебор серверов в плоской сабе вслепую. Отключать `lowestdelay` — лечить симптом и ломать failover диверс-сабы; фикс правильнее в корне (не отдавать hy2-креды relay-нод).

---

## 3. (б) RU-трафик течёт через WG — РУ-сайты видят зарубежный IP

### Б1. ws-cdn: RU-правил нет вообще, при этом sockopt на WG есть
**Severity: high.** Найдено шестью линзами.

`config_ws_cdn.json.j2`: routing (`:25-38`) = только `api-in` + fan-out по `user`; в outbounds (`:75-100`) **нет тега `direct-local`**; нет `routing.domainStrategy`; нет секции `dns`. При этом дефолтный outbound `direct` **сам** получает `sockopt.interface = _primary` прямо из шаблона (`:78-84`).

Ключевое, что часто недооценивают: fan-out тут не обязателен. На **однолинковом** relay'е `build_xray_relay_outbounds` возвращает `[]`, user-правил нет вовсе — а весь ws-cdn-трафик всё равно уходит в WG через дефолтный `direct` с sockopt. Плюс второй канал доставки: `relay_jump_node/tasks/main.yml:183-224` ищет `config*.json` глобом, `reconcile_xray.sh:25-30` имеет явный case для `config_ws_cdn.json`, `xray_reconcile.jq:30-40` проставляет sockopt — то есть привязка к туннелю приезжает и при attach/detach линка, когда install-роли не крутятся. (Попутно: `docs/infrastructure/nodes.md:214` утверждает, что reconcile патчит «только `config.json` (Reality)» — это неверно с апреля 2026 и должно чиниться тем же коммитом.)

Пострадавшие: юзер на РУ relay-ноде, чей клиент выбрал ws-cdn-лег. Сбер/Тинькофф, Госуслуги, Ozon/WB, Avito, Кинопоиск, VK/Mail видят турецкий/французский/британский IP → капчи, «недоступно в вашем регионе», блок входа в банк. Чаще всего это ровно те, у кого reality/xhttp зарезаны DPI, — самая нуждающаяся в RU-обходе группа.

**Фикс неполон, если копировать только правила.** Нужны все пять кусков (уроки №2-3 из `nodes.md:206-212`): секция `dns` (77.88.8.8 первым), `routing.domainStrategy: IPIfNonMatch`, два RU-правила **выше** fan-out, outbound `direct-local` (freedom, без sockopt), и `include_role: xray_geoip` в `install_vless_ws_cdn/tasks/main.yml` (сейчас роль подключена только в reality:105 и xhttp:125 — на ws-cdn-only ноде `geoip.dat` отсутствует, и `xray -test` на `:155` уронит плей). `sniffing` уже на месте (`:69-72`). Заодно добавить `try-restart xray-ws-cdn` в `geoip-update.service` (сейчас там только `xray` и `xray-xhttp`).

Хорошая новость: `xray_reconcile.jq` фильтрует строго по `startswith("direct-wg")` и дописывает fan-out **в конец** `routing.rules`, поэтому добавленные `direct-local` + RU-правила переживут attach/detach и останутся выше — как это уже работает на reality/xhttp.

Оговорка по масштабу: утверждение «ws-cdn раскатан на 10/10» по репозиторию **не подтверждается** — ранбук 443-унификации фиксирует срез на 23.07 (есть на 4vds-dk, 4vds-ru, vsin-nl, vsin-ru, ufo-ru-01) и помечает шаг добавления как опциональный. Доказуемый периметр — РУ relay-ноды, у которых ws-cdn есть. См. §4, запрос 1.

### Б2. Двухпроходный IPIfNonMatch: geoip-safety-net мёртв у всех, у кого есть fan-out-правило
**Severity: high.**

Xray при `domainStrategy: IPIfNonMatch` делает два прохода по **всему** списку правил: первый — без DNS-клиента в контексте (ip-матчер по domain-назначению не может сматчиться), второй — с резолвом, и **только если в первом не сматчилось ни одно правило**. Правило `{"user": [emails], "outboundTag": "direct-wgN"}` матчится в первом проходе всегда (email известен без резолва). Итог: для любого domain-назначения, не попавшего под 4 regexp'а, срабатывает user-правило, второй проход не наступает, `ip: [geoip:ru]` не проверяется вообще.

Уточнения к формулировке (важно, чтобы не переделали лишнее):
- Не «никогда», а «у кредов с `exit_id`». Для холодного warm-pool/легаси без `exit_id` user-правила нет → второй проход выполняется → geoip работает. Отсюда и «не у всех».
- `geoip:private` из этого исключается: приватный адрес всегда приходит IP-литералом, ip-правило стоит выше user-правила и матчится в первом проходе.
- Заявленный в `nodes.md:211` safety-net «для прямых IP-коннектов» практически не существует: `sniffing.destOverride: ["http","tls"]` подменяет IP-литерал на отснифанный домен. Живёт только для не-TLS/не-HTTP raw TCP и для UDP/QUIC.
- Область шире, чем мульти-линк: `resolve_exit_interface` (`relay.py:203-227`) не проверяет число линков (в отличие от `build_xray_relay_outbounds`, `:254-255`) и отдаёт wgN любому кредy с `exit_id` → `EXIT_INTERFACE` → `manage_vless_*_user.sh: rewrite_routing()` создаёт правило `direct-wg0` даже на однолинковом relay'е. Комментарии в четырёх местах кода («None (single-link or non-relay) is omitted») утверждают обратное — это расхождение реализации с задокументированным намерением, а не хак.
- Состояние **мигает**: `site.yml` перерендеривает config.json из шаблона и стирает эти правила (geoip временно оживает), следующий `provision_device`/`resync` возвращает их. Отсюда «вчера работало, сегодня нет» на одной ноде.

**Что реально ломается:** длинный хвост РУ-ресурсов вне 4 regexp'ов. Самые весомые: `userapi.com` (sun\*.userapi.com — фото/видео VK), `mycdn.me` (CDN Mail.ru/VK) — то есть основной объём байт самой массовой РУ-соцсети, при том что «морда» vk.com идёт direct-local. Далее: `yastatic.net`, `avatars.mds.yandex.net`, гео-лицензированный стриминг `okko.tv`, `premier.one`, `more.tv`, `zvuk.com`, `my.games`; зоны `.moscow`, `.tatar`, `.xn--d1acj3b`, `.xn--p1acf`.

**Два варианта фикса, у них разная цена и риск:**
- Дешёвый и безопасный: расширить domain-список явными `domain:` записями (порядок правил перестаёт зависеть от резолва). Список вынести в одно место — сейчас он продублирован копипастой в reality и xhttp.
- Семантический: `IPIfNonMatch` → `IPOnDemand` (dnsClient цепляется до первого прохода). Цена — резолв на каждое domain-назначение и повышение критичности качества DNS (§4-В3). Плюс гейт числа линков в `resolve_exit_interface`, симметрично `build_xray_relay_outbounds`.

### Б3. Дрейф конфигов: RU-правила рождаются только при полном `site.yml`
**Severity: medium.**

RU-правила существуют **только** в jinja-шаблонах. Все инкрементальные пути (`manage_vless_*_user.sh: rewrite_routing`, `bulk_apply_clients.py:117-124`, `xray_reconcile.jq:43-55`) их **сохраняют**, но никогда не создают — фильтр `startswith("direct-wg")`. Реконсайлер сходит ноды по `desired_generation > reconciled_generation`, а этот счётчик бампает только `mark_node_dirty` из `create_or_coalesce_node_bootstrap` — то есть по правкам **в БД**. Изменение файла шаблона не бампает ничего, периодического drift-свипа нет (content-diff явно вынесен в Phase 4 «Deferred» в `provisioning_reconciler_epic.md`).

Следствия:
- Нода, забутстрапленная до 21-22.04.2026 (reality) / до 05.06.2026 (xhttp) и с тех пор только реконсайленная, крутит конфиг без RU-блока. Лечит любой полный `site.yml`; **не** лечат: device apply/revoke, resync, relay attach/detach, renew_certs, upgrade_xray, warm-pool.
- **Attach exit'а — самый опасный частный случай.** `POST /exits/{id}/links` создаёт только `relay_tunnel/apply` (одна роль `relay_jump_node`), не бампает generation и не перерендеривает конфиг. jq моментально ставит sockopt на дефолтный `direct` — то есть нода начинает туннелировать всё, — а добавить `direct-local` он не умеет. Если конфиг старый (или это ws-cdn) — RU-трафик начинает течь ровно в момент attach и не самолечится. Историческое подтверждение класса: снапшоты `infra/ansible/recovered/*__config_xhttp.json` (19.05.2026) — на всех 5 РУ relay'ях `direct.sockopt=wg0` при нуле RU-правил.
- Индикатор отставания слеп: `nodes_outdated_release` (`xray_releases.py:298-299`) исключает ноды с `release_version IS NULL`, а NULL — ровно у тех, кого не бутстрапили с 26.07.2026 (дата появления маркера). Фиче два дня, значит сейчас NULL практически у всех.

**Смягчающее:** июльская кампания унификации гоняла полный `site.yml` по флоту, поэтому «протухшие» ноды, вероятно, отсутствуют — но это гипотеза, проверяемая только запросом (§4).

### Б4. QUIC/HTTP3: domain-правило не применяется
`destOverride: ["http","tls"]` во всех трёх vless-инбаундах — QUIC-сниффер не включён, для UDP/443 SNI не извлекается, решает только `ip: geoip:ru`. Расхождение возникает узко: РУ-домены, резолвящиеся в **не-РУ** IP (мелкие `.ru` за Cloudflare/иностранным CDN) — по TCP идут `direct-local`, по HTTP/3 проваливаются в fan-out и уходят через exit. Крупные РУ-сервисы на РУ-IP ловятся geoip и по QUIC — сценарий «антифрод банка рвёт сессию» кодом не подтверждается. Плюс компенсатор «Yandex-DNS первым» на QUIC не работает (адрес уже резолвлен клиентом). Фикс однострочный: добавить `"quic"` в `destOverride` в reality и xhttp.

---

## 4. (в) Частные утечки и хрупкости

**В1. `mail.*` уходит напрямую с РУ-IP.** Regexp `^(yandex|mail|vk|ok|...)\..+$` анкорится на первый лейбл, а не на регистрируемый суффикс. Практически значим ровно один токен — `mail.`: `mail.google.com` (веб-Gmail), `mail.proton.me`, `mail.yahoo.com`, `mail.zoho.com`, корпоративные `mail.<компания>.<tld>`, IMAPS/SMTPS-хосты на 993/465. Для Gmail это утечка/приватность (security-алерт «вход из России» у юзера, включившего VPN именно от этого); для `mail.proton.me` — **жёсткая поломка**: Proton в РФ заблокирован, домен просто не откроется. Второстепенно и редко: `rbc.com`, `ria.com`, `avito.ma`, `ok.*`, `hh.*`, `lenta.com`. Деанона клиента нет — наружу выходит IP relay-ноды. Токены `ozon|tass|habr|2gis|dzen|dtf` на не-.ru зонах — это **цель** правила, не побочный ущерб. Обратная сторона того же якоря: `www.vk.com`, `m.vk.com`, `www.habr.com` первым лейблом не матчатся (для `.ru` прикрыто `\.ru$`, для `.com` — нет). Минимальный фикс: убрать `mail` из списка (он покрыт `\.ru$`) и заменить якорь на `(^|\.)(...)\.` с ограничением TLD.

**В2. `geoip:private` → доступ клиента к внутренностям ноды.** Правило `ip: ["geoip:ru","geoip:private"] → direct-local` отправляет приватные и loopback-назначения на локальный стек. Уникальная новая поверхность: gRPC-порты xray API 127.0.0.1:10085/10086/10087 (`StatsService` — read-only, но `statsquery -pattern 'user>>>'` отдаёт перечисление всех email'ов ноды `user-<uid>-<devid>`/`warm-*` и их трафик), приватная сеть ДЦ и **169.254.169.254** (метадата-эндпоинт облака — утечка user-data/креды инстанса, confidence medium, зависит от хостера). `HandlerService` не включён, манипуляции юзерами недоступны. Важно: убрать `geoip:private` из правила **не закрывает** дыру — на не-relay нодах и в ws-cdn приватные назначения долетают до loopback через дефолтный `direct` без всякого правила. Фикс: `{"ip":["geoip:private"],"outboundTag":"block"}` первым правилом (после api-in) во **всех трёх** шаблонах.

**В3. DNS: решение и коннект расходятся, резолв идёт вне туннеля открытым UDP/53.** Все freedom-outbound'ы (2+N штук: `direct`, `direct-local`, по одному `direct-wgN`) — без `domainStrategy`, то есть AsIs: домен отдаётся ОС, резолвится системным резолвером ноды, запрос не наследует sockopt и уходит по main-таблице мимо туннеля. Секция `dns` (77.88.8.8 → 8.8.8.8 → 1.1.1.1) используется только для routing-решения, причём сама подчинена routing: 77.88.8.8 матчит `geoip:ru` → `direct-local` (открытый UDP/53 с РУ-IP), а 8.8.8.8/1.1.1.1 уезжают в WG (география exit'а). Следствия: (а) классификация и факт коннекта используют разные ответы; (б) **отравление ответа** (заглушка РКН у резолвера или инъекция ТСПУ на транзите) даёт подставной РУ-IP → матчит `geoip:ru` → коннект к **заблокированному** сайту уходит напрямую, мимо туннеля — самый короткий путь к «VPN не работает на том, ради чего куплен»; (в) обратный слив: Яндекс-DNS с РУ-egress отдаёт для иностранных сайтов ближайшие РУ-кэши → `geoip:ru` → `direct-local`. Утверждение «резолвер РУ-хостера» кодом не подтверждается (`/etc/resolv.conf` ничем не управляется) — достаточен более слабый и проверяемый инвариант: резолв неуправляемым резолвером, открытым UDP/53, вне WG. Фикс придётся делать в **трёх шаблонах + `xray_reconcile.jq`** одновременно — jq пересобирает `direct-wg*` по шаблону `{protocol:"freedom", tag, streamSettings:{sockopt}}` без `domainStrategy` и молча откатит правку только в .j2 на первом же attach.

**В4. Висячий `outboundTag: direct-wg0` на однолинковых relay'ях.** Правило создаётся (три пути: per-device provision, warm-pool, bulk-resync), а outbound — нет (`build_xray_relay_outbounds` спецкейсит `<=1` линк). Xray логирует «non existing outTag» и падает на дефолтный `direct`, у которого тот же sockopt на wg0 — результат сегодня **эквивалентен** задуманному, пользовательского эффекта ноль, RU-утечки нет (правило дописывается в хвост, после RU-блока). Остаётся: латентная зависимость от фолбэка xray, шум в логах, маскирующий настоящие ошибки тега, и «вечно changed» рендер с лишними рестартами. На мульти-линке тот же механизм в транзиентном окне (линк в БД есть, reconcile не докрутил) уводит свежего юзера через **чужой** exit — самолечится.

**В5. Легаси-креды выпадают из fan-out.** `build_xray_relay_outbounds` читает только `Credential.access_username` и молча пропускает NULL, тогда как resync берёт `cred.access_username or device.access_username`. Токсичную пару «`exit_id NOT NULL` + `access_username NULL`» создаёт **только** админское «сменить exit» (`switch_subscription_exit/switch_device_exit` делают bulk UPDATE без фильтра по username, явно захватывая легаси-строки). Итог: админ видит «переключил», юзер остаётся на primary wgN. Залипает на пути `relay_tunnel apply` (там авто-resync не запускается вовсе) до следующего `site.yml`. К RU split-tunnel отношения не имеет. Фикс: `outerjoin` Device + `coalesce(...)`, симметрично ресинку, плюс разовый backfill.

**В6. Окно между созданием кредa и успехом apply.** Cold-path создаёт кред с `is_active=False`; `build_xray_relay_outbounds` его не видит, авто-resync тоже (фильтр по `is_active`). Ре-рендер fan-out в этом окне стирает правило. Второй, худший путь — `relay_tunnel apply` (attach/detach/reconnect), где авто-resync не запускается вообще. Эффект: выход через primary exit вместо назначенного, без потери связности и без RU-утечки; самолечится. Только мульти-линк, только промах warm-пула.

**В7. `_build_relay_wg_links` пропускает линки без `wg_public_key`, а fan-out/primary — нет.** На мульти-линке со смешанным состоянием роль сносит wgN, а xray продолжает на него биндиться → ENETUNREACH (fail-closed) для затронутой когорты. Триггер только админ-ручной (`PATCH /exits/{id}` с пустым ключом — `WGExitNodePatch` не валидирует непустоту, либо правка БД); оба attach-пути отказывают с 400.

**В8. Сетевой уровень (все — hardening, ни один не даёт утечки):**
- IPv6-литералы в не-HTTP/TLS трафике падают с ENETUNREACH (туннель IPv4-only — намеренно, `RELAY_ROADMAP` 0.1 «Strip IPv6»). HTTP/HTTPS спасает sniffing, подменяющий литерал на домен. Зона поражения: SSH/IMAP/игры/P2P/QUIC к v6-литералам. Fail-closed.
- MTU в обоих wg-шаблонах не зафиксирован, MSS-clamping на exit'е нет. Сценарий: аплинк exit'а уже аплинка relay'я (облачные оверлеи 1450/1460, DDoS-скрубберы) → «сайт наполовину». Проверить по репо нельзя. Фикс: одна строка `-j TCPMSS --clamp-mss-to-pmtu` в FORWARD на exit'е + явный `MTU =` для детерминизма.
- `PostUp = ip route add 0.0.0.0/0 dev %i metric 200` с одинаковой метрикой для всех wgN: у мульти-линковых relay'ев (по recovered-дампам — 5 РУ-нод × 7 конфигов) 6 из 7 add'ов падают с EEXIST, глотается `|| true`. Функционально безвредно — маршрут вестигиален целиком, весь egress идёт через SO_BINDTODEVICE. Вред: `ip route` вводит в заблуждение при разборе инцидента, а комментарий в шаблоне описывает несуществующее поведение.
- Метрика основного дефолта нигде не проверяется. Если у хостера дефолт приедет с метрикой >200, нода **не** начнёт тихо течь — она умрёт громко: wg-маршрут перехватит собственные инкапсулированные пакеты, ядро дропнет их защитой от петли, SSH отвалится, ansible упадёт на таске старта wg-quick. Это hardening-заметка (preflight-ассерт), не объяснение жалобы.

---

## 5. Почему это жило долго: наблюдаемость

Единственный инструмент, вообще смотрящий на RU-правила, — `scripts/audit_split_routing.sh`, 251 строка bash. Он:
- **нигде не вызывается** — `grep -rn audit_split_routing` даёт только самоссылки: ни CI, ни cron, ни systemd-таймер, ни один playbook, ни одна дока;
- читает **только статический** `inventories/prod/hosts.yml → vpn_nodes` = 7 хостов; cloud-spawn ноды (`4vds-ru-01`, `4vds-dk-01`, `vsin-nl-01`; по ранбуку ещё `vsin-ru`) отсутствуют в статике по прямому комментарию `hosts.yml:34-36` — включая **РУ**-ноду `4vds-ru-01`. `--node` фильтрует тот же словарь;
- проверяет **только** `config.json` и `config_xhttp.json`. `config_ws_cdn.json` и `/etc/hysteria/config.yaml` — вне поля зрения, то есть два протокола со стопроцентной поломкой невидимы;
- собирает и **выбрасывает** признаки основного механизма: `reality_has_direct_local_ob` (:92) и `reality_ru_domain_rules_count` (:94) не читаются ни таблицей, ни алертами (видны только в `--json`); для xhttp они не собираются вовсе. Единственный reality-алерт (:231-232) висит на `geoip:ru` — то есть на safety-net, который коммит 48ae3bd прямо описывает как нерабочий;
- не проверяет `outboundTag` у geoip-правила: `{ip:["geoip:ru"], outboundTag:"direct-wg0"}` пройдёт как валидное. Это **не гипотеза** — ровно такая регрессия была: e72e2db выкатил правило на `direct`, фикс 678fb3b, разбор в `nodes.md:208` («2ip.ru с клиента показывал IP exit-ноды»). Нынешний чек показал бы тот текучий флот зелёным. Та же дыра в xhttp-фильтре (:101);
- не ассертит `reality_direct_sockopt_iface` (печатает колонкой) и вообще не читает `xhttp_direct_sockopt_iface`. Инвариант «is_relay ⇒ direct привязан к wgN» не проверяется — а он достижим: ручной `ansible-playbook site.yml -l <relay>` перерендерит config.json без sockopt (`{% if _primary %}`), тогда как `relay_jump_node` на таком прогоне намеренно no-op;
- egress-пробы (:140, :146) — обычный shell-curl с ноды по OS-маршрутам, **не через xray**. Ни `routing.rules`, ни fan-out не проверяются ни разу, ни для какого протокола: весь аудит — статическая проверка присутствия ключей в JSON;
- свежесть geoip.dat меряет по mtime, а `mv "$TMP" "$DEST"` обновляет mtime независимо от содержимого → замороженное mgmt-зеркало даёт вечно «свежий» файл;
- при отсутствии jq выдаёт алерт с **неверной причиной** («RU-трафик уходит через WG») вместо «нет jq» (маркер `jq_present` в таблицу не попадает);
- и в финале безусловно печатает **«✓ Split routing на всех нодах в норме»** при покрытии 7/10 нод и 2/4 протоколов. Это не слепое пятно, а активный ложный green.

За пределами скрипта: в бэкенде/админке/боте `grep` по `direct-local|geoip:ru|split_routing|split_tunnel` даёт **ноль**. Единственный автоматический чек — `geoip_loaded` в `check_node_health:155-178` (проверяет наличие файла, не содержимое routing), он же — единственное, что покрывает cloud-ноды (диагностика DB-driven). Пробер `probes/agent.py` делает только TCP/TLS reachability (это намеренно, `probes/README.md`). Метрик по relay-линкам и geoip нет; при этом метрики воркера **вообще** не доезжают до Prometheus (нет HTTP-экспортера, скрейпится только vpn-backend) — известный долг id63. `rule_files`/`alerting`/alertmanager в `prometheus.yml.j2` отсутствуют, но транспорт алертинга есть готовый (`notify_admins` с дедупом и per-node mute), так что дешевле алертить из воркера.

Самая дешёвая непокрытая телеметрия: outbound-тег в access-логе xray (`-> direct-local` vs `-> direct-wg2`) — именно эта строка исторически вскрыла баг (`nodes.md:210`), но её никто не собирает (`xray_enforcer.py` парсит только `from <IP> … email:`).

**Документация врёт в шести местах** — и это механизм, по которому регрессия прошла незамеченной: тот, кто раскатывал hy2 и 443-унификацию, читал, что эти протоколы «неактуальны».
- `nodes.md:5` и `docs/NODES.md:29`: «install_hysteria2 закомментирована, UI не даёт создавать» — роль активна с 22.07, backend-API создание **разрешает** (`api/nodes.py:1192-1201`), запрет остался только в admin-UI и обходится прямым вызовом API. Внутри того же файла `:230` говорит обратное («install_hysteria2 — всегда в списке»).
- `nodes.md:218`: «ws_cdn и hysteria2 RU-обхода не несут (на данный момент неактуальны)» — скобка мертва.
- `nodes.md:214`: «reconcile патчит только config.json (Reality)» — неверно **с апреля 2026** (маска `config*.json` появилась в c926838, за 7 недель до написания раздела). Вывод «обход переживает attach/detach» при этом верен.
- `nodes.md:189` и `:344`: «группа `wg_exit_nodes` пустая, relay-схема в проде не работает» — неверно с 21.04.2026 (7 exit-хостов), при том что 19 строками ниже документ описывает прод-инцидент на работающей relay-паре.
- `nodes.md:202`/`:209`: описание семантики `IPIfNonMatch` («резолвит, только если ни одно **domain**-правило не попало») неверно — второй проход не наступает, если сматчилось **любое** правило, включая user. `:211` (safety-net для прямых IP) при этом корректна.
- `nodes.md:176-177`: relay описан через legacy `relay_config` и единственный `wg0`, тогда как source of truth — таблица `relay_exit_links` и per-link `wgN`.

---

## 6. Что проверить на проде прямо сейчас

Порядок важен: первые два запроса определяют, критична находка А1/Б1 сегодня или латентна.

**1. Главный вопрос — есть ли hy2/ws-cdn на нодах с живыми relay-линками (SQL):**
```sql
SELECT n.name, c.protocol, c.is_enabled, count(DISTINCT l.id) AS links
FROM vpn_nodes n
JOIN relay_exit_links l ON l.relay_node_id = n.id
LEFT JOIN vpn_configs c ON c.node_id = n.id
GROUP BY n.name, c.protocol, c.is_enabled
ORDER BY n.name, c.protocol;
```
Любая строка с `protocol IN ('hysteria2','vless_ws_cdn')`, `is_enabled = true` и `links > 0` — подтверждённая боевая поломка. (Имена таблиц/колонок сверить, если схема отличается.)

**2. Сколько кредов реально роздано по протоколам и сколько запинено на exit:**
```sql
SELECT proto, count(*) AS total, count(*) FILTER (WHERE exit_id IS NOT NULL) AS pinned FROM credentials WHERE is_active GROUP BY proto;
```
`pinned` по vless-протоколам = масштаб находки Б2 (у них мёртв geoip-проход). Строки `hysteria2` с `pinned > 0` = масштаб битой атрибуции exit'ов.

**3. Токсичная пара для switch-exit (находка В5):**
```sql
SELECT count(*) FROM credentials WHERE exit_id IS NOT NULL AND access_username IS NULL;
```
Ноль — находка теоретическая; больше нуля — у этих юзеров «сменить exit» молча не сработал.

**4. Отставшие ноды (дрейф Б3):**
```sql
SELECT name, release_version, versions_checked_at, is_active FROM vpn_nodes ORDER BY release_version NULLS FIRST;
```
Ожидаемо почти везде NULL (маркеру 2 дня) — это значит, что сигнал дрейфа сейчас нерабочий, и п.5 обязателен.

**5. Фактическое состояние RU-правил на каждой ноде (запускать НА ноде, по всем, включая cloud-spawn):**
```bash
for f in /usr/local/etc/xray/config*.json; do echo "== $f"; jq -c '{direct_local:([.outbounds[]|select(.tag=="direct-local")]|length), ru_rules:([.routing.rules[]|select(.outboundTag=="direct-local")]|length), ru_rule_idx:([.routing.rules|to_entries[]|select(.value.outboundTag=="direct-local")|.key]|first), first_wg_rule_idx:([.routing.rules|to_entries[]|select(.value.outboundTag//""|startswith("direct-wg"))|.key]|first), strategy:.routing.domainStrategy, dns:(.dns.servers//[]), direct_sockopt:(.outbounds[0].streamSettings.sockopt.interface//"none")}' "$f"; done
```
Норма для relay-ноды: `direct_local >= 1`, `ru_rules >= 2`, `ru_rule_idx < first_wg_rule_idx`, `strategy == "IPIfNonMatch"`, `dns` непустой, `direct_sockopt == wgN`. `config_ws_cdn.json` сейчас провалит всё, кроме `direct_sockopt` — это ожидаемо и есть находка Б1.

**6. hy2 на relay (находка А1) — на ноде:**
```bash
ls /etc/wireguard/wg*.conf 2>/dev/null | wc -l; test -f /etc/hysteria/config.yaml && grep -cE '^(acl|outbounds):' /etc/hysteria/config.yaml
```
Если первое `> 0`, а второе `0` при существующем файле — на этой ноде hy2-юзеры выходят с РУ-IP.

**7. Живы ли все инстансы (аудит этого не проверяет):**
```bash
systemctl is-active xray xray-xhttp xray-ws-cdn hysteria-server; ip route show default; ip route show | grep -c 'metric 200'
```

**8. На каждой exit-ноде — NAT и его персистентность (находка А2):**
```bash
iptables -t nat -S POSTROUTING | grep -c MASQUERADE; dpkg -l 2>/dev/null | grep -c iptables-persistent; wg show wg0 allowed-ips | awk '{print $2}' | sort | uniq -d
```
`MASQUERADE = 0` — зарубежка мертва у всех relay'ев этого exit'а прямо сейчас. `iptables-persistent = 0` — умрёт после следующего ребута. Непустой вывод `uniq -d` — коллизия адресов (находка В-гонка).

**9. Есть ли жалующиеся юзеры именно на hy2/ws-cdn (быстрая корреляция):** сопоставить последние обращения в поддержку с `credentials.proto` и `node.name` активных устройств этих юзеров.

**10. Единственная честная проверка результата (тулинга нет — только руками):** подключиться клиентом поочерёдно к reality-, ws-cdn- и hy2-эндпоинту одной РУ relay-ноды и на каждом сравнить IP на зарубежном эхо (`ifconfig.me`) и на РУ (`2ip.ru`). Норма: зарубежный — IP exit'а, РУ — IP relay-ноды. hy2 покажет РУ-IP на обоих, ws-cdn — IP exit'а на обоих.

Запуск `bash scripts/audit_split_routing.sh --json` полезен как срез по reality/xhttp на 7 статических нодах — но **финальную строку «в норме» игнорировать**: она не покрывает ws-cdn, hy2 и 3 cloud-ноды.

---

## 7. План исправления по приоритетам

### P0 — сегодня, останавливает кровь
| # | Что | Размер |
|---|---|---|
| 1 | **Не отдавать hy2-креды нод с `RelayExitLink` в `/api/sub`** (фильтр в `_decrypt_configs` по `cred.proto == 'hysteria2'` + наличию линков). Немедленный митигейт находки А1 без правки инфры | маленькая |
| 2 | Альтернатива/дополнение к п.1 без деплоя: **снять enabled hy2-конфиги с relay-нод через админку** (по результату запроса §6.1) | нулевая (операция) |
| 3 | Проверить и при необходимости вернуть **MASQUERADE на exit-нодах** (§6.8); при `iptables-persistent = 0` — перенести NAT/FORWARD в `PostUp/PostDown` шаблона `wg0.conf.j2` с `iptables -C \|\| -A` | маленькая |

### P1 — на этой неделе, закрывает основную массу жалоб
| # | Что | Размер |
|---|---|---|
| 4 | **Портировать RU-блок в `config_ws_cdn.json.j2` целиком**: секция `dns`, `domainStrategy: IPIfNonMatch`, два RU-правила выше fan-out, outbound `direct-local` без sockopt, `include_role: xray_geoip` в роль, `try-restart xray-ws-cdn` в `geoip-update.service`. Затем полный `site.yml` по флоту | средняя |
| 5 | **Расширить domain-список** явными `domain:` записями: `userapi.com`, `mycdn.me`, `yastatic.net`, `yandex.net`, `vk.ru`, `okko.tv`, `premier.one`, `more.tv`, `my.games`, `zvuk.com` + зоны `.moscow`, `.tatar`, `.xn--d1acj3b`, `.xn--p1acf`. Даёт больше всего пользы без изменения семантики роутера | маленькая |
| 6 | **Убрать `mail` из regexp** (покрыт `\.ru$`), заменить якорь `^(...)` на `(^\|\.)(...)\.` с ограничением TLD | маленькая |
| 7 | `{"ip":["geoip:private"],"outboundTag":"block"}` **первым правилом во всех трёх шаблонах** (закрывает доступ клиентов к StatsService и 169.254.169.254) | маленькая |
| 8 | Добавить `"quic"` в `destOverride` в reality и xhttp | маленькая |
| 9 | **`PATCH /exits/{id}` и `keygen_exit`**: при изменении `host`/`wg_port`/`wg_public_key` ставить `exit bootstrap` + `relay_tunnel apply` на каждый линк (payload как у `reconnect`), либо запретить правку этих полей по образцу `update_node` | средняя |
| 10 | `After=wg-quick@wg0.service` + `Wants=` в drop-in для `xray`/`xray-xhttp`/`xray-ws-cdn`; `Restart=on-failure` для `wg-quick@` | маленькая |
| 11 | **Обновить доки в том же коммите** (правило same-pass): `nodes.md` строки 5, 176-177, 189, 202, 209, 214, 218, 344; `docs/NODES.md:29`; матрица «протокол × есть ли сплит» с датой | маленькая |

### P2 — структурные, чтобы не вернулось
| # | Что | Размер |
|---|---|---|
| 12 | **Единый источник правды `SPLIT_TUNNEL_PROTOCOLS = {vless_reality, vless_xhttp}`** и гейты: (а) предупреждение/запрет при создании hy2/ws-cdn конфига на ноде с `RelayExitLink` (`api/nodes.py:1192+`); (б) не создавать креды не-сплитовых протоколов на relay-нодах во **всех четырёх** циклах (`provisioning.py:3206`, `:3565`, `:4300`, `warm_pool.py:156`); (в) фильтр в сабе. Без (а) дыра вернётся при следующей раскатке протокола | средняя |
| 13 | **Решить проблему двухпроходности** (Б2): гейт `resolve_exit_interface` по числу линков, симметрично `build_xray_relay_outbounds`, + либо `IPOnDemand`, либо опора на расширенный domain-список из п.5. Требует обсуждения — `IPOnDemand` повышает цену DNS-качества | средняя |
| 14 | **Вынести RU-блок в общий jinja-макрос/`vars_files`**, подключаемый всеми vless-шаблонами + CI-ассерт паритета (парсить все `roles/install_vless_*/templates/*.json.j2` при пустых extra_vars: есть `direct-local` без sockopt, оба RU-правила, `IPIfNonMatch`, непустой `dns.servers`, индекс RU-правил < индекса любого `direct-wg*`). Правка физически не сможет приземлиться в один протокол | средняя |
| 15 | **Починить `audit_split_routing.sh`**: список нод из БД/админ-API; блоки для `config_ws_cdn.json` и `/etc/hysteria/config.yaml`; `and .outboundTag == "direct-local"` в select на :91 и :101; вывести и заалертить `has_direct_local_ob`, `ru_domain_rules_count`, `direct_sockopt_iface` (инвариант `is_relay ⇔ sockopt`), симметрично для xhttp; `systemctl is-active` + `xray -test`; различать «нет jq» и «нет правила»; убрать безусловное «✓ в норме» | средняя |
| 16 | **Health-aware распределение** (дешевле авто-reconnect и не нарушает анти-flapping): фильтр по `last_handshake_at` в `choose_exit_for_relay` и `primary_wg_interface`, чтобы новые креды и дефолтный outbound не пиннились на мёртвый линк | маленькая |
| 17 | Фолбэк по DNS: `domainStrategy: "UseIP"` на `direct`/`direct-wgN`, доменно-адресная секция `dns` (РУ → 77.88.8.8, остальное → 1.1.1.1 с принудительным туннелем). Обязательно синхронно в **трёх шаблонах + `xray_reconcile.jq`** | средняя |
| 18 | `outerjoin Device + coalesce(access_username)` в `build_xray_relay_outbounds` + разовый backfill; `UNIQUE (exit_id, wg_client_address_v4)` + retry на IntegrityError; запретить пустой `wg_public_key` в `WGExitNodePatch` | маленькая |

### P3 — правильные, но дорогие
| # | Что | Размер |
|---|---|---|
| 19 | **Настоящий hy2-сплит**: `outbounds: [{name: tunnel, type: direct, direct: {bindDevice: <wgN>}}, {name: local, type: direct}]` + `acl.inline` (RU → local первыми, `tunnel(all)` последним), `hysteria2_bind_device` прокинуть из `xray_primary_interface`. `manage_hy2_user.sh` правит конфиг через `yaml.safe_load→dump`, так что новые секции переживут per-user правки. Утверждение «нельзя без policy routing» (комментарий `main.yml:232`) неверно, но фича апстрима по репозиторию не проверяется — нужен пилот на одной ноде | большая |
| 20 | **Тик verification/drift**: одна SSH-команда на ноду (переиспользовать плумбинг `node_versions.py`), jq-выжимка по всем `config*.json` → колонка `routing_audit` (JSONB) + `routing_audit_at` в `vpn_nodes`, бейдж в админке, пуш через `_notify_admins_safe` при регрессе. Закрывает ~90% сценариев, потому что они видны прямо в файле | большая |
| 21 | **E2E-проба реального egress**: `location /__ipecho` в nginx на каждой ноде + эфемерный xray-клиент в `probes/agent.py`, два запроса (РУ-назначение → ожидаем IP самой ноды; зарубежное → ожидаем IP exit'а) → новый probe kind + gauge. Единственное, что валидирует сплит как целое независимо от механизма поломки. Дешевле промежуточный шаг: собирать outbound-тег из access-лога xray | большая |
| 22 | Хеш рендер-инпутов ролей → `/etc/vpn-node-release.json` → tick-node-versions показывает отставших и (опционально) сам зовёт `mark_node_dirty` с rolling-лимитом. Плюс `nodes_release_unknown` в `versions_overview` (сейчас NULL-ноды исключены) | средняя |
| 23 | Hardening: MSS-clamp на exit'е + явный `MTU`; уникальная метрика/таблица на линк вместо общей 200; sha256 рядом с `geoip.dat` на зеркале + сверка в фетчере + чек возраста в `check_node_health`; preflight-ассерт метрики дефолта; решение по IPv6 (fail-fast `queryStrategy: UseIPv4` + block, либо dual-stack) | средняя |

---

## 8. Проверено и КОРРЕКТНО — не переделывать

- **Порядок правил на reality/xhttp.** RU-правила стоят выше fan-out и остаются выше: оба рантайм-пути дописывают user-правила **в конец** (`xray_reconcile.jq: .routing.rules + ($fan_out|map(...))`, `manage_vless_user.sh:122 +=`). First-match-wins сохраняется.
- **`xray_reconcile.jq` не ломает RU-обход.** Фильтрует строго по `startswith("direct-wg")`, `direct-local` и RU-правила сохраняет; `sockopt` трогает только у `direct`. RU-обход на reality/xhttp переживает attach/detach линков.
- **Domain-regexp по отснифанному SNI — несущий и рабочий механизм.** Весь `.ru/.su/.рф` + 22 бренда идут напрямую с РУ-IP независимо от возраста `geoip.dat`, от резолвера и от наличия fan-out-правила.
- **`geoip:ru` для прямых IP-литералов** (не-TLS/не-HTTP raw TCP, UDP/QUIC) работает: target IP известен на первом проходе, правило стоит выше user-правила.
- **`direct-local` без sockopt** — корректная конструкция.
- **`Table = off` + WG-дефолт метрикой 200** в `wg-client.conf.j2` — намеренная и правильная основа схемы: туннелем пользуются только явно забинденные сокеты. Метрику не «чинить».
- **Отсутствие RU-правил на зарубежных/standalone-нодах безвредно** — `_primary` пуст, `direct` и `direct-local` egress'ят одинаково.
- **Формулировки в боте/оффере уже осторожные.** `bot/handlers.py:2334-2337` прямо пишет «работает не на всех серверах и не на всех протоколах… напиши в поддержку», комментарий над текстом называет исключения. Расхождения обещания и реальности нет — есть UX-проблема «юзер не знает, какой лег выбрал его клиент». Тест, охраняющий эту формулировку, тоже есть.
- **Легаси-креды с `exit_id IS NULL` намеренно исключены из fan-out** (задокументировано в докстринге `relay.py:242-246`) и корректно падают на дефолтный `direct` + primary wgN.
- **Warm-pool не имеет окна невидимости**: `pool_state=assigned` и `is_active=True` ставятся одной транзакцией, до этого кред виден по ветке `warm`.
- **Batch-attach защищён flush-guard'ом** (гонка адресов внутри батча невозможна); `detach`/`evacuate` фильтруют по `node_id` симметрично fan-out.
- **Фетчер geoip** имеет `MIN_BYTES=5MB` sanity + атомарный `mv`; провал скачивания обрывает oneshot до `try-restart`, так что «битый geoip уронил xray по таймеру» — не подтверждённый путь. `xray -test -config` в ролях валит плей на синтаксически битом routing.
- **`sniffing.enabled` + `destOverride` включены на всех трёх vless-инбаундах** — единственный из предусловий RU-обхода, который на ws-cdn уже на месте.
- **IPv4-only туннель — намеренное решение** (`RELAY_ROADMAP` стадия 0.1, коммит acc7dd0), а не забытый код.
- **`node_exporter:9100` не становится доступен клиентам** через `geoip:private` — правило DROP вешается без `-i` и ловит loopback.
- **Auto-diagnose как принцип** (наблюдать, не чинить) — задокументированное решение против flapping'а; предлагаемый фикс (health-фильтр в выборе exit'а) его не нарушает.

---

## 9. Чего мы НЕ знаем (проверяется только на проде)

1. **Какие именно ноды имеют одновременно enabled hy2/ws-cdn конфиг и `RelayExitLink`.** Это состояние БД. Если таких нет — А1 и Б1 деградируют до latent; кода, который бы это предотвращал, всё равно нет. Запрос §6.1.
2. **Остались ли ноды с конфигом старше 22.04 (reality) / 05.06 (xhttp).** Июльская кампания унификации гоняла полный `site.yml` по флоту, так что вероятно нет — но индикатор (`release_version`) сейчас слеп (NULL почти везде). Запросы §6.4-6.5.
3. **Есть ли креды с `exit_id NOT NULL` + `access_username NULL`.** Ноль → находка В5 теоретическая.
4. **Стоит ли `iptables-persistent` на конкретных exit-боксах** (дубль-managed хост `uk-pq-02` управляется ещё и отдельным проектом `vpn-setup`).
5. **Реальная доля ws-cdn по флоту.** Ранбук фиксирует срез на 23.07 и помечает добавление как опциональный шаг; «10/10» по репозиторию не подтверждается.
6. **Семантика замера `lowestdelay` в HAPP** (url-test через прокси или сырой RTT). От этого зависит, «hy2 почти всегда выигрывает» или «примерно поровну».
7. **Перехватывает ли конкретный хостер исходящий UDP/53** и что реально лежит в `/etc/resolv.conf` (репозиторием не управляется).
8. **MTU аплинков** relay- и exit-хостеров — сценарий MSS без этого не воспроизводится.
9. **Метрика дефолтного маршрута** у каждого хостера (образ/cloud-init, роль не проверяет).
10. **Живы ли `geoip-update.timer` и mgmt-зеркало** — mtime-проверка в аудите заморозку зеркала не отличает от свежести.