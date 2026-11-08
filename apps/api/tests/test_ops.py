from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db as dbm
from app import ops
from app.config import get_settings
from app.models import Alert
from tests.conftest import Api
from tests.test_gateway import bearer, gw, mk_key  # noqa: F401  (gw is a fixture)


def chat(c, key, content="hi", **kw):
    return c.post("/v1/chat/completions", headers=bearer(key),
                  json={"model": "coder", "messages": [{"role": "user", "content": content}], **kw})


def open_alerts(root, **q):
    return root.get("/admin/alerts?status=open").json()


# ------------------------------------------------------------------ rule engine (pure)

def inputs(**kw):
    return ops.OpsInputs(**kw)


def dep(**kw):
    base = dict(id="d1", model="coder", health="healthy", depth=0, active=0, max_queue=100, max_active=2)
    return ops.DeploymentState(**{**base, **kw})


def test_rules_fire_and_stay_quiet():
    assert ops.evaluate_rules(inputs(deployments=[dep()])) == {}
    want = ops.evaluate_rules(inputs(
        deployments=[dep(health="unhealthy", depth=25)], avg_queue_wait_ms_5m=45_000, auth_failures_10m=12,
        quotas=[ops.QuotaUse("user:abc", "tokens_per_day", 95, 100), ops.QuotaUse("team:x", "requests_per_day", 10, 10),
                ops.QuotaUse("user:ok", "requests_per_day", 10, 100)]))
    types = {v[0] for v in want.values()}
    assert types == {"inference_unavailable", "queue_depth_high", "queue_wait_high", "repeated_auth_failures",
                     "quota_approaching", "quota_exceeded"}
    assert next(v for v in want.values() if v[0] == "inference_unavailable")[1] == "critical"
    assert not any("user:ok" in k for k in want)  # 10% used: no alert


def test_queue_depth_threshold_scales_with_small_queues():
    # a 5-deep queue limit is 80% full at 4 waiting, well below the default threshold of 20
    assert ops.evaluate_rules(inputs(deployments=[dep(depth=4, max_queue=5)]))


def test_reconcile_dedupes_updates_and_auto_resolves(gw):  # noqa: F811
    with Session(dbm.get_engine()) as db:
        w1 = ops.evaluate_rules(inputs(deployments=[dep(depth=25)]))
        assert ops.reconcile_alerts(db, w1) == {"opened": 1, "resolved": 0, "open": 1}
        assert ops.reconcile_alerts(db, w1)["opened"] == 0  # same condition: no duplicate
        w2 = ops.evaluate_rules(inputs(deployments=[dep(depth=40)]))
        ops.reconcile_alerts(db, w2)
        rows = list(db.scalars(select(Alert)))
        assert len(rows) == 1 and "40" in rows[0].message  # message refreshed in place
        assert ops.reconcile_alerts(db, {}) == {"opened": 0, "resolved": 1, "open": 0}  # condition cleared
        assert db.scalars(select(Alert)).one().status == "resolved"
        ops.reconcile_alerts(db, w1)  # comes back -> a fresh open alert
        assert len(list(db.scalars(select(Alert)))) == 2


# ------------------------------------------------------------------ health + alerts end to end

def test_health_check_marks_unhealthy_alerts_and_recovers(gw):  # noqa: F811
    root, llm = gw["root"], gw["llm"]
    k = mk_key(Api(gw["app"], "deva@x.io"))
    chat(Api(gw["app"]).c, k["key"])  # creates the 'coder' deployment queue state
    res = root.post("/admin/inference/check").json()
    assert all(h["healthy"] for h in res["health"]) and res["alerts"]["open"] == 0
    dep_row = next(d for d in root.get("/admin/inference").json()["deployments"] if d["model"] == "coder")
    assert dep_row["status"] == "healthy" and dep_row["last_check"]["detail"] == "ok"

    llm.unhealthy = True
    res = root.post("/admin/inference/check").json()
    assert not any(h["healthy"] for h in res["health"])
    alerts = open_alerts(root)
    assert {a["type"] for a in alerts} == {"inference_unavailable"} and alerts[0]["severity"] == "critical"
    assert root.get("/admin/dashboard").json()["totals"]["critical_alerts"] == 2  # coder + chat

    llm.unhealthy = False
    root.post("/admin/inference/check")
    assert open_alerts(root) == []  # auto-resolved when the server came back
    resolved = root.get("/admin/alerts?status=resolved").json()
    assert resolved and all(a["auto_resolved"] for a in resolved)


def test_gateway_down_is_reported_unhealthy(gw):  # noqa: F811
    gw["llm"].down = True
    res = gw["root"].post("/admin/inference/check").json()
    assert res["health"] and not any(h["healthy"] for h in res["health"])
    assert "unreachable" in res["health"][0]["detail"]


