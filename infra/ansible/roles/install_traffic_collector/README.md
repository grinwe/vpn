# install_traffic_collector

Разворачивает на VPN-ноде учёт трафика по пользователям и отправку его в
backend.

## Как это работает

- Одна цепочка iptables `VPN_TRAFFIC`, в ней по две правила на каждого
  юзера: `--dport PORT` (uplink) и `--sport PORT` (downlink), помеченные
  `--comment "vpnuser=<username>;dir=up|down"`.
- Источник истины для списка пользователей — `/etc/shadowtls-ss/users.d/`,
  куда пишет `manage_vpn_user.sh`. Коллектор при каждом запуске:
  1. Сверяет iptables с этим каталогом (добавляет новых, удаляет старых).
  2. Читает счётчики через `iptables -L VPN_TRAFFIC -vnxZ` (zero-and-read).
  3. POST'ит дельты на `/api/nodes/{node_id}/traffic`.
- systemd timer запускает `vpn-traffic-collect.service` раз в минуту.

## Почему iptables, а не xray stats

Текущий стек — ShadowTLS + нативный Shadowsocks (см.
`install_shadowtls_stack`). Там нет xray-core вообще; а у ShadowTLS каждый
юзер сидит на своём порту — значит, port-based accounting в ядре линукса
даёт точный per-user учёт бесплатно. Для будущего VLESS Reality (один
порт, user tag внутри xray) нужно будет параллельно включить xray stats
API — это вне scope этой роли, отдельный кусок работы.

## Переменные

| Имя | Обязательна | Описание |
|---|---|---|
| `traffic_collector_backend_url` | да | База API, e.g. `https://vpn.example.com` |
| `traffic_collector_backend_token` | да | Admin-токен backend'а |
| `traffic_collector_node_id` | да | ID строки `VPNNode` на backend'е. По умолчанию берётся из host-var `node_id` |

## Пример

```yaml
- hosts: vpn_nodes
  become: yes
  roles:
    - install_shadowtls_stack
    - role: install_traffic_collector
      vars:
        traffic_collector_backend_url: https://vpn.example.com
        traffic_collector_backend_token: "{{ vault_admin_token }}"
        traffic_collector_node_id: "{{ hostvars[inventory_hostname].node_id }}"
```

## Чего эта роль НЕ делает

- Не трогает `FORWARD` — ShadowTLS-сервер терминирует соединения локально
  и делает исходящие от своего имени, поэтому INPUT+OUTPUT хватает.
- Не пытается корректно работать, если iptables заменён на nftables.
  ShadowTLS-стек сам ставит правила через legacy iptables, так что на
  live-нодах это совместимо. Миграция на nft — отдельная задача.
- Не дедуплицирует сэмплы при перезапуске. Если таймер запустился дважды
  одновременно, -Z у обоих гонятся на одну цепочку и одна часть сэмплов
  будет зачтена дважды. systemd timer это гарантирует не делать.
