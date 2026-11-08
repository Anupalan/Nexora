"""Log sinks: export activity records to your own systems (roadmap §10.8).

Two streams can be sent: `requests` (one record per finished model request: who, which model and
deployment, tokens, timings, outcome; never prompt or response content) and `audit` (the hash-chained
audit log). A sink is a JSON-lines file (written under LOG_SINK_DIR only) or a webhook (JSON POST,
optionally signed with HMAC-SHA256 using a secret read from an environment variable).

Delivery is at-least-once: a cursor per sink advances only after the destination accepted a batch, so a
failure is simply retried next cycle and a crash can repeat a batch. Every record carries a stable `id`
(or `seq` for audit) for de-duplication. Records are exported once they are `LAG_S` old so a request that
committed slightly late is not skipped by the cursor.
"""

import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path

import anyio
import httpx
from sqlalchemy import and_, func, or_, select

from app.config import get_settings
from app.inference import dbcall
from app.models import (
    AuditLog,
    InferenceNode,
    InferenceRequest,
    Model,
    ModelDeployment,
    PlatformSetting,
    as_utc,
    utcnow,
)

KEY = "log_sinks"
LAG_S = 15
BATCH = 500
MAX_BATCHES = 10
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.jsonl$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
STREAMS = ("requests", "audit")
TERMINAL = ("completed", "failed", "cancelled", "rejected")
transport_override: httpx.BaseTransport | None = None  # tests inject a mock transport here


# ------------------------------------------------------------------ configuration

def load_sinks(db) -> list[dict]:
    row = db.get(PlatformSetting, KEY)
    return list((row.value or {}).get("sinks", [])) if row else []


def _save_sinks(db, sinks: list[dict], actor_id=None) -> None:
    row = db.get(PlatformSetting, KEY) or PlatformSetting(key=KEY, value={})
    row.value, row.updated_by = {"sinks": sinks}, actor_id
    db.add(row)


def status_of(db, sink_id: str) -> dict:
    row = db.get(PlatformSetting, f"sink:{sink_id}")
    return dict(row.value or {}) if row else {}


def _save_status(db, sink_id: str, value: dict) -> None:
    row = db.get(PlatformSetting, f"sink:{sink_id}") or PlatformSetting(key=f"sink:{sink_id}", value={})
    row.value = value
    db.add(row)


def validate(body: dict, existing: dict | None = None) -> dict:
    """Return a clean sink definition or raise ValueError."""
    d = {**(existing or {}), **{k: v for k, v in body.items() if v is not None}}
    name = str(d.get("name", "")).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("A sink needs a name (1-80 characters).")
    kind = d.get("type")
    if kind not in ("file", "webhook"):
        raise ValueError("type must be 'file' or 'webhook'.")
    streams = d.get("streams") or ["requests"]
    if not isinstance(streams, list) or not streams or any(s not in STREAMS for s in streams):
        raise ValueError("streams must be a non-empty list drawn from: requests, audit.")
    out = {"id": d.get("id") or uuid.uuid4().hex[:12], "name": name, "type": kind, "enabled": bool(d.get("enabled", True)),
           "streams": sorted(set(streams))}
    if kind == "file":
        fn = str(d.get("path", ""))
        if not FILE_NAME.match(fn):
            raise ValueError("path must be a plain file name ending in .jsonl (letters, digits, '.', '_', '-').")
        if not get_settings().log_sink_dir:
            raise ValueError("File sinks are disabled until LOG_SINK_DIR is set on the server.")
        out["path"] = fn
    else:
        url = str(d.get("url", ""))
        if not re.match(r"^https?://[^\s/]+(/\S*)?$", url) or len(url) > 500:
            raise ValueError("url must be an http(s) address.")
        out["url"] = url
        env = d.get("secret_env")
        if env:
            if not ENV_NAME.match(str(env)):
                raise ValueError("secret_env must be an environment variable name like SINK_SECRET.")
            if env not in os.environ:
                raise ValueError(f"The environment variable {env} is not set on the server.")
            out["secret_env"] = env
    return out


def view(sink: dict, status: dict) -> dict:
    return {**sink, "status": {k: status.get(k) for k in ("last_ok", "last_error", "failures", "sent")}}


