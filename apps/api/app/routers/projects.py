import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.audit import audit
from app.authz import (
    admin_team_ids,
    get_project_or_404,
    get_team_or_404,
    is_super,
    is_team_admin,
    require_project_manager,
    team_membership,
)
from app.db import get_db
from app.deps import ApiError, current_user
from app.models import Project, ProjectMember, User
from app.schemas import (
    ProjectCreate,
    ProjectDetail,
    ProjectMemberIn,
    ProjectMemberOut,
    ProjectOut,
    ProjectUpdate,
)

router = APIRouter(prefix="/projects", tags=["projects"])


def _detail(db: DBSession, project: Project, level: str) -> ProjectDetail:
    rows = db.execute(
        select(ProjectMember, User).join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project.id).order_by(ProjectMember.created_at)
    ).all()
    members = [ProjectMemberOut(user_id=u.id, name=u.name, email=u.email,
                                access_level=m.access_level) for m, u in rows]
    return ProjectDetail(**ProjectOut.model_validate(project).model_dump(),
                         your_access=level, members=members)


@router.get("", response_model=list[ProjectOut])
def list_projects(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    q = select(Project).order_by(Project.name, Project.id).limit(limit).offset(offset)
    if not is_super(actor):
        q = q.where(or_(
            Project.team_id.in_(admin_team_ids(db, actor)),
            Project.id.in_(select(ProjectMember.project_id)
                           .where(ProjectMember.user_id == actor.id)),
        ))
    return list(db.scalars(q))


@router.post("", response_model=ProjectDetail, status_code=201)
def create_project(body: ProjectCreate, actor: User = Depends(current_user),
                   db: DBSession = Depends(get_db)):
    team = get_team_or_404(db, actor, body.team_id)
    if not is_team_admin(db, actor, team.id):
        raise ApiError(403, "FORBIDDEN", "Team admin access required.")
    project = Project(team_id=team.id, name=body.name, description=body.description)
    db.add(project)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "PROJECT_EXISTS", "A project with this name exists in the team.")
    audit(db, actor, "project.create", "project", project.id, team_id=team.id)
    db.commit()
    return _detail(db, project, "owner")


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: uuid.UUID, actor: User = Depends(current_user),
                db: DBSession = Depends(get_db)):
    project, level = get_project_or_404(db, actor, project_id)
    return _detail(db, project, level)


@router.patch("/{project_id}", response_model=ProjectDetail)
def update_project(project_id: uuid.UUID, body: ProjectUpdate, actor: User = Depends(current_user),
                   db: DBSession = Depends(get_db)):
    project, level = get_project_or_404(db, actor, project_id)
    require_project_manager(level)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for k, v in changes.items():
        setattr(project, k, v)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "PROJECT_EXISTS", "A project with this name exists in the team.")
    audit(db, actor, "project.update", "project", project.id, fields=sorted(changes))
    db.commit()
    return _detail(db, project, level)


@router.post("/{project_id}/members", response_model=ProjectDetail, status_code=201)
def add_member(project_id: uuid.UUID, body: ProjectMemberIn, actor: User = Depends(current_user),
               db: DBSession = Depends(get_db)):
    project, level = get_project_or_404(db, actor, project_id)
    require_project_manager(level)
    target = db.get(User, body.user_id)
    # Project members must belong to the owning team (super admins exempt as targets).
    if target is None or not (is_super(target)
                              or team_membership(db, target, project.team_id) is not None):
        raise ApiError(400, "NOT_TEAM_MEMBER", "User must be a member of the project's team.")
    if body.access_level == "owner" and level != "owner":
        raise ApiError(403, "FORBIDDEN", "Only an owner can grant owner access.")
    existing = db.get(ProjectMember, (project.id, target.id))
    if existing:
        existing.access_level = body.access_level
    else:
        db.add(ProjectMember(project_id=project.id, user_id=target.id,
                             access_level=body.access_level))
    audit(db, actor, "project.member_set", "project", project.id, user_id=target.id,
          access_level=body.access_level)
    db.commit()
    return _detail(db, project, level)


@router.delete("/{project_id}/members/{user_id}", response_model=ProjectDetail)
def remove_member(project_id: uuid.UUID, user_id: uuid.UUID, actor: User = Depends(current_user),
                  db: DBSession = Depends(get_db)):
    project, level = get_project_or_404(db, actor, project_id)
    require_project_manager(level)
    m = db.get(ProjectMember, (project.id, user_id))
    if m is None:
        raise ApiError(404, "NOT_FOUND", "Member not found.")
    if m.access_level == "owner" and level != "owner":
        raise ApiError(403, "FORBIDDEN", "Only an owner can remove an owner.")
    db.delete(m)
    audit(db, actor, "project.member_remove", "project", project.id, user_id=user_id)
    db.commit()
    return _detail(db, project, level)
