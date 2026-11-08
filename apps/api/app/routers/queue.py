import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from app import inference
from app.authz import admin_team_ids, is_super
from app.deps import ApiError, current_user, require_super_admin
from app.inference import dbcall, get_scheduler
from app.models import InferenceRequest, Model, ModelDeployment, User
from app.scheduler import Scheduler

router = APIRouter(tags=["queue"])


class EstimateIn(BaseModel):
    model: str


def _status(db, r: InferenceRequest) -> dict:
    return {"id": str(r.id), "status": r.status, "model_id": str(r.model_id),
            "deployment_id": str(r.deployment_id), "queue_sequence": r.queue_sequence,
            "created_at": r.created_at, "started_at": r.started_at, "completed_at": r.completed_at,
            "error_code": r.error_code, "queue_time_ms": r.queue_time_ms}


async def _visible(actor: User, rid: uuid.UUID) -> dict:
    admin_teams = [str(t) for t in await dbcall(admin_team_ids, actor)] if not is_super(actor) else []

    def q(db):
        r = db.get(InferenceRequest, rid)
        if r is None:
            return None
        ok = r.user_id == actor.id or is_super(actor) or (r.team_id and str(r.team_id) in admin_teams)
        return (_status(db, r), r.deployment_id) if ok else None

    res = await dbcall(q)
    if res is None:
        raise ApiError(404, "NOT_FOUND", "Request not found.")
    return {"info": res[0], "dep": res[1]}


@router.get("/queue/status/{request_id}")
async def queue_status(request_id: uuid.UUID, actor: User = Depends(current_user),
                       sched: Scheduler = Depends(get_scheduler)):
    v = await _visible(actor, request_id)
    info = v["info"]
    if info["status"] == "queued":
        pos = await sched.position(v["dep"], request_id)
        info["position"] = pos
        info["ahead"] = None if pos is None else pos - 1
    return info


@router.post("/queue/{request_id}/cancel", status_code=202)
async def cancel(request_id: uuid.UUID, actor: User = Depends(current_user),
                 sched: Scheduler = Depends(get_scheduler)):
    v = await _visible(actor, request_id)
    dep, status = v["dep"], v["info"]["status"]
    if status == "queued":
        if await sched.remove_queued(dep, request_id):
            await inference.settle(sched, request_id, dep, status="cancelled", consumed=False,
                                   code="cancelled", msg="Cancelled by user.")
        return {"id": str(request_id), "status": "cancelled"}
    if status == "running":
        await sched.request_cancel(request_id)  # the running worker aborts upstream and settles
        return {"id": str(request_id), "status": "cancelling"}
    return {"id": str(request_id), "status": status}


@router.get("/queue/me")
async def my_queue(actor: User = Depends(current_user)):
    def q(db):
        rows = db.scalars(select(InferenceRequest).where(
            InferenceRequest.user_id == actor.id, InferenceRequest.status.in_(("queued", "running")))
            .order_by(InferenceRequest.created_at))
        return [_status(db, r) for r in rows]
    return await dbcall(q)


@router.post("/queue/estimate")
async def estimate(body: EstimateIn, _: User = Depends(current_user),
                   sched: Scheduler = Depends(get_scheduler)):
    from app.config import get_settings

    def q(db):
        m = db.scalar(select(Model).where(Model.model_alias == body.model, Model.enabled.is_(True)))
        if m is None:
            return None
        dep = db.scalar(select(ModelDeployment).where(ModelDeployment.model_id == m.id))
        recent = [d for (d,) in db.execute(select(InferenceRequest.duration_ms).where(
            InferenceRequest.model_id == m.id, InferenceRequest.status == "completed",
            InferenceRequest.duration_ms.is_not(None)).order_by(InferenceRequest.completed_at.desc())
            .limit(50))]
        return dep, recent

    res = await dbcall(q)
    if res is None or res[0] is None:
        raise ApiError(404, "NOT_FOUND", "Model not found.")
    dep, recent = res
    max_active = int((dep.configuration or {}).get("max_active_requests")
                     or get_settings().default_max_active_requests)
    depth, active = await sched.depth(dep.id), await sched.active_count(dep.id)
    est = None
    if len(recent) >= 5:  # only claim an estimate when we have real samples
        est = int((depth + 1) / max_active * (sum(recent) / len(recent)))
    return {"queue_depth": depth, "active": active, "max_active": max_active, "estimated_wait_ms": est}


@router.get("/admin/queue")
async def admin_queue(_: User = Depends(require_super_admin), sched: Scheduler = Depends(get_scheduler)):
    def q(db):
        return [(d.id, m.model_alias, d.deployment_name, d.configuration or {}) for d, m in db.execute(
            select(ModelDeployment, Model).join(Model, Model.id == ModelDeployment.model_id))]

    out = []
    for dep_id, alias, name, cfg in await dbcall(q):
        out.append({"deployment_id": str(dep_id), "model": alias, "deployment": name, "config": cfg,
                    "queued": await sched.depth(dep_id), "active": await sched.active_count(dep_id)})
    recent = await dbcall(lambda db: db.scalar(select(func.count()).select_from(InferenceRequest)
                                               .where(InferenceRequest.status.in_(("queued", "running")))))
    return {"deployments": out, "in_flight_records": recent}
