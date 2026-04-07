"""Placeholder driver for manually-managed nodes.

``manual`` providers never create or destroy servers — the ops team does it
out of band. The driver exists so that the rest of the backend does not need
to special-case ``provider is None``.
"""
from __future__ import annotations

from .base import CloudServer, DriverError


class ManualDriver:
    kind = "manual"

    def create_server(self, **_: object) -> CloudServer:
        raise DriverError("Manual provider does not support automated creation")

    def destroy_server(self, external_id: str) -> None:
        raise DriverError("Manual provider does not support automated destruction")

    def list_regions(self) -> list[str]:
        return []