def create(db, body: dict, actor_id=None) -> dict:
    sink = validate(body)
    sinks = load_sinks(db)
    if len(sinks) >= 10:
        raise ValueError("At most 10 sinks.")
    if any(s["name"] == sink["name"] for s in sinks):
        raise ValueError("A sink with this name already exists.")
    # New sinks start from "now" unless a backfill is requested, so enabling one never floods the destination.
    backfill = bool(body.get("backfill"))
    head = db.scalar(select(func.max(AuditLog.seq)))
    _save_status(db, sink["id"], {
        "requests": {"at": (utcnow() - timedelta(seconds=LAG_S)).isoformat() if not backfill else None, "id": None},
        "audit_seq": 0 if backfill else int(head or 0), "failures": 0, "sent": {"requests": 0, "audit": 0}})
    _save_sinks(db, [*sinks, sink], actor_id)
    db.commit()
    return sink


def update(db, sink_id: str, body: dict, actor_id=None) -> dict | None:
    sinks = load_sinks(db)
    cur = next((s for s in sinks if s["id"] == sink_id), None)
    if cur is None:
        return None
    new = validate({k: v for k, v in body.items() if k != "id"}, cur)
    new["id"] = sink_id
    if any(s["name"] == new["name"] and s["id"] != sink_id for s in sinks):
        raise ValueError("A sink with this name already exists.")
    _save_sinks(db, [new if s["id"] == sink_id else s for s in sinks], actor_id)
    db.commit()
    return new


def delete(db, sink_id: str, actor_id=None) -> bool:
    sinks = load_sinks(db)
    if not any(s["id"] == sink_id for s in sinks):
        return False
    _save_sinks(db, [s for s in sinks if s["id"] != sink_id], actor_id)
    st = db.get(PlatformSetting, f"sink:{sink_id}")
    if st:
        db.delete(st)
    db.commit()
    return True


# ------------------------------------------------------------------ records

def request_record(r: InferenceRequest, model: str, deployment: str, node: str | None) -> dict:
    ms = lambda v: v  # noqa: E731  (already integers)
    return {
        "stream": "request", "id": str(r.id), "created_at": as_utc(r.created_at).isoformat(),
        "completed_at": as_utc(r.completed_at).isoformat() if r.completed_at else None,
        "status": r.status, "user_id": str(r.user_id), "team_id": str(r.team_id) if r.team_id else None,
        "project_id": str(r.project_id) if r.project_id else None,
        "api_key_id": str(r.api_key_id) if r.api_key_id else None, "model": model, "deployment": deployment,
        "node": node, "class": r.workload_class, "source": r.source, "streamed": r.stream,
        "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "queue_ms": ms(r.queue_time_ms),
        "ttft_ms": r.time_to_first_token_ms, "duration_ms": r.duration_ms, "finish_reason": r.finish_reason,
        "tool_calls": r.tool_calls, "has_tools": r.has_tools, "request_bytes": r.request_bytes,
        "client_ip": r.client_ip, "user_agent": r.user_agent, "error_code": r.error_code,
    }


def audit_record(a: AuditLog) -> dict:
    return {"stream": "audit", "seq": a.seq, "id": str(a.id), "created_at": as_utc(a.created_at).isoformat(),
            "actor_id": str(a.actor_id) if a.actor_id else None, "action": a.action,
            "entity_type": a.entity_type, "entity_id": a.entity_id, "metadata": a.meta,
            "prev_hash": a.prev_hash, "hash": a.hash}


def _requests_batch(db, cur: dict) -> tuple[list[dict], dict]:
    cutoff = utcnow() - timedelta(seconds=LAG_S)
    q = (select(InferenceRequest, Model.model_alias, ModelDeployment.deployment_name, InferenceNode.name)
         .join(Model, Model.id == InferenceRequest.model_id)
         .join(ModelDeployment, ModelDeployment.id == InferenceRequest.deployment_id)
         .outerjoin(InferenceNode, InferenceNode.id == ModelDeployment.node_id)
         .where(InferenceRequest.status.in_(TERMINAL), InferenceRequest.completed_at.is_not(None),
                InferenceRequest.completed_at <= cutoff)
         .order_by(InferenceRequest.completed_at, InferenceRequest.id).limit(BATCH))
    if cur.get("at"):
        from datetime import datetime
        at = as_utc(datetime.fromisoformat(cur["at"]))
        after = [InferenceRequest.completed_at > at]
        if cur.get("id"):
            after = [or_(InferenceRequest.completed_at > at,
                         and_(InferenceRequest.completed_at == at, InferenceRequest.id > uuid.UUID(cur["id"])))]
        q = q.where(*after)
    rows = list(db.execute(q))
    recs = [request_record(r, m, d, n) for r, m, d, n in rows]
    new = {"at": as_utc(rows[-1][0].completed_at).isoformat(), "id": str(rows[-1][0].id)} if rows else cur
    return recs, new


