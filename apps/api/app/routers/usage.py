"""Usage analytics, request history and CSV export (roadmap §5.7, §5.8, §18).

Reports are computed from inference_requests (Postgres, the durable source). Prompt and
completion content is never stored. Scope: developers see their own requests; team admins
see requests of the teams they administer; super admins see everything.
"""

import csv
import io
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session as DBSession

from app.authz import admin_team_ids, get_project_or_404, get_team_or_404, is_super, is_team_admin
from app.db import get_db
from app.deps import ApiError, current_user
from app.models import ApiKey, InferenceRequest, Model, User, as_utc, utcnow

router = APIRouter(prefix="/usage", tags=["usage"])
MAX_ROWS = 200_000


class Filters:
    def __init__(self, date_from: datetime | None = None, date_to: datetime | None = None,
                 project_id: uuid.UUID | None = None, api_key_id: uuid.UUID | None = None,
                 model: str | None = None, status: str | None = None, source: str | None = None,
                 user_id: uuid.UUID | None = None, team_id: uuid.UUID | None = None):
        self.date_to = as_utc(date_to) if date_to else utcnow()
        self.date_from = as_utc(date_from) if date_from else self.date_to - timedelta(days=30)
        self.project_id, self.api_key_id, self.model = project_id, api_key_id, model
        self.status, self.source, self.user_id, self.team_id = status, source, user_id, team_id


def filters(date_from: datetime | None = Query(None, alias="from"),
            date_to: datetime | None = Query(None, alias="to"), project_id: uuid.UUID | None = None,
            api_key_id: uuid.UUID | None = None, model: str | None = None, status: str | None = None,
            source: str | None = None, user_id: uuid.UUID | None = None,
            team_id: uuid.UUID | None = None) -> Filters:
    f = Filters(date_from, date_to, project_id, api_key_id, model, status, source, user_id, team_id)
    if f.date_to - f.date_from > timedelta(days=366):
        raise ApiError(400, "RANGE_TOO_LARGE", "Date range may not exceed 366 days.")
    return f


def _conditions(db: DBSession, actor: User, f: Filters, *, only_self: bool = False,
                unrestricted: bool = False) -> list:
    c = [InferenceRequest.created_at >= f.date_from, InferenceRequest.created_at <= f.date_to]
    if only_self:
        c.append(InferenceRequest.user_id == actor.id)
    elif not (is_super(actor) or unrestricted):
        teams = admin_team_ids(db, actor)
        c.append(or_(InferenceRequest.user_id == actor.id,
                     InferenceRequest.team_id.in_(teams) if teams else False))
    if f.project_id:
        c.append(InferenceRequest.project_id == f.project_id)
    if f.api_key_id:
        c.append(InferenceRequest.api_key_id == f.api_key_id)
    if f.status:
        c.append(InferenceRequest.status == f.status)
    if f.source:
        c.append(InferenceRequest.source == f.source)
    if f.user_id:
        c.append(InferenceRequest.user_id == f.user_id)
    if f.team_id:
        c.append(InferenceRequest.team_id == f.team_id)
    if f.model:
        c.append(InferenceRequest.model_id.in_(select(Model.id).where(Model.model_alias == f.model)))
    return c


def _rows(db: DBSession, conds: list):
    q = (select(InferenceRequest, Model.model_alias).join(Model, Model.id == InferenceRequest.model_id)
         .where(and_(*conds)).order_by(InferenceRequest.created_at.desc()).limit(MAX_ROWS))
    return db.execute(q).all()


