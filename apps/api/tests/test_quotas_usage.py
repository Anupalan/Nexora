import redis as sync_redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.db as dbm
from app.config import get_settings
from app.models import InferenceRequest, UsageRecord
from tests.conftest import Api
from tests.test_gateway import bearer, gw, mk_key  # noqa: F401  (gw is a fixture)


def chat(c, key, content="hello", **kw):
    return c.post("/v1/chat/completions", headers=bearer(key),
                  json={"model": "coder", "messages": [{"role": "user", "content": content}], **kw})


def counter(pattern: str) -> int:
    r = sync_redis.Redis.from_url(get_settings().redis_url)
    vals = [int(r.get(k)) for k in r.scan_iter(pattern)]
    return sum(vals)


def dev_key(gw, **kw):  # noqa: F811
    return mk_key(Api(gw["app"], "deva@x.io"), **kw)


def test_requests_per_minute_quota(gw):  # noqa: F811
    root = gw["root"]
    r = root.post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                    "requests_per_minute": 2})
    assert r.status_code == 201, r.text
    k, c = dev_key(gw), Api(gw["app"]).c
    assert chat(c, k["key"]).status_code == 200
    assert chat(c, k["key"]).status_code == 200
    r = chat(c, k["key"])
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert len(gw["llm"].calls_to("/v1/chat/completions")) == 2  # rejected request never hit upstream


def test_token_quota_rejects_oversized_and_reconciles(gw):  # noqa: F811
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "tokens_per_minute": 60})
    k, c = dev_key(gw), Api(gw["app"]).c
    assert chat(c, k["key"], max_tokens=100).status_code == 429  # one request reserves >60 tokens
    assert chat(c, k["key"], max_tokens=10).status_code == 200
    # Reservation was reconciled to actual usage (3 prompt + 1 completion), not the estimate.
    assert counter(f"nexora:q:user:{gw['dev_a'].id}:tok:minute:*") == 4


def test_output_cap_from_quota_clamps_request(gw):  # noqa: F811
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "max_output_tokens": 7})
    k, c = dev_key(gw), Api(gw["app"]).c
    chat(c, k["key"], max_tokens=50)
    assert gw["llm"].calls_to("/v1/chat/completions")[-1]["body"]["max_tokens"] == 7
    chat(c, k["key"])  # none requested -> cap injected
    assert gw["llm"].calls_to("/v1/chat/completions")[-1]["body"]["max_tokens"] == 7


def test_most_restrictive_scope_wins(gw):  # noqa: F811
    root = gw["root"]
    pid = gw["proj_a"]["id"]
    root.post(f"/projects/{pid}/members", {"user_id": str(gw["dev_a"].id), "access_level": "developer"})
    root.post("/admin/quotas", {"scope_type": "global", "requests_per_minute": 100})
    root.post("/admin/quotas", {"scope_type": "team", "scope_id": gw["team_a"]["id"],
                                "requests_per_minute": 1})
    k, c = dev_key(gw, project_id=pid), Api(gw["app"]).c
    assert chat(c, k["key"]).status_code == 200
    r = chat(c, k["key"])
    assert r.status_code == 429 and "team" in r.json()["error"]["message"]
    # a developer on team B is unaffected by team A's limit
    kb = mk_key(Api(gw["app"], "devb@x.io"))
    assert chat(c, kb["key"]).status_code == 200


def test_per_key_rate_limit(gw):  # noqa: F811
    k, c = dev_key(gw, rpm_limit=1), Api(gw["app"]).c
    assert chat(c, k["key"]).status_code == 200
    assert chat(c, k["key"]).status_code == 429


def test_quota_admin_api(gw):  # noqa: F811
    dev = Api(gw["app"], "deva@x.io")
    assert dev.post("/admin/quotas", {"scope_type": "global", "requests_per_minute": 1}).status_code == 403
    root = gw["root"]
    q = root.post("/admin/quotas", {"scope_type": "global", "requests_per_minute": 5}).json()
    assert root.post("/admin/quotas", {"scope_type": "global"}).status_code == 409
    assert root.post("/admin/quotas", {"scope_type": "user", "scope_id": "nope"}).status_code == 422
    assert root.post("/admin/quotas", {"scope_type": "team", "scope_id":
                                       "00000000-0000-0000-0000-000000000001"}).status_code == 404
    assert root.post("/admin/quotas", {"scope_type": "global", "requests_per_minute": 0}).status_code in (409, 422)
    assert root.patch(f"/admin/quotas/{q['id']}", {"requests_per_minute": None,
                                                    "tokens_per_day": 10}).json()["requests_per_minute"] is None
    assert dev.get("/quotas/me").json()["global"]["tokens_per_day"] == 10
    assert dev.get(f"/quotas/team/{gw['team_a']['id']}").status_code == 200
    assert dev.get(f"/quotas/team/{gw['team_b']['id']}").status_code == 404


