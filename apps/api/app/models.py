"""Schema: identity, organisation, sessions, audit, models, keys, quotas, inference (roadmap §10)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")

# Role names
SUPER_ADMIN = "super_admin"
TEAM_ADMIN = "team_admin"
DEVELOPER = "developer"
ROLE_NAMES = (SUPER_ADMIN, TEAM_ADMIN, DEVELOPER)

# Project access levels
PROJECT_LEVELS = ("owner", "maintainer", "developer", "read_only")
PROJECT_MANAGER_LEVELS = ("owner", "maintainer")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(SAUuid, primary_key=True, default=uuid.uuid4)


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(50), unique=True)
    permissions: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lowercase
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|suspended|deactivated
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    role: Mapped[Role] = relationship(lazy="joined")


class Session(Base):
    """Server-side browser session. Only a SHA-256 of the token is stored."""

    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    membership_role: Mapped[str] = mapped_column(String(20), default="member")  # admin|member
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("team_id", "name"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    access_level: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Append-only. Never store passwords, tokens or other secrets in metadata."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(SAUuid, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Model(Base):
    """A stable model alias exposed to developers (roadmap §5.4)."""

    __tablename__ = "models"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))  # display name
    model_alias: Mapped[str] = mapped_column(String(100), unique=True)  # what clients send
    litellm_model: Mapped[str] = mapped_column(String(200))  # LiteLLM `model_name`
    provider: Mapped[str] = mapped_column(String(50), default="vllm")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    configuration: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelDeployment(Base):
    __tablename__ = "model_deployments"
    __table_args__ = (UniqueConstraint("model_id", "deployment_name", name="uq_model_deployment_name"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"), index=True)
    deployment_name: Mapped[str] = mapped_column(String(200))
    endpoint_reference: Mapped[str] = mapped_column(String(200))  # secret/config reference, no creds
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")
    configuration: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    """Metadata for a LiteLLM virtual key. The plaintext is never stored (roadmap §5.3)."""

    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    litellm_key_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA-256 of plaintext
    key_prefix: Mapped[str] = mapped_column(String(16))  # e.g. "sk-nx-ab12…" for display
    name: Mapped[str] = mapped_column(String(200))
    allowed_models: Mapped[list] = mapped_column(JSONType, default=list)  # model aliases
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|revoked
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")


QUOTA_SCOPES = ("global", "team", "user", "key", "project")
GLOBAL_SCOPE_ID = "*"


class Quota(Base):
    """One row per scope. NULL limit = unlimited. Effective policy = every applicable scope must pass."""

    __tablename__ = "quotas"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str] = mapped_column(String(64))  # UUID string, or "*" for global
    requests_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_minute: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_per_day: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_per_month: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_concurrent: Mapped[int | None] = mapped_column(Integer, nullable=True)  # queued + running
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class InferenceRequest(Base):
    """Durable record of one model request (roadmap §5.7). Postgres is the source of truth."""

    __tablename__ = "inference_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        Index("ix_inf_user_created", "user_id", "created_at"),
        Index("ix_inf_status_created", "status", "created_at"),
        Index("ix_inf_team_created", "team_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=True)
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"))
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_deployments.id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(String(20), default="api")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    queue_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reservation: Mapped[dict] = mapped_column(JSONType, default=dict)  # Redis counter keys held
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RequestEvent(Base):
    __tablename__ = "request_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inference_requests.id", ondelete="RESTRICT"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    event_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageRecord(Base):
    """Append-only token ledger; one row per request that consumed model capacity."""

    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_user_recorded", "user_id", "recorded_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inference_requests.id", ondelete="RESTRICT"), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(SAUuid, nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(SAUuid, nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(SAUuid, nullable=True)
    model_id: Mapped[uuid.UUID] = mapped_column(SAUuid)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    """In-app alert (roadmap §5.9). One open alert per dedupe_key; resolved when the condition clears."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_status_created", "status", "created_at"),
                      Index("ix_alert_dedupe", "dedupe_key", "status"))
    id: Mapped[uuid.UUID] = _uuid_pk()
    alert_type: Mapped[str] = mapped_column(String(50), index=True)
    severity: Mapped[str] = mapped_column(String(20))  # info | warning | critical
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved
    dedupe_key: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(SAUuid, nullable=True)  # null = auto-resolved

