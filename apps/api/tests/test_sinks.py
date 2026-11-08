"""Richer request context and log sinks."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import db as dbm
from app import sinks
from app.config import get_settings
from app.inference import dbcall  # noqa: F401
from app.models import InferenceRequest, utcnow
from tests.conftest import Api
from tests.test_gateway import bearer, gw, mk_key  # noqa: F401  (gw is a fixture)

BODY = {"model": "coder", "messages": [{"role": "user", "content": "hi"}]}


def ask(g, key, **extra):
    h = {**bearer(key), **extra.pop("headers", {})}
    return Api(g["app"]).c.post("/v1/chat/completions", headers=h, json={**BODY, **extra})


def rows():
    with Session(dbm.get_engine()) as db:
        return list(db.scalars(select(InferenceRequest).order_by(InferenceRequest.created_at)))


def age_all(seconds=60):
    """Make finished requests old enough to be exported (the exporter waits a few seconds)."""
    with Session(dbm.get_engine()) as db:
        db.execute(update(InferenceRequest).values(completed_at=utcnow() - timedelta(seconds=seconds)))
        db.commit()


# ============================================================ request context

def test_request_context_is_recorded_without_content(gw):
    k = mk_key(Api(gw["app"], "deva@x.io"))
    gw["llm"].choices = [{"message": {"content": "x", "tool_calls": [{"id": "1"}, {"id": "2"}]},
                          "finish_reason": "tool_calls"}]
    r = ask(gw, k["key"], tools=[{"type": "function", "function": {"name": "f"}}],
            headers={"user-agent": "opencode/1.2"})
    assert r.status_code == 200
    row = rows()[-1]
    assert row.user_agent == "opencode/1.2" and row.client_ip and row.request_bytes > 20
    assert row.has_tools is True and row.finish_reason == "tool_calls" and row.tool_calls == 2


def test_streaming_finish_reason_and_tool_calls(gw):
    k = mk_key(Api(gw["app"], "deva@x.io"))
    gw["llm"].stream_chunks = [
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a","function":{"name":"f"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"b"}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b"data: [DONE]\n\n"]
    r = ask(gw, k["key"], stream=True)
    assert r.status_code == 200
    row = rows()[-1]
    assert row.finish_reason == "tool_calls" and row.tool_calls == 2 and row.has_tools is False


def test_client_ip_logging_can_be_turned_off_and_forwarded_for_is_opt_in(gw, monkeypatch):
    k = mk_key(Api(gw["app"], "deva@x.io"))
    ask(gw, k["key"], headers={"x-forwarded-for": "203.0.113.9"})
    assert rows()[-1].client_ip != "203.0.113.9"  # untrusted header ignored by default
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    ask(gw, k["key"], headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert rows()[-1].client_ip == "203.0.113.9"
    monkeypatch.setattr(get_settings(), "log_client_ip", False)
    ask(gw, k["key"])
    assert rows()[-1].client_ip is None and rows()[-1].user_agent is not None


# ============================================================ sinks

@pytest.fixture(autouse=True)
def no_lag(monkeypatch):
    monkeypatch.setattr(sinks, "LAG_S", 0)  # production waits 15 s before exporting a finished request


@pytest.fixture
def sink_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "log_sink_dir", str(tmp_path))
    return tmp_path


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_file_sink_validation(gw, sink_dir, monkeypatch):
    root = gw["root"]
    ok = {"name": "siem", "type": "file", "path": "activity.jsonl", "streams": ["requests", "audit"]}
    assert root.post("/admin/log-sinks", ok).status_code == 201
    assert root.post("/admin/log-sinks", ok).status_code == 422  # duplicate name
    for bad in ({"path": "../evil.jsonl"}, {"path": "/etc/passwd"}, {"path": "a/b.jsonl"}, {"path": "x.txt"}):
        assert root.post("/admin/log-sinks", {**ok, "name": "n" + bad["path"][:3], **bad}).status_code == 422
    assert root.post("/admin/log-sinks", {"name": "w", "type": "webhook", "url": "ftp://x"}).status_code == 422
    assert root.post("/admin/log-sinks", {"name": "w", "type": "webhook", "url": "https://h.test/x",
                                          "secret_env": "NOPE_NOT_SET"}).status_code == 422
    monkeypatch.setattr(get_settings(), "log_sink_dir", None)
    assert root.post("/admin/log-sinks", {**ok, "name": "again"}).status_code == 422  # no directory configured


def test_file_sink_exports_requests_once_without_content(gw, sink_dir):
    root = gw["root"]
    k = mk_key(Api(gw["app"], "deva@x.io"))
    ask(gw, k["key"])  # before the sink exists: not exported (no backfill)
    age_all()
    sid = root.post("/admin/log-sinks", {"name": "f", "type": "file", "path": "a.jsonl"}).json()["id"]
    ask(gw, k["key"], headers={"user-agent": "cline"})
    ask(gw, k["key"])
    out = root.post(f"/admin/log-sinks/{sid}/export").json()
    assert out["error"] is None and out["sent"]["requests"] == 2
    recs = lines(sink_dir / "a.jsonl")
    assert len(recs) == 2 and {r["stream"] for r in recs} == {"request"}
    r0 = recs[0]
    assert r0["model"] == "coder" and r0["status"] == "completed" and r0["output_tokens"] == 1
    assert r0["finish_reason"] == "stop" and "user_agent" in r0 and "messages" not in json.dumps(r0)
    assert root.post(f"/admin/log-sinks/{sid}/export").json()["sent"]["requests"] == 0  # cursor advanced
    assert len(lines(sink_dir / "a.jsonl")) == 2
    ask(gw, k["key"])
    root.post(f"/admin/log-sinks/{sid}/export")
    ids = [r["id"] for r in lines(sink_dir / "a.jsonl")]
    assert len(ids) == 3 and len(set(ids)) == 3  # nothing sent twice
    st = next(s for s in root.get("/admin/log-sinks").json()["sinks"] if s["id"] == sid)["status"]
    assert st["sent"]["requests"] == 3 and st["failures"] == 0


def test_recent_requests_wait_for_the_lag(gw, sink_dir, monkeypatch):
    monkeypatch.setattr(sinks, "LAG_S", 3600)
    root = gw["root"]
    k = mk_key(Api(gw["app"], "deva@x.io"))
    sid = root.post("/admin/log-sinks", {"name": "f", "type": "file", "path": "a.jsonl"}).json()["id"]
    ask(gw, k["key"])  # just finished: inside the lag window
    assert root.post(f"/admin/log-sinks/{sid}/export").json()["sent"]["requests"] == 0


def test_audit_stream_is_exported_in_chain_order(gw, sink_dir):
    root = gw["root"]
    sid = root.post("/admin/log-sinks", {"name": "au", "type": "file", "path": "audit.jsonl",
                                         "streams": ["audit"], "backfill": True}).json()["id"]
    root.post(f"/admin/log-sinks/{sid}/export")
    recs = lines(sink_dir / "audit.jsonl")
    assert recs and all(r["stream"] == "audit" for r in recs)
    assert [r["seq"] for r in recs] == sorted(r["seq"] for r in recs) and recs[0]["seq"] == 1
    assert all(r["hash"] and r["prev_hash"] for r in recs)


def test_webhook_signs_the_body_retries_after_failure_and_alerts(gw, monkeypatch):
    root = gw["root"]
    monkeypatch.setenv("SINK_SECRET", "s3cret")
    calls, state = [], {"fail": True}

    def handler(req: httpx.Request):
        calls.append((req.headers.get("x-nexora-signature"), req.content))
        return httpx.Response(500 if state["fail"] else 204)

    monkeypatch.setattr(sinks, "transport_override", httpx.MockTransport(handler))
    k = mk_key(Api(gw["app"], "deva@x.io"))
    sid = root.post("/admin/log-sinks", {"name": "hook", "type": "webhook", "url": "https://hook.test/in",
                                         "secret_env": "SINK_SECRET"}).json()["id"]
    ask(gw, k["key"])
    for _ in range(3):
        assert root.post(f"/admin/log-sinks/{sid}/export").json()["error"]
    first = len(calls)
    assert first >= 3
    import hashlib
    import hmac
    sig, body = calls[0]
    assert sig == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    payload = json.loads(body)
    assert payload["sink"] == "hook" and len(payload["records"]) == 1
    root.post("/admin/alerts/evaluate")
    assert any(a["type"] == "log_sink_failing" for a in root.get("/admin/alerts?status=open").json())
    state["fail"] = False  # destination recovers: the same record is delivered, nothing was lost
    out = root.post(f"/admin/log-sinks/{sid}/export").json()
    assert out["error"] is None and out["sent"]["requests"] == 1
    assert json.loads(calls[-1][1])["records"][0]["id"] == payload["records"][0]["id"]
    root.post("/admin/alerts/evaluate")
    assert not any(a["type"] == "log_sink_failing" for a in root.get("/admin/alerts?status=open").json())


def test_test_endpoint_update_delete_and_audit(gw, sink_dir):
    root = gw["root"]
    sid = root.post("/admin/log-sinks", {"name": "t", "type": "file", "path": "t.jsonl"}).json()["id"]
    assert root.post(f"/admin/log-sinks/{sid}/test").json() == {"ok": True, "error": None}
    assert lines(sink_dir / "t.jsonl")[0]["stream"] == "test"
    assert root.patch(f"/admin/log-sinks/{sid}", {"enabled": False}).json()["enabled"] is False
    assert root.patch(f"/admin/log-sinks/{sid}", {"streams": ["bogus"]}).status_code == 422
    assert root.delete(f"/admin/log-sinks/{sid}").status_code == 204
    assert root.post(f"/admin/log-sinks/{sid}/test").status_code == 404
    acts = {a["action"] for a in root.get("/admin/audit-logs?limit=100").json()}
    assert {"logsink.create", "logsink.update", "logsink.delete"} <= acts


def test_sinks_are_super_admin_only(gw):
    dev = Api(gw["app"], "deva@x.io")
    assert dev.get("/admin/log-sinks").status_code == 403
    assert dev.post("/admin/log-sinks", {"name": "x", "type": "file", "path": "x.jsonl"}).status_code == 403
