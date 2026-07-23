# Reality «н/д»: корневой анализ + утренний ранбук (2026-07-23, ночь)

Сессия ночная, автономная. Прод-**мутации** режет auto-классификатор, юзер спал —
поэтому всё рискованное **застейджено**, ничего вслепую не катал. «Не убил всё».

## TL;DR

- **Корень reality «н/д» найден и пофикшен в коде** (не задеплоено): баг ребилда
  `config_text` — URI кредов пеклись по `subscription.node` вместо ноды самого
  кредо → reality/hy2 диверсных юзеров указывали на чужой IP. **37/97 кредов
  испорчено, 7 юзеров.** Фикс + регресс-тест закоммичены (`2917721`).
- **443-унификация ЖИВА на dc-ru-01** (верифицирована боем). Флот-раскатку НЕ
  трогал — gated, делаем вместе.
- Тег отката: `checkpoint/reality-fixes-20260723` @ `2917721`; до фиксов — `b772b2f`.

## Что сделано ночью (готово, в git на ветке dev)

| Коммит | Что |
|--------|-----|
| `a5c1e0e` | fix(reality): дедуп map-ключа в `stream-unify.conf.j2` — баг, из-за которого reload nginx падал при ре-унификации dc (dest без `www` → дубль ключа → nginx `conflicting parameter`). |
| `2917721` | fix(sub): `rebuild_subscription_config_text` строит по `cfg.node`, а не `subscription.node` + регресс-тест `test_rebuild_diverse_node`. Проверено: **red без фикса, green с** (docker `backend:audit-test2` против audit-pg). |

Ещё: тег `checkpoint/reality-fixes-20260723`. Унификация dc восстановлена и
верифицирована (reality на `:443` через stream ssl_preread, `9443` закрыт снаружи).

## Анализ: почему reality отдавал «н/д» (3 драйвера)

**1. Баг ребилда config_text (ГЛАВНЫЙ, пофикшен).**
`rebuild_subscription_config_text` пёк URI КАЖДОГО кредо через `subscription.node`
(primary-ноду). Диверсная (N×M) подписка держит креды на РАЗНЫХ нодах — билдер
запекал IP primary-ноды во ВСЕ reality/hy2 URI (xhttp уцелевал — доменный).
Скан прода: **37 из 97 активных reality+hy2 кредов** с `config_text host != host
своей ноды`; **7 юзеров: `[1, 3, 4, 1000005, 1000021, 1000023, 1000032]`**; худшие
ноды vsin-nl-01 (11), dc-ru-01 (11), 4vds-dk-01 (10). Ремедиэйшн — ре-ребилд после
деплоя (шаг 3 ниже).
⚠️ Мои же bulk-rebuild'ы в этой сессии (откат+ре-унификация) через этот баг и
подпортили часть кредов — ремедиэйшн обязателен.

**2. aeza-ru-01 (node 10) dest-дрейф.**
Нода реально зеркалит `www.gosuslugi.ru` — а `gosuslugi.ru` **не отдаёт h2**
(проверено openssl) → Reality "target sent incorrect server hello" → reality мёртв.
В БД при этом `sni=www.ozon.ru` (дрейф). Чинить: `dest → ozon.ru` (шаг 2).

**3. asus.com (4vds-dk-01, vsin-nl-01).**
С nl-web `www.asus.com:443` не поднимается вообще (ни default, ни tls1_3). Нужна
**нода-side** проверка достижимости с самих 4vds-dk/vsin-nl (шаг 4). Если и там
битый — сменить dest.

Проверенные РАБОЧИЕ dest'ы (TLS1.3+h2 ✓, из nl-web): `ozon.ru`, `vk.ru`,
`www.yandex.ru`, `www.wildberries.ru`. `ufo-ru-01/02/03` в БД `dest=None`, но ноды
держат `vk.ru`/`www.yandex.ru` (h2 ✓) — reality рабочий, дрейф чисто косметический
в БД. (Ранее в сессии уже пофикшены 4 битых dest: lenta/mail/rutube → ozon/ya/wb/asus.)

## Утренний ранбук (по порядку, SUPERVISED)

Все прод-команды — через `!` (классификатор режет их у меня) или дай permission-rule.

