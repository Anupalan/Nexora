import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.audit import audit
from app.authz import get_team_or_404, is_super, is_team_admin
from app.db import get_db
from app.deps import ApiError, current_user, require_super_admin
from app.models import Team, TeamMember, User
from app.schemas import TeamCreate, TeamDetail, TeamMemberIn, TeamMemberOut, TeamOut, TeamUpdate

router = APIRouter(prefix="/teams", tags=["teams"])


def _detail(db: DBSession, team: Team) -> TeamDetail:
    rows = db.execute(
        select(TeamMember, User).join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team.id).order_by(TeamMember.joined_at)
    ).all()
    members = [TeamMemberOut(user_id=u.id, name=u.name, email=u.email,
                             membership_role=m.membership_role, joined_at=m.joined_at)
               for m, u in rows]
    return TeamDetail(id=team.id, name=team.name, status=team.status,
                      created_at=team.created_at, members=members)


def _require_team_admin(db: DBSession, actor: User, team: Team) -> None:
    if not is_team_admin(db, actor, team.id):
        raise ApiError(403, "FORBIDDEN", "Team admin access required.")


@router.get("", response_model=list[TeamOut])
def list_teams(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    q = select(Team).order_by(Team.name).limit(limit).offset(offset)
    if not is_super(actor):
        q = q.where(Team.id.in_(select(TeamMember.team_id).where(TeamMember.user_id == actor.id)))
    return list(db.scalars(q))


@router.post("", response_model=TeamOut, status_code=201)
def create_team(body: TeamCreate, actor: User = Depends(require_super_admin),
                db: DBSession = Depends(get_db)):
    team = Team(name=body.name)
    db.add(team)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "TEAM_EXISTS", "A team with this name already exists.")
    audit(db, actor, "team.create", "team", team.id, name=team.name)
    db.commit()
    return team


@router.get("/{team_id}", response_model=TeamDetail)
def get_team(team_id: uuid.UUID, actor: User = Depends(current_user),
             db: DBSession = Depends(get_db)):
    return _detail(db, get_team_or_404(db, actor, team_id))


@router.patch("/{team_id}", response_model=TeamDetail)
def update_team(team_id: uuid.UUID, body: TeamUpdate, actor: User = Depends(current_user),
                db: DBSession = Depends(get_db)):
    team = get_team_or_404(db, actor, team_id)
    _require_team_admin(db, actor, team)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for k, v in changes.items():
        setattr(team, k, v)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "TEAM_EXISTS", "A team with this name already exists.")
    audit(db, actor, "team.update", "team", team.id, fields=sorted(changes))
    db.commit()
    return _detail(db, team)


@router.post("/{team_id}/members", response_model=TeamDetail, status_code=201)
def add_member(team_id: uuid.UUID, body: TeamMemberIn, actor: User = Depends(current_user),
               db: DBSession = Depends(get_db)):
    team = get_team_or_404(db, actor, team_id)
    _require_team_admin(db, actor, team)
    if db.get(User, body.user_id) is None:
        raise ApiError(404, "NOT_FOUND", "User not found.")
    if body.membership_role == "admin" and not is_super(actor):
        # Only a super admin can mint new team admins.
        raise ApiError(403, "FORBIDDEN", "Only a super admin can assign team admins.")
    db.add(TeamMember(team_id=team.id, user_id=body.user_id, membership_role=body.membership_role))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "ALREADY_MEMBER", "User is already a member of this team.")
    audit(db, actor, "team.member_add", "team", team.id, user_id=body.user_id,
          membership_role=body.membership_role)
    db.commit()
    return _detail(db, team)


@router.delete("/{team_id}/members/{user_id}", response_model=TeamDetail)
def remove_member(team_id: uuid.UUID, user_id: uuid.UUID, actor: User = Depends(current_user),
                  db: DBSession = Depends(get_db)):
    team = get_team_or_404(db, actor, team_id)
    _require_team_admin(db, actor, team)
    m = db.scalar(select(TeamMember).where(TeamMember.team_id == team.id,
                                           TeamMember.user_id == user_id))
    if m is None:
        raise ApiError(404, "NOT_FOUND", "Member not found.")
    if m.membership_role == "admin" and not is_super(actor):
        raise ApiError(403, "FORBIDDEN", "Only a super admin can remove team admins.")
    # Removing from the team also drops their project access within it.
    from app.models import Project, ProjectMember

    for pm in db.scalars(select(ProjectMember).join(Project, Project.id == ProjectMember.project_id)
                         .where(Project.team_id == team.id, ProjectMember.user_id == user_id)):
        db.delete(pm)
    db.delete(m)
    audit(db, actor, "team.member_remove", "team", team.id, user_id=user_id)
    db.commit()
    return _detail(db, team)
