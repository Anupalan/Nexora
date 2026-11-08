"""Concurrency, FIFO, cancellation and recovery tests against a real Redis."""

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
import redis as sync_redis
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db as dbm
from app import inference, quota
from app.config import get_settings
from app.models import InferenceRequest, Model, ModelDeployment, utcnow
from tests.conftest import Api
from tests.test_gateway import bearer, gw, mk_key  # noqa: F401  (gw is a fixture)

pytestmark = []


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k.upper(), str(v))
    get_settings.cache_clear()


def aclient(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60)


async def post(c, key, label, **extra):
    return await c.post("/v1/chat/completions", headers=bearer(key),
                        json={"model": "coder", "messages": [{"role": "user", "content": label}], **extra})


async def until(cond, timeout=10.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        r = cond()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return r
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def rows(**where):
    with Session(dbm.get_engine()) as db:
        q = select(InferenceRequest).order_by(InferenceRequest.created_at)
        for k, v in where.items():
            q = q.where(getattr(InferenceRequest, k) == v)
        out = list(db.scalars(q))
        for r in out:
            db.expunge(r)
        return out


def dep_id():
    with Session(dbm.get_engine()) as db:
        return db.scalar(select(ModelDeployment.id).join(Model, Model.id == ModelDeployment.model_id)
                         .where(Model.model_alias == "coder"))


def rcounter(pattern):
    r = sync_redis.Redis.from_url(get_settings().redis_url)
    return sum(int(r.get(k)) for k in r.scan_iter(pattern))


@pytest.mark.anyio
async def test_fifo_order_single_slot(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=1)
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        tasks = []
        try:
            for i in range(5):
                tasks.append(asyncio.create_task(post(c, k, f"m{i}")))
                await until(lambda i=i: len(rows()) == i + 1)  # deterministic submission order
            await until(lambda: len(llm.order) == 1)
            assert await gw["sched"].depth(dep_id()) == 4
        finally:
            llm.gate.set()
        res = await asyncio.gather(*tasks)
    assert [r.status_code for r in res] == [200] * 5
    assert llm.order == [f"m{i}" for i in range(5)]  # claimed strictly in arrival order
    assert llm.max_inflight == 1
    assert [r.queue_sequence for r in rows()] == [1, 2, 3, 4, 5]


@pytest.mark.anyio
async def test_concurrent_producers_stay_ordered_and_bounded(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=2, default_max_queue_length=1000)
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    claims: list[str] = []
    real_claim = gw["sched"].try_claim

    async def recording_claim(dep, rid, max_active):
        ok = await real_claim(dep, rid, max_active)
        if ok:
            claims.append(str(rid))
        return ok

    gw["sched"].try_claim = recording_claim
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    n = 30
    async with aclient(gw["app"]) as c:
        tasks = [asyncio.create_task(post(c, k, f"m{i}")) for i in range(n)]
        try:
            await until(lambda: len(claims) == 2 and len(rows()) == n
                        and all(r.queue_sequence is not None for r in rows()))
            seqs = sorted(r.queue_sequence for r in rows())
            assert seqs == list(range(1, n + 1))  # unique, gap-free under concurrent producers
        finally:
            llm.gate.set()
        res = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in res)
    assert llm.max_inflight <= 2
    by_id = {str(r.id): r.queue_sequence for r in rows()}
    assert len(claims) == n
    claim_seqs = [by_id[c_] for c_ in claims]
    assert claim_seqs == sorted(claim_seqs) == list(range(1, n + 1))  # strict FIFO claims


@pytest.mark.anyio
async def test_quota_reservation_is_atomic_under_concurrency(gw):  # noqa: F811
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "requests_per_minute": 5})
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        res = await asyncio.gather(*[post(c, k, f"m{i}") for i in range(20)])
    codes = sorted(r.status_code for r in res)
    assert codes.count(200) == 5 and codes.count(429) == 15  # exactly the limit, no overrun
    assert len(rows()) == 5
    assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:req:minute:*") == 5


@pytest.mark.anyio
async def test_concurrency_quota_counts_in_flight(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=1)
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "max_concurrent": 2})
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        t = [asyncio.create_task(post(c, k, f"m{i}")) for i in range(2)]
        try:
            await until(lambda: len(rows()) == 2)
            r3 = await post(c, k, "m3")
            assert r3.status_code == 429 and "max concurrent" in r3.json()["error"]["message"]
        finally:
            llm.gate.set()
        assert [r.status_code for r in await asyncio.gather(*t)] == [200, 200]
        assert (await post(c, k, "m4")).status_code == 200  # slots released afterwards
    assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:conc") == 0


