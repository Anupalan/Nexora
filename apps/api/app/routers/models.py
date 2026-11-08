import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.audit import audit
from app.db import get_db
from app.deps import ApiError, current_user, require_super_admin
from app.models import Model, User
from app.schemas import ModelCreate, ModelOut, ModelUpdate

router = APIRouter(tags=["models"])


def _get(db: DBSession, model_id: uuid.UUID) -> Model:
    m = db.get(Model, model_id)
    if m is None:
        raise ApiError(404, "NOT_FOUND", "Model not found.")
    return m


@router.get("/models", response_model=list[ModelOut])
def list_models(_: User = Depends(current_user), db: DBSession = Depends(get_db)):
    return list(db.scalars(select(Model).where(Model.enabled.is_(True)).order_by(Model.model_alias)))


@router.get("/models/{model_id}", response_model=ModelOut)
def get_model(model_id: uuid.UUID, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    m = _get(db, model_id)
    if not m.enabled and user.role.name != "super_admin":
        raise ApiError(404, "NOT_FOUND", "Model not found.")
    return m


@router.get("/admin/models", response_model=list[ModelOut])
def admin_list_models(_: User = Depends(require_super_admin), db: DBSession = Depends(get_db)):
    return list(db.scalars(select(Model).order_by(Model.model_alias)))


@router.post("/admin/models", response_model=ModelOut, status_code=201)
def register_model(body: ModelCreate, actor: User = Depends(require_super_admin),
                   db: DBSession = Depends(get_db)):
    m = Model(**body.model_dump())
    db.add(m)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "ALIAS_EXISTS", "A model with this alias already exists.")
    audit(db, actor, "model.create", "model", m.id, alias=m.model_alias)
    db.commit()
    return m


@router.patch("/admin/models/{model_id}", response_model=ModelOut)
def update_model(model_id: uuid.UUID, body: ModelUpdate, actor: User = Depends(require_super_admin),
                 db: DBSession = Depends(get_db)):
    m = _get(db, model_id)
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(m, k, v)
    audit(db, actor, "model.update", "model", m.id, fields=sorted(changes))
    db.commit()
    return m


def _toggle(model_id: uuid.UUID, enabled: bool, actor: User, db: DBSession) -> Model:
    m = _get(db, model_id)
    m.enabled = enabled
    audit(db, actor, "model.enable" if enabled else "model.disable", "model", m.id)
    db.commit()
    return m


@router.post("/admin/models/{model_id}/enable", response_model=ModelOut)
def enable(model_id: uuid.UUID, actor: User = Depends(require_super_admin),
           db: DBSession = Depends(get_db)):
    return _toggle(model_id, True, actor, db)


@router.post("/admin/models/{model_id}/disable", response_model=ModelOut)
def disable(model_id: uuid.UUID, actor: User = Depends(require_super_admin),
            db: DBSession = Depends(get_db)):
    return _toggle(model_id, False, actor, db)
