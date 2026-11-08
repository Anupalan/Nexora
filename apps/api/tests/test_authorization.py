from tests.conftest import Api


def test_developer_cannot_manage_users_or_teams(world):
    dev = Api(world["app"], "deva@x.io")
    assert dev.get("/users").status_code == 403
    assert dev.post("/users", {"name": "n", "email": "n@x.io", "password": "x" * 12}).status_code == 403
    assert dev.post("/teams", {"name": "Z"}).status_code == 403
    assert dev.get("/admin/audit-logs").status_code == 403
    assert dev.post(f"/users/{world['dev_b'].id}/suspend").status_code == 403


def test_cross_team_isolation(world):
    dev_a = Api(world["app"], "deva@x.io")
    # Team B and its project are invisible (404, not 403) to team A's developer.
    assert dev_a.get(f"/teams/{world['team_b']['id']}").status_code == 404
    assert dev_a.get(f"/projects/{world['proj_b']['id']}").status_code == 404
    assert dev_a.get(f"/users/{world['dev_b'].id}").status_code == 404
    teams = {t["name"] for t in dev_a.get("/teams").json()}
    assert teams == {"A"}


def test_team_admin_scope(world):
    ta = Api(world["app"], "ta@x.io")
    assert {t["name"] for t in ta.get("/teams").json()} == {"A"}
    assert {p["name"] for p in ta.get("/projects").json()} == {"pa"}
    assert ta.get(f"/projects/{world['proj_b']['id']}").status_code == 404
    # sees only members of own team
    emails = {u["email"] for u in ta.get("/users").json()}
    assert "devb@x.io" not in emails and "deva@x.io" in emails
    # can create a project in own team, not in another
    assert ta.post("/projects", {"team_id": world["team_a"]["id"], "name": "new"}).status_code == 201
    assert ta.post("/projects", {"team_id": world["team_b"]["id"], "name": "new"}).status_code == 404
    # cannot mint team admins or create users
    r = ta.post(f"/teams/{world['team_a']['id']}/members",
                {"user_id": str(world["outsider"].id), "membership_role": "admin"})
    assert r.status_code == 403
    assert ta.post("/users", {"name": "n", "email": "n@x.io", "password": "x" * 12}).status_code == 403


def test_project_membership_and_levels(world):
    ta = Api(world["app"], "ta@x.io")
    pid, dev = world["proj_a"]["id"], str(world["dev_a"].id)
    dev_api = Api(world["app"], "deva@x.io")
    # team member without project access cannot see the project
    assert dev_api.get(f"/projects/{pid}").status_code == 404
    assert ta.post(f"/projects/{pid}/members", {"user_id": dev, "access_level": "read_only"}).status_code == 201
    got = dev_api.get(f"/projects/{pid}").json()
    assert got["your_access"] == "read_only"
    # read-only cannot modify or manage members
    assert dev_api.patch(f"/projects/{pid}", {"description": "x"}).status_code == 403
    assert dev_api.post(f"/projects/{pid}/members", {"user_id": dev, "access_level": "owner"}).status_code == 403
    # maintainer can edit but cannot grant owner
    ta.post(f"/projects/{pid}/members", {"user_id": dev, "access_level": "maintainer"})
    assert dev_api.patch(f"/projects/{pid}", {"description": "ok"}).status_code == 200
    r = dev_api.post(f"/projects/{pid}/members", {"user_id": dev, "access_level": "owner"})
    assert r.status_code == 403


def test_project_member_must_be_in_team(world):
    ta = Api(world["app"], "ta@x.io")
    r = ta.post(f"/projects/{world['proj_a']['id']}/members",
                {"user_id": str(world["dev_b"].id), "access_level": "developer"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOT_TEAM_MEMBER"


def test_removing_team_member_drops_project_access(world):
    root, pid = world["root"], world["proj_a"]["id"]
    dev = str(world["dev_a"].id)
    root.post(f"/projects/{pid}/members", {"user_id": dev, "access_level": "developer"})
    dev_api = Api(world["app"], "deva@x.io")
    assert dev_api.get(f"/projects/{pid}").status_code == 200
    assert root.delete(f"/teams/{world['team_a']['id']}/members/{dev}").status_code == 200
    assert dev_api.get(f"/projects/{pid}").status_code == 404


def test_duplicate_conflicts(world):
    root = world["root"]
    assert root.post("/teams", {"name": "A"}).status_code == 409
    r = root.post("/users", {"name": "x", "email": "DEVA@x.io", "password": "x" * 12})
    assert r.status_code == 409
    r = root.post(f"/teams/{world['team_a']['id']}/members", {"user_id": str(world["dev_a"].id)})
    assert r.status_code == 409


def test_password_policy(world):
    r = world["root"].post("/users", {"name": "x", "email": "n@x.io", "password": "short"})
    assert r.status_code == 422


def test_audit_events_for_privileged_actions(world):
    root = world["root"]
    root.post(f"/users/{world['dev_b'].id}/suspend")
    actions = {a["action"] for a in root.get("/admin/audit-logs?limit=200").json()}
    assert {"team.create", "team.member_add", "project.create", "user.suspend",
            "auth.login"} <= actions
    log = next(a for a in root.get("/admin/audit-logs?action=user.suspend").json())
    assert log["actor_id"] and log["entity_id"] == str(world["dev_b"].id)


def test_role_change_guard(world):
    root = world["root"]
    me = root.get("/auth/me").json()["id"]
    assert root.patch(f"/users/{me}", {"role": "developer"}).status_code == 400
    r = root.patch(f"/users/{world['dev_a'].id}", {"role": "team_admin"})
    assert r.status_code == 200 and r.json()["role"] == "team_admin"