def _bucket() -> dict:
    return {"requests": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _add(b: dict, r: InferenceRequest) -> None:
    b["requests"] += 1
    b["failed"] += r.status == "failed"
    b["input_tokens"] += r.input_tokens or 0
    b["output_tokens"] += r.output_tokens or 0
    b["total_tokens"] += (r.input_tokens or 0) + (r.output_tokens or 0)


def build_report(rows, f: Filters) -> dict:
    totals, by_day, by_model, by_project, by_key = _bucket(), {}, {}, {}, {}
    queue_ms, ttft_ms = [], []
    for r, alias in rows:
        if r.status == "rejected":  # never reached the model; shown in history only
            continue
        _add(totals, r)
        _add(by_day.setdefault(as_utc(r.created_at).date().isoformat(), _bucket()), r)
        _add(by_model.setdefault(alias, _bucket()), r)
        _add(by_project.setdefault(str(r.project_id) if r.project_id else None, _bucket()), r)
        _add(by_key.setdefault(str(r.api_key_id) if r.api_key_id else None, _bucket()), r)
        if r.queue_time_ms is not None:
            queue_ms.append(r.queue_time_ms)
        if r.time_to_first_token_ms is not None:
            ttft_ms.append(r.time_to_first_token_ms)
    avg = lambda xs: int(sum(xs) / len(xs)) if xs else None  # noqa: E731
    return {
        "from": f.date_from, "to": f.date_to,
        "totals": {**totals, "avg_queue_ms": avg(queue_ms), "avg_ttft_ms": avg(ttft_ms)},
        "by_day": [{"date": k, **v} for k, v in sorted(by_day.items())],
        "by_model": [{"model": k, **v} for k, v in sorted(by_model.items())],
        "by_project": [{"project_id": k, **v} for k, v in by_project.items()],
        "by_key": [{"api_key_id": k, **v} for k, v in by_key.items()],
    }


@router.get("/me")
def usage_me(f: Filters = Depends(filters), actor: User = Depends(current_user),
             db: DBSession = Depends(get_db)):
    return build_report(_rows(db, _conditions(db, actor, f, only_self=True)), f)


@router.get("/team/{team_id}")
def usage_team(team_id: uuid.UUID, f: Filters = Depends(filters), actor: User = Depends(current_user),
               db: DBSession = Depends(get_db)):
    team = get_team_or_404(db, actor, team_id)
    if not is_team_admin(db, actor, team.id):
        raise ApiError(403, "FORBIDDEN", "Team admin access required.")
    f.team_id = team.id
    return build_report(_rows(db, _conditions(db, actor, f)), f)


@router.get("/project/{project_id}")
def usage_project(project_id: uuid.UUID, f: Filters = Depends(filters),
                  actor: User = Depends(current_user), db: DBSession = Depends(get_db)):
    project, level = get_project_or_404(db, actor, project_id)
    if level not in ("owner", "maintainer"):
        raise ApiError(403, "FORBIDDEN", "Project owner or maintainer access required.")
    f.project_id = project.id
    # Project managers see all usage in the project, not just their own.
    return build_report(_rows(db, _conditions(db, actor, f, unrestricted=True)), f)


@router.get("/report")
def usage_report(f: Filters = Depends(filters), actor: User = Depends(current_user),
                 db: DBSession = Depends(get_db)):
    return build_report(_rows(db, _conditions(db, actor, f)), f)


def _history_item(r: InferenceRequest, alias: str) -> dict:
    return {"id": str(r.id), "model": alias, "project_id": str(r.project_id) if r.project_id else None,
            "api_key_id": str(r.api_key_id) if r.api_key_id else None, "source": r.source,
            "status": r.status, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "queue_time_ms": r.queue_time_ms, "time_to_first_token_ms": r.time_to_first_token_ms,
            "duration_ms": r.duration_ms, "error_code": r.error_code, "created_at": r.created_at}


@router.get("/requests")
def request_history(f: Filters = Depends(filters), limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0), actor: User = Depends(current_user),
                    db: DBSession = Depends(get_db)):
    q = (select(InferenceRequest, Model.model_alias).join(Model, Model.id == InferenceRequest.model_id)
         .where(and_(*_conditions(db, actor, f))).order_by(InferenceRequest.created_at.desc(),
                                                            InferenceRequest.id).limit(limit).offset(offset))
    return [_history_item(r, a) for r, a in db.execute(q).all()]


def _safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # CSV/formula injection


CSV_COLUMNS = ["id", "created_at", "model", "project_id", "api_key_id", "source", "status",
               "input_tokens", "output_tokens", "queue_time_ms", "time_to_first_token_ms",
               "duration_ms", "error_code"]


@router.get("/export")
def export_csv(f: Filters = Depends(filters), actor: User = Depends(current_user),
               db: DBSession = Depends(get_db)):
    rows = _rows(db, _conditions(db, actor, f))

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_COLUMNS)
        yield buf.getvalue()
        for r, alias in rows:
            buf.seek(0)
            buf.truncate()
            item = _history_item(r, alias)
            w.writerow([_safe(item[c]) for c in CSV_COLUMNS])
            yield buf.getvalue()

    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"content-disposition": 'attachment; filename="nexora-usage.csv"'})


_ = ApiKey  # keys referenced in filters by id only
