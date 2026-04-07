"""Cloud provider abstraction for automated node provisioning.

The goal of this package is to isolate all "talk-to-the-IaaS" code behind
a narrow interface so that the rest of the backend only deals with
`VPNNode` records. Adding a new provider means implementing
:class:`CloudDriver` and registering it in :data:`DRIVERS`.
"""
from __future__ import annotations

from .base import CloudDriver, CloudServer, DriverError, get_driver

__all__ = ["CloudDriver", "CloudServer", "DriverError", "get_driver"]
