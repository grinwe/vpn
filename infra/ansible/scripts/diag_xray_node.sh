#!/usr/bin/env bash
# diag_xray_node.sh — выяснить, сколько xray-инстансов реально крутится
# на ноде и какие конфиг-файлы они читают. Запускать НА САМОЙ НОДЕ:
#
#   scp scripts/diag_xray_node.sh root@<node>:/tmp/
#   ssh root@<node> bash /tmp/diag_xray_node.sh
#
# Не требует прав кроме root + установленных xray/jq/systemctl.

set -u

hr() { printf '\n=== %s ===\n' "$*"; }

hr "Содержимое /usr/local/etc/xray/"
ls -la /usr/local/etc/xray/ 2>/dev/null || echo "(нет директории)"

hr "Содержимое /etc/xray/ (на случай альтернативного пути)"
ls -la /etc/xray/ 2>/dev/null || echo "(нет директории)"

hr "Все xray-юниты systemd"
ls /etc/systemd/system/xray*.service \
   /lib/systemd/system/xray*.service \
   /usr/lib/systemd/system/xray*.service 2>/dev/null

hr "ExecStart из всех найденных xray-юнитов"
for f in /etc/systemd/system/xray*.service \
         /lib/systemd/system/xray*.service \
         /usr/lib/systemd/system/xray*.service; do
  [ -f "$f" ] || continue
  echo "-- $f --"
  grep -E '^ExecStart' "$f"
done

hr "Запущенные xray-процессы (с полным cmdline)"
ps -eo pid,user,args | grep -E '/xray\b' | grep -v grep

hr "Статус всех xray*.service"
systemctl list-units --type=service --all | grep -i xray

hr "Числа: inbound'ов / clients / user-emails в каждом config-файле"
for f in /usr/local/etc/xray/*.json /etc/xray/*.json; do
  [ -f "$f" ] || continue
  echo "-- $f --"
  jq -r '
    .inbounds // [] | map({
      tag,
      port,
      clients_total: (.settings.clients // [] | length),
      user_clients: ([.settings.clients[]? | select(.email | startswith("user-"))] | length),
      warm_clients: ([.settings.clients[]? | select(.email | startswith("warm-"))] | length),
      other_clients: ([.settings.clients[]? | select((.email // "") | (startswith("user-") or startswith("warm-")) | not)] | length)
    })
  ' "$f" 2>/dev/null || echo "(не парсится jq)"
done

hr "Уникальные user-* emails в access-логах за последний час (если есть)"
for log in /var/log/xray/access*.log /var/log/xray/*.log; do
  [ -f "$log" ] || continue
  echo "-- $log --"
  # access-log xray пишет email в формате "email: user-..." на каждой строке connection-accept
  grep -oE 'email: user-[0-9]+-[0-9]+-[0-9]+-[a-f0-9]+' "$log" 2>/dev/null \
    | sort -u | head -40 | sed 's/^/  /'
  total=$(grep -cE 'email: user-' "$log" 2>/dev/null || echo 0)
  echo "  (total user-connections in this log: $total)"
done

hr "Сейчас открытые TCP-соединения на xray-портах (9443, 10443, 8443)"
ss -tn state established '( sport = :9443 or sport = :10443 or sport = :8443 )' 2>/dev/null \
  | head -20

hr "Готово"
