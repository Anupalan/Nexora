"""End-to-end demo of the minimum business workflow (roadmap §51) against RUNNING servers.

    python tests/e2e/demo.py [--base http://localhost:3000] [--creds .dev-credentials.txt]

Needs: web+API running, Postgres, Redis, and an OpenAI-compatible backend (infra/mock-llm or
LiteLLM). Reads the super-admin email/password from the credentials file (never printed) and
creates uniquely named users/teams so it can be re-run. Exits non-zero on the first failed step.
"""

import argparse
import json
import re
import sys
import uuid

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:3000")
ap.add_argument("--creds", default=".dev-credentials.txt")
ap.add_argument("--litellm-model", default="qwen-coder",
                help="LiteLLM model_name behind the `coder` alias (NOT the openai/... provider path)")
args = ap.parse_args()

creds = dict(re.findall(r"^(email|password):\s*(.+)$", open(args.creds).read(), re.M))
RUN = uuid.uuid4().hex[:6]
DEV_EMAIL, DEV_PW = f"dev-{RUN}@example.com", f"demo-{uuid.uuid4().hex}"
step = 0


def ok(cond: bool, msg: str, detail: str = "") -> None:
    global step
    step += 1
    print(f"{'PASS' if cond else 'FAIL'}  {step:>2}. {msg}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


class Session:
    def __init__(self):
        self.c = httpx.Client(base_url=args.base, timeout=60)

    def login(self, email, pw):
        r = self.c.post("/api/v1/auth/login", json={"email": email, "password": pw})
        return r

    def call(self, method, path, **kw):
        h = {"x-csrf-token": self.c.cookies.get("nexora_csrf", "")} if method != "GET" else {}
        return self.c.request(method, f"/api/v1{path}", headers=h, **kw)


admin, dev = Session(), Session()
r = admin.login(creds["email"], creds["password"])
ok(r.status_code == 200 and r.json()["role"] == "super_admin", "1. Super Admin logs into Nexora", r.text[:120])

team = admin.call("POST", "/teams", json={"name": f"Demo Team {RUN}"}).json()
ok("id" in team, "2. Super Admin creates a team", str(team))

r = admin.call("POST", "/users", json={"name": "Demo Dev", "email": DEV_EMAIL, "password": DEV_PW,
                                       "role": "developer"})
ok(r.status_code == 201, "3. Super Admin creates a developer", r.text[:150])
dev_id = r.json()["id"]
ok(admin.call("POST", f"/teams/{team['id']}/members", json={"user_id": dev_id}).status_code == 201,
   "   ...and adds them to the team")

proj = admin.call("POST", "/projects", json={"team_id": team["id"], "name": f"Demo Project {RUN}"}).json()
ok("id" in proj, "4. Super Admin creates a project", str(proj))

r = admin.call("POST", f"/projects/{proj['id']}/members", json={"user_id": dev_id, "access_level": "developer"})
ok(r.status_code == 201, "5. Super Admin assigns the developer to the project", r.text[:150])

models = admin.call("GET", "/admin/models").json()
existing = next((m for m in models if m["model_alias"] == "coder"), None)
if existing is None:
    admin.call("POST", "/admin/models", json={"name": "Coder", "model_alias": "coder",
                                              "litellm_model": args.litellm_model})
else:  # make re-runs deterministic: point the alias at the right LiteLLM model
    admin.call("PATCH", f"/admin/models/{existing['id']}", json={"litellm_model": args.litellm_model})
r = admin.call("POST", "/admin/quotas", json={"scope_type": "user", "scope_id": dev_id,
                                              "requests_per_day": 3, "tokens_per_day": 100000})
ok(r.status_code == 201, "6. Super Admin assigns request and token quotas (3 requests/day)", r.text[:150])

r = dev.login(DEV_EMAIL, DEV_PW)
ok(r.status_code == 200 and r.json()["role"] == "developer", "7. Developer logs in", r.text[:120])

r = dev.call("POST", "/keys", json={"name": "demo-laptop", "project_id": proj["id"]})
ok(r.status_code == 201 and r.json()["key"].startswith("sk-nx-"), "8. Developer generates an API key", r.text[:200])
KEY = r.json()["key"]
ok("key" not in dev.call("GET", f"/keys/{r.json()['id']}").json(), "   ...secret is not retrievable again")

# 9-14: an OpenAI-compatible client (what OpenCode/Cline/Aider do) pointed at {base}/v1
oa = httpx.Client(base_url=f"{args.base}/v1", headers={"Authorization": f"Bearer {KEY}"}, timeout=60)
ok(any(m["id"] == "coder" for m in oa.get("/models").json()["data"]),
   "9. Developer points an OpenAI-compatible client at Nexora; /v1/models lists the model")

msg = {"model": "coder", "messages": [{"role": "user", "content": "write add(a, b)"}]}
r = oa.post("/chat/completions", json=msg)
ok(r.status_code == 200 and r.json()["usage"]["total_tokens"] > 0 and r.headers.get("x-nexora-request-id"),
   "10-13. Key authenticated, quota checked, queued, routed to the model, response returned",
   r.text[:200])

chunks, usage_seen = [], False
with oa.stream("POST", "/chat/completions", json={**msg, "stream": True}) as s:
    ok(s.status_code == 200, "14. Streaming request is accepted", str(s.status_code))
    for line in s.iter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            obj = json.loads(line[6:])
            chunks.append(obj)
            usage_seen = usage_seen or bool(obj.get("usage"))
ok(len(chunks) > 3, "    ...response streams back incrementally", f"{len(chunks)} chunks")

me = dev.call("GET", "/usage/me").json()["totals"]
ok(me["requests"] == 2 and me["total_tokens"] > 0, "15-16. Usage recorded; Developer sees consumption in the portal",
   json.dumps(me))
hist = dev.call("GET", "/usage/requests").json()
ok(len(hist) == 2 and all(h["status"] == "completed" for h in hist), "    ...and request history", str(hist)[:200])

rep = admin.call("GET", f"/usage/project/{proj['id']}").json()["totals"]
ok(rep["requests"] == 2, "17. Admin sees the requests in organisation-wide reports", json.dumps(rep))

ok(oa.post("/chat/completions", json=msg).status_code == 200, "    third request still within quota")
r = oa.post("/chat/completions", json=msg)
err = r.json().get("error", {})
ok(r.status_code == 429 and err.get("code") == "quota_exceeded" and "limit" in err.get("message", "")
   and r.headers.get("retry-after"), "18. Over quota: rejected with a clear error and Retry-After",
   r.text[:200])
print(f"      -> {err['message']} (retry in {r.headers['retry-after']}s)")

ok(dev.call("POST", "/keys", json={"name": "x"}).status_code == 201, "Bonus: developer can still manage keys")
audit = admin.call("GET", "/admin/audit-logs?limit=100").json()
acts = {a["action"] for a in audit}
ok({"user.create", "team.create", "project.create", "quota.create", "key.create"} <= acts,
   "Bonus: privileged actions are in the audit log")
print(f"\nAll {step} steps passed (run {RUN}).")
