"""Operational logic: deployment health checks, alert rules, dashboard KPIs (roadmap §5.9, §23, §30).

Inputs from Redis (queue depth, quota counters) are gathered asynchronously first, so the rule
engine itself is plain, deterministic and unit-testable.
"""

from dataclasses import dataclass, field
from datetime import timedelta

import anyio
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app import quota
from app.config import get_settings
from app.inference import dbcall
from app.litellm import LiteLLMClient
from app.models import (
    Alert,
    AuditLog,
    InferenceRequest,
    Model,
    ModelDeployment,
    Quota,
    User,
    as_utc,
    utcnow,
)
from app.scheduler import Scheduler

# ---------------------------------------------------------------- health checks


def _deployments(db: DBSession) -> list[tuple]:
    return [(d.id, m.model_alias, m.litellm_model, m.enabled) for d, m in db.execute(
        select(ModelDeployment, Model).join(Model, Model.id == ModelDeployment.model_id))]


def _store_health(db: DBSession, dep_id, healthy: bool, detail: str, latency_ms: int) -> None:
    d = db.get(ModelDeployment, dep_id)
    d.health_status = "healthy" if healthy else "unhealthy"
    d.configuration = {**(d.configuration or {}), "health": {
        "checked_at": utcnow().isoformat(), "latency_ms": latency_ms, "detail": detail}}
    db.commit()


async def check_deployments(litellm: LiteLLMClient) -> list[dict]:
    """Probe every enabled deployment through LiteLLM and persist the result."""
    st = get_settings()
    out = []
    for dep_id, alias, litellm_model, enabled in await dbcall(_deployments):
        if not enabled:
            continue
        t0 = anyio.current_time()
        res = await anyio.to_thread.run_sync(litellm.health, litellm_model, st.health_timeout_seconds)
        ms = int((anyio.current_time() - t0) * 1000)
        await dbcall(_store_health, dep_id, res["healthy"], res["detail"], ms)
        out.append({"deployment_id": str(dep_id), "model": alias, **res, "latency_ms": ms})
    return out


# ---------------------------------------------------------------- alert rules

@dataclass
class DeploymentState:
    id: str
    model: str
    health: str
    depth: int
    active: int
    max_queue: int
    max_active: int


@dataclass
class QuotaUse:
    scope: str
    field: str
    used: int
    limit: int


@dataclass
class OpsInputs:
    deployments: list[DeploymentState] = field(default_factory=list)
    avg_queue_wait_ms_5m: int | None = None
    started_5m: int = 0
    auth_failures_10m: int = 0
    quotas: list[QuotaUse] = field(default_factory=list)


Desired = dict[str, tuple[str, str, str, dict]]  # dedupe_key -> (type, severity, message, details)


def evaluate_rules(inp: OpsInputs) -> Desired:
    st = get_settings()
    want: Desired = {}
    for d in inp.deployments:
        if d.health == "unhealthy":
            want[f"inference_unavailable:{d.id}"] = (
                "inference_unavailable", "critical",
                f"Model '{d.model}' is unhealthy or its inference server is unreachable.", {"model": d.model})
        if d.depth >= min(st.alert_queue_depth, max(1, int(d.max_queue * 0.8))):
            want[f"queue_depth:{d.id}"] = (
                "queue_depth_high", "warning",
                f"Queue for '{d.model}' has {d.depth} waiting requests (limit {d.max_queue}).",
                {"model": d.model, "depth": d.depth})
    if inp.avg_queue_wait_ms_5m is not None and inp.avg_queue_wait_ms_5m >= st.alert_queue_wait_ms:
        secs = inp.avg_queue_wait_ms_5m // 1000
        want["queue_wait"] = ("queue_wait_high", "warning",
                              f"Average queue wait is {secs}s over the last 5 minutes.",
                              {"avg_ms": inp.avg_queue_wait_ms_5m})
    if inp.auth_failures_10m >= st.alert_auth_failures:
        want["auth_failures"] = ("repeated_auth_failures", "warning",
                                 f"{inp.auth_failures_10m} failed sign-ins in the last 10 minutes.",
                                 {"count": inp.auth_failures_10m})
    for q in inp.quotas:
        ratio = q.used / q.limit
        key = f"quota:{q.scope}:{q.field}"
        label = f"{q.scope} {q.field.replace('_', ' ')}"
        if ratio >= 1:
            want[key] = ("quota_exceeded", "warning", f"Quota exceeded: {label} ({q.used}/{q.limit}).",
                         {"used": q.used, "limit": q.limit})
        elif ratio * 100 >= st.alert_quota_percent:
            want[key] = ("quota_approaching", "info", f"Quota nearly used: {label} ({q.used}/{q.limit}).",
                         {"used": q.used, "limit": q.limit})
    return want


def reconcile_alerts(db: DBSession, want: Desired) -> dict:
    """Open alerts for new conditions; auto-resolve alerts whose condition cleared."""
    opened = resolved = 0
    open_rows = {a.dedupe_key: a for a in db.scalars(select(Alert).where(Alert.status == "open"))}
    for key, (typ, sev, msg, details) in want.items():
        existing = open_rows.get(key)
        if existing is None:
            db.add(Alert(alert_type=typ, severity=sev, message=msg, dedupe_key=key, details=details))
            opened += 1
        elif existing.message != msg or existing.alert_type != typ:  # same condition, new numbers
            existing.message, existing.alert_type, existing.severity = msg, typ, sev
            existing.details = details
    for key, a in open_rows.items():
        if key not in want:
            a.status, a.resolved_at = "resolved", utcnow()
            resolved += 1
    db.commit()
    return {"opened": opened, "resolved": resolved, "open": len(want)}


