# Roadmap: от текущего состояния до MVP и дальше

> Составлен 2026-04-10 на основе внешнего аудита и текущего состояния кодовой базы.
> Аудит проведён по состоянию на начало апреля 2026.

## Текущее состояние проекта

Проект прошёл значительный путь: production-архитектура с warm credential pool,
multi-cloud autoscale, 4-протокольный fallback, monthly billing (V2),
scoped API tokens. Это инфраструктура уровня коммерческого SaaS.

### Что уже закрыто из аудита

| # | Задача из аудита | Статус | Комментарий |
|---|---|---|---|
| 1 | Ansible extra_vars → RCE | ✅ Закрыто | `json.dumps(extra_vars)` inline, нет string interpolation |
| 2 | Telegram initData HMAC | ✅ Закрыто | `_verify_init_data()` с HMAC-SHA256 + auth_date TTL |
| 3 | Webhook verification (Stars, CryptoBot) | ✅ Закрыто | HMAC-SHA256 для CryptoBot, shared secret для Stars |
| 4 | Rate limiting (slowapi) | ✅ Частично | Глобальный лимит 300/min, но нет per-route лимитов |
| 5 | Billing V2 (ежемесячный) | ✅ Закрыто | `renew_subscription()`, `change_plan()`, auto-renew toggle |
| 6 | VLESS URI `encryption=none` | ✅ Закрыто | Добавлен в `_build_vless_reality_credential()` |
| 7 | QR-код в webapp | ✅ Закрыто | `qrcode` пакет подключен, canvas рендеринг |
| 8 | In-place миграция (без задвоения подписок) | ✅ Закрыто | `migrate_subscriptions_off` переделан |

### Что НЕ закрыто

| # | Задача | Приоритет | Трудозатраты |
|---|---|---|---|
| A | Redis: requirepass + ACL | 🔴 Критично | 0.5 дня |
| B | Протоколы: дописать Ansible роли (VLESS+XHTTP, Hysteria2) | 🔴 Критично | 3-4 дня |
| C | CORS explicit origins | 🟡 Средне | 0.5 дня |
| D | Per-route rate limits (auth, topup, activate) | 🟡 Средне | 0.5 дня |
| E | YooKassa → удалить | 🟡 Средне | 1 день |
| F | Docker secrets для SSH key и APP_SECRET_KEY | 🟡 Средне | 0.5 дня |
| G | Экран «Подписка готова» после активации | 🟡 UX | 0.5 дня |
| H | Relay-архитектура (RU jump-нода → WG tunnel → зарубежная нода) | 🔴 Стратегически | 2-3 дня |

---

## Фаза 1: Security Sprint (неделя 1)

### 🔴 A. Redis auth (0.5 дня)

Redis без `requirepass` в docker-compose = потенциальный RCE через RQ pickle deserialization.

**Что сделать:**
- Добавить `requirepass` в redis command в docker-compose.yml
- Обновить `REDIS_URL` на `redis://:password@redis:6379/0`
- Отключить Lua scripting через ACL (`redis-server --rename-command EVAL ""`)
- Обновить Redis до последней 7.x (CVE-2025-49844)

### 🟡 C. CORS explicit origins (0.5 дня)

Сейчас CORS не настроен вообще. Webapp на том же origin → безопасно, но для гигиены:
- Добавить `CORSMiddleware` с `allow_origins=[os.getenv("WEBAPP_ORIGIN")]`

### 🟡 D. Per-route rate limits (0.5 дня)

Глобальный лимит есть (300/min), но auth/topup/activate нужны жёстче:
- `/api/webapp/auth` → 10/min
- `/api/webapp/subscriptions/activate` → 5/min
- `/api/webapp/topup` → 10/min

### 🟡 F. Docker secrets (0.5 дня)

SSH key для Ansible и APP_SECRET_KEY лежат в `.env` / env-переменных.
Перевести на Docker secrets или mounted files.

---

## Фаза 2: Протоколы (неделя 1-2)

### 🔴 B. Приоритетные протоколы — дописать Ansible роли

Выбор протокола на уровне нод уже реализован в бэкенде. Нужны Ansible роли
для трёх приоритетных протоколов:

**Приоритет протоколов:**
1. **VLESS + Reality** — основной (уже работает, допилить: whitelisted SNI, gRPC transport)
2. **VLESS + XHTTP** — основной TCP (новая роль, обход 16KB curtain ТСПУ)
3. **Hysteria2** — fallback (UDP, сложнее для ТСПУ)

