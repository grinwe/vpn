#!/usr/bin/env bash
# vpn-db-backup — дамп БД vpn из compose-стека, шифрование и раскладка копий
# по exit-нодам. Ставится ролью infra/ansible/roles/db_backup, конфиг —
# /etc/vpn-db-backup/config. Запускается таймером vpn-db-backup.timer и
# вручную из playbooks/db_backup.yml (тот же скрипт — плановый и ручной
# бэкап не расходятся).
#
#   vpn-db-backup            # дамп → шифр → push на все exit'ы → ротация
#   vpn-db-backup --no-push  # только локальный дамп (+ ротация)
#
# Коды выхода: 0 — всё ок; 1 — дамп не снят или не зашифрован (фатально);
# 2 — дамп на месте, но хотя бы один exit копию не принял (юнит покажет
# failed, локальный дамп цел, следующий прогон повторит).
#
# Восстановление — docs/operations/runbook.md «Бэкапы и восстановление БД».
set -euo pipefail

CONFIG="${VPN_DB_BACKUP_CONFIG:-/etc/vpn-db-backup/config}"
# shellcheck disable=SC1090
. "$CONFIG"

: "${BACKUP_DIR:=/opt/vpn-backups}"
: "${STACK_DIR:=/opt/vpn}"
: "${RETENTION_DAYS:=14}"
: "${REMOTE_RETENTION_DAYS:=30}"
: "${PASSPHRASE_FILE:=/etc/vpn-db-backup/passphrase}"
: "${SSH_KEY:=/root/.ssh/vpn-db-backup_ed25519}"
: "${SSH_CONNECT_TIMEOUT:=25}"
: "${STATE_DIR:=/var/lib/vpn-db-backup}"
: "${PBKDF2_ITER:=600000}"
: "${TARGETS:=}"   # "name=host:port name=host:port ..."

PUSH=1
case "${1:-}" in
    "") ;;
    --no-push) PUSH=0 ;;
    *) echo "usage: vpn-db-backup [--no-push]" >&2; exit 64 ;;
esac

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "FATAL: $*" >&2; exit 1; }

umask 077
mkdir -p "$BACKUP_DIR" "$STATE_DIR"
[[ -r "$PASSPHRASE_FILE" ]] || die "passphrase file $PASSPHRASE_FILE is missing"
[[ -r "$SSH_KEY" ]] || (( PUSH == 0 )) || die "ssh key $SSH_KEY is missing"

STAMP=$(date -u +%Y%m%d-%H%M%S)
DUMP="$BACKUP_DIR/vpn-$STAMP.dump"
ENC="$DUMP.enc"
# Шифрованный файл нужен только на время push'а: убираем его и при обычном
# выходе, и если юнит убьют по таймауту / хост перезагрузят посреди push'а.
trap 'rm -f "$ENC"' EXIT

# ── 1. Дамп (pg_dump custom-format из db-контейнера стека) ─────────────
log "pg_dump -> $DUMP"
if ! ( cd "$STACK_DIR" && docker compose exec -T db pg_dump -U vpn -Fc -d vpn > "$DUMP" ); then
    rm -f "$DUMP"
    die "pg_dump failed"
fi
SIZE=$(stat -c %s "$DUMP")
MAGIC=$(head -c 5 "$DUMP")
if (( SIZE < 4096 )) || [[ "$MAGIC" != "PGDMP" ]]; then
    rm -f "$DUMP"
    die "dump is too small ($SIZE B) or not a pg_dump custom archive"
fi
log "dump OK: $SIZE bytes"
# Строка для playbooks/db_backup.yml (fetch на контроллер).
echo "DUMP=$DUMP"

# ── 2. Шифрование (AES-256-CBC, ключ из парольной фразы через PBKDF2) ──
# Расшифровка: те же флаги + -d, см. runbook.
if ! openssl enc -aes-256-cbc -md sha256 -pbkdf2 -iter "$PBKDF2_ITER" -salt \
        -in "$DUMP" -out "$ENC" -pass "file:$PASSPHRASE_FILE"; then
    rm -f "$ENC"
    die "openssl enc failed (dump kept at $DUMP)"
fi

# ── 3. Раскладка по exit-нодам ─────────────────────────────────────────
# Приёмник фиксирует файл только при совпадении размера и sha256 — обрыв
# связи посреди передачи даёт ему EOF, а не ошибку.
ENC_SIZE=$(stat -c %s "$ENC")
ENC_SHA=$(sha256sum < "$ENC" | cut -c1-64)
declare -A RESULT=()
TOTAL=0
FAILED=0
if (( PUSH )); then
    [[ -n "$TARGETS" ]] || log "WARNING: TARGETS is empty — no off-host copies will be made"
    for entry in $TARGETS; do
        name="${entry%%=*}"
        hostport="${entry#*=}"
        host="${hostport%%:*}"
        port="${hostport##*:}"
        [[ "$port" == "$host" ]] && port=22
        TOTAL=$((TOTAL + 1))
        # LogLevel=ERROR: иначе первое знакомство с хостом (accept-new) печатает
        # «Warning: Permanently added…» в stderr, и оно попадало бы в $out.
        ssh_opts=(
            -i "$SSH_KEY" -p "$port"
            -o BatchMode=yes -o IdentitiesOnly=yes
            -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" -o ConnectionAttempts=2
            -o StrictHostKeyChecking=accept-new
            -o UserKnownHostsFile="$STATE_DIR/known_hosts"
            -o ServerAliveInterval=15 -o ServerAliveCountMax=4
            -o LogLevel=ERROR
        )
        # Судим по ПОСЛЕДНЕЙ строке ответа: там «OK <size>» приёмника или его
        # «ERR: …»; всё остальное (stderr ssh) остаётся в логе для диагностики.
        if out=$(ssh "${ssh_opts[@]}" "root@$host" "put $(basename "$ENC") $ENC_SIZE $ENC_SHA" < "$ENC" 2>&1) \
                && [[ "${out##*$'\n'}" == OK* ]]; then
            RESULT[$name]=ok
            log "push $name ($host): $out"
            ssh "${ssh_opts[@]}" "root@$host" "prune $REMOTE_RETENTION_DAYS" > /dev/null 2>&1 \
                || log "prune on $name failed (non-fatal)"
        else
            RESULT[$name]=fail
            FAILED=$((FAILED + 1))
            log "push $name ($host) FAILED: ${out:-no output}"
        fi
    done
else
    log "push skipped (--no-push)"
fi
rm -f "$ENC"

# ── 4. Ротация локальных дампов (и .enc-огрызков от прерванных прогонов) ─
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'vpn-*.dump' -o -name 'vpn-*.dump.enc' \) \
    -mtime +"$RETENTION_DAYS" -print -delete | sed 's/^/pruned local: /' || true

# ── 5. Статус последнего прогона (для ручной проверки и мониторинга) ───
{
    printf '{"finished_at":"%s","dump":"%s","size_bytes":%s,"pushed":%s,"failed":%s,"targets":{' \
        "$(date -u +%FT%TZ)" "$DUMP" "$SIZE" "$((TOTAL - FAILED))" "$FAILED"
    first=1
    for k in "${!RESULT[@]}"; do
        (( first )) || printf ','
        first=0
        printf '"%s":"%s"' "$k" "${RESULT[$k]}"
    done
    printf '}}\n'
} > "$STATE_DIR/last-run.json"

log "done: pushed $((TOTAL - FAILED))/$TOTAL, failed $FAILED"
(( FAILED == 0 )) || exit 2
