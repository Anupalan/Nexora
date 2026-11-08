"""Routing administration (roadmap §6, §19): nodes, deployments, policies, freeze and explanations."""

import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import router as routing
from app import sampler, warmup
from app.audit import audit
from app.authz import admin_team_ids, is_super
from app.deps import ApiError, current_user, require_super_admin
from app.inference import dbcall, get_scheduler
from app.litellm import LiteLLMClient, get_litellm
from app.models import (
    ApiKey,
    InferenceNode,
    InferenceRequest,
    Model,
    ModelDeployment,
    PlatformSetting,
    RoutingDecision,
    RoutingPolicy,
    User,
    utcnow,
)
from app.scheduler import Scheduler

router = APIRouter(tags=["routing"])


def _clean_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ApiError(422, "VALIDATION_ERROR", "runtime_url must be http(s)://host[:port] with no credentials.")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    runtime_url: str | None = Field(default=None, max_length=300)
    max_concurrent: int | None = Field(default=None, ge=1, le=1024)
    memory_budget_gb: int | None = Field(default=None, ge=1, le=4096)


class NodeUpdate(BaseModel):
    runtime_url: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=1024)
    memory_budget_gb: int | None = Field(default=None, ge=1, le=4096)


class DeploymentCreate(BaseModel):
    model_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._@:-]+$")
    node_id: uuid.UUID | None = None
    litellm_model: str | None = Field(default=None, max_length=200)
    runtime_model: str | None = Field(default=None, max_length=200)
    weight: int = Field(default=100, ge=0, le=10000)
    context_limit: int | None = Field(default=None, ge=1)
    max_active_requests: int | None = Field(default=None, ge=1, le=256)
    max_queue_length: int | None = Field(default=None, ge=1, le=100000)
    load_seconds: float | None = Field(default=None, ge=0, le=3600)
    keep_warm: bool | None = None
    keep_alive: str | None = Field(default=None, pattern=r"^(-1|[1-9][0-9]{0,5}[smh])$")


class DeploymentUpdate(BaseModel):
    node_id: uuid.UUID | None = None
    litellm_model: str | None = Field(default=None, max_length=200)
    runtime_model: str | None = Field(default=None, max_length=200)
    weight: int | None = Field(default=None, ge=0, le=10000)
    enabled: bool | None = None
    draining: bool | None = None
    context_limit: int | None = Field(default=None, ge=1)
    max_active_requests: int | None = Field(default=None, ge=1, le=256)
    max_queue_length: int | None = Field(default=None, ge=1, le=100000)
    load_seconds: float | None = Field(default=None, ge=0, le=3600)
    keep_warm: bool | None = None
    keep_alive: str | None = Field(default=None, pattern=r"^(-1|[1-9][0-9]{0,5}[smh])$")


class PolicyIn(BaseModel):
    strategy: str = Field(pattern="^(fixed|least_loaded|weighted)$")
    residency_aware: bool = True
    exclude_mismatched: bool = True
    reference_digest: str | None = Field(default=None, max_length=64)
    reference_deployment_id: uuid.UUID | None = None  # use this deployment's model build as the reference
    default_service_s: float = Field(default=routing.DEFAULT_SERVICE_S, gt=0, le=3600)


# ---------------------------------------------------------------- nodes

def _node_out(n: InferenceNode, deployments: int = 0) -> dict:
    st = n.state or {}
    return {"id": str(n.id), "name": n.name, "runtime_url": n.runtime_url, "enabled": n.enabled,
            "max_concurrent": n.max_concurrent, "memory_budget_gb": n.memory_budget_gb,
            "deployments": deployments, "sampled_at": n.sampled_at,
            "reachable": st.get("reachable"), "version": st.get("version"), "error": st.get("error"),
            "resident": [{"model": k, **v} for k, v in (st.get("resident") or {}).items()],
            "installed": st.get("tags") or {}}


@router.get("/admin/nodes")
async def list_nodes(_: User = Depends(require_super_admin)):
    def q(db):
        counts = {}
        for (nid,) in db.execute(select(ModelDeployment.node_id).where(ModelDeployment.node_id.is_not(None))):
            counts[nid] = counts.get(nid, 0) + 1
        nodes = db.scalars(select(InferenceNode).order_by(InferenceNode.name))
        return [_node_out(n, counts.get(n.id, 0)) for n in nodes]
    return await dbcall(q)


