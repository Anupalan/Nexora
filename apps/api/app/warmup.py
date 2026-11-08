"""Warm-up and keep-alive manager (roadmap §6.5).

Cold-loading a model costs seconds (about 5-6 s for an 8B model, more for a 30B one). A deployment with
`keep_warm` set is kept resident on its node: this module loads it when it is missing and refreshes Ollama's
keep-alive timer before it runs out, because any client can shorten the timer with its own `keep_alive`.

Safety rules, because these nodes also serve production traffic:
  * opt-in per deployment (`keep_warm`), never automatic;
  * a model that is NOT resident is only loaded when the node has a `memory_budget_gb` and the resident
    models plus this one fit inside it, so a preload can never push a production model out. Refreshing a
    model that is already resident needs no budget (it changes nothing but the timer);
  * the load is an empty-prompt /api/generate (loads the model, generates nothing);
  * after a failure it backs off, instead of hammering a struggling node every cycle.
"""

import logging
from datetime import datetime, timedelta, timezone

import anyio
import httpx
from sqlalchemy import select

from app import sampler
from app.inference import dbcall
from app.models import InferenceNode, ModelDeployment, utcnow

log = logging.getLogger("nexora")

DEFAULT_KEEP_ALIVE = "24h"
REFRESH_BELOW_S = 3600  # refresh when less than this remains on the timer
BACKOFF_S = 300
LOAD_TIMEOUT_S = 180.0
GB = 1_000_000_000


def keep_alive_seconds(value: str) -> float | None:
    """'-1' means forever (None); otherwise seconds from forms like 30m, 24h, 90s."""
    if value.strip() == "-1":
        return None
    unit = {"s": 1, "m": 60, "h": 3600}[value[-1]]
    return int(value[:-1]) * unit


def parse_expiry(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def plan(dep: dict, node: dict, now: datetime) -> tuple[str, str]:
    """Pure decision for one deployment: ('skip'|'load'|'refresh', reason)."""
    state = node["state"]
    if not state.get("reachable"):
        return "skip", "node unreachable"
    rt = dep["runtime_model"]
    if rt not in (state.get("tags") or {}):
        return "skip", f"model '{rt}' is not installed on the node"
    resident = state.get("resident") or {}
    if rt in resident:
        exp = parse_expiry(resident[rt].get("expires_at"))
        if exp is not None and exp - now >= timedelta(seconds=REFRESH_BELOW_S):
            return "skip", "already resident with enough keep-alive left"
        return "refresh", "keep-alive is running low" if exp else "keep-alive unknown"
    budget = node["memory_budget_gb"]
    if not budget:
        return "skip", "not resident, and the node has no memory budget set, so it will not be preloaded"
    used = sum(v.get("size") or 0 for v in resident.values())
    need = (state.get("sizes") or {}).get(rt, 0)
    if used + need > budget * GB:
        return "skip", (f"loading would exceed the node's {budget} GB memory budget "
                        f"({used / GB:.1f} GB loaded + {need / GB:.1f} GB needed)")
    return "load", "model is not resident"


def _send(url: str, runtime_model: str, keep_alive: str) -> tuple[bool, str]:
    """Blocking. An empty prompt loads the model and sets the timer without generating anything."""
    try:
        with httpx.Client(timeout=LOAD_TIMEOUT_S, transport=sampler.transport_override) as c:
            r = c.post(f"{url.rstrip('/')}/api/generate",
                       json={"model": runtime_model, "prompt": "", "stream": False, "keep_alive": keep_alive})
            r.raise_for_status()
        return True, "ok"
    except (httpx.HTTPError, ValueError) as e:
        return False, type(e).__name__


def _snapshot(db) -> list[tuple[dict, dict]]:
    rows = db.execute(select(ModelDeployment, InferenceNode).join(
        InferenceNode, InferenceNode.id == ModelDeployment.node_id).where(
        ModelDeployment.enabled.is_(True), ModelDeployment.draining.is_(False),
        InferenceNode.enabled.is_(True), InferenceNode.runtime_url.is_not(None)))
    out = []
    for d, n in rows:
        cfg = d.configuration or {}
        if not cfg.get("keep_warm") or not d.runtime_model:
            continue
        out.append(({"id": d.id, "name": d.deployment_name, "runtime_model": d.runtime_model,
                     "keep_alive": cfg.get("keep_alive") or DEFAULT_KEEP_ALIVE, "warm": cfg.get("warm") or {}},
                    {"id": n.id, "name": n.name, "url": n.runtime_url, "state": dict(n.state or {}),
                     "memory_budget_gb": n.memory_budget_gb}))
    return out


def _record(db, dep_id, result: dict) -> None:
    d = db.get(ModelDeployment, dep_id)
    prev = (d.configuration or {}).get("warm") or {}
    failures = 0 if result["ok"] else int(prev.get("failures", 0)) + 1
    d.configuration = {**(d.configuration or {}), "warm": {**result, "failures": failures,
                                                           "at": utcnow().isoformat()}}
    db.commit()


async def warm_one(dep: dict, node: dict, *, force: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    action, reason = plan(dep, node, now)
    out = {"deployment": dep["name"], "node": node["name"], "action": action, "reason": reason, "ok": True}
    if action == "skip":
        return out
    last = dep["warm"]
    if not force and not last.get("ok", True) and last.get("at"):
        age = (now - parse_expiry(last["at"])).total_seconds()
        if age < BACKOFF_S:
            return {**out, "action": "skip", "reason": f"backing off after a failure ({int(BACKOFF_S - age)} s left)"}
    ok, detail = await anyio.to_thread.run_sync(_send, node["url"], dep["runtime_model"], dep["keep_alive"])
    out.update(ok=ok, detail=detail)
    await dbcall(_record, dep["id"], {"action": action, "ok": ok, "detail": detail, "reason": reason})
    if ok:
        await sampler.sample_node(node["id"], node["url"])  # residency changed: refresh what the router sees
    else:
        log.warning("warm-up %s of %s on %s failed: %s", action, dep["name"], node["name"], detail)
    return out


async def warm_cycle() -> list[dict]:
    """One pass over every keep_warm deployment. Sequential, and each step re-reads node state, so a node
    never loads two models at once and the memory-budget check always sees the previous load."""
    results = []
    for dep_id in [d["id"] for d, _ in await dbcall(_snapshot)]:
        for dep, node in await dbcall(_snapshot):
            if dep["id"] == dep_id:
                results.append(await warm_one(dep, node))
    return [r for r in results if not r["reason"].startswith("already resident")]


async def warm_now(deployment_id) -> dict:
    """Admin action: run the plan for one deployment immediately (ignores the failure back-off)."""
    for dep, node in await dbcall(_snapshot):
        if dep["id"] == deployment_id:
            return await warm_one(dep, node, force=True)
    return {"action": "skip", "ok": False, "reason": "deployment is not keep_warm, enabled and on a node"}
