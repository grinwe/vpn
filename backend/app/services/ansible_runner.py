"""Helper utilities to invoke Ansible playbooks for provisioning."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import models

# Strict whitelists for values that get interpolated into the dynamically
# rendered inventory YAML (see ``build_inventory_for_node``). Because that
# renderer uses a raw ``str.format()`` on a template, any newline, colon,
# or quote in an unchecked value would break out of its intended position
# and let the caller inject arbitrary inventory keys — effectively
# overriding ``ansible_host`` / ``ansible_user`` / ``--private-key`` and
# redirecting the next playbook run to an attacker-controlled box using
# the backend's real SSH credentials.
#
# Names follow a narrow DNS-ish shape: lowercase alnum + hyphen, max 63
# chars, must not start with a hyphen (kills ``--limit=-foo`` getopt
# ambiguity). Hosts accept letters, digits, dots, hyphens, colons and
# square brackets — enough for IPv4, IPv6 (bracketed or bare), and DNS
# names — but reject whitespace, quotes, and every YAML metacharacter.
# ssh_port is range-checked in Python; ``int`` typing alone would allow
# zero and negatives past the template.
_NODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NODE_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")


class InvalidNodeIdentity(ValueError):
    """Raised when a VPNNode's identity fields can't be safely embedded in YAML.

    Hard contract violation: the backend refuses to render an inventory
    for this node. Callers should treat it as non-retriable (the node
    either was mis-created and needs a fix, or is an active injection
    attempt). Never swallow this into a generic retry loop — log and
    mark the task/node as permanently broken instead.
    """


def validate_node_name(name: str | None) -> None:
    """Reject names that wouldn't be safe as an inventory hostname.

    Split out so API routes that only have a name (``/nodes/spawn``,
    where the host is resolved later by the cloud driver) can do an
    early check without fabricating a bogus host/port pair.
    """
    if not isinstance(name, str) or not _NODE_NAME_RE.match(name):
        raise InvalidNodeIdentity(
            f"node.name={name!r} does not match [a-z0-9][a-z0-9-]{{0,62}}"
        )


def validate_node_identity_fields(
    name: str | None, host: str | None, ssh_port: int | None
) -> None:
    """Reject values that would poison the inventory template.

    Public helper — used both by :func:`_validate_node_for_inventory`
    (service-layer defence-in-depth) and by API routes that accept raw
    ``VPNNodeCreate`` payloads so bad input gets a 400 instead of
    silently landing in the DB and failing later.

    Raises :class:`InvalidNodeIdentity` with a human-readable message
    naming the first field that failed.
    """
    validate_node_name(name)
    if not isinstance(host, str) or not _NODE_HOST_RE.match(host):
        raise InvalidNodeIdentity(
            f"node.host={host!r} has characters not allowed in inventory YAML"
        )
    if not isinstance(ssh_port, int) or isinstance(ssh_port, bool):
        raise InvalidNodeIdentity(
            f"node.ssh_port={ssh_port!r} must be int"
        )
    if ssh_port < 1 or ssh_port > 65535:
        raise InvalidNodeIdentity(
            f"node.ssh_port={ssh_port!r} is out of range [1, 65535]"
        )


def _validate_node_for_inventory(node: models.VPNNode) -> None:
    """Wrap :func:`validate_node_identity_fields` for an ORM ``VPNNode``."""
    validate_node_identity_fields(node.name, node.host, node.ssh_port)


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


def _ensure_ssh_control_path_dir() -> None:
    """Make sure the ssh ControlMaster socket directory exists.

    ``infra/ansible/ansible.cfg`` sets
    ``ControlPath=~/.ansible/cp/%h-%p-%r``. With the ``cwd=ANSIBLE_ROOT``
    fix that finally lets ansible.cfg load, that ControlPath becomes
    effective — but ssh refuses to create the socket if the *parent*
    directory doesn't exist, dying with
    ``unix_listener: cannot bind to path … No such file or directory``
    on the first task of every play. The fresh backend container has
    neither ``/root/.ansible`` nor ``/root/.ansible/cp``, so mkdir it
    idempotently before every run. Cheaper than patching ansible.cfg
    (ControlMaster + pipelining are a real speedup for multi-task plays
    like ``site.yml`` and the diagnose playbook, which would otherwise
    open a fresh ssh connection per task).
    """
    control_dir = Path.home() / ".ansible" / "cp"
    control_dir.mkdir(parents=True, exist_ok=True)


def build_inventory_for_node(node: models.VPNNode, ansible_user: str = "root") -> Path:
    """Generate a temporary inventory file for a single node.

    The returned path points at a ``delete=False`` temp file — **the
    caller is responsible for unlinking it** in a ``finally`` block,
    otherwise ``/tmp`` accumulates one inventory per playbook run. Every
    existing call site wraps the run in ``try/finally`` with
    ``inventory.unlink()`` — grep for ``build_inventory_for_node``
    before adding a new caller.

    Raises :class:`InvalidNodeIdentity` if any of ``node.name``,
    ``node.host`` or ``node.ssh_port`` would corrupt the YAML or inject
    inventory keys. The validation runs *before* any filesystem work,
    so a rejected call leaves ``/tmp`` untouched.
    """
    _ensure_ansible_root()
    _validate_node_for_inventory(node)
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


def build_inventory_for_exit_node(
    exit_node: models.WGExitNode, ansible_user: str = "root"
) -> Path:
    """Generate a single-host inventory for a WG exit node.

    Mirrors :func:`build_inventory_for_node` but puts the host under the
    ``wg_exit_nodes`` group so ``bootstrap_exit.yml`` (which targets
    ``hosts: wg_exit_nodes``) matches. Identity fields are validated
    through the same whitelist to keep the inventory YAML
    injection-proof; an exit node whose name/host/ssh_port don't match
    the regex is rejected with :class:`InvalidNodeIdentity`.

    Caller must unlink the returned temp file in a ``finally`` block.
    """
    _ensure_ansible_root()
    validate_node_identity_fields(exit_node.name, exit_node.host, exit_node.ssh_port)
    inventory_content = """
