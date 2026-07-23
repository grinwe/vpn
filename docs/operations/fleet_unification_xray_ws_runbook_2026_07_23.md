# Фитовая раскатка: 443-унификация + xray v26.3.27 + ws-cdn (ранбук)

Дата: 2026-07-23. Автор-ассистент + оператор (adept38).

## Цель
Свести reality+xhttp(+ws-cdn) на публичный **:443** (nginx-stream ssl_preread) по всему
флоту, попутно обновив **xray → v26.3.27** и добавив **ws-cdn** на ноды без него.
Убирает DPI-tell «reality на 9443».

## Ключевой принцип: ВСЁ — один бутстрап ноды
`xray_core`-роль запинена на `v26.3.27` → любой бутстрап апгрейдит xray. Унификация =
`public_port=443` на reality-конфиге (роли рендерят stream). ws-cdn = добавить конфиг
(авто DNS) + тот же бутстрап ставит nginx+cert+xray-ws. Т.е. **на ноду — один прогон**.

## Пререквизиты (ГОТОВО — но проверить деплой, см. ⚠️)
- ✅ Rebuild-фикс config_text по cfg.node (2917721) — иначе URI бьются на диверсе.
- ✅ Легаси-ключ фикс (0aea0ca) — иначе на ufo-01/02/03,aeza reality-роль скипается.
- ✅ stream-unify.conf.j2 дедуп (a5c1e0e) — иначе nginx reload падает на dest без www.
- ✅ UI-порт (4fd987f).
- ✅ **camo_dest fallback (402c145)** — БЕЗ него легаси-ноды (ufo-01/02/03) падают на
  `assert vless_reality_dest length>0`: их reality-dest лежит в `settings.camo_dest`, а
  не `settings.dest`. Именно на этом упала канарейка ufo-ru-01 (таска 2619). Фикс
  0aea0ca (из этого же списка) вскрыл проблему — раньше роль тихо скипалась.
  ⚠️ Коммит СДЕЛАН ПОЗЖЕ первой версии ранбука → **убедиться, что backend с 402c145
  задеплоен на прод (`--tags app`) ДО легаси-батча.**
- ✅ probes.py public_port (этот же деплой) — /probes/targets бьёт публичный порт
  reality, а не 9443; иначе health unified-нод сгниёт, когда пробы оживут.
- ✅ xhttp reload-handler guarded на combo-unify (этот же деплой) — батч 3 сходится
  за ОДИН прогон (см. ниже), а не падает на первом.
- ✅ dc-ru-01 — унифицирован и валидирован боем (эталон).
- ✅ Бэкап БД: `backups/vpn-20260723-105552.dump`. Тег `checkpoint/reality-fixes-20260723`.
- ⚠️ **PUSH перед раскаткой**: dev опережает origin на десятки коммитов, тег локальный,
  CI не гонялся. `git push origin dev --tags` до старта флот-раскатки.

## Текущее покрытие (на 2026-07-23)
- Унифицировано: **dc-ru-01** + **ufo-ru-01** + **4vds-ru-01** (3/10; все внешне зелено,
  reality:443 / xhttp:443 / ws:443 / 9443-closed). 4vds-ru-01 — combo-батч-3, прошёл за
  один прогон ПОСЛЕ фиксов ниже (task 2641). ⚠️ dc/ufo-01 U делали БЕЗ инвалидации
  warm-пула → дёрнуть `invalidate_node_warm_pool(db, id)` ретроспективно.
- ws-cdn ЕСТЬ: 4vds-dk, 4vds-ru✅, vsin-nl, vsin-ru, ufo-ru-01. НЕТ: aeza, dc, tw, ufo-02/03.
- Остаток батч-3 (combo, ws есть): vsin-ru (4 юзера), vsin-nl (9), 4vds-dk (9).
- Легаси-схема ключа (keypair-чек перед бутстрапом): **ufo-ru-02/03** (ufo-01, aeza — сделаны).
- xray: часть на 26.3.27, часть на 25.6.8 (апгрейд по бутстрапу).

