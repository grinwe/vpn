#!/usr/bin/env bash
# Add or remove a VLESS+WS+CDN client from xray WS config.
# Usage:
#   manage_vless_ws_user.sh add <email> <uuid>
#   manage_vless_ws_user.sh del <email>
set -euo pipefail

CONFIG="/usr/local/etc/xray/config_ws_cdn.json"
INBOUND_TAG="vless-ws-cdn"

usage() { echo "Usage: $0 add <email> <uuid> | del <email>" >&2; exit 1; }

cmd_add() {
  local email="$1" uuid="$2"
  local tmp; tmp=$(mktemp)
  jq --arg tag "${INBOUND_TAG}" --arg email "${email}" \
     --arg uuid "${uuid}" \
    '(.inbounds[] | select(.tag == $tag) | .settings.clients) |=
      ((map(select(.email != $email))) + [{id: $uuid, email: $email}])' \
    "${CONFIG}" >"${tmp}"
  jq -e . "${tmp}" >/dev/null || { rm -f "${tmp}"; exit 1; }
  mv "${tmp}" "${CONFIG}"; chmod 0640 "${CONFIG}"
  systemctl restart xray-ws-cdn
  echo "added ws-cdn user ${email}"
}

cmd_del() {
  local email="$1"
  local tmp; tmp=$(mktemp)
  jq --arg tag "${INBOUND_TAG}" --arg email "${email}" \
    '(.inbounds[] | select(.tag == $tag) | .settings.clients) |=
      map(select(.email != $email))' \
    "${CONFIG}" >"${tmp}"
  jq -e . "${tmp}" >/dev/null || { rm -f "${tmp}"; exit 1; }
  mv "${tmp}" "${CONFIG}"; chmod 0640 "${CONFIG}"
  systemctl restart xray-ws-cdn
  echo "removed ws-cdn user ${email}"
}

[[ $# -ge 1 ]] || usage
case "$1" in
  add) [[ $# -ge 3 ]] || usage; cmd_add "$2" "$3" ;;
  del) [[ $# -ge 2 ]] || usage; cmd_del "$2" ;;
  *) usage ;;
esac
