#!/usr/bin/env bash
# who_holds_apt.sh — короткая диагностика: КТО ИМЕННО держит apt/dpkg lock
# прямо сейчас + что в systemd, что планируется. Цель — определить настоящего
# виновника зависших bootstrap'ов после ребута.
#
# Запуск:
#   ssh root@<host> 'bash -s' < scripts/who_holds_apt.sh

set -u
hr() { printf '\n=== %s ===\n' "$*"; }

hr "Lock-файлы apt/dpkg — кто держит ПРЯМО СЕЙЧАС"
# -v выдаёт PID + USER + COMMAND по каждому держателю lock'а
for f in /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
         /var/lib/apt/lists/lock /var/cache/apt/archives/lock; do
    echo "--- $f ---"
    if fuser -v "$f" 2>&1 | grep -q .; then
        fuser -v "$f" 2>&1
    else
        echo "(свободен)"
    fi
done

hr "apt/unattended-upgrades — процессы"
ps -ef | grep -E 'apt|dpkg|unattended' | grep -v grep || echo "(нет)"

hr "Состояние apt-daily / unattended таймеров"
for unit in apt-daily.timer apt-daily.service apt-daily-upgrade.timer \
            apt-daily-upgrade.service unattended-upgrades.service; do
    state=$(systemctl is-enabled "$unit" 2>/dev/null || echo "?")
    active=$(systemctl is-active "$unit" 2>/dev/null || echo "?")
    printf '  %-40s enabled=%-10s active=%s\n' "$unit" "$state" "$active"
done

hr "Все таймеры — что должно стартануть ближайшим"
systemctl list-timers --no-pager 2>/dev/null | head -15

hr "cloud-init — закончил инициализацию?"
# cloud-init часто после ребута ещё минут 5-10 что-то делает.
# Если status=done — он не помеха. Если running — это и есть виновник.
cloud-init status --long 2>/dev/null || echo "(cloud-init не установлен)"

hr "Последние 20 строк journalctl об apt/unattended"
journalctl --no-pager -n 500 2>/dev/null \
    | grep -iE 'apt|unattended|dpkg' | tail -20

hr "uptime"
uptime
echo "Время сейчас: $(date)"
