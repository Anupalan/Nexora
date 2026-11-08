import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.audit import audit
from app.authz import admin_team_ids, is_super
from app.db import get_db
from app.deps import ApiError, current_user, require_super_admin
from app.litellm import LiteLLMClient, get_litellm
from app.models import Role, TeamMember, User
from app.routers.auth import revoke_user_sessions, user_out
from app.routers.keys import revoke_user_keys
from app.schemas import UserCreate, UserOut, UserUpdate
from app.security import hash_password

router = APIRouter(prefix="/users", tags=["users"])


def _role(db: DBSession, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))
    if role is None:
        raise ApiError(500, "ROLE_MISSING", f"Role {name} is not seeded.")
    return role


def _shares_admin_team(db: DBSession, actor: User, target_id: uuid.UUID) -> bool:
    teams = admin_team_ids(db, actor)
    if not teams:
        return False
    return db.scalar(select(TeamMember.id).where(
        TeamMember.user_id == target_id, TeamMember.team_id.in_(teams)).limit(1)) is not None


@router.get("", response_model=list[UserOut])
def list_users(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    q = select(User).order_by(User.created_at, User.id).limit(limit).offset(offset)
    if not is_super(actor):
        teams = admin_team_ids(db, actor)
        if not teams:
            raise ApiError(403, "FORBIDDEN", "Admin access required.")
        member_ids = select(TeamMember.user_id).where(TeamMember.team_id.in_(teams))
        q = q.where(User.id.in_(member_ids))
    return [user_out(u) for u in db.scalars(q)]


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, actor: User = Depends(require_super_admin),
                db: DBSession = Depends(get_db)):
    user = User(name=body.name, email=body.email.lower(), role_id=_role(db, body.role).id,
                password_hash=hash_password(body.password), status="active")
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "EMAIL_EXISTS", "A user with this email already exists.")
    audit(db, actor, "user.create", "user", user.id, role=body.role)
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: uuid.UUID, actor: User = Depends(current_user),
             db: DBSession = Depends(get_db)):
    target = db.get(User, user_id)
    if target is None or not (is_super(actor) or actor.id == user_id
                              or _shares_admin_team(db, actor, user_id)):
        raise ApiError(404, "NOT_FOUND", "User not found.")
    return user_out(target)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: uuid.UUID, body: UserUpdate, actor: User = Depends(require_super_admin),
                db: DBSession = Depends(get_db)):
    target = db.get(User, user_id)
    if target is None:
        raise ApiError(404, "NOT_FOUND", "User not found.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "role" in changes:
        if target.id == actor.id and changes["role"] != target.role.name:
            raise ApiError(400, "SELF_ROLE_CHANGE", "You cannot change your own role.")
        target.role_id = _role(db, changes["role"]).id
        db.flush()
        db.refresh(target)
    if "name" in changes:
        target.name = changes["name"]
    audit(db, actor, "user.update", "user", target.id, fields=sorted(changes))
    db.commit()
    return user_out(target)


def _set_status(user_id: uuid.UUID, status: str, actor: User, db: DBSession,
                litellm: LiteLLMClient) -> UserOut:
    target = db.get(User, user_id)
    if target is None:
        raise ApiError(404, "NOT_FOUND", "User not found.")
    if status != "active" and target.id == actor.id:
        raise ApiError(400, "SELF_SUSPEND", "You cannot suspend your own account.")
    target.status = status
    if status != "active":
        revoke_user_sessions(db, target.id)  # takes effect immediately
        revoke_user_keys(db, target.id, litellm)
    else:
        target.failed_logins, target.locked_until = 0, None
    audit(db, actor, "user.suspend" if status != "active" else "user.activate", "user", target.id)
    db.commit()
    return user_out(target)


@router.post("/{user_id}/suspend", response_model=UserOut)
def suspend(user_id: uuid.UUID, actor: User = Depends(require_super_admin),
            db: DBSession = Depends(get_db), litellm: LiteLLMClient = Depends(get_litellm)):
    return _set_status(user_id, "suspended", actor, db, litellm)


@router.post("/{user_id}/activate", response_model=UserOut)
def activate(user_id: uuid.UUID, actor: User = Depends(require_super_admin),
             db: DBSession = Depends(get_db), litellm: LiteLLMClient = Depends(get_litellm)):
    return _set_status(user_id, "active", actor, db, litellm)
