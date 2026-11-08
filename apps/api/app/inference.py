"""Admission, queueing, execution and settlement of inference requests (roadmap §21).

Flow: authenticate -> resolve quotas -> atomically reserve -> durable record -> FIFO enqueue ->
wait for turn (connection held open; queue position never reserves a slot) -> forward to LiteLLM
-> stream through unbuffered -> record actual usage -> reconcile reservation -> release slot.
"""

import asyncio
import json
import math
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import redis.asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import quota, router
from app.config import get_settings
from app.db import session_scope
from app.gwerrors import GatewayError
from app.litellm import LiteLLMClient
from app.models import (
    ApiKey,
    InferenceNode,
    InferenceRequest,
    Model,
    ModelDeployment,
    RequestEvent,
    UsageRecord,
    utcnow,
)
from app.scheduler import Scheduler

# ---- redis / scheduler singletons ------------------------------------------------------


class LoopRedis:
    """Redis client resolved per running event loop (connections are loop-bound)."""

    def __init__(self, url: str):
        self._url = url
        self._clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _client(self):
        loop = asyncio.get_running_loop()
        c = self._clients.get(loop)
        if c is None:
            c = aioredis.Redis.from_url(self._url)
            self._clients[loop] = c
        return c

    def __getattr__(self, name: str):
        return getattr(self._client(), name)


_scheduler: Scheduler | None = None


def set_scheduler(s: Scheduler | None) -> None:
    global _scheduler
    _scheduler = s


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        st = get_settings()
        _scheduler = Scheduler(LoopRedis(st.redis_url), lease_seconds=st.lease_seconds,
                               waiter_ttl_seconds=st.waiter_ttl_seconds)
    return _scheduler


async def dbcall(fn: Callable, *args, **kwargs):
    """Run a sync DB function with a fresh session in a worker thread."""
    def run():
        with session_scope() as db:
            return fn(db, *args, **kwargs)
    return await anyio.to_thread.run_sync(run)


# ---- sync DB helpers ---------------------------------------------------------------------

def ensure_default_deployment(db, model_id: uuid.UUID) -> ModelDeployment:
    """One queue per deployment, so there must be exactly one default; safe under concurrency."""
    def first():
        return db.scalar(select(ModelDeployment).where(ModelDeployment.model_id == model_id)
                         .order_by(ModelDeployment.created_at, ModelDeployment.deployment_name))

    dep = first()
    if dep is None:
        db.add(ModelDeployment(model_id=model_id, deployment_name="default", endpoint_reference="litellm"))
        try:
            db.commit()
        except IntegrityError:  # another request created it first (unique model_id+name)
            db.rollback()
        dep = first()
    return dep


def load_context(db, key_id: uuid.UUID, alias: str) -> dict | None:
    key = db.get(ApiKey, key_id)
    model = db.scalar(select(Model).where(Model.model_alias == alias, Model.enabled.is_(True)))
    if key is None or model is None:
        return None
    ensure_default_deployment(db, model.id)  # every model has at least one deployment
    return {
        "user_id": key.user_id, "team_id": key.team_id, "project_id": key.project_id,
        "key_id": key.id, "model_id": model.id, "model_max_out": model.max_output_tokens,
        "loaded": router.load_candidates(db, model.id),
        "limits": quota.resolve_limits(db, key),
    }


def create_request(db, ctx: dict, *, rid: uuid.UUID, decision: router.Decision, reservation: dict,
                   stream: bool, idem: str | None, source: str) -> uuid.UUID | None:
    cand = decision.chosen
    r = InferenceRequest(
        id=rid, user_id=ctx["user_id"], team_id=ctx["team_id"], project_id=ctx["project_id"],
        api_key_id=ctx["key_id"], model_id=ctx["model_id"], deployment_id=uuid.UUID(cand.id),
        source=source, status="queued", stream=stream, idempotency_key=idem,
        reserved_tokens=reservation["tokens"], reservation=reservation)
    db.add(r)
    try:
        db.flush()
        db.add(RequestEvent(request_id=r.id, event_type="queued", event_data={"deployment": cand.name}))
        router.record_decision(db, r.id, 1, ctx["loaded"].policy, decision)
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return r.id


