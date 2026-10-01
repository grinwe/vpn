# Fleet inventory — провайдеры, биллинг, продления

Единая карта: кто где живёт, сколько стоит, когда продлевать. Данные снапшот на **2026-04-24**. Обновляется вручную — при добавлении/удалении сервера, смене тарифа.

> Технический inventory для ansible — [inventories/prod/hosts.yml](../../infra/ansible/inventories/prod/hosts.yml). Этот файл — **биллинг + провайдерская карта + личные VPN оператора**, не источник истины для provisioning'а (им владеет БД `VPNNode` / `WGExitNode` + сам hosts.yml).

> Личные VPN оператора (раздел «Personal») конфигурируются **отдельным** ansible-проектом [vpn-setup](../../../vpn-setup/inventory.yml) — не `infra/ansible`. Не путать.

---

## Сводка

| Категория | Кол-во | Стоимость/мес (RUB) |
|---|---:|---:|
| **Production fleet** |  |  |
| &nbsp;&nbsp;Relay (RU) | 5 | 3 376.55 |
| &nbsp;&nbsp;Exit (non-RU) | 8 | 4 335.10 |
| &nbsp;&nbsp;Mgmt (web+monitoring+db) | 1 | 2 180.85 |
| **Personal (оператора, не флот)** | 3 | 2 409.85 + 1984.hosting TBD |
| **Отпускаем / отменено** | 2 | — (не продлевать) |
| **Итого прод-флот** | **14** | **9 892.50** |
| **С учётом personal** | **17+1** | **~12 300** |

Провайдеры:
- **UFO Hosting** (bill.ufo.hosting) — **баланс 152.27 RUB** ⚠️ не хватает на продление даже одной ноды (мин. 605.85).
- **Aeza** (my.aeza.ru) — баланс «51.00 [б+]» (бонусы/промо).
- **DataCheap** (vps.datacheap.ru) — **баланс 0.00 RUB** ⚠️
- **1984 Hosting** (1984.hosting) — исландский, personal-only. Autorenew ON.

---

## Production fleet

### Relay nodes — RU

Принимают клиентский Reality/xHTTP-трафик, туннелируют через WG в exit'ы. Роли в ansible: `vpn_nodes` группа.

| Node | IP | Провайдер | Тариф | RUB/мес | Продлить до |
|---|---|---|---|---:|---|
| `ru-pq-01` | 171.22.134.110 | UFO | Naos[RU] | 605.85 | 2026-05-16 |
| `ru-pq-02` | 171.22.134.123 | UFO | Haedus[RU] | 710.85 | 2026-05-17 |
| `ru-pq-03` | 171.22.134.124 | UFO | Haedus[RU] | 710.85 | 2026-05-17 |
| `ru-ae-01` | 178.20.208.67 | Aeza (MSK-1) | `ordinary-ivory` | 849.00 | 2026-05-04 |
| `ru-dc-01` | 45.91.53.69 | DataCheap | VPS Start NVMe unlimited (RU) | 500.00 | 2026-05-20 |

**Подытог relay:** 3 376.55 RUB/мес.

### Exit nodes — non-RU (WireGuard)

Terminus трафика. Relay → WG → exit → Internet. Роли в ansible: `wg_exit_nodes` группа.

| Node | IP | Регион | Провайдер | Тариф | RUB/мес | Продлить до |
|---|---|---|---|---|---:|---|
| `kr-pq-01` | 45.136.149.113 | kr | UFO | Naos[KR] | 605.85 | 2026-05-16 |
| `tq-pq-01` | 185.235.243.24 | tq-central | UFO | Naos[TR] | 605.85 | 2026-05-11 |
| `fr-pq-01` | 185.234.64.186 | fr-west | UFO | Naos[FR] | 605.85 | 2026-05-09 |
| `fr-pq-02` | 94.232.247.34 | fr-cent | UFO | Naos[FR] | 605.85 | 2026-05-16 |
| `ur-pq-01` | 146.19.230.21 | uk-cent | UFO | Naos[UK] | 605.85 | 2026-05-09 |
| `uk-pq-02` | 94.131.122.199 | uk-cent | UFO | Naos[UK] | 605.85 | 2026-05-04 |
| `cz-cent` | 103.119.18.55 | cz | DataCheap | VPS Promo NVMe (CZ) | 350.00 | 2026-05-20 |
| `nl-cent-01` | 45.81.35.97 | nl | DataCheap | VPS Promo NVMe (NL) | 350.00 | 2026-05-20 |

**Подытог exit:** 4 335.10 RUB/мес.

