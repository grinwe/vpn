#!/usr/bin/env python3
"""Мини-клиент админ-API прода: ``admin_api.py METHOD PATH [JSON_BODY]``.

Токен ``vault_admin_api_token`` читается из vault через subprocess и в вывод не
попадает. HTTP-статус печатается в stderr, тело — в stdout (JSON с отступами,
если это JSON). База — ``ADMIN_API_BASE`` (по умолчанию https://grinwer.online).

    ./admin_api.py GET /api/nodes > nodes.json
    ./admin_api.py GET /api/exits > exits.json
    ./admin_api.py GET /api/users/1 > user1.json
    ./admin_api.py POST /api/subscriptions/21/devices '{}'      # тест-устройство
    ./admin_api.py POST /api/devices/<id>/revoke '{}'           # убрать после проверки

Запись в прод (POST) классификатор агентской сессии блокирует — такие вызовы
запускает оператор (``!`` в Claude Code, вывод в файл, stdin из /dev/null).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[2] / "infra" / "ansible"
BASE = os.getenv("ADMIN_API_BASE", "https://grinwer.online")


def token() -> str:
    out = subprocess.run(
        ["ansible-vault", "view", "group_vars/web/vault.yml",
         "--vault-password-file", os.path.expanduser("~/.vpn_vault_pass")],
        cwd=ANSIBLE, capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("vault_admin_api_token:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("vault_admin_api_token not found in vault")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    method, path = sys.argv[1], sys.argv[2]
    body = sys.argv[3] if len(sys.argv) > 3 else None
    cmd = ["curl", "-sS", "-m", "120", "-X", method, "-w", "\n%{http_code}",
           "-H", f"X-Admin-Token: {token()}",
           "-H", f"X-Admin-Actor: {os.getenv('ADMIN_ACTOR', 'egress-probe')}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data", body]
    cmd.append(BASE + path)
    res = subprocess.run(cmd, capture_output=True, text=True)
    raw, _, code = res.stdout.rpartition("\n")
    print(f"HTTP {code}", file=sys.stderr)
    try:
        print(json.dumps(json.loads(raw), ensure_ascii=False, indent=1))
    except ValueError:
        print(raw)


if __name__ == "__main__":
    main()
