# Расследование: ws-cdn / xhttp «коннект есть — данных нет» + per-protocol CF-домены

> **РАЗВЯЗКА (2026-06-09):** корень — НЕ сервер. CF-проксирование (orange cloud)
> для ws/xhttp мёртвое: RKN DPI режет именно Cloudflare-плечо на 4G (сервер
> доказанно жив — 101 Switching + 2.4 MB downlink). **Решение принято и внедрено:**
> ws-cdn и xhttp переведены на ПРЯМУЮ раздачу — случайный `*.wgse.info` сабдомен,
> DNS-only (grey cloud), нода сама выпускает Let's Encrypt серт (HTTP-01). CF теперь
> только DNS. `grwr.ink` (отдельная xhttp-зона) + Origin CA — ретайрнуты. См. память
> `project_cf_ws_cdn_dead`. Всё ниже — исторический форензик того, как пришли к выводу.

Сессия отладки тест-ноды (`81.90.31.111`, затем чистый загрансервер) + рефактор
CF-фронта под раздельные домены. Дата: 2026-06-08.

---

## 1. С чего началось — упавший прогон site.yml

Прогон роли на ноде падал на хендлере `reload nginx xhttp`:
```
[emerg] open() "/etc/nginx/sites-enabled/xhttp-ka1.grinwer.online.conf" failed (2: No such file or directory)
```

**Корень:** таск `Find existing xhttp vhosts` чистит stale-vhost'ы модулем `find`,
у которого `file_type` по умолчанию = `file` → **симлинки в `sites-enabled`
игнорируются**. `Remove stale` сносил только файл из `sites-available`, а битый
симлинк оставался → `nginx -t` падал → reload падал.

**Фикс (в репо):** `file_type: any` добавлен в `find`-таски:
- `roles/install_vless_xhttp/tasks/main.yml` — `Find existing xhttp vhosts`
- `roles/install_vless_ws_cdn/tasks/main.yml` — `Find existing ws-cdn vhosts` + teardown-find

Ноду вылечили (битый симлинк сносится теперь самой ролью).

---

## 2. Основная проблема — ws/xhttp подключаются, но «ничего не грузит»

Reality работает. ws-cdn и xhttp: коннект есть, пинг 2-3с проходит, трафик не идёт.

### Что проверено и ИСКЛЮЧЕНО (на ноде, фактами)
| Слой | Статус |
|---|---|
| Транспорт CF→nginx→xray | ✅ доставляет (`/ws 101 16894`, `/xh 200 +data` в access-логе) |
| Routing / egress (wg0–wg4) | ✅ все exit'ы отдают, wg4 1MB@2MB/s, реальные сайты грузятся |
| Client-URI vs сервер-конфиг | ✅ совпадают (`provisioning.py:247-314`: `type=ws,path=/ws` / `type=xhttp,path=/xh,mode=auto`) |
| Шаблоны xray/nginx | ✅ каноничны (ревью) |
| MTU / PMTU | ✅ не виноват (xray-сокеты бьются о wg MSS 1380 < 1408) |
| Клиент (v2rayNG + Happ, Android, WiFi+4G) | ✅ одинаковый симптом → не клиент-специфика |
| CF-зона (свежий загрансервер на субдомене wgse.info) | ❌ симптом репродьюсится → причина в shared-слое |

### Сигнатуры ошибок xray (присутствовали ДО любых наших правок)
- `proxy/vless/encoding: failed to read packet length > websocket: close 1006 (abnormal closure): unexpected EOF`
- `proxy/vless/inbound: firstLen = 0 ... failed to read request version` (частично — bare-TCP шум мониторинга на loopback-порт)

