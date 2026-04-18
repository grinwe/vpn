#!/usr/bin/env bash
# Add or remove a Hysteria2 user in the config.
# Usage:
#   manage_hy2_user.sh add <username> <password>
#   manage_hy2_user.sh del <username>
set -euo pipefail

CONFIG="/etc/hysteria/config.yaml"

usage() { echo "Usage: $0 add <username> <password> | del <username>" >&2; exit 1; }

cmd_add() {
  local user="$1" pass="$2"
  # Use python3 to safely edit YAML
  python3 -c "
import yaml, sys
with open('${CONFIG}') as f:
    cfg = yaml.safe_load(f)
up = cfg.setdefault('auth', {}).setdefault('userpass', {})
up['${user}'] = '${pass}'
with open('${CONFIG}', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
  "
  systemctl restart hysteria-server
  echo "added hy2 user ${user}"
}

cmd_del() {
  local user="$1"
  python3 -c "
import yaml
with open('${CONFIG}') as f:
    cfg = yaml.safe_load(f)
up = cfg.get('auth', {}).get('userpass', {})
up.pop('${user}', None)
with open('${CONFIG}', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
  "
  systemctl restart hysteria-server
  echo "removed hy2 user ${user}"
}

[[ $# -ge 1 ]] || usage
case "$1" in
  add) [[ $# -ge 3 ]] || usage; cmd_add "$2" "$3" ;;
  del) [[ $# -ge 2 ]] || usage; cmd_del "$2" ;;
  *) usage ;;
esac
