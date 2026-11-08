"""Backend authorization rules (deny by default). Roadmap §4.4.

Resources a caller cannot see at all return 404 (no enumeration); resources they can see
but not modify return 403.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.deps import ApiError
from app.models import (
    PROJECT_MANAGER_LEVELS,
    SUPER_ADMIN,
    Project,
    ProjectMember,
    Team,
    TeamMember,
    User,
)


def is_super(user: User) -> bool:
    return user.role.name == SUPER_ADMIN


def team_membership(db: DBSession, user: User, team_id: uuid.UUID) -> str | None:
    m = db.scalar(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user.id)
    )
    return m.membership_role if m else None


def is_team_admin(db: DBSession, user: User, team_id: uuid.UUID) -> bool:
    return is_super(user) or team_membership(db, user, team_id) == "admin"


def get_team_or_404(db: DBSession, user: User, team_id: uuid.UUID) -> Team:
    team = db.get(Team, team_id)
    if team is None or (not is_super(user) and team_membership(db, user, team_id) is None):
        raise ApiError(404, "NOT_FOUND", "Team not found.")
    return team


def project_level(db: DBSession, user: User, project: Project) -> str | None:
    """Effective access: 'owner' for super/team admins, else explicit project level."""
    if is_team_admin(db, user, project.team_id):
        return "owner"
    m = db.get(ProjectMember, (project.id, user.id))
    return m.access_level if m else None


def get_project_or_404(db: DBSession, user: User, project_id: uuid.UUID) -> tuple[Project, str]:
    project = db.get(Project, project_id)
    level = project_level(db, user, project) if project else None
    if project is None or level is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    return project, level


def require_project_manager(level: str) -> None:
    if level not in PROJECT_MANAGER_LEVELS:
        raise ApiError(403, "FORBIDDEN", "Project owner or maintainer access required.")


def admin_team_ids(db: DBSession, user: User) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(TeamMember.team_id).where(
                TeamMember.user_id == user.id, TeamMember.membership_role == "admin"
            )
        )
    )
