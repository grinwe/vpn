#!/usr/bin/env bash
# diag_xray_runtime_all.sh — собрать runtime-state со ВСЕХ vpn_nodes
# через playbooks/recover_xray_runtime.yml.
#
# Запуск с дев-машины:
#   bash scripts/diag_xray_runtime_all.sh
#
# Что вытаскиваем с каждой ноды:
#   - stats emails из reality + xhttp xray-инстансов (gRPC api)
#   - access-логи (включая ротированные .gz) — все встреченные emails
#
# Output:
#   infra/ansible/recovered-runtime/<host>.json
#
# Этот скрипт — тонкая обёртка, чтобы не пугать длинным
# ansible-playbook -i ... --ask-vault-pass ... ну ты понял.

set -eu
cd "$(dirname "$0")/.."

ansible-playbook \
  -i infra/ansible/inventories/prod/hosts.yml \
  infra/ansible/playbooks/recover_xray_runtime.yml \
  --ask-vault-pass
