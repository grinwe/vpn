#!/usr/bin/env bash
# audit_split_routing.sh — пройти по vpn_nodes из inventory и снять
# срез split-routing telemetry. Что проверяем:
#   * наличие xray + конфигов (config.json + config_xhttp.json)
#   * есть ли в routing rule с geoip:ru → direct-local outbound
#     (т.е. RU-трафик выпускается без WG-туннеля)
#   * sockopt.interface у default direct outbound (= куда идёт
#     не-RU трафик: если "wgN" → через WG к exit'у, если пусто →
#     прямо с relay'я)
#   * свежесть /usr/local/share/xray/geoip.dat (age в днях)
#   * статус geoip-update.timer + последний run сервиса
#   * smoke-test реального egress IP (curl ifconfig.me — direct
#     outbound = должен быть RU IP relay'я, если ноды правильные)
#
# Запуск с дев-машины (по образцу workers.sh — парсит mgmt из
# inventory, дальше SSH'ит ПО ВСЕМ vpn_nodes напрямую):
#   ./scripts/audit_split_routing.sh           # печать таблицы
#   ./scripts/audit_split_routing.sh --json    # сырой JSON для grep'а
#   ./scripts/audit_split_routing.sh --node ru-pq-01   # одна нода
#
# Env overrides:
#   SSH_USER  — default: root
#   PARALLEL  — параллельность SSH-сборов. Default: 6.
#
# Идём напрямую с дев-машины, потому что mgmt — это бэк-стек, у него
# доступ к нодам по тем же ключам что у нас, разницы нет, а кейс
# «один из workers повис на mgmt'е» нам тут не нужен.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SSH_USER="${SSH_USER:-root}"
PARALLEL="${PARALLEL:-6}"

MODE="table"
NODE_FILTER=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) MODE="json"; shift ;;
        --node) NODE_FILTER="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# Parse inventory → "name|host" по строке для vpn_nodes
HOSTS=$(python3 - <<PY
import yaml, sys
inv = yaml.safe_load(open("$REPO_ROOT/infra/ansible/inventories/prod/hosts.yml"))
vpn = inv["all"]["children"]["vpn_nodes"]["hosts"]
for name, h in vpn.items():
    if "$NODE_FILTER" and name != "$NODE_FILTER":
        continue
    print(f"{name}|{h['ansible_host']}")
PY
)

if [[ -z "$HOSTS" ]]; then
    echo "Нет хостов для аудита (фильтр '$NODE_FILTER'?)" >&2
    exit 1
fi

# Remote-side скрипт. Вшит heredoc'ом — на ноде запускается через
# bash -s. Никаких файлов на ноде не остаётся.
REMOTE_SCRIPT='
set +e

jstr() { printf "\"%s\"" "$(printf "%s" "$1" | sed "s/\\\\/\\\\\\\\/g; s/\"/\\\\\"/g")"; }
out=()
kv() { out+=("\"$1\": $2"); }
kvs() { out+=("\"$1\": $(jstr "$2")"); }

# 1. binaries
[ -x /usr/local/bin/xray ] && xray_present=true || xray_present=false
kv xray_present $xray_present

# 2. xray-configs
RC=/usr/local/etc/xray/config.json
XC=/usr/local/etc/xray/config_xhttp.json
[ -f "$RC" ] && reality_cfg_exists=true || reality_cfg_exists=false
[ -f "$XC" ] && xhttp_cfg_exists=true || xhttp_cfg_exists=false
kv reality_cfg_exists $reality_cfg_exists
kv xhttp_cfg_exists $xhttp_cfg_exists

