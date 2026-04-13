#!/usr/bin/env bash
# Add or remove a VLESS+XHTTP client from xray XHTTP config.
# Usage:
#   manage_vless_xhttp_user.sh add <email> <uuid>
#   manage_vless_xhttp_user.sh del <email>
set -euo pipefail

CONFIG="/usr/local/etc/xray/config_xhttp.json"
INBOUND_TAG="vless-xhttp"
LOCK="/var/lock/manage_vless_xhttp.lock"

usage() { echo "Usage: $0 add <email> <uuid> | del <email>" >&2; exit 1; }

cmd_add() {
  local email="$1" uuid="$2"
  local tmp; tmp=$(mktemp)
  (
    flock -w 30 200
    jq --arg tag "${INBOUND_TAG}" --arg email "${email}" \
       --arg uuid "${uuid}" \
      '(.inbounds[] | select(.tag == $tag) | .settings.clients) |=
        ((map(select(.email != $email))) + [{id: $uuid, email: $email}])' \
      "${CONFIG}" >"${tmp}"
    jq -e . "${tmp}" >/dev/null || { rm -f "${tmp}"; exit 1; }
    mv "${tmp}" "${CONFIG}"; chmod 0640 "${CONFIG}"
  ) 200>"${LOCK}"
  # NO_RESTART=1 skips the restart — caller restarts once after a batch.
  [[ "${NO_RESTART:-}" == "1" ]] || systemctl restart xray-xhttp
  echo "added xhttp user ${email}"
}

cmd_del() {
  local email="$1"
  local tmp; tmp=$(mktemp)
  (
    flock -w 30 200
    jq --arg tag "${INBOUND_TAG}" --arg email "${email}" \
      '(.inbounds[] | select(.tag == $tag) | .settings.clients) |=
        map(select(.email != $email))' \
      "${CONFIG}" >"${tmp}"
    jq -e . "${tmp}" >/dev/null || { rm -f "${tmp}"; exit 1; }
    mv "${tmp}" "${CONFIG}"; chmod 0640 "${CONFIG}"
  ) 200>"${LOCK}"
  [[ "${NO_RESTART:-}" == "1" ]] || systemctl restart xray-xhttp
  echo "removed xhttp user ${email}"
}

[[ $# -ge 1 ]] || usage
case "$1" in
  add) [[ $# -ge 3 ]] || usage; cmd_add "$2" "$3" ;;
  del) [[ $# -ge 2 ]] || usage; cmd_del "$2" ;;
  *) usage ;;
esac
