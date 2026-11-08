"""Mock LiteLLM proxy + vLLM backend for local development and the end-to-end demo.

Mimics the parts of LiteLLM Nexora uses: /key/generate, /key/update, /key/delete (master key),
and OpenAI-compatible /v1/models, /v1/chat/completions, /v1/completions authenticated by the
virtual keys it issued (model allow-list enforced, deleted keys rejected). Replies are canned
and streamed token by token. It is NOT a faithful LiteLLM: validate against the real proxy.

    MOCK_MASTER_KEY=dev MOCK_TOKEN_DELAY=0.05 uvicorn server:app --port 4000
"""

import asyncio
import hashlib
import json
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

MASTER = os.environ.get("MOCK_MASTER_KEY", "dev")
OPEN = os.environ.get("MOCK_OPEN") == "1"  # act as a plain vLLM backend: accept any bearer key
DELAY = float(os.environ.get("MOCK_TOKEN_DELAY", "0.05"))
REPLY = os.environ.get("MOCK_REPLY", "def add(a, b):\n    return a + b\n").split(" ")

app = FastAPI(title="mock-llm")
keys: dict[str, dict] = {}  # sha256(key) -> {models, deleted}
stats = {"inference_calls": 0}


def _h(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def _bearer(auth: str | None) -> str:
    return (auth or "").removeprefix("Bearer ").strip()


def _master(auth: str | None) -> None:
    if _bearer(auth) != MASTER:
        raise HTTPException(401, "bad master key")


@app.post("/key/generate")
async def generate(request: Request, authorization: str | None = Header(None)):
    _master(authorization)
    b = await request.json()
    key = b.get("key") or f"sk-{os.urandom(8).hex()}"
    keys[_h(key)] = {"models": b.get("models") or [], "deleted": False, "alias": b.get("key_alias")}
    return {"key": key, "token": _h(key), "models": b.get("models")}


@app.post("/key/update")
async def update(request: Request, authorization: str | None = Header(None)):
    _master(authorization)
    b = await request.json()
    k = keys.get(b.get("key"))
    if k is None:
        raise HTTPException(404, "unknown key")
    if "models" in b:
        k["models"] = b["models"]
    return {"status": "ok"}


@app.post("/key/delete")
async def delete(request: Request, authorization: str | None = Header(None)):
    _master(authorization)
    for ident in (await request.json()).get("keys", []):
        if ident in keys:
            keys[ident]["deleted"] = True
    return {"deleted_keys": (await request.json()).get("keys", [])}


def _virtual(auth: str | None, model: str | None = None) -> dict:
    if OPEN:
        return {"models": [model] if model else ["qwen-coder"], "deleted": False}
    k = keys.get(_h(_bearer(auth)))
    if k is None or k["deleted"]:
        raise HTTPException(401, "invalid key")
    if model and k["models"] and model not in k["models"]:
        raise HTTPException(401, f"key not allowed to access model {model}")
    return k


@app.get("/v1/models")
async def models(authorization: str | None = Header(None)):
    k = _virtual(authorization)
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in k["models"]]}


@app.get("/stats")
async def get_stats():
    return stats


async def _complete(request: Request, authorization: str | None, chat: bool):
    b = await request.json()
    _virtual(authorization, b.get("model"))
    stats["inference_calls"] += 1
    n_prompt = max(1, len(json.dumps(b.get("messages") or b.get("prompt") or "")) // 4)
    max_tokens = b.get("max_tokens") or b.get("max_completion_tokens") or len(REPLY)
    words = REPLY[:max_tokens]
    usage = {"prompt_tokens": n_prompt, "completion_tokens": len(words),
             "total_tokens": n_prompt + len(words)}
    cid, created, model = f"chatcmpl-{os.urandom(4).hex()}", int(time.time()), b.get("model")

    if not b.get("stream"):
        await asyncio.sleep(DELAY * len(words))
        text = " ".join(words)
        choice = ({"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                  if chat else {"index": 0, "text": text, "finish_reason": "stop"})
        return JSONResponse({"id": cid, "object": "chat.completion", "created": created,
                             "model": model, "choices": [choice], "usage": usage})

    async def sse():
        for i, w in enumerate(words):
            await asyncio.sleep(DELAY)
            delta = {"content": w + ("" if i == len(words) - 1 else " ")}
            ch = ({"index": 0, "delta": delta, "finish_reason": None} if chat
                  else {"index": 0, "text": delta["content"], "finish_reason": None})
            yield f"data: {json.dumps({'id': cid, 'created': created, 'model': model, 'choices': [ch]})}\n\n"
        if (b.get("stream_options") or {}).get("include_usage"):
            yield f"data: {json.dumps({'id': cid, 'model': model, 'choices': [], 'usage': usage})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str | None = Header(None)):
    return await _complete(request, authorization, True)


@app.post("/v1/completions")
async def completions(request: Request, authorization: str | None = Header(None)):
    return await _complete(request, authorization, False)
