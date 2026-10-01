#!/usr/bin/env bash
# diag_monitoring.sh — диагностика backend-scrape'а для Grafana/Prometheus.
# Запускать на nl-monitoring (он же nl-web — один физический хост).
#
# Usage: ./scripts/diag_monitoring.sh 2>&1 | tee diag.out
# Scp'нуть с ноды: scp root@45.14.244.140:/tmp/diag.out .

set -u

hr() { printf '\n========== %s ==========\n' "$*"; }

hr "1. docker containers involved"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Networks}}' \
  | grep -E 'vpn-prometheus|vpn-grafana|backend|worker|bot|admin|webapp' || true

hr "2. prometheus networks (must contain vpn_default AFTER re-deploy)"
docker inspect vpn-prometheus \
  --format '{{json .NetworkSettings.Networks}}' 2>/dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(k,"=>",v.get("IPAddress","?")) for k,v in d.items()]' 2>/dev/null \
  || docker inspect vpn-prometheus --format '{{.NetworkSettings.Networks}}'

hr "3. vpn_default network exists?"
docker network ls | grep -E 'vpn_default|vpn-monitoring'

hr "4. containers attached to vpn_default"
docker network inspect vpn_default \
  --format '{{range .Containers}}{{.Name}}  {{.IPv4Address}}{{"\n"}}{{end}}' 2>/dev/null \
  || echo "network vpn_default not found"

hr "5. effective prometheus.yml (what's actually mounted)"
docker exec vpn-prometheus cat /etc/prometheus/prometheus.yml 2>/dev/null \
  | grep -A3 'vpn-backend' \
  || echo "failed — prom container down?"

hr "6. DNS: can prom resolve 'backend'?"
docker exec vpn-prometheus getent hosts backend 2>/dev/null \
  || docker exec vpn-prometheus nslookup backend 2>/dev/null \
  || echo "(no getent/nslookup in prom image — trying wget)"

hr "7. direct scrape test from inside prometheus container"
docker exec vpn-prometheus wget -qO- --timeout=5 http://backend:8000/metrics 2>&1 \
  | head -5 \
  || echo "scrape fail"

hr "7b. same via host.docker.internal (old path, should also fail on Linux)"
docker exec vpn-prometheus wget -qO- --timeout=5 http://host.docker.internal:8000/metrics 2>&1 \
  | head -5 \
  || true

hr "8. from host, what does backend say?"
curl -sS --max-time 5 -o /tmp/metrics_from_host http://127.0.0.1:8000/metrics \
  && head -3 /tmp/metrics_from_host \
  || echo "backend unreachable from host loopback (!!!)"

hr "9. prometheus targets API — scrape errors"
curl -sS --max-time 5 http://127.0.0.1:9090/api/v1/targets \
  | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("json parse:", e); sys.exit(0)
active = d.get("data", {}).get("activeTargets", [])
for t in active:
    job = t.get("labels", {}).get("job", "?")
    url = t.get("scrapeUrl", "?")
    health = t.get("health", "?")
    err = t.get("lastError", "")
    print(f"{health:8s} {job:20s} {url}")
    if err:
        print(f"         └─ error: {err}")
' 2>/dev/null || echo "prom API unreachable"

hr "10. backend container — is it really on vpn_default?"
docker inspect $(docker ps -q --filter name=backend) \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{$v.IPAddress}}{{"\n"}}{{end}}' \
  2>/dev/null || echo "no backend container"

hr "DONE"
