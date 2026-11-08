"""Workload lanes: slot math, the atomic claim (real Redis), class assignment, and end-to-end isolation."""

import asyncio
import json

import pytest

from app.router import Candidate, Policy, choose, expected_wait
from app.workload import WorkloadConfig, claim_json, reserved_slots, validate
from tests.conftest import Api
from tests.test_gateway import bearer, gw, mk_key  # noqa: F401  (gw is a fixture)
from tests.test_scheduler import aclient, post, settings, until  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ============================================================ slot math and validation

def test_reserved_slots_default_split():
    cfg = WorkloadConfig()
    assert reserved_slots(4, cfg) == {"pms": 2, "coding": 1, "extraction": 1, "standard": 0}
    assert reserved_slots(8, cfg) == {"pms": 4, "coding": 2, "extraction": 2, "standard": 0}


def test_reserved_slots_never_exceed_capacity_and_ties_favour_priority():
    cfg = WorkloadConfig()
    for n in range(1, 40):
        got = reserved_slots(n, cfg)
        assert sum(got.values()) <= n and all(v >= 0 for v in got.values()), (n, got)
    # 2 slots: pms takes one; the last slot is a tie between coding and extraction -> extraction (priority 1)
    assert reserved_slots(2, cfg) == {"pms": 1, "coding": 0, "extraction": 1, "standard": 0}
    assert reserved_slots(1, cfg)["pms"] == 1  # the biggest remainder wins the only slot
    # partial reservation leaves slots unreserved
    part = validate({"pms": {"reserved_pct": 50, "priority": 1}}, 60)
    assert reserved_slots(4, part) == {"pms": 2, "standard": 0}


def test_validation():
    assert "standard" in validate({"a-b_1": {"reserved_pct": 10, "priority": 3}}, 30).classes  # catch-all added
    for bad in ({"Bad": {"reserved_pct": 1}}, {"x": {"reserved_pct": 1}}, {"ok1": {"reserved_pct": 101}},
                {"ok1": {"reserved_pct": 60}, "ok2": {"reserved_pct": 60}}, {"ok1": {"reserved_pct": 5, "priority": 0}}):
        with pytest.raises(ValueError):
            validate(bad, 60)
    with pytest.raises(ValueError):
        validate({"ok1": {"reserved_pct": 5}}, -1)
    cfg = json.loads(claim_json(4, WorkloadConfig()))
    assert cfg["_idle"] == 60 and cfg["pms"] == {"r": 2, "p": 2, "k": 0} and cfg["extraction"]["p"] == 1
    assert cfg["extraction"]["k"] == 1 and cfg["_burst"] == 4  # extraction is critical by default


def test_unknown_class_names_fall_back_to_standard():
    assert WorkloadConfig().known("pms") == "pms" and WorkloadConfig().known("nope") == "standard"
    assert WorkloadConfig().known(None) == "standard"


def cand(**kw):
    base = dict(id="d", name="d", model_alias="m", litellm_model="m", max_active=4, max_queue=100)
    return Candidate(**{**base, **kw})


def lanes(**kw):
    cfg = WorkloadConfig()
    state = dict(reserved=reserved_slots(4, cfg), class_prio={n: c["priority"] for n, c in cfg.classes.items()},
                 hold=True, cls_active={}, cls_queued={}, cls_seen={})
    return cand(**{**state, **kw})


def test_expected_wait_is_unchanged_when_no_lane_state_exists():
    c = cand(max_active=2, active=2, queued=2, avg_service_s=2.0)
    assert expected_wait(c, Policy()) == 3 / 2 * 2.0  # (2+2-2+1)/2 * 2s: the original formula


