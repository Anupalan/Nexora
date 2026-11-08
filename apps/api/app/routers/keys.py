import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DBSession

from app.audit import audit
from app.authz import admin_team_ids, get_project_or_404, is_super
from app.config import get_settings
from app.db import get_db
from app.deps import ApiError, current_user
from app.litellm import LiteLLMClient, get_litellm
from app.models import ApiKey, Model, User, utcnow
from app.schemas import KeyCreate, KeyCreated, KeyOut, KeyUpdate
from app.security import hash_token, new_token

router = APIRouter(prefix="/keys", tags=["keys"])


def _enabled_aliases(db: DBSession) -> set[str]:
    return set(db.scalars(select(Model.model_alias).where(Model.enabled.is_(True))))


def _validate_models(db: DBSession, requested: list[str] | None) -> list[str]:
    enabled = _enabled_aliases(db)
    if requested is None:
        requested = sorted(enabled)
    if not requested:
        raise ApiError(400, "NO_MODELS", "No models are available for this key.")
    bad = sorted(set(requested) - enabled)
    if bad:
        raise ApiError(400, "MODEL_NOT_PERMITTED", "Some models are unavailable.", {"models": bad})
    return sorted(set(requested))


def _litellm_names(db: DBSession, aliases: list[str]) -> list[str]:
    """LiteLLM checks a key's model list against the model name in each request, which Nexora
    rewrites from alias to the LiteLLM model name. Keys must therefore be created with those names."""
    rows = dict(db.execute(select(Model.model_alias, Model.litellm_model)
                           .where(Model.model_alias.in_(aliases))).all())
    return sorted({rows[a] for a in aliases if a in rows})


def _visible_key(db: DBSession, actor: User, key_id: uuid.UUID) -> ApiKey:
    k = db.get(ApiKey, key_id)
    can_see = k is not None and (
        k.user_id == actor.id or is_super(actor)
        or (k.team_id is not None and k.team_id in admin_team_ids(db, actor))
    )
    if not can_see:
        raise ApiError(404, "NOT_FOUND", "Key not found.")
    return k


def _new_secret() -> str:
    return get_settings().key_prefix + new_token()


def _prefix(secret: str) -> str:
    return secret[: len(get_settings().key_prefix) + 4] + "…"


def revoke_user_keys(db: DBSession, user_id: uuid.UUID, litellm: LiteLLMClient) -> None:
    """Revoke all active keys of a user (used on suspension). Gateway checks status locally."""
    for k in db.scalars(select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.status == "active")):
        k.status, k.revoked_at = "revoked", utcnow()
        if k.litellm_key_id:
            try:
                litellm.delete_key(k.litellm_key_id)
            except ApiError:
                pass  # already unusable through Nexora; cleanup is retried by ops tooling


