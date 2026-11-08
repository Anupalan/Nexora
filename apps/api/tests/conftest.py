import json
import os
import shutil
import subprocess
import time

import httpx
import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db as dbm
from app import inference
from app.cli import seed_roles
from app.config import get_settings
from app.inference import LoopRedis
from app.litellm import LiteLLMClient, get_litellm
from app.models import Role, User
from app.scheduler import Scheduler
from app.security import hash_password

PW = "correct-horse-battery"
MASTER = "sk-master-secret"
REDIS_PORT = 6391


@pytest.fixture(scope="session")
def redis_url():
    """A real Redis (Lua scripts are part of the behaviour under test). CI sets TEST_REDIS_URL."""
    url = os.environ.get("TEST_REDIS_URL")
    proc = None
    if not url:
        exe = shutil.which("redis-server")
        if not exe:
            pytest.skip("redis-server not available and TEST_REDIS_URL not set")
        proc = subprocess.Popen([exe, "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"redis://127.0.0.1:{REDIS_PORT}/0"
    c = sync_redis.Redis.from_url(url)
    for _ in range(50):
        try:
            c.ping()
            break
        except sync_redis.ConnectionError:
            time.sleep(0.1)
    else:
        pytest.skip("could not reach redis")
    yield url
    if proc:
        proc.terminate()


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, redis_url):
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("SWEEPER_INTERVAL_SECONDS", "3600")
    get_settings.cache_clear()
    sync_redis.Redis.from_url(redis_url).flushall()
    dbm.configure_engine(f"sqlite:///{tmp_path}/test.db")
    dbm.Base.metadata.create_all(dbm.get_engine())
    with Session(dbm.get_engine()) as db:
        seed_roles(db)
    st = get_settings()
    sched = Scheduler(LoopRedis(redis_url), lease_seconds=st.lease_seconds,
                      waiter_ttl_seconds=st.waiter_ttl_seconds)
    inference.set_scheduler(sched)
    yield sched
    inference.set_scheduler(None)
    for c in _CLIENTS:
        c.__exit__(None, None, None)
    _CLIENTS.clear()
    get_settings.cache_clear()


@pytest.fixture
def sched(_env) -> Scheduler:
    return _env


_CLIENTS: list = []


@pytest.fixture
def app_():
    from app.main import app

    return app


def make_user(email: str, role: str = "developer", name: str | None = None) -> User:
    with Session(dbm.get_engine()) as db:
        r = db.scalar(select(Role).where(Role.name == role))
        u = User(name=name or email.split("@")[0], email=email, role_id=r.id,
                 password_hash=hash_password(PW))
        db.add(u)
        db.commit()
        db.refresh(u)
        return u


class Api:
    """Test client wrapper that logs in and echoes the CSRF token."""

    def __init__(self, app, email: str | None = None, password: str = PW):
        self.c = TestClient(app, base_url="http://testserver")
        self.c.__enter__()  # one event loop per client (runs the app lifespan too)
        _CLIENTS.append(self.c)
        if email:
            r = self.c.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert r.status_code == 200, r.text

    def _h(self):
        t = self.c.cookies.get("nexora_csrf")
        return {"x-csrf-token": t} if t else {}

    def get(self, url, **kw):
        return self.c.get(f"/api/v1{url}", **kw)

    def post(self, url, json=None, **kw):
        return self.c.post(f"/api/v1{url}", json=json, headers=self._h(), **kw)

    def patch(self, url, json=None):
        return self.c.patch(f"/api/v1{url}", json=json, headers=self._h())

    def delete(self, url):
        return self.c.delete(f"/api/v1{url}", headers=self._h())


@pytest.fixture
def world(app_):
    """Super admin, two teams (A, B), a team admin for A, devs in each, one project each."""
    make_user("root@x.io", "super_admin")
    make_user("ta@x.io", "team_admin")
    dev_a = make_user("deva@x.io")
    dev_b = make_user("devb@x.io")
    outsider = make_user("out@x.io")
    root = Api(app_, "root@x.io")
    team_a = root.post("/teams", {"name": "A"}).json()
    team_b = root.post("/teams", {"name": "B"}).json()
    ta = root.get("/users").json()
    ta_id = next(u["id"] for u in ta if u["email"] == "ta@x.io")
    root.post(f"/teams/{team_a['id']}/members", {"user_id": ta_id, "membership_role": "admin"})
    root.post(f"/teams/{team_a['id']}/members", {"user_id": str(dev_a.id)})
    root.post(f"/teams/{team_b['id']}/members", {"user_id": str(dev_b.id)})
    proj_a = root.post("/projects", {"team_id": team_a["id"], "name": "pa"}).json()
    proj_b = root.post("/projects", {"team_id": team_b["id"], "name": "pb"}).json()
    return dict(root=root, team_a=team_a, team_b=team_b, proj_a=proj_a, proj_b=proj_b,
                dev_a=dev_a, dev_b=dev_b, outsider=outsider, ta_id=ta_id, app=app_)


# ---- fake LiteLLM ---------------------------------------------------------------------
class FakeLiteLLM:
    def __init__(self):
        self.calls: list[dict] = []
        self.keys: dict[str, list[str]] = {}  # issued virtual key -> allowed model names
        self.aliases: dict[str, str] = {}  # key_alias -> key (real LiteLLM: aliases must be unique)
        self.tokens: dict[str, str] = {}  # hashed token -> key
        self.down = False
        self.gate = None  # asyncio.Event: inference calls wait on it until set
        self.inflight = 0
        self.max_inflight = 0
        self.order: list = []  # labels (first message content) in the order calls reached upstream
        self.stream_chunks = [b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
                              b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
                              b"data: [DONE]\n\n"]

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        body = json.loads(req.content) if req.content else {}
        self.calls.append({"path": req.url.path, "auth": req.headers.get("authorization"), "body": body})
        if req.url.path == "/key/generate":
            alias = body.get("key_alias")
            if alias and alias in self.aliases:
                return httpx.Response(400, json={"error": {"message": f"Key with alias '{alias}' already exists."}})
            self.keys[body["key"]] = body.get("models", [])
            tok = "hash-" + body["key"][-8:]
            self.tokens[tok] = body["key"]
            if alias:
                self.aliases[alias] = body["key"]
            return httpx.Response(200, json={"key": body["key"], "token": tok})
        if req.url.path == "/key/delete":
            for ident in body.get("keys", []):
                key = self.tokens.get(ident, ident)
                self.keys.pop(key, None)
                self.aliases = {a: k for a, k in self.aliases.items() if k != key}
            return httpx.Response(200, json={})
        if req.url.path == "/key/update":
            return httpx.Response(200, json={})
        if req.url.path.endswith("/completions"):
            # Like real LiteLLM: the key's model list is checked against the model in the request.
            caller = (req.headers.get("authorization") or "").removeprefix("Bearer ")
            if caller.startswith("sk-nx-") and caller not in self.keys:
                return httpx.Response(401, json={"error": {"message": "Invalid API key"}})
            if caller in self.keys and body.get("model") not in self.keys[caller]:
                return httpx.Response(401, json={"error": {"message": "key not allowed to access model"}})
            if body.get("stream"):
                # A real stream (not pre-read content) so the proxy's streaming path is exercised.
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      stream=httpx.ByteStream(b"".join(self.stream_chunks)))
            return httpx.Response(200, json={"id": "c1", "model": body["model"],
                                             "choices": [{"message": {"content": "hi"}}],
                                             "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
        return httpx.Response(404)

    def paths(self):
        return [c["path"] for c in self.calls]

    def calls_to(self, path: str) -> list[dict]:
        return [c for c in self.calls if c["path"] == path]


class DualTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    def __init__(self, fake: FakeLiteLLM):
        self.fake = fake

    def handle_request(self, request):
        return self.fake.handler(request)

    async def handle_async_request(self, request):
        f = self.fake
        if not request.url.path.endswith("/completions"):
            return f.handler(request)
        body = json.loads(request.content)
        msgs = body.get("messages") or [{}]
        f.order.append(msgs[0].get("content"))
        f.inflight += 1
        f.max_inflight = max(f.max_inflight, f.inflight)
        try:
            if f.gate is not None:
                await f.gate.wait()
            return f.handler(request)
        finally:
            f.inflight -= 1


@pytest.fixture
def llm(app_, sched):
    fake = FakeLiteLLM()
    client = LiteLLMClient("http://litellm", MASTER, 30, transport=DualTransport(fake))
    app_.dependency_overrides[get_litellm] = lambda: client
    yield fake
    app_.dependency_overrides.pop(get_litellm, None)
