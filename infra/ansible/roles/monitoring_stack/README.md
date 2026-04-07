# monitoring_stack

Self-hosted Prometheus + Grafana as a `docker-compose` stack on a single host.

## Design choices

- **127.0.0.1 only.** Grafana binds to `127.0.0.1:3000`, Prometheus to
  `127.0.0.1:9090`. No TLS, no nginx, no public surface. Access is via
  SSH port-forward.
- **Provisioning on disk.** Dashboards and the Prometheus datasource are
  files on disk that Grafana reads at startup — not created through the
  HTTP API. Wipe the container, re-run ansible, you get an identical
  state.
- **Persistent data bind-mounted** to `/opt/vpn-monitoring/data/*` so
  `tar -czf` is a valid backup and a container upgrade cannot accidentally
  nuke the TSDB.
- **Scrape targets** for `vpn-nodes` are expanded from the inventory at
  template time. Adding a node = edit inventory + re-run the playbook.
- **Admin password in vault.** Role refuses to run if the default
  sentinel is still in place.

## Usage

```bash
cd vpn/infra/ansible

# One-time: create a vault file with the grafana admin password.
mkdir -p group_vars/monitoring
cat > group_vars/monitoring/vault.yml <<'EOF'
monitoring_stack_grafana_admin_password: "use-a-real-password-here"
EOF
ansible-vault encrypt group_vars/monitoring/vault.yml

# Apply: installs docker, renders configs, brings the stack up, and
# installs node_exporter on every host in vpn_nodes.
ansible-playbook -i inventories/prod/hosts.yml \
    playbooks/deploy_monitoring.yml --ask-vault-pass

# Access grafana:
ssh -L 3000:localhost:3000 root@45.14.244.140
# → open http://localhost:3000  (user: admin, pass: from vault)
```

## Re-running on config change

The role is fully idempotent:
- Editing `prometheus.yml.j2` triggers a `SIGHUP` reload (no scrape
  downtime).
- Editing `docker-compose.yml.j2` / datasource / dashboards triggers
  `docker compose up -d --force-recreate`.

## Backup / restore

```bash
# On the monitoring host
tar -czf /root/monitoring-backup-$(date +%F).tar.gz -C /opt vpn-monitoring/data
```

Restore = drop the tarball back in place and run the playbook.
