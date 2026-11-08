from sqlalchemy.orm import Session

import app.db as dbm
from app.models import Session as SessionRow
from tests.conftest import PW, Api, make_user


def test_login_logout_me(app_):
    make_user("a@x.io")
    api = Api(app_, "a@x.io")
    assert api.get("/auth/me").json()["email"] == "a@x.io"
    assert api.post("/auth/logout").status_code == 204
    assert api.get("/auth/me").status_code == 401


def test_cookie_flags_and_no_token_in_db(app_):
    make_user("a@x.io")
    api = Api(app_)
    r = api.c.post("/api/v1/auth/login", json={"email": "A@X.io", "password": PW})
    assert r.status_code == 200  # email is case-insensitive
    sc = r.headers.get_list("set-cookie")
    sess = next(c for c in sc if c.startswith("nexora_session="))
    assert "HttpOnly" in sess and "SameSite=lax" in sess
    token = api.c.cookies.get("nexora_session")
    with Session(dbm.get_engine()) as db:
        rows = db.query(SessionRow).all()
        assert len(rows) == 1 and rows[0].token_hash != token


def test_bad_credentials_generic_and_audited(app_):
    make_user("a@x.io")
    api = Api(app_)
    for body in ({"email": "a@x.io", "password": "wrong-password-1"},
                 {"email": "nobody@x.io", "password": "wrong-password-1"}):
        r = api.c.post("/api/v1/auth/login", json=body)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    make_user("root@x.io", "super_admin")
    root = Api(app_, "root@x.io")
    logs = root.get("/admin/audit-logs?action=auth.login_failed").json()
    assert len(logs) == 2
    assert PW not in str(logs) and "wrong-password" not in str(logs)


def test_lockout_after_repeated_failures(app_):
    make_user("a@x.io")
    api = Api(app_)
    for _ in range(5):
        api.c.post("/api/v1/auth/login", json={"email": "a@x.io", "password": "nope-nope-nope"})
    r = api.c.post("/api/v1/auth/login", json={"email": "a@x.io", "password": PW})
    assert r.status_code == 429


def test_csrf_required_on_unsafe_methods(app_):
    make_user("a@x.io")
    api = Api(app_, "a@x.io")
    r = api.c.post("/api/v1/auth/refresh")  # no header
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
    assert api.post("/auth/refresh").status_code == 200


def test_refresh_rotates_token(app_):
    make_user("a@x.io")
    api = Api(app_, "a@x.io")
    old = api.c.cookies.get("nexora_session")
    api.post("/auth/refresh")
    new = api.c.cookies.get("nexora_session")
    assert old != new
    stale = Api(app_)
    stale.c.cookies.set("nexora_session", old)
    assert stale.get("/auth/me").status_code == 401


def test_unauthenticated(app_):
    assert Api(app_).get("/users").status_code == 401
    assert Api(app_).get("/teams").status_code == 401


def test_suspend_revokes_sessions_and_blocks_login(world):
    root, app_ = world["root"], world["app"]
    victim = Api(app_, "deva@x.io")
    assert victim.get("/auth/me").status_code == 200
    assert root.post(f"/users/{world['dev_a'].id}/suspend").status_code == 200
    assert victim.get("/auth/me").status_code == 401  # existing session dies immediately
    r = Api(app_).c.post("/api/v1/auth/login", json={"email": "deva@x.io", "password": PW})
    assert r.status_code == 401
    assert root.post(f"/users/{world['dev_a'].id}/activate").status_code == 200
    Api(app_, "deva@x.io")


def test_cannot_suspend_self(world):
    root = world["root"]
    me = root.get("/auth/me").json()["id"]
    assert root.post(f"/users/{me}/suspend").status_code == 400


def test_validation_envelope(app_):
    r = Api(app_).c.post("/api/v1/auth/login", json={"email": "not-an-email"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.headers["x-request-id"]


def test_health(app_):
    from fastapi.testclient import TestClient

    c = TestClient(app_)
    assert c.get("/health/live").json() == {"status": "ok"}
    assert c.get("/health/ready").status_code == 200