### 0. Бэкап БД (перед всем)
```
! cd /home/ataradin/work_ai/vpn && ./scripts/db_dump.sh --custom
```

### 1. Деплой фиксов (код + шаблон в контейнер)
```
! cd /home/ataradin/work_ai/vpn/infra/ansible && ansible-playbook site.yml --tags app --vault-password-file ~/.vpn_vault_pass
```
`--tags app` перезальёт backend/worker (rebuild-фикс) + обновлённый `stream-unify.conf.j2`
в контейнер. Роли на ноды это НЕ катит (это делает bootstrap при унификации).

### 2. Фикс dest aeza (gosuslugi → ozon.ru)
```
! cd /home/ataradin/work_ai/vpn/infra/ansible && ansible nl-web -i inventories/prod/hosts.yml --vault-password-file ~/.vpn_vault_pass -m script -a '/tmp/claude-1000/-home-ataradin-work-ai-vpn/85dfe6cb-7838-4e5a-ba43-08f5891da298/scratchpad/fix_aeza_dest.sh'
```
Через ~2 мин verify: `openssl s_client -connect 178.20.208.67:9443 -servername ozon.ru`
→ ожидаем `CN=*.ozon.ru` (не gosuslugi).

### 3. Ре-ребилд 7 испорченных юзеров (ПОСЛЕ деплоя!)
```
! cd /home/ataradin/work_ai/vpn/infra/ansible && ansible nl-web -i inventories/prod/hosts.yml --vault-password-file ~/.vpn_vault_pass -m script -a '/tmp/claude-1000/-home-ataradin-work-ai-vpn/85dfe6cb-7838-4e5a-ba43-08f5891da298/scratchpad/remediate_rebuild.sh'
```
Скрипт сам печатает верификацию: `MISMATCH=0` = все креды встали на свои ноды.

### 4. asus.com — нода-side проверка
```
! cd /home/ataradin/work_ai/vpn/infra/ansible && ansible 4vds-dk-01,vsin-nl-01 -i inventories/prod/hosts.yml --vault-password-file ~/.vpn_vault_pass -m shell -a "echo | timeout 8 openssl s_client -connect www.asus.com:443 -servername www.asus.com -tls1_3 -alpn h2 2>/dev/null | grep -iE 'ALPN|Protocol :'"
```
Если пусто (нет h2/недоступен) → сменить dest на рабочий: `refresh-reality-dest`
node 30 (4vds-dk) и/или 27 (vsin-nl) → `vk.ru` или `www.yandex.ru`.

### 5. Тест-ссылка dc (моя проверка в HAPP)
После шага 3 девайс 11230 (юзер 1) станет корректным — reality на dc:443.
Саб: `https://grn-ssync.pro/wwaCl3ZO-wTFAoc64DV_SCOzInmMSM9QX3V2ykVJGMw`
Проверить: reality-entry должен быть `@45.91.53.67:443` (dc), не aeza.

### 6. GATED: флот-унификация 443 — ТОЛЬКО когда всё выше зелёное
НЕ катать вслепую. Обсудить со мной:
- порядок нод, окно reality-down на каждой (секунды при успехе, но при сбое — как на
  dc — reality застревает на loopback, нужно чинить сразу);
- на каждой ноде stream-модуль (`libnginx-mod-stream`) должен встать до bootstrap
  (иначе таймаут apt на RU-нодах);
- пре-проверить рендер `stream-unify.conf.j2` для каждой (dest с/без `www` — фикс
  дедупа уже в коде, но проверить на нодах с `www.*` dest);
- health-порты localhost — ок, probe-блокера НЕТ (проверено на dc).

## Что НЕ делал сознательно
- Флот-унификацию вслепую (только что поймал 2 бага — шаблон + ребилд).
- Любые прод-мутации без присмотра.
- DIVERSE_SUB_NODES не трогал (это не баг флага — баг был в ребилде).

## Staged-скрипты (в scratchpad этой сессии)
- `remediate_rebuild.sh` — ре-ребилд 7 юзеров + verify (шаг 3).
- `fix_aeza_dest.sh` — aeza dest → ozon.ru (шаг 2).
- (команды продублированы инлайн выше — скрипты не обязательны.)
