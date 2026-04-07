"""Helper utilities to invoke Ansible playbooks for provisioning."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import models

def _default_ansible_root() -> Path:
    env = os.getenv("ANSIBLE_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "infra" / "ansible"


ANSIBLE_ROOT = _default_ansible_root()


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
    timeout: int = 300,
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
    # ANSIBLE_PRIVATE_KEY_FILE is a first-class ansible env var, but we also
    # pass it explicitly so that an operator running the worker outside
    # docker-compose can just export a path and have it work.
    private_key = os.getenv("ANSIBLE_PRIVATE_KEY_FILE")
    if private_key:
        cmd.extend(["--private-key", private_key])
    if limit:
        cmd.extend(["--limit", limit])
    if extra_vars:
        cmd.extend(["--extra-vars", json.dumps(extra_vars)])

    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Ansible playbook timed out") from exc