def reject_unrouted(db, ctx: dict, rid: uuid.UUID, decision: router.Decision, *, stream: bool, source: str) -> None:
    """Log a request that no deployment could take. It never had a reservation or a queue slot."""
    cands = ctx["loaded"].candidates
    r = InferenceRequest(
        id=rid, user_id=ctx["user_id"], team_id=ctx["team_id"], project_id=ctx["project_id"],
        api_key_id=ctx["key_id"], model_id=ctx["model_id"], deployment_id=uuid.UUID(cands[0].id),
        source=source, status="rejected", stream=stream, error_code=decision.error_code,
        error_message=decision.error_message, completed_at=utcnow(), reserved_tokens=0, reservation={})
    db.add(r)
    db.flush()
    db.add(RequestEvent(request_id=r.id, event_type="rejected", event_data={"code": decision.error_code}))
    router.record_decision(db, r.id, 1, ctx["loaded"].policy, decision)
    db.commit()


def existing_by_idem(db, user_id, idem: str) -> str | None:
    r = db.scalar(select(InferenceRequest).where(InferenceRequest.user_id == user_id,
                                                  InferenceRequest.idempotency_key == idem))
    return str(r.id) if r else None


def set_sequence(db, rid: uuid.UUID, seq: int) -> None:
    r = db.get(InferenceRequest, rid)
    r.queue_sequence = seq
    db.commit()


def reject_request(db, rid: uuid.UUID, code: str, message: str) -> None:
    r = db.get(InferenceRequest, rid)
    r.status, r.error_code, r.error_message, r.completed_at = "rejected", code, message, utcnow()
    db.add(RequestEvent(request_id=rid, event_type="rejected", event_data={"code": code}))
    db.commit()


def mark_running(db, rid: uuid.UUID) -> int:
    from app.models import as_utc

    r = db.get(InferenceRequest, rid)
    now = utcnow()
    r.status, r.started_at = "running", now
    r.queue_time_ms = int((now - as_utc(r.created_at)).total_seconds() * 1000)
    db.add(RequestEvent(request_id=rid, event_type="started", event_data={"queue_ms": r.queue_time_ms}))
    db.commit()
    return r.queue_time_ms


def finalize(db, rid: uuid.UUID, *, status: str, consumed: bool, in_tok: int = 0, out_tok: int = 0,
             ttft_ms: int | None = None, code: str | None = None, msg: str | None = None) -> dict | None:
    """Move a queued/running request to a terminal state exactly once. None if already final."""
    from app.models import as_utc

    r = db.get(InferenceRequest, rid, with_for_update=True)
    if r is None or r.status not in ("queued", "running"):
        return None
    now = utcnow()
    snap = {"reservation": r.reservation or {}, "deployment_id": r.deployment_id,
            "reserved": r.reserved_tokens}
    r.status, r.completed_at = status, now
    r.error_code, r.error_message = code, (msg[:1000] if msg else None)
    if consumed:
        r.input_tokens, r.output_tokens, r.time_to_first_token_ms = in_tok, out_tok, ttft_ms
        if r.started_at:
            r.duration_ms = int((now - as_utc(r.started_at)).total_seconds() * 1000)
        db.add(UsageRecord(request_id=r.id, user_id=r.user_id, team_id=r.team_id, project_id=r.project_id,
                           api_key_id=r.api_key_id, model_id=r.model_id, input_tokens=in_tok,
                           output_tokens=out_tok, total_tokens=in_tok + out_tok))
    db.add(RequestEvent(request_id=rid, event_type=status, event_data={"code": code} if code else {}))
    db.commit()
    return snap


# ---- admission ---------------------------------------------------------------------------

@dataclass
class Admitted:
    id: uuid.UUID
    dep_id: uuid.UUID
    max_active: int
    litellm_model: str
    est_input: int
    reserved: int
    stream: bool
    sequence: int
    model_id: uuid.UUID | None = None
    dep_name: str = ""
    model_alias: str = ""
    node_id: str | None = None
    node_max: int = 0
    attempt: int = 1


def _estimate_input(body: dict) -> int:
    src = body.get("messages") if body.get("messages") is not None else body.get("prompt", "")
    return max(1, math.ceil(len(json.dumps(src, ensure_ascii=False)) / 4))


