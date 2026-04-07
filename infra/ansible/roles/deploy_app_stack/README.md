# deploy_app_stack

Brings up the full `vpn/` application stack (postgres, redis, backend,
worker, bot, admin SPA) on a single Ubuntu host via docker compose.
Zero-touch: the role installs docker, syncs the repo, renders `.env`
from vaulted secrets, builds images, and waits for backend healthz.

## Usage

One-time setup:

```bash
cd vpn/infra/ansible

# 1. Create the vault with real secrets
cd group_vars/web
cp vault.yml.example vault.yml
$EDITOR vault.yml
ansible-vault encrypt vault.yml
cd ../..

# 2. Put the provisioning SSH private key on the controller and
#    set deploy_app_stack_provisioning_key_src in group_vars/web.yml
#    (leave empty to bootstrap without node provisioning).

# 3. Run
ansible-playbook -i inventories/prod/hosts.yml site.yml \
    --tags web --ask-vault-pass
```

Subsequent code deploys (no nginx/TLS touch):

```bash
ansible-playbook -i inventories/prod/hosts.yml \
    playbooks/deploy_app_stack.yml --ask-vault-pass
```

Force a full recreate (restart all containers even if nothing changed):

```bash
ansible-playbook -i inventories/prod/hosts.yml \
    playbooks/deploy_app_stack.yml --ask-vault-pass \
    -e deploy_app_stack_force_recreate=true
```

## What the role does

1. Asserts vault secrets are present and non-trivial.
2. Installs `docker.io`, `docker-compose-plugin`, `rsync`.
3. Creates `/opt/vpn` and a `0700` secrets directory.
4. rsyncs the repo to `/opt/vpn`, excluding vcs/venvs/node_modules/.env.
5. Renders `.env` (mode `0600`) from `templates/env.j2` using
   `deploy_app_stack_*` vars.
6. Copies the provisioning SSH private key to `/opt/vpn/secrets/`
   (if configured).
7. `docker compose build` only when the sync actually changed files.
8. `docker compose up -d` unconditionally (cheap no-op when nothing
   changed).
9. Polls `http://127.0.0.1:8000/healthz` until 200.

## Idempotency

- Source changes → sync is `changed` → build runs → compose up detects
  new images and recreates affected services.
- `.env` change → template is `changed` → handler triggers
  `docker compose up -d --force-recreate`.
- Nothing changed → sync is no-op, build skipped, `up -d` is a no-op
  in compose (reports "up-to-date" for every service).

## What it does NOT do

- TLS / public nginx — that's `deploy_web_frontend`.
- Database migrations — the backend runs Alembic on startup.
- Node provisioning — that's `site.yml`'s first play on `vpn_nodes`.
