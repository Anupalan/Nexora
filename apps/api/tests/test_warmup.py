"""Warm-up / keep-alive manager: the pure plan, the safety rules, and the admin flow."""

from datetime import datetime, timedelta, timezone

import pytest

from app import warmup
from tests.conftest import Api
from tests.test_gateway import gw  # noqa: F401  (fixture)
from tests.test_router_integration import routed  # noqa: F401  (fixture)

GB = warmup.GB
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def node(resident=None, tags=None, sizes=None, budget=None, reachable=True):
    return {"memory_budget_gb": budget, "state": {
        "reachable": reachable, "tags": tags if tags is not None else {"m": "abc"},
        "sizes": sizes or {"m": 10 * GB}, "resident": resident or {}}}


DEP = {"runtime_model": "m", "name": "d"}


def at(hours):
    return (NOW + timedelta(hours=hours)).isoformat()


# ------------------------------------------------------------------ the pure plan

def test_plan_refreshes_a_resident_model_only_when_the_timer_runs_low():
    assert warmup.plan(DEP, node({"m": {"expires_at": at(5)}}), NOW)[0] == "skip"
    assert warmup.plan(DEP, node({"m": {"expires_at": at(0.5)}}), NOW)[0] == "refresh"
    assert warmup.plan(DEP, node({"m": {}}), NOW)[0] == "refresh"  # unknown expiry: be safe


def test_plan_never_preloads_without_a_memory_budget():
    action, why = warmup.plan(DEP, node(), NOW)
    assert action == "skip" and "no memory budget" in why


def test_plan_preloads_only_what_fits_the_budget():
    other = {"other": {"size": 100 * GB, "expires_at": at(5)}}
    assert warmup.plan(DEP, node(budget=128), NOW)[0] == "load"
    action, why = warmup.plan(DEP, node(other, budget=105), NOW)  # 100 loaded + 10 needed > 105
    assert action == "skip" and "exceed" in why
    assert warmup.plan(DEP, node(other, budget=120), NOW)[0] == "load"


def test_plan_skips_unreachable_nodes_and_uninstalled_models():
    assert warmup.plan(DEP, node(reachable=False, budget=128), NOW)[0] == "skip"
    assert warmup.plan(DEP, node(tags={}, budget=128), NOW)[0] == "skip"


def test_keep_alive_forms():
    assert warmup.keep_alive_seconds("24h") == 86400 and warmup.keep_alive_seconds("90s") == 90
    assert warmup.keep_alive_seconds("-1") is None


def test_parse_expiry_accepts_ollama_timestamps():
    d = warmup.parse_expiry("2026-11-09T19:29:38.849425134+05:30")  # nanoseconds + local offset
    assert d is not None and d.tzinfo is not None
    assert warmup.parse_expiry("garbage") is None and warmup.parse_expiry(None) is None


# ------------------------------------------------------------------ through the admin API

def setup_node(g, budget=None):
    g["ollama"].nodes["dgx1.test"]["sizes"] = {"qwen:8b": 10 * GB}
    g["ollama"].nodes["dgx1.test"]["resident"] = []
    g["ollama"].generates.clear()
    root = g["root"]
    if budget:
        assert root.patch(f"/admin/nodes/{g['nodes']['dgx1']['id']}", {"memory_budget_gb": budget}).status_code == 200
    root.post(f"/admin/nodes/{g['nodes']['dgx1']['id']}/sample")
    return g["dep"]["coder@dgx1"]


def test_warm_now_needs_keep_warm_and_a_budget_to_load(routed):
    g, root = routed, routed["root"]
    dep = setup_node(g)
    assert root.post(f"/admin/deployments/{dep}/warm").json()["action"] == "skip"  # keep_warm is off
    assert root.patch(f"/admin/deployments/{dep}", {"keep_warm": True, "keep_alive": "12h"}).status_code == 200
    res = root.post(f"/admin/deployments/{dep}/warm").json()
    assert res["action"] == "skip" and "memory budget" in res["reason"]
    assert g["ollama"].generates == []  # nothing was sent to the node