async def admit(sched: Scheduler, *, key_id: uuid.UUID, alias: str, body: dict, idem: str | None,
                source: str = "api") -> Admitted:
    st = get_settings()
    ctx = await dbcall(load_context, key_id, alias)
    if ctx is None:
        raise GatewayError(404, "model_not_found", f"The model '{alias}' does not exist or you do "
                                                   "not have access to it.")
    if idem:
        prior = await dbcall(existing_by_idem, ctx["user_id"], idem)
        if prior:
            raise GatewayError(409, "duplicate_request",
                               f"A request with this Idempotency-Key already exists ({prior}).")

    # Output cap = tightest of client request, model limit and any quota scope.
    caps = [c for c in (ctx["model_max_out"], quota.effective_max_output(ctx["limits"])) if c]
    cap = min(caps) if caps else None
    out_field = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
    asked = body.get(out_field)
    asked = asked if isinstance(asked, int) and asked > 0 else None
    chosen = min([x for x in (asked, cap) if x] or [0]) or None
    if chosen:
        body[out_field] = chosen
    est_out = chosen or st.default_output_tokens_estimate
    est_in = _estimate_input(body)
    est = est_in + est_out

    # Choose the deployment before reserving quota, so a request nothing can serve costs no quota.
    loaded = ctx["loaded"]
    rid = uuid.uuid4()
    try:
        await router.fill_load(sched, loaded)
    except RedisError:
        raise GatewayError(503, "scheduler_unavailable", "Request scheduler is unavailable.", "api_error")
    decision = router.choose(loaded.candidates, loaded.policy, est_tokens=est, request_id=str(rid))
    if decision.chosen is None:
        await dbcall(reject_unrouted, ctx, rid, decision, stream=bool(body.get("stream")), source=source)
        status = {"context_length_exceeded": 400, "queue_full": 429}.get(decision.error_code, 503)
        raise GatewayError(status, decision.error_code, decision.error_message,
                           "invalid_request_error" if status == 400 else
                           "rate_limit_error" if status == 429 else "api_error",
                           headers={"Retry-After": "5"} if status in (429, 503) else None)
    pick = decision.chosen

    now = utcnow()
    plan = quota.build_plan(ctx["limits"], now, est)
    try:
        rej = await quota.reserve(sched.redis, plan, now)
        if rej:
            raise GatewayError(429, rej.code, rej.message, "rate_limit_error",
                               headers={"Retry-After": str(rej.retry_after)})
        reservation = plan.reservation(est)
        created = await dbcall(create_request, ctx, rid=rid, decision=decision, reservation=reservation,
                               stream=bool(body.get("stream")), idem=idem, source=source)
        if created is None:  # lost an idempotency race
            await quota.release_capacity(sched.redis, reservation, requests=True, tokens=est)
            raise GatewayError(409, "duplicate_request",
                               "A request with this Idempotency-Key already exists.")
        try:
            seq = await sched.enqueue(pick.id, rid, pick.max_queue)
            if seq < 0:
                await dbcall(reject_request, rid, "queue_full", "The request queue is full.")
                await quota.release_capacity(sched.redis, reservation, requests=True, tokens=est)
                raise GatewayError(429, "queue_full", "The request queue is full. Retry shortly.",
                                   "rate_limit_error", headers={"Retry-After": "5"})
            await dbcall(set_sequence, rid, seq)
        except asyncio.CancelledError:
            # The client vanished after the record and reservation exist: undo both, even though the
            # task is being cancelled (the shield lets this cleanup finish).
            with anyio.CancelScope(shield=True):
                await sched.remove_queued(pick.id, rid)
                await settle(sched, rid, uuid.UUID(pick.id), status="cancelled", consumed=False,
                             code="client_disconnected", msg="Client disconnected during admission.")
            raise
    except RedisError:
        raise GatewayError(503, "scheduler_unavailable", "Request scheduler is unavailable.", "api_error")
    return Admitted(rid, pick.id, pick.max_active, pick.litellm_model, est_in, est,
                    bool(body.get("stream")), seq, model_id=ctx["model_id"], dep_name=pick.name,
                    model_alias=pick.model_alias, node_id=pick.node_id, node_max=pick.node_budget or 0)


# ---- settlement --------------------------------------------------------------------------