> **⚠️ Dual-use:** `uk-pq-02` (94.131.122.199) — помимо роли exit-ноды в флоте, хостит **личный MTProto-прокси** оператора (port 39281, `direct-mtproto` в [vpn-setup/inventory.yml](../../../vpn-setup/inventory.yml)). Трафики не пересекаются (разные порты), но при жёстких действиях на ноде (reboot, firewall) учитывать.

> **☠️ 2026-08-20:** `uk-pq-02` (94.131.122.199), `ur-pq-01` (146.19.230.21) и `fr-pq-02` (94.232.247.34) мертвы (SSH/ICMP молчат, MTProto на 39281 тоже) — вычищены из статик-инвентаря `inventories/prod/hosts.yml`. `fr-pq-01` сменил IP: 185.234.64.186 мёртв, актуальный exit `ufo-fr-01` живёт на 194.59.245.195 (синк с БД). Актуальный источник истины по exit-ам — таблица `wg_exit_nodes` в БД.

### Management

Один физический NL-сервер, в inventory разделён на 3 логические группы (web, monitoring, db) — при росте флота можно раздвоить (начинать с вывода БД).

| Node | IP | Провайдер | Тариф | RUB/мес | Продлить до |
|---|---|---|---|---:|---|
| `nl-web` / `nl-monitoring` / `mgmt-1` | 45.14.244.140 | UFO | Diadem[NL] | 2 180.85 | 2026-05-20 |

> В [vpn-setup/inventory.yml](../../../vpn-setup/inventory.yml) эта же машина упоминается как `nl-server-cdn` (старая CDN-схема VLESS+WS) и `nl-server` (WG-exit для личных VPN оператора). Для прода актуально только её использование как web+monitoring+db. CDN/exit-роль из vpn-setup на проде **не активна**.

**Итого production fleet: 9 892.50 RUB/мес.**

---

## Personal VPN (не флот, операторские)

Серверы оператора для собственного использования и как fallback «когда весь прод-флот недоступен». Конфигурация — отдельный ansible-проект [vpn-setup](../../../vpn-setup/inventory.yml). В [infra/ansible/inventories/prod/hosts.yml](../../infra/ansible/inventories/prod/hosts.yml) **не включены** — чтобы не попали под `site.yml` и автоматические rollout'ы.

| Internal name | IP | Провайдер | Тариф | RUB/мес | Продлить до | Схема |
|---|---|---|---|---:|---|---|
| `my-vps` | 194.76.137.37 | UFO | Naos[DE] | 710.85 | 2026-05-07 | VLESS Reality + Hysteria2 (SNI=computeruniverse.net) |
| `ru-server` | 217.144.184.11 | Aeza (MSK-1) | `awful-blue` | 849.00 | 2026-05-04 | VLESS Reality + WG-client → `nl-server` (SNI=www.asus.com) |
| `vpsoddpyje` | 89.147.111.191 | 1984.hosting | — (autorenew) | 850.00* | 2026-04-30 | **TODO:** добавить в vpn-setup/inventory.yml |

\* 1984.hosting — примерная конвертация ~€7-9/мес → ~750-900 RUB. Точный тариф уточнить в инвойсах.

**Подытог personal:** ~2 410 RUB/мес + 1984.hosting (TBD).

### Что делать с `89.147.111.191`

Сервер оплачен, autorenew включён, но в ansible-пайплайне (`vpn-setup`) его нет — нода фактически не сконфигурирована через IaC. Два варианта:

1. **Добавить в vpn-setup как `reality_nodes`** (по аналогии с `my-vps`): создать запись в `/home/ataradin/work_ai/vpn-setup/inventory.yml`, уникальный `vless_sni` (например `www.github.com`), прогнать `ansible-playbook -i inventory.yml playbook.yml --tags reality -l <name>`.
2. **Оставить как «ручной»** и держать конфиг вне git'а (не рекомендуется — ротация ключей/dest'а превратится в headache через полгода).

---

## Отпускаем / не продлеваем

| IP | Провайдер | RUB/мес | Expiry | Причина |
|---|---|---:|---|---|
| `94.131.121.68` | UFO Naos[RU] | 605.85 | 2026-04-26 | Забанен всеми RU-провайдерами, бесполезен даже как relay. **Autorenew OFF.** |
| DataCheap 224763 | NL Promo NVMe | 350.00 | 2026-04-26 | Заказ без IP (не активировался). Балланс DataCheap 0.00 — истечёт сам. |

Просто не продлевать. Ничего делать не надо.

---

## Renewal watchlist (следующие 30 дней)

