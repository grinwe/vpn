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

## Пререквизиты (ГОТОВО, задеплоено 2026-07-23)
- ✅ Rebuild-фикс config_text по cfg.node (2917721) — иначе URI бьются на диверсе.
- ✅ Легаси-ключ фикс (0aea0ca) — иначе на ufo-01/02/03,aeza reality-роль скипается.
- ✅ stream-unify.conf.j2 дедуп (a5c1e0e) — иначе nginx reload падает на dest без www.
- ✅ UI-порт (4fd987f).
- ✅ dc-ru-01 — унифицирован и валидирован боем (эталон).
- ✅ Бэкап БД: `backups/vpn-20260723-105552.dump`. Тег `checkpoint/reality-fixes-20260723`.

## Текущее покрытие (на 2026-07-23)
- Унифицировано: **dc-ru-01** (public_port=443).
- ws-cdn ЕСТЬ: 4vds-dk, 4vds-ru, vsin-nl, vsin-ru. НЕТ: aeza, dc, tw, ufo-01/02/03.
- Легаси-схема ключа (нужен keypair-чек перед бутстрапом): **ufo-ru-01/02/03, aeza**.
- xray: часть на 26.3.27, часть на 25.6.8 (апгрейд по бутстрапу).

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
```

### Шаг W (опц., если добавляем ws-cdn) — создать конфиг + DNS
```
POST /api/nodes/{id}/configs  {"protocol":"vless-ws-cdn","sni":"","port":443,"is_enabled":true}
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

---

## Канарейка: ufo-ru-01 (легаси, инкрементально)
1. **K** keypair-чек (легаси!).
2. **U** унификация (public_port=443) → **B** бутстрап → **V** проверка reality:443/9443-closed/xray26.
3. Пауза, убедиться что reality на ufo-ru-01 жив (внешне + в HAPP).
4. **W** добавить ws-cdn → **B** бутстрап → **V** проверка ws-домена.
5. **R** bulk-rebuild юзеров ufo-ru-01.
6. Оператор проверяет в HAPP.
🟢 Всё зелёное → канарейка пройдена.

## Раскатка на флот (после зелёной канарейки)
Батчами по 2-3, keypair-чек для легаси, та же процедура, проверка каждой ноды:
- Батч 1 (легаси): ufo-ru-02, ufo-ru-03 (keypair-чек!).
- Батч 2: tw-ru-01, aeza-ru-01 (уже v26+ozon; добить унификацию+ws).
- Батч 3 (уже ws-cdn есть, только унификация+xray): 4vds-ru, vsin-ru, 4vds-dk, vsin-nl.
- avps-ru-01 — по состоянию.
NB: на нодах с `www.<dest>` проверить рендер stream-unify (дедуп-фикс в коде, но глянуть).

## Откат ноды
Убрать `public_port` с reality-конфига + бутстрап → роли рендерят non-unified (reality
публично на своём порту, stream-блок удаляется). URI юзеров назад на порт reality
(bulk-rebuild). Как откатывали dc 2026-07-23.

## Что НЕ делаем
- Не гоним пачкой без проверки каждой.
- Не используем refresh-reality-dest.
- Не бутстрапим легаси-ноду без keypair-чека.
