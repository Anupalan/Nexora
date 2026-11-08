"""Request routing across deployments (roadmap §6).

`choose()` is pure and deterministic: the same candidates, policy and request id always give the same
answer, which is what makes decisions reproducible, explainable and testable. Everything that touches
the database or Redis lives in `load_candidates()`, `fill_load()` and `record_decision()`.

Safety rules baked in here:
  * only enabled, non-draining, healthy deployments are eligible;
  * a deployment whose context window is too small for the request is never chosen;
  * a deployment whose queue is full is not chosen (and if every queue is full the caller gets 429);
  * with a reference model build set, deployments serving a different build are excluded;
  * when routing is frozen the strategy is forced to `fixed` and residency awareness is off.
"""

import hashlib
import uuid
from dataclasses import asdict, dataclass, field

from sqlalchemy import func, select

from app import workload
from app.config import get_settings
from app.models import (
    InferenceNode,
    InferenceRequest,
    Model,
    ModelDeployment,
    PlatformSetting,
    RoutingDecision,
    RoutingPolicy,
)

DEFAULT_SERVICE_S = 5.0
DEFAULT_LOAD_S = 10.0
STRATEGIES = ("fixed", "least_loaded", "weighted")
FREEZE_KEY = "routing_freeze"


@dataclass
class Candidate:
    id: str
    name: str
    model_alias: str
    litellm_model: str
    node_id: str | None = None
    node: str | None = None
    node_enabled: bool = True
    node_budget: int | None = None
    weight: int = 100
    enabled: bool = True
    draining: bool = False
    health: str = "unknown"
    context_limit: int | None = None
    runtime_model: str | None = None
    digest: str | None = None
    resident: bool | None = None  # model loaded on its node; None = unknown
    load_seconds: float = DEFAULT_LOAD_S
    max_active: int = 2
    max_queue: int = 100
    avg_service_s: float | None = None
    # live state (filled from Redis)
    queued: int = 0
    active: int = 0
    node_active: int = 0
    # workload lanes (filled by load_candidates / fill_load); empty means classes are not in play
    reserved: dict[str, int] = field(default_factory=dict)  # class -> reserved slots on this deployment
    class_prio: dict[str, int] = field(default_factory=dict)  # class -> priority (lower goes first)
    hold: bool = False  # recently-active classes keep their reserved slots
    cls_active: dict[str, int] = field(default_factory=dict)
    cls_queued: dict[str, int] = field(default_factory=dict)
    cls_seen: dict[str, bool] = field(default_factory=dict)


@dataclass
class Policy:
    id: str | None = None
    version: int = 0
    strategy: str = "least_loaded"
    residency_aware: bool = True
    exclude_mismatched: bool = True
    reference_digest: str | None = None
    default_service_s: float = DEFAULT_SERVICE_S
    frozen: bool = False


@dataclass
class Decision:
    chosen: Candidate | None
    strategy: str
    reason: str
    candidates: list[dict] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class Loaded:
    model_id: uuid.UUID
    alias: str
    candidates: list[Candidate]
    node_deployments: dict[str, list[str]]  # node id -> every deployment id on that node
    policy: Policy
    workload: "workload.WorkloadConfig" = field(default_factory=workload.WorkloadConfig)


# ------------------------------------------------------------------ pure decision logic

def ineligible(c: Candidate, p: Policy, est_tokens: int, exclude: set[str]) -> str | None:
    if c.id in exclude:
        return "excluded after a failure"
    if not c.enabled or not c.node_enabled:
        return "disabled"
    if c.draining:
        return "draining"
    if c.health == "unhealthy":
        return "unhealthy"
    if c.weight <= 0:
        return "weight is 0"
    if p.reference_digest and p.exclude_mismatched and c.digest and c.digest != p.reference_digest:
        return "serves a different model build than the reference"
    if c.context_limit and est_tokens > c.context_limit:
        return f"context window {c.context_limit} is smaller than the request (~{est_tokens} tokens)"
    if c.queued >= c.max_queue:
        return "queue is full"
    return None


