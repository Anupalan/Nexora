import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.security import MIN_PASSWORD_LENGTH


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class UserOut(ORM):
    id: uuid.UUID
    name: str
    email: str
    status: str
    role: str
    last_login_at: datetime | None
    created_at: datetime


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(max_length=256)
    role: Literal["super_admin", "team_admin", "developer"] = "developer"

    @field_validator("password")
    @classmethod
    def _pw(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        return v


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Literal["super_admin", "team_admin", "developer"] | None = None


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "inactive"] | None = None


class TeamMemberIn(BaseModel):
    user_id: uuid.UUID
    membership_role: Literal["admin", "member"] = "member"


class TeamMemberOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    membership_role: str
    joined_at: datetime


class TeamOut(ORM):
    id: uuid.UUID
    name: str
    status: str
    created_at: datetime


class TeamDetail(TeamOut):
    members: list[TeamMemberOut]


class ProjectCreate(BaseModel):
    team_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    repository_url: str | None = Field(default=None, max_length=500)
    default_branch: str = Field(default="main", max_length=200)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    repository_url: str | None = Field(default=None, max_length=500)
    default_branch: str | None = Field(default=None, max_length=200)
    status: Literal["active", "archived"] | None = None


class ProjectMemberIn(BaseModel):
    user_id: uuid.UUID
    access_level: Literal["owner", "maintainer", "developer", "read_only"]


class ProjectMemberOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    access_level: str


class ProjectOut(ORM):
    id: uuid.UUID
    team_id: uuid.UUID
    name: str
    description: str | None
    repository_url: str | None
    default_branch: str
    status: str
    created_at: datetime


class ProjectDetail(ProjectOut):
    your_access: str
    members: list[ProjectMemberOut]


class AuditOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: str | None
    metadata: dict
    created_at: datetime