@router.post("/admin/nodes", status_code=201)
async def create_node(body: NodeCreate, actor: User = Depends(require_super_admin)):
    url = _clean_url(body.runtime_url) if body.runtime_url else None

    def q(db):
        if db.scalar(select(InferenceNode).where(InferenceNode.name == body.name)):
            raise ApiError(409, "NODE_EXISTS", "A node with this name already exists.")
        n = InferenceNode(name=body.name, runtime_url=url, max_concurrent=body.max_concurrent,
                          memory_budget_gb=body.memory_budget_gb)
        db.add(n)
        db.flush()
        audit(db, actor, "node.create", "node", n.id, name=n.name)
        db.commit()
        return _node_out(n)
    return await dbcall(q)


@router.patch("/admin/nodes/{node_id}")
async def update_node(node_id: uuid.UUID, body: NodeUpdate, actor: User = Depends(require_super_admin)):
    changes = body.model_dump(exclude_unset=True)
    if changes.get("runtime_url"):
        changes["runtime_url"] = _clean_url(changes["runtime_url"])

    def q(db):
        n = db.get(InferenceNode, node_id)
        if n is None:
            raise ApiError(404, "NOT_FOUND", "Node not found.")
        for k, v in changes.items():
            setattr(n, k, v)
        audit(db, actor, "node.update", "node", n.id, fields=sorted(changes))
        db.commit()
        return _node_out(n)
    return await dbcall(q)


@router.post("/admin/deployments/{deployment_id}/warm")
async def warm_deployment(deployment_id: uuid.UUID, actor: User = Depends(require_super_admin)):
    """Load or refresh this deployment's model now. Needs keep_warm on; a model that is not resident is only
    loaded when its node has a memory budget the model fits in (so production models are never pushed out)."""
    res = await warmup.warm_now(deployment_id)
    await dbcall(lambda db: (audit(db, actor, "deployment.warm", "deployment", deployment_id,
                                   result=res["action"], ok=res["ok"]), db.commit()))
    return res


@router.post("/admin/nodes/{node_id}/sample")
async def sample_node_now(node_id: uuid.UUID, _: User = Depends(require_super_admin)):
    node = await dbcall(lambda db: (lambda n: (n.id, n.runtime_url) if n else None)(db.get(InferenceNode, node_id)))
    if node is None or not node[1]:
        raise ApiError(404, "NOT_FOUND", "Node not found or it has no runtime_url to sample.")
    await sampler.sample_node(*node)
    return await dbcall(lambda db: _node_out(db.get(InferenceNode, node_id)))


# ---------------------------------------------------------------- deployments

async def _deployment_rows(sched: Scheduler, model_id: uuid.UUID | None = None) -> list[dict]:
    def q(db):
        rows = list(db.scalars(select(Model.id).order_by(Model.model_alias))) if model_id is None else [model_id]
        return [routing.load_candidates(db, m) for m in rows]

    out = []
    for loaded in await dbcall(q):
        await routing.fill_load(sched, loaded)
        for c in loaded.candidates:
            out.append({"id": c.id, "model_id": str(loaded.model_id), "model": c.model_alias, "name": c.name,
                        "node": c.node, "node_id": c.node_id, "litellm_model": c.litellm_model,
                        "runtime_model": c.runtime_model, "weight": c.weight, "enabled": c.enabled,
                        "draining": c.draining, "health": c.health, "resident": c.resident,
                        "digest": c.digest, "context_limit": c.context_limit, "max_active": c.max_active,
                        "max_queue": c.max_queue, "load_seconds": c.load_seconds, "queued": c.queued,
                        "keep_warm": c.keep_warm, "warm": c.warm,
                        "active": c.active, "strategy": loaded.policy.strategy, "frozen": loaded.policy.frozen,
                        "classes": {n: {"reserved": c.reserved.get(n, 0), "active": c.cls_active.get(n, 0),
                                        "queued": c.cls_queued.get(n, 0)} for n in loaded.workload.classes}})
    return out


@router.get("/admin/deployments")
async def list_deployments(model_id: uuid.UUID | None = None, _: User = Depends(require_super_admin),
                           sched: Scheduler = Depends(get_scheduler)):
    return await _deployment_rows(sched, model_id)


def _sync_key_models(db, model: Model, litellm: LiteLLMClient) -> dict:
    """LiteLLM checks a key's model list against the name in each request, so every deployment name an
    alias can route to must be on the keys that may use the alias."""
    names = {model.litellm_model} | {d.litellm_model for d in db.scalars(
        select(ModelDeployment).where(ModelDeployment.model_id == model.id)) if d.litellm_model}
    synced = failed = 0
    for k in db.scalars(select(ApiKey).where(ApiKey.status == "active")):
        if model.model_alias not in (k.allowed_models or []) or not k.litellm_key_id:
            continue
        others = {m.litellm_model for m in db.scalars(select(Model).where(
            Model.model_alias.in_([a for a in k.allowed_models if a != model.model_alias])))}
        extra = {d.litellm_model for d in db.scalars(select(ModelDeployment).join(
            Model, Model.id == ModelDeployment.model_id).where(
            Model.model_alias.in_([a for a in k.allowed_models if a != model.model_alias]))) if d.litellm_model}
        try:
            litellm.update_key(k.litellm_key_id, models=sorted(names | others | extra))
            synced += 1
        except ApiError:
            failed += 1
    return {"keys_synced": synced, "keys_failed": failed}


