"""Retention for detailed request logs (roadmap §10.8).

Only the detail tables are purged: per-request lifecycle events and routing decisions. Request headers
(`inference_requests`), usage records and the audit log are kept, because quotas, usage reports and the audit
trail depend on them. Prompt and response content is not stored at all unless content logging is turned on
(it is off by default). A value of 0 keeps that table forever. The purge runs about once a day.
"""

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app import auditchain
from app.models import PlatformSetting, RequestEvent, RoutingDecision, utcnow

KEY = "retention"
STATUS_KEY = "maintenance_status"
DEFAULTS = {"request_events_days": 90, "routing_decisions_days": 90}
BATCH = 5000


def load(db: DBSession) -> dict:
    row = db.get(PlatformSetting, KEY)
    return {**DEFAULTS, **{k: int(v) for k, v in ((row.value if row else None) or {}).items() if k in DEFAULTS}}


def save(db: DBSession, values: dict, actor_id=None) -> dict:
    for k, v in values.items():
        if k not in DEFAULTS or not 0 <= int(v) <= 3650:
            raise ValueError(f"{k} must be 0-3650 days (0 keeps everything).")
    merged = {**load(db), **{k: int(v) for k, v in values.items()}}
    row = db.get(PlatformSetting, KEY) or PlatformSetting(key=KEY, value={})
    row.value, row.updated_by = merged, actor_id
    db.add(row)
    db.commit()
    return merged


def purge(db: DBSession) -> dict:
    cfg, now, out = load(db), utcnow(), {}
    for name, table, col in (("request_events", RequestEvent, RequestEvent.created_at),
                             ("routing_decisions", RoutingDecision, RoutingDecision.created_at)):
        days = cfg[f"{name}_days"]
        out[name] = 0
        if days <= 0:
            continue
        cutoff = now - timedelta(days=days)
        while True:  # batches keep each transaction short
            ids = list(db.scalars(select(table.id).where(col < cutoff).limit(BATCH)))
            if not ids:
                break
            db.execute(delete(table).where(table.id.in_(ids)))
            db.commit()
            out[name] += len(ids)
    return out


def maintenance(db: DBSession, force: bool = False) -> dict | None:
    """Purge old detail rows and verify the audit chain, at most once every 24 hours."""
    row = db.get(PlatformSetting, STATUS_KEY)
    last = (row.value or {}).get("at") if row else None
    if not force and last and utcnow() - _parse(last) < timedelta(hours=24):
        return None
    auditchain.seal(db)
    result = {"at": utcnow().isoformat(), "purged": purge(db), "audit_chain": auditchain.verify(db)}
    row = db.get(PlatformSetting, STATUS_KEY) or PlatformSetting(key=STATUS_KEY, value={})
    row.value = result
    db.add(row)
    db.commit()
    return result


def _parse(text: str):
    from datetime import datetime, timezone
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
