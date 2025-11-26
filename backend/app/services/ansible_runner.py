"""Helper utilities to invoke Ansible playbooks for provisioning."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import models

ANSIBLE_ROOT = Path(__file__).resolve().parents[3] / "infra" / "ansible"


def _ensure_ansible_root() -> None:
    if not ANSIBLE_ROOT.exists() or not ANSIBLE_ROOT.is_dir():
        raise FileNotFoundError(
            f"Ansible root {ANSIBLE_ROOT} not found. Ensure infra/ansible is shipped alongside the backend."
        )
    if not os.access(ANSIBLE_ROOT, os.R_OK):
        raise PermissionError(f"Ansible root {ANSIBLE_ROOT} is not readable by the backend process")


def build_inventory_for_node(node: models.VPNNode, ansible_user: str = "root") -> Path:
    """Generate a temporary inventory file for a single node."""
    _ensure_ansible_root()
    inventory_content = """
all:
  hosts:
    {name}:
      ansible_host: {host}
      ansible_port: {port}
      ansible_user: {user}
  children:
    vpn_nodes:
      hosts:
        {name}:
    db_host:
      hosts: {{}}
""".format(name=node.name, host=node.host, port=node.ssh_port, user=ansible_user)
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix="-inventory.yml")
    handle.write(inventory_content)
    handle.flush()
    return Path(handle.name)


def run_playbook(
    playbook: str,
    inventory: Path,
    *,
    limit: str | None = None,
    extra_vars: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess:
    """Execute an Ansible playbook and return the completed process."""
    _ensure_ansible_root()
    playbook_path = ANSIBLE_ROOT / playbook
    if not playbook_path.exists():
        raise FileNotFoundError(f"Playbook {playbook_path} not found")

    cmd = [
        "ansible-playbook",
        str(playbook_path),
        "-i",
        str(inventory),
    ]
    if limit:
        cmd.extend(["--limit", limit])
    if extra_vars:
        cmd.extend(["--extra-vars", json.dumps(extra_vars)])

    return subprocess.run(cmd, check=False, capture_output=True, text=True)
