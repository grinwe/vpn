#!/usr/bin/env bash
# Diagnose ansible runtime regression after the 2026-05-19 mgmt-rebuild.
#
# Запускается на самом mgmt-хосте (внутри backend/worker контейнера),
# собирает все signals, которые могут объяснять x5 slowdown ansible-task'ов
# при том, что ansible.cfg в репо выглядит правильно. Output один txt
# на stdout — можно redirect'нуть в файл и приложить к issue.
#
# Что собираем (в порядке частоты регрессий):
#   1. ansible/python version — могло проапгрейдиться при rebuild
#   2. effective config — что реально активно (ansible.cfg иногда теряется)
#   3. ControlMaster — есть ли socket-reuse между tasks (главный speedup)
#   4. fact_caching state — кешируются ли facts (5-10s/host оверхед)
#   5. SSH handshake time до одной ноды baseline
#   6. ansible ping с -vvv — single-task baseline + transport details
#   7. profile_tasks dry-run (если установлен callback) — какой шаг внутри
#      playbook тормозит
#   8. Docker resource cgroups — может worker процесс upal в throttle
#
# Использование:
#   На mgmt:
#     cd /opt/vpn && docker compose exec worker bash
#     bash /path/to/diag_ansible_perf.sh ru-pq-01 | tee /tmp/diag.txt
#
#   Или с конкретной нодой:
#     bash scripts/diag_ansible_perf.sh ru-adminvps-01
#
# Aргумент 1 (опционально): inventory hostname для SSH-теста. По умолчанию
# берётся первая нода из vpn_nodes group через ansible inventory.

set -u  # без -e — продолжаем даже если отдельные шаги фейлятся

TARGET="${1:-}"

section() {
    echo
    echo "═══════════════════════════════════════════════════════════════════════════════"
    echo "║ $*"
    echo "═══════════════════════════════════════════════════════════════════════════════"
}

run() {
    echo "\$ $*"
    eval "$@" 2>&1 || echo "  (rc=$?)"
    echo
}

# ── 0. Sanity ─────────────────────────────────────────────────────────
section "0. host context"
run "date -u"
run "hostname"
run "uname -a"
run "whoami; id"
run "pwd"

# ── 1. Versions ───────────────────────────────────────────────────────
section "1. ansible / python versions (mismatched after rebuild → first suspect)"
run "ansible --version"
run "python3 --version"
run "which ansible-playbook"

# ── 2. Effective config — что реально активно ──────────────────────────
section "2. effective ansible config (ansible-config dump --only-changed)"
run "ansible-config dump --only-changed"
echo "Note: если pipelining/ControlMaster/fact_caching отсутствуют выше —"
echo "      значит ansible.cfg не подхватился (wrong cwd? envvar override?)"

section "2.5 ANSIBLE_* env vars в текущем процессе"
run "env | grep -i '^ANSIBLE\|^ANSIBLE_' | sort"

section "2.6 какой config файл найден"
run "ansible --version | head -3"
run "ANSIBLE_DEBUG=1 ansible-config view 2>&1 | head -5"

# ── 3. ControlMaster state ────────────────────────────────────────────
section "3. ControlMaster: socket dir + existing connections"
run "ls -la ~/.ansible/cp/ 2>&1 || echo '  (нет директории — ControlMaster не используется?)'"
run "ls -la /tmp/ansible-ssh* 2>&1 || echo '  (нет /tmp socket'ов)"
echo "Если в ~/.ansible/cp нет .sock файлов после ansible run'а — ControlMaster"
echo "не сохраняет сокеты. Каждая task = свежий ssh handshake (1-3s оверхед)."

# ── 4. fact_caching state ─────────────────────────────────────────────
section "4. fact_caching directory"
FACTS_DIR="$(ansible-config dump --only-changed 2>/dev/null | grep -i fact_caching_connection | sed -E 's/.*= *//; s/ *$//')"
FACTS_DIR="${FACTS_DIR:-/var/tmp/ansible_facts}"
run "ls -la $FACTS_DIR/ 2>&1 | head -10 || echo '  (нет кеша — fact_caching не работает?)'"
run "stat $FACTS_DIR 2>&1 | head -5"

