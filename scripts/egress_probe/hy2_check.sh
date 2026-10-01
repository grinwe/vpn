#!/bin/bash
# Только чтение: как hysteria2 на ноде выпускает трафик (туннель или РУ-IP).
echo "== $(hostname) $(date '+%F %T %Z')"
CFG=/etc/hysteria/config.yaml
echo "-- service: $(systemctl is-active hysteria-server 2>/dev/null) since $(systemctl show hysteria-server -p ActiveEnterTimestamp --value 2>/dev/null)"
echo "-- listen:"; ss -ulnp 2>/dev/null | grep -i hysteria | awk '{print "   "$4}' | head -3
echo "-- outbounds/bindDevice:"
grep -nE '^\s*(outbounds|- name|type|bindDevice|direct|mode)\b|bindDevice' "$CFG" 2>/dev/null | sed 's/^/   /' | head -20
# Только правила из секции acl (без комментариев шаблона: на свежей ноде, пока
# manage_hy2_user.sh не перезаписал конфиг yaml.dump-ом, в них тоже есть «(»).
ACL="$(awk '/^acl:/{f=1;next} f && /^[^[:space:]#]/{f=0} f' "$CFG" 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -E '^[[:space:]]*-[[:space:]]*[a-z0-9_-]+\(')"
echo "-- acl: $(printf '%s\n' "$ACL" | grep -c .) rules; first/last:"
printf '%s\n' "$ACL" | awk 'NR<=3{print; next} {last=$0} END{if (last!="") print "   ...\n" last}' | sed 's/^/   /'
echo "-- tls (ожидается cert фронта из /etc/letsencrypt, без acme:):"
grep -A2 -E '^(tls|acme):' "$CFG" 2>/dev/null | sed 's/^/   /' | head -6
echo "-- wg ifaces:"; ip -br link 2>/dev/null | grep -E '^wg' | sed 's/^/   /'
echo "-- hop DNAT rules (udp 20000:40000 -> 443): $(iptables -t nat -S 2>/dev/null | grep -c '20000:40000')"
echo "-- hysteria log (last 30 min, errors/warn):"
journalctl -u hysteria-server --since '-30 min' --no-pager 2>/dev/null | grep -iE 'error|warn|fatal' | tail -5 | cut -c1-220 | sed 's/^/   /'
echo "-- hysteria log lines last 30 min: $(journalctl -u hysteria-server --since '-30 min' --no-pager 2>/dev/null | wc -l)"
true