ShadowTLS v3 — мёртв (Aparecium, май 2025). Два неустранимых вектора детекции,
последний коммит 11 месяцев назад. Выводим из приоритетных.

**Что сделать:**

1. **Роль `install_vless_xhttp`** (новая, 2 дня):
   - Xray-core с XHTTP transport (мультиплексирует VPN-трафик через HTTP-запросы)
   - Неотличим от веб-браузинга, обходит 16KB curtain ТСПУ
   - Обновить provisioning.py — новый тип ноды `vless_xhttp`

2. **Роль `install_hysteria2`** (новая, 1 день):
   - Port hopping + Salamander obfuscation
   - UDP-based → ТСПУ оптимизирован для TCP

3. **Обновить `install_vless_reality`** (0.5 дня):
   - Whitelisted SNI (не www.asus.com — проверить актуальность)
   - gRPC transport (HTTP/2 multiplexing, устойчивее TCP)

### Добавление нод — ручное

В данный момент ноды добавляются вручную через админку + Ansible.
Aeza — кандидат для автоматизации через API (на будущее), но для MVP
ручного добавления достаточно.

---

## Фаза 3: Relay-архитектура (неделя 2-3)

### 🔴 H. RU jump-нода → WG tunnel → зарубежная нода

Критично в контексте сбора 150₽/ГБ за зарубежный трафик и whitelist-режима ТСПУ.
Трафик через RU jump-ноду остаётся «внутренним», сбор не применяется.

**Архитектура (референс — `/work_ai/vpn-setup/`):**

```
Клиент → VLESS Reality (RU:443) → WireGuard tunnel → Зарубежная нода → Интернет
                                    ↑ сокетный outbound
                                    sockopt.interface: "wg0"
```

**Data flow:**
1. Клиент подключается к RU VPS по VLESS Reality (для ТСПУ — TLS handshake к легитимному SNI)
2. Xray на RU расшифровывает VLESS, отправляет outbound через `wg0` (WireGuard интерфейс)
3. WireGuard на RU шифрует и шлёт UDP-пакеты на зарубежную ноду
4. WireGuard на зарубежной ноде расшифровывает, NAT MASQUERADE → интернет

**Ключевые конфиги (из vpn-setup):**

WireGuard клиент на RU (`Table = off`, чтобы SSH не ходил через туннель):
```
[Interface]
PrivateKey = {{ wg_private_key }}
Address = 10.77.0.X/24
Table = off

PostUp  = ip route add 0.0.0.0/0 dev %i metric 200 || true
PostDown = ip route del 0.0.0.0/0 dev %i metric 200 || true

[Peer]
PublicKey = {{ exit_node_pubkey }}
Endpoint = {{ exit_node_ip }}:51820
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
```

Xray outbound на RU (ключевая строка — `interface: "wg0"`):
```json
{
  "outbounds": [{
    "protocol": "freedom",
    "streamSettings": {
      "sockopt": { "interface": "wg0" }
    }
  }]
}
```

WireGuard сервер на зарубежной ноде:
```
[Interface]
PrivateKey = {{ server_key }}
Address = 10.77.0.1/24
ListenPort = 51820

[Peer]
PublicKey = {{ ru_node_pubkey }}
AllowedIPs = 10.77.0.X/32
```

**Что сделать:**
1. Адаптировать роли из vpn-setup в vpn/infra/ansible:
   - `wg_exit_node` — серверная часть WG на зарубежной ноде
   - Расширить `install_vless_reality` — WG клиент + sockopt outbound
2. Управление ключами: exit-нода генерирует keypair для каждого peer,
   передаёт через Ansible facts (как в vpn-setup)
3. Provisioning: при создании relay-ноды — привязать к exit-ноде,
   авто-настройка WG tunnel при provision_device
4. В UI/модели: relay-нода = обычная нода с типом `vless_reality_relay`,
   юзер не видит разницы

---

## Фаза 4: Операционная готовность (неделя 3)

### 🟡 E. Drop YooKassa (1 день)

YooKassa гарантированно откажет VPN-мерчанту. 65-75% VPN-сервисов уже потеряли
российские платёжные системы.

**Рекомендуемый платёжный стек:**
1. **Telegram Stars** — primary (нативная интеграция, нет юрлица в РФ)
2. **CryptoBot USDT/TON** — secondary (комиссия 1-3%, стимулировать дисконтом 10-15%)
3. **Card-to-card (SBP)** — manual fallback для крупных сумм

