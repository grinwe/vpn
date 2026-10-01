#!/bin/bash
# reconcile_xray.sh — apply multi-link outbound/routing state to one xray config.
#
# Args: $1 = path to xray config*.json
# Env:  FAN_OUT  = JSON list [{interface,emails}]
#       PRIMARY  = primary wgN (empty → strip sockopt on default direct)
#
# Exit codes:
#   0   — config changed, notify xray reload
#   78  — config already in desired state, skip reload
#   1   — jq produced invalid JSON or file I/O error
#
# Lives as a plain file instead of inline shell because Ansible 2.17's
# argument splitter choked on the jq DSL + brace groups + jinja in the
# same task. Role task just copies + invokes.
set -euo pipefail

CONFIG="${1:?config path required}"

# Сериализуемся с per-user manage_vless_*_user.sh по ТОМУ ЖЕ локу, что
# держит соответствующий manage-скрипт. Иначе read-modify-write mv ниже
# гоняется с add/del юзера и молча затирает клиента (lost-update).
# Лок выбирается по имени конфига; для незнакомых имён — собственный лок
# (хотя бы сериализует reconcile сам с собой).
case "$(basename "$CONFIG")" in
  config.json)         LOCK="/var/lock/manage_vless_user.lock" ;;
  config_xhttp.json)   LOCK="/var/lock/manage_vless_xhttp.lock" ;;
  config_ws_cdn.json)  LOCK="/var/lock/manage_vless_ws.lock" ;;
  *)                   LOCK="/var/lock/reconcile_$(basename "$CONFIG").lock" ;;
esac
exec 200>"$LOCK"
if ! flock -w 30 200; then
  echo "reconcile_xray: could not acquire lock $LOCK within 30s" >&2
  exit 1
fi

TMP=$(mktemp)

jq --argjson fan_out "${FAN_OUT:-[]}" --arg primary "${PRIMARY:-}" \
   -f /tmp/xray_reconcile.jq "$CONFIG" > "$TMP"

if ! jq -e . "$TMP" > /dev/null; then
  rm -f "$TMP"
  exit 1
fi

if cmp -s "$TMP" "$CONFIG"; then
  rm -f "$TMP"
  exit 78
fi

mv "$TMP" "$CONFIG"
chown root:nogroup "$CONFIG" 2>/dev/null || chown root:root "$CONFIG"
chmod 0640 "$CONFIG"