# 3. routing inspection (jq нужен, ставится apt install jq в bootstrap)
if command -v jq >/dev/null 2>&1; then
    if [ -f "$RC" ]; then
        rc_geoip_rule=$(jq "[.routing.rules[]? | select((.ip? // []) | index(\"geoip:ru\"))] | length > 0" "$RC" 2>/dev/null || echo false)
        rc_direct_local_ob=$(jq "[.outbounds[]? | select(.tag == \"direct-local\")] | length > 0" "$RC" 2>/dev/null || echo false)
        rc_direct_iface=$(jq -r ".outbounds[]? | select(.tag == \"direct\") | .streamSettings.sockopt.interface // \"\"" "$RC" 2>/dev/null)
        rc_ru_domain_rules=$(jq "[.routing.rules[]? | select(.outboundTag == \"direct-local\" and ((.domain? // []) | length > 0))] | length" "$RC" 2>/dev/null || echo 0)
        kv reality_has_geoip_ru_rule $rc_geoip_rule
        kv reality_has_direct_local_ob $rc_direct_local_ob
        kvs reality_direct_sockopt_iface "$rc_direct_iface"
        kv reality_ru_domain_rules_count $rc_ru_domain_rules
    fi
    if [ -f "$XC" ]; then
        xc_geoip_rule=$(jq "[.routing.rules[]? | select((.ip? // []) | index(\"geoip:ru\"))] | length > 0" "$XC" 2>/dev/null || echo false)
        xc_direct_iface=$(jq -r ".outbounds[]? | select(.tag == \"direct\") | .streamSettings.sockopt.interface // \"\"" "$XC" 2>/dev/null)
        kv xhttp_has_geoip_ru_rule $xc_geoip_rule
        kvs xhttp_direct_sockopt_iface "$xc_direct_iface"
    fi
else
    kv jq_present false
fi

# 4. geoip.dat
GP=/usr/local/share/xray/geoip.dat
if [ -f "$GP" ]; then
    gp_mtime=$(stat -c %Y "$GP" 2>/dev/null || echo 0)
    gp_size=$(stat -c %s "$GP" 2>/dev/null || echo 0)
    gp_age=$(( ($(date +%s) - gp_mtime) / 86400 ))
    kv geoip_size_bytes $gp_size
    kv geoip_age_days $gp_age
else
    kvs geoip_status "missing"
fi

# 5. timer + last service run
t_en=$(systemctl is-enabled geoip-update.timer 2>/dev/null || echo "missing")
t_ac=$(systemctl is-active geoip-update.timer 2>/dev/null || echo "missing")
last_trigger=$(systemctl show geoip-update.service --property=ExecMainExitTimestamp --value 2>/dev/null | head -c 40)
last_exit=$(systemctl show geoip-update.service --property=ExecMainStatus --value 2>/dev/null)
kvs geoip_timer_enabled "$t_en"
kvs geoip_timer_active "$t_ac"
kvs geoip_last_run "$last_trigger"
kvs geoip_last_exit "$last_exit"