## 🔴 ИНЦИДЕНТ-УРОК 4vds-ru-01 (первый заход лёг — читать!)
Первый unify combo-ноды 4vds-ru-01 положил её ЦЕЛИКОМ: unify-роль вставляла блок
`stream{}` в nginx.conf, даже когда `libnginx-mod-stream` не установился (стояло
`failed_when: false` → сбой замаскирован) → nginx `unknown directive "stream"` → nginx
down, reality на loopback, **dpkg заклинило** (nginx half-configured) → даже откат падал
на `bootstrap_node: Install base packages`. Расклинка — вручную: снять stream-блок
(`/etc/nginx/stream.d/reality-unify.conf` + blockinfile-маркер `V8-443-UNIFY-STREAM`),
`dpkg --configure -a`, `systemctl restart nginx`, затем non-unify bootstrap.
**ЗАКРЫТО фиксами (2026-07-23, задеплоено):**
- `install_vless_reality` PREFLIGHT: ставит+ПРОВЕРЯЕТ stream-модуль (assert на
  `modules-enabled/*mod-stream*.conf`, `file_type: any` т.к. это СИМЛИНК) ДО того как
  reality уедет на loopback, с retries, ГРОМКО. Не встал → падаем до правки reality,
  нода жива. (коммиты 341e7c5 + 3cb2667.)
- `install_vless_xhttp`: guarded pre-cert reload (combo-unify).
⇒ ПЕРЕД unify combo-ноды: preflight сам гарантирует модуль, НО убедись, что xhttp/ws
LE-серты ЕСТЬ на диске (иначе pre-cert путь; на 4vds-ru xhttp-серт отсутствовал и всплыл
второй reload). Пре-инсталл модуля вручную (`apt install libnginx-mod-stream` + чек
симлинка) — хороший де-риск.

---

## ⚠️ РИСКИ (читать до старта)
1. **Окно reality-down на бутстрапе**: reality уезжает на loopback:9443, а stream:443
   активируется только когда xhttp-роль двигает vhost на 8443 и релоадит nginx. Если play
   упадёт МЕЖДУ — reality снаружи недоступен (ловили на dc). ⇒ гнать по одной, следить,
   при падении чинить сразу (см. dc-инцидент: дедуп-баг шаблона → nginx reload fail).
2. **Легаси keypair-матч**: на ufo-01/02/03,aeza reality-роль теперь отработает и
   перерендерит config.json ключом из `settings.private_key`. Если он ≠ keypair на ноде →
   клиенты отвалятся. **ПЕРЕД бутстрапом легаси-ноды — сверить** (шаг K ниже).
3. **refresh-reality-dest НЕ ИСПОЛЬЗОВАТЬ** — он реплровижинит сабы (churn). Только
   правка DB + точечный бутстрап.
4. **Диверс-сабы**: после унификации ноды — bulk-rebuild затронутых юзеров (URI на :443).
   ⚠️ R переписывает креды ВСЕХ нод саба — соблюдать инвариант «никто между U и V» (см. шаг R).
5. **Combo-нода (ws-cdn УЖЕ стоит, батч 3)**: без фикса первый unify-бутстрап падал —
   xhttp-роль флашила reload, пока ws-cdn-vhost ещё на :443 (его роль ПОЗЖЕ) → duplicate
   listen → play abort ДО ws-cdn-роли, reality на loopback. **ЗАКРЫТО фиксом reload-handler
   (этот деплой): на combo-unify фейл reload толерируется, ws-cdn-роль ниже доводит nginx до
   консистентного состояния — сходится за один прогон.** Если фикс не доехал в prod-worker —
   симптом тот же, лечение: немедленный второй бутстрап. Валидируется канарейкой №2.
6. **Мина: /probes/targets** (закрыто фиксом public_port в этом деплое) — раньше пробы
   reality шли в 9443 (снаружи closed на unified) → health гнил → нода выпадала из choose_node.
   Теперь бьёт публичный порт. Проверить, что фикс в проде до включения probe-агента.
7. **Warm-пул**: шаг U напрямую в БД НЕ инвалидирует warm-бандлы (:9443 раздастся новым) —
   в шаге U добавлен `invalidate_node_warm_pool`; для уже-унифицированных dc/ufo-01 — ретро.

---

## Пер-нодовая процедура (шаблон)