def test_a_held_reservation_makes_borrowing_classes_wait_but_not_its_owner():
    # extraction has filled the 2 slots it may use; pms is recently active (its 2 slots are held)
    c = lanes(active=2, cls_active={"extraction": 2}, cls_seen={"pms": True, "extraction": True},
              cls_queued={"extraction": 3}, avg_service_s=2.0)
    assert expected_wait(c, Policy(), "extraction") > 0          # backlog, and no borrowable slots left
    assert expected_wait(c, Policy(), "pms") == 0                 # its reserved slots are free for it
    # nobody has been seen recently: extraction can use every free slot, so its wait disappears
    idle = lanes(active=2, cls_active={"extraction": 2}, cls_seen={}, cls_queued={"extraction": 0})
    assert expected_wait(idle, Policy(), "extraction") == 0


def test_routing_sends_a_class_to_the_deployment_where_its_lane_is_free():
    swamped = lanes(id="a", name="a", active=2, cls_active={"extraction": 2}, cls_seen={"pms": True, "extraction": True},
                    cls_queued={"extraction": 6}, avg_service_s=3.0)
    quiet = lanes(id="b", name="b", cls_seen={}, avg_service_s=3.0)
    assert choose([swamped, quiet], Policy(), est_tokens=10, request_id="r", klass="extraction").chosen.name == "b"
    # pms sees both as free (its lane is reserved on both), so the choice is a tie that spreads
    names = {choose([swamped, quiet], Policy(), est_tokens=10, request_id=f"r{i}", klass="pms").chosen.name for i in range(40)}
    assert names == {"a", "b"}
    row = choose([swamped, quiet], Policy(), est_tokens=10, request_id="r", klass="pms").candidates[0]
    assert row["class"] == "pms" and row["class_reserved"] == 2


# ============================================================ the atomic claim, against real Redis

DEP, CFG = "dep1", WorkloadConfig()


def table(max_active=4, cfg=CFG):
    return claim_json(max_active, cfg)


async def enqueue(s, rid, klass, idle=60):
    seq = await s.enqueue(DEP, rid, 1000, klass, idle)
    assert seq > 0
    return seq


async def claim(s, rid, max_active=4, cfg=CFG):
    return await s.try_claim(DEP, rid, max_active, None, 0, table(max_active, cfg))


async def drain(s, ids, max_active=4, cfg=CFG):
    """Offer every waiting request a turn, repeatedly, like the real waiters do; return who got in."""
    got = []
    for _ in range(len(ids) + 1):
        for rid in ids:
            if rid not in got and await claim(s, rid, max_active, cfg):
                got.append(rid)
    return got


@pytest.mark.anyio
async def test_idle_classes_do_not_hold_capacity_so_one_class_can_use_everything(sched):
    ids = [f"e{i}" for i in range(6)]
    for r in ids:
        await enqueue(sched, r, "extraction")
    got = await drain(sched, ids)
    assert got == ids[:4]  # all four slots, in FIFO order; nobody else has been active
    st = await sched.class_state(DEP, list(CFG.classes))
    assert st["active"] == {"extraction": 4} and st["queued"]["extraction"] == 2


@pytest.mark.anyio
async def test_a_recently_active_class_keeps_its_reserved_slots(sched):
    await enqueue(sched, "p0", "pms")
    assert await claim(sched, "p0")
    await sched.release_active(DEP, "p0")  # pms finished, but it was active a moment ago
    ids = [f"e{i}" for i in range(6)]
    for r in ids:
        await enqueue(sched, r, "extraction")
    got = await drain(sched, ids)
    assert len(got) == 2  # extraction's own slot + one borrowed; pms's two slots stay held
    await enqueue(sched, "p1", "pms")
    await enqueue(sched, "p2", "pms")
    assert await claim(sched, "p1") and await claim(sched, "p2")  # the chatbot is not delayed at all
    assert await sched.active_count(DEP) == 4
    # ...and a third pms request waits, since pms is at its reservation and nothing is free
    await enqueue(sched, "p3", "pms")
    assert not await claim(sched, "p3")


NONCRIT = validate({"pms": {"reserved_pct": 50, "priority": 2}, "coding": {"reserved_pct": 25, "priority": 3},
                    "extraction": {"reserved_pct": 25, "priority": 1}}, 60)  # same lanes, nothing critical