Стратегия: Stars для онбординга, CryptoBot для повторных оплат с дисконтом.

### 🟡 G. Экран «Подписка готова» (0.5 дня)

Сейчас после активации — `alert()` в Plans.tsx. Нужен полноценный экран:
- QR сразу открыт (не за кнопкой)
- Конфиг-ссылка с кнопкой «Скопировать»
- Подсказка: «Откройте Hiddify / v2rayNG → отсканируйте QR»

Спецификация в [BILLING_V2.md](BILLING_V2.md#ux-после-оплаты--активации).

---

## Итого до MVP

| Фаза | Задачи | Дней |
|---|---|---|
| Security Sprint | Redis, CORS, per-route limits, Docker secrets | 2 |
| Протоколы | VLESS+XHTTP роль, Hysteria2 роль, Reality обновление | 3-4 |
| Relay-архитектура | WG exit-node роль, RU relay настройка, provisioning | 2-3 |
| Операционная готовность | Drop YooKassa, activation UX screen | 1.5 |
| **Итого** | | **~9-11 дней** |

---

## Фаза 5+: Рост и масштаб (месяц 2-6)

### Месяц 2-3: Масштабирование инфраструктуры
- Aeza API для автоматического добавления нод (если API позволяет)
- Дополнительные cloud-провайдеры (Oracle Free Tier, AWS Lightsail)
- api.py split на модули (Router-based architecture)
- CI/CD pipeline: automated tests → build → deploy

### Месяц 3-4: Рост и удержание
- AmneziaWG как дополнительный протокол
- Автоматический protocol switching (sub link с приоритизацией)
- Push-уведомления через бота (низкий баланс, миграции)
- Fernet key rotation через MultiFernet (90-дневный цикл)

### Месяц 4-6: Устойчивость
- Multi-region probes из 5+ регионов РФ
- Automated protocol rotation при детекции блокировки
- FAQ-бот, knowledge base
- DR: бэкапы базы, конфигов, ключей

### Месяц 6-12: Масштаб
- White-label API для реселлеров (scoped tokens уже есть)
- Geolocation выбор: DE, NL, FI, SE, US, SG
- Traffic analytics для пользователей

---

## Финансовая модель

**Допущения:** цена 199₽/мес, churn 20%, Hetzner 430₽/нода, 40 юзеров/нода,
платёжный микс 60% Stars (комиссия 30%) / 40% CryptoBot (комиссия 2%).
Средняя выручка после комиссий: ~162₽/пользователь.

| Сценарий | Новых/мес | Breakeven | Годовая прибыль |
|---|---|---|---|
| Органический (10/мес) | 10 | Месяц 2-3 | ~30,000₽ |
| Активный маркетинг (50/мес) | 50 | Месяц 1 | ~230,000₽ |
| Вирусный рост (200/мес) | 200 | День 1 | ~1,000,000₽ |

**Ключевые рычаги маржи:**
- Снизить долю Stars с 60% до 30% (стимулировать CryptoBot) → +15-20% revenue
- Увеличить max_users/node с 50 до 80 → -37% себестоимость
- Снизить churn с 20% до 12% (quality of service) → +67% LTV

---

## Killer Risks

| Risk | Вероятность | Митигация |
|---|---|---|
| Whitelist-режим интернета в РФ | Средняя | Relay-архитектура (RU→Foreign) — в плане |
| Сбор 150₽/ГБ международного трафика | Низкая (не подтверждён) | Relay через RU VPS (трафик «внутренний») |
| Блокировка Telegram | Низкая-средняя | Веб-кабинет, QR для sub links |
| Solo developer (bus factor = 1) | Высокая | Максимальная автоматизация, runbooks |
| ТСПУ блокирует все текущие протоколы | Средняя | 3+ протоколов, probes, быстрый rollout |

---

## Порядок работ

1. **Redis auth** — 0.5 дня, закрывает критическую security-дыру
2. **CORS + per-route limits** — 1 день, security hygiene
3. **VLESS+XHTTP Ansible роль** — 2 дня, новый приоритетный протокол
4. **Hysteria2 Ansible роль** — 1 день, UDP fallback
5. **VLESS Reality обновление** — 0.5 дня, whitelisted SNI + gRPC
6. **Relay-архитектура** — 2-3 дня, RU jump-нода + WG tunnel (референс: vpn-setup)
7. **Экран «Подписка готова»** — 0.5 дня, UX fix
8. **Drop YooKassa** — 1 день, убрать юридический риск