@pytest.mark.anyio
async def test_queue_full_rejects_with_retry_after(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=1, default_max_queue_length=1)
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        t = [asyncio.create_task(post(c, k, "m0"))]
        try:
            await until(lambda: len(llm.order) == 1)  # m0 holds the only slot
            t.append(asyncio.create_task(post(c, k, "m1")))
            await until(lambda: gw["sched"].depth(dep_id()))  # m1 now fills the 1-deep queue
            r = await post(c, k, "overflow")
            assert r.status_code == 429 and r.json()["error"]["code"] == "queue_full"
            assert r.headers["retry-after"] == "5"
        finally:
            llm.gate.set()
        await asyncio.gather(*t)
    statuses = [(r.status, r.error_code) for r in rows()]
    assert ("rejected", "queue_full") in statuses
    assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:*") == 0  # no quota configured -> nothing held


@pytest.mark.anyio
async def test_cancel_queued_releases_reservation_and_keeps_order(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=1)
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "requests_per_minute": 10})
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    dev = Api(gw["app"], "deva@x.io")
    k = mk_key(dev)["key"]
    async with aclient(gw["app"]) as c:
        t = []
        try:
            for i in range(3):
                t.append(asyncio.create_task(post(c, k, f"m{i}")))
                await until(lambda i=i: len(rows()) == i + 1)
            queued = rows()[1]
            assert dev.get(f"/queue/status/{queued.id}").json()["position"] == 1
            assert dev.post(f"/queue/{queued.id}/cancel").json()["status"] == "cancelled"
            assert dev.get(f"/queue/status/{queued.id}").json()["status"] == "cancelled"
            assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:req:minute:*") == 2  # its slot came back
        finally:
            llm.gate.set()
        res = await asyncio.gather(*t)
    assert [r.status_code for r in res] == [200, 409, 200]
    assert res[1].json()["error"]["code"] == "request_cancelled"
    assert llm.order == ["m0", "m2"]  # the cancelled one never ran; m2 moved up


@pytest.mark.anyio
async def test_cancel_running_request(gw):  # noqa: F811
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "max_concurrent": 3})
    dev = Api(gw["app"], "deva@x.io")
    k = mk_key(dev)["key"]
    async with aclient(gw["app"]) as c:
        t = asyncio.create_task(post(c, k, "long"))
        try:
            rid = (await until(lambda: [r for r in rows() if r.status == "running"]))[0].id
            assert dev.post(f"/queue/{rid}/cancel").json()["status"] == "cancelling"
            r = await asyncio.wait_for(t, 10)
        finally:
            llm.gate.set()
    assert r.status_code == 409 and r.json()["error"]["code"] == "request_cancelled"
    row = rows()[0]
    assert row.status == "cancelled" and row.completed_at is not None
    assert await gw["sched"].active_count(dep_id()) == 0
    assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:conc") == 0


@pytest.mark.anyio
async def test_client_disconnect_while_queued_frees_everything(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=1)
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "requests_per_minute": 10})
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        first = asyncio.create_task(post(c, k, "m0"))
        await until(lambda: len(llm.order) == 1)
        gone = asyncio.create_task(post(c, k, "m1"))
        await until(lambda: len(rows()) == 2)
        gone.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gone
        await until(lambda: rows()[1].status == "cancelled")
        assert rows()[1].error_code == "client_disconnected"
        assert await gw["sched"].depth(dep_id()) == 0
        assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:req:minute:*") == 1
        llm.gate.set()
        assert (await first).status_code == 200


@pytest.mark.anyio
async def test_queue_timeout(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=1, queue_timeout_seconds=1)
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        first = asyncio.create_task(post(c, k, "m0"))
        await until(lambda: len(llm.order) == 1)
        r = await post(c, k, "m1")
        llm.gate.set()
        await first
    assert r.status_code == 504 and r.json()["error"]["code"] == "queue_timeout"
    assert rows()[1].status == "failed" and rows()[1].error_code == "queue_timeout"


