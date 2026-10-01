# Анти-РКН апгрейды протоколов — план и развилки (handoff)

> **Статус:** планирование, решения владельца ЖДУТСЯ (см. «Открытые развилки»).
> **Дата:** 2026-06-25 (research июнь 2026). **Ветка:** `dev`.
> **Зачем файл:** сессия перезапускается — это self-contained контекст, чтобы поднять работу с нуля. Все file:line проверены против кода на `dev`.

---

## 0. С чего начать (две дешёвые диагностики — ДО любого кода)

Прод-интроспекция мне (Claude) закрыта классификатором → **гоняет владелец** через `!` (`docker compose exec backend python -c`, без кавычек-ломалок). Эти два замера решают срочность/целесообразность всего плана:

1. **Масштаб SNI-коллизий (решает срочность фикса #1-b):**
   ```
   SELECT sni, count(*) FROM vpn_configs WHERE protocol='vless_reality' GROUP BY sni ORDER BY 2 DESC;
   ```
   Много нод на одном `sni` (особенно в одном регионе) = коллизии уже есть.

2. **Гейт #4 — парсит ли HAPP `encryption=mlkem768x25519plus...`** из саб-доставленного `vless://` (1-2 часа ручного теста: поднять VLESS-Enc на тестовой ноде, скормить HAPP). Если HAPP молча дропает параметр → хендшейк **жёстко падает** (mismatch НЕ soft-fail как у Reality) → #4 уходит в чистый radar, код не пишем.

Также опц. аудит для #2: `SELECT DISTINCT sni, port FROM vpn_configs WHERE protocol='vless_reality';` — глазами: нет apple/icloud, всё `:443`.

---

## 1. Два вердикта (главное)

- **#1 (SNI-коллизии vs «сибирская» схема РКН): риск реальный, но СЕЙЧАС дремлет.** Цепочка подтверждена в коде, НО `DIVERSE_SUB_NODES=1` (off) и HAPP-autoconnect за гейтом → у одного клиента в одной сабе пока нет >3 хендшейков к одному SNI. Это **превентивный фикс ПЕРЕД включением diverse-сабы**, не пожар. Сама «сибирская» схема — гипотеза (**доказательность medium**), поэтому только дешёвая гигиена, без дорогой инфры.
- **#4 (VLESS Encryption на CDN-плечи): НЕ катить сейчас.** Клиентская поддержка (HAPP — наш linchpin) не подтверждена; mismatch = жёсткий фейл. Готовить за флагом можно, флипать — только после canary-interop.
- **Лучший ROI прямо сейчас:** Приоритет 3 (аудит #2 + Xray-bump #3) + фикс #1-b. Доказано, дёшево, не зависит от гипотез.

---

## 2. Контекст ресёрча (июнь 2026, что нового в протоколах)

Подтверждено состязательно (3-0 если не указано иное). Наш стек: VLESS-Reality (primary), VLESS-XHTTP, VLESS-WS-CDN (CF **DNS-only**, оранжевое проксирование мертво под РКН), legacy hy2/shadowtls.

**Xray / sing-box / Mihomo:**
- **VLESS Encryption** — новый пост-квант слой (ML-KEM-768 + X25519), PR #5067 (RPRX), мёрж **28.08.2025**, первый релиз **v25.9.5**. Дополняет Reality, не заменяет. Режим **`random`** = полностью случайный вид трафика. Для CDN: «VLESS Encryption + XTLS Vision + XHTTP XMUX».
- **REALITY + ML-DSA-65** пост-квант подпись серта — PR #4915, **v25.7.26 (26.07.2025)**.
- **X25519MLKEM768** гибрид: Mihomo **v1.19.9 (22.05.2025)**; Xray **v26.3.27 (27.03.2026)** — на uTLS Firefox/Safari + ECH `echForceQuery=full`.
- **Xray v26.3.27:** фреймворк обфускации **Finalmask** + **нативный Hysteria2 в самом Xray** (PR #5679); REALITY warning на non-443 порт / Apple-iCloud target (→ бан IP).
- **sing-box 1.12.0 (~04.08.2025):** протокол **AnyTLS** (анти-TLS-in-TLS).
- **SNI-slicing по дефолту** во всех QUIC-инструментах с мая-июня 2025 (quic-go v0.52.0, Hysteria 2.6.2) — legacy hy2 при апдейте ≥2.6.2 получает бесплатно.

**РКН/ТСПУ:**
- **«Сибирская» схема (июнь 2026, реверс П. Осетрова/@hyperion_cs, Habr 1044396):** тихая 120s-деградация (не RST) срабатывает ТОЛЬКО при совпадении ТРЁХ признаков (И, не ИЛИ): (1) подозрительная подсеть/ASN, (2) помеченный TLS-фингерпринт, (3) **>3 параллельных TLS к одному SNI, интервал <350-400мс, за 60с**. ⚠️ **medium — автор сам зовёт это «рабочими гипотезами», варьируется по операторам.**
- **QUIC:** РКН исторически (март 2022) режет `v1 + порт 443 + payload ≥1001B`. Современные SNI-QUIC-обходы **мерены на GFW Китая, для РКН не подтверждены** — нужен локальный тест из РФ.

**Новое:** AmneziaWG 2.0 (март 2026) — активная мимикрия CPS (под DNS/QUIC/SIP); вендорский анонс, против РКН не бенчмаркнут.

**⛔ ОПРОВЕРГНУТО (НЕ делать):**
- Менять uTLS-фингерпринт с `chrome` на firefox/edge/360/qq «из-за JA4» — **решительно опровергнуто (0-3)**.
- Слух «РКН чёрнит целые подсети Selectel/Yandex + эскалация 600с на весь TLS» — **не подтвердился (1-2)**.
- «Последний Xray v26.6.22» — не подтверждён; опираться на v26.3.27.

Источники: github.com/XTLS/Xray-core (PR #5067, #4915, releases v25.7.26/v26.3.27), MetaCubeX/mihomo v1.19.9, sing-box changelog, USENIX Sec 2025 (Zohaib et al.), Habr 1044396, amnezia.org blog.

---

## 3. План по слоям (все file:line проверены)

### Приоритет 1 — SNI-коллизии (#1). Источник коллизий — ТОЛЬКО Reality (ws-cdn/xhttp имеют уникальный `*.wgse`/`.grwr` сабдомен, безопасны).

| Фикс | Где (file:line) | Объём | Риск | Когда |
|---|---|---|---|---|
| **(b) region-distinct выбор SNI** ⭐ | `backend/app/services/node_spawner.py:248-266` `pick_reality_sni` — `used`-запрос (`:260-265`) сделать per-region (join VPNConfig→VPNNode по cc) + исключать SNI, уже занятые в этом cc: `min([s for s in pool if s not in same_region_snis] or pool, key=...)`. Бонус: авто-чинит и `refresh_reality_dest` (`api/nodes.py:2197`). | 0.5-1д | LOW | **делать в любом случае** (гигиена) |
| (a) diverse-pick SNI-aware | `provisioning.py` `_maybe_attach_diverse:2695` + `choose_node:53` — добавить `exclude_snis` (засев из reality-SNI нод в наборе, аналог `used_regions:2758-2764`) + 3-й пасс (region+SNI → SNI → last-resort). | 1-1.5д | MED (горячий `choose_node` — прогнать impact) | только если включаем diverse-сабу |
| (c) отвязать `lowestdelay` от diverse | `backend/app/api_extensions.py:129-131` — новый флаг `SUB_HAPP_LOWESTDELAY`; при diverse не ставить `subscription-autoconnect-type: lowestdelay` (единственный рычаг — значение type, нативного concurrency-cap у HAPP нет). | 0.5д | LOW (нужен UX-ОК) | только с diverse-сабой |
| (d) порядок эндпоинтов в сабе | `api_extensions.py:212-218` / `:274-280` — уникально-SNI ws/xhttp первыми | — | — | **отложить / не делать без запроса** |

Пулы dest: `REALITY_DEST_POOLS` (`node_spawner.py:65-79`) — тонкие `cz/pl/se/ch`=2 домена, РУ=5 (коллизии при РУ-флоте >5 reality-нод). Расширение пулов требует ресёрча доменов на TLS1.3+H2 (комментарий `:62`) — **развилка**.

Не контролируем: конкурентность HAPP url-test проб (попадает ли в <400мс) — серверно не управляется. Это linchpin-неизвестность (память: «HAPP Auto-failover НЕ проверен»).

### Приоритет 2 — VLESS Encryption (#4). ЗА ФЛАГОМ, гейт на HAPP-парсинг (диагностика №2). 6 точек:
- Версия: `infra/ansible/roles/xray_core/defaults/main.yml:5` `v25.6.8 → v26.3.27` (1 место, 3 роли наследуют; `xray-core-fetch.sh:44-52` сам переустановит; **re-bootstrap не нужен**).
- Сервер decryption: `install_vless_ws_cdn/templates/config_ws_cdn.json.j2:56` и `install_vless_xhttp/templates/config_xhttp.json.j2:80` → `{{ ..._decryption | default('none') }}` (дефолт `none` = обратная совместимость).
- Клиент URI: `provisioning.py:260` (ws-cdn) и `:304` (xhttp) — `encryption=` строка из `config.settings`.
- Plumbing: `provisioning.py:498-505` / `:514-519` — добавить `..._decryption` в `extra.update`.
- Ключи: helper в `services/vless.py` (~`:46`) — **shell-out к `xray x25519`/`xray mlkem768`** (меньше зависимостей; `cryptography` не имеет ML-KEM). Генерация в `api/nodes.py:_build_config_from_payload` перед `models.VPNConfig(...)` (`:245`) по образцу reality-ветки (`:214-221`), persist как reality (`node_spawner.py:309-313`, `encrypt()`). Вторая копия пути: `nodes.py:896/:918`.
- Флаг `VLESS_ENC_ENABLED` (default off, как `DIVERSE_SUB_NODES`).

⚠️ Старые клиенты `encryption=none` на ноде с `decryption=mlkem` → **сломаются** (hard-fail) → нужен canary-cohort или синхронный reprovision. Объём 2-3д кода + неопр. interop-canary. **Развилка:** X25519-only старт (для анти-РКН важен `random` on-wire, не PQ-auth) vs сразу ML-KEM.

### Приоритет 3 — быстрые победы ⭐ (лучший ROI)
- **#2 Reality-аудит:** пулы в коде **чисты** (нет apple/icloud, всё `:443`). Проверить прод на legacy/ручные override (диагностика выше). 0.5д, LOW.
- **#3 Xray → v26.3.27:** bump `xray_core/defaults/main.yml:5` + прогон роли (in-place, **не re-bootstrap**), **сначала canary-нода** (v26 мажорная — проверить `xray version` + health + что reality/ws/xhttp поднялись). Закрывает #2-warning И предпосылку #4. 0.5д canary + раскат.
- Комбо: bump до v26.3.27 на canary + аудит dest — один заход, разблокирует #4.

### Радар (#5) — 0 работы
AmneziaWG 2.0 CPS, AnyTLS — мониторить поддержку в HAPP/sing-box. «Сибирская» схема — ждать подтверждённых порогов. Триггер ревизии: любой получает подтверждённую поддержку HAPP.

---

## 4. Открытые развилки (нужно решение владельца)

- [ ] **Прогнать 2 диагностики** (коллизии + HAPP-парсит-mlkem)? — разблокирует всё. *(рекоменд. да)*
- [ ] **Фикс #1-b** (region-distinct SNI) — делать? *(рекоменд. да, гигиена)*
- [ ] **Расширять dest-пулы** (нужен ресёрч доменов TLS1.3+H2) — да/нет?
- [ ] **Включаем `DIVERSE_SUB_NODES>1`?** — если нет, фиксы (a)/(c)/(d) откладываются (dead code).
- [ ] **Xray bump до v26.3.27** на canary — го? какая нода canary?
- [ ] **#4 VLESS-Enc** — кодить за флагом сейчас или ждать HAPP-теста? *(рекоменд. сначала тест)*
- [ ] **#4 auth** — X25519-only старт vs сразу ML-KEM-768? *(рекоменд. X25519-only)*

## 5. Рекомендованный старт
Прогнать диагностику №1 (коллизии) и №2 (HAPP-тест); **параллельно** можно делать фикс #1-b + готовить Xray-bump на canary — дёшево, безопасно, на доказанном (не на гипотезах). #4 и «сибирскую» инфру — за флагом/на радаре до подтверждений.

## Незакоммиченный контекст сессии (на всякий)
До этого плана в этой сессии сделаны и **задеплоены** (но НЕ закоммичены, ветка `dev`): (1) фикс `report_reconnected` — читал `details["users"]` вместо `details["<proto>"]["users"]`, всегда False; (2) кнопка «✅ Всё работает» + `POST /report-ok`. Ранее закоммичены+запушены (2 коммита): бэкстоп авто-закрытия diagnose-инцидентов + `report-broken-device` (per-device failover в боте). PR `dev→main` не поднимали (ждём конца разработки).
