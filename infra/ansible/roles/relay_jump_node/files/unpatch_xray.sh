#!/bin/bash
# unpatch_xray.sh — strip every relay-specific bit from an xray config.
#
# Args: $1 = path to xray config*.json
# Env:  (none)
#
# Exit codes:
#   0   — config changed, notify xray reload
#   78  — config already in desired state, skip reload
#   1   — jq produced invalid JSON or file I/O error
#
# Lives as a plain file for the same reason as reconcile_xray.sh:
# Ansible 2.17's argument splitter chokes on inline jq DSL.
set -euo pipefail

CONFIG="${1:?config path required}"
TMP=$(mktemp)

jq -f /tmp/xray_unpatch.jq "$CONFIG" > "$TMP"

if ! jq -e . "$TMP" > /dev/null; then
  rm -f "$TMP"
  exit 1
fi

if cmp -s "$TMP" "$CONFIG"; then
  rm -f "$TMP"
  exit 78
fi

mv "$TMP" "$CONFIG"
chown root:nogroup "$CONFIG" 2>/dev/null || chown root:root "$CONFIG"
chmod 0640 "$CONFIG"