@router.post("/admin/deployments", status_code=201)
async def create_deployment(body: DeploymentCreate, actor: User = Depends(require_super_admin),
                            litellm: LiteLLMClient = Depends(get_litellm)):
    def q(db):
        m = db.get(Model, body.model_id)
        if m is None:
            raise ApiError(404, "NOT_FOUND", "Model not found.")
        if body.node_id and db.get(InferenceNode, body.node_id) is None:
            raise ApiError(404, "NOT_FOUND", "Node not found.")
        if db.scalar(select(ModelDeployment).where(ModelDeployment.model_id == m.id,
                                                   ModelDeployment.deployment_name == body.name)):
            raise ApiError(409, "DEPLOYMENT_EXISTS", "This model already has a deployment with that name.")
        cfg = {k: v for k, v in (("max_active_requests", body.max_active_requests),
                                 ("max_queue_length", body.max_queue_length),
                                 ("load_seconds", body.load_seconds), ("keep_warm", body.keep_warm),
                                 ("keep_alive", body.keep_alive)) if v is not None}
        d = ModelDeployment(model_id=m.id, deployment_name=body.name, endpoint_reference="litellm",
                            node_id=body.node_id, litellm_model=body.litellm_model,
                            runtime_model=body.runtime_model, weight=body.weight,
                            context_limit=body.context_limit, configuration=cfg)
        db.add(d)
        db.flush()
        audit(db, actor, "deployment.create", "deployment", d.id, model=m.model_alias, name=d.deployment_name)
        db.commit()
        return {"id": str(d.id), **_sync_key_models(db, m, litellm)}
    return await dbcall(q)


@router.patch("/admin/deployments/{deployment_id}")
async def update_deployment(deployment_id: uuid.UUID, body: DeploymentUpdate,
                            actor: User = Depends(require_super_admin),
                            litellm: LiteLLMClient = Depends(get_litellm)):
    changes = body.model_dump(exclude_unset=True)
    cfg_keys = {k: changes.pop(k) for k in ("max_active_requests", "max_queue_length", "load_seconds",
                                            "keep_warm", "keep_alive")
                if k in changes and changes[k] is not None}

    def q(db):
        d = db.get(ModelDeployment, deployment_id)
        if d is None:
            raise ApiError(404, "NOT_FOUND", "Deployment not found.")
        if changes.get("node_id") and db.get(InferenceNode, changes["node_id"]) is None:
            raise ApiError(404, "NOT_FOUND", "Node not found.")
        for k, v in changes.items():
            setattr(d, k, v)
        if cfg_keys:
            d.configuration = {**(d.configuration or {}), **cfg_keys}
        audit(db, actor, "deployment.update", "deployment", d.id, **{**{k: str(v) for k, v in changes.items()},
                                                                      **cfg_keys})
        db.commit()
        out = {"id": str(d.id), "configuration": d.configuration}
        if "litellm_model" in changes:
            out.update(_sync_key_models(db, db.get(Model, d.model_id), litellm))
        return out
    return await dbcall(q)


# ---------------------------------------------------------------- policy and freeze

def _policy_out(p: RoutingPolicy) -> dict:
    return {"id": str(p.id), "model_id": str(p.model_id) if p.model_id else None, "version": p.version,
            "strategy": p.strategy, "configuration": p.configuration, "status": p.status,
            "created_at": p.created_at}


@router.get("/admin/routing/status")
async def routing_status(_: User = Depends(require_super_admin)):
    def q(db):
        frozen = bool((db.get(PlatformSetting, routing.FREEZE_KEY) or PlatformSetting(value={})).value.get("frozen"))
        pols = [_policy_out(p) for p in db.scalars(select(RoutingPolicy).where(RoutingPolicy.status == "active"))]
        return {"frozen": frozen, "policies": pols, "strategies": list(routing.STRATEGIES)}
    return await dbcall(q)


def _set_frozen(db, actor: User, frozen: bool) -> dict:
    row = db.get(PlatformSetting, routing.FREEZE_KEY) or PlatformSetting(key=routing.FREEZE_KEY, value={})
    row.value, row.updated_by = {"frozen": frozen, "at": utcnow().isoformat()}, actor.id
    db.add(row)
    audit(db, actor, "routing.freeze" if frozen else "routing.unfreeze", "routing", routing.FREEZE_KEY)
    db.commit()
    return {"frozen": frozen}


