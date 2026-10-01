#!/usr/bin/env bash
# kill_stuck_ansible.sh — прибить застрявшие wait-task'и ансибла на ноде.
#
# Симптом: несколько python3.12 без аргументов сидят в poll() часами,
# parent = /bin/sh -c '/usr/bin/python3.12 && sleep 0' от systemd. Это
# wait-task'и из bootstrap_node которые попали в race с параллельными
# `apt install` от других worker'ов (см. POSTMORTEM § wait-storm).
#
# Запуск НА НОДЕ как root:
#   bash kill_stuck_ansible.sh         # dry-run, только покажет что нашлось
#   bash kill_stuck_ansible.sh --kill  # реально прибить
#
# Или одной командой с dev-машины:
#   ssh root@<host> 'bash -s -- --kill' < scripts/kill_stuck_ansible.sh

set -u

MODE="${1:-dry-run}"

hr() { printf '\n=== %s ===\n' "$*"; }

hr "Поиск застрявших ansible python-процессов"
# Подозрительные = python3.12 без скрипта в argv, parent — sh-wrapper'ом,
# который в свою очередь от init/systemd (т.е. brоп ансибла через SSH).
SUSPECTS=()
for pid in $(ps -eo pid,cmd | awk '/python3\.12/ && !/grep/ && !/kill_stuck/ {print $1}'); do
    [ -e "/proc/$pid" ] || continue
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | sed 's/ $//')
    # Голый /usr/bin/python3.12 без скрипта — кандидат
    if [ "$cmdline" = "/usr/bin/python3.12" ]; then
        ppid=$(awk '{print $4}' /proc/$pid/stat 2>/dev/null)
        parent_cmd=$(tr '\0' ' ' < "/proc/$ppid/cmdline" 2>/dev/null | sed 's/ $//')
        if [[ "$parent_cmd" =~ "/usr/bin/python3.12" ]]; then
            etime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
            echo "  PID $pid (parent=$ppid, etime=$etime): $cmdline"
            echo "    parent: $parent_cmd"
            SUSPECTS+=("$pid" "$ppid")
        fi
    fi
done

if [ "${#SUSPECTS[@]}" -eq 0 ]; then
    echo "Ничего не нашлось — все python'ы либо с скриптом, либо нет sh-wrapper'а."
    exit 0
fi

hr "Также добавим висящие shell-loop'ы (while fuser / sleep)"
# Бывает что python уже отдал управление, но осталась shell'ка с while.
for pid in $(pgrep -f 'while fuser' 2>/dev/null); do
    [ -e "/proc/$pid" ] || continue
    echo "  PID $pid: while-fuser loop"
    SUSPECTS+=("$pid")
done

if [ "$MODE" != "--kill" ]; then
    hr "Dry-run — для реального killʼа: $0 --kill"
    echo "Найдено PID'ов для прибития: ${#SUSPECTS[@]}"
    exit 0
fi

hr "SIGTERM по списку"
# Сначала вежливо, потом KILL.
# Уникализируем PID'ы (могут повторяться если ppid тот же что был ранее).
UNIQUE=($(printf '%s\n' "${SUSPECTS[@]}" | sort -u))
for pid in "${UNIQUE[@]}"; do
    if kill -TERM "$pid" 2>/dev/null; then
        echo "  TERM $pid"
    fi
done

echo "Ждём 5s..."
sleep 5

hr "Кто остался — SIGKILL"
for pid in "${UNIQUE[@]}"; do
    if [ -e "/proc/$pid" ]; then
        kill -KILL "$pid" 2>/dev/null && echo "  KILL $pid"
    fi
done

hr "Итоговое состояние"
sleep 1
ps -ef | grep -E "python3\.12|while fuser" | grep -v grep || echo "(чисто)"
echo
echo "Apt-локи сейчас:"
fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock 2>&1 \
    || echo "(все 4 свободны)"
