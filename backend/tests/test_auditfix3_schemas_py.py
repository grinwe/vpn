"""Auditfix3 (schemas.py) — находки 135, 136.

135: в VPNConfigUpdate поле ``protocol`` больше не задублировано.
136: pydantic-схема семпла трафика переименована в NodeTrafficReportSample,
     чтобы не коллизировать с ORM-моделью models.NodeTrafficSample.
"""
from app import schemas


def test_vpnconfig_update_protocol_declared_once():
    # Ровно одно поле protocol в модели.
    assert "protocol" in schemas.VPNConfigUpdate.model_fields
    src_fields = list(schemas.VPNConfigUpdate.model_fields.keys())
    assert src_fields.count("protocol") == 1


def test_node_traffic_report_sample_renamed():
    # Новое имя есть, старое коллизирующее имя из schemas ушло.
    assert hasattr(schemas, "NodeTrafficReportSample")
    assert not hasattr(schemas, "NodeTrafficSample")
    fields = schemas.NodeTrafficReportSample.model_fields
    assert set(fields) == {"access_username", "uplink_bytes", "downlink_bytes"}


def test_node_traffic_report_wire_contract_unchanged():
    # Имя JSON-полей не меняется — контракт node-side reporter'а тот же.
    report = schemas.NodeTrafficReport(
        collected_at="2026-07-04T00:00:00Z",
        samples=[{"access_username": "u1", "uplink_bytes": 1, "downlink_bytes": 2}],
    )
    assert report.samples[0].access_username == "u1"
    assert type(report.samples[0]).__name__ == "NodeTrafficReportSample"