def expected_wait(c: Candidate, p: Policy, klass: str = workload.DEFAULT_CLASS) -> float:
    """Seconds a new request of `klass` would wait for a slot (queueing only, not generation).

    Slots held back for other recently-active classes are not available to this request, and classes that
    outrank it count as work ahead of it. With no other class in play this reduces to the plain
    (queued + running - slots + 1) / slots formula.
    """
    svc = c.avg_service_s or p.default_service_s
    if not c.cls_seen and not c.cls_queued and not c.cls_active:
        ahead = max(0, c.queued + c.active - c.max_active + 1)
        wait = ahead / max(1, c.max_active) * svc
    else:
        r_k, a_k = c.reserved.get(klass, 0), c.cls_active.get(klass, 0)
        held_others = sum(max(0, c.reserved.get(o, 0) - c.cls_active.get(o, 0))
                          for o in c.reserved if o != klass and c.hold and c.cls_seen.get(o))
        held_others = min(held_others, max(0, c.max_active - 1))  # holding never shuts every slot
        free_total = max(0, c.max_active - c.active)
        free_k = free_total if a_k < r_k else max(0, free_total - held_others)
        prio = c.class_prio.get(klass, 99)
        backlog = c.cls_queued.get(klass, 0)
        if a_k >= r_k:  # only borrowing: higher-priority classes that are waiting are served first
            backlog += sum(n for o, n in c.cls_queued.items() if o != klass and c.class_prio.get(o, 99) < prio)
        cap = max(1, c.max_active - held_others)
        wait = max(0, backlog - free_k + 1) * svc / cap
    if c.node_budget and c.node_active >= c.node_budget:
        wait += svc  # the node as a whole is saturated
    return wait


def cold_cost(c: Candidate, p: Policy) -> float:
    return float(c.load_seconds) if (p.residency_aware and c.resident is False) else 0.0


