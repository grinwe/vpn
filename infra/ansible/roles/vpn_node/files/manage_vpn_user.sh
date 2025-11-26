#!/usr/bin/env bash
set -euo pipefail

# manage_vpn_user.sh - lightweight user provisioning helper for ShadowTLS + Shadowsocks.
#
# This script keeps per-user metadata in /etc/shadowtls-ss/users.d and prepares
# SIP002-compatible ss:// URLs for each user. The underlying ss-server remains a
# single instance configured via /etc/shadowtls-ss/shadowsocks.json.

CONFIG_FILE="/etc/shadowtls-ss/shadowsocks.json"
USERS_DIR="/etc/shadowtls-ss/users.d"

usage() {
  cat >&2 <<USAGE
Usage:
  $0 add-shadowtls-ss <username> <password> <method> <port>
  $0 del-shadowtls-ss <username>

The Shadowsocks service must already be bootstrapped (see shadowtls_power_script.sh).
USAGE
  exit 1
}

require_config() {
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "Shadowsocks config ${CONFIG_FILE} is missing. Run shadowtls_power_script.sh first." >&2
    exit 2
  fi
}

ensure_dirs() {
  mkdir -p "${USERS_DIR}"
}

read_config_value() {
  local field="$1"
  python - "$CONFIG_FILE" "$field" <<'PY'
import json
import sys

config_path = sys.argv[1]
field = sys.argv[2]
try:
    with open(config_path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    value = data.get(field)
    if value is None:
        sys.exit(1)
    print(value)
except FileNotFoundError:
    sys.exit(1)
PY
}

generate_ss_uri() {
  local username="$1"
  local password="$2"
  local method="$3"
  local port="$4"
  local server="$5"

  # SIP002 URI: ss://BASE64(method:password)@server:port#username
  local base64_creds
  base64_creds=$(printf "%s" "${method}:${password}" | base64 -w0)
  echo "ss://${base64_creds}@${server}:${port}#${username}"
}

add_shadowtls_ss_user() {
  local username="$1"
  local password="$2"
  local method="$3"
  local port="$4"

  require_config
  ensure_dirs

  local server
  if ! server=$(read_config_value server); then
    server="0.0.0.0"
  fi

  local user_file="${USERS_DIR}/${username}.conf"
  local ss_uri
  ss_uri=$(generate_ss_uri "${username}" "${password}" "${method}" "${port}" "${server}")

  # Use a temporary file to keep operations idempotent and atomic.
  local tmp_file="${user_file}.tmp"
  cat > "${tmp_file}" <<EOF_USER
# Managed by manage_vpn_user.sh - generated on $(date -Is).
USERNAME="${username}"
PASSWORD="${password}"
METHOD="${method}"
PORT="${port}"
SERVER="${server}"
SS_URI="${ss_uri}"
EOF_USER

  chmod 600 "${tmp_file}"
  mv "${tmp_file}" "${user_file}"
  echo "Provisioned Shadowsocks mapping for user '${username}' at ${user_file}."
}

del_shadowtls_ss_user() {
  local username="$1"
  local user_file="${USERS_DIR}/${username}.conf"

  if [[ -f "${user_file}" ]]; then
    rm -f "${user_file}"
    echo "Removed Shadowsocks mapping for user '${username}'."
  else
    echo "No Shadowsocks mapping found for user '${username}'."
  fi
}

main() {
  if [[ $# -lt 2 ]]; then
    usage
  fi

  local action="$1"
  shift

  case "${action}" in
    add-shadowtls-ss)
      if [[ $# -ne 4 ]]; then
        usage
      fi
      add_shadowtls_ss_user "$@"
      ;;
    del-shadowtls-ss)
      if [[ $# -ne 1 ]]; then
        usage
      fi
      del_shadowtls_ss_user "$1"
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