Объединённый список **прод-флот + personal** (биллинг-аккаунты общие, за одно пополнение можно закрыть всё по провайдеру).

```
2026-04-26  [drop]        ru-spare-01 (UFO 94.131.121.68)       — отпустить
2026-04-26  [drop]        DataCheap 224763 pending              — отпустить
2026-04-30  [personal]    vpsoddpyje (1984 89.147.111.191)      autorenew ON
2026-05-04  [fleet]       uk-pq-02         (UFO 94.131.122.199)  605.85
2026-05-04  [fleet]       ru-ae-01         (Aeza 178.20.208.67)  849.00  ⚠️ боевой relay
2026-05-04  [personal]    ru-server        (Aeza 217.144.184.11) 849.00
2026-05-07  [personal]    my-vps           (UFO 194.76.137.37)   710.85
2026-05-09  [fleet]       ur-pq-01         (UFO 146.19.230.21)   605.85
2026-05-09  [fleet]       fr-pq-01         (UFO 185.234.64.186)  605.85
2026-05-11  [fleet]       tq-pq-01         (UFO 185.235.243.24)  605.85
2026-05-16  [fleet]       ru-pq-01         (UFO 171.22.134.110)  605.85
2026-05-16  [fleet]       kr-pq-01         (UFO 45.136.149.113)  605.85
2026-05-16  [fleet]       fr-pq-02         (UFO 94.232.247.34)   605.85
2026-05-17  [fleet]       ru-pq-02         (UFO 171.22.134.123)  710.85
2026-05-17  [fleet]       ru-pq-03         (UFO 171.22.134.124)  710.85
2026-05-20  [fleet]       nl-web           (UFO 45.14.244.140)   2180.85
2026-05-20  [fleet]       nl-cent-01       (DataCheap 45.81.35.97)  350.00
2026-05-20  [fleet]       cz-cent          (DataCheap 103.119.18.55) 350.00
2026-05-20  [fleet]       ru-dc-01         (DataCheap 45.91.53.69)   500.00
```

### Минимум для перекрытия следующих 30 дней

Суммируем только то, что продлеваем (без `[drop]`).

| Провайдер | Продлеваемых серверов | Стоимость | Баланс | Надо пополнить |
|---|---:|---:|---:|---:|
| UFO | 12 (11 fleet + 1 personal) | 9 160.20 RUB | 152.27 | **+9 010 RUB** |
| Aeza | 2 (1 fleet + 1 personal) | 1 698.00 RUB | ~51 (бонусы) | **проверить + пополнить** |
| DataCheap | 3 (все fleet) | 1 200.00 RUB | 0.00 | **+1 200 RUB** |
| 1984.hosting | 1 (personal) | ~850 RUB | autorenew ON | — |

**Суммарно ~11 900 RUB** чтобы закрыть май.

---

## Провайдерские аккаунты

| Провайдер | URL | Email | Баланс | Использование |
|---|---|---|---|---|
| UFO Hosting | https://bill.ufo.hosting | adept38@gmail.com | 152.27 RUB | 12 серверов (11 fleet + 1 personal) + 1 drop |
| Aeza | https://my.aeza.ru | — | ~51 (бонусы) | 2 сервера (1 fleet + 1 personal) |
| DataCheap | https://vps.datacheap.ru | grinwer@tutamail.com | 0.00 RUB | 3 fleet + 1 drop |
| 1984 Hosting | https://1984.hosting | adept38@gmail.com (#292550) | — | 1 personal (autorenew) |

---

## Заметки

- **VPS ID у UFO** привязаны к их внутренним `vm15XXXXXX.example.com` FQDN'ам — для поиска в панели провайдера.
- **Дата-центры UFO** шифруются в названии тарифа: `Naos[RU/KR/FR/TR/DE/UK]`, `Haedus[RU]`. `Diadem` — топ-тариф (mgmt).
- **Aeza имена** (`ordinary-ivory`, `awful-blue`) — автогенерируемые. Мапим по IP.
- **DataCheap `grinwer.example.com`** — автодомен, не путать с нашим `grinwer.online`.
- **1984.hosting `vpsoddpyje`** — автогенерируемое имя. Email `adept38@gmail.com` — тот же, что для UFO.
- **Изначально vpn-setup был личным экспериментом оператора** (MTProto, Reality, Hysteria2, CDN-варианты). Из него вырос `infra/ansible` — продакшн-проект VPN-as-a-Service. Два проекта сосуществуют, overlapping server `94.131.122.199` (dual-use) — единственное пересечение.