def choose(cands: list[Candidate], policy: Policy, *, est_tokens: int, request_id: str,
           exclude: set[str] | frozenset = frozenset(), klass: str = workload.DEFAULT_CLASS) -> Decision:
    exclude = set(exclude)
    strategy = "fixed" if policy.frozen else policy.strategy
    pol = policy if not policy.frozen else Policy(**{**asdict(policy), "residency_aware": False})
    rows: list[dict] = []
    eligible: list[Candidate] = []
    for c in sorted(cands, key=lambda x: (x.name, x.id)):
        why = ineligible(c, pol, est_tokens, exclude)
        wait, cold = expected_wait(c, pol, klass), cold_cost(c, pol)
        rows.append({"deployment": c.name, "deployment_id": c.id, "node": c.node, "eligible": why is None,
                     "excluded_because": why, "queued": c.queued, "active": c.active,
                     "max_active": c.max_active, "weight": c.weight, "health": c.health,
                     "resident": c.resident, "expected_wait_s": round(wait, 2),
                     "cold_load_s": round(cold, 2), "class": klass, "class_reserved": c.reserved.get(klass, 0),
                     "class_active": c.cls_active.get(klass, 0), "class_queued": c.cls_queued.get(klass, 0),
                     "score": round((wait + cold) * (100 / max(c.weight, 1)), 3)})
        if why is None:
            eligible.append(c)
    prefix = "routing frozen: " if policy.frozen else ""

    if not eligible:
        # Explain the real obstacle. Deployments that are simply unavailable (disabled, draining, unhealthy,
        # excluded) say nothing about the request, so look only at the ones that could otherwise serve it.
        unavailable = ("disabled", "draining", "unhealthy", "weight is 0", "excluded after a failure",
                       "serves a different model build than the reference")
        relevant = [r["excluded_because"] for r in rows if r["excluded_because"] not in unavailable]
        if relevant and all(w.startswith("context window") for w in relevant):
            return Decision(None, strategy, prefix + "no deployment can hold this request", rows,
                            "context_length_exceeded",
                            f"The request (~{est_tokens} tokens) exceeds every deployment's context window.")
        if relevant and any(w == "queue is full" for w in relevant) and all(
                w == "queue is full" or w.startswith("context window") for w in relevant):
            return Decision(None, strategy, prefix + "every usable queue is full", rows, "queue_full",
                            "Every queue for this model is full. Retry shortly.")
        return Decision(None, strategy, prefix + "no eligible deployment", rows, "no_healthy_deployment",
                        "No healthy deployment is available for this model.")

    by_id = {r["deployment_id"]: r for r in rows}
    if strategy == "fixed":
        chosen = sorted(eligible, key=lambda c: (-c.weight, c.name, c.id))[0]
        reason = f"fixed: first eligible deployment by weight ({chosen.name})"
    elif strategy == "weighted":
        pool = sorted(eligible, key=lambda c: (c.name, c.id))
        total = sum(c.weight for c in pool)
        point = int(hashlib.sha256(request_id.encode()).hexdigest(), 16) % total
        chosen, acc = pool[-1], 0
        for c in pool:
            acc += c.weight
            if point < acc:
                chosen = c
                break
        reason = f"weighted: request hashed into {chosen.name}'s share ({chosen.weight} of {total})"
    else:  # least_loaded
        def key(c: Candidate):
            return (by_id[c.id]["score"], -c.weight)

        ranked = sorted(eligible, key=lambda c: (key(c), c.name, c.id))
        tied = [c for c in ranked if key(c) == key(ranked[0])]
        if len(tied) > 1:
            # Equal expected delay and weight: spread load instead of always favouring the same node.
            # Hashing the request id keeps the choice deterministic (reproducible in tests and the simulator).
            chosen = min(tied, key=lambda c: hashlib.sha256(f"{request_id}:{c.id}".encode()).hexdigest())
        else:
            chosen = ranked[0]
        r = by_id[chosen.id]
        reason = f"least_loaded: {chosen.name} has the lowest expected delay ({r['expected_wait_s']}s queue"
        if r["cold_load_s"]:
            reason += f" + {r['cold_load_s']}s cold model load"
        reason += ")"
        others = [c for c in ranked if c.id != chosen.id]
        if others:
            o = by_id[others[0].id]
            reason += f" vs {others[0].name} {o['expected_wait_s']}s" + (
                f" + {o['cold_load_s']}s cold" if o["cold_load_s"] else "")
        if len(tied) > 1:
            reason += f"; tied with {len(tied) - 1} other deployment(s), picked by request hash to spread load"
        if chosen.resident:
            reason += "; model already loaded there"
    return Decision(chosen, strategy, prefix + reason, rows)


# ------------------------------------------------------------------ state loading (DB and Redis)

def load_policy(db, model_id) -> Policy:
    frozen = bool((db.get(PlatformSetting, FREEZE_KEY) or PlatformSetting(value={})).value.get("frozen"))
    row = db.scalar(select(RoutingPolicy).where(RoutingPolicy.model_id == model_id,
                                                RoutingPolicy.status == "active")) \
        or db.scalar(select(RoutingPolicy).where(RoutingPolicy.model_id.is_(None),
                                                 RoutingPolicy.status == "active"))
    p = Policy(frozen=frozen)
    if row:
        cfg = row.configuration or {}
        p = Policy(id=str(row.id), version=row.version, strategy=row.strategy, frozen=frozen,
                   residency_aware=bool(cfg.get("residency_aware", True)),
                   exclude_mismatched=bool(cfg.get("exclude_mismatched", True)),
                   reference_digest=cfg.get("reference_digest"),
                   default_service_s=float(cfg.get("default_service_s", DEFAULT_SERVICE_S)))
    return p


