import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db as dbm
from app.models import ApiKey, AuditLog, Model
from tests.conftest import MASTER, Api


@pytest.fixture
def gw(world, llm, sched):
    """World + two registered models (one will be used for permission tests)."""
    root = world["root"]
    for alias, lm in (("coder", "openai/qwen-coder"), ("chat", "openai/qwen-chat")):
        r = root.post("/admin/models", {"name": alias, "model_alias": alias, "litellm_model": lm,
                                        "max_output_tokens": 100})
        assert r.status_code == 201, r.text
    world["llm"] = llm
    world["sched"] = sched
    return world


def mk_key(api, **kw):
    r = api.post("/keys", {"name": "laptop", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def bearer(key):
    return {"authorization": f"Bearer {key}"}


def test_model_admin_only(gw):
    dev = Api(gw["app"], "deva@x.io")
    assert dev.post("/admin/models", {"name": "x", "model_alias": "x", "litellm_model": "x"}).status_code == 403
    assert gw["root"].post("/admin/models", {"name": "x", "model_alias": "coder",
                                              "litellm_model": "x"}).status_code == 409
    assert {m["model_alias"] for m in dev.get("/models").json()} == {"coder", "chat"}


def test_key_secret_shown_once_and_never_stored(gw):
    dev = Api(gw["app"], "deva@x.io")
    created = mk_key(dev)
    secret = created["key"]
    assert secret.startswith("sk-nx-") and created["key_prefix"] != secret
    assert "key" not in dev.get(f"/keys/{created['id']}").json()
    assert secret not in dev.get("/keys").text
    with Session(dbm.get_engine()) as db:
        row = db.scalar(select(ApiKey))
        assert secret not in str([getattr(row, c.name) for c in ApiKey.__table__.columns])
        assert secret not in str([a.meta for a in db.scalars(select(AuditLog))])
    # LiteLLM got the master key for management, and the right policy
    gen = next(c for c in gw["llm"].calls if c["path"] == "/key/generate")
    assert gen["auth"] == f"Bearer {MASTER}" and gen["body"]["models"] == ["chat", "coder"]
    assert MASTER not in dev.get("/keys").text


def test_gateway_auth_rejections(gw):
    c = Api(gw["app"]).c
    assert c.get("/v1/models").status_code == 401
    r = c.get("/v1/models", headers=bearer("sk-nx-bogus"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
    assert c.post("/v1/chat/completions", json={"model": "coder"}, headers=bearer("nope")).status_code == 401


def test_models_filtered_by_key(gw):
    dev = Api(gw["app"], "deva@x.io")
    k = mk_key(dev, models=["coder"])
    r = Api(gw["app"]).c.get("/v1/models", headers=bearer(k["key"]))
    assert [m["id"] for m in r.json()["data"]] == ["coder"]


def test_chat_forwarding_uses_callers_key_and_maps_model(gw):
    dev = Api(gw["app"], "deva@x.io")
    k = mk_key(dev)
    c = Api(gw["app"]).c
    r = c.post("/v1/chat/completions", headers=bearer(k["key"]),
               json={"model": "coder", "messages": [], "max_tokens": 5000})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    call = gw["llm"].calls[-1]
    assert call["auth"] == f"Bearer {k['key']}"          # caller's virtual key, never the master key
    assert call["body"]["model"] == "openai/qwen-coder"  # alias mapped
    assert call["body"]["max_tokens"] == 100             # clamped to model limit


def test_streaming_passthrough(gw):
    k = mk_key(Api(gw["app"], "deva@x.io"))
    c = Api(gw["app"]).c
    with c.stream("POST", "/v1/chat/completions", headers=bearer(k["key"]),
                  json={"model": "coder", "stream": True, "messages": []}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())
    assert body == b"".join(gw["llm"].stream_chunks)


def test_model_not_permitted_or_disabled(gw):
    k = mk_key(Api(gw["app"], "deva@x.io"), models=["coder"])
    c = Api(gw["app"]).c
    r = c.post("/v1/chat/completions", headers=bearer(k["key"]), json={"model": "chat", "messages": []})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    assert c.post("/v1/chat/completions", headers=bearer(k["key"]), json={"messages": []}).status_code == 400
    with Session(dbm.get_engine()) as db:
        mid = db.scalar(select(Model.id).where(Model.model_alias == "coder"))
    gw["root"].post(f"/admin/models/{mid}/disable")
    r = c.post("/v1/chat/completions", headers=bearer(k["key"]), json={"model": "coder", "messages": []})
    assert r.status_code == 404
    # creating a key with a disabled/unknown model is refused
    assert Api(gw["app"], "deva@x.io").post("/keys", {"name": "n", "models": ["coder"]}).status_code == 400


def test_revoked_expired_and_suspended_keys_rejected(gw):
    app_ = gw["app"]
    dev = Api(app_, "deva@x.io")
    c = Api(app_).c
    ok = lambda key: c.get("/v1/models", headers=bearer(key)).status_code  # noqa: E731

    k1 = mk_key(dev)
    assert ok(k1["key"]) == 200
    assert dev.post(f"/keys/{k1['id']}/revoke").status_code == 200
    assert ok(k1["key"]) == 401
    assert "/key/delete" in gw["llm"].paths()

    k2 = mk_key(dev)
    with Session(dbm.get_engine()) as db:
        from datetime import timedelta

        from app.models import utcnow
        row = db.get(ApiKey, __import__("uuid").UUID(k2["id"]))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert ok(k2["key"]) == 401

    k3 = mk_key(dev)
    assert gw["root"].post(f"/users/{gw['dev_a'].id}/suspend").status_code == 200
    assert ok(k3["key"]) == 401
    gw["root"].post(f"/users/{gw['dev_a'].id}/activate")
    assert ok(k3["key"]) == 401  # suspension revoked it for good; a new key is needed


def test_rotate_invalidates_old_secret(gw):
    dev = Api(gw["app"], "deva@x.io")
    k = mk_key(dev)
    c = Api(gw["app"]).c
    r = dev.post(f"/keys/{k['id']}/rotate")
    assert r.status_code == 200
    new = r.json()["key"]
    assert new != k["key"]
    assert c.get("/v1/models", headers=bearer(k["key"])).status_code == 401
    assert c.get("/v1/models", headers=bearer(new)).status_code == 200
    assert gw["llm"].paths().count("/key/generate") == 2 and "/key/delete" in gw["llm"].paths()


def test_key_visibility_and_ownership(gw):
    app_ = gw["app"]
    proj = gw["proj_a"]["id"]
    gw["root"].post(f"/projects/{proj}/members", {"user_id": str(gw["dev_a"].id), "access_level": "developer"})
    k = mk_key(Api(app_, "deva@x.io"), project_id=proj)
    assert k["team_id"] == gw["team_a"]["id"]
    other = Api(app_, "devb@x.io")
    assert other.get(f"/keys/{k['id']}").status_code == 404
    assert other.post(f"/keys/{k['id']}/revoke").status_code == 404
    assert other.get("/keys").json() == []
    ta = Api(app_, "ta@x.io")  # team admin sees and may revoke, but not rotate/edit
    assert [x["id"] for x in ta.get("/keys").json()] == [k["id"]]
    assert ta.post(f"/keys/{k['id']}/rotate").status_code == 403
    assert ta.patch(f"/keys/{k['id']}", {"name": "x"}).status_code == 403
    assert ta.post(f"/keys/{k['id']}/revoke").status_code == 200


def test_project_key_requires_project_access(gw):
    dev_b = Api(gw["app"], "devb@x.io")
    assert dev_b.post("/keys", {"name": "n", "project_id": gw["proj_a"]["id"]}).status_code == 404
    pid = gw["proj_a"]["id"]
    gw["root"].post(f"/projects/{pid}/members", {"user_id": str(gw["dev_a"].id), "access_level": "read_only"})
    assert Api(gw["app"], "deva@x.io").post("/keys", {"name": "n", "project_id": pid}).status_code == 403


def test_update_policy_syncs_to_litellm(gw):
    dev = Api(gw["app"], "deva@x.io")
    k = mk_key(dev)
    r = dev.patch(f"/keys/{k['id']}", {"models": ["chat"], "rpm_limit": 10})
    assert r.status_code == 200 and r.json()["allowed_models"] == ["chat"]
    upd = next(c for c in gw["llm"].calls if c["path"] == "/key/update")
    assert upd["body"]["models"] == ["chat"] and upd["body"]["rpm_limit"] == 10


def test_gateway_down_creates_nothing(gw):
    gw["llm"].down = True
    r = Api(gw["app"], "deva@x.io").post("/keys", {"name": "n"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "GATEWAY_UNAVAILABLE"
    with Session(dbm.get_engine()) as db:
        assert db.scalar(select(ApiKey)) is None


def test_upstream_down_returns_openai_error(gw):
    k = mk_key(Api(gw["app"], "deva@x.io"))
    gw["llm"].down = True
    r = Api(gw["app"]).c.post("/v1/chat/completions", headers=bearer(k["key"]),
                              json={"model": "coder", "messages": []})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unavailable"
