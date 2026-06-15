"""Base types for cloud provider drivers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ... import models
from ...security import decrypt


class DriverError(RuntimeError):
    """Raised when a cloud provider operation fails."""


@dataclass
class CloudServer:
    """Provider-agnostic representation of a created server."""

    external_id: str
    ipv4: str
    ipv6: str | None = None
    region: str | None = None
    plan: str | None = None
    monthly_cost: float | None = None
    # Рут-пароль, выданный хостером при заказе (для провайдеров без инъекции
    # SSH-ключа, напр. 4vps — нужен для first-connect SSH перед установкой
    # нашего ключа). Не логировать. None если хостер инжектит ключ.
    root_password: str | None = field(default=None, repr=False)
    raw: dict | None = field(default=None, repr=False)


class CloudDriver(Protocol):
    """All cloud providers must implement this interface."""

    kind: str

    def create_server(
        self,
        *,
        name: str,
        region: str,
        plan: str,
        image: str,
        ssh_key_ids: list[str] | None = None,
        user_data: str | None = None,
    ) -> CloudServer: ...

    def destroy_server(self, external_id: str) -> None: ...

    def list_regions(self) -> list[str]: ...

    # ── Опциональные capabilities (не все провайдеры умеют). Вызывающий код
    # проверяет наличие через hasattr перед вызовом — Protocol тут документирует
    # контракт, но не обязывает существующие драйверы их реализовывать. ──

    def reinstall_server(
        self, external_id: str, image: str, *, password: str | None = None
    ) -> None:
        """Переустановить ОС на сервере (in-place, IP сохраняется)."""
        ...

    def list_plans(self) -> list[dict]:
        """Тарифы провайдера для admin-формы заказа: ``[{id, name, ...}]``."""
        ...

    def list_images(self) -> list[dict]:
        """OS-образы провайдера: ``[{id, name, ...}]``."""
        ...

    def get_balance(self) -> float | None:
        """Текущий баланс аккаунта у провайдера (единицы — как у провайдера)."""
        ...

    def renew_server(self, external_id: str) -> None:
        """Продлить аренду сервера (списывает с баланса)."""
        ...

    def set_autoprolong(self, external_id: str, enabled: bool = True) -> bool:
        """Вкл/выкл авто-продление на стороне провайдера. Возвращает состояние."""
        ...

    # ── Async-spawn split (только драйверы, чей create_server БЛОКИРУЕТ до
    # выдачи IP). HTTP-роут /nodes/spawn использует эту пару вместо create_server,
    # чтобы быстро (за секунды) зафиксировать заказанный сервер в БД и не убивать
    # воркер долгим поллингом. См. node_spawner.spawn_node_async. ──

    def order_server(
        self,
        *,
        name: str,
        region: str,
        plan: str,
        image: str,
        ssh_key_ids: list[str] | None = None,
        user_data: str | None = None,
    ) -> tuple[str, str]:
        """Быстрый заказ БЕЗ ожидания IP → ``(external_id, root_password)``."""
        ...

    def wait_for_ipv4(self, external_id: str) -> tuple[str, float | None, dict]:
        """Дождаться IP+active для заказанного сервера → ``(ipv4, monthly_cost, raw)``."""
        ...


def get_driver(provider: models.CloudProvider) -> CloudDriver:
    """Instantiate a driver for the given provider record."""
    token = decrypt(provider.api_token_enc) if provider.api_token_enc else None
    kind = provider.kind.value if hasattr(provider.kind, "value") else str(provider.kind)

    if kind == "hetzner":
        from .hetzner import HetznerDriver

        if not token:
            raise DriverError("Hetzner provider has no API token configured")
        return HetznerDriver(token=token)

    if kind == "vultr":
        from .vultr import VultrDriver

        if not token:
            raise DriverError("Vultr provider has no API token configured")
        return VultrDriver(token=token)

    if kind == "digitalocean":
        from .digitalocean import DigitalOceanDriver

        if not token:
            raise DriverError("DigitalOcean provider has no API token configured")
        return DigitalOceanDriver(token=token)

    if kind == "aeza":
        from .aeza import AezaDriver

        if not token:
            raise DriverError("Aeza provider has no API token configured")
        return AezaDriver(token=token)

    if kind == "4vps":
        from .fourvps import FourVpsDriver

        if not token:
            raise DriverError("4vps provider has no API token configured")
        return FourVpsDriver(token=token)

    if kind in ("vdsina", "vdsina_ru"):
        from .vdsina import VdsinaDriver

        if not token:
            raise DriverError("VDSina provider has no API token configured")
        # vdsina = .com (драйверный дефолт _BASE), vdsina_ru = .ru-инсталляция.
        # Токен доменно-специфичен (см. vdsina.py). ssh_key_ids нужны reinstall'у
        # (его сигнатура их не получает) — переинжектить наш ключ при ротации ОС.
        base = "https://userapi.vdsina.ru/v1" if kind == "vdsina_ru" else None
        return VdsinaDriver(
            token=token, ssh_key_ids=provider.ssh_key_ids or [], base=base
        )

    if kind == "billmgr":
        from .billmgr import BillmgrDriver

        if not token:
            raise DriverError("billmgr provider has no API token configured")
        # token — JSON {base_url, username, password} (драйвер парсит сам).
        return BillmgrDriver(token=token)

    if kind == "manual":
        from .manual import ManualDriver

        return ManualDriver()

    raise DriverError(f"Unsupported cloud provider kind: {kind}")
