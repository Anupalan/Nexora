"""Workload classes: configuration and assignment (roadmap §6.2). Super admin only."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import workload
from app.audit import audit
from app.deps import ApiError, require_super_admin
from app.inference import dbcall
from app.models import ApiKey, ModelDeployment, PlatformSetting, Project, User

router = APIRouter(prefix="/admin", tags=["workload classes"])


class ClassIn(BaseModel):
    reserved_pct: int = Field(ge=0, le=100)
    priority: int = Field(default=5, ge=1, le=99)
    critical: bool = False  # served before every other class at a free slot (bounded by critical_burst)


class ConfigIn(BaseModel):
    classes: dict[str, ClassIn]
    borrow_idle_after_s: int = Field(default=60, ge=0, le=86_400)
    critical_burst: int = Field(default=4, ge=1, le=1000)


class AssignIn(BaseModel):
    workload_class: str | None = Field(default=None, max_length=32)  # null = fall back to the project/default


def _view(db, cfg: workload.WorkloadConfig) -> dict:
    deps = []
    for d in db.scalars(select(ModelDeployment).order_by(ModelDeployment.deployment_name)):
        n = int((d.configuration or {}).get("max_active_requests") or 2)
        deps.append({"deployment": d.deployment_name, "max_active": n,
                     "reserved_slots": workload.reserved_slots(n, cfg)})
    return {**cfg.as_dict(), "deployments": deps}


@router.get("/workload-classes")
async def get_classes(_: User = Depends(require_super_admin)):
    return await dbcall(lambda db: _view(db, workload.load_config(db)))


@router.put("/workload-classes")
async def put_classes(body: ConfigIn, actor: User = Depends(require_super_admin)):
    def q(db):
        current = workload.load_config(db)
        try:
            cfg = workload.validate({k: v.model_dump() for k, v in body.classes.items()}, body.borrow_idle_after_s,
                                 body.critical_burst)
        except ValueError as e:
            raise ApiError(422, "VALIDATION_ERROR", str(e))
        removed = sorted(set(current.classes) - set(cfg.classes))
        if removed:  # waiting requests of a removed class would be stranded; shrink it to 0% instead
            raise ApiError(422, "CLASS_IN_USE", f"Classes cannot be removed ({', '.join(removed)}); "
                                                "set their reserved percentage to 0 instead.")
        row = db.get(PlatformSetting, workload.SETTING_KEY) or PlatformSetting(key=workload.SETTING_KEY, value={})
        row.value, row.updated_by = cfg.as_dict(), actor.id
        db.add(row)
        audit(db, actor, "workload.classes", "workload_classes", workload.SETTING_KEY,
              classes={k: v["reserved_pct"] for k, v in cfg.classes.items()},
              borrow_idle_after_s=cfg.borrow_idle_after_s, critical_burst=cfg.critical_burst,
              critical=sorted(k for k, v in cfg.classes.items() if v.get("critical")))
        db.commit()
        return _view(db, cfg)
    return await dbcall(q)


def _assign(db, model, ident: uuid.UUID, body: AssignIn, actor: User, label: str) -> dict:
    cfg = workload.load_config(db)
    if body.workload_class is not None and body.workload_class not in cfg.classes:
        raise ApiError(422, "UNKNOWN_CLASS", f"Unknown class. Defined classes: {', '.join(sorted(cfg.classes))}.")
    row = db.get(model, ident)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"{label.capitalize()} not found.")
    row.workload_class = body.workload_class
    audit(db, actor, f"{label}.workload_class", label, ident, workload_class=body.workload_class)
    db.commit()
    return {"id": str(ident), "workload_class": row.workload_class}


@router.patch("/projects/{project_id}/workload-class")
async def set_project_class(project_id: uuid.UUID, body: AssignIn, actor: User = Depends(require_super_admin)):
    return await dbcall(_assign, Project, project_id, body, actor, "project")


@router.patch("/keys/{key_id}/workload-class")
async def set_key_class(key_id: uuid.UUID, body: AssignIn, actor: User = Depends(require_super_admin)):
    return await dbcall(_assign, ApiKey, key_id, body, actor, "key")
