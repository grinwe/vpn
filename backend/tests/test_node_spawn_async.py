"""Async node-spawn tests.

Covers the fix for the ``POST /nodes/spawn`` 502 + orphaned-server bug:

  * 4vps driver ``create_server`` was split into a fast ``order_server``
    (just ``buyServer``) + a blocking ``wait_for_ipv4`` (poll /myservers).
    ``create_server`` must still compose them identically (autoscale path).
  * ``spawn_node_async`` (used by the HTTP route) must record a tracking
    ``VPNNode`` SYNCHRONOUSLY — right after ordering, BEFORE the IP is known —
    so a paid-for server is never orphaned. That row must be ``is_active=False``
    (kept out of ``choose_node``) with a placeholder host until the background
    finalizer fills in the real IP.

The driver tests mock ``_call`` so no network is touched. The spawn test
patches ``_finalize_spawn`` to a no-op so the daemon thread can't race the
test's DB teardown.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.security import decrypt, encrypt
from app.services import node_spawner
from app.services.cloud import DriverError
from app.services.cloud.fourvps import FourVpsDriver
from app.services.node_spawner import SPAWN_PLACEHOLDER_HOST, spawn_node_async


# ── 4vps driver split: order_server / wait_for_ipv4 / create_server ──


def _driver_with_calls(responses: dict[str, object]) -> FourVpsDriver:
    """A FourVpsDriver whose ``_call`` returns canned envelopes keyed by path."""
    driver = FourVpsDriver(token="1:apikey")

    def fake_call(method: str, path: str, params: dict):  # type: ignore[no-untyped-def]
        if path not in responses:
            raise AssertionError(f"unexpected 4vps call: {method} {path}")
        return responses[path]

    driver._call = fake_call  # type: ignore[method-assign]
    return driver


def test_fourvps_order_server_returns_id_and_password() -> None:
    driver = _driver_with_calls(
        {"/action/buyServer": {"serverid": "srv-123", "password": "pw-4vps"}}
    )
    external_id, root_password = driver.order_server(
        name="n", region="1", plan="2", image="3"
    )
    assert external_id == "srv-123"
    assert root_password == "pw-4vps"


def test_fourvps_order_server_raises_without_serverid() -> None:
    driver = _driver_with_calls({"/action/buyServer": {"password": "x"}})
    with pytest.raises(DriverError, match="serverid"):
        driver.order_server(name="n", region="1", plan="2", image="3")


def test_fourvps_wait_for_ipv4_returns_ip_and_cost() -> None:
    driver = _driver_with_calls(
        {
            "/myservers": {
                "serverlist": [
                    {"id": "srv-9", "ipv4": "9.9.9.9", "status": "active", "price": "7"}
                ]
            }
        }
    )
    ipv4, monthly_cost, raw = driver.wait_for_ipv4("srv-9")
    assert ipv4 == "9.9.9.9"
    assert monthly_cost == 7.0
    assert raw["id"] == "srv-9"


def test_fourvps_wait_for_ipv4_raises_on_no_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FourVpsDriver(token="1:apikey")
    # _wait_active polls with sleeps until a 600s deadline — short-circuit it to
    # the "no IP" outcome so the raise path is tested without waiting.
    monkeypatch.setattr(driver, "_wait_active", lambda sid: ("", "installing", {}))
    with pytest.raises(DriverError, match="no IPv4"):
        driver.wait_for_ipv4("srv-9")


def test_fourvps_create_server_composes_order_and_wait() -> None:
    driver = _driver_with_calls(
        {
            "/action/buyServer": {"serverid": "srv-9", "password": "pw9"},
            "/myservers": {
                "serverlist": [
                    {"id": "srv-9", "ipv4": "9.9.9.9", "status": "active", "price": "7"}
                ]
            },
        }
    )
    server = driver.create_server(name="n", region="dc1", plan="t2", image="os3")
    assert server.external_id == "srv-9"
    assert server.ipv4 == "9.9.9.9"
    assert server.monthly_cost == 7.0
    assert server.root_password == "pw9"
    assert server.region == "dc1"
    assert server.plan == "t2"


# ── 4vps offerings parsing (presets live UNDER locations, not at top) ──
# Real getTarifList shape: keys are LOCATIONS, tariffs are the `presets` inside.
_TARIFLIST = {
    "tarifList": {
        "1": {  # ОАЭ — only has cx21 (no cheap cx01)
            "clusterInfo": {"id": 1, "dc_name": "ОАЭ", "flag": "ae", "presets": [15]},
            "presets": {
                "15": {"id": 15, "name": "cx21", "cpu_number": 2, "ram_mib": 4092,
                       "rom": 25600, "commentParsed": {"price": 1080}},
            },
        },
        "10": {  # Финляндия — cx01 + cx21
            "clusterInfo": {"id": 10, "dc_name": "Финляндия", "flag": "fi", "presets": [13, 15]},
            "presets": {
                "13": {"id": 13, "name": "cx01", "cpu_number": 1, "ram_mib": 1024,
                       "rom": 10240, "commentParsed": {"price": 590}},
                "15": {"id": 15, "name": "cx21", "cpu_number": 2, "ram_mib": 4092,
                       "rom": 25600, "commentParsed": {"price": 1080}},
            },
        },
    }
}
_IMAGES = {"images": {"14": "Ubuntu 22.04", "307": "Debian 12", "1098": "Ubuntu 24.04"}}


def test_fourvps_list_plans_returns_presets_not_locations() -> None:
    driver = _driver_with_calls({"/getTarifList": _TARIFLIST})
    plans = driver.list_plans()
    by_id = {p["id"]: p for p in plans}
    # presets (13, 15) — NOT the location keys (1, 10); deduped across locations.
    assert set(by_id) == {13, 15}
    assert by_id[13]["name"] == "cx01"
    assert by_id[13]["price"] == 590.0
    assert by_id[13]["cpu"] == 1
    assert by_id[13]["ram_mib"] == 1024
    assert by_id[13]["rom"] == 10240


def test_fourvps_list_images_with_preset_and_location() -> None:
    driver = _driver_with_calls({"/getImages/13/10": _IMAGES})
    imgs = {i["id"]: i["name"] for i in driver.list_images(13, 10)}
    assert imgs[14] == "Ubuntu 22.04"
    assert imgs[1098] == "Ubuntu 24.04"


def test_fourvps_list_images_noarg_returns_representative_catalog() -> None:
    # No-arg (offerings): driver fetches getImages for the first valid
    # (preset, location) pair — ОАЭ presets[0]=15, loc=1 → /getImages/15/1.
    driver = _driver_with_calls({"/getTarifList": _TARIFLIST, "/getImages/15/1": _IMAGES})
    names = {i["name"] for i in driver.list_images()}
    assert "Ubuntu 22.04" in names


# ── spawn_node_async: synchronous tracking-row creation ──────────────


def _make_4vps_provider(db: Session) -> models.CloudProvider:
    provider = models.CloudProvider(
        name="prov-4vps",
        kind=models.CloudProviderKind.fourvps,
        api_token_enc=encrypt("1:apikey"),
        is_active=True,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def test_spawn_node_async_tracks_server_before_ip(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_4vps_provider(db_session)

    ordered: dict[str, object] = {}

    def fake_order_server(**kwargs):  # type: ignore[no-untyped-def]
        ordered.update(kwargs)
        return "srv-123", "root-pw"

    fake_driver = SimpleNamespace(
        order_server=fake_order_server,
        wait_for_ipv4=lambda sid: ("1.2.3.4", 5.0, {}),
        set_autoprolong=lambda sid, enabled=True: enabled,
    )
    monkeypatch.setattr(node_spawner, "get_driver", lambda _p: fake_driver)

    # Capture (don't run) the background finalizer: assert ONLY the synchronous
    # half here, and keep the daemon thread from racing the test DB teardown.
    started: list[tuple] = []

    class _DummyThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=False):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self) -> None:
            started.append((self.target, self.args, self.kwargs))

    monkeypatch.setattr(node_spawner.threading, "Thread", _DummyThread)

    node = spawn_node_async(
        db_session,
        provider_id=provider.id,
        name="auto-node-1",
        region="dc1",
        plan="t2",
        image="os3",
    )

    # The order happened synchronously and the row tracks the paid-for server.
    assert ordered.get("region") == "dc1"
    assert node.provider_external_id == "srv-123"
    assert node.provider_root_password_enc is not None
    # Placeholder host + inactive ⇒ never selected by choose_node (no broken creds).
    assert node.host == SPAWN_PLACEHOLDER_HOST
    assert node.is_active is False
    assert node.status == models.VPNNodeStatus.registering
    assert node.provider_id == provider.id

    # Reality config is created up-front (host-independent).
    reality = (
        db_session.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality,
        )
        .first()
    )
    assert reality is not None

    # The slow part (poll IP → bootstrap) is deferred to the background
    # finalizer, not run inline.
    assert len(started) == 1
    target, args, _kwargs = started[0]
    assert target is node_spawner._finalize_spawn
    assert args == (node.id,)


def test_spawn_node_async_rejects_inactive_provider(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_4vps_provider(db_session)
    provider.is_active = False
    db_session.commit()
    with pytest.raises(node_spawner.NodeSpawnError, match="inactive"):
        spawn_node_async(
            db_session,
            provider_id=provider.id,
            name="n",
            region="dc1",
            plan="t2",
            image="os3",
        )


# ── reinstall rotates + STORES the root password ─────────────────────
# Reinstall сбрасывает root-пароль; без сохранения мы не сможем зайти по
# паролю и переустановить provisioning-ключ (4vps ключ не инжектит).


def test_reinstall_node_stores_rotated_password(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _make_4vps_provider(db_session)
    node = models.VPNNode(
        name="n1", region="10", host="1.2.3.4",
        status=models.VPNNodeStatus.active, is_active=True,
        provider_id=provider.id, provider_external_id="srv-1",
        provider_root_password_enc=encrypt("old-pw"),
    )
    db_session.add(node)
    db_session.commit()
    db_session.refresh(node)

    used: dict[str, object] = {}

    def fake_reinstall(external_id, image, *, password=None):  # type: ignore[no-untyped-def]
        used["external_id"] = external_id
        used["image"] = image
        used["password"] = password

    monkeypatch.setattr(
        node_spawner, "get_driver",
        lambda _p: SimpleNamespace(reinstall_server=fake_reinstall),
    )
    # Capture (don't run) the background reinstall finalizer — else it would
    # block ~8 min polling SSH on an unreachable host.
    started: list[tuple] = []

    class _DummyThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=False):
            self.target = target
            self.args = args

        def start(self) -> None:
            started.append((self.target, self.args))

    monkeypatch.setattr(node_spawner.threading, "Thread", _DummyThread)

    node_spawner.reinstall_node(db_session, node, image="14")

    # сгенерили пароль, передали драйверу И сохранили (≠ старого)
    assert used["image"] == "14"
    assert used["password"]
    assert decrypt(node.provider_root_password_enc) == used["password"]
    assert decrypt(node.provider_root_password_enc) != "old-pw"
    assert node.status == models.VPNNodeStatus.registering
    # bootstrap отложен в фоновый финализатор (ждёт SSH), не пнут синхронно
    assert len(started) == 1
    assert started[0][0] is node_spawner._reinstall_finalize


# ── provisioning pubkey derivation (для password-инъекции ключа) ─────


def test_provisioning_pubkey_reads_pub_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import ssh_bootstrap

    key = tmp_path / "k"
    key.write_text("PRIV")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAATESTBASE64 comment@host\n")
    monkeypatch.setenv("ANSIBLE_PRIVATE_KEY_FILE", str(key))
    # коммент обрезается, остаётся "<type> <base64>"
    assert ssh_bootstrap.provisioning_pubkey() == "ssh-ed25519 AAAATESTBASE64"


def test_ensure_provisioning_key_no_key_is_falsey(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import ssh_bootstrap

    monkeypatch.delenv("ANSIBLE_PRIVATE_KEY_FILE", raising=False)
    # нет ключа → не бросает, возвращает False (bootstrap не должен падать)
    assert ssh_bootstrap.ensure_provisioning_key("1.2.3.4", "pw") is False