def load_candidates(db, model_id) -> Loaded | None:
    st = get_settings()
    model = db.get(Model, model_id)
    if model is None:
        return None
    rows = list(db.execute(
        select(ModelDeployment, InferenceNode)
        .outerjoin(InferenceNode, InferenceNode.id == ModelDeployment.node_id)
        .where(ModelDeployment.model_id == model_id)))
    cands: list[Candidate] = []
    node_ids: set[str] = set()
    for d, n in rows:
        cfg = d.configuration or {}
        state = (n.state or {}) if n is not None else {}
        resident = None
        if n is not None and d.runtime_model and state.get("reachable"):
            resident = d.runtime_model in (state.get("resident") or {})
        recent = [ms for (ms,) in db.execute(
            select(InferenceRequest.duration_ms).where(
                InferenceRequest.deployment_id == d.id, InferenceRequest.status == "completed",
                InferenceRequest.duration_ms.is_not(None)).order_by(InferenceRequest.completed_at.desc()).limit(50))]
        if n is not None:
            node_ids.add(str(n.id))
        cands.append(Candidate(
            id=str(d.id), name=d.deployment_name, model_alias=model.model_alias,
            litellm_model=d.litellm_model or model.litellm_model, node_id=str(n.id) if n else None,
            node=n.name if n else None, node_enabled=n.enabled if n else True,
            node_budget=n.max_concurrent if n else None, weight=d.weight, enabled=d.enabled,
            draining=d.draining, health=d.health_status, context_limit=d.context_limit or model.context_limit,
            runtime_model=d.runtime_model, digest=(state.get("tags") or {}).get(d.runtime_model or ""),
            resident=resident, load_seconds=float(cfg.get("load_seconds") or DEFAULT_LOAD_S),
            max_active=int(cfg.get("max_active_requests") or st.default_max_active_requests),
            max_queue=int(cfg.get("max_queue_length") or st.default_max_queue_length),
            avg_service_s=(sum(recent) / len(recent) / 1000) if len(recent) >= 5 else None))
    wl = workload.load_config(db)
    for c in cands:
        c.reserved = workload.reserved_slots(c.max_active, wl)
        c.class_prio = {n: v["priority"] for n, v in wl.classes.items()}
        c.hold = wl.borrow_idle_after_s > 0
    node_deps: dict[str, list[str]] = {}
    if node_ids:
        for dep_id, node_id in db.execute(select(ModelDeployment.id, ModelDeployment.node_id).where(
                ModelDeployment.node_id.in_([uuid.UUID(x) for x in node_ids]))):
            node_deps.setdefault(str(node_id), []).append(str(dep_id))
    return Loaded(model_id, model.model_alias, cands, node_deps, load_policy(db, model_id), wl)


async def fill_load(sched, loaded: Loaded) -> None:
    """Read live queue depth and running counts from Redis."""
    for c in loaded.candidates:
        c.queued, c.active = await sched.depth(c.id), await sched.active_count(c.id)
        st = await sched.class_state(c.id, list(loaded.workload.classes))
        c.cls_active, c.cls_queued, c.cls_seen = st["active"], st["queued"], st["seen"]
    cache: dict[str, int] = {}
    for c in loaded.candidates:
        if c.node_id:
            if c.node_id not in cache:
                counts = [await sched.active_count(d) for d in loaded.node_deployments[c.node_id]]
                cache[c.node_id] = sum(counts)
            c.node_active = cache[c.node_id]


def record_decision(db, request_id, attempt: int, policy: Policy, decision: Decision,
                    failover_from: str | None = None) -> None:
    db.add(RoutingDecision(
        request_id=request_id, attempt=attempt, policy_id=uuid.UUID(policy.id) if policy.id else None,
        policy_version=policy.version or None, strategy=decision.strategy,
        chosen_deployment_id=uuid.UUID(decision.chosen.id) if decision.chosen else None,
        candidates=decision.candidates, reason=decision.reason,
        failover_from_deployment_id=uuid.UUID(failover_from) if failover_from else None))


_ = func