async def settle(sched: Scheduler, adm_id: uuid.UUID, dep_id: uuid.UUID, *, status: str, consumed: bool,
                 in_tok: int = 0, out_tok: int = 0, ttft_ms: int | None = None,
                 code: str | None = None, msg: str | None = None) -> None:
    with anyio.CancelScope(shield=True):  # must finish even if the client went away
        snap = await dbcall(finalize, adm_id, status=status, consumed=consumed, in_tok=in_tok,
                            out_tok=out_tok, ttft_ms=ttft_ms, code=code, msg=msg)
        if snap is None:
            return
        res = snap["reservation"]
        try:
            if consumed:
                delta = in_tok + out_tok - snap["reserved"]
                await quota.adjust_tokens(sched.redis, res.get("tok", []), delta)
                await quota.release_capacity(sched.redis, res, requests=False, tokens=0)
            else:
                await quota.release_capacity(sched.redis, res, requests=True, tokens=snap["reserved"])
            await sched.release_active(dep_id, adm_id)
            await sched.clear(adm_id)
        except RedisError:
            pass  # counters are rebuilt from Postgres by ensure_counters/sweeper


# ---- waiting and execution ---------------------------------------------------------------

async def try_claim(sched: Scheduler, adm: Admitted) -> bool:
    return await sched.try_claim(adm.dep_id, adm.id, adm.max_active, adm.node_id, adm.node_max)


def _apply_move(adm: Admitted, info: dict) -> None:
    adm.dep_id, adm.max_active, adm.litellm_model = info["dep_id"], info["max_active"], info["litellm_model"]
    adm.dep_name, adm.node_id, adm.node_max = info["dep_name"], info.get("node_id"), info.get("node_max", 0)


async def wait_for_turn(sched: Scheduler, adm: Admitted, *, disconnected: Callable | None = None,
                        on_wait: Callable | None = None) -> str:
    """Returns claimed | cancelled | timeout | disconnected. Never holds a slot while waiting."""
    st = get_settings()
    deadline = time.monotonic() + st.queue_timeout_seconds
    delay, last_touch, last_note = 0.02, 0.0, 0.0
    while True:
        if await try_claim(sched, adm):
            return "claimed"
        pos = await sched.position(adm.dep_id, adm.id)
        if pos is None:
            moved = await sched.get_moved(adm.id)  # re-routed to another deployment while waiting?
            if moved and moved["dep_id"] != adm.dep_id:
                _apply_move(adm, moved)
                continue
            return "cancelled"
        now = time.monotonic()
        if now > deadline:
            return "timeout"
        if disconnected and await disconnected():
            return "disconnected"
        if now - last_touch > st.waiter_ttl_seconds / 3:
            await sched.touch_waiter(adm.id)
            last_touch = now
        if on_wait and now - last_note > 5:
            last_note = now
            await on_wait(pos)
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 0.25)


async def abandon(sched: Scheduler, adm: Admitted) -> None:
    """The handler was cancelled after admission: release whatever the request still holds."""
    with anyio.CancelScope(shield=True):
        if await sched.remove_queued(adm.dep_id, adm.id):  # still waiting: nothing was consumed
            await settle(sched, adm.id, uuid.UUID(adm.dep_id), status="cancelled", consumed=False,
                         code="client_disconnected", msg="Client disconnected.")
        else:  # it had been claimed: settle as consumed (a no-op if it already finished)
            await settle(sched, adm.id, uuid.UUID(adm.dep_id), status="cancelled", consumed=True,
                         in_tok=adm.est_input, out_tok=0, code="client_disconnected", msg="Client disconnected.")


async def drop_waiter(sched: Scheduler, adm: Admitted, status: str, code: str, msg: str) -> None:
    """Remove from the queue and settle as not-consumed. Only the remover settles.

    Shielded: on client disconnect Starlette keeps cancelling the response task, which would
    otherwise interrupt this between the Redis removal and the database/quota settlement.
    """
    with anyio.CancelScope(shield=True):
        if await sched.remove_queued(adm.dep_id, adm.id):
            await settle(sched, adm.id, adm.dep_id, status=status, consumed=False, code=code, msg=msg)


class UpstreamCancelled(Exception):
    """The request was cancelled (via the cancel API) before/while connecting upstream."""


@dataclass
class Upstream:
    resp: httpx.Response
    client: httpx.AsyncClient
    started: float
    cancelled: asyncio.Event
    watcher: asyncio.Task

    async def close(self) -> None:
        self.watcher.cancel()
        await self.resp.aclose()
        await self.client.aclose()


