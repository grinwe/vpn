#!/usr/bin/env bash
# db_dump.sh — снять дамп БД vpn с mgmt-хоста на локальную машину.
#
# По умолчанию pg_dump --format=custom (-Fc): один файл, сжатый, его
# восстанавливают через pg_restore с гибкостью (можно selective restore
# отдельных таблиц, перезаписать тип constraint'а и т.д.).
#
# Запуск:
#   ./scripts/db_dump.sh                # custom-формат в ./backups/
#   ./scripts/db_dump.sh --plain        # plain SQL (psql < file для restore)
#   ./scripts/db_dump.sh --schema-only  # только DDL, без данных
#   ./scripts/db_dump.sh --out /tmp     # своя директория для дампа
#
# Env overrides (как в workers.sh / audit_*):
#   MGMT_HOST — IP/hostname. Default: парсится из ansible inventory.
#   MGMT_USER — SSH user. Default: root.
#   STACK_DIR — путь к /opt/vpn на mgmt'е. Default: /opt/vpn.
#
# pipefail важен — без него ssh падает, а $OUT остаётся как «успешный»
# файл с частичным дампом. С pipefail ssh-ошибка ломает весь pipeline,
# мы это видим и подчищаем.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MGMT_USER="${MGMT_USER:-root}"
STACK_DIR="${STACK_DIR:-/opt/vpn}"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/backups}"

FORMAT="custom"   # custom | plain
SCHEMA_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --plain)       FORMAT="plain"; shift ;;
        --custom)      FORMAT="custom"; shift ;;
        --schema-only) SCHEMA_ONLY=true; shift ;;
        --out)         OUT_DIR="$2"; shift 2 ;;
        -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "${MGMT_HOST:-}" ]]; then
    MGMT_HOST=$(python3 -c "
import yaml, sys
try:
    inv = yaml.safe_load(open('$REPO_ROOT/infra/ansible/inventories/prod/hosts.yml'))
    print(inv['all']['children']['db_host']['hosts']['mgmt-1']['ansible_host'])
except Exception as e:
    sys.stderr.write(f'Cannot resolve mgmt host: {e}\n')
    sys.exit(2)
") || {
        echo "Set MGMT_HOST=<ip> or fix inventory parsing" >&2
        exit 2
    }
fi

mkdir -p "$OUT_DIR"

STAMP=$(date +%Y%m%d-%H%M%S)
if [[ "$FORMAT" == "plain" ]]; then
    EXT="sql"
else
    EXT="dump"
fi
$SCHEMA_ONLY && SUFFIX="-schema" || SUFFIX=""
OUT="$OUT_DIR/vpn-${STAMP}${SUFFIX}.${EXT}"

# pg_dump args на удалённой стороне. -Fc для custom — самый удобный
# формат для pg_restore. --no-owner: ускоряем восстановление в локальную
# БД, где владельца "vpn" может не быть. --no-privileges: то же про GRANT'ы.
PG_ARGS=(-U vpn -d vpn --no-owner --no-privileges)
[[ "$FORMAT" == "custom" ]] && PG_ARGS+=(-Fc)
$SCHEMA_ONLY && PG_ARGS+=(--schema-only)

echo "→ Source : ${MGMT_USER}@${MGMT_HOST}:${STACK_DIR}"
echo "→ Format : ${FORMAT}${SCHEMA_ONLY:+, schema-only}"
echo "→ Output : $OUT"
echo

# `docker compose exec -T` — без TTY (мы шлём stdin/stdout как pipe).
# Без -T docker зовёт setpgrp + tcsetpgrp и SIGTTOU'ит pipe — дамп
# обрывается на ~256 байт.
ssh -o BatchMode=yes -o ConnectTimeout=15 "${MGMT_USER}@${MGMT_HOST}" \
    "cd ${STACK_DIR} && docker compose exec -T db pg_dump ${PG_ARGS[*]}" \
    > "$OUT"

# Sanity: pg_dump-custom magic = "PGDMP" в первых 5 байтах. Plain — DDL
# в первых строках. Если получили <1KB или начало явно похоже на ошибку
# (errors|fatal|usage:|denied) — что-то пошло не так, чистим и фейлимся.
SIZE_BYTES=$(stat -c %s "$OUT" 2>/dev/null || stat -f %z "$OUT")
if (( SIZE_BYTES < 1024 )); then
    echo "✗ Дамп подозрительно мал ($SIZE_BYTES байт). Содержимое:" >&2
    head -c 500 "$OUT" >&2
    echo >&2
    rm -f "$OUT"
    exit 3
fi

SIZE_H=$(du -h "$OUT" | cut -f1)
echo "✓ Готово: $OUT ($SIZE_H)"
echo
if [[ "$FORMAT" == "custom" ]]; then
    cat <<EOF
Restore на локальную БД:
    createdb vpn
    pg_restore -h localhost -U vpn -d vpn --no-owner --no-privileges \\
        --clean --if-exists "$OUT"

Selective: распаковать TOC и выбрать таблицы:
    pg_restore -l "$OUT" > /tmp/toc.list
    # отредактировать /tmp/toc.list (удалить ненужные строки)
    pg_restore -L /tmp/toc.list -h localhost -U vpn -d vpn "$OUT"
EOF
else
    cat <<EOF
Restore plain SQL:
    psql -h localhost -U vpn -d vpn -f "$OUT"
EOF
fi
