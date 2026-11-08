"""Routing: pure decision logic first, then integration with real Redis and the fake LiteLLM/Ollama."""

import asyncio
import random

import httpx

from app import inference
from app.router import Candidate, Policy, choose
from tests.conftest import Api
from tests.test_gateway import bearer, mk_key
from tests.test_scheduler import aclient, post, until


def cand(name, **kw):
    base = dict(id=f"id-{name}", name=name, model_alias="m", litellm_model=f"m-{name}", max_active=2, max_queue=10)
    return Candidate(**{**base, **kw})


def pick(cands, policy=None, **kw):
    return choose(cands, policy or Policy(), est_tokens=kw.pop("est_tokens", 100), request_id=kw.pop("rid", "r1"), **kw)


# ============================================================ pure decision logic

def test_ineligible_reasons_are_recorded_and_never_chosen():
    cs = [cand("a", enabled=False), cand("b", draining=True), cand("c", health="unhealthy"),
          cand("d", weight=0), cand("e", context_limit=50), cand("f", queued=10), cand("ok")]
    d = pick(cs)
    assert d.chosen.name == "ok"
    why = {r["deployment"]: r["excluded_because"] for r in d.candidates}
    assert why["a"] == "disabled" and why["b"] == "draining" and why["c"] == "unhealthy"
    assert why["d"] == "weight is 0" and "context window 50" in why["e"] and why["f"] == "queue is full"
    assert why["ok"] is None


def test_node_disabled_excludes_all_its_deployments():
    assert pick([cand("a", node_enabled=False)]).error_code == "no_healthy_deployment"


def test_error_codes():
    assert pick([cand("a", context_limit=10), cand("b", context_limit=20)]).error_code == "context_length_exceeded"
    assert pick([cand("a", queued=10), cand("b", queued=10)]).error_code == "queue_full"
    assert pick([cand("a", health="unhealthy")]).error_code == "no_healthy_deployment"
    assert pick([]).error_code == "no_healthy_deployment"
    # unavailable deployments do not hide the real reason: this request is simply too large
    assert pick([cand("off", enabled=False), cand("down", health="unhealthy"),
                 cand("small", context_limit=10)]).error_code == "context_length_exceeded"
    assert pick([cand("full", queued=10), cand("small", context_limit=10)]).error_code == "queue_full"
    # a deployment that is merely too small does not mask a healthy one
    assert pick([cand("small", context_limit=10), cand("big", context_limit=1000)]).chosen.name == "big"


def test_least_loaded_prefers_less_busy_deployment():
    busy = cand("busy", active=2, queued=3)
    idle = cand("idle", active=0)
    assert pick([busy, idle]).chosen.name == "idle"
    assert pick([cand("a", active=1), cand("b", active=2, queued=1)]).chosen.name == "a"


def test_residency_aware_prefers_a_loaded_model_over_a_cold_idle_one():
    warm_busyish = cand("warm", resident=True, active=2, queued=0, avg_service_s=2.0)  # 1 ahead * 1s = 1s wait
    cold_idle = cand("cold", resident=False, load_seconds=10)
    d = pick([cold_idle, warm_busyish])
    assert d.chosen.name == "warm" and "already loaded" in d.reason
    # ...but a long queue on the warm node makes the cold load worth paying
    warm_swamped = cand("warm", resident=True, active=2, queued=40, avg_service_s=2.0)
    assert pick([cold_idle, warm_swamped]).chosen.name == "cold"
    # residency awareness can be switched off
    assert pick([cold_idle, warm_busyish], Policy(residency_aware=False)).chosen.name == "cold"


def test_node_budget_saturation_counts_against_a_deployment():
    on_full_node = cand("a", node_budget=2, node_active=2)
    on_free_node = cand("b", node_budget=2, node_active=0)
    assert pick([on_full_node, on_free_node]).chosen.name == "b"


def test_weights_break_ties_and_scale_load():
    assert pick([cand("a", weight=100), cand("b", weight=300)]).chosen.name == "b"  # tie on load -> higher weight
    # equal raw load, but weight 400 makes b look 4x less loaded
    assert pick([cand("a", active=2, weight=100), cand("b", active=2, weight=400)]).chosen.name == "b"


def test_decisions_are_deterministic_regardless_of_input_order():
    base = [cand(n, active=i % 2, weight=100 + i) for i, n in enumerate("abcdef")]
    first = pick(base).chosen.name
    for seed in range(20):
        shuffled = base[:]
        random.Random(seed).shuffle(shuffled)
        assert pick(shuffled).chosen.name == first