@pytest.mark.anyio
async def test_holding_never_blocks_the_only_slot(sched):
    # one slot (a node that runs requests one at a time): pms was just active, yet extraction may use the slot
    await enqueue(sched, "p0", "pms")
    assert await claim(sched, "p0", 1, NONCRIT)
    await sched.release_active(DEP, "p0")
    await enqueue(sched, "e0", "extraction")
    assert await claim(sched, "e0", 1, NONCRIT)  # not held back: an idle GPU helps nobody
    await enqueue(sched, "p1", "pms")
    await enqueue(sched, "e1", "extraction")
    assert not await claim(sched, "p1", 1, NONCRIT)  # running work is never preempted
    await sched.release_active(DEP, "e0")
    assert not await claim(sched, "e1", 1, NONCRIT)  # older extraction loses the slot...
    assert await claim(sched, "p1", 1, NONCRIT)      # ...to pms, which is under its reservation


def test_expected_wait_never_assumes_every_slot_is_held():
    one = lanes(max_active=1, reserved=reserved_slots(1, WorkloadConfig()), cls_seen={"pms": True}, cls_queued={})
    assert expected_wait(one, Policy(), "extraction") == 0  # the lone slot is free for a borrower


@pytest.mark.anyio
async def test_with_holding_disabled_everything_is_borrowable(sched):
    cfg = validate({"pms": {"reserved_pct": 50, "priority": 2}, "extraction": {"reserved_pct": 25, "priority": 1}}, 0)
    await enqueue(sched, "p0", "pms", idle=0)
    assert await claim(sched, "p0", cfg=cfg)
    await sched.release_active(DEP, "p0")
    ids = [f"e{i}" for i in range(5)]
    for r in ids:
        await enqueue(sched, r, "extraction", idle=0)
    assert len(await drain(sched, ids, cfg=cfg)) == 4


@pytest.mark.anyio
async def test_the_owner_reclaims_the_next_free_slot_ahead_of_older_borrowers(sched):
    ids = [f"e{i}" for i in range(6)]
    for r in ids:
        await enqueue(sched, r, "extraction")
    assert await drain(sched, ids) == ids[:4]  # extraction has borrowed every slot
    await enqueue(sched, "p0", "pms")  # arrives later than all of those, but pms is under its reservation
    assert not await claim(sched, "p0")  # no free slot yet; running work is never preempted
    await sched.release_active(DEP, "e0")  # one extraction request finishes
    assert not await claim(sched, "e4")  # older queued extraction does NOT get the slot...
    assert await claim(sched, "p0")      # ...the owner reclaims it
    assert (await sched.class_state(DEP, ["pms", "extraction"]))["active"] == {"extraction": 3, "pms": 1}


@pytest.mark.anyio
async def test_priority_decides_between_classes_that_are_only_borrowing(sched):
    cfg = validate({"alpha": {"reserved_pct": 0, "priority": 2}, "beta": {"reserved_pct": 0, "priority": 1}}, 0)
    await enqueue(sched, "a1", "alpha", 0)   # older
    await enqueue(sched, "b1", "beta", 0)    # newer, but class beta has the better priority
    assert not await claim(sched, "a1", 1, cfg)
    assert await claim(sched, "b1", 1, cfg)
    await sched.release_active(DEP, "b1")
    assert await claim(sched, "a1", 1, cfg)


@pytest.mark.anyio
async def test_being_under_a_reservation_beats_priority_and_age(sched):
    # extraction (priority 1) already holds its one reserved slot, so a second extraction request is only borrowing
    await enqueue(sched, "e_run", "extraction")
    assert await claim(sched, "e_run", 4, NONCRIT)
    await enqueue(sched, "e1", "extraction")  # older, and extraction has the better priority...
    await enqueue(sched, "p0", "pms")         # ...but pms is still under its own reservation
    assert not await claim(sched, "e1", 4, NONCRIT)       # the slot is pms's to reclaim
    assert await claim(sched, "p0", 4, NONCRIT)
    assert await claim(sched, "e1", 4, NONCRIT)           # extraction may borrow what is left after pms's held slot