def _audit_batch(db, last_seq: int) -> tuple[list[dict], int]:
    rows = list(db.scalars(select(AuditLog).where(AuditLog.seq > last_seq).order_by(AuditLog.seq).limit(BATCH)))
    return [audit_record(a) for a in rows], (rows[-1].seq if rows else last_seq)


# ------------------------------------------------------------------ delivery

def deliver(sink: dict, records: list[dict]) -> None:
    """Blocking; raises on failure. Called from a worker thread."""
    if not records:
        return
    if sink["type"] == "file":
        base = Path(get_settings().log_sink_dir or "").resolve()
        target = (base / sink["path"]).resolve()
        if base not in target.parents:
            raise OSError("path escapes the sink directory")
        with open(target, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n")
        return
    body = json.dumps({"sink": sink["name"], "records": records}, separators=(",", ":"), sort_keys=True).encode()
    headers = {"content-type": "application/json", "user-agent": "nexora-log-sink"}
    if sink.get("secret_env"):
        secret = os.environ.get(sink["secret_env"])
        if not secret:
            raise RuntimeError(f"environment variable {sink['secret_env']} is not set")
        headers["x-nexora-signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with httpx.Client(timeout=10.0, transport=transport_override, follow_redirects=False) as c:
        c.post(sink["url"], content=body, headers=headers).raise_for_status()


async def export_sink(sched, sink: dict) -> dict:
    """Send everything new to one sink, up to MAX_BATCHES batches. Never raises."""
    lock = f"nexora:sinklock:{sink['id']}"
    if not await sched.redis.set(lock, 1, nx=True, ex=120):
        return {"sink": sink["name"], "skipped": "another worker is exporting"}
    sent = {"requests": 0, "audit": 0}
    err = None
    try:
        for _ in range(MAX_BATCHES):
            progressed = False
            st = await dbcall(status_of, sink["id"])
            cursor = st.get("requests") or {"at": None, "id": None}
            if "requests" in sink["streams"]:
                recs, new = await dbcall(_requests_batch, cursor)
                if recs:
                    await anyio.to_thread.run_sync(deliver, sink, recs)
                    await dbcall(_advance, sink["id"], "requests", new, len(recs))
                    sent["requests"] += len(recs)
                    progressed = True
            if "audit" in sink["streams"]:
                recs, new_seq = await dbcall(_audit_batch, int(st.get("audit_seq", 0)))
                if recs:
                    await anyio.to_thread.run_sync(deliver, sink, recs)
                    await dbcall(_advance, sink["id"], "audit_seq", new_seq, len(recs))
                    sent["audit"] += len(recs)
                    progressed = True
            if not progressed:
                break
    except Exception as e:  # noqa: BLE001 - one broken destination must not stop the others
        err = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        await sched.redis.delete(lock)
    await dbcall(_record_result, sink["id"], err)
    return {"sink": sink["name"], "sent": sent, "error": err}


def _advance(db, sink_id: str, field: str, value, n: int) -> None:
    st = status_of(db, sink_id)
    st[field] = value
    sent = dict(st.get("sent") or {})
    key = "audit" if field == "audit_seq" else "requests"
    sent[key] = int(sent.get(key, 0)) + n
    st["sent"] = sent
    _save_status(db, sink_id, st)
    db.commit()


def _record_result(db, sink_id: str, err: str | None) -> None:
    st = status_of(db, sink_id)
    if err:
        st["failures"], st["last_error"] = int(st.get("failures", 0)) + 1, err
    else:
        st["failures"], st["last_error"], st["last_ok"] = 0, None, utcnow().isoformat()
    _save_status(db, sink_id, st)
    db.commit()


async def run_all(sched) -> list[dict]:
    out = []
    for sink in await dbcall(load_sinks):
        if sink.get("enabled"):
            out.append(await export_sink(sched, sink))
    return [o for o in out if o.get("error") or any((o.get("sent") or {}).values())]


def failing(db) -> list[tuple[str, int, str]]:
    out = []
    for s in load_sinks(db):
        st = status_of(db, s["id"])
        if s.get("enabled") and int(st.get("failures", 0)) >= 3:
            out.append((s["name"], int(st["failures"]), str(st.get("last_error") or "")))
    return out


def send_test(sink: dict) -> str | None:
    """Send one clearly-marked test record. Returns an error string or None."""
    try:
        deliver(sink, [{"stream": "test", "id": uuid.uuid4().hex, "created_at": utcnow().isoformat(),
                        "message": "Nexora log sink test"}])
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:200]}"