@router.post("/admin/routing/freeze")
async def freeze(actor: User = Depends(require_super_admin)):
    """Kill switch: every model is routed with the fixed strategy and residency awareness off."""
    return await dbcall(_set_frozen, actor, True)


@router.post("/admin/routing/unfreeze")
async def unfreeze(actor: User = Depends(require_super_admin)):
    return await dbcall(_set_frozen, actor, False)


@router.get("/admin/routing/policies")
async def list_policies(model_id: uuid.UUID | None = None, _: User = Depends(require_super_admin)):
    def q(db):
        stmt = select(RoutingPolicy).order_by(RoutingPolicy.model_id, RoutingPolicy.version.desc())
        if model_id:
            stmt = stmt.where(RoutingPolicy.model_id == model_id)
        return [_policy_out(p) for p in db.scalars(stmt)]
    return await dbcall(q)


@router.put("/admin/routing/policies/{target}")
async def put_policy(target: str, body: PolicyIn, actor: User = Depends(require_super_admin)):
    """`target` is a model id, or `default` for the policy used by models without their own."""
    model_id = None
    if target != "default":
        try:
            model_id = uuid.UUID(target)
        except ValueError:
            raise ApiError(422, "VALIDATION_ERROR", "target must be a model id or 'default'.")

    def q(db):
        if model_id and db.get(Model, model_id) is None:
            raise ApiError(404, "NOT_FOUND", "Model not found.")
        ref = body.reference_digest
        if body.reference_deployment_id:
            d = db.get(ModelDeployment, body.reference_deployment_id)
            n = db.get(InferenceNode, d.node_id) if d and d.node_id else None
            ref = ((n.state or {}).get("tags") or {}).get(d.runtime_model or "") if n else None
            if not ref:
                raise ApiError(409, "NO_DIGEST", "That deployment's model build is not known yet (sample its node).")
        prev = list(db.scalars(select(RoutingPolicy).where(RoutingPolicy.model_id == model_id,
                                                           RoutingPolicy.status == "active")))
        version = (max([p.version for p in db.scalars(select(RoutingPolicy).where(
            RoutingPolicy.model_id == model_id))] or [0])) + 1
        for p in prev:
            p.status = "retired"
        new = RoutingPolicy(model_id=model_id, version=version, strategy=body.strategy, created_by=actor.id,
                            configuration={"residency_aware": body.residency_aware,
                                           "exclude_mismatched": body.exclude_mismatched,
                                           "reference_digest": ref,
                                           "default_service_s": body.default_service_s})
        db.add(new)
        db.flush()
        audit(db, actor, "routing.policy", "routing_policy", new.id, model_id=model_id, version=version,
              strategy=body.strategy)
        db.commit()
        return _policy_out(new)
    return await dbcall(q)


# ---------------------------------------------------------------- decisions and explanations

def _decision_out(d: RoutingDecision) -> dict:
    return {"request_id": str(d.request_id), "attempt": d.attempt, "strategy": d.strategy,
            "policy_version": d.policy_version, "chosen_deployment_id": str(d.chosen_deployment_id)
            if d.chosen_deployment_id else None, "reason": d.reason, "candidates": d.candidates,
            "failover_from_deployment_id": str(d.failover_from_deployment_id)
            if d.failover_from_deployment_id else None, "created_at": d.created_at}


@router.get("/routing/explain/{request_id}")
async def explain(request_id: uuid.UUID, actor: User = Depends(current_user)):
    """Why a request ran where it did. Owner, their team's admin, or a super admin."""
    def q(db):
        r = db.get(InferenceRequest, request_id)
        ok = r is not None and (r.user_id == actor.id or is_super(actor)
                                or (r.team_id is not None and r.team_id in admin_team_ids(db, actor)))
        if not ok:
            raise ApiError(404, "NOT_FOUND", "Request not found.")
        rows = db.scalars(select(RoutingDecision).where(RoutingDecision.request_id == request_id)
                          .order_by(RoutingDecision.attempt, RoutingDecision.created_at))
        return {"request_id": str(request_id), "deployment_id": str(r.deployment_id), "status": r.status,
                "decisions": [_decision_out(d) for d in rows]}
    return await dbcall(q)


@router.get("/admin/routing/decisions")
async def decisions(deployment_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=200),
                    _: User = Depends(require_super_admin)):
    def q(db):
        stmt = select(RoutingDecision).order_by(RoutingDecision.created_at.desc()).limit(limit)
        if deployment_id:
            stmt = stmt.where(RoutingDecision.chosen_deployment_id == deployment_id)
        return [_decision_out(d) for d in db.scalars(stmt)]
    return await dbcall(q)
