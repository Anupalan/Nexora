"""OpenAI-compatible inference API (`/v1`). Roadmap §17, §21.

Authenticates Nexora virtual keys, enforces model access, then runs the request through quota
reservation and the per-deployment FIFO queue before forwarding to LiteLLM with the caller's own
key (the master key never leaves the management path).

Queued requests: the HTTP connection is held open and nothing is sent until the request is
admitted. For streaming requests that must wait, the response starts immediately as SSE with
`: queued` comment lines as keepalives (ignored by OpenAI clients); if the upstream then fails,
an SSE `data: {"error": ...}` event is sent. Requests admitted immediately use the upstream's
real status code. `x-nexora-request-id` identifies the request for status/cancel APIs.
"""

import asyncio
import json
from contextlib import aclosing
from dataclasses import dataclass

import anyio
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from app import inference
from app.gwerrors import GatewayError
from app.inference import Admitted, dbcall, get_scheduler
from app.litellm import LiteLLMClient, get_litellm
from app.models import ApiKey, Model, as_utc, utcnow
from app.scheduler import Scheduler
from app.security import hash_token

router = APIRouter(prefix="/v1", tags=["gateway"])


@dataclass
class KeyInfo:
    id: object
    user_id: object
    allowed_models: list[str]
    secret: str


def _lookup(db, token: str) -> KeyInfo | None:
    from datetime import timedelta

    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_token(token)))
    now = utcnow()
    if (key is None or key.status != "active" or key.user.status != "active"
            or (key.expires_at and as_utc(key.expires_at) <= now)):
        return None
    if key.last_used_at is None or now - as_utc(key.last_used_at) > timedelta(minutes=1):
        key.last_used_at = now
        db.commit()
    return KeyInfo(key.id, key.user_id, list(key.allowed_models), token)


async def gateway_key(request: Request) -> KeyInfo:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise GatewayError(401, "invalid_api_key", "Missing bearer API key.", "authentication_error")
    info = await dbcall(_lookup, token.strip())
    if info is None:
        raise GatewayError(401, "invalid_api_key", "Invalid, expired or revoked API key.",
                           "authentication_error")
    return info


@router.get("/models")
async def list_models(key: KeyInfo = Depends(gateway_key)):
    def q(db):
        return [(m.model_alias, int(m.created_at.timestamp())) for m in db.scalars(
            select(Model).where(Model.enabled.is_(True), Model.model_alias.in_(key.allowed_models))
            .order_by(Model.model_alias))]

    rows = await dbcall(q)
    return {"object": "list", "data": [
        {"id": a, "object": "model", "created": c, "owned_by": "nexora"} for a, c in rows]}


def _route_headers(adm: Admitted) -> dict:
    """Tell the client what actually served the request (roadmap §19)."""
    return {"x-nexora-deployment": adm.dep_name, "x-nexora-model": adm.model_alias,
            "x-nexora-class": adm.klass}


def _error_event(code: str, message: str) -> list[bytes]:
    err = {"error": {"message": message, "type": "api_error", "param": None, "code": code}}
    return [f"data: {json.dumps(err)}\n\n".encode(), b"data: [DONE]\n\n"]


async def _non_stream(sched: Scheduler, adm: Admitted, up: inference.Upstream, headers: dict) -> Response:
    st = up.resp.status_code
    try:
        raw = await up.resp.aread()
    except httpx.HTTPError:
        raw = b""
    finally:
        await up.close()
    if up.cancelled.is_set():
        await inference.settle(sched, adm.id, adm.dep_id, status="cancelled", consumed=True,
                               in_tok=adm.est_input, out_tok=0, code="cancelled")
        raise GatewayError(409, "request_cancelled", "The request was cancelled.", headers=headers)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if st >= 400:
        await inference.settle(sched, adm.id, adm.dep_id, status="failed", consumed=False,
                               code=f"upstream_{st}", msg=raw[:500].decode(errors="replace"))
    else:
        i, o = inference.usage_from_json(data, adm.est_input)
        await inference.settle(sched, adm.id, adm.dep_id, status="completed", consumed=True,
                               in_tok=i, out_tok=o,
                               ttft_ms=None)
    return JSONResponse(content=data, status_code=st, headers=headers)


async def _respond(sched, litellm, adm, body, key, headers) -> Response:
    """Claimed: connect upstream and answer (stream or not)."""
    try:
        up = await inference.dispatch(sched, litellm, adm, body, key.secret)
        headers = {**headers, **_route_headers(adm)}  # a failover may have changed the deployment
    except inference.UpstreamCancelled:
        await inference.settle(sched, adm.id, adm.dep_id, status="cancelled", consumed=True,
                               in_tok=adm.est_input, out_tok=0, code="cancelled")
        raise GatewayError(409, "request_cancelled", "The request was cancelled.", headers=headers)
    except httpx.HTTPError:
        await inference.settle(sched, adm.id, adm.dep_id, status="failed", consumed=False,
                               code="upstream_unavailable", msg="Model gateway unreachable.")
        raise GatewayError(502, "upstream_unavailable", "Model gateway is unavailable.", "api_error",
                           headers=headers)
    if not adm.stream:
        return await _non_stream(sched, adm, up, headers)
    if up.resp.status_code >= 400:
        raw = await up.resp.aread()
        await up.close()
        await inference.settle(sched, adm.id, adm.dep_id, status="failed", consumed=False,
                               code=f"upstream_{up.resp.status_code}", msg=raw[:500].decode(errors="replace"))
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"error": {"message": "Upstream error", "type": "api_error", "code": "upstream_error"}}
        return JSONResponse(content=data, status_code=up.resp.status_code, headers=headers)
    ctype = up.resp.headers.get("content-type", "text/event-stream")
    return StreamingResponse(inference.stream_through(sched, adm, up), media_type=ctype,
                             headers={**headers, "cache-control": "no-cache", "x-accel-buffering": "no"})


