"""Audit hash chain, retention purge and the admin endpoints."""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import auditchain, retention
from app import db as dbm
from app.models import AuditLog, RequestEvent, RoutingDecision, utcnow
from tests.test_gateway import gw  # noqa: F401  (fixture)


def add_rows(n, start=0):
    with Session(dbm.get_engine()) as db:
        for i in range(n):
            db.add(AuditLog(action=f"test.event{start + i}", entity_type="test", entity_id=str(i),
                            meta={"n": i, "nested": {"b": 1, "a": [1, 2]}}, created_at=utcnow() + timedelta(milliseconds=start + i)))
        db.commit()


def test_sealing_numbers_rows_in_order_and_is_idempotent(gw):
    with Session(dbm.get_engine()) as db:
        before = db.scalar(select(AuditLog).order_by(AuditLog.seq.desc()).limit(1))
    add_rows(5)
    with Session(dbm.get_engine()) as db:
        sealed = auditchain.seal(db)
        assert sealed >= 5 and auditchain.seal(db) == 0
        rows = list(db.scalars(select(AuditLog).order_by(AuditLog.seq)))
        assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
        assert rows[0].prev_hash == auditchain.GENESIS
        assert all(rows[i].prev_hash == rows[i - 1].hash for i in range(1, len(rows)))
        v = auditchain.verify(db)
        assert v["ok"] and v["checked"] == len(rows) and v["unsealed"] == 0
        assert v["head"]["seq"] == len(rows)
    _ = before


def test_a_new_batch_continues_the_chain(gw):
    with Session(dbm.get_engine()) as db:
        auditchain.seal(db)
        n1 = auditchain.verify(db)["checked"]
    add_rows(3, start=1000)
    with Session(dbm.get_engine()) as db:
        auditchain.seal(db)
        v = auditchain.verify(db)
    assert v["ok"] and v["checked"] == n1 + 3


def test_altering_a_sealed_row_is_detected(gw):
    add_rows(4)
    with Session(dbm.get_engine()) as db:
        auditchain.seal(db)
        target = db.scalar(select(AuditLog).where(AuditLog.action == "test.event2"))
        # SQLite has no append-only trigger, so we can simulate someone with raw database access
        db.execute(update(AuditLog).where(AuditLog.id == target.id).values(meta={"n": 999}))
        db.commit()
        v = auditchain.verify(db)
    assert v["ok"] is False and v["problem"]["seq"] == target.seq and "altered" in v["problem"]["reason"]


def test_deleting_a_middle_row_is_detected(gw):
    add_rows(4)
    with Session(dbm.get_engine()) as db:
        auditchain.seal(db)
        target = db.scalar(select(AuditLog).where(AuditLog.action == "test.event1"))
        db.delete(target)
        db.commit()
        v = auditchain.verify(db)
    assert v["ok"] is False and "missing" in v["problem"]["reason"]


def test_deleting_the_newest_rows_is_caught_by_an_external_anchor(gw):
    add_rows(3)
    with Session(dbm.get_engine()) as db:
        auditchain.seal(db)
        head = auditchain.verify(db)["head"]
        assert auditchain.verify(db, head["seq"], head["hash"])["ok"]
        db.delete(db.scalar(select(AuditLog).where(AuditLog.seq == head["seq"])))
        db.commit()
        assert auditchain.verify(db)["ok"] is True  # on its own the chain cannot see a missing tail
        v = auditchain.verify(db, head["seq"], head["hash"])
    assert v["ok"] is False and "no longer exists" in v["problem"]["reason"]


def test_a_wrong_anchor_hash_fails(gw):
    add_rows(2)
    with Session(dbm.get_engine()) as db:
        auditchain.seal(db)
        head = auditchain.verify(db)["head"]
        assert auditchain.verify(db, head["seq"], "f" * 64)["ok"] is False


def test_admin_endpoints_verify_retention_and_maintenance(gw):
    root = gw["root"]
    v = root.get("/admin/audit-logs/verify").json()
    assert v["ok"] is True and v["checked"] > 0 and v["unsealed"] == 0  # the endpoint seals first
    rows = root.get("/admin/audit-logs?limit=5").json()
    assert all(r["hash"] for r in rows) and rows[0]["seq"] is not None
    assert root.get("/admin/retention").json()["settings"] == retention.DEFAULTS
    assert root.put("/admin/retention", {"request_events_days": 30}).json()["settings"]["request_events_days"] == 30
    assert root.put("/admin/retention", {"request_events_days": -1}).status_code == 422
    assert root.put("/admin/retention", {"bogus": 1}).status_code == 422
    m = root.post("/admin/maintenance/run").json()
    assert m["audit_chain"]["ok"] is True and set(m["purged"]) == {"request_events", "routing_decisions"}
    assert "retention.update" in {a["action"] for a in root.get("/admin/audit-logs?limit=100").json()}


def test_purge_removes_only_old_detail_rows_and_zero_keeps_everything(gw):
    from tests.test_router_integration import chat, mk_key  # noqa: F401
    root = gw["root"]
    from tests.conftest import Api
    k = mk_key(Api(gw["app"], "deva@x.io"))
    assert chat_ok(gw, k)
    with Session(dbm.get_engine()) as db:
        ev = db.scalar(select(RequestEvent).limit(1))
        assert ev is not None
        total = len(list(db.scalars(select(RequestEvent))))
        old = utcnow() - timedelta(days=200)
        db.execute(update(RequestEvent).where(RequestEvent.id == ev.id).values(created_at=old))
        db.commit()
        retention.save(db, {"request_events_days": 0})
        assert retention.purge(db)["request_events"] == 0  # 0 = keep forever
        retention.save(db, {"request_events_days": 90})
        out = retention.purge(db)
        assert out["request_events"] == 1
        assert len(list(db.scalars(select(RequestEvent)))) == total - 1
    _ = (root, RoutingDecision)


def chat_ok(g, k):
    from tests.test_gateway import bearer
    from tests.conftest import Api
    r = Api(g["app"]).c.post("/v1/chat/completions", headers=bearer(k["key"]),
                             json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]})
    return r.status_code == 200
