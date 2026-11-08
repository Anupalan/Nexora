"""Samples each inference node's Ollama (version, installed models + digests, loaded models) and derives
per-deployment health from it (roadmap §6.3).

This uses only read-only endpoints (/api/version, /api/tags, /api/ps), so it never loads a model or
perturbs production traffic. That also makes it a better health signal for Ollama than a completion
probe, which would load a cold model every cycle.
"""

import anyio
import httpx
from sqlalchemy import select

from app.inference import dbcall
from app.models import InferenceNode, ModelDeployment, utcnow

transport_override: httpx.BaseTransport | None = None  # tests inject a mock transport here


def sample_url(url: str, timeout: float = 5.0) -> dict:
    """Blocking; call from a worker thread. Never raises."""
    base = url.rstrip("/")
    try:
        with httpx.Client(timeout=timeout, transport=transport_override) as c:
            version = c.get(f"{base}/api/version").raise_for_status().json().get("version")
            tags = c.get(f"{base}/api/tags").raise_for_status().json().get("models", [])
            ps = c.get(f"{base}/api/ps").raise_for_status().json().get("models", [])
    except (httpx.HTTPError, ValueError) as e:
        return {"reachable": False, "error": type(e).__name__}
    return {
        "reachable": True, "version": version,
        "tags": {m["name"]: m.get("digest", "")[:12] for m in tags},
        "resident": {m["name"]: {"size": m.get("size"), "context_length": m.get("context_length"),
                                 "expires_at": m.get("expires_at")} for m in ps},
    }


def _nodes(db):
    return [(n.id, n.name, n.runtime_url) for n in db.scalars(select(InferenceNode).where(
        InferenceNode.enabled.is_(True), InferenceNode.runtime_url.is_not(None)))]


def _store(db, node_id, state: dict) -> None:
    n = db.get(InferenceNode, node_id)
    n.state, n.sampled_at = state, utcnow()
    for d in db.scalars(select(ModelDeployment).where(ModelDeployment.node_id == node_id)):
        if not d.runtime_model:
            continue
        if not state.get("reachable"):
            healthy, detail = False, f"node unreachable ({state.get('error', 'no response')})"
        elif d.runtime_model not in state["tags"]:
            healthy, detail = False, f"model '{d.runtime_model}' is not installed on the node"
        else:
            healthy, detail = True, "ok"
        d.health_status = "healthy" if healthy else "unhealthy"
        d.configuration = {**(d.configuration or {}), "health": {
            "checked_at": utcnow().isoformat(), "detail": detail, "source": "node sampler"}}
    db.commit()


async def sample_node(node_id, url: str) -> dict:
    state = await anyio.to_thread.run_sync(sample_url, url)
    await dbcall(_store, node_id, state)
    return state


async def sample_all() -> list[dict]:
    out = []
    for node_id, name, url in await dbcall(_nodes):
        state = await sample_node(node_id, url)
        out.append({"node": name, **{k: state.get(k) for k in ("reachable", "version", "error")}})
    return out