def _stream_after_wait(sched, litellm, adm, body, key, request: Request, headers):
    async def gen():
        yield f": queued request_id={adm.id}\n\n".encode()
        task = asyncio.create_task(inference.wait_for_turn(sched, adm))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=5)
                if not task.done():
                    pos = await sched.position(adm.dep_id, adm.id)
                    yield f": queued position={pos}\n\n".encode()
        except (asyncio.CancelledError, GeneratorExit):
            task.cancel()
            await inference.drop_waiter(sched, adm, "cancelled", "client_disconnected",
                                        "Client disconnected while queued.")
            raise
        outcome = task.result()
        if outcome != "claimed":
            if outcome == "timeout":
                await inference.drop_waiter(sched, adm, "failed", "queue_timeout", "Timed out in queue.")
            for b in _error_event("queue_timeout" if outcome == "timeout" else "request_cancelled",
                                  "Timed out waiting in the queue." if outcome == "timeout"
                                  else "The request was cancelled."):
                yield b
            return
        try:
            up = await inference.dispatch(sched, litellm, adm, body, key.secret)
        except inference.UpstreamCancelled:
            await inference.settle(sched, adm.id, adm.dep_id, status="cancelled", consumed=True,
                                   in_tok=adm.est_input, out_tok=0, code="cancelled")
            for b in _error_event("request_cancelled", "The request was cancelled."):
                yield b
            return
        except httpx.HTTPError:
            await inference.settle(sched, adm.id, adm.dep_id, status="failed", consumed=False,
                                   code="upstream_unavailable", msg="Model gateway unreachable.")
            for b in _error_event("upstream_unavailable", "Model gateway is unavailable."):
                yield b
            return
        if up.resp.status_code >= 400:
            raw = await up.resp.aread()
            await up.close()
            await inference.settle(sched, adm.id, adm.dep_id, status="failed", consumed=False,
                                   code=f"upstream_{up.resp.status_code}",
                                   msg=raw[:500].decode(errors="replace"))
            for b in _error_event(f"upstream_{up.resp.status_code}", "The model request failed."):
                yield b
            return
        async with aclosing(inference.stream_through(sched, adm, up)) as inner:
            async for b in inner:
                yield b

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={**headers, "cache-control": "no-cache", "x-accel-buffering": "no"})


async def _proxy(request: Request, key: KeyInfo, sched: Scheduler, litellm: LiteLLMClient):
    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        raise GatewayError(400, "invalid_json", "Request body must be a JSON object.")
    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise GatewayError(400, "missing_model", "The 'model' field is required.")
    if alias not in key.allowed_models:
        # Same answer for "unknown" and "not permitted": don't reveal other models.
        raise GatewayError(404, "model_not_found", f"The model '{alias}' does not exist or you do "
                                                   "not have access to it.")
    idem = (request.headers.get("idempotency-key") or "")[:200] or None
    adm = await inference.admit(sched, key_id=key.id, alias=alias, body=body, idem=idem)
    try:
        headers = {"x-nexora-request-id": str(adm.id), **_route_headers(adm)}

        if await inference.try_claim(sched, adm):
            return await _respond(sched, litellm, adm, body, key, headers)
        if adm.stream:
            return _stream_after_wait(sched, litellm, adm, body, key, request, headers)

        try:
            outcome = await inference.wait_for_turn(sched, adm, disconnected=request.is_disconnected)
        except asyncio.CancelledError:  # server-side cancellation of the handler
            with anyio.CancelScope(shield=True):
                await inference.drop_waiter(sched, adm, "cancelled", "client_disconnected",
                                            "Client disconnected while queued.")
            raise
        if outcome == "claimed":
            return await _respond(sched, litellm, adm, body, key, headers)
        if outcome == "timeout":
            await inference.drop_waiter(sched, adm, "failed", "queue_timeout", "Timed out in queue.")
            raise GatewayError(504, "queue_timeout", "Timed out waiting in the queue.", "api_error",
                               headers=headers)
        if outcome == "disconnected":
            await inference.drop_waiter(sched, adm, "cancelled", "client_disconnected",
                                        "Client disconnected while queued.")
            return Response(status_code=499)
        raise GatewayError(409, "request_cancelled", "The request was cancelled.", headers=headers)
    except asyncio.CancelledError:
        await inference.abandon(sched, adm)
        raise


@router.post("/chat/completions")
async def chat_completions(request: Request, key: KeyInfo = Depends(gateway_key),
                           sched: Scheduler = Depends(get_scheduler),
                           litellm: LiteLLMClient = Depends(get_litellm)):
    return await _proxy(request, key, sched, litellm)


@router.post("/completions")
async def completions(request: Request, key: KeyInfo = Depends(gateway_key),
                      sched: Scheduler = Depends(get_scheduler),
                      litellm: LiteLLMClient = Depends(get_litellm)):
    return await _proxy(request, key, sched, litellm)
