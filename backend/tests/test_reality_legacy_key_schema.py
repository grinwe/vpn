"""Регресс: reality private_key бывает в ДВУХ схемах.

Новая — private_key_enc (Fernet). Старая (легаси-ноды ufo-ru-01/02/03, aeza) —
private_key плейнтекстом в settings. _collect_site_extra_vars должен читать обе,
иначе на легаси-нодах reality-роль скипается ("Skip role when VLESS Reality is
not configured") → config.json не перерендеривается (dest/унификация не едут).
"""
from __future__ import annotations

from sqlalchemy.orm.attributes import flag_modified

from app import models
from app.security import encrypt
from app.services.provisioning import _collect_site_extra_vars

from .factories import make_config, make_node


def _set_settings(db_session, cfg, **overrides):
    s = dict(cfg.settings or {})
    for k, v in overrides.items():
        if v is None:
            s.pop(k, None)
        else:
            s[k] = v
    cfg.settings = s
    flag_modified(cfg, "settings")
    db_session.commit()


def test_reality_legacy_plaintext_private_key(db_session):
    node = make_node(db_session, name="legacy-node", host="10.0.0.9")
    cfg = make_config(db_session, node, protocol=models.VPNConfigProtocol.vless_reality)
    # легаси-схема: plaintext private_key, БЕЗ private_key_enc
    _set_settings(db_session, cfg, private_key_enc=None, private_key="LEGACY-PLAINTEXT-KEY")
    ev = _collect_site_extra_vars(db_session, node)
    assert ev.get("vless_reality_private_key") == "LEGACY-PLAINTEXT-KEY", ev
    assert ev.get("vless_reality_public_key")  # присутствует → роль НЕ скипнет


def test_reality_new_encrypted_private_key(db_session):
    node = make_node(db_session, name="new-node", host="10.0.0.10")
    cfg = make_config(db_session, node, protocol=models.VPNConfigProtocol.vless_reality)
    _set_settings(db_session, cfg, private_key=None,
                  private_key_enc=encrypt("NEW-ENCRYPTED-KEY"))
    ev = _collect_site_extra_vars(db_session, node)
    assert ev.get("vless_reality_private_key") == "NEW-ENCRYPTED-KEY", ev


def test_reality_legacy_key_encrypted_in_place(db_session):
    """Миграция легаси-секретов (223dd71) зашифровала ключ ПРЯМО в поле
    `private_key`. Без decrypt на этой ветке в config.json уезжает `enc:v1:...`
    → `xray -test`: invalid privateKey → бутстрап легаси-ноды падает."""
    node = make_node(db_session, name="migrated-legacy", host="10.0.0.12")
    cfg = make_config(db_session, node, protocol=models.VPNConfigProtocol.vless_reality)
    _set_settings(db_session, cfg, private_key_enc=None,
                  private_key=encrypt("MIGRATED-LEGACY-KEY"))
    ev = _collect_site_extra_vars(db_session, node)
    assert ev.get("vless_reality_private_key") == "MIGRATED-LEGACY-KEY", ev


def test_reality_legacy_dest_in_camo_dest(db_session):
    """Легаси-ноды хранят reality dest в camo_dest (host:port), не в dest.
    Без fallback reality-роль падает на assert `vless_reality_dest length>0`."""
    node = make_node(db_session, name="legacy-dest-node", host="10.0.0.11")
    cfg = make_config(db_session, node, protocol=models.VPNConfigProtocol.vless_reality)
    _set_settings(db_session, cfg, private_key="K", private_key_enc=None,
                  dest=None, camo_dest="vk.ru:443")
    ev = _collect_site_extra_vars(db_session, node)
    assert ev.get("vless_reality_dest") == "vk.ru:443", ev