async def _watch(sched: Scheduler, adm: Admitted, cancelled: asyncio.Event, box: dict) -> None:
    """Lease heartbeat + cancellation flag while a request is running."""
    st = get_settings()
    last_hb = time.monotonic()
    while True:
        await asyncio.sleep(0.25)
        if await sched.cancel_requested(adm.id):
            cancelled.set()
            if box.get("resp") is not None:
                await box["resp"].aclose()
            elif box.get("fwd") is not None:
                box["fwd"].cancel()
            return
        if time.monotonic() - last_hb >= st.heartbeat_seconds:
            last_hb = time.monotonic()
            await sched.heartbeat_active(adm.dep_id, adm.id)


async def start_upstream(sched: Scheduler, litellm: LiteLLMClient, adm: Admitted, body: dict,
                         caller_key: str) -> Upstream:
    """Claimed: mark running, connect. Raises httpx.HTTPError if the gateway is unreachable."""
    await dbcall(mark_running, adm.id)
    cancelled = asyncio.Event()
    box: dict = {}
    watcher = asyncio.create_task(_watch(sched, adm, cancelled, box))
    body = {**body, "model": adm.litellm_model}
    if adm.stream:
        opts = dict(body.get("stream_options") or {})
        opts.setdefault("include_usage", True)
        body["stream_options"] = opts
    path = "/v1/chat/completions" if "messages" in body else "/v1/completions"
    box["fwd"] = asyncio.ensure_future(litellm.forward(path, body, caller_key))
    try:
        resp = await box["fwd"]
    except asyncio.CancelledError:
        watcher.cancel()
        if cancelled.is_set():
            raise UpstreamCancelled from None
        box["fwd"].cancel()
        raise
    except BaseException:
        watcher.cancel()
        raise
    box["resp"] = resp
    return Upstream(resp, resp.extensions["nexora_client"], time.monotonic(), cancelled, watcher)