def test_equal_candidates_spread_load_deterministically():
    cs = [cand("a"), cand("b"), cand("c")]
    counts = {"a": 0, "b": 0, "c": 0}
    for i in range(3000):
        d = pick(cs, rid=f"req-{i}")
        counts[d.chosen.name] += 1
        assert d.chosen.name == pick(list(reversed(cs)), rid=f"req-{i}").chosen.name  # same id, same answer
    assert all(800 < n < 1200 for n in counts.values()), counts  # roughly a third each
    assert "spread load" in pick(cs, rid="x").reason
    # a real difference still wins over the tie-break
    assert pick([cand("a", active=2), cand("b"), cand("c", weight=50)], rid="x").chosen.name == "b"  # a is at capacity
    assert {pick([cand("a"), cand("b"), cand("c", weight=50)], rid=f"r{i}").chosen.name for i in range(60)} == {"a", "b"}


def test_fixed_strategy_uses_weight_then_name():
    p = Policy(strategy="fixed")
    assert pick([cand("b"), cand("a")], p).chosen.name == "a"
    assert pick([cand("a", weight=50), cand("b", weight=200)], p).chosen.name == "b"
    # load is ignored by fixed
    assert pick([cand("a", active=2, queued=9), cand("b")], p).chosen.name == "a"


def test_weighted_strategy_is_reproducible_proportional_and_skips_zero_weight():
    p = Policy(strategy="weighted")
    cs = [cand("a", weight=70), cand("b", weight=30), cand("z", weight=0)]
    assert pick(cs, p, rid="same").chosen.name == pick(cs, p, rid="same").chosen.name
    counts = {"a": 0, "b": 0}
    for i in range(2000):
        name = pick(cs, p, rid=f"req-{i}").chosen.name
        counts[name] += 1  # 'z' would KeyError: weight 0 is never chosen
    assert 0.62 < counts["a"] / 2000 < 0.78


def test_freeze_forces_fixed_and_ignores_residency():
    cold_first = cand("a", resident=False)           # fixed picks 'a' by name
    warm_second = cand("b", resident=True, active=0)
    live = pick([cold_first, warm_second], Policy(strategy="least_loaded"))
    assert live.chosen.name == "b"
    frozen = pick([cold_first, warm_second], Policy(strategy="least_loaded", frozen=True))
    assert frozen.chosen.name == "a" and frozen.strategy == "fixed" and frozen.reason.startswith("routing frozen")


def test_reference_digest_excludes_a_different_build_only_when_configured():
    a, b = cand("a", digest="aaa"), cand("b", digest="bbb")
    assert pick([a, b]).error_code is None                      # no reference: both stay eligible
    d = pick([a, b], Policy(reference_digest="bbb"))
    assert d.chosen.name == "b"
    assert next(r for r in d.candidates if r["deployment"] == "a")["excluded_because"].startswith("serves a different")
    both = {pick([a, b], Policy(reference_digest="bbb", exclude_mismatched=False), rid=f"r{i}").chosen.name
            for i in range(60)}
    assert both == {"a", "b"}  # exclusion switched off: the other build is routable again


def test_exclude_set_models_a_failed_deployment():
    cs = [cand("a"), cand("b")]
    assert pick(cs, exclude={"id-a"}).chosen.name == "b"
    assert pick(cs, exclude={"id-a", "id-b"}).error_code == "no_healthy_deployment"


def test_reason_explains_the_numbers():
    d = pick([cand("warm", resident=True), cand("cold", resident=False, load_seconds=10)])
    assert "warm" in d.reason and "10.0s cold" in d.reason
    assert all({"deployment", "queued", "active", "expected_wait_s", "cold_load_s", "score"} <= set(r)
               for r in d.candidates)


_ = (asyncio, httpx, inference, Api, bearer, mk_key, aclient, post, until)


def test_faster_node_wins_at_equal_load():
    from app.router import choose, Policy
    slow = cand("slow", resident=True, svc_by_class={"pms": 6.0}, avg_service_s=6.0)
    fast = cand("fast", resident=True, svc_by_class={"pms": 1.5}, avg_service_s=6.0)
    for i in range(20):
        d = choose([slow, fast], Policy(), est_tokens=10, request_id=f"r{i}", klass="pms")
        assert d.chosen.name == "fast"
        assert d.candidates[0]["service_measured"] is True


def test_class_history_not_polluted_by_other_work():
    from app.router import choose, Policy
    # a node whose all-class average is dragged up by long extraction jobs is still fast for chat
    a = cand("a", resident=True, avg_service_s=20.0, svc_by_class={"pms": 1.0, "extraction": 40.0})
    b = cand("b", resident=True, avg_service_s=3.0, svc_by_class={"pms": 3.0})
    assert choose([a, b], Policy(), est_tokens=10, request_id="x", klass="pms").chosen.name == "a"
    assert choose([a, b], Policy(), est_tokens=10, request_id="x", klass="extraction").chosen.name == "b"


def test_unmeasured_node_is_not_shunned():
    from app.router import choose, Policy
    measured = cand("m", resident=True, avg_service_s=2.0)
    fresh = cand("n", resident=True)  # no history: borrows the measured median, so it ties and gets traffic
    names = {choose([measured, fresh], Policy(), est_tokens=10, request_id=f"r{i}").chosen.name for i in range(30)}
    assert names == {"m", "n"}
