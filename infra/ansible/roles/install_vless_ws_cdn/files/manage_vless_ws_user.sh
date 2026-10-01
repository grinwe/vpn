#!/usr/bin/env bash
# Add or remove a VLESS+WS+CDN client from xray WS config.
# Usage:
#   manage_vless_ws_user.sh add <email> <uuid>
#   manage_vless_ws_user.sh del <email>
# Env:
#   EXIT_INTERFACE=wgN — G.6. Attach email to the direct-wgN routing
#                        rule on add (creates it if absent); del always
#                        strips email from every direct-wg* rule.
#   NO_RESTART=1       — skip systemctl restart (batch mode).
set -euo pipefail

CONFIG="/usr/local/etc/xray/config_ws_cdn.json"
INBOUND_TAG="vless-ws-cdn"
# Сериализация параллельных вызовов через flock — тот же паттерн, что в
# manage_vless_user.sh / manage_vless_xhttp_user.sh: без лока два
# параллельных provision_device читают конфиг одновременно и запись
# одного затирает юзера другого (lost update).
LOCK="/var/lock/manage_vless_ws.lock"

usage() { echo "Usage: $0 add <email> <uuid> | del <email>" >&2; exit 1; }

# G.6 — mirrored from manage_vless_user.sh. See that file for the
# full rationale; same jq program, different CONFIG path.
rewrite_routing() {
  local email="$1" action="$2" iface="${EXIT_INTERFACE:-}"
  # See manage_vless_user.sh for the full rationale — add without
  # a target iface must be a no-op so resync doesn't wipe routing.
  if [[ "${action}" == "add" && -z "${iface}" ]]; then
    return 0
  fi
  local tmp; tmp=$(mktemp)
  jq --arg email "${email}" --arg iface "${iface}" --arg action "${action}" \
    '
    .routing.rules =
      ( .routing.rules
        | map(
            if (.outboundTag // "") | startswith("direct-wg")
            then .user = ((.user // []) - [$email])
            else .
            end
          )
        | map(select(
            ((.outboundTag // "") | startswith("direct-wg") | not)
            or ((.user // []) | length) > 0
          ))
      )
    |
    if $action == "add" and $iface != "" then
      (.routing.rules
        | map(select(.outboundTag == ("direct-" + $iface)))
        | length
      ) as $has
      | if $has > 0 then
          .routing.rules = (.routing.rules | map(
            if .outboundTag == ("direct-" + $iface)
            then .user = (((.user // []) + [$email]) | unique)
            else .
            end
          ))
        else
          .routing.rules += [{
            "type": "field",
            "user": [$email],
            "outboundTag": ("direct-" + $iface)
          }]
        end
    else . end
    ' "${CONFIG}" >"${tmp}"
  jq -e . "${tmp}" >/dev/null || { rm -f "${tmp}"; exit 1; }
  mv "${tmp}" "${CONFIG}"; chmod 0640 "${CONFIG}"
}

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
    rewrite_routing "${email}" "add"
  ) 200>"${LOCK}"
  # NO_RESTART=1 skips the restart — caller restarts once after a batch.
  [[ "${NO_RESTART:-}" == "1" ]] || systemctl restart xray-ws-cdn
  echo "added ws-cdn user ${email}${EXIT_INTERFACE:+ via ${EXIT_INTERFACE}}"
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
    rewrite_routing "${email}" "del"
  ) 200>"${LOCK}"
  [[ "${NO_RESTART:-}" == "1" ]] || systemctl restart xray-ws-cdn
  echo "removed ws-cdn user ${email}"
}

[[ $# -ge 1 ]] || usage
case "$1" in
  add) [[ $# -ge 3 ]] || usage; cmd_add "$2" "$3" ;;
  del) [[ $# -ge 2 ]] || usage; cmd_del "$2" ;;
  *) usage ;;
esac
