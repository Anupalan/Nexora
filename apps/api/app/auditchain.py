"""Tamper-evident audit log: a hash chain over the append-only table (roadmap §10.7).

Rows are written by many requests at once, so they are chained afterwards by a single sealer: it takes the
rows that have no hash yet, in (created_at, id) order, numbers them continuing from the last sealed row, and
stores for each one  hash = sha256(canonical row + previous hash).  Changing, deleting or reordering any
sealed row breaks every hash after it, which `verify` finds. The Postgres trigger lets a row be updated only
to receive its chain columns, once.

What the chain cannot detect on its own is deleting the newest rows (nothing after them disagrees). For that
keep the head (`seq` and `hash`) somewhere outside the database, e.g. in your monitoring or a ticket, and pass
it to `verify` as an anchor: it fails if the chain no longer contains that exact entry.
"""

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models import AuditLog, as_utc

GENESIS = "0" * 64
LOCK_KEY = 7203141  # Postgres advisory lock so two API workers never seal at once
BATCH = 1000


def canonical(row: AuditLog, seq: int, prev_hash: str) -> str:
    return json.dumps({
        "seq": seq, "prev": prev_hash, "id": str(row.id),
        "actor": str(row.actor_id) if row.actor_id else None, "action": row.action,
        "entity_type": row.entity_type, "entity_id": row.entity_id, "metadata": row.meta,
        "created_at": as_utc(row.created_at).isoformat(),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(row: AuditLog, seq: int, prev_hash: str) -> str:
    return hashlib.sha256(canonical(row, seq, prev_hash).encode()).hexdigest()


def seal(db: DBSession, limit: int = BATCH) -> int:
    """Chain up to `limit` unsealed rows; returns how many. Safe to call from several workers."""
    if db.get_bind().dialect.name == "postgresql":
        if not db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_KEY))):
            return 0
    last = db.scalar(select(AuditLog).where(AuditLog.seq.is_not(None)).order_by(AuditLog.seq.desc()).limit(1))
    seq, prev = (last.seq, last.hash) if last else (0, GENESIS)
    rows = list(db.scalars(select(AuditLog).where(AuditLog.hash.is_(None))
                           .order_by(AuditLog.created_at, AuditLog.id).limit(limit)))
    for r in rows:
        seq += 1
        h = digest(r, seq, prev)
        r.seq, r.prev_hash, r.hash = seq, prev, h
        prev = h
    db.commit()
    return len(rows)


def verify(db: DBSession, anchor_seq: int | None = None, anchor_hash: str | None = None) -> dict:
    """Walk the whole chain. `ok` is false at the first problem, which is described in `problem`."""
    checked, prev, expect, problem, anchor_ok = 0, GENESIS, 1, None, None
    for r in db.scalars(select(AuditLog).where(AuditLog.seq.is_not(None)).order_by(AuditLog.seq)
                        .execution_options(yield_per=500)):
        if r.seq != expect:
            problem = {"seq": expect, "reason": f"entry {expect} is missing (next found is {r.seq})"}
            break
        if r.prev_hash != prev or r.hash != digest(r, r.seq, prev):
            problem = {"seq": r.seq, "id": str(r.id), "reason": "entry was altered or its link to the previous "
                                                                "entry is broken"}
            break
        if anchor_seq == r.seq:
            anchor_ok = r.hash == anchor_hash
        prev, expect, checked = r.hash, expect + 1, checked + 1
    head = db.scalar(select(AuditLog).where(AuditLog.seq.is_not(None)).order_by(AuditLog.seq.desc()).limit(1))
    unsealed = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.hash.is_(None))) or 0
    if problem is None and anchor_seq is not None:
        if anchor_seq > checked:
            problem = {"seq": anchor_seq, "reason": "the anchored entry no longer exists (newer entries were removed)"}
        elif anchor_ok is False:
            problem = {"seq": anchor_seq, "reason": "the anchored entry's hash does not match"}
    return {"ok": problem is None, "checked": checked, "unsealed": unsealed, "problem": problem,
            "head": {"seq": head.seq, "hash": head.hash} if head else None}