### Ресерч (внешние источники)
- **#5918** — `firstLen=0 / no data after TLS` бывает **клиент-специфичным** (iOS Happ/Streisand; на PC/Android тот же конфиг работал). У нас оба Android-клиента падают → этот класс не подходит.
- **1006 unexpected EOF** — [документированное поведение Cloudflare](https://developers.cloudflare.com/network/websockets/): CF молча рвёт **idle-WebSocket на 100с**, и [рандомно закрывает WS 1006](https://community.cloudflare.com/t/cloudflare-randomly-closing-websocket-connections-with-1006/609967). Лечится keepalive-пингами (`heartbeatPeriod`) + длинными таймаутами.
- CF **«оптимизации»** (Rocket Loader / Auto Minify / Polish / Mirage) манглят проксируемый поток → для VLESS-over-CDN их надо OFF.
- Версия xray тоже [ломала VLESS-WS-CDN регрессиями](https://github.com/XTLS/Xray-core/issues/3216) (на ноде v25.6.8).

### ❗ Тупиковая ветка (http2) — добавлено и ОТКАЧЕНО
Гипотеза «origin `listen ... http2` ломает WS-upgrade» оказалась неверной:
CF для WebSocket ходит в origin по HTTP/1.1 **всегда**, а xhttp h2 наоборот нужен
(снятие h2 сломало xhttp). Ошибки VLESS-декода идентичны до/после. **Откатил оба
шаблона обратно на `ssl http2`.** Чистый ноль.

### Что осталось как улучшение (Рычаг B, в репо)
- `config_ws_cdn.json.j2` — `heartbeatPeriod: 30` в `wsSettings` (против CF idle-1006).
- `wscdn-nginx.conf.j2` + `xhttp-vhost.conf.j2` — `proxy_read/send_timeout` 300s → 3600s.

**Статус корня:** НЕ подтверждён. Решающий тест — вынести xhttp на отдельную
чистую CF-зону (`grwr.ink`) и сравнить с ws на `wgse.info`. Под это сделан сплит ↓.

---

## 3. Рефактор: раздельные CF-домены per-protocol (xhttp→grwr.ink, ws→wgse.info)

Заодно починен **латентный баг (вероятное «пересечение»):** обе роли писали
Origin-cert в один файл `/etc/nginx/ssl/wgse-origin.crt`. С разными доменами роли
затирали бы cert друг друга → один протокол отдаёт чужой cert → CF Full(strict) reject.

### Изменённые файлы (всё за флагом — пусто = старое single-zone поведение)
**Backend:**
- `backend/app/services/cloudflare_dns.py` — домен-параметризован: per-zone кэш zone_id, `xhttp_front_domain()`, `create_node_record(domain=)`, `delete_record(domain=)`.
- `backend/app/api/nodes.py` — `_attach_cf_subdomain` выбирает зону по протоколу (ws→`front_domain()`, xhttp→`xhttp_front_domain()`), пишет `cf_front_domain` в settings; `_teardown_cf_subdomain` сносит в той же зоне.
- `backend/app/services/provisioning.py` — xhttp берёт Origin-cert **под зону самого конфига** (новый grwr-конфиг → grwr-cert; legacy wgse-конфиг → wgse-cert), отдельный путь `/etc/nginx/ssl/xhttp-origin.crt`.

**Infra:**
- `roles/install_vless_xhttp/tasks/main.yml` — cert ставится в отдельный файл (через `vless_xhttp_cert_path`), не в общий `wgse-origin.crt`.
- `roles/deploy_app_stack/defaults/main.yml` — `deploy_app_stack_xhttp_front_domain` + `_xhttp_origin_cert/key_b64` (из `vault_xhttp_front_domain` / `vault_grwr_origin_cert/key`).
- `roles/deploy_app_stack/templates/env.j2` — `XHTTP_FRONT_DOMAIN`, `XHTTP_ORIGIN_CERT_B64`, `XHTTP_ORIGIN_KEY_B64`.
- `docker-compose.yml` — те же 3 env-var в backend **и** worker `environment:`.
- `group_vars/web/vault.yml.example` — `vault_xhttp_front_domain` + `vault_grwr_origin_cert/key` (плейсхолдеры; реальный grwr-cert/key — в зашифрованный vault, в репо НЕ клал).

### Активация (xhttp→grwr.ink)
1. **CF**: зона `grwr.ink` активна, SSL = Full(strict), Rocket Loader/Minify/Polish/Mirage = OFF. Токен `vault_cloudflare_api_token` должен иметь `Zone.DNS:Edit` на grwr.ink тоже.
2. **Vault** (`ansible-vault edit infra/ansible/group_vars/web/vault.yml`): `vault_xhttp_front_domain: grwr.ink` + `vault_grwr_origin_cert/key` (PEM блок-скаляры `|`).
3. **Передеплой web**: `ansible-playbook -i inventories/prod/hosts.yml site.yml --tags web --ask-vault-pass`.
4. Создать **новый** xhttp-конфиг (пустой sni) → бэкенд сам выпустит `<rand>.grwr.ink` + поставит grwr-cert.
5. Тест: xhttp на grwr.ink vs ws на wgse.info.

### Не автоматизировано
Origin CA cert grwr.ink выпущен вручную (CF → SSL/TLS → Origin Server), SAN `*.grwr.ink, grwr.ink`.

---

## 4. Открытый вопрос / следующий шаг
Если xhttp на чистой зоне `grwr.ink` оживёт, а ws на `wgse.info` — нет → причина
zone-wide в старой зоне (Rocket Loader/Minify/cache), чистим её. Если и на grwr
падает → CF-зона ни при чём, идём в минимальный bare `vless+ws+tls` конфиг (один
клиент, `/test`, freedom direct, без routing/sniffing) для финального разреза
«обвязка шаблона vs фундамент CF/cert».

## Незакоммиченные правки на момент написания
Всё перечисленное — в рабочем дереве, НЕ закоммичено. Перед коммитом: `ruff check
backend bot`, `ansible-lint infra/ansible`. Деплой только через ansible (`--tags web`).
