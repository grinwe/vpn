#!/usr/bin/env bash
#
# Add or remove a VLESS+Reality client from xray config.
#
# Usage:
#   manage_vless_user.sh add <email> <uuid> [flow]
#   manage_vless_user.sh del <email>
#
# Env:
#   EXIT_INTERFACE=wgN   — G.6 (multi-link relays). When set, add
#                          attaches the email to the xray routing rule
#                          targeting ``direct-wgN`` (creating the rule
#                          if absent). Del always strips the email from
#                          every direct-wg* rule. Unset means fall back
#                          to the default ``direct`` outbound.
#   NO_RESTART=1         — skip systemctl restart (batch mode).
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

# G.6 — keep routing rules in sync with the clients[] edit. Both add
# and del strip the email from every ``direct-wg*`` rule first; add
# then appends the email to the rule matching ``$EXIT_INTERFACE``
# (creating the rule if it doesn't exist yet). No-op when the env var
# is unset: unmatched users fall through to the default ``direct``
# outbound (which the role points at primary wgN on single-link
# relays and at plain egress on direct nodes).
rewrite_routing() {
  local email="$1"
  local action="$2"            # "add" | "del"
  local iface="${EXIT_INTERFACE:-}"

  # Preserve template-rendered routing state on add-without-iface.
  # resync_node.yml re-adds every user after a site.yml template
  # re-render; if we unconditionally stripped emails here, every
  # resync would wipe the routing rules the template just wrote.
  # Del always runs — removing a user should clean up every rule.
  if [[ "${action}" == "add" && -z "${iface}" ]]; then
    return 0
  fi

  local tmp
  tmp=$(mktemp)
  jq \
    --arg email "${email}" \
    --arg iface "${iface}" \
    --arg action "${action}" \
    '
    # Strip email from every direct-wg* rule, drop any rule left with
    # an empty user list so detach of the last user cleans up the rule.
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
    # On add with a target interface, ensure the email is in the
    # matching rule; create the rule if missing.
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

  if ! jq -e . "${tmp}" >/dev/null; then
    echo "Refusing to write malformed config (routing pass)" >&2
    rm -f "${tmp}"
    exit 1
  fi
  mv "${tmp}" "${CONFIG}"
  chown root:nogroup "${CONFIG}"
  chmod 0640 "${CONFIG}"
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
  rewrite_routing "${email}" "add"
  # reload_xray is deliberately NOT called here — the restart runs
  # outside the flock section (see bottom of file) so a hung
  # systemctl can't wedge the lock for every subsequent invocation.
  echo "added vless user ${email}${EXIT_INTERFACE:+ via ${EXIT_INTERFACE}}"
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
  rewrite_routing "${email}" "del"
  # reload_xray runs outside the flock section (see bottom of file).
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
# (jq parse + write), so there's no meaningful serialization overhead.
#
# Two invariants (mirrors manage_vless_xhttp_user.sh):
#   1. `-w 30` bounds the wait — a stuck lock holder makes this call
#      fail (exit 1) instead of hanging forever. ansible's command
#      module has no timeout of its own, so an unbounded wait would
#      block the playbook and, with it, a provisioning-orchestrator
#      worker slot (they're semaphore-limited).
#   2. The `systemctl restart xray` (reload_xray) runs OUTSIDE the
#      locked section. Restart under the lock means one hung
#      systemctl wedges every subsequent invocation on the node.
(
  flock -w 30 200 || { echo "manage_vless_user: lock timeout after 30s" >&2; exit 1; }
  main "$@"
) 200>"${LOCKFILE}"
reload_xray
