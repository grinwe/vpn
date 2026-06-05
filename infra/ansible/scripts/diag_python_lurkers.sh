#!/usr/bin/env bash
# diag_python_lurkers.sh — выяснить кто и зачем запускает «голые»
# python3.12-процессы на хосте.
#
# Симптом: `ps -ef | grep python` показывает несколько пар:
#   /bin/sh -c /usr/bin/python3.12 && sleep 0
#   /usr/bin/python3.12
# без явных аргументов. По cmdline понять что они делают невозможно
# — нужно копаться в /proc/<pid>/ + systemd journal + cron.
#
# Запуск на ноде/mgmt:
#   bash diag_python_lurkers.sh > lurkers.out 2>&1
#
# Или одной командой с dev-машины:
#   ssh root@<host> 'bash -s' < scripts/diag_python_lurkers.sh
#
# Не требует прав кроме root. Read-only, ничего не модифицирует.

set -u

hr() { printf '\n=== %s ===\n' "$*"; }

hr "Все python-процессы с полной cmdline (включая null-аргументы)"
# `ps` собирает cmdline через спейс-сепаратор и обрезает аргументы с null.
# Парсим /proc/PID/cmdline напрямую — там null-separated, видны все args.
ps -eo pid,ppid,etime,user,comm | awk 'NR==1 || /python/' | while IFS= read -r line; do
    pid=$(echo "$line" | awk '{print $1}')
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
        cmdline=""
        if [ -r "/proc/$pid/cmdline" ]; then
            cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" | sed 's/ $//')
        fi
        printf '%-50s cmdline=[%s]\n' "$line" "$cmdline"
    else
        echo "$line"
    fi
done

hr "Дерево процессов (фокус на python и шеллы)"
# pstree даёт визуально красивее, но не всегда установлен — fallback на ps.
if command -v pstree >/dev/null 2>&1; then
    pstree -palT | grep -E 'python|sh -c' | head -40
else
    ps -ef --forest | grep -vE 'grep|forest' | head -60
fi

hr "Для каждого подозрительного python3.12 — детальный inspect"
# Подозрительный = python3.12 без явного скрипта в argv
for pid in $(ps -eo pid,cmd | awk '/python3\.12/ && !/grep/ && !/diag_/ {print $1}'); do
    [ -e "/proc/$pid" ] || continue
    echo
    echo "--- PID $pid ---"
    echo "comm:    $(cat /proc/$pid/comm 2>/dev/null)"
    echo "ppid:    $(awk '{print $4}' /proc/$pid/stat 2>/dev/null)"
    echo "started: $(stat -c '%y' /proc/$pid 2>/dev/null)"
    echo "cwd:     $(readlink /proc/$pid/cwd 2>/dev/null)"
    echo "exe:     $(readlink /proc/$pid/exe 2>/dev/null)"
    echo "cmdline: $(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)"
    echo "args from /proc/cmdline (newlines = arg-separator):"
    tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | nl | sed 's/^/  /'
    echo "open fds (first 10):"
    ls -la "/proc/$pid/fd/" 2>/dev/null | head -12 | sed 's/^/  /'
    echo "open files (lsof, если есть):"
    if command -v lsof >/dev/null 2>&1; then
        lsof -p "$pid" 2>/dev/null | head -15 | sed 's/^/  /'
    else
        echo "  (lsof не установлен)"
    fi
    echo "stack (как процесс что-то ждёт):"
    cat "/proc/$pid/stack" 2>/dev/null | head -5 | sed 's/^/  /' || echo "  (нет CAP_SYS_PTRACE / kernel.yama)"
    echo "syscall (что сейчас в kernel):"
    cat "/proc/$pid/syscall" 2>/dev/null | sed 's/^/  /' || echo "  (n/a)"
    echo "parent process info:"
    ppid=$(awk '{print $4}' /proc/$pid/stat 2>/dev/null)
    if [ -n "$ppid" ] && [ -e "/proc/$ppid" ]; then
        echo "  parent_cmdline: $(tr '\0' ' ' < /proc/$ppid/cmdline 2>/dev/null)"
        echo "  parent_comm:    $(cat /proc/$ppid/comm 2>/dev/null)"
        gppid=$(awk '{print $4}' /proc/$ppid/stat 2>/dev/null)
        if [ -n "$gppid" ] && [ -e "/proc/$gppid" ]; then
            echo "  grandparent:    $(tr '\0' ' ' < /proc/$gppid/cmdline 2>/dev/null)"
        fi
    fi
done

hr "Cron-задачи (системные и юзерские)"
ls -la /etc/cron.d/ /etc/cron.daily/ /etc/cron.hourly/ /etc/cron.weekly/ 2>/dev/null | head -30
echo "--- crontab -l ---"
crontab -l 2>/dev/null || echo "(нет user-crontab у root)"
echo "--- systemd timers ---"
systemctl list-timers --no-pager --all 2>/dev/null | head -20

hr "Что в последних 50 строках journalctl упоминает python"
journalctl --no-pager -n 200 2>/dev/null | grep -iE 'python|cron|unattended' | tail -50

hr "Сетевые соединения python-процессов (что куда коннектится)"
for pid in $(ps -eo pid,cmd | awk '/python3\.12/ && !/grep/ && !/diag_/ {print $1}'); do
    [ -e "/proc/$pid" ] || continue
    echo "--- PID $pid network ---"
    ss -tnp 2>/dev/null | grep "pid=$pid" | head -10 | sed 's/^/  /'
done

hr "Lock-файлы которые python-ы могут держать"
echo "--- apt/dpkg locks ---"
fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock 2>&1 || echo "(все 4 apt/dpkg lock'а свободны)"
echo "--- системные lock-файлы ---"
ls -la /var/run/*.lock /var/run/*.pid 2>/dev/null | head -10

hr "Готово"
