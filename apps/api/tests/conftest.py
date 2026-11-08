import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.db as dbm
from app.cli import seed_roles
from app.config import get_settings
from app.models import Role, User
from app.security import hash_password

PW = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "false")
    get_settings.cache_clear()
    dbm.configure_engine("sqlite://")
    dbm.Base.metadata.create_all(dbm.get_engine())
    with Session(dbm.get_engine()) as db:
        seed_roles(db)
    yield
    get_settings.cache_clear()


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
import json  # noqa: E402

import httpx  # noqa: E402

from app.litellm import LiteLLMClient, get_litellm  # noqa: E402

MASTER = "sk-master-secret"


class FakeLiteLLM:
    def __init__(self):
        self.calls: list[dict] = []
        self.down = False
        self.stream_chunks = [b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
                              b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
                              b"data: [DONE]\n\n"]

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        body = json.loads(req.content) if req.content else {}
        self.calls.append({"path": req.url.path, "auth": req.headers.get("authorization"), "body": body})
        if req.url.path == "/key/generate":
            return httpx.Response(200, json={"key": body["key"], "token": "hash-" + body["key"][-8:]})
        if req.url.path in ("/key/update", "/key/delete"):
            return httpx.Response(200, json={})
        if req.url.path.endswith("/completions"):
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


@pytest.fixture
def llm(app_):
    fake = FakeLiteLLM()
    client = LiteLLMClient("http://litellm", MASTER, 30, transport=httpx.MockTransport(fake.handler))
    app_.dependency_overrides[get_litellm] = lambda: client
    yield fake
    app_.dependency_overrides.pop(get_litellm, None)
