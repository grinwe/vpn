#!/usr/bin/env bash
#
# Per-user traffic collector for VLESS+Reality (xray-core).
#
# VLESS Reality serves all users from a single port, so iptables port-
# based accounting is useless here — we read xray's own stats API via
# the gRPC endpoint exposed on 127.0.0.1:10085 (configured in the
# install_vless_reality role's config.json.j2).
#
# xray statsquery with --reset returns:
#     user>>>EMAIL>>>traffic>>>uplink   <bytes>
#     user>>>EMAIL>>>traffic>>>downlink <bytes>
# and zeroes the counter, so each invocation reports exactly the delta
# since the previous one. Safe to run from a once-a-minute systemd timer.
#
# Environment (loaded from /etc/shadowtls-ss/collector.env — yes we
# reuse the same env file as the iptables collector; the file is the
# node-wide collector config, not specific to either stack):
#   BACKEND_URL   — e.g. https://vpn.example.com
#   BACKEND_TOKEN — scoped API token (traffic:write)
#   NODE_ID       — integer, matches the VPNNode row on the backend
#   XRAY_API      — optional, defaults to 127.0.0.1:10085
#
set -euo pipefail

ENV_FILE="/etc/vpn-collector/collector.env"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

: "${BACKEND_URL:?BACKEND_URL is not set}"
: "${BACKEND_TOKEN:?BACKEND_TOKEN is not set}"
: "${NODE_ID:?NODE_ID is not set}"
XRAY_API="${XRAY_API:-127.0.0.1:10085}"

# If xray is not installed on this node we silently succeed so the timer
# can be enabled uniformly across the fleet.
if ! command -v xray >/dev/null 2>&1; then
    exit 0
fi
if [[ ! -f /usr/local/etc/xray/config.json ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Query stats with --reset and parse.
#
# xray api statsquery output is one key/value pair per line:
#     user>>>alice@vpn>>>traffic>>>uplink: 12345
# We only care about the user>>> entries.
# ---------------------------------------------------------------------------
declare -A up_bytes=()
declare -A down_bytes=()

stats_output=$(xray api statsquery \
    --server="${XRAY_API}" \
    -pattern "user>>>" \
    -reset 2>/dev/null || true)

if [[ -z "${stats_output}" ]]; then
    # No clients matched, or xray not ready yet. Either way there is
    # nothing to report — this is NOT an error.
    exit 0
fi

# xray api output format varies slightly across versions: some ship
# JSON (with `"stat":` entries), some ship flat "key: value" lines.
# Handle both by first trying jq for the JSON case and falling back to
# awk parsing otherwise.
if echo "${stats_output}" | jq -e . >/dev/null 2>&1; then
    while IFS=$'\t' read -r name value; do
        email=$(awk -F'>>>' '{print $2}' <<<"${name}")
        direction=$(awk -F'>>>' '{print $4}' <<<"${name}")
        case "${direction}" in
            uplink)   up_bytes["${email}"]="${value:-0}" ;;
            downlink) down_bytes["${email}"]="${value:-0}" ;;
        esac
    done < <(echo "${stats_output}" | jq -r '.stat[]? | [.name, .value // "0"] | @tsv')
else
    while IFS= read -r line; do
        key="${line%%:*}"
        raw="${line#*:}"
        # Strip whitespace
        value=$(tr -d '[:space:]' <<<"${raw}")
        email=$(awk -F'>>>' '{print $2}' <<<"${key}")
        direction=$(awk -F'>>>' '{print $4}' <<<"${key}")
        case "${direction}" in
            uplink)   up_bytes["${email}"]="${value:-0}" ;;
            downlink) down_bytes["${email}"]="${value:-0}" ;;
        esac
    done <<<"${stats_output}"
fi

# ---------------------------------------------------------------------------
# Union of emails seen in either direction.
# ---------------------------------------------------------------------------
declare -A seen=()
for e in "${!up_bytes[@]}"; do seen["${e}"]=1; done
for e in "${!down_bytes[@]}"; do seen["${e}"]=1; done

samples_json=""
for email in "${!seen[@]}"; do
    up="${up_bytes[${email}]:-0}"
    down="${down_bytes[${email}]:-0}"
    if [[ -n "${samples_json}" ]]; then
        samples_json+=","
    fi
    samples_json+=$(printf '{"access_username":"%s","uplink_bytes":%s,"downlink_bytes":%s}' \
        "${email}" "${up}" "${down}")
done

if [[ -z "${samples_json}" ]]; then
    exit 0
fi

collected_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
payload=$(printf '{"collected_at":"%s","samples":[%s]}' "${collected_at}" "${samples_json}")

curl --silent --show-error --fail --max-time 15 \
    -X POST "${BACKEND_URL}/api/nodes/${NODE_ID}/traffic" \
    -H "X-Api-Token: ${BACKEND_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "${payload}" >/dev/null