def test_warm_now_loads_the_model_with_an_empty_prompt_and_the_configured_keep_alive(routed):
    g, root = routed, routed["root"]
    dep = setup_node(g, budget=64)
    root.patch(f"/admin/deployments/{dep}", {"keep_warm": True, "keep_alive": "12h"})
    res = root.post(f"/admin/deployments/{dep}/warm").json()
    assert res["action"] == "load" and res["ok"] is True
    sent = g["ollama"].generates
    assert len(sent) == 1 and sent[0]["prompt"] == "" and sent[0]["keep_alive"] == "12h"
    assert sent[0]["model"] == "qwen:8b" and sent[0]["host"] == "dgx1.test"
    row = next(d for d in root.get("/admin/deployments").json() if d["id"] == dep)
    assert row["resident"] is True and row["keep_warm"] is True and row["warm"]["ok"] is True  # re-sampled
    again = root.post(f"/admin/deployments/{dep}/warm").json()
    assert again["action"] == "skip" and len(g["ollama"].generates) == 1  # plenty of keep-alive left


def test_a_low_timer_is_refreshed_without_needing_a_budget(routed):
    g, root = routed, routed["root"]
    dep = setup_node(g)
    g["ollama"].nodes["dgx1.test"]["resident"] = ["qwen:8b"]
    g["ollama"].nodes["dgx1.test"]["expires"]["qwen:8b"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    root.post(f"/admin/nodes/{g['nodes']['dgx1']['id']}/sample")
    root.patch(f"/admin/deployments/{dep}", {"keep_warm": True})
    res = root.post(f"/admin/deployments/{dep}/warm").json()
    assert res["action"] == "refresh" and res["ok"] is True
    assert g["ollama"].generates[0]["keep_alive"] == "24h"  # the default


def test_a_failed_load_is_recorded_alerts_after_repeats_and_backs_off(routed):
    import anyio

    g, root = routed, routed["root"]
    dep = setup_node(g, budget=64)
    root.patch(f"/admin/deployments/{dep}", {"keep_warm": True})
    g["ollama"].nodes["dgx1.test"]["generate_fail"] = True
    for _ in range(3):
        res = root.post(f"/admin/deployments/{dep}/warm").json()  # admin action ignores the back-off
        assert res["ok"] is False
    row = next(d for d in root.get("/admin/deployments").json() if d["id"] == dep)
    assert row["warm"]["failures"] == 3 and row["warm"]["ok"] is False
    n = len(g["ollama"].generates)
    root.post("/admin/inference/check")  # the scheduled cycle backs off after a failure
    assert len(g["ollama"].generates) == n
    alerts = root.get("/admin/alerts?status=open").json()
    assert any(a["type"] == "warmup_failed" for a in alerts)
    g["ollama"].nodes["dgx1.test"]["generate_fail"] = False
    assert root.post(f"/admin/deployments/{dep}/warm").json()["ok"] is True
    root.post("/admin/alerts/evaluate")
    assert not any(a["type"] == "warmup_failed" for a in root.get("/admin/alerts?status=open").json())


def test_the_scheduled_cycle_keeps_opted_in_models_warm_and_ignores_the_rest(routed):
    g, root = routed, routed["root"]
    dep = setup_node(g, budget=64)
    root.patch(f"/admin/deployments/{dep}", {"keep_warm": True})
    out = root.post("/admin/inference/check").json()
    assert [w["deployment"] for w in out["warm"]] == ["coder@dgx1"] and out["warm"][0]["action"] == "load"
    assert all(x["host"] == "dgx1.test" for x in g["ollama"].generates)  # dgx2 was never touched


def test_validation_and_audit(routed):
    g, root = routed, routed["root"]
    dep = g["dep"]["coder@dgx1"]
    assert root.patch(f"/admin/deployments/{dep}", {"keep_alive": "forever"}).status_code == 422
    assert root.patch(f"/admin/nodes/{g['nodes']['dgx1']['id']}", {"memory_budget_gb": 0}).status_code == 422
    root.patch(f"/admin/deployments/{dep}", {"keep_warm": True})
    root.post(f"/admin/deployments/{dep}/warm")
    assert "deployment.warm" in {a["action"] for a in root.get("/admin/audit-logs?limit=100").json()}