def test_quota_alerts_from_real_counters(gw):  # noqa: F811
    root = gw["root"]
    root.post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id), "requests_per_day": 2})
    k, c = mk_key(Api(gw["app"], "deva@x.io")), Api(gw["app"]).c
    chat(c, k["key"])
    root.post("/admin/alerts/evaluate")
    assert open_alerts(root) == []  # 1 of 2 used = 50%, below the 90% warning level
    chat(c, k["key"])
    root.post("/admin/alerts/evaluate")
    assert [a["type"] for a in open_alerts(root)] == ["quota_exceeded"]  # 2 of 2


def test_repeated_auth_failures_alert(gw):  # noqa: F811
    c = Api(gw["app"]).c
    for i in range(10):
        c.post("/api/v1/auth/login", json={"email": f"nobody{i}@x.io", "password": "wrong-password-1"})
    gw["root"].post("/admin/alerts/evaluate")
    assert [a["type"] for a in open_alerts(gw["root"])] == ["repeated_auth_failures"]


def test_manual_resolve_reopens_if_condition_persists(gw):  # noqa: F811
    root = gw["root"]
    gw["llm"].unhealthy = True
    root.post("/admin/inference/check")
    first = open_alerts(root)[0]
    assert root.post(f"/admin/alerts/{first['id']}/resolve").json()["status"] == "resolved"
    assert [a for a in open_alerts(root) if a["id"] == first["id"]] == []
    root.post("/admin/alerts/evaluate")  # still unhealthy -> alert is raised again
    assert any(a["type"] == "inference_unavailable" for a in open_alerts(root))
    logs = root.get("/admin/audit-logs?action=alert.resolve").json()
    assert logs and logs[0]["entity_id"] == first["id"]


def test_ops_endpoints_are_admin_only(gw):  # noqa: F811
    dev = Api(gw["app"], "deva@x.io")
    ta = Api(gw["app"], "ta@x.io")
    for path in ("/admin/dashboard", "/admin/inference", "/admin/alerts"):
        assert dev.get(path).status_code == 403 and ta.get(path).status_code == 403
    assert dev.post("/admin/inference/check").status_code == 403
    assert dev.post("/admin/alerts/evaluate").status_code == 403


def test_dashboard_kpis(gw):  # noqa: F811
    root = gw["root"]
    ka, kb, c = mk_key(Api(gw["app"], "deva@x.io")), mk_key(Api(gw["app"], "devb@x.io")), Api(gw["app"]).c
    for key in (ka, ka, kb):
        assert chat(c, key["key"]).status_code == 200
    d = root.get("/admin/dashboard").json()
    t = d["today"]
    assert (t["active_developers"], t["requests"], t["failed_requests"]) == (2, 3, 0)
    assert (t["input_tokens"], t["output_tokens"]) == (9, 3)
    assert d["queue"] == {"depth": 0, "active": 0, "avg_wait_ms_5m": d["queue"]["avg_wait_ms_5m"]}
    assert {m["model"] for m in d["models"]} == {"chat", "coder"} and d["gpu"] is None  # GPU comes from DGX exporter


# ------------------------------------------------------------------ audit search

def test_audit_log_filters(gw):  # noqa: F811
    root = gw["root"]
    assert {a["action"] for a in root.get("/admin/audit-logs?action=team.create").json()} == {"team.create"}
    team_id = gw["team_a"]["id"]
    by_entity = root.get(f"/admin/audit-logs?entity_type=team&entity_id={team_id}").json()
    assert by_entity and all(a["entity_id"] == team_id for a in by_entity)
    assert {a["action"] for a in root.get("/admin/audit-logs?q=project").json()} == {"project.create"}
    assert root.get("/admin/audit-logs?q=zzz-nothing").json() == []
    assert root.get("/admin/audit-logs?from=2020-01-01T00:00:00Z&to=2020-12-31T00:00:00Z").json() == []
    assert len(root.get("/admin/audit-logs?from=2020-01-01T00:00:00Z&limit=3").json()) == 3
    # LIKE wildcards in user input are neutralised, not interpreted
    assert root.get("/admin/audit-logs?q=%25").status_code == 200


# ------------------------------------------------------------------ prometheus

def test_metrics_disabled_until_token_set_and_protected(gw, monkeypatch):  # noqa: F811
    c = Api(gw["app"]).c
    assert c.get("/metrics").status_code == 404  # off by default
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret")
    get_settings.cache_clear()
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers=bearer("wrong")).status_code == 401
    k = mk_key(Api(gw["app"], "deva@x.io"))
    chat(c, k["key"])
    gw["llm"].unhealthy = True
    gw["root"].post("/admin/inference/check")
    body = c.get("/metrics", headers=bearer("scrape-secret")).text
    for needle in ('nexora_up 1.0', 'nexora_queue_depth{deployment="default",model="coder",node=""} 0.0',
                   'nexora_model_healthy{deployment="default",model="coder",node=""} 0.0', 'nexora_requests_total{model="coder",status="completed"} 1.0',
                   'nexora_tokens_total{direction="output",model="coder"}', 'nexora_alerts_open{severity="critical"}'):
        assert needle in body, needle
    assert "scrape-secret" not in body and "sk-nx-" not in body
