import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.audit import audit
from app.authz import get_team_or_404
from app.db import get_db
from app.deps import ApiError, current_user, require_super_admin
from app.models import GLOBAL_SCOPE_ID, Project, Quota, Team, User
from app.schemas import QuotaCreate, QuotaLimits, QuotaOut

router = APIRouter(tags=["quotas"])
LIMIT_FIELDS = list(QuotaLimits.model_fields)


def _validate_scope(db: DBSession, scope_type: str, scope_id: str | None) -> str:
    if scope_type == "global":
        return GLOBAL_SCOPE_ID
    try:
        uuid.UUID(scope_id or "")
    except ValueError:
        raise ApiError(422, "VALIDATION_ERROR", "scope_id must be a UUID for this scope type.")
    model = {"team": Team, "user": User, "project": Project}.get(scope_type)
    if model is not None and db.get(model, uuid.UUID(scope_id)) is None:
        raise ApiError(404, "NOT_FOUND", f"{scope_type} not found.")
    return str(uuid.UUID(scope_id))


@router.get("/admin/quotas", response_model=list[QuotaOut])
def list_quotas(_: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    return list(db.scalars(select(Quota).order_by(Quota.scope_type, Quota.scope_id)))


@router.post("/admin/quotas", response_model=QuotaOut, status_code=201)
def create_quota(body: QuotaCreate, actor: User = Depends(require_super_admin),
                 db: DBSession = Depends(get_db)):
    sid = _validate_scope(db, body.scope_type, body.scope_id)
    if db.scalar(select(Quota).where(Quota.scope_type == body.scope_type, Quota.scope_id == sid)):
        raise ApiError(409, "QUOTA_EXISTS", "A quota already exists for this scope; update it instead.")
    q = Quota(scope_type=body.scope_type, scope_id=sid, **body.model_dump(include=set(LIMIT_FIELDS)))
    db.add(q)
    db.flush()
    audit(db, actor, "quota.create", "quota", q.id, scope_type=q.scope_type, scope_id=sid)
    db.commit()
    return q


@router.patch("/admin/quotas/{quota_id}", response_model=QuotaOut)
def update_quota(quota_id: uuid.UUID, body: QuotaLimits, actor: User = Depends(require_super_admin),
                 db: DBSession = Depends(get_db)):
    q = db.get(Quota, quota_id)
    if q is None:
        raise ApiError(404, "NOT_FOUND", "Quota not found.")
    changes = body.model_dump(exclude_unset=True)  # explicit null clears a limit
    for k, v in changes.items():
        setattr(q, k, v)
    audit(db, actor, "quota.update", "quota", q.id, fields=sorted(changes))
    db.commit()
    return q


def _mine(db: DBSession, scope_type: str, scope_id: str) -> Quota | None:
    return db.scalar(select(Quota).where(Quota.scope_type == scope_type, Quota.scope_id == scope_id))


@router.get("/quotas/me")
def my_quotas(actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    """Limits that apply to the caller (informational; enforcement is server-side)."""
    from app.models import TeamMember

    out = {"global": _mine(db, "global", GLOBAL_SCOPE_ID), "user": _mine(db, "user", str(actor.id))}
    teams = [t.team_id for t in db.scalars(select(TeamMember).where(TeamMember.user_id == actor.id))]
    out["teams"] = [q for t in teams if (q := _mine(db, "team", str(t)))]
    return {
        "global": QuotaOut.model_validate(out["global"]) if out["global"] else None,
        "user": QuotaOut.model_validate(out["user"]) if out["user"] else None,
        "teams": [QuotaOut.model_validate(q) for q in out["teams"]],
    }


@router.get("/quotas/team/{team_id}", response_model=QuotaOut | None)
def team_quota(team_id: uuid.UUID, actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    team = get_team_or_404(db, actor, team_id)
    q = _mine(db, "team", str(team.id))
    return QuotaOut.model_validate(q) if q else None
