"""OpenAI-compatible inference API (`/v1`). Roadmap §17.

Authenticates Nexora virtual keys, enforces model access, then forwards to LiteLLM using the
caller's own key (the master key never leaves the management path). Queueing and quota
reservation arrive in Phase 3; requests are forwarded directly until then.
"""

import json
from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession
from starlette.background import BackgroundTask

from app.db import get_db
from app.litellm import LiteLLMClient, get_litellm
from app.models import ApiKey, Model, as_utc, utcnow
from app.security import hash_token

router = APIRouter(prefix="/v1", tags=["gateway"])


class GatewayError(HTTPException):
    def __init__(self, status: int, code: str, message: str, type_: str = "invalid_request_error"):
        super().__init__(status_code=status, detail={"openai": {
            "message": message, "type": type_, "param": None, "code": code}})


def gateway_key(request: Request, db: DBSession = Depends(get_db)) -> tuple[ApiKey, str]:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise GatewayError(401, "invalid_api_key", "Missing bearer API key.", "authentication_error")
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_token(token.strip())))
    now = utcnow()
    if (key is None or key.status != "active" or key.user.status != "active"
            or (key.expires_at and as_utc(key.expires_at) <= now)):
        raise GatewayError(401, "invalid_api_key", "Invalid, expired or revoked API key.",
                           "authentication_error")
    if key.last_used_at is None or now - as_utc(key.last_used_at) > timedelta(minutes=1):
        key.last_used_at = now
        db.commit()
    return key, token.strip()


@router.get("/models")
def list_models(auth: tuple[ApiKey, str] = Depends(gateway_key), db: DBSession = Depends(get_db)):
    key, _ = auth
    rows = db.scalars(select(Model).where(Model.enabled.is_(True),
                                          Model.model_alias.in_(key.allowed_models))
                      .order_by(Model.model_alias))
    return {"object": "list", "data": [
        {"id": m.model_alias, "object": "model", "created": int(m.created_at.timestamp()),
         "owned_by": "nexora"} for m in rows]}


async def _proxy(path: str, request: Request, auth: tuple[ApiKey, str], db: DBSession,
                 litellm: LiteLLMClient):
    key, secret = auth
    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        raise GatewayError(400, "invalid_json", "Request body must be a JSON object.")
    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise GatewayError(400, "missing_model", "The 'model' field is required.")
    model = db.scalar(select(Model).where(Model.model_alias == alias, Model.enabled.is_(True)))
    if model is None or alias not in key.allowed_models:
        # Same answer for "unknown" and "not permitted": don't reveal other models.
        raise GatewayError(404, "model_not_found", f"The model '{alias}' does not exist or you "
                                                   "do not have access to it.")
    if model.max_output_tokens:
        for f in ("max_tokens", "max_completion_tokens"):
            if isinstance(body.get(f), int) and body[f] > model.max_output_tokens:
                body[f] = model.max_output_tokens
    body["model"] = model.litellm_model
    try:
        upstream = await litellm.forward(path, body, secret)
    except httpx.HTTPError:
        raise GatewayError(502, "upstream_unavailable", "Model gateway is unavailable.", "api_error")

    client = upstream.extensions["nexora_client"]

    async def close():
        await upstream.aclose()
        await client.aclose()

    ctype = upstream.headers.get("content-type", "application/json")
    if body.get("stream") or ctype.startswith("text/event-stream"):
        # Pass bytes straight through: no buffering of the completion.
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 media_type=ctype, background=BackgroundTask(close),
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
    try:
        content = await upstream.aread()
    finally:
        await close()
    return JSONResponse(content=json.loads(content) if content else {}, status_code=upstream.status_code)


@router.post("/chat/completions")
async def chat_completions(request: Request, auth: tuple[ApiKey, str] = Depends(gateway_key),
                           db: DBSession = Depends(get_db), litellm: LiteLLMClient = Depends(get_litellm)):
    return await _proxy("/v1/chat/completions", request, auth, db, litellm)


@router.post("/completions")
async def completions(request: Request, auth: tuple[ApiKey, str] = Depends(gateway_key),
                      db: DBSession = Depends(get_db), litellm: LiteLLMClient = Depends(get_litellm)):
    return await _proxy("/v1/completions", request, auth, db, litellm)