# ── 5. SSH baseline до ноды ────────────────────────────────────────────
section "5. SSH handshake baseline до целевой ноды"
if [[ -z "$TARGET" ]]; then
    TARGET="$(ansible-inventory --list 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(iter(d.get("vpn_nodes",{}).get("hosts",[])), ""))' 2>/dev/null)"
fi
if [[ -z "$TARGET" ]]; then
    echo "Не смог выбрать target — передай hostname первым аргументом скрипта"
else
    echo "Target: $TARGET"
    TARGET_HOST="$(ansible-inventory --host "$TARGET" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ansible_host",""))' 2>/dev/null)"
    echo "Resolved host: ${TARGET_HOST:-?}"
    run "time ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no $TARGET 'echo pong'"
    run "time ansible -i inventories/prod/hosts.yml $TARGET -m ping 2>&1 | tail -10"
fi

# ── 6. profile_tasks single-task baseline ──────────────────────────────
section "6. ansible ping with -vvv (transport details + timing)"
if [[ -n "$TARGET" ]]; then
    echo "Looking at: control socket reuse, pipelining=True line, gather_facts time"
    run "time ANSIBLE_STDOUT_CALLBACK=default ansible -i inventories/prod/hosts.yml $TARGET -m ping -vvv 2>&1 | tail -40"
fi

# ── 7. profile_tasks для realистичной диагностики ─────────────────────
section "7. profile_tasks callback (если включить — увидим тяжёлые шаги)"
echo "Чтобы профилировать **реальный** playbook:"
echo "  ANSIBLE_STDOUT_CALLBACK=profile_tasks ansible-playbook playbooks/diagnose_relay_link.yml ..."
echo "  ANSIBLE_CALLBACKS_ENABLED=profile_tasks  # для новых ансиблов"
echo "  Это покажет per-task длительность с топом самых медленных."

# ── 8. cgroups / resource limits ──────────────────────────────────────
section "8. cgroup limits на текущем процессе (docker resource throttle?)"
run "cat /proc/self/cgroup 2>&1 | head -5"
run "cat /sys/fs/cgroup/cpu.max 2>&1 || cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>&1"
run "cat /sys/fs/cgroup/memory.max 2>&1 || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>&1"
run "free -h"
run "nproc"

# ── 9. Network latency mgmt→node ──────────────────────────────────────
section "9. Network latency до целевой ноды"
if [[ -n "${TARGET_HOST:-}" ]]; then
    run "ping -c 5 -i 0.2 $TARGET_HOST"
    run "traceroute -n -m 12 $TARGET_HOST 2>&1 | head -15 || echo '  (traceroute не установлен)'"
fi

# ── 10. sudoers requiretty проверка через ansible -m setup ─────────────
section "10. requiretty на ноде (ломает pipelining → x2-5 slowdown)"
if [[ -n "$TARGET" ]]; then
    echo "Если ниже видим 'sudo: sorry, you must have a tty' → pipelining не работает"
    run "ansible -i inventories/prod/hosts.yml $TARGET -b -m command -a 'whoami' 2>&1 | tail -10"
fi

echo
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "║ Готово. Куда смотреть в первую очередь:"
echo "║  • §2 ansible-config dump — есть ли pipelining + ControlMaster + fact_caching"
echo "║  • §5/6 — ssh handshake time + ansible ping time (норма ≤ 3s, тревога > 5s)"
echo "║  • §6 -vvv — ищи 'reusing existing ssh connection'. Если этого нет на 2-3"
echo "║    подряд task'е — ControlMaster сломан, главный fix."
echo "║  • §10 — требование tty в sudoers на ноде ломает pipelining"
echo "║  • §9 — если ping mgmt→node > 50ms или traceroute через лишние хопы,"
echo "║    проблема network'а нового VPS у хостера."
echo "═══════════════════════════════════════════════════════════════════════════════"