@router.get("", response_model=list[KeyOut])
def list_keys(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
              actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    q = select(ApiKey).order_by(ApiKey.created_at.desc(), ApiKey.id).limit(limit).offset(offset)
    if not is_super(actor):
        q = q.where(or_(ApiKey.user_id == actor.id,
                        ApiKey.team_id.in_(admin_team_ids(db, actor))))
    return list(db.scalars(q))


@router.post("", response_model=KeyCreated, status_code=201)
def create_key(body: KeyCreate, actor: User = Depends(current_user), db: DBSession = Depends(get_db),
               litellm: LiteLLMClient = Depends(get_litellm)):
    team_id = None
    if body.project_id:
        project, level = get_project_or_404(db, actor, body.project_id)
        if level == "read_only":
            raise ApiError(403, "FORBIDDEN", "Read-only members cannot create keys.")
        team_id = project.team_id
    models = _validate_models(db, body.models)
    secret = _new_secret()
    row_id = uuid.uuid4()
    expires = utcnow() + timedelta(days=body.expires_in_days)
    key_id = litellm.generate_key(
        # LiteLLM requires unique key aliases across all keys, so suffix with the key's own id.
        key=secret, alias=f"{actor.email}:{body.name}:{row_id.hex[:8]}", models=_litellm_names(db, models),
        duration=f"{body.expires_in_days}d", rpm=body.rpm_limit, tpm=body.tpm_limit,
        metadata={"nexora_user_id": str(actor.id), "nexora_project_id": str(body.project_id or "")},
    )
    row = ApiKey(id=row_id, user_id=actor.id, team_id=team_id, project_id=body.project_id,
                 litellm_key_id=key_id or None, key_hash=hash_token(secret),
                 key_prefix=_prefix(secret), name=body.name, allowed_models=models,
                 rpm_limit=body.rpm_limit, tpm_limit=body.tpm_limit, expires_at=expires)
    db.add(row)
    try:
        db.flush()
        audit(db, actor, "key.create", "api_key", row.id, project_id=body.project_id, models=models)
        db.commit()
    except Exception:
        db.rollback()
        if key_id:
            try:
                litellm.delete_key(key_id)
            except ApiError:
                pass
        raise
    return KeyCreated(**KeyOut.model_validate(row).model_dump(), key=secret)


@router.get("/{key_id}", response_model=KeyOut)
def get_key(key_id: uuid.UUID, actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    return _visible_key(db, actor, key_id)


@router.patch("/{key_id}", response_model=KeyOut)
def update_key(key_id: uuid.UUID, body: KeyUpdate, actor: User = Depends(current_user),
               db: DBSession = Depends(get_db), litellm: LiteLLMClient = Depends(get_litellm)):
    k = _visible_key(db, actor, key_id)
    if k.user_id != actor.id:
        raise ApiError(403, "FORBIDDEN", "Only the key owner can change its policy.")
    if k.status != "active":
        raise ApiError(409, "KEY_REVOKED", "Key is revoked.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "models" in changes:
        changes["models"] = _validate_models(db, changes["models"])
    if k.litellm_key_id:
        litellm.update_key(k.litellm_key_id,
                           models=_litellm_names(db, changes["models"]) if "models" in changes else None,
                           rpm_limit=changes.get("rpm_limit"), tpm_limit=changes.get("tpm_limit"))
    if "models" in changes:
        k.allowed_models = changes.pop("models")
    for f, v in changes.items():
        setattr(k, f, v)
    audit(db, actor, "key.update", "api_key", k.id)
    db.commit()
    return k


@router.post("/{key_id}/rotate", response_model=KeyCreated)
def rotate_key(key_id: uuid.UUID, actor: User = Depends(current_user), db: DBSession = Depends(get_db),
               litellm: LiteLLMClient = Depends(get_litellm)):
    k = _visible_key(db, actor, key_id)
    if k.user_id != actor.id:
        raise ApiError(403, "FORBIDDEN", "Only the key owner can rotate it.")
    if k.status != "active":
        raise ApiError(409, "KEY_REVOKED", "Key is revoked.")
    secret = _new_secret()
    remaining_days = 1
    if k.expires_at:
        from app.models import as_utc

        remaining_days = max(1, (as_utc(k.expires_at) - utcnow()).days + 1)
    new_id = litellm.generate_key(
        key=secret, alias=f"{actor.email}:{k.name}:{uuid.uuid4().hex[:8]}",  # old key still exists here
        models=_litellm_names(db, list(k.allowed_models)),
        duration=f"{remaining_days}d", rpm=k.rpm_limit, tpm=k.tpm_limit,
        metadata={"nexora_user_id": str(actor.id), "nexora_project_id": str(k.project_id or "")},
    )
    old_id = k.litellm_key_id
    k.key_hash, k.key_prefix, k.litellm_key_id = hash_token(secret), _prefix(secret), new_id or None
    audit(db, actor, "key.rotate", "api_key", k.id)
    db.commit()  # old secret stops working in Nexora's gateway here
    if old_id:
        try:
            litellm.delete_key(old_id)
        except ApiError:
            pass
    return KeyCreated(**KeyOut.model_validate(k).model_dump(), key=secret)


@router.post("/{key_id}/revoke", response_model=KeyOut)
def revoke_key(key_id: uuid.UUID, actor: User = Depends(current_user), db: DBSession = Depends(get_db),
               litellm: LiteLLMClient = Depends(get_litellm)):
    k = _visible_key(db, actor, key_id)
    if k.status == "revoked":
        return k
    k.status, k.revoked_at = "revoked", utcnow()
    gateway_deleted = True
    if k.litellm_key_id:
        try:
            litellm.delete_key(k.litellm_key_id)
        except ApiError:
            gateway_deleted = False
    audit(db, actor, "key.revoke", "api_key", k.id, owner_id=k.user_id,
          litellm_deleted=gateway_deleted)
    db.commit()
    return k
