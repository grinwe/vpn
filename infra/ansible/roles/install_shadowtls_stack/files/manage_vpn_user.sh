#!/usr/bin/env bash
#
# Add or remove a ShadowTLS+Shadowsocks client on this node.
#
# Usage:
#   manage_vpn_user.sh add <email> <uuid>
#   manage_vpn_user.sh del <email>
#
# v1 BEHAVIOR — NO-OP WITH AUDIT:
#   All devices on a given node currently share a single ss-rust
#   password and a single shadow-tls password, provisioned by the
#   ansible role from backend-supplied extra_vars. There is no
#   per-user state on the node itself; revocation happens at the
#   backend layer (deleting the Device / Credential row stops that
#   device from being handed out a URI on subscription refresh, and
#   rotating the node's password invalidates everyone who already
#   has one).
#
#   provision_device.yml still invokes this helper for every add/del
#   so the provisioning pipeline stays uniform across protocols. We
#   log each call to /var/log/shadowtls-users.log so operators have
#   a trail of who was granted access at the node level.
#
#
set -euo pipefail

LOG=/var/log/shadowtls-users.log
mkdir -p "$(dirname "$LOG")"
touch "$LOG"
chmod 0640 "$LOG"

usage() {
  cat >&2 <<'USAGE'
Usage:
  manage_vpn_user.sh add <email> <uuid>
  manage_vpn_user.sh del <email>
  manage_vpn_user.sh add-shadowtls-ss <username> <password> <method> <port>   # legacy
  manage_vpn_user.sh del-shadowtls-ss <username> <password> <method> <port>   # legacy
USAGE
  exit 1
}

# The playbook (infra/ansible/playbooks/provision_device.yml) was
# written against the PREVIOUS placeholder script and still calls us
# with the four-arg legacy verbs add-shadowtls-ss / del-shadowtls-ss.
# Accept both shapes here so we don't have to touch the playbook in
# the same change that flipped the role over — the arguments are
# ignored either way (single shared node credential in v1), we just
# log the event for the audit trail.
log_event() {
  local event="$1"; shift
  local ts
  ts=$(date --utc +%Y-%m-%dT%H:%M:%SZ)
  echo "${ts} ${event} $*" >> "$LOG"
}

main() {
  [[ $# -ge 1 ]] || usage
  local command="$1"
  shift
  case "${command}" in
    add)
      [[ $# -eq 2 ]] || usage
      log_event "add" "email=$1" "uuid=$2"
      echo "shadowtls add: noop (shared node credential) — logged"
      ;;
    del)
      [[ $# -eq 1 ]] || usage
      log_event "del" "email=$1"
      echo "shadowtls del: noop (shared node credential) — logged"
      ;;
    add-shadowtls-ss)
      # Legacy signature: add-shadowtls-ss <username> <password> <method> <port>
      [[ $# -eq 4 ]] || usage
      log_event "add-legacy" "user=$1" "method=$3" "port=$4"
      echo "shadowtls add-legacy: noop (shared node credential) — logged"
      ;;
    del-shadowtls-ss)
      [[ $# -eq 4 ]] || usage
      log_event "del-legacy" "user=$1" "method=$3" "port=$4"
      echo "shadowtls del-legacy: noop (shared node credential) — logged"
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