# 6. WG-ifaces (для контекста: relay или нет)
wg_ifaces=$(ls /etc/wireguard/*.conf 2>/dev/null | xargs -n1 basename 2>/dev/null | sed "s/\\.conf$//" | tr "\n" "," | sed "s/,$//")
kvs wg_interfaces "$wg_ifaces"
if [ -n "$wg_ifaces" ]; then is_relay=true; else is_relay=false; fi
kv is_relay $is_relay

# 7. Smoke: direct egress — что мир видит, когда relay шлёт без iface
# Cap by 5s так чтобы один лагающий хост не блочил весь audit.
direct_ip=$(timeout 6 curl -s --max-time 5 https://ifconfig.me 2>/dev/null || echo "?")
kvs egress_direct_ip "$direct_ip"

# Если есть WG-интерфейс — пробьём егress через первый
first_wg=$(echo "$wg_ifaces" | cut -d, -f1)
if [ -n "$first_wg" ]; then
    wg_ip=$(timeout 6 curl -s --max-time 5 --interface "$first_wg" https://ifconfig.me 2>/dev/null || echo "?")
    kvs "egress_via_${first_wg}_ip" "$wg_ip"
fi

# Final JSON
IFS=,
echo "{${out[*]}}"
'

# Per-node collector. Параллелится через xargs.
collect_one() {
    local entry="$1"
    local name="${entry%%|*}"
    local host="${entry##*|}"
    local json
    json=$(ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
        "$SSH_USER@$host" bash -s 2>/dev/null <<< "$REMOTE_SCRIPT" || true)
    if [ -z "$json" ] || ! printf '%s' "$json" | python3 -c "import sys,json; json.loads(sys.stdin.read())" 2>/dev/null; then
        json='{"error":"ssh_or_remote_failed"}'
    fi
    printf '{"name":"%s","host":"%s","data":%s}\n' "$name" "$host" "$json"
}
export -f collect_one
export REMOTE_SCRIPT SSH_USER

RESULTS=$(echo "$HOSTS" | xargs -n1 -P "$PARALLEL" -I{} bash -c 'collect_one "$@"' _ {})

# Уложим в JSON-массив
ALL_JSON=$(printf '[%s]' "$(echo "$RESULTS" | paste -sd,)")

if [[ "$MODE" == "json" ]]; then
    echo "$ALL_JSON" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))"
    exit 0
fi

# Pretty-таблица. Поля выбраны самые информативные для split-routing.
export ALL_JSON_ENV="$ALL_JSON"
python3 - <<'PY'
import json, sys, os, datetime
data = json.loads(os.environ["ALL_JSON_ENV"])

def color(s, c):
    return f"\033[{c}m{s}\033[0m" if sys.stdout.isatty() else s
def ok(s):   return color(s, "32")
def warn(s): return color(s, "33")
def bad(s):  return color(s, "31")
def dim(s):  return color(s, "90")

print(f"{'NODE':<18} {'XRAY':<5} {'GEOIP':<14} {'TIMER':<10} {'RC.geoIP':<10} {'RC.direct→':<14} {'XHTTP.geoIP':<12} {'direct-IP':<18} {'wg-IP':<18}")
print("─" * 138)

alerts = []
for row in data:
    name = row["name"]
    d = row.get("data", {})
    if d.get("error"):
        print(f"{name:<18} {bad('SSH-FAIL'):<25}")
        alerts.append(f"{name}: SSH failed")
        continue
    xray = ok("yes") if d.get("xray_present") else bad("no")
    age = d.get("geoip_age_days", "?")
    geoip_size = d.get("geoip_size_bytes", 0)
    if d.get("geoip_status") == "missing":
        geoip_col = bad("MISSING")
        alerts.append(f"{name}: geoip.dat MISSING")
    elif isinstance(age, int) and age > 30:
        geoip_col = bad(f"{age}d / {geoip_size//1024}k")
        alerts.append(f"{name}: geoip.dat ({age}d) > 30 days — auto-update сломан?")
    elif isinstance(age, int) and age > 14:
        geoip_col = warn(f"{age}d / {geoip_size//1024}k")
    else:
        geoip_col = ok(f"{age}d / {geoip_size//1024}k")

    t_en = d.get("geoip_timer_enabled", "?")
    t_ac = d.get("geoip_timer_active", "?")
    if t_en == "enabled" and t_ac == "active":
        timer_col = ok("ok")
    elif t_en == "missing":
        timer_col = bad("missing")
        alerts.append(f"{name}: geoip-update.timer не установлен (роль не пробежала?)")
    else:
        timer_col = warn(f"{t_en}/{t_ac}")
        alerts.append(f"{name}: timer state '{t_en}/{t_ac}'")

    rc_geoip = ok("yes") if d.get("reality_has_geoip_ru_rule") else (bad("NO") if d.get("reality_cfg_exists") else dim("—"))
    if d.get("reality_cfg_exists") and not d.get("reality_has_geoip_ru_rule"):
        alerts.append(f"{name}: reality routing БЕЗ geoip:ru rule — RU-трафик уходит через WG!")
    rc_iface = d.get("reality_direct_sockopt_iface", "") or dim("(none)")
    xc_geoip = ok("yes") if d.get("xhttp_has_geoip_ru_rule") else (bad("NO") if d.get("xhttp_cfg_exists") else dim("—"))
    if d.get("xhttp_cfg_exists") and not d.get("xhttp_has_geoip_ru_rule"):
        alerts.append(f"{name}: xhttp routing БЕЗ geoip:ru rule")

    dip = d.get("egress_direct_ip", "?") or "?"
    wg_keys = [k for k in d.keys() if k.startswith("egress_via_") and k.endswith("_ip")]
    wgip = d.get(wg_keys[0], "?") if wg_keys else dim("(no wg)")

    print(f"{name:<18} {xray:<14} {geoip_col:<23} {timer_col:<19} {rc_geoip:<19} {rc_iface:<14} {xc_geoip:<21} {dip:<18} {wgip:<18}")

print()
if alerts:
    print(color("ALERTS:", "33;1"))
    for a in alerts:
        print(f"  • {a}")
else:
    print(color("✓ Split routing на всех нодах в норме.", "32"))
PY