all:
  hosts:
    {name}:
      ansible_host: {host}
      ansible_port: {port}
      ansible_user: {user}
  children:
    wg_exit_nodes:
      hosts:
        {name}:
""".format(
        name=exit_node.name,
        host=exit_node.host,
        port=exit_node.ssh_port,
        user=ansible_user,
    )
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
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Execute an Ansible playbook and return the completed process.

    ``timeout`` defaults to ``ANSIBLE_PLAYBOOK_TIMEOUT`` env var (300s).
    Override at call sites where the playbook is genuinely longer —
    e.g. bootstrapping a new node that pulls packages — so ops can
    tune without a code change.
    """
    if timeout is None:
        timeout = int(os.getenv("ANSIBLE_PLAYBOOK_TIMEOUT", "300"))
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

    # Run from ANSIBLE_ROOT so that `ansible.cfg` sitting next to `site.yml`
    # is picked up (ansible only consults the cfg at cwd, ~/.ansible.cfg,
    # or /etc/ansible/ansible.cfg — never next to the invoked playbook).
    # Without this:
    #   - `roles_path = roles:playbooks/../roles` from ansible.cfg is ignored,
    #     so `playbooks/diagnose_node.yml` cannot find `check_node_health`
    #     (default lookup is `playbooks/roles/`, which doesn't exist).
    #   - ssh ControlMaster + pipelining are silently disabled, which means
    #     every task opens a fresh ssh connection.
    # `site.yml` happens to work without cwd only because its `roles/` is
    # adjacent to the playbook — a coincidence, not a design.
    #
    # Second catch after cwd is set: ansible.cfg enables ControlMaster with
    # ``ControlPath=~/.ansible/cp/…``, so ssh needs that parent directory
    # to exist before it can create sockets. Fresh backend containers
    # don't have it → mkdir -p it here.
    _ensure_ssh_control_path_dir()
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ANSIBLE_ROOT),
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.TimeoutExpired несёт частичный stdout/stderr до
        # момента kill — без этого в UI /admin/tasks видно только
        # голое "timed out" и непонятно какой play/task завис.
        # Пишем tail в сообщение, чтобы _mark_task->error_message
        # показывало последний прогресс Ansible.
        def _tail(buf: bytes | str | None, n: int = 40) -> str:
            if not buf:
                return ""
            s = buf.decode(errors="replace") if isinstance(buf, bytes) else buf
            lines = [ln for ln in s.splitlines() if ln.strip()]
            return "\n".join(lines[-n:])

        tail_stdout = _tail(exc.stdout)
        tail_stderr = _tail(exc.stderr)
        parts = [f"Ansible playbook timed out after {timeout}s"]
        if tail_stderr:
            parts.append(f"--- stderr tail ---\n{tail_stderr}")
        if tail_stdout:
            parts.append(f"--- stdout tail ---\n{tail_stdout}")
        raise RuntimeError("\n".join(parts)) from exc
