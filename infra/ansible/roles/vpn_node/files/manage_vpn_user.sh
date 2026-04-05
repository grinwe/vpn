#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="/etc/shadowtls-ss/shadowsocks.json"
USERS_DIR="/etc/shadowtls-ss/users.d"

usage() {
  cat >&2 <<'USAGE'
Usage:
  manage_vpn_user.sh add-shadowtls-ss <username> <password> <method> <port>
  manage_vpn_user.sh del-shadowtls-ss <username>
USAGE
  exit 1
}

require_config() {
  if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "Shadowsocks config not found at ${CONFIG_PATH}" >&2
    exit 1
  fi
}

ensure_users_dir() {
  mkdir -p "${USERS_DIR}"
}

get_server_ip() {
  local ip
  ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
  if [[ -z "${ip}" ]]; then
    echo "Unable to determine server IPv4 address" >&2
    exit 1
  fi
  echo "${ip}"
}

add_shadowtls_ss() {
  local username password method port
  username=$1
  password=$2
  method=$3
  port=$4

  require_config
  ensure_users_dir

  local user_file="${USERS_DIR}/${username}.conf"
  cat >"${user_file}" <<EOFUSER
METHOD=${method}
PASSWORD=${password}
PORT=${port}
EOFUSER

  local server_ip
  server_ip=$(get_server_ip)
  local ss_payload="${method}:${password}@${server_ip}:${port}"
  local ss_url
  ss_url="ss://$(printf '%s' "${ss_payload}" | base64 | tr -d '\n')"

  echo "${ss_url}"
}

del_shadowtls_ss() {
  local username
  username=$1

  local user_file="${USERS_DIR}/${username}.conf"
  if [[ ! -f "${user_file}" ]]; then
    echo "User ${username} not found in ${USERS_DIR}" >&2
    exit 1
  fi

  rm -f "${user_file}"
}

main() {
  if [[ $# -lt 2 ]]; then
    usage
  fi

  local command=$1
  shift

  case "${command}" in
    add-shadowtls-ss)
      [[ $# -eq 4 ]] || usage
      add_shadowtls_ss "$@"
      ;;
    del-shadowtls-ss)
      [[ $# -eq 1 ]] || usage
      del_shadowtls_ss "$@"
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