def _db_inputs(db: DBSession) -> tuple[list[tuple], int | None, int, int, list[Quota]]:
    now = utcnow()
    deps = []
    for d, m in db.execute(select(ModelDeployment, Model).join(Model, Model.id == ModelDeployment.model_id)
                           .where(Model.enabled.is_(True))):
        cfg = d.configuration or {}
        deps.append((d.id, m.model_alias, d.health_status,
                     int(cfg.get("max_queue_length") or get_settings().default_max_queue_length),
                     int(cfg.get("max_active_requests") or get_settings().default_max_active_requests)))
    five = now - timedelta(minutes=5)
    waits = [w for (w,) in db.execute(select(InferenceRequest.queue_time_ms).where(
        InferenceRequest.started_at >= five, InferenceRequest.queue_time_ms.is_not(None)))]
    fails = db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "auth.login_failed", AuditLog.created_at >= now - timedelta(minutes=10))) or 0
    quotas = list(db.scalars(select(Quota)))
    for q in quotas:
        db.expunge(q)
    return deps, (int(sum(waits) / len(waits)) if waits else None), len(waits), fails, quotas


async def gather_inputs(sched: Scheduler) -> OpsInputs:
    deps, avg_wait, n_started, fails, quotas = await dbcall(_db_inputs)
    inp = OpsInputs(avg_queue_wait_ms_5m=avg_wait, started_5m=n_started, auth_failures_10m=fails)
    for dep_id, alias, health, max_q, max_a in deps:
        inp.deployments.append(DeploymentState(str(dep_id), alias, health, await sched.depth(dep_id),
                                               await sched.active_count(dep_id), max_q, max_a))
    now = utcnow()
    for q in quotas:
        scope = (q.scope_type, q.scope_id)
        for period in ("day", "month"):  # minute windows are too short-lived to alert on
            start, _ = quota.window_bounds(period, now)
            for metric, fld in (("req", f"requests_per_{period}"), ("tok", f"tokens_per_{period}")):
                limit = getattr(q, fld)
                if limit:
                    used = await sched.redis.get(quota.counter_key(scope, metric, period, start))
                    label = f"{q.scope_type}:{q.scope_id[:8]}"
                    inp.quotas.append(QuotaUse(label, fld, int(used or 0), limit))
    return inp


async def run_alert_cycle(sched: Scheduler) -> dict:
    inp = await gather_inputs(sched)
    return await dbcall(lambda db: reconcile_alerts(db, evaluate_rules(inp)))


async def ops_cycle(sched: Scheduler, litellm: LiteLLMClient) -> dict:
    health = await check_deployments(litellm)
    alerts = await run_alert_cycle(sched)
    return {"health": health, "alerts": alerts}


# ---------------------------------------------------------------- dashboard

def _today_kpis(db: DBSession) -> dict:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = list(db.scalars(select(InferenceRequest).where(InferenceRequest.created_at >= start)))
    real = [r for r in rows if r.status != "rejected"]
    tps = []
    for r in real:
        measured = r.duration_ms and r.time_to_first_token_ms is not None
        if r.status == "completed" and r.output_tokens and measured:
            gen_s = (r.duration_ms - r.time_to_first_token_ms) / 1000
            if gen_s > 0:
                tps.append(r.output_tokens / gen_s)
    ttfts = [r.time_to_first_token_ms for r in real if r.time_to_first_token_ms is not None]
    return {
        "active_developers": len({r.user_id for r in real}),
        "requests": len(real),
        "failed_requests": sum(r.status == "failed" for r in real),
        "rejected_requests": len(rows) - len(real),
        "input_tokens": sum(r.input_tokens or 0 for r in real),
        "output_tokens": sum(r.output_tokens or 0 for r in real),
        "avg_ttft_ms": int(sum(ttfts) / len(ttfts)) if ttfts else None,
        "avg_generation_tokens_per_s": round(sum(tps) / len(tps), 1) if tps else None,
        "since": as_utc(start),
    }


def _totals(db: DBSession) -> dict:
    return {
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
        "open_alerts": db.scalar(select(func.count()).select_from(Alert).where(Alert.status == "open")) or 0,
        "critical_alerts": db.scalar(select(func.count()).select_from(Alert).where(
            Alert.status == "open", Alert.severity == "critical")) or 0,
    }


async def dashboard(sched: Scheduler) -> dict:
    inp = await gather_inputs(sched)
    today = await dbcall(_today_kpis)
    totals = await dbcall(_totals)
    return {
        "today": today,
        "totals": totals,
        "queue": {"depth": sum(d.depth for d in inp.deployments),
                  "active": sum(d.active for d in inp.deployments),
                  "avg_wait_ms_5m": inp.avg_queue_wait_ms_5m},
        "models": [{"model": d.model, "health": d.health, "queued": d.depth, "active": d.active,
                    "max_active": d.max_active} for d in inp.deployments],
        # GPU / unified-memory figures come from the DGX Spark's exporter via Prometheus, not Nexora.
        "gpu": None,
    }
