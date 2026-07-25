#!/usr/bin/env bash
# Add or remove a Hysteria2 user in the config.
# Usage:
#   manage_hy2_user.sh add <username> <password>
#   manage_hy2_user.sh del <username>
set -euo pipefail

CONFIG="/etc/hysteria/config.yaml"

usage() { echo "Usage: $0 add <username> <password> | del <username>" >&2; exit 1; }

# NO_RESTART=1 — не рестартить сервис после правки (батч-ресинк из
# resync_node_hy2.yml добавляет десятки юзеров подряд и рестартит ОДИН раз в
# конце; иначе systemd StartLimitBurst (5 за 10s) прибьёт hysteria-server).
_maybe_restart() {
  if [[ "${NO_RESTART:-0}" == "1" ]]; then
    echo "NO_RESTART=1 — skip hysteria-server restart"
    return 0
  fi
  systemctl restart hysteria-server
}

# Имя/пароль передаём через окружение, а НЕ подстановкой в текст python-скрипта:
# значение с кавычкой или переводом строки иначе выполнялось бы как код (root на
# ноде). Наши секреты — token_urlsafe, но зависеть от этого нельзя (аудит
# 2026-07-25).
cmd_add() {
  local user="$1" pass="$2"
  # Use python3 to safely edit YAML
  HY2_CONFIG="${CONFIG}" HY2_USER="${user}" HY2_PASS="${pass}" python3 -c "
import os, yaml
path = os.environ['HY2_CONFIG']
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
up = cfg.setdefault('auth', {}).setdefault('userpass', {})
up[os.environ['HY2_USER']] = os.environ['HY2_PASS']
with open(path, 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
  "
  _maybe_restart
  echo "added hy2 user ${user}"
}

cmd_del() {
  local user="$1"
  HY2_CONFIG="${CONFIG}" HY2_USER="${user}" python3 -c "
import os, yaml
path = os.environ['HY2_CONFIG']
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
up = cfg.get('auth', {}).get('userpass', {})
up.pop(os.environ['HY2_USER'], None)
with open(path, 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
  "
  _maybe_restart
  echo "removed hy2 user ${user}"
}

[[ $# -ge 1 ]] || usage
case "$1" in
  add) [[ $# -ge 3 ]] || usage; cmd_add "$2" "$3" ;;
  del) [[ $# -ge 2 ]] || usage; cmd_del "$2" ;;
  *) usage ;;
esac