@pytest.mark.anyio
async def test_fifo_inside_a_class_is_strict(sched):
    ids = [f"x{i}" for i in range(4)]
    for r in ids:
        await enqueue(sched, r, "coding")
    assert not await claim(sched, "x2") and not await claim(sched, "x1")  # only the head may go
    assert await claim(sched, "x0") and await claim(sched, "x1") and await claim(sched, "x2")


@pytest.mark.anyio
async def test_release_is_idempotent_and_queue_operations_keep_lanes_consistent(sched):
    await enqueue(sched, "p0", "pms")
    assert await claim(sched, "p0")
    await sched.release_active(DEP, "p0")
    await sched.release_active(DEP, "p0")  # releasing twice must not drive the counter negative
    assert (await sched.class_state(DEP, ["pms"]))["active"].get("pms", 0) == 0
    await enqueue(sched, "c0", "coding")
    assert await sched.remove_queued(DEP, "c0") is True and await sched.remove_queued(DEP, "c0") is False
    assert (await sched.class_state(DEP, ["coding"]))["queued"]["coding"] == 0
    await enqueue(sched, "c1", "coding")
    assert await sched.move_queued(DEP, "dep2", "c1", 100, 60) > 0
    assert (await sched.class_state(DEP, ["coding"]))["queued"]["coding"] == 0
    assert (await sched.class_state("dep2", ["coding"]))["queued"]["coding"] == 1  # the class travels with it


# ============================================================ assignment, config and reporting (HTTP)

def test_class_assignment_resolution_and_authorization(gw):  # noqa: F811
    root, app_ = gw["root"], gw["app"]
    dev = Api(app_, "deva@x.io")
    pid = gw["proj_a"]["id"]
    root.post(f"/projects/{pid}/members", {"user_id": str(gw["dev_a"].id), "access_level": "developer"})
    k = mk_key(dev, project_id=pid)
    anon = Api(app_).c

    def hit():
        r = anon.post("/v1/chat/completions", headers=bearer(k["key"]), json={
            "model": "coder", "messages": [{"role": "user", "content": "x"}], "max_tokens": 5})
        assert r.status_code == 200
        return r.headers["x-nexora-class"]

    assert hit() == "standard"  # nothing assigned
    assert dev.patch(f"/admin/projects/{pid}/workload-class", {"workload_class": "pms"}).status_code == 403
    assert Api(app_, "ta@x.io").patch(f"/admin/keys/{k['id']}/workload-class", {"workload_class": "pms"}).status_code == 403
    assert root.patch(f"/admin/projects/{pid}/workload-class", {"workload_class": "pms"}).json()["workload_class"] == "pms"
    assert hit() == "pms"  # the project's class applies to its keys
    assert root.patch(f"/admin/keys/{k['id']}/workload-class", {"workload_class": "extraction"}).status_code == 200
    assert hit() == "extraction"  # a key's own class wins
    assert root.patch(f"/admin/keys/{k['id']}/workload-class", {"workload_class": None}).status_code == 200
    assert hit() == "pms"  # cleared: back to the project's
    assert root.patch(f"/admin/projects/{pid}/workload-class", {"workload_class": "nope"}).status_code == 422
    assert root.patch("/admin/keys/00000000-0000-0000-0000-000000000000/workload-class", {"workload_class": "pms"}).status_code == 404
    assert root.get(f"/projects/{pid}").json()["workload_class"] == "pms"
    assert {"project.workload_class", "key.workload_class"} <= {a["action"] for a in root.get("/admin/audit-logs?limit=100").json()}