### Шаг K (ТОЛЬКО легаси-ноды ufo-01/02/03,aeza) — keypair-чек
Сверить приватный ключ reality в config.json ноды с `settings.private_key` в DB:
```
# на ноде:
ansible <node> ... -m shell -a "grep -oE '\"privateKey\": *\"[^\"]*\"' /usr/local/etc/xray/config.json"
# в DB (worker python): cfg.settings['private_key']
```
- Совпадают → безопасно, идём дальше.
- Различаются → НЕ бутстрапить как есть. Сначала синхронизировать
  `settings.private_key` = ключ с ноды (или принять смену keypair + bulk-rebuild ВСЕХ
  юзеров ноды сразу после). Для aeza это было неважно (reality был мёртв) — уже сделано.

### Шаг U — включить унификацию (public_port=443)
```python
# worker python: на reality-конфиге ноды
s = dict(cfg.settings); s["public_port"] = 443; cfg.settings = s
flag_modified(cfg,"settings"); db.commit()
# ⚠️ ОБЯЗАТЕЛЬНО инвалидировать warm-пул — прямая правка settings (в обход API-пути
# PATCH /nodes/.../configs) НЕ дёргает invalidate_node_warm_pool сама → уже прогретые
# бандлы держат старый порт :9443 и раздадутся новым юзерам как есть.
from app.services.warm_pool import invalidate_node_warm_pool
invalidate_node_warm_pool(db, node.id); db.commit()
```

### Шаг W (опц., если добавляем ws-cdn) — создать конфиг + DNS
```
POST /api/nodes/{id}/configs
  {"protocol":"vless-ws-cdn","name":"ws-cdn","sni":"","port":443,"is_enabled":true,"defer_bootstrap":true}
# name — ОБЯЗАТЕЛЕН (без него 422). defer_bootstrap:true — иначе при reconciler ВКЛ
#   POST сам пометит ноду dirty и реконсайлер задиспатчит бутстрап через ~5-8с (ДО шага B),
#   а шаг B доклеит второй ненаблюдаемый прогон. С defer=true бутстрап делаем сами в шаге B.
# пустой sni → _provision_cf_subdomain минтит *.wgse домен + CF DNS A-record на IP ноды
```

### Шаг B — бутстрап (xray v26 + унификация + ws-cdn РАЗОМ)
```python
task,created = orch.create_or_coalesce_node_bootstrap(node, {"reason":"unify+xray+ws"},
                                                      defer_to_reconciler=False)
db.commit(); orch.run_task_async(task, node=node)   # NB: commit ДО enqueue!
```
Ждать success (~3-5 мин). При fail — читать task.error_message + result.stdout.

### Шаг V — верификация (внешне, с nl-web)
```
:443 SNI=<reality-dest>      → серт dest'а (reality-мимикрия)        [reality на 443 ✓]
:443 SNI=<xhttp-домен>       → LE-серт                               [xhttp через stream ✓]
:443 SNI=<ws-домен> (если)   → LE-серт ws                            [ws через stream ✓]
:9443 снаружи                → CLOSED                                [tell убран ✓]
xray version                 → 26.3.27                               [апгрейд ✓]
health_score                 → None (serviceable)
```

### Шаг R — пересобрать URI юзеров ноды на :443
```
POST /api/subscriptions/bulk-rebuild-config {"user_ids":[<юзеры ноды>]}
# затем скан MISMATCH=0
```
⚠️ **R переписывает креды ВСЕХ нод саба, не только текущей** (диверс: у юзера ноды A
креды и на B/C; rebuild перегенерит их по ТЕКУЩЕЙ БД). Поэтому **R гнать ТОЛЬКО когда
НИ ОДНА нода флота не висит между шагом U и успешным V**: иначе соседняя нода, у которой
public_port=443 уже в БД, но бутстрап ещё не прошёл, даст юзеру URI на :443, которого
снаружи нет («reality н/д»). При батчах 2-3 — либо R после того как ВСЯ пачка прошла V,
либо U каждой ноды строго после R предыдущей.

«Юзеры ноды» (готового эндпоинта нет; `/nodes/{id}/users` — по traffic-sample, не годится):
```sql
SELECT DISTINCT d.user_id FROM credentials c JOIN devices d ON d.id=c.device_id WHERE c.node_id=<id>;
```
Скан MISMATCH: скрипт живёт в scratchpad прошлой сессии и сверяет только host — **добавить
сверку порта** (URI-порт == public_port), иначе кред со старым :9443 даст ложный MISMATCH=0.