def usage_from_json(data: Any, est_in: int) -> tuple[int, int]:
    u = data.get("usage") if isinstance(data, dict) else None
    if isinstance(u, dict) and ("prompt_tokens" in u or "total_tokens" in u):
        p = int(u.get("prompt_tokens") or 0)
        c = int(u.get("completion_tokens") or max(0, int(u.get("total_tokens") or 0) - p))
        return p, c
    text = ""
    for ch in (data.get("choices") or []) if isinstance(data, dict) else []:
        text += ((ch.get("message") or {}).get("content") or ch.get("text") or "")
    return est_in, max(1, len(text) // 4)


class SSEUsage:
    """Incrementally scans an SSE byte stream for a usage block, without altering the bytes."""

    def __init__(self):
        self.buf = b""
        self.usage: tuple[int, int] | None = None
        self.chars = 0

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        for ln in lines:
            ln = ln.strip()
            if not ln.startswith(b"data:"):
                continue
            payload = ln[5:].strip()
            if payload in (b"", b"[DONE]"):
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if isinstance(obj, dict):
                if isinstance(obj.get("usage"), dict):
                    self.usage = usage_from_json(obj, 0)
                for ch in obj.get("choices") or []:
                    self.chars += len((ch.get("delta") or {}).get("content") or ch.get("text") or "")


async def stream_through(sched: Scheduler, adm: Admitted, up: Upstream,
                         prefix: list[bytes] | None = None) -> AsyncIterator[bytes]:
    """Yield upstream bytes untouched; always settle, including on client disconnect."""
    sse, ttft, status, code, msg = SSEUsage(), None, "completed", None, None
    try:
        for p in prefix or []:
            yield p
        async for chunk in up.resp.aiter_raw():
            if ttft is None:
                ttft = int((time.monotonic() - up.started) * 1000)
            sse.feed(chunk)
            yield chunk
        if up.cancelled.is_set():
            status, code = "cancelled", "cancelled"
    except (asyncio.CancelledError, GeneratorExit):
        status, code = "cancelled", "client_disconnected"
        raise
    except httpx.HTTPError as e:
        status, code, msg = ("cancelled", "cancelled", None) if up.cancelled.is_set() \
            else ("failed", "upstream_error", str(e))
    finally:
        in_tok, out_tok = sse.usage or (adm.est_input, max(1, sse.chars // 4) if sse.chars else 0)
        with anyio.CancelScope(shield=True):
            await settle(sched, adm.id, adm.dep_id, status=status, consumed=True, in_tok=in_tok,
                         out_tok=out_tok, ttft_ms=ttft, code=code, msg=msg)
            await up.close()


# ---- failover and queued re-routing (roadmap §6) -----------------------------------------

def looks_like_backend_connection_failure(status: int, raw: bytes) -> bool:
    """LiteLLM answers 500 'OpenAIException - Connection error.' when a node cannot be reached.

    The text alone is NOT proof the request never ran (a connection reset mid-request reads the same),
    so callers must also confirm the node is really unreachable before failing over.
    """
    if status not in (500, 502, 503, 504):
        return False
    text = raw[:2000].decode(errors="replace").lower()
    return any(m in text for m in ("connection error", "cannot connect", "connection refused",
                                   "all connection attempts failed", "name or service not known"))


def _node_url_for(db, dep_id: str):
    d = db.get(ModelDeployment, uuid.UUID(dep_id))
    n = db.get(InferenceNode, d.node_id) if d and d.node_id else None
    return (n.id, n.runtime_url) if n and n.runtime_url else (None, None)


async def node_confirmed_down(dep_id: str) -> bool:
    """Probe the deployment's node right now (read-only). Only a confirmed outage permits failover."""
    from app import sampler

    node_id, url = await dbcall(_node_url_for, dep_id)
    if not url:
        return False  # cannot confirm -> do not replay
    state = await sampler.sample_node(node_id, url)  # also refreshes health for every deployment on it
    return not state.get("reachable")


def _reroute_db(db, rid: uuid.UUID, new_dep: str, seq: int, policy, decision, *, from_dep: str, attempt: int,
                event: str, requeue: bool) -> None:
    r = db.get(InferenceRequest, rid)
    if r is None or r.status not in ("queued", "running"):
        return
    r.deployment_id, r.queue_sequence = uuid.UUID(new_dep), seq
    if requeue:  # it was running on the failed deployment and never produced output
        r.status, r.started_at, r.queue_time_ms = "queued", None, None
    router.record_decision(db, rid, attempt, policy, decision, failover_from=from_dep)
    db.add(RequestEvent(request_id=rid, event_type=event,
                        event_data={"from": from_dep, "to": new_dep, "reason": decision.reason[:300]}))
    db.commit()


def _mark_unhealthy(db, dep_id: str, detail: str) -> None:
    d = db.get(ModelDeployment, uuid.UUID(dep_id))
    d.health_status = "unhealthy"
    d.configuration = {**(d.configuration or {}), "health": {
        "checked_at": utcnow().isoformat(), "detail": detail, "source": "dispatch failure"}}
    db.commit()


async def failover(sched: Scheduler, adm: Admitted) -> bool:
    """Move a request that could not be dispatched to another deployment. False if there is none."""
    old = adm.dep_id
    await dbcall(_mark_unhealthy, old, "node unreachable during dispatch")
    loaded = await dbcall(router.load_candidates, adm.model_id)
    await router.fill_load(sched, loaded)
    decision = router.choose(loaded.candidates, loaded.policy, est_tokens=adm.reserved,
                             request_id=str(adm.id), exclude={old})
    if decision.chosen is None:
        return False
    new = decision.chosen
    await sched.release_active(old, adm.id)
    seq = await sched.enqueue(new.id, adm.id, new.max_queue)
    if seq < 0:
        return False
    adm.attempt += 1
    _apply_move(adm, {"dep_id": new.id, "max_active": new.max_active, "litellm_model": new.litellm_model,
                      "dep_name": new.name, "node_id": new.node_id, "node_max": new.node_budget or 0})
    adm.sequence = seq
    await dbcall(_reroute_db, adm.id, new.id, seq, loaded.policy, decision, from_dep=old,
                 attempt=adm.attempt, event="failover", requeue=True)
    return True


async def dispatch(sched: Scheduler, litellm: LiteLLMClient, adm: Admitted, body: dict,
                   caller_key: str) -> Upstream:
    """Connect upstream; if a node proves unreachable, fail over once to another deployment."""
    up = await start_upstream(sched, litellm, adm, body, caller_key)
    if up.resp.status_code < 400:
        return up
    raw = await up.resp.aread()
    if not (looks_like_backend_connection_failure(up.resp.status_code, raw)
            and await node_confirmed_down(adm.dep_id) and await failover(sched, adm)):
        return up  # callers handle the error response (it is not replayed)
    await up.close()
    outcome = await wait_for_turn(sched, adm)
    if outcome != "claimed":
        if outcome == "timeout":
            await drop_waiter(sched, adm, "failed", "queue_timeout", "Timed out in queue after failover.")
        raise httpx.ConnectError("deployment unavailable after failover")
    return await start_upstream(sched, litellm, adm, body, caller_key)


def _model_of(db, dep_id):
    d = db.get(ModelDeployment, uuid.UUID(dep_id))
    return d.model_id


def _reserved(db, rid):
    r = db.get(InferenceRequest, uuid.UUID(rid))
    return r.reserved_tokens if r else 0


def _needs_evacuation(db) -> list[tuple[uuid.UUID, uuid.UUID]]:
    rows = db.execute(select(ModelDeployment, InferenceNode).outerjoin(
        InferenceNode, InferenceNode.id == ModelDeployment.node_id))
    out = []
    for d, n in rows:
        node_off = n is not None and not n.enabled
        if not d.enabled or d.draining or d.health_status == "unhealthy" or node_off:
            out.append((d.id, d.model_id))
    return out


async def rebalance_queued(sched: Scheduler) -> dict:
    """Move still-queued requests off deployments that are unhealthy, draining or disabled.

    Only queued requests move (running ones finish or fail where they are). Moves happen in queue
    order, so their relative order is preserved; waiters notice the move and keep waiting.
    """
    moved = stuck = 0
    for dep_id, model_id in await dbcall(_needs_evacuation):
        ids = await sched.queued_ids(str(dep_id))
        if not ids:
            continue
        loaded = await dbcall(router.load_candidates, model_id)
        await router.fill_load(sched, loaded)
        for rid in ids:
            est = await dbcall(_reserved, rid)
            decision = router.choose(loaded.candidates, loaded.policy, est_tokens=est, request_id=rid,
                                     exclude={str(dep_id)})
            if decision.chosen is None:
                stuck += len(ids) - ids.index(rid)
                break
            new = decision.chosen
            seq = await sched.move_queued(str(dep_id), new.id, rid, new.max_queue)
            if seq < 0:
                stuck += 1
                continue
            await sched.set_moved(rid, {"dep_id": new.id, "max_active": new.max_active,
                                        "litellm_model": new.litellm_model, "dep_name": new.name,
                                        "node_id": new.node_id, "node_max": new.node_budget or 0})
            await dbcall(_reroute_db, uuid.UUID(rid), new.id, seq, loaded.policy, decision,
                         from_dep=str(dep_id), attempt=2, event="rerouted", requeue=False)
            new.queued += 1  # later moves in this pass see the added load, so they spread out
            moved += 1
    return {"moved": moved, "stuck": stuck}


async def sweep_once(sched: Scheduler) -> dict:
    """Recover from crashes: expired leases, orphaned waiters, and DB rows Redis forgot."""
    from datetime import timedelta

    st = get_settings()
    out = {"leases": 0, "orphans": 0, "lost": 0}

    def load(db):
        deps = [(d.id, d.id) for d in db.scalars(select(ModelDeployment))]
        cutoff = utcnow() - timedelta(seconds=2 * st.waiter_ttl_seconds)
        live = [(r.id, r.deployment_id, r.status, r.reserved_tokens) for r in db.scalars(
            select(InferenceRequest).where(InferenceRequest.status.in_(("queued", "running")),
                                           InferenceRequest.created_at < cutoff))]
        return [d for d, _ in deps], live

    deps, live = await dbcall(load)
    for dep in deps:
        for rid in await sched.expired_leases(dep):
            # Outcome unknown: never replay. Charge the reservation and record the failure.
            r = uuid.UUID(rid)
            reserved = next((x[3] for x in live if x[0] == r), 0)
            await settle(sched, r, dep, status="failed", consumed=True, in_tok=reserved, out_tok=0,
                         code="lease_expired", msg="Worker lost; outcome unknown (not retried).")
            out["leases"] += 1
        for rid in await sched.queued_ids(dep):
            if not await sched.waiter_alive(rid):
                r = uuid.UUID(rid)
                if await sched.remove_queued(dep, r):
                    await settle(sched, r, dep, status="cancelled", consumed=False, code="waiter_lost",
                                 msg="Client connection lost while queued.")
                    out["orphans"] += 1
    for rid, dep, status, reserved in live:
        tracked = (str(rid) in await sched.queued_ids(dep)) or (str(rid) in await sched.active_ids(dep))
        if not tracked:
            await settle(sched, rid, dep, status="failed", consumed=(status == "running"),
                         in_tok=reserved if status == "running" else 0, out_tok=0, code="scheduler_restart",
                         msg="Request state was lost during a scheduler restart (not retried).")
            out["lost"] += 1
    return out