def test_class_configuration_api(gw):  # noqa: F811
    root, dev = gw["root"], Api(gw["app"], "deva@x.io")
    assert dev.get("/admin/workload-classes").status_code == 403
    cfg = root.get("/admin/workload-classes").json()
    assert cfg["classes"]["extraction"]["priority"] == 1 and cfg["borrow_idle_after_s"] == 60
    body = {"classes": {k: {"reserved_pct": v["reserved_pct"], "priority": v["priority"]} for k, v in cfg["classes"].items()},
            "borrow_idle_after_s": 30}
    body["classes"]["extraction"]["reserved_pct"] = 40
    bad = {**body, "classes": {**body["classes"], "pms": {"reserved_pct": 50, "priority": 2}}}  # 50+25+40 = 115
    assert root.put("/admin/workload-classes", bad).status_code == 422
    body["classes"]["pms"]["reserved_pct"] = 35
    ok = root.put("/admin/workload-classes", body).json()
    assert ok["classes"]["extraction"]["reserved_pct"] == 40 and ok["borrow_idle_after_s"] == 30
    assert root.get("/admin/workload-classes").json()["classes"]["pms"]["reserved_pct"] == 35  # persisted
    assert ok["deployments"] and "extraction" in ok["deployments"][0]["reserved_slots"]
    shrunk = {**body, "classes": {k: v for k, v in body["classes"].items() if k != "coding"}}
    r = root.put("/admin/workload-classes", shrunk)
    assert r.status_code == 422 and r.json()["error"]["code"] == "CLASS_IN_USE"
    assert root.put("/admin/workload-classes", {"classes": {"Bad Name": {"reserved_pct": 1}}}).status_code == 422
    assert "workload.classes" in {a["action"] for a in root.get("/admin/audit-logs?limit=50").json()}


def test_usage_reports_break_down_and_filter_by_class(gw):  # noqa: F811
    root, app_ = gw["root"], gw["app"]
    ka, kb = mk_key(Api(app_, "deva@x.io")), mk_key(Api(app_, "devb@x.io"))
    root.patch(f"/admin/keys/{ka['id']}/workload-class", {"workload_class": "pms"})
    root.patch(f"/admin/keys/{kb['id']}/workload-class", {"workload_class": "extraction"})
    anon = Api(app_).c
    for key, n in ((ka, 2), (kb, 3)):
        for _ in range(n):
            assert anon.post("/v1/chat/completions", headers=bearer(key["key"]), json={
                "model": "coder", "messages": [{"role": "user", "content": "x"}], "max_tokens": 5}).status_code == 200
    rep = root.get("/usage/report").json()
    assert {c["workload_class"]: c["requests"] for c in rep["by_class"]} == {"pms": 2, "extraction": 3}
    assert root.get("/usage/report?workload_class=extraction").json()["totals"]["requests"] == 3
    hist = root.get("/usage/requests?workload_class=pms").json()
    assert len(hist) == 2 and {h["workload_class"] for h in hist} == {"pms"}
    assert "workload_class" in root.get("/usage/export").text.splitlines()[0]


# ============================================================ end to end: lanes protect the chatbot

@pytest.mark.anyio
async def test_a_chat_request_is_not_stuck_behind_an_extraction_burst(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=4)  # 4 slots: pms 2, coding 1, extraction 1
    root, app_, llm = gw["root"], gw["app"], gw["llm"]
    kp, ke = mk_key(Api(app_, "deva@x.io")), mk_key(Api(app_, "devb@x.io"))
    root.patch(f"/admin/keys/{kp['id']}/workload-class", {"workload_class": "pms"})
    root.patch(f"/admin/keys/{ke['id']}/workload-class", {"workload_class": "extraction"})
    async with aclient(app_) as c:
        first = await post(c, kp["key"], "pms-warmup", max_tokens=5)  # pms is now recently active
        assert first.status_code == 200 and first.headers["x-nexora-class"] == "pms"
        llm.gate = asyncio.Event()
        burst = [asyncio.create_task(post(c, ke["key"], f"ext-{i}", max_tokens=5)) for i in range(8)]
        try:
            await until(lambda: len([x for x in llm.order if x.startswith("ext-")]) == 2)
            await asyncio.sleep(0.5)
            assert len([x for x in llm.order if x.startswith("ext-")]) == 2  # extraction is held to 2 of the 4 slots
            chat = asyncio.create_task(post(c, kp["key"], "pms-chat", max_tokens=5))
            await until(lambda: "pms-chat" in llm.order)  # reaches the model while 6 extraction jobs still wait
            assert len([x for x in llm.order if x.startswith("ext-")]) == 2
        finally:
            llm.gate.set()
        results = await asyncio.gather(chat, *burst)
    assert all(r.status_code == 200 for r in results)
    assert results[0].headers["x-nexora-class"] == "pms"
    assert llm.max_inflight <= 4


