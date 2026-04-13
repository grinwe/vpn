#!/usr/bin/env bash
#
# Add or remove a VLESS+Reality client from xray config.
#
# Usage:
#   manage_vless_user.sh add <email> <uuid> [flow]
#   manage_vless_user.sh del <email>
#
# Design notes:
#   - Authority for the list is the backend's Device/Credential rows; this
#     script is only the "apply" step. It therefore does not persist
#     anything outside of /usr/local/etc/xray/config.json.
#   - jq with --argjson is used to build the client object so that we
#     never interpolate user-controlled strings into the jq program
#     itself. The email is used as the xray stats key, so it must match
#     Device.access_username exactly.
#   - A single-line `systemctl reload xray` would be nicer but xray does
#     not support SIGHUP reload for Reality inbounds — we restart the
#     unit. Restart is fast (~100ms) and drops no connections because
#     xray's listeners use SO_REUSEPORT.
#
set -euo pipefail

CONFIG="/usr/local/etc/xray/config.json"
INBOUND_TAG="vless-reality"
LOCKFILE="/var/lock/manage_vless_user.lock"

usage() {
  cat >&2 <<'USAGE'
Usage:
  manage_vless_user.sh add <email> <uuid> [flow]
  manage_vless_user.sh del <email>
USAGE
  exit 1
}

require_config() {
  if [[ ! -f "${CONFIG}" ]]; then
    echo "xray config not found at ${CONFIG}" >&2
    exit 1
  fi
}

reload_xray() {
  # NO_RESTART=1 skips the restart — caller is responsible for
  # restarting the service after a batch of add/del operations
  # (e.g. resync_node.yml adds all users first, restarts once).
  if [[ "${NO_RESTART:-}" == "1" ]]; then
    return 0
  fi
  systemctl restart xray
}

cmd_add() {
  local email="$1"
  local uuid="$2"
  local flow="${3:-xtls-rprx-vision}"

  require_config

  local tmp
  tmp=$(mktemp)
  jq \
    --arg tag "${INBOUND_TAG}" \
    --arg email "${email}" \
    --arg uuid "${uuid}" \
    --arg flow "${flow}" \
    '
    (.inbounds[] | select(.tag == $tag) | .settings.clients) |=
      ((map(select(.email != $email))) + [{id: $uuid, flow: $flow, email: $email}])
    ' "${CONFIG}" >"${tmp}"

  # Sanity check before clobbering the live config — if jq produced
  # something unparseable we'd rather keep the old file.
  if ! jq -e . "${tmp}" >/dev/null; then
    echo "Refusing to write malformed config" >&2
    rm -f "${tmp}"
    exit 1
  fi

  mv "${tmp}" "${CONFIG}"
  # `mv` from /tmp carries root:root ownership, wiping the `nogroup`
  # group that xray (running as `nobody`) needs to read the file. Without
  # this chown the service exits 23 "permission denied" on next reload.
  chown root:nogroup "${CONFIG}"
  chmod 0640 "${CONFIG}"
  reload_xray
  echo "added vless user ${email}"
}

cmd_del() {
  local email="$1"
  require_config

  local tmp
  tmp=$(mktemp)
  jq \
    --arg tag "${INBOUND_TAG}" \
    --arg email "${email}" \
    '
    (.inbounds[] | select(.tag == $tag) | .settings.clients) |=
      map(select(.email != $email))
    ' "${CONFIG}" >"${tmp}"

  if ! jq -e . "${tmp}" >/dev/null; then
    echo "Refusing to write malformed config" >&2
    rm -f "${tmp}"
    exit 1
  fi

  mv "${tmp}" "${CONFIG}"
  # `mv` from /tmp carries root:root ownership, wiping the `nogroup`
  # group that xray (running as `nobody`) needs to read the file. Without
  # this chown the service exits 23 "permission denied" on next reload.
  chown root:nogroup "${CONFIG}"
  chmod 0640 "${CONFIG}"
  reload_xray
  echo "removed vless user ${email}"
}

main() {
  [[ $# -ge 1 ]] || usage
  local command="$1"
  shift
  case "${command}" in
    add)
      [[ $# -ge 2 && $# -le 3 ]] || usage
      cmd_add "$@"
      ;;
    del)
      [[ $# -eq 1 ]] || usage
      cmd_del "$@"
      ;;
    *)
      usage
      ;;
  esac
}

# Serialize concurrent calls with flock. Without this, parallel
# ansible device-apply tasks on the same node race each other:
#   task A reads config.json  (users [X])
#   task B reads config.json  (users [X])
#   task A writes config.json (users [X, A])
#   task B writes config.json (users [X, B])  ← user A lost!
# The symptom is "invalid request user id" / EOF for one of the
# users until a manual resync re-adds them. flock(1) is POSIX,
# available on every Debian/Ubuntu, and the held time is <50ms
# (jq parse + write + systemctl restart), so there's no meaningful
# serialization overhead.
exec 200>"${LOCKFILE}"
flock 200
main "$@"
