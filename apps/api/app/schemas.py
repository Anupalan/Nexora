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


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
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


# ---- models & keys -------------------------------------------------------------------
class ModelOut(ORM):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())
    id: uuid.UUID
    name: str
    model_alias: str
    provider: str
    description: str | None
    context_limit: int | None
    max_output_tokens: int | None
    enabled: bool


class ModelCreate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: str = Field(min_length=1, max_length=200)
    model_alias: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:/-]+$")
    litellm_model: str = Field(min_length=1, max_length=200)
    provider: str = Field(default="vllm", max_length=50)
    description: str | None = None
    context_limit: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)


class ModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    litellm_model: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    context_limit: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    project_id: uuid.UUID | None = None
    models: list[str] | None = None  # default: every enabled model
    expires_in_days: int = Field(default=90, ge=1, le=365)
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    models: list[str] | None = None
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)


class KeyOut(ORM):
    id: uuid.UUID
    user_id: uuid.UUID
    team_id: uuid.UUID | None
    project_id: uuid.UUID | None
    name: str
    key_prefix: str
    allowed_models: list[str]
    rpm_limit: int | None
    tpm_limit: int | None
    expires_at: datetime | None
    status: str
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class KeyCreated(KeyOut):
    key: str  # plaintext, shown exactly once


# ---- quotas, queue, usage --------------------------------------------------------------
class QuotaLimits(BaseModel):
    requests_per_minute: int | None = Field(default=None, ge=1)
    requests_per_day: int | None = Field(default=None, ge=1)
    requests_per_month: int | None = Field(default=None, ge=1)
    tokens_per_minute: int | None = Field(default=None, ge=1)
    tokens_per_day: int | None = Field(default=None, ge=1)
    tokens_per_month: int | None = Field(default=None, ge=1)
    max_concurrent: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)


class QuotaCreate(QuotaLimits):
    scope_type: Literal["global", "team", "user", "key", "project"]
    scope_id: str | None = None  # required except for global


class QuotaOut(QuotaLimits):
    id: uuid.UUID
    scope_type: str
    scope_id: str
    model_config = ConfigDict(from_attributes=True)