# ============================================================ critical class (extraction goes first)

@pytest.mark.anyio
async def test_a_critical_class_goes_before_a_class_under_its_reservation(sched):
    # one slot; pms is under its reservation and older, yet critical extraction takes the next free slot
    await enqueue(sched, "x0", "extraction")
    assert await claim(sched, "x0", 1)
    await enqueue(sched, "p1", "pms")
    await enqueue(sched, "x1", "extraction")
    await sched.release_active(DEP, "x0")
    assert not await claim(sched, "p1", 1)  # pms waits...
    assert await claim(sched, "x1", 1)      # ...critical extraction goes first
    # contrast: with nothing critical, pms (under its reservation) wins - see the NONCRIT test above


@pytest.mark.anyio
async def test_critical_work_cannot_starve_the_others_beyond_the_burst(sched):
    # normal order (once the burst is used up) favours pms, so we can see the guard hand it a turn
    cfg = validate({"pms": {"reserved_pct": 50, "priority": 1},
                    "extraction": {"reserved_pct": 25, "priority": 2, "critical": True}}, 60, critical_burst=3)
    await enqueue(sched, "p1", "pms")
    ex = [f"x{i}" for i in range(8)]
    for r in ex:
        await enqueue(sched, r, "extraction")
    order = []
    for _ in range(5):
        for rid in ["p1", *ex]:
            if rid not in order and await claim(sched, rid, 1, cfg):
                order.append(rid)
                await sched.release_active(DEP, rid)
                break
    assert order[:3] == ["x0", "x1", "x2"]  # critical goes first, three times in a row...
    assert order[3] == "p1"                 # ...then the waiting class gets its turn
    assert order[4] == "x3"                 # and the burst counter restarts


@pytest.mark.anyio
async def test_critical_alone_is_unrestricted(sched):
    cfg = validate({"extraction": {"reserved_pct": 25, "priority": 1, "critical": True}}, 60, critical_burst=1)
    ids = [f"x{i}" for i in range(4)]
    for r in ids:
        await enqueue(sched, r, "extraction")
    got = []
    for rid in ids:
        assert await claim(sched, rid, 1, cfg)  # no one else waits, so the burst limit never applies
        await sched.release_active(DEP, rid)
        got.append(rid)
    assert got == ids


def test_router_counts_critical_backlog_ahead_of_other_classes():
    cfg = WorkloadConfig()
    base = lanes(max_active=4, cls_seen={"pms": True, "extraction": True}, avg_service_s=2.0,
                 class_crit={n: bool(c.get("critical")) for n, c in cfg.classes.items()})
    quiet = expected_wait(Candidate(**{**base.__dict__, "cls_queued": {}}), Policy(), "pms")
    swamped = expected_wait(Candidate(**{**base.__dict__, "cls_queued": {"extraction": 6}}), Policy(), "pms")
    assert swamped > quiet  # chat now sees critical extraction work as ahead of it
    crit_self = expected_wait(Candidate(**{**base.__dict__, "cls_queued": {"pms": 6}}), Policy(), "extraction")
    assert crit_self <= quiet + 1e-9  # extraction does not wait behind pms
