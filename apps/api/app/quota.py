"""Quota policy resolution and atomic Redis reservations (roadmap §5.5).

Every applicable scope (global, team(s), user, key, project) is checked; a request must pass
ALL of them, so the effective limit is the most restrictive. Request/token capacity is reserved
atomically in a single Lua script before admission and reconciled against actual usage after.
Postgres (inference_requests) is the durable record; Redis counters can be rebuilt from it.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models import ApiKey, InferenceRequest, Quota, TeamMember

PERIODS = ("minute", "day", "month")
Scope = tuple[str, str]

RESERVE_LUA = """
local n = tonumber(ARGV[1])
for i = 1, n do
  local limit = tonumber(ARGV[2 + (i - 1) * 3])
  local delta = tonumber(ARGV[3 + (i - 1) * 3])
  if limit > 0 then
    local cur = tonumber(redis.call('GET', KEYS[i]) or '0')
    if cur + delta > limit then return i end
  end
end
for i = 1, n do
  local delta = tonumber(ARGV[3 + (i - 1) * 3])
  local ttl = tonumber(ARGV[4 + (i - 1) * 3])
  redis.call('INCRBY', KEYS[i], delta)
  if ttl > 0 then redis.call('EXPIRE', KEYS[i], ttl) end
end
return 0
"""


def window_bounds(period: str, now: datetime) -> tuple[int, int]:
    """UTC-aligned window [start, end) as epoch seconds."""
    now = now.astimezone(UTC)
    if period == "minute":
        start = now.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=1)
    elif period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
    return int(start.timestamp()), int(end.timestamp())


def counter_key(scope: Scope, metric: str, period: str, start: int) -> str:
    return f"nexora:q:{scope[0]}:{scope[1]}:{metric}:{period}:{start}"


def conc_key(scope: Scope) -> str:
    return f"nexora:q:{scope[0]}:{scope[1]}:conc"


@dataclass
class PlanItem:
    key: str
    limit: int
    delta: int
    ttl: int
    kind: str  # req | tok | conc
    scope: Scope
    metric: str
    period: str | None
    window_end: int | None


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)

    def reservation(self, tokens: int) -> dict:
        return {"tokens": tokens,
                "req": [i.key for i in self.items if i.kind == "req"],
                "tok": [i.key for i in self.items if i.kind == "tok"],
                "conc": [i.key for i in self.items if i.kind == "conc"]}


@dataclass
class Rejection:
    code: str
    message: str
    retry_after: int


def resolve_limits(db: DBSession, key: ApiKey) -> dict[Scope, dict]:
    """Scope -> {limit_field: value} for every applicable scope that has a limit."""
    scopes: list[Scope] = [("global", "*"), ("user", str(key.user_id)), ("key", str(key.id))]
    if key.project_id:
        scopes.append(("project", str(key.project_id)))
    team_ids = ([key.team_id] if key.team_id
                else list(db.scalars(select(TeamMember.team_id).where(TeamMember.user_id == key.user_id))))
    scopes += [("team", str(t)) for t in team_ids]
    rows = db.scalars(select(Quota).where(
        Quota.scope_type.in_({s[0] for s in scopes}), Quota.scope_id.in_({s[1] for s in scopes})))
    out: dict[Scope, dict] = {}
    fields = [c for c in Quota.__table__.columns.keys()
              if c.startswith(("requests_per_", "tokens_per_"))
              or c in ("max_concurrent", "max_output_tokens")]
    for q in rows:
        scope = (q.scope_type, q.scope_id)
        if scope in scopes:
            lim = {f: getattr(q, f) for f in fields if getattr(q, f) is not None}
            if lim:
                out[scope] = lim
    # Per-key RPM/TPM set at key creation act as key-scope limits (most restrictive wins).
    ks = ("key", str(key.id))
    for f, v in (("requests_per_minute", key.rpm_limit), ("tokens_per_minute", key.tpm_limit)):
        if v:
            out.setdefault(ks, {})[f] = min(v, out.get(ks, {}).get(f, v))
    return out


def effective_max_output(limits: dict[Scope, dict]) -> int | None:
    vals = [lim["max_output_tokens"] for lim in limits.values() if "max_output_tokens" in lim]
    return min(vals) if vals else None


def build_plan(limits: dict[Scope, dict], now: datetime, est_tokens: int) -> Plan:
    plan = Plan()
    for scope, lim in limits.items():
        for period in PERIODS:
            start, end = window_bounds(period, now)
            ttl = end - int(now.timestamp()) + 3600
            for metric, field_, delta in (("req", f"requests_per_{period}", 1),
                                          ("tok", f"tokens_per_{period}", est_tokens)):
                if field_ in lim:
                    plan.items.append(PlanItem(counter_key(scope, metric, period, start), lim[field_],
                                               delta, ttl, metric, scope, field_, period, end))
        if "max_concurrent" in lim:
            plan.items.append(PlanItem(conc_key(scope), lim["max_concurrent"], 1, 0, "conc", scope,
                                       "max_concurrent", None, None))
    return plan


async def reserve(redis, plan: Plan, now: datetime) -> Rejection | None:
    if not plan.items:
        return None
    args: list = [len(plan.items)]
    for i in plan.items:
        args += [i.limit, i.delta, i.ttl]
    failed = await redis.eval(RESERVE_LUA, len(plan.items), *[i.key for i in plan.items], *args)
    if not failed:
        return None
    it = plan.items[int(failed) - 1]
    retry = max(1, (it.window_end - int(now.timestamp())) if it.window_end else 5)
    what = it.metric.replace("_", " ")
    return Rejection("quota_exceeded",
                     f"The {it.scope[0]} {what} limit ({it.limit}) has been reached.", retry)


async def adjust_tokens(redis, tok_keys: list[str], delta: int) -> None:
    if delta and tok_keys:
        pipe = redis.pipeline()
        for k in tok_keys:
            pipe.incrby(k, delta)
        await pipe.execute()


async def release_capacity(redis, reservation: dict, *, requests: bool, tokens: int) -> None:
    """Undo (part of) a reservation. `tokens` is how many reserved tokens to give back."""
    pipe = redis.pipeline()
    for k in reservation.get("conc", []):
        pipe.decr(k)
    if requests:
        for k in reservation.get("req", []):
            pipe.decr(k)
    if tokens:
        for k in reservation.get("tok", []):
            pipe.decrby(k, tokens)
    await pipe.execute()


EPOCH_KEY = "nexora:q:epoch"


async def ensure_counters(redis, db: DBSession) -> bool:
    """Rebuild Redis counters from Postgres if Redis was wiped/restarted. Returns True if rebuilt."""
    if await redis.exists(EPOCH_KEY):
        return False
    now = int(datetime.now(UTC).timestamp())
    totals: dict[str, int] = {}
    month_ago = datetime.now(UTC) - timedelta(days=32)
    for r in db.scalars(select(InferenceRequest).where(InferenceRequest.created_at >= month_ago)):
        res = r.reservation or {}
        in_flight = r.status in ("queued", "running")
        finished = r.status in ("completed", "failed", "cancelled")
        consumed = in_flight or (r.started_at is not None and finished)
        if not consumed:
            continue
        used = (r.reserved_tokens if in_flight
                else (r.input_tokens or 0) + (r.output_tokens or 0))
        for k in res.get("req", []):
            totals[k] = totals.get(k, 0) + 1
        for k in res.get("tok", []):
            totals[k] = totals.get(k, 0) + used
        if in_flight:
            for k in res.get("conc", []):
                totals[k] = totals.get(k, 0) + 1
    pipe = redis.pipeline()
    for k, v in totals.items():
        if k.endswith(":conc"):
            pipe.set(k, v)
        else:
            try:
                end = int(k.rsplit(":", 1)[1])  # window start; skip windows that are long over
            except ValueError:
                continue
            if now - end < 40 * 86400:
                pipe.set(k, v, ex=40 * 86400)
    pipe.set(EPOCH_KEY, now)
    await pipe.execute()
    return True


def new_id() -> uuid.UUID:
    return uuid.uuid4()