def test_idempotency_key(gw):  # noqa: F811
    k, c = dev_key(gw), Api(gw["app"]).c
    h = {**bearer(k["key"]), "idempotency-key": "abc-1"}
    body = {"model": "coder", "messages": [{"role": "user", "content": "x"}]}
    assert c.post("/v1/chat/completions", headers=h, json=body).status_code == 200
    r = c.post("/v1/chat/completions", headers=h, json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_request"
    with Session(dbm.get_engine()) as db:
        assert db.scalar(select(func.count()).select_from(InferenceRequest)) == 1


def test_usage_history_export_and_scoping(gw):  # noqa: F811
    root, app_ = gw["root"], gw["app"]
    pid = gw["proj_a"]["id"]
    root.post(f"/projects/{pid}/members", {"user_id": str(gw["dev_a"].id), "access_level": "developer"})
    ka, kb, c = dev_key(gw, project_id=pid), mk_key(Api(app_, "devb@x.io")), Api(app_).c
    for _ in range(2):
        assert chat(c, ka["key"]).status_code == 200
    assert chat(c, kb["key"]).status_code == 200

    dev = Api(app_, "deva@x.io")
    me = dev.get("/usage/me").json()["totals"]
    assert (me["requests"], me["input_tokens"], me["output_tokens"], me["total_tokens"]) == (2, 6, 2, 8)
    hist = dev.get("/usage/requests").json()
    assert len(hist) == 2 and {h["status"] for h in hist} == {"completed"} and hist[0]["model"] == "coder"
    csv_text = dev.get("/usage/export").text
    assert csv_text.splitlines()[0].startswith("id,created_at,model") and len(csv_text.splitlines()) == 3

    # team admin of A sees team A's project usage only; dev B can't see team A at all
    ta = Api(app_, "ta@x.io")
    assert ta.get(f"/usage/team/{gw['team_a']['id']}").json()["totals"]["requests"] == 2
    assert ta.get("/usage/report").json()["totals"]["requests"] == 2
    assert Api(app_, "devb@x.io").get(f"/usage/team/{gw['team_a']['id']}").status_code == 404
    assert dev.get(f"/usage/team/{gw['team_a']['id']}").status_code == 403
    assert root.get("/usage/report").json()["totals"]["requests"] == 3
    assert root.get(f"/usage/project/{pid}").json()["totals"]["requests"] == 2
    assert Api(app_, "devb@x.io").get(f"/usage/project/{pid}").status_code == 404
    assert dev.get(f"/usage/project/{pid}").status_code == 403  # plain developer, not a manager

    with Session(dbm.get_engine()) as db:  # ledger matches request records
        assert db.scalar(select(func.sum(UsageRecord.total_tokens))) == 12
    assert root.get("/usage/report?from=2021-01-01T00:00:00Z&to=2021-12-31T00:00:00Z"
                    ).json()["totals"]["requests"] == 0
    assert root.get("/usage/report?from=2020-01-01T00:00:00Z&to=2026-12-31T00:00:00Z").status_code == 400


def test_queue_status_visibility_and_estimate(gw):  # noqa: F811
    pid = gw["proj_a"]["id"]
    gw["root"].post(f"/projects/{pid}/members", {"user_id": str(gw["dev_a"].id), "access_level": "developer"})
    k, c = dev_key(gw, project_id=pid), Api(gw["app"]).c
    rid = chat(c, k["key"]).headers["x-nexora-request-id"]
    dev = Api(gw["app"], "deva@x.io")
    assert dev.get(f"/queue/status/{rid}").json()["status"] == "completed"
    assert Api(gw["app"], "ta@x.io").get(f"/queue/status/{rid}").status_code == 200  # team admin
    assert Api(gw["app"], "devb@x.io").get(f"/queue/status/{rid}").status_code == 404
    assert dev.get("/queue/me").json() == []
    est = dev.post("/queue/estimate", {"model": "coder"}).json()
    assert est["queue_depth"] == 0 and est["estimated_wait_ms"] is None  # no fake precision
    assert dev.get("/admin/queue").status_code == 403
    q = gw["root"].get("/admin/queue").json()
    assert q["deployments"][0]["model"] == "coder"
