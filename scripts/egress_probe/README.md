# egress_probe — реальный выход нод и что видят сервисы

Клиентская проверка «куда на самом деле выходит трафик» по каждой ноде и протоколу,
и какую страну при этом видят Google и OpenAI. curl напрямую к ноде тут бесполезен:
на 443 ответит маскировка. Запрос надо пустить **сквозь** протокол, поэтому локально
поднимается клиент (xray для vless-*, hysteria для hy2) с SOCKS-портом.

Написано по итогам разбора 2026-09-29 (жалобы «на одном протоколе Gemini видит
Россию»): утечек не нашлось, зато Google геолоцировал exit-ы `dc-cz-01`, `dc-nl-01`
и ноду `4vds-dk-01` как RU. Чек-лист, где это используется:
[docs/operations/node_exit_acceptance_checklist.md](../../docs/operations/node_exit_acceptance_checklist.md).

## Порядок

Скрипты — здесь (`P`), данные — ВНЕ репо (`O`): в выгрузках креды, а деплой
rsync-ает рабочее дерево целиком и `.gitignore` не читает (страховочный
exclude для `scripts/egress_probe/*.json|*.jsonl|*.out` в роли `deploy_app_stack`
есть, но данные сюда класть не надо).

```bash
P=$PWD/scripts/egress_probe          # из корня репо
O=~/.cache/vpn-egress-probe/out; mkdir -p "$O"; cd "$O"
$P/fetch_clients.sh                  # клиенты тех же версий, что на нодах (из дефолтов ролей)

$P/admin_api.py GET /api/nodes > nodes.json
$P/admin_api.py GET /api/exits > exits.json

# тест-устройство на подписке владельца (sub 21): 4 ноды × 4 протокола
$P/admin_api.py POST /api/subscriptions/21/devices '{}' > newdev.json   # → target_id = device id
$P/admin_api.py GET /api/users/1 > user1.json

$P/collect_creds.py --user user1.json --nodes nodes.json --sub 21 --device <id> > creds.json
# остальные ноды — простаивающими устройствами той же подписки (живые не берутся)
$P/collect_creds.py --user user1.json --nodes nodes.json --sub 21 --idle-cover --exclude-device <id> > creds2.json

$P/egress_probe.py creds.json > probe.jsonl
$P/show_probe.py --nodes nodes.json --exits exits.json probe.jsonl

# взгляд сервисов: reality-креды; у RU relay один девайс = один её exit,
# все exit-ы relay — --all-idle (с дублями) или switch-exit тест-устройства
$P/collect_creds.py ... --only-protos vless-reality > reality_view.json
$P/service_view_probe.py reality_view.json

$P/admin_api.py POST /api/devices/<id>/revoke '{}'    # убрать тест-устройство
```

## Что меряется

| Скрипт | Что | Норма |
|---|---|---|
| `egress_probe.py` tcp | `cloudflare.com/cdn-cgi/trace` через туннель | RU relay-нода → IP её exit-а; зарубежная → сама нода |
| `egress_probe.py` udp | STUN (stun.cloudflare.com:3478) сквозь туннель: dokodemo-door у xray, udpForwarding у hysteria | тот же выход, что tcp (иначе QUIC течёт) |
| `egress_probe.py` v6 | `https://[2606:4700:4700::1111]/cdn-cgi/trace` | `ERR …unexpected eof while reading` = v6-выхода нет, утечки нет (ERR в v6 — норма; ERR в tcp/openai/udp — нога не работает) |
| `egress_probe.py` openai | `chatgpt.com/cdn-cgi/trace` | страна не RU (это гео **Cloudflare**, не OpenAI) |
| `service_view_probe.py` yt_GL | YouTube `"GL":"XX"` — страна по мнению Google | не RU (иначе Gemini-приложение «недоступно в стране») |
| `service_view_probe.py` gemini | Gemini API с фейковым ключом | `ok (400)` (= «API key not valid»); `🔴 BLOCKED` = «location is not supported»; `?` — разобрать тело |
| `service_view_probe.py` openai | `api.openai.com/v1/models` без ключа | `ok (401)`; `🔴 BLOCKED` = `unsupported_country_region_territory` |
| `hy2_check.sh` | на ноде: `bindDevice`, ACL, tls, listen, DNAT-хоп, логи hysteria | (RU relay) `bindDevice: wgN`, ACL заканчивается `tunnel(all)`; (direct) без bindDevice, ACL `reject(geoip:private)`, `local(all)`; tls — cert фронта, без `acme:`; хоп-правило есть |

Кросс-чек публичных геобаз (без туннеля): `ip-api.com/json/<ip>`, `ipinfo.io/<ip>/json`,
`api.country.is/<ip>` (≈ MaxMind GeoLite). Они расходятся между собой, решает взгляд
самого сервиса.

`hy2_check.sh` запускается на нодах через ansible:

```bash
cd infra/ansible
ansible all -i "<ip1>,<ip2>," -u root --ssh-common-args="-i ~/.ssh/vpn_provisioning_ed25519" \
  -m script -a ../../scripts/egress_probe/hy2_check.sh > ~/.cache/vpn-egress-probe/out/hy2_check.out 2>&1 < /dev/null
```

## Грабли

- **Кто запускает.** Агентской сессии классификатор не даёт: исполнять скачанные
  бинари («Code from External»), писать в админ-API («Modify Shared Resources»),
  ходить ansible на ноды («Production Reads»). Это делает оператор (в Claude Code:
  `!`, вывод в файл, `< /dev/null`, иначе ansible падает на non-blocking IO).
- **WSL и прокси.** `HTTPS_PROXY`/`TELEPORT_*` из окружения вычищаются (`CLEAN_ENV`),
  иначе curl идёт мимо SOCKS. GitHub через прокси качается медленно,
  `fetch_clients.sh` докачивает.
- **hy2 из WSL** не коннектится ни к одной ноде («no recent network activity»), хотя
  QUIC до Google ходит, а с ПК оператора hy2 работает. Hy2 проверять `hy2_check.sh`
  или с телефона: hy2-нога + `https://chatgpt.com/cdn-cgi/trace`.
- **Cloudflare trace обманчив** для вопроса «видит ли сервис РФ»: смотреть YouTube GL
  и Gemini API.
- Простаивающими устройствами подписки владельца пробовать можно (sharing-энфорсер
  в проде выключен), устройства со свежим `last_seen_at` не трогать.
