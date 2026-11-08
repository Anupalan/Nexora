"""Routing end to end: real Redis and database, fake LiteLLM and fake Ollama nodes."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db as dbm
from app import inference
from app.models import InferenceRequest, RoutingDecision, UsageRecord
from tests.conftest import Api
from tests.test_gateway import bearer, gw, mk_key  # noqa: F401  (gw is a fixture)
from tests.test_scheduler import aclient, post, settings, until  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


BODY = {"model": "coder", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 5}
CONN_ERR = "litellm.InternalServerError: InternalServerError: OpenAIException - Connection error."


@pytest.fixture
def routed(gw, ollama):  # noqa: F811
    """Model `coder` with two single-slot deployments on two nodes; the default deployment is disabled."""
    root = gw["root"]
    ollama.add("dgx1.test", version="0.40.2", tags={"qwen:8b": "d1d1d1d1d1d1"}, resident=["qwen:8b"])
    ollama.add("dgx2.test", version="0.40.5", tags={"qwen:8b": "d1d1d1d1d1d1"}, resident=["qwen:8b"])
    n1 = root.post("/admin/nodes", {"name": "dgx1", "runtime_url": "http://dgx1.test:11434"}).json()
    n2 = root.post("/admin/nodes", {"name": "dgx2", "runtime_url": "http://dgx2.test:11434"}).json()
    coder = next(m for m in root.get("/admin/models").json() if m["model_alias"] == "coder")
    for d in root.get(f"/admin/deployments?model_id={coder['id']}").json():
        root.patch(f"/admin/deployments/{d['id']}", {"enabled": False})  # leave only the routed pair
    ids = {}
    for name, node in (("coder@dgx1", n1), ("coder@dgx2", n2)):
        r = root.post("/admin/deployments", {
            "model_id": coder["id"], "name": name, "node_id": node["id"], "litellm_model": name.replace("@", "-"),
            "runtime_model": "qwen:8b", "max_active_requests": 1, "load_seconds": 8})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    for n in (n1, n2):
        root.post(f"/admin/nodes/{n['id']}/sample")
    gw.update(coder=coder, nodes={"dgx1": n1, "dgx2": n2}, dep=ids, ollama=ollama)
    return gw


def chat(c, key, **kw):
    return c.post("/v1/chat/completions", headers=bearer(key), json={**BODY, **kw})


def resample(g):
    for n in g["nodes"].values():
        g["root"].post(f"/admin/nodes/{n['id']}/sample")


def decisions(rid):
    with Session(dbm.get_engine()) as db:
        return [(d.attempt, d.strategy, str(d.chosen_deployment_id), d.reason, str(d.failover_from_deployment_id)
                 if d.failover_from_deployment_id else None)
                for d in db.scalars(select(RoutingDecision).where(RoutingDecision.request_id == uuid.UUID(rid))
                                    .order_by(RoutingDecision.attempt))]


# ============================================================ steering and explanations

def test_a_loaded_model_beats_a_cold_one_and_the_decision_is_explainable(routed):
    g = routed
    g["ollama"].nodes["dgx1.test"]["resident"] = []  # dgx1 is idle but cold
    resample(g)
    k = mk_key(Api(g["app"], "deva@x.io"))
    r = chat(Api(g["app"]).c, k["key"])
    assert r.status_code == 200
    assert r.headers["x-nexora-deployment"] == "coder@dgx2" and r.headers["x-nexora-model"] == "coder"
    assert g["llm"].calls_to("/v1/chat/completions")[-1]["body"]["model"] == "coder-dgx2"  # that deployment's name

    rid = r.headers["x-nexora-request-id"]
    owner = Api(g["app"], "deva@x.io").get(f"/routing/explain/{rid}").json()
    d = owner["decisions"][0]
    assert d["chosen_deployment_id"] == g["dep"]["coder@dgx2"] and "already loaded" in d["reason"]
    by = {c["deployment"]: c for c in d["candidates"]}
    assert by["coder@dgx1"]["resident"] is False and by["coder@dgx1"]["cold_load_s"] == 8.0
    assert by["coder@dgx2"]["resident"] is True and by["coder@dgx2"]["score"] < by["coder@dgx1"]["score"]
    # visibility: owner and admin yes, someone else no
    assert Api(g["app"], "devb@x.io").get(f"/routing/explain/{rid}").status_code == 404
    assert g["root"].get(f"/routing/explain/{rid}").status_code == 200
    assert g["root"].get("/admin/routing/decisions").json()[0]["request_id"] == rid


@pytest.mark.anyio
async def test_load_spreads_across_both_deployments_and_each_queue_stays_fifo(routed):
    g = routed
    g["llm"].gate = asyncio.Event()
    k = mk_key(Api(g["app"], "deva@x.io"))["key"]
    async with aclient(g["app"]) as c:
        tasks = []
        try:
            for i in range(4):
                tasks.append(asyncio.create_task(post(c, k, f"m{i}", max_tokens=5)))
                await until(lambda i=i: _count_rows() == i + 1 and all(r.queue_sequence for r in _rows()))
            by_dep = {}
            for r in _rows():
                by_dep.setdefault(str(r.deployment_id), []).append(r.queue_sequence)
            assert sorted(len(v) for v in by_dep.values()) == [2, 2]  # 2 + 2, not 4 + 0
            assert all(v == sorted(v) for v in by_dep.values())
        finally:
            g["llm"].gate.set()
        res = await asyncio.gather(*tasks)
    assert [r.status_code for r in res] == [200] * 4
    names = sorted(r.headers["x-nexora-deployment"] for r in res)
    assert names == ["coder@dgx1", "coder@dgx1", "coder@dgx2", "coder@dgx2"]
    models = [x["body"]["model"] for x in g["llm"].calls_to("/v1/chat/completions")]
    assert sorted(models) == ["coder-dgx1", "coder-dgx1", "coder-dgx2", "coder-dgx2"]


def _rows():
    with Session(dbm.get_engine()) as db:
        out = list(db.scalars(select(InferenceRequest).order_by(InferenceRequest.created_at)))
        for r in out:
            db.expunge(r)
        return out


def _count_rows():
    return len(_rows())


# ============================================================ policy, freeze, validation

def test_policy_versions_fixed_strategy_and_the_freeze_switch(routed):
    g, root = routed, routed["root"]
    k = mk_key(Api(g["app"], "deva@x.io"))
    c = Api(g["app"]).c
    mid = g["coder"]["id"]
    # make dgx1 look busy by default routing: with least_loaded the idle one wins; with fixed it is by weight
    root.patch(f"/admin/deployments/{g['dep']['coder@dgx2']}", {"weight": 300})
    first = root.put(f"/admin/routing/policies/{mid}", {"strategy": "fixed"}).json()
    assert first["version"] == 1 and first["status"] == "active"
    assert chat(c, k["key"]).headers["x-nexora-deployment"] == "coder@dgx2"  # fixed: highest weight
    second = root.put(f"/admin/routing/policies/{mid}", {"strategy": "least_loaded"}).json()
    assert second["version"] == 2
    versions = {p["version"]: p["status"] for p in root.get(f"/admin/routing/policies?model_id={mid}").json()}
    assert versions == {1: "retired", 2: "active"}
    assert root.put(f"/admin/routing/policies/{mid}", {"strategy": "nope"}).status_code == 422

    # freeze: forced to fixed even though the policy says least_loaded; unfreeze restores it
    g["ollama"].nodes["dgx2.test"]["resident"] = []  # dgx2 now cold -> least_loaded would prefer dgx1
    resample(g)
    assert chat(c, k["key"]).headers["x-nexora-deployment"] == "coder@dgx1"
    assert root.post("/admin/routing/freeze").json() == {"frozen": True}
    r = chat(c, k["key"])
    assert r.headers["x-nexora-deployment"] == "coder@dgx2"  # fixed by weight, residency ignored
    assert decisions(r.headers["x-nexora-request-id"])[0][3].startswith("routing frozen")
    assert root.get("/admin/routing/status").json()["frozen"] is True
    root.post("/admin/routing/unfreeze")
    assert chat(c, k["key"]).headers["x-nexora-deployment"] == "coder@dgx1"
    actions = {a["action"] for a in root.get("/admin/audit-logs?limit=100").json()}
    assert {"routing.policy", "routing.freeze", "routing.unfreeze", "deployment.create"} <= actions


def test_routing_admin_is_super_admin_only_and_validates_input(routed):
    g, root = routed, routed["root"]
    dev, ta = Api(g["app"], "deva@x.io"), Api(g["app"], "ta@x.io")
    paths = [("get", "/admin/nodes"), ("get", "/admin/deployments"), ("get", "/admin/routing/status"),
             ("get", "/admin/routing/policies"), ("get", "/admin/routing/decisions"),
             ("post", "/admin/routing/freeze"), ("post", "/admin/routing/unfreeze")]
    for method, path in paths:
        for who in (dev, ta):
            assert getattr(who, method)(path).status_code == 403, path
    assert dev.post("/admin/nodes", {"name": "x"}).status_code == 403
    for url in ("ftp://h:1", "http://u:p@h:1", "http://h:1/?q=1", "http://h:1/#f", "nonsense"):
        assert root.post("/admin/nodes", {"name": "bad", "runtime_url": url}).status_code == 422, url
    assert root.post("/admin/nodes", {"name": "dgx1"}).status_code == 409
    assert root.post("/admin/nodes", {"name": "has space"}).status_code == 422
    dup = {"model_id": g["coder"]["id"], "name": "coder@dgx1"}
    assert root.post("/admin/deployments", dup).status_code == 409
    assert root.post("/admin/deployments", {**dup, "name": "bad name!"}).status_code == 422
    assert root.post("/admin/deployments", {**dup, "name": "ok", "model_id": str(uuid.uuid4())}).status_code == 404
    assert root.post("/admin/deployments", {**dup, "name": "ok", "node_id": str(uuid.uuid4())}).status_code == 404
    assert root.patch(f"/admin/deployments/{uuid.uuid4()}", {"weight": 1}).status_code == 404
    assert root.patch(f"/admin/deployments/{g['dep']['coder@dgx1']}", {"weight": -1}).status_code == 422


def test_new_deployment_names_are_pushed_to_existing_keys(routed):
    g, root = routed, routed["root"]
    k = mk_key(Api(g["app"], "deva@x.io"))
    sk = k["key"]
    assert "coder-dgx3" not in g["llm"].keys[sk]
    r = root.post("/admin/deployments", {"model_id": g["coder"]["id"], "name": "coder@dgx3",
                                         "litellm_model": "coder-dgx3"})
    assert r.status_code == 201 and r.json()["keys_synced"] >= 1 and r.json()["keys_failed"] == 0
    assert {"coder-dgx1", "coder-dgx2", "coder-dgx3"} <= set(g["llm"].keys[sk])


# ============================================================ node sampler, health, build equivalence

def test_node_health_comes_from_the_sampler_and_steers_routing(routed):
    g, root = routed, routed["root"]
    k = mk_key(Api(g["app"], "deva@x.io"))
    c = Api(g["app"]).c
    dep = {d["name"]: d for d in root.get("/admin/deployments").json()}
    assert dep["coder@dgx1"]["health"] == "healthy" and dep["coder@dgx1"]["resident"] is True

    g["ollama"].nodes["dgx1.test"]["down"] = True
    resample(g)
    nodes = {n["name"]: n for n in root.get("/admin/nodes").json()}
    assert nodes["dgx1"]["reachable"] is False and nodes["dgx2"]["version"] == "0.40.5"
    assert {d["name"]: d["health"] for d in root.get("/admin/deployments").json()}["coder@dgx1"] == "unhealthy"
    assert chat(c, k["key"]).headers["x-nexora-deployment"] == "coder@dgx2"

    g["ollama"].nodes["dgx1.test"]["down"] = False
    g["ollama"].nodes["dgx1.test"]["tags"] = {}  # reachable, but the model was removed from it
    resample(g)
    health = {d["name"]: d for d in root.get("/admin/deployments").json()}
    assert health["coder@dgx1"]["health"] == "unhealthy"
    g["ollama"].nodes["dgx1.test"]["tags"] = {"qwen:8b": "d1d1d1d1d1d1"}
    resample(g)
    assert {d["name"]: d["health"] for d in root.get("/admin/deployments").json()}["coder@dgx1"] == "healthy"
    res = root.post("/admin/inference/check").json()  # the ops cycle uses the sampler too, no completion probes
    assert {h["source"] for h in res["health"] if h["deployment"].startswith("coder@")} == {"node sampler"}
    assert not [x for x in g["llm"].calls if x["path"] == "/health" and "coder-dgx" in str(x["body"])]


def test_different_model_builds_raise_alerts_and_a_reference_excludes_the_stale_one(routed):
    g, root = routed, routed["root"]
    g["ollama"].nodes["dgx1.test"]["tags"] = {"qwen:8b": "aaaaaaaaaaaa"}
    g["ollama"].nodes["dgx2.test"]["tags"] = {"qwen:8b": "bbbbbbbbbbbb"}
    resample(g)
    root.post("/admin/alerts/evaluate")
    open_ = {a["type"]: a for a in root.get("/admin/alerts?status=open").json()}
    assert open_["deployment_digest_mismatch"]["severity"] == "warning" and "coder" in open_["deployment_digest_mismatch"]["message"]
    assert open_["node_version_skew"]["severity"] == "info" and "0.40.2" in open_["node_version_skew"]["message"]

    k = mk_key(Api(g["app"], "deva@x.io"))
    c = Api(g["app"]).c
    g["ollama"].nodes["dgx2.test"]["resident"] = []  # dgx2 cold: dgx1 is the clear first choice
    resample(g)
    # no reference configured: both stay eligible (excluding both would be worse than a mismatch)
    assert chat(c, k["key"]).headers["x-nexora-deployment"] == "coder@dgx1"
    pol = root.put(f"/admin/routing/policies/{g['coder']['id']}",
                   {"strategy": "least_loaded", "reference_deployment_id": g["dep"]["coder@dgx2"]})
    assert pol.json()["configuration"]["reference_digest"] == "bbbbbbbbbbbb"
    r = chat(c, k["key"])
    assert r.headers["x-nexora-deployment"] == "coder@dgx2"  # dgx1 now excluded as a different build
    why = {x["deployment"]: x["excluded_because"] for x in Api(g["app"], "deva@x.io").get(
        f"/routing/explain/{r.headers['x-nexora-request-id']}").json()["decisions"][0]["candidates"]}
    assert why["coder@dgx1"].startswith("serves a different model build")
    # matching builds again: alerts clear on the next evaluation
    g["ollama"].nodes["dgx1.test"]["tags"] = {"qwen:8b": "bbbbbbbbbbbb"}
    resample(g)
    root.post("/admin/alerts/evaluate")
    assert "deployment_digest_mismatch" not in {a["type"] for a in root.get("/admin/alerts?status=open").json()}
    assert root.put(f"/admin/routing/policies/{g['coder']['id']}", {
        "strategy": "fixed", "reference_deployment_id": g["dep"]["coder@dgx1"]}).status_code == 200
    g["ollama"].nodes["dgx1.test"]["tags"] = {}
    resample(g)
    assert root.put(f"/admin/routing/policies/{g['coder']['id']}", {
        "strategy": "fixed", "reference_deployment_id": g["dep"]["coder@dgx1"]}).status_code == 409


def test_context_window_limits_which_deployment_can_take_a_request(routed):
    g, root = routed, routed["root"]
    k = mk_key(Api(g["app"], "deva@x.io"))
    c = Api(g["app"]).c
    root.patch(f"/admin/deployments/{g['dep']['coder@dgx1']}", {"context_limit": 200})
    root.patch(f"/admin/deployments/{g['dep']['coder@dgx2']}", {"context_limit": 100000})
    big = "word " * 400  # ~500 tokens: too big for dgx1
    r = chat(c, k["key"], messages=[{"role": "user", "content": big}])
    assert r.status_code == 200 and r.headers["x-nexora-deployment"] == "coder@dgx2"
    root.patch(f"/admin/deployments/{g['dep']['coder@dgx2']}", {"context_limit": 300})
    r = chat(c, k["key"], messages=[{"role": "user", "content": big}])
    assert r.status_code == 400 and r.json()["error"]["code"] == "context_length_exceeded"
    rejected = [x for x in _rows() if x.status == "rejected"]
    assert rejected and rejected[-1].error_code == "context_length_exceeded"  # still logged
    assert decisions(str(rejected[-1].id))[0][2] == "None"  # nothing was chosen
    assert chat(c, k["key"]).status_code == 200  # a small request still fits


# ============================================================ node budget

@pytest.mark.anyio
async def test_node_concurrency_budget_holds_back_other_deployments_on_the_same_node(routed):
    g, root = routed, routed["root"]
    root.patch(f"/admin/deployments/{g['dep']['coder@dgx2']}", {"enabled": False})  # coder -> dgx1 only
    chat_model = next(m for m in root.get("/admin/models").json() if m["model_alias"] == "chat")
    for d in root.get(f"/admin/deployments?model_id={chat_model['id']}").json():
        root.patch(f"/admin/deployments/{d['id']}", {"enabled": False})
    root.post("/admin/deployments", {"model_id": chat_model["id"], "name": "chat@dgx1", "litellm_model": "chat-dgx1",
                                     "node_id": g["nodes"]["dgx1"]["id"], "runtime_model": "qwen:8b",
                                     "max_active_requests": 1})
    root.patch(f"/admin/nodes/{g['nodes']['dgx1']['id']}", {"max_concurrent": 1})  # one request at a time, node-wide
    k = mk_key(Api(g["app"], "deva@x.io"))["key"]
    g["llm"].gate = asyncio.Event()
    async with aclient(g["app"]) as c:
        try:
            a = asyncio.create_task(post(c, k, "coder-request", max_tokens=5))
            await until(lambda: len(g["llm"].order) == 1)
            b = asyncio.create_task(c.post("/v1/chat/completions", headers=bearer(k), json={
                "model": "chat", "messages": [{"role": "user", "content": "chat-request"}], "max_tokens": 5}))
            await until(lambda: _count_rows() == 2 and all(r.queue_sequence for r in _rows()))
            await asyncio.sleep(0.6)  # its own deployment has a free slot, but the node does not
            assert g["llm"].order == ["coder-request"]
            assert [r.status for r in _rows()] == ["running", "queued"]
        finally:
            g["llm"].gate.set()
        ra, rb = await asyncio.gather(a, b)
    assert ra.status_code == rb.status_code == 200
    assert g["llm"].order == ["coder-request", "chat-request"] and g["llm"].max_inflight == 1


# ============================================================ failover and re-routing

def test_failover_only_when_the_node_is_confirmed_down_and_never_replays_otherwise(routed):
    g = routed
    k = mk_key(Api(g["app"], "deva@x.io"))
    c = Api(g["app"]).c
    calls = lambda: [x["body"]["model"] for x in g["llm"].calls_to("/v1/chat/completions")]  # noqa: E731

    g["ollama"].nodes["dgx2.test"]["resident"] = []  # dgx2 cold, so dgx1 is deterministically tried first
    resample(g)
    # 1) dgx1 refuses connections, and a probe confirms the node is down -> served by dgx2, once
    g["llm"].fail_models["coder-dgx1"] = (500, CONN_ERR)
    g["ollama"].nodes["dgx1.test"]["down"] = True
    r = chat(c, k["key"])
    assert r.status_code == 200 and r.headers["x-nexora-deployment"] == "coder@dgx2"
    assert calls() == ["coder-dgx1", "coder-dgx2"]  # one failed attempt, one successful execution
    rid = r.headers["x-nexora-request-id"]
    d = decisions(rid)
    assert [x[0] for x in d] == [1, 2] and d[0][2] == g["dep"]["coder@dgx1"]
    assert d[1][2] == g["dep"]["coder@dgx2"] and d[1][4] == g["dep"]["coder@dgx1"]  # failover_from recorded
    row = next(x for x in _rows() if str(x.id) == rid)
    assert row.status == "completed" and str(row.deployment_id) == g["dep"]["coder@dgx2"]
    with Session(dbm.get_engine()) as db:
        assert len(list(db.scalars(select(UsageRecord).where(UsageRecord.request_id == row.id)))) == 1
    assert {d["name"]: d["health"] for d in g["root"].get("/admin/deployments").json()}["coder@dgx1"] == "unhealthy"

    # 2) same error text but the node answers its probe: the outcome is unknown -> no replay, error returned
    g["ollama"].nodes["dgx1.test"]["down"] = False
    resample(g)
    before = len(calls())
    r = chat(c, k["key"])
    assert r.status_code == 500 and r.headers["x-nexora-deployment"] == "coder@dgx1"
    assert calls()[before:] == ["coder-dgx1"]  # exactly one attempt, nothing re-sent elsewhere
    assert len(decisions(r.headers["x-nexora-request-id"])) == 1
    assert next(x for x in _rows() if str(x.id) == r.headers["x-nexora-request-id"]).status == "failed"

    # 3) a different 5xx is never treated as a connection failure
    g["llm"].fail_models["coder-dgx1"] = (500, "model runner crashed")
    before = len(calls())
    assert chat(c, k["key"]).status_code == 500 and calls()[before:] == ["coder-dgx1"]


@pytest.mark.anyio
async def test_queued_requests_move_off_a_draining_deployment_in_order(routed):
    g, root = routed, routed["root"]
    root.put(f"/admin/routing/policies/{g['coder']['id']}", {"strategy": "fixed"})  # everything lands on dgx1
    g["llm"].gate = asyncio.Event()
    k = mk_key(Api(g["app"], "deva@x.io"))["key"]
    async with aclient(g["app"]) as c:
        tasks = []
        try:
            for i in range(3):
                tasks.append(asyncio.create_task(post(c, k, f"r{i}", max_tokens=5)))
                await until(lambda i=i: _count_rows() == i + 1 and all(r.queue_sequence for r in _rows()))
            assert {str(r.deployment_id) for r in _rows()} == {g["dep"]["coder@dgx1"]}
            assert await g["sched"].depth(g["dep"]["coder@dgx1"]) == 2

            root.patch(f"/admin/deployments/{g['dep']['coder@dgx1']}", {"draining": True})
            out = await inference.rebalance_queued(g["sched"])
            assert out == {"moved": 2, "stuck": 0}
            assert await g["sched"].depth(g["dep"]["coder@dgx1"]) == 0
            moved = [r for r in _rows() if str(r.deployment_id) == g["dep"]["coder@dgx2"]]
            assert len(moved) == 2 and moved[0].queue_sequence < moved[1].queue_sequence  # order preserved
        finally:
            g["llm"].gate.set()
        res = await asyncio.gather(*tasks)
    assert [r.status_code for r in res] == [200, 200, 200]
    assert [r.headers["x-nexora-deployment"] for r in res] == ["coder@dgx1", "coder@dgx2", "coder@dgx2"]
    assert g["llm"].order == ["r0", "r1", "r2"]  # per-deployment FIFO survived the move
    rerouted = [d for r in _rows() for d in decisions(str(r.id)) if d[0] == 2]
    assert len(rerouted) == 2 and all(d[4] == g["dep"]["coder@dgx1"] for d in rerouted)
    assert await g["sched"].active_count(g["dep"]["coder@dgx1"]) == 0 == await g["sched"].active_count(g["dep"]["coder@dgx2"])