@pytest.mark.anyio
async def test_streaming_request_that_waits_gets_keepalive_then_stream(gw, monkeypatch):  # noqa: F811
    settings(monkeypatch, default_max_active_requests=1)
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        first = asyncio.create_task(post(c, k, "m0"))
        await until(lambda: len(llm.order) == 1)
        second = asyncio.create_task(post(c, k, "m1", stream=True))
        await until(lambda: len(rows()) == 2)
        llm.gate.set()
        await first
        r = await second
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.content.startswith(b": queued request_id=")  # SSE comment: ignored by OpenAI clients
    assert r.content.endswith(b"".join(llm.stream_chunks))  # then the untouched upstream stream
    assert rows()[1].status == "completed"


@pytest.mark.anyio
async def test_lease_expiry_recovery_does_not_replay(gw):  # noqa: F811
    llm = gw["llm"]
    llm.gate = asyncio.Event()
    sched = gw["sched"]
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        t = asyncio.create_task(post(c, k, "m0"))
        rid = (await until(lambda: [r for r in rows() if r.status == "running"]))[0].id
        await sched.redis.zadd(f"nexora:active:{dep_id()}", {str(rid): 0})  # lease long expired
        out = await inference.sweep_once(sched)
        assert out["leases"] == 1
        row = rows()[0]
        assert row.status == "failed" and row.error_code == "lease_expired"
        assert await sched.active_count(dep_id()) == 0
        llm.gate.set()
        await t
    assert len(llm.calls_to("/v1/chat/completions")) == 1  # never re-sent


@pytest.mark.anyio
async def test_redis_wipe_rebuilds_counters_and_fails_lost_requests(gw, sched):  # noqa: F811
    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "requests_per_day": 100, "tokens_per_day": 100000})
    llm = gw["llm"]
    k = mk_key(Api(gw["app"], "deva@x.io"))["key"]
    async with aclient(gw["app"]) as c:
        assert (await post(c, k, "done")).status_code == 200
        before = (rcounter(f"nexora:q:user:{gw['dev_a'].id}:req:day:*"),
                  rcounter(f"nexora:q:user:{gw['dev_a'].id}:tok:day:*"))
        assert before == (1, 4)
        llm.gate = asyncio.Event()
        t = asyncio.create_task(post(c, k, "inflight"))
        await until(lambda: len(llm.order) == 2)
        await sched.redis.flushall()  # Redis lost everything, including the in-flight request
        assert after_wipe(gw) == 0
        with Session(dbm.get_engine()) as db:
            assert await quota.ensure_counters(sched.redis, db) is True
        assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:req:day:*") == 2  # completed + in-flight
        with Session(dbm.get_engine()) as db:  # age the in-flight row past the sweeper grace period
            r = db.scalars(select(InferenceRequest).where(InferenceRequest.status == "running")).one()
            r.created_at = utcnow() - timedelta(minutes=5)
            db.commit()
        out = await inference.sweep_once(sched)
        assert out["lost"] == 1
        llm.gate.set()
        await t
    lost = next(r for r in rows() if r.error_code == "scheduler_restart")
    assert lost.status == "failed"
    assert [r.status for r in rows() if r.id != lost.id] == ["completed"]
    assert uuid.UUID(str(lost.id))


def after_wipe(world):
    return rcounter(f"nexora:q:user:{world['dev_a'].id}:*")


@pytest.mark.anyio
async def test_queued_cleanup_survives_repeated_cancellation(gw):  # noqa: F811
    """Regression: a disconnect cancels the response task at every await; cleanup must still finish."""
    import anyio

    gw["root"].post("/admin/quotas", {"scope_type": "user", "scope_id": str(gw["dev_a"].id),
                                      "requests_per_minute": 10})
    k = mk_key(Api(gw["app"], "deva@x.io"))
    sched = gw["sched"]
    adm = await inference.admit(sched, key_id=uuid.UUID(k["id"]), alias="coder", idem=None,
                                body={"model": "coder", "messages": [{"role": "user", "content": "x"}]})
    assert await sched.depth(adm.dep_id) == 1
    with anyio.CancelScope() as scope:
        scope.cancel()  # level-triggered: every unshielded await in here raises CancelledError
        await inference.drop_waiter(sched, adm, "cancelled", "client_disconnected", "gone")
    row = rows()[0]
    assert row.status == "cancelled" and row.error_code == "client_disconnected"
    assert await sched.depth(adm.dep_id) == 0
    assert rcounter(f"nexora:q:user:{gw['dev_a'].id}:req:minute:*") == 0  # reservation given back
