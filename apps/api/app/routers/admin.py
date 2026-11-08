from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.db import get_db
from app.deps import require_super_admin
from app.models import AuditLog, User
from app.schemas import AuditOut

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/audit-logs", response_model=list[AuditOut])
def audit_logs(action: str | None = None, actor_id: str | None = None,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               _: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
    if action:
        q = q.where(AuditLog.action == action)
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    return [AuditOut(id=a.id, actor_id=a.actor_id, action=a.action, entity_type=a.entity_type,
                     entity_id=a.entity_id, metadata=a.meta, created_at=a.created_at)
            for a in db.scalars(q)]
