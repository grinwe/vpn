"""PATCH /api/nodes/{id} — правка name/region/pool_id; GET /pools.

Переименование валидируется как inventory-хост + уникальность; pool_id
проверяется на существование и может очищаться (null). host/ssh_port вне
скоупа эндпоинта.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from tests.factories import make_node


def test_update_node_name_and_region(client, db_session: Session) -> None:
    node = make_node(db_session, name="old-name", region="ru-old")
    resp = client.patch(
        f"/api/nodes/{node.id}", json={"name": "ru-msk-01", "region": "ru-msk"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "ru-msk-01"
    assert body["region"] == "ru-msk"
    db_session.expire_all()
    n = db_session.get(models.VPNNode, node.id)
    assert n.name == "ru-msk-01"
    assert n.region == "ru-msk"


def test_update_node_invalid_name_400(client, db_session: Session) -> None:
    node = make_node(db_session, name="okname", region="ru")
    resp = client.patch(f"/api/nodes/{node.id}", json={"name": "Bad_Name!"})
    assert resp.status_code == 400, resp.text
    db_session.expire_all()
    assert db_session.get(models.VPNNode, node.id).name == "okname"


def test_update_node_duplicate_name_409(client, db_session: Session) -> None:
    make_node(db_session, name="node-a", region="ru", host="10.0.0.1")
    b = make_node(db_session, name="node-b", region="ru", host="10.0.0.2")
    resp = client.patch(f"/api/nodes/{b.id}", json={"name": "node-a"})
    assert resp.status_code == 409, resp.text


def test_update_node_pool_assignment_and_clear(client, db_session: Session) -> None:
    node = make_node(db_session, name="pool-node", region="ru")
    pool = models.ServerPool(name="ru-pool")
    db_session.add(pool)
    db_session.commit()
    pid = pool.id

    # несуществующий пул → 400
    bad = client.patch(f"/api/nodes/{node.id}", json={"pool_id": 999999})
    assert bad.status_code == 400, bad.text

    # валидный пул → проставился
    ok = client.patch(f"/api/nodes/{node.id}", json={"pool_id": pid})
    assert ok.status_code == 200, ok.text
    assert ok.json()["pool_id"] == pid

    # очистка пула (null) → обнулился
    clr = client.patch(f"/api/nodes/{node.id}", json={"pool_id": None})
    assert clr.status_code == 200, clr.text
    assert clr.json()["pool_id"] is None


def test_list_pools_sorted_by_name(client, db_session: Session) -> None:
    db_session.add(models.ServerPool(name="pool-z"))
    db_session.add(models.ServerPool(name="pool-a"))
    db_session.commit()
    resp = client.get("/api/pools")
    assert resp.status_code == 200, resp.text
    names = [p["name"] for p in resp.json()]
    assert "pool-a" in names and "pool-z" in names
    assert names == sorted(names)
