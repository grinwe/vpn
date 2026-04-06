#!/usr/bin/env bash
#
# Per-user traffic collector for the ShadowTLS+Shadowsocks stack.
#
# Design notes:
#   - The ShadowTLS install gives every user their own TCP port (see
#     /etc/shadowtls-ss/users.d/*.conf). That means port-based iptables
#     accounting is naturally per-user — no need to poke xray stats or
#     install ebpf probes. One chain, one rule per port.
#   - We maintain a dedicated chain VPN_TRAFFIC that is jumped to from
#     both INPUT and OUTPUT. Each user gets two matching rules tagged
#     with `--comment "vpnuser=<username>;dir=up|down"`. Tags are the
#     source of truth — if iptables and users.d drift, we reconcile by
#     removing unknown tags and adding missing ones.
#   - Every run reads counters with -Z (zero-and-read) so each POST to
#     the backend represents exactly the delta since the previous run.
#   - The script is intentionally idempotent and safe to run every
#     minute from a systemd timer. It is NOT safe to run two copies in
#     parallel on the same host (they would race on -Z). The systemd
#     timer uses a single one-shot unit, so this is fine.
#
# Environment (loaded from /etc/shadowtls-ss/collector.env):
#   BACKEND_URL   — e.g. https://vpn.example.com
#   BACKEND_TOKEN — scoped API token (traffic:write)
#   NODE_ID       — integer, matches the VPNNode row on the backend
#
set -euo pipefail

ENV_FILE="/etc/vpn-collector/collector.env"
USERS_DIR="/etc/shadowtls-ss/users.d"
CHAIN="VPN_TRAFFIC"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

: "${BACKEND_URL:?BACKEND_URL is not set}"
: "${BACKEND_TOKEN:?BACKEND_TOKEN is not set}"
: "${NODE_ID:?NODE_ID is not set}"

# If the ShadowTLS stack is not installed on this host, silently succeed.
# The fleet runs a uniform collector unit set; per-node applicability is
# decided here rather than by enabling/disabling timers.
if [[ ! -d "${USERS_DIR}" ]]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Ensure the VPN_TRAFFIC chain exists and is reachable from INPUT + OUTPUT.
# ---------------------------------------------------------------------------
iptables -w -N "${CHAIN}" 2>/dev/null || true
iptables -w -C INPUT -j "${CHAIN}" 2>/dev/null || iptables -w -I INPUT -j "${CHAIN}"
iptables -w -C OUTPUT -j "${CHAIN}" 2>/dev/null || iptables -w -I OUTPUT -j "${CHAIN}"

# ---------------------------------------------------------------------------
# Reconcile rules in the chain with the current users.d directory.
#
# Desired state: for each user file `<username>.conf` containing a `PORT=N`
# line, have two rules in VPN_TRAFFIC tagged
#     vpnuser=<username>;dir=up
#     vpnuser=<username>;dir=down
# The `up` rule matches --dport N (client → server), the `down` rule
# matches --sport N (server → client).
# ---------------------------------------------------------------------------
declare -A desired_ports=()
if [[ -d "${USERS_DIR}" ]]; then
    shopt -s nullglob
    for user_file in "${USERS_DIR}"/*.conf; do
        username=$(basename "${user_file}" .conf)
        port=$(awk -F= '/^PORT=/ {print $2; exit}' "${user_file}" | tr -d '[:space:]')
        if [[ -n "${port}" ]]; then
            desired_ports["${username}"]="${port}"
        fi
    done
    shopt -u nullglob
fi

# Extract current tags + line numbers from iptables.
declare -A current_tags=()
while IFS= read -r line; do
    tag=$(sed -n 's/.*vpnuser=\([^;]*\);dir=\(up\|down\).*/\1:\2/p' <<<"${line}")
    [[ -n "${tag}" ]] && current_tags["${tag}"]=1
done < <(iptables -w -S "${CHAIN}")

# Remove rules whose tag is no longer in desired state.
for tag in "${!current_tags[@]}"; do
    username="${tag%:*}"
    direction="${tag#*:}"
    if [[ -z "${desired_ports[$username]+x}" ]]; then
        iptables -w -D "${CHAIN}" -p tcp --dport 0 2>/dev/null || true
        # Proper delete: iterate -S output again and remove by spec.
        while IFS= read -r spec; do
            if grep -q "vpnuser=${username};dir=${direction}" <<<"${spec}"; then
                rule="${spec#-A ${CHAIN} }"
                # shellcheck disable=SC2086
                iptables -w -D "${CHAIN}" ${rule} || true
            fi
        done < <(iptables -w -S "${CHAIN}")
    fi
done

# Add missing rules for users present in users.d but not yet in iptables.
for username in "${!desired_ports[@]}"; do
    port="${desired_ports[$username]}"
    if [[ -z "${current_tags[${username}:up]+x}" ]]; then
        iptables -w -A "${CHAIN}" -p tcp --dport "${port}" \
            -m comment --comment "vpnuser=${username};dir=up"
    fi
    if [[ -z "${current_tags[${username}:down]+x}" ]]; then
        iptables -w -A "${CHAIN}" -p tcp --sport "${port}" \
            -m comment --comment "vpnuser=${username};dir=down"
    fi
done

# ---------------------------------------------------------------------------
# Read counters with -Z so we measure exactly this window.
# iptables -L output format (with -vnx):
#     pkts bytes target prot opt in out source destination /* comment */
# We key on the comment.
# ---------------------------------------------------------------------------
declare -A up_bytes=()
declare -A down_bytes=()

while IFS= read -r line; do
    if [[ "${line}" =~ vpnuser=([^\;]+)\;dir=(up|down) ]]; then
        username="${BASH_REMATCH[1]}"
        direction="${BASH_REMATCH[2]}"
        bytes=$(awk '{print $2}' <<<"${line}")
        if [[ "${direction}" == "up" ]]; then
            up_bytes["${username}"]="${bytes:-0}"
        else
            down_bytes["${username}"]="${bytes:-0}"
        fi
    fi
done < <(iptables -w -L "${CHAIN}" -vnxZ)

# ---------------------------------------------------------------------------
# Build the JSON payload and POST it.
# ---------------------------------------------------------------------------
samples_json=""
for username in "${!desired_ports[@]}"; do
    up="${up_bytes[${username}]:-0}"
    down="${down_bytes[${username}]:-0}"
    if [[ -n "${samples_json}" ]]; then
        samples_json+=","
    fi
    samples_json+=$(printf '{"access_username":"%s","uplink_bytes":%s,"downlink_bytes":%s}' \
        "${username}" "${up}" "${down}")
done

collected_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
payload=$(printf '{"collected_at":"%s","samples":[%s]}' "${collected_at}" "${samples_json}")

# --fail so a non-2xx status returns nonzero and systemd logs the failure.
# --max-time prevents a hung backend from stalling the timer.
curl --silent --show-error --fail --max-time 15 \
    -X POST "${BACKEND_URL}/api/nodes/${NODE_ID}/traffic" \
    -H "X-Api-Token: ${BACKEND_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "${payload}" >/dev/null
