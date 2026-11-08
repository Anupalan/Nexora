import hmac
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DBSession

from app import auditchain, metrics, ops, retention
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.deps import ApiError, require_super_admin
from app.inference import dbcall, get_scheduler
from app.litellm import LiteLLMClient, get_litellm
from app.models import Alert, AuditLog, Model, ModelDeployment, User, as_utc
from app.scheduler import Scheduler
from app.schemas import AuditOut

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/audit-logs", response_model=list[AuditOut])
def audit_logs(action: str | None = None, actor_id: str | None = None, entity_type: str | None = None,
               entity_id: str | None = None, q: str | None = Query(None, max_length=100),
               date_from: datetime | None = Query(None, alias="from"),
               date_to: datetime | None = Query(None, alias="to"),
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               _: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    """Filter by exact action/entity/actor, a date range, or free text `q` (matches action or entity id)."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= as_utc(date_from))
    if date_to:
        stmt = stmt.where(AuditLog.created_at <= as_utc(date_to))
    if q:
        like = f"%{q.replace('%', '').replace('_', ' ')}%"
        stmt = stmt.where(or_(AuditLog.action.ilike(like), AuditLog.entity_id.ilike(like)))
    return [AuditOut(id=a.id, actor_id=a.actor_id, action=a.action, entity_type=a.entity_type,
                     entity_id=a.entity_id, metadata=a.meta, created_at=a.created_at,
                     seq=a.seq, hash=a.hash)
            for a in db.scalars(stmt)]


@router.get("/audit-logs/verify")
def verify_audit_chain(anchor_seq: int | None = Query(None, ge=1), anchor_hash: str | None = Query(None, max_length=64),
                       _: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    """Check the audit log's hash chain end to end. To also catch deletion of the newest entries, pass an
    earlier head (`seq` and `hash`, as returned here) that you stored outside the database."""
    auditchain.seal(db)
    return auditchain.verify(db, anchor_seq, anchor_hash)


@router.get("/retention")
def get_retention(_: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    from app.models import PlatformSetting

    status = db.get(PlatformSetting, retention.STATUS_KEY)
    return {"settings": retention.load(db), "last_maintenance": status.value if status else None}


@router.put("/retention")
def put_retention(body: dict, actor: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    try:
        values = retention.save(db, body, actor.id)
    except (ValueError, TypeError) as e:
        raise ApiError(422, "VALIDATION_ERROR", str(e))
    audit(db, actor, "retention.update", "retention", "retention", **values)
    db.commit()
    return {"settings": values}


@router.post("/maintenance/run")
def run_maintenance(actor: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    """Purge detail rows past retention and verify the audit chain now (it also runs daily)."""
    result = retention.maintenance(db, force=True)
    audit(db, actor, "maintenance.run", "retention", "retention", purged=str(result["purged"]))
    db.commit()
    return result


@router.get("/dashboard")
async def dashboard(_: User = Depends(require_super_admin), sched: Scheduler = Depends(get_scheduler)):
    return await ops.dashboard(sched)


@router.get("/inference")
async def inference_health(_: User = Depends(require_super_admin), sched: Scheduler = Depends(get_scheduler)):
    """Per-deployment health (from the last check), queue and concurrency."""
    inp = await ops.gather_inputs(sched)

    def q(db):
        return {str(d.id): (d.configuration or {}).get("health") for d in db.scalars(select(ModelDeployment))}

    health = await dbcall(q)
    return {"deployments": [{"deployment_id": d.id, "model": d.model, "deployment": d.deployment, "node": d.node,
                             "status": d.health, "queued": d.depth,
                             "active": d.active, "max_active": d.max_active, "max_queue": d.max_queue,
                             "last_check": health.get(d.id)} for d in inp.deployments]}


@router.post("/inference/check")
async def run_health_check(actor: User = Depends(require_super_admin),
                           sched: Scheduler = Depends(get_scheduler),
                           litellm: LiteLLMClient = Depends(get_litellm)):
    """Probe now (also runs on a schedule) and re-evaluate alerts."""
    return await ops.ops_cycle(sched, litellm)


@router.get("/alerts")
def list_alerts(status: str | None = Query(None, pattern="^(open|resolved)$"),
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                _: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    stmt = select(Alert).order_by(Alert.created_at.desc(), Alert.id).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(Alert.status == status)
    return [{"id": str(a.id), "type": a.alert_type, "severity": a.severity, "message": a.message,
             "status": a.status, "details": a.details, "created_at": a.created_at,
             "resolved_at": a.resolved_at, "auto_resolved": a.status == "resolved" and a.resolved_by is None}
            for a in db.scalars(stmt)]


@router.post("/alerts/evaluate")
async def evaluate_alerts(_: User = Depends(require_super_admin), sched: Scheduler = Depends(get_scheduler)):
    return await ops.run_alert_cycle(sched)


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: uuid.UUID, actor: User = Depends(require_super_admin),
                  db: DBSession = Depends(get_db)):
    """Manually dismiss an alert. If the condition still holds it is re-opened on the next evaluation."""
    a = db.get(Alert, alert_id)
    if a is None:
        raise ApiError(404, "NOT_FOUND", "Alert not found.")
    if a.status == "open":
        from app.models import utcnow

        a.status, a.resolved_at, a.resolved_by = "resolved", utcnow(), actor.id
        audit(db, actor, "alert.resolve", "alert", a.id, alert_type=a.alert_type)
        db.commit()
    return {"id": str(a.id), "status": a.status}


@router.get("/metrics-token-configured")
def metrics_configured(_: User = Depends(require_super_admin)):
    return {"configured": bool(get_settings().metrics_token)}


_ = Model  # models referenced for clarity of the admin surface


def metrics_endpoint(router_: APIRouter):
    """Registered at /metrics (not under /api/v1): token-protected, disabled until a token is set."""

    @router_.get("/metrics", include_in_schema=False)
    async def prometheus(authorization: str | None = Header(None),
                         sched: Scheduler = Depends(get_scheduler)):
        token = get_settings().metrics_token
        if not token:
            raise ApiError(404, "NOT_FOUND", "Not found.")
        sent = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(sent, token):
            raise ApiError(401, "UNAUTHENTICATED", "Invalid metrics token.")
        return Response(await metrics.render(sched), media_type="text/plain; version=0.0.4")


metrics_router = APIRouter()
metrics_endpoint(metrics_router)