---

## ✅ Канарейка №1 (легаси-инкрементал) — ПРОЙДЕНА: ufo-ru-01
K keypair-чек → U → B → V (reality:443/9443-closed/xray26) → W ws-cdn → B → V (ws-домен) →
всё внешне зелено. Легаси-фиксы (private_key 0aea0ca + camo_dest 402c145) отработали боем.

## Канарейка №2 (code-fix: combo-batch-3 за один прогон)
Цель — доказать, что xhttp reload-handler guard (этот деплой) сводит **combo-ноду с уже
стоящим ws-cdn** за ОДИН прогон, а не роняет play на первом (см. риск №5 ниже). Берём combo-
ноду с **минимумом юзеров** из батча 3.
1. Убедиться, что backend с фиксами задеплоен (`--tags app`) — иначе в prod-worker старая роль.
2. Выбрать combo-ноду ws-cdn с наименьшим числом держателей кредов:
   ```sql
   SELECT c.node_id, count(DISTINCT d.user_id) u FROM credentials c
   JOIN devices d ON d.id=c.device_id GROUP BY c.node_id ORDER BY u;  -- сверить с ws-cdn нодами
   ```
3. **U** унификация (public_port=443, + invalidate warm-пул) → **B** бутстрап.
4. Наблюдать task: должен пройти за ОДИН прогон (ws-cdn-роль двигает свой vhost на 8443,
   `nginx -t` проходит, reload консистентный). Если фикс НЕ доехал — play упадёт на
   `reload nginx xhttp` (duplicate listen) → лечится немедленным вторым бутстрапом.
5. **V** проверка (reality:443 / ws-домен:443 / xhttp:443 / 9443 closed / xray26).
6. **R** bulk-rebuild юзеров ноды (после V, с учётом инварианта R выше).
🟢 Один прогон, всё зелёное → фикс валиден, катим батч 3.

## Раскатка на флот (после зелёных канареек)
Батчами по 2-3, keypair-чек для легаси, та же процедура, проверка каждой ноды.
**Инвариант R (см. шаг R): не гнать bulk-rebuild, пока в пачке есть нода между U и V.**
- Батч 1 (легаси): ufo-ru-02, ufo-ru-03 (keypair-чек! + пре-чек 8443, см. ниже).
- Батч 2: tw-ru-01, aeza-ru-01 (уже v26+ozon; добить унификацию+ws).
- Батч 3 (combo, уже ws-cdn есть — сходятся за один прогон после фикса): 4vds-ru, vsin-ru, 4vds-dk, vsin-nl.
- avps-ru-01 — по состоянию.

### Пре-чек легаси-нод (ufo-02/03) — shadow-tls на 8443
Роль `install_shadowtls_stack` ВЫКЛючена из site.yml → бутстрап НЕ остановит легаси
shadow-tls, а он биндит `0.0.0.0:8443` (дефолт) — ровно тот loopback-порт, куда унификация
сажает nginx TLS-vhost'ы. Перед бутстрапом:
```
ss -ltnp | grep ':8443'   # если shadow-tls жив на 0.0.0.0:8443 → systemctl stop+disable shadow-tls
```
NB: на нодах с `www.<dest>` проверить рендер stream-unify (дедуп-фикс в коде, но глянуть).
NB2: h2-ALPN на combo-ноде «протекает» на ws-домен (общий сокет 8443 с xhttp `http2`) —
клиентов не ломает (xray ws-dialer = http/1.1), но анти-DPI-замысел ws-шаблона (без h2)
аннулируется. Если решим лечить — снять `http2` из общего listen или развести сокеты.

## Откат ноды
Убрать `public_port` с reality-конфига + бутстрап → роли рендерят non-unified (reality
публично на своём порту, stream-блок удаляется). URI юзеров назад на порт reality
(bulk-rebuild). Как откатывали dc 2026-07-23.

## Что НЕ делаем
- Не гоним пачкой без проверки каждой.
- Не используем refresh-reality-dest.
- Не бутстрапим легаси-ноду без keypair-чека.
